import json
from pathlib import Path

import jsonschema
import pytest

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.models import QcResult
from ns_common.paths import contracts_dir
from ns_studio.media.audio import Loudness, loudnorm
from ns_studio.media.qc import (
    QcStage,
    drift_check,
    duration_check,
    loudness_check,
    parse_blackdetect,
    run_qc,
)
from tests.helpers import make_combo, make_ctx

needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")


def make_video(
    out: Path, *, seconds: float = 3.0, size: str = "1080x1920", audio: str = "normalized",
    video: str = "testsrc2", faststart: bool = True,
) -> Path:
    """A small H.264/AAC file with controllable defects."""
    tone = out.parent / (out.stem + "_tone.wav")
    if audio == "normalized":
        raw = ff.make_tone_wav(out.parent / (out.stem + "_raw.wav"), seconds, sr=48000)
        loudnorm(raw, tone, i=-14.0, tp=-1.5)
    elif audio == "loud":
        ff.run(["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-af", "volume=4.0,alimiter=limit=0.95",
                "-t", f"{seconds}", "-ac", "1", str(tone)])
    else:  # silent
        ff.make_silence(tone, seconds, sr=48000)
    src = f"{video}=size={size}:rate=30" if video == "testsrc2" else f"color=c=black:size={size}:rate=30"
    args = ["-f", "lavfi", "-i", src, "-i", str(tone), "-t", f"{seconds}", "-c:v", "libx264", "-preset",
            "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    if faststart:
        args += ["-movflags", "+faststart"]
    ff.run([*args, str(out)])
    return out


# ---- pure checks ------------------------------------------------------------------------------------------


def test_duration_check_bucket_and_cap():
    assert duration_check(50.0, "short", 50).passed
    assert duration_check(57.4, "short", 50).passed and not duration_check(57.6, "short", 50).passed
    assert duration_check(42.6, "short", 50).passed and not duration_check(42.4, "short", 50).passed
    assert not duration_check(180.0, "short", 175).passed  # 179 s cap wins over the bucket tolerance
    assert duration_check(120.0, "short", None).passed


def test_duration_check_long_window():
    assert duration_check(510.0, "long", None).passed and duration_check(900.0, "long", None).passed
    assert not duration_check(509.0, "long", None).passed and not duration_check(901.0, "long", None).passed


@pytest.mark.parametrize(
    ("lufs", "tp", "ok"),
    [(-14.0, -1.5, True), (-13.0, -1.0, True), (-15.0, -3.0, True), (-15.1, -2.0, False),
     (-12.9, -2.0, False), (-14.0, -0.5, False), (float("-inf"), -90.0, False), (float("nan"), 0.0, False)],
)
def test_loudness_check_limits(lufs: float, tp: float, ok: bool):
    assert loudness_check(Loudness(lufs, tp)).passed is ok


def test_drift_check():
    assert drift_check(120.0).passed and not drift_check(120.1).passed
    assert not drift_check(None).passed


def test_parse_blackdetect():
    text = "[blackdetect @ 0x1] black_start:0 black_end:1.5 black_duration:1.5\nother line\n"
    assert parse_blackdetect(text) == [(0.0, 1.5, 1.5)]


# ---- real files -------------------------------------------------------------------------------------------


@needs_ffmpeg
def test_good_short_passes_every_check(tmp_path: Path):
    v = make_video(tmp_path / "good.mp4")
    res = run_qc("s1", v, "short", bucket_s=3.0, drift_ms=40.0, first_caption_s=0.0)
    assert res.passed, res.model_dump()
    assert set(res.checks) == {
        "file_opens", "duration", "loudness", "caption_drift_ms", "black_frames",
        "hook_first_1500ms", "aspect",
    }
    schema = json.loads((contracts_dir() / "schemas" / "qc.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(res.model_dump(mode="json"), schema)


@needs_ffmpeg
@pytest.mark.parametrize(
    ("defect", "expected"),
    [
        ("silent", {"loudness", "hook_first_1500ms"}),
        ("loud", {"loudness"}),
        ("black", {"black_frames"}),
        ("wrong_size", {"aspect"}),
        ("no_faststart", {"file_opens"}),
    ],
)
def test_defects_fail_the_right_checks(tmp_path: Path, defect: str, expected: set[str]):
    kw: dict = {}
    if defect == "silent":
        kw["audio"] = "silent"
    elif defect == "loud":
        kw["audio"] = "loud"
    elif defect == "black":
        kw["video"] = "black"
    elif defect == "wrong_size":
        kw["size"] = "720x1280"
    elif defect == "no_faststart":
        kw["faststart"] = False
    v = make_video(tmp_path / f"{defect}.mp4", **kw)
    res = run_qc("s1", v, "short", bucket_s=3.0, drift_ms=40.0, first_caption_s=0.0)
    failed = {k for k, c in res.checks.items() if not c.passed}
    assert not res.passed and expected <= failed, (defect, failed)
    assert res.reason and all(k in res.reason for k in expected)


@needs_ffmpeg
def test_caption_and_duration_defects(tmp_path: Path):
    v = make_video(tmp_path / "ok.mp4")
    late = run_qc("s1", v, "short", bucket_s=3.0, drift_ms=200.0, first_caption_s=0.4)
    failed = {k for k, c in late.checks.items() if not c.passed}
    assert failed == {"caption_drift_ms", "hook_first_1500ms"}
    wrong_bucket = run_qc("s1", v, "short", bucket_s=50.0, drift_ms=10.0, first_caption_s=0.0)
    assert {k for k, c in wrong_bucket.checks.items() if not c.passed} == {"duration"}


@needs_ffmpeg
def test_unreadable_file_fails_file_opens(tmp_path: Path):
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    res = run_qc("s1", bad, "short", bucket_s=3.0, drift_ms=1.0, first_caption_s=0.0)
    assert not res.passed and not res.checks["file_opens"].passed


@needs_ffmpeg
def test_long_kind_has_no_hook_check_and_wants_landscape(tmp_path: Path):
    v = make_video(tmp_path / "wide.mp4", size="1920x1080")
    res = run_qc("long", v, "long", drift_ms=10.0)
    assert "hook_first_1500ms" not in res.checks
    assert res.checks["aspect"].passed and not res.checks["duration"].passed  # 3 s is not 8:30-15:00


# ---- stage ------------------------------------------------------------------------------------------------


def _stage_inputs(ctx, video: Path, drift: float | None = 30.0) -> None:
    ctx.write_json("render/report.json", {"s1": {"path": str(video), "duration_s": 3.0}})
    ctx.write_json("captions/report.json", {"s1": {"drift_ms": drift, "first_caption_s": 0.0}})


@needs_ffmpeg
def test_qc_stage_writes_results_and_passes(tmp_home: Path, tmp_path: Path):
    ctx = make_ctx(tmp_home, combo=make_combo(length_bucket=3))
    _stage_inputs(ctx, make_video(tmp_path / "v.mp4"))
    QcStage().run(ctx)
    assert ctx.read_json("qc/report.json") == {"s1": {"passed": True, "reason": None}}
    assert QcResult.model_validate(ctx.read_json("qc/s1.json")).passed


@needs_ffmpeg
def test_qc_stage_raises_e404_but_keeps_evidence(tmp_home: Path, tmp_path: Path):
    ctx = make_ctx(tmp_home, combo=make_combo(length_bucket=3))
    _stage_inputs(ctx, make_video(tmp_path / "v.mp4"), drift=500.0)
    with pytest.raises(NsError) as ei:
        QcStage().run(ctx)
    assert ei.value.code == "NS-E404" and "caption_drift_ms" in ei.value.detail
    assert ctx.read_json("qc/report.json")["s1"]["passed"] is False


@needs_ffmpeg
def test_qc_stage_drops_a_failing_short_and_keeps_the_rest(tmp_home: Path, tmp_path: Path):
    """The first dev-mode master failed "s5: duration" after everything was rendered; now s5 is dropped."""
    ctx = make_ctx(tmp_home, combo=make_combo(length_bucket=3))
    good = make_video(tmp_path / "good.mp4")
    ctx.write_json("render/report.json", {"s1": {"path": str(good), "duration_s": 3.0},
                                          "s2": {"path": str(good), "duration_s": 3.0}})
    ctx.write_json("captions/report.json", {"s1": {"drift_ms": 30.0, "first_caption_s": 0.0},
                                            "s2": {"drift_ms": 500.0, "first_caption_s": 0.0}})
    QcStage().run(ctx)                                               # no NS-E404
    assert ctx.read_json("qc/report.json")["s2"]["passed"] is False
    assert list(ctx.read_json("render/report.json")) == ["s1"]     # the Outbox will publish s1 only
    assert ctx.read_json("qc/dropped.json") == {"s2": "caption_drift_ms"}
