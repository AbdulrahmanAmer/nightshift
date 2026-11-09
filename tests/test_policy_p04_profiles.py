"""P04 policy gates under both story profiles (H-8 merged into this branch).

`StoryProfile` (ns_studio/text/story_profile.py) moves the thresholds of the text stages for an install made
for testing with a 1 to 2 GB model (`tiny_story`). Its own docstring promises that the policy gates and the
safety checks do not read it. These tests run the P04 checks (F-H59, F-H105, F-H106, F-H114, F-H115, F-H116)
with the profile OFF (`normal`: the package's behaviour, unchanged) and ON (`tiny`) and expect the same
answer from both; one test shows the profile really is on by the one thing it must change (an expansion's
word floor) while the policy gate in front of the story stays shut.

No test here needs FFmpeg or a model: audio is synthetic numpy, the LLM is `FakeLlm`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import ns_studio.text.stages_shorts  # noqa: F401  (registers the shorts stages)
from ns_common.errors import NsError
from ns_common.models import Runtime
from ns_studio.policy import final_checks as fc
from ns_studio.policy import text_checks
from ns_studio.stages import REGISTRY
from ns_studio.text import stages_master as M
from ns_studio.text import story_profile as sp
from tests.helpers import make_channel, make_ctx
from tests.test_policy_p04 import (
    NEUTRAL_EXTRA,
    QC_FLATNESS,
    ExpandLlm,
    KeepOldLlm,
    _advice,
    _audio_ctx,
    _bed,
    _llm,
    _log_records,
    _master_job,
    _noise,
    _pc,
    _policy_on,
    _short,
    _speechlike,
    _with_old_phrase,
)

PROFILES = ["normal", "tiny"]

SECOND_EXTRA = (  # 34 new neutral words, different sentences from NEUTRAL_EXTRA (a second expansion's text)
    "Rain began to tap against the glass. She pulled the blanket higher and counted the seconds between "
    "the knocks. The clock on the wall ticked on, patient and steady, as the night wore thin."
)


@pytest.fixture(params=PROFILES)
def profile(request: pytest.FixtureRequest) -> str:
    return request.param


def _set_profile(ctx: Any, name: str) -> Any:
    """Switch a job context to the named profile (the runtime switch `tiny_story`) and prove it took."""
    if name == "tiny":
        ctx.runtime = Runtime.model_validate({**ctx.runtime.model_dump(mode="json"), "tiny_story": True})
    assert sp.profile_of(ctx).name == name
    return ctx


def _profiled(tmp_home: Path, name: str, **kw: Any) -> Any:
    return _set_profile(make_ctx(tmp_home, **kw), name)


def _expand_ctx(monkeypatch: pytest.MonkeyPatch, tmp_home: Path, name: str) -> Any:
    """A finished master job (made on the normal profile: its fake plan is a normal one), then the profile."""
    ctx = _master_job(monkeypatch, tmp_home)
    ctx.write_json("policy_text.json", {"stage": "text", "passed": True, "checks": {}, "advice": {}})
    return _set_profile(ctx, name)


# ---------------------------------------------------------------- F-H59 expansion text is policy-checked
def test_expansion_with_unsafe_text_is_refused_on_both_profiles(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, profile: str
):
    ctx = _expand_ctx(monkeypatch, tmp_home, profile)
    before = ctx.file("story.json").read_bytes()
    ctx.services.llm = _llm(_pc(unsafe_categories=["minors"], notes="a child is hurt"))
    with pytest.raises(NsError) as ei:
        M.expand_chapter(ctx, 1, 120)
    assert ei.value.code == "NS-E601" and "minors" in str(ei.value)
    assert ctx.file("story.json").read_bytes() == before


def test_expansion_with_a_blocked_name_or_claim_is_refused_on_both_profiles(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, profile: str
):
    ctx = _expand_ctx(monkeypatch, tmp_home, profile)
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


def test_expansion_scans_only_the_new_sentences_on_both_profiles(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, profile: str
):
    ctx = _expand_ctx(monkeypatch, tmp_home, profile)
    old_text = _with_old_phrase(
        ctx,
        "The ceiling leaked again. On the radio a news report droned. Nothing in real life felt this wrong.",
    )
    ctx.services.llm = KeepOldLlm(old_text, NEUTRAL_EXTRA)
    new = M.expand_chapter(ctx, 1, 40)
    assert "ceiling leaked again" in new.text
    ctx.services.llm = KeepOldLlm(new.text, SECOND_EXTRA + " It was caught on camera.")
    with pytest.raises(NsError) as ei:
        M.expand_chapter(ctx, 1, 40)
    assert (
        ei.value.code == "NS-E603" and "caught on camera" in str(ei.value) and "leaked" not in str(ei.value)
    )


def test_a_clean_expansion_is_kept_and_its_notes_merge_on_both_profiles(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, profile: str
):
    ctx = _expand_ctx(monkeypatch, tmp_home, profile)
    ctx.write_json("policy_notes.json", {"intensity": "low", "real_setting": False})
    ctx.services.llm = _llm(_pc(intensity="high"))
    assert M.expand_chapter(ctx, 1, 120).word_count > 0
    assert ctx.read_json("policy_notes.json")["intensity"] == "high"


def test_without_a_text_gate_an_expansion_asks_nothing_on_both_profiles(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, profile: str
):
    ctx = _master_job(monkeypatch, tmp_home)  # no policy_text.json: --no-policy
    _set_profile(ctx, profile)
    ctx.services.llm = _llm(_pc(unsafe_categories=["minors"]))
    M.expand_chapter(ctx, 1, 120)
    assert [c for c in ctx.services.llm.calls if c["prompt_name"] == "policy_check"] == []  # type: ignore[attr-defined]


SHORT_EXTRA = (  # about 36 words: over a quarter of 120, under a half
    "The hallway stayed quiet for a long while. Nobody moved. The porch light flickered twice, and then it "
    "held steady. She counted the steps to the stairs and listened for the floor to answer. Nothing did."
)


def test_the_profile_moves_the_word_floor_but_not_the_policy_gate(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """The one thing the profile must change, shown on this code: an expansion of a third of the words asked.

    Normal wants half (NS-E303). Tiny wants a quarter, so it lets the short expansion through to the policy
    gate, and the gate still refuses a blocked name and a claim of truth in it. Off, the package's order of
    checks is unchanged: the word floor first, then the policy gate.
    """
    clean = _expand_ctx(monkeypatch, tmp_home, "normal")
    original = clean.file("story.json").read_bytes()
    old_text = clean.read_json("story.json")["chapters"][0]["text"]
    added = len(SHORT_EXTRA.split())
    assert 120 // 4 <= added < 120 // 2
    clean.services.llm = KeepOldLlm(old_text, SHORT_EXTRA)
    with pytest.raises(NsError) as normal_err:
        M.expand_chapter(clean, 1, 120)
    assert normal_err.value.code == "NS-E303"
    _set_profile(clean, "tiny")
    assert M.expand_chapter(clean, 1, 120).word_count > 0  # the profile took: the same text is accepted

    for bad, code in (
        ("Then Donald Trump walked into the room.", "NS-E602"),
        ("I swear this is a true story.", "NS-E603"),
    ):
        ctx = clean  # a second job in one home would be refused as a repeated opening: put the story back
        ctx.file("story.json").write_bytes(original)
        before = ctx.file("story.json").read_bytes()
        ctx.services.llm = KeepOldLlm(
            ctx.read_json("story.json")["chapters"][0]["text"], f"{SHORT_EXTRA} {bad}"
        )
        with pytest.raises(NsError) as ei:
            M.expand_chapter(ctx, 1, 120)
        assert ei.value.code == code
        assert ctx.file("story.json").read_bytes() == before


# ---------------------------------------------------------------- F-H105 ideas, text gate, final re-check
def test_chosen_idea_is_classified_on_both_profiles(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, profile: str
):
    _policy_on(monkeypatch)
    ctx = _profiled(tmp_home, profile)
    ctx.services.llm = _llm([_pc(unsafe_categories=["graphic_harm"], notes="gore"), _pc()])
    REGISTRY["ideas"].run(ctx)
    asked = [c for c in ctx.services.llm.calls if c["prompt_name"] == "policy_check"]  # type: ignore[attr-defined]
    assert len(asked) == 2  # the first survivor was flagged, the second was taken
    chosen = ctx.read_json("idea.json")
    assert chosen["premise"] == ctx.read_json("ideas_all.json")[1]["premise"]


def test_all_ideas_unsafe_is_e601_on_both_profiles(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, profile: str
):
    _policy_on(monkeypatch)
    ctx = _profiled(tmp_home, profile)
    ctx.services.llm = _llm(_pc(unsafe_categories=["hate"], notes="slur"))
    with pytest.raises(NsError) as ei:
        REGISTRY["ideas"].run(ctx)
    assert ei.value.code == "NS-E601" and not ctx.file("idea.json").exists()


def test_banked_idea_is_classified_on_both_profiles(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, profile: str
):
    _policy_on(monkeypatch)
    ctx = _profiled(tmp_home, profile)
    ctx.db.execute(
        "insert into ideas(channel, premise, twist, created_at, status) values (?,?,?,?,?)",
        (ctx.channel.slug, "a banked premise", "a banked twist", "2026-10-01", "approved"),
    )
    ctx.services.llm = _llm(_pc(unsafe_categories=["minors"], notes="child in danger"))
    with pytest.raises(NsError) as ei:
        REGISTRY["ideas"].run(ctx)
    assert ei.value.code == "NS-E601" and "a banked premise" in str(ei.value)


def test_text_gate_and_final_recheck_fail_unsafe_stories_on_both_profiles(tmp_home: Path, profile: str):
    ctx = _profiled(tmp_home, profile)
    ctx.write_json("story.json", {"text": "A quiet house at night."})
    ctx.services.llm = _llm(_pc(unsafe_categories=["hate"], notes="slur"))
    assert not text_checks.UnsafeTopics().run(ctx).passed

    def graphic(c: Any, mode: str, **v: Any) -> dict[str, Any]:
        flags = [{"category": "graphic_harm", "reason": "g"}]
        return {
            "verdict": "fail",
            "reason": "graphic",
            "flags": flags,
            "intensity": "high",
            "real_setting": False,
        }

    res = fc.FinalUnsafeTopics(graphic).run(ctx)
    assert not res.passed and "graphic_harm" in res.detail

    def calm(c: Any, mode: str, **v: Any) -> dict[str, Any]:
        return {"verdict": "pass", "reason": "", "flags": [], "intensity": "low", "real_setting": None}

    ctx.write_json("policy_notes.json", {"intensity": "medium", "real_setting": False})
    assert fc.FinalUnsafeTopics(calm).run(ctx).passed
    notes = ctx.read_json("policy_notes.json")
    assert notes["intensity"] == "medium" and notes["real_setting"] is None


# ---------------------------------------------------------------- F-H106 intensity, F-H114 real setting
def test_age_restriction_suggestion_follows_the_intensity_on_both_profiles(tmp_home: Path, profile: str):
    mature = make_channel(slug="grim", audience="mature")
    for intensity, expected in (("high", True), ("low", False)):
        ctx = _profiled(tmp_home, profile, channel=mature)
        ctx.write_json("story.json", {"text": "A tale."})
        ctx.services.llm = _llm(_pc(intensity=intensity))
        assert text_checks.UnsafeTopics().run(ctx).passed
        assert fc.compute_advice(ctx, place_ner=lambda t: []).age_restrict_suggested is expected


def test_disclosure_advice_is_the_same_on_both_profiles(tmp_home: Path, profile: str):
    ctx = _profiled(tmp_home, profile)
    ctx.write_json("story.json", {"text": "He walked through Lisbon."})
    ctx.write_json("policy_notes.json", {"real_setting": False})
    assert fc.compute_advice(ctx, place_ner=lambda t: []).ai_disclosure == "no"
    assert fc.compute_advice(ctx, place_ner=lambda t: [("Lisbon", "GPE")]).ai_disclosure == "yes"
    ctx.write_json("policy_notes.json", {"real_setting": None})
    assert fc.compute_advice(ctx, place_ner=lambda t: []).ai_disclosure == "yes"
    assert _advice(tmp_home, {"real_setting": True}, []) == "yes"  # the helper's own context is normal


# ---------------------------------------------------------------- F-H115 hook presence
def test_short_must_open_with_its_hook_on_both_profiles(tmp_home: Path, profile: str):
    ctx = _profiled(tmp_home, profile)
    _short(
        ctx, 1, "Nobody ever answers the door.", "Nobody ever answers the door. The porch light flickered."
    )
    assert fc.ShortHookPresent().run(ctx).passed
    _short(ctx, 2, "The camera saw something it should not.", "The porch light flickered twice. It was cold.")
    res = fc.ShortHookPresent().run(ctx)
    assert not res.passed and "s2" in res.detail and "s1" not in res.detail


# ---------------------------------------------------------------- F-H116 flatness: measured, warning only
def _audio_on(tmp_home: Path, monkeypatch: pytest.MonkeyPatch, profile: str, samples: Any) -> Any:
    ctx = _audio_ctx(tmp_home, monkeypatch, samples)
    return _set_profile(ctx, profile)


def test_flatness_is_measured_and_only_warns_on_both_profiles(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, profile: str
):
    ok_samples = _speechlike(_noise(-62))
    ctx = _audio_on(tmp_home, monkeypatch, profile, ok_samples)
    res = fc.AudioOnlyVoice().run(ctx)
    measured = fc.quiet_flatness(ok_samples)
    assert measured is not None and measured > fc.TONAL_FLATNESS_MAX
    assert res.passed and "WARNING" not in res.detail
    row = ctx.read_json(QC_FLATNESS)["items"]["long"]
    assert row["quiet_flatness"] == pytest.approx(measured, abs=1e-4) and row["warned"] is False
    assert [r for r in _log_records(ctx) if r["event"] == "audio_flatness"]

    bed = _bed()
    ctx = _audio_on(tmp_home, monkeypatch, profile, bed)
    res = fc.AudioOnlyVoice().run(ctx)
    over = fc.quiet_flatness(bed)
    assert over is not None and over < fc.TONAL_FLATNESS_MAX
    assert res.passed and "WARNING" in res.detail  # over the threshold: a warning, no longer a failure
    row = ctx.read_json(QC_FLATNESS)["items"]["long"]
    assert row["quiet_flatness"] == pytest.approx(over, abs=1e-4) and row["warned"] is True
    assert [r for r in _log_records(ctx) if r["event"] == "audio_flatness_warning"]


def test_the_loud_bed_floor_still_fails_on_both_profiles(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, profile: str
):
    from tests.test_policy_p04 import _tone

    ctx = _audio_on(tmp_home, monkeypatch, profile, _speechlike(_tone(-30) + _tone(-34, hz=660.0)))
    res = fc.AudioOnlyVoice().run(ctx)
    assert not res.passed and "no silence anywhere" in res.detail
