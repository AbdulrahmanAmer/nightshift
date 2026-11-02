"""Helpers for the ``v01_samples`` step and the Verify screen (standard library only)."""

from __future__ import annotations

import math
import shutil
import struct
import wave
from array import array
from pathlib import Path

BARS = 28
MIN_BAR = 0.25


def wav_seconds(path: Path) -> float:
    """Length of a WAV file in seconds (0.0 when it cannot be read)."""
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() / float(w.getframerate() or 1)
    except (OSError, EOFError, wave.Error):
        return 0.0


def wav_bars(path: Path, count: int = BARS) -> list[float]:
    """``count`` loudness bars in [0.25, 1] for the waveform: RMS per bucket, scaled to the loudest one."""
    try:
        with wave.open(str(path), "rb") as w:
            channels = w.getnchannels()
            width = w.getsampwidth()
            frames = w.readframes(w.getnframes())
    except (OSError, EOFError, wave.Error):
        return [MIN_BAR] * count
    if width != 2 or not frames:
        return [MIN_BAR] * count
    samples = array("h")
    samples.frombytes(frames[: len(frames) - (len(frames) % 2)])
    if channels > 1:
        samples = samples[::channels]
    if not samples:
        return [MIN_BAR] * count
    per = max(1, len(samples) // count)
    rms: list[float] = []
    for i in range(count):
        chunk = samples[i * per : (i + 1) * per] if i < count - 1 else samples[i * per :]
        rms.append(math.sqrt(sum(s * s for s in chunk) / len(chunk)) if len(chunk) else 0.0)
    top = max(rms)
    if top <= 0:
        return [MIN_BAR] * count
    return [round(MIN_BAR + (1 - MIN_BAR) * (r / top), 3) for r in rms]


def write_sine_wav(path: Path, seconds: float = 1.0, rate: int = 16000, freq: float = 440.0) -> Path:
    """A mono 16-bit sine wave (simulate mode and tests)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    total = int(seconds * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(
            b"".join(
                struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)
                                      * (0.3 + 0.7 * i / max(1, total))))
                for i in range(total)
            )
        )
    return path


def seed_scratch_home(home: Path, scratch: Path, slug: str) -> list[Path]:
    """Copy ``config/runtime.json`` and ``config/channels/<slug>.yaml`` into a scratch home.

    The test render runs there, so the real outbox and database stay clean. Returns the files copied.
    """
    copied: list[Path] = []
    for rel in (Path("config") / "runtime.json", Path("config") / "channels" / f"{slug}.yaml"):
        src = home / rel
        if src.is_file():
            dst = scratch / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            copied.append(dst)
    return copied


def newest_file(root: Path, pattern: str) -> Path | None:
    """The most recently modified file below ``root`` that matches ``pattern``."""
    if not root.is_dir():
        return None
    found = [p for p in root.rglob(pattern) if p.is_file()]
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def frame_grab_argv(ffmpeg: str, video: Path, out: Path) -> list[str]:
    """ffmpeg command line that saves one frame (at 2 s, else the first) of ``video`` as a JPEG."""
    return [ffmpeg, "-y", "-v", "error", "-ss", "2", "-i", str(video), "-frames:v", "1", "-q:v", "3", str(out)]


def duration_argv(ffprobe: str, video: Path) -> list[str]:
    """ffprobe command line that prints the duration in seconds."""
    return [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)]
