"""Pacing drives the TTS speed and the word budget agrees with QC (review row 4)."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ns_common.pacing import NOMINAL_WPM, PACING_WPM, pacing_speed
from ns_common.tts_client import FakeTts, wav_seconds
from ns_studio.media.master import voice_items
from ns_studio.media.qc import duration_check
from ns_studio.media.voice import job_speed, narration_items
from ns_studio.text.budget import MASTER_MAX_MINUTES, master_budget, master_max_words, short_budget
from ns_studio.text.validators import check_master_length
from tests.helpers import make_channel, make_combo, make_ctx

HOOK = 12


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
    """Before the fix every pacing spoke at the voice's own speed: brisk 50 s ran 56 s, measured ran 5 s short."""
    b = short_budget(bucket, pacing, hook_words=HOOK)
    wav = tmp_path / "s.wav"
    FakeTts().speak(" ".join(["word"] * (b.words + HOOK)), "Ryan", pacing_speed(1.0, pacing), wav)
    assert wav_seconds(wav) == pytest.approx(bucket, rel=0.03)


@pytest.mark.parametrize("pacing", ["brisk", "standard", "measured"])
def test_validator_band_sits_inside_the_qc_band(pacing: str):
    """A story the length validator accepts never fails the +-15 % duration check for length alone."""
    for bucket in (35, 50, 75):
        b = short_budget(bucket, pacing, hook_words=HOOK)
        for words in (b.min_words, b.max_words):
            seconds = (words + HOOK) / PACING_WPM[pacing] * 60.0
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


def test_check_master_length_catches_the_overshoot_qc_would_reject():
    """14 minutes at +20 % per chapter is 16.8 minutes: QC fails at 15:00 after the whole render."""
    assert check_master_length(int(14 * 155 * 1.2), 155) is not None
    assert check_master_length(14 * 155, 155) is None
    v = check_master_length(2500, 140)
    assert v is not None and v.rule == "master_length"
