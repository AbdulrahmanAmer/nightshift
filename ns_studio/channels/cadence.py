"""Cadence governor (spec section 18.6, component C7)."""

from __future__ import annotations

import importlib
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from ns_common.db import Db
from ns_common.models import Channel
from ns_common.paths import Paths
from ns_studio import ownership
from ns_studio.channels.crud import all_channels, edit_channel

MAX_SHORTS_PER_DAY = 3
MAX_MASTERS_PER_WEEK = 3
NEW_WEEKS = 2.0
GROWING_WEEKS = 6.0
GROWING_VIEWED_MIN = 60.0
QUALITY_VIEWED_MIN = 50.0
REJECT_RATE_LIMIT = 0.4
REJECT_WINDOW = 10
REJECT_MIN_REVIEWED = 5
CRITIC_BAR_STEP = 0.5
CRITIC_BAR_COOLDOWN_DAYS = 7
JITTER_MIN = 45
#: The GPU kit's own channel (dev/kit/gpukit.py, card gpu-kit): a night never plans it, whatever its status,
#: except one full story video on each of the two nights the kit armed (_kit_night_job).
KIT_CHANNEL = "gpu-kit"


@dataclass
class CadencePlan:
    """What the governor allows a channel right now."""

    channel: str
    stage: str
    masters_per_week: int
    shorts_per_day: int
    quality_drop: bool = False
    reasons: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """One-line summary for `studio status`."""
        drop = " QUALITY DROP" if self.quality_drop else ""
        return (
            f"{self.stage}: {self.masters_per_week} master(s)/week, {self.shorts_per_day} Short(s)/day{drop}"
        )


@dataclass
class DailyJob:
    """Jobs the nightly run should create for one channel today."""

    channel: Channel
    fmt: str
    count: int
    plan: CadencePlan


def _parse(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def channel_age_weeks(db: Db, ch: Channel, now: datetime) -> float:
    """Weeks since the channel was created (falls back to its first recorded combo)."""
    start: datetime | None = None
    if ch.created_at:
        start = _parse(ch.created_at)
    else:
        r = db.one("select min(created_at) c from combos where channel=?", (ch.slug,))
        if r and r["c"]:
            start = _parse(r["c"])
    if start is None:
        return 0.0
    return max(0.0, (now - start).total_seconds() / (7 * 86400))


def shorts_viewed_rate(db: Db, channel: str, now: datetime, days: int) -> float | None:
    """Views-weighted mean viewed % of Shorts over the last `days` analytics days (None without data)."""
    since = (now - timedelta(days=days)).date().isoformat()
    r = db.one(
        "select sum(a.viewed_pct * a.views) wv, sum(a.views) v from analytics a "
        "join outputs o on o.output_id=a.output_id where o.channel=? and o.kind != 'long' and a.date >= ? "
        "and a.viewed_pct is not null and a.views is not null",
        (channel, since),
    )
    if not r or not r["v"]:
        return None
    return float(r["wv"]) / float(r["v"])


def rejection_rate(db: Db, channel: str, limit: int = REJECT_WINDOW) -> tuple[int, int]:
    """`(rejected, reviewed)` among the last `limit` reviewed outputs (pending ones do not count)."""
    rows = db.query(
        "select status from outputs where channel=? and status != 'pending' "
        "order by created_at desc, rowid desc limit ?",
        (channel, limit),
    )
    return sum(1 for r in rows if r["status"] == "rejected"), len(rows)


def plan_channel(db: Db, ch: Channel, now: datetime) -> CadencePlan:
    """Apply the section 18.6 table to one channel."""
    weeks = channel_age_weeks(db, ch, now)
    rate14 = shorts_viewed_rate(db, ch.slug, now, 14)
    reasons: list[str] = []
    if weeks < NEW_WEEKS:
        stage, masters, shorts = "new (weeks 1-2)", 1, 1
    elif weeks < GROWING_WEEKS:
        stage, masters = "growing (weeks 3-6)", 2
        if rate14 is not None and rate14 >= GROWING_VIEWED_MIN:
            shorts = 2
            reasons.append(f"14-day viewed rate {rate14:.0f} % allows 2 Shorts/day")
        else:
            shorts = 1
            reasons.append("no analytics or viewed rate under 60 %: staying at 1 Short/day")
    else:
        stage, masters, shorts = "steady", ch.masters_per_week, ch.shorts_per_day_max
    shorts = min(shorts, ch.shorts_per_day_max, MAX_SHORTS_PER_DAY)
    masters = min(masters, MAX_MASTERS_PER_WEEK)

    drop = False
    rate7 = shorts_viewed_rate(db, ch.slug, now, 7)
    if rate7 is not None and rate7 < QUALITY_VIEWED_MIN:
        drop = True
        reasons.append(f"7-day viewed rate {rate7:.0f} % is under {QUALITY_VIEWED_MIN:.0f} %")
    rejected, reviewed = rejection_rate(db, ch.slug)
    if reviewed >= REJECT_MIN_REVIEWED and rejected / reviewed >= REJECT_RATE_LIMIT:
        drop = True
        reasons.append(f"owner rejected {rejected} of the last {reviewed} outputs")
    if drop:
        shorts = 1
    return CadencePlan(ch.slug, stage, masters, shorts, drop, reasons)


def apply_quality_drop(paths: Paths, db: Db, ch: Channel, plan: CadencePlan, now: datetime) -> float | None:
    """Raise `critic_bar` by 0.5 (max 10) once per cooldown while the quality drop persists.

    Returns the new bar, or None when nothing changed. Recorded as an incident of kind `warning`.
    """
    if not plan.quality_drop:
        return None
    since = (now - timedelta(days=CRITIC_BAR_COOLDOWN_DAYS)).isoformat(timespec="seconds")
    recent = db.one(
        "select 1 from incidents where channel=? and kind='warning' "
        "and note like 'quality_drop%' and ts >= ?",
        (ch.slug, since),
    )
    if recent or ch.critic_bar >= 10.0:
        return None
    new_bar = min(10.0, round(ch.critic_bar + CRITIC_BAR_STEP, 1))
    edit_channel(paths, db, ch.slug, {"critic_bar": new_bar})
    db.execute(
        "insert into incidents(channel, ts, kind, note) values (?,?,?,?)",
        (
            ch.slug,
            now.isoformat(timespec="seconds"),
            "warning",
            f"quality_drop: critic_bar {ch.critic_bar} -> {new_bar}; " + "; ".join(plan.reasons),
        ),
    )
    return new_bar


#: Rows no owner file exists for (written by 0.1.0 to 0.1.14) younger than this may still be running; for
#: everything written since 0.1.15 "running" is decided by a live owner (`ownership`), never by age.
IN_PROGRESS_H = ownership.LEGACY_IN_PROGRESS_H


def _jobs_made(
    db: Db, paths: Paths | None, channel: str, *, long: bool, since: datetime, now: datetime
) -> list[tuple[str, datetime, bool]]:
    """`(job_id, started, running)` of the jobs of `channel` whose combos row is newer than `since`.

    `long` picks masters (`item = 'long'`) or Shorts. A job with a finished video of its own counts as made;
    one without counts only while it is live work (or ended cleanly: `ownership.in_progress`). A job a power
    cut or a crash killed counts for nothing, so its channel is due again at once; `running` is True for live
    work, which is the reason `nothing due` gives.
    """
    op = "=" if long else "!="
    kind = "o.kind = 'long'" if long else "o.kind != 'long'"
    rows = db.query(
        f"select c.job_id job_id, min(c.created_at) started, "
        f"exists(select 1 from outputs o where o.job_id = c.job_id and {kind}) has_out "
        f"from combos c where c.channel=? and c.item {op} 'long' and c.created_at >= ? group by c.job_id",
        (channel, since.isoformat(timespec="seconds")),
    )
    out: list[tuple[str, datetime, bool]] = []
    for r in rows:
        job_id, started = str(r["job_id"]), _parse(r["started"])
        live = paths is not None and ownership.liveness(paths, job_id) == "live"
        if r["has_out"] or live or ownership.in_progress(paths, job_id, started, now):
            out.append((job_id, started, live))
    return out


def _last_master_at(db: Db, channel: str, now: datetime, paths: Paths | None = None) -> datetime | None:
    """When the channel last got a master: the newest finished long video, or the start of a long job that is
    live work. A failed or killed master used to count through its combo row (for 12 hours, then for the whole
    spacing): the first dev-mode nightly failed at clips and the retry planned nothing."""
    done = db.one("select max(created_at) c from outputs where channel=? and kind='long'", (channel,))
    started = [
        s for _, s, _ in _jobs_made(db, paths, channel, long=True, since=now - timedelta(days=7), now=now)
    ]
    stamps = ([_parse(done["c"])] if done and done["c"] else []) + started
    return max(stamps) if stamps else None


def _masters_since(db: Db, channel: str, since: datetime, now: datetime, paths: Paths | None = None) -> int:
    """Masters that count for the week: finished long videos since `since` plus live long jobs."""
    done = db.query(
        "select distinct job_id from outputs where channel=? and kind='long' and created_at >= ?",
        (channel, since.isoformat(timespec="seconds")),
    )
    finished = {str(r["job_id"]) for r in done}
    running = [j for j, _, _ in _jobs_made(db, paths, channel, long=True, since=since, now=now)]
    return len(finished | set(running))


def _busy_reason(paths: Paths | None, jobs: Sequence[tuple[str, datetime, bool]]) -> str | None:
    """ "job X, at stage S, process P" for the live job among `jobs`, if any."""
    if paths is None:
        return None
    for job_id, _, live in jobs:
        if live:
            return ownership.live_description(paths, job_id)
    return None


def jobs_due_today(
    db: Db,
    ch: Channel,
    plan: CadencePlan,
    now: datetime,
    *,
    paths: Paths | None = None,
    why: list[str] | None = None,
) -> int:
    """Number of jobs to create today for `ch` under `plan`.

    With `paths`, work is "running" only while a live process owns it. `why` receives, when nothing is due,
    the true reason: a job still being made (with its stage and process), or the video the channel has.
    """
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    def say(text: str) -> None:
        if why is not None:
            why.append(text)

    if ch.format == "shorts_only":
        made = _jobs_made(db, paths, ch.slug, long=False, since=midnight, now=now)
        due = max(0, plan.shorts_per_day - len(made))
        if not due:
            busy = _busy_reason(paths, made)
            say(f"is still making today's Shorts ({busy})" if busy else "already has today's Shorts")
        return due
    spacing = max(1, 7 // max(1, plan.masters_per_week))
    last = _last_master_at(db, ch.slug, now, paths)
    week = _jobs_made(db, paths, ch.slug, long=True, since=now - timedelta(days=7), now=now)
    busy = _busy_reason(paths, week)
    if last is not None and now - last < timedelta(days=spacing) - timedelta(hours=6):
        say(f"its full video is still being made ({busy})" if busy else "already has this week's full video")
        return 0
    if _masters_since(db, ch.slug, now - timedelta(days=7), now, paths) >= plan.masters_per_week:
        say(f"its full video is still being made ({busy})" if busy else "already has this week's full video")
        return 0
    return 1


def plan_daily(
    paths: Paths, db: Db, now: datetime | None = None, *, why: dict[str, str] | None = None
) -> list[DailyJob]:
    """The nightly plan: one entry per active channel that is due today (never the GPU kit's channel).

    `why` (when given) receives, per active channel that is not due, the reason in words.
    """
    now = now or datetime.now(UTC)
    out: list[DailyJob] = []
    for ch in all_channels(paths):
        if ch.status != "active" or ch.slug == KIT_CHANNEL:
            continue
        plan = plan_channel(db, ch, now)
        reasons: list[str] = []
        count = jobs_due_today(db, ch, plan, now, paths=paths, why=reasons)
        if count > 0:
            out.append(DailyJob(ch, ch.format, count, plan))
        elif why is not None and reasons:
            why[ch.slug] = reasons[0]
    kit = _kit_night_job(paths, db, now)
    if kit is not None:
        out.append(kit)
    return out


def _kit_night_job(paths: Paths, db: Db, now: datetime) -> DailyJob | None:
    """The one exception to never planning the kit's channel: on a night the GPU kit armed (its record,
    `ns_supervisor.kitnights`, read by its one reader), one full story video on it, once a night, never after
    the record's end. Anything unusual (no record, an unreadable one, no channel file): None."""
    def made_tonight(lo: datetime, hi: datetime) -> bool:
        rows = db.query("select created_at from combos where channel = ?", (KIT_CHANNEL,))
        return any(lo <= _aware(r["created_at"]) < hi for r in rows)

    try:
        night = importlib.import_module("ns_supervisor.kitnights").kit_night(paths, now, made_tonight)
        if night is None:
            return None
        ch = next((c for c in all_channels(paths) if c.slug == KIT_CHANNEL), None)
        if ch is None:
            return None
    except Exception:  # noqa: BLE001 - the owner's night never depends on the kit
        return None
    why = [f"the GPU kit's armed night {night}"]
    plan = CadencePlan(ch.slug, f"GPU kit night {night}", 1, 0, reasons=why)
    return DailyJob(ch, "master_first", 1, plan)


def _aware(ts: str) -> datetime:
    when = datetime.fromisoformat(ts)
    return when if when.tzinfo is not None else when.replace(tzinfo=UTC)


# ---------------------------------------------------------------- release order and jitter
@dataclass(frozen=True)
class ReleaseItem:
    """A finished video waiting for a publish slot."""

    output_id: str
    story: str
    kind: str  # "long" or "short"


def interleave(items: list[ReleaseItem]) -> list[ReleaseItem]:
    """Masters first, then Shorts round-robin across stories so neighbours never share a story."""
    masters = [i for i in items if i.kind == "long"]
    groups: dict[str, list[ReleaseItem]] = {}
    for i in items:
        if i.kind != "long":
            groups.setdefault(i.story, []).append(i)
    ordered: list[ReleaseItem] = list(masters)
    queues = list(groups.values())
    while any(queues):
        for q in queues:
            if q:
                ordered.append(q.pop(0))
    return ordered


def _window(window: str) -> tuple[int, int]:
    a, b = window.split("-")
    h1, m1 = (int(x) for x in a.split(":"))
    h2, m2 = (int(x) for x in b.split(":"))
    return h1 * 60 + m1, h2 * 60 + m2


def assign_publish_times(
    items: list[ReleaseItem],
    first_day: date,
    window: str,
    per_day: int,
    rng: random.Random,
    *,
    taken: Mapping[date, int] | None = None,
) -> dict[str, datetime]:
    """Publish time per output: masters on `first_day`, Shorts `per_day` a day from the next day on.

    `first_day` is the channel's local date (slots are local wall-clock times). `taken` counts the Shorts
    earlier nights already scheduled per day: a day with `per_day` of them is skipped, a day with fewer takes
    only the difference, and its new Shorts get the slots between the old ones. Every slot is jittered by up
    to +/-45 minutes and clamped inside the publish window.
    """
    start, end = _window(window)
    span = max(1, end - start)

    def slot(day: date, index: int, count: int) -> datetime:
        centre = start + (index + 0.5) * span / count
        minute = min(end, max(start, centre + rng.uniform(-JITTER_MIN, JITTER_MIN)))
        return datetime(day.year, day.month, day.day, int(minute) // 60, int(minute) % 60)

    out: dict[str, datetime] = {}
    ordered = interleave(items)
    masters = [i for i in ordered if i.kind == "long"]
    shorts = [i for i in ordered if i.kind != "long"]
    for k, m in enumerate(masters):
        out[m.output_id] = slot(first_day, k, max(1, len(masters)))
    per_day = max(1, min(per_day, MAX_SHORTS_PER_DAY))
    day = first_day + timedelta(days=1 if masters else 0)
    queue = list(shorts)
    while queue:
        before = (taken or {}).get(day, 0)
        room = per_day - before
        if room > 0:
            chunk, queue = queue[:room], queue[room:]
            for k, s in enumerate(chunk):
                out[s.output_id] = slot(day, before + k, before + len(chunk))
        day += timedelta(days=1)
    return out
