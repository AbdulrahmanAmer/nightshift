"""A damaged `studio.db` is found at start, set aside and replaced by the newest good nightly copy.

Before: nothing checked the database. A torn file after a power cut, a full disk or a bad sector made every
job of every night fail with an uncoded error until the owner found `ns restore`, which needs a zip they may
not have; and the nightly copies (`Db.backup`) were written in place and never checked, so a damaged live
file was copied over the good copies, fourteen nights later none was good.

Damage is decided by evidence, never guessed (`check`):
* `PRAGMA quick_check` on a read-only connection (busy timeout `CHECK_BUSY_MS`, stopped after
  `CHECK_LIMIT_S`) once per process and file, when the process first opens it (`open_studio_db`). Damaged:
  rows other than `ok`, or SQLite's own CORRUPT / NOTADB answer ("database disk image is malformed", "file
  is not a database"). A file of zero bytes, or no file at all, is "lost" when this home has a history
  (a nightly copy, or a job folder with finished videos); without one it is a new install, made as before.
* NOT damage: a file another program holds or locks (BUSY, LOCKED, CANTOPEN, IOERR: it is opened as before
  and checked at the next start); a schema this SQLite cannot read ("malformed database schema", "unsupported
  file format": a newer program wrote it); a check that ran out of time; a rollback journal left behind
  (a writing connection rolls it back, as every open does, and the check is made on that connection). A
  `-wal` whose frames are torn is not damage either: SQLite checks every frame's checksum and drops the ones
  that fail, so the file it reads is whole (what those frames held is lost, and nothing can tell).
* The full `PRAGMA integrity_check` is made of each nightly copy before it is used, and of each copy when it
  is made (`Db.backup`), so a damaged copy is never kept as good.

The restore (`_restore`, steps D4 to D9 of the card) runs under `data/studio.db.restore.lock`: one process
restores, the others wait for it (`LOCK_WAIT_S`) and then open the restored file. A record,
`run/db-restore.json`, is written before anything moves and says how far the restore came (`step`); whoever
opens the database next finishes it from there, so a restore that starts is always finished, and no step
does anything twice:
  chosen    the copy to use is named; nothing has moved yet
  set_aside studio.db (and -wal, -shm) are in data/damaged/studio-<stamp>.db*; they are never deleted
  in_place  the copy (or a new, empty database when no copy is good) is studio.db; its hash is recorded
            before the rename, so a start that finds a studio.db at `set_aside` knows whether it is ours
  put_back  migrated, the videos made since the copy are back in the Queue; the list is in the record
then `run/db-restored.json` says what was used and what is missing, the morning note NS-W213 is left (once:
the same words already waiting are not added again) and the record is removed.

Windows refuses to rename a file another process has open, so a restore never replaces a database another
process uses: NS-E209 with Stop All as the way out (`_refused`). The same code, with the disk named, when the
disk is full. Each NS-E209 is a morning note too, once while the same words wait.

The morning notes belong to `ns_supervisor.notify`, which `ns_common` cannot import: every opener passes its
`note` (`ns_studio.notifications.db_note` for the studio and the dashboard).
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ns_common.atomic import copy_atomic, quarantine, read_text_retry, write_text_atomic
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_common.singleton import AlreadyRunning, FileLock

#: Leaves one morning note (the owner of the notes is `ns_supervisor.notify`, out of `ns_common`'s reach).
NoteFn = Callable[[str], object]

DAMAGED_CODE = "NS-E209"
RESTORED_CODE = "NS-W213"
#: The read-only check waits this long for a writer that holds the file (then: not checked, not damage).
CHECK_BUSY_MS = 5000
#: A check that runs longer than this is stopped (SQLite's progress handler) and counts as not checked. A
#: quick check of a 70 MB studio.db takes about 0.3 s on the laptop; this bound is for a disk that crawls.
CHECK_LIMIT_S = 60.0
#: How long a process waits for another one that is restoring (card D3), then NS-E209 "still being replaced".
LOCK_WAIT_S = 60.0
LOCK_POLL_S = 0.2
#: How long the set-aside tries again while Windows refuses the rename. Another process's own check holds the
#: file for well under a second; a window that keeps the database open holds it for good (NS-E209).
RENAME_WAIT_S = 10.0
RENAME_POLL_S = 0.25
#: A nightly copy must hold these tables to count as a copy of studio.db (an older copy lacks newer columns,
#: which `Db.migrate` adds).
REQUIRED_TABLES = frozenset({"channels", "runs", "outputs", "combos"})
#: `ns doctor` shows the restore while its record is younger than this.
RESTORED_SHOWN_DAYS = 7

CHECK_FILE = "db-check.json"         # run/: the last check of studio.db (time, result)
RECORD_FILE = "db-restore.json"      # run/: a restore in progress (its step)
RESTORED_FILE = "db-restored.json"   # run/: the last restore (what was used, what is missing)
LOCK_FILE = "studio.db.restore.lock"  # data/
DAMAGED_DIR = "damaged"              # data/
RESTORING_SUFFIX = ".restoring"
_BACKUP_NAME = re.compile(r"^studio-(\d{8})\.db$")
_SIDE = ("", "-wal", "-shm")

# verdicts of `check`
OK = "ok"
DAMAGED = "damaged"
LOST = "lost"            # zero bytes, or no file, in a home that has a history
NEW = "new"              # no file (or zero bytes) and no history: a new install
HELD = "not_checked"     # held, locked or unreadable by another program: not damage
NEWER = "newer"          # a schema this SQLite cannot read: not damage
SLOW = "too_slow"        # the check ran out of time: not damage


@dataclass
class Check:
    """What one check found: `verdict` (above) and SQLite's words."""

    verdict: str
    detail: str = ""
    seconds: float = 0.0

    @property
    def damaged(self) -> bool:
        return self.verdict in (DAMAGED, LOST)


# ------------------------------------------------------------------------------------------- evidence
def classify(e: sqlite3.Error) -> str:
    """The verdict an SQLite error is evidence of (DAMAGED only for SQLite's own corruption answers)."""
    text = str(e).lower()
    name = str(getattr(e, "sqlite_errorname", "") or "")
    if "malformed database schema" in text or "unsupported file format" in text:
        return NEWER
    if name.startswith("SQLITE_CORRUPT") or name == "SQLITE_NOTADB":
        return DAMAGED
    if "database disk image is malformed" in text or "file is not a database" in text:
        return DAMAGED
    if text == "interrupted" or name == "SQLITE_INTERRUPT":
        return SLOW
    return HELD


def is_damage_error(e: BaseException | None) -> bool:
    """True when `e`, or an error it was raised from, is SQLite saying the database file is damaged."""
    seen = 0
    while e is not None and seen < 10:
        if isinstance(e, sqlite3.DatabaseError) and classify(e) == DAMAGED:
            return True
        e = e.__cause__ or e.__context__
        seen += 1
    return False


def _run(conn: sqlite3.Connection, sql: str) -> list[Any]:
    """One check statement (a seam: a test runs it slowly to reach the time bound)."""
    return [r[0] for r in conn.execute(sql).fetchall()]


def _pragma(uri: str, pragma: str, *, busy_ms: int, limit_s: float) -> list[Any]:
    """`PRAGMA <pragma>` on its own connection, stopped at `limit_s`: an integrity check is one long step of
    SQLite's engine, so the bound is `Connection.interrupt` from a timer thread (repeated every 50 ms past
    the deadline, as an interrupt between two statements does nothing); stopped, it raises "interrupted"."""
    conn = sqlite3.connect(uri, uri=True, timeout=busy_ms / 1000, isolation_level=None,
                           check_same_thread=False)
    done = threading.Event()

    def bound() -> None:
        if done.wait(limit_s):
            return
        while not done.is_set():
            conn.interrupt()
            done.wait(0.05)

    timer = threading.Thread(target=bound, name="db-check-bound", daemon=True)
    try:
        conn.execute(f"PRAGMA busy_timeout = {int(busy_ms)}")
        timer.start()
        return _run(conn, f"PRAGMA {pragma}")
    finally:
        done.set()
        if timer.is_alive():
            timer.join()
        conn.close()


def _rows_verdict(rows: list[Any], started: float) -> Check:
    took = round(time.monotonic() - started, 3)
    if rows == ["ok"]:
        return Check(OK, "", took)
    first = str(rows[0]).splitlines()[:2] if rows else ["no answer"]
    return Check(DAMAGED, " / ".join(first)[:300], took)


def check(path: Path, *, full: bool = False, fixed: bool = False) -> Check:
    """`PRAGMA quick_check` (or `integrity_check` with `full`) of `path`, never writing to it.

    `fixed`: the file is a copy nobody writes (a nightly copy, a copy being put in place): read with
    `immutable=1`, so no `-wal`/`-shm` is made beside it. A live file is read with `mode=ro` (its `-wal` is
    read too). A hot rollback journal needs a writer to roll it back; then the check is made on a writing
    connection, as the first open would do anyway.
    """
    started = time.monotonic()
    try:
        st = path.stat()
    except FileNotFoundError:
        return Check(NEW, "no file")
    except OSError as e:
        return Check(HELD, f"{type(e).__name__}: {e}")
    if not stat.S_ISREG(st.st_mode):  # a folder of that name: opened as before, which says so (NS-E327)
        return Check(HELD, "not a file")
    if st.st_size == 0:
        return Check(NEW, "zero bytes")
    pragma = "integrity_check" if full else "quick_check"
    mode = "immutable=1" if fixed else "mode=ro"
    try:
        rows = _pragma(f"{path.as_uri()}?{mode}", pragma, busy_ms=CHECK_BUSY_MS, limit_s=CHECK_LIMIT_S)
    except sqlite3.Error as e:
        name = str(getattr(e, "sqlite_errorname", "") or "")
        if not fixed and name in ("SQLITE_READONLY_ROLLBACK", "SQLITE_READONLY_RECOVERY"):
            try:
                rows = _pragma(f"{path.as_uri()}?mode=rw", pragma, busy_ms=CHECK_BUSY_MS,
                               limit_s=CHECK_LIMIT_S)
            except sqlite3.Error as e2:
                return Check(classify(e2), str(e2), round(time.monotonic() - started, 3))
            return _rows_verdict(rows, started)
        return Check(classify(e), str(e), round(time.monotonic() - started, 3))
    return _rows_verdict(rows, started)


def has_history(paths: Paths) -> bool:
    """True when this home made videos before: a nightly copy, or a job folder with an outbox manifest."""
    try:
        if any(_BACKUP_NAME.match(p.name) for p in paths.backup_dir.iterdir()):
            return True
    except OSError:
        pass
    try:
        return any((j / "outbox" / "manifest.json").is_file() for j in paths.jobs_dir.iterdir())
    except OSError:
        return False


def assess(paths: Paths) -> Check:
    """The live studio.db: `check`, with a missing or empty file read against the home's history."""
    found = check(paths.db_path)
    if found.verdict == NEW and has_history(paths):
        return Check(LOST, f"{found.detail}, while this home has nightly copies or finished videos")
    return found


# ------------------------------------------------------------------------------------------- the files
def run_dir(paths: Paths) -> Path:
    return paths.home / "run"


def damaged_dir(paths: Paths) -> Path:
    return paths.data / DAMAGED_DIR


def record_path(paths: Paths) -> Path:
    return run_dir(paths) / RECORD_FILE


def restored_path(paths: Paths) -> Path:
    return run_dir(paths) / RESTORED_FILE


def check_path(paths: Paths) -> Path:
    return run_dir(paths) / CHECK_FILE


def _now() -> datetime:
    return datetime.now(UTC)


def _local(at: datetime | float) -> str:
    when = datetime.fromtimestamp(at, UTC) if isinstance(at, float | int) else at
    return when.astimezone().strftime("%Y-%m-%d at %H:%M")


def read_json(path: Path) -> dict[str, Any] | None:
    """A JSON object from `path`; None when absent or unreadable."""
    try:
        data = json.loads(read_text_retry(path))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(data, indent=2) + "\n")


def write_check(paths: Paths, found: Check, log: NsLogger | None, **extra: Any) -> None:
    """`run/db-check.json`: when studio.db was last checked and what was found (`ns doctor` shows it)."""
    data = {"at": _now().isoformat(timespec="microseconds"), "result": found.verdict, "detail": found.detail,
            "seconds": found.seconds, "pid": os.getpid(), **extra}
    try:
        _write(check_path(paths), data)
    except OSError as e:  # a record of the check only: never a reason to stop
        if log is not None:
            log.warn("db_check_not_recorded", str(e))


def read_record(paths: Paths) -> dict[str, Any] | None:
    """The restore in progress, or None. A record that cannot be read is set aside as `.bad` and read as a
    restore from its first step (nothing it may have moved is lost: every move is to `data/damaged`)."""
    path = record_path(paths)
    if not path.exists():
        return None
    rec = read_json(path)
    if rec is None or not isinstance(rec.get("step"), str):
        quarantine(path)
        return {"step": "chosen", "unreadable_record": True}
    return rec


def _backups(paths: Paths) -> list[tuple[Path, str]]:
    try:
        names = sorted((p for p in paths.backup_dir.iterdir() if _BACKUP_NAME.match(p.name)), reverse=True)
    except OSError:
        return []
    return [(p, _BACKUP_NAME.match(p.name).group(1)) for p in names]  # type: ignore[union-attr]


def good_copy(path: Path) -> str | None:
    """None when `path` passes the full check and holds studio.db's tables, else why not."""
    found = check(path, full=True, fixed=True)
    if found.verdict != OK:
        return f"{found.verdict}: {found.detail}"
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?immutable=1", uri=True)
        try:
            tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
        finally:
            conn.close()
    except sqlite3.Error as e:
        return f"{classify(e)}: {e}"
    missing = REQUIRED_TABLES - tables
    return f"not a copy of studio.db (no {', '.join(sorted(missing))})" if missing else None


def choose(paths: Paths, log: NsLogger | None) -> tuple[Path | None, list[str]]:
    """D4: the newest nightly copy that passes the full check, and the names of the ones that did not."""
    refused: list[str] = []
    for path, _day in _backups(paths):
        why = good_copy(path)
        if why is None:
            return path, refused
        refused.append(path.name)
        if log is not None:
            log.warn("db_backup_not_good", why, backup=str(path))
    return None, refused


# ------------------------------------------------------------------------------------------- messages
NOT_IN_THE_COPY = (
    "Not in that copy, so to be done again if you need them: what you approved, rejected or marked posted "
    "since then (and the links of videos posted since then), analytics files imported since then (import "
    "them again), ideas you added, videos added to the Clipper, requests made in the Dashboard since then, "
    "and the story-repeat history of those days. Your settings and channels are files and are not affected."
)
GONE_HISTORY = (
    "Its history before now is gone: approvals, posted links, imported analytics, ideas, Clipper imports, "
    "Dashboard requests and the story-repeat history. Your settings and channels are files and are not "
    "affected."
)
KEPT = "The damaged file is kept in data\\damaged for the diagnostics."
BROKE_MID_RUN = (
    "The database became damaged during tonight's run, so the night stopped. Close every Nightshift window "
    "(Stop All), then open the Dashboard: it replaces the database with the newest good copy."
)
BROKE_IN_DASHBOARD = (
    "Nightshift's database became damaged. Close every Nightshift window (Stop All), then open the "
    "Dashboard: it replaces the database with the newest good copy."
)
WAY_OUT_WINDOW = (
    "Close every Nightshift window (Stop All), then open the Dashboard: it puts back the newest good copy."
)
WAY_OUT_RETRY = (
    "Close every Nightshift window (Stop All), then open the Dashboard: Nightshift tries again. If it still "
    "fails, run 'ns doctor --bundle' and send the zip."
)


def _found_words(why: str) -> str:
    return {"lost": "was missing or empty"}.get(why, "was damaged")


def restored_message(info: dict[str, Any]) -> str:
    """The morning note of a finished restore (card D9), true of what `info` says was done."""
    found = _local(datetime.fromisoformat(str(info["found_at"])))
    words = _found_words(str(info.get("why")))
    n = len(info.get("put_back") or [])
    videos = f"{n} video{'' if n == 1 else 's'}"
    if info.get("backup"):
        made = _local(float(info["backup_time"]))
        head = (f"{RESTORED_CODE}: Nightshift's database {words} (found {found}) and was replaced by the "
                f"nightly copy made {made}.")
        back = (f" Since that copy, the {videos} you made {'is' if n == 1 else 'are'} back in the Queue as "
                "'to review': if you had approved or posted any of them, do it again in the Queue." if n
                else " No video was made since that copy.")
        tail = f" {NOT_IN_THE_COPY}"
    else:
        refused = info.get("refused") or []
        none = (f"none of its {len(refused)} nightly copies passed the check" if refused
                else "there was no nightly copy of it yet")
        head = (f"{RESTORED_CODE}: Nightshift's database {words} (found {found}) and {none}, so a new one "
                "was made.")
        back = (f" The videos are not lost: the {videos} found in their job folders "
                f"{'is' if n == 1 else 'are'} in the Queue as 'to review'; if you had approved or posted any "
                "of them, do it again in the Queue." if n
                else " No finished video was found in the job folders.")
        tail = f" {GONE_HISTORY}"
    unread = int(info.get("unreadable") or 0)
    gone = int(info.get("gone") or 0)
    extra = ""
    if unread:
        extra += (f" {unread} more video{'' if unread == 1 else 's'} could not be put back because the "
                  "upload sheet beside it cannot be read; the video files are still in the Outbox folder.")
    if gone:
        verb = "is" if gone == 1 else "are"
        extra += (f" {gone} video{'' if gone == 1 else 's'} made since then {verb} no longer in the Outbox "
                  "(rejected or moved) and stayed out of the Queue.")
    kept = f" {KEPT}" if info.get("damaged") else ""
    return head + back + extra + tail + kept


def _failed(paths: Paths, log: NsLogger | None, note: NoteFn, why: str, way_out: str, **data: Any) -> NsError:
    """NS-E209 for this start: logged, recorded for `ns doctor`, said once as a morning note, and raised."""
    sentence = f"{DAMAGED_CODE}: Nightshift's database is damaged and could not be replaced: {why}. {way_out}"
    if log is not None:
        log.error("db_restore_failed", sentence, code=DAMAGED_CODE, **data)
    write_check(paths, Check("restore_failed", sentence), log)
    _say(note, sentence, log)
    return NsError(DAMAGED_CODE, why, hint=way_out, said=sentence)


def _say(note: NoteFn, message: str, log: NsLogger | None) -> None:
    try:
        note(message)
    except Exception as e:  # noqa: BLE001 - the note is a courtesy on top of the record and the error
        if log is not None:
            log.warn("db_note_failed", str(e), message=message)


def _disk_full(e: BaseException) -> bool:
    if isinstance(e, OSError):
        return e.errno == errno.ENOSPC or getattr(e, "winerror", None) in (39, 112)
    if isinstance(e, sqlite3.Error):
        return getattr(e, "sqlite_errorname", "") == "SQLITE_FULL" or "disk is full" in str(e).lower()
    return False


def _refused(e: OSError) -> bool:
    """Windows' answer to renaming a file another process has open (sharing violation, access denied)."""
    return isinstance(e, PermissionError) or getattr(e, "winerror", None) in (5, 32, 33)


# ------------------------------------------------------------------------------------------- the lock
@contextlib.contextmanager
def _restore_lock(paths: Paths, log: NsLogger | None, note: NoteFn) -> Iterator[None]:
    """D3: one process restores; another waits for it up to `LOCK_WAIT_S`, then NS-E209."""
    lock = FileLock(paths.data / LOCK_FILE)
    deadline = time.monotonic() + LOCK_WAIT_S
    waited = False
    while True:
        try:
            lock.acquire()
            break
        except AlreadyRunning as e:
            if not waited and log is not None:
                log.info("db_restore_wait", "another Nightshift program is replacing the database",
                         owner=e.owner)
            waited = True
            if time.monotonic() >= deadline:
                raise _failed(paths, log, note, "another Nightshift program has been replacing it for more "
                              f"than {LOCK_WAIT_S:.0f} seconds", WAY_OUT_RETRY, owner=e.owner) from None
            time.sleep(LOCK_POLL_S)
        except OSError as e:  # the lock file itself cannot be made: the folder, the disk
            why = "the disk is full" if _disk_full(e) else f"its lock file cannot be made ({e})"
            raise _failed(paths, log, note, why, WAY_OUT_RETRY) from e
    try:
        yield
    finally:
        lock.release()


# ------------------------------------------------------------------------------------------- the opener
def ensure_sound(paths: Paths, log: NsLogger | None, note: NoteFn) -> Check:
    """D0 to D9: leave studio.db sound, or raise NS-E209. Returns what the check found."""
    if read_record(paths) is None:
        found = assess(paths)
        write_check(paths, found, log)
        if not found.damaged:
            if found.verdict in (HELD, NEWER, SLOW) and log is not None:
                log.info("db_not_checked", f"{found.verdict}: {found.detail}; checked at the next start")
            return found
        if log is not None:
            log.warn("db_damaged", found.detail, verdict=found.verdict)
    with _restore_lock(paths, log, note):
        rec = read_record(paths)
        if rec is None:  # another process may have restored it while this one waited
            found = assess(paths)
            write_check(paths, found, log)
            if not found.damaged:
                return found
            rec = _start(paths, log, note, found)
        _finish(paths, log, note, rec)
    found = assess(paths)
    write_check(paths, found, log, restored=True)
    return found


def _start(paths: Paths, log: NsLogger | None, note: NoteFn, found: Check) -> dict[str, Any]:
    """D4, then D5's record: written before anything moves."""
    backup, refused = choose(paths, log)
    now = _now()
    rec: dict[str, Any] = {
        "step": "chosen", "why": found.verdict, "detail": found.detail,
        "found_at": now.isoformat(timespec="seconds"), "stamp": now.strftime("%Y%m%d-%H%M%S"),
        "backup": str(backup) if backup else None,
        "backup_time": backup.stat().st_mtime if backup else None, "refused": refused, "pid": os.getpid(),
    }
    try:
        _write(record_path(paths), rec)
    except OSError as e:
        why = "the disk is full" if _disk_full(e) else f"the record of the restore cannot be written ({e})"
        raise _failed(paths, log, note, why, WAY_OUT_RETRY) from e
    return rec


def _save(paths: Paths, rec: dict[str, Any], log: NsLogger | None, note: NoteFn) -> None:
    try:
        _write(record_path(paths), rec)
    except OSError as e:
        why = "the disk is full" if _disk_full(e) else f"the record of the restore cannot be written ({e})"
        raise _failed(paths, log, note, why, WAY_OUT_RETRY) from e


def _finish(paths: Paths, log: NsLogger | None, note: NoteFn, rec: dict[str, Any]) -> None:
    """Run the record's remaining steps in order; each one ends by saving the next step."""
    if rec.get("unreadable_record") or "stamp" not in rec:
        now = _now()
        rec.update({"why": "damaged", "detail": "the record of an earlier restore could not be read",
                    "found_at": now.isoformat(timespec="seconds"), "stamp": now.strftime("%Y%m%d-%H%M%S"),
                    "refused": []})
        rec.pop("unreadable_record", None)
        rec.pop("backup", None)  # chosen again below
    if rec["step"] in ("chosen", "set_aside") and not _still_good(rec):
        backup, refused = choose(paths, log)
        rec.update({"backup": str(backup) if backup else None,
                    "backup_time": backup.stat().st_mtime if backup else None,
                    "refused": sorted(set(rec.get("refused") or []) | set(refused))})
        _save(paths, rec, log, note)
    if rec["step"] == "chosen":
        _set_aside(paths, log, note, rec)
    if rec["step"] == "set_aside":
        _put_in_place(paths, log, note, rec)
    if rec["step"] == "in_place":
        _put_back(paths, log, note, rec)
    if rec["step"] == "put_back":
        _tell(paths, log, note, rec)


def _still_good(rec: dict[str, Any]) -> bool:
    """The record's copy is still there and good (None: the fresh path was chosen, nothing to re-check)."""
    if "backup" not in rec:
        return False
    if rec["backup"] is None:  # no good copy was left: a good one cannot have appeared since
        return True
    return good_copy(Path(str(rec["backup"]))) is None


def _aside_name(paths: Paths, stamp: str, base: str = "studio") -> Path:
    folder = damaged_dir(paths)
    target = folder / f"{base}-{stamp}.db"
    n = 2
    while any(Path(str(target) + s).exists() for s in _SIDE):
        target = folder / f"{base}-{stamp}-{n}.db"
        n += 1
    return target


def _move_aside(
    paths: Paths, rec: dict[str, Any], log: NsLogger | None, note: NoteFn, *, base: str, may_cancel: bool
) -> list[str]:
    """Move studio.db, -wal and -shm (main file first) to data/damaged. Windows refuses the main file while
    another process has it open: tried again for `RENAME_WAIT_S`, then NS-E209. `may_cancel` (the set-aside of
    D5): when the refused file is studio.db itself, nothing of the restore has happened, so its record goes
    and the next start begins again from the check."""
    live = paths.db_path
    named = rec.get("aside_name")
    target = damaged_dir(paths) / str(named) if named else _aside_name(paths, str(rec["stamp"]), base)
    moved: list[str] = []
    for suffix in _SIDE:
        src = Path(str(live) + suffix)
        if not src.exists():
            continue
        dst = Path(str(target) + suffix)
        deadline = time.monotonic() + RENAME_WAIT_S
        while True:
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.rename(src, dst)
                moved.append(dst.name)
                break
            except FileNotFoundError:
                break
            except OSError as e:
                if _refused(e) and time.monotonic() < deadline:
                    time.sleep(RENAME_POLL_S)
                    continue
                if may_cancel and not moved and suffix == "":
                    with contextlib.suppress(OSError):
                        record_path(paths).unlink()
                if _refused(e):
                    raise _failed(paths, log, note, "another Nightshift window is using it, so it could not "
                                  "be set aside", WAY_OUT_WINDOW, file=str(src)) from e
                why = "the disk is full" if _disk_full(e) else f"{src.name} could not be set aside ({e})"
                raise _failed(paths, log, note, why, WAY_OUT_RETRY, file=str(src)) from e
    return moved


def _set_aside(paths: Paths, log: NsLogger | None, note: NoteFn, rec: dict[str, Any]) -> None:
    """D5: the damaged files go to data/damaged (never deleted), under one name kept in the record, so a
    set-aside that is finished by a later start puts the -wal and -shm beside their own file."""
    if not rec.get("aside_name"):
        rec["aside_name"] = _aside_name(paths, str(rec["stamp"])).name
        _save(paths, rec, log, note)
    moved = _move_aside(paths, rec, log, note, base="studio", may_cancel=True)
    rec["damaged"] = sorted(set(rec.get("damaged") or []) | set(moved))
    rec["step"] = "set_aside"
    _save(paths, rec, log, note)
    if log is not None:
        log.warn("db_set_aside", f"{', '.join(moved) or 'nothing'} moved to {damaged_dir(paths)}")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _copy_backup(src: Path, dst: Path) -> None:
    """The copy of D6 (a seam: the tests stop the process here, or give it a full disk)."""
    copy_atomic(src, dst)


def _put_in_place(paths: Paths, log: NsLogger | None, note: NoteFn, rec: dict[str, Any]) -> None:
    """D6: the chosen copy (or a new, empty database) becomes studio.db by one rename."""
    from ns_common.db import Db

    live = paths.db_path
    if live.exists() and rec.get("copy_sha256") and _sha256(live) == rec["copy_sha256"]:
        rec["step"] = "in_place"  # the rename was made; the process stopped before it said so
        _save(paths, rec, log, note)
        return
    if any(Path(str(live) + s).exists() for s in _SIDE):
        # a studio.db that is not our copy, or a -wal/-shm beside where the copy goes: a stale -wal would be
        # replayed onto the copy and break it, so they are set aside too (never deleted)
        moved = _move_aside(paths, {"stamp": f"{rec['stamp']}-found"}, log, note, base="studio",
                            may_cancel=False)
        rec["damaged"] = sorted(set(rec.get("damaged") or []) | set(moved))
    staging = live.with_name(live.name + RESTORING_SUFFIX)
    for suffix in _SIDE:
        Path(str(staging) + suffix).unlink(missing_ok=True)
    try:
        if rec.get("backup"):
            _copy_backup(Path(str(rec["backup"])), staging)
            why = good_copy(staging)
            if why is not None:
                staging.unlink(missing_ok=True)
                raise _failed(paths, log, note, f"the copy of the nightly copy failed its check ({why})",
                              WAY_OUT_RETRY)
        else:
            fresh = Db(staging)
            try:
                fresh.migrate()
            finally:
                fresh.close()
            for suffix in ("-wal", "-shm"):
                Path(str(staging) + suffix).unlink(missing_ok=True)
        rec["copy_sha256"] = _sha256(staging)
        _save(paths, rec, log, note)
        os.replace(staging, live)
    except NsError:
        raise
    except (OSError, sqlite3.Error) as e:
        with contextlib.suppress(OSError):
            staging.unlink(missing_ok=True)
        if _disk_full(e):
            raise _failed(paths, log, note, f"the disk is full ({_free_words(paths)})",
                          "Free space on that drive, then open the Dashboard: Nightshift tries again.") from e
        raise _failed(paths, log, note, f"the copy could not be put in place ({type(e).__name__}: {e})",
                      WAY_OUT_RETRY) from e
    rec["step"] = "in_place"
    _save(paths, rec, log, note)
    if log is not None:
        log.warn("db_put_in_place", f"studio.db is now {rec.get('backup') or 'a new, empty database'}")


def _free_words(paths: Paths) -> str:
    try:
        import shutil

        free = shutil.disk_usage(paths.data).free
    except OSError:
        return "the free space could not be read"
    return f"{paths.data.anchor or paths.data} has {free / 1e6:.0f} MB free"


def _put_back(paths: Paths, log: NsLogger | None, note: NoteFn, rec: dict[str, Any]) -> None:
    """D7 and D8: migrate, then the videos made since the copy go back to the Queue (each at most once)."""
    from ns_common.db import Db

    db = Db(paths.db_path)
    try:
        db.migrate()
        since = rec.get("backup_time") if rec.get("backup") else None
        found = put_back_videos(paths, db, since)
    except (OSError, sqlite3.Error) as e:
        if _disk_full(e):
            raise _failed(paths, log, note, f"the disk is full ({_free_words(paths)})",
                          "Free space on that drive, then open the Dashboard: Nightshift tries again.") from e
        raise _failed(paths, log, note, f"the restored copy could not be prepared ({type(e).__name__}: {e})",
                      WAY_OUT_RETRY) from e
    finally:
        db.close()
    rec["put_back"] = sorted(set(rec.get("put_back") or []) | set(found["put_back"]))
    rec["unreadable"] = found["unreadable"]
    rec["gone"] = found["gone"]
    rec["step"] = "put_back"
    _save(paths, rec, log, note)


def _duration(job_dir: Path, item: str) -> float | None:
    try:
        if item == "long":
            data = json.loads(read_text_retry(job_dir / "render" / "long.json"))
        else:
            data = json.loads(read_text_retry(job_dir / "render" / "report.json")).get(item) or {}
        value = data.get("duration_s")
        return float(value) if value is not None else None
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def put_back_videos(paths: Paths, db: Any, since: float | None) -> dict[str, Any]:
    """D8: each job folder whose `outbox/manifest.json` is newer than `since` (every one when None) gives its
    items back as `outputs` rows, status `pending`, from the item's sheet `.json`. A row the copy has stays as
    it is (`on conflict do nothing`). An item whose sheet cannot be read is counted (`unreadable`); one whose
    video is no longer where the manifest says (rejected, moved) is counted (`gone`)."""
    from ns_common.models import Metadata

    put: list[str] = []
    unreadable = gone = 0
    try:
        folders = sorted(p for p in paths.jobs_dir.iterdir() if p.is_dir())
    except OSError:
        folders = []
    for job_dir in folders:
        manifest_file = job_dir / "outbox" / "manifest.json"
        try:
            made = manifest_file.stat().st_mtime
        except OSError:
            continue
        if since is not None and made <= since:
            continue
        try:
            manifest = json.loads(read_text_retry(manifest_file))
            items = list(manifest.items())
        except (OSError, ValueError, AttributeError):
            unreadable += 1
            continue
        created = datetime.fromtimestamp(made, UTC).isoformat()
        for item, entry in items:
            try:
                meta = Metadata.model_validate_json(read_text_retry(Path(entry["json"])))
                mp4 = Path(entry["mp4"])
            except (OSError, ValueError, KeyError, TypeError):
                unreadable += 1
                continue
            if not mp4.is_file():
                gone += 1
                continue
            output_id = f"{job_dir.name}/{item}"
            cur = db.execute(
                "insert into outputs(output_id, job_id, channel, kind, k, title, path, duration_s, combo_id,"
                " status, profile, created_at) values (?,?,?,?,?,?,?,?,?,'pending',?,?)"
                " on conflict(output_id) do nothing",
                (output_id, job_dir.name, meta.channel, meta.kind, meta.k, meta.title, str(mp4),
                 _duration(job_dir, item), meta.combo_id, meta.profile, created),
            )
            if cur.rowcount:
                put.append(output_id)
    return {"put_back": put, "unreadable": unreadable, "gone": gone}


def _tell(paths: Paths, log: NsLogger | None, note: NoteFn, rec: dict[str, Any]) -> None:
    """D9: `run/db-restored.json`, the note (once), then the record goes."""
    info = {k: rec.get(k) for k in ("why", "detail", "found_at", "backup", "backup_time", "refused",
                                    "damaged", "put_back", "unreadable", "gone")}
    info["backup_made"] = _local(float(rec["backup_time"])) if rec.get("backup") else None
    info["restored_at"] = _now().isoformat(timespec="seconds")
    info["by_pid"] = os.getpid()
    info["missing"] = NOT_IN_THE_COPY if rec.get("backup") else GONE_HISTORY
    info["message"] = restored_message(info)
    try:
        _write(restored_path(paths), info)
    except OSError as e:
        if log is not None:
            log.warn("db_restored_not_recorded", str(e))
    if log is not None:
        log.warn("db_restored", info["message"], code=RESTORED_CODE)
    _say(note, info["message"], log)
    try:
        record_path(paths).unlink(missing_ok=True)
    except OSError as e:  # the next start runs D9 again: the same words, which the note leaves out as said
        if log is not None:
            log.warn("db_restore_record_left", str(e))


# ------------------------------------------------------------------------------------------- assistant.db
ASSISTANT_SET_ASIDE = "The Assistant's conversation history was damaged and was set aside; it starts empty."


def ensure_assistant_sound(paths: Paths, log: NsLogger | None, note: NoteFn) -> Check:
    """The Assistant's own database: no copy exists (decided: its loss is the conversation history only), so a
    damaged one is set aside in data/damaged and a new one starts. Only the dashboard opens it."""
    live = paths.assistant_db
    found = check(live)
    if not found.damaged:
        return found
    stamp = _now().strftime("%Y%m%d-%H%M%S")
    target = _aside_name(paths, stamp, "assistant")
    for suffix in _SIDE:
        src = Path(str(live) + suffix)
        if not src.exists():
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, Path(str(target) + suffix))
        except OSError as e:
            if log is not None:
                log.error("assistant_db_not_set_aside", str(e), file=str(src))
            raise NsError(DAMAGED_CODE, f"the Assistant's database {src.name} could not be set aside ({e})",
                          hint=WAY_OUT_WINDOW) from e
    if log is not None:
        log.warn("assistant_db_set_aside", found.detail, kept=str(target))
    _say(note, ASSISTANT_SET_ASIDE, log)
    return found
