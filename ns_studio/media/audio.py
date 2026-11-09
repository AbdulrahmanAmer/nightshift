"""Audio helpers on FFmpeg: loudness measurement and normalization, assembly, silence (component C6)."""

from __future__ import annotations

import json
import math
import re
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_studio.media.diskfull import run_ffmpeg

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


LINEAR_TOLERANCE_DB = 0.1   # loudnorm limiting a peak by a tenth of a dB moves the loudness by less
OVERSHOOT_DB = 1.0          # A clipped peak grows by up to this much once it is resampled and AAC-encoded.
LIMITER_HEADROOM_DB = OVERSHOOT_DB + 1.0   # The ceiling sits this far under `tp`: that growth and 1 dB more.
FIT_MARGIN_DB = 0.7         # `limit_to_linear` stops once the lifted peak is this far under `tp`.
LIMITER_ATTACK_MS = 2.0
LIMITER_RELEASE_MS = 120.0  # long enough not to ripple on low voices
LIMITER_RATE_HZ = 192000    # the limiter works at this rate so it holds true peaks, not only sample peaks
OUTPUT_RATE_HZ = 48000      # the rate every stored or delivered audio file is written at
MAX_LIMITER_PASSES = 2      # each pass is a whole FFmpeg run over the audio; the second is the best guess
MAX_BOOST_STEP_DB = 15.0
AIM_PAST_DB = 0.3           # the second boost aims this far over the target; loudnorm then only trims
MIN_LOUDNESS_PER_DB = 0.15  # floor for the loudness a dB of boost buys: a flat curve must not ask for 40 dB

FINAL_LUFS = -14.0          # what a Short or a master is delivered at (spec S1, §7.7)
FINAL_TP = -1.5
VOICE_LUFS = -16.0          # what the voice stage leaves each narration item at
# The voice stage keeps peaks where the final +2 dB lift (-16 to -14 LUFS) still ends under `FINAL_TP`, so the
# narration is limited once, at the voice stage, and the Short or master stage only has to apply a plain gain.
# loudnorm may land up to half a LU under -16 (it only corrects beyond 0.5), and the lift grows by as much.
VOICE_TP = FINAL_TP - (FINAL_LUFS - VOICE_LUFS) - 0.5


def linear_possible(input_i: float, input_tp: float, *, i: float, tp: float) -> bool:
    """Whether a plain gain from `input_i` LUFS to `i` keeps the true peak at or under `tp`.

    loudnorm stays linear only then; otherwise it limits on the fly and ends short of the target (a
    -20.4 LUFS / -0.5 dBTP clip came out at -17.1 LUFS for a -14 target).
    """
    return input_tp + (i - input_i) <= tp + LINEAR_TOLERANCE_DB


def limiter_ceiling_db(tp: float) -> float:
    """The sample-peak ceiling (dBFS) `boost_limiter` holds the audio to."""
    return tp - LIMITER_HEADROOM_DB


def boost_limiter(boost_db: float, tp: float) -> str:
    """Lift by `boost_db`, then hold the peaks at a fixed ceiling just under `tp`.

    The ceiling stays well inside alimiter's 0.0625..1 range whatever the clip's level, which a ceiling
    placed relative to a very quiet clip would not. The limiter runs oversampled: it holds sample peaks, and
    once the result is written at 48 kHz the true peak of real speech reads 1 to 2 dB above them, which put
    narration back over the Short's limit and made the Short limit it a second time.
    """
    limit = 10 ** (limiter_ceiling_db(tp) / 20)
    return (
        f"aresample={LIMITER_RATE_HZ},volume={boost_db:.2f}dB,alimiter=limit={limit:.4g}"
        f":attack={LIMITER_ATTACK_MS:g}:release={LIMITER_RELEASE_MS:g}:level=0,aresample={OUTPUT_RATE_HZ}"
    )


def limit_to_linear(
    measure: Callable[[str], dict[str, float] | None], source: dict[str, float], *, i: float, tp: float
) -> tuple[str, dict[str, float] | None, float]:
    """The `boost_limiter` stage that lets loudnorm reach `i` linearly, with its measurement and boost.

    `source` is the loudnorm pass-1 report of the unprocessed audio; `measure(pre)` repeats pass 1 after
    `pre`. The first boost brings the loudness to `i`; the limiter then takes some off. The loss starts where
    the lifted peak reaches the ceiling and grows with the boost from there, so the next boost comes from the
    secant through that knee (no loss) and the first measurement, aimed `AIM_PAST_DB` over the target: a
    landing above it is fixed by loudnorm's linear gain alone, one just under it would leave the loudness
    short. A step is at most `MAX_BOOST_STEP_DB`. It stops when the lifted peak is `FIT_MARGIN_DB` under `tp`,
    or after `MAX_LIMITER_PASSES` (the last attempt is returned): speech takes two FFmpeg runs, not four.
    `("", None, 0.0)` when a measurement fails: the caller keeps the plain chain.
    """
    boost = i - source["input_i"]
    pre, got, used = "", None, boost
    knee = limiter_ceiling_db(tp) - source["input_tp"]   # the boost at which peaks first touch the ceiling
    prior = (knee, source["input_i"] + knee)             # (boost, loudness) of the point before: no loss
    for _ in range(MAX_LIMITER_PASSES):
        pre, used = boost_limiter(boost, tp), boost
        got = measure(pre)
        if got is None:
            return "", None, 0.0
        shortfall = i - got["input_i"]
        if got["input_tp"] + shortfall <= tp - FIT_MARGIN_DB:
            break
        boost_span = boost - prior[0]
        slope = (got["input_i"] - prior[1]) / boost_span if abs(boost_span) > 0.05 else 1.0
        slope = max(MIN_LOUDNESS_PER_DB, min(1.0, slope))   # a dB of boost never buys more than a dB
        prior = (boost, got["input_i"])
        step = (shortfall + AIM_PAST_DB) / slope
        boost += max(-MAX_BOOST_STEP_DB, min(MAX_BOOST_STEP_DB, step))
    return pre, got, used


def _pass_one(in_wav: Path, target: str, stage: str, log: NsLogger | None = None) -> dict[str, float] | None:
    """Loudnorm pass 1 over `in_wav` after the filter `stage`; None when FFmpeg fails or it is silent."""
    af = f"{stage},loudnorm={target}:print_format=json"
    args = ["-i", str(in_wav), "-vn", "-af", af, "-f", "null", "-"]
    try:
        got = parse_loudnorm_json(ff.run(args).stderr)
    except NsError as e:
        if log:
            log.warn("loudness_limit_failed", "pass 1 after the peak stage failed; plain loudnorm instead",
                     code=e.code, stage=stage)
        return None
    return got if all(math.isfinite(got.get(k, math.nan)) for k in ("input_i", "input_tp")) else None


def loudnorm(
    in_wav: Path, out_wav: Path, *, i: float = -16.0, tp: float = -1.5, lra: float = 11.0, sr: int = 48000,
    log: NsLogger | None = None,
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
        run_ffmpeg(
            ["-i", str(in_wav), "-vn", "-ar", str(sr), "-ac", "1", "-c:a", "pcm_s16le", str(out_wav)],
            folder=out_wav.parent,
            out=out_wav,
        )
        return
    pre = ""
    if not linear_possible(m["input_i"], m["input_tp"], i=i, tp=tp):  # quiet and peaky: lift and limit first
        pre, limited, boost = limit_to_linear(lambda st: _pass_one(in_wav, target, st, log), m, i=i, tp=tp)
        if limited and log:
            log.info("loudness_limited", boost_db=round(boost, 2), ceiling_db=limiter_ceiling_db(tp),
                     input_i=limited["input_i"], input_tp=limited["input_tp"])
        m = limited or m
    measured = (
        f"measured_I={m['input_i']}:measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}"
        f":measured_thresh={m['input_thresh']}:offset={m['target_offset']}"
    )
    run_ffmpeg(
        ["-i", str(in_wav), "-vn", "-af",
         f"{pre + ',' if pre else ''}loudnorm={target}:{measured}:linear=true:print_format=summary",
         "-ar", str(sr), "-ac", "1", "-c:a", "pcm_s16le", str(out_wav)],
        folder=out_wav.parent,
        out=out_wav,
    )
    got = measure_loudness(out_wav)
    if math.isfinite(got.lufs) and abs(got.lufs - i) > 0.5:
        gain = i - got.lufs
        if math.isfinite(got.true_peak):
            gain = min(gain, tp - got.true_peak)
        tmp = out_wav.with_suffix(".fix.wav")
        run_ffmpeg(["-i", str(out_wav), "-af", f"volume={gain:.2f}dB", "-ar", str(sr), "-ac", "1",
                    "-c:a", "pcm_s16le", str(tmp)], folder=tmp.parent, out=tmp)
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
    run_ffmpeg([*args, "-filter_complex", graph, "-map", "[out]", "-ar", str(sr), "-ac", "1",
                "-c:a", "pcm_s16le", str(out_wav)], folder=out_wav.parent, out=out_wav)
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


def cut_audio(src: Path, out_wav: Path, start_s: float, end_s: float, *, sr: int = 48000) -> Path:
    """Cut [start_s, end_s) of `src` into a mono 16-bit WAV at `sr` Hz."""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    ff.run(["-i", str(src), "-ss", f"{start_s:.3f}", "-to", f"{end_s:.3f}", "-ar", str(sr), "-ac", "1",
            "-c:a", "pcm_s16le", str(out_wav)])
    return out_wav
