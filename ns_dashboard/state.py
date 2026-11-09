"""Shared application state: locked DB access, settings file, wiring container (component C9)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ns_common.atomic import read_text_retry, write_text_atomic
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
    """The nightly time of the registered Windows task (`config/schedule.json`, written by `ns schedule`).

    A task the owner switched off (mode `switched_off`) still has its time, and that is the one to show: the
    page must not name another time than the task has (D-28). A dev install's `disabled` task has none.
    """
    try:
        from ns_supervisor.schedule_cfg import MODE_SWITCHED_OFF, read_run_time, read_schedule_mode
    except ImportError:
        return None
    return read_run_time(paths, include_disabled=read_schedule_mode(paths) == MODE_SWITCHED_OFF)


def scheduled_mode(paths: Paths) -> str:
    """`registered`, `interactive_only` (NS-W205: runs only while signed in), `disabled` (a dev install's
    task that never fires), `switched_off` (the owner turned the task off in Task Scheduler; D-28) or `none`
    (no nightly task)."""
    try:
        from ns_supervisor.schedule_cfg import read_schedule_mode
    except ImportError:
        return "none"
    return read_schedule_mode(paths)


def schedule_is_registered(paths: Paths) -> bool:
    """True when Windows holds a 'Nightshift Daily' task the owner has: `registered`, `interactive_only` or
    `switched_off`.

    `schedule.json` existing is not enough: a dev install's `disabled` task never fires, so the page must
    not say a task exists. An `interactive_only` task (NS-W205) does exist and runs whenever the owner is
    signed in, so it stays registered here and `schedule_mode` carries the difference. A `switched_off` task
    exists too, so the page must not say Windows refused it; `EngineService.next_run` says None for it, so the
    page shows "Automatic runs off" instead of a coming run.
    """
    return scheduled_mode(paths) in ("registered", "interactive_only", "switched_off")


def status_schedule_mode(paths: Paths) -> str:
    """`scheduled_mode` as the Status contract spells it: only `registered` and `interactive_only`, the rest
    (`disabled`, `switched_off`) read as `none`."""
    mode = scheduled_mode(paths)
    return mode if mode in ("registered", "interactive_only") else "none"


_RECORD_CHECK_EVERY_S = 60.0
_record_checked: dict[str, float] = {}


def refresh_schedule_record(paths: Paths, *, now: Callable[[], float] = time.monotonic) -> None:
    """Let `schedule.json` follow a task the owner switched off once they change it in Task Scheduler.

    Only when the record says `switched_off`, at most once a minute (it asks PowerShell), and never raising:
    without it the page would go on saying "Automatic runs off" after the owner switched the task back on.
    """
    if scheduled_mode(paths) != "switched_off":
        return
    key = str(paths.home)
    t = now()
    if t - _record_checked.get(key, -_RECORD_CHECK_EVERY_S) < _RECORD_CHECK_EVERY_S:
        return
    _record_checked[key] = t
    try:
        from ns_supervisor.schedule import sync_record

        sync_record(paths)
    except Exception:  # a task that cannot be read changes nothing
        return


def load_dashboard_settings(paths: Paths) -> dict[str, Any]:
    """Settings with defaults filled in; a missing or corrupt file yields the defaults.

    `run_time` is the time of the registered nightly task when there is one, because that is when the engine
    really starts; `dashboard.json` only remembers what the owner typed.
    """
    out = dict(DEFAULT_SETTINGS)
    f = settings_file(paths)
    if f.exists():
        try:
            raw = json.loads(read_text_retry(f))
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
    write_text_atomic(settings_file(paths), json.dumps(merged, indent=2) + "\n")
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
    #: Model downloads started from Settings: name -> {state: running | done | failed, message}.
    model_downloads: dict[str, dict[str, str]] = field(default_factory=dict)
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
        raw = json.loads(read_text_retry(ui_prefs_file(paths)))
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
