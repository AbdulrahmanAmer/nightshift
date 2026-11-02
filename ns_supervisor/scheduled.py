"""C2: dashboard jobs that were scheduled for a later time (`dashboard_jobs.params_json.schedule_at`).

A scheduled job never keeps the engine up: it holds no service, so `Supervisor._busy()` ignores it and the
normal auto-exit and the nightly look-ahead (`schedule_cfg`) decide when the engine ends. What the
supervisor does with them is start the dashboard at startup, so the dashboard's worker runs the jobs that
came due while the engine was down. The nightly task (`Nightshift Daily` -> `studio run --daily`) starts a
headless supervisor, which therefore wakes the dashboard for any overdue job and the worker drains them.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from ns_common.paths import Paths


def overdue_scheduled_jobs(paths: Paths, now: datetime | None = None) -> int:
    """How many queued dashboard jobs have a `schedule_at` that has already passed.

    Reads `studio.db` read-only; no database, an old schema or any read error counts as none.
    """
    db = paths.db_path
    if not db.is_file():
        return 0
    moment = (now or datetime.now()).astimezone()
    try:
        con = sqlite3.connect(f"{Path(db).as_uri()}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return 0
    try:
        rows = con.execute(
            "SELECT params_json FROM dashboard_jobs"
            " WHERE status = 'queued' AND params_json LIKE '%schedule_at%'"
        ).fetchall()
    except sqlite3.Error:
        return 0
    finally:
        con.close()
    due = 0
    for (raw,) in rows:
        try:
            at = json.loads(raw)["params"]["schedule_at"]
            when = datetime.fromisoformat(at)
        except (ValueError, KeyError, TypeError):
            continue
        if when.tzinfo is None:
            when = when.astimezone()
        if when <= moment:
            due += 1
    return due
