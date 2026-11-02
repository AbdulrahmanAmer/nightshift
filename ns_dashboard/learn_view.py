"""The numbers on the Results page, read from the imported analytics (component C9).

`GET /learn/summary` answers what the page draws: `top` and `bottom` videos (views, watched to the end), the
`daily` views of the last 14 days split into Shorts and full videos, the week-8 `milestone`, and the last
import (`imported_at`, `matched`, `unmatched`, `changes`). Everything is computed from `analytics`, `outputs`
and the job log, so a reload shows the same page as right after an import.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from ns_dashboard.state import LockedDb

DAYS = 14
LIST_SIZE = 5


def _latest_per_output(db: LockedDb) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT o.output_id, o.title, o.kind, a.views, a.viewed_pct FROM analytics a"
        " JOIN outputs o ON o.output_id = a.output_id"
        " WHERE a.date = (SELECT MAX(date) FROM analytics WHERE output_id = a.output_id)"
    )
    return [
        {
            "title": r["title"] or r["output_id"],
            "views": int(r["views"] or 0),
            "viewed_pct": round(float(r["viewed_pct"] or 0), 1),
            "kind": r["kind"],
        }
        for r in rows
    ]


def top_bottom(db: LockedDb) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The most and the least watched videos (by views), at most five each, never the same video twice."""
    rows = sorted(_latest_per_output(db), key=lambda r: -r["views"])
    top = rows[:LIST_SIZE]
    bottom = [r for r in reversed(rows[LIST_SIZE:])][:LIST_SIZE]
    return top, bottom


def daily(db: LockedDb) -> list[dict[str, Any]]:
    """Views per day for the last 14 days of imported data: `{date, shorts, long}` (clips count as Shorts)."""
    last = db.one("SELECT MAX(date) AS d FROM analytics")
    if last is None or not last["d"]:
        return []
    end = date.fromisoformat(last["d"])
    start = end - timedelta(days=DAYS - 1)
    rows = db.query(
        "SELECT a.date AS d, o.kind AS kind, COALESCE(SUM(a.views), 0) AS v FROM analytics a"
        " JOIN outputs o ON o.output_id = a.output_id WHERE a.date BETWEEN ? AND ? GROUP BY a.date, o.kind",
        (start.isoformat(), end.isoformat()),
    )
    by_day: dict[str, dict[str, int]] = {}
    for r in rows:
        day = by_day.setdefault(r["d"], {"shorts": 0, "long": 0})
        day["long" if r["kind"] == "long" else "shorts"] += int(r["v"])
    return [
        {"date": d.isoformat(), **by_day.get(d.isoformat(), {"shorts": 0, "long": 0})}
        for d in (start + timedelta(days=i) for i in range(DAYS))
    ]


def milestone(db: LockedDb, channels: list[str], now: datetime | None = None) -> dict[str, Any] | None:
    """The week-8 go/no-go of the first channel that has analytics, in the words of the Results page."""
    from ns_studio.learn import status

    if db.one("SELECT 1 AS x FROM analytics LIMIT 1") is None:
        return None
    for slug in channels:
        with db.lock:
            m = status.milestone(db.db, slug, now or datetime.now(UTC))
        if m.verdict == "no-data":
            continue
        weeks = int(m.weeks_since_first_upload or 0)
        return {
            "label": f"Week-8 check for {slug}",
            "rule": (
                f"Shorts watched to the end {status.SHORTS_VIEWED_GO:.0f}% or more with subscribers growing,"
                " or full videos that keep people watching. " + " ".join(m.reasons)
            ).strip(),
            "current": round(m.shorts_viewed_rate_7d or 0),
            "target": round(status.SHORTS_VIEWED_GO),
            "weeks": max(0, min(weeks, int(status.DUE_WEEKS))),
            "weeks_needed": int(status.DUE_WEEKS),
        }
    return None
