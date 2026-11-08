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


def test_peak_ceiling_keeps_the_target_crest() -> None:
    # crest allowed by the target is -1.5 - -14 = 12.5 dB; the ceiling sits 2 dB inside it
    assert rd.peak_ceiling_db(-20.0) == pytest.approx(-9.5)
    lin = rd.limiter_filter(-9.5)
    assert lin.startswith("alimiter=limit=0.335:") and lin.endswith("level=0")
    assert "limit=0.0625:" in rd.limiter_filter(-60.0) and "limit=1:" in rd.limiter_filter(6.0)


def test_audio_filter_puts_the_peak_stage_before_loudnorm() -> None:
    m = {"input_i": -20.0, "input_tp": -8.0, "input_lra": 1.0, "input_thresh": -30.0, "target_offset": 0.0}
    f = rd.audio_filter(m, pre="alimiter=limit=0.4:attack=2:release=50:level=0")
    assert f.startswith("alimiter=limit=0.4:attack=2:release=50:level=0,loudnorm=")
    assert "measured_I=-20.00" in f
    assert rd.audio_filter(m).startswith("loudnorm=")


def _m(i: float, tp: float) -> dict[str, float]:
    return {"input_i": i, "input_tp": tp, "input_lra": 1.0, "input_thresh": i - 10, "target_offset": 0.0}


def test_plan_lowers_the_ceiling_until_the_peak_fits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake(spec: rd.RenderSpec, log: object = None, pre: str = "") -> dict[str, float]:
        seen.append(pre)
        if not pre:
            return _m(-20.38, -0.47)
        return _m(-21.0, -9.0) if len(seen) == 2 else _m(-21.5, -11.0)   # first try still 3 dB too hot

    monkeypatch.setattr(rd, "measure_loudness", fake)
    plan = rd.plan_loudness(spec_for("center_crop", tmp_path / "o.mp4"))
    assert plan.mode == "limited" and len(seen) == 3 and seen[1] != seen[2]
    assert plan.measured == _m(-21.5, -11.0) and plan.source == _m(-20.38, -0.47)
    rep = plan.report()
    assert rep["input_i"] == -20.38 and rep["limited_tp"] == -11.0 and rep["mode"] == "limited"


def test_plan_stays_linear_for_normal_and_silent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(rd, "measure_loudness", lambda spec, log=None, pre="": _m(-18.0, -8.0))
    plan = rd.plan_loudness(spec_for("center_crop", tmp_path / "o.mp4"))
    assert plan.report() == {"mode": "linear", "input_i": -18.0, "input_tp": -8.0} and plan.pre == ""
    monkeypatch.setattr(rd, "measure_loudness", lambda spec, log=None, pre="": None)
    silent = rd.plan_loudness(spec_for("center_crop", tmp_path / "o.mp4"))
    assert silent.report() == {"mode": "linear", "input_i": None, "input_tp": None}


def make_peaky_source(path: Path) -> None:
    """12 s, 1280x720: a quiet 220 Hz tone with a 5 ms low-passed noise click every half second.

    Measured by loudnorm: about -20 LUFS with a peak near 0 dBTP, so +6 dB cannot be applied linearly. The
    noise is low-passed at 3 kHz so the clicks resemble speech plosives, not full-band hash.
    """
    graph = (
        "[1:a]volume=1.2[s];"
        "[2:a]lowpass=f=3000,volume='if(lt(mod(t,0.5),0.005),1.5,0)':eval=frame[c];"
        "[s][c]amix=inputs=2:normalize=0,aformat=channel_layouts=mono[a]"
    )
    ffmpeg.run(
        ["-f", "lavfi", "-i", "color=c=black:s=1280x720:r=30:d=12",
         "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=12",
         "-f", "lavfi", "-i", "anoisesrc=d=12:sample_rate=48000:amplitude=0.9:seed=7",
         "-filter_complex", graph, "-map", "0:v", "-map", "[a]",
         "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-b:a", "192k", str(path)]
    )


@needs_ffmpeg
def test_quiet_peaky_source_still_reaches_minus_14(tmp_path: Path) -> None:
    src = tmp_path / "peaky.mp4"
    make_peaky_source(src)
    base = spec_for("center_crop", tmp_path / "c1.mp4", start=1.0, end=10.0)
    spec = rd.RenderSpec(**{**base.__dict__, "video": src})
    before = rd.measure_loudness(spec)
    assert before is not None and not rd.linear_possible(before["input_i"], before["input_tp"])  # the defect
    out, loud = rd.render_clip_report(spec)
    assert loud["mode"] == "limited"
    assert loud["input_i"] == pytest.approx(before["input_i"], abs=0.01)
    assert loud["input_tp"] == pytest.approx(before["input_tp"], abs=0.01)
    assert isinstance(loud["limited_i"], float) and isinstance(loud["input_i"], float)
    assert isinstance(loud["limited_tp"], float) and isinstance(loud["input_tp"], float)
    assert loud["limited_i"] < loud["input_i"] and loud["limited_tp"] < loud["input_tp"]
    probe = ffmpeg.run(
        ["-i", str(out), "-vn", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"]
    )
    m = rd.parse_loudnorm(probe.stderr)
    assert m is not None and -15.0 <= m["input_i"] <= -13.0
    assert m["input_tp"] <= -1.0


@needs_ffmpeg
def test_normal_source_keeps_the_linear_two_pass(tmp_path: Path) -> None:
    out, loud = rd.render_clip_report(spec_for("center_crop", tmp_path / "c1.mp4"))
    assert out.is_file() and loud["mode"] == "linear" and "limited_i" not in loud
    assert set(loud) >= {"mode", "input_i", "input_tp"}
