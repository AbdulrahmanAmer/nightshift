"""Generic layout engine (spec 17.1): graph strings per format and a real FFmpeg render per format."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ns_common import ffmpeg as ff
from ns_common.models import Format
from ns_studio.media import formats as F
from ns_studio.media import layout as L
from ns_studio.media.layout import SourceSpec
from tests.fixtures.clipper.make_sample import facecam_truth, shared_sample

needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg not available")
SRC_W, SRC_H = 1280, 720
PRELUDE = "[0:v]setpts=PTS-STARTPTS,fps=30,setsar=1[src]"
_PADS = re.compile(r"\[([^\]\[]+)\]")


def cam_rect() -> tuple[int, int, int, int]:
    t = facecam_truth()
    return t["x"], t["y"], t["w"], t["h"]


def std_sources() -> dict[str, SourceSpec]:
    """A source mapping that serves every region source name used by the shipped formats."""
    return {
        "full": SourceSpec(SRC_W, SRC_H),
        "facecam": SourceSpec(SRC_W, SRC_H, crop=cam_rect()),
        "screen": SourceSpec(SRC_W, SRC_H),
        "face:0": SourceSpec(SRC_W, SRC_H, crop=(0, 0, 810, 720)),
        "face:1": SourceSpec(SRC_W, SRC_H, crop=(470, 0, 810, 720)),
    }


def check_pads(fragments: list[str], external: set[str], out_label: str = "v0") -> None:
    """Each consumed pad exists before use and is used once; `out_label` is the only leftover result."""
    produced = set(external)
    consumed: list[str] = []
    for frag in fragments:
        head = re.match(r"^((?:\[[^\]\[]+\])+)", frag)
        ins = _PADS.findall(head.group(1)) if head else []
        tail = re.search(r"((?:\[[^\]\[]+\])+)$", frag)
        outs = _PADS.findall(tail.group(1)) if tail else []
        for p in ins:
            assert p in produced, f"{p} used before it exists in {frag}"
            consumed.append(p)
        assert outs, f"fragment without an output pad: {frag}"
        produced.update(outs)
    assert len(consumed) == len(set(consumed)), "a pad is consumed twice (needs a split)"
    assert fragments[-1].endswith(f"[{out_label}]")
    assert set(produced) - set(consumed) - set(external) == {out_label}


# ---------- geometry helpers ----------
def test_even() -> None:
    assert L.even(607.5) == 608 and L.even(405) == 404 and L.even(0.2) == 2


def test_cover_without_a_crop_trims_the_centre_to_the_region_aspect() -> None:
    region = F.get_format("short_9x16").layout.regions[0]
    chain = L.region_chain(region, SourceSpec(SRC_W, SRC_H))
    assert chain == "crop=404:720:438:0,scale=1080:1920:flags=lanczos"


def test_cover_keeps_an_explicit_crop_that_is_close_to_the_aspect() -> None:
    cam = F.get_format("clip_gaming_facecam").layout.regions[0]
    spec = SourceSpec(SRC_W, SRC_H, crop=(944, 16, 320, 180))
    near = L.region_chain(cam.model_copy(update={"h": 608}), spec)
    assert near.startswith("crop=320:180:944:16,scale=1080:608")
    far = L.region_chain(cam, spec)  # 1080x640 is wider
    assert far.startswith("crop=304:180:952:16,scale=1080:640")


def test_contain_letterboxes_and_screen_defaults_to_it() -> None:
    screen = F.get_format("clip_screen_plus_cam").layout.regions[1]
    chain = L.region_chain(screen, SourceSpec(SRC_W, SRC_H))
    assert chain == (
        "scale=1080:1280:force_original_aspect_ratio=decrease:flags=lanczos,"
        "pad=1080:1280:(ow-iw)/2:(oh-ih)/2:color=black"
    )
    full = F.get_format("short_9x16").layout.regions[0]
    assert "pad=" in L.region_chain(full, SourceSpec(SRC_W, SRC_H, fit="contain"))


def test_blur_fill_and_sliding_crop() -> None:
    bg = F.get_format("clip_center_crop").layout.regions[0]
    chain = L.region_chain(bg, SourceSpec(SRC_W, SRC_H))
    assert chain.startswith("scale=270:480:force_original_aspect_ratio=increase,crop=270:480,boxblur=10:2")
    assert chain.endswith("eq=brightness=-0.2,scale=1080:1920")
    main = F.get_format("clip_talking_head").layout.regions[0]
    slid = L.region_chain(main, SourceSpec(SRC_W, SRC_H, crop=(100, 0, 404, 720), x_expr="100+t*10"))
    assert slid == "crop=w=404:h=720:x='clip(100+t*10,0,876)':y=0:exact=1,scale=1080:1920:flags=lanczos"


def test_fit_facecam_height_shrinks_the_camera_and_grows_the_panel_below() -> None:
    fmt = F.get_format("clip_gaming_facecam")
    fitted = L.fit_facecam_height(fmt, 320, 180)
    cam, game = fitted.layout.regions
    assert (cam.y, cam.h) == (0, 608) and (game.y, game.h) == (608, 1312)
    assert game.y + game.h == 1920
    assert L.fit_facecam_height(fmt, 200, 200) == fmt  # taller than the maximum: unchanged
    wide = L.fit_facecam_height(fmt, 1920, 400).layout.regions
    assert wide[0].h == 224 and wide[1].y == 224
    assert L.fit_facecam_height(F.get_format("short_9x16"), 320, 180) == F.get_format("short_9x16")


def test_caption_anchor_is_the_seam_or_the_preset_value() -> None:
    gaming = F.get_format("clip_gaming_facecam")
    assert L.caption_anchor_pct(gaming) == pytest.approx(100 * 640 / 1920)
    assert L.caption_anchor_pct(L.fit_facecam_height(gaming, 320, 180)) == pytest.approx(100 * 608 / 1920)
    assert L.caption_anchor_pct(F.get_format("short_9x16")) == 72.0
    assert L.caption_anchor_pct(F.get_format("clip_podcast_stacked")) == 75.0


# ---------- graphs ----------
@pytest.mark.parametrize("fid", sorted(F.load_formats()))
def test_every_format_builds_a_consistent_graph(fid: str) -> None:
    fmt = F.get_format(fid)
    fragments = L.build_regions_graph(fmt, std_sources(), duration_s=2.0)
    check_pads(fragments, {"src"})
    graph = ";".join(fragments)
    assert graph.count("scale=") >= len(fmt.layout.regions)
    for r in fmt.layout.regions:
        if r.source != "blur_fill":
            assert f"scale={r.w}:{r.h}" in graph


def test_single_region_formats_are_one_fragment() -> None:
    for fid, size in (("short_9x16", "1080:1920"), ("long_16x9", "1920:1080"), ("square_1x1", "1080:1080")):
        frags = L.build_regions_graph(F.get_format(fid), std_sources())
        assert len(frags) == 1 and f"scale={size}" in frags[0] and frags[0].startswith("[src]crop=")
        assert frags[0].endswith("[v0]")


def test_gaming_places_camera_and_game_by_crop_scale_overlay() -> None:
    fmt = L.fit_facecam_height(F.get_format("clip_gaming_facecam"), 320, 180)
    frags = L.build_regions_graph(fmt, std_sources(), duration_s=7.0)
    graph = ";".join(frags)
    assert frags[0] == "[src]split=2[src_0][src_1]"
    assert "[src_0]crop=320:180:944:16,scale=1080:608:flags=lanczos[r0]" in graph
    assert "[src_1]crop=" in graph and "scale=1080:1312:flags=lanczos[r1]" in graph
    assert "color=c=black:s=1080x1920:r=30:d=7.000[base]" in graph
    assert "[base][r0]overlay=0:0:shortest=1[o0]" in graph
    assert "[o0][r1]overlay=0:608:shortest=1[v0]" in graph


def test_stacked_uses_one_face_crop_per_half() -> None:
    graph = ";".join(L.build_regions_graph(F.get_format("clip_podcast_stacked"), std_sources()))
    assert "crop=810:720:0:0,scale=1080:960" in graph and "crop=810:720:470:0,scale=1080:960" in graph
    assert "overlay=0:0" in graph and "overlay=0:960" in graph


def test_centre_crop_overlays_the_foreground_on_the_blurred_fill() -> None:
    frags = L.build_regions_graph(F.get_format("clip_center_crop"), std_sources())
    graph = ";".join(frags)
    assert "color=" not in graph  # the fill covers the canvas and is the base
    assert "boxblur=10:2" in graph and "[r0][r1]overlay=152:220:shortest=1[v0]" in graph
    assert "scale=776:1380:flags=lanczos" in graph


def test_screen_plus_cam_letterboxes_the_screen() -> None:
    fmt = L.fit_facecam_height(F.get_format("clip_screen_plus_cam"), 320, 180)
    graph = ";".join(L.build_regions_graph(fmt, std_sources()))
    assert "pad=1080:1312" in graph and "crop=320:180:944:16" in graph


def test_a_new_layout_is_only_data() -> None:
    fmt = Format.model_validate({
        "id": "split_h", "name": "Side by side", "kind": "long",
        "canvas": {"w": 1920, "h": 1080, "fps": 30}, "duration": {"min_s": 10, "max_s": 60},
        "captions": {"styles_allowed": ["clean_white"]},
        "layout": {"regions": [
            {"name": "l", "x": 0, "y": 0, "w": 960, "h": 1080, "source": "face:0"},
            {"name": "r", "x": 960, "y": 0, "w": 960, "h": 1080, "source": "face:1"},
        ]},
    })
    frags = L.build_regions_graph(fmt, std_sources())
    check_pads(frags, {"src"})
    assert "overlay=0:0" in ";".join(frags) and "overlay=960:0" in ";".join(frags)


def test_a_region_without_a_source_is_an_error() -> None:
    with pytest.raises(ValueError):
        L.build_regions_graph(F.get_format("clip_gaming_facecam"), {})


def test_separate_input_labels_are_not_split() -> None:
    fmt = F.get_format("clip_podcast_stacked")
    sources = {"face:0": SourceSpec(100, 100, label="a"), "face:1": SourceSpec(100, 100, label="b")}
    frags = L.build_regions_graph(fmt, sources)
    assert not any("split" in f for f in frags)
    check_pads(frags, {"a", "b"})


# ---------- real renders ----------
@needs_ffmpeg
@pytest.mark.parametrize("fid", sorted(F.load_formats()))
def test_every_format_renders_two_seconds_at_its_canvas(fid: str, tmp_path: Path) -> None:
    fmt = F.get_format(fid)
    graph = ";".join([PRELUDE, *L.build_regions_graph(fmt, std_sources(), duration_s=2.0)])
    out = tmp_path / f"{fid}.mp4"
    ff.run([
        "-ss", "2", "-t", "2", "-i", str(shared_sample().video), "-filter_complex", graph, "-map", "[v0]",
        "-an", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out),
    ])
    assert ff.video_size(out) == (fmt.canvas.w, fmt.canvas.h)
    assert abs(ff.duration_s(out) - 2.0) < 0.4
    if fmt.canvas.h == 1920:
        assert ff.video_size(out) == (1080, 1920)
