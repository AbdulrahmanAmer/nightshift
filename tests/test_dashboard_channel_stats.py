# ruff: noqa: E501
"""Channel `stats` (CR-C3-UI-6): QC pass rate over the last 30 days, target, week count and retention."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ns_common.models import Channel, ChannelStats
from ns_common.paths import Paths
from ns_dashboard.channel_stats import (
    DEFAULT_QC_PASS_TARGET,
    channel_stats,
    qc_pass_target,
    qc_tallies,
)
from ns_dashboard.fakes import seed_channel, seed_output
from ns_dashboard.state import open_db

pytest.importorskip("fastapi")
from tests.test_dashboard_api import make_env  # noqa: E402

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)


def write_job(
    paths: Paths,
    channel: str,
    days_ago: float,
    results: dict[str, bool],
    *,
    suffix: str = "ab12",
    report_only: bool = False,
) -> str:
    """A job folder as the QC stage leaves it: job.json, qc/<item>.json per item and qc/report.json."""
    created = NOW - timedelta(days=days_ago)
    job_id = f"{channel}-{created:%Y%m%d}-{suffix}"
    d = paths.jobs_dir / job_id
    (d / "qc").mkdir(parents=True)
    (d / "job.json").write_text(
        json.dumps({"job_id": job_id, "channel": channel, "fmt": "master_first", "created_at": created.isoformat(timespec="seconds")}),
        encoding="utf-8",
    )
    if not report_only:
        for item, passed in results.items():
            doc = {"item": item, "passed": passed, "checks": {}, "reason": None if passed else "loudness"}
            (d / "qc" / f"{item}.json").write_text(json.dumps(doc), encoding="utf-8")
    report = {item: {"passed": passed, "reason": None if passed else "loudness"} for item, passed in results.items()}
    (d / "qc" / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return job_id


def test_pass_rate_is_passed_over_rendered_in_the_last_30_days(tmp_home: Path):
    paths = Paths.from_env()
    write_job(paths, "after-dark", 1, {"long": True, "s1": True, "s2": False}, suffix="aaaa")
    write_job(paths, "after-dark", 20, {"s1": True, "s2": True, "s3": True}, suffix="bbbb")
    write_job(paths, "after-dark", 31, {"s1": False, "s2": False}, suffix="cccc")  # outside the window
    write_job(paths, "tiny-mysteries", 2, {"s1": False}, suffix="dddd")
    tallies = qc_tallies(paths, NOW)
    assert (tallies["after-dark"].passed, tallies["after-dark"].total) == (5, 6)
    assert tallies["after-dark"].rate == pytest.approx(5 / 6, abs=1e-3)
    assert tallies["tiny-mysteries"].rate == 0.0


def test_the_window_edge_follows_created_at(tmp_home: Path):
    paths = Paths.from_env()
    write_job(paths, "after-dark", 29.5, {"s1": True}, suffix="aaaa")
    write_job(paths, "after-dark", 30.5, {"s1": False}, suffix="bbbb")
    t = qc_tallies(paths, NOW)["after-dark"]
    assert (t.passed, t.total) == (1, 1)
    assert qc_tallies(paths, NOW + timedelta(days=2)) == {}  # both jobs have left the window


def test_report_json_is_used_when_item_files_are_missing(tmp_home: Path):
    paths = Paths.from_env()
    write_job(paths, "after-dark", 3, {"long": True, "s1": False}, report_only=True)
    t = qc_tallies(paths, NOW)["after-dark"]
    assert (t.passed, t.total) == (1, 2)


def test_nothing_checked_means_none_and_junk_is_ignored(tmp_home: Path):
    paths = Paths.from_env()
    assert qc_tallies(paths, NOW) == {}  # no jobs folder yet
    paths.ensure()
    (paths.jobs_dir / "no-qc-yet-20261101-aaaa").mkdir()
    (paths.jobs_dir / "stray-file.txt").write_text("x", encoding="utf-8")
    bad = paths.jobs_dir / "bad-20261101-bbbb"
    (bad / "qc").mkdir(parents=True)
    (bad / "job.json").write_text("{not json", encoding="utf-8")
    broken = write_job(paths, "after-dark", 1, {"s1": True}, suffix="cccc")
    (paths.jobs_dir / broken / "qc" / "s2.json").write_text("{nope", encoding="utf-8")
    t = qc_tallies(paths, NOW)
    assert list(t) == ["after-dark"] and (t["after-dark"].passed, t["after-dark"].total) == (1, 1)
    empty = write_job(paths, "night-desk", 1, {}, suffix="dddd")
    assert empty and qc_tallies(paths, NOW)["night-desk"].rate is None


def test_target_defaults_to_0_9_and_honours_a_channel_value():
    ch = Channel(slug="a-b", display_name="A", niche="n")
    assert qc_pass_target(ch) == DEFAULT_QC_PASS_TARGET == 0.9
    assert qc_pass_target(SimpleNamespace(qc_pass_target=0.75)) == 0.75  # type: ignore[arg-type]
    for bad in (1.5, -0.1, True, "0.8", None):
        assert qc_pass_target(SimpleNamespace(qc_pass_target=bad)) == 0.9  # type: ignore[arg-type]


def test_channel_stats_object_has_the_contract_shape(tmp_home: Path):
    paths = Paths.from_env()
    db = open_db(paths)
    seed_channel(db, paths, "after-dark")
    seed_channel(db, paths, "night-desk")
    write_job(paths, "after-dark", 1, {"long": True, "s1": True, "s2": True, "s3": False}, suffix="aaaa")
    chans = [Channel(slug="after-dark", display_name="A", niche="n"), Channel(slug="night-desk", display_name="N", niche="n")]
    stats = channel_stats(paths, db, chans, NOW)
    a = ChannelStats.model_validate(stats["after-dark"])
    assert a.qc_pass_rate == 0.75 and a.qc_pass_target == 0.9 and a.spark == [] and a.watched_pct is None
    n = ChannelStats.model_validate(stats["night-desk"])
    assert n.qc_pass_rate is None and n.week == 0
    db.close()


def test_week_count_and_retention_come_from_outputs_and_analytics(tmp_home: Path):
    paths = Paths.from_env()
    db = open_db(paths)
    seed_channel(db, paths, "after-dark")
    recent_a = seed_output(db, paths, kind="long", with_files=False)
    recent_b = seed_output(db, paths, kind="short", k=1, job_id=recent_a.split("/")[0], with_files=False)  # same job
    other_job = seed_output(db, paths, kind="long", with_files=False)
    old = seed_output(db, paths, kind="long", with_files=False)
    for oid, when in ((recent_a, NOW - timedelta(days=1)), (recent_b, NOW - timedelta(days=1)), (other_job, NOW - timedelta(days=6)), (old, NOW - timedelta(days=9))):
        db.execute("UPDATE outputs SET created_at = ? WHERE output_id = ?", (when.isoformat(), oid))
    for oid, day, pct in ((recent_a, "2026-10-30", 50.0), (other_job, "2026-10-30", 70.0), (recent_a, "2026-11-01", 80.0), (old, "2026-09-01", 10.0)):
        db.execute("INSERT INTO analytics(output_id, date, avg_pct_viewed) VALUES (?, ?, ?)", (oid, day, pct))
    stats = channel_stats(paths, db, [Channel(slug="after-dark", display_name="A", niche="n")], NOW)["after-dark"]
    assert stats["week"] == 2  # two distinct jobs in the last 7 days
    assert stats["spark"] == [60.0, 80.0]  # per day, oldest first; the September row is outside 14 days
    assert stats["watched_pct"] == pytest.approx((50 + 70 + 80) / 3, abs=0.1)
    db.close()


def test_channels_route_returns_stats_with_the_qc_pass_rate(tmp_home: Path):
    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths, "after-dark")
    seed_channel(state.db, state.paths, "night-desk")
    now = datetime.now(UTC)
    d = state.paths.jobs_dir / f"after-dark-{now:%Y%m%d}-ab12"
    (d / "qc").mkdir(parents=True)
    (d / "job.json").write_text(json.dumps({"job_id": d.name, "channel": "after-dark", "created_at": now.isoformat()}), encoding="utf-8")
    for item, passed in (("s1", True), ("s2", True), ("s3", True), ("s4", False)):
        (d / "qc" / f"{item}.json").write_text(json.dumps({"item": item, "passed": passed, "checks": {}}), encoding="utf-8")
    cards = {c["slug"]: c for c in client.get("/api/channels").json()}
    stats = ChannelStats.model_validate(cards["after-dark"]["stats"])
    assert stats.qc_pass_rate == 0.75 and stats.qc_pass_target == 0.9
    assert ChannelStats.model_validate(cards["night-desk"]["stats"]).qc_pass_rate is None
    assert {"week", "watched_pct", "spark", "qc_pass_rate", "qc_pass_target"} == set(cards["after-dark"]["stats"])
