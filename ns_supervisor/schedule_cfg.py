"""C2 schedule config: `config\\schedule.json` {run_time, task_name} shared by `ns schedule` and the
supervisor's auto-exit rule (spec 6.2: stay up when a run is due within 60 minutes)."""

from __future__ import annotations

import contextlib
import json
import re
import time
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

from ns_common import first_start
from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_common.singleton import AlreadyRunning, FileLock

TASK_NAME = "Nightshift Daily"
_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
#: A task a dev install registered and switched off (it never fires; a real install replaces it enabled).
MODE_DISABLED = "disabled"
#: What 0.1.15-rc1 and rc2 wrote for a task the owner disabled in Task Scheduler. Nothing writes it any more:
#: off is "no record" (which every updater since 0.1.14 reads as "do not register"); it is still read as off.
MODE_SWITCHED_OFF = "switched_off"
_OFF_MODES = (MODE_DISABLED, MODE_SWITCHED_OFF)
#: Automatic runs are on: Windows held an enabled task when `ns_supervisor.schedule` last looked.
ON_MODES = ("registered", "interactive_only")


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


#: The way to write `schedule.json` again: the command, or the dashboard's switch.
RECORD_WAY_OUT = "'ns schedule install --time HH:MM' (or Settings > Automatic runs) writes it"


def record_note(paths: Paths) -> str | None:
    """What `ns schedule show` adds about the record itself: None when it holds a valid time, else a line
    naming the file and the way out. A missing file is not damage (Automatic runs are off, or were never set,
    `_forget`), and the line says so; one that cannot be read or holds no valid time is said as such
    (`read_run_time` treats both as "no schedule" and says nothing)."""
    f = schedule_file(paths)
    try:
        data = json.loads(read_text_retry(f))
    except FileNotFoundError:
        return (f"{f} does not exist: no nightly time is recorded (Automatic runs are off or were never "
                f"set); {RECORD_WAY_OUT}.")
    except OSError as e:
        return (f"{f} could not be read ({type(e).__name__}: {e}); if another program holds it, close "
                "that program and run 'ns schedule show' again.")
    except ValueError as e:
        return f"{f} could not be read (not valid JSON: {e}); {RECORD_WAY_OUT} again."
    run_time = data.get("run_time") if isinstance(data, dict) else None
    if not (isinstance(run_time, str) and valid_time(run_time)):
        return f"{f} holds no valid nightly time (run_time {run_time!r}); {RECORD_WAY_OUT} again."
    return None


def write_run_time(paths: Paths, run_time: str, mode: str = "registered") -> None:
    """Persist the schedule so the supervisor knows when the next run is due.

    `mode` is `registered` (the task runs unattended), `interactive_only` (Windows refused the
    unattended mode, NS-W205: it runs only while the owner is signed in) or `disabled` (a dev install: the
    task exists but never fires; `read_run_time` answers None for it). Off has no record at all.
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


def is_on(paths: Paths) -> bool:
    """True when the record says Automatic runs are on (an enabled 'Nightshift Daily' task). The one question
    every page, the Assistant and the engine ask; only `ns_supervisor.schedule` writes the answer."""
    return read_schedule_mode(paths) in ON_MODES


def _preferences(paths: Paths) -> dict[str, object]:
    try:
        data = json.loads(read_text_retry(paths.config / "dashboard.json"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


#: How long a writer of `dashboard.json` waits for the other one (each holds the lock for one small write).
PREFERENCES_WAIT_S = 5.0


@contextlib.contextmanager
def preferences_lock(paths: Paths) -> Iterator[None]:
    """Hold `run/dashboard-settings.lock` for one read-modify-write of `config/dashboard.json`.

    The file has two writers that each change one key and must keep the other's: the Settings save
    (`ns_dashboard.state.save_dashboard_settings`, any key) and `remember_run_time` (the kept time, also
    from the background look at Windows and from `ns schedule`, another process). Both read and write inside
    this lock, so neither writes back a stale copy of the other's key. Windows drops the lock with its
    holder.

    The lock is a courtesy between two rare writers, never a reason to lose the owner's change: when it is
    still held after PREFERENCES_WAIT_S, or its file cannot be created or opened at all (`run` read-only or
    not a folder, an antivirus or backup program holding it, a lost permission), the write goes ahead
    unlocked. Only the guard against that one interleaving is missing then, so the owner is not told; the
    log gets one WARN per process (`preferences_lock_unavailable`). An error on release is swallowed.
    """
    lock = FileLock(paths.home / "run" / "dashboard-settings.lock")
    deadline = time.monotonic() + PREFERENCES_WAIT_S
    while True:
        try:
            lock.acquire()
            break
        except AlreadyRunning:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
        except OSError as e:
            _warn_lock_once(paths, e)
            break
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock.release()


_lock_warned: set[str] = set()


def _warn_lock_once(paths: Paths, why: OSError) -> None:
    key = str(paths.home)
    if key in _lock_warned:
        return
    _lock_warned.add(key)
    with contextlib.suppress(OSError):
        NsLogger("schedule", paths.logs).warn(
            "preferences_lock_unavailable",
            f"run/dashboard-settings.lock could not be opened ({why}); dashboard.json is written without it",
        )


def remembered_run_time(paths: Paths) -> str | None:
    """The time kept in `config/dashboard.json` for when Automatic runs are off (None when there is none)."""
    when = _preferences(paths).get("run_time")
    return when if isinstance(when, str) and valid_time(when) else None


def remember_run_time(paths: Paths, run_time: str) -> None:
    """Keep the nightly time the owner chose in `config/dashboard.json` (the dashboard's own settings).

    `schedule.json` is the record of an enabled task. While Automatic runs are off (never switched on,
    switched off, refused by Windows: NS-W206) there is no such file and the chosen time is only a preference;
    switching on registers the task at it.
    """
    target = paths.config / "dashboard.json"
    with preferences_lock(paths):
        data = _preferences(paths)
        data["run_time"] = run_time
        first_start.guard(target)
        write_text_atomic(target, json.dumps(data, indent=2) + "\n")


def clear_run_time(paths: Paths) -> None:
    """Remove schedule.json: Automatic runs are off (no task, or one disabled in Task Scheduler)."""
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
