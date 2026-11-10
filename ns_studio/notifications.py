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
from ns_studio.ownership import WHILE_NIGHTLY_ON_CLAUSE

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
    parts.extend(no_room_message(report))
    left = time_part(report)
    if left:
        parts.append(left)
    return "; ".join(parts) if parts else None


def _limit(report: dict[str, Any]) -> int | None:
    value = (report.get("timing") or {}).get("limit_s")
    return int(value) if isinstance(value, int | float) and value > 0 else None


def _noted(report: dict[str, Any]) -> list[dict[str, Any]]:
    """The `out_of_time` entries the morning note says: all but a new job's "cannot fit a night" already told
    with the same evidence on an earlier night (`told`: the Last night card says it every night)."""
    from ns_studio import nightfit

    return [i for i in report.get("out_of_time") or []
            if i.get("reason") != nightfit.CANNOT_FIT_NEW or i.get("told", True)]


def time_part(report: dict[str, Any]) -> str | None:
    """The toast's part about work the night left for time (card night-fits, S10), or None."""
    from ns_studio import nightfit
    from ns_studio.nightclock import limit_adjective

    items = [i for i in report.get("out_of_time") or [] if i.get("reason") != nightfit.CANNOT_FIT_NEW]
    if not items:
        return None
    n = sum(int(i.get("jobs") or 1) for i in items)
    limit = _limit(report)
    within = f"Windows' {limit_adjective(limit)} limit" if limit else "Windows' limit"
    return f"{_plural(n, 'video', 'videos')} not finished tonight, to end inside {within} (NS-W211)"


def said(item: dict[str, Any]) -> str:
    """The one sentence of an `out_of_time` or `long_jobs` entry: the one the night's decision rendered
    (`nightfit.say`, kept in `said`); an entry without it (a hand-made or older one) is rendered by the same
    function from its fields."""
    from ns_studio import nightfit

    text = item.get("said")
    if isinstance(text, str) and text:
        return text
    reason = str(item.get("reason") or "")
    if reason not in nightfit.REASONS:
        job, resumed = item.get("job_id"), item.get("resumed")
        reason = nightfit.RESUME_NO_ROOM if resumed else nightfit.STOP_NO_ROOM
        reason = reason if job else nightfit.NEW_NO_ROOM
    where = nightfit.RESUME if item.get("resumed") else nightfit.STAGE
    where = where if item.get("job_id") else nightfit.NEW
    d = nightfit.Decision(
        nightfit.REASONS[reason], reason, where, item.get("stage"), item.get("need_s"), item.get("left_s"),
        str(item.get("basis") or nightfit.HISTORY), nights=item.get("nights"),
    )
    return nightfit.say(d, channel=str(item.get("channel") or "a channel"), plan=str(item.get("fmt") or ""),
                        jobs=int(item.get("jobs") or 1))


def time_note(report: dict[str, Any]) -> str | None:
    """The morning note of a night that left work for time (card night-fits, S10): how long the night took
    of the time Windows allows the task, then the sentence of each decision that left or gave up work (and of
    each video begun to be made over several nights), each with its own way on, and what the owner can
    change. None when there is nothing of that."""
    from ns_studio import nightfit
    from ns_studio.nightclock import limit_words, words

    items = _noted(report)
    long_jobs: list[dict[str, Any]] = report.get("long_jobs") or []
    if not (items or long_jobs):
        return None
    timing: dict[str, Any] = report.get("timing") or {}
    limit, used = _limit(report), timing.get("used_s")
    head = "Last night's run"
    if used is not None and limit:
        head += f" used {words(used)} of the {limit_words(limit)} Windows allows it"
    elif used is not None:
        head += f" used {words(used)}"
    else:
        head += " ran"
    gpu = timing.get("gpu_s") or 0
    if gpu >= 60:
        head += f", {words(gpu)} of it getting the graphics card (waiting for it and loading its models)"
    # measured on the simulated graphics card (H-10): the night's times say so, as every reader of the card
    mark = timing.get("simulated_card") if isinstance(timing.get("simulated_card"), str) else None
    parts = [f"{mark}: {head}." if mark else f"{head}."]
    for item in [*long_jobs, *items]:
        text = said(item)
        parts.append(f"{text}." if mark and text.startswith(mark) else f"{text[:1].upper()}{text[1:]}.")
    if any(i.get("reason") in (nightfit.NEW_NO_ROOM, None) and not i.get("job_id")
           for i in items):
        parts.append("To fit more into one night, set fewer Full videos per week for a channel in "
                     "Channels, or pause a channel.")
    return " ".join(parts) + " (NS-W211)"


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
            "an interrupted night waits for the GPU; the next night continues it "
            + WHILE_NIGHTLY_ON_CLAUSE
            + " (it stopped at {at})",
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


def no_room_message(report: dict[str, Any]) -> list[str]:
    """The morning-note line for jobs the night did not start because the drive was too full (D-33)."""
    refused: list[dict[str, Any]] = report.get("no_room", [])
    if not refused:
        return []
    left = sum(int(r.get("jobs", 1)) for r in refused)
    why = refused[0].get("short", "")
    return [f"{_plural(left, 'job', 'jobs')} not started, not enough disk space ({why})"]


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
