import json
import re
from pathlib import Path

import pytest

from ns_common.errors import NsError
from ns_common.models import Idea
from ns_studio.context import JobContext
from ns_studio.series import stages as SS
from ns_studio.series.importer import build_artifacts
from ns_studio.series.store import ScriptChapter, ScriptDoc, ShortDoc, StoryDir, parse_short
from ns_studio.stages import REGISTRY, run_stage
from ns_studio.text import stages_master as M
from ns_studio.text.models_local import Plan, ShortsPlanItem, Story
from tests.helpers import make_channel, make_ctx
from tests.test_series_workshop import Recorder, built, make_workshop


def produced(tmp_home: Path) -> tuple[StoryDir, JobContext]:
    """A written, approved, designed and produced part 1, plus a job context holding its source file."""
    rec = Recorder()
    ws = make_workshop(tmp_home, enqueue=rec)
    slug = built(ws, write=(1,))
    ws.approve(slug, 1)
    ws.design(slug)
    ws.produce(slug, 1)
    req = rec.requests[0]
    src = json.loads((ws.paths.job_dir(req.job_id) / "script_source.json").read_text(encoding="utf-8"))
    ctx = make_ctx(tmp_home, fmt="master_first", channel=make_channel(format="master_first"))
    ctx.write_json("script_source.json", src)
    return StoryDir.open(ws.paths, slug), ctx


def shorts_plan(ctx: JobContext) -> list[ShortsPlanItem]:
    return [ShortsPlanItem.model_validate(i) for i in ctx.read_json("shorts_plan.json")]


def test_stage_is_registered_without_a_gpu_tenant():
    st = REGISTRY["script_import"]
    assert isinstance(st, SS.ScriptImportStage) and st.tenant is None
    assert st.inputs == ("script_source.json",)
    assert "story.json" in st.outputs and "shorts_plan.json" in st.outputs


def test_import_writes_the_artifacts_of_the_master_stages(tmp_home: Path):
    sd, ctx = produced(tmp_home)
    REGISTRY["script_import"].run(ctx)
    script = sd.read_script(1)
    story = Story.model_validate(ctx.read_json("story.json"))
    assert story.working_title == "Wet Footprints" and story.revisions == 0
    assert [(c.n, c.title) for c in story.chapters] == [(c.n, c.title) for c in script.chapters]
    for ch, sc in zip(story.chapters, script.chapters, strict=True):
        assert ch.text == sc.text and ch.word_count > 0 and ch.summary == sc.summary
        assert [s.idx for s in ch.sentences] == list(range(len(ch.sentences)))
    assert [c.cliff_line_idx is None for c in story.chapters] == [False] * 4 + [True]

    plan = Plan.model_validate(ctx.read_json("plan.json"))
    assert [c.title for c in plan.chapters] == [c.title for c in script.chapters]
    assert plan.chapters[0].cliff.startswith("The footprints cross the porch")
    meta = ctx.read_json("plan_meta.json")
    assert meta == {"working_title": "Wet Footprints", "wpm": 155, "min_words": 1395}
    idea = Idea.model_validate(ctx.read_json("idea.json"))
    assert "suburban street" in idea.setting and idea.premise.startswith("A night nurse's doorbell camera")
    assert idea.protagonist.startswith("Mara Quill")

    sql = "select working_title, word_count, opening_line from stories where job_id=?"
    row = ctx.db.one(sql, (ctx.job_id,))
    assert row is not None and row["working_title"] == "Wet Footprints"
    assert row["word_count"] == sum(c.word_count for c in story.chapters)
    assert row["opening_line"] == "The doorbell camera pings at 3:12 every night."


def test_shorts_plan_matches_the_shorts_files(tmp_home: Path):
    sd, ctx = produced(tmp_home)
    REGISTRY["script_import"].run(ctx)
    story = Story.model_validate(ctx.read_json("story.json"))
    flat = [t for _, _, t in sd.read_script(1).sentences()]
    files = sd.read_shorts(1)
    plan = shorts_plan(ctx)
    assert len(files) == len(plan) >= 4 and [p.k for p in plan] == [f.k for f in files]
    for doc, item in zip(files, plan, strict=True):
        assert item.chosen_hook == doc.hook and item.hooks[0] == doc.hook
        assert item.closing_line == doc.closing_line
        assert item.length_bucket == doc.length_bucket and item.chapter == doc.chapter
        assert item.combo_id == ctx.combo.combo_id and item.start_s is None and item.end_s is None
        assert M.span_text(story, item.chapter, item.sentence_start, item.sentence_end) == " ".join(
            flat[doc.span[0] - 1 : doc.span[1]]
        )
        assert len(item.chosen_hook.split()) <= 12 and len(item.closing_line.split()) <= 15
    assert len({p.chosen_hook for p in plan}) == len(plan)


def test_stage_is_idempotent_through_run_stage(tmp_home: Path):
    _sd, ctx = produced(tmp_home)
    assert run_stage(ctx, REGISTRY["script_import"]) is True
    assert run_stage(ctx, REGISTRY["script_import"]) is False
    assert run_stage(ctx, REGISTRY["script_import"], force=True) is True


def test_downstream_master_stages_accept_the_imported_artifacts(tmp_home: Path):
    sd, ctx = produced(tmp_home)
    REGISTRY["script_import"].run(ctx)
    for name in ("visual_plan_long", "seo_long", "seo_shorts"):
        REGISTRY[name].run(ctx)
    assert len(ctx.read_json("seo_shorts.json")) == len(sd.read_shorts(1))
    assert ctx.read_json("visual_plan.json") and ctx.read_json("seo_long.json")["title"]


def edit_script(sd: StoryDir, old: str, new: str) -> None:
    raw = sd.script(1).read_text(encoding="utf-8")
    assert old in raw
    sd.script(1).write_text(raw.replace(old, new, 1), encoding="utf-8")


def test_hand_edited_text_is_used_and_later_shorts_follow_their_sentences(tmp_home: Path):
    sd, ctx = produced(tmp_home)
    REGISTRY["script_import"].run(ctx)
    before = shorts_plan(ctx)
    n_before = len(Story.model_validate(ctx.read_json("story.json")).chapters[2].sentences)
    edit_script(sd, "\n\n[BREAK]\n\n## 4.", "\n\nMy own added sentence.\n\n[BREAK]\n\n## 4.")
    run_stage(ctx, REGISTRY["script_import"], force=True)
    story = Story.model_validate(ctx.read_json("story.json"))
    assert story.chapters[2].text.endswith("My own added sentence.")
    assert len(story.chapters[2].sentences) == n_before + 1
    after = shorts_plan(ctx)
    # every Short still sits on the same sentences of its chapter although the global numbers moved
    assert [(a.chapter, a.sentence_start, a.sentence_end) for a in after] == [
        (b.chapter, b.sentence_start, b.sentence_end) for b in before
    ]


def test_a_sentence_added_before_a_short_shifts_only_that_chapter(tmp_home: Path):
    sd, ctx = produced(tmp_home)
    REGISTRY["script_import"].run(ctx)
    before = shorts_plan(ctx)
    edit_script(sd, "## 1. Wet Footprints\n\n", "## 1. Wet Footprints\n\nA brand new opening line.\n\n")
    run_stage(ctx, REGISTRY["script_import"], force=True)
    after = shorts_plan(ctx)
    for b, a in zip(before, after, strict=True):
        shift = 1 if b.chapter == 1 else 0
        got = (a.chapter, a.sentence_start, a.sentence_end)
        assert got == (b.chapter, b.sentence_start + shift, b.sentence_end + shift)
    assert any(b.chapter == 1 for b in before)


def rewrite_short(sd: StoryDir, k: int, fn) -> ShortDoc:
    path = sd.short_file(1, k)
    doc = parse_short(path.read_text(encoding="utf-8"))
    path.write_text(fn(path.read_text(encoding="utf-8"), doc), encoding="utf-8")
    return doc


def test_short_spans_may_not_cross_a_chapter(tmp_home: Path):
    sd, ctx = produced(tmp_home)
    def widen(t: str, d: ShortDoc) -> str:
        return t.replace(f"span: [{d.span[0]}, {d.span[1]}]", f"span: [{d.span[0]}, 400]")

    doc = rewrite_short(sd, 1, widen)
    assert doc.chapter == 1
    with pytest.raises(NsError) as err:
        REGISTRY["script_import"].run(ctx)
    assert err.value.code == "NS-E303" and "crosses a chapter break" in err.value.detail


def test_short_hooks_must_be_short_and_distinct(tmp_home: Path):
    sd, ctx = produced(tmp_home)
    long_hook = " ".join(["word"] * 13)
    rewrite_short(sd, 2, lambda t, d: re.sub(r"(?m)^hook: .*$", f"hook: {long_hook}", t))
    with pytest.raises(NsError) as err:
        REGISTRY["script_import"].run(ctx)
    assert err.value.code == "NS-E303" and "s02: hook must be 1-12 words" in err.value.detail

    first = parse_short(sd.short_file(1, 1).read_text(encoding="utf-8"))
    rewrite_short(sd, 2, lambda t, d: re.sub(r"(?m)^hook: .*$", f"hook: {first.hook}", t))
    with pytest.raises(NsError) as err2:
        REGISTRY["script_import"].run(ctx)
    assert err2.value.code == "NS-E303" and "duplicate" in err2.value.detail


def test_missing_files_are_reported(tmp_home: Path):
    sd, ctx = produced(tmp_home)
    sd.short_file(1, 1).unlink()
    # the source lists the file that is gone now
    with pytest.raises(NsError, match="short file not found"):
        REGISTRY["script_import"].run(ctx)
    sd.script(1).unlink()
    with pytest.raises(NsError, match="script not found"):
        REGISTRY["script_import"].run(ctx)


def test_source_without_a_concept_still_imports(tmp_home: Path):
    _sd, ctx = produced(tmp_home)
    src = ctx.read_json("script_source.json")
    src.pop("concept")
    ctx.write_json("script_source.json", src)
    REGISTRY["script_import"].run(ctx)
    idea = Idea.model_validate(ctx.read_json("idea.json"))
    assert idea.setting == "an everyday place" and idea.protagonist == "the narrator"


def chapters(n: int) -> list[ScriptChapter]:
    return [ScriptChapter(n=i, title=f"Chapter title {i}", text=f"Sentence one of {i}. Sentence two of {i}.")
            for i in range(1, n + 1)]  # fmt: skip


def test_build_artifacts_models_long_parts_beyond_the_master_plan_limit():
    art5 = build_artifacts(ScriptDoc(part=1, title="T", chapters=chapters(5)), [], wpm=155, combo_id="c")
    assert Plan.model_validate(art5.plan).target_minutes == 9
    art9 = build_artifacts(ScriptDoc(part=1, title="T", chapters=chapters(9)), [], wpm=155, combo_id="c")
    assert len(art9.plan["chapters"]) == 9 and art9.shorts_plan == []
    assert [c.cliff_line_idx is None for c in art9.story.chapters] == [False] * 8 + [True]
    assert art9.plan_meta["min_words"] == 1395


def test_build_artifacts_checks_shorts_against_the_script():
    script = ScriptDoc(part=1, title="T", chapters=chapters(5))
    ok = ShortDoc(k=2, hook="Second one here.", span=[3, 4], closing_line="More on the channel.",
                  length_bucket=35, start_text="Sentence one of 2")  # fmt: skip
    art = build_artifacts(script, [ok], wpm=155, combo_id="c")
    item = art.shorts_plan[0]
    assert (item["k"], item["chapter"], item["sentence_start"], item["sentence_end"]) == (2, 2, 0, 1)
    assert item["hooks"] == ["Second one here."] and item["combo_id"] == "c"
    bad = ok.model_copy(update={"span": [2, 3], "start_text": ""})
    with pytest.raises(NsError, match="crosses a chapter break"):
        build_artifacts(script, [bad], wpm=155, combo_id="c")
    with pytest.raises(NsError, match="ascending"):
        build_artifacts(script, [ok.model_copy(update={"span": [5, 4]})], wpm=155, combo_id="c")
    with pytest.raises(NsError, match="duplicate"):
        build_artifacts(script, [ok, ok.model_copy(update={"k": 3})], wpm=155, combo_id="c")


def test_orchestrator_plans_master_from_script_when_a_source_file_exists(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio.orchestrator import FORMAT_STAGES, MASTER_FIRST_STAGES, MASTER_FROM_SCRIPT_STAGES, run_job

    assert MASTER_FROM_SCRIPT_STAGES[0] == "script_import" and "ideas" not in MASTER_FROM_SCRIPT_STAGES
    assert set(MASTER_FROM_SCRIPT_STAGES) - {"script_import"} <= set(MASTER_FIRST_STAGES)
    monkeypatch.setitem(FORMAT_STAGES, "master_from_script", ["script_import", "visual_plan_long"])
    monkeypatch.setitem(FORMAT_STAGES, "master_first", ["ideas", "long_plan"])
    ctx = make_ctx(tmp_home, fmt="master_first", channel=make_channel(format="master_first"))
    assert run_job(ctx, dry_run=True) == ["ideas", "long_plan"]
    ctx.write_json("script_source.json", {})
    assert run_job(ctx, dry_run=True) == ["script_import", "visual_plan_long"]
