"""Pacing drives the TTS speed and the word budget agrees with QC (review row 4)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ns_common.pacing import (
    CARD_S,
    NOMINAL_WPM,
    PACING_WPM,
    master_seconds,
    narration_seconds,
    pacing_speed,
    pause_plan,
)
from ns_common.tts_client import FakeTts, TtsItem, wav_seconds
from ns_studio.media.master import voice_items
from ns_studio.media.qc import duration_check
from ns_studio.media.voice import job_speed, narration_items
from ns_studio.text.budget import (
    MASTER_MAX_MINUTES,
    MASTER_MAX_SECONDS,
    master_budget,
    master_max_words,
    short_budget,
)
from ns_studio.text.validators import check_master_length
from tests.helpers import make_channel, make_combo, make_ctx

HOOK = 12


def punctuated(words: int, *, per_sentence: int = 12, per_paragraph: int = 5) -> str:
    """`words` words of narration as the TTS service gets it: sentences, with a paragraph every few of them.

    The earlier tests used "word word word" with no punctuation, so the fake inserted no pauses and the
    budgets looked nested inside QC's bands.
    """
    sentences = []
    for i in range(0, words, per_sentence):
        n = min(per_sentence, words - i)
        sentences.append(" ".join(f"w{i + k}" for k in range(n)) + ".")  # numbered: no two sentences alike
    paragraphs = [" ".join(sentences[j : j + per_paragraph]) for j in range(0, len(sentences), per_paragraph)]
    return "\n\n".join(paragraphs)


def test_pacing_speed_scales_the_voice_and_clamps():
    assert pacing_speed(1.0, "standard") == 1.0
    assert pacing_speed(1.0, "brisk") == pytest.approx(175 / 155, abs=1e-3)
    assert pacing_speed(1.0, "measured") == pytest.approx(140 / 155, abs=1e-3)
    assert pacing_speed(1.3, "brisk") == 1.3  # clamped to the API range
    assert pacing_speed(0.7, "measured") == 0.7
    with pytest.raises(KeyError):
        pacing_speed(1.0, "frantic")


@pytest.mark.parametrize("pacing", ["brisk", "standard", "measured"])
@pytest.mark.parametrize("bucket", [35, 50, 75])
def test_a_budgeted_short_speaks_for_its_bucket(tmp_path: Path, pacing: str, bucket: int):
    """Before the fix every pacing spoke at the voice speed: brisk 50 s ran 56 s, measured ran 5 s short."""
    b = short_budget(bucket, pacing, hook_words=HOOK)
    wav = tmp_path / "s.wav"
    FakeTts().speak(punctuated(b.words + HOOK), "Ryan", pacing_speed(1.0, pacing), wav)
    assert wav_seconds(wav) == pytest.approx(bucket, rel=0.03)  # the pauses are counted in the budget


@pytest.mark.parametrize("pacing", ["brisk", "standard", "measured"])
def test_validator_band_sits_inside_the_qc_band(pacing: str):
    """A story the length validator accepts never fails the +-15 % duration check for length alone."""
    for bucket in (35, 50, 75):
        b = short_budget(bucket, pacing, hook_words=HOOK)
        for words in (b.min_words, b.max_words):
            seconds = narration_seconds(punctuated(words + HOOK), PACING_WPM[pacing])
            assert duration_check(seconds, "short", bucket).passed, (pacing, bucket, words, seconds)


def test_voice_items_carry_the_pacing_speed(tmp_home: Path):
    ctx = make_ctx(tmp_home, channel=make_channel(), combo=make_combo(pacing="brisk"))
    assert job_speed(ctx) == pacing_speed(ctx.channel.voice.speed, "brisk") > 1.0
    parts = [{"n": 1, "text": "One two three. Four five six.", "hook": "Hook words here."}]
    assert {it.speed for it in narration_items(parts, "Ryan", job_speed(ctx))} == {job_speed(ctx)}
    ctx.write_json("story.json", {"chapters": [{"n": 1, "title": "One", "text": "One two three."}]})
    ctx.write_json("shorts_plan.json", [])
    items = voice_items(ctx)
    assert items and all(it.speed == job_speed(ctx) for it in items)


def test_nominal_rate_is_the_standard_pace():
    assert NOMINAL_WPM == PACING_WPM["standard"]


def test_a_master_cannot_plan_or_hold_more_than_fourteen_minutes():
    with pytest.raises(ValueError):
        master_budget(MASTER_MAX_MINUTES + 1, "standard")
    assert master_budget(MASTER_MAX_MINUTES, "standard").target_words <= master_max_words(155)
    with pytest.raises(ValidationError):
        make_channel(long_minutes=(10, 15))
    assert make_channel(long_minutes=(10, 14)).long_minutes == (10, 14)


def _master_chapters(total_words: int, n: int = 5) -> list[str]:
    return [punctuated(total_words // n) for _ in range(n)]


def test_check_master_length_catches_the_overshoot_qc_would_reject():
    """14 minutes at +20 % per chapter is 16.8 minutes: QC fails at 15:00 after the whole render."""
    wpm = 155
    assert check_master_length(_master_chapters(int(14 * wpm * 1.2)), wpm) is not None
    v = check_master_length(_master_chapters(2500, 5), 140)
    assert v is not None and v.rule == "master_length"
    assert check_master_length(_master_chapters(master_budget(14, "standard").target_words), wpm) is None


def test_a_fourteen_minute_budget_renders_under_fifteen_minutes():
    """Re-review row 6: 14 x 155 words of speech played for about 15:12 once the pauses were added."""
    wpm = 155
    old_words = 14 * wpm
    assert master_seconds(_master_chapters(old_words), wpm) > 15 * 60  # the old budget: QC raises NS-E404
    mb = master_budget(14, "standard")
    chapters = _master_chapters(mb.target_words, mb.chapters)
    est = master_seconds(chapters, wpm)
    assert est == pytest.approx(14 * 60, rel=0.02) and est < MASTER_MAX_SECONDS < 15 * 60
    # a story right at the validator's limit still fits the ceiling
    assert master_seconds(_master_chapters(master_max_words(wpm), mb.chapters), wpm) < 15 * 60


def test_the_fake_tts_audio_of_a_fourteen_minute_master_is_under_fifteen_minutes(tmp_path: Path):
    """The same through the fake's real silences: chapters with a cliff line and a card between them."""
    wpm = 155
    mb = master_budget(14, "standard")
    texts = _master_chapters(mb.target_words, mb.chapters)
    items = [
        TtsItem(f"c{i}", t, "Ryan", 1.0, () if i == len(texts) - 1 else (t.split(".")[-2].strip() + ".",))
        for i, t in enumerate(texts)
    ]
    files = FakeTts(wpm=wpm).speak_batch(items, tmp_path)
    total = sum(f.seconds for f in files) + CARD_S * (len(files) - 1)
    assert total < 15 * 60 and total == pytest.approx(master_seconds(texts, wpm), rel=0.01)


def test_pause_plan_matches_the_tts_service_chunking():
    """`pause_plan` (budget and fake) and `chunk_text` (service) must agree on every silence."""
    from ns_tts.audio import chunk_text

    text = punctuated(130) + "\n\nShort. And then it ended without a sound at all."
    cliff = "And then it ended without a sound at all."
    plan = pause_plan(text, [cliff])
    chunks = chunk_text(text, cliff_lines=[cliff])
    assert [round(p, 3) for _t, p in plan] == [c.pause_after_ms / 1000 for c in chunks]
    assert [t for t, _p in plan] == [c.text for c in chunks]


def test_unpunctuated_text_has_no_pauses_which_is_why_the_old_tests_agreed():
    assert narration_seconds(" ".join(["word"] * 155), 155) == pytest.approx(60.0)
