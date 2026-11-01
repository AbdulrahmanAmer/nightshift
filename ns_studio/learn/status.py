"""`studio status` data: performers, rejections, incidents and the week-8 go/no-go (component C7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

from ns_common.db import Db
from ns_common.models import Channel
from ns_common.paths import Paths
from ns_studio.learn import reweight

SHORTS_VIEWED_GO = 55.0
MASTER_AVG_VIEW_GO_S = 180.0
WATCH_HOURS_TARGET = 4000.0
DUE_WEEKS = 8.0


@dataclass
class Performer:
    """One output with its score."""

    output_id: str
    title: str
    score: float


@dataclass
class ChannelStatus:
    """Everything `studio status` prints for one channel."""

    slug: str
    status: str
    counts: dict[str, int] = field(default_factory=dict)
    top: list[Performer] = field(default_factory=list)
    bottom: list[Performer] = field(default_factory=list)
    reject_reasons: list[str] = field(default_factory=list)
    incidents: list[str] = field(default_factory=list)
    last_import: dict = field(default_factory=dict)


def _title(db: Db, output_id: str) -> str:
    r = db.one("select title from outputs where output_id=?", (output_id,))
    return r["title"] if r else output_id


def performers(db: Db, channel: str, n: int = 3) -> tuple[list[Performer], list[Performer]]:
    """Top and bottom `n` outputs by score (empty lists when no analytics are imported)."""
    scored = sorted(reweight.scored_outputs(db, channel), key=lambda t: t[1], reverse=True)
    items = [Performer(oid, _title(db, oid), s) for oid, s, _ in scored]
    return items[:n], list(reversed(items[-n:])) if len(items) > n else []


def channel_status(db: Db, paths: Paths, ch: Channel) -> ChannelStatus:
    """Collect counts, performers, reject reasons, incidents and the last import for `ch`."""
    st = ChannelStatus(slug=ch.slug, status=ch.status)
    for r in db.query("select status, count(*) n from outputs where channel=? group by status", (ch.slug,)):
        st.counts[r["status"]] = int(r["n"])
    st.top, st.bottom = performers(db, ch.slug)
    for r in db.query(
        "select output_id, review_note from outputs where channel=? and status='rejected' "
        "order by created_at desc limit 10",
        (ch.slug,),
    ):
        st.reject_reasons.append(f"{r['output_id']}: {r['review_note'] or 'no reason given'}")
    for r in db.query(
        "select ts, kind, note from incidents where channel=? order by ts desc, incident_id desc limit 5",
        (ch.slug,),
    ):
        st.incidents.append(f"{r['ts']} {r['kind']}: {r['note'] or ''}".strip())
    st.last_import = reweight.load_snapshot(paths, ch.slug)
    return st


def format_status(st: ChannelStatus, cadence_line: str = "") -> list[str]:
    """Plain-text lines for one channel."""
    lines = [f"channel {st.slug} [{st.status}]"]
    if cadence_line:
        lines.append(f"  cadence: {cadence_line}")
    counts = ", ".join(f"{k}={v}" for k, v in sorted(st.counts.items())) or "no outputs yet"
    lines.append(f"  outputs: {counts}")
    for label, items in (("top", st.top), ("bottom", st.bottom)):
        for p in items:
            lines.append(f"  {label}: {p.score:.3f}  {p.title}  ({p.output_id})")
    if st.reject_reasons:
        lines.append("  recent rejections:")
        lines += [f"    {r}" for r in st.reject_reasons]
    if st.incidents:
        lines.append("  incidents:")
        lines += [f"    {i}" for i in st.incidents]
    if st.last_import:
        lines.append(f"  last import {st.last_import.get('at', '?')}; weights changed:")
        for pool, changes in st.last_import.get("changes", {}).items():
            for opt, (old, new) in changes.items():
                lines.append(f"    {pool}/{opt}: {old:.2f} -> {new:.2f}")
    return lines


# ---------------------------------------------------------------- go / no-go (spec 18.12)
Verdict = Literal["go", "no-go", "too-early", "no-data"]


@dataclass
class Milestone:
    """Result of `studio status --milestone` for one channel."""

    channel: str
    verdict: Verdict
    weeks_since_first_upload: float | None
    shorts_viewed_rate_7d: float | None
    subs_last_7d: int | None
    subs_prev_7d: int | None
    masters_avg_view_s: float | None
    watch_hours: float
    projected_12mo_hours: float | None
    shorts_path_ok: bool
    masters_path_ok: bool
    reasons: list[str] = field(default_factory=list)


def _latest_day(db: Db, channel: str) -> datetime | None:
    r = db.one(
        "select max(a.date) d from analytics a join outputs o on o.output_id=a.output_id where o.channel=?",
        (channel,),
    )
    if not r or not r["d"]:
        return None
    return datetime.fromisoformat(r["d"]).replace(tzinfo=UTC)


def _window_sum(db: Db, channel: str, column: str, start: str, end: str) -> int:
    r = db.one(
        f"select coalesce(sum(a.{column}), 0) s from analytics a join outputs o on o.output_id=a.output_id "
        "where o.channel=? and a.date between ? and ?",
        (channel, start, end),
    )
    return int(r["s"]) if r else 0


def milestone(db: Db, channel: str, now: datetime | None = None) -> Milestone:
    """Compute the week-8 go/no-go from imported analytics."""
    now = now or datetime.now(UTC)
    first = db.one(
        "select min(u.published_at) p from uploads u "
        "join outputs o on o.output_id=u.output_id where o.channel=?",
        (channel,),
    )
    last_day = _latest_day(db, channel)
    weeks: float | None = None
    if first and first["p"]:
        started = datetime.fromisoformat(first["p"])
        started = started if started.tzinfo else started.replace(tzinfo=UTC)
        weeks = (now - started).total_seconds() / (7 * 86400)
    m = Milestone(channel, "no-data", weeks, None, None, None, None, 0.0, None, False, False)
    if last_day is None:
        m.reasons.append("no analytics imported yet")
        return m

    d7 = (last_day - timedelta(days=6)).date().isoformat()
    d14 = (last_day - timedelta(days=13)).date().isoformat()
    d8 = (last_day - timedelta(days=7)).date().isoformat()
    end = last_day.date().isoformat()
    row = db.one(
        "select sum(a.viewed_pct * a.views) wv, sum(a.views) v from analytics a "
        "join outputs o on o.output_id=a.output_id where o.channel=? and o.kind != 'long' "
        "and a.date between ? and ? and a.viewed_pct is not null and a.views is not null",
        (channel, d7, end),
    )
    if row and row["v"]:
        m.shorts_viewed_rate_7d = float(row["wv"]) / float(row["v"])
    last7 = _window_sum(db, channel, "subs_gained", d7, end)
    prev7 = _window_sum(db, channel, "subs_gained", d14, d8)
    m.subs_last_7d, m.subs_prev_7d = last7, prev7

    masters = db.query(
        "select a.views, a.avg_view_duration_s from analytics a join outputs o on o.output_id=a.output_id "
        "and a.date = (select max(date) from analytics where output_id=o.output_id) "
        "where o.channel=? and o.kind='long' and a.avg_view_duration_s is not null",
        (channel,),
    )
    if masters:
        m.masters_avg_view_s = sum(float(r["avg_view_duration_s"]) for r in masters) / len(masters)
        total = sum(float(r["views"] or 0) * float(r["avg_view_duration_s"]) for r in masters)
        m.watch_hours = total / 3600.0
        if weeks and weeks > 0:
            m.projected_12mo_hours = m.watch_hours / weeks * 52.0

    m.shorts_path_ok = (
        m.shorts_viewed_rate_7d is not None
        and m.shorts_viewed_rate_7d >= SHORTS_VIEWED_GO
        and last7 > prev7
    )
    m.masters_path_ok = (
        m.masters_avg_view_s is not None
        and m.masters_avg_view_s >= MASTER_AVG_VIEW_GO_S
        and m.projected_12mo_hours is not None
        and m.projected_12mo_hours >= WATCH_HOURS_TARGET
    )
    if m.shorts_path_ok:
        m.reasons.append(f"Shorts 7-day viewed rate {m.shorts_viewed_rate_7d:.0f} % and subscribers growing")
    if m.masters_path_ok:
        m.reasons.append(
            f"masters average view {m.masters_avg_view_s:.0f} s and {m.projected_12mo_hours:.0f} h/year pace"
        )
    if weeks is None or weeks < DUE_WEEKS:
        m.verdict = "too-early"
        m.reasons.append(f"week 8 after the first upload not reached yet ({weeks or 0:.1f} weeks)")
    elif m.shorts_path_ok or m.masters_path_ok:
        m.verdict = "go"
    else:
        m.verdict = "no-go"
        m.reasons.append("neither the Shorts path nor the masters path meets the section 18.12 thresholds")
    return m


def format_milestone(m: Milestone) -> list[str]:
    """Plain-text lines for `studio status --milestone`."""

    def f(v: float | None, spec: str = ".1f") -> str:
        return "n/a" if v is None else format(v, spec)

    lines = [
        f"milestone {m.channel}: {m.verdict.upper()}",
        f"  weeks since first upload: {f(m.weeks_since_first_upload)}",
        f"  Shorts 7-day viewed rate: {f(m.shorts_viewed_rate_7d)} % (go at >= {SHORTS_VIEWED_GO:.0f})",
        f"  subscribers last 7 d / previous 7 d: {m.subs_last_7d} / {m.subs_prev_7d}",
        f"  masters average view duration: {f(m.masters_avg_view_s, '.0f')} s "
        f"(go at >= {MASTER_AVG_VIEW_GO_S:.0f})",
        f"  watch hours so far: {m.watch_hours:.1f}; 12-month pace: {f(m.projected_12mo_hours, '.0f')} "
        f"(go at >= {WATCH_HOURS_TARGET:.0f})",
    ]
    lines += [f"  - {r}" for r in m.reasons]
    return lines
