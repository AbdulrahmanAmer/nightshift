"""C2 schedule config: `config\\schedule.json` {run_time, task_name} shared by `ns schedule` and the
supervisor's auto-exit rule (spec 6.2: stay up when a run is due within 60 minutes)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.paths import Paths

TASK_NAME = "Nightshift Daily"
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
#: A task a dev install registered and switched off (it never fires; a real install replaces it enabled).
MODE_DISABLED = "disabled"
#: A task the owner switched off in Task Scheduler (D-28): an update keeps it off, it has no coming run.
MODE_SWITCHED_OFF = "switched_off"
_OFF_MODES = (MODE_DISABLED, MODE_SWITCHED_OFF)


def valid_time(text: str) -> bool:
    """True for a 24-hour `HH:MM` string."""
    return _HHMM.match(text) is not None


def schedule_file(paths: Paths) -> Path:
    """Location of schedule.json."""
    return paths.config / "schedule.json"


def read_run_time(paths: Paths, *, include_disabled: bool = False) -> str | None:
    """The configured nightly `HH:MM`, or None when no schedule is registered or the file is bad.

    A task in an off mode (`disabled`: a dev install; `switched_off`: the owner turned it off) is not a
    schedule: no run is coming, so this says None, unless `include_disabled` asks for the time it would have.
    """
    f = schedule_file(paths)
    try:
        data = json.loads(read_text_retry(f))
        run_time = data["run_time"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if data.get("mode") in _OFF_MODES and not include_disabled:
        return None
    return run_time if isinstance(run_time, str) and valid_time(run_time) else None


def write_run_time(paths: Paths, run_time: str, mode: str = "registered") -> None:
    """Persist the schedule so the supervisor knows when the next run is due.

    `mode` is `registered` (the task runs unattended), `interactive_only` (Windows refused the
    unattended mode, NS-W205: it runs only while the owner is signed in), `disabled` (a dev install: the task
    exists but never fires) or `switched_off` (the owner switched it off in Task Scheduler and an update kept
    it so, D-28). For both off modes `read_run_time` answers None, so no run is expected.
    """
    data = {"run_time": run_time, "task_name": TASK_NAME, "mode": mode}
    write_text_atomic(schedule_file(paths), json.dumps(data, indent=2) + "\n")


def read_schedule_mode(paths: Paths) -> str:
    """`registered`, `interactive_only`, `disabled`, `switched_off` or `none` (no task, or no usable time).

    Only `registered` is a task that wakes the PC and runs while the owner is away. A file without
    `mode` counts as `registered` (written before the mode existed).
    """
    try:
        data = json.loads(read_text_retry(schedule_file(paths)))
        run_time = data["run_time"]
        mode = data.get("mode")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "none"
    if not (isinstance(run_time, str) and valid_time(run_time)):
        return "none"
    return mode if mode in ("interactive_only", *_OFF_MODES) else "registered"


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
