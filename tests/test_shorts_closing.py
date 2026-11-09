"""P02: a shorts_only hook is cut to 12 words (F-H53) and every part gets a closing line of at most 15 words
that is voiced as `p<n>_close`, timed by the captions and appended by the render (F-H54).

The audio is `FakeTts` tone bursts (no FFmpeg); the render's own audio assembly is in test_render.py.
"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from ns_common.llm_client import FakeLlm
from ns_common.pacing import PACING_WPM
from ns_common.tts_client import FakeTts, TtsItem, wav_seconds
from ns_studio.context import FIXTURES_LLM, JobContext
from ns_studio.media import voice as voice_mod
from ns_studio.media.captions import CLOSE_GAP_S, HOOK_GAP_S, CaptionsStage, first_event_start
from ns_studio.media.qc import duration_check
from ns_studio.media.voice import VoiceStage, narration_items, read_closings
from ns_studio.stages import REGISTRY, inputs_hash, run_stage
from ns_studio.text import stages_shorts as S
from ns_studio.text.budget import short_budget
from ns_studio.text.validators import load_banned
from ns_studio.voice_wpm import record_voice_wpm
from tests.helpers import make_combo, make_ctx

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


def test_hooks_of_a_job_from_before_closing_lines_are_kept_not_redone(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("parts.json", [_part(1)])
    REGISTRY["hooks"].run(ctx)
    ctx.file("closings.json").unlink()  # a job from before closing lines, resumed
    calls = len(ctx.services.llm.calls)  # type: ignore[attr-defined]
    REGISTRY["hooks"].run(ctx)
    assert len(ctx.services.llm.calls) == calls  # type: ignore[attr-defined]
    assert not ctx.file("closings.json").exists()


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


# ---- break-it round: the budget, the closing line's content, older jobs ------------------------------------

BUCKETS = (35, 50, 75)
DISTINCT_HOOK = " ".join(f"hook{i}" for i in range(12))  # not the opening words of any body: it is voiced
MEASURED_WPM = 120.0  # a voice that speaks slower than the table (155 at standard pacing)
MEASURED_SENTENCE = "The doorbell camera pings at three twelve every night and I never look at the clips."
MEASURED_TEXT = " ".join([MEASURED_SENTENCE] * 12)


def _story(words: int) -> str:
    """`words` words in sentences of ten, the sentence length the word budget assumes."""
    out, left = [], words
    while left > 0:
        n = min(10, left)
        out.append(" ".join(["word"] * n) + ".")
        left -= n
    return " ".join(out)


def _short_seconds(tmp_path: Path, wpm: float, body: int, hook: int, closing: int) -> float:
    """Seconds of hook + gap + body + gap + closing as the tone fake speaks them (pauses included)."""
    tts, total = FakeTts(wpm=wpm), HOOK_GAP_S + CLOSE_GAP_S
    for i, words in enumerate((body, hook, closing)):
        wav = tmp_path / f"w{i}.wav"
        tts.speak(_story(words), "Ryan", 1.0, wav)
        total += wav_seconds(wav)
    return total


def _measured_ctx(tmp_home: Path, bucket: int, pacing: str) -> JobContext:
    """A job planned after another job measured the voice slower than the pacing table says."""
    first = make_ctx(tmp_home, combo=make_combo(length_bucket=bucket, pacing=pacing))
    items = [TtsItem("p1", MEASURED_TEXT, "Ryan", 1.0)]
    wav = first.subdir("audio") / "p1.wav"
    FakeTts(wpm=MEASURED_WPM).speak(MEASURED_TEXT, "Ryan", 1.0, wav)
    assert record_voice_wpm(first, "Ryan", items, {"p1": {"path": str(wav), "seconds": wav_seconds(wav)}})
    return make_ctx(tmp_home, combo=make_combo(length_bucket=bucket, pacing=pacing))


@pytest.mark.parametrize("source", ["table", "measured"])
@pytest.mark.parametrize("pacing", sorted(PACING_WPM))
@pytest.mark.parametrize("bucket", BUCKETS)
def test_the_longest_body_with_hook_and_the_closing_the_stage_picks_stays_inside_the_qc_band(
    tmp_home: Path, tmp_path: Path, bucket: int, pacing: str, source: str
):
    """The model may write a 15-word closing line, but the budget reserves 10: for a body at the top of its
    band `fit_closing` takes a line that fits, so the finished Short is not dropped by QC (NS-W406)."""
    if source == "measured":
        ctx = _measured_ctx(tmp_home, bucket, pacing)
    else:
        ctx = make_ctx(tmp_home, combo=make_combo(length_bucket=bucket, pacing=pacing))
    budget = S.ideas_budget(ctx)
    assert (budget.wpm == PACING_WPM[pacing]) == (source == "table")
    hook = DISTINCT_HOOK
    part = _part(1, _story(budget.max_words), hook=hook)
    closing = S.fit_closing(ctx, part, hook, _story(S.CLOSING_MAX_WORDS), 1)
    seconds = _short_seconds(tmp_path, budget.wpm, budget.max_words, S.HOOK_MAX_WORDS, len(closing.split()))
    assert duration_check(seconds, "short", bucket).passed, (seconds / bucket, closing, budget)
    target = _short_seconds(tmp_path, budget.wpm, budget.words, S.HOOK_MAX_WORDS, 6)  # a short default line
    assert duration_check(target, "short", bucket).passed, target / bucket


def test_a_budget_without_the_closing_allowance_let_the_longest_short_fail_qc(tmp_home: Path, tmp_path: Path):
    """Before: closing_words=0, a body at the cap and a 12-word closing line gave 1.22 x the 50 s bucket."""
    ctx = make_ctx(tmp_home)
    old = short_budget(50, "standard", hook_words=S.HOOK_MAX_WORDS, wpm=155)
    now = S.ideas_budget(ctx)
    assert now.max_words < old.max_words
    too_long = _short_seconds(tmp_path, 155, old.max_words, S.HOOK_MAX_WORDS, 12)
    assert not duration_check(too_long, "short", 50).passed
    assert duration_check(
        _short_seconds(tmp_path, 155, now.max_words, S.HOOK_MAX_WORDS, 12), "short", 50
    ).passed


def test_a_long_closing_line_is_kept_when_the_body_leaves_room_for_it(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    hook = DISTINCT_HOOK
    twelve = " ".join(["word"] * 12)
    part = _part(1, _story(S.ideas_budget(ctx).words), hook=hook)
    assert S.fit_closing(ctx, part, hook, twelve, 1) == twelve


def test_a_closing_line_the_short_has_no_room_for_is_replaced_by_a_default(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    hook = DISTINCT_HOOK
    fifteen = " ".join(["word"] * S.CLOSING_MAX_WORDS)
    top = _part(1, _story(S.ideas_budget(ctx).max_words), hook=hook)
    assert S.fit_closing(ctx, top, hook, fifteen, 2) == "Part 2 is up next."
    far_over = _part(1, _story(S.ideas_budget(ctx).max_words * 2), hook=hook)
    shortest = min(S.DEFAULT_SHORT_CLOSINGS + (S.default_closing(1, 1),), key=lambda c: len(c.split()))
    assert S.fit_closing(ctx, far_over, hook, fifteen, 1) == shortest


def test_the_hooks_stage_writes_the_line_that_fits(tmp_home: Path):
    top = _part(1, _story(S.ideas_budget(make_ctx(tmp_home)).max_words))
    answer = {"hooks": [DISTINCT_HOOK], "closing_line": _story(S.CLOSING_MAX_WORDS)}
    ctx = _hooks_ctx(tmp_home, answer, [top])
    REGISTRY["hooks"].run(ctx)
    line = ctx.read_json("closings.json")["1"]
    assert line == S.default_closing(1, 1) and len(line.split()) < S.CLOSING_MAX_WORDS


def test_the_shorts_budget_counts_the_closing_line_the_audio_gets(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    with_close = S.ideas_budget(ctx)
    without = short_budget(50, "standard", hook_words=S.HOOK_MAX_WORDS, wpm=with_close.wpm)
    assert without.words - with_close.words == S.SHORT_CLOSING_WORDS


def test_a_closing_line_that_claims_the_story_is_real_is_replaced(tmp_home: Path):
    for claim in ("This is a true story, follow for more.", "Caught on camera, follow for more."):
        ctx = _hooks_ctx(tmp_home, _all_too_long(claim), [_part(1), _part(2)])
        REGISTRY["hooks"].run(ctx)
        closings = ctx.read_json("closings.json")
        assert closings == {"1": "Part 2 is up next.", "2": S.DEFAULT_SHORT_CLOSINGS[1]}, claim


def test_a_closing_line_with_a_banned_phrase_is_replaced(tmp_home: Path):
    banned = load_banned()[0]
    ctx = _hooks_ctx(tmp_home, _all_too_long(f"Well, {banned} more soon."), [_part(1)])
    REGISTRY["hooks"].run(ctx)
    assert ctx.read_json("closings.json")["1"] == S.DEFAULT_SHORT_CLOSINGS[0]
    assert S.closing_is_clean("Follow for the next story.")
    assert not S.closing_is_clean(banned)


def test_resuming_an_older_shorts_job_keeps_its_hooks_and_does_not_redo_them(tmp_home: Path):
    ctx = _hooks_ctx(tmp_home, _all_too_long(), [_part(1)])
    stage = REGISTRY["hooks"]
    assert run_stage(ctx, stage) is True
    ctx.file("closings.json").unlink()  # the job was made before closing lines existed
    ctx.services.llm = FakeLlm(FIXTURES_LLM, overrides={"hook": {"hooks": ["Another hook entirely."]}})
    before = ctx.read_json("parts.json")
    run_stage(ctx, stage, force=True)  # the stage is entered again on resume: it must not pick new hooks
    assert ctx.read_json("parts.json") == before and not ctx.file("closings.json").exists()
    assert ctx.services.llm.calls == []  # type: ignore[attr-defined]
    assert S.HooksStage.outputs == ("parts.json", "hooks_all.json")  # no missing output on an older job


def test_the_voice_hash_of_an_older_job_is_the_one_it_had_before_closing_lines(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    for name in ("parts.json", "story.json", "shorts_plan.json"):
        ctx.write_json(name, {"name": name})
    old = hashlib.sha256()
    for name in ("parts.json", "story.json", "shorts_plan.json"):
        old.update(name.encode())
        old.update(ctx.file(name).read_bytes())
    voice = VoiceStage()
    assert inputs_hash(ctx, voice) == old.hexdigest()  # no closings.json: nothing added to the hash
    ctx.write_json("closings.json", {"1": "Follow for the next story."})
    with_closing = inputs_hash(ctx, voice)
    assert with_closing != old.hexdigest()
    ctx.write_json("closings.json", {"1": "Another line."})
    assert inputs_hash(ctx, voice) != with_closing  # a new line is a changed input: the voice is redone


def test_a_damaged_closings_file_means_no_closing_line_not_a_dead_voice_stage(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    assert read_closings(ctx) is None
    ctx.file("closings.json").write_text("{not json", encoding="utf-8")
    assert read_closings(ctx) is None
    ctx.write_json("closings.json", ["not", "a", "dict"])
    assert read_closings(ctx) is None
    ctx.write_json("closings.json", {"1": "Follow."})
    assert read_closings(ctx) == {"1": "Follow."}


# ---- review round: the cut fallback hook is not voiced a second time --------------------------------------


def _no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """`synthesize` without FFmpeg: the loudness pass copies, the measurements are stubs."""
    monkeypatch.setattr(voice_mod, "loudnorm", lambda src, dst, **kw: shutil.copyfile(src, dst))
    monkeypatch.setattr(voice_mod, "measure_lufs", lambda p: (-16.0, -3.0))
    monkeypatch.setattr(voice_mod.ff, "duration_s", wav_seconds)


def _spoken_words(ctx: JobContext, manifest: dict[str, dict]) -> list[str]:
    """Every word the voice stage actually had spoken for the part, from the sidecar of each audio item."""
    sidecars = [ctx.file(f"audio/{ident}.txt").read_text(encoding="utf-8") for ident in manifest]
    return [w for text in sidecars for w in text.split()]


def test_a_cut_fallback_hook_is_voiced_once_through_the_voice_stage(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """The 12-word cut of a 26-word first sentence is the opening of the body: spoken as its own item it
    would be heard, a gap, and then heard again in full (the effect, not only `parts.json["hook"]`)."""
    _no_ffmpeg(monkeypatch)
    ctx = _hooks_ctx(tmp_home, _all_too_long(), [_part(1)])
    REGISTRY["hooks"].run(ctx)
    part = ctx.read_json("parts.json")[0]
    assert part["hook"] == " ".join(LONG_SENTENCE.split()[:12])  # the cut, as F-H53 asks
    VoiceStage().run(ctx)
    manifest = ctx.read_json("audio/manifest.json")
    assert sorted(manifest) == ["p1", "p1_close"]  # no p1_hook: the body already opens with those words
    said = _spoken_words(ctx, manifest)
    assert said.count("Nobody") == 1 and said.count("house") == 1
    assert said == BODY.split() + ctx.read_json("closings.json")["1"].split()


def test_a_hook_that_opens_the_body_is_not_voiced_separately_however_it_is_written():
    sentences_first = "Nobody told me that the house at the end of the road had been empty since ninety one."
    body = f"{sentences_first} I went in anyway."
    for hook in (
        "Nobody told me that the house at the end of the road",  # the 12-word cut
        "Nobody told me that the house,",  # a cut that ended on a comma
        "NOBODY told me",  # case does not matter
        "Nobody told me that the house at the end of the road had been empty since ninety one.",
    ):
        items = narration_items([_part(1, body, hook=hook)], "Ryan", 1.0)
        assert [i.id for i in items] == ["p1"], hook


def test_a_hook_the_body_does_not_open_with_is_still_voiced_first():
    body = "The stairs were warm. I went up."
    for hook in ("Nobody warned me about the stairs.", "The stairs were cold.", "I went up.", "warm"):
        items = narration_items([_part(1, body, hook=hook)], "Ryan", 1.0)
        assert [i.id for i in items] == ["p1", "p1_hook"], hook
    assert [i.id for i in narration_items([_part(1, body, hook="")], "Ryan", 1.0)] == ["p1"]
