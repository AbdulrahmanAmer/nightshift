# ruff: noqa: E501
"""H-8: the engine-test story profile (docs/DEV_MODE.md, "A tiny model finishes a story").

On an install made for testing (`tiny_story` in runtime.json) the text stages read the thresholds a 1 to 2 GB
model can meet from ONE place, `ns_studio.text.story_profile`. Every validator still runs; only its numbers
move. With the switch off the numbers are byte-for-byte what they were. The switch itself (answers, refusal
without `allow_cpu`, the dashboard) is in `test_tiny_story_switch.py`.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import ns_studio.text.stages_shorts  # noqa: F401  (registers the shorts stages)
from ns_common.embed_client import FakeEmbed
from ns_common.errors import NsError
from ns_common.llm_client import FakeLlm
from ns_common.models import Plan, Runtime, ShortsPlanItem, Story
from ns_common.pacing import master_fixed_seconds, words_for_seconds
from ns_common.textstats import fk_grade, word_count
from ns_studio.context import FIXTURES_LLM
from ns_studio.orchestrator import FORMAT_STAGES, run_job
from ns_studio.stages import REGISTRY
from ns_studio.text import stages_master as M
from ns_studio.text import validators as V
from ns_studio.text.budget import (
    chapter_budget,
    distribute_words,
    master_budget,
    short_budget,
)
from tests.helpers import make_channel, make_ctx

RUBRIC = ("hook", "clarity", "pacing", "payoff", "cliffhangers", "originality", "voice_fit")
GOOD_CRITIC = {"scores": {k: 9 for k in RUBRIC}, "fixes": []}
TINY_MASTER_WORDS = words_for_seconds(9 * 60, 155, fixed_s=master_fixed_seconds(7))  # 9:00 in 7 chapters


def profiles() -> Any:
    """The profile module, imported when a test runs so each test fails by itself where it is missing."""
    from ns_studio.text import story_profile

    return story_profile


def tiny_runtime(rt: Runtime | None = None) -> Runtime:
    """The runtime of an engine-test install (validated: the field must exist and be a bool)."""
    rt = rt or Runtime.default("cpu-dev")
    return Runtime.model_validate({**rt.model_dump(mode="json"), "tiny_story": True})


# ------------------------------------------------------------------ text at the scale a small model writes
SUBJ = [
    "The old man",
    "A small dog",
    "My older sister",
    "The tall girl",
    "Our grey cat",
    "The night guard",
    "A thin boy",
]
VERB = ["saw", "found", "held", "lost", "took", "heard", "left"]
OBJ = [
    "a red key",
    "the cold lamp",
    "an open door",
    "the wet map",
    "a bent spoon",
    "the dull bell",
    "a torn coat",
]
TAIL = [
    "by the pier.",
    "in the hall.",
    "on the road.",
    "near the shed.",
    "at the gate.",
    "under the bed.",
    "past the mill.",
]
ENDINGS = [
    "Then the lamp on the pier went out.",
    "I could not tell who had called my name.",
    "The tide had brought a boat I did not know.",
    "My hands shook as the radio began to hum.",
    "Somebody had set the table for two.",
    "The stairs below me started to creak.",
    "A second shadow stood beside mine on the wall.",
    "The bell rang once more, far out at sea.",
    "Nobody in the village would say her name.",
    "The key turned in a lock that was not there.",
    "Every window in the house faced the water.",
    "At dawn the pier was gone.",
]


def body_sentences(seed: int, count: int) -> list[str]:
    """`count` plain 10-word sentences (grade about 0.75: far under today's floor of 5)."""
    out = []
    for i in range(count):
        j = seed * 7 + i
        out.append(f"{SUBJ[j % 7]} {VERB[(j * 3 + 1) % 7]} {OBJ[(j * 5 + 2) % 7]} {TAIL[(j * 2 + seed) % 7]}")
    return out


def chapter_text(n: int, sentences: int = 14) -> str:
    """A chapter of `sentences` sentences (14 give about 138 words) ending on its own cliff line."""
    s = [*body_sentences(n, sentences - 1), ENDINGS[n - 1]]
    return "\n\n".join(" ".join(s[i : i + 4]) for i in range(0, len(s), 4))


def story_text(sentences: int) -> str:
    """A Short story of `sentences` 10-word sentences."""
    return " ".join(body_sentences(3, sentences))


def fake_story(chapters: int = 7) -> Story:
    return Story.model_validate(
        {
            "working_title": "The Pier Lamp",
            "chapters": [
                M.build_chapter(
                    n, f"Chapter {n}", chapter_text(n), "Facts.", wpm=155, is_last=n == chapters
                ).model_dump()
                for n in range(1, chapters + 1)
            ],
        }
    )


def test_the_fake_model_writes_at_a_scale_no_normal_install_accepts():
    """Guard for the tests below: the text is plain (grade under 5), short (under a normal chapter) and clean."""
    for n in range(1, 13):
        text = chapter_text(n)
        assert 130 <= word_count(text) <= 150
        assert -2.0 <= fk_grade(text) < 5.0
        assert V.check_banned(text, V.load_banned()) is None and V.check_sentence_length(text) is None


# ------------------------------------------------------------------------------------- the profile itself
def test_profile_of_follows_the_runtime_switch(tmp_home: Path):
    sp = profiles()
    ctx = make_ctx(tmp_home)
    assert sp.profile_of(ctx) is sp.NORMAL and sp.NORMAL.name == "normal" and not sp.NORMAL.is_tiny
    ctx.runtime = tiny_runtime(ctx.runtime)
    assert sp.profile_of(ctx) is sp.TINY and sp.TINY.name == "tiny" and sp.TINY.is_tiny


def test_the_normal_profile_holds_todays_numbers():
    sp = profiles()
    n = sp.NORMAL
    assert (n.grade_lo, n.grade_hi) == (5.0, 8.0) and n.grade_ask == "5-8"
    assert n.chapter_band == (0.8, 1.2)  # spec 7.0: +-20 %
    assert (n.chapter_beats_lo, n.chapter_beats_hi, n.story_beats_lo, n.story_beats_hi) == (2, 4, 4, 7)
    assert n.short_band is None and n.master_minutes is None and n.master_chapters is None
    assert n.master_floor_share is None and n.master_floor_s is None and n.moments_cap is None
    assert (n.master_short_share, n.expand_divisor) == (0.8, 2)
    assert (n.part_chapter_words, n.part_hard_floor) == (350, 0.75)
    assert [n.moments_min(k) for k in (1, 4, 6)] == [1, 4, 6]


def test_the_tiny_profile_is_a_scale_down_never_a_switch_off():
    sp = profiles()
    t, n = sp.TINY, sp.NORMAL
    assert t.grade_lo < n.grade_lo and t.grade_hi == n.grade_hi  # the floor moves, the ceiling stays
    assert t.chapter_band[0] < n.chapter_band[0] and t.chapter_band[1] > n.chapter_band[1]
    assert t.short_band is not None and 0 < t.short_band[0] < 1 < t.short_band[1]
    assert t.moments_min(4) == 2 and t.moments_min(1) == 1  # a cap, never a raise
    assert t.master_floor_s is not None and t.master_floor_s >= 510.0  # never under QC's own 8:30
    assert t.master_chapters is not None and 5 <= t.master_chapters <= 7  # the Plan model's own bounds
    assert t.master_minutes is not None and t.master_minutes >= 9
    assert t.part_hard_floor < n.part_hard_floor and t.part_chapter_words < n.part_chapter_words


# ------------------------------------------------------------------------- budgets: off is byte-for-byte
@pytest.mark.parametrize(
    ("bucket", "pacing", "want"),
    [
        (35, "standard", (70, 60, 80)),
        (50, "standard", (106, 92, 120)),
        (75, "measured", (149, 130, 169)),
        (35, "brisk", (80, 69, 91)),
    ],
)
def test_short_budget_with_the_switch_off_is_todays(bucket: int, pacing: str, want: tuple[int, int, int]):
    sp = profiles()
    for kw in ({}, {"profile": sp.NORMAL}):
        b = short_budget(bucket, pacing, **kw)
        assert (b.words, b.min_words, b.max_words) == want and b.parts == 1


def test_master_and_chapter_budgets_with_the_switch_off_are_todays():
    sp = profiles()
    for kw in ({}, {"profile": sp.NORMAL}):
        mb9, mb14 = master_budget(9, "standard", **kw), master_budget(14, "standard", **kw)
        assert (mb9.target_words, mb9.min_words, mb9.chapters) == (1259, 1259, 5)
        assert mb9.chapter_budgets == (252, 252, 252, 252, 251)
        assert (mb14.target_words, mb14.min_words, mb14.chapters) == (1970, 1259, 5)
        b = chapter_budget(394, 155, **kw)
        assert (b.words, b.min_words, b.max_words) == (394, 315, 473)


def test_validators_with_the_switch_off_are_todays():
    easy = "The dog ran home. The cat sat on the mat. We went to bed early."
    v = V.check_reading_level(easy)
    assert v is not None and v.detail == f"grade {fk_grade(easy)}, allowed 5.0-8.0"
    assert V.check_reading_level(easy, profile=profiles().NORMAL) == v
    assert V.check_reading_level(easy, lo=-5.0) is None  # an explicit bound still wins
    t = V.check_length("word " * 45, short_budget(50, "standard"))
    assert t is not None and t.detail == "45 words, allowed 92-120"


# ---------------------------------------------------------------------------- budgets: on, scaled
def test_tiny_short_budget_asks_for_the_same_words_and_accepts_a_wider_band():
    sp = profiles()
    normal, tiny = short_budget(50, "standard"), short_budget(50, "standard", profile=sp.TINY)
    assert tiny.words == normal.words == 106  # the ask is what QC needs: unchanged
    assert (tiny.min_words, tiny.max_words) == (37, 170)
    assert tiny.min_words < normal.min_words and tiny.max_words > normal.max_words
    assert (tiny.parts, tiny.wpm) == (normal.parts, normal.wpm)


def test_tiny_master_budget_plans_the_shortest_master_in_the_most_chapters():
    sp = profiles()
    mb = master_budget(14, "standard", profile=sp.TINY)
    assert (mb.target_minutes, mb.chapters, mb.wpm) == (9, 7, 155)
    assert mb.target_words == TINY_MASTER_WORDS
    assert mb.chapter_budgets == distribute_words(TINY_MASTER_WORDS, 7)
    assert mb.min_words == round(mb.target_words * 0.5)  # the planning floor is scaled too
    assert master_budget((10, 14), "brisk", profile=sp.TINY).target_minutes == 9
    with pytest.raises(ValueError):  # the input bounds are not the profile's
        master_budget(8, "standard", profile=sp.TINY)
    b = chapter_budget(178, 155, profile=sp.TINY)
    assert (b.words, b.min_words, b.max_words) == (178, 89, 356)


def test_tiny_reading_floor_and_length_band_still_fail_what_is_too_little():
    sp = profiles()
    plain = chapter_text(2)
    assert V.check_reading_level(plain) is not None  # grade 0.75: under today's floor
    assert V.check_reading_level(plain, profile=sp.TINY) is None
    fragments = " ".join(["Go. Go. Go. Go. Go. Go."] * 6)
    v = V.check_reading_level(fragments, profile=sp.TINY)
    assert v is not None and v.rule == "reading_level" and "allowed -2.0-8.0" in v.detail
    hard = "The epistemological ramifications of the aforementioned investigation remained fundamentally indeterminate."
    assert V.check_reading_level(hard, profile=sp.TINY) is not None  # the ceiling did not move
    b = short_budget(50, "standard", profile=sp.TINY)
    assert V.check_length(story_text(5), b) is None  # 50 words
    low = V.check_length(story_text(3), b)  # 30 words
    assert low is not None and low.detail == "30 words, allowed 37-170"
    assert V.check_length(story_text(24), b) is not None  # 240 words: over the band


def test_validate_story_and_chapter_take_the_profile():
    sp = profiles()
    embed = FakeEmbed()
    five, three = story_text(5), story_text(3)
    b = short_budget(50, "standard")
    assert {v.rule for v in V.validate_story(five, b, past_openings=[], embed=embed)} == {
        "length",
        "reading_level",
    }
    tb = short_budget(50, "standard", profile=sp.TINY)
    assert V.validate_story(five, tb, past_openings=[], embed=embed, profile=sp.TINY) == []
    assert [v.rule for v in V.validate_story(three, tb, past_openings=[], embed=embed, profile=sp.TINY)] == [
        "length"
    ]
    cb = chapter_budget(178, 155, profile=sp.TINY)
    ok = V.validate_chapter(chapter_text(1), cb, chapter_n=1, past_openings=[], embed=embed, profile=sp.TINY)
    assert ok == []
    short = V.validate_chapter(
        chapter_text(1, 6), cb, chapter_n=1, past_openings=[], embed=embed, profile=sp.TINY
    )
    assert [v.rule for v in short] == ["length"]
    normal = V.validate_chapter(
        chapter_text(1), chapter_budget(178, 155), chapter_n=1, past_openings=[], embed=embed
    )
    assert {v.rule for v in normal} == {"length", "reading_level"}


# ------------------------------------------------------------------------------ the master text stages
def plan_answer(chapters: int = 7) -> dict[str, Any]:
    return {
        "working_title": "The Pier Lamp",
        "chapters": [
            {"title": f"The Lamp {n}", "beats": ["She finds a key."], "cliff": ENDINGS[n - 1]}
            for n in range(1, chapters + 1)
        ],
    }


def moments(chapters: tuple[int, ...]) -> list[dict[str, Any]]:
    """Six-sentence spans (60 words) for the 35 s bucket, one per chapter named."""
    return [
        {
            "chapter": c,
            "sentence_start": 2,
            "sentence_end": 7,
            "length_bucket": 35,
            "why_standalone": "A clear beat.",
        }
        for c in chapters
    ]


def master_ctx(
    monkeypatch: pytest.MonkeyPatch,
    tmp_home: Path,
    *,
    tiny: bool,
    texts: dict[int, str] | None = None,
    picked: tuple[int, ...] = (1, 3, 5),
):
    """A master job whose fake model writes 7 chapters of about 138 words (a 1 to 2 GB model's size)."""
    monkeypatch.setitem(FORMAT_STAGES, "master_first", list(M.MASTER_STAGES))
    ctx = make_ctx(
        tmp_home, fmt="master_first", channel=make_channel(format="master_first", long_minutes=(11, 11))
    )
    if tiny:
        ctx.runtime = tiny_runtime(ctx.runtime)
    texts = texts or {}
    overrides: dict[str, Any] = {
        "long_plan": plan_answer(),
        "critic": GOOD_CRITIC,
        "shorts_select": {"moments": moments(picked)},
    }
    for n in range(1, 8):
        overrides[f"chapter_draft_{n}"] = {
            "text": texts.get(n, chapter_text(n)),
            "summary": "Two plain facts.",
        }
    ctx.services.llm = FakeLlm(FIXTURES_LLM, overrides=overrides)
    return ctx


def test_a_model_at_the_tiny_scale_passes_every_text_stage(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = master_ctx(monkeypatch, tmp_home, tiny=True)
    assert run_job(ctx) == M.MASTER_STAGES  # all nine, none raised

    plan = Plan.model_validate(ctx.read_json("plan.json"))
    mb = master_budget(11, "standard", profile=profiles().TINY)
    assert (plan.target_minutes, plan.target_words, len(plan.chapters)) == (9, mb.target_words, 7)
    assert [c.word_budget for c in plan.chapters] == list(mb.chapter_budgets)
    assert ctx.read_json("plan_meta.json")["min_words"] == mb.min_words
    story = Story.model_validate(ctx.read_json("story.json"))
    assert len(story.chapters) == 7
    assert all(130 <= c.word_count <= 150 for c in story.chapters)
    shorts = [ShortsPlanItem.model_validate(i) for i in ctx.read_json("shorts_plan.json")]
    assert [s.chapter for s in shorts] == [1, 3, 5]  # 3 of the 4 a normal install wants
    row = ctx.db.one("select word_count from stories where job_id=?", (ctx.job_id,))
    assert row is not None and row["word_count"] == sum(c.word_count for c in story.chapters)
    first = next(c for c in ctx.services.llm.calls if c["prompt_name"] == "chapter_draft_1")  # type: ignore[attr-defined]
    b = chapter_budget(mb.chapter_budgets[0], 155, profile=profiles().TINY)
    assert f"between {b.min_words} and {b.max_words})" in first["user"]
    assert "grade 3-8" in first["system"] and "grade 5-8" not in first["system"]


def test_a_model_that_writes_less_than_the_tiny_scale_still_fails_with_e303(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    little = chapter_text(3, 6)  # about 60 words: under half of a 178-word chapter
    ctx = master_ctx(monkeypatch, tmp_home, tiny=True, texts={3: little})
    with pytest.raises(NsError) as ei:
        run_job(ctx)
    assert ei.value.code == "NS-E303"
    mb = master_budget(11, "standard", profile=profiles().TINY)
    b = chapter_budget(mb.chapter_budgets[2], 155, profile=profiles().TINY)
    assert word_count(little) < b.min_words
    assert f"length: {word_count(little)} words, allowed {b.min_words}-{b.max_words}" in ei.value.detail


def test_a_reading_level_under_the_tiny_floor_still_fails_with_e303(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    fragments = " ".join([f"Go {w}. Go {w}. Go {w}." for w in ("home", "now", "back", "down")] * 3)
    text = "\n\n".join([fragments] * 3)  # long enough, but grade under -2
    assert word_count(text) >= 90 and fk_grade(text) < -2.0
    ctx = master_ctx(monkeypatch, tmp_home, tiny=True, texts={2: text})
    with pytest.raises(NsError) as ei:
        run_job(ctx)
    assert ei.value.code == "NS-E303" and "reading_level" in ei.value.detail


def test_the_same_model_with_the_switch_off_fails_the_normal_thresholds(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = master_ctx(monkeypatch, tmp_home, tiny=False)
    with pytest.raises(NsError) as ei:
        run_job(ctx)
    assert ei.value.code == "NS-E303"
    mb = master_budget(11, "standard")
    b = chapter_budget(distribute_words(mb.target_words, 7)[0], 155)
    first = f"length: {word_count(chapter_text(1))} words, allowed {b.min_words}-{b.max_words}"
    assert first in ei.value.detail and "reading_level: grade" in ei.value.detail


class ReviseLlm(FakeLlm):
    """Answers `revise` with a chapter of 13 plain sentences (about 130 words), the scale a small model writes."""

    def complete_text(
        self,
        *,
        prompt_name: str,
        system: str,
        user: str,
        thinking: bool = False,
        temperature: float = 0.8,
        max_tokens: int = 2048,
    ) -> str:
        if prompt_name == "revise":
            self._record(prompt_name=prompt_name, system=system, user=user, thinking=thinking)
            return chapter_text(2, 13)
        return super().complete_text(prompt_name=prompt_name, system=system, user=user, thinking=thinking)


def logged(ctx: Any, event: str) -> list[dict[str, Any]]:
    """The records of `event` the job wrote to its studio log."""
    out = []
    for f in sorted(ctx.log.logs_dir.glob("studio-*.jsonl")):
        out += [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()]
    return [r for r in out if r["event"] == event]


def test_a_continuity_revision_of_a_tiny_chapter_is_validated_by_the_profile(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """The continuity stage revises a chapter and validates the new text again: that call reads the profile."""
    ctx = master_ctx(monkeypatch, tmp_home, tiny=True)
    found = {"chapter": 2, "problem": "The lamp changes colour.", "fix": "Keep it red."}
    answers = [{"ok": False, "issues": [found]}, *[{"ok": True, "issues": []}] * 5]  # six chapter pairs
    llm = ReviseLlm(FIXTURES_LLM, overrides={**ctx.services.llm.overrides, "continuity": answers})  # type: ignore[attr-defined]
    ctx.services.llm = llm
    assert run_job(ctx) == M.MASTER_STAGES
    revisions = [c for c in llm.calls if c["prompt_name"] == "revise"]
    assert len(revisions) == 1 and "Keep it red." in revisions[0]["user"]
    story = Story.model_validate(ctx.read_json("story.json"))
    assert story.chapters[1].word_count == word_count(chapter_text(2, 13)) and story.revisions == 1
    assert ctx.read_json("continuity.json")["revised"] == [2]

    ctx.runtime = Runtime.default("cpu-dev")  # the same revision with the switch off is a normal install's
    with pytest.raises(NsError) as ei:
        REGISTRY["continuity"].run(ctx)
    assert ei.value.code == "NS-E303" and "length:" in ei.value.detail and "reading_level:" in ei.value.detail


def test_the_story_is_short_warning_is_at_the_profiles_share_of_the_target(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = master_ctx(monkeypatch, tmp_home, tiny=True)
    run_job(ctx)
    plan = Plan.model_validate(ctx.read_json("plan.json"))
    words = sum(c.word_count for c in Story.model_validate(ctx.read_json("story.json")).chapters)
    assert (
        0.5 * plan.target_words <= words < 0.8 * plan.target_words
    )  # short for a normal install, not a tiny one
    assert logged(ctx, "story_short") == []

    ctx.runtime = Runtime.default("cpu-dev")
    REGISTRY["continuity"].run(ctx)  # the same story on a normal install is reported
    warned = logged(ctx, "story_short")
    assert len(warned) == 1 and warned[0]["level"] == "WARN"
    assert warned[0]["data"]["words"] == words and warned[0]["data"]["target"] == plan.target_words


@pytest.mark.parametrize("tiny", [False, True])
def test_shorts_selection_needs_four_moments_normally_and_two_when_tiny(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, tiny: bool
):
    ctx = master_ctx(monkeypatch, tmp_home, tiny=tiny, picked=(1, 4))  # the model finds two usable moments
    ctx.write_json("story.json", fake_story().model_dump())
    if tiny:
        REGISTRY["shorts_select"].run(ctx)
        assert [i["chapter"] for i in ctx.read_json("shorts_plan.json")] == [1, 4]
    else:
        with pytest.raises(NsError) as ei:
            REGISTRY["shorts_select"].run(ctx)
        assert ei.value.code == "NS-E303" and "yielded 2 usable moments, need 4" in ei.value.detail


class ExpandLlm(FakeLlm):
    """Answers `expand` with the chapter plus six more plain sentences (60 words)."""

    def complete_text(
        self,
        *,
        prompt_name: str,
        system: str,
        user: str,
        thinking: bool = False,
        temperature: float = 0.8,
        max_tokens: int = 2048,
    ) -> str:
        if prompt_name == "expand":
            return chapter_text(1) + " " + " ".join(body_sentences(5, 6))
        return super().complete_text(prompt_name=prompt_name, system=system, user=user, thinking=thinking)


@pytest.mark.parametrize("tiny", [False, True])
def test_expansion_accepts_a_smaller_addition_when_tiny(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, tiny: bool
):
    """`expand_chapter` wants half of the words it asked for; a 1 to 2 GB model adds about a quarter of them."""
    ctx = master_ctx(monkeypatch, tmp_home, tiny=tiny)
    ctx.write_json("story.json", fake_story().model_dump())
    ctx.services.llm = ExpandLlm(FIXTURES_LLM)
    if tiny:
        new = M.expand_chapter(ctx, 1, 200)  # asked 200, got 60: 50 would do
        assert new.word_count == word_count(chapter_text(1)) + 60
    else:
        with pytest.raises(NsError) as ei:
            M.expand_chapter(ctx, 1, 200)  # half of 200 is 100
        assert ei.value.code == "NS-E303" and "added 60 words, wanted 200" in ei.value.detail


# --------------------------------------------------------------------------------- the Shorts text stages
def short_ctx(tmp_home: Path, *, tiny: bool, sentences: int):
    ctx = make_ctx(tmp_home)
    if tiny:
        ctx.runtime = tiny_runtime(ctx.runtime)
    ctx.services.llm = FakeLlm(FIXTURES_LLM, overrides={"critic": GOOD_CRITIC})
    text = story_text(sentences)
    ctx.write_json("story.json", {"text": text, "word_count": word_count(text), "revisions": 0})
    return ctx


def test_a_tiny_short_story_passes_critic_revise(tmp_home: Path):
    ctx = short_ctx(tmp_home, tiny=True, sentences=5)  # 50 words, grade 0.8: inside the band
    REGISTRY["critic_revise"].run(ctx)
    assert ctx.read_json("story.json")["critic_avg"] == pytest.approx(9.0)
    count = ctx.db.one("select count(*) c from stories")
    assert count is not None and count["c"] == 1


@pytest.mark.parametrize(
    ("tiny", "sentences", "rules"), [(True, 3, ["length"]), (False, 5, ["length", "reading_level"])]
)
def test_critic_revise_keeps_failing_what_the_profile_says_is_too_little(
    tmp_home: Path, tiny: bool, sentences: int, rules: list[str]
):
    ctx = short_ctx(tmp_home, tiny=tiny, sentences=sentences)
    with pytest.raises(NsError) as ei:
        REGISTRY["critic_revise"].run(ctx)
    assert ei.value.code == "NS-E303"
    assert [r for r in ("length", "reading_level") if f"{r}:" in ei.value.detail] == rules


@pytest.mark.parametrize("tiny", [False, True])
def test_the_short_story_budget_comes_from_the_profile(tmp_home: Path, tiny: bool):
    from ns_studio.text.stages_shorts import ideas_budget

    ctx = make_ctx(tmp_home)
    if tiny:
        ctx.runtime = tiny_runtime(ctx.runtime)
    want = short_budget(50, "standard", profile=profiles().TINY if tiny else profiles().NORMAL)
    assert ideas_budget(ctx) == want


# ----------------------------------------------------------------------------------------------- prompts
@pytest.mark.parametrize(
    ("tiny", "grade", "long_beats", "story_beats"),
    [(False, "5-8", "2-4", "4-7"), (True, "3-8", "1-2", "3-4")],
)
def test_prompts_read_the_profile_and_say_todays_words_when_it_is_off(
    tmp_home: Path, tiny: bool, grade: str, long_beats: str, story_beats: str
):
    from ns_studio.text import prompts as P

    sp = profiles()
    ctx = make_ctx(tmp_home)
    if tiny:
        ctx.runtime = tiny_runtime(ctx.runtime)
    common = P._common_vars(ctx)
    assert common["story_profile"] is (sp.TINY if tiny else sp.NORMAL)
    idea = {"premise": "P.", "twist": "T.", "setting": "S", "protagonist": "R", "why_chapters": "W."}
    plan = {"beats": [{"name": "a", "summary": "b"}], "ending_line_idea": "e"}
    draft = P.render(P.load_prompt("story_draft"), **common, plan=plan, budget=short_budget(50, "standard"))
    assert f"Reading level: grade {grade}. No sentence longer than 30 words." in draft
    lp = P.render(P.load_prompt("long_plan"), **common, idea=idea, budget=master_budget(10, "standard"))
    assert f"beats ({long_beats} concrete events)" in lp
    out = P.render(P.load_prompt("story_plan"), **common, idea=idea, budget=short_budget(50, "standard"))
    assert f"{story_beats} beats: setup" in out
    system = P.render_system(P.load_prompt("chapter_draft"), **common)
    assert f"Reading level grade {grade}, no sentence longer than 30 words." in system


def test_a_prompt_rendered_without_a_profile_says_what_it_always_said(tmp_home: Path):
    """Callers that do not pass `story_profile` (tests, the clipper, owner scripts) get the normal wording."""
    from ns_studio.text import prompts as P

    ctx = make_ctx(tmp_home)
    idea = {"premise": "P.", "twist": "T.", "setting": "S", "protagonist": "R", "why_chapters": "W."}
    common = {k: v for k, v in P._common_vars(ctx).items() if k != "story_profile"}
    out = P.render(P.load_prompt("long_plan"), **common, idea=idea, budget=master_budget(10, "standard"))
    assert "beats (2-4 concrete events)" in out
    for name in ("long_plan", "story_draft", "critic", "revise", "ideas", "expand"):
        p = P.load_prompt(name)
        assert P.render_system(p, **common) == p.system  # a shipped system text is no template
    system = P.render_system(P.load_prompt("chapter_draft"), **common)
    assert "Reading level grade 5-8, no sentence longer than 30 words." in system


# --------------------------------------------------------------------------- the media floor and workshop
@dataclass
class Job:
    """All `floor_s` needs of a job context: the install's runtime and whether the script is the owner's."""

    runtime: Runtime
    owner_script: bool = False


def test_the_duration_floor_is_the_profiles_and_never_under_qc():
    import ns_studio.media.master as MM
    from ns_studio.media.qc import LONG_RANGE_S

    sp = profiles()
    ctx = Job(Runtime.default("cpu-dev"))
    assert MM.floor_s(ctx) == MM.FLOOR_S == 540.0
    ctx.runtime = tiny_runtime()
    # the 30 s planning margin goes, QC's 8:30 stays and a few seconds over it keep a master off QC's edge
    assert MM.floor_s(ctx) == sp.TINY.master_floor_s
    assert LONG_RANGE_S[0] + 5 <= MM.floor_s(ctx) < MM.FLOOR_S


def test_an_owners_own_script_keeps_the_normal_floor_on_a_tiny_install():
    import ns_studio.media.master as MM

    ctx = Job(tiny_runtime(), owner_script=True)
    assert MM.floor_s(ctx) == MM.FLOOR_S == 540.0  # not a model-size problem: 8:30 to 9:00 still fails


def test_series_parts_are_planned_in_more_and_smaller_chapters_when_tiny(tmp_home: Path):
    from ns_common.db import Db
    from ns_common.logging import NsLogger
    from ns_common.paths import Paths
    from ns_studio.context import FAKEABLE, build_services
    from ns_studio.series import workshop as W

    sp = profiles()
    assert not hasattr(W, "CHAPTER_WORDS") and not hasattr(W, "HARD_FLOOR")  # the profile is the one place
    assert W.chapter_count(15 * 155) == 7 and W.chapter_count(15 * 155, profile=sp.NORMAL) == 7
    assert [W.chapter_count(m * 155, profile=sp.TINY) for m in (9, 12, 15, 25)] == [7, 10, 12, 12]

    paths = Paths.from_env()
    paths.ensure()
    runtime = Runtime.default("cpu-dev")
    db = Db(paths.db_path)
    db.migrate()
    services = build_services(runtime, fake=set(FAKEABLE), paths=paths)
    ws = W.Workshop(paths, services, runtime, db, NsLogger("studio", paths.logs, echo=False), None)
    target = 15 * 155
    drafted = [
        {"n": n, "text": chapter_text(n), "word_budget": 194, "below_bar": False, "avg": 9.0}
        for n in range(1, 13)
    ]
    total = sum(word_count(c["text"]) for c in drafted)  # about 1,660 words: 71 % of the target
    assert 0.4 * target < total < 0.75 * target
    with pytest.raises(NsError) as ei:
        ws._check_part(drafted, target, 155, total)
    assert ei.value.code == "NS-E303" and f"part has {total} words, the target is {target}" in ei.value.detail
    notes = ws._check_part(drafted, target, 155, total, profile=sp.TINY)
    assert not [n for n in notes if n.startswith("chapter")]  # every chapter fits the tiny band
    with pytest.raises(NsError):  # and the floor is still a floor
        ws._check_part(drafted, target, 155, int(0.3 * target), profile=sp.TINY)


# ------------------------------------------------------------------ the series route end to end (write_part)
def workshop_with(tmp_home: Path, *, tiny: bool, overrides: dict[str, Any]):
    """A workshop on fakes whose runtime has the switch `tiny`, a story with 3 parts of 12 minutes mapped."""
    from ns_common.config import save_channel
    from ns_common.db import Db
    from ns_common.logging import NsLogger
    from ns_common.paths import Paths
    from ns_studio.context import FAKEABLE, build_services
    from ns_studio.series import workshop as W

    paths = Paths.from_env()
    paths.ensure()
    runtime = Runtime.default("cpu-dev")
    if tiny:
        runtime = tiny_runtime(runtime)
    services = build_services(runtime, fake=set(FAKEABLE), paths=paths)
    services.llm = FakeLlm(FIXTURES_LLM, overrides=overrides)
    db = Db(paths.db_path)
    db.migrate()
    save_channel(paths, make_channel(format="master_first"))
    ws = W.Workshop(paths, services, runtime, db, NsLogger("studio", paths.logs, echo=False), None)
    slug = ws.start("after-dark", "A night nurse's doorbell camera shows a hooded figure")
    ws.concepts(slug)
    ws.choose(slug, "A")
    ws.map_parts(slug, 3, 12)
    return ws, slug


def calls_named(ws: Any, name: str) -> list[dict[str, Any]]:
    return [c for c in ws.services.llm.calls if c["prompt_name"] == name]


def test_a_series_part_at_the_tiny_scale_is_written_when_tiny_and_refused_when_not(tmp_home: Path):
    """5 chapters of about 188 words are 50 % of a 12 minute part (1,860 words): the floor is 40 % or 75 %."""
    sp = profiles()
    small = {
        f"chapter_draft_{n}": {"text": chapter_text(n, 19), "summary": "Two plain facts."}
        for n in range(1, 6)
    }
    overrides = {**small, "shorts_select": {"moments": moments((1, 2))}}
    total = sum(word_count(chapter_text(n, 19)) for n in range(1, 6))
    assert 0.4 * 1860 < total < 0.75 * 1860  # under the normal hard floor, over the tiny one

    ws, slug = workshop_with(tmp_home, tiny=True, overrides=overrides)
    summary = ws.write_part(slug, 1)
    assert summary["chapters"] == 5 and len(summary["shorts"]) == 2  # a normal install wants 4
    assert not [w for w in summary["warnings"] if w.startswith("chapter")]  # every chapter fits the tiny band
    outline = calls_named(ws, "workshop_part_outline")[0]["user"]
    assert "in about 10 chapters of roughly 186 words" in outline  # 1,860 words in chapters of 190, not 350
    b = chapter_budget(372, 155, profile=sp.TINY)
    assert f"between {b.min_words} and {b.max_words})" in calls_named(ws, "chapter_draft_1")[0]["user"]

    ws, slug = workshop_with(tmp_home, tiny=False, overrides=overrides)
    with pytest.raises(NsError) as ei:
        ws.write_part(slug, 1)
    assert ei.value.code == "NS-E303" and "target is 1860" in ei.value.detail
    assert "in about 5 chapters of roughly 372 words" in calls_named(ws, "workshop_part_outline")[0]["user"]


@pytest.mark.parametrize("tiny", [False, True])
def test_a_series_part_needs_four_standalone_shorts_normally_and_two_when_tiny(tmp_home: Path, tiny: bool):
    two = json.loads((FIXTURES_LLM / "shorts_select.json").read_text(encoding="utf-8"))["moments"][:2]
    ws, slug = workshop_with(tmp_home, tiny=tiny, overrides={"shorts_select": {"moments": two}})
    if tiny:
        assert len(ws.write_part(slug, 1)["shorts"]) == 2
    else:
        with pytest.raises(NsError) as ei:
            ws.write_part(slug, 1)
        assert ei.value.code == "NS-E303" and "need 4" in ei.value.detail
