"""C2 weekly update check (spec J8, gap 16): at most once a week the supervisor asks GitHub whether a newer
release exists and, when one does, shows a toast and publishes an `update` event.

The state lives in `config/update_check.json` (`last_checked`, `latest`); a failed check (offline) is not
recorded, so it is retried at the next poll. The Settings notifications toggle is honoured. Everything the
job touches is injectable so tests never reach the network or PowerShell.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ns_common.config import notifications_enabled
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_supervisor import control, notify, releases, update

WEEK_S = 7 * 24 * 3600
TITLE = "Nightshift"

Checker = Callable[[str], dict[str, Any]]
Toaster = Callable[[str, str], bool]


def state_file(paths: Paths) -> Path:
    """`config/update_check.json`."""
    return paths.config / "update_check.json"


def load_state(paths: Paths) -> dict[str, Any]:
    """The saved state; empty when the file is missing or unreadable."""
    try:
        data = json.loads(state_file(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def due(paths: Paths, now: datetime, interval_s: float = WEEK_S) -> bool:
    """True when the last successful check is older than `interval_s` (or there was none)."""
    raw = load_state(paths).get("last_checked")
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return (now - last).total_seconds() >= interval_s


def message(result: dict[str, Any]) -> str:
    """Toast text for a newer release."""
    return f"Version {result['latest']} is available (you have {result['current']}). Open Settings > Update."


def run_check(
    paths: Paths,
    *,
    now: datetime | None = None,
    interval_s: float = WEEK_S,
    current: str | None = None,
    checker: Checker | None = None,
    toaster: Toaster | None = None,
) -> dict[str, Any] | None:
    """One poll of the weekly timer: nothing unless a check is due; otherwise ask GitHub, remember the
    answer and toast when a newer release exists. Returns the check result (None when nothing was due or
    GitHub could not be reached)."""
    now = now or datetime.now(UTC)
    if not due(paths, now, interval_s):
        return None
    log = NsLogger("supervisor", paths.logs, echo=False)
    here = current or update.current_version(control.app_path(paths))
    try:
        result = (checker or releases.check)(here)
    except NsError as e:
        log.warn("update_check_failed", str(e))
        return None
    f = state_file(paths)
    f.parent.mkdir(parents=True, exist_ok=True)
    saved = {"last_checked": now.isoformat(timespec="seconds"), "latest": result["latest"]}
    f.write_text(json.dumps(saved, indent=2) + "\n", encoding="utf-8")
    log.info("update_check", current=here, latest=result["latest"], available=result["update_available"])
    if result["update_available"] and notifications_enabled(paths):
        (toaster or notify.toast_or_note)(TITLE, message(result))
    return result


def make_job(paths: Paths) -> Callable[[], dict[str, Any] | None]:
    """The callable the supervisor polls (in a worker thread)."""
    return lambda: run_check(paths)
