from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from ns_common.errors import NsError
from ns_common.paths import contracts_dir

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
        today = today or datetime.now(UTC).date()
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"studio-{today:%Y%m%d}.db"
        dest = sqlite3.connect(target)
        try:
            self.connect().backup(dest)
        finally:
            dest.close()
        existing = sorted(backup_dir.glob("studio-*.db"))
        stale = existing[:-keep] if len(existing) > keep else []
        for old in stale:
            old.unlink()
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
