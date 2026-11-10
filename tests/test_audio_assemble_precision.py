"""C6 audio: assembled narration keeps its full 16-bit sample precision (no coarse sample format)."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from ns_common import ffmpeg
from ns_common.errors import NsError
from ns_studio.media import audio

needs_ffmpeg = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not available")

SR_IN = 24000
SR_OUT = 48000
FREQ = 440.0
AMP = 0.125          # 1/8 of full scale: quiet narration, where coarse quantisation is audible as hiss
TONE_S = 1.0
GAP_S = 0.5
WINDOW_S = 0.04
EDGE_S = 0.05        # the resampler's start-up and the tone's edges are not part of the measure
LIMIT = 0.002        # tone-fit residual (rms, relative to the amplitude) a clean path stays under


def _write_tone(path: Path) -> Path:
    t = np.arange(int(SR_IN * TONE_S)) / SR_IN
    pcm = np.round(AMP * 32768 * np.sin(2 * np.pi * FREQ * t)).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR_IN)
        w.writeframes(pcm.tobytes())
    return path


def _read(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == SR_OUT
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")


def _residuals(samples: np.ndarray, start_s: float, end_s: float) -> list[float]:
    """Least-squares fit of a 440 Hz tone (sine, cosine, offset) in each window; rms residual / AMP."""
    n = int(WINDOW_S * SR_OUT)
    out: list[float] = []
    first = int((start_s + EDGE_S) * SR_OUT)
    last = int((end_s - EDGE_S) * SR_OUT)
    for a in range(first, last - n + 1, n):
        x = samples[a:a + n].astype(np.float64) / 32768.0
        t = (np.arange(n) + a) / SR_OUT
        basis = np.stack([np.sin(2 * np.pi * FREQ * t), np.cos(2 * np.pi * FREQ * t), np.ones(n)], axis=1)
        coef, *_ = np.linalg.lstsq(basis, x, rcond=None)
        out.append(float(np.sqrt(np.mean((x - basis @ coef) ** 2)) / AMP))
    return out


def _worst_residual(samples: np.ndarray) -> float:
    spans = [(0.0, TONE_S), (TONE_S + GAP_S, 2 * TONE_S + GAP_S)]
    return max(max(_residuals(samples, a, b)) for a, b in spans)


def _segments(tmp_path: Path) -> list[Path | float]:
    tone = _write_tone(tmp_path / "tone.wav")
    return [tone, GAP_S, tone]


@needs_ffmpeg
def test_a_tone_through_assemble_audio_keeps_its_precision(tmp_path: Path) -> None:
    out = audio.assemble_audio(_segments(tmp_path), tmp_path / "joined.wav", sr=SR_OUT)
    samples = _read(out)
    assert len(samples) >= int((2 * TONE_S + GAP_S - 0.05) * SR_OUT)
    assert _worst_residual(samples) <= LIMIT


@needs_ffmpeg
def test_assembled_samples_use_the_full_16_bit_range(tmp_path: Path) -> None:
    out = audio.assemble_audio(_segments(tmp_path), tmp_path / "joined.wav", sr=SR_OUT)
    samples = _read(out)
    assert np.count_nonzero(samples % 256) > len(samples) // 2


@needs_ffmpeg
def test_assemble_in_parts_keeps_the_same_precision(tmp_path: Path) -> None:
    out = audio.assemble_audio(_segments(tmp_path), tmp_path / "joined.wav", sr=SR_OUT, max_inputs=2)
    samples = _read(out)
    assert np.count_nonzero(samples % 256) > len(samples) // 2
    assert _worst_residual(samples) <= LIMIT
    assert not (tmp_path / "joined.parts").exists()


def test_assemble_audio_of_nothing_is_ns_e401(tmp_path: Path) -> None:
    with pytest.raises(NsError) as err:
        audio.assemble_audio([], tmp_path / "joined.wav")
    assert err.value.code == "NS-E401"
