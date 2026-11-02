"""Dashboard job store (table `dashboard_jobs`) and the single FIFO worker thread (component C9)."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ns_common.db import new_job_id
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_dashboard.errors import AdapterUnavailable
from ns_dashboard.events import EventHub
from ns_dashboard.state import LockedDb

KINDS = ("run", "clip", "learn", "verify")
STATUSES = ("queued", "running", "done", "failed", "cancelled")
GPU_KINDS = frozenset({"run", "clip", "verify"})


class JobCancelled(Exception):
    """Raised inside a handler when the owner cancelled the job (checked between stages)."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


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

    def to_dict(self) -> dict[str, Any]:
        """API shape: `progress` is a percentage (0-100), `stage` the current stage name."""
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "channel": self.channel,
            "status": self.status,
            "progress": round(self.progress * 100, 1),
            "stage": _stage_of(self.message),
            "message": self.message,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "params": self.params,
            "result": self.result,
            "cancel_requested": self.cancel_requested,
            "schedule_at": self.schedule_at,
        }


_STAGE_RE = re.compile(r"^(.*) \(\d+/\d+\)$")


def _stage_of(message: str) -> str | None:
    """Stage name from a `name (i/n)` progress message (or `Downloading NN%` -> `download`), else None."""
    if (message or "").startswith("Downloading"):
        return "download"
    m = _STAGE_RE.match(message or "")
    return m.group(1) if m else None


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

    def next_queued(self, exclude: Iterable[str] = ()) -> JobRecord | None:
        """Oldest queued job that is due (FIFO by insertion order), skipping kinds in `exclude`.

        A job with a future `schedule_at` stays queued and is passed over until its time has come.
        """
        return next((r for r in self._queued(exclude) if self.is_due(r)), None)

    def scheduled(self) -> list[JobRecord]:
        """Queued jobs that are waiting for a start time, soonest first."""
        waiting = [r for r in self._queued() if not self.is_due(r)]
        return sorted(waiting, key=lambda r: r.not_before() or self.clock())

    def running(self) -> JobRecord | None:
        """The job currently running, if any."""
        row = self.db.one("SELECT * FROM dashboard_jobs WHERE status = 'running' ORDER BY rowid LIMIT 1")
        return self._record(row) if row else None

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

    def finish(self, job_id: str, status: str, message: str, result: dict[str, Any] | None = None) -> None:
        """Terminal state; merges `result` into `params_json`."""
        with self.db.tx():
            row = self.db.one("SELECT progress, params_json FROM dashboard_jobs WHERE job_id = ?", (job_id,))
            if row is None:
                return
            params, old = _unpack(row["params_json"])
            merged = result if result is not None else old
            payload = json.dumps({"params": params, "result": merged}, default=str)
            progress = 1.0 if status == "done" else float(row["progress"] or 0.0)
            self.db.execute(
                "UPDATE dashboard_jobs SET status = ?, message = ?, progress = ?, finished_at = ?,"
                " params_json = ? WHERE job_id = ?",
                (status, message, progress, _now(), payload, job_id),
            )

    def cancel_queued(self, job_id: str) -> bool:
        """Cancel a job that has not started."""
        n = self.db.execute(
            "UPDATE dashboard_jobs SET status = 'cancelled', message = 'Cancelled before it started',"
            " finished_at = ? WHERE job_id = ? AND status = 'queued'",
            (_now(), job_id),
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

    def recover_interrupted(self) -> int:
        """Fail jobs left `running` by a previous process; returns how many."""
        return self.db.execute(
            "UPDATE dashboard_jobs SET status = 'failed', message = 'Interrupted by a restart',"
            " finished_at = ? WHERE status = 'running'",
            (_now(),),
        )


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

    def _publish(self) -> None:
        rec = self._store.get(self.job_id)
        if rec is not None:
            self._hub.publish("dashboard_job", rec.to_dict())

    def progress(self, fraction: float, message: str | None = None) -> None:
        """Record progress (0..1) and an optional message, and notify the UI."""
        self._progress = max(0.0, min(1.0, float(fraction)))
        self._store.update_progress(self.job_id, self._progress, message)
        self._publish()

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
    ) -> None:
        self.store = store
        self.handlers = handlers
        self.hub = hub
        self.log = log
        self.gpu_gate = gpu_gate
        self.inline_kinds = inline_kinds
        self._cv = threading.Condition()
        self._wake = False
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._running_id: str | None = None

    # --- control ---
    def start(self) -> None:
        """Start the worker thread (idempotent) after failing jobs a crashed process left running."""
        if self._thread is not None and self._thread.is_alive():
            return
        n = self.store.recover_interrupted()
        if n:
            self.log.warn("jobs_recovered", f"{n} interrupted job(s) marked failed", count=n)
        self._stopping = False
        self._thread = threading.Thread(target=self._loop, name="ns-dashboard-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Ask the thread to exit and wait up to `timeout` seconds."""
        with self._cv:
            self._stopping = True
            self._cv.notify_all()
        t = self._thread
        if t is not None:
            t.join(timeout)
        self._thread = None

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
        if rec.status == "queued" and self.store.cancel_queued(job_id):
            self.log.info("job_cancelled", job_id=job_id)
            self._publish(job_id)
            return "cancelled"
        if rec.status == "running":
            self.store.request_cancel(job_id)
            self.store.update_progress(job_id, rec.progress, "Cancelling after the current stage...")
            self._publish(job_id)
            return "cancelling"
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
    def running_job_id(self) -> str | None:
        """Id of the job being executed by this worker."""
        return self._running_id

    def wait_idle(self, timeout: float) -> bool:
        """Block until nothing is queued or running (thread mode); False on timeout."""
        with self._cv:
            return self._cv.wait_for(self._is_idle, timeout)

    def _is_idle(self) -> bool:
        return self._running_id is None and self.store.next_queued() is None

    # --- execution ---
    def _publish(self, job_id: str) -> None:
        rec = self.store.get(job_id)
        if rec is not None:
            self.hub.publish("dashboard_job", rec.to_dict())

    def run_next(self) -> bool:
        """Run exactly one queued job to completion; False when the queue is empty."""
        rec = self.store.next_queued(self.inline_kinds)
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

    def _execute(self, rec: JobRecord, *, track: bool = True) -> None:
        """Mark `rec` running, call its handler and record the outcome."""
        if track:
            self._running_id = rec.job_id
        if not self.store.mark_running(rec.job_id):
            if track:
                self._running_id = None
            return
        self._publish(rec.job_id)
        ctl = JobControl(rec, self.store, self.hub)
        self.log.info("job_start", job_id=rec.job_id, kind=rec.kind, channel=rec.channel)
        try:
            handler = self.handlers.get(rec.kind)
            if handler is None:
                raise RuntimeError(f"no handler for job kind {rec.kind!r}")
            if self.gpu_gate is not None and rec.kind in GPU_KINDS:
                self.gpu_gate(ctl)
            result = handler(ctl) or {}
            self.store.finish(rec.job_id, "done", "Done", result)
            self.log.info("job_done", job_id=rec.job_id, kind=rec.kind)
        except JobCancelled:
            self.store.finish(rec.job_id, "cancelled", "Cancelled")
            self.log.info("job_cancelled", job_id=rec.job_id)
        except AdapterUnavailable as e:  # a collaborator that has not landed yet: say so plainly
            self.store.finish(rec.job_id, "failed", str(e), {"error": {"code": "unavailable", "hint": None}})
            self.log.warn("job_unavailable", str(e), job_id=rec.job_id)
        except NsError as e:
            self.store.finish(
                rec.job_id, "failed", str(e), {"error": {"code": e.code, "hint": e.definition.hint}}
            )
            self.log.error("job_failed", str(e), job_id=rec.job_id, code=e.code)
        except Exception as e:  # the worker thread must survive any handler failure
            msg = f"{type(e).__name__}: {e}"
            self.store.finish(rec.job_id, "failed", msg, {"error": {"code": "job_failed", "hint": None}})
            self.log.error("job_failed", msg, job_id=rec.job_id)
        finally:
            self.store.clear_cancel(rec.job_id)
            if track:
                self._running_id = None
            self._publish(rec.job_id)
            with self._cv:
                self._cv.notify_all()

    def _loop(self) -> None:
        while not self._stopping:
            if self.run_next():
                continue
            with self._cv:
                if self._stopping:
                    break
                self._cv.wait_for(lambda: self._wake or self._stopping, timeout=1.0)
                self._wake = False
