"""C4 ns_tts: checks for a voice-clone reference recording (spec 7.4, J6).

A usable sample is one clean speaker, 3-30 s long, loud enough and without a constant noise bed. The
checks are numpy only (frame RMS statistics), so they run anywhere. A bad sample raises `NS-E312`.
A non-WAV recording (the browser records webm/m4a/mp3) is converted to mono 24 kHz WAV with FFmpeg first.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ns_common import ffmpeg
from ns_common.errors import NsError
from ns_tts.audio import read_wav

MIN_SECONDS = 3.0
MAX_SECONDS = 30.0   # longer recordings are cut to the first 30 s
MIN_SNR_DB = 12.0    # loud frames (90th percentile) vs the quiet floor (5th percentile)
MIN_LEVEL_DBFS = -40.0  # loud frames must reach this RMS level
MAX_CLIPPED = 0.01   # share of samples at full scale
FRAME_MS = 20
_EPS = 1e-9


@dataclass(frozen=True)
class SampleReport:
    """What the checks measured."""

    seconds: float
    level_dbfs: float
    snr_db: float
    clipped: float


def analyse(x: np.ndarray, sr: int) -> SampleReport:
    """Duration, speech level, signal-to-floor ratio and clipping of mono float audio."""
    n = max(1, int(sr * FRAME_MS / 1000))
    frames = len(x) // n
    if frames == 0:
        return SampleReport(len(x) / max(1, sr), -120.0, 0.0, 0.0)
    rms = np.sqrt(np.mean(x[: frames * n].reshape(frames, n).astype(np.float64) ** 2, axis=1))
    db = 20.0 * np.log10(rms + _EPS)
    loud = float(np.percentile(db, 90))
    floor = float(np.percentile(db, 5))
    clipped = float(np.mean(np.abs(x) >= 0.999)) if len(x) else 0.0
    return SampleReport(len(x) / sr, round(loud, 1), round(loud - floor, 1), clipped)


def verdict(r: SampleReport) -> str | None:
    """Why the sample is unusable (plain words), or None when it is fine."""
    if r.seconds < MIN_SECONDS:
        return f"the recording is {r.seconds:.1f} s long; at least {MIN_SECONDS:.0f} s of speech is needed"
    if r.level_dbfs < MIN_LEVEL_DBFS:
        return "the recording is too quiet or silent"
    if r.clipped > MAX_CLIPPED:
        return "the recording is distorted (too loud)"
    if r.snr_db < MIN_SNR_DB:
        return "there is constant noise or music behind the voice"
    return None


def to_wav(src: Path, dst: Path) -> Path:
    """Mono 24 kHz 16-bit WAV of the first `MAX_SECONDS` of `src`; NS-E312 when it is not audio."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        ffmpeg.run(["-i", str(src), "-vn", "-t", f"{MAX_SECONDS:.0f}", "-ac", "1", "-ar", "24000",
                    "-c:a", "pcm_s16le", str(dst)], timeout_s=120)
    except NsError as e:
        raise NsError("NS-E312", "the file could not be read as audio") from e
    return dst


def prepare_sample(src: Path, work_dir: Path) -> tuple[Path, SampleReport]:
    """A checked WAV for cloning: `src` itself if it is a 3-30 s WAV, else a converted copy in `work_dir`.

    Raises `NsError("NS-E312")` with the reason in the detail when the sample is too short, too quiet,
    clipped or noisy.
    """
    path = src
    try:
        x, sr = read_wav(src)
        if len(x) / max(1, sr) > MAX_SECONDS:
            path = to_wav(src, work_dir / "sample.wav")
            x, sr = read_wav(path)
    except ValueError:  # not a (supported) WAV: let FFmpeg decode it
        path = to_wav(src, work_dir / "sample.wav")
        x, sr = read_wav(path)
    report = analyse(x, sr)
    why = verdict(report)
    if why is not None:
        raise NsError("NS-E312", why)
    return path, report
