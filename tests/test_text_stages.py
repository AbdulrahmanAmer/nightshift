import json
from pathlib import Path

import pytest

from ns_common.embed_client import to_blob
from ns_common.errors import NsError
from ns_common.llm_client import FakeLlm
from ns_common.models import Part, Seo, VisualBeat
from ns_common.textstats import split_sentences, word_count
from ns_studio.context import FIXTURES_LLM
from ns_studio.orchestrator import FORMAT_STAGES, SHORTS_ONLY_STAGES, run_job
from ns_studio.stages import REGISTRY
from ns_studio.text import stages_shorts as S
from tests.helpers import make_ctx

STAGES = ["ideas", "plan", "draft", "critic_revise", "split", "hooks", "seo", "visual_plan"]
RUBRIC = ("hook", "clarity", "pacing", "payoff", "cliffhangers", "originality", "voice_fit")
BAD_CRITIC = {"scores": {k: 5 for k in RUBRIC}, "fixes": ["Fix it."]}


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES_LLM / name).read_text(encoding="utf-8"))


def _text_only(monkeypatch: pytest.MonkeyPatch, tmp_home: Path):
    monkeypatch.setitem(FORMAT_STAGES, "text_only", list(FORMAT_STAGES["shorts_only"][:8]))
    return make_ctx(tmp_home, fmt="text_only")


def test_registration_order():
    assert FORMAT_STAGES["shorts_only"][:8] == STAGES
    assert list(SHORTS_ONLY_STAGES[:8]) == STAGES
    assert all(n in REGISTRY for n in STAGES)
    assert all(REGISTRY[n].tenant == "llama" for n in STAGES)


def test_text_pipeline_runs_on_fixtures(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = _text_only(monkeypatch, tmp_home)
    assert run_job(ctx) == STAGES
    idea = ctx.read_json("idea.json")
    assert idea["premise"].startswith("Every night at 3:12")
    story = ctx.read_json("story.json")
    assert story["word_count"] > 40 and story["revisions"] == 0
    parts = [Part.model_validate(p) for p in ctx.read_json("parts.json")]
    assert len(parts) == 1
    assert parts[0].hook == "Every night at 3:12, someone stands on my porch."
    assert parts[0].word_count == word_count(parts[0].text)
    assert parts[0].cliffhanger is False
    seo = [Seo.model_validate(s) for s in ctx.read_json("seo.json")]
    assert "#Shorts" in seo[0].hashtags and seo[0].description.endswith("Original fiction.")
    assert 10 <= len(seo[0].tags) <= 20
    beats = [VisualBeat.model_validate(b) for b in ctx.read_json("visual_plan.json")]
    assert len(beats) == 4
    covered = sorted(i for b in beats for i in range(b.sentence_start, b.sentence_end + 1))
    assert covered == list(range(len(split_sentences(parts[0].text))))
    expected = {"ideas", "story_plan", "story_draft", "critic", "split", "hook", "judge", "seo"}
    assert set(ctx.prompt_versions) >= expected | {"visual_plan"}
    assert ctx.db.one("select count(*) c from stories")["c"] == 1
    assert ctx.db.one("select count(*) c from ideas where used=1")["c"] == 1
    assert len(ctx.read_json("ideas_all.json")) == 8
    assert ctx.read_json("hooks_all.json")[0][0] == parts[0].hook


def test_fixture_story_is_one_coherent_text():
    draft = (FIXTURES_LLM / "story_draft.txt").read_text(encoding="utf-8").strip()
    assert _fixture("split.json")["parts"][0]["text"] == draft
    assert len(split_sentences(draft)) == 9
    assert _fixture("visual_plan.json")["beats"][-1]["sentence_end"] == 8


def test_second_run_makes_no_further_llm_calls(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = _text_only(monkeypatch, tmp_home)
    run_job(ctx)
    before = len(ctx.services.llm.calls)  # type: ignore[attr-defined]
    run_job(ctx)
    assert len(ctx.services.llm.calls) == before  # type: ignore[attr-defined]
    assert ctx.db.one("select count(*) c from stories")["c"] == 1


def test_critic_fail_twice_raises_e304(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.services.llm = FakeLlm(FIXTURES_LLM, overrides={"critic": BAD_CRITIC})
    text = "The doorbell camera pings at 3:12 every night. " * 10
    ctx.write_json("story.json", {"text": text, "word_count": 90, "revisions": 0})
    with pytest.raises(NsError) as ei:
        REGISTRY["critic_revise"].run(ctx)
    assert ei.value.code == "NS-E304"
    calls = ctx.services.llm.calls  # type: ignore[attr-defined]
    assert len([c for c in calls if c["prompt_name"] == "revise"]) == 2


def test_critic_pass_after_one_revision(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    good = _fixture("critic.json")
    ctx.services.llm = FakeLlm(FIXTURES_LLM, overrides={"critic": [BAD_CRITIC, good]})
    draft = (FIXTURES_LLM / "story_draft.txt").read_text(encoding="utf-8")
    ctx.write_json("story.json", {"text": draft, "word_count": word_count(draft), "revisions": 0})
    REGISTRY["critic_revise"].run(ctx)
    story = ctx.read_json("story.json")
    assert story["revisions"] == 1
    assert story["critic_avg"] == pytest.approx(sum(good["scores"].values()) / 7)
    assert ctx.read_json("critic.json")["verdict"] == "pass"
    assert ctx.db.one("select revisions from stories")["revisions"] == 1


def test_critic_validator_failure_is_e303(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("story.json", {"text": "Too short.", "word_count": 2, "revisions": 0})
    with pytest.raises(NsError) as ei:
        REGISTRY["critic_revise"].run(ctx)
    assert ei.value.code == "NS-E303"
    assert "length" in ei.value.detail


def test_critic_numbers_requires_all_scores():
    with pytest.raises(NsError) as ei:
        S.critic_numbers({"scores": {"hook": 9}})
    assert ei.value.code == "NS-E302"


def test_ideas_dedupes_against_history(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    first = _fixture("ideas.json")["ideas"][0]
    vec = ctx.services.embed.embed([first["premise"]])[0]
    ctx.db.execute(
        "insert into ideas(channel, premise, twist, embedding, created_at, used) values (?,?,?,?,?,1)",
        (ctx.channel.slug, first["premise"], first["twist"], to_blob(vec), "2026-10-01"),
    )
    REGISTRY["ideas"].run(ctx)
    assert ctx.read_json("idea.json")["premise"] != first["premise"]
    avoid = ctx.services.llm.calls[0]["user"]  # type: ignore[attr-defined]
    assert first["premise"] in avoid


def test_ideas_all_similar_is_e304(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    first = _fixture("ideas.json")["ideas"][0]
    ctx.services.llm = FakeLlm(FIXTURES_LLM, overrides={"ideas": {"ideas": [first]}})
    vec = ctx.services.embed.embed([first["premise"]])[0]
    ctx.db.execute(
        "insert into ideas(channel, premise, twist, embedding, created_at, used) values (?,?,?,?,?,1)",
        (ctx.channel.slug, first["premise"], first["twist"], to_blob(vec), "2026-10-01"),
    )
    with pytest.raises(NsError) as ei:
        REGISTRY["ideas"].run(ctx)
    assert ei.value.code == "NS-E304"


def test_ideas_bad_schema_is_e302(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.services.llm = FakeLlm(FIXTURES_LLM, overrides={"ideas": {"ideas": [{"premise": "only this"}]}})
    with pytest.raises(NsError) as ei:
        REGISTRY["ideas"].run(ctx)
    assert ei.value.code == "NS-E302"


def test_split_recomputes_numbers_and_clears_last_cliffhanger(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    wrong = {"word_count": 99, "est_seconds": 99, "cliffhanger": True}
    parts = [
        {"n": 1, "text": "One two three four.", "hook": "", **wrong},
        {"n": 2, "text": "Five six seven.", "hook": "Five!", **wrong},
    ]
    ctx.services.llm = FakeLlm(FIXTURES_LLM, overrides={"split": {"parts": parts}})
    ctx.write_json("story.json", {"text": "x", "word_count": 1, "revisions": 0})
    REGISTRY["split"].run(ctx)
    out = [Part.model_validate(p) for p in ctx.read_json("parts.json")]
    assert [p.word_count for p in out] == [4, 3]
    assert out[0].hook == "One two three four."
    assert out[0].est_seconds == pytest.approx(4 / 155 * 60, abs=0.01)
    assert [p.cliffhanger for p in out] == [True, False]


def test_hooks_drop_long_candidates_and_respect_judge(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    long_hook = "one two three four five six seven eight nine ten eleven twelve thirteen"
    hooks = {"hooks": [long_hook, "Short and true.", "Another honest one.", "Third.", "Fourth."]}
    ctx.services.llm = FakeLlm(FIXTURES_LLM, overrides={"hook": hooks, "judge": {"index": 1, "reason": "x"}})
    part = {"n": 1, "text": "A part.", "hook": "", "word_count": 2, "est_seconds": 1.0, "cliffhanger": False}
    ctx.write_json("parts.json", [part])
    REGISTRY["hooks"].run(ctx)
    assert ctx.read_json("parts.json")[0]["hook"] == "Another honest one."
    assert long_hook not in ctx.read_json("hooks_all.json")[0]


def test_seo_finalize_rules():
    seo = Seo(
        title="A Night I Will Not Forget Any Time Soon Ever",
        description="d",
        tags=["a"],
        hashtags=[],
        keywords=[],
    )
    out = S.finalize_seo(seo, primary_keyword="scary story", fiction_label=True, part_n=None, parts_total=1)
    assert out.title.lower().startswith("scary story")
    assert out.hashtags[0] == "#Shorts" and 3 <= len(out.hashtags) <= 5
    assert out.description.rstrip().endswith("Original fiction.")
    assert 10 <= len(out.tags) <= 20


def test_finalize_seo_series_part_and_master_line():
    seo = Seo(
        title="Scary Story: The Door",
        description="  Honest line.  ",
        tags=["x", "x", *[f"{'y' * 53}{i:02d}" for i in range(18)]],
        hashtags=["#shorts", "horror story", "#Horror", "#a", "#b"],
        keywords=["k"],
    )
    out = S.finalize_seo(
        seo,
        primary_keyword="scary story",
        fiction_label=False,
        part_n=2,
        parts_total=3,
        master_title="The Door",
    )
    assert out.title.endswith("(Part 2)")
    assert out.description.splitlines() == ["Honest line.", "Part 2 of 3.", "From the full story: The Door"]
    assert out.hashtags[0] == "#Shorts" and out.hashtags.count("#Shorts") == 1 and len(out.hashtags) == 5
    assert "#horrorstory" in out.hashtags
    assert 10 <= len(out.tags) <= 20
    assert sum(len(t) for t in out.tags) + len(out.tags) - 1 <= 500
    assert len({t.lower() for t in out.tags}) == len(out.tags)


def test_finalize_seo_title_capped_at_100():
    seo = Seo(title="x" * 95, description="d", tags=["a"], hashtags=[])
    out = S.finalize_seo(seo, primary_keyword="word", fiction_label=True, part_n=None, parts_total=1)
    assert len(out.title) <= 100


def _beat(s: int, e: int, kw: str = "kw") -> VisualBeat:
    return VisualBeat(part=1, beat=0, sentence_start=s, sentence_end=e, keywords=[kw], mood="calm")


def test_repair_beats_fills_gaps_and_drops_overlaps():
    default = _beat(0, 0, "default")
    raw = [_beat(0, 1, "a"), _beat(1, 2, "overlap"), _beat(5, 20, "tail")]
    fixed = S.repair_beats(raw, 8, group=3, default=default)
    spans = [(b.sentence_start, b.sentence_end) for b in fixed]
    assert spans == [(0, 1), (2, 4), (5, 7)]
    assert [b.beat for b in fixed] == [0, 1, 2]
    assert fixed[1].keywords == ["a"]


def test_repair_beats_uses_default_when_nothing_valid():
    fixed = S.repair_beats([], 4, group=3, default=_beat(0, 0, "default"))
    assert [(b.sentence_start, b.sentence_end) for b in fixed] == [(0, 2), (3, 3)]
    assert fixed[0].keywords == ["default"]
