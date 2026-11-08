"""C6 audio: the boost-then-limit stage that lets loudnorm reach the target on quiet, peaky audio."""
from __future__ import annotations

from pathlib import Path

import pytest

from ns_common import ffmpeg
from ns_studio.media import audio

needs_ffmpeg = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not available")


def _m(i: float, tp: float) -> dict[str, float]:
    return {"input_i": i, "input_tp": tp}


def test_linear_possible_uses_the_given_targets() -> None:
    assert not audio.linear_possible(-20.38, -0.47, i=-14.0, tp=-1.5)
    assert audio.linear_possible(-20.0, -7.5, i=-14.0, tp=-1.5)
    assert audio.linear_possible(-20.0, -10.0, i=-16.0, tp=-1.5)
    assert not audio.linear_possible(-20.0, -7.0, i=-14.0, tp=-1.5)


def test_boost_limiter_ceiling_does_not_follow_the_clip() -> None:
    assert audio.limiter_ceiling_db(-1.5) == -3.5
    assert audio.boost_limiter(6.4, -1.5) == "volume=6.40dB,alimiter=limit=0.6683:attack=2:release=50:level=0"
    assert audio.boost_limiter(38.0, -1.5).split(",")[1] == audio.boost_limiter(6.4, -1.5).split(",")[1]


def test_limit_to_linear_stops_at_the_first_fit_and_reports_the_boost_used() -> None:
    seen: list[str] = []

    def measure(pre: str) -> dict[str, float] | None:
        seen.append(pre)
        return _m(-17.0, -3.5) if len(seen) == 1 else _m(-14.2, -3.5)

    pre, got, boost = audio.limit_to_linear(measure, _m(-20.38, -0.47), i=-14.0, tp=-1.5)
    assert len(seen) == 2 and pre == seen[1] and got == _m(-14.2, -3.5)
    assert boost == pytest.approx(9.38) and pre.startswith("volume=9.38dB,")


def test_limit_to_linear_gives_up_after_the_last_pass_and_on_a_failed_measurement() -> None:
    calls: list[str] = []

    def never(pre: str) -> dict[str, float] | None:
        calls.append(pre)
        return _m(-17.0, -1.0)

    pre, got, boost = audio.limit_to_linear(never, _m(-20.38, -0.47), i=-14.0, tp=-1.5)
    assert len(calls) == audio.MAX_LIMITER_PASSES and pre == calls[-1] and got == _m(-17.0, -1.0)
    assert boost == pytest.approx(6.38 + 3 * 3.0)
    assert audio.limit_to_linear(lambda pre: None, _m(-20.38, -0.47), i=-14.0, tp=-1.5) == ("", None, 0.0)


def make_peaky_wav(path: Path) -> None:
    """9 s mono 48 kHz: a quiet 220 Hz tone with a 5 ms low-passed noise click every 70 ms.

    About -20.6 LUFS with a -0.8 dBTP peak, the shape of a quiet phone recording: +6.6 dB cannot be applied
    linearly, and the old chain ended at -20.8 LUFS (it lifted nothing).
    """
    graph = (
        "[0:a]volume=0.6[s];"
        "[1:a]lowpass=f=3000,aeval=exprs='val(0)*1.3*lt(mod(t,0.07),0.005)':c=same[c];"
        "[s][c]amix=inputs=2:normalize=0,aformat=channel_layouts=mono[a]"
    )
    ffmpeg.run(
        ["-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=9",
         "-f", "lavfi", "-i", "anoisesrc=d=9:sample_rate=48000:amplitude=0.9:seed=7",
         "-filter_complex", graph, "-map", "[a]", "-c:a", "pcm_s16le", str(path)]
    )


@needs_ffmpeg
def test_loudnorm_reaches_the_target_on_quiet_peaky_audio(tmp_path: Path) -> None:
    src, out = tmp_path / "peaky.wav", tmp_path / "out.wav"
    make_peaky_wav(src)
    before = audio.measure_loudness(src)
    assert before.lufs < -19.0 and not audio.linear_possible(before.lufs, before.true_peak, i=-14.0, tp=-1.5)
    audio.loudnorm(src, out, i=-14.0, tp=-1.5)
    got = audio.measure_loudness(out)
    assert abs(got.lufs - -14.0) <= 0.5
    assert got.true_peak <= -1.0
