"""C2 weekly update check (spec J8, gap 16): at most once a week the supervisor asks GitHub whether a newer
release exists and, when one does, shows a toast and publishes an `update` event.

The state lives in `config/update_check.json` (`last_checked`, `latest`); a failed check (offline) is not
recorded, so it is retried at the next poll. The Settings notifications toggle is honoured. Everything the
job touches is injectable so tests never reach the network or PowerShell.

A check that is REFUSED (`NS-E704`: a test release source that is set where it may not be, or that names no
address on this PC) does not clear by itself the way an offline PC does, so the owner is told, with the
reason: once, and again at most once a week while that reason lasts (`refused` in the state file), by toast
or, with no desktop, a failed toast or notifications off, by a note the dashboard shows. Another reason is
told at once. The next successful check forgets it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.config import notifications_enabled
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_supervisor import control, notify, releases, update

WEEK_S = 7 * 24 * 3600
TITLE = "Nightshift"
#: Refusals that last until somebody changes the PC's settings: the owner is told (`tell_refusal`).
LASTING_REFUSALS = ("NS-E704",)

Checker = Callable[[str], dict[str, Any]]
Toaster = Callable[[str, str], bool]


def state_file(paths: Paths) -> Path:
    """`config/update_check.json`."""
    return paths.config / "update_check.json"


def load_state(paths: Paths) -> dict[str, Any]:
    """The saved state; empty when the file is missing or unreadable."""
    try:
        data = json.loads(read_text_retry(state_file(paths)))
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


def refusal_message(e: NsError) -> str:
    """What the owner reads when update checks are refused: the code, its message, the reason of this
    refusal with its way out (the detail, in brackets as the hint says), and the general hint."""
    d = e.definition
    why = f" ({e.detail})" if e.detail else ""
    return f"Update checks are switched off ({e.code}). {d.message}{why} {d.hint}"


def tell_refusal(
    paths: Paths,
    e: NsError,
    now: datetime,
    toaster: Toaster | None,
    log: NsLogger,
    interval_s: float = WEEK_S,
) -> bool:
    """Tell the owner about a lasting refusal unless they were told about the same one (code and detail)
    within `interval_s`. A state written before the detail was kept counts as the same one.

    Told means a toast was shown or a note waits for the dashboard; only then is it remembered, so a telling
    that failed is tried again at the next poll. With notifications off no toast is shown, only the note.
    Returns True when the owner was told now.
    """
    state = load_state(paths)
    prev = state.get("refused")
    if isinstance(prev, dict) and prev.get("code") == e.code and prev.get("detail", e.detail) == e.detail:
        try:
            told = datetime.fromisoformat(str(prev.get("told")))
            if told.tzinfo is None:
                told = told.replace(tzinfo=UTC)
            if 0 <= (now - told).total_seconds() < interval_s:
                return False
        except (ValueError, TypeError):
            pass  # an unreadable time: tell again
    text = refusal_message(e)

    def waits(notes: list[notify.Note]) -> bool:
        return any(n["message"] == text for n in notes)

    shown = noted = False
    try:
        if notifications_enabled(paths):
            shown = bool((toaster or notify.toast_or_note)(TITLE, text))
        if not shown:
            # `toast_or_note` leaves the note itself when no toast could show: then it waits and is not
            # left twice. Either way the note is in the file when this returns without raising.
            notify.leave_note(paths, TITLE, text, once=waits)
            noted = True
    except Exception as err:  # the telling must never hurt the check; it is tried again at the next poll
        log.error("update_refusal_not_told", f"{type(err).__name__}: {err}", code=e.code)
    if not (shown or noted):
        log.error("update_refusal_not_told", "no toast was shown and no note could be left", code=e.code)
        return False
    state["refused"] = {"code": e.code, "told": now.isoformat(timespec="seconds"), "detail": e.detail}
    try:
        write_text_atomic(state_file(paths), json.dumps(state, indent=2) + "\n")
    except OSError as err:  # told, but not remembered: the owner may be told again at the next poll
        log.error("update_refusal_not_recorded", str(err), code=e.code)
    log.warn("update_refusal_told", text, code=e.code, toast=shown)
    return True


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
    GitHub could not be reached or the check was refused; a lasting refusal is told to the owner)."""
    now = now or datetime.now(UTC)
    if not due(paths, now, interval_s):
        return None
    log = NsLogger("supervisor", paths.logs, echo=False)
    here = current or update.current_version(control.app_path(paths))
    try:
        result = (checker or releases.check)(here)
    except NsError as e:
        log.warn("update_check_failed", str(e))
        if e.code in LASTING_REFUSALS:
            tell_refusal(paths, e, now, toaster, log)
        return None
    saved = {"last_checked": now.isoformat(timespec="seconds"), "latest": result["latest"]}
    write_text_atomic(state_file(paths), json.dumps(saved, indent=2) + "\n")
    log.info("update_check", current=here, latest=result["latest"], available=result["update_available"])
    if result["update_available"] and notifications_enabled(paths):
        (toaster or notify.toast_or_note)(TITLE, message(result))
    return result


def make_job(paths: Paths) -> Callable[[], dict[str, Any] | None]:
    """The callable the supervisor polls (in a worker thread)."""
    return lambda: run_check(paths)
