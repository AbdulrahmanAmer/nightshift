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
    assert "vstack=inputs=2" in g["gaming_facecam"] and "crop=320:180:944:16" in g["gaming_facecam"]
    assert "vstack=inputs=2" in g["podcast_stacked"] and "drawbox=" in g["podcast_stacked"]
    assert "between(t," in g["podcast_stacked"]
    assert "crop=w=404:h=720:x='clip(" in g["talking_head"] and "[src]crop=w=404" in g["podcast_single"]
    assert "boxblur" in g["center_crop"] and "overlay=152:220" in g["center_crop"]
    assert "pad=1080:" in g["screen_plus_cam"] and "vstack=inputs=2" in g["screen_plus_cam"]
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
