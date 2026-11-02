import json
import random
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from ns_common.db import Db
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_studio import daily, jobs
from ns_studio import orchestrator as orch
from ns_studio.channels import cadence, crud
from ns_studio.stages import REGISTRY
from tests.helpers import add_analytics, add_output, make_channel

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)


def _env(tmp_home: Path):
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    return paths, db


def _channel(paths, db, age_weeks: float, **kw):
    created = (NOW - timedelta(weeks=age_weeks)).isoformat(timespec="seconds")
    ch = make_channel(created_at=created, **kw)
    crud.add_channel(paths, db, ch)
    return crud.get_channel(paths, ch.slug)


def test_stage_rules(tmp_home: Path):
    paths, db = _env(tmp_home)
    new = cadence.plan_channel(db, _channel(paths, db, 1), NOW)
    assert (new.masters_per_week, new.shorts_per_day) == (1, 1) and "new" in new.stage
    ch = crud.edit_channel(paths, db, "after-dark", {"created_at": (NOW - timedelta(weeks=4)).isoformat()})
    grow = cadence.plan_channel(db, ch, NOW)
    assert (grow.masters_per_week, grow.shorts_per_day) == (2, 1)  # no analytics: stay at 1 Short/day
    oid = add_output(db, "j1", "s1")
    add_analytics(db, oid, "2026-11-15", views=100, viewed_pct=65.0)
    ch2 = crud.edit_channel(paths, db, "after-dark", {"shorts_per_day_max": 2})
    assert cadence.plan_channel(db, ch2, NOW).shorts_per_day == 2
    steady = crud.edit_channel(paths, db, "after-dark", {"created_at": (NOW - timedelta(weeks=9)).isoformat(),
                                                         "masters_per_week": 3, "shorts_per_day_max": 3})
    plan = cadence.plan_channel(db, steady, NOW)
    assert (plan.masters_per_week, plan.shorts_per_day) == (3, 3) and plan.stage == "steady"


def test_quality_drop_by_viewed_rate_and_rejections(tmp_home: Path):
    paths, db = _env(tmp_home)
    ch = _channel(paths, db, 9, shorts_per_day_max=3)
    oid = add_output(db, "j1", "s1")
    add_analytics(db, oid, "2026-11-18", views=100, viewed_pct=40.0)
    plan = cadence.plan_channel(db, ch, NOW)
    assert plan.quality_drop and plan.shorts_per_day == 1 and any("7-day" in r for r in plan.reasons)
    db.execute("delete from analytics")
    assert not cadence.plan_channel(db, ch, NOW).quality_drop
    for i in range(10):
        add_output(db, f"r{i}", "s1", status="rejected" if i < 4 else "approved",
                   created_at=f"2026-11-{i + 1:02d}T10:00:00+00:00")
    assert cadence.rejection_rate(db, "after-dark") == (4, 10)
    plan2 = cadence.plan_channel(db, ch, NOW)
    assert plan2.quality_drop and any("rejected 4 of the last 10" in r for r in plan2.reasons)


def test_rejection_needs_enough_reviews_and_ignores_pending(tmp_home: Path):
    paths, db = _env(tmp_home)
    ch = _channel(paths, db, 9)
    for i in range(3):
        add_output(db, f"r{i}", "s1", status="rejected")
    for i in range(5):
        add_output(db, f"p{i}", "s1", status="pending")
    assert not cadence.plan_channel(db, ch, NOW).quality_drop


def test_apply_quality_drop_raises_bar_once_per_cooldown(tmp_home: Path):
    paths, db = _env(tmp_home)
    ch = _channel(paths, db, 9, critic_bar=8.0)
    plan = cadence.CadencePlan("after-dark", "steady", 1, 1, True, ["x"])
    assert cadence.apply_quality_drop(paths, db, ch, plan, NOW) == 8.5
    assert crud.get_channel(paths, "after-dark").critic_bar == 8.5
    assert cadence.apply_quality_drop(paths, db, crud.get_channel(paths, "after-dark"), plan, NOW) is None
    later = NOW + timedelta(days=8)
    assert cadence.apply_quality_drop(paths, db, crud.get_channel(paths, "after-dark"), plan, later) == 9.0
    assert cadence.apply_quality_drop(paths, db, ch, cadence.CadencePlan("a", "s", 1, 1), NOW) is None
    top = crud.edit_channel(paths, db, "after-dark", {"critic_bar": 10.0})
    assert cadence.apply_quality_drop(paths, db, top, plan, later + timedelta(days=30)) is None


def test_jobs_due_today_for_shorts_and_masters(tmp_home: Path):
    paths, db = _env(tmp_home)
    ch = _channel(paths, db, 9, format="shorts_only", shorts_per_day_max=2)
    plan = cadence.CadencePlan("after-dark", "steady", 1, 2)
    assert cadence.jobs_due_today(db, ch, plan, NOW) == 2
    add_output(db, "j1", "s1", created_at=NOW.isoformat())
    assert cadence.jobs_due_today(db, ch, plan, NOW) == 1
    add_output(db, "j2", "s1", created_at=NOW.isoformat())
    assert cadence.jobs_due_today(db, ch, plan, NOW) == 0

    master = ch.model_copy(update={"format": "master_first"})
    plan_m = cadence.CadencePlan("after-dark", "growing", 2, 1)
    assert cadence.jobs_due_today(db, master, plan_m, NOW) == 1
    add_output(db, "m1", "long", created_at=(NOW - timedelta(days=1)).isoformat())
    assert cadence.jobs_due_today(db, master, plan_m, NOW) == 0  # 2/week means one every 3 days
    assert cadence.jobs_due_today(db, master, plan_m, NOW + timedelta(days=3)) == 1


def test_plan_daily_skips_paused_channels(tmp_home: Path):
    paths, db = _env(tmp_home)
    _channel(paths, db, 9, format="shorts_only")
    crud.add_channel(paths, db, make_channel(slug="two", display_name="Two", niche="cozy mysteries",
                                             format="shorts_only"))
    crud.edit_channel(paths, db, "two", {"status": "paused"})
    plan = cadence.plan_daily(paths, db, NOW)
    assert [p.channel.slug for p in plan] == ["after-dark"]


def test_interleave_and_publish_times():
    items = [
        cadence.ReleaseItem("a/long", "a", "long"),
        cadence.ReleaseItem("a/s1", "a", "short"),
        cadence.ReleaseItem("a/s2", "a", "short"),
        cadence.ReleaseItem("b/s1", "b", "short"),
        cadence.ReleaseItem("b/s2", "b", "short"),
    ]
    order = [i.output_id for i in cadence.interleave(items)]
    assert order == ["a/long", "a/s1", "b/s1", "a/s2", "b/s2"]
    times = cadence.assign_publish_times(items, date(2026, 11, 21), "18:00-21:00", 2, random.Random(4))
    assert times["a/long"].date() == date(2026, 11, 21)
    assert times["a/s1"].date() == times["b/s1"].date() == date(2026, 11, 22)
    assert times["a/s2"].date() == times["b/s2"].date() == date(2026, 11, 23)
    for t in times.values():
        assert 18 * 60 <= t.hour * 60 + t.minute <= 21 * 60
    assert times["a/s1"] != times["b/s1"]


def test_publish_times_jitter_is_bounded():
    items = [cadence.ReleaseItem(f"x/s{i}", f"x{i}", "short") for i in range(60)]
    for seed in range(5):
        times = cadence.assign_publish_times(items, date(2026, 11, 21), "18:00-21:00", 3, random.Random(seed))
        for t in times.values():
            assert 18 * 60 <= t.hour * 60 + t.minute <= 21 * 60


# ---------------------------------------------------------------- daily run
class OkStage:
    name = "daily_ok"
    tenant = None
    inputs = ()
    outputs = ("ok.json",)

    def run(self, ctx):
        ctx.write_json("ok.json", {"ok": True})


def test_run_daily_runs_jobs_backs_up_and_reports(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    paths, db = _env(tmp_home)
    _channel(paths, db, 9, format="shorts_only", shorts_per_day_max=2)
    monkeypatch.setitem(REGISTRY, "daily_ok", OkStage())
    monkeypatch.setitem(orch.FORMAT_STAGES, "shorts_only", ["daily_ok"])
    env = jobs.open_env(light=True)
    report = daily.run_daily(env, now=NOW, rng=random.Random(1))
    assert len(report["jobs"]) == 2 and report["failures"] == []
    assert Path(report["backup"]).exists() and Path(report["backup"]).parent == paths.backup_dir
    nightly = paths.logs / "nightly-20261120.json"
    assert json.loads(nightly.read_text(encoding="utf-8"))["run_id"] == report["run_id"]
    row = db.one("select mode, finished_at, summary_json from runs where run_id=?", (report["run_id"],))
    assert row is not None and row["mode"] == "daily" and row["finished_at"] and row["summary_json"]
    again = daily.run_daily(env, now=NOW, rng=random.Random(2))
    assert again["jobs"] == []  # both Shorts of the day already exist


def test_run_daily_isolates_failures(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    paths, db = _env(tmp_home)
    _channel(paths, db, 9, format="shorts_only", shorts_per_day_max=2)
    env = jobs.open_env(light=True)
    calls = {"n": 0}

    def flaky(ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise NsError("NS-E304", "no story passed")
        return ["x"]

    report = daily.run_daily(env, now=NOW, rng=random.Random(1), run_fn=flaky)
    assert [f["code"] for f in report["failures"]] == ["NS-E304"] and len(report["jobs"]) == 1


def test_run_daily_raises_critic_bar_on_quality_drop(tmp_home: Path):
    paths, db = _env(tmp_home)
    _channel(paths, db, 9, format="shorts_only", critic_bar=8.0)
    oid = add_output(db, "old", "s1", created_at="2026-11-01T10:00:00+00:00")
    add_analytics(db, oid, "2026-11-19", views=100, viewed_pct=30.0)
    env = jobs.open_env(light=True)
    report = daily.run_daily(env, now=NOW, rng=random.Random(1), run_fn=lambda ctx: [])
    assert report["channels"][0]["critic_bar"] == 8.5
    saved = yaml.safe_load(paths.channel_file("after-dark").read_text(encoding="utf-8"))
    assert saved["critic_bar"] == 8.5


def _finished_job(paths: Paths, job: str, story: str, shorts: int, *, master: bool):
    """A finished job laid out like the real Outbox (by story, no job id in the path) plus its manifest."""
    from ns_common.models import PolicyAdvice, Seo
    from ns_studio.media.outbox import build_metadata, write_sheet_txt

    ch = make_channel()
    seo = Seo(title=story, description="A hook.
Original fiction.", tags=[f"t{i}" for i in range(10)],
              hashtags=["#Shorts", "#horror", "#story"], keywords=[])
    root = paths.outbox / ch.slug / "stories" / story
    manifest: dict[str, dict[str, str]] = {}
    items = ([("long", "long", None)] if master else []) + [(f"s{k}", "short", k) for k in range(1, shorts + 1)]
    for item, kind, k in items:
        folder = root / ("full" if kind == "long" else "shorts")
        folder.mkdir(parents=True, exist_ok=True)
        name = story if kind == "long" else f"{story} - Short {k:02d}"
        meta = build_metadata(
            channel=ch, job_id=job, kind=kind, k=k, seo=seo, advice=PolicyAdvice(), shots=[],
            combo_id="c", prompt_versions={}, publish_at="2026-11-21T18:00",
            extra={"related_video_instruction": f"Set Related video -> {story}"} if kind == "short" else None,
        )
        mp4 = folder / f"{name}.mp4"
        mp4.write_bytes(b"x")
        mp4.with_suffix(".json").write_text(meta.model_dump_json(indent=2), encoding="utf-8")
        write_sheet_txt(meta, mp4.name, mp4.with_suffix(".txt"),
                        related_title=story if kind == "short" else None)
        manifest[item] = {"mp4": str(mp4), "json": str(mp4.with_suffix(".json")),
                          "txt": str(mp4.with_suffix(".txt"))}
    job_dir = paths.job_dir(job)
    (job_dir / "outbox").mkdir(parents=True, exist_ok=True)
    (job_dir / "outbox" / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return manifest


def test_apply_publish_times_reads_the_job_manifest(tmp_home: Path):
    """Review row 6: the old date/job glob matched nothing in the story-organised Outbox."""
    paths, db = _env(tmp_home)
    _channel(paths, db, 9)
    env = jobs.open_env(light=True)
    job = "after-dark-20261120-abcd"
    manifest = _finished_job(paths, job, "The Long Night", 2, master=True)
    assert daily.apply_publish_times(env, [job], random.Random(3), NOW) == 3
    whens = {i: json.loads(Path(e["json"]).read_text(encoding="utf-8"))["publish_after"]
             for i, e in manifest.items()}
    assert whens["long"].startswith("2026-11-20T")  # the master goes first
    assert whens["s1"].startswith("2026-11-21T") and whens["s2"].startswith("2026-11-21T")
    # the .txt sheet carries the same time as the .json
    for i, e in manifest.items():
        txt = Path(e["txt"]).read_text(encoding="utf-8")
        assert f"PUBLISH AFTER (local time): {whens[i].replace('T', ' ')}" in txt
    assert "Related video: set Related video -> The Long Night" in Path(manifest["s1"]["txt"]).read_text(
        encoding="utf-8"
    )


def test_shorts_only_jobs_of_one_night_get_different_slots(tmp_home: Path):
    """Two one-Short jobs of a 2/day channel used to both land at 18:00 +-45 min the next evening."""
    paths, db = _env(tmp_home)
    _channel(paths, db, 9)
    env = jobs.open_env(light=True)
    jobs_ = ["after-dark-20261120-aaaa", "after-dark-20261120-bbbb"]
    mans = [_finished_job(paths, j, f"Story {n}", 1, master=False) for n, j in enumerate(jobs_)]
    assert daily.apply_publish_times(env, jobs_, random.Random(5), NOW) == 2
    times = sorted(
        datetime.fromisoformat(json.loads(Path(m["s1"]["json"]).read_text(encoding="utf-8"))["publish_after"])
        for m in mans
    )
    assert times[0].date() == times[1].date() == date(2026, 11, 21)  # two a day, from tomorrow on
