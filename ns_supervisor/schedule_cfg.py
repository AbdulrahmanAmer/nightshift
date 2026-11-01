"""C2 schedule config: `config\\schedule.json` {run_time, task_name} shared by `ns schedule` and the
supervisor's auto-exit rule (spec 6.2: stay up when a run is due within 60 minutes)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from ns_common.paths import Paths

TASK_NAME = "Nightshift Daily"
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def valid_time(text: str) -> bool:
    """True for a 24-hour `HH:MM` string."""
    return _HHMM.match(text) is not None


def schedule_file(paths: Paths) -> Path:
    """Location of schedule.json."""
    return paths.config / "schedule.json"


def read_run_time(paths: Paths) -> str | None:
    """The configured nightly `HH:MM`, or None when no schedule is registered or the file is bad."""
    f = schedule_file(paths)
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        run_time = data["run_time"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return run_time if isinstance(run_time, str) and valid_time(run_time) else None


def write_run_time(paths: Paths, run_time: str) -> None:
    """Persist the schedule so the supervisor knows when the next run is due."""
    f = schedule_file(paths)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"run_time": run_time, "task_name": TASK_NAME}, indent=2) + "\n", "utf-8")


def clear_run_time(paths: Paths) -> None:
    """Remove schedule.json (task unregistered)."""
    schedule_file(paths).unlink(missing_ok=True)


def seconds_until_next_run(now: datetime, run_time: str) -> float:
    """Seconds from `now` (local, naive) until the next occurrence of `run_time`."""
    m = _HHMM.match(run_time)
    if m is None:
        raise ValueError(f"bad run_time {run_time!r}")
    target = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    if target < now:
        target += timedelta(days=1)
    return (target - now).total_seconds()
