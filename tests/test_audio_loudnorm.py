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
    assert audio.boost_limiter(6.4, -1.5) == f"aresample=192000,volume=6.40dB,{lim},aresample=48000"
    assert audio.boost_limiter(38.0, -1.5).split(",")[2] == audio.boost_limiter(6.4, -1.5).split(",")[2]


def test_limit_to_linear_stops_at_the_first_fit_and_reports_the_boost_used() -> None:
    seen: list[str] = []

    def measure(pre: str) -> dict[str, float] | None:
        seen.append(pre)
        return _m(-17.0, -3.5) if len(seen) == 1 else _m(-14.2, -3.5)

    pre, got, boost = audio.limit_to_linear(measure, _m(-20.38, -0.47), i=-14.0, tp=-1.5)
    # the source peak (-0.47) already stands 3.03 dB over the ceiling (-3.5), so the limiter bites from a
    # boost of -3.03 dB (-23.41 LUFS). From that knee to (6.38 dB, -17.0) the loudness climbs 0.68 LU per dB,
    # and the second boost aims 0.3 LU past the target: 6.38 + 3.3 / 0.68 = 11.22
    assert len(seen) == 2 and pre == seen[1] and got == _m(-14.2, -3.5)
    assert boost == pytest.approx(11.22, abs=0.01) and pre.startswith("aresample=192000,volume=11.22dB,")


def test_limit_to_linear_accepts_a_near_fit_on_the_first_try() -> None:
    seen: list[str] = []

    def measure(pre: str) -> dict[str, float] | None:
        seen.append(pre)
        return _m(-14.9, -3.5)               # 0.9 LU short; the lifted peak lands at -2.6, under -1.5 - 0.7

    pre, got, boost = audio.limit_to_linear(measure, _m(-20.38, -0.47), i=-14.0, tp=-1.5)
    assert len(seen) == 1 and pre == seen[0] and got == _m(-14.9, -3.5) and boost == pytest.approx(6.38)


def _boost_of(pre: str) -> float:
    return float(pre.split("volume=")[1].split("dB")[0])


def test_limit_to_linear_steps_by_the_chord_slope_and_caps_the_step() -> None:
    seen: list[str] = []
    answers = [_m(-17.0, -3.5), _m(-14.2, -3.5)]

    def measure(pre: str) -> dict[str, float] | None:
        seen.append(pre)
        return answers[len(seen) - 1]

    _, _, boost = audio.limit_to_linear(measure, _m(-20.38, -0.47), i=-14.0, tp=-1.5)
    # the slope from the knee is 0.68 LU per dB: (3.0 short + 0.3 past) / 0.68
    assert [_boost_of(p) for p in seen] == pytest.approx([6.38, 11.22], abs=0.01)
    assert boost == pytest.approx(11.22, abs=0.01)
    seen.clear()
    answers[:] = [_m(-20.3, -3.5), _m(-14.2, -3.5)]      # almost no gain for 6.4 dB: the step is capped
    audio.limit_to_linear(measure, _m(-20.38, -0.47), i=-14.0, tp=-1.5)
    assert [_boost_of(p) for p in seen] == pytest.approx([6.38, 6.38 + audio.MAX_BOOST_STEP_DB])


@pytest.mark.parametrize("knee", [0.0, -4.0, 3.0])
@pytest.mark.parametrize("loss", [0.1, 0.2, 0.3, 0.4])
@pytest.mark.parametrize("quiet", [-22.0, -20.0, -19.0])
def test_limit_to_linear_needs_two_passes_when_the_loss_follows_the_boost(
    quiet: float, loss: float, knee: float
) -> None:
    """The limiter takes `loss` of every dB of boost past its knee off: the first pass teaches the slope."""
    seen: list[str] = []

    def measure(pre: str) -> dict[str, float] | None:
        boost = _boost_of(pre)
        return _m(quiet + boost - loss * max(0.0, boost - knee), -3.5) if seen.append(pre) is None else None

    source = _m(quiet, audio.limiter_ceiling_db(-1.5) - knee)    # the peak sits `knee` dB under the ceiling
    _, got, boost = audio.limit_to_linear(measure, source, i=-14.0, tp=-1.5)
    assert got is not None and len(seen) <= 2 <= audio.MAX_LIMITER_PASSES
    shortfall = -14.0 - got["input_i"]
    assert len(seen) == 1 or shortfall <= 0.0        # a second pass aims past the target: loudnorm trims
    assert got["input_tp"] + shortfall <= -1.5 - audio.FIT_MARGIN_DB
    assert boost == pytest.approx(_boost_of(seen[-1]), abs=0.01)    # the string is rounded to 0.01 dB


def test_limit_to_linear_gives_up_after_the_last_pass_and_on_a_failed_measurement() -> None:
    calls: list[str] = []

    def never(pre: str) -> dict[str, float] | None:
        calls.append(pre)
        return _m(-17.0, -1.0)

    pre, got, boost = audio.limit_to_linear(never, _m(-20.38, -0.47), i=-14.0, tp=-1.5)
    assert len(calls) == audio.MAX_LIMITER_PASSES and pre == calls[-1] and got == _m(-17.0, -1.0)
    assert boost == pytest.approx(11.22, abs=0.01)
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
def test_narration_is_limited_once_and_the_short_adds_no_second_limiter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The voice stage holds peaks to the Short's ceiling, so the Short only lifts it by 2 dB, linearly."""
    from ns_studio.media.render import build_short_audio

    src, voice = tmp_path / "peaky.wav", tmp_path / "voice.wav"
    make_peaky_wav(src)
    log = NsLogger("studio", tmp_path / "logs", echo=False)
    audio.loudnorm(src, voice, i=audio.VOICE_LUFS, tp=audio.VOICE_TP, log=log)
    narrated = audio.measure_loudness(voice)
    assert abs(narrated.lufs - audio.VOICE_LUFS) <= 0.5
    assert audio.linear_possible(narrated.lufs, narrated.true_peak, i=audio.FINAL_LUFS, tp=audio.FINAL_TP)
    runs: list[list[str]] = []
    real = audio.ff.run

    def counting(args: list[str], **kw: Any) -> Any:
        runs.append(args)
        return real(args, **kw)

    monkeypatch.setattr(audio.ff, "run", counting)
    final = build_short_audio(None, voice, tmp_path / "raw.wav", tmp_path / "final.wav", 0.15, log=log)
    monkeypatch.undo()
    got = audio.measure_loudness(final)
    assert abs(got.lufs - audio.FINAL_LUFS) <= 0.5 and got.true_peak <= -1.0
    assert not any("alimiter" in a for args in runs for a in args)
    assert [r["event"] for r in _records(tmp_path)].count("loudness_limited") == 1     # the voice stage's


def make_wideband_wav(path: Path) -> None:
    """9 s mono 22.05 kHz, about -19 LUFS and -1.4 dBTP: a 220 Hz tone and 5 ms noise clicks up to Nyquist.

    The shape of TTS speech at its native rate: the sample peaks a limiter holds are 1 to 3 dB under the true
    peaks that appear once the file is resampled to 48 kHz.
    """
    graph = (
        "[0:a]volume=0.6[s];"
        "[1:a]lowpass=f=11000,aeval=exprs='val(0)*0.5*lt(mod(t,0.07),0.005)':c=same[c];"
        "[s][c]amix=inputs=2:normalize=0,aformat=channel_layouts=mono[a]"
    )
    ffmpeg.run(
        ["-f", "lavfi", "-i", "sine=frequency=220:sample_rate=22050:duration=9",
         "-f", "lavfi", "-i", "anoisesrc=d=9:sample_rate=22050:amplitude=0.9:seed=7",
         "-filter_complex", graph, "-map", "[a]", "-c:a", "pcm_s16le", str(path)]
    )


@needs_ffmpeg
def test_boost_limiter_holds_true_peaks_not_only_sample_peaks(tmp_path: Path) -> None:
    """Limited at the file's own rate, this clip's true peak ends about 3 dB over the ceiling at 48 kHz."""
    src, out = tmp_path / "wide.wav", tmp_path / "limited.wav"
    make_wideband_wav(src)
    limiter = audio.boost_limiter(12.0, audio.VOICE_TP)
    ffmpeg.run(["-i", str(src), "-af", limiter, "-c:a", "pcm_s16le", str(out)])
    ceiling = audio.limiter_ceiling_db(audio.VOICE_TP)
    assert audio.measure_loudness(out).true_peak <= ceiling + 0.3
    plain = tmp_path / "plain.wav"
    old_limit = 10 ** (ceiling / 20)
    sample_rate_limiter = f"volume=12dB,alimiter=limit={old_limit:.4g}:attack=2:release=120:level=0"
    ffmpeg.run(["-i", str(src), "-af", sample_rate_limiter, "-ar", "48000", "-c:a", "pcm_s16le", str(plain)])
    assert audio.measure_loudness(plain).true_peak > ceiling + 1.0       # what the sample-rate limiter left


@needs_ffmpeg
def test_the_short_adds_no_second_limiter_to_wideband_speech(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Narration at a TTS rate keeps its true peak under `VOICE_TP`, so the Short only lifts it linearly."""
    from ns_studio.media.render import build_short_audio

    src, voice = tmp_path / "wide.wav", tmp_path / "voice.wav"
    make_wideband_wav(src)
    log = NsLogger("studio", tmp_path / "logs", echo=False)
    audio.loudnorm(src, voice, i=audio.VOICE_LUFS, tp=audio.VOICE_TP, log=log)
    narrated = audio.measure_loudness(voice)
    assert abs(narrated.lufs - audio.VOICE_LUFS) <= 0.5 and narrated.true_peak <= audio.VOICE_TP + 0.1
    runs: list[list[str]] = []
    real = audio.ff.run

    def counting(args: list[str], **kw: Any) -> Any:
        runs.append(args)
        return real(args, **kw)

    monkeypatch.setattr(audio.ff, "run", counting)
    final = build_short_audio(None, voice, tmp_path / "raw.wav", tmp_path / "final.wav", 0.15, log=log)
    monkeypatch.undo()
    got = audio.measure_loudness(final)
    assert abs(got.lufs - audio.FINAL_LUFS) <= 0.5 and got.true_peak <= audio.FINAL_TP
    assert not any("alimiter" in a for args in runs for a in args)


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
