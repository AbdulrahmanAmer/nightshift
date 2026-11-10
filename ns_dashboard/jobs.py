"""Dashboard job store (table `dashboard_jobs`) and the single FIFO worker thread (component C9)."""

from __future__ import annotations

import dataclasses
import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ns_common.commandline import render
from ns_common.db import new_job_id
from ns_common.errors import NsError
from ns_common.keepawake import keep_awake
from ns_common.logging import NsLogger
from ns_common.textstats import word_count
from ns_dashboard.errors import AdapterUnavailable
from ns_dashboard.events import EventHub
from ns_dashboard.state import LockedDb
from ns_studio.errors import hint_of

KINDS = ("run", "clip", "learn", "verify", "series_part", "apply_voice")
STATUSES = ("queued", "running", "done", "failed", "cancelled")
GPU_KINDS = frozenset({"run", "clip", "verify", "series_part", "apply_voice"})
JOB_OWNER = "dashboard:job"  # the owner id of the worker's heartbeat to the engine (never holds the GPU)
PULSE_S = 60.0  # how often a running job tells the engine it is alive
PAUSED_CODE = "NS-E330"  # the studio stopped a job because its channel was paused (contracts/errors.json)
HELD_PREFIX = "Paused: "  # the message of a queued job that waits because its channel is paused (P47)
#: How often in a row a job the studio stopped for a pause is put back in line although the worker itself does
#: not see the pause (the owner resumed before the worker looked). A person can cause this once per pause; a
#: studio and a worker that disagree about the same profile would do it for ever, so the fourth time it fails.
PAUSE_REQUEUE_LIMIT = 3
REQUEUED, CANCEL_ASKED, GONE = "requeued", "cancel_asked", "gone"  # what `JobStore.requeue` answers
#: Called with a queued job: the plain reason it must not start now (its channel is paused), or None.
Hold = Callable[["JobRecord"], str | None]


class JobCancelled(Exception):
    """Raised inside a handler when the owner cancelled the job (checked between stages)."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def scheduled_text(at: datetime) -> str:
    """The message of a job that waits for its start time (Create > Pick a time)."""
    return f"Scheduled for {at:%a %d %b %H:%M}"


def released_text(rec: JobRecord) -> str:
    """The message of a held job once its channel is resumed: what it read before the hold. A Series Workshop
    part is queued with its title as the message (`params["title"]`, `workshop.make_db_enqueue`); any other
    job with "Queued", as `JobStore.create` writes it."""
    title = rec.params.get("title")
    return title if isinstance(title, str) and title.strip() else "Queued"


def asked_videos(params: dict[str, Any]) -> int:
    """How many videos a stored run request makes: its `count` (1 when unset, and for the owner's own script,
    which is one video whatever the count says)."""
    if params.get("script") and not params.get("resume_job_id"):
        return 1
    try:
        return max(1, int(params.get("count") or 1))
    except (TypeError, ValueError):
        return 1


def owed_after_stop(
    params: dict[str, Any], seen: Iterable[Any], stop: dict[str, Any] | None = None
) -> dict[str, Any]:
    """What a run request that stopped before its end still has to make, as the params that make it:
    `{"resume_job_id": <the half-made studio job, or None>, "count": <videos to make, that one included>}`.

    The ONE place that decides it (P47: every video asked for is made exactly once), read by the worker when
    it puts a paused job back in line and by Retry (`svc_jobs._resume_params`). `seen` are the studio jobs the
    pass that stopped announced, in order (a continued one first). `stop` is the studio's own account when it
    stopped for a pause (`left`, `half_made`: `studio_bridge._owed_on_pause`), which also knows a stop between
    two videos (no half-made one). Without it (an error, a restart, a killed studio process) the last job
    announced counts as half made; when none was announced the pass made nothing and owes what was asked."""
    left = (stop or {}).get("left")
    if isinstance(left, int) and not isinstance(left, bool) and left >= 0:
        half = (stop or {}).get("half_made")
        return {"resume_job_id": str(half) if half else None, "count": left + (1 if half else 0)}
    ids = [str(i) for i in seen if i]
    if ids:
        return {"resume_job_id": ids[-1], "count": 1 + max(0, asked_videos(params) - len(ids))}
    half = params.get("resume_job_id")
    return {"resume_job_id": str(half) if half else None, "count": asked_videos(params)}


def cancelled_text(params: dict[str, Any]) -> str:
    """The message of a job cancelled while it was queued. A request a pause had stopped mid-way did start:
    it says what was made and that the half-made video stays unfinished in its job folder (nothing is
    removed; the folder is the one `studio run --resume <id>` continues)."""
    if params.get("asked") is None:  # set when the worker puts a stopped job back: without it, nothing ran
        return "Cancelled before it started"
    made, half = len(params.get("made_job_ids") or []), half_made_of(params)
    text = f"Cancelled while it waited: {made} of {params['asked']} videos were made"
    if half:
        text += f"; the half-made one ({half}) is not finished and its files stay in its job folder"
    return text + "; no further video of this request is made"


def half_made_of(params: dict[str, Any]) -> str | None:
    """The studio job a waiting request left half made, or None: only a request the worker put back after a
    stop has one (`asked` is set then): the video it continues (`resume_job_id`), or a series part's own job.
    A Retry that has not started yet names a job too, but that one belongs to the card it retries."""
    if params.get("asked") is None:
        return None
    half = params.get("resume_job_id") or params.get("job_id")
    return str(half) if half else None


def _uniq(items: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(str(i) for i in items if i))


@dataclass
class JobRecord:
    """One row of `dashboard_jobs` with `params_json` unpacked into `params` and `result`."""

    job_id: str
    kind: str
    channel: str | None
    status: str
    progress: float
    message: str
    created_at: str
    started_at: str | None
    finished_at: str | None
    params: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    cancel_requested: bool = False
    dismissed_at: str | None = None

    @property
    def schedule_at(self) -> str | None:
        """Start time the owner asked for (ISO 8601 with offset); None means as soon as possible."""
        value = self.params.get("schedule_at")
        return value if isinstance(value, str) and value else None

    def not_before(self) -> datetime | None:
        """`schedule_at` as an aware datetime (None when unset or unreadable, which means run now)."""
        if self.schedule_at is None:
            return None
        try:
            at = datetime.fromisoformat(self.schedule_at)
        except ValueError:
            return None
        return at if at.tzinfo is not None else at.astimezone()

    def failed_stage(self) -> str | None:
        """The stage a failed job stopped at (kept in `result.error.stage`: the message becomes the error)."""
        err = (self.result or {}).get("error")
        stage = err.get("stage") if isinstance(err, dict) else None
        return stage if isinstance(stage, str) and stage else None

    def to_dict(self) -> dict[str, Any]:
        """API shape: `progress` is a percentage (0-100), `stage` the current stage name."""
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "channel": self.channel,
            "status": self.status,
            "progress": round(self.progress * 100, 1),
            "stage": _stage_of(self.message) or self.failed_stage(),
            "step": _steps_of(self.message)[0],
            "steps": _steps_of(self.message)[1],
            "message": render(self.message),     # stored raw; shown as this owner types its commands (FN-3)
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "params": public_params(self.params),
            "result": shown_result(self.result),
            "cancel_requested": self.cancel_requested,
            "schedule_at": self.schedule_at,
            "dismissed_at": self.dismissed_at,
        }


def shown_result(result: Any) -> Any:
    """A job's stored result as the card shows it: the error's hint and message (stored with the plain
    `ns ...` words) given as this install's owner types the commands now (FN-3). Never written back."""
    if not isinstance(result, dict) or not isinstance(result.get("error"), dict):
        return result
    error = {
        k: render(v) if k in ("hint", "message", "detail") and isinstance(v, str) else v
        for k, v in result["error"].items()
    }
    return {**result, "error": error}


def public_params(params: dict[str, Any]) -> dict[str, Any]:
    """The params as the API shows them: the owner's script (up to 30 KB) becomes its word count.

    The job keeps the full text for the studio child; a list of jobs polled every few seconds should not
    carry it each time.
    """
    if "script" not in params:
        return params
    text = str(params.get("script") or "")
    return {**{k: v for k, v in params.items() if k != "script"}, "script_words": word_count(text)}


_STAGE_RE = re.compile(r"^(.*) \((\d+)/(\d+)\)$")
INTERRUPTED_CODE = "NS-E328"
INTERRUPTED_HINT = (
    "Nightshift or the PC restarted while this job ran. Press Retry from where it stopped: the finished "
    "stages are kept. If it happens every night, check the PC's sleep and restart settings."
)


def _stage_of(message: str) -> str | None:
    """Stage name from a `name (i/n)` progress message (or `Downloading NN%` -> `download`), else None."""
    if (message or "").startswith("Downloading"):
        return "download"
    m = _STAGE_RE.match(message or "")
    return m.group(1) if m else None


def _steps_of(message: str) -> tuple[int | None, int | None]:
    """`(i, n)` from a `name (i/n)` progress message, else `(None, None)`."""
    m = _STAGE_RE.match(message or "")
    return (int(m.group(2)), int(m.group(3))) if m else (None, None)


def _dismissed_at(raw: str | None) -> str | None:
    """When the owner hid the job's card (`dismissed_at` in `params_json`), else None."""
    try:
        d = json.loads(raw or "")
    except ValueError:
        return None
    value = d.get("dismissed_at") if isinstance(d, dict) else None
    return value if isinstance(value, str) and value else None


def _unpack(raw: str | None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Split `params_json` into (params, result); tolerates a bare params object."""
    if not raw:
        return {}, None
    try:
        d = json.loads(raw)
    except ValueError:
        return {}, None
    if not isinstance(d, dict):
        return {}, None
    if "params" in d or "result" in d:
        params = d.get("params")
        result = d.get("result")
        return (params if isinstance(params, dict) else {}), (result if isinstance(result, dict) else None)
    return d, None


class JobStore:
    """CRUD over `dashboard_jobs`; cancel requests are tracked in memory."""

    def __init__(
        self, db: LockedDb, *, clock: Callable[[], datetime] = lambda: datetime.now().astimezone()
    ) -> None:
        self.db = db
        self.clock = clock
        self._cancel: set[str] = set()
        self._lock = threading.Lock()

    def _record(self, row: sqlite3.Row) -> JobRecord:
        params, result = _unpack(row["params_json"])
        with self._lock:
            requested = row["job_id"] in self._cancel
        return JobRecord(
            job_id=row["job_id"],
            kind=row["kind"],
            channel=row["channel"],
            status=row["status"],
            progress=float(row["progress"] or 0.0),
            message=row["message"] or "",
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            params=params,
            result=result,
            dismissed_at=_dismissed_at(row["params_json"]),
            cancel_requested=requested and row["status"] == "running",
        )

    def create(self, kind: str, channel: str | None, params: dict[str, Any]) -> JobRecord:
        """Insert a queued job and return it."""
        if kind not in KINDS:
            raise ValueError(f"unknown job kind {kind!r}")
        payload = json.dumps({"params": params, "result": None}, default=str)
        for _ in range(8):
            job_id = new_job_id(kind)
            try:
                self.db.execute(
                    "INSERT INTO dashboard_jobs(job_id, kind, channel, status, progress, message, created_at,"
                    " params_json) VALUES (?, ?, ?, 'queued', 0, 'Queued', ?, ?)",
                    (job_id, kind, channel, _now(), payload),
                )
            except sqlite3.IntegrityError:
                continue
            rec = self.get(job_id)
            assert rec is not None
            return rec
        raise RuntimeError("could not allocate a unique job id")

    def get(self, job_id: str) -> JobRecord | None:
        """The job or None."""
        row = self.db.one("SELECT * FROM dashboard_jobs WHERE job_id = ?", (job_id,))
        return self._record(row) if row else None

    def list_jobs(
        self, status: str | None = None, kind: str | None = None, limit: int = 50
    ) -> list[JobRecord]:
        """Newest first, optionally filtered."""
        where, args = [], []
        if status:
            where.append("status = ?")
            args.append(status)
        if kind:
            where.append("kind = ?")
            args.append(kind)
        sql = "SELECT * FROM dashboard_jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY rowid DESC LIMIT ?"
        args.append(max(1, min(limit, 500)))
        return [self._record(r) for r in self.db.query(sql, args)]

    def _queued(self, exclude: Iterable[str] = ()) -> Iterator[JobRecord]:
        skip = tuple(exclude)
        marks = ",".join("?" for _ in skip)
        extra = f" AND kind NOT IN ({marks})" if skip else ""
        sql = f"SELECT * FROM dashboard_jobs WHERE status = 'queued'{extra} ORDER BY rowid"
        for row in self.db.query(sql, skip):
            yield self._record(row)

    def is_due(self, rec: JobRecord) -> bool:
        """True when `rec` may start now: it has no start time, or the time has come."""
        at = rec.not_before()
        return at is None or at <= self.clock()

    def next_queued(self, exclude: Iterable[str] = (), hold: Hold | None = None) -> JobRecord | None:
        """Oldest queued job that is due (FIFO by insertion order), skipping kinds in `exclude`.

        A job with a future `schedule_at` stays queued and is passed over until its time has come. With
        `hold`, a job it gives a reason for (its channel is paused) is passed over too.
        """
        for r in self._queued(exclude):
            if hold is not None and hold(r) is not None:
                continue
            if self.is_due(r):
                return r
        return None

    def queued_jobs(self) -> list[JobRecord]:
        """Every queued job, oldest first (due or not)."""
        return list(self._queued())

    def scheduled(self) -> list[JobRecord]:
        """Queued jobs that are waiting for a start time, soonest first."""
        waiting = [r for r in self._queued() if not self.is_due(r)]
        return sorted(waiting, key=lambda r: r.not_before() or self.clock())

    def running(self) -> JobRecord | None:
        """The job currently running, if any."""
        row = self.db.one("SELECT * FROM dashboard_jobs WHERE status = 'running' ORDER BY rowid LIMIT 1")
        return self._record(row) if row else None

    def running_all(self) -> list[JobRecord]:
        """Every job whose row says `running` (more than one only after a crash)."""
        rows = self.db.query("SELECT * FROM dashboard_jobs WHERE status = 'running' ORDER BY rowid")
        return [self._record(r) for r in rows]

    def active_ids(self) -> set[str]:
        """Ids of the jobs that are queued or running."""
        rows = self.db.query("SELECT job_id FROM dashboard_jobs WHERE status IN ('queued', 'running')")
        return {str(r["job_id"]) for r in rows}

    def statuses_of(self, kind: str) -> dict[str, str]:
        """Job id -> status of every job of `kind` (the start sweep tells finished work from lost work)."""
        rows = self.db.query("SELECT job_id, status FROM dashboard_jobs WHERE kind = ?", (kind,))
        return {str(r["job_id"]): str(r["status"]) for r in rows}

    def counts(self) -> dict[str, int]:
        """Number of queued and running jobs."""
        out = {"queued": 0, "running": 0}
        for r in self.db.query("SELECT status, COUNT(*) AS n FROM dashboard_jobs GROUP BY status"):
            if r["status"] in out:
                out[r["status"]] = int(r["n"])
        return out

    def mark_running(self, job_id: str) -> bool:
        """queued -> running; False when the job was cancelled in the meantime."""
        n = self.db.execute(
            "UPDATE dashboard_jobs SET status = 'running', started_at = ?, message = 'Starting'"
            " WHERE job_id = ? AND status = 'queued'",
            (_now(), job_id),
        )
        return n == 1

    def update_progress(self, job_id: str, progress: float, message: str | None) -> None:
        """Write progress (clamped to 0..1) and, when given, the message."""
        p = max(0.0, min(1.0, float(progress)))
        if message is None:
            self.db.execute("UPDATE dashboard_jobs SET progress = ? WHERE job_id = ?", (p, job_id))
        else:
            self.db.execute(
                "UPDATE dashboard_jobs SET progress = ?, message = ? WHERE job_id = ?", (p, message, job_id)
            )

    def set_queued_message(self, job_id: str, message: str) -> bool:
        """Write the message of a job only while it is still `queued`; True when it was written.

        The hold text is written from a snapshot taken a moment earlier, on the request thread (Channels pause
        and resume) or the worker's; the job may have started or been cancelled since, and then its own
        message and progress must stay (one UPDATE with the status in the WHERE, no read-then-write)."""
        changed = self.db.execute(
            "UPDATE dashboard_jobs SET progress = 0.0, message = ? WHERE job_id = ? AND status = 'queued'",
            (message, job_id),
        )
        return changed > 0

    def requeue(
        self, job_id: str, message: str, params_update: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
    ) -> str:
        """running -> queued with `message`, as if it had never started, its place in the queue (insertion
        order) kept. Answers `REQUEUED`; `GONE` when it is no longer running; `CANCEL_ASKED` (nothing
        changed) when the owner pressed Cancel on it: a Cancel always wins over putting a job back (P47).

        Without `params_update` the row is otherwise untouched (a job that waited at the GPU gate and found
        its channel paused). With it (a job the studio stopped for a pause, NS-E330) the params are merged and
        the result is replaced by `result`: what the request made so far, so the waiting card lists it.

        The Cancel flag and the status are read inside one transaction, and `JobWorker.cancel` looks at the
        row again after it set the flag, so one of the two always sees the other."""
        with self.db.tx():
            if self.is_cancel_requested(job_id):
                return CANCEL_ASKED
            row = self.db.one("SELECT status, params_json FROM dashboard_jobs WHERE job_id = ?", (job_id,))
            if row is None or row["status"] != "running":
                return GONE
            payload = row["params_json"]
            if params_update is not None:
                params, _old = _unpack(payload)
                payload = json.dumps(
                    {"params": {**params, **params_update}, "result": result,
                     "dismissed_at": _dismissed_at(payload)},
                    default=str,
                )
            self.db.execute(
                "UPDATE dashboard_jobs SET status = 'queued', started_at = NULL, progress = 0.0, message = ?,"
                " params_json = ? WHERE job_id = ? AND status = 'running'",
                (message, payload, job_id),
            )
        return REQUEUED

    def outputs_of(self, studio_job_ids: Iterable[str]) -> list[str]:
        """Ids of the `outputs` rows of these studio jobs, in their order (as `studio_bridge.output_ids`)."""
        out: list[str] = []
        for job_id in studio_job_ids:
            try:
                rows = self.db.query(
                    "SELECT output_id FROM outputs WHERE job_id = ? ORDER BY created_at, output_id", (job_id,)
                )
            except sqlite3.Error:  # the list is a courtesy of the card: the videos are in the Queue anyway
                continue
            out.extend(str(r["output_id"]) for r in rows)
        return out

    def finish(self, job_id: str, status: str, message: str, result: dict[str, Any] | None = None) -> None:
        """Terminal state; merges `result` into `params_json`."""
        with self.db.tx():
            row = self.db.one("SELECT progress, params_json FROM dashboard_jobs WHERE job_id = ?", (job_id,))
            if row is None:
                return
            params, old = _unpack(row["params_json"])
            merged = result if result is not None else old
            payload = json.dumps(
                {"params": params, "result": merged, "dismissed_at": _dismissed_at(row["params_json"])},
                default=str,
            )
            progress = 1.0 if status == "done" else float(row["progress"] or 0.0)
            self.db.execute(
                "UPDATE dashboard_jobs SET status = ?, message = ?, progress = ?, finished_at = ?,"
                " params_json = ? WHERE job_id = ?",
                (status, message, progress, _now(), payload, job_id),
            )

    def dismiss(self, job_id: str) -> JobRecord | None:
        """Hide a finished job's card (the Create page's Earlier list); the row and its outputs stay.

        None when the job is unknown; a job that is still queued or running is not changed (the card is
        where it is cancelled from). Returns the record either way so the caller can say what happened.
        """
        with self.db.tx():
            row = self.db.one("SELECT status, params_json FROM dashboard_jobs WHERE job_id = ?", (job_id,))
            if row is None:
                return None
            if row["status"] in ("queued", "running"):
                return self.get(job_id)
            params, result = _unpack(row["params_json"])
            payload = json.dumps({"params": params, "result": result, "dismissed_at": _now()}, default=str)
            self.db.execute("UPDATE dashboard_jobs SET params_json = ? WHERE job_id = ?", (payload, job_id))
        return self.get(job_id)

    def cancel_queued(self, job_id: str) -> bool:
        """Cancel a job that is queued: not started, or put back in line by a pause (`cancelled_text` says
        which; the result then names the half-made studio job, whose folder is left as it is)."""
        with self.db.tx():
            row = self.db.one("SELECT status, params_json FROM dashboard_jobs WHERE job_id = ?", (job_id,))
            if row is None or row["status"] != "queued":
                return False
            payload = row["params_json"]
            params, result = _unpack(payload)
            half = half_made_of(params)
            if half:
                ids = _uniq([*((result or {}).get("studio_job_ids") or []), half])
                payload = json.dumps(
                    {"params": params, "result": {**(result or {}), "studio_job_ids": ids, "half_made": half},
                     "dismissed_at": _dismissed_at(payload)},
                    default=str,
                )
            self.db.execute(
                "UPDATE dashboard_jobs SET status = 'cancelled', message = ?, finished_at = ?,"
                " params_json = ? WHERE job_id = ? AND status = 'queued'",
                (cancelled_text(params), _now(), payload, job_id),
            )
        return True

    def mark_cancelling(self, job_id: str) -> bool:
        """Say on a RUNNING job that it stops after its stage; False (nothing written) when it is not running
        any more, so the text never lands on a job that was put back in line meanwhile."""
        n = self.db.execute(
            "UPDATE dashboard_jobs SET message = 'Cancelling after the current stage...'"
            " WHERE job_id = ? AND status = 'running'",
            (job_id,),
        )
        return n == 1

    def request_cancel(self, job_id: str) -> None:
        """Flag a running job; the handler notices at its next stage boundary."""
        with self._lock:
            self._cancel.add(job_id)

    def is_cancel_requested(self, job_id: str) -> bool:
        """True when `request_cancel` was called for `job_id`."""
        with self._lock:
            return job_id in self._cancel

    def clear_cancel(self, job_id: str) -> None:
        """Forget the flag once the job ended."""
        with self._lock:
            self._cancel.discard(job_id)

    def note_result(self, job_id: str, fields: dict[str, Any]) -> None:
        """Merge `fields` into the job's result while it runs (the studio job ids as they are announced).

        A job killed mid-run (the PC restarted, the engine was stopped) used to lose them: `finish` never ran,
        so `recover_interrupted` left a row with no studio job and the Retry button had nothing to continue.
        """
        with self.db.tx():
            row = self.db.one("SELECT params_json FROM dashboard_jobs WHERE job_id = ?", (job_id,))
            if row is None:
                return
            params, old = _unpack(row["params_json"])
            payload = json.dumps(
                {"params": params, "result": {**(old or {}), **fields},
                 "dismissed_at": _dismissed_at(row["params_json"])},
                default=str,
            )
            self.db.execute("UPDATE dashboard_jobs SET params_json = ? WHERE job_id = ?", (payload, job_id))

    def recover_interrupted(self) -> int:
        """Fail jobs left `running` by a previous process; returns how many.

        The stage the job was at (from its last message, "voice (10/16)") and the studio job ids it
        announced (`note_result`) are kept in `result.error.stage` and `result.studio_job_ids`, so the card
        says where it stopped and Retry can continue it.
        """
        rows = list(self.db.query(
            "SELECT job_id, message, params_json FROM dashboard_jobs WHERE status = 'running'"
        ))
        for row in rows:
            params, old = _unpack(row["params_json"])
            stage = _stage_of(row["message"] or "")
            error = {"code": INTERRUPTED_CODE, "hint": INTERRUPTED_HINT, "stage": stage}
            result = {**(old or {}), "error": error}
            payload = json.dumps(
                {"params": params, "result": result, "dismissed_at": _dismissed_at(row["params_json"])},
                default=str,
            )
            self.db.execute(
                "UPDATE dashboard_jobs SET status = 'failed', message = 'Interrupted by a restart',"
                " finished_at = ?, params_json = ? WHERE job_id = ?",
                (_now(), payload, row["job_id"]),
            )
        return len(rows)


class JobControl:
    """What a handler receives: parameters, progress reporting and cancel checks."""

    def __init__(self, record: JobRecord, store: JobStore, hub: EventHub) -> None:
        self.job_id = record.job_id
        self.kind = record.kind
        self.channel = record.channel
        self.params = record.params
        self._store = store
        self._hub = hub
        self._progress = 0.0
        self.extras: dict[str, Any] = {}  # extra result fields the handler merges in

    def _publish(self) -> None:
        rec = self._store.get(self.job_id)
        if rec is not None:
            self._hub.publish("dashboard_job", rec.to_dict())

    def progress(self, fraction: float, message: str | None = None) -> None:
        """Record progress (0..1) and an optional message, and notify the UI."""
        self._progress = max(0.0, min(1.0, float(fraction)))
        self._store.update_progress(self.job_id, self._progress, message)
        self._publish()

    def note(self, **fields: Any) -> None:
        """Record result fields now (not only at the end), so they survive a restart mid-job."""
        self.extras.update(fields)
        self._store.note_result(self.job_id, fields)

    def download(self, percent: float) -> None:
        """Report download progress: stage `download`, message `Downloading 42%`."""
        pct = int(max(0, min(100, percent)))
        self.check_cancel()
        self.progress(pct / 100, f"Downloading {pct}%")

    def waiting(self, message: str) -> None:
        """Show a waiting message without changing the progress value."""
        self.progress(self._progress, message)

    def cancelled(self) -> bool:
        """True once the owner asked to cancel."""
        return self._store.is_cancel_requested(self.job_id)

    def check_cancel(self) -> None:
        """Raise `JobCancelled` when cancellation was requested."""
        if self.cancelled():
            raise JobCancelled(self.job_id)

    def stages(self, names: Iterable[str]) -> Iterator[str]:
        """Iterate stage names; checks the cancel flag and records progress before each stage."""
        items = list(names)
        total = max(1, len(items))
        for i, name in enumerate(items):
            self.check_cancel()
            self.progress(i / total, f"{name} ({i + 1}/{len(items)})")
            yield name


Handler = Callable[[JobControl], dict[str, Any] | None]
GpuGate = Callable[[JobControl], None]


def make_gpu_gate(
    status_fn: Callable[[], dict[str, Any] | None],
    *,
    sleeper: Callable[[float], None] = time.sleep,
    poll_s: float = 2.0,
    max_wait_s: float = 7200.0,
) -> GpuGate:
    """Gate that waits while the supervisor reports a GPU owner (nightly run, Nightshift Code).

    A missing supervisor does not block (the adapter then fails with its own message). Gives up
    after `max_wait_s` with NS-E201.
    """

    def gate(ctl: JobControl) -> None:
        waited = 0.0
        while True:
            ctl.check_cancel()
            status = status_fn()
            owner = (status or {}).get("gpu_owner")
            if not owner:
                return
            if waited >= max_wait_s:
                raise NsError("NS-E201", f"GPU still held by {owner} after {int(waited)} s")
            ctl.waiting(f"Waiting for GPU (held by {owner})")
            sleeper(poll_s)
            waited += poll_s

    return gate


class JobWorker:
    """Runs queued jobs one at a time, oldest first, on a single background thread."""

    def __init__(
        self,
        store: JobStore,
        handlers: dict[str, Handler],
        hub: EventHub,
        log: NsLogger,
        *,
        gpu_gate: GpuGate | None = None,
        inline_kinds: frozenset[str] = frozenset({"learn"}),
        on_recover: dict[str, Callable[[JobRecord], None]] | None = None,
        on_end: dict[str, Callable[[JobRecord], None]] | None = None,
        on_done: dict[str, Callable[[JobRecord], None]] | None = None,
        sweep: Callable[[set[str], dict[str, str]], object] | None = None,
        hold: Hold | None = None,
        recover_studio: Callable[[], object] | None = None,
        idle_poll_s: float = 30.0,
        awake: Callable[[], AbstractContextManager[None]] = keep_awake,
        pulse: Callable[[], object] | None = None,
        pulse_s: float = PULSE_S,
    ) -> None:
        self.store = store
        self.handlers = handlers
        self.hub = hub
        self.log = log
        self.gpu_gate = gpu_gate
        self.inline_kinds = inline_kinds
        self.on_recover = on_recover or {}
        #: Per kind: called when a job of that kind ends without finishing (cancelled in the queue, cancelled
        #: or failed before or inside its handler), so work it had reserved (a series part) is given back.
        self.on_end = on_end or {}
        #: Per kind: called when a job of that kind finished (a series part becomes `produced`).
        self.on_done = on_done or {}
        #: Called once at start with the ids of the queued and running jobs, after interrupted ones failed.
        self.sweep = sweep
        #: Asked about every queued job before it may start: the reason it must wait (its channel is paused),
        #: or None. A held job stays `queued`, its message says why, and it starts once the reason is gone.
        self.hold = hold
        #: Called once at start: the studio runs and jobs whose process is gone (a nightly killed by a power
        #: cut or a crash) are closed as interrupted and offered in the Queue with Retry (defect D-31).
        self.recover_studio = recover_studio
        #: While nothing runs, `on_idle` is also called this often (a request saved during the nightly run,
        #: which is not a dashboard job, has no job end to wait for).
        self.idle_poll_s = idle_poll_s
        #: Held on the thread that runs a job, for the length of the job: the PC must not sleep under it.
        self.awake = awake
        #: Called every `pulse_s` seconds while a job runs (tells the engine the dashboard is working, so its
        #: idle exit does not stop the service under a download or a render).
        self.pulse = pulse
        self.pulse_s = pulse_s
        #: Called on the worker thread when a job ended and nothing else is queued (saved Assistant requests).
        self.on_idle: Callable[[], None] | None = None
        self._ended = False
        self._cv = threading.Condition()
        self._wake = False
        self._stopping = False
        self._exited = False
        self._thread: threading.Thread | None = None
        self._running_id: str | None = None
        self._running_ctl: JobControl | None = None

    # --- control ---
    def start(self) -> None:
        """Start the worker thread (idempotent) after failing jobs a crashed process left running."""
        t = self._thread
        if t is not None and t.is_alive():
            if self._stopping:  # stop() timed out (thread in a long answer): carry on, never start a second
                with self._cv:
                    if not self._exited:
                        self._stopping = False
                        return
                t.join(2.0)
                if t.is_alive():
                    return
            else:
                return
        interrupted = self.store.running_all()
        n = self.store.recover_interrupted()
        if n:
            self.log.warn("jobs_recovered", f"{n} interrupted job(s) marked failed", count=n)
        for rec in interrupted:  # e.g. a series part goes back from "producing" to "approved"
            hook = self.on_recover.get(rec.kind)
            if hook is not None:
                try:
                    hook(rec)
                except Exception as e:  # recovery must never stop the worker from starting
                    self.log.warn("recover_failed", str(e), job_id=rec.job_id, kind=rec.kind)
        if self.recover_studio is not None:
            try:
                self.recover_studio()
            except Exception as e:  # recovery must never stop the worker from starting
                self.log.warn("studio_recover_failed", str(e))
        if self.sweep is not None:
            try:
                self.sweep(self.store.active_ids(), self.store.statuses_of("series_part"))
            except Exception as e:  # a sweep that fails must never stop the worker from starting
                self.log.warn("sweep_failed", str(e))
        self._stopping = False
        self._exited = False
        self._thread = threading.Thread(target=self._loop, name="ns-dashboard-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> bool:
        """Ask the thread to exit and wait up to `timeout` seconds; True when it has ended.

        A thread that is still inside a job or an Assistant answer is kept (not forgotten), so `start()` can
        resume it and there are never two workers.
        """
        with self._cv:
            self._stopping = True
            self._cv.notify_all()
        t = self._thread
        if t is not None:
            t.join(timeout)
            if t.is_alive():
                return False
        self._thread = None
        return True

    def enqueue(self, job_id: str) -> None:
        """Wake the worker; the job must already be in the store."""
        self.log.info("job_queued", job_id=job_id)
        with self._cv:
            self._wake = True
            self._cv.notify_all()

    def cancel(self, job_id: str) -> str:
        """Cancel a job: queued -> `cancelled` now; running -> `cancelling` until its stage ends.

        Returns the resulting state, or the job's terminal status when it already ended.
        Raises LookupError for an unknown id.
        """
        rec = self.store.get(job_id)
        if rec is None:
            raise LookupError(job_id)
        # The job can change under us: the worker starts it (queued -> running) or puts it back because its
        # channel is paused (running -> queued, `JobStore.requeue`). Each turn acts on what the row says and
        # looks again, so a Cancel is never left on a job that went back to waiting (P47).
        for _ in range(4):
            if rec.status == "queued":
                if self.store.cancel_queued(job_id):
                    self.store.clear_cancel(job_id)
                    self.log.info("job_cancelled", job_id=job_id)
                    self._end_hook(rec)
                    self._publish(job_id)
                    return "cancelled"
            elif rec.status == "running":
                self.store.request_cancel(job_id)
                if self.store.mark_cancelling(job_id):
                    self._publish(job_id)
                    return "cancelling"
                self.store.clear_cancel(job_id)  # it ended or was put back before the flag: act on the row
            else:
                break
            rec = self.store.get(job_id) or rec
        refreshed = self.store.get(job_id)
        return refreshed.status if refreshed else rec.status

    def cancel_running(self) -> str | None:
        """Cancel whatever runs now (Quit Nightshift); returns the job id or None."""
        rec = self.store.running()
        if rec is None:
            return None
        self.cancel(rec.job_id)
        return rec.job_id

    @property
    def started(self) -> bool:
        """True while the worker thread is alive."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def running_job_id(self) -> str | None:
        """Id of the job being executed by this worker."""
        return self._running_id

    @property
    def running_control(self) -> JobControl | None:
        """The `JobControl` of the job being executed (its `extras` name the studio jobs it announced)."""
        return self._running_ctl

    def wait_idle(self, timeout: float) -> bool:
        """Block until nothing is queued or running (thread mode); False on timeout."""
        with self._cv:
            return self._cv.wait_for(self._is_idle, timeout)

    def _is_idle(self) -> bool:
        return self._running_id is None and self.store.next_queued(hold=self._held) is None

    # --- holding a paused channel's jobs ---
    def _hold_reason(self, rec: JobRecord) -> str | None:
        """The hold's answer for `rec`; a hold that fails to answer is logged and counts as no hold."""
        if self.hold is None:
            return None
        try:
            return self.hold(rec)
        except Exception as e:  # a profile that cannot be read must not stop the queue: run as before
            self.log.warn("hold_check_failed", str(e), job_id=rec.job_id)
            return None

    def _requeue_if_held(self, rec: JobRecord, ctl: JobControl) -> bool:
        """The job holds the GPU gate: ask the hold once more (the gate can wait for hours, and a pause may
        have come meanwhile). When it says wait, the job goes back to `queued` with the reason and True is
        returned: nothing of it runs. A Cancel pressed meanwhile still wins (it raises `JobCancelled`)."""
        reason = self._hold_reason(rec)
        if reason is None:
            return False
        outcome = self.store.requeue(rec.job_id, reason)
        if outcome == CANCEL_ASKED:
            raise JobCancelled(rec.job_id)
        if outcome == REQUEUED:
            self.log.info("job_held_after_gate", job_id=rec.job_id, channel=rec.channel, reason=reason)
        return True

    def _requeue_paused(self, rec: JobRecord, ctl: JobControl, stop: dict[str, Any]) -> bool:
        """The studio stopped the job with NS-E330 (its channel was paused while it ran): put it back in line
        with everything the request still owes (`owed_after_stop`: the half-made video, continued from its
        stage, and the videos after it), and remember what it made so far (`made_job_ids`), so the finished
        card lists every video of the request. True when it was put back.

        When the worker sees the pause too, the job waits, held, and continues by itself after the resume.
        When it does not (the owner resumed before the worker looked), nothing is lost either: the job goes
        back as plain "Queued" and continues in its turn. A person can cause that once per pause; a studio
        that keeps reporting a pause the worker cannot see would go round for ever, so after
        `PAUSE_REQUEUE_LIMIT` such turns in a row the answer is False and the job fails with NS-E330 (its
        Retry still covers everything owed). False also for a worker with no hold at all, which cannot hold
        anything. A Cancel pressed meanwhile wins: `JobCancelled`. The kind's end hook is not called: a
        series part stays with its job and its job folder."""
        if self.hold is None or rec.kind not in ("run", "series_part"):
            return False
        update: dict[str, Any] = {"asked": 1}  # a series part is one video, half made in its own job folder
        made = _uniq(rec.params.get("made_job_ids") or [])
        if rec.kind == "run":
            seen = _uniq(ctl.extras.get("studio_job_ids") or [])
            owed = owed_after_stop(rec.params, seen, stop)
            if owed["count"] < 1:
                return False
            made = _uniq([*made, *(i for i in seen if i != owed["resume_job_id"])])
            asked = rec.params.get("asked") or asked_videos(rec.params)
            update = {**owed, "made_job_ids": made, "asked": asked}
        later = dataclasses.replace(rec, params={**rec.params, **update})
        reason = self._hold_reason(later)  # the message says what the request owes, from its new params
        turns = int(rec.params.get("pause_requeues") or 0)
        if reason is None and turns >= PAUSE_REQUEUE_LIMIT:
            return False
        update["pause_requeues"] = 0 if reason is not None else turns + 1
        result = {"studio_job_ids": made, "output_ids": self.store.outputs_of(made)} if made else None
        outcome = self.store.requeue(rec.job_id, reason or self._released_text(later), update, result)
        if outcome == CANCEL_ASKED:
            raise JobCancelled(rec.job_id)
        if outcome == REQUEUED:
            event = "job_held_mid_way" if reason is not None else "job_requeued_after_pause"
            self.log.info(event, job_id=rec.job_id, channel=rec.channel, reason=reason, **update)
        return True

    def _released_text(self, rec: JobRecord) -> str:
        """The message of a queued job that no pause holds: its set time while that is ahead, else what it
        read when it was queued (`released_text`)."""
        at = rec.not_before()
        return scheduled_text(at) if at is not None and not self.store.is_due(rec) else released_text(rec)

    def _held(self, rec: JobRecord) -> str | None:
        """Why `rec` must not start (None: it may). Keeps the job's message true: the reason while held,
        the usual "Queued" / "Scheduled for ..." again once the reason is gone."""
        reason = self._hold_reason(rec)
        if reason is not None:
            if rec.message != reason and self.store.set_queued_message(rec.job_id, reason):
                self.log.info("job_held", job_id=rec.job_id, channel=rec.channel, reason=reason)
                self._publish(rec.job_id)
        elif rec.message.startswith(HELD_PREFIX):
            if self.store.set_queued_message(rec.job_id, self._released_text(rec)):
                self.log.info("job_released", job_id=rec.job_id, channel=rec.channel)
                self._publish(rec.job_id)
        return reason

    def refresh_holds(self) -> None:
        """Bring the message of every queued job up to date with its channel (called when a channel is
        paused or resumed, so the Queue is right at once, also while another job runs)."""
        if self.hold is None:
            return
        for rec in self.store.queued_jobs():
            self._held(rec)

    # --- execution ---
    def _publish(self, job_id: str) -> None:
        rec = self.store.get(job_id)
        if rec is not None:
            self.hub.publish("dashboard_job", rec.to_dict())

    def run_next(self) -> bool:
        """Run exactly one queued job to completion; False when the queue is empty."""
        rec = self.store.next_queued(self.inline_kinds, hold=self._held)
        if rec is None:
            return False
        self._execute(rec)
        return True

    def run_inline(self, job_id: str) -> JobRecord:
        """Run one specific queued job on the calling thread (used for the quick, GPU-free `learn` import).

        Returns the finished record. Raises LookupError for an unknown id.
        """
        rec = self.store.get(job_id)
        if rec is None:
            raise LookupError(job_id)
        if rec.status == "queued":
            self._execute(rec, track=False)
        done = self.store.get(job_id)
        assert done is not None
        return done

    def _pulse_loop(self, stop: threading.Event) -> None:
        """Call `pulse` now and then until `stop`; a failing pulse is only logged."""
        pulse = self.pulse
        while pulse is not None and not stop.is_set():
            try:
                pulse()
            except Exception as e:  # the engine may be restarting; the next pulse will tell
                self.log.debug("pulse_failed", str(e))
            stop.wait(self.pulse_s)

    def _execute(self, rec: JobRecord, *, track: bool = True) -> None:
        """Run `rec` (see `_run_job`) with the PC held awake and the engine told that a job is running."""
        stop = threading.Event()
        pulser: threading.Thread | None = None
        if self.pulse is not None:
            pulser = threading.Thread(target=self._pulse_loop, args=(stop,), name="ns-job-pulse", daemon=True)
            pulser.start()
        try:
            with self.awake():
                self._run_job(rec, track=track)
        finally:
            stop.set()
            if pulser is not None:
                pulser.join(1.0)

    def _run_job(self, rec: JobRecord, *, track: bool = True) -> None:
        """Mark `rec` running, call its handler and record the outcome."""
        if track:
            self._running_id = rec.job_id
        if not self.store.mark_running(rec.job_id):
            if track:
                self._running_id = None
            return
        self._publish(rec.job_id)
        ctl = JobControl(rec, self.store, self.hub)
        if track:
            self._running_ctl = ctl
        self.log.info("job_start", job_id=rec.job_id, kind=rec.kind, channel=rec.channel)
        try:
            handler = self.handlers.get(rec.kind)
            if handler is None:
                raise RuntimeError(f"no handler for job kind {rec.kind!r}")
            if self.gpu_gate is not None and rec.kind in GPU_KINDS:
                self.gpu_gate(ctl)
            if self._requeue_if_held(rec, ctl):  # paused while it waited: no video, no hooks, back in line
                return
            result = handler(ctl) or {}
            self.store.finish(rec.job_id, "done", "Done", self._whole(rec, result))
            self.log.info("job_done", job_id=rec.job_id, kind=rec.kind)
            self._done_hook(rec)
        except JobCancelled:
            self._end_cancelled(rec, ctl)
        except AdapterUnavailable as e:  # a collaborator that has not landed yet: say so plainly
            failure = self._whole(rec, self._failure(ctl, "unavailable", None))
            self.store.finish(rec.job_id, "failed", str(e), failure)
            self.log.warn("job_unavailable", str(e), job_id=rec.job_id)
            self._end_hook(rec)
        except NsError as e:
            if e.code == PAUSED_CODE:
                try:
                    if self._requeue_paused(rec, ctl, e.data):
                        return  # stopped by a pause: back in line with all it owes, it continues by itself
                except JobCancelled:
                    self._end_cancelled(rec, ctl)
                    return
            failure = self._failure(ctl, e.code, hint_of(e))
            if e.code == PAUSED_CODE:  # the studio's account of the stop: Retry makes exactly what is owed
                failure["stop"] = {k: e.data[k] for k in ("left", "half_made") if k in e.data}
            self.store.finish(rec.job_id, "failed", str(e), self._whole(rec, failure))
            self.log.error("job_failed", str(e), job_id=rec.job_id, code=e.code)
            self._end_hook(rec)
        except Exception as e:  # the worker thread must survive any handler failure
            msg = f"{type(e).__name__}: {e}"
            failure = self._whole(rec, self._failure(ctl, "job_failed", None))
            self.store.finish(rec.job_id, "failed", msg, failure)
            self.log.error("job_failed", msg, job_id=rec.job_id)
            self._end_hook(rec)
        finally:
            self.store.clear_cancel(rec.job_id)
            if track:
                self._running_ctl = None
                self._running_id = None
            self._publish(rec.job_id)
            with self._cv:
                self._cv.notify_all()

    def _end_cancelled(self, rec: JobRecord, ctl: JobControl) -> None:
        """The owner cancelled the running job: record it, with what it had made, and give its work back."""
        self.store.finish(rec.job_id, "cancelled", "Cancelled", self._whole(rec, dict(ctl.extras)) or None)
        self.log.info("job_cancelled", job_id=rec.job_id)
        self._end_hook(rec)

    def _whole(self, rec: JobRecord, result: dict[str, Any]) -> dict[str, Any]:
        """`result` with the videos the request made in the passes before a pause put it back in line
        (`made_job_ids`), first: a request's card lists every video it made, however it ended (P47)."""
        made = _uniq(rec.params.get("made_job_ids") or [])
        if not made:
            return result
        return {
            **result,
            "studio_job_ids": _uniq([*made, *(result.get("studio_job_ids") or [])]),
            "output_ids": _uniq([*self.store.outputs_of(made), *(result.get("output_ids") or [])]),
        }

    def _done_hook(self, rec: JobRecord) -> None:
        """Run the `on_done` hook of the job's kind; it must never break the worker."""
        hook = self.on_done.get(rec.kind)
        if hook is None:
            return
        try:
            hook(rec)
        except Exception as e:
            self.log.warn("done_hook_failed", str(e), job_id=rec.job_id, kind=rec.kind)

    def _end_hook(self, rec: JobRecord) -> None:
        """Run the `on_end` hook of the job's kind; it must never break the worker."""
        hook = self.on_end.get(rec.kind)
        if hook is None:
            return
        try:
            hook(rec)
        except Exception as e:
            self.log.warn("end_hook_failed", str(e), job_id=rec.job_id, kind=rec.kind)

    def _failure(self, ctl: JobControl, code: str, hint: str | None) -> dict[str, Any]:
        """Result of a failed job: the error with the stage it stopped at, plus what the handler had noted
        (the studio job ids, so the logs of the failed run can be found)."""
        rec = self.store.get(ctl.job_id)
        stage = _stage_of(rec.message) if rec is not None else None
        return {**ctl.extras, "error": {"code": code, "hint": hint, "stage": stage}}

    def _idle(self) -> None:
        if self.on_idle is None:
            return
        try:
            self.on_idle()
        except Exception as e:  # an answer that fails must not stop the worker
            self.log.warn("idle_hook_failed", str(e))

    def _loop(self) -> None:
        try:
            self._run_loop()
        finally:
            with self._cv:
                self._exited = True

    def _run_loop(self) -> None:
        next_poll = time.monotonic() + self.idle_poll_s
        while True:
            with self._cv:  # the decision to leave is taken under the lock `start()` uses to resume us
                if self._stopping:
                    self._exited = True
                    return
            if self.run_next():
                self._ended = True
                continue
            if self._ended:
                self._ended = False
                self._idle()
                continue
            if self.on_idle is not None and time.monotonic() >= next_poll:
                next_poll = time.monotonic() + self.idle_poll_s
                self._idle()
                continue
            with self._cv:
                if self._stopping:
                    continue
                self._cv.wait_for(lambda: self._wake or self._stopping, timeout=1.0)
                self._wake = False
