"""The `stats` object of a channel card (CR-C3-UI-6): this week's count, retention and the QC pass rate.

`qc_pass_rate` is passed / total over the QC results written by jobs of the last 30 days. A job's QC stage
writes `data/jobs/<job_id>/qc/<item>.json` (a `QcResult`) for every rendered video, passed or not, and
`qc/report.json` with the same verdicts; `job.json` names the channel and the creation time. None means no
video of the channel was rendered and checked in that window.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ns_common.models import Channel, ChannelStats
from ns_common.paths import Paths
from ns_dashboard.state import LockedDb

QC_WINDOW_DAYS = 30
DEFAULT_QC_PASS_TARGET = 0.9
RETENTION_DAYS = 14
_JOB_DATE = re.compile(r"-(\d{8})-[0-9a-f]{4}$")


@dataclass
class QcTally:
    """Rendered videos that were checked, and how many passed."""

    passed: int = 0
    total: int = 0

    @property
    def rate(self) -> float | None:
        """Share that passed (0..1), or None when nothing was checked."""
        return round(self.passed / self.total, 4) if self.total else None


def _aware(text: str) -> datetime | None:
    try:
        at = datetime.fromisoformat(text)
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _verdicts(qc_dir: Path) -> list[bool]:
    """One pass/fail per checked item of a job: the per-item files, else `report.json`."""
    out: list[bool] = []
    for f in sorted(qc_dir.glob("*.json")):
        if f.name == "report.json":
            continue
        data = _read_json(f)
        if isinstance(data, dict) and isinstance(data.get("passed"), bool):
            out.append(data["passed"])
    if out:
        return out
    report = _read_json(qc_dir / "report.json")
    if isinstance(report, dict):
        return [bool(v["passed"]) for v in report.values() if isinstance(v, dict) and "passed" in v]
    return []


def qc_tallies(paths: Paths, now: datetime | None = None, days: int = QC_WINDOW_DAYS) -> dict[str, QcTally]:
    """QC pass counts per channel slug over the last `days` days."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    out: dict[str, QcTally] = {}
    root = paths.jobs_dir
    if not root.is_dir():
        return out
    for job_dir in root.iterdir():
        qc_dir = job_dir / "qc"
        if not qc_dir.is_dir():
            continue
        m = _JOB_DATE.search(job_dir.name)
        if m and m.group(1) < (cutoff - timedelta(days=1)).strftime("%Y%m%d"):
            continue  # the date in the job id is older than the window: skip without reading anything
        meta = _read_json(job_dir / "job.json")
        if not isinstance(meta, dict) or not isinstance(meta.get("channel"), str):
            continue
        created = _aware(str(meta.get("created_at") or ""))
        if created is None or created < cutoff:
            continue
        tally = out.setdefault(meta["channel"], QcTally())
        for verdict in _verdicts(qc_dir):
            tally.total += 1
            tally.passed += int(verdict)
    return out


def qc_pass_target(channel: Channel) -> float:
    """The line the Channels page draws: the profile's `qc_pass_target` (0.9 unless the profile sets it)."""
    return float(channel.qc_pass_target)


def _retention(db: LockedDb, slug: str, now: datetime) -> tuple[float | None, list[float]]:
    """Mean `avg_pct_viewed` of the channel's videos over the last 14 days, and its daily trend."""
    since = (now - timedelta(days=RETENTION_DAYS)).strftime("%Y-%m-%d")
    rows = db.query(
        "SELECT a.date AS day, AVG(a.avg_pct_viewed) AS pct FROM analytics a"
        " JOIN outputs o ON o.output_id = a.output_id"
        " WHERE o.channel = ? AND a.date >= ? AND a.avg_pct_viewed IS NOT NULL"
        " GROUP BY a.date ORDER BY a.date",
        (slug, since),
    )
    spark = [round(float(r["pct"]), 1) for r in rows]
    mean = db.one(
        "SELECT AVG(a.avg_pct_viewed) AS pct FROM analytics a JOIN outputs o ON o.output_id = a.output_id"
        " WHERE o.channel = ? AND a.date >= ? AND a.avg_pct_viewed IS NOT NULL",
        (slug, since),
    )
    pct = mean["pct"] if mean is not None and mean["pct"] is not None else None
    return (round(float(pct), 1) if pct is not None else None), spark


def channel_stats(
    paths: Paths,
    db: LockedDb,
    channels: list[Channel],
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    """`stats` per channel slug, shaped as `ns_common.models.ChannelStats`."""
    now = now or datetime.now(UTC)
    tallies = qc_tallies(paths, now)
    week_since = (now - timedelta(days=7)).isoformat()
    out: dict[str, dict[str, Any]] = {}
    for ch in channels:
        # the plan the card compares against is full videos a week, so only jobs that made one count; a
        # Shorts-only channel has no full videos and counts every job (the dev channel showed "5 of 1 made"
        # after four one-Short jobs and a clip job)
        kinds = "" if ch.format == "shorts_only" else " AND kind = 'long'"
        made = db.one(
            f"SELECT COUNT(DISTINCT job_id) AS n FROM outputs WHERE channel = ? AND created_at >= ?{kinds}",
            (ch.slug, week_since),
        )
        pct, spark = _retention(db, ch.slug, now)
        stats = ChannelStats(
            week=int(made["n"]) if made is not None else 0,
            watched_pct=pct,
            spark=spark,
            qc_pass_rate=tallies.get(ch.slug, QcTally()).rate,
            qc_pass_target=qc_pass_target(ch),
        )
        out[ch.slug] = stats.model_dump(mode="json")
    return out
