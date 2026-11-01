from __future__ import annotations

import secrets
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from ns_common.paths import contracts_dir


def new_job_id(slug: str, when: datetime | None = None) -> str:
    when = when or datetime.now(UTC)
    return f"{slug}-{when:%Y%m%d}-{secrets.token_hex(2)}"


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
        self.connect().executescript(sql)

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
