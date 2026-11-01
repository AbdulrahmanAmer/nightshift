"""C8 Clipper: FFmpeg analysis of the source video (spec 23.5 step 1).

Scene cuts (`select='gt(scene,0.3)'`), silences (`silencedetect`), integrated loudness (`ebur128`) and the
loudest seconds (RMS per second of `audio.wav`). Face and facecam detection live in `detect.py`; the
analyze stage merges both into `analysis.json`. All parsers are pure and tested on sample log text.
"""
from __future__ import annotations

import re
import wave
from pathlib import Path

import numpy as np

from ns_common import ffmpeg
from ns_studio.clipper.models import Analysis, Loudness, LoudPeak, SilenceSpan

SCENE_THRESHOLD = 0.3
SILENCE_DB = -35
SILENCE_MIN_S = 0.7
PEAK_COUNT = 40
LONG_TIMEOUT_S = 4 * 3600

_PTS = re.compile(r"pts_time:\s*([0-9]+(?:\.[0-9]+)?)")
_SIL_START = re.compile(r"silence_start:\s*(-?[0-9]+(?:\.[0-9]+)?)")
_SIL_END = re.compile(r"silence_end:\s*(-?[0-9]+(?:\.[0-9]+)?)")
_LUFS = re.compile(r"\bI:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*LUFS")
_LRA = re.compile(r"\bLRA:\s*(-?[0-9]+(?:\.[0-9]+)?)\s*LU\b")


# ---------- parsers ----------
def parse_scene_cuts(stderr: str, *, min_t: float = 0.5) -> list[float]:
    """`pts_time` of every `showinfo` line (the frames `select` let through), without the very first frame."""
    cuts: list[float] = []
    for line in stderr.splitlines():
        m = _PTS.search(line) if "showinfo" in line else None
        if m:
            cuts.append(float(m.group(1)))
    return sorted({round(t, 3) for t in cuts if t >= min_t})


def parse_silences(stderr: str, duration_s: float) -> list[SilenceSpan]:
    """Spans from `silence_start` / `silence_end` log lines; an open final span ends at `duration_s`."""
    spans: list[SilenceSpan] = []
    open_start: float | None = None
    for line in stderr.splitlines():
        s = _SIL_START.search(line)
        if s:
            open_start = max(0.0, float(s.group(1)))
            continue
        e = _SIL_END.search(line)
        if e and open_start is not None:
            spans.append(SilenceSpan(start=open_start, end=max(open_start, float(e.group(1)))))
            open_start = None
    if open_start is not None:
        spans.append(SilenceSpan(start=open_start, end=max(open_start, duration_s)))
    return spans


def parse_ebur128(stderr: str) -> tuple[float | None, float | None]:
    """Integrated loudness (LUFS) and loudness range (LU) from the last `I:` / `LRA:` values in the log."""
    lufs = _LUFS.findall(stderr)
    lra = _LRA.findall(stderr)
    i_val = float(lufs[-1]) if lufs else None
    if i_val is not None and i_val <= -69.0:  # ebur128 prints -70 for silence
        i_val = None
    return i_val, (float(lra[-1]) if lra else None)


# ---------- FFmpeg runs ----------
def scene_cuts(video: Path) -> list[float]:
    """Scene-cut timestamps in seconds (decoded at 320 px width to stay fast on long videos)."""
    proc = ffmpeg.run(
        [
            "-i", str(video), "-an",
            "-vf", f"scale=320:-2,select='gt(scene,{SCENE_THRESHOLD})',showinfo",
            "-f", "null", "-",
        ],
        timeout_s=LONG_TIMEOUT_S,
    )
    return parse_scene_cuts(proc.stderr)


def audio_events(wav: Path, duration_s: float) -> tuple[list[SilenceSpan], float | None, float | None]:
    """Silences, integrated LUFS and LRA of `wav` in one pass (`silencedetect` + `ebur128`)."""
    proc = ffmpeg.run(
        [
            "-i", str(wav),
            "-af", f"silencedetect=noise={SILENCE_DB}dB:d={SILENCE_MIN_S},ebur128",
            "-f", "null", "-",
        ],
        timeout_s=LONG_TIMEOUT_S,
    )
    lufs, lra = parse_ebur128(proc.stderr)
    return parse_silences(proc.stderr, duration_s), lufs, lra


def loudness_peaks(wav: Path, count: int = PEAK_COUNT, margin_db: float = 6.0) -> list[LoudPeak]:
    """The loudest seconds: local maxima of per-second RMS that stand `margin_db` above the median."""
    levels: list[float] = []
    with wave.open(str(wav), "rb") as w:
        rate, width = w.getframerate(), w.getsampwidth()
        if width != 2:
            return []
        while True:
            raw = w.readframes(rate * 60)
            if not raw:
                break
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            for s in range(0, len(samples), rate):
                seg = samples[s : s + rate]
                if seg.size >= rate // 2:
                    levels.append(20.0 * float(np.log10(float(np.sqrt(np.mean(seg * seg))) + 1e-9)))
    if len(levels) < 3:
        return []
    arr = np.array(levels)
    floor = float(np.median(arr)) + margin_db
    peaks = [
        LoudPeak(t=float(i), db=round(float(arr[i]), 1))
        for i in range(len(arr))
        if arr[i] >= floor
        and arr[i] >= (arr[i - 1] if i > 0 else -200.0)
        and arr[i] >= (arr[i + 1] if i + 1 < len(arr) else -200.0)
    ]
    peaks.sort(key=lambda p: -p.db)
    return sorted(peaks[:count], key=lambda p: p.t)


def media_loudness(wav: Path, lufs: float | None, lra: float | None) -> Loudness:
    """Combine ebur128 values with the loudest-seconds list."""
    return Loudness(integrated_lufs=lufs, lra=lra, peaks=loudness_peaks(wav))


# ---------- prompt view ----------
def window_view(
    cuts: list[float],
    silences: list[SilenceSpan],
    peaks: list[LoudPeak],
    w_start: float,
    w_end: float,
) -> dict[str, object]:
    """The slice of the analysis that falls inside a window, compact enough for the prompt."""
    in_cuts = [c for c in cuts if w_start <= c <= w_end]
    span_min = max((w_end - w_start) / 60.0, 1e-6)
    return {
        "scene_cuts": [round(c, 1) for c in in_cuts[:60]],
        "scene_cuts_per_min": round(len(in_cuts) / span_min, 2),
        "silences": [
            [round(s.start, 1), round(s.end, 1)]
            for s in silences
            if s.end >= w_start and s.start <= w_end and s.end - s.start >= 1.5
        ][:40],
        "loud_peaks": [[p.t, p.db] for p in peaks if w_start <= p.t <= w_end][:20],
    }


def prompt_view(a: Analysis, w_start: float, w_end: float) -> dict[str, object]:
    """The `analysis` variable of `clip_select.md`: frame facts, facecam, faces and the window's events."""
    return {
        "width": a.width,
        "height": a.height,
        "fps": a.fps,
        "facecam": a.facecam.model_dump() if a.facecam else None,
        "n_stable_faces": a.n_stable_faces,
        "faces_detected": a.faces_detected,
        "window": window_view(a.scene_cuts, a.silences, a.loudness.peaks, w_start, w_end),
    }
