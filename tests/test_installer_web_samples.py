"""C1: helpers for the test voice and the test render (standard library only), and the v01_samples step."""

import os
import wave
from pathlib import Path

from installer.stage2.steps.v01_samples import V01Samples, sample_dir
from installer.stage2.testing import FakeUi, example_answers, make_test_context
from installer.stage2.web import samples
from ns_common.errors import NsError


def test_wav_bars_are_28_values_between_a_quarter_and_one(tmp_path: Path):
    wav = samples.write_sine_wav(tmp_path / "sine.wav", seconds=1.0)
    bars = samples.wav_bars(wav)
    assert len(bars) == 28 and all(0.25 <= b <= 1.0 for b in bars)
    assert max(bars) == 1.0 and bars[-1] > bars[0]                 # the generated tone swells towards the end
    assert abs(samples.wav_seconds(wav) - 1.0) < 0.01


def test_wav_bars_survive_silence_stereo_and_junk(tmp_path: Path):
    silent = tmp_path / "silent.wav"
    with wave.open(str(silent), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000)
    assert samples.wav_bars(silent) == [0.25] * 28
    stereo = tmp_path / "stereo.wav"
    with wave.open(str(stereo), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes((b"\x10\x27\x10\x27") * 4000)
    assert len(samples.wav_bars(stereo)) == 28 and samples.wav_bars(stereo)[0] == 1.0
    junk = tmp_path / "junk.wav"
    junk.write_bytes(b"not a wav")
    assert samples.wav_bars(junk) == [0.25] * 28 and samples.wav_seconds(junk) == 0.0
    assert samples.wav_bars(tmp_path / "missing.wav") == [0.25] * 28
    assert len(samples.wav_bars(silent, count=10)) == 10


def test_scratch_home_gets_only_the_runtime_json_and_the_channel(tmp_path: Path):
    home = tmp_path / "home"
    (home / "config" / "channels").mkdir(parents=True)
    (home / "config" / "runtime.json").write_text("{}", encoding="utf-8")
    (home / "config" / "channels" / "after-dark.yaml").write_text("slug: after-dark", encoding="utf-8")
    (home / "config" / "channels" / "other.yaml").write_text("slug: other", encoding="utf-8")
    (home / "config" / "answers.json").write_text("{}", encoding="utf-8")
    (home / "outbox").mkdir()
    (home / "outbox" / "video.mp4").write_bytes(b"x")
    scratch = tmp_path / "scratch"
    copied = samples.seed_scratch_home(home, scratch, "after-dark")
    found = sorted(p.relative_to(scratch).as_posix() for p in scratch.rglob("*") if p.is_file())
    assert found == ["config/channels/after-dark.yaml", "config/runtime.json"]
    assert len(copied) == 2
    assert samples.seed_scratch_home(home, tmp_path / "s2", "no-such-channel") == [
        tmp_path / "s2" / "config" / "runtime.json"]


def test_newest_file_and_command_lines(tmp_path: Path):
    (tmp_path / "a").mkdir()
    old = tmp_path / "a" / "old.mp4"
    new = tmp_path / "new.mp4"
    old.write_bytes(b"1")
    new.write_bytes(b"2")
    os.utime(old, (1_000_000, 1_000_000))
    assert samples.newest_file(tmp_path, "*.mp4") == new
    assert samples.newest_file(tmp_path / "nowhere", "*.mp4") is None
    grab = samples.frame_grab_argv("ffmpeg", Path("v.mp4"), Path("f.jpg"))
    assert grab[:2] == ["ffmpeg", "-y"] and "-frames:v" in grab and grab[-1] == "f.jpg"
    assert samples.duration_argv("ffprobe", Path("v.mp4"))[-1] == "v.mp4"


def test_the_step_writes_a_voice_sample_in_simulate_mode_and_reports_it(tmp_path: Path):
    ui = FakeUi()
    seen: list[tuple[str, Path, float | None]] = []
    ui.sample = lambda kind, path, seconds=None: seen.append((kind, path, seconds))  # type: ignore
    ctx = make_test_context(tmp_path, ui=ui, raw_answers=example_answers(), resolve=True)
    step = V01Samples()
    assert step.optional and step.id == "v01_samples" and not step.check(ctx)
    step.run(ctx)
    wav = sample_dir(ctx) / "voice-sample.wav"
    assert wav.is_file() and seen == [("voice", wav, seen[0][2])] and seen[0][2] > 1.0
    assert ctx.flag("samples") == "done" and step.check(ctx)


def test_a_failing_voice_command_ends_in_the_warning_ns_w403(tmp_path: Path):
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, simulate=False)
    step = V01Samples()
    try:
        step.run(ctx)
    except NsError as e:
        assert e.code == "NS-W403" and "voice sample" in e.detail and "test render" in e.detail
    else:
        raise AssertionError("expected NS-W403")
    assert ctx.flag("samples") == "failed" and step.check(ctx)         # a failed attempt is not repeated
