"""End-of-run toasts for the nightly run (component C7, spec J10, gap 14).

"Nightshift: 1 master + 5 Shorts ready for review" after a night that made videos, and
"Nightshift: run failed (NS-E203), open Dashboard" when it did not. The toast itself is
`ns_supervisor.notify.toast` (PowerShell, no extra package), reached through a lazily imported callable so C7
keeps no import of C2 at module level; the Settings > Notifications toggle (`config/dashboard.json`) is
honoured by `send`.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Sequence
from typing import Any

from ns_common.config import notifications_enabled
from ns_common.db import Db
from ns_common.logging import NsLogger
from ns_common.paths import Paths

Notifier = Callable[[str, str], object]
TITLE = "Nightshift"


def default_notifier() -> Notifier | None:
    """`ns_supervisor.notify.toast_or_note` (a toast, or a morning note when no desktop session can show one).

    Falls back to plain `toast` for a supervisor without notes, and to None without the package.
    """
    try:
        module = importlib.import_module("ns_supervisor.notify")
        return getattr(module, "toast_or_note", None) or module.toast
    except (ImportError, AttributeError):
        return None


def nightly_notifier(paths: Paths, log: NsLogger) -> Notifier | None:
    """The notifier for the nightly run, decided by Settings > Notifications alone.

    Fake services (a dev or engine-test install) do not matter: the toast is a Windows integration that does
    not depend on the engines. When no notifier is returned, `toast_skipped` says why.
    """
    if not notifications_enabled(paths):
        log.info("toast_skipped", reason="notifications_off")
        return None
    notifier = default_notifier()
    if notifier is None:
        log.info("toast_skipped", reason="no_notifier")
    return notifier


def output_counts(db: Db, job_ids: Sequence[str]) -> tuple[int, int]:
    """(masters, Shorts) the given jobs put in the review queue."""
    if not job_ids:
        return 0, 0
    marks = ",".join("?" for _ in job_ids)
    sql = f"select kind, count(*) as n from outputs where job_id in ({marks}) group by kind"
    rows = db.query(sql, job_ids)
    by_kind = {r["kind"]: int(r["n"]) for r in rows}
    return by_kind.get("long", 0), by_kind.get("short", 0)


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def nightly_message(report: dict[str, Any], masters: int, shorts: int) -> str | None:
    """The toast text for a finished nightly run, or None when nothing was due and nothing failed."""
    failures: list[dict[str, Any]] = report.get("failures", [])
    done = len(report.get("jobs", []))
    made: list[str] = []
    if masters:
        made.append(_plural(masters, "master", "masters"))
    if shorts:
        made.append(_plural(shorts, "Short", "Shorts"))
    parts: list[str] = []
    if made:
        parts.append(" + ".join(made) + " ready for review")
    elif done:
        parts.append(f"{_plural(done, 'job', 'jobs')} finished, open Dashboard to review")
    if failures:
        codes = ", ".join(dict.fromkeys(str(f.get("code", "?")) for f in failures))
        if parts:
            parts.append(f"{_plural(len(failures), 'job', 'jobs')} failed ({codes})")
        else:
            parts.append(failure_message(codes))
    parts.extend(_interrupted_parts(report.get("interrupted_jobs") or []))
    unreadable = report.get("unreadable_profiles", [])
    if unreadable:
        parts.append(f"{_plural(len(unreadable), 'channel', 'channels')} skipped, profile cannot be read")
    return "; ".join(parts) if parts else None


def _interrupted_parts(items: Sequence[dict[str, Any]]) -> list[str]:
    """What the toast says about an earlier night that was killed (power cut, crash)."""
    out: list[str] = []
    for resumed, text in (
        (True, "an interrupted night was resumed (it had stopped at {at})"),
        (False, "an interrupted night needs Retry in the Queue (it stopped at {at})"),
    ):
        stages = [
            str(i.get("stage") or "an unknown stage") for i in items if bool(i.get("resumed")) is resumed
        ]
        if stages:
            out.append(text.format(at=", ".join(dict.fromkeys(stages))))
    return out


def failure_message(code: str) -> str:
    """The toast text when the whole run stopped with `code`."""
    return f"run failed ({code}), open Dashboard"


def send(paths: Paths, log: NsLogger, notifier: Notifier | None, message: str | None) -> bool:
    """Show `message` unless notifications are off, there is no notifier, or the toast fails."""
    if notifier is None or not message:
        return False
    if not notifications_enabled(paths):
        log.info("toast_suppressed", message=message)
        return False
    try:
        shown = bool(notifier(TITLE, message))
    except Exception as e:  # a toast must never fail the night
        log.warn("toast_failed", str(e))
        return False
    log.info("toast", message=message, shown=shown)
    return shown
