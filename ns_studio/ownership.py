"""Who owns a run or a job: a live process, decided by an OS lock, never by the age of a row (defect D-31).

A nightly killed by a power cut or a crash left its `runs` row open for good and its half-made job in no
list, and for 12 hours the planner counted the dead job's combos row as "still running" and said the
channel already had its week's video. Age cannot tell a dead process from a slow one; a lock the operating
system drops when the process ends can (`ns_common.singleton.FileLock`, the one the engine uses). So:

* While a run (`studio run --daily`) or a job (`run_job_guarded`) executes, its process holds
  `data/owners/<id>.lock`, and `data/owners/<id>.json` says what it is (kind, origin, channel, format, pid,
  the jobs of a run) and how it stands: `starting` (announced when the combos row is written), `running`,
  `done`, `failed`, `interrupted`. The OS releases the lock with the process, so a recycled pid can never
  pass for a live owner. No column was added to studio.db: owner info lives beside the job, so nothing
  needs a migration.
* A lock that cannot be looked at (its file cannot be opened: an antivirus, a lost permission) is the one
  place where the record's pid is asked, and there a pid alone proves nothing: Windows hands a dead process's
  pid to the next one. So the work is held without a bound only while that pid is alive AND its process
  started no later than the record says the work did (`_unprobed`, NS-W117, cleared by ending that process).
  Every other state of such a lock (no pid, a pid that cannot be checked, a record that cannot be read) ends
  `UNPROBED_UNKNOWN_PID_S` after the last time known from disk, with NS-W118; a pid that is gone or reused
  ends after `UNPROBED_LIMIT` looks or at that same time, whichever comes first.
* `liveness` is `live` while the lock is held (or a job was announced a moment ago and has not taken it
  yet), `dead` when a run or job that said `running` has no holder, `ended` after a clean end,
  `interrupted` once `recover` has closed it, and `unknown` for rows with no owner file (studio.db written
  by 0.1.0 to 0.1.14).
* A lock file or the `owners` folder that does not exist is made by the lock itself (`FileLock.acquire`), so a
  home from 0.1.14, a restored backup, a cleaned folder or a file a crash left empty all work. A file error
  while making it (an antivirus scan, a sharing violation) is tried again for about a second (`_acquire`); one
  that lasts is refused as `LockUnavailable`, and another live process holding the lock as `JobBusy`. The
  nightly turns either refusal into a `runs` row and a morning note (`daily._refuse_night`).
* `recover` runs at the start of the engine (the dashboard worker), of `studio run` and of the nightly: a
  dead run row is closed as interrupted, a dead story job is closed at the stage it reached and offered in
  the Queue the way a killed dashboard job is (a failed `run` card with NS-E328 and its studio job id, so
  Retry continues it), and a morning note says so. The nightly then resumes the jobs it started
  (`pending_resumes`) instead of starting from ideas.
* Rows with no owner file are bounded by `LEGACY_IN_PROGRESS_H` (the old assumption, now only for old
  rows): a younger one may still be running, an older one is closed as interrupted, owner unknown.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol

from ns_common.atomic import write_text_atomic
from ns_common.config import notifications_enabled
from ns_common.db import new_job_id
from ns_common.errors import describe
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_common.singleton import AlreadyRunning, FileLock
from ns_studio.errors import OUT_OF_TIME, PAUSED, UNEXPECTED, StudioError

OWNERS = "owners"
INTERRUPTED_CODE = "NS-E328"
INTERRUPTED_HINT = (
    "The night's run was interrupted (power cut, crash or restart). Press Retry to continue it from where "
    "it stopped: the finished stages are kept. If it happens every night, check the PC's sleep and restart "
    "settings."
)
#: The hint for a job that was not the nightly's (Run now, a scheduled request, `studio run`): no night's run
#: was interrupted. The words are the dashboard's own for its interrupted jobs (`ns_dashboard.jobs`).
INTERRUPTED_HINT_BY_HAND = (
    "Nightshift or the PC restarted while this job ran. Press Retry from where it stopped: the finished "
    "stages are kept. If it happens every night, check the PC's sleep and restart settings."
)
#: The `origin` of a record whose job the nightly run made. Every other origin (`manual`: Run now, a scheduled
#: request, `studio run`, a series part) is a job the owner started: no text may call it the night's run.
NIGHTLY_ORIGIN = "daily"
#: A job announced by `make_job` is given this long to take its lock before it is called dead.
CREATE_GRACE_S = 180.0
#: Rows no owner file exists for (written by 0.1.0 to 0.1.14): younger than this they may still be running.
LEGACY_IN_PROGRESS_H = 12
#: The nightly resumes an interrupted job of its own up to this old; an older one waits for Retry.
RESUME_WITHIN_DAYS = 3
# Since Automatic runs became a switch (Settings), a nightly run comes only while it is on. A card's words are
# fixed when it is written, so every sentence that promises "the next nightly run" carries this condition.
WHILE_NIGHTLY_ON = "A nightly run comes only while Automatic runs are on in Settings."
#: The same condition as a clause of a sentence ("... continues it while Automatic runs are on in Settings").
WHILE_NIGHTLY_ON_CLAUSE = "while Automatic runs are on in Settings"
IF_NIGHTLY_ON = f" ({WHILE_NIGHTLY_ON_CLAUSE})"  # the same condition inside a sentence
#: Records of runs and jobs that ended cleanly are kept this long (they only explain `studio status`).
KEEP_ENDED_DAYS = 30
#: A lock another process really holds is retried this often before `hold` gives up (a liveness probe takes
#: the lock for a few milliseconds and must not turn a start-up into a failure).
ACQUIRE_TRIES = 6
ACQUIRE_WAIT_S = 0.1
#: A lock FILE that cannot be made or opened (an antivirus scan holding the new file, a sharing violation
#: while another process deletes the old one: an `OSError` from the folder or the open) is tried again after
#: each of these waits: five tries over about a second, then the work is refused. Never used for a lock
#: another process HOLDS (`AlreadyRunning`: that is `ACQUIRE_TRIES` above). A MISSING file or folder is not
#: an error at all: `FileLock.acquire` makes both.
OPEN_RETRY_WAITS_S = (0.1, 0.2, 0.3, 0.4)
#: The same for a liveness probe, which is asked often (every `/api/status`) and must stay quick.
PROBE_RETRY_WAITS_S = (0.05, 0.1)
#: A lock that cannot be probed at all (an `OSError` that lasts: an antivirus or backup tool holding the file,
#: a permission the folder lost) counts as HELD, so nobody closes work that may be running. Every such hold
#: has a bound but one (`_unprobed` is the one place that decides):
#: * the record's pid is alive and its process started no later than the record's own time plus
#:   `PID_START_SLACK_S`, or its start time cannot be read, or Windows says it has the lock file open: held
#:   for as long as that process lives, with the note `UNPROBED_CODE`;
#: * the pid is gone, or alive but started later and not known to have the file open (a reused pid): dead
#:   after `UNPROBED_LIMIT` failed probes in a row in this process, or at the time bound below, whichever
#:   comes first (a process that asks once, such as the nightly's `recover`, never reaches the count);
#: * anything else (no pid, a pid that cannot be checked, no readable record): dead `UNPROBED_UNKNOWN_PID_S`
#:   after the last time known from disk (the record's `started_at`/`announced_at`, else the record file's
#:   mtime, else the lock file's), with the note `UNPROBED_UNOWNED_CODE`. The clock is on disk because most
#:   askers live for seconds (`studio status`, `recover`): a clock in memory would start again in each.
UNPROBED_LIMIT = 3
UNPROBED_UNKNOWN_PID_S = 3600.0
UNPROBED_CODE = "NS-W117"
UNPROBED_UNOWNED_CODE = "NS-W118"
#: Record times are written in whole seconds, after the process started: a process that started up to this
#: long after the record's time is still its writer.
PID_START_SLACK_S = 2.0
#: What Windows said about who has a lock file open (`_pid_holds`, a call that can take seconds) is kept
#: this long per owner, so a status page that asks every few seconds does not repeat it.
PID_HOLDS_KEEP_S = 60.0
#: `recover` waits this long for another process that is closing the same things (the engine and the nightly
#: start together after a power cut), then scans again: the nightly must not plan before the job is closed.
RECOVER_WAIT_S = 20.0
OPEN_STATES = ("starting", "running")
#: Whose dead jobs the Queue offers (a clip, a series part or a calibration has its own way back).
STORY_ORIGINS = ("daily", "manual")
OFFER_SOURCE = "nightly_recovery"
#: The `owner` of a record the nightly closed because the job's channel was paused while it ran (P47): its
#: Queue card says so, with NS-E330, instead of naming a restart that did not happen.
PAUSED_OWNER = "its channel was paused"
PAUSED_REASON = "its channel is paused"  # `resume_problem` of a job whose channel is paused
#: The `owner` of a record the nightly closed itself before a stage, to end inside Windows' time limit for the
#: nightly task (card night-fits, S6), and the `reason` that record carries: its card says so, with NS-W211.
OUT_OF_TIME_OWNER = "it was left for time"
OUT_OF_TIME_REASON = "out_of_time"
#: The `owner` and `reason` of a record the nightly closed itself because its stage stopped as the PC woke
#: from a sleep (card night-fits-b, 4b.4): continued by the next nightly run like a job left for time.
SLEPT_OWNER = "the PC went to sleep"
SLEPT_REASON = "slept"
#: What the run does and does not hold off (`ns_common.keepawake`: ES_SYSTEM_REQUIRED stops idle sleep only).
SLEEP_WAYS = (
    "the run keeps the PC from falling asleep when idle, but not from Sleep chosen in Start, the power "
    "button, a closed lid or a low battery"
)
SLEPT_HINT = (
    f"The PC went to sleep while the night's run worked on this video ({SLEEP_WAYS}). Press Retry to "
    "continue it from where it stopped: the finished stages are kept."
)
#: The hint of a nightly job whose stop was explained (`nightcause`): the card's words say what stopped it.
INTERRUPTED_HINT_KNOWN = "Press Retry to continue it from where it stopped: the finished stages are kept."

Liveness = Literal["live", "dead", "interrupted", "ended", "unknown"]


class JobBusy(StudioError):
    """Another process really owns the job right now (a Retry from the Queue is running it): not a failure.

    `owner` is that process's pid when its lock file says so."""

    def __init__(self, code: str, detail: str = "", owner: int | None = None) -> None:
        super().__init__(code, detail)
        self.owner = owner


class LockUnavailable(StudioError):
    """No lock file can be made at all, also after the tries (a folder that is a file, a lock path that is a
    folder, a read-only or blocked disk): nobody could tell this work is alive, so it is refused, never run
    unowned. `path` is the lock file and `cause` the `OSError` that stopped it."""

    def __init__(
        self, code: str, detail: str = "", path: Path | None = None, cause: OSError | None = None
    ) -> None:
        super().__init__(code, detail)
        self.path = path
        self.cause = cause


class SqlDb(Protocol):
    """What `recover` needs of a database: `ns_common.db.Db` and the dashboard's `LockedDb` both fit."""

    def query(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ...) -> Sequence[Any]: ...

    def one(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ...) -> Any: ...

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ...) -> Any: ...


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(when: datetime | None = None) -> str:
    return (when or _now()).isoformat(timespec="seconds")


def _parse(ts: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# ---------------------------------------------------------------- the files beside the jobs
def owners_dir(paths: Paths) -> Path:
    """`data/owners`: one `.lock` and one `.json` per run or job that has been announced or has run."""
    return paths.data / OWNERS


def _stem(owner_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", owner_id)


def lock_path(paths: Paths, owner_id: str) -> Path:
    """The file whose OS lock says "a live process owns `owner_id`"."""
    return owners_dir(paths) / f"{_stem(owner_id)}.lock"


def record_path(paths: Paths, owner_id: str) -> Path:
    """The JSON record of `owner_id` (kind, origin, pid, state, ...)."""
    return owners_dir(paths) / f"{_stem(owner_id)}.json"


def read_record(paths: Paths, owner_id: str) -> dict[str, Any] | None:
    """The record of `owner_id`, or None when there is none (or it cannot be read)."""
    try:
        data = json.loads(record_path(paths, owner_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_record(paths: Paths, owner_id: str, **fields: Any) -> dict[str, Any]:
    """Merge `fields` into the record of `owner_id` (made when absent). A record that cannot be written is
    the old behaviour (no owner known), never a failure of the job."""
    rec = read_record(paths, owner_id) or {"id": owner_id}
    rec.update(fields)
    with contextlib.suppress(OSError):
        write_text_atomic(record_path(paths, owner_id), json.dumps(rec, indent=2, default=str) + "\n")
    return rec


def list_records(paths: Paths) -> list[dict[str, Any]]:
    """Every record under `data/owners`, oldest announced first."""
    out: list[dict[str, Any]] = []
    try:
        files = sorted(owners_dir(paths).glob("*.json"))
    except OSError:
        return out
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("id"):
            out.append(data)
    out.sort(key=lambda r: str(r.get("announced_at") or r.get("started_at") or ""))
    return out


def forget(paths: Paths, owner_id: str) -> None:
    """Remove the record and lock file of `owner_id` (what a version before 0.1.15 left: nothing)."""
    for f in (record_path(paths, owner_id), lock_path(paths, owner_id)):
        with contextlib.suppress(OSError):
            f.unlink()


# ---------------------------------------------------------------- is anybody there
def _acquire(lock: FileLock, waits: Sequence[float] | None = None) -> None:
    """`lock.acquire()`, tried again after each of `waits` (default `OPEN_RETRY_WAITS_S`) while the lock FILE
    cannot be made or opened (`OSError` from the folder or the open). `AlreadyRunning` (the file is there and
    another process holds it) is no file error: it goes out at once, for the caller's own wait. The `OSError`
    of the last try goes out after the last wait."""
    for wait in OPEN_RETRY_WAITS_S if waits is None else waits:
        try:
            lock.acquire()
            return
        except OSError:  # `AlreadyRunning` is not an OSError and is not caught here
            time.sleep(wait)
    lock.acquire()


#: Per owner (home + id): [failed probes in a row, the codes the owner was told, when the first of them
#: failed, the live pid that keeps it held (None when none does), the kept answer of `_pid_holds` as
#: (pid, answer, when), and for a live pid that could not be proven the owner the name of its program].
#: A good probe removes the entry.
_UNPROBED: dict[tuple[str, str], list[Any]] = {}


def _pid_alive(pid: int) -> bool | None:
    """True while a process with this pid exists, False when none does, None when that cannot be checked.

    Never "alive" and never "gone" because a check failed: `_unprobed` bounds an unknown by time. tasklist
    (`pid_alive`) answers "no process" also when it could not be run or timed out, so its "no" counts only
    when psutil says the same; when psutil is missing or fails, "no" is "cannot tell". Tests replace this.
    """
    try:
        from ns_supervisor.update_job import pid_alive

        if pid_alive(pid):
            return True
        import psutil

        return None if psutil.pid_exists(pid) else False
    except Exception:  # noqa: BLE001 - cannot tell
        return None


def _pid_started(pid: int) -> datetime | None:
    """When the process that has this pid now was started; None when there is none or it cannot be read."""
    try:
        import psutil

        return datetime.fromtimestamp(psutil.Process(pid).create_time(), UTC)
    except Exception:  # noqa: BLE001 - no psutil, no such process, access denied: not known
        return None


def _pid_holds(pid: int, lock: Path) -> bool | None:
    """Whether the process with this pid has `lock` open (the holder of an owner lock keeps its file open
    for as long as it holds it): True, False, or None when Windows will not say.

    Asked of the Windows Restart Manager, which lists the processes using a file without opening it. It
    answers for a file another program is holding (a scan, a backup) and refuses (None) for one this account
    has lost the right to read; None too off Windows. It can take a second or two, so `_unprobed` asks only
    before it would call a live process a reused pid.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class Unique(ctypes.Structure):
            _fields_ = (("pid", wintypes.DWORD), ("start", wintypes.FILETIME))

        class Info(ctypes.Structure):
            _fields_ = (
                ("process", Unique),
                ("app", wintypes.WCHAR * 256),
                ("service", wintypes.WCHAR * 64),
                ("kind", ctypes.c_int),
                ("status", wintypes.ULONG),
                ("session", wintypes.DWORD),
                ("restartable", wintypes.BOOL),
            )

        rm = ctypes.WinDLL("rstrtmgr")  # type: ignore[attr-defined]  # Windows only; else the except below
        session = wintypes.DWORD()
        if rm.RmStartSession(ctypes.byref(session), 0, ctypes.create_unicode_buffer(33)):
            return None
        try:
            names = (wintypes.LPCWSTR * 1)(str(lock))
            if rm.RmRegisterResources(session, 1, names, 0, None, 0, None):
                return None
            needed, got, reasons = wintypes.UINT(), wintypes.UINT(64), wintypes.DWORD()
            infos = (Info * 64)()
            if rm.RmGetList(session, ctypes.byref(needed), ctypes.byref(got), infos, ctypes.byref(reasons)):
                return None  # access denied, or more users than fit: not known
            return any(infos[i].process.pid == pid for i in range(got.value))
        finally:
            rm.RmEndSession(session)
    except Exception:  # noqa: BLE001 - not Windows, no Restart Manager: not known
        return None


def _pid_name(pid: int) -> str | None:
    """The program name of the process with this pid (`python.exe`), None when it cannot be read."""
    try:
        from ns_supervisor.control import image_name

        return image_name(pid)
    except Exception:  # noqa: BLE001 - not known
        return None


def unprobed_message(owner_id: str, pid: int | None = None) -> str:
    """The sentence the owner reads (morning note, `studio status`) while `owner_id` is held only because its
    lock cannot be probed: the code's message and hint, and which work it is.

    NS-W117 when a live process is known to own it (`pid`, or the one `_unprobed` found), with how to clear
    it; NS-W118 when none is, which says the hold ends by itself within the hour. When the live process could
    not be proven to be the owner (its start time cannot be read) the note says so, names its program and
    does not tell the owner to end it: it may be another program that was given the same number.
    """
    entry = next((st for (_, o), st in _UNPROBED.items() if o == owner_id and st[3]), None)
    if pid is None and entry is not None:
        pid = entry[3]
    if pid and entry is not None and entry[3] == pid and entry[5]:
        d = describe(UNPROBED_CODE)
        return (
            f"{UNPROBED_CODE}: {d.message} {owner_id} (process {pid}, {entry[5]}, has the number this work "
            "recorded and is running, but Nightshift cannot read when it started, so it may be another "
            f"program). {d.hint} Do not end process {pid} unless you know it is Nightshift's: restarting the "
            "PC clears this safely either way. Release in the Queue does not clear it (it frees the graphics "
            "card only)."
        )
    if pid:
        d = describe(UNPROBED_CODE)
        return (
            f"{UNPROBED_CODE}: {d.message} {owner_id} (process {pid} is still running). {d.hint} Ending "
            f"process {pid} in Task Manager clears it too: the next start of Nightshift or the next nightly "
            "run then closes it as interrupted. Release in the Queue does not clear it (it frees the "
            "graphics card only)."
        )
    d = describe(UNPROBED_UNOWNED_CODE)
    return f"{UNPROBED_UNOWNED_CODE}: {d.message} {owner_id}. {d.hint}"


def unprobed_owners(paths: Paths) -> list[str]:
    """Owners of this home held at the moment only because their lock could not be probed."""
    return sorted(o for (home, o), st in _UNPROBED.items() if home == str(paths.home) and st[0] > 0)


def _unprobed(paths: Paths, owner_id: str, handed: Mapping[str, Any] | None = None) -> bool:
    """The answer of `lock_held` when the lock cannot be probed: True (held) or, once bounded, False (dead).

    The one place that decides it, for every state the record can be in (`handed` is the record `liveness`
    had, used when the file can no longer be read: missing, locked or half written):

    * its pid is this process: held exactly while this process holds the work (`_HELD`), else gone;
    * its pid is alive and that process started no later than the record's newest time (`started_at`,
      `announced_at`) plus `PID_START_SLACK_S`: held, with no bound, and the owner is told once per process
      with NS-W117 (which process, and that ending it or restarting the PC clears it). Live work is never
      released here;
    * its pid is alive and its start time cannot be read: held the same way when its program is Python
      (every owner is) or has no readable name; the note then says it may be another program and does not
      tell the owner to end it. An unreadable start time never shortens the life of work that may be
      running. A program with another name is not Nightshift's: a reused pid, gone;
    * its pid is alive but started later than that: before it is called a reused pid, Windows is asked
      whether that process has the lock file open (`_pid_holds`; the answer is kept `PID_HOLDS_KEEP_S`).
      It has: held as above, whatever the clocks say (a clock set back after the process started must not
      close live work). It has not, or Windows will not say: the pid was given to another program, gone;
    * its pid is gone, or reused as above: dead after `UNPROBED_LIMIT` failed probes in a row in this
      process, or at the time bound, whichever is first;
    * no pid, a pid nothing can be asked about, a live pid with no time in the record to compare with, no
      record: unknown. Held until `UNPROBED_UNKNOWN_PID_S` after the last known time, then dead; the owner
      is told once per process with NS-W118 (it ends by itself; `recover` then closes the work).

    The last known time is on disk, so a process that lives for seconds decides the same as one that has
    watched for an hour: the record's newest time, else the record file's mtime, else the lock file's
    (`stat` works where opening does not). Only when none of them can be read does it count from this
    process's first failed probe. Limit: the times are wall-clock. When Windows will not say who has the
    lock file open (a file this account may not read), a clock set back by more than the slack between a
    process's start and its record still makes it look reused: it is closed as interrupted while it runs;
    `_take` refuses a second start for as long as the file cannot be opened.
    """
    key = (str(paths.home), owner_id)
    now = _now()
    state = _UNPROBED.setdefault(key, [0, set(), now, None, None, None])
    state[0] += 1
    rec = read_record(paths, owner_id) or handed or {}
    pid = rec.get("pid")
    pid = pid if isinstance(pid, int) and pid > 0 else None
    times = [t for t in (_parse(rec.get(k)) for k in ("started_at", "announced_at")) if t is not None]
    known = max(times) if times else None
    alive: bool | None = None  # True: provably its owner; False: gone or reused; None: cannot tell
    unsure: str | None = None  # the program of a live pid that is held without proof that it is the owner
    if pid == os.getpid():
        with _HELD_GUARD:
            alive = (str(owners_dir(paths)), owner_id) in _HELD
    elif pid is not None:
        started = _pid_started(pid)
        exists = True if started is not None else _pid_alive(pid)
        if exists is False:
            alive = False
        elif exists and started is None:
            name = _pid_name(pid)  # a process is there: never cut short, unless it is plainly not ours
            alive = name is None or "python" in name.lower()
            unsure = name or "a program whose name cannot be read"
        elif started is not None and known is not None:
            alive = (started - known).total_seconds() <= PID_START_SLACK_S
            if not alive:  # looks reused: the lock file it holds open outranks the comparison of clocks
                kept = state[4]
                if kept is None or kept[0] != pid or abs((now - kept[2]).total_seconds()) > PID_HOLDS_KEEP_S:
                    kept = state[4] = (pid, _pid_holds(pid, lock_path(paths, owner_id)), now)
                alive = kept[1] is True
    state[3] = pid if alive else None
    state[5] = unsure if alive else None

    def tell(code: str) -> None:
        if code not in state[1]:
            state[1].add(code)
            with contextlib.suppress(Exception):  # a note must never make the probe fail
                leave_note(paths, None, unprobed_message(owner_id, state[3]))

    if alive:
        tell(UNPROBED_CODE)
        return True
    last = known
    for f in (record_path(paths, owner_id), lock_path(paths, owner_id)):
        if last is None:
            with contextlib.suppress(Exception):
                last = datetime.fromtimestamp(f.stat().st_mtime, UTC)
    if last is None:
        last = state[2]
    if (now - last).total_seconds() > UNPROBED_UNKNOWN_PID_S:
        return False
    if alive is False:
        return state[0] < UNPROBED_LIMIT  # a probe or two more settles it: nothing to tell the owner
    tell(UNPROBED_UNOWNED_CODE)
    return True


def lock_held(paths: Paths, owner_id: str, rec: Mapping[str, Any] | None = None) -> bool:
    """True while some process (this one too) holds the lock of `owner_id`, or when it cannot be probed.

    Fails closed, with a bound: an `OSError` while looking at or opening the lock file (another program holds
    it, the folder lost a permission) is "held", never "free", so `liveness` says "live" and nobody closes
    work that may be running: `recover` leaves it open for its next call, the planner (`in_progress`, cadence)
    counts it, `run_state` says running and `studio status` lists it as working. The bound is `_unprobed`
    (which gets `rec`, the record the caller had, for when the file can no longer be read): only a live
    process that provably started the work keeps it held for good (note `NS-W117`); every other state ends
    within `UNPROBED_UNKNOWN_PID_S` of its last known time (note `NS-W118`).
    """
    path = lock_path(paths, owner_id)
    key = (str(paths.home), owner_id)
    try:
        if not path.exists():  # no lock file: nobody ever held it
            _UNPROBED.pop(key, None)
            return False
    except OSError:
        return _unprobed(paths, owner_id, rec)
    probe = FileLock(path)
    try:
        _acquire(probe, PROBE_RETRY_WAITS_S)  # a file that cannot be opened for a moment is not "not held"
    except AlreadyRunning:
        _UNPROBED.pop(key, None)
        return True
    except OSError:
        return _unprobed(paths, owner_id, rec)  # could not look: held, within the bound
    probe.release()
    _UNPROBED.pop(key, None)
    return False


def liveness(paths: Paths, owner_id: str, rec: Mapping[str, Any] | None = None) -> Liveness:
    """Whether `owner_id` has a live owner, from its record and its lock (see the module note).

    A record that is missing or cannot be read when this starts is `unknown`, not live: its callers bound it
    by age (`LEGACY_IN_PROGRESS_H`, in `in_progress`, `run_state` and `_recover_legacy`), so the lock is not
    asked. A record this call did get is passed on to `lock_held`, which needs it when the lock cannot be
    probed and the file can no longer be read.
    """
    rec = rec if rec is not None else read_record(paths, owner_id)
    if rec is None:
        return "unknown"
    state = rec.get("state")
    if state == "interrupted":
        return "interrupted"
    if state not in OPEN_STATES:
        return "ended"
    if lock_held(paths, owner_id, rec):
        return "live"
    announced = _parse(rec.get("announced_at"))
    if (
        state == "starting"
        and announced is not None
        and (_now() - announced).total_seconds() < CREATE_GRACE_S
    ):
        return "live"  # `make_job` wrote the combos row a moment ago and the job has not taken its lock yet
    return "dead"


def in_progress(paths: Paths | None, job_id: str, created: datetime | None, now: datetime) -> bool:
    """Whether a job with a combos row but no finished video of its own still counts for the planner.

    Live work counts, and so does a job that ended cleanly (its video is in the Outbox or its run said done).
    A dead, failed or interrupted job counts for nothing. Only a job no owner file exists for (written before
    0.1.15) falls back to its age: younger than `LEGACY_IN_PROGRESS_H` it may still be running.
    """
    status = liveness(paths, job_id) if paths is not None else "unknown"
    if status == "live":
        return True
    if status == "unknown":
        return created is not None and now - created < timedelta(hours=LEGACY_IN_PROGRESS_H)
    rec = read_record(paths, job_id) if paths is not None else None
    return bool(rec and rec.get("state") == "done")


def job_info(paths: Paths, job_id: str) -> dict[str, Any]:
    """`job.json` of the job (channel, fmt, created_at), or {} when it is missing or unreadable."""
    try:
        data = json.loads((paths.job_dir(job_id) / "job.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def reached_stage(paths: Paths, job_id: str, fmt: str | None) -> str | None:
    """The stage a job stopped at: the first stage of its plan that `stage_state.json` does not call done.

    The plan is the one the job runs (`orchestrator.stage_names`: an owner script or a series part runs its
    own, a job made without the policy gates has none). None when the job has no plan; the last stage when all
    of them finished (the kill came at the end).
    """
    try:
        entries = json.loads((paths.job_dir(job_id) / "stage_state.json").read_text(encoding="utf-8"))
        done = {n for n, e in entries.items() if isinstance(e, dict) and e.get("status") == "done"}
    except (OSError, ValueError, AttributeError):
        done = set()
    try:
        from ns_studio.orchestrator import stage_names

        names = stage_names(fmt or "", paths.job_dir(job_id))
    except Exception:  # noqa: BLE001 - a plan that cannot be read only costs the stage name
        names = []
    for name in names:
        if name not in done:
            return name
    return names[-1] if names else None


def live_description(paths: Paths, job_id: str) -> str | None:
    """Words for a job with a live owner (job, stage, process), else None: what `nothing due` says."""
    rec = read_record(paths, job_id)
    if rec is None or liveness(paths, job_id, rec) != "live":
        return None
    stage = reached_stage(paths, job_id, rec.get("fmt"))
    parts = [f"job {job_id}"]
    if stage:
        parts.append(f"at stage {stage}")
    if rec.get("pid"):
        parts.append(f"process {rec['pid']}")
    return ", ".join(parts)


# ---------------------------------------------------------------- holding it
_HELD: dict[tuple[str, str], int] = {}
_HELD_GUARD = threading.Lock()


def _take(paths: Paths, kind: str, owner_id: str) -> FileLock:
    """The OS lock of `owner_id`; `JobBusy` when another process really owns it, `LockUnavailable` when no
    lock file can be made at all (a folder that is a file, a read-only or blocked disk; after five tries over
    about a second: refused, never run with no owner known). A missing file or folder is made on the way."""
    lock = FileLock(lock_path(paths, owner_id))
    for attempt in range(ACQUIRE_TRIES):
        try:
            _acquire(lock)
            return lock
        except AlreadyRunning as e:
            if attempt == ACQUIRE_TRIES - 1:
                who = f" (process {e.owner})" if e.owner else ""
                raise JobBusy(
                    UNEXPECTED, f"{kind} {owner_id} is already running in another process{who}", e.owner
                ) from e
            time.sleep(ACQUIRE_WAIT_S)
        except OSError as e:
            # No lock file can be made: nobody could tell this work is alive, so a second start would close it
            # as dead and continue it next to the live one. Refuse rather than run unowned.
            raise LockUnavailable(
                UNEXPECTED,
                f"{kind} {owner_id} cannot be owned, its lock file cannot be made: {e}",
                lock.path,
                e,
            ) from e
    raise LockUnavailable(UNEXPECTED, f"{kind} {owner_id} cannot be owned", lock.path)


@contextlib.contextmanager
def hold(paths: Paths, kind: str, owner_id: str, **fields: Any) -> Iterator[None]:
    """Own `owner_id` (a `run` or a `job`) for the length of the block.

    Takes the OS lock, records `running` with this pid, and records how it ended: `done` or `failed` (an error
    came out, the job is over and Retry-able, not interrupted). A kill, and Ctrl+C or anything else that is
    not an `Exception`, record nothing: the record keeps saying `running` with no lock holder, which is what
    `liveness` calls dead, so `recover` closes it with its card, its stage and its note, the way it closes a
    killed process. Re-entrant inside one process (the nightly holds the job and `run_job_guarded` holds it
    again). Another process holding it raises `JobBusy` (NS-E327, with the pid); a lock that cannot be made at
    all, also after five tries over about a second, raises `LockUnavailable` (NS-E327): work nobody could see
    as alive is never started. Both are raised before the block runs, so a caller can tell them from a failure
    of the work (the nightly does: `daily._refuse_night`).
    """
    key = (str(owners_dir(paths)), owner_id)
    with _HELD_GUARD:
        nested = key in _HELD
        if nested:
            _HELD[key] += 1
    if nested:
        try:
            yield
        finally:
            with _HELD_GUARD:
                if key in _HELD:
                    _HELD[key] = max(1, _HELD[key] - 1)
        return
    lock = _take(paths, kind, owner_id)
    with _HELD_GUARD:
        _HELD[key] = 1
    known = read_record(paths, owner_id) or {}
    extra = {k: v for k, v in fields.items() if v is not None}
    if kind == "job" and known.get("cannot_fit"):
        # Someone runs a job the night gave up (a Retry, `studio run --resume`): it is no longer given up. If
        # this run is interrupted too, the job is an interrupted job like any other and the nightly decides
        # about it again by the same decision (card night-fits: a healed stage is then continued).
        extra.update(cannot_fit=None, resume_problem=None, misses=0, first_next=False)
    origin = known.get("origin") or extra.get("origin") or "manual"
    extra.pop("origin", None)
    write_record(
        paths,
        owner_id,
        kind=kind,
        state="running",
        pid=os.getpid(),
        started_at=_iso(),
        origin=origin,
        **extra,
    )
    state: str | None = "done"
    error: str | None = None
    try:
        yield
    except Exception as e:
        state, error = "failed", getattr(e, "code", None)
        raise
    except BaseException:
        state = None  # Ctrl+C and the like: left `running` and unheld, so `recover` closes it in full
        raise
    finally:
        with _HELD_GUARD:
            _HELD.pop(key, None)
        if state is None:
            write_record(paths, owner_id, pid=None)  # this process is not what ended it, and may live on
        else:
            write_record(paths, owner_id, state=state, ended_at=_iso(), error=error)
        lock.release()
        with contextlib.suppress(OSError):
            lock.path.unlink()


def hold_job(paths: Paths, job_id: str, **fields: Any) -> contextlib.AbstractContextManager[None]:
    """`hold` for a job; `fields` (origin, channel, fmt, run_id) fill the record when it has none yet."""
    return hold(paths, "job", job_id, **fields)


def hold_run(paths: Paths, run_id: str, **fields: Any) -> contextlib.AbstractContextManager[None]:
    """`hold` for a nightly run."""
    return hold(paths, "run", run_id, **fields)


def announce_job(
    paths: Paths,
    job_id: str,
    *,
    origin: str,
    channel: str,
    fmt: str,
    run_id: str | None = None,
    created: datetime | None = None,
) -> None:
    """Write the record of a job the moment its combos row is written, before it can take its lock.

    `origin` is `daily` (the nightly run, which resumes it after a kill) or `manual` (`studio run`, the
    dashboard: offered in the Queue with Retry, never resumed by itself).
    """
    write_record(
        paths,
        job_id,
        kind="job",
        state="starting",
        origin=origin,
        channel=channel,
        fmt=fmt,
        run_id=run_id,
        announced_at=_iso(),
        job_created_at=_iso(created),
        pid=os.getpid(),
    )


def note_run_job(paths: Paths, run_id: str, job_id: str, channel: str) -> None:
    """Add a job to the run's record, so a dead run can say which jobs it had."""
    rec = read_record(paths, run_id) or {}
    jobs = [j for j in rec.get("jobs") or [] if isinstance(j, dict) and j.get("job_id") != job_id]
    write_record(paths, run_id, jobs=[*jobs, {"job_id": job_id, "channel": channel}])


# ---------------------------------------------------------------- what became of an interrupted job
#: The fates the Queue's "Last night" card words apart: still open (needs Retry), continued and finished,
#: continued and stopped again, waiting for the GPU (the next night continues it).
FATES = ("open", "resumed", "failed_again", "waiting")


def interrupted_entry(
    job_id: str, channel: str | None, stage: str | None, fate: str = "open", *, has_card: bool | None = None
) -> dict[str, Any]:
    """One `interrupted_jobs` entry of a run summary: which job, where it stopped and what became of it.

    `state` is the fate (one of `FATES`); `resumed` is `state == "resumed"` (the key older readers know);
    `has_card` says whether a Retry card in the Queue stands for the job (omitted when not known).
    """
    if fate not in FATES:
        raise ValueError(f"unknown fate {fate!r}")
    item: dict[str, Any] = {
        "job_id": job_id,
        "channel": channel,
        "stage": stage,
        "state": fate,
        "resumed": fate == "resumed",
    }
    if has_card is not None:
        item["has_card"] = has_card
    return item


def has_card(rec: Mapping[str, Any]) -> bool:
    """Whether the Queue has a Retry card for this record's job: `make_job` announced it and a person (the
    dashboard) or the nightly started it. A job known only through `hold` (a series part, a job reopened by
    hand) has none: its way back is `studio run --resume`."""
    return rec.get("origin") in STORY_ORIGINS and bool(rec.get("announced_at"))


def current_interrupted(paths: Paths, db: SqlDb, items: Sequence[Any] | None) -> list[dict[str, Any]]:
    """The `interrupted_jobs` of a run summary, as true when they are served.

    A summary is written once, when the run is closed, and says what was true then. A job it lists as open may
    since have been finished (Retry from the Queue, or a nightly that continued it): such an entry is served
    as `resumed`, so the card never says "not finished" of a job that has its video. Entries from a version
    that wrote no `state` read as `resumed` when they say so and as `open` otherwise.
    """
    out: list[dict[str, Any]] = []
    for raw in items or []:
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        fate = item.get("state")
        if fate not in FATES:
            fate = "resumed" if item.get("resumed") is True else "open"
        job_id = str(item.get("job_id") or "")
        if fate != "resumed" and job_id:
            fmt = (read_record(paths, job_id) or {}).get("fmt") or job_info(paths, job_id).get("fmt")
            if job_info(paths, job_id) and job_finished(paths, db, job_id, fmt):
                fate = "resumed"
        item["state"], item["resumed"] = fate, fate == "resumed"
        out.append(item)
    return out


# ---------------------------------------------------------------- the run row, as /api/status shows it
def run_state(
    paths: Paths,
    run_id: str,
    started_at: str | None,
    finished_at: str | None,
    summary: Mapping[str, Any] | None,
    now: datetime | None = None,
) -> tuple[str, str]:
    """`(state, note)` of a `runs` row: `running`, `finished`, `failed` or `interrupted`, and why.

    A row with no finish time is not a run that is going on: it is running only while its owner is alive.
    """
    now = now or _now()
    if finished_at:
        if isinstance(summary, Mapping) and summary.get("failed"):
            why = f": {summary['reason']}" if summary.get("reason") else ""  # a refused night says why
            return "failed", f"the night stopped on a problem ({summary['failed']}) before it finished{why}"
        if isinstance(summary, Mapping) and summary.get("interrupted") is True:
            if summary.get("cause_said"):  # what stopped it, found when it was closed (`nightcause`)
                said = str(summary["cause_said"])
                said = said[:1].lower() + said[1:] if said.startswith("The ") else said
                return "interrupted", f"interrupted: {said}"
            if summary.get("owner") == "unknown":
                return (
                    "interrupted",
                    "interrupted: it never finished (owner unknown, written by an older version)",
                )
            return "interrupted", "interrupted: it never finished (power cut, crash or restart)"
        return "finished", ""
    status = liveness(paths, run_id)
    if status == "live":
        return "running", ""
    if status in ("dead", "interrupted"):
        rec = read_record(paths, run_id) or {}
        who = f" (process {rec['pid']} is gone)" if rec.get("pid") else ""
        return "interrupted", f"interrupted: it never finished{who}"
    started = _parse(started_at)
    if started is not None and now - started < timedelta(hours=LEGACY_IN_PROGRESS_H):
        return "running", "owner unknown (written by an older version): it may still be running"
    return "interrupted", "interrupted: it never finished (owner unknown, written by an older version)"


# ---------------------------------------------------------------- the Queue card of an interrupted job
def _rows_for(db: SqlDb, job_id: str) -> list[Any]:
    """The `dashboard_jobs` rows that name `job_id` (`_` and `%` in an id are letters, not wildcards)."""
    exact = job_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return list(
        db.query(
            "select job_id, status, params_json from dashboard_jobs where params_json like ? escape '\\'",
            (f"%{exact}%",),
        )
    )


def _our_row(rows: Sequence[Any]) -> Any | None:
    for r in rows:
        try:
            params = json.loads(r["params_json"]).get("params")
        except (ValueError, AttributeError):
            continue
        if isinstance(params, dict) and params.get("source") == OFFER_SOURCE:
            return r
    return None


def _card_text(
    stage: str | None,
    resume: str,
    reason: str | None = None,
    owner: str | None = None,
    origin: str = NIGHTLY_ORIGIN,
    by: str = "nightly",
    limit_s: int | None = None,
    cause_said: str | None = None,
) -> str:
    """The words of an interrupted job's Queue card. Three things are said, each from its own fact:

    - what was interrupted, from who made the job (`origin`): the night's run only for a job of the nightly
      run; a job the owner started (Run now, a scheduled request, `studio run`) stopped "while this job ran",
      and nothing promises it a nightly run, because the nightly continues only its own jobs;
    - who continued a job that has finished since (`by`): the nightly run, or `studio run --resume` by hand;
    - the cause, from who stopped it last (`owner`): a pause, or a dead process;
    - the way back, from why it waits NOW (`resume`, `reason`), whatever stopped it first. A job whose channel
      is paused cannot be retried (Retry answers 409 `channel_paused`), so its card never says "press Retry"
      without "after the resume", and it says that the first nightly run after the resume continues it. The
      sentences stay true when the owner pauses or resumes later, because nothing rewrites a card then.

    A job the night left before a stage for time (`OUT_OF_TIME_OWNER`, card night-fits) says that instead of a
    stop, with the task's limit (`limit_s`, the one the night planned with; the registered one by default).
    A job of a dead night whose stop was explained (`cause_said`, `nightcause`) says what stopped it; one the
    night closed as the PC woke from a sleep (`SLEPT_OWNER`) says that.
    """
    at = f"Interrupted at {stage}" if stage else "Interrupted"
    cause = "its channel was paused" if owner == PAUSED_OWNER else "the PC or Nightshift stopped"
    nightly = origin == NIGHTLY_ORIGIN
    during = "during the night's run" if nightly else "while this job ran"
    stopped = f"{at}: {cause} {during}."
    if nightly and owner == SLEPT_OWNER:
        stopped = f"{at}: the PC went to sleep during the night's run, and the stage stopped as it woke."
    elif nightly and cause_said and owner != PAUSED_OWNER:
        said = cause_said[:1].lower() + cause_said[1:] if cause_said.startswith("The ") else cause_said
        stopped = f"{at}: {said}."
    if owner == OUT_OF_TIME_OWNER:
        stopped = _left_words(stage, limit_s)
        if resume == "pending" and reason is None and nightly:
            return f"{stopped} The next nightly run continues it{IF_NIGHTLY_ON}, or press Retry."
    if resume == "resumed":
        if by == "hand":
            return f"{stopped} It was continued with studio run --resume and it finished."
        return f"{stopped} The nightly run continued it and it finished."
    if not nightly:
        why = f" ({reason})" if reason else ""
        return f"{stopped} Press Retry to continue it{why}."
    if reason == PAUSED_REASON:
        since = "" if owner == PAUSED_OWNER else " Its channel was paused after that."
        return (
            f"{stopped}{since} Resume the channel: the next nightly run then continues it, if the job "
            f"started no more than {RESUME_WITHIN_DAYS} days before that run; or press Retry after the "
            f"resume. {WHILE_NIGHTLY_ON}"
        )
    if resume == "pending":
        return (
            f"{stopped} The next nightly run continues it, or press Retry (if its channel is paused by then, "
            f"both wait for the resume). {WHILE_NIGHTLY_ON}"
        )
    why = f" ({reason})" if reason else ""
    return f"{stopped} Press Retry to continue it{why}."


def _left_words(stage: str | None, limit_s: int | None) -> str:
    """`Left at <stage> so the night's run would end inside Windows' 6-hour limit for the nightly task.`"""
    from ns_studio.nightclock import limit_adjective

    if limit_s is None:
        from ns_supervisor.schedule_cfg import TASK_TIME_LIMIT_S

        limit_s = int(TASK_TIME_LIMIT_S)
    where = f" at {stage}" if stage else ""
    return (
        f"Left{where} so the night's run would end inside Windows' {limit_adjective(int(limit_s))} limit for "
        "the nightly task."
    )


def _card_error(
    stage: str | None,
    owner: str | None,
    reason: str | None,
    origin: str = NIGHTLY_ORIGIN,
    cause_said: str | None = None,
) -> dict[str, Any]:
    """The code and hint of the card: NS-E330 when a pause stopped the job or holds it now (its hint says to
    resume the channel first), NS-W211 for a job the night left for time, else NS-E328 (its hint says to press
    Retry, which then works). The NS-E328 hint names the night's run only for a job of the nightly run
    (`origin`), and names no guessed cause when the card's words say what stopped it (a sleep,
    `cause_said`)."""
    if owner == PAUSED_OWNER or reason == PAUSED_REASON:
        return {"code": PAUSED, "hint": describe(PAUSED).hint, "stage": stage}
    if owner == OUT_OF_TIME_OWNER:
        return {"code": OUT_OF_TIME, "hint": describe(OUT_OF_TIME).hint, "stage": stage}
    hint = INTERRUPTED_HINT if origin == NIGHTLY_ORIGIN else INTERRUPTED_HINT_BY_HAND
    if origin == NIGHTLY_ORIGIN and owner == SLEPT_OWNER:
        hint = SLEPT_HINT
    elif origin == NIGHTLY_ORIGIN and cause_said:
        hint = INTERRUPTED_HINT_KNOWN
    return {"code": INTERRUPTED_CODE, "hint": hint, "stage": stage}


def _origin_of(rec: Mapping[str, Any]) -> str:
    """Who made the job of an owner record or of a card's `interrupted` block: `daily` or `manual`.

    A record always says (`hold` and `announce_job` write it). A card written before the key existed does
    not: there a `run_id` tells, because only the nightly run gives its jobs one."""
    origin = rec.get("origin")
    if isinstance(origin, str) and origin:
        return origin
    return NIGHTLY_ORIGIN if rec.get("run_id") else "manual"


def _write_card(
    db: SqlDb, card_id: str, status: str, message: str, result: dict[str, Any], *, finished: str
) -> None:
    row = db.one("select params_json from dashboard_jobs where job_id = ?", (card_id,))
    params: dict[str, Any] = {}
    if row is not None:
        with contextlib.suppress(ValueError, AttributeError):
            params = json.loads(row["params_json"]).get("params") or {}
    payload = json.dumps({"params": params, "result": result, "dismissed_at": None}, default=str)
    db.execute(
        "update dashboard_jobs set status = ?, message = ?, progress = ?, finished_at = ?, params_json = ?"
        " where job_id = ?",
        (status, message, 1.0 if status == "done" else 0.0, finished, payload, card_id),
    )


def _card_result(
    job_id: str, stage: str | None, rec: Mapping[str, Any], resume: str, reason: str | None
) -> dict[str, Any]:
    return {
        "studio_job_ids": [job_id],
        "error": _card_error(stage, rec.get("owner"), reason, _origin_of(rec), rec.get("cause_said")),
        "interrupted": {
            "stage": stage,
            "origin": _origin_of(rec),
            "run_id": rec.get("run_id"),
            "owner": rec.get("owner") or "gone",
            "resume": resume,
            "reason": reason,
            "at": _iso(),
            "limit_s": rec.get("limit_s"),
            "cause": rec.get("cause"),
            "cause_said": rec.get("cause_said"),
        },
    }


def offer_job(
    db: SqlDb,
    job_id: str,
    rec: Mapping[str, Any],
    stage: str | None,
    *,
    resume: str,
    reason: str | None = None,
    when: datetime | None = None,
) -> str | None:
    """Put the interrupted job in the Queue as a failed `run` card (NS-E328, its studio job id, its stage),
    the way the dashboard's own recovery does, so Retry continues it. One card per job: a second
    interruption updates it. None (and nothing written) when a dashboard card already stands for the job."""
    rows = _rows_for(db, job_id)
    mine = _our_row(rows)
    if mine is None and rows:
        return None
    result = _card_result(job_id, stage, rec, resume, reason)
    message = _card_text(stage, resume, reason, rec.get("owner"), _origin_of(rec), limit_s=rec.get("limit_s"),
                         cause_said=rec.get("cause_said"))
    stamp = _iso(when)
    if mine is not None:
        _write_card(db, str(mine["job_id"]), "failed", message, result, finished=stamp)
        return str(mine["job_id"])
    params = {
        "channel": rec.get("channel"),
        "format": rec.get("fmt"),
        "count": 1,
        "source": OFFER_SOURCE,
        "run_id": rec.get("run_id"),
    }
    payload = json.dumps({"params": params, "result": result, "dismissed_at": None}, default=str)
    for _ in range(8):
        card_id = new_job_id("run")
        try:
            db.execute(
                "insert into dashboard_jobs(job_id, kind, channel, status, progress, message, created_at,"
                " started_at, finished_at, params_json) values (?, 'run', ?, 'failed', 0, ?, ?, ?, ?, ?)",
                (
                    card_id,
                    rec.get("channel"),
                    message,
                    _now().isoformat(timespec="milliseconds"),
                    rec.get("job_created_at") or stamp,
                    stamp,
                    payload,
                ),
            )
        except sqlite3.IntegrityError:  # the id is taken: draw another one
            continue
        return card_id
    return None


def _settle(
    db: SqlDb,
    job_id: str,
    status: str,
    resume: str,
    *,
    reason: str | None = None,
    failure: tuple[str, str] | None = None,
    output_ids: Sequence[str] | None = None,
    by: str = "nightly",
    stopped: Mapping[str, Any] | None = None,
) -> None:
    """Rewrite the card of an interrupted job after somebody tried to continue it (no card, no change).

    `by` is who: the nightly run (it continues only its own jobs), or `hand` for `studio run --resume`.
    `stopped` replaces what stopped it last (`owner`, `stage`, `limit_s`): the continuation stopped too."""
    mine = _our_row(_rows_for(db, job_id))
    if mine is None:
        return
    raw = db.one("select params_json from dashboard_jobs where job_id = ?", (mine["job_id"],))
    try:
        result = dict(json.loads(raw["params_json"]).get("result") or {})
    except (ValueError, AttributeError, TypeError):
        result = {}
    interrupted = dict(result.get("interrupted") or {})
    interrupted["resume"] = resume
    if reason is not None:
        interrupted["reason"] = reason
    interrupted.update(stopped or {})
    result["interrupted"] = interrupted
    stage = interrupted.get("stage")
    origin = _origin_of(interrupted)
    message = _card_text(
        stage, resume, reason, interrupted.get("owner"), origin, by, limit_s=interrupted.get("limit_s"),
        cause_said=interrupted.get("cause_said"),
    )
    if resume == "needs_retry" or stopped:  # the code and hint follow why it waits now, like the words
        result["error"] = _card_error(stage, interrupted.get("owner"), reason, origin,
                                      interrupted.get("cause_said"))
    if status == "done":
        result.pop("error", None)
        result["output_ids"] = list(output_ids or [])
    if failure is not None:
        code, detail = failure
        try:
            hint = describe(code).hint
        except KeyError:
            hint = ""
        result["error"] = {"code": code, "hint": hint, "stage": stage}
        at = f"Interrupted at {stage}; " if stage else "Interrupted; "
        message = f"{at}the nightly run continued it, but it stopped again: {detail}"
    _write_card(db, str(mine["job_id"]), status, message, result, finished=_iso())


def settle_resumed(db: SqlDb, job_id: str, output_ids: Sequence[str], *, by: str = "nightly") -> None:
    """The job was continued and it finished: its card is done, with the outputs, and says who continued it
    (`by`: the nightly run, or `hand` for `studio run --resume`)."""
    _settle(db, job_id, "done", "resumed", output_ids=output_ids, by=by)


def settle_failed(db: SqlDb, job_id: str, code: str, detail: str) -> None:
    """The nightly continued the job and it failed for a reason of its own: the card shows that failure."""
    _settle(db, job_id, "failed", "retry", failure=(code, detail))


def settle_left_for_time(db: SqlDb, job_id: str, stage: str | None, limit_s: int | None) -> None:
    """The nightly continued the job and left it again before `stage` for time (card night-fits, S6): its card
    says so, NS-W211, and that the next nightly run continues it."""
    stopped = {"owner": OUT_OF_TIME_OWNER, "stage": stage, "limit_s": limit_s}
    _settle(db, job_id, "failed", "pending", stopped=stopped)


def settle_slept(db: SqlDb, job_id: str, stage: str | None) -> None:
    """The nightly continued the job and its stage stopped as the PC woke from a sleep (card night-fits-b):
    its card says so and that the next nightly run continues it."""
    stopped = {"owner": SLEPT_OWNER, "stage": stage, "cause": SLEPT_REASON, "cause_said": None}
    _settle(db, job_id, "failed", "pending", stopped=stopped)


def settle_needs_retry(db: SqlDb, job_id: str, reason: str) -> None:
    """The nightly cannot continue the job (its channel was paused, it is too old): the card says Retry."""
    _settle(db, job_id, "failed", "needs_retry", reason=reason)


# ---------------------------------------------------------------- recovery
@dataclass
class Interrupted:
    """A job a dead owner left behind."""

    job_id: str
    channel: str | None
    fmt: str | None
    stage: str | None
    origin: str
    run_id: str | None
    owner: str
    resumable: bool
    problem: str | None = None
    card_id: str | None = None
    dashboard_owned: bool = False
    #: what stopped its dead night (`nightcause.Cause`), for a job of the nightly run; None when not asked
    cause: Any = None

    @property
    def has_card(self) -> bool:
        """True when a Retry card stands for the job in the Queue (ours, or the dashboard's own)."""
        return self.card_id is not None or self.dashboard_owned


@dataclass
class Recovery:
    """What one `recover` call closed."""

    runs: list[dict[str, Any]] = field(default_factory=list)
    jobs: list[Interrupted] = field(default_factory=list)

    @property
    def any(self) -> bool:
        """True when something was closed."""
        return bool(self.runs or self.jobs)


def resume_problem(paths: Paths, rec: Mapping[str, Any], now: datetime | None = None) -> str | None:
    """Why the nightly cannot continue this job by itself (None when it can): its inputs must be valid."""
    now = now or _now()
    if rec.get("origin") != "daily":
        return "it was not started by the nightly run"
    info = job_info(paths, str(rec["id"]))
    if not info or not info.get("combo"):
        return "the files of the job are gone"
    from ns_studio.channels.crud import get_channel

    try:
        ch = get_channel(paths, str(info.get("channel") or ""))
    except Exception:  # noqa: BLE001 - StudioError (unknown channel) or a profile that no longer loads
        return "its channel no longer exists"
    if ch.status != "active":
        return "its channel is paused"
    if str(info.get("fmt")) != ch.format:
        return "its channel now makes another type of video"
    if rec.get("cannot_fit"):  # the night's time decision gave it up (card night-fits): its own sentence
        return str(rec["cannot_fit"])
    from ns_studio import nightfit

    anchor = age_anchor(paths, rec, info)
    if anchor is not None and nightfit.too_old((now - anchor).total_seconds() / 86400, RESUME_WITHIN_DAYS):
        if rec.get("progressed_at"):
            return f"no night has finished a stage of it for more than {RESUME_WITHIN_DAYS} days"
        return f"it is more than {RESUME_WITHIN_DAYS} days old"
    return None


def age_anchor(
    paths: Paths,
    rec: Mapping[str, Any],
    info: Mapping[str, Any] | None = None,
    *,
    lines: Sequence[Mapping[str, Any]] | None = None,
) -> datetime | None:
    """Where a job's age counts from (card night-fits, `nightfit.too_old`): when a stage of it last FINISHED
    (the ledger's `stage` lines, so a night Windows killed after it finished stages still counts, and the
    record's `progressed_at` written when the night left it), else its creation. A night that left it
    without finishing a stage does not make it younger. `lines`: the ledger already read (else it is read)."""
    if info is None:
        info = job_info(paths, str(rec["id"]))
    from ns_studio import ledger

    if lines is None:
        try:
            lines = ledger.read(paths)
        except Exception:  # noqa: BLE001 - an unreadable ledger: the record alone
            lines = []
    found = [t for t in (_parse(rec.get("progressed_at")), _parse(ledger.last_done_at(lines, str(rec["id"]))))
             if t is not None]
    return max(found) if found else _parse(info.get("created_at"))


def _has_output(db: SqlDb, job_id: str) -> bool:
    return db.one("select 1 from outputs where job_id = ? limit 1", (job_id,)) is not None


def job_finished(paths: Paths, db: SqlDb, job_id: str, fmt: str | None = None) -> bool:
    """Whether the job ran its whole plan: every stage of its format is `done` in `stage_state.json`.

    An `outputs` row is not enough: the outbox stage records the long video first and the Shorts after it,
    so a kill between the two leaves a video and a job that is not finished. Only a job with no readable stage
    state or no known plan (a record from before the stage runner, or a plan that cannot be named) falls back
    to "it has an output".
    """
    try:
        entries = json.loads((paths.job_dir(job_id) / "stage_state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        entries = None
    names: list[str] = []
    try:
        from ns_studio.orchestrator import stage_names

        names = stage_names(fmt or "", paths.job_dir(job_id))  # its own plan: gates only if it runs them
    except Exception:  # noqa: BLE001 - a plan that cannot be read: fall back to the output rows
        names = []
    if not isinstance(entries, dict) or not names:
        return _has_output(db, job_id)
    return all(isinstance(entries.get(n), dict) and entries[n].get("status") == "done" for n in names)


def _close_job(
    paths: Paths, db: SqlDb, rec: dict[str, Any], now: datetime, owner: str, cause: Any = None
) -> Interrupted | None:
    """Close one dead job: offer it in the Queue first, then record it as interrupted.

    The card comes first on purpose: the record is what tells the next `recover` that there is still
    something open, so when the database refuses the card (locked) the record stays open and the next call
    tries again, instead of losing the job for good. Raises what the database raised. `cause` is what stopped
    its dead night (`nightcause.Cause`): its card and record keep the sentence when it says more than the old
    words.
    """
    kind, said, _extra = _cause_fields(cause)
    # this stop's cause only: a cause an earlier stop of the same job left in its record is not this one's
    rec = {**rec, "cause": kind, "cause_said": said}
    job_id = str(rec["id"])
    info = job_info(paths, job_id)
    fmt = rec.get("fmt") or info.get("fmt")
    if job_finished(paths, db, job_id, fmt):  # the kill came after the job was made: nothing was interrupted
        write_record(paths, job_id, state="done", closed_at=_iso(now))
        return None
    if not info:  # killed before the job folder was written: there is nothing to continue or to tell
        write_record(paths, job_id, state="interrupted", closed_at=_iso(now), owner=owner)
        return None
    channel = rec.get("channel") or info.get("channel")
    stage = reached_stage(paths, job_id, fmt)
    origin = str(rec.get("origin") or "manual")
    closing = {
        **rec,
        "state": "interrupted",
        "interrupted_at_stage": stage,
        "closed_at": _iso(now),
        "owner": owner,
        "channel": channel,
        "fmt": fmt,
    }
    problem = resume_problem(paths, closing, now) if origin == "daily" else None
    item = Interrupted(
        job_id,
        channel,
        fmt,
        stage,
        origin,
        rec.get("run_id"),
        owner,
        origin == "daily" and problem is None,
        problem,
        cause=cause,
    )
    # Only a job `make_job` announced is a story job someone started; a series part or a job reopened by hand
    # (a record made by `hold` alone) has its own way back, so the Queue gets no second card for it.
    if has_card(closing):
        resume = "pending" if item.resumable else "needs_retry"
        created = _parse(info.get("created_at")) or now
        item.card_id = offer_job(
            db,
            job_id,
            closing,
            stage,
            resume=resume,
            reason=problem,
            when=_last_seen(paths, [job_id], created),
        )
        item.dashboard_owned = item.card_id is None
    write_record(
        paths,
        job_id,
        state="interrupted",
        interrupted_at_stage=stage,
        closed_at=_iso(now),
        owner=owner,
        channel=channel,
        fmt=fmt,
        cause=rec["cause"],
        cause_said=rec["cause_said"],
    )
    return item


def _last_seen(paths: Paths, job_ids: Sequence[str], floor: datetime, run_id: str | None = None) -> datetime:
    """The latest sign of life: the newest write to the job's stage state, job file or the run's record."""
    newest: float | None = None
    files = [
        job_info_path
        for job_id in job_ids
        for job_info_path in (paths.job_dir(job_id) / "stage_state.json", paths.job_dir(job_id) / "job.json")
    ]
    if run_id:
        files.append(record_path(paths, run_id))
    for f in files:
        with contextlib.suppress(OSError):
            m = f.stat().st_mtime
            newest = m if newest is None else max(newest, m)
    seen = datetime.fromtimestamp(newest, UTC) if newest is not None else floor
    return max(seen, floor)


def _close_run(
    paths: Paths,
    db: SqlDb,
    run_id: str,
    started: datetime | None,
    listed: Sequence[Mapping[str, Any]],
    owner: str,
    closed: Mapping[str, Interrupted],
    now: datetime,
    *,
    mode_note: str | None = None,
    cause: Any = None,
) -> dict[str, Any]:
    # `jobs` is what the run made (the key the Queue counts videos from); what it left half made is listed
    # apart, under `interrupted_jobs`, so an interrupted night never reads as "1 video made".
    summary: dict[str, Any] = {
        "interrupted": True,
        "owner": owner,
        "closed_at": _iso(now),
        "jobs": [],
        "interrupted_jobs": [],
    }
    kind, said, extra = _cause_fields(cause)
    if said is not None:  # what stopped it (`nightcause`): `run_state` and the morning note say it
        summary["cause"], summary["cause_said"] = kind, said
    for j in listed:
        job_id = str(j.get("job_id"))
        item = closed.get(job_id)
        earlier = read_record(paths, job_id) or {}
        if item is not None:
            summary["interrupted_jobs"].append(
                interrupted_entry(
                    job_id, j.get("channel") or item.channel, item.stage, has_card=item.has_card
                )
            )
        elif earlier.get("state") == "interrupted":
            summary["interrupted_jobs"].append(
                interrupted_entry(job_id, j.get("channel"), earlier.get("interrupted_at_stage"))
            )
        else:
            summary["jobs"].append({"job_id": job_id, "channel": j.get("channel")})
    floor = started or now
    seen = _last_seen(paths, [str(j.get("job_id")) for j in listed], floor, run_id)
    db.execute(
        "update runs set finished_at = ?, summary_json = ? where run_id = ? and finished_at is null",
        (_iso(seen), json.dumps(summary), run_id),
    )
    write_record(paths, run_id, state="interrupted", closed_at=_iso(now), owner=owner)
    out: dict[str, Any] = {"run_id": run_id, "owner": owner, "interrupted_jobs": summary["interrupted_jobs"]}
    if said is not None:
        out.update(cause=kind, said=said, extra=extra)
    return out


def _cause_fields(cause: Any) -> tuple[str | None, str | None, str | None]:
    """(kind, sentence, NS-W212 sentence) of a `nightcause.Cause` that says more than the old words, else
    three Nones."""
    if cause is None or not getattr(cause, "known", False):
        return None, None, None
    extra = getattr(cause, "extra", None)
    return str(cause.kind), str(cause.said), str(extra) if extra else None


def _recover_owned(
    paths: Paths,
    db: SqlDb,
    now: datetime,
    out: Recovery,
    log: NsLogger | None = None,
    explain: Any = None,
) -> None:
    """Close the dead jobs, then the dead runs. One job the database refuses (locked) never stops the others:
    it stays open, and so does the run that listed it, so the next call closes both. `explain(run_id)` says
    what stopped a dead nightly run (`nightcause`, asked once per run; None: not asked)."""
    records = list_records(paths)
    closed: dict[str, Interrupted] = {}
    stuck: set[str] = set()
    dead_runs = {
        str(rec["id"]) for rec in records
        if rec.get("kind") == "run" and rec.get("state") in OPEN_STATES
        and liveness(paths, str(rec["id"]), rec) == "dead"
    }
    causes: dict[str, Any] = {}

    def cause_of(run_id: Any) -> Any:
        if explain is None or not run_id or str(run_id) not in dead_runs:
            return None
        if str(run_id) not in causes:
            try:
                causes[str(run_id)] = explain(str(run_id))
            except Exception as e:  # noqa: BLE001 - a cause is never required: the old words stay
                causes[str(run_id)] = None
                if log is not None:
                    log.warn("night_cause_failed", str(e), run_id=str(run_id))
        return causes[str(run_id)]

    for rec in records:
        if rec.get("kind") != "job" or rec.get("state") not in OPEN_STATES:
            continue
        if liveness(paths, str(rec["id"]), rec) != "dead":
            continue
        pid = rec.get("pid")
        cause = cause_of(rec.get("run_id")) if rec.get("origin") == NIGHTLY_ORIGIN else None
        try:
            item = _close_job(paths, db, rec, now, f"process {pid} is gone" if pid else "gone", cause)
        except Exception as e:  # noqa: BLE001 - sqlite3 errors, a locked database: retried by the next call
            stuck.add(str(rec["id"]))
            if log is not None:
                log.warn("interrupted_job_not_closed", str(e), job_id=str(rec["id"]))
            continue
        if item is not None:
            closed[item.job_id] = item
            out.jobs.append(item)
    for rec in records:
        if rec.get("kind") != "run" or str(rec["id"]) not in dead_runs:
            continue
        run_id = str(rec["id"])
        if stuck & {str(j.get("job_id")) for j in rec.get("jobs") or [] if isinstance(j, dict)}:
            continue  # one of its jobs could not be closed yet: the run stays open with it
        row = db.one("select started_at, finished_at from runs where run_id = ?", (run_id,))
        if row is not None and row["finished_at"]:  # it finished; the process died before it let go
            write_record(paths, run_id, state="done", closed_at=_iso(now))
            continue
        pid = rec.get("pid")
        cause = cause_of(run_id)
        out.runs.append(
            _close_run(
                paths,
                db,
                run_id,
                _parse(row["started_at"]) if row else _parse(rec.get("started_at")),
                [j for j in rec.get("jobs") or [] if isinstance(j, dict)],
                f"process {pid} is gone" if pid else "gone",
                closed,
                now,
                cause=cause,
            )
        )
        if cause is not None:  # once, as the run is closed: what NS-W212's two-in-two-weeks counts
            from ns_studio import nightcause

            nightcause.record(paths, run_id, cause, now=now, log=log)


def _recover_legacy(
    paths: Paths, db: SqlDb, now: datetime, out: Recovery, log: NsLogger | None = None
) -> None:
    """Unfinished `runs` rows no owner file exists for (0.1.0 to 0.1.14): closed when older than the bound."""
    rows = list(
        db.query("select run_id, started_at, mode from runs where finished_at is null order by started_at")
    )
    for row in rows:
        run_id = str(row["run_id"])
        if read_record(paths, run_id) is not None:
            continue  # an owner is known: `_recover_owned` decided (a live run is left alone)
        started = _parse(row["started_at"])
        if started is None or now - started < timedelta(hours=LEGACY_IN_PROGRESS_H):
            continue  # may still be running under an older version
        closed: dict[str, Interrupted] = {}
        listed: list[dict[str, Any]] = []
        stuck = False
        if row["mode"] == "daily":
            for job_id, channel in _legacy_jobs(paths, db, str(row["started_at"])):
                rec = write_record(
                    paths,
                    job_id,
                    kind="job",
                    state="running",
                    origin="daily",
                    channel=channel,
                    fmt=job_info(paths, job_id).get("fmt"),
                    run_id=run_id,
                    announced_at=_iso(started),
                )
                try:
                    item = _close_job(paths, db, rec, now, "unknown")
                except Exception as e:  # noqa: BLE001 - a locked database: the whole row is retried next call
                    forget(paths, job_id)
                    stuck = True
                    if log is not None:
                        log.warn("interrupted_job_not_closed", str(e), job_id=job_id)
                    continue
                listed.append({"job_id": job_id, "channel": channel})
                if item is not None:
                    closed[job_id] = item
                    out.jobs.append(item)
        if stuck:
            continue  # the row stays open with its jobs, so the next call closes them together
        out.runs.append(_close_run(paths, db, run_id, started, listed, "unknown", closed, now))


def _legacy_jobs(paths: Paths, db: SqlDb, started_at: str) -> list[tuple[str, str]]:
    """Jobs a dead pre-0.1.15 nightly left half made: its own combos rows, no video, a job folder.

    "Its own" is decided by equality, never by a time window: a 0.1.14 nightly stamps its `runs.started_at`
    and the `combos.created_at` of every job it makes with the same `now` (`daily._run_daily` -> `make_job`
    -> `record_combo`). Only the nightly inserts a `runs` row, so a Run now, a `studio run` or a series part
    made after the killed night has no row of its own and lies inside any window up to the next night: it is
    not adopted (the nightly would continue it by itself, paid work nobody asked for). A job a dashboard card
    already names (a Run now that started in the same second) is the dashboard's, not adopted either.

    Known gap: a job that failed on its own earlier that night (QC fail, any error) is unfinished too, and a
    0.1.x job folder keeps no failure marker (the report that listed it is written only when the night ends),
    so it cannot be told from an interrupted one and is adopted. See the follow-up in the pull request.
    """
    sql = (
        "select job_id, channel, min(created_at) c from combos where created_at = ? group by job_id, channel"
    )
    out: list[tuple[str, str]] = []
    for r in db.query(sql + " order by c", (started_at,)):
        job_id = str(r["job_id"])
        info = job_info(paths, job_id)
        if (
            read_record(paths, job_id) is not None
            or not info
            or _rows_for(db, job_id)
            or job_finished(paths, db, job_id, info.get("fmt"))
        ):
            continue
        out.append((job_id, str(r["channel"])))
    return out


def _way_back(j: Interrupted) -> str:
    """How the owner continues a job: its Queue card when there is one, else the command that reopens it."""
    if j.has_card:
        return "press Retry in the Queue"
    return f"run studio run --resume {j.job_id}"


def recovery_message(
    out: Recovery, *, outcomes: Mapping[str, tuple[str, str | None]] | None = None
) -> str | None:
    """The morning note for what `recover` closed, or None when nobody needs telling.

    `outcomes` is what the nightly then did with a resumable job, by job id: `("resumed", None)`,
    `("failed", <code>)`, `("waiting", None)` (the GPU was busy), `("paused", None)` (its channel was paused
    during the night), `("busy", None)` (a Retry from the Queue was already running it), or the night's time
    decision with its one sentence: `("out_of_time", <sentence>)`, `("gave_up", <sentence>)`, or
    `("slept", <sentence>)` (its stage stopped as the PC woke from a sleep). Without an outcome the note never
    claims the job is being continued: it says when it will be, and how to continue it by hand.

    A night's job (or run) whose stop `nightcause` explained starts with that sentence ("Windows restarted
    the PC at 03:12 to finish installing updates, during the night's run, while <channel>'s video was at
    <stage>.") instead of "The night's run was interrupted (power cut, crash or restart)", which stays for
    a night that left no evidence (an older version's).
    """
    outcomes = outcomes or {}
    night: list[str] = []  # jobs of the nightly run whose stop nothing explains
    explained: list[str] = []  # jobs (or runs) of the nightly run with what stopped them (`nightcause`)
    extra: str | None = None
    hand: list[str] = []  # jobs the owner started: no night's run was interrupted for them
    unknown = False
    for j in out.jobs:
        if j.dashboard_owned:
            continue  # the dashboard's own card says it
        unknown = unknown or j.owner == "unknown"
        where = f"stopped at {j.stage}" if j.stage else "stopped"
        what, code = outcomes.get(j.job_id, ("", None))
        if what == "resumed":
            tail = "The nightly run continued it and it finished."
        elif what == "failed":
            tail = (
                f"The nightly run continued it, but it stopped again ({code}); {_way_back(j)} to try again."
            )
        elif what == "waiting":
            tail = (
                f"The GPU was busy, so it waits for the next nightly run; or {_way_back(j)}. "
                f"{WHILE_NIGHTLY_ON}"
            )
        elif what == "busy":
            tail = "It is being continued right now from the Queue."
        elif what in ("out_of_time", "gave_up", "slept") and code:  # the night's decision, its one sentence
            tail = f"{code[:1].upper()}{code[1:]}."
        elif what == "out_of_time":
            tail = (
                "The night's run had too little time left to continue it inside Windows' limit for the "
                "nightly task, so the next nightly run continues it first; "
                f"or {_way_back(j)}. {WHILE_NIGHTLY_ON}"
            )
        elif what == "paused":
            tail = (
                "Its channel was paused during the night, so it waits; resume the channel and the next "
                f"nightly run continues it, as long as the job started no more than {RESUME_WITHIN_DAYS} "
                f"days before that run; an older one is not continued by the night, so {_way_back(j)}. "
                f"{WHILE_NIGHTLY_ON}"
            )
        elif j.resumable:
            tail = f"It continues at the next nightly run, or {_way_back(j)}. {WHILE_NIGHTLY_ON}"
        else:
            tail = (
                "Press Retry in the Queue to continue it" if j.has_card else f"To continue it, {_way_back(j)}"
            ) + (f" ({j.problem})." if j.problem else ".")
        _kind, said, more = _cause_fields(j.cause)
        if j.origin == NIGHTLY_ORIGIN and said:  # what stopped the night, then what became of the job
            video = f"while {j.channel or j.job_id}'s video was at {j.stage}" if j.stage else (
                f"while {j.channel or j.job_id}'s video was being made")
            explained.append(f"{said}, {video}. {tail}")
            extra = extra or more
            continue
        (night if j.origin == NIGHTLY_ORIGIN else hand).append(f"{j.channel or j.job_id} {where}. {tail}")
    if not night and not explained and out.runs:
        for r in out.runs:
            if r.get("said"):
                explained.append(f"{r['said']}. It had not made a video yet, so nothing is lost.")
                extra = extra or r.get("extra")
        bare = [r for r in out.runs if not r.get("said")]
        if bare:
            unknown = unknown or any(r.get("owner") == "unknown" for r in bare)
            night.append("It had not made a video yet, so nothing is lost.")
    parts: list[str] = list(explained)
    if extra:  # NS-W212: a second restart for updates within two weeks, said once
        parts.append(f"{extra}.")
    if night:  # a run of the night was closed, or a job it had made, and nothing explains what stopped it
        parts.append("The night's run was interrupted (power cut, crash or restart). " + " ".join(night))
    if hand:
        head = "A job you started yourself was" if len(hand) == 1 else "Jobs you started yourself were"
        parts.append(f"{head} interrupted (power cut, crash or restart). " + " ".join(hand))
    if not parts:
        return None
    text = " ".join(parts)
    return text + " (owner unknown: it was started by an older version.)" if unknown else text


def leave_note(paths: Paths, log: NsLogger | None, message: str) -> bool:
    """Leave `message` as a morning note (`run/morning_note.json`) unless notifications are off."""
    if not notifications_enabled(paths):
        return False
    try:
        from ns_supervisor.notify import leave_note as _leave

        _leave(paths, "Nightshift", message)
    except Exception as e:  # noqa: BLE001 - a note must never stop a night or an engine start
        if log is not None:
            log.warn("morning_note_failed", str(e))
        return False
    return True


def recover(
    paths: Paths,
    db: SqlDb,
    *,
    now: datetime | None = None,
    log: NsLogger | None = None,
    note: bool = True,
    events: Any = None,
    boot: datetime | None = None,
) -> Recovery:
    """Close what a dead process left open: unfinished runs, half-made jobs; tell the owner once.

    Safe to call from anywhere, any number of times: a run or job with a live owner is left alone, and what
    was closed is closed (a second call finds nothing). A call that meets another one at work waits for it
    (`RECOVER_WAIT_S`) and then scans, so what it returns is what it closed itself, possibly nothing. The
    nightly passes `note=False` and leaves its own note after it has tried to continue the jobs
    (`leave_recovery_note`), so the note says what happened.

    A dead nightly run is explained by `nightcause.cause` (card night-fits-b) from its ledger, its beat,
    Windows' System log (`events`: `winevents.SystemLog()` by default, refused under pytest, which reads as
    "could not be read") and the PC's last start (`boot`: psutil's by default).
    """
    now = now or _now()
    out = Recovery()

    def explain(run_id: str) -> Any:
        from ns_common.winevents import SystemLog
        from ns_studio import nightcause

        source = events if events is not None else SystemLog()
        return nightcause.cause(paths, run_id, source, boot if boot is not None else _boot_time(), log=log)

    guard = FileLock(owners_dir(paths) / "recover.lock")  # the engine and the nightly may start together
    deadline = time.monotonic() + RECOVER_WAIT_S
    while True:
        try:
            _acquire(guard)
            break
        except AlreadyRunning:
            # Another process is closing the same things right now. Leaving at once would let the nightly plan
            # before that process has written the job's record (it writes it last): the job would not be
            # continued tonight and a second master would be started. Wait, then scan.
            if time.monotonic() >= deadline:
                return out
            time.sleep(0.1)
        except OSError:
            break  # no lock file can be made here, also after the tries: carry on unguarded
    try:
        _recover_owned(paths, db, now, out, log, explain)
        _recover_legacy(paths, db, now, out, log)
        _prune(paths, now)
    finally:
        guard.release()
    if out.any:
        if log is not None:
            log.warn(
                "interrupted_closed",
                f"{len(out.runs)} run(s) and {len(out.jobs)} job(s) closed as interrupted",
                runs=[r["run_id"] for r in out.runs],
                jobs=[j.job_id for j in out.jobs],
            )
        if note:
            leave_recovery_note(paths, log, out)
    return out


def _boot_time() -> datetime | None:
    """When this PC last started (psutil), or None when it cannot be read."""
    try:
        import psutil

        return datetime.fromtimestamp(float(psutil.boot_time()), UTC)
    except Exception:  # noqa: BLE001 - not known: the cause says less
        return None


def leave_recovery_note(
    paths: Paths,
    log: NsLogger | None,
    out: Recovery,
    outcomes: Mapping[str, tuple[str, str | None]] | None = None,
) -> bool:
    """Leave the morning note for `out` (see `recovery_message`); False when there was nothing to say."""
    message = recovery_message(out, outcomes=outcomes)
    return leave_note(paths, log, message) if message else False


def pending_resumes(paths: Paths, db: SqlDb, now: datetime | None = None) -> list[dict[str, Any]]:
    """Interrupted jobs of the nightly run that it can continue: records in state `interrupted` whose inputs
    are still valid. One that can no longer be continued is left for Retry and its card says why."""
    now = now or _now()
    out: list[dict[str, Any]] = []
    for rec in list_records(paths):
        if rec.get("kind") != "job" or rec.get("state") != "interrupted" or rec.get("origin") != "daily":
            continue
        job_id = str(rec["id"])
        if job_finished(paths, db, job_id, rec.get("fmt")):
            write_record(paths, job_id, state="done")
            continue
        problem = resume_problem(paths, rec, now)
        if problem is not None:
            if rec.get("resume_problem") != problem:
                write_record(paths, job_id, resume_problem=problem)
                settle_needs_retry(db, job_id, problem)
            continue
        if rec.get("resume_problem"):  # the problem is gone: a later one, the same again, is told again
            write_record(paths, job_id, resume_problem=None)
        out.append(rec)
    return out


def _prune(paths: Paths, now: datetime) -> None:
    """Forget the records of runs and jobs that ended long ago (they only explain `studio status`)."""
    cutoff = now - timedelta(days=KEEP_ENDED_DAYS)
    for rec in list_records(paths):
        if rec.get("state") not in ("done", "failed", "interrupted"):
            continue
        ended = _parse(rec.get("ended_at") or rec.get("closed_at"))
        if ended is not None and ended < cutoff:
            forget(paths, str(rec["id"]))


def status_lines(paths: Paths, db: SqlDb, channel: str) -> list[str]:
    """Lines for `studio status`: a job being made right now, and the interrupted ones that need Retry."""
    lines: list[str] = []
    for rec in list_records(paths):
        if rec.get("kind") != "job" or rec.get("channel") != channel:
            continue
        job_id = str(rec["id"])
        state = liveness(paths, job_id, rec)
        if state == "live":
            lines.append(f"  working: {live_description(paths, job_id)}")
            if job_id in unprobed_owners(paths):
                lines.append(f"  held: {unprobed_message(job_id)}")
        elif (
            state in ("dead", "interrupted")
            and job_info(paths, job_id)
            and not job_finished(paths, db, job_id, rec.get("fmt"))
        ):
            stage = rec.get("interrupted_at_stage") or reached_stage(paths, job_id, rec.get("fmt"))
            at = f" at stage {stage}" if stage else ""
            # A card stands for a job `make_job` announced, but only once `recover` has closed it (the next
            # start of Nightshift, `studio run` or nightly run; this command closes nothing). Until then the
            # Queue has no Retry for it and the one way that works now is the command. Only the nightly's
            # own jobs are continued by the nightly.
            if has_card(rec) and state == "interrupted":
                way = "press Retry in the Queue"
                if rec.get("origin") == NIGHTLY_ORIGIN:
                    way += f" or the next nightly run continues it{IF_NIGHTLY_ON}"
            elif has_card(rec):
                way = (
                    f"its process is gone; continue it now with studio run --resume {job_id}, or press Retry"
                    " in the Queue after the next start of Nightshift"
                )
                if rec.get("origin") == NIGHTLY_ORIGIN:
                    way += f"; the next nightly run continues it too{IF_NIGHTLY_ON}"
            else:
                way = f"continue it with studio run --resume {job_id}"
            lines.append(f"  interrupted: job {job_id} stopped{at}; {way}")
    return lines
