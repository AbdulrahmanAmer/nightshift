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
from ns_studio.context import FIXTURES_LLM
from ns_studio.orchestrator import FORMAT_STAGES, run_job
from ns_studio.policy import final_checks as fc
from ns_studio.policy import judge, text_checks
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
def _policy_on(monkeypatch: pytest.MonkeyPatch, fmt: str = "shorts_only") -> None:
    monkeypatch.setitem(FORMAT_STAGES, fmt, [*FORMAT_STAGES[fmt], TEXT_STAGE])


def test_chosen_idea_is_classified_before_planning(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    _policy_on(monkeypatch)
    ctx = make_ctx(tmp_home)
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


def test_all_ideas_unsafe_is_e601(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    _policy_on(monkeypatch)
    ctx = make_ctx(tmp_home)
    ctx.services.llm = _llm(_pc(unsafe_categories=["hate"], notes="slur"))
    with pytest.raises(NsError) as ei:
        REGISTRY["ideas"].run(ctx)
    assert ei.value.code == "NS-E601"
    assert not ctx.file("idea.json").exists()


def test_banked_idea_is_classified_too(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    _policy_on(monkeypatch)
    ctx = make_ctx(tmp_home)
    ctx.db.execute(
        "insert into ideas(channel, premise, twist, created_at, status) values (?,?,?,?,?)",
        (ctx.channel.slug, "a banked premise", "a banked twist", "2026-10-01", "approved"),
    )
    ctx.services.llm = _llm(_pc(unsafe_categories=["minors"], notes="child in danger"))
    with pytest.raises(NsError) as ei:
        REGISTRY["ideas"].run(ctx)
    assert ei.value.code == "NS-E601" and "a banked premise" in str(ei.value)


def test_idea_is_not_classified_without_policy_gates(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    assert TEXT_STAGE not in FORMAT_STAGES["shorts_only"]
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


def test_audio_check_fails_a_music_bed_quieter_than_the_floor_test(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    bed = _speechlike(_tone(-62) + _tone(-66, hz=660.0))
    res = fc.AudioOnlyVoice().run(_audio_ctx(tmp_home, monkeypatch, bed))
    assert not res.passed and "tonal" in res.detail


def test_audio_check_passes_room_tone_and_digital_silence(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    for gap in (_noise(-62), np.zeros(SR // 2)):
        res = fc.AudioOnlyVoice().run(_audio_ctx(tmp_home, monkeypatch, _speechlike(gap)))
        assert res.passed, res.detail
