"""P02: a shorts_only hook is cut to 12 words (F-H53) and every part gets a closing line of at most 15 words
that is voiced as `p<n>_close`, timed by the captions and appended by the render (F-H54).

The audio is `FakeTts` tone bursts (no FFmpeg); the render's own audio assembly is in test_render.py.
"""

import json
from pathlib import Path

import pytest

from ns_common.llm_client import FakeLlm
from ns_common.tts_client import FakeTts, wav_seconds
from ns_studio.context import FIXTURES_LLM, JobContext
from ns_studio.media.captions import CLOSE_GAP_S, HOOK_GAP_S, CaptionsStage, first_event_start
from ns_studio.media.voice import narration_items
from ns_studio.stages import REGISTRY
from ns_studio.text import stages_shorts as S
from tests.helpers import make_ctx

LONG_SENTENCE = (
    "Nobody told me that the house at the end of the road had been empty since the winter of ninety "
    "one when the last tenant left."
)  # 26 words
BODY = f"{LONG_SENTENCE} I went in anyway. The stairs were warm."
THIRTEEN = "one two three four five six seven eight nine ten eleven twelve thirteen"


def _part(n: int, text: str = BODY, hook: str = "") -> dict:
    return {
        "n": n,
        "text": text,
        "hook": hook,
        "word_count": len(text.split()),
        "est_seconds": 10.0,
        "cliffhanger": False,
    }


def _hooks_ctx(tmp_home: Path, hook_answer: dict, parts: list[dict]) -> JobContext:
    ctx = make_ctx(tmp_home)
    ctx.services.llm = FakeLlm(
        FIXTURES_LLM, overrides={"hook": hook_answer, "judge": {"index": 0, "reason": "x"}}
    )
    ctx.write_json("parts.json", parts)
    return ctx


def _all_too_long(closing: str | None = "Follow for the next story.") -> dict:
    out: dict = {"hooks": [THIRTEEN + f" {i}" for i in range(5)]}
    if closing is not None:
        out["closing_line"] = closing
    return out


# ---- F-H53 -------------------------------------------------------------------------------------------


def test_fallback_hook_is_cut_to_twelve_words_when_every_candidate_is_too_long(tmp_home: Path):
    ctx = _hooks_ctx(tmp_home, _all_too_long(), [_part(1)])
    REGISTRY["hooks"].run(ctx)
    hook = ctx.read_json("parts.json")[0]["hook"]
    assert len(LONG_SENTENCE.split()) == 26 and len(hook.split()) == 12
    assert hook == " ".join(LONG_SENTENCE.split()[:12])
    assert ctx.read_json("hooks_all.json") == [[hook]]


def test_a_short_first_sentence_is_the_fallback_hook_unchanged(tmp_home: Path):
    ctx = _hooks_ctx(tmp_home, _all_too_long(), [_part(1, "The stairs were warm. I went up.")])
    REGISTRY["hooks"].run(ctx)
    assert ctx.read_json("parts.json")[0]["hook"] == "The stairs were warm."


def test_cut_words_keeps_what_fits_and_drops_a_dangling_comma():
    assert S.cut_words("one two three", 12) == "one two three"
    assert S.cut_words("one two three, four five", 3) == "one two three"


# ---- F-H54: the line ---------------------------------------------------------------------------------


def test_every_part_gets_a_closing_line_of_at_most_fifteen_words(tmp_home: Path):
    ctx = _hooks_ctx(
        tmp_home, _all_too_long("Follow the channel for more stories like this."), [_part(1), _part(2)]
    )
    REGISTRY["hooks"].run(ctx)
    closings = ctx.read_json("closings.json")
    assert list(closings) == ["1", "2"]
    assert all(0 < len(c.split()) <= 15 for c in closings.values())
    assert closings["1"] == "Follow the channel for more stories like this."


def test_an_overlong_or_missing_closing_line_is_replaced_by_the_default_of_its_part(tmp_home: Path):
    sixteen = " ".join(["word"] * 16)
    for answer in (_all_too_long(sixteen), _all_too_long(None), _all_too_long("   ")):
        ctx = _hooks_ctx(tmp_home, answer, [_part(1), _part(2)])
        REGISTRY["hooks"].run(ctx)
        closings = ctx.read_json("closings.json")
        assert closings["1"] == "Part 2 is up next."  # a part with another after it points there
        assert closings["2"] == S.DEFAULT_SHORT_CLOSINGS[1]
        assert all(len(c.split()) <= 15 for c in closings.values())


def test_the_hooks_stage_asks_the_model_for_the_closing_line_of_each_part(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("parts.json", [_part(1), _part(2)])
    REGISTRY["hooks"].run(ctx)
    calls = [c for c in ctx.services.llm.calls if c["prompt_name"] == "hook"]  # type: ignore[attr-defined]
    assert len(calls) == 2 and "closing line" in calls[0]["user"] and "part 2 of 2" in calls[1]["user"]
    assert ctx.read_json("closings.json")["1"] == "Follow the channel for the next story."


def test_hooks_are_redone_when_only_the_closing_file_is_missing(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("parts.json", [_part(1)])
    REGISTRY["hooks"].run(ctx)
    ctx.file("closings.json").unlink()  # a job from before closing lines, resumed
    REGISTRY["hooks"].run(ctx)
    assert ctx.file("closings.json").exists()


def test_a_new_split_clears_the_closing_lines_with_the_hooks(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("parts.json", [_part(1)])
    REGISTRY["hooks"].run(ctx)
    ctx.write_json("story.json", {"text": BODY, "word_count": 40, "revisions": 0})
    REGISTRY["split"].run(ctx)
    assert not ctx.file("closings.json").exists() and not ctx.file("hooks_all.json").exists()


# ---- F-H54: voice, captions ----------------------------------------------------------------------------


def test_the_closing_line_is_a_narration_item_of_its_own():
    closings = {"1": "Follow for the next story.", "2": "Part 3 is up next."}
    items = narration_items([_part(1, hook="A hook."), _part(2)], "Ryan", 1.0, "en", closings)
    assert [i.id for i in items] == ["p1", "p1_hook", "p1_close", "p2", "p2_close"]
    assert items[2].text == "Follow for the next story." and items[2].language == "en"
    assert [i.id for i in narration_items([_part(1)], "Ryan", 1.0)] == ["p1"]  # no closings file: as before


def _voice_part(ctx: JobContext, close_text: str | None) -> dict:
    """audio/manifest.json of one part (hook, body, optional closing), spoken by the tone fake."""
    hook = "Nobody told me about the house."
    ctx.write_json("parts.json", [_part(1, hook=hook)])
    manifest = {}
    audio = ctx.subdir("audio")
    for ident, text in (("p1", BODY), ("p1_hook", hook), ("p1_close", close_text)):
        if text is None:
            continue
        wav = audio / f"{ident}.wav"
        FakeTts().speak(text, "Ryan", 1.0, wav)
        wav.with_suffix(".txt").write_text(text, encoding="utf-8")
        manifest[ident] = {"path": str(wav), "seconds": round(wav_seconds(wav), 3)}
    ctx.write_json("audio/manifest.json", manifest)
    return manifest


def test_captions_time_the_closing_line_after_the_body(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    man = _voice_part(ctx, "Follow for the next story.")
    CaptionsStage().run(ctx)
    tl = ctx.read_json("captions/s1.json")
    body_end = man["p1_hook"]["seconds"] + HOOK_GAP_S + man["p1"]["seconds"]
    assert tl["close_offset_s"] == pytest.approx(body_end + CLOSE_GAP_S, abs=1e-3)
    assert tl["close_s"] == man["p1_close"]["seconds"] and tl["close_gap_s"] == CLOSE_GAP_S
    assert tl["audio_s"] == pytest.approx(tl["close_offset_s"] + man["p1_close"]["seconds"], abs=1e-3)
    closing = [w for w in tl["words"] if w["sentence_idx"] == -2]
    assert [w["w"] for w in closing] == ["Follow", "for", "the", "next", "story."]
    assert closing[0]["start"] == pytest.approx(tl["close_offset_s"], abs=1e-3)
    assert ctx.file("words/p1_close.json").exists()
    rep = ctx.read_json("captions/report.json")["s1"]
    assert rep["drift_ms"] == pytest.approx(0.0, abs=1e-6) and rep["match_ratio"] == 1.0
    ass = ctx.file("captions/s1.ass").read_text(encoding="utf-8")
    assert "STORY." in ass.upper() and first_event_start(ass) == 0.0  # drawn, and the hook still opens it


def test_a_part_without_a_closing_line_is_timed_as_before(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    man = _voice_part(ctx, None)
    CaptionsStage().run(ctx)
    tl = json.loads(ctx.file("captions/s1.json").read_text(encoding="utf-8"))
    assert tl["close_s"] == 0.0 and tl["close_gap_s"] == 0.0
    assert tl["audio_s"] == pytest.approx(
        man["p1_hook"]["seconds"] + HOOK_GAP_S + man["p1"]["seconds"], abs=1e-3
    )
    assert not [w for w in tl["words"] if w["sentence_idx"] == -2]
