# ruff: noqa: E501
import json
from pathlib import Path

import pytest

import ns_studio.text.stages_shorts  # noqa: F401
from ns_common.errors import NsError
from ns_common.llm_client import FakeLlm
from ns_common.models import (
    LlamaCfg,
    LlamaFlags,
    Plan,
    Runtime,
    Seo,
    SeoLong,
    ShortsPlanItem,
    Story,
    VisualBeat,
)
from ns_common.textstats import split_sentences, word_count
from ns_studio.context import FIXTURES_LLM
from ns_studio.orchestrator import FORMAT_STAGES, MASTER_FIRST_STAGES, run_job
from ns_studio.stages import REGISTRY
from ns_studio.text import stages_master as M
from ns_studio.text.budget import master_budget
from tests.helpers import make_channel, make_ctx

RUBRIC = ("hook", "clarity", "pacing", "payoff", "cliffhangers", "originality", "voice_fit")
BAD_CRITIC = {"scores": {k: 5 for k in RUBRIC}, "fixes": ["Fix it."]}
GOOD_CRITIC = {"scores": {k: 9 for k in RUBRIC}, "fixes": []}
CHANNEL = {"format": "master_first", "long_minutes": (11, 11)}


class TextLlm(FakeLlm):
    """FakeLlm whose `revise` answers with a fixed text (the plain FakeLlm serves the short story)."""

    def __init__(self, fixtures_dir: Path, revise_text: str, **kw) -> None:
        super().__init__(fixtures_dir, **kw)
        self.revise_text = revise_text

    def complete_text(self, *, prompt_name: str, system: str, user: str, thinking: bool = False,
                      temperature: float = 0.8, max_tokens: int = 2048) -> str:
        if prompt_name == "revise":
            self.calls.append({"prompt_name": prompt_name, "user": user})
            return self.revise_text
        return super().complete_text(prompt_name=prompt_name, system=system, user=user, thinking=thinking)


def _chapter_fixture(n: int) -> dict:
    return json.loads((FIXTURES_LLM / f"chapter_draft_{n}.json").read_text(encoding="utf-8"))


def _fixture_story() -> Story:
    chapters = []
    for n in range(1, 6):
        d = _chapter_fixture(n)
        chapters.append(M.build_chapter(n, f"Chapter {n}", d["text"], d["summary"], wpm=155, is_last=n == 5))
    return Story(working_title="The Doorbell Camera", chapters=chapters)


def _master_ctx(monkeypatch: pytest.MonkeyPatch, tmp_home: Path, stages: list[str] | None = None):
    monkeypatch.setitem(FORMAT_STAGES, "master_first", list(stages or M.MASTER_STAGES))
    return make_ctx(tmp_home, fmt="master_first", channel=make_channel(**CHANNEL))


def test_registration_order():
    assert FORMAT_STAGES["master_first"][:9] == M.MASTER_STAGES
    assert list(MASTER_FIRST_STAGES[:9]) == M.MASTER_STAGES
    assert all(n in REGISTRY for n in M.MASTER_STAGES)
    assert all(REGISTRY[n].tenant == "llama" for n in M.MASTER_STAGES)
    assert len(set(M.MASTER_STAGES)) == 9


def test_master_pipeline_on_fixtures(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = _master_ctx(monkeypatch, tmp_home)
    assert run_job(ctx) == M.MASTER_STAGES

    plan = Plan.model_validate(ctx.read_json("plan.json"))
    mb = master_budget(11, "standard")
    assert (plan.target_minutes, plan.target_words) == (11, mb.target_words)
    assert [c.word_budget for c in plan.chapters] == list(mb.chapter_budgets)
    assert ctx.read_json("plan_meta.json")["working_title"] == "The Doorbell Camera"

    story = Story.model_validate(ctx.read_json("story.json"))
    assert story.working_title == "The Doorbell Camera" and len(story.chapters) == 5
    for ch in story.chapters:
        assert ch.text == _chapter_fixture(ch.n)["text"].strip()
        assert [s.idx for s in ch.sentences] == list(range(len(ch.sentences)))
        assert ch.sentences[-1].text == split_sentences(ch.text)[-1]
        assert 0.8 * min(mb.chapter_budgets) <= ch.word_count <= 1.2 * max(mb.chapter_budgets)
        assert ch.est_seconds == pytest.approx(ch.word_count / 155 * 60, abs=0.01)
    assert [c.cliff_line_idx is None for c in story.chapters] == [False] * 4 + [True]
    assert story.chapters[0].cliff_line_idx == len(story.chapters[0].sentences) - 1
    assert sum(c.word_count for c in story.chapters) >= 9 * 155
    assert ctx.read_json("critic.json")["critic_avg"] >= 8

    cont = ctx.read_json("continuity.json")
    assert cont["pairwise"] is True and cont["issues"] == []
    calls = ctx.services.llm.calls  # type: ignore[attr-defined]
    assert len([c for c in calls if c["prompt_name"] == "continuity"]) == 4

    row = ctx.db.one("select working_title, word_count from stories where job_id=?", (ctx.job_id,))
    assert row["working_title"] == "The Doorbell Camera"
    assert row["word_count"] == sum(c.word_count for c in story.chapters)

    seo = SeoLong.model_validate(ctx.read_json("seo_long.json"))
    assert seo.title == "Horror Story: My Doorbell Camera Records Tomorrow Night"
    assert "[[CHAPTERS]]" in seo.description and seo.description.rstrip().endswith("Original fiction.")
    assert "#Shorts" not in seo.hashtags and 3 <= len(seo.hashtags) <= 5
    assert 10 <= len(seo.tags) <= 20 and [c.n for c in seo.chapter_titles] == [1, 2, 3, 4, 5]

    shorts = [ShortsPlanItem.model_validate(i) for i in ctx.read_json("shorts_plan.json")]
    assert [s.k for s in shorts] == [1, 2, 3, 4, 5]
    assert [(s.chapter, s.sentence_start, s.sentence_end) for s in shorts] == [
        (1, 15, 19),
        (2, 14, 21),
        (3, 11, 20),
        (4, 7, 13),
        (5, 11, 19),
    ]
    assert [s.length_bucket for s in shorts] == [35, 50, 75, 50, 50]
    assert all(len(s.hooks) == 5 and s.chosen_hook == s.hooks[0] for s in shorts)
    assert all(len(s.chosen_hook.split()) <= 12 and len(s.closing_line.split()) <= 15 for s in shorts)
    assert shorts[0].chosen_hook == "I came home to wet footprints on a dry porch."
    combos = ctx.read_json("short_combos.json")  # each Short has its own combo, not the master's
    assert [s.combo_id for s in shorts] == [combos[str(s.k)]["combo_id"] for s in shorts]
    assert all(s.combo_id != "c-test" and s.start_s is None for s in shorts)

    beats = [VisualBeat.model_validate(b) for b in ctx.read_json("visual_plan.json")]
    for ch in story.chapters:
        mine = sorted((b for b in beats if b.chapter == ch.n), key=lambda b: b.sentence_start)
        covered = [i for b in mine for i in range(b.sentence_start, b.sentence_end + 1)]
        assert covered == list(range(len(ch.sentences))), ch.n
        assert all(b.part is None and len(b.keywords) <= 3 for b in mine)
        spans = [(s.sentence_start, s.sentence_end) for s in shorts if s.chapter == ch.n]
        for b in mine:
            inside = any(b.sentence_start <= e and b.sentence_end >= s for s, e in spans)
            assert b.need_vertical is inside, (ch.n, b.beat)
    assert any(b.need_vertical for b in beats) and not all(b.need_vertical for b in beats)

    seo_shorts = [Seo.model_validate(s) for s in ctx.read_json("seo_shorts.json")]
    assert len(seo_shorts) == 5
    for s in seo_shorts:
        assert s.hashtags[0] == "#Shorts" and 10 <= len(s.tags) <= 20
        assert f"From the full story: {seo.title}" in s.description
        assert s.description.rstrip().endswith("Original fiction.")
    assert seo_shorts[0].title == "Horror Story: Wet Footprints on a Dry Porch"

    assert set(ctx.prompt_versions) >= {
        "ideas", "long_plan", "chapter_draft", "critic", "continuity", "long_seo", "judge",
        "shorts_select", "short_hook", "seo", "visual_plan",
    }  # fmt: skip


def test_each_short_is_written_and_titled_in_its_own_combo(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """Review row 5: hook and title prompts used the master's combo, and the ledger burned other combos."""
    ctx = _master_ctx(monkeypatch, tmp_home)
    run_job(ctx)
    combos = ctx.read_json("short_combos.json")
    assert set(combos) == {"1", "2", "3", "4", "5"}
    calls = ctx.services.llm.calls  # type: ignore[attr-defined]
    for k, combo in combos.items():
        assert (combo["structure"], combo["pacing"]) == (ctx.combo.structure, ctx.combo.pacing)
        hook_call = next(c for c in calls if c["prompt_name"] == f"short_hook_{k}")
        assert f"Hook style: {combo['hook']}" in hook_call["user"]
        seo_call = next(c for c in calls if c["prompt_name"] == f"seo_short_{k}")
        assert f"Title formula: {combo['title_formula']}" in seo_call["user"]
    ledger = ctx.db.query("select item, combo_id from combos where job_id=? order by item", (ctx.job_id,))
    assert [r["item"] for r in ledger] == ["s1", "s2", "s3", "s4", "s5"]
    assert [r["combo_id"] for r in ledger] == [combos[str(k)]["combo_id"] for k in range(1, 6)]


def test_master_second_run_makes_no_llm_calls(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = _master_ctx(monkeypatch, tmp_home)
    run_job(ctx)
    before = len(ctx.services.llm.calls)  # type: ignore[attr-defined]
    run_job(ctx)
    assert len(ctx.services.llm.calls) == before  # type: ignore[attr-defined]
    assert ctx.db.one("select count(*) c from stories")["c"] == 1


def test_ideas_budget_uses_the_master_target(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = _master_ctx(monkeypatch, tmp_home)
    REGISTRY["ideas"].run(ctx)
    assert f"about {master_budget(11, 'standard').target_words} words" in ctx.services.llm.calls[0]["user"]  # type: ignore[attr-defined]


def test_whole_story_continuity_on_gpu_profile(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = _master_ctx(monkeypatch, tmp_home, M.MASTER_STAGES[:5])
    ctx.runtime = Runtime.default("gpu")
    run_job(ctx)
    assert ctx.read_json("continuity.json")["pairwise"] is False
    calls = [c for c in ctx.services.llm.calls if c["prompt_name"] == "continuity"]  # type: ignore[attr-defined]
    assert len(calls) == 1 and "the whole story" in calls[0]["user"]


def test_pairwise_continuity_switch():
    ctx_gpu = Runtime.default("gpu")
    small = Runtime(profile="gpu", llama=LlamaCfg(flags=LlamaFlags(ctx=8192)))

    class Fake:
        def __init__(self, runtime: Runtime) -> None:
            self.runtime = runtime

    assert M.pairwise_continuity(Fake(ctx_gpu)) is False  # type: ignore[arg-type]
    assert M.pairwise_continuity(Fake(small)) is True  # type: ignore[arg-type]
    assert M.pairwise_continuity(Fake(Runtime.default("cpu-dev"))) is True  # type: ignore[arg-type]


def test_continuity_issue_triggers_one_revise_and_is_recorded(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = _master_ctx(monkeypatch, tmp_home, M.MASTER_STAGES[:4])
    run_job(ctx)
    ch3 = _chapter_fixture(3)["text"]
    revised = ch3.replace("my own handwriting", "my own careful handwriting")
    issue = {"ok": False, "issues": [{"chapter": 3, "problem": "Handwriting is unclear.", "fix": "Say careful."}]}
    ctx.services.llm = TextLlm(FIXTURES_LLM, revised, overrides={"continuity": [issue, issue]})
    ctx.runtime = Runtime.default("gpu")
    REGISTRY["continuity"].run(ctx)
    story = Story.model_validate(ctx.read_json("story.json"))
    assert "careful handwriting" in story.chapters[2].text and story.chapters[1].text.startswith("I stood")
    assert ctx.read_json("continuity.json")["revised"] == [3]
    revise_calls = [c for c in ctx.services.llm.calls if c["prompt_name"] == "revise"]  # type: ignore[attr-defined]
    assert len(revise_calls) == 1 and "- Say careful." in revise_calls[0]["user"]
    assert story.revisions == 1


def test_chapters_critic_revises_each_failing_chapter_once(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = _master_ctx(monkeypatch, tmp_home, M.MASTER_STAGES[:3])
    run_job(ctx)
    text = _chapter_fixture(2)["text"]
    ctx.services.llm = TextLlm(FIXTURES_LLM, text, overrides={"critic": [BAD_CRITIC, GOOD_CRITIC]})
    REGISTRY["chapters_critic"].run(ctx)
    rev = ctx.read_json("story_revised.json")
    assert rev["revisions"] == 5 and all(c["text"] == text.strip() for c in rev["chapters"])
    assert [c["revisions"] for c in ctx.read_json("critic.json")["chapters"]] == [1] * 5


def test_chapters_critic_fails_with_e304(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = _master_ctx(monkeypatch, tmp_home, M.MASTER_STAGES[:3])
    run_job(ctx)
    ctx.services.llm = TextLlm(FIXTURES_LLM, "x", overrides={"critic": BAD_CRITIC})
    with pytest.raises(NsError) as ei:
        REGISTRY["chapters_critic"].run(ctx)
    assert ei.value.code == "NS-E304" and "chapter 1" in ei.value.detail


def test_chapters_critic_validates_length_e303(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = _master_ctx(monkeypatch, tmp_home, M.MASTER_STAGES[:3])
    run_job(ctx)
    draft = ctx.read_json("story_draft.json")
    draft["chapters"][1]["text"] = "Too short to be a chapter."
    ctx.write_json("story_draft.json", draft)
    with pytest.raises(NsError) as ei:
        REGISTRY["chapters_critic"].run(ctx)
    assert ei.value.code == "NS-E303" and "length" in ei.value.detail


def test_long_plan_with_wrong_chapter_count_is_e302(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = _master_ctx(monkeypatch, tmp_home)
    REGISTRY["ideas"].run(ctx)
    plan = json.loads((FIXTURES_LLM / "long_plan.json").read_text(encoding="utf-8"))
    plan["chapters"] = plan["chapters"][:4]
    ctx.services.llm = FakeLlm(FIXTURES_LLM, overrides={"long_plan": plan})
    with pytest.raises(NsError) as ei:
        REGISTRY["long_plan"].run(ctx)
    assert ei.value.code == "NS-E302"


def test_chapter_drafts_are_saved_and_reused_for_the_same_plan(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = _master_ctx(monkeypatch, tmp_home, M.MASTER_STAGES[:3])
    run_job(ctx)
    assert sorted(p.name for p in (ctx.job_dir / "drafts").glob("ch*.json")) == [f"ch{n}.json" for n in range(1, 6)]
    before = len([c for c in ctx.services.llm.calls if c["prompt_name"].startswith("chapter_draft")])  # type: ignore[attr-defined]
    REGISTRY["chapters_draft"].run(ctx)
    after = len([c for c in ctx.services.llm.calls if c["prompt_name"].startswith("chapter_draft")])  # type: ignore[attr-defined]
    assert before == after == 5
    users = [c["user"] for c in ctx.services.llm.calls if c["prompt_name"] == "chapter_draft_3"]  # type: ignore[attr-defined]
    assert "Chapter 2, The Hall Light:" in users[0]
    assert "The hall light was on. I never leave it on, not even once." in users[0]  # verbatim tail of ch 2


# --- Shorts selection resolver ---------------------------------------------------------------------------


def _moments() -> list[dict]:
    return json.loads((FIXTURES_LLM / "shorts_select.json").read_text(encoding="utf-8"))["moments"]


def test_resolve_moments_sizes_fixture_spans_to_buckets():
    out = M.resolve_moments(_moments(), _fixture_story(), buckets=[35, 50, 75], wpm=155, count_min=4, count_max=6)
    assert [(m["chapter"], m["length_bucket"], m["words"]) for m in out] == [
        (1, 35, 75),
        (2, 50, 100),
        (3, 75, 159),
        (4, 50, 111),
        (5, 50, 119),
    ]


def test_resolve_moments_drops_overlaps_unknown_chapters_and_extra():
    story = _fixture_story()
    base = _moments()
    overlap = {**base[0], "sentence_start": 17, "sentence_end": 19}
    unknown = {**base[0], "chapter": 9}
    out = M.resolve_moments([base[0], overlap, unknown, *base[1:]], story, buckets=[35, 50, 75], wpm=155,
                            count_min=4, count_max=4)
    assert [m["chapter"] for m in out] == [1, 2, 3, 4]


def test_resolve_moments_clamps_and_reends_spans_that_miss_the_bucket():
    story = _fixture_story()
    short = {"chapter": 3, "sentence_start": 11, "sentence_end": 12, "length_bucket": 50, "why_standalone": "x"}
    huge = {"chapter": 5, "sentence_start": 0, "sentence_end": 999, "length_bucket": 75, "why_standalone": "y"}
    out = M.resolve_moments([short, huge], story, buckets=[35, 50, 75], wpm=155, count_min=1, count_max=6)
    by_ch = {m["chapter"]: m for m in out}
    for m in out:
        target = M.short_body_words(m["length_bucket"], 155)
        assert 0.8 * target <= m["words"] <= 1.2 * target
    assert by_ch[3]["sentence_start"] == 11 and by_ch[3]["sentence_end"] > 12
    assert by_ch[5]["sentence_start"] == 0 and by_ch[5]["sentence_end"] <= len(story.chapters[4].sentences) - 1


def test_resolve_moments_too_few_is_e303():
    with pytest.raises(NsError) as ei:
        M.resolve_moments(_moments()[:2], _fixture_story(), buckets=[35, 50, 75], wpm=155, count_min=4, count_max=6)
    assert ei.value.code == "NS-E303"


# --- expand_chapter ------------------------------------------------------------------------------------


def test_expansion_plan_picks_shortest_chapter_and_clamps_words():
    story = _fixture_story()
    shortest = min(story.chapters, key=lambda c: c.word_count).n
    assert M.expansion_plan(story, 50) == (shortest, 120)
    assert M.expansion_plan(story, 150) == (shortest, 150)
    assert M.expansion_plan(story, 900) == (shortest, 200)


def test_sentence_index_map_and_remap():
    mapping = M.sentence_index_map(["a", "b", "c"], ["a", "x", "b", "c"])
    assert mapping == {0: 0, 1: 2, 2: 3}
    assert M.remap_index(1, mapping, 4) == 2
    assert M.remap_index(1, {0: 0}, 2) == 1  # unmatched: offset from the nearest matched neighbour
    assert M.remap_index(5, {0: 0}, 3) == 2  # clamped to the new length


def test_expand_chapter_updates_story_and_remaps_plans(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = _master_ctx(monkeypatch, tmp_home)
    run_job(ctx)
    old = Story.model_validate(ctx.read_json("story.json")).chapters[0]
    new = M.expand_chapter(ctx, 1, 120)
    assert new.word_count - old.word_count >= 120 and len(new.sentences) == len(old.sentences) + 8
    story = Story.model_validate(ctx.read_json("story.json"))
    assert story.chapters[0].text == new.text
    assert story.chapters[1].text == _chapter_fixture(2)["text"].strip()
    assert [(s["sentence_start"], s["sentence_end"]) for s in ctx.read_json("shorts_plan.json")][0] == (22, 27)
    mine = sorted(
        (b for b in ctx.read_json("visual_plan.json") if b["chapter"] == 1), key=lambda b: b["sentence_start"]
    )
    assert mine[0]["sentence_start"] == 0 and mine[-1]["sentence_end"] == len(new.sentences) - 1
    assert all(b["sentence_start"] == a["sentence_end"] + 1 for a, b in zip(mine, mine[1:], strict=False))
    assert [b["need_vertical"] for b in mine].count(True) == 2
    # the chapter's own spoken sentences are unchanged: every old sentence is still present in order
    old_texts = [s.text for s in old.sentences]
    new_texts = [s.text for s in new.sentences]
    assert [t for t in new_texts if t in old_texts] == old_texts
    row = ctx.db.one("select word_count from stories where job_id=?", (ctx.job_id,))
    assert row["word_count"] == sum(c.word_count for c in story.chapters)


def test_expand_chapter_rejects_too_small_expansion(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = _master_ctx(monkeypatch, tmp_home)
    run_job(ctx)
    with pytest.raises(NsError) as ei:
        M.expand_chapter(ctx, 1, 400)
    assert ei.value.code == "NS-E303"
    with pytest.raises(NsError):
        M.expand_chapter(ctx, 9, 120)


def test_expand_fixture_keeps_every_sentence_of_chapter_one():
    expanded = (FIXTURES_LLM / "expand.txt").read_text(encoding="utf-8")
    original = _chapter_fixture(1)["text"]
    new_s, old_s = split_sentences(expanded), split_sentences(original)
    assert [s for s in new_s if s in old_s] == old_s
    assert word_count(expanded) - word_count(original) >= 120


# --- local models and the policy / clipper fixtures ---------------------------------------------------------


def test_local_models_enforce_spec_constraints():
    from pydantic import ValidationError

    from ns_common.models import ClipPlanItem, PolicyCheckResult

    plan = json.loads((FIXTURES_LLM / "long_plan.json").read_text(encoding="utf-8"))
    chapters = plan["chapters"]
    Plan(target_minutes=10, target_words=1550, chapters=chapters)
    with pytest.raises(ValidationError):
        Plan(target_minutes=10, target_words=1550, chapters=chapters[:4])
    with pytest.raises(ValidationError):  # H-8b: the engine test plans 4 minutes; no length is refused
        Plan(target_minutes=0, target_words=1550, chapters=chapters)
    clips = json.loads((FIXTURES_LLM / "clip_select.json").read_text(encoding="utf-8"))["clips"]
    assert [ClipPlanItem.model_validate(c).k for c in clips] == [1, 2, 3]
    assert all(c["end_s"] - c["start_s"] >= 20 for c in clips)
    from ns_studio.text.prompts import parse_policy_check

    policy = parse_policy_check(json.loads((FIXTURES_LLM / "policy_check.json").read_text(encoding="utf-8")), "fixture")
    assert policy.unsafe_categories == [] and policy.standalone is True
    with pytest.raises(ValidationError):
        PolicyCheckResult.model_validate({"unsafe_categories": [], "hook_honest": True})


def test_policy_fixture_categories_are_known_ids():
    from ns_studio.text.prompts import unsafe_categories

    ids = {c["id"] for c in unsafe_categories()}
    assert {"real_people", "minors", "graphic_harm", "hate", "advice", "fake_news"} <= ids
    policy = json.loads((FIXTURES_LLM / "policy_check.json").read_text(encoding="utf-8"))
    assert set(policy["unsafe_categories"]) <= ids


def test_policy_check_prompt_runs_through_call_json(tmp_home: Path):
    from ns_studio.text.prompts import call_json

    ctx = make_ctx(tmp_home)
    out = call_json(ctx, "policy_check", story="Story.", title="A title", hook="A hook.", span_text="A span.")
    assert out["hook_honest"] is True
    call = ctx.services.llm.calls[-1]  # type: ignore[attr-defined]
    assert call["thinking"] is True and "fake_news:" in call["user"] and "Selected moment" in call["user"]


def test_models_local_is_a_thin_alias_of_ns_common_models():
    import ns_common.models as shared
    from ns_studio.text import models_local

    for name in ("Plan", "PlanChapter", "Story", "StoryChapter", "Sentence", "ShortsPlanItem", "SeoLong"):
        assert getattr(models_local, name) is getattr(shared, name), name
    assert models_local.ClipPlanItem is shared.ClipPlanItem
    assert models_local.PolicyCheckResult is shared.PolicyCheckResult
