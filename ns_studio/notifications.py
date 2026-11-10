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


#: Plans whose job makes a full video (and its Shorts); every other plan makes one Short a job.
FULL_VIDEO_PLANS = ("master_first", "master_from_script", "master_from_owner_script")


def _limit(report: dict[str, Any]) -> int | None:
    value = (report.get("timing") or {}).get("limit_s")
    return int(value) if isinstance(value, int | float) and value > 0 else None


def time_part(report: dict[str, Any]) -> str | None:
    """The toast's part about work the night left for time (card night-fits, S10), or None."""
    from ns_studio.nightclock import limit_adjective

    items = [i for i in report.get("out_of_time") or [] if i.get("reason") != STAGE_TOO_LONG]
    if not items:
        return None
    n = sum(int(i.get("jobs") or 1) for i in items)
    limit = _limit(report)
    within = f"Windows' {limit_adjective(limit)} limit" if limit else "Windows' limit"
    return f"{_plural(n, 'video', 'videos')} not finished tonight, to end inside {within} (NS-W211)"


def _left_words(item: dict[str, Any]) -> str:
    """One thing the night left for time, as the morning note says it."""
    from ns_studio.nightclock import words

    channel = str(item.get("channel") or "a channel")
    need, left = item.get("need_s"), item.get("left_s")
    was = "" if left is None else f", {words(left)} {'was' if 0 <= left < 120 else 'were'} left"
    if item.get("job_id") and not item.get("resumed"):  # S6: stopped at a stage boundary
        return f"{channel}'s video stopped at {item.get('stage') or 'a stage'} and continues next"
    if item.get("job_id"):  # S3: a half-made video that was not continued tonight
        about = "it needs more time" if need is None else f"it needs about {words(need)} more"
        if item.get("basis") == "default":
            about += " by Nightshift's first estimate"
        at = f", at {item['stage']}" if item.get("stage") else ""
        return f"{channel}'s half-made video ({about}{at}{was}) continues next"
    jobs = int(item.get("jobs") or 1)
    full = str(item.get("fmt") or "") in FULL_VIDEO_PLANS
    what = _plural(jobs, "full video", "full videos") if full else _plural(jobs, "Short", "Shorts")
    what = what.removeprefix("1 ") if jobs == 1 else what
    if need is None:
        return f"{channel}'s {what}"
    if item.get("basis") == "default":
        took = f"Nightshift plans {words(need)} for one until it has timed one on this PC"
    else:
        took = f"{'it' if jobs == 1 else 'each'} takes about {words(need)} here"
    return f"{channel}'s {what} ({took}{was})"


#: `out_of_time[].reason` of a job holding a stage longer than a whole night here (`daily.STAGE_TOO_LONG`).
STAGE_TOO_LONG = "stage_too_long"


def _long_job_words(item: dict[str, Any]) -> str:
    """(c) A video more work than one night can give: made over several nights."""
    from ns_studio.nightclock import words
    from ns_studio.ownership import RESUME_WITHIN_DAYS

    channel = str(item.get("channel") or "a channel")
    what = "full video" if str(item.get("fmt") or "") in FULL_VIDEO_PLANS else "Short"
    nights = int(item.get("nights") or 2)
    out = (
        f"{channel}'s {what} takes about {words(item.get('need_s') or 0)} of work here, more than the "
        f"{words(item.get('capacity_s') or 0)} one night can give, so it is made over {nights} nights: each "
        "night continues it where the last one stopped"
    )
    if nights > RESUME_WITHIN_DAYS:
        out += (
            f". That is more than {RESUME_WITHIN_DAYS} nights for one video; to make videos in fewer nights, "
            "set Default type to Shorts only for that channel in Channels"
        )
    return out


def _too_long_words(item: dict[str, Any]) -> str:
    """(d) A stage measured longer than a whole night: the night cannot finish it, ever, on this PC."""
    from ns_studio.nightclock import words

    channel = str(item.get("channel") or "a channel")
    what = "full videos" if str(item.get("plan") or "") in FULL_VIDEO_PLANS else "Shorts"
    took, night = words(item.get("measured_s") or 0), words(item.get("capacity_s") or 0)
    return (
        f"the {item.get('stage')} stage of {channel}'s {what} took at least {took} on this PC, longer than "
        f"the {night} one night can give, so the nightly run no longer begins it and {channel}'s {what} are "
        "not made at night. Make them from Create (a run you start yourself has no time limit), or set "
        "Default type to Shorts only for that channel in Channels"
    )


def time_note(report: dict[str, Any]) -> str | None:
    """The morning note of a night that left work for time (card night-fits, S10): how long the night took
    of the time Windows allows the task, what was left with the numbers it was decided on, how it continues
    and what the owner can change; a video made over several nights (c) and a stage longer than any night (d,
    said once). None when there is nothing of that."""
    from ns_studio.nightclock import limit_words, words

    items = [i for i in report.get("out_of_time") or [] if i.get("reason") != STAGE_TOO_LONG]
    long_jobs: list[dict[str, Any]] = report.get("long_jobs") or []
    too_long: list[dict[str, Any]] = report.get("too_long_stages") or []
    if not (items or long_jobs or too_long):
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
    parts = [f"{head}."]
    if items:
        parts.append(f"Not finished in the night: {'; '.join(_left_words(i) for i in items)}.")
    parts += [f"{_long_job_words(i)}." for i in long_jobs]
    parts += [f"{_too_long_words(i)[:1].upper()}{_too_long_words(i)[1:]}." for i in too_long]
    if items or long_jobs:
        parts.append(
            f"The next nightly run continues what was left first ({WHILE_NIGHTLY_ON_CLAUSE}). To fit more "
            "into one night, set fewer Full videos per week for a channel in Channels, or pause a channel."
        )
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
