"""C8 Clipper: templates and the layout engine."""
from __future__ import annotations

import pytest

from ns_studio.clipper import layout as lay
from ns_studio.clipper.models import (
    CANVAS_H,
    CANVAS_W,
    TEMPLATE_NAMES,
    ClipLayout,
    Crop,
    FaceFrame,
    Rect,
)

W, H = 1280, 720
CAM = Rect(x=944, y=16, w=320, h=180)
FRAME = Rect(x=0, y=0, w=W, h=H)


def two_faces(speaking: list[int | None]) -> list[FaceFrame]:
    left, right = Rect(x=150, y=200, w=100, h=100), Rect(x=850, y=200, w=100, h=100)
    return [FaceFrame(t=float(i), boxes=[left, right], speaking=s) for i, s in enumerate(speaking)]


def one_face(xs: list[int]) -> list[FaceFrame]:
    return [
        FaceFrame(t=float(i), boxes=[Rect(x=x - 50, y=100, w=100, h=100)], speaking=None)
        for i, x in enumerate(xs)
    ]


def overlaps(a: Rect, b: Rect) -> bool:
    return a.x < b.right and b.x < a.right and a.y < b.bottom and b.y < a.bottom


def inside(r: Rect, outer: Rect = FRAME) -> bool:
    return r.inside(outer)


# ---------- templates ----------
def test_six_templates_load() -> None:
    tpls = lay.load_templates()
    assert set(tpls) == set(TEMPLATE_NAMES)
    for t in tpls.values():
        assert (t.safe_top, t.safe_bottom) == (220, 320)
        assert t.title_card is False
        if t.caption_anchor_y_pct != "seam":
            assert 100 * 220 / CANVAS_H <= t.caption_anchor_y_pct <= 100 * (CANVAS_H - 320) / CANVAS_H


def test_stacked_template_has_speaker_border() -> None:
    assert lay.load_templates()["podcast_stacked"].speaker_border_px == 2


# ---------- gaming ----------
def test_gaming_regions() -> None:
    layout = ClipLayout(template="gaming_facecam", facecam=CAM)
    crops = lay.plan_layout("gaming_facecam", W, H, layout, t_start=0, t_end=30)
    assert len(crops) == 1
    c = crops[0]
    assert c.region_b == CAM
    assert not overlaps(c.region, CAM)
    assert inside(c.region)
    panels = lay.canvas_panels(lay.load_templates()["gaming_facecam"], c)
    cam_p, game_p = panels["cam"], panels["game"]
    assert cam_p.w == CANVAS_W and cam_p.h <= 640
    assert game_p.y == cam_p.h and game_p.h == CANVAS_H - cam_p.h
    assert c.region.w / c.region.h == pytest.approx(game_p.w / game_p.h, abs=0.01)
    expected = 100 * cam_p.h / CANVAS_H
    assert lay.caption_y_pct(lay.load_templates()["gaming_facecam"], c) == pytest.approx(expected)


def test_gaming_tall_camera_is_cropped_to_640() -> None:
    layout = ClipLayout(template="gaming_facecam", facecam=Rect(x=944, y=16, w=200, h=200))
    c = lay.plan_layout("gaming_facecam", W, H, layout, t_start=0, t_end=10)[0]
    assert c.region_b is not None and c.region_b.w == 200 and c.region_b.h < 200
    assert lay.canvas_panels(lay.load_templates()["gaming_facecam"], c)["cam"].h <= 640


def test_gaming_without_facecam_raises() -> None:
    with pytest.raises(ValueError):
        lay.plan_layout("gaming_facecam", W, H, ClipLayout(template="gaming_facecam"), t_end=5)


def test_screen_plus_cam_shows_the_whole_frame() -> None:
    layout = ClipLayout(template="screen_plus_cam", facecam=CAM)
    c = lay.plan_layout("screen_plus_cam", W, H, layout, t_start=0, t_end=10)[0]
    assert c.region == FRAME and c.region_b == CAM


# ---------- stacked ----------
def test_stacked_panels_and_speaker_switch() -> None:
    layout = ClipLayout(template="podcast_stacked", faces=two_faces([0] * 10 + [1] * 10))
    crops = lay.plan_layout("podcast_stacked", W, H, layout, t_start=0, t_end=20)
    assert len(crops) == 2
    assert crops[0].speaking == 0 and crops[-1].speaking == 1
    assert crops[0].t_end == crops[1].t_start
    for c in crops:
        assert inside(c.region) and c.region_b is not None and inside(c.region_b)
        assert c.region.w / c.region.h == pytest.approx(1080 / 960, abs=0.01)
    panels = lay.canvas_panels(lay.load_templates()["podcast_stacked"], crops[0])
    assert (panels["top"].h, panels["bottom"].y, panels["bottom"].h) == (960, 960, 960)
    rb = crops[0].region_b
    assert rb is not None and crops[0].region.x < rb.x


def test_stacked_ignores_single_frame_blips() -> None:
    layout = ClipLayout(template="podcast_stacked", faces=two_faces([0, 0, 0, 1, 0, 0, 0, 0]))
    crops = lay.plan_layout("podcast_stacked", W, H, layout, t_start=0, t_end=8)
    assert len(crops) == 1 and crops[0].speaking == 0


def test_stacked_needs_two_faces() -> None:
    layout = ClipLayout(template="podcast_stacked", faces=one_face([300] * 5))
    with pytest.raises(ValueError):
        lay.plan_layout("podcast_stacked", W, H, layout, t_start=0, t_end=5)


# ---------- tracked ----------
def test_smooth_track_stays_inside_deadband() -> None:
    samples = [(float(t), 600.0 + (30 if t % 2 else -30)) for t in range(10)]
    track = lay.smooth_track(samples, 404, 1280, 0, 9)
    assert len({round(x, 3) for _, x in track}) == 1


def test_smooth_track_follows_after_leaving_middle() -> None:
    samples = [(0.0, 300.0), (4.0, 300.0), (5.0, 900.0), (12.0, 900.0)]
    track = lay.smooth_track(samples, 404, 1280, 0, 12)
    assert track[0][1] == pytest.approx(300.0)
    assert track[-1][1] == pytest.approx(900.0, abs=1.0)
    assert all(abs(x - 300.0) < 1e-6 for t, x in track if t <= 4.0)
    xs = [x for _, x in track]
    assert xs == sorted(xs)  # moves one way, no overshoot


def test_smooth_track_clamps_to_frame() -> None:
    track = lay.smooth_track([(0.0, 10.0), (5.0, 10.0)], 404, 1280, 0, 5)
    assert min(x for _, x in track) >= 202.0


def test_tracked_crop_follows_the_face() -> None:
    layout = ClipLayout(template="talking_head", faces=one_face([300] * 5 + [900] * 15))
    crops = lay.plan_layout("talking_head", W, H, layout, t_start=0, t_end=19)
    assert all(inside(c.region) and c.region.h == H for c in crops)
    assert all(c.region.w / c.region.h == pytest.approx(9 / 16, abs=0.01) for c in crops)
    assert crops[0].region.x == pytest.approx(300 - 202, abs=3)
    assert crops[-1].region.x == pytest.approx(900 - 202, abs=3)
    assert len({c.region.x for c in crops if c.t_end <= 3.0}) == 1
    assert all(a.t_end == b.t_start for a, b in zip(crops, crops[1:], strict=False))


def test_tracked_without_faces_centres() -> None:
    crops = lay.plan_layout("talking_head", W, H, ClipLayout(template="talking_head"), t_start=0, t_end=10)
    assert len(crops) == 1 and crops[0].region.x == pytest.approx((W - crops[0].region.w) / 2, abs=2)


def test_tracked_stable_face_gives_one_crop() -> None:
    layout = ClipLayout(template="podcast_single", faces=one_face([640, 650, 635, 645, 640, 642]))
    crops = lay.plan_layout("podcast_single", W, H, layout, t_start=0, t_end=5)
    assert len(crops) == 1


# ---------- centre ----------
def test_center_crop_is_centred_and_in_safe_area() -> None:
    c = lay.plan_layout("center_crop", W, H, ClipLayout(template="center_crop"), t_start=0, t_end=10)[0]
    assert c.region.h == H and abs((c.region.x + c.region.w / 2) - W / 2) <= 1
    panels = lay.canvas_panels(lay.load_templates()["center_crop"], c)
    assert panels["bg"] == Rect(x=0, y=0, w=CANVAS_W, h=CANVAS_H)
    assert panels["fg"].y >= 220 and panels["fg"].bottom <= CANVAS_H - 320


# ---------- all templates ----------
@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_every_template_yields_valid_panels(name: str) -> None:
    layout = ClipLayout(template=name, facecam=CAM, faces=two_faces([0] * 6 + [1] * 6))
    crops = lay.plan_layout(name, W, H, layout, t_start=0, t_end=11)
    assert crops and crops[0].t_start == 0 and crops[-1].t_end == 11
    tpl = lay.load_templates()[name]
    for c in crops:
        assert inside(c.region)
        assert c.region_b is None or inside(c.region_b)
        for p in lay.canvas_panels(tpl, c).values():
            assert inside(p, Rect(x=0, y=0, w=CANVAS_W, h=CANVAS_H))
            assert p.w > 0 and p.h > 0


def test_crop_model_roundtrip_in_layout() -> None:
    c = Crop(t_start=0, t_end=1, region=FRAME)
    assert ClipLayout(template="center_crop", crops=[c]).crops[0] == c
