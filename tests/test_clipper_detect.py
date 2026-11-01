"""C8 Clipper: detection (frames, face clustering, facecam scan, speaking, template rule)."""
from __future__ import annotations

import numpy as np
import pytest

from ns_common import ffmpeg
from ns_common.errors import NsError
from ns_studio.clipper import detect as det
from ns_studio.clipper.models import Rect
from tests.fixtures.clipper.make_sample import facecam_truth, shared_sample

needs_ffmpeg = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not available")

FW, FH = 320, 180
CAM = (232, 10, 80, 45)  # x, y, w, h in the synthetic frames (6.25 % of the area, 16:9, top right)


def synth_frames(
    n: int = 10, cam: tuple[int, int, int, int] | None = CAM, stripes: bool = False, seed: int = 3
) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    frames: list[np.ndarray] = []
    for i in range(n):
        bg = rng.integers(90, 120, size=(FH, FW)).astype(np.uint8)  # low-amplitude noise: no strong edges
        if stripes:
            bg[:, 100:103] = 255
            bg[:, 200:203] = 255
        if cam is not None:
            x, y, w, h = cam
            bg[y : y + h, x : x + w] = 255  # frame
            bg[y + 2 : y + h - 2, x + 2 : x + w - 2] = 60  # inside
            bx = x + 10 + (i * 7) % (w - 30)  # moving blob inside
            bg[y + 12 : y + 28, bx : bx + 14] = 230
        frames.append(bg)
    return frames


def close(r: Rect | None, want: tuple[int, int, int, int], tol: int) -> bool:
    return r is not None and all(abs(a - b) <= tol for a, b in zip((r.x, r.y, r.w, r.h), want, strict=True))


# ---------- sizes ----------
def test_work_size() -> None:
    assert det.work_size(1920, 1080) == (640, 360)
    assert det.work_size(1280, 720) == (640, 360)
    assert det.work_size(320, 180) == (320, 180)
    assert det.work_size(1080, 1920) == (640, 1138)


def test_spread_times_inside_the_video() -> None:
    ts = det.spread_times(100.0)
    assert len(ts) == 10 and ts[0] == pytest.approx(5.0) and ts[-1] == pytest.approx(95.0)


# ---------- facecam scan ----------
def test_scan_finds_synthetic_rectangle_without_cv2() -> None:
    r = det.scan_facecam(synth_frames(), use_cv2=False)
    assert close(r, CAM, 4), r


def test_scan_ignores_full_height_stripes() -> None:
    r = det.scan_facecam(synth_frames(stripes=True), use_cv2=False)
    assert close(r, CAM, 4), r


def test_scan_none_without_a_rectangle() -> None:
    assert det.scan_facecam(synth_frames(cam=None), use_cv2=False) is None


def test_scan_rejects_center_rectangles() -> None:
    assert det.scan_facecam(synth_frames(cam=(120, 60, 80, 45)), use_cv2=False) is None


def test_scan_rejects_wrong_aspect() -> None:
    assert det.scan_facecam(synth_frames(cam=(232, 10, 60, 60)), use_cv2=False) is None


def test_scan_needs_enough_frames() -> None:
    assert det.scan_facecam(synth_frames(n=2), use_cv2=False) is None


def test_scan_with_opencv() -> None:
    pytest.importorskip("cv2")
    r = det.scan_facecam(synth_frames(), use_cv2=True)
    assert close(r, CAM, 4), r


# ---------- face clustering ----------
def jitter(x: int, y: int, i: int) -> tuple[int, int, int, int]:
    return (x + (i % 3) - 1, y + (i % 2), 40, 40)


def test_single_stable_face() -> None:
    tracks = det.cluster_faces([[jitter(100, 50, i)] for i in range(10)], 640)
    assert len(tracks) == 1 and tracks[0].presence == 1.0


def test_two_faces_sorted_left_to_right() -> None:
    dets = [[jitter(400, 50, i), jitter(100, 50, i)] for i in range(10)]
    tracks = det.cluster_faces(dets, 640)
    assert len(tracks) == 2 and tracks[0].cx < tracks[1].cx


def test_presence_threshold_is_seventy_percent() -> None:
    seven = [[jitter(100, 50, i)] if i < 7 else [] for i in range(10)]
    five = [[jitter(100, 50, i)] if i < 5 else [] for i in range(10)]
    assert len(det.cluster_faces(seven, 640)) == 1
    assert det.cluster_faces(five, 640) == []


def test_wandering_face_is_not_stable() -> None:
    dets = [[(100 if i % 2 else 400, 50, 40, 40)] for i in range(10)]
    assert det.cluster_faces(dets, 640) == []


def test_cluster_without_frames() -> None:
    assert det.cluster_faces([], 640) == []


# ---------- speaking ----------
def tracks_pair() -> list[det.FaceTrack]:
    return [
        det.FaceTrack(cx=50, cy=50, w=40, h=40, presence=1.0),
        det.FaceTrack(cx=150, cy=50, w=40, h=40, presence=1.0),
    ]


def test_speaking_picks_the_face_with_lower_third_motion() -> None:
    frames = []
    for i in range(6):
        g = np.zeros((100, 200), dtype=np.uint8)
        if i % 2:
            g[56:70, 30:70] = 100  # lower third of face 0 flickers
        frames.append(det.Frame(t=float(i), gray=g))
    assert det.speaking_series(frames, tracks_pair()) == [0] * 6


def test_speaking_none_when_still() -> None:
    frames = [det.Frame(t=float(i), gray=np.zeros((100, 200), dtype=np.uint8)) for i in range(4)]
    assert det.speaking_series(frames, tracks_pair()) == [None] * 4


def test_speaking_needs_two_faces() -> None:
    frames = [det.Frame(t=0.0, gray=np.zeros((10, 10), dtype=np.uint8))]
    assert det.speaking_series(frames, tracks_pair()[:1]) == [None]


# ---------- template rule ----------
CAMRECT = Rect(x=1, y=1, w=10, h=6)


@pytest.mark.parametrize(
    ("facecam", "faces", "want"),
    [
        (CAMRECT, 0, "gaming_facecam"),
        (CAMRECT, 3, "gaming_facecam"),
        (None, 2, "podcast_stacked"),
        (None, 1, "talking_head"),
        (None, 0, "center_crop"),
    ],
)
def test_auto_rule(facecam: Rect | None, faces: int, want: str) -> None:
    assert det.decide_template("auto", facecam, faces) == (want, None)


def test_requested_template_applicable() -> None:
    assert det.decide_template("podcast_stacked", None, 2) == ("podcast_stacked", None)
    assert det.decide_template("center_crop", None, 0) == ("center_crop", None)
    assert det.decide_template("screen_plus_cam", CAMRECT, 0) == ("screen_plus_cam", None)


@pytest.mark.parametrize(
    ("requested", "facecam", "faces"),
    [
        ("gaming_facecam", None, 3),
        ("screen_plus_cam", None, 1),
        ("podcast_stacked", None, 1),
        ("talking_head", CAMRECT, 0),
        ("podcast_single", None, 0),
    ],
)
def test_inapplicable_template_falls_back_with_w402(requested: str, facecam: Rect | None, faces: int) -> None:
    assert det.decide_template(requested, facecam, faces) == ("center_crop", "NS-W402")


def test_unknown_template_is_an_error() -> None:
    with pytest.raises(NsError):
        det.decide_template("nope", None, 0)


# ---------- detector plumbing ----------
def test_load_detector_none_without_model(tmp_path) -> None:
    assert det.load_detector(tmp_path / "missing.onnx") is None


def test_fake_detector_counts_calls() -> None:
    fd = det.FakeFaceDetector(lambda i: [(i, 0, 1, 1)])
    frame = np.zeros((4, 4, 3), dtype=np.uint8)
    assert fd.detect(frame) == [(0, 0, 1, 1)] and fd.detect(frame) == [(1, 0, 1, 1)]


# ---------- FFmpeg-backed ----------
@needs_ffmpeg
def test_sampling_shapes(tmp_path) -> None:
    sample = shared_sample()
    size = det.work_size(1280, 720)
    rng = det.sample_range(sample.video, 0.0, 10.0, size, tmp_path / "t")
    assert 9 <= len(rng) <= 11 and rng[0].gray.shape == (360, 640) and rng[0].bgr is None
    pts = det.sample_points(sample.video, [5.0, 45.0], size, tmp_path / "t", color=True)
    assert len(pts) == 2 and pts[0].bgr is not None and pts[0].bgr.shape == (360, 640, 3)


@needs_ffmpeg
def test_fixture_facecam_found_within_five_percent(tmp_path) -> None:
    sample = shared_sample()
    res = det.run_detection(
        sample.video, [(0.0, 30.0)], 90.0, (1280, 720), tmp_path / "t", detector=None, use_cv2=False
    )
    truth = facecam_truth()
    assert res.facecam is not None
    assert abs(res.facecam.x - truth["x"]) <= 0.05 * 1280
    assert abs(res.facecam.y - truth["y"]) <= 0.05 * 720
    assert abs(res.facecam.w - truth["w"]) <= 0.05 * 1280
    assert abs(res.facecam.h - truth["h"]) <= 0.05 * 720
    assert (res.template, res.warning, res.n_stable_faces) == ("gaming_facecam", None, 0)


@needs_ffmpeg
def test_fixture_facecam_found_with_opencv(tmp_path) -> None:
    pytest.importorskip("cv2")
    sample = shared_sample()
    res = det.run_detection(
        sample.video, [(0.0, 30.0)], 90.0, (1280, 720), tmp_path / "t", detector=None, use_cv2=True
    )
    truth = facecam_truth()
    assert res.facecam is not None
    assert abs(res.facecam.x - truth["x"]) <= 0.05 * 1280 and abs(res.facecam.y - truth["y"]) <= 0.05 * 720


@needs_ffmpeg
def test_run_detection_with_scripted_faces(tmp_path) -> None:
    sample = shared_sample()
    fake = det.FakeFaceDetector(lambda i: [(100, 100, 60, 60), (440, 100, 60, 60)])
    res = det.run_detection(
        sample.video, [(0.0, 10.0)], 90.0, (1280, 720), tmp_path / "t",
        detector=fake, requested="podcast_stacked", use_cv2=False,
    )
    assert res.n_stable_faces == 2 and res.template == "podcast_stacked" and res.warning is None
    assert 9 <= len(res.faces) <= 11
    assert [b.x for b in res.faces[0].boxes] == [200, 880]  # scaled x2 to source pixels


@needs_ffmpeg
def test_requested_stacked_without_faces_warns(tmp_path) -> None:
    sample = shared_sample()
    res = det.run_detection(
        sample.video, [(0.0, 5.0)], 90.0, (1280, 720), tmp_path / "t",
        detector=None, requested="podcast_stacked", use_cv2=False,
    )
    assert (res.template, res.warning) == ("center_crop", "NS-W402")
