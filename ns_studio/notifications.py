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


def _fate(item: dict[str, Any]) -> str:
    """What became of an interrupted job: `resumed`, `waiting` (the GPU was busy), or still needing a person:
    `retry` (a Queue card stands for it) or `command` (it has none: `studio run --resume`)."""
    fate = item.get("state") or ("resumed" if item.get("resumed") else "open")
    if fate in ("resumed", "waiting"):
        return str(fate)
    return "retry" if item.get("has_card", True) else "command"


def _interrupted_parts(items: Sequence[dict[str, Any]]) -> list[str]:
    """What the toast says about an earlier night that was killed (power cut, crash)."""
    out: list[str] = []
    for fate, text in (
        ("resumed", "an interrupted night was resumed (it had stopped at {at})"),
        (
            "waiting",
            "an interrupted night waits for the GPU, the next night continues it (it stopped at {at})",
        ),
        ("retry", "an interrupted night needs Retry in the Queue (it stopped at {at})"),
        (
            "command",
            "an interrupted night can be continued with studio run --resume {job} (it stopped at {at})",
        ),
    ):
        mine = [i for i in items if _fate(i) == fate]
        if fate == "command":  # `--resume` takes one job: one command per job, each one runnable as written
            for i in mine:
                at = str(i.get("stage") or "an unknown stage")
                out.append(text.format(at=at, job=str(i.get("job_id") or "")))
        elif mine:
            stages = [str(i.get("stage") or "an unknown stage") for i in mine]
            out.append(text.format(at=", ".join(dict.fromkeys(stages)), job=""))
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
