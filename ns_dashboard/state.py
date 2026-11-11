"""Shared application state: locked DB access, settings file, wiring container (component C9)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ns_common import first_start
from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.db import Db, UnusableDb, open_studio_db
from ns_common.dbrestore import DAMAGED_CODE as DB_DAMAGED
from ns_common.errors import NsError
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
    """Open and migrate `studio.db` (the shared schema already has `dashboard_jobs`).

    Checked first (card db-integrity, `open_studio_db`): a damaged file is replaced by the newest good copy
    and the owner told (NS-W213). When it cannot be (NS-E209: another window has it open, a full disk) the
    dashboard still starts, on an `UnusableDb`: every page that reads the database answers 503 with NS-E209's
    sentence and way out, the job worker does not start, and the bell holds the same words."""
    from ns_studio.notifications import db_note

    try:
        db = open_studio_db(paths, NsLogger("dashboard", paths.logs), note=db_note(paths))
    except NsError as e:
        if e.code != DB_DAMAGED:
            raise
        return LockedDb(UnusableDb(paths.db_path, e))
    db.migrate()
    return LockedDb(db)


def settings_file(paths: Paths) -> Path:
    """Location of the dashboard-owned settings file (config/dashboard.json)."""
    return paths.config / "dashboard.json"


def scheduled_run_time(paths: Paths) -> str | None:
    """The nightly time of the enabled Windows task (`config/schedule.json`, which only
    `ns_supervisor.schedule` writes).

    None while Automatic runs are off (the time is then the one kept in `dashboard.json`). A `switched_off`
    record of 0.1.15-rc2 that no look at Windows has converted yet still carries the task's own time.
    """
    try:
        from ns_supervisor.schedule_cfg import MODE_SWITCHED_OFF, read_run_time, read_schedule_mode
    except ImportError:
        return None
    return read_run_time(paths, include_disabled=read_schedule_mode(paths) == MODE_SWITCHED_OFF)


def scheduled_mode(paths: Paths) -> str:
    """The record as it is: `registered` or `interactive_only` (Automatic runs on; the second runs only while
    signed in, NS-W205), `disabled` (a dev install's task that never fires), `none` (off), or the
    `switched_off` an older candidate wrote (off)."""
    try:
        from ns_supervisor.schedule_cfg import read_schedule_mode
    except ImportError:
        return "none"
    return read_schedule_mode(paths)


def schedule_is_registered(paths: Paths) -> bool:
    """True when Automatic runs are on: Windows held an enabled 'Nightshift Daily' task at the last look.

    The one answer behind `schedule_registered`, `next_run`, the top bar, the Queue card and the Assistant's
    plan. Off (no task, a task disabled in Task Scheduler, a dev install's disabled task) is False and is a
    normal state, not a warning; `schedule_mode` tells the signed-in-only task (NS-W205) from the unattended.
    """
    return status_schedule_mode(paths) != "none"


def status_schedule_mode(paths: Paths) -> str:
    """`scheduled_mode` as the Status contract spells it: `registered` or `interactive_only` when on, `none`
    for everything that is off."""
    mode = scheduled_mode(paths)
    return mode if mode in ("registered", "interactive_only") else "none"


_RECORD_CHECK_EVERY_S = 60.0
_record_checked: dict[str, float] = {}
#: True makes a look at Windows run inside the request that asked (tests only). The product never does: a
#: look can take as long as PowerShell does (bounded at `schedule.READ_TIMEOUT_S`), and no page waits for it.
SYNC_INLINE = False


def refresh_schedule_record(paths: Paths, *, now: Callable[[], float] = time.monotonic) -> None:
    """Let `schedule.json` follow the Windows task: at most one look a minute per home, never raising.

    Without it a task the owner deleted or disabled in Task Scheduler would go on reading "on", and one
    switched on there would read "off". The look runs in a background thread and the page is answered at
    once with the last recorded state (also the first page of a new dashboard); what the look found shows on
    the next poll, a few seconds later. A dev install has nothing to follow (`sync_record` returns at once).
    """
    key = str(paths.home)
    t = now()
    if key in _record_checked and t - _record_checked[key] < _RECORD_CHECK_EVERY_S:
        return
    _record_checked[key] = t

    def look() -> None:
        try:
            from ns_supervisor.schedule import sync_record

            sync_record(paths)
        except Exception:  # a task that cannot be read changes nothing
            return

    if SYNC_INLINE:
        look()
    else:
        threading.Thread(target=look, name="schedule-sync", daemon=True).start()


def load_dashboard_settings(paths: Paths) -> dict[str, Any]:
    """Settings with defaults filled in; a missing or corrupt file yields the defaults.

    `run_time` is the time of the enabled nightly task when there is one, because that is when the engine
    really starts; while Automatic runs are off it is the time kept in `dashboard.json`.
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
    """Merge `data` into the stored settings and return the result.

    Read and written inside `schedule_cfg.preferences_lock`: the kept run time has a second writer
    (`remember_run_time`, also from the background look at Windows), and neither may write back a stale copy
    of the other's key.
    """
    try:
        from ns_supervisor.schedule_cfg import preferences_lock
    except ImportError:
        preferences_lock = None
    with preferences_lock(paths) if preferences_lock is not None else nullcontext():
        merged = load_dashboard_settings(paths)
        merged.update({k: v for k, v in data.items() if k in DEFAULT_SETTINGS})
        first_start.guard(settings_file(paths))
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
