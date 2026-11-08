"""C8 Clipper: filter graph builders and per-template renders (real FFmpeg, libx264)."""
from __future__ import annotations

from pathlib import Path

import pytest

from ns_common import ffmpeg
from ns_common.errors import NsError
from ns_common.models import ClipLayout, Crop, FaceFrame, Rect
from ns_studio.clipper import layout as lay
from ns_studio.clipper import render as rd
from ns_studio.clipper.models import TEMPLATE_NAMES
from ns_studio.media import audio
from tests.fixtures.clipper.make_sample import facecam_truth, shared_sample

needs_ffmpeg = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not available")

_STYLE_FIELDS = (
    "Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
    "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
    "MarginL, MarginR, MarginV, Encoding"
)
ASS = "\n".join(
    [
        "[Script Info]",
        "ScriptType: v4.00+",
        "PlayResX: 1080",
        "PlayResY: 1920",
        "",
        "[V4+ Styles]",
        f"Format: {_STYLE_FIELDS}",
        "Style: Default,Arial,64,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,0,0,0,0,100,100,0,0,1,"
        "3,0,2,40,40,400,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        "Dialogue: 0,0:00:00.00,0:00:03.00,Default,,0,0,0,,Hello world",
        "",
    ]
)


def cam() -> Rect:
    t = facecam_truth()
    return Rect(x=t["x"], y=t["y"], w=t["w"], h=t["h"])


def rich_layout(name: str) -> ClipLayout:
    left, right = Rect(x=150, y=200, w=100, h=100), Rect(x=850, y=200, w=100, h=100)
    faces = [
        FaceFrame(t=float(i), boxes=[left, right], speaking=0 if i < 6 else 1) for i in range(0, 12)
    ]
    return ClipLayout(template=name, facecam=cam(), faces=faces)


def spec_for(
    name: str, out: Path, ass: Path | None = None, start: float = 2.0, end: float = 9.0
) -> rd.RenderSpec:
    crops = lay.plan_layout(name, 1280, 720, rich_layout(name), t_start=start, t_end=end)
    return rd.RenderSpec(
        video=shared_sample().video, out=out, start=start, end=end, template=lay.load_templates()[name],
        crops=crops, src_w=1280, src_h=720, ass=ass,
    )


# ---------- pure helpers ----------
def test_filter_path_escapes_drive_colon_and_backslashes() -> None:
    assert rd.filter_path(Path("C:\\Users\\x y\\c1.ass")) == "C\\:/Users/x y/c1.ass"


def test_lerp_expr_constant_and_ramp() -> None:
    assert rd.lerp_expr([(0.0, 100.0), (2.0, 100.0)]) == "100"
    e = rd.lerp_expr([(0.0, 100.0), (1.0, 100.0), (2.0, 300.0)])
    assert e.startswith("if(lt(t,0.000),100,") and "(t-1.000)/1.000" in e and e.count("if(") == 3


def test_relative_crops_clip_and_shift() -> None:
    c = [Crop(t_start=0, t_end=10, region=Rect(x=0, y=0, w=10, h=10))]
    out = rd.relative_crops(c, 4.0, 8.0)
    assert [(x.t_start, x.t_end) for x in out] == [(0.0, 4.0)]


def test_relative_crops_falls_back_to_nearest() -> None:
    c = [Crop(t_start=50, t_end=60, region=Rect(x=0, y=0, w=10, h=10))]
    out = rd.relative_crops(c, 4.0, 8.0)
    assert len(out) == 1 and (out[0].t_start, out[0].t_end) == (0.0, 4.0)


def test_parse_loudnorm_report() -> None:
    err = 'noise\n{\n "input_i" : "-20.5",\n "input_tp" : "-3.1",\n "input_lra" : "1.2",\n' \
          ' "input_thresh" : "-31.0",\n "output_i" : "-14.0",\n "target_offset" : "0.4"\n}\n'
    m = rd.parse_loudnorm(err)
    assert m is not None and m["input_i"] == -20.5 and m["target_offset"] == 0.4
    assert "measured_I=-20.50" in rd.audio_filter(m) and "linear=true" in rd.audio_filter(m)


def test_parse_loudnorm_silence_is_none() -> None:
    err = '{\n "input_i" : "-inf",\n "input_tp" : "-inf",\n "input_lra" : "0.0",\n' \
          ' "input_thresh" : "-70.0",\n "target_offset" : "inf"\n}'
    assert rd.parse_loudnorm(err) is None
    assert "measured_I" not in rd.audio_filter(None)


def test_encoder_args() -> None:
    assert rd.encoder_args("libx264", "ultrafast")[:2] == ["-c:v", "libx264"]
    assert rd.encoder_args("h264_nvenc", "p4")[:2] == ["-c:v", "h264_nvenc"]


def test_graph_per_layout_contains_the_expected_filters(tmp_path: Path) -> None:
    g = {n: rd.build_filter_graph(spec_for(n, tmp_path / f"{n}.mp4")) for n in TEMPLATE_NAMES}
    assert "overlay=0:608" in g["gaming_facecam"] and "crop=320:180:944:16" in g["gaming_facecam"]
    assert "overlay=0:960" in g["podcast_stacked"] and "drawbox=" in g["podcast_stacked"]
    assert "between(t," in g["podcast_stacked"]
    assert "crop=w=404:h=720:x='clip(" in g["talking_head"] and "[src]crop=w=404" in g["podcast_single"]
    assert "boxblur" in g["center_crop"] and "overlay=152:220" in g["center_crop"]
    assert "pad=1080:" in g["screen_plus_cam"] and "overlay=0:608" in g["screen_plus_cam"]
    assert all(v.endswith("[vout]") for v in g.values())


def test_graph_burns_captions_when_given(tmp_path: Path) -> None:
    g = rd.build_filter_graph(spec_for("center_crop", tmp_path / "o.mp4", ass=tmp_path / "c1.ass"))
    assert ",ass='" in g and g.index("ass=") < g.index("[vout]")


def test_graph_without_crops_is_an_error(tmp_path: Path) -> None:
    s = spec_for("center_crop", tmp_path / "o.mp4")
    empty = rd.RenderSpec(**{**s.__dict__, "crops": []})
    with pytest.raises(NsError):
        rd.build_filter_graph(empty)


def test_args_put_faststart_and_audio_last_chain(tmp_path: Path) -> None:
    a = rd.build_args(spec_for("center_crop", tmp_path / "o.mp4"), None)
    assert a[a.index("-movflags") + 1] == "+faststart" and a[-1].endswith("o.mp4")
    assert "[0:a]loudnorm" in a[a.index("-filter_complex") + 1]


# ---------- real renders ----------
@needs_ffmpeg
@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_every_template_renders_1080x1920(name: str, tmp_path: Path) -> None:
    out = rd.render_clip(spec_for(name, tmp_path / "c1.mp4"))
    assert out.exists()
    assert ffmpeg.video_size(out) == (1080, 1920)
    assert abs(ffmpeg.duration_s(out) - 7.0) < 0.5
    kinds = [s["codec_type"] for s in ffmpeg.probe(out)["streams"]]
    assert kinds.count("video") == 1 and kinds.count("audio") == 1
    assert ffmpeg.has_faststart(out)


@needs_ffmpeg
def test_render_with_burned_captions_and_loudness(tmp_path: Path) -> None:
    ass = tmp_path / "caps" / "c1.ass"
    ass.parent.mkdir()
    ass.write_text(ASS, encoding="utf-8")
    out = rd.render_clip(spec_for("gaming_facecam", tmp_path / "c1.mp4", ass=ass))
    assert ffmpeg.video_size(out) == (1080, 1920)
    probe = ffmpeg.run(
        ["-i", str(out), "-vn", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"]
    )
    m = rd.parse_loudnorm(probe.stderr)
    assert m is not None and -15.0 <= m["input_i"] <= -13.0
    assert m["input_tp"] <= -1.0


# ---------- loudness on quiet, peaky sources ----------
# Measured on a real clip (NS-E404, 2026-11-08): -20.38 LUFS with a -0.47 dBTP peak.
PEAKY = {"input_i": -20.38, "input_tp": -0.47}


def test_linear_possible_decision() -> None:
    assert not rd.linear_possible(PEAKY["input_i"], PEAKY["input_tp"])  # +6.4 dB would put the peak at +5.9
    assert rd.linear_possible(-20.0, -10.0)                                # +6 dB lands the peak at -4
    assert rd.linear_possible(-20.0, -7.5)                                 # exactly -1.5 dBTP
    assert rd.linear_possible(-20.0, -7.45)                                # inside the 0.1 dB tolerance
    assert not rd.linear_possible(-20.0, -7.3)
    assert rd.linear_possible(-12.0, -0.5)                                 # louder than the target: a cut


def test_boost_limiter_has_a_fixed_ceiling_inside_the_limiter_range() -> None:
    # the ceiling is TARGET_TP - 2 = -3.5 dBFS whatever the clip's level; only the boost follows the clip
    f = audio.boost_limiter(6.38, rd.TARGET_TP)
    assert f == "volume=6.38dB,alimiter=limit=0.6683:attack=2:release=120:level=0"
    quiet = audio.boost_limiter(31.7, rd.TARGET_TP)
    assert quiet.split(",")[1] == f.split(",")[1]
    assert 0.0625 < float(quiet.split("limit=")[1].split(":")[0]) < 1.0


def test_audio_filter_puts_the_peak_stage_before_loudnorm() -> None:
    m = {"input_i": -20.0, "input_tp": -8.0, "input_lra": 1.0, "input_thresh": -30.0, "target_offset": 0.0}
    f = rd.audio_filter(m, pre="volume=6.00dB,alimiter=limit=0.4:attack=2:release=50:level=0")
    assert f.startswith("volume=6.00dB,alimiter=limit=0.4:attack=2:release=50:level=0,loudnorm=")
    assert "measured_I=-20.00" in f
    assert rd.audio_filter(m).startswith("loudnorm=")


def _m(i: float, tp: float) -> dict[str, float]:
    return {"input_i": i, "input_tp": tp, "input_lra": 1.0, "input_thresh": i - 10, "target_offset": 0.0}


def _boost(pre: str) -> float:
    return float(pre.split("volume=")[1].split("dB")[0])


def _fake_measure(monkeypatch: pytest.MonkeyPatch, answers: list[dict[str, float] | None]) -> list[str]:
    """Make `measure_loudness` answer in order (first the unprocessed clip); returns each `pre` seen."""
    seen: list[str] = []

    def fake(spec: rd.RenderSpec, log: object = None, pre: str = "") -> dict[str, float] | None:
        seen.append(pre)
        return answers[len(seen) - 1]

    monkeypatch.setattr(rd, "measure_loudness", fake)
    return seen


def test_plan_raises_the_boost_until_the_peak_fits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # the limiter takes 3.0 LU off at 6.38 dB, then 1.8 LU still missing at 9.38: the first step adds the raw
    # shortfall (3.0), the second the shortfall over the secant slope (1.8 / (1.2 / 3.0) = 4.5)
    seen = _fake_measure(
        monkeypatch, [_m(-20.38, -0.47), _m(-17.0, -3.5), _m(-15.8, -3.5), _m(-14.2, -3.5)]
    )
    plan = rd.plan_loudness(spec_for("center_crop", tmp_path / "o.mp4"))
    assert plan.mode == "limited" and len(seen) == 4
    assert [_boost(p) for p in seen[1:]] == pytest.approx([6.38, 9.38, 13.88])
    assert seen[1].split(",")[1] == seen[2].split(",")[1]                  # the ceiling does not move
    assert plan.pre == seen[3] and plan.measured == _m(-14.2, -3.5) and plan.source == _m(-20.38, -0.47)
    rep = plan.report()
    assert rep == {"mode": "limited", "input_i": -20.38, "input_tp": -0.47, "limited_i": -14.2,
                   "limited_tp": -3.5, "boost_db": 13.88, "ceiling_db": -3.5}


def test_plan_stops_after_the_last_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hot = _m(-17.0, -1.0)                                                  # never fits
    seen = _fake_measure(monkeypatch, [_m(-20.38, -0.47), hot, hot, hot, hot])
    plan = rd.plan_loudness(spec_for("center_crop", tmp_path / "o.mp4"))
    assert len(seen) == 1 + audio.MAX_LIMITER_PASSES
    assert plan.mode == "limited" and plan.pre == seen[-1]
    assert plan.report()["boost_db"] == pytest.approx(15.38)


def test_plan_falls_back_when_a_measure_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_measure(monkeypatch, [_m(-20.38, -0.47), _m(-17.0, -1.0), None])
    plan = rd.plan_loudness(spec_for("center_crop", tmp_path / "o.mp4"))
    assert len(seen) == 3 and plan.mode == "linear" and plan.pre == "" and plan.measured == plan.source
    assert plan.report() == {"mode": "linear", "input_i": -20.38, "input_tp": -0.47}


def test_plan_stays_linear_for_normal_and_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "measure_loudness", lambda spec, log=None, pre="": _m(-18.0, -8.0))
    plan = rd.plan_loudness(spec_for("center_crop", tmp_path / "o.mp4"))
    assert plan.report() == {"mode": "linear", "input_i": -18.0, "input_tp": -8.0} and plan.pre == ""
    monkeypatch.setattr(rd, "measure_loudness", lambda spec, log=None, pre="": None)
    silent = rd.plan_loudness(spec_for("center_crop", tmp_path / "o.mp4"))
    assert silent.report() == {"mode": "linear", "input_i": None, "input_tp": None}


def make_peaky_source(path: Path, level_db: float = 0.0) -> None:
    """12 s, 1280x720: a quiet 220 Hz tone with a 5 ms low-passed noise click every 70 ms, `level_db` overall.

    The clicks are gated per sample (`aeval`), so each is really 5 ms long, like a speech plosive; the noise
    is low-passed at 3 kHz because full-band hash would make the resampler ring. At 0 dB loudnorm measures
    about -20.6 LUFS with a -0.8 dBTP peak (the real clip was -20.4 / -0.5) and the old chain renders it at
    -20.7 LUFS, 6.7 LU short. At -25 dB it is the very quiet case (about -45.6 LUFS / -25.8 dBTP).
    """
    graph = (
        "[1:a]volume=0.6[s];"
        "[2:a]lowpass=f=3000,aeval=exprs='val(0)*1.3*lt(mod(t,0.07),0.005)':c=same[c];"
        f"[s][c]amix=inputs=2:normalize=0,volume={level_db:g}dB,aformat=channel_layouts=mono[a]"
    )
    ffmpeg.run(
        ["-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30:d=12",
         "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=12",
         "-f", "lavfi", "-i", "anoisesrc=d=12:sample_rate=48000:amplitude=0.9:seed=7",
         "-filter_complex", graph, "-map", "0:v", "-map", "[a]",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-b:a", "192k", str(path)]
    )


Loud = dict[str, float | str | None]


Finals = dict[str, dict[str, float]]


def _render_peaky(tmp_path: Path, level_db: float) -> tuple[dict[str, float], Loud, Finals]:
    src = tmp_path / "peaky.mp4"
    make_peaky_source(src, level_db)
    base = spec_for("center_crop", tmp_path / "c1.mp4", start=1.0, end=10.0)
    spec = rd.RenderSpec(**{**base.__dict__, "video": src})
    before = rd.measure_loudness(spec)
    assert before is not None and not rd.linear_possible(before["input_i"], before["input_tp"])  # the defect
    old = tmp_path / "old.mp4"
    ffmpeg.run(rd.build_args(rd.RenderSpec(**{**spec.__dict__, "out": old}), before))  # the old chain
    out, loud = rd.render_clip_report(spec)
    return before, loud, {"old": _final(old), "new": _final(out)}


def _final(path: Path) -> dict[str, float]:
    probe = ffmpeg.run(
        ["-i", str(path), "-vn", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"]
    )
    m = rd.parse_loudnorm(probe.stderr)
    assert m is not None
    return m


@needs_ffmpeg
def test_quiet_peaky_source_still_reaches_minus_14(tmp_path: Path) -> None:
    before, loud, final = _render_peaky(tmp_path, 0.0)
    assert before["input_i"] > -25.0 and final["old"]["input_i"] < -16.0       # the old chain falls short
    assert loud["mode"] == "limited"
    assert loud["input_i"] == pytest.approx(before["input_i"], abs=0.01)
    assert loud["input_tp"] == pytest.approx(before["input_tp"], abs=0.01)
    assert isinstance(loud["limited_i"], float) and isinstance(loud["limited_tp"], float)
    assert loud["ceiling_db"] == -3.5 and isinstance(loud["boost_db"], float) and loud["boost_db"] > 6.0
    assert -15.0 <= final["new"]["input_i"] <= -13.0
    assert final["new"]["input_tp"] <= -1.0


@needs_ffmpeg
def test_very_quiet_peaky_source_still_reaches_minus_14(tmp_path: Path) -> None:
    before, loud, final = _render_peaky(tmp_path, -25.0)
    assert before["input_i"] < -40.0 and final["old"]["input_i"] < -16.0
    assert loud["mode"] == "limited" and loud["ceiling_db"] == -3.5
    assert isinstance(loud["boost_db"], float) and loud["boost_db"] > 30.0
    assert -15.0 <= final["new"]["input_i"] <= -13.0
    assert final["new"]["input_tp"] <= -1.0


@needs_ffmpeg
def test_normal_source_keeps_the_linear_two_pass(tmp_path: Path) -> None:
    out, loud = rd.render_clip_report(spec_for("center_crop", tmp_path / "c1.mp4"))
    assert out.is_file() and loud["mode"] == "linear" and "limited_i" not in loud
    assert set(loud) >= {"mode", "input_i", "input_tp"}
