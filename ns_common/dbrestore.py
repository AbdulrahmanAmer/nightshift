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
* ONE decision: `decide` is the only place that says "damaged / restore / rebuild / resume / give up"; it
  returns a `Decision` (an `Act` and the evidence it rests on) from the live file's check, the state of each
  nightly copy (`survey_copies`) and a left-over record compared with the disk (`record_state`). Only
  positive evidence moves or replaces a file: a copy that is held, slow or unlistable stops the restore with
  NS-E209 and changes nothing (it is never "a damaged copy"), and a record of an earlier run is checked
  against the studio.db that is there now before anything acts on it (a sound file that is not the record's
  copy stays, and the record goes).
* A damage answer while the database is in use (the night's stop, the dashboard's 503) and a nightly copy
  that fails its check leave `run/db-full-check.json`: the next start's check is then the FULL one.
* A check that could not be made is counted (`run/db-check.json`), and a night without a nightly copy too
  (`run/db-backup.json`); both are said with NS-W214 (the first from the 3rd start in a row).
* NOT damage: a file another program holds or locks (BUSY, LOCKED, CANTOPEN, IOERR: it is opened as before
  and checked at the next start); a file format this SQLite cannot read ("unsupported file format": a newer
  program wrote it); a check that ran out of time; a rollback journal left behind
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
import enum
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

#: The code of "the database's safety net is not working": it could not be checked at `NOT_CHECKED_SAID_AFTER`
#: starts in a row, or a night made no nightly copy.
UNSAFE_CODE = "NS-W214"
NOT_CHECKED_SAID_AFTER = 3
#: The record's format. A record of another version, or with a step unknown to this program, is not acted on.
RECORD_VERSION = 1
STEPS = ("chosen", "set_aside", "in_place", "put_back")
FULL_CHECK_FILE = "db-full-check.json"  # run/: SQLite answered CORRUPT in use; the next start checks in full
BACKUP_STATE_FILE = "db-backup.json"    # run/: the nights in a row that made no nightly copy
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
    #: The FULL check was the one made (`_decide_now` sets it): only then may the full-check mark be answered.
    full: bool = False

    @property
    def damaged(self) -> bool:
        return self.verdict in (DAMAGED, LOST)


# ------------------------------------------------------------------------------------------- evidence
def classify(e: sqlite3.Error) -> str:
    """The verdict an SQLite error is evidence of (DAMAGED only for SQLite's own corruption answers)."""
    text = str(e).lower()
    name = str(getattr(e, "sqlite_errorname", "") or "")
    if "unsupported file format" in text:
        return NEWER
    # "malformed database schema" is SQLITE_CORRUPT of the schema table (damage to page 1), not a newer
    # program: this product's plain schema gives no SQLite that answer. It falls to the lines below.
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


def history(paths: Paths) -> bool | None:
    """True when this home made videos before (a nightly copy, or a job folder with an outbox manifest),
    False when both folders were read and hold none, None when one of them could not be read: "could not
    look" is not "no history"."""
    unknown = False
    try:
        if any(_BACKUP_NAME.match(p.name) for p in paths.backup_dir.iterdir()):
            return True
    except FileNotFoundError:
        pass
    except OSError:
        unknown = True
    try:
        if any((j / "outbox" / "manifest.json").is_file() for j in paths.jobs_dir.iterdir()):
            return True
    except FileNotFoundError:
        pass
    except OSError:
        unknown = True
    return None if unknown else False


def has_history(paths: Paths) -> bool:
    return history(paths) is True


def assess(paths: Paths, *, full: bool = False) -> Check:
    """The live studio.db: `check`, with a missing or empty file read against the home's history. A home
    whose history cannot be read is not a new install: the missing file is LOST, and `decide` then stops on
    the copies it cannot list (no new, empty database is made over a history nobody could look at)."""
    found = check(paths.db_path, full=full)
    if found.verdict == NEW:
        known = history(paths)
        if known is None:
            return Check(LOST, f"{found.detail}, and the nightly copies or job folders cannot be read now")
        if known:
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


def full_check_path(paths: Paths) -> Path:
    return run_dir(paths) / FULL_CHECK_FILE


def backup_state_path(paths: Paths) -> Path:
    return run_dir(paths) / BACKUP_STATE_FILE


def read_record(paths: Paths) -> dict[str, Any] | None:
    """The restore in progress, or None. A record that cannot be read, of another version, or with a step
    this program does not know is set aside as `.bad` and read as NO record (row R1): what is on the disk
    decides, and nothing is moved on the word of a record nobody can read."""
    path = record_path(paths)
    if not path.exists():
        return None
    rec = read_json(path)
    known = (rec is not None and rec.get("step") in STEPS and isinstance(rec.get("stamp"), str)
             and isinstance(rec.get("found_at"), str)
             and rec.get("version", RECORD_VERSION) == RECORD_VERSION)
    if known and rec is not None:
        try:
            datetime.fromisoformat(rec["found_at"])
        except ValueError:
            known = False
    if not known or rec is None:
        quarantine(path)
        return None
    return rec


def _backups(paths: Paths) -> list[tuple[Path, str]]:
    """The nightly copies, newest first. No folder: no copies (positive). Any other OSError is RAISED: a
    folder that cannot be listed is "could not look", never "there are none"."""
    try:
        names = sorted((p for p in paths.backup_dir.iterdir() if _BACKUP_NAME.match(p.name)), reverse=True)
    except FileNotFoundError:
        return []
    return [(p, _BACKUP_NAME.match(p.name).group(1)) for p in names]  # type: ignore[union-attr]


# ------------------------------------------------------------------------------------------- the decision
# states of one nightly copy
GOOD = "good"        # the full check answered ok and it holds studio.db's tables
BAD = "bad"          # read, and positively not usable: SQLite's damage answer, zero bytes, not studio.db
GONE = "gone"        # no file any more
UNKNOWN = "unknown"  # could not look: held, locked, slow, an OSError. Never "bad".


@dataclass(frozen=True)
class CopyState:
    name: str
    state: str
    detail: str = ""
    path: str = ""


@dataclass(frozen=True)
class Copies:
    """The nightly copies as looked at, newest first, up to the first GOOD or UNKNOWN one. `listed` False:
    the folder itself could not be listed (UNKNOWN for all of them)."""

    listed: bool
    detail: str = ""
    states: tuple[CopyState, ...] = ()


@dataclass(frozen=True)
class RecordState:
    """A restore record checked against the disk: `ours` (studio.db's sha256 is the one the record took of
    its copy before the rename), `aside` (the record's set-aside file is in data/damaged), `begun` (the
    put-back began: it writes to the copy, so `ours` no longer holds)."""

    step: str
    ours: bool | None = False  # None: studio.db is there but could not be read to compare
    aside: bool = False
    begun: bool = False


class Act(enum.Enum):
    KEEP = "keep"                # studio.db is sound, or a new install: nothing is done
    LEAVE = "leave"              # could not look: nothing is changed, it is counted and said after a bound
    RESTORE = "restore"          # positive damage, and a copy that passed its full check
    REBUILD = "rebuild"          # positive damage, and every copy was read and is positively not usable
    STOP = "stop"                # positive damage, but a copy could not be looked at: NS-E209, no change
    RESUME = "resume"            # a record that matches the disk: its remaining (additive) steps are run
    DROP_RECORD = "drop_record"  # a record that does not match a sound studio.db: the file stays, it goes


@dataclass(frozen=True)
class Decision:
    """What is done about studio.db and the evidence it rests on. `row` is the row of the state table."""

    act: Act
    row: str
    live: Check
    record: RecordState | None = None
    copies: Copies | None = None
    backup: str | None = None
    reason: str = ""

    @property
    def refused(self) -> list[str]:
        """The copies that were read and are positively not usable."""
        return [c.name for c in (self.copies.states if self.copies else ()) if c.state == BAD]


def copy_state(path: Path) -> CopyState:
    """One nightly copy, by the full check: GOOD, BAD (positive), GONE or UNKNOWN (could not look)."""
    found = check(path, full=True, fixed=True)
    where = str(path)
    if found.verdict == NEW:
        return CopyState(path.name, GONE if found.detail == "no file" else BAD, found.detail, where)
    if found.verdict == DAMAGED:
        return CopyState(path.name, BAD, f"{found.verdict}: {found.detail}", where)
    if found.verdict != OK:
        return CopyState(path.name, UNKNOWN, f"{found.verdict}: {found.detail}", where)
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?immutable=1", uri=True)
        try:
            tables = {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
        finally:
            conn.close()
    except sqlite3.Error as e:
        verdict = classify(e)
        return CopyState(path.name, BAD if verdict == DAMAGED else UNKNOWN, f"{verdict}: {e}", where)
    missing = REQUIRED_TABLES - tables
    if missing:
        return CopyState(path.name, BAD, f"not a copy of studio.db (no {', '.join(sorted(missing))})", where)
    return CopyState(path.name, GOOD, "", where)


def good_copy(path: Path) -> str | None:
    """None when `path` passes the full check and holds studio.db's tables, else why not."""
    found = copy_state(path)
    return None if found.state == GOOD else f"{found.state}: {found.detail}"


def survey_copies(paths: Paths, log: NsLogger | None) -> Copies:
    """The evidence about the nightly copies (no decision): each one's state, newest first, up to the first
    GOOD or UNKNOWN."""
    try:
        listed = _backups(paths)
    except OSError as e:
        return Copies(False, f"{type(e).__name__}: {e}")
    states: list[CopyState] = []
    for path, _day in listed:
        found = copy_state(path)
        states.append(found)
        if found.state != GOOD and log is not None:
            log.warn("db_backup_not_good", f"{found.state}: {found.detail}", backup=str(path))
        if found.state in (GOOD, UNKNOWN):
            break
    return Copies(True, "", tuple(states))


def record_state(paths: Paths, rec: dict[str, Any]) -> RecordState:
    """The record's claims read from the disk (no decision)."""
    ours: bool | None = False
    want = rec.get("copy_sha256")
    if want:
        try:
            ours = _sha256(paths.db_path) == want
        except FileNotFoundError:
            ours = False
        except OSError:
            ours = None  # could not look
    named = rec.get("aside_name")
    aside = bool(named) and (damaged_dir(paths) / str(named)).exists()
    return RecordState(str(rec["step"]), ours, aside, bool(rec.get("put_back_begun")))


def _from_copies(live: Check, record: RecordState | None, copies: Copies, row: str) -> Decision:
    """Positive damage of studio.db: what the copies allow (rows L3, L4, L5)."""
    if not copies.listed:
        return Decision(Act.STOP, f"{row}/L4", live, record, copies, None,
                        f"the folder of the nightly copies cannot be read now ({copies.detail})")
    for c in copies.states:
        if c.state == GOOD:
            return Decision(Act.RESTORE, f"{row}/L3", live, record, copies, c.path,
                            f"{c.name} passed its check")
        if c.state == UNKNOWN:
            return Decision(Act.STOP, f"{row}/L4", live, record, copies, None,
                            f"the nightly copy {c.name} cannot be read now ({c.detail})")
    return Decision(Act.REBUILD, f"{row}/L5", live, record, copies, None,
                    "every nightly copy was read and none is usable" if copies.states else "no nightly copy")


def decide(live: Check, record: RecordState | None, copies: Callable[[], Copies]) -> Decision:
    """THE decision about studio.db: every "damaged / restore / put back / give up" of this module is one row
    of this function (the state table of the fix, rows L1-L5 and R2-R9). Pure: `live` is the check of the
    file as it is now, `record` a left-over record already compared with the disk, `copies` gives the
    evidence about the nightly copies and is asked only by the rows that need it.

    The invariant: only positive evidence moves or replaces a database. "Could not look" is LEAVE or STOP."""
    unknown = live.verdict not in (OK, NEW, DAMAGED, LOST)
    if record is None:
        if live.verdict in (OK, NEW):
            return Decision(Act.KEEP, "L1", live)
        if unknown:
            return Decision(Act.LEAVE, "L2", live, reason=f"{live.verdict}: {live.detail}")
        return _from_copies(live, None, copies(), "L")
    if unknown:  # R6: a record never makes "could not look" into a reason to move anything
        return Decision(Act.LEAVE, "R6", live, record, reason=f"{live.verdict}: {live.detail}")
    if record.step in ("chosen", "set_aside"):
        if live.verdict == OK:
            if record.ours is None:  # R6: whether it is the recorded copy could not be read
                return Decision(Act.LEAVE, "R6", live, record, reason="studio.db could not be compared "
                                "with the copy of the restore that was begun")
            if record.ours:  # the rename was made; the process stopped before it said so
                return Decision(Act.RESUME, "R7", live, record, reason="studio.db is the recorded copy")
            return Decision(Act.DROP_RECORD, "R2", live, record,
                            reason="studio.db is not the recorded copy and passed its check")
        moved = record.aside or record.step == "set_aside"
        row = "R3" if not moved else ("R5" if live.verdict == DAMAGED else "R4")
        return _from_copies(live, record, copies(), row)
    if live.verdict == OK:  # in_place, put_back: only additive steps are left
        return Decision(Act.RESUME, "R8", live, record, reason="the copy is in place and passed its check")
    return _from_copies(live, record, copies(), "R9")


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
    if info.get("not_ours"):
        extra += (" The database in place now is not the copy as Nightshift put it there (another program "
                  "replaced or changed it before this was finished); it passed its check and was kept.")
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
    if not record_path(paths).exists():  # with a record, another process may be mid-restore: under the lock
        d = _decide_now(paths, log, None)
        if d.act in (Act.KEEP, Act.LEAVE):
            return _settled(paths, log, note, d)
        if log is not None:
            log.warn("db_damaged", d.live.detail, verdict=d.live.verdict)
    with _restore_lock(paths, log, note):
        rec = read_record(paths)
        d = _decide_now(paths, log, rec)  # again: another process may have restored it while this one waited
        if d.act in (Act.KEEP, Act.LEAVE):
            return _settled(paths, log, note, d)
        _act(paths, log, note, d, rec)
    found = assess(paths)
    write_check(paths, found, log, restored=d.act is not Act.DROP_RECORD, row=d.row, not_checked_starts=0)
    return found


def _decide_now(paths: Paths, log: NsLogger | None, rec: dict[str, Any] | None) -> Decision:
    """Gather the evidence from the disk and ask `decide`. The live check is the FULL one while the mark of a
    damage answer in use waits (`want_full_check`)."""
    full = full_check_path(paths).exists()
    live = assess(paths, full=full)
    live.full = full  # a mark left WHILE a quick check ran is not answered by it: it waits for the next start
    state = record_state(paths, rec) if rec is not None else None
    d = decide(live, state, lambda: survey_copies(paths, log))
    if log is not None and d.act not in (Act.KEEP,):
        log.info("db_decision", d.reason, act=d.act.value, row=d.row, verdict=live.verdict)
    return d


def newest_copy_words(paths: Paths) -> str:
    """"the newest nightly copy is from <date>", by the folder's listing (each copy was checked when made)."""
    try:
        listed = _backups(paths)
    except OSError as e:
        return f"the nightly copies cannot be listed now ({type(e).__name__})"
    if not listed:
        return "there is no nightly copy yet"
    try:
        return f"the newest nightly copy is from {_local(listed[0][0].stat().st_mtime)}"
    except OSError:
        return f"the newest nightly copy is {listed[0][0].name}"


FULL_CHECK_PASSED = (
    "Nightshift's database answered 'damaged' while it was in use; the full check at this start found it "
    "sound, so nothing was replaced. If a night stops the same way again, run 'ns doctor --bundle' and send "
    "the zip."
)
NOT_CHECKED_WAY_OUT = (
    "Close every Nightshift window (Stop All) and any other program that uses the Nightshift folder, then "
    "open the Dashboard: Nightshift checks it again. If this note returns, run 'ns doctor --bundle' and send "
    "the zip."
)
COPY_WAY_OUT = (
    "Close any program that is using the folder data\\backup (a backup tool, a virus scan), then close every "
    "Nightshift window (Stop All) and open the Dashboard: Nightshift looks again. If nothing is using that "
    "folder and this note returns, move the copy it names out of data\\backup (keep the file): Nightshift "
    "then uses the next newest copy. If it names no copy, run 'ns doctor --bundle' and send the zip."
)


def _settled(paths: Paths, log: NsLogger | None, note: NoteFn, d: Decision) -> Check:
    """KEEP and LEAVE: nothing is changed. A check that could not be made is counted in `run/db-check.json`
    and, from the `NOT_CHECKED_SAID_AFTER`-th start in a row, said (NS-W214; the same words wait once)."""
    if d.act is Act.KEEP:
        write_check(paths, d.live, log, not_checked_starts=0, row=d.row)
        # Only a check that WAS the full one answers the mark. A mark another process left while this start's
        # quick check ran (the night stopping at that moment) stays, and the next start checks in full.
        if d.live.verdict == OK and d.live.full:
            clear_full_check(paths, log)
            _say(note, FULL_CHECK_PASSED, log)
        return d.live
    before = read_json(check_path(paths)) or {}
    try:
        n = int(before.get("not_checked_starts") or 0) + 1
    except (TypeError, ValueError):
        n = 1
    write_check(paths, d.live, log, not_checked_starts=n, row=d.row)
    if log is not None:
        log.info("db_not_checked", f"{d.reason}; nothing was changed", starts=n)
    if n >= NOT_CHECKED_SAID_AFTER:
        _say(note, f"{UNSAFE_CODE}: Nightshift's database could not be checked at its last starts "
                   f"({d.live.verdict}: {d.live.detail}), so damage to it would not be found; "
                   f"{newest_copy_words(paths)}. {NOT_CHECKED_WAY_OUT}", log)
    return d.live


def want_full_check(paths: Paths, why: str, log: NsLogger | None = None) -> None:
    """SQLite answered CORRUPT while the database was in use (the night's stop, the dashboard's 503), or a
    nightly copy failed its full check: the next start's check of studio.db is the FULL one (the quick check
    does not compare an index with its table), and `decide` rests on that."""
    try:
        _write(full_check_path(paths), {"at": _now().isoformat(timespec="seconds"), "why": why[:500],
                                        "pid": os.getpid()})
    except OSError as e:
        if log is not None:
            log.warn("db_full_check_not_marked", str(e))


def clear_full_check(paths: Paths, log: NsLogger | None) -> None:
    try:
        full_check_path(paths).unlink(missing_ok=True)
    except OSError as e:  # the next start checks in full once more: slower, never wrong
        if log is not None:
            log.warn("db_full_check_mark_left", str(e))


def backup_failed(
    paths: Paths, why: str, log: NsLogger | None, note: NoteFn, *, damaged: bool = False
) -> str:
    """A night made no nightly copy: counted in `run/db-backup.json` and said (NS-W214, with the age of the
    newest copy). `damaged`: the copy's full check answered damage, so the live file is checked in full at
    the next start. Returns the sentence."""
    before = read_json(backup_state_path(paths)) or {}
    try:
        n = int(before.get("failed_nights") or 0) + 1
    except (TypeError, ValueError):
        n = 1
    now = _now().isoformat(timespec="seconds")
    try:
        _write(backup_state_path(paths), {"failed_nights": n, "since": before.get("since") or now,
                                          "last_at": now, "last_error": why[:500]})
    except OSError as e:
        if log is not None:
            log.warn("db_backup_state_not_written", str(e))
    if damaged:
        want_full_check(paths, f"the nightly copy failed its check: {why}", log)
    sentence = (f"{UNSAFE_CODE}: No nightly copy of Nightshift's database was made tonight ({why}); "
                f"{newest_copy_words(paths)}. Free space on the drive of the Nightshift folder and close any "
                "program that uses the folder data\\backup; the next night makes a copy again while "
                "Automatic runs are on in Settings. If this note returns, run 'ns doctor --bundle' and send "
                "the zip.")
    if log is not None:
        log.warn("db_backup_failed", sentence, code=UNSAFE_CODE, nights=n)
    _say(note, sentence, log)
    return sentence


def backup_made(paths: Paths) -> None:
    """A nightly copy was made and passed its check: the count of failed nights ends."""
    with contextlib.suppress(OSError):
        backup_state_path(paths).unlink(missing_ok=True)


KEPT_FOUND = (
    "Nightshift had begun to replace its database (found damaged {found}) and was stopped before it "
    "finished. The database that is there now passed its check, so it was kept as it is and the "
    "replacement was not continued. Anything that was set aside is kept in data\\damaged."
)


def _act(paths: Paths, log: NsLogger | None, note: NoteFn, d: Decision, rec: dict[str, Any] | None) -> None:
    """Carry out `d` (under the restore lock). Nothing here decides: each branch is one `Act`."""
    if d.act is Act.DROP_RECORD and rec is not None:
        said = KEPT_FOUND.format(found=_local(datetime.fromisoformat(str(rec["found_at"]))))
        if log is not None:
            log.warn("db_restore_record_dropped", said, step=rec.get("step"), row=d.row)
        try:
            quarantine(record_path(paths))
        except OSError as e:  # the next start decides the same again
            if log is not None:
                log.warn("db_restore_record_left", str(e))
        _say(note, said, log)
        return
    if d.act is Act.STOP:
        moved = d.record is not None and (d.record.aside or d.record.step != "chosen")
        if d.record is not None and not moved:  # nothing has moved: the next start decides from the check
            with contextlib.suppress(OSError):
                record_path(paths).unlink(missing_ok=True)
        raise _failed(paths, log, note, f"{d.reason}, so nothing was changed", COPY_WAY_OUT, row=d.row)
    if d.act is Act.RESUME and rec is not None and d.record is not None:
        if d.row == "R7":
            rec["step"] = "in_place"
        elif rec["step"] == "in_place" and d.record.ours is False and not d.record.begun:
            rec["not_ours"] = True  # sound, but not the bytes this program put there: said in the note
        _save(paths, rec, log, note)
        _finish(paths, log, note, rec)
        return
    if d.act in (Act.RESTORE, Act.REBUILD):
        if rec is None or rec["step"] in ("in_place", "put_back"):  # L3, L5, R9: a restore from its start
            rec = _start(paths, log, note, d, carried=(rec or {}).get("damaged"))
        else:  # R3, R4, R5: the record's own restore goes on, with the copy decided now
            try:
                _choose_into(rec, d)
            except OSError as e:  # the copy went between the check and here: nothing has been moved for it
                raise _failed(paths, log, note, f"the nightly copy cannot be read now ({e}), so nothing was "
                              "changed", COPY_WAY_OUT, row=d.row) from e
            _save(paths, rec, log, note)
        _finish(paths, log, note, rec)


def _choose_into(rec: dict[str, Any], d: Decision) -> None:
    backup = Path(d.backup) if d.backup else None
    rec.update({"backup": str(backup) if backup else None,
                "backup_time": backup.stat().st_mtime if backup else None,
                "refused": sorted(set(rec.get("refused") or []) | set(d.refused)), "row": d.row})


def _start(
    paths: Paths, log: NsLogger | None, note: NoteFn, d: Decision, carried: list[str] | None = None
) -> dict[str, Any]:
    """D5's record, from the decision: written before anything moves."""
    now = _now()
    rec: dict[str, Any] = {
        "version": RECORD_VERSION, "step": "chosen", "why": d.live.verdict, "detail": d.live.detail,
        "found_at": now.isoformat(timespec="seconds"), "stamp": now.strftime("%Y%m%d-%H%M%S"),
        "pid": os.getpid(), "damaged": list(carried or []),
    }
    try:
        _choose_into(rec, d)
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
    # Which copy, and whether anything may move at all, was decided by `decide` in this same call, under the
    # lock, from the disk as it is now (`_act`): no step below looks for a reason of its own.
    if rec["step"] == "chosen":
        _set_aside(paths, log, note, rec)
    if rec["step"] == "set_aside":
        _put_in_place(paths, log, note, rec)
    if rec["step"] == "in_place":
        _put_back(paths, log, note, rec)
    if rec["step"] == "put_back":
        _tell(paths, log, note, rec)


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
                    raise _failed(paths, log, note, "another program is using it, so it could not "
                                  "be set aside", WAY_OUT_WINDOW, file=str(src)) from e
                why = "the disk is full" if _disk_full(e) else f"{src.name} could not be set aside ({e})"
                raise _failed(paths, log, note, why, WAY_OUT_RETRY, file=str(src)) from e
    return moved


def _set_aside(paths: Paths, log: NsLogger | None, note: NoteFn, rec: dict[str, Any]) -> None:
    """D5: the damaged files go to data/damaged (never deleted), under one name kept in the record, so a
    set-aside that is finished by a later start puts the -wal and -shm beside their own file."""
    named = rec.get("aside_name")
    taken = bool(named) and (damaged_dir(paths) / str(named)).exists() and paths.db_path.exists()
    if not named or taken:  # taken: that name holds an earlier studio.db; the one here now gets its own
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
    # "studio.db is already our copy" is row R7 of `decide` and never reaches this step.
    if any(Path(str(live) + s).exists() for s in _SIDE):
        # Reached only by rows R4 and R5 (or straight after this call's own set-aside): `decide` read the file
        # that is here now and it is absent, empty or positively damaged; a sound one is row R2 and stays. A
        # -wal/-shm beside where the copy goes would be replayed onto the copy and break it, so what is here
        # is set aside too (never deleted).
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

    if not rec.get("put_back_begun"):  # from here studio.db is written to: its sha256 is no longer the copy's
        rec["put_back_begun"] = True
        _save(paths, rec, log, note)
    db = Db(paths.db_path)
    try:
        db.migrate()
        since = rec.get("backup_time") if rec.get("backup") else None
        found = find_videos(paths, db, since)
        # Saved BEFORE the first insert: each insert is committed by itself, so a process killed between two
        # of them leaves rows the next start no longer sees as missing. The ids of an earlier, killed
        # put-back stay in the list (union), and the note counts every video that went back.
        rec["put_back"] = sorted(set(rec.get("put_back") or []) | {row[0] for row in found["rows"]})
        rec["unreadable"] = found["unreadable"]
        rec["gone"] = found["gone"]
        _save(paths, rec, log, note)
        insert_videos(db, found["rows"])
        # counted from the rows that exist: an id saved by a killed run whose row is not there is not claimed
        rec["put_back"] = [i for i in rec["put_back"]
                           if db.execute("select 1 from outputs where output_id=?", (i,)).fetchone()]
    except (OSError, sqlite3.Error) as e:
        if _disk_full(e):
            raise _failed(paths, log, note, f"the disk is full ({_free_words(paths)})",
                          "Free space on that drive, then open the Dashboard: Nightshift tries again.") from e
        raise _failed(paths, log, note, f"the restored copy could not be prepared ({type(e).__name__}: {e})",
                      WAY_OUT_RETRY) from e
    finally:
        db.close()
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


_INSERT_OUTPUT = (
    "insert into outputs(output_id, job_id, channel, kind, k, title, path, duration_s, combo_id,"
    " status, profile, created_at) values (?,?,?,?,?,?,?,?,?,'pending',?,?)"
    " on conflict(output_id) do nothing"
)


def put_back_videos(paths: Paths, db: Any, since: float | None) -> dict[str, Any]:
    """D8 in one call (`find_videos`, then `insert_videos`): for a caller with no record to keep. The restore
    itself saves the found ids between the two (`_put_back`)."""
    found = find_videos(paths, db, since)
    put = insert_videos(db, found["rows"])
    return {"put_back": put, "unreadable": found["unreadable"], "gone": found["gone"]}


def insert_videos(db: Any, rows: list[tuple[Any, ...]]) -> list[str]:
    """The rows of `find_videos` into `outputs`; a row that is there stays as it is. Returns the ids added."""
    put: list[str] = []
    for row in rows:
        if db.execute(_INSERT_OUTPUT, row).rowcount:
            put.append(str(row[0]))
    return put


def find_videos(paths: Paths, db: Any, since: float | None) -> dict[str, Any]:
    """D8, reading only: each job folder whose `outbox/manifest.json` is newer than `since` (every one when
    None) gives its items as `outputs` rows (`rows`, status `pending`, from the item's sheet `.json`) when
    the database has no row of that id. An item whose sheet cannot be read is counted (`unreadable`); one
    whose video is no longer where the manifest says (rejected, moved) is counted (`gone`)."""
    from ns_common.models import Metadata

    rows: list[tuple[Any, ...]] = []
    unreadable = gone = 0
    try:
        folders = sorted(p for p in paths.jobs_dir.iterdir() if p.is_dir())
    except FileNotFoundError:  # no job folder at all: positively no video. Any other OSError is raised: a
        folders = []           # folder that cannot be listed must not become "no video was found"
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
            if db.execute("select 1 from outputs where output_id=?", (output_id,)).fetchone():
                continue
            rows.append((output_id, job_dir.name, meta.channel, meta.kind, meta.k, meta.title, str(mp4),
                         _duration(job_dir, item), meta.combo_id, meta.profile, created))
    return {"rows": rows, "unreadable": unreadable, "gone": gone}


def _tell(paths: Paths, log: NsLogger | None, note: NoteFn, rec: dict[str, Any]) -> None:
    """D9: `run/db-restored.json`, the note (once), then the record goes."""
    info = {k: rec.get(k) for k in ("why", "detail", "found_at", "backup", "backup_time", "refused",
                                    "damaged", "put_back", "unreadable", "gone")}
    info["backup_made"] = _local(float(rec["backup_time"])) if rec.get("backup") else None
    info["restored_at"] = _now().isoformat(timespec="seconds")
    info["by_pid"] = os.getpid()
    info["missing"] = NOT_IN_THE_COPY if rec.get("backup") else GONE_HISTORY
    info["not_ours"] = bool(rec.get("not_ours"))
    info["row"] = rec.get("row")
    info["message"] = restored_message(info)
    clear_full_check(paths, log)  # the restore is the answer to a damage answer in use
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
    # The same decision as studio.db's: no copy of this file is kept, so "the copies" are positively none, and
    # only SQLite's own damage answer (row L5) sets it aside; held, slow or unreadable leaves it (row L2).
    if decide(found, None, lambda: Copies(True)).act is not Act.REBUILD:
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
