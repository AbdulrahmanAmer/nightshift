from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from ns_common import ffmpeg
from ns_studio.policy import final_checks as fc
from ns_studio.policy import phash
from tests.helpers import make_channel, make_ctx

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
pytestmark = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not installed")
FAKE_BUT_CLIPS = {"llm", "tts", "whisper", "embed"}  # real clip sources => uniqueness check is active


def _video(path: Path, source: str, seconds: int = 3, audio: str | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["-f", "lavfi", "-i", f"{source}=size=128x128:rate=10"]
    if audio:
        args += ["-f", "lavfi", "-i", audio]
    args += ["-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    args += ["-c:a", "aac"] if audio else ["-an"]
    ffmpeg.run([*args, str(path)])
    return path


# ---------------------------------------------------------------- phash
def test_phash_identical_and_different_content(tmp_path: Path):
    a = _video(tmp_path / "a.mp4", "testsrc2")
    b = _video(tmp_path / "b.mp4", "testsrc2")
    c = _video(tmp_path / "c.mp4", "mandelbrot")
    ha, hb, hc = phash.frame_hashes(a), phash.frame_hashes(b), phash.frame_hashes(c)
    assert len(ha) >= 8
    assert phash.distance(ha, hb) <= 2
    assert phash.distance(ha, hc) > fc.PHASH_MEAN_MAX


def test_phash_encode_decode_roundtrip():
    hs = [0, 1, 2**63 + 5]
    assert phash.decode(phash.encode(hs)) == hs
    assert phash.decode("zz") == []
    assert phash.distance([], [1]) == 64.0
    assert phash.distance([0b1011], [0b0001]) == 2


def test_hash_frame_is_deterministic():
    rng = np.random.default_rng(1)
    frame = rng.integers(0, 255, size=1024, dtype=np.uint8)
    assert phash.hash_frame(frame) == phash.hash_frame(frame.copy())


# ---------------------------------------------------------------- render uniqueness
def test_render_uniqueness_across_jobs(tmp_home: Path):
    src = _video(tmp_home / "src.mp4", "testsrc2")
    other = _video(tmp_home / "other.mp4", "mandelbrot")
    first = make_ctx(tmp_home, fake=FAKE_BUT_CLIPS)
    first.file("long").mkdir()
    first.file("long/master.mp4").write_bytes(src.read_bytes())
    assert fc.RenderUniqueness().run(first).passed
    assert (first.paths.data / fc.INDEX_NAME).exists()
    second = make_ctx(tmp_home, fake=FAKE_BUT_CLIPS)
    second.file("long").mkdir()
    second.file("long/master.mp4").write_bytes(src.read_bytes())
    res = fc.RenderUniqueness().run(second)
    assert not res.passed and first.job_id in res.detail
    third = make_ctx(tmp_home, fake=FAKE_BUT_CLIPS)
    third.file("long").mkdir()
    third.file("long/master.mp4").write_bytes(other.read_bytes())
    assert fc.RenderUniqueness().run(third).passed


def test_short_must_differ_from_its_own_master(tmp_home: Path):
    src = _video(tmp_home / "src.mp4", "testsrc2")
    ctx = make_ctx(tmp_home, fake=FAKE_BUT_CLIPS)
    ctx.file("long").mkdir()
    ctx.file("shorts/s1").mkdir(parents=True)
    ctx.file("long/master.mp4").write_bytes(src.read_bytes())
    ctx.file("shorts/s1/short.mp4").write_bytes(src.read_bytes())
    res = fc.RenderUniqueness().run(ctx)
    assert not res.passed and "long and s1" in res.detail


def test_render_uniqueness_skipped_for_fake_footage(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.file("long").mkdir()
    ctx.file("long/master.mp4").write_bytes(b"x")
    assert "skipped" in fc.RenderUniqueness().run(ctx).detail
    assert "no renders" in fc.RenderUniqueness().run(make_ctx(tmp_home)).detail


# ---------------------------------------------------------------- long runtime
def _master(ctx) -> None:
    ctx.file("long").mkdir(exist_ok=True)
    ctx.file("long/master.mp4").write_bytes(b"x")


def test_long_runtime_floor_and_midrolls(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    assert "no master" in fc.LongRuntime().run(ctx).detail
    _master(ctx)
    short = fc.LongRuntime(lambda p: 400.0).run(ctx)
    assert not short.passed and "400" in short.detail
    assert fc.LongRuntime(lambda p: 600.0).run(ctx).passed
    ctx.write_json("long/chapters.json", {"chapters": [], "midroll_suggestions_s": [60.0, 300.0, 560.0]})
    res = fc.LongRuntime(lambda p: 600.0).run(ctx)
    assert not res.passed and "60 s" in res.detail and "560 s" in res.detail and "300" not in res.detail
    ctx.write_json("long/chapters.json", {"midroll_suggestions_s": [150.0, 300.0]})
    assert fc.LongRuntime(lambda p: 600.0).run(ctx).passed


def test_long_runtime_boundary(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _master(ctx)
    assert fc.LongRuntime(lambda p: 510.0).run(ctx).passed
    assert not fc.LongRuntime(lambda p: 509.9).run(ctx).passed


# ---------------------------------------------------------------- clip reuse
def _usage(ctx, clip: str, channel: str, job: str, days_ago: float, item: str = "s1") -> None:
    ctx.db.execute(
        "insert or ignore into channels(slug, display_name, created_at) values (?,?,?)",
        (channel, channel, "2026-01-01T00:00:00"),
    )
    ctx.db.execute(
        "insert or ignore into clips(source, clip_id, url, license, fetched_at) values (?,?,?,?,?)",
        ("pexels", clip, "u", "l", "2026-01-01T00:00:00"),
    )
    when = (NOW - timedelta(days=days_ago)).isoformat()
    ctx.db.execute(
        "insert into clip_usage(source, clip_id, channel, job_id, item, used_at) values (?,?,?,?,?,?)",
        ("pexels", clip, channel, job, item, when),
    )


def test_clip_reuse_windows(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    check = fc.ClipReuse(clock=lambda: NOW)
    _usage(ctx, "1", ctx.channel.slug, ctx.job_id, 0)
    assert check.run(ctx).passed
    _usage(ctx, "1", ctx.channel.slug, "old-20261025-aaaa", 7)
    res = check.run(ctx)
    assert not res.passed and "this channel" in res.detail
    ctx.db.execute("delete from clip_usage where job_id != ?", (ctx.job_id,))
    _usage(ctx, "1", ctx.channel.slug, "old-20261001-aaaa", 20)
    assert check.run(ctx).passed  # 14-day window over
    _usage(ctx, "1", "other-ch", "oth-20261015-bbbb", 20)
    res2 = check.run(ctx)
    assert not res2.passed and "other-ch" in res2.detail
    ctx.db.execute("delete from clip_usage where job_id != ?", (ctx.job_id,))
    _usage(ctx, "1", "other-ch", "oth-20260901-bbbb", 40)
    assert check.run(ctx).passed  # 30-day window over


def test_clip_reuse_twice_in_one_video(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _usage(ctx, "9", ctx.channel.slug, ctx.job_id, 0, "long")
    _usage(ctx, "9", ctx.channel.slug, ctx.job_id, 0, "long")
    res = fc.ClipReuse(clock=lambda: NOW).run(ctx)
    assert not res.passed and "twice" in res.detail
    # the same clip in a Short and in its own master is allowed
    ctx2 = make_ctx(tmp_home)
    _usage(ctx2, "8", ctx2.channel.slug, ctx2.job_id, 0, "long")
    _usage(ctx2, "8", ctx2.channel.slug, ctx2.job_id, 0, "s1")
    assert fc.ClipReuse(clock=lambda: NOW).run(ctx2).passed


# ---------------------------------------------------------------- audio
def test_quiet_floor_levels():
    sr = fc.SR
    t = np.arange(sr * 2) / sr
    tone = 0.3 * np.sin(2 * np.pi * 440 * t)
    gated = tone.copy()
    gated[: sr // 2] = 0.0
    assert fc.quiet_floor_db(gated) < fc.BED_FLOOR_DBFS
    assert fc.quiet_floor_db(0.05 * tone) > fc.BED_FLOOR_DBFS
    assert fc.quiet_floor_db(np.zeros(10)) == -120.0


_PAUSED = r"aevalsrc=if(lt(mod(t\,1)\,0.5)\,0.3*sin(2*PI*440*t)\,0):s=16000:d=3"
_BED = "sine=frequency=440:sample_rate=16000:duration=3,volume=0.1"


def test_audio_only_voice_passes_for_narration_with_pauses(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _video(ctx.file("long/master.mp4"), "testsrc2", audio=_PAUSED)
    res = fc.AudioOnlyVoice().run(ctx)
    assert res.passed, res.detail


def test_audio_only_voice_flags_music_bed(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _video(ctx.file("long/master.mp4"), "testsrc2", audio=_BED)
    res = fc.AudioOnlyVoice().run(ctx)
    assert not res.passed and "music bed" in res.detail


def test_audio_only_voice_flags_second_audio_stream(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    out = ctx.file("long/master.mp4")
    out.parent.mkdir(parents=True)
    ffmpeg.run(
        ["-f", "lavfi", "-i", "testsrc2=size=128x128:rate=10", "-f", "lavfi", "-i", _PAUSED,
         "-f", "lavfi", "-i", _PAUSED, "-map", "0:v", "-map", "1:a", "-map", "2:a", "-t", "3",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(out)]
    )
    res = fc.AudioOnlyVoice().run(ctx)
    assert not res.passed and "2 audio streams" in res.detail


# ---------------------------------------------------------------- disclosure
def test_disclosure_advice(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("story.json", {"text": "A man heard knocking in an old farmhouse."})
    ctx.write_json("policy_notes.json", {"intensity": "low", "real_setting": False})  # the text gate's answer
    a = fc.compute_advice(ctx, place_ner=lambda t: [])
    assert a.ai_disclosure == "no" and not a.made_for_kids and not a.age_restrict_suggested
    ctx.write_json("policy_notes.json", {"intensity": "low", "real_setting": True})
    assert fc.compute_advice(ctx, place_ner=lambda t: []).ai_disclosure == "yes"
    ctx.write_json("policy_notes.json", {"intensity": "low", "real_setting": False})
    ctx.write_json("story.json", {"text": "Honestly, this really happened to me."})
    assert fc.compute_advice(ctx, place_ner=lambda t: []).ai_disclosure == "yes"
    mature = make_ctx(tmp_home, channel=make_channel(slug="grim", audience="mature"))
    mature.write_json("policy_notes.json", {"intensity": "high", "real_setting": False})
    assert fc.compute_advice(mature, place_ner=lambda t: []).age_restrict_suggested
    assert fc.DisclosureAdvice().run(mature).passed


def test_final_checks_order():
    assert [c.name for c in fc.final_checks()] == [
        "unsafe_topics",
        "render_uniqueness",
        "long_runtime",
        "clip_reuse",
        "audio_only_voice",
        "short_hook",
        "disclosure_advice",
    ]
