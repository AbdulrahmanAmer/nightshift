"""C6 audio: the boost-then-limit stage that lets loudnorm reach the target on quiet, peaky audio."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ns_common import ffmpeg
from ns_common.logging import NsLogger
from ns_studio.media import audio

needs_ffmpeg = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not available")


def _records(tmp_path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for f in (tmp_path / "logs").glob("*.jsonl") for ln in f.read_text().splitlines()]


def _m(i: float, tp: float) -> dict[str, float]:
    return {"input_i": i, "input_tp": tp}


def test_linear_possible_uses_the_given_targets() -> None:
    assert not audio.linear_possible(-20.38, -0.47, i=-14.0, tp=-1.5)
    assert audio.linear_possible(-20.0, -7.5, i=-14.0, tp=-1.5)
    assert audio.linear_possible(-20.0, -10.0, i=-16.0, tp=-1.5)
    assert not audio.linear_possible(-20.0, -7.0, i=-14.0, tp=-1.5)


def test_boost_limiter_ceiling_does_not_follow_the_clip() -> None:
    assert audio.limiter_ceiling_db(-1.5) == -3.5
    lim = "alimiter=limit=0.6683:attack=2:release=120:level=0"
    assert audio.boost_limiter(6.4, -1.5) == f"volume=6.40dB,{lim}"
    assert audio.boost_limiter(38.0, -1.5).split(",")[1] == audio.boost_limiter(6.4, -1.5).split(",")[1]


def test_limit_to_linear_stops_at_the_first_fit_and_reports_the_boost_used() -> None:
    seen: list[str] = []

    def measure(pre: str) -> dict[str, float] | None:
        seen.append(pre)
        return _m(-17.0, -3.5) if len(seen) == 1 else _m(-14.2, -3.5)

    pre, got, boost = audio.limit_to_linear(measure, _m(-20.38, -0.47), i=-14.0, tp=-1.5)
    assert len(seen) == 2 and pre == seen[1] and got == _m(-14.2, -3.5)
    assert boost == pytest.approx(9.38) and pre.startswith("volume=9.38dB,")


def test_limit_to_linear_accepts_a_near_fit_on_the_first_try() -> None:
    seen: list[str] = []

    def measure(pre: str) -> dict[str, float] | None:
        seen.append(pre)
        return _m(-14.9, -3.5)               # 0.9 LU short; the lifted peak lands at -2.6, under -1.5 - 0.7

    pre, got, boost = audio.limit_to_linear(measure, _m(-20.38, -0.47), i=-14.0, tp=-1.5)
    assert len(seen) == 1 and pre == seen[0] and got == _m(-14.9, -3.5) and boost == pytest.approx(6.38)


def test_limit_to_linear_steps_by_the_secant_and_caps_the_step() -> None:
    seen: list[str] = []
    answers = [_m(-17.0, -3.5), _m(-15.8, -3.5), _m(-14.2, -3.5)]

    def measure(pre: str) -> dict[str, float] | None:
        seen.append(pre)
        return answers[len(seen) - 1]

    _, _, boost = audio.limit_to_linear(measure, _m(-20.38, -0.47), i=-14.0, tp=-1.5)
    # first step: the raw shortfall 3.0; then 1.8 over the slope 1.2 / 3.0 = 4.5
    assert [float(p.split("volume=")[1].split("dB")[0]) for p in seen] == pytest.approx([6.38, 9.38, 13.88])
    assert boost == pytest.approx(13.88)
    flat = [_m(-17.0, -3.5), _m(-16.9, -3.5), _m(-14.2, -3.5)]       # almost no gain for 3 dB: step is capped
    seen.clear()
    answers[:] = flat
    audio.limit_to_linear(measure, _m(-20.38, -0.47), i=-14.0, tp=-1.5)
    assert [float(p.split("volume=")[1].split("dB")[0]) for p in seen] == pytest.approx([6.38, 9.38, 15.38])


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
    audio.loudnorm(src, out, i=-14.0, tp=-1.5, log=NsLogger("studio", tmp_path / "logs", echo=False))
    got = audio.measure_loudness(out)
    assert abs(got.lufs - -14.0) <= 0.5
    assert got.true_peak <= -1.0
    recs = _records(tmp_path)
    (rec,) = [r for r in recs if r["event"] == "loudness_limited"]
    assert rec["data"]["ceiling_db"] == -3.5 and rec["data"]["boost_db"] > 6.0
    assert rec["data"]["input_i"] < 0 and rec["data"]["input_tp"] < -2.0


@needs_ffmpeg
def test_loudnorm_warns_and_falls_back_when_the_peak_stage_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = tmp_path / "peaky.wav"
    make_peaky_wav(src)
    log = NsLogger("studio", tmp_path / "logs", echo=False)

    def broken(args: list[str], **kw: Any) -> Any:
        if any("alimiter" in a for a in args):
            raise audio.NsError("NS-E401", "alimiter missing")
        return real(args, **kw)

    real = audio.ff.run
    monkeypatch.setattr(audio.ff, "run", broken)
    audio.loudnorm(src, tmp_path / "out.wav", i=-14.0, tp=-1.5, log=log)       # falls back to plain loudnorm
    recs = _records(tmp_path)
    assert [r["event"] for r in recs] == ["loudness_limit_failed"] and recs[0]["level"] == "WARN"
    assert recs[0]["data"]["code"] == "NS-E401"
