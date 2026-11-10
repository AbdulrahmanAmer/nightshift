# ruff: noqa: E501
"""P04: policy gates (spec 18.3, 18.7, 18.8): F-H59, F-H105, F-H106, F-H114, F-H115, F-H116.

No test here needs FFmpeg or a model: audio is synthetic numpy, the LLM is `FakeLlm`, spaCy is faked.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import ns_studio.text.stages_shorts  # noqa: F401  (registers the shorts stages)
from ns_common.errors import NsError
from ns_common.llm_client import FakeLlm
from ns_studio import jobs
from ns_studio.context import FIXTURES_LLM, JobContext
from ns_studio.orchestrator import FORMAT_STAGES, run_job
from ns_studio.policy import final_checks as fc
from ns_studio.policy import idea_check, judge, text_checks
from ns_studio.policy.gate import FINAL_STAGE, TEXT_STAGE, PolicyFinalStage
from ns_studio.stages import REGISTRY
from ns_studio.text import prompts as P
from ns_studio.text import stages_master as M
from tests.helpers import make_channel, make_ctx

CHANNEL = {"format": "master_first", "long_minutes": (11, 11)}


def _pc(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "unsafe_categories": [],
        "hook_honest": True,
        "title_honest": True,
        "standalone": None,
        "notes": "",
        "intensity": "low",
        "real_setting": False,
    }
    return base | kw


def _llm(answers: Any) -> FakeLlm:
    return FakeLlm(FIXTURES_LLM, overrides={"policy_check": answers})


class ExpandLlm(FakeLlm):
    """FakeLlm whose `expand` answers with the shipped text plus one extra sentence."""

    def __init__(self, extra: str, **kw: Any) -> None:
        super().__init__(FIXTURES_LLM, **kw)
        self.extra = extra

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
        text = super().complete_text(prompt_name=prompt_name, system=system, user=user, thinking=thinking)
        return f"{text.rstrip()} {self.extra}" if prompt_name == "expand" else text


def _master_job(monkeypatch: pytest.MonkeyPatch, tmp_home: Path):
    monkeypatch.setitem(FORMAT_STAGES, "master_first", list(M.MASTER_STAGES))
    ctx = make_ctx(tmp_home, fmt="master_first", channel=make_channel(**CHANNEL))
    run_job(ctx)
    return ctx


# ---------------------------------------------------------------- F-H59 expansion text is policy-checked
def test_expanded_chapter_is_judged_when_the_text_gate_ran(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = _master_job(monkeypatch, tmp_home)
    ctx.write_json("policy_text.json", {"stage": "text", "passed": True, "checks": {}, "advice": {}})
    before = ctx.file("story.json").read_bytes()
    ctx.services.llm = _llm(_pc(unsafe_categories=["minors"], notes="a child is hurt"))
    with pytest.raises(NsError) as ei:
        M.expand_chapter(ctx, 1, 120)
    assert ei.value.code == "NS-E601" and "minors" in str(ei.value)
    assert ctx.file("story.json").read_bytes() == before  # the unsafe text never reaches the story
    asked = [c for c in ctx.services.llm.calls if c["prompt_name"] == "policy_check"]  # type: ignore[attr-defined]
    assert asked and "doorbell camera pings at 3:12" in asked[0]["user"]


def test_expanded_chapter_with_a_blocked_name_or_claim_is_refused(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = _master_job(monkeypatch, tmp_home)
    ctx.write_json("policy_text.json", {"stage": "text", "passed": True, "checks": {}, "advice": {}})
    before = ctx.file("story.json").read_bytes()
    ctx.services.llm = ExpandLlm("Then Donald Trump walked into the room.")
    with pytest.raises(NsError) as ei:
        M.expand_chapter(ctx, 1, 120)
    assert ei.value.code == "NS-E602"
    ctx.services.llm = ExpandLlm("I swear this is a true story.")
    with pytest.raises(NsError) as ei2:
        M.expand_chapter(ctx, 1, 120)
    assert ei2.value.code == "NS-E603"
    assert ctx.file("story.json").read_bytes() == before


class KeepOldLlm(FakeLlm):
    """FakeLlm whose `expand` answers with the chapter it was given plus `extra` (old sentences verbatim)."""

    def __init__(self, old_text: str, extra: str, **kw: Any) -> None:
        super().__init__(FIXTURES_LLM, **kw)
        self.old_text, self.extra = old_text, extra

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
            return f"{self.old_text} {self.extra}"
        return super().complete_text(prompt_name=prompt_name, system=system, user=user, thinking=thinking)


NEUTRAL_EXTRA = (
    "The hallway stayed quiet for a long while. Nobody moved. The porch light flickered twice, and then it "
    "held steady. She counted the steps to the stairs and listened for the floor to answer. Nothing did. "
    "The kettle clicked off in the next room. He waited by the window and watched the street go dark. "
    "A car passed slowly and its lights swept across the ceiling and were gone. The house settled around them."
)


def _with_old_phrase(ctx: Any, phrase: str) -> str:
    """Put `phrase` into chapter 1 of the stored story (as if the text gate had passed it); returns its text."""
    story = M.load_story(ctx)
    old = story.chapters[0]
    text = f"{phrase} {old.text}"
    story.chapters[0] = M.build_chapter(
        old.n, old.title, text, old.summary, wpm=M._wpm(ctx), is_last=len(story.chapters) == 1
    )
    ctx.write_json("story.json", story.model_dump())
    return text


def test_old_sentences_with_a_scanned_phrase_do_not_fail_an_expansion(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """The text gate scans metadata for claim phrases, not the story: old sentences must not trip E603 later."""
    ctx = _master_job(monkeypatch, tmp_home)
    ctx.write_json("policy_text.json", {"stage": "text", "passed": True, "checks": {}, "advice": {}})
    old_text = _with_old_phrase(
        ctx,
        "The ceiling leaked again. On the radio a news report droned. Nothing in real life felt this wrong.",
    )
    ctx.services.llm = KeepOldLlm(old_text, NEUTRAL_EXTRA)
    new = M.expand_chapter(ctx, 1, 40)
    assert "ceiling leaked again" in new.text and "porch light flickered twice, and then" in new.text


def test_a_scanned_phrase_in_the_new_sentences_still_fails_an_expansion(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = _master_job(monkeypatch, tmp_home)
    ctx.write_json("policy_text.json", {"stage": "text", "passed": True, "checks": {}, "advice": {}})
    old_text = _with_old_phrase(ctx, "The ceiling leaked again.")
    ctx.services.llm = KeepOldLlm(old_text, NEUTRAL_EXTRA + " It was caught on camera.")
    with pytest.raises(NsError) as ei:
        M.expand_chapter(ctx, 1, 40)
    assert (
        ei.value.code == "NS-E603" and "caught on camera" in str(ei.value) and "leaked" not in str(ei.value)
    )


def test_expanded_chapter_that_passes_is_kept_and_notes_merge(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = _master_job(monkeypatch, tmp_home)
    ctx.write_json("policy_text.json", {"stage": "text", "passed": True, "checks": {}, "advice": {}})
    ctx.write_json("policy_notes.json", {"intensity": "low", "real_setting": False})
    ctx.services.llm = _llm(_pc(intensity="high"))
    new = M.expand_chapter(ctx, 1, 120)
    assert new.word_count > 0
    assert ctx.read_json("policy_notes.json")["intensity"] == "high"  # the expansion made the story darker


def test_expansion_without_a_text_gate_asks_nothing(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """--no-policy: no `policy_text.json`, so no policy call (the old behaviour)."""
    ctx = _master_job(monkeypatch, tmp_home)
    ctx.services.llm = _llm(_pc(unsafe_categories=["minors"]))
    M.expand_chapter(ctx, 1, 120)
    assert [c for c in ctx.services.llm.calls if c["prompt_name"] == "policy_check"] == []  # type: ignore[attr-defined]


# ---------------------------------------------------------------- F-H105 ideas, prompts, final re-check
def _policy_on(home: Path) -> JobContext:
    """A Short whose policy is on: the gates are in the stage table since the stage modules loaded."""
    jobs.load_stage_modules()
    return make_ctx(home, policy=True)


def test_chosen_idea_is_classified_before_planning(tmp_home: Path):
    ctx = _policy_on(tmp_home)
    ctx.services.llm = _llm([_pc(unsafe_categories=["graphic_harm"], notes="gore"), _pc()])
    REGISTRY["ideas"].run(ctx)
    calls = ctx.services.llm.calls  # type: ignore[attr-defined]
    names = [c["prompt_name"] for c in calls]
    assert names.count("policy_check") == 2  # the first survivor was flagged, the second was taken
    chosen = ctx.read_json("idea.json")
    assert chosen["premise"] == ctx.read_json("ideas_all.json")[1]["premise"]
    assert chosen["premise"] in [c for c in calls if c["prompt_name"] == "policy_check"][1]["user"]
    row = ctx.db.one("select premise from ideas where used=1 and job_id=?", (ctx.job_id,))
    assert row["premise"] == chosen["premise"]


def test_all_ideas_unsafe_is_e601(tmp_home: Path):
    ctx = _policy_on(tmp_home)
    ctx.services.llm = _llm(_pc(unsafe_categories=["hate"], notes="slur"))
    with pytest.raises(NsError) as ei:
        REGISTRY["ideas"].run(ctx)
    assert ei.value.code == "NS-E601"
    assert not ctx.file("idea.json").exists()


def test_banked_idea_is_classified_too(tmp_home: Path):
    ctx = _policy_on(tmp_home)
    ctx.db.execute(
        "insert into ideas(channel, premise, twist, created_at, status) values (?,?,?,?,?)",
        (ctx.channel.slug, "a banked premise", "a banked twist", "2026-10-01", "approved"),
    )
    ctx.services.llm = _llm(_pc(unsafe_categories=["minors"], notes="child in danger"))
    with pytest.raises(NsError) as ei:
        REGISTRY["ideas"].run(ctx)
    assert ei.value.code == "NS-E601" and "a banked premise" in str(ei.value)


def test_idea_is_not_classified_without_policy_gates(tmp_home: Path):
    """A job with its policy off: the gates are in the stage table, its own plan leaves them out."""
    jobs.load_stage_modules()
    ctx = make_ctx(tmp_home, policy=False)
    assert TEXT_STAGE in FORMAT_STAGES["shorts_only"] and not idea_check.enabled(ctx)
    ctx.services.llm = _llm(_pc(unsafe_categories=["hate"]))
    REGISTRY["ideas"].run(ctx)
    assert [c["prompt_name"] for c in ctx.services.llm.calls] == ["ideas"]  # type: ignore[attr-defined]


@pytest.mark.parametrize("name", ["story_plan", "story_draft", "chapter_draft"])
def test_draft_prompts_carry_the_policy_rules_and_a_new_version(name: str):
    p = P.load_prompt(name)
    assert p.version == 2
    assert "{{ policy_rules }}" in (P.PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def test_draft_prompts_render_the_rules(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    P.call_text(ctx, "story_draft", plan={"beats": [], "ending_line_idea": "x"}, budget=_budget())
    user = ctx.services.llm.calls[-1]["user"]  # type: ignore[attr-defined]
    assert "Content rules" in user and "Never claim the story is real" in user
    P.call_json(
        ctx,
        "story_plan",
        idea={"premise": "p", "twist": "t", "setting": "s", "protagonist": "q"},
        budget=_budget(),
    )
    assert "Never claim the story is real" in ctx.services.llm.calls[-1]["user"]  # type: ignore[attr-defined]


def _budget() -> dict[str, Any]:
    return {"words": 100, "parts": 1, "words_per_part": 100, "min_words": 80, "max_words": 120}


def test_final_gate_has_an_unsafe_topics_recheck_over_script_and_metadata(tmp_home: Path):
    names = [c.name for c in fc.final_checks()]
    assert names == [
        "unsafe_topics",
        "render_uniqueness",
        "long_runtime",
        "clip_reuse",
        "audio_only_voice",
        "short_hook",
        "disclosure_advice",
    ]
    assert fc.FinalUnsafeTopics.gate == "final"
    ctx = make_ctx(tmp_home)
    ctx.write_json("story.json", {"text": "A quiet house at night."})
    ctx.write_json(
        "seo.json",
        [{"title": "Title with the secret word zebra", "description": "d", "tags": ["a"], "hashtags": []}],
    )
    seen: dict[str, Any] = {}

    def spy(c: Any, mode: str, **v: Any) -> dict[str, Any]:
        seen.update(v, mode=mode)
        return {
            "verdict": "fail",
            "reason": "graphic",
            "flags": [{"category": "graphic_harm", "reason": "g"}],
            "intensity": "high",
            "real_setting": False,
        }

    res = fc.FinalUnsafeTopics(spy).run(ctx)
    assert not res.passed and "graphic_harm" in res.detail
    assert seen["mode"] == "unsafe_topics" and "A quiet house at night." in seen["story"]
    assert "zebra" in seen["story"]  # the metadata is part of what was judged
    assert fc.FinalUnsafeTopics().code == "NS-E601"


def test_final_recheck_passes_and_merges_the_notes(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("story.json", {"text": "A quiet house at night."})
    ctx.write_json("policy_notes.json", {"intensity": "medium", "real_setting": False})

    def calm(c: Any, mode: str, **v: Any) -> dict[str, Any]:
        return {"verdict": "pass", "reason": "", "flags": [], "intensity": "low", "real_setting": None}

    assert fc.FinalUnsafeTopics(calm).run(ctx).passed
    notes = ctx.read_json("policy_notes.json")
    assert notes["intensity"] == "medium"  # never lowered by a later, calmer reading
    assert notes["real_setting"] is None  # an unclear answer beats an earlier "no"


def test_final_stage_leases_the_llm():
    assert PolicyFinalStage.tenant == "llama" and FINAL_STAGE == "policy_final"


# ---------------------------------------------------------------- F-H106 intensity
def test_default_judge_returns_the_intensity(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.services.llm = _llm(_pc(intensity="high"))
    assert judge.default_judge(ctx, "unsafe_topics", story="S")["intensity"] == "high"
    ctx.services.llm = _llm(_pc(intensity="extreme"))  # not one of low|medium|high
    assert judge.default_judge(ctx, "unsafe_topics", story="S")["intensity"] == "low"
    ctx.services.llm = _llm({k: v for k, v in _pc().items() if k != "intensity"})
    assert judge.default_judge(ctx, "unsafe_topics", story="S")["intensity"] == "low"


def test_mature_channel_with_intense_story_gets_the_age_restriction_suggestion(tmp_home: Path):
    """End to end through the classifier: the text gate records, the final advice reads."""
    mature = make_channel(slug="grim", audience="mature")
    ctx = make_ctx(tmp_home, channel=mature)
    ctx.write_json("story.json", {"text": "A dark tale."})
    ctx.services.llm = _llm(_pc(intensity="high"))
    assert text_checks.UnsafeTopics().run(ctx).passed
    assert fc.compute_advice(ctx, place_ner=lambda t: []).age_restrict_suggested is True
    calm = make_ctx(tmp_home, channel=mature)
    calm.write_json("story.json", {"text": "A calm tale."})
    calm.services.llm = _llm(_pc(intensity="low"))
    text_checks.UnsafeTopics().run(calm)
    assert fc.compute_advice(calm, place_ner=lambda t: []).age_restrict_suggested is False
    general = make_ctx(tmp_home)
    general.write_json("story.json", {"text": "A dark tale."})
    general.services.llm = _llm(_pc(intensity="high"))
    text_checks.UnsafeTopics().run(general)
    assert fc.compute_advice(general, place_ner=lambda t: []).age_restrict_suggested is False


def test_policy_check_prompt_and_schema_ask_for_intensity_and_real_setting():
    p = P.load_prompt("policy_check")
    assert p.version == 3
    assert "intensity" in p.system and "real_setting" in p.system
    props = P.PROMPT_SCHEMAS["policy_check"]["properties"]
    assert props["intensity"]["enum"] == ["low", "medium", "high"]
    assert "real_setting" in props
    assert {"intensity", "real_setting"} <= set(P.PROMPT_SCHEMAS["policy_check"]["required"])


def test_studio_side_keys_do_not_break_the_shared_model():
    from ns_common.models import PolicyCheckResult

    res = P.parse_policy_check(_pc(), "policy_check")
    assert isinstance(res, PolicyCheckResult) and res.unsafe_categories == []
    with pytest.raises(NsError):
        P.parse_policy_check({"unsafe_categories": []}, "policy_check")


def test_series_text_gate_accepts_the_new_keys(tmp_home: Path):
    from ns_studio.series import policy as sp

    ctx = make_ctx(tmp_home)
    ctx.services.llm = _llm(_pc(intensity="medium", real_setting=True))
    sp.text_gate(ctx, "A quiet house.", what="part 1")


# ---------------------------------------------------------------- F-H114 real setting
def _advice(
    tmp_home: Path,
    notes: dict[str, Any] | None,
    places: list[tuple[str, str]],
    text: str = "A man heard knocking.",
):
    ctx = make_ctx(tmp_home)
    ctx.write_json("story.json", {"text": text})
    if notes is not None:
        ctx.write_json("policy_notes.json", notes)
    return fc.compute_advice(ctx, place_ner=lambda t: places).ai_disclosure


def test_disclosure_real_setting_flag(tmp_home: Path):
    assert _advice(tmp_home, {"real_setting": False}, []) == "no"
    assert _advice(tmp_home, {"real_setting": True}, []) == "yes"


def test_disclosure_unclear_is_yes(tmp_home: Path):
    assert _advice(tmp_home, {"real_setting": None}, []) == "yes"
    assert _advice(tmp_home, {}, []) == "yes"  # the classifier never answered
    assert _advice(tmp_home, None, []) == "yes"


def test_disclosure_any_place_entity_is_yes_even_for_places_not_in_the_old_list(tmp_home: Path):
    for label in ("GPE", "LOC", "FAC"):
        assert (
            _advice(tmp_home, {"real_setting": False}, [("Lisbon", label)], "He walked through Lisbon.")
            == "yes"
        )


def test_the_fixed_place_list_is_gone():
    assert not hasattr(fc, "REAL_PLACES")


def test_place_entities_keeps_only_places(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    class Ent:
        def __init__(self, text: str, label: str) -> None:
            self.text, self.label_ = text, label

    class Doc:
        ents = [
            Ent("Lisbon", "GPE"),
            Ent("Sarah", "PERSON"),
            Ent("Alfama", "LOC"),
            Ent("Ritz", "FAC"),
            Ent("Acme", "ORG"),
        ]

    monkeypatch.setitem(text_checks._NLP_CACHE, "nlp", lambda t: Doc())
    from ns_common.paths import Paths

    got = text_checks.place_entities(Paths.from_env(), "x")
    assert got == [("Lisbon", "GPE"), ("Alfama", "LOC"), ("Ritz", "FAC")]
    monkeypatch.setitem(text_checks._NLP_CACHE, "nlp", None)
    assert text_checks.place_entities(Paths.from_env(), "x") == []  # no spaCy: the classifier's flag decides


def test_text_gate_records_the_real_setting_answer(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("story.json", {"text": "He drove to Lisbon."})
    ctx.services.llm = _llm(_pc(real_setting=True))
    text_checks.UnsafeTopics().run(ctx)
    assert ctx.read_json("policy_notes.json")["real_setting"] is True
    ctx.services.llm = _llm({k: v for k, v in _pc().items() if k != "real_setting"})
    text_checks.UnsafeTopics().run(ctx)
    assert ctx.read_json("policy_notes.json")["real_setting"] is None


def test_policy_check_fixture_answers_both_new_fields():
    data = json.loads((FIXTURES_LLM / "policy_check.json").read_text(encoding="utf-8"))
    assert data["intensity"] == "low" and data["real_setting"] is False


# ---------------------------------------------------------------- F-H115 hook presence in derived Shorts
def _words(text: str) -> list[dict[str, Any]]:
    return [
        {"w": w, "start": i * 0.3, "end": i * 0.3 + 0.25, "sentence_idx": -1}
        for i, w in enumerate(text.split())
    ]


def _short(ctx: Any, k: int, hook: str, spoken: str) -> None:
    plans = ctx.read_json("shorts_plan.json") if ctx.file("shorts_plan.json").exists() else []
    plans.append({"k": k, "chapter": 1, "sentence_start": 0, "sentence_end": 2, "chosen_hook": hook})
    ctx.write_json("shorts_plan.json", plans)
    ctx.write_json(f"captions/s{k}.json", {"k": k, "words": _words(spoken)})


def test_short_must_open_with_its_hook(tmp_home: Path):
    ctx = make_ctx(tmp_home)  # fake services: dev runs are checked too
    _short(
        ctx,
        1,
        "Nobody ever answers the door.",
        "Nobody ever answers the door. The porch light flickered twice.",
    )
    assert fc.ShortHookPresent().run(ctx).passed
    _short(ctx, 2, "The camera saw something it should not.", "The porch light flickered twice. It was cold.")
    res = fc.ShortHookPresent().run(ctx)
    assert not res.passed and "s2" in res.detail and "s1" not in res.detail
    assert fc.ShortHookPresent().code == "NS-E611"


def test_hook_failure_has_its_own_hint_and_restarts_at_the_voice(tmp_home: Path):
    """short_hook shares NS-E611 with render_uniqueness: its hint must not say "visuals are being redrawn"."""
    from ns_studio.policy import regen

    ctx = make_ctx(tmp_home)
    _short(ctx, 1, "The camera saw something it should not.", "The porch light flickered twice. It was cold.")
    chk = fc.ShortHookPresent()
    assert not chk.run(ctx).passed
    hint = chk.hint(ctx) or ""
    assert "hook" in hint and "similar" not in hint and "redrawn" not in hint
    names = ["ideas", "hooks", "voice", "captions", "clips", "render", "qc"]
    assert regen.earliest_stage("NS-E611", names) == "clips"  # render_uniqueness: new footage
    assert (
        regen.earliest_stage("NS-E611", names, check="short_hook") == "voice"
    )  # a hook is voiced, not drawn


def test_hook_match_ignores_case_and_punctuation_and_one_wrong_word(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _short(ctx, 1, "Nobody ever answers the door!", "nobody ever answer the door, the porch light flickered")
    assert fc.ShortHookPresent().run(ctx).passed


def test_hook_check_has_nothing_to_do_without_derived_shorts(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    assert fc.ShortHookPresent().run(ctx).passed
    ctx.write_json(
        "shorts_plan.json",
        [{"k": 1, "chapter": 1, "sentence_start": 0, "sentence_end": 1, "chosen_hook": ""}],
    )
    assert fc.ShortHookPresent().run(ctx).passed  # no hook was chosen, nothing to assert
    ctx.write_json(
        "shorts_plan.json",
        [{"k": 1, "chapter": 1, "sentence_start": 0, "sentence_end": 1, "chosen_hook": "Hook here now."}],
    )
    assert not fc.ShortHookPresent().run(ctx).passed  # a Short was planned but its captions are missing


# ---------------------------------------------------------------- F-H116 tonal energy in the quiet frames
SR = fc.SR


def _speechlike(gap: np.ndarray, seconds: int = 6) -> np.ndarray:
    """Loud noise bursts (speech) with `gap` between them."""
    rng = np.random.default_rng(1)
    burst = 0.2 * rng.standard_normal(SR // 2)
    parts: list[np.ndarray] = []
    for _ in range(seconds):
        parts += [burst, gap]
    return np.concatenate(parts)


def _noise(db: float, n: int = SR // 2, seed: int = 2) -> np.ndarray:
    return (10 ** (db / 20)) * np.random.default_rng(seed).standard_normal(n)


def _tone(db: float, n: int = SR // 2, hz: float = 440.0) -> np.ndarray:
    return (10 ** (db / 20)) * np.sqrt(2) * np.sin(2 * np.pi * hz * np.arange(n) / SR)


def test_flatness_separates_room_tone_from_a_quiet_bed():
    room = _speechlike(_noise(-62))
    bed = _speechlike(_tone(-62) + _tone(-66, hz=660.0) + _tone(-70, hz=880.0))
    assert (
        fc.quiet_floor_db(room) < fc.BED_FLOOR_DBFS and fc.quiet_floor_db(bed) < fc.BED_FLOOR_DBFS
    )  # the old test passes both
    f_room, f_bed = fc.quiet_flatness(room), fc.quiet_flatness(bed)
    assert f_room is not None and f_bed is not None
    assert f_room > fc.TONAL_FLATNESS_MAX > f_bed


def test_digital_silence_has_no_flatness_to_measure():
    assert fc.quiet_flatness(_speechlike(np.zeros(SR // 2))) is None
    assert fc.quiet_flatness(np.zeros(10)) is None


def _audio_ctx(tmp_home: Path, monkeypatch: pytest.MonkeyPatch, samples: np.ndarray):
    ctx = make_ctx(tmp_home)
    master = ctx.file("long/master.mp4")
    master.parent.mkdir(parents=True)
    master.write_bytes(b"x")
    monkeypatch.setattr(fc.ffmpeg, "probe", lambda p: {"streams": [{"codec_type": "audio"}]})
    monkeypatch.setattr(fc, "_decode_audio", lambda p: samples)
    return ctx


QC_FLATNESS = "qc/audio_flatness.json"


def _bed() -> np.ndarray:
    return _speechlike(_tone(-62) + _tone(-66, hz=660.0))


def test_a_tonal_quiet_passage_is_a_warning_not_a_failure(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """F-H116 is "measured, warning only": the value is not calibrated on qwen-tts, so it never stops a night."""
    ctx = _audio_ctx(tmp_home, monkeypatch, _bed())
    res = fc.AudioOnlyVoice().run(ctx)
    assert res.passed, res.detail
    assert "WARNING" in res.detail and "quiet_flatness" in res.detail
    assert "NS-E614" not in res.detail


def test_the_measured_flatness_is_in_the_qc_report_and_the_log_when_it_passes(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = _audio_ctx(tmp_home, monkeypatch, _speechlike(_noise(-62)))
    res = fc.AudioOnlyVoice().run(ctx)
    assert res.passed and "WARNING" not in res.detail
    measured = fc.quiet_flatness(_speechlike(_noise(-62)))
    assert measured is not None and measured > fc.TONAL_FLATNESS_MAX
    entry = ctx.read_json(QC_FLATNESS)["items"]["long"]
    assert entry["quiet_flatness"] == pytest.approx(measured, abs=1e-4) and entry["warned"] is False
    assert ctx.read_json(QC_FLATNESS)["warn_below"] == fc.TONAL_FLATNESS_MAX
    assert f"{measured:.3f}" in res.detail  # the gate's own report (policy_final.json) carries it too
    logged = [r for r in _log_records(ctx) if r["event"] == "audio_flatness"]
    assert logged and logged[-1]["level"] == "INFO"
    assert logged[-1]["data"]["quiet_flatness"] == pytest.approx(measured, abs=1e-4)


def test_the_measured_flatness_is_in_the_qc_report_and_the_log_when_over_the_threshold(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = _audio_ctx(tmp_home, monkeypatch, _bed())
    res = fc.AudioOnlyVoice().run(ctx)
    measured = fc.quiet_flatness(_bed())
    assert measured is not None and measured < fc.TONAL_FLATNESS_MAX
    entry = ctx.read_json(QC_FLATNESS)["items"]["long"]
    assert entry["quiet_flatness"] == pytest.approx(measured, abs=1e-4) and entry["warned"] is True
    assert f"{measured:.3f}" in res.detail
    warned = [r for r in _log_records(ctx) if r["event"] == "audio_flatness_warning"]
    assert warned and warned[-1]["level"] == "WARN" and "code" not in warned[-1]["data"]  # no NS-W code fits
    assert warned[-1]["data"]["quiet_flatness"] == pytest.approx(measured, abs=1e-4)


def test_flatness_with_nothing_to_measure_is_recorded_as_null(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = _audio_ctx(tmp_home, monkeypatch, _speechlike(np.zeros(SR // 2)))
    res = fc.AudioOnlyVoice().run(ctx)
    assert res.passed and "WARNING" not in res.detail
    assert ctx.read_json(QC_FLATNESS)["items"]["long"]["quiet_flatness"] is None


def test_the_threshold_is_one_constant(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = _audio_ctx(tmp_home, monkeypatch, _speechlike(_noise(-62)))
    assert fc.AudioOnlyVoice().run(ctx).passed and "WARNING" not in fc.AudioOnlyVoice().run(ctx).detail
    monkeypatch.setattr(fc, "TONAL_FLATNESS_MAX", 0.99)  # the only place the limit lives
    res = fc.AudioOnlyVoice().run(ctx)
    assert res.passed and "WARNING" in res.detail
    assert ctx.read_json(QC_FLATNESS)["warn_below"] == 0.99


def test_the_qc_file_does_not_disturb_the_channel_pass_rate(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """channel_stats reads every qc/*.json that has a boolean `passed`: this file must not have one."""
    ctx = _audio_ctx(tmp_home, monkeypatch, _bed())
    ctx.write_json("qc/long.json", {"passed": True, "checks": {}})  # a real per-item QC verdict next to it
    assert fc.AudioOnlyVoice().run(ctx).passed
    written = ctx.file(QC_FLATNESS)
    assert written.exists()  # without the file this test would pass on code that never writes it
    assert "passed" not in ctx.read_json(QC_FLATNESS)
    from ns_dashboard.channel_stats import _verdicts

    assert _verdicts(ctx.file("qc")) == [True]  # the flatness file adds no verdict of its own


def test_a_stale_flatness_file_is_removed_when_no_render_could_be_measured(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = _audio_ctx(tmp_home, monkeypatch, _bed())
    assert fc.AudioOnlyVoice().run(ctx).passed and ctx.file(QC_FLATNESS).exists()
    two_streams = {"streams": [{"codec_type": "audio"}, {"codec_type": "audio"}]}
    monkeypatch.setattr(fc.ffmpeg, "probe", lambda p: two_streams)
    res = fc.AudioOnlyVoice().run(ctx)
    assert not res.passed and "2 audio streams" in res.detail
    assert not ctx.file(QC_FLATNESS).exists()  # its values belonged to an earlier pass's renders


def test_a_floor_failure_still_fails_and_still_measures_the_flatness(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """The loud half (no silence anywhere, BED_FLOOR_DBFS) is unchanged: that one is a failure."""
    loud_bed = _speechlike(_tone(-30) + _tone(-34, hz=660.0))
    ctx = _audio_ctx(tmp_home, monkeypatch, loud_bed)
    res = fc.AudioOnlyVoice().run(ctx)
    assert not res.passed and "no silence anywhere" in res.detail
    assert "quiet_flatness" in ctx.read_json(QC_FLATNESS)["items"]["long"]


def test_audio_check_passes_room_tone_and_digital_silence(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    for gap in (_noise(-62), np.zeros(SR // 2)):
        res = fc.AudioOnlyVoice().run(_audio_ctx(tmp_home, monkeypatch, _speechlike(gap)))
        assert res.passed, res.detail


def _log_records(ctx: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in sorted(ctx.paths.logs.glob("*.jsonl")):
        out += [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    return out


def test_a_failed_hook_check_restarts_the_run_at_the_voice_not_the_clips(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """run_job_guarded reads the failing check from the error: short_hook redoes the voice, not the footage."""
    from ns_studio import orchestrator as orch
    from ns_studio.policy import regen
    from tests.test_policy_gate import Counter, FlakyGate

    class HookGate(FlakyGate):
        def run(self, ctx: Any) -> None:
            n = self.counts.get("gate_x", 0)
            self.counts["gate_x"] = n + 1
            if n < 1:
                raise NsError("NS-E611", "short_hook: s1 opens with the wrong words", check="short_hook")
            ctx.write_json("gate_x.json", {"ok": True})

    counts: dict[str, int] = {}
    plan = ["ideas", "voice", "clips", "gate_x"]
    for name in plan[:3]:
        monkeypatch.setitem(REGISTRY, name, Counter(name, "llama", counts))
    monkeypatch.setitem(REGISTRY, "gate_x", HookGate("NS-E611", 1, counts))
    monkeypatch.setitem(orch.FORMAT_STAGES, "rg", plan)
    regen.run_job_guarded(make_ctx(tmp_home, fmt="rg"))
    assert counts == {"ideas": 1, "voice": 2, "clips": 2, "gate_x": 2}
