"""Shared application state: locked DB access, settings file, wiring container (component C9)."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ns_common.db import Db
from ns_common.logging import NsLogger
from ns_common.paths import Paths

if TYPE_CHECKING:
    from ns_dashboard.adapters import Adapters
    from ns_dashboard.events import EventHub
    from ns_dashboard.jobs import JobStore, JobWorker
    from ns_dashboard.supervisor_link import SupervisorLink

DEFAULT_SETTINGS: dict[str, Any] = {"run_time": "03:00", "notifications": True}


class LockedDb:
    """`ns_common.db.Db` shared between request threads and the job worker behind one RLock."""

    def __init__(self, db: Db) -> None:
        self.db = db
        self.lock = threading.RLock()

    def query(self, sql: str, params: Sequence | Mapping = ()) -> list[sqlite3.Row]:
        """All rows of a SELECT."""
        with self.lock:
            return self.db.query(sql, params)

    def one(self, sql: str, params: Sequence | Mapping = ()) -> sqlite3.Row | None:
        """First row of a SELECT or None."""
        with self.lock:
            return self.db.one(sql, params)

    def execute(self, sql: str, params: Sequence | Mapping = ()) -> int:
        """Run one statement (autocommit) and return the affected row count."""
        with self.lock:
            return self.db.execute(sql, params).rowcount

    @contextmanager
    def tx(self) -> Iterator[LockedDb]:
        """Run several statements atomically; rolls back when the block raises."""
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.db.execute("COMMIT")

    def close(self) -> None:
        """Close the underlying connection."""
        with self.lock:
            self.db.close()


def open_db(paths: Paths) -> LockedDb:
    """Open and migrate `studio.db` (the shared schema already has `dashboard_jobs`)."""
    db = Db(paths.db_path)
    db.migrate()
    return LockedDb(db)


def settings_file(paths: Paths) -> Path:
    """Location of the dashboard-owned settings file (config/dashboard.json)."""
    return paths.config / "dashboard.json"


def load_dashboard_settings(paths: Paths) -> dict[str, Any]:
    """Settings with defaults filled in; a missing or corrupt file yields the defaults."""
    out = dict(DEFAULT_SETTINGS)
    f = settings_file(paths)
    if f.exists():
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            out.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
    return out


def save_dashboard_settings(paths: Paths, data: Mapping[str, Any]) -> dict[str, Any]:
    """Merge `data` into the stored settings and return the result."""
    merged = load_dashboard_settings(paths)
    merged.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
    f = settings_file(paths)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    return merged


@dataclass
class AppState:
    """Everything the service layer needs; built once by `create_app`."""

    paths: Paths
    db: LockedDb
    log: NsLogger
    hub: EventHub
    link: SupervisorLink
    adapters: Adapters
    store: JobStore
    worker: JobWorker
    draining: bool = False
    loading_since: dict[str, float] = field(default_factory=dict)
    last_bundle: Path | None = None
    fake_services: bool = False
