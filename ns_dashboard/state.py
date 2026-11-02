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
        """Close the underlying connection (the next query opens it again)."""
        with self.lock:
            self.db.close()

    @contextmanager
    def released(self) -> Iterator[None]:
        """Hold the database closed for the block (other threads wait), then open it and migrate it again.

        For a restore that replaces `studio.db`: a connection that stays open (or is opened by a polling
        request in between) keeps the `-wal` and `-shm` files locked on Windows.
        """
        with self.lock:
            self.db.close()
            try:
                yield
            finally:
                self.db.close()
                self.db.migrate()


def open_db(paths: Paths) -> LockedDb:
    """Open and migrate `studio.db` (the shared schema already has `dashboard_jobs`)."""
    db = Db(paths.db_path)
    db.migrate()
    return LockedDb(db)


def settings_file(paths: Paths) -> Path:
    """Location of the dashboard-owned settings file (config/dashboard.json)."""
    return paths.config / "dashboard.json"


def scheduled_run_time(paths: Paths) -> str | None:
    """The nightly time of the registered Windows task (`config/schedule.json`, written by `ns schedule`)."""
    try:
        from ns_supervisor.schedule_cfg import read_run_time
    except ImportError:
        return None
    return read_run_time(paths)


def load_dashboard_settings(paths: Paths) -> dict[str, Any]:
    """Settings with defaults filled in; a missing or corrupt file yields the defaults.

    `run_time` is the time of the registered nightly task when there is one, because that is when the engine
    really starts; `dashboard.json` only remembers what the owner typed.
    """
    out = dict(DEFAULT_SETTINGS)
    f = settings_file(paths)
    if f.exists():
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            out.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
    registered = scheduled_run_time(paths)
    if registered:
        out["run_time"] = registered
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
    last_backup: Path | None = None
    fake_services: bool = False


UI_THEMES = ("light", "dark", "soft")


def ui_prefs_file(paths: Paths) -> Path:
    """`config/ui_prefs.json`, written by the installer's setup window (CR-C1-UI-8)."""
    return paths.config / "ui_prefs.json"


def load_ui_prefs(paths: Paths) -> dict[str, Any]:
    """`{owner_name, theme}` from the setup window's choices; missing or invalid values come back as None.

    The greeting uses `owner_name`; the first start of the page uses `theme` (the owner's later choice in the
    page is kept in the browser and wins).
    """
    out: dict[str, Any] = {"owner_name": None, "theme": None}
    try:
        raw = json.loads(ui_prefs_file(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(raw, dict):
        return out
    name = raw.get("owner_name")
    if isinstance(name, str) and name.strip():
        out["owner_name"] = " ".join(name.split())[:40]
    theme = raw.get("theme", raw.get("ui_theme"))
    if theme in UI_THEMES:
        out["theme"] = theme
    return out
