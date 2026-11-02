"""Speed without a pitch change: the FFmpeg `atempo` chain (review row 12)."""

from pathlib import Path

import numpy as np
import pytest

from ns_common import ffmpeg
from ns_tts import audio
from ns_tts.audio import atempo_chain, atempo_filter, time_stretch, write_wav


def test_atempo_chain_stays_inside_ffmpegs_range():
    assert atempo_chain(1.0) == [] and atempo_filter(1.0) == "" and atempo_filter(1.0004) == ""
    assert atempo_chain(0.7) == [0.7] and atempo_chain(1.3) == [1.3]  # the channel window: one filter
    assert atempo_filter(0.8) == "atempo=0.8" and atempo_filter(1.25) == "atempo=1.25"
    assert atempo_chain(2.0) == [2.0] and atempo_chain(0.5) == [0.5]
    assert atempo_chain(4.0) == [2.0, 2.0]
    assert atempo_chain(3.0) == [2.0, 1.5]
    assert atempo_chain(0.25) == [0.5, 0.5]
    assert atempo_chain(0.3) == [0.5, 0.6]
    assert atempo_filter(3.0) == "atempo=2,atempo=1.5"
    for speed in (0.05, 0.3, 0.7, 0.9, 1.1, 1.3, 2.5, 7.0):
        factors = atempo_chain(speed)
        assert all(0.5 <= f <= 2.0 for f in factors)
        assert abs(float(np.prod(factors)) - speed) / speed < 1e-4


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan")])
def test_atempo_chain_rejects_non_positive_speed(bad: float):
    with pytest.raises(ValueError):
        atempo_chain(bad)


def test_time_stretch_calls_ffmpeg_atempo_and_not_a_resampler(monkeypatch: pytest.MonkeyPatch):
    seen: list[list[str]] = []

    def fake_run(args: list[str], **kw: object) -> None:
        seen.append(args)
        write_wav(Path(args[-1]), np.zeros(1000, dtype=np.float32), 24000)

    monkeypatch.setattr(ffmpeg, "run", fake_run)
    x = np.ones(24000, dtype=np.float32)
    assert time_stretch(x, 24000, 1.0) is x and not seen  # no change: no process
    assert time_stretch(np.zeros(0, dtype=np.float32), 24000, 0.8).size == 0 and not seen
    y = time_stretch(x, 24000, 0.8)
    assert len(y) == 1000
    (args,) = seen
    assert args[args.index("-filter:a") + 1] == "atempo=0.8"
    assert args[args.index("-ar") + 1] == "24000"
    time_stretch(x, 24000, 3.0)
    assert seen[1][seen[1].index("-filter:a") + 1] == "atempo=2,atempo=1.5"
    assert audio.ATEMPO_MIN == 0.5 and audio.ATEMPO_MAX == 2.0


def _tone(freq: float, seconds: float = 1.0, sr: int = 24000) -> np.ndarray:
    return (0.4 * np.sin(2 * np.pi * freq * np.arange(int(sr * seconds)) / sr)).astype(np.float32)


def _peak_hz(x: np.ndarray, sr: int = 24000) -> float:
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.argmax(spec)) * sr / len(x)


@pytest.mark.skipif(not ffmpeg.available(), reason="needs ffmpeg")
@pytest.mark.parametrize("speed", [0.7, 0.8, 1.2, 1.3])
def test_time_stretch_changes_length_and_keeps_pitch(speed: float):
    x = _tone(440.0)
    y = time_stretch(x, 24000, speed)
    assert abs(len(y) / len(x) - 1.0 / speed) < 0.05  # speed > 1 shortens, < 1 lengthens
    assert abs(_peak_hz(y) - 440.0) < 8.0  # resampling would move it to 440 * speed
