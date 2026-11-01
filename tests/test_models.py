import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ns_common import models as m
from ns_common.paths import contracts_dir


def test_channel_defaults_and_validation():
    ch = m.Channel(slug="after-dark", display_name="After Dark", niche="horror", language="en")
    assert ch.status == "active"
    assert ch.format == "master_first"
    assert ch.long_minutes == (10, 14)
    assert ch.pools.hooks  # defaults non-empty
    with pytest.raises(ValidationError):
        m.Channel(slug="Bad Slug!", display_name="x", niche="y", language="en")


def test_job_id_pattern():
    md = m.Metadata(
        channel="after-dark", job_id="after-dark-20261101-ab12", kind="short", k=1,
        title="t", description="d", tags=["a"], hashtags=["#Shorts"], publish_after="2026-11-02T18:00:00",
        ai_disclosure="no", made_for_kids=False, age_restrict_suggested=False, clips=[], combo_id="c1",
        prompt_versions={"hook": 1},
    )
    assert md.kind == "short"
    with pytest.raises(ValidationError):
        md.model_copy(update={"job_id": "nope"}).model_validate(md.model_dump() | {"job_id": "nope"})


def test_runtime_default_profiles():
    rt = m.Runtime.default("cpu-dev")
    assert rt.video.encoder == "libx264"
    assert rt.llama.port == 8080 and rt.embed.port == 8081 and rt.tts.port == 8091
    gpu = m.Runtime.default("gpu")
    assert gpu.video.encoder == "h264_nvenc"
    assert gpu.llama.flags.ctx == 16384


def test_every_schema_file_matches_models():
    out = contracts_dir() / "schemas"
    for name, model in m.SCHEMA_MODELS.items():
        f = out / f"{name}.schema.json"
        assert f.exists(), f"missing {f}; run python -m ns_common.models"
        assert json.loads(f.read_text(encoding="utf-8")) == model.model_json_schema(), name


def test_export_writes_all(tmp_path: Path):
    files = m.export_schemas(tmp_path)
    assert {f.stem.replace(".schema", "") for f in files} == set(m.SCHEMA_MODELS)


def test_answers_installer_fields():
    ans = m.Answers(channel=m.AnswersChannel(display_name="x", niche="y"))
    assert ans.llm_tier == "small"
    assert ans.home_path is None and ans.models_source is None and ans.restore_from is None
    with pytest.raises(ValidationError):
        m.Answers.model_validate({"channel": {"display_name": "x", "niche": "y"}, "llm_tier": "huge"})


def test_llama_cfg_story_flags_and_build():
    cfg = m.LlamaCfg()
    assert cfg.story_flags is None and cfg.build is None
    cfg = m.LlamaCfg(story_flags=m.LlamaFlags(ctx=32768), build="cuda13")
    assert cfg.story_flags is not None and cfg.story_flags.ctx == 32768
    with pytest.raises(ValidationError):
        m.LlamaCfg.model_validate({"build": "cpu"})


def test_metadata_accepts_clip_kind():
    md = m.Metadata(
        channel="after-dark", job_id="after-dark-20261101-ab12", kind="clip", k=2,
        title="t", description="d", tags=["a"], hashtags=["#Shorts"], publish_after="2026-11-02T18:00:00",
        ai_disclosure="no", made_for_kids=False, age_restrict_suggested=False, clips=[], combo_id="",
        prompt_versions={}, source_video="talk.mp4", template="podcast_stacked",
    )
    assert md.kind == "clip" and md.source_video == "talk.mp4" and md.template == "podcast_stacked"
    base = md.model_dump() | {"kind": "short", "source_video": None, "template": None}
    assert m.Metadata.model_validate(base).source_video is None


def _chapter(n: int) -> dict:
    return {"n": n, "title": f"c{n}", "beats": ["b"], "cliff": "x", "word_budget": 500}


def test_plan_needs_five_to_seven_chapters():
    five = [_chapter(i) for i in range(1, 6)]
    ok = m.Plan.model_validate({"target_minutes": 10, "target_words": 1550, "chapters": five})
    assert len(ok.chapters) == 5
    with pytest.raises(ValidationError):
        m.Plan.model_validate({"target_minutes": 10, "target_words": 1550, "chapters": five[:4]})
    with pytest.raises(ValidationError):
        m.Plan.model_validate({"target_minutes": 8, "target_words": 1550, "chapters": five})


def test_story_and_seo_long_and_shorts_plan():
    story = m.Story.model_validate({
        "working_title": "t",
        "chapters": [{
            "n": 1, "title": "c", "text": "One. Two.", "summary": "s", "word_count": 2, "est_seconds": 1.0,
            "sentences": [{"idx": 0, "text": "One."}, {"idx": 1, "text": "Two."}],
        }],
    })
    assert story.revisions == 0 and story.chapters[0].cliff_line_idx is None
    seo = m.SeoLong.model_validate({
        "title": "t", "description": "d", "tags": ["a"], "hashtags": [],
        "chapter_titles": [{"n": 1, "title": "c"}],
    })
    assert seo.keywords == []
    item = {
        "k": 1, "chapter": 1, "sentence_start": 0, "sentence_end": 3, "hooks": ["h"], "chosen_hook": "h",
        "closing_line": "c", "length_bucket": 35, "combo_id": "x", "why_standalone": "w",
    }
    plan = m.ShortsPlan.model_validate({"shorts": [item]})
    assert plan.shorts[0].start_s is None
    with pytest.raises(ValidationError):
        m.ShortsPlanItem.model_validate(item | {"hooks": ["a"] * 6})


def test_clip_plan_item_and_policy_check():
    item = m.ClipPlanItem(k=1, start_s=1.0, end_s=31.0, title="t", score=0.5)
    assert item.status == "kept" and item.layout is None and item.caption_style is None
    assert item.duration_s == 30.0
    with pytest.raises(ValidationError):
        m.ClipPlanItem(k=1, start_s=10.0, end_s=10.0, title="t", score=0.5)
    with pytest.raises(ValidationError):
        m.ClipPlanItem(k=1, start_s=1.0, end_s=5.0, title="t", score=1.5)
    assert m.ClipPlan.model_validate({"clips": [item.model_dump()]}).clips[0].k == 1
    res = m.PolicyCheckResult(
        unsafe_categories=[], hook_honest=True, title_honest=True, standalone=None, notes=""
    )
    assert res.standalone is None
    with pytest.raises(ValidationError):
        m.PolicyCheckResult.model_validate({"unsafe_categories": [], "hook_honest": True})


def test_transcript_and_clip_layout():
    tr = m.Transcript.model_validate({
        "source": "a.mp4", "duration_s": 5.0,
        "segments": [{"i": 0, "start": 0.0, "end": 2.0, "text": "hi there"}],
        "words": [{"w": "hi", "start": 0.0, "end": 0.5, "seg": 0}],
    })
    assert tr.language == "en"
    backwards = [{"i": 0, "start": 3.0, "end": 2.0, "text": "x"}]
    with pytest.raises(ValidationError):
        m.Transcript.model_validate(tr.model_dump() | {"segments": backwards})
    lay = m.ClipLayout(
        template="podcast_stacked",
        crops=[m.Crop(
            t_start=0, t_end=5, region=m.Rect(x=0, y=0, w=10, h=10), region_b=m.Rect(x=10, y=0, w=10, h=10),
            speaking=0,
        )],
    )
    assert lay.facecam is None and lay.crops[0].region_b is not None
    assert m.Rect(x=1, y=1, w=2, h=2).inside(m.Rect(x=0, y=0, w=10, h=10))


def test_format_preset_model():
    fmt = m.Format.model_validate({
        "id": "short_9x16", "name": "Short 9:16", "kind": "short",
        "canvas": {"w": 1080, "h": 1920},
        "duration": {"min_s": 20, "max_s": 60, "buckets": [35, 50]},
        "captions": {"styles_allowed": ["punch_yellow"]},
        "layout": {"regions": [{"name": "main", "w": 1080, "h": 1920, "source": "full"}]},
    })
    assert fmt.canvas.fps == 30 and fmt.safe_margins.top == 0 and fmt.shots is None
    bad = {
        "id": "x", "name": "x", "kind": "short", "canvas": {"w": 1, "h": 1},
        "duration": {"min_s": 1, "max_s": 2}, "captions": {},
        "layout": {"regions": [{"name": "m", "w": 1, "h": 1, "source": "nope"}]},
    }
    with pytest.raises(ValidationError):
        m.Format.model_validate(bad)


def test_new_schema_names_are_registered():
    for name in (
        "plan", "story", "shorts_plan", "seo_long", "clip_plan", "policy_check", "transcript",
        "clip_layout", "clip_template", "format",
    ):
        assert name in m.SCHEMA_MODELS
