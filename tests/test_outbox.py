import json
from datetime import datetime
from pathlib import Path

import jsonschema
import pytest

from ns_common.models import Metadata, PolicyAdvice, Seo
from ns_common.paths import contracts_dir
from ns_studio.media.outbox import (
    OutboxStage,
    build_metadata,
    job_date,
    outbox_target,
    publish_after,
    rejected_path,
    safe_slug,
    sheet_text,
    short_day_offset,
    write_sheet_txt,
)
from ns_studio.media.shotplan import Shot
from tests.helpers import make_channel, make_ctx

SEO = {
    "title": "Scary Story: The Doorbell Camera",
    "description": "A camera records someone at 3:12.\nOriginal fiction.",
    "tags": [f"tag{i}" for i in range(10)],
    "hashtags": ["#Shorts", "#horror", "#scarystory"],
    "keywords": ["scary story"],
}


def _shots() -> list[Shot]:
    return [
        Shot(0, 0.0, 2.0, "fake", "a-1", "x.mp4", 0.0, 1080, 1920, "fake://a/1", "fake", "dark hallway"),
        Shot(0, 2.0, 4.0, "fake", "b-2", "y.mp4", 0.0, 1080, 1920, "fake://b/2", "fake", "rain"),
        Shot(1, 4.0, 6.0, "fake", "a-1", "x.mp4", 0.0, 1080, 1920, "fake://a/1", "fake", "dark hallway"),
    ]


def _schema() -> dict:
    return json.loads((contracts_dir() / "schemas" / "metadata.schema.json").read_text(encoding="utf-8"))


def test_job_date_parses_job_id():
    assert job_date("after-dark-20261101-ab12") == datetime(2026, 11, 1)
    with pytest.raises(ValueError):
        job_date("no-date-here")


def test_publish_after_is_within_45_minutes_of_window_start_and_seeded():
    ch = make_channel(publish_window="18:00-21:00")
    jid = "after-dark-20261101-ab12"
    seen = set()
    for key in ("s1", "s2", "s3", "s4", "s5", "s6", "long"):
        when = datetime.fromisoformat(publish_after(ch, jid, key, day_offset=1))
        base = datetime(2026, 11, 2, 18, 0)
        assert abs((when - base).total_seconds()) <= 45 * 60
        seen.add(when)
    assert len(seen) > 1  # not a constant
    assert publish_after(ch, jid, "s1") == publish_after(ch, jid, "s1")
    assert publish_after(ch, jid, "s1", day_offset=3).startswith("2026-11-04")


def test_short_day_offsets_spread_shorts_over_days():
    ch = make_channel(shorts_per_day_max=2)
    assert [short_day_offset(ch, k) for k in range(1, 6)] == [1, 1, 2, 2, 3]


def test_build_metadata_validates_against_schema_and_dedupes_clips():
    meta = build_metadata(
        channel=make_channel(), job_id="after-dark-20261101-ab12", kind="short", k=1, seo=Seo(**SEO),
        advice=PolicyAdvice(), shots=_shots(), combo_id="c-test", prompt_versions={"seo": 2},
        publish_at="2026-11-02T18:05",
    )
    assert [c.id for c in meta.clips] == ["a-1", "b-2"]
    assert meta.ai_disclosure == "no" and meta.made_for_kids is False and meta.k == 1
    jsonschema.validate(json.loads(meta.model_dump_json()), _schema())


def test_sheet_text_has_everything_to_paste():
    meta = build_metadata(
        channel=make_channel(), job_id="after-dark-20261101-ab12", kind="short", k=1, seo=Seo(**SEO),
        advice=PolicyAdvice(ai_disclosure="yes", age_restrict_suggested=True), shots=_shots(),
        combo_id="c", prompt_versions={}, publish_at="2026-11-02T18:05",
    )
    txt = sheet_text(meta, "s1.mp4", related_title="The Full Story")
    for needle in (
        "FILE: s1.mp4", "TITLE: Scary Story: The Doorbell Camera", "Original fiction.", "#Shorts #horror",
        "TAGS: tag0, tag1", "PUBLISH AFTER (local time): 2026-11-02 18:05",
        "Altered or synthetic content: Yes", "Made for kids: No", "Age restriction suggested: Yes",
        "Related video: set Related video -> The Full Story",
    ):
        assert needle in txt
    assert "Related video" not in sheet_text(meta, "s1.mp4")


def test_write_sheet_txt(tmp_path: Path):
    meta = build_metadata(
        channel=make_channel(), job_id="after-dark-20261101-ab12", kind="short", k=1, seo=Seo(**SEO),
        advice=PolicyAdvice(), shots=_shots(), combo_id="c", prompt_versions={},
        publish_at="2026-11-02T18:05",
    )
    p = write_sheet_txt(meta, "s1.mp4", tmp_path / "s1.txt")
    assert p.read_text(encoding="utf-8").startswith("FILE: s1.mp4")


# ---- stage ------------------------------------------------------------------------------------------------


def _prepare(ctx, tmp_path: Path) -> Path:
    video = tmp_path / "short.mp4"
    video.write_bytes(b"\x00" * 2048)
    ctx.write_json("render/report.json", {"s1": {"path": str(video), "duration_s": 6.0}})
    ctx.write_json("qc/report.json", {"s1": {"passed": True, "reason": None}})
    ctx.write_json("seo.json", [SEO])
    shots = [s.to_json() for s in _shots()]
    ctx.write_json("clips/shots.json", {"s1": {"total_s": 6.0, "treatment": "dark_slowzoom", "shots": shots}})
    for s in _shots():
        ctx.db.execute(
            "insert or ignore into clips(source, clip_id, url, license, fetched_at) values (?,?,?,?,?)",
            (s.source, s.clip_id, s.url, s.license, "2026-11-01T00:00:00+00:00"),
        )
    return video


STORY_DIR = "Scary Story The Doorbell Camera"
SHORT_NAME = "Scary Story The Doorbell Camera - Short 01"


def _short_dir(ctx) -> Path:
    return ctx.paths.outbox / "after-dark" / "stories" / STORY_DIR / "shorts"


def test_outbox_stage_copies_files_and_writes_rows(tmp_home: Path, tmp_path: Path):
    ctx = make_ctx(tmp_home)
    ctx.prompt_versions = {"seo": 1}
    _prepare(ctx, tmp_path)
    OutboxStage().run(ctx)
    d = _short_dir(ctx)
    mp4 = d / f"{SHORT_NAME}.mp4"
    assert mp4.stat().st_size == 2048
    meta = json.loads((d / f"{SHORT_NAME}.json").read_text(encoding="utf-8"))
    jsonschema.validate(meta, _schema())
    assert Metadata.model_validate(meta).job_id == ctx.job_id and meta["prompt_versions"] == {"seo": 1}
    assert (d / f"{SHORT_NAME}.txt").read_text(encoding="utf-8").startswith(f"FILE: {SHORT_NAME}.mp4")
    out = ctx.db.one("select * from outputs where output_id=?", (f"{ctx.job_id}/s1",))
    assert out["kind"] == "short" and out["k"] == 1 and out["status"] == "pending"
    assert out["path"] == str(mp4) and out["duration_s"] == 6.0 and out["combo_id"] == "c-test"
    usage = ctx.db.query(
        "select source, clip_id, item from clip_usage where job_id=? order by clip_id", (ctx.job_id,)
    )
    assert [(u["clip_id"], u["item"]) for u in usage] == [("a-1", "s1"), ("b-2", "s1")]
    assert ctx.read_json("outbox/manifest.json")["s1"]["mp4"] == str(mp4)


def test_outbox_stage_is_idempotent_and_keeps_review_status(tmp_home: Path, tmp_path: Path):
    ctx = make_ctx(tmp_home)
    _prepare(ctx, tmp_path)
    OutboxStage().run(ctx)
    ctx.db.execute("update outputs set status='approved' where job_id=?", (ctx.job_id,))
    OutboxStage().run(ctx)
    assert ctx.db.one("select count(*) c from outputs where job_id=?", (ctx.job_id,))["c"] == 1
    assert ctx.db.one("select status from outputs where job_id=?", (ctx.job_id,))["status"] == "approved"
    assert ctx.db.one("select count(*) c from clip_usage where job_id=?", (ctx.job_id,))["c"] == 2
    assert [p.name for p in (_short_dir(ctx).parent.parent).iterdir()] == [STORY_DIR]  # no -2 folder


def test_outbox_uses_policy_advice_when_present(tmp_home: Path, tmp_path: Path):
    ctx = make_ctx(tmp_home)
    _prepare(ctx, tmp_path)
    ctx.write_json("policy_final.json", {
        "stage": "final", "passed": True, "checks": {},
        "advice": {"ai_disclosure": "yes", "made_for_kids": False, "age_restrict_suggested": True},
    })
    OutboxStage().run(ctx)
    meta = json.loads((_short_dir(ctx) / f"{SHORT_NAME}.json").read_text(encoding="utf-8"))
    assert meta["ai_disclosure"] == "yes" and meta["age_restrict_suggested"] is True


# ---- spec 15.2a path shapes ------------------------------------------------------------------------


def test_safe_slug_is_windows_safe():
    assert safe_slug("Scary Story: The Doorbell Camera") == "Scary Story The Doorbell Camera"
    assert safe_slug('A: "Title"/with|bad?*<>') == "A Title with bad"
    assert safe_slug("   ") == "" and safe_slug("???") == ""
    assert safe_slug("CON") == "CON_" and safe_slug("com1") == "com1_"
    assert safe_slug("Trailing dots...  ") == "Trailing dots"
    assert len(safe_slug("x" * 200)) == 80
    long = safe_slug("word " * 40)
    assert len(long) <= 80 and not long.endswith(" ")
    assert safe_slug("tab\tand\nnewline") == "tab and newline"


def test_outbox_target_story_shapes_and_collision_suffix(tmp_home: Path):
    a = make_ctx(tmp_home)
    a.write_json("seo.json", [SEO])
    full = outbox_target(a, "long")
    base = a.paths.outbox / "after-dark" / "stories" / STORY_DIR
    assert full == base / "full" / "Scary Story The Doorbell Camera.mp4" and full.parent.is_dir()
    assert outbox_target(a, "short", 2) == base / "shorts" / "Scary Story The Doorbell Camera - Short 02.mp4"
    assert outbox_target(a, "long") == full  # same job: same folder
    b = make_ctx(tmp_home)  # another job with the same title
    b.write_json("seo.json", [SEO])
    second = outbox_target(b, "short", 1)
    assert second.parent.parent.name == STORY_DIR + "-2"
    c = make_ctx(tmp_home)
    c.write_json("seo.json", [SEO])
    assert outbox_target(c, "long").parent.parent.name == STORY_DIR + "-3"
    assert outbox_target(b, "long").parent.parent.name == STORY_DIR + "-2"


def test_outbox_target_prefers_master_title_and_series_layout(tmp_home: Path):
    ctx = make_ctx(tmp_home, fmt="master_first")
    ctx.write_json("seo_long.json", {"title": "The Long Night"})
    ctx.write_json("seo.json", [SEO])
    assert outbox_target(ctx, "long").parent.parent.name == "The Long Night"
    s = make_ctx(tmp_home, fmt="master_first")
    s.write_json(
        "series_context.json", {"series_slug": "Night Bus", "part_n": 3, "series_title": "The Night Bus"}
    )
    root = s.paths.outbox / "after-dark" / "series" / "Night Bus" / "part-03"
    assert outbox_target(s, "long") == root / "full" / "The Night Bus - Part 03.mp4"
    assert outbox_target(s, "short", 1) == root / "shorts" / "The Night Bus - Part 03 - Short 01.mp4"


def test_rejected_path_mirrors_the_outbox_tree(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("seo.json", [SEO])
    mp4 = outbox_target(ctx, "short", 1)
    rej = rejected_path(ctx.paths, mp4)
    assert rej == ctx.paths.rejected / "after-dark" / "stories" / STORY_DIR / "shorts" / mp4.name


def test_outbox_paths_stay_under_200_characters(tmp_home: Path):
    """Review row 15: an 80-character title was in every path twice; with a longish home it passed 260."""
    from ns_studio.media.outbox import MAX_PATH, MIN_STEM

    long_title = "The Night Bus That Never Stopped At Harrow Street And What The Driver Said To Me At 3 AM"
    ctx = make_ctx(tmp_home)
    ctx.write_json("seo.json", [{**SEO, "title": long_title}])
    ctx.write_json("seo_long.json", {"title": long_title})
    for kind, k in (("long", None), ("short", 1)):
        target = outbox_target(ctx, kind, k)
        longest = target.with_suffix(".json")
        assert len(str(longest)) <= max(MAX_PATH, len(str(ctx.paths.outbox)) + 80), str(longest)
        assert len(target.parent.parent.name) >= MIN_STEM
    # the series layout has the slug and the title in the path; both are cut
    s = make_ctx(tmp_home, fmt="master_first")
    s.write_json(
        "series_context.json",
        {"series_slug": "x" * 60, "part_n": 3, "series_title": "The Very Long Series Title " * 4},
    )
    sheet = outbox_target(s, "short", 2).with_suffix(".json")
    assert len(str(sheet)) <= max(MAX_PATH, len(str(s.paths.outbox)) + 120), str(sheet)
