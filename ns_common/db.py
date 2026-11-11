from __future__ import annotations

import contextlib
import os
import secrets
import sqlite3
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ns_common.errors import NsError
from ns_common.paths import contracts_dir

if TYPE_CHECKING:
    from ns_common.logging import NsLogger
    from ns_common.paths import Paths

# How often `claim_job_id` draws again before it gives up. An id has 65536 values per name and day, so 64
# refusals in a row mean the claim itself is broken (a folder that cannot be made), not bad luck.
JOB_ID_TRIES = 64


def new_job_id(slug: str, when: datetime | None = None) -> str:
    """A candidate id `<slug>-<yyyymmdd>-<4 hex>`. It is NOT known to be free: 16 random bits per name and
    day repeat (20 ids a day collide about once in 300 days). Whoever makes something under the id takes it
    with `claim_job_id`, which draws again when the id already names something."""
    when = when or datetime.now(UTC)
    return f"{slug}-{when:%Y%m%d}-{secrets.token_hex(2)}"


def claim_job_id(slug: str, claim: Callable[[str], object], when: datetime | None = None) -> str:
    """Draw ids until `claim(job_id)` takes one, and return it.

    `claim` makes the thing the id names in ONE step that fails when it is already there, and reports that
    with `FileExistsError` (a folder: `claim_dir`) or `sqlite3.IntegrityError` (a row with a unique key).
    Asking first and making it afterwards is not a claim: two processes (the dashboard and the nightly run)
    can both be told the id is free. Every other error of `claim` goes to the caller unchanged.
    `NS-E205` after `JOB_ID_TRIES` refusals in a row."""
    last = ""
    for _ in range(JOB_ID_TRIES):
        job_id = new_job_id(slug, when)
        try:
            claim(job_id)
        except (FileExistsError, sqlite3.IntegrityError) as e:
            last = f"{job_id}: {e}"
            continue
        return job_id
    raise NsError("NS-E205", f"{JOB_ID_TRIES} ids in a row for {slug!r} were taken; the last was {last}")


def claim_dir(path_for: Callable[[str], Path]) -> Callable[[str], None]:
    """A `claim` for `claim_job_id`: make the folder `path_for(job_id)`, failing when it exists.

    Creating a folder is one step for the file system, so of two makers of the same id exactly one gets it.
    Missing parent folders are made."""

    def claim(job_id: str) -> None:
        path_for(job_id).mkdir(parents=True, exist_ok=False)

    return claim


# Columns added to existing tables after the first release (CR-C8-3, idea bank). `CREATE TABLE IF NOT EXISTS`
# does not alter a database made by an older schema.sql, so `migrate()` adds what is missing.
_ADDED_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "sources": (("rights_confirmed", "INTEGER NOT NULL DEFAULT 0"), ("title", "TEXT")),
    "ideas": (("status", "TEXT NOT NULL DEFAULT 'new'"), ("title", "TEXT"), ("source", "TEXT")),
    "outputs": (("profile", "TEXT"),),  # H-8b: 'tiny_story' for a video made on an engine-test install
}


class Db:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA busy_timeout = 5000")
            self._conn = conn
        return self._conn

    def migrate(self) -> None:
        sql = (contracts_dir() / "schema.sql").read_text(encoding="utf-8")
        conn = self.connect()
        conn.executescript(sql)
        for table, columns in _ADDED_COLUMNS.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def execute(self, sql: str, params: Sequence | Mapping = ()) -> sqlite3.Cursor:
        return self.connect().execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence]) -> None:
        conn = self.connect()
        with conn:
            conn.executemany(sql, rows)

    def query(self, sql: str, params: Sequence | Mapping = ()) -> list[sqlite3.Row]:
        return list(self.connect().execute(sql, params).fetchall())

    def one(self, sql: str, params: Sequence | Mapping = ()) -> sqlite3.Row | None:
        return self.connect().execute(sql, params).fetchone()

    def backup(self, backup_dir: Path, keep: int = 14, today: date | None = None) -> Path:
        """The nightly copy `studio-<date>.db` (card db-integrity 4b.3), written whole and checked first.

        SQLite's backup API writes `studio-<date>.db.tmp`; the copy must pass `PRAGMA integrity_check` (a copy
        of a damaged page is as damaged as its source) before it is renamed over that date's file. Only then
        are the copies older than the newest `keep` deleted, and the `.tmp` a killed backup left. A copy that
        fails its check, or cannot be written whole (a full disk), is deleted and `BackupFailed` says why: the
        older copies all stay, and that date's earlier copy too."""
        from ns_common.dbrestore import check

        today = today or datetime.now(UTC).date()
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"studio-{today:%Y%m%d}.db"
        tmp = target.with_name(target.name + ".tmp")
        try:
            tmp.unlink(missing_ok=True)
            dest = sqlite3.connect(tmp)
            try:
                self.connect().backup(dest)
            finally:
                dest.close()
            found = check(tmp, full=True, fixed=True)
            if found.verdict != "ok":
                raise BackupFailed(f"the copy failed its check ({found.verdict}: {found.detail})")
            with open(tmp, "r+b") as fh:
                os.fsync(fh.fileno())
            os.replace(tmp, target)
        except BackupFailed:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise
        except (OSError, sqlite3.Error) as e:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise BackupFailed(f"the copy could not be written ({type(e).__name__}: {e})") from e
        existing = sorted(backup_dir.glob("studio-*.db"))
        stale = existing[:-keep] if len(existing) > keep else []
        for old in stale:
            old.unlink()
        for left in backup_dir.glob("studio-*.db.tmp"):  # a backup killed mid-way on another date
            with contextlib.suppress(OSError):
                left.unlink()
        return target

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> Db:
        self.connect()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class BackupFailed(Exception):
    """`Db.backup` made no copy (it failed its check, or could not be written); the older copies stay."""


class UnusableDb(Db):
    """A database this process could not put right (NS-E209): every use raises that error.

    The dashboard keeps answering with it (503 and the sentence) instead of exiting, so the owner reads
    why and what to do; it never opens the file."""

    def __init__(self, path: Path, error: NsError) -> None:
        super().__init__(path)
        self.error = error

    def connect(self) -> sqlite3.Connection:
        raise self.error

    def migrate(self) -> None:
        raise self.error


#: The files this process has already checked (`open_studio_db`): once per process and file.
_checked: set[str] = set()
_checked_lock = threading.Lock()


def open_studio_db(paths: Paths, log: NsLogger | None, *, note: Callable[[str], object]) -> Db:
    """The one opener of `studio.db` (card db-integrity): checked once per process before the first use.

    A damaged file is set aside and replaced by the newest good nightly copy, the videos made since then are
    put back in the Queue, and `note` leaves the owner's morning note NS-W213
    (`ns_common.dbrestore.ensure_sound`); NS-E209 when it cannot be replaced (another process has it open, a
    full disk). A file held by another program, or written by a newer program, is not damage: it is opened as
    before. Returns the `Db`, not yet connected; the caller migrates it as before."""
    from ns_common.dbrestore import ensure_sound

    key = os.path.normcase(os.path.abspath(paths.db_path))
    with _checked_lock:
        if key not in _checked:
            ensure_sound(paths, log, note)
            _checked.add(key)
    return Db(paths.db_path)


def open_assistant_db(paths: Paths, log: NsLogger | None, *, note: Callable[[str], object]) -> Db:
    """The opener of `assistant.db`: a damaged one is set aside in data/damaged and a new one starts (no copy
    of it is kept: its loss is the conversation history only), with a morning note."""
    from ns_common.dbrestore import ensure_assistant_sound

    key = os.path.normcase(os.path.abspath(paths.assistant_db))
    with _checked_lock:
        if key not in _checked:
            ensure_assistant_sound(paths, log, note)
            _checked.add(key)
    return Db(paths.assistant_db)
