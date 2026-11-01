"""Audio helpers on FFmpeg: loudness measurement and normalization, assembly, silence (component C6)."""

from __future__ import annotations

import json
import math
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ns_common import ffmpeg as ff
from ns_common.errors import NsError

_NUM = r"(-?(?:\d+(?:\.\d+)?|inf))"
_I_RE = re.compile(rf"\bI:\s*{_NUM} LUFS")
_TP_RE = re.compile(rf"\bPeak:\s*{_NUM} dBFS")
_SIL_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SIL_END = re.compile(r"silence_end:\s*(-?[\d.]+)")
_LOUDNORM_JSON = re.compile(r'\{\s*"input_i"[^}]*\}', re.DOTALL)


@dataclass(frozen=True)
class Loudness:
    """Integrated loudness (LUFS) and true peak (dBTP) of an audio stream."""

    lufs: float
    true_peak: float


def _to_float(s: str) -> float:
    s = s.strip()
    if s.endswith("inf"):
        return -math.inf if s.startswith("-") else math.inf
    return float(s)


def parse_ebur128(stderr: str) -> Loudness:
    """Parse the `Summary:` block of FFmpeg's ebur128 filter; NaN values when it is absent."""
    idx = stderr.rfind("Summary:")
    tail = stderr[idx:] if idx >= 0 else ""
    i_m = _I_RE.search(tail)
    tp_m = _TP_RE.search(tail)
    return Loudness(
        lufs=_to_float(i_m.group(1)) if i_m else math.nan,
        true_peak=_to_float(tp_m.group(1)) if tp_m else math.nan,
    )


def measure_loudness(path: Path) -> Loudness:
    """Measure integrated loudness and true peak of the first audio stream of `path` with ebur128."""
    proc = ff.run(["-i", str(path), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"])
    return parse_ebur128(proc.stderr)


def measure_lufs(wav: Path) -> tuple[float, float]:
    """(integrated LUFS, true peak dBTP) of `wav`."""
    m = measure_loudness(wav)
    return m.lufs, m.true_peak


def parse_loudnorm_json(stderr: str) -> dict[str, float]:
    """Extract the JSON block printed by `loudnorm=print_format=json` as floats."""
    blocks = _LOUDNORM_JSON.findall(stderr)
    if not blocks:
        raise NsError("NS-E401", "loudnorm measurement block not found")
    raw = json.loads(blocks[-1])
    return {k: _to_float(str(v)) for k, v in raw.items() if k != "normalization_type"}


def loudnorm(
    in_wav: Path, out_wav: Path, *, i: float = -16.0, tp: float = -1.5, lra: float = 11.0, sr: int = 48000
) -> None:
    """Two-pass linear loudnorm of `in_wav` to `out_wav` (mono, `sr` Hz, 16-bit).

    A final measurement corrects any residual offset above 0.5 LU with a plain gain, never exceeding `tp`.
    Raises NsError NS-E401 when FFmpeg fails.
    """
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    target = f"I={i}:TP={tp}:LRA={lra}"
    first = ff.run(
        ["-i", str(in_wav), "-vn", "-af", f"loudnorm={target}:print_format=json", "-f", "null", "-"]
    )
    m = parse_loudnorm_json(first.stderr)
    if not math.isfinite(m.get("input_i", math.nan)):
        ff.run(["-i", str(in_wav), "-vn", "-ar", str(sr), "-ac", "1", "-c:a", "pcm_s16le", str(out_wav)])
        return
    measured = (
        f"measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
        f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}"
    )
    ff.run(
        ["-i", str(in_wav), "-vn", "-af", f"loudnorm={target}:{measured}:linear=true:print_format=summary",
         "-ar", str(sr), "-ac", "1", "-c:a", "pcm_s16le", str(out_wav)]
    )
    got = measure_loudness(out_wav)
    if math.isfinite(got.lufs) and abs(got.lufs - i) > 0.5:
        gain = i - got.lufs
        if math.isfinite(got.true_peak):
            gain = min(gain, tp - got.true_peak)
        tmp = out_wav.with_suffix(".fix.wav")
        ff.run(["-i", str(out_wav), "-af", f"volume={gain:.2f}dB", "-ar", str(sr), "-ac", "1",
                "-c:a", "pcm_s16le", str(tmp)])
        shutil.move(str(tmp), str(out_wav))


def assemble_audio(segments: Sequence[Path | float], out_wav: Path, *, sr: int = 48000) -> Path:
    """Concatenate audio files (Path) and silences (float seconds) into one mono WAV at `sr` Hz."""
    if not segments:
        raise NsError("NS-E401", "nothing to assemble")
    args: list[str] = []
    chains: list[str] = []
    for k, seg in enumerate(segments):
        if isinstance(seg, Path):
            args += ["-i", str(seg)]
        else:
            args += ["-f", "lavfi", "-t", f"{seg:.3f}", "-i", f"anullsrc=r={sr}:cl=mono"]
        chains.append(f"[{k}:a]aformat=sample_rates={sr}:channel_layouts=mono[a{k}]")
    joined = "".join(f"[a{k}]" for k in range(len(segments)))
    graph = ";".join(chains) + f";{joined}concat=n={len(segments)}:v=0:a=1[out]"
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    ff.run([*args, "-filter_complex", graph, "-map", "[out]", "-ar", str(sr), "-ac", "1",
            "-c:a", "pcm_s16le", str(out_wav)])
    return out_wav


def parse_silences(stderr: str) -> list[tuple[float, float | None]]:
    """Parse silencedetect output into (start, end) pairs; end is None for a silence running to the end."""
    out: list[tuple[float, float | None]] = []
    for line in stderr.splitlines():
        s = _SIL_START.search(line)
        if s:
            out.append((float(s.group(1)), None))
            continue
        e = _SIL_END.search(line)
        if e and out and out[-1][1] is None:
            out[-1] = (out[-1][0], float(e.group(1)))
    return out


def speech_onset_s(path: Path, *, noise_db: float = -35.0, min_silence_s: float = 0.05) -> float:
    """Seconds of leading silence in the first audio stream of `path` (0.0 when speech starts at once)."""
    proc = ff.run(["-i", str(path), "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_silence_s}",
                   "-f", "null", "-"])
    sil = parse_silences(proc.stderr)
    if sil and sil[0][0] <= 0.01:
        end = sil[0][1]
        return end if end is not None else ff.duration_s(path)
    return 0.0
