"""C8 Clipper: detection (spec 19.2 C5, 19.4). Runs on CPU.

Frame sampling through FFmpeg raw output, YuNet faces (OpenCV, lazy), stable-face clustering, a facecam
rectangle scan (Canny with OpenCV, simple gradients with numpy otherwise), a lower-third motion speaking
heuristic, and the `auto` template rule. Heavy imports (`cv2`) happen inside functions only.
"""
from __future__ import annotations

import importlib
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from ns_common import ffmpeg
from ns_common.errors import NsError
from ns_common.models import FaceFrame, Rect

BoxT = tuple[int, int, int, int]  # x, y, w, h

WORK_WIDTH = 640
YUNET_SCORE = 0.8
FACE_RADIUS_FRAC = 0.15
FACE_MIN_PRESENCE = 0.70
SPREAD_FRAMES = 10
MAX_RANGE_FRAMES = 900

TEMPLATE_NEEDS: dict[str, str] = {
    "gaming_facecam": "facecam",
    "screen_plus_cam": "facecam",
    "podcast_stacked": "faces_2",
    "podcast_single": "faces_1",
    "talking_head": "faces_1",
    "center_crop": "none",
}


# ---------- frames ----------
@dataclass
class Frame:
    """A sampled frame at source time `t`; `gray` is always set, `bgr` only when colour was requested."""

    t: float
    gray: np.ndarray
    bgr: np.ndarray | None = None


def work_size(src_w: int, src_h: int, width: int = WORK_WIDTH) -> tuple[int, int]:
    """Working frame size: `width` wide (never upscaled), even height, same aspect."""
    w = min(width, src_w)
    w -= w % 2
    h = int(round(w * src_h / src_w / 2.0)) * 2
    return w, max(2, h)


def _to_gray(bgr: np.ndarray) -> np.ndarray:
    b = bgr[..., 0].astype(np.float32)
    g = bgr[..., 1].astype(np.float32)
    r = bgr[..., 2].astype(np.float32)
    return (0.114 * b + 0.587 * g + 0.299 * r).astype(np.uint8)


def _read_raw(path: Path, size: tuple[int, int], color: bool) -> list[np.ndarray]:
    w, h = size
    data = np.fromfile(path, dtype=np.uint8)
    per = w * h * (3 if color else 1)
    n = data.size // per
    shape = (h, w, 3) if color else (h, w)
    return [data[i * per : (i + 1) * per].reshape(shape) for i in range(n)]


def _frames_from_raw(
    raw: list[np.ndarray], times: Sequence[float], color: bool
) -> list[Frame]:
    out: list[Frame] = []
    for t, arr in zip(times, raw, strict=False):
        if color:
            out.append(Frame(t=t, gray=_to_gray(arr), bgr=arr))
        else:
            out.append(Frame(t=t, gray=arr))
    return out


def sample_range(
    video: Path, start: float, end: float, size: tuple[int, int], tmp_dir: Path, *, color: bool = False
) -> list[Frame]:
    """One frame per second in [start, end) scaled to `size`."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    raw_path = tmp_dir / f"range_{int(start * 1000)}_{int(end * 1000)}.raw"
    pix = "bgr24" if color else "gray"
    ffmpeg.run(
        [
            "-ss", f"{start:.3f}", "-t", f"{max(0.5, end - start):.3f}", "-i", str(video), "-an",
            "-vf", f"fps=1,scale={size[0]}:{size[1]}", "-f", "rawvideo", "-pix_fmt", pix, str(raw_path),
        ]
    )
    try:
        raw = _read_raw(raw_path, size, color)
    finally:
        raw_path.unlink(missing_ok=True)
    times = [start + i for i in range(len(raw))]
    return _frames_from_raw(raw, times, color)


def sample_points(
    video: Path, times: Sequence[float], size: tuple[int, int], tmp_dir: Path, *, color: bool = False
) -> list[Frame]:
    """One frame at each time of `times`, scaled to `size` (times that yield no frame are skipped)."""
    tmp_dir.mkdir(parents=True, exist_ok=True)
    pix = "bgr24" if color else "gray"
    out: list[Frame] = []
    for i, t in enumerate(times):
        raw_path = tmp_dir / f"pt_{i}.raw"
        ffmpeg.run(
            [
                "-ss", f"{t:.3f}", "-i", str(video), "-an", "-frames:v", "1",
                "-vf", f"scale={size[0]}:{size[1]}", "-f", "rawvideo", "-pix_fmt", pix, str(raw_path),
            ]
        )
        try:
            raw = _read_raw(raw_path, size, color)
        finally:
            raw_path.unlink(missing_ok=True)
        out.extend(_frames_from_raw(raw[:1], [t], color))
    return out


def spread_times(duration_s: float, n: int = SPREAD_FRAMES) -> list[float]:
    """`n` times spread over 5 % to 95 % of the video."""
    if n <= 1:
        return [duration_s / 2.0]
    return [duration_s * (0.05 + 0.90 * i / (n - 1)) for i in range(n)]


# ---------- faces ----------
class FaceDetector(Protocol):
    """Finds faces in a BGR frame; boxes are (x, y, w, h) in that frame's pixels."""

    def detect(self, frame_bgr: np.ndarray) -> list[BoxT]:
        """Return face boxes with score at or above the detector's threshold."""
        ...


def _cv2() -> Any:
    """The cv2 module, imported lazily; None when OpenCV is not installed."""
    try:
        return importlib.import_module("cv2")
    except ImportError:
        return None


class YuNetDetector:
    """YuNet via `cv2.FaceDetectorYN` (model at `<home>/models/cv/yunet.onnx`)."""

    def __init__(self, model_path: Path, score_threshold: float = YUNET_SCORE) -> None:
        self.model_path = model_path
        self.score_threshold = score_threshold
        self._det: Any = None

    @staticmethod
    def available(model_path: Path) -> bool:
        """True when OpenCV imports and the model file exists."""
        return _cv2() is not None and model_path.exists()

    def detect(self, frame_bgr: np.ndarray) -> list[BoxT]:
        """Detect faces; the frame is already working-size (640 px wide)."""
        cv2 = _cv2()
        if cv2 is None:
            return []
        h, w = frame_bgr.shape[:2]
        if self._det is None:
            self._det = cv2.FaceDetectorYN.create(
                str(self.model_path), "", (w, h), score_threshold=self.score_threshold
            )
        else:
            self._det.setInputSize((w, h))
        _, faces = self._det.detect(frame_bgr)
        if faces is None:
            return []
        return [
            (int(f[0]), int(f[1]), int(f[2]), int(f[3]))
            for f in faces
            if float(f[-1]) >= self.score_threshold
        ]


class FakeFaceDetector:
    """Scripted detector for tests: `script(call_index)` returns the boxes for that call."""

    def __init__(self, script: Callable[[int], list[BoxT]]) -> None:
        self.script = script
        self.calls = 0

    def detect(self, frame_bgr: np.ndarray) -> list[BoxT]:
        """Return the scripted boxes for this call."""
        boxes = self.script(self.calls)
        self.calls += 1
        return boxes


def load_detector(model_path: Path) -> FaceDetector | None:
    """A YuNet detector when OpenCV and the model are present, else None (faces are skipped)."""
    return YuNetDetector(model_path) if YuNetDetector.available(model_path) else None


@dataclass
class FaceTrack:
    """A stable face: mean box and the fraction of sampled frames it appears in."""

    cx: float
    cy: float
    w: float
    h: float
    presence: float
    hits: dict[int, BoxT] = field(default_factory=dict)  # frame index -> box

    @property
    def box(self) -> BoxT:
        """Mean box (x, y, w, h)."""
        return (int(self.cx - self.w / 2), int(self.cy - self.h / 2), int(self.w), int(self.h))


def _center(b: BoxT) -> tuple[float, float]:
    return b[0] + b[2] / 2.0, b[1] + b[3] / 2.0


def cluster_faces(
    detections: list[list[BoxT]],
    frame_w: int,
    *,
    radius_frac: float = FACE_RADIUS_FRAC,
    min_presence: float = FACE_MIN_PRESENCE,
) -> list[FaceTrack]:
    """Stable faces: clusters present in at least `min_presence` of the frames, sorted left to right.

    `detections[i]` are the boxes of sampled frame i. A face joins a cluster when its centre lies within
    `radius_frac * frame_w` of the cluster centre.
    """
    n = len(detections)
    if n == 0:
        return []
    radius = radius_frac * frame_w
    remaining: list[tuple[int, BoxT]] = [(i, b) for i, boxes in enumerate(detections) for b in boxes]
    tracks: list[FaceTrack] = []
    while remaining:
        best_members: list[tuple[int, BoxT]] = []
        best_frames = -1
        for _, seed in remaining[:: max(1, len(remaining) // 300)]:
            sx, sy = _center(seed)
            per_frame: dict[int, tuple[float, BoxT]] = {}
            for fi, b in remaining:
                bx, by = _center(b)
                d = math.hypot(bx - sx, by - sy)
                if d <= radius and (fi not in per_frame or d < per_frame[fi][0]):
                    per_frame[fi] = (d, b)
            if len(per_frame) > best_frames:
                best_frames = len(per_frame)
                best_members = [(fi, b) for fi, (_, b) in per_frame.items()]
        centers = [_center(b) for _, b in best_members]
        cx = sum(c[0] for c in centers) / len(centers)
        cy = sum(c[1] for c in centers) / len(centers)
        hits = {fi: b for fi, b in best_members}
        track = FaceTrack(
            cx=cx,
            cy=cy,
            w=sum(b[2] for _, b in best_members) / len(best_members),
            h=sum(b[3] for _, b in best_members) / len(best_members),
            presence=len(hits) / n,
            hits=hits,
        )
        remaining = [(fi, b) for fi, b in remaining if not _near(b, cx, cy, radius)]
        if track.presence >= min_presence:
            tracks.append(track)
    return sorted(tracks, key=lambda t: t.cx)


def _near(b: BoxT, cx: float, cy: float, radius: float) -> bool:
    bx, by = _center(b)
    return math.hypot(bx - cx, by - cy) <= radius


# ---------- speaking ----------
def speaking_series(frames: list[Frame], tracks: list[FaceTrack]) -> list[int | None]:
    """Per frame, the index of the track whose face lower third moves most (None when all are still).

    Motion is the mean absolute difference to the previous sampled frame inside the lower third of the
    track's mean box, averaged with the previous value (about 1 s of smoothing at 1 fps).
    """
    if len(tracks) < 2 or len(frames) < 2:
        return [None] * len(frames)
    n_tracks = len(tracks)
    scores = np.zeros((len(frames), n_tracks), dtype=np.float32)
    for fi in range(1, len(frames)):
        a = frames[fi - 1].gray.astype(np.int16)
        b = frames[fi].gray.astype(np.int16)
        for ti, tr in enumerate(tracks):
            x, y, w, h = tr.box
            y0 = max(0, y + (2 * h) // 3)
            y1 = min(b.shape[0], y + h)
            x0, x1 = max(0, x), min(b.shape[1], x + w)
            if y1 <= y0 or x1 <= x0:
                continue
            scores[fi, ti] = float(np.mean(np.abs(b[y0:y1, x0:x1] - a[y0:y1, x0:x1])))
    scores[0] = scores[1]
    smooth = scores.copy()
    smooth[1:] = (scores[1:] + scores[:-1]) / 2.0
    out: list[int | None] = []
    for row in smooth:
        top = int(np.argmax(row))
        out.append(top if float(row[top]) > 0.5 else None)
    return out


# ---------- facecam scan ----------
@dataclass(frozen=True)
class FacecamParams:
    """Facecam rectangle scan thresholds (spec 19.4; min_area_frac deviates from 8 %, see plan)."""

    min_area_frac: float = 0.05
    max_area_frac: float = 0.30
    min_aspect: float = 4 / 3 - 0.06
    max_aspect: float = 16 / 9 + 0.06
    persistence: float = 0.8
    side_cover: float = 0.85
    corner_frac: float = 0.35
    max_lines: int = 80
    min_side_frac: float = 0.12


DEFAULT_FACECAM = FacecamParams()


def _gradient_edges(gray: np.ndarray, thresh: int = 40) -> np.ndarray:
    g = gray.astype(np.int16)
    edges = np.zeros(gray.shape, dtype=bool)
    edges[:, 1:] |= np.abs(g[:, 1:] - g[:, :-1]) > thresh
    edges[1:, :] |= np.abs(g[1:, :] - g[:-1, :]) > thresh
    return edges


def edge_map(gray: np.ndarray, *, use_cv2: bool = True) -> np.ndarray:
    """Boolean edge map: OpenCV Canny (united with the gradient map) when available, else gradients only.

    Canny alone thins a 1-2 px overlay border to whichever flank wins non-maximum suppression, which flips
    from frame to frame; the gradient map marks both flanks every time, so the union stays persistent.
    """
    cv2 = _cv2() if use_cv2 else None
    if cv2 is not None:
        return (np.asarray(cv2.Canny(gray, 80, 160)) > 0) | _gradient_edges(gray)
    return _gradient_edges(gray)


def _dilate(mask: np.ndarray) -> np.ndarray:
    out = mask.copy()
    out[1:, :] |= mask[:-1, :]
    out[:-1, :] |= mask[1:, :]
    out[:, 1:] |= mask[:, :-1]
    out[:, :-1] |= mask[:, 1:]
    return out


def _line_candidates(mask: np.ndarray, axis: int, min_len: int, limit: int) -> list[int]:
    """Indices of columns (axis=0) or rows (axis=1) holding a run of at least `min_len` set pixels."""
    n = mask.shape[1] if axis == 0 else mask.shape[0]
    scored: list[tuple[int, int]] = []
    for i in range(n):
        line = mask[:, i] if axis == 0 else mask[i, :]
        d = np.diff(np.concatenate(([0], line.astype(np.int8), [0])))
        starts = np.flatnonzero(d == 1)
        ends = np.flatnonzero(d == -1)
        longest = int(np.max(ends - starts)) if starts.size else 0
        if longest >= min_len:
            scored.append((longest, i))
    scored.sort(reverse=True)
    picked: list[int] = []
    for _, i in scored:
        if all(abs(i - p) > 2 for p in picked):
            picked.append(i)
        if len(picked) >= limit:
            break
    return sorted(picked)


def scan_facecam(
    grays: Sequence[np.ndarray],
    params: FacecamParams = DEFAULT_FACECAM,
    *,
    face_boxes: Sequence[BoxT] = (),
    use_cv2: bool = True,
) -> Rect | None:
    """Find a stable axis-aligned rectangle (a facecam overlay) in `grays`; coordinates of the frames.

    Edges are accumulated over the frames; a rectangle needs all four sides covered by edges present in at
    least `persistence` of the frames. Aspect 4:3 to 16:9, area within the parameter range, centre in a
    corner zone. Ties prefer more faces inside, then better coverage, then the larger rectangle.
    """
    n = len(grays)
    if n < 3:
        return None
    h, w = grays[0].shape[:2]
    acc = np.zeros((h, w), dtype=np.int32)
    for g in grays:
        acc += edge_map(g, use_cv2=use_cv2).astype(np.int32)
    need = int(math.ceil(params.persistence * n - 1e-9))
    pd = _dilate(acc >= need)
    xs = _line_candidates(pd, 0, int(params.min_side_frac * h), params.max_lines)
    ys = _line_candidates(pd, 1, int(params.min_side_frac * w), params.max_lines)
    if len(xs) < 2 or len(ys) < 2:
        return None
    pdi = pd.astype(np.int32)
    row_cum = np.concatenate([np.zeros((h, 1), np.int32), np.cumsum(pdi, axis=1)], axis=1)  # (h, w+1)
    col_cum = np.concatenate([np.zeros((1, w), np.int32), np.cumsum(pdi, axis=0)], axis=0)  # (h+1, w)

    xa = np.array(xs)
    ya = np.array(ys)
    frame_area = float(w * h)
    w_lo = 0.9 * math.sqrt(params.min_area_frac * frame_area * params.min_aspect)
    w_hi = 1.1 * math.sqrt(params.max_area_frac * frame_area * params.max_aspect)
    h_lo = 0.9 * math.sqrt(params.min_area_frac * frame_area / params.max_aspect)
    h_hi = 1.1 * math.sqrt(params.max_area_frac * frame_area / params.min_aspect)
    ix1, ix2 = np.triu_indices(len(xa), k=1)
    iy1, iy2 = np.triu_indices(len(ya), k=1)
    rw = (xa[ix2] - xa[ix1] + 1).astype(np.float64)
    rh = (ya[iy2] - ya[iy1] + 1).astype(np.float64)
    cx = (xa[ix1] + xa[ix2]) / 2.0
    cy = (ya[iy1] + ya[iy2]) / 2.0
    in_x = (cx < params.corner_frac * w) | (cx > (1 - params.corner_frac) * w)
    in_y = (cy < params.corner_frac * h) | (cy > (1 - params.corner_frac) * h)
    kx = (rw >= w_lo) & (rw <= w_hi) & in_x
    ky = (rh >= h_lo) & (rh <= h_hi) & in_y
    ix1, ix2, rw = ix1[kx], ix2[kx], rw[kx]
    iy1, iy2, rh = iy1[ky], iy2[ky], rh[ky]
    if rw.size == 0 or rh.size == 0:
        return None
    area = rw[:, None] * rh[None, :] / frame_area
    aspect = rw[:, None] / rh[None, :]
    ok = (
        (area >= params.min_area_frac)
        & (area <= params.max_area_frac)
        & (aspect >= params.min_aspect)
        & (aspect <= params.max_aspect)
    )
    pi, pj = np.nonzero(ok)
    if pi.size == 0:
        return None
    x1, x2 = xa[ix1[pi]], xa[ix2[pi]]
    y1, y2 = ya[iy1[pj]], ya[iy2[pj]]
    top = (row_cum[y1, x2 + 1] - row_cum[y1, x1]) / (x2 - x1 + 1)
    bottom = (row_cum[y2, x2 + 1] - row_cum[y2, x1]) / (x2 - x1 + 1)
    left = (col_cum[y2 + 1, x1] - col_cum[y1, x1]) / (y2 - y1 + 1)
    right = (col_cum[y2 + 1, x2] - col_cum[y1, x2]) / (y2 - y1 + 1)
    cover = np.minimum(np.minimum(top, bottom), np.minimum(left, right))
    good = np.flatnonzero(cover >= params.side_cover)
    if good.size == 0:
        return None
    best: tuple[int, int, int] | None = None
    best_idx = -1
    for gi in good:
        rect = (int(x1[gi]), int(y1[gi]), int(x2[gi] - x1[gi] + 1), int(y2[gi] - y1[gi] + 1))
        inside = sum(1 for fb in face_boxes if _box_inside(fb, rect))
        key = (inside, int(round(float(cover[gi]) * 10)), rect[2] * rect[3])
        if best is None or key > best:
            best, best_idx = key, int(gi)
    g = best_idx
    return Rect(x=int(x1[g]), y=int(y1[g]), w=int(x2[g] - x1[g] + 1), h=int(y2[g] - y1[g] + 1))


def _box_inside(b: BoxT, rect: BoxT) -> bool:
    cx, cy = _center(b)
    return rect[0] <= cx <= rect[0] + rect[2] and rect[1] <= cy <= rect[1] + rect[3]


# ---------- template rule ----------
def decide_template(requested: str, facecam: Rect | None, n_faces: int) -> tuple[str, str | None]:
    """(template, warning code). `auto` picks by facecam / stable faces; an inapplicable request falls back
    to center_crop with `NS-W402`."""
    if requested == "auto":
        if facecam is not None:
            return "gaming_facecam", None
        if n_faces >= 2:
            return "podcast_stacked", None
        if n_faces == 1:
            return "talking_head", None
        return "center_crop", None
    need = TEMPLATE_NEEDS.get(requested)
    if need is None:
        raise NsError("NS-E405", f"unknown template {requested!r}")
    ok = (
        need == "none"
        or (need == "facecam" and facecam is not None)
        or (need == "faces_1" and n_faces >= 1)
        or (need == "faces_2" and n_faces >= 2)
    )
    return (requested, None) if ok else ("center_crop", "NS-W402")


# ---------- whole-video detection (analysis step) ----------
CHUNK_S = 120.0


@dataclass
class DetectResult:
    """Outcome of `detect_video`; coordinates are source pixels and source seconds."""

    facecam: Rect | None
    faces: list[FaceFrame]  # one entry per sampled second; boxes follow the stable tracks, left to right
    n_stable_faces: int
    faces_detected: bool  # False when no face detector was available


@dataclass
class _Det:
    box: BoxT
    motion: float  # mean abs difference of the face's lower third to the previous sampled frame


def _scale_box(b: BoxT, f: float) -> Rect:
    return Rect(x=int(b[0] * f), y=int(b[1] * f), w=max(1, int(b[2] * f)), h=max(1, int(b[3] * f)))


def lower_third_motion(prev: np.ndarray, cur: np.ndarray, box: BoxT) -> float:
    """Mean absolute grey difference inside the lower third of `box` between two frames."""
    x, y, w, h = box
    y0, y1 = max(0, y + (2 * h) // 3), min(cur.shape[0], y + h)
    x0, x1 = max(0, x), min(cur.shape[1], x + w)
    if y1 <= y0 or x1 <= x0:
        return 0.0
    a = prev[y0:y1, x0:x1].astype(np.int16)
    b = cur[y0:y1, x0:x1].astype(np.int16)
    return float(np.mean(np.abs(b - a)))


def speaking_from_motion(rows: list[list[float]]) -> list[int | None]:
    """Per frame, the track with the most lower-third motion (smoothed over the last two samples)."""
    if not rows:
        return []
    arr = np.array(rows, dtype=np.float32)
    smooth = arr.copy()
    smooth[1:] = (arr[1:] + arr[:-1]) / 2.0
    out: list[int | None] = []
    for row in smooth:
        top = int(np.argmax(row))
        out.append(top if float(row[top]) > 0.5 else None)
    return out


def _closest(dets: list[_Det], track: FaceTrack, radius: float) -> _Det | None:
    """The detection nearest the track centre within `radius`, else None."""
    best: _Det | None = None
    best_d = radius
    for d in dets:
        cx, cy = _center(d.box)
        dist = math.hypot(cx - track.cx, cy - track.cy)
        if dist < best_d:
            best, best_d = d, dist
    return best


def detect_video(
    video: Path,
    duration_s: float,
    src_size: tuple[int, int],
    tmp_dir: Path,
    *,
    detector: FaceDetector | None,
    params: FacecamParams = DEFAULT_FACECAM,
    use_cv2: bool = True,
    chunk_s: float = CHUNK_S,
) -> DetectResult:
    """Facecam scan on 10 spread frames, then (with a detector) faces at 1 fps over the whole video.

    Stable faces are clustered over every sampled frame; the speaking face per second comes from the
    lower-third motion measured in the same pass.
    """
    size = work_size(*src_size)
    scale = src_size[0] / size[0]
    color = detector is not None
    spread = sample_points(video, spread_times(duration_s), size, tmp_dir, color=color)
    spread_dets = [detector.detect(f.bgr) for f in spread if f.bgr is not None] if detector else []
    flat_boxes = [b for boxes in spread_dets for b in boxes]
    fc = scan_facecam([f.gray for f in spread], params, face_boxes=flat_boxes, use_cv2=use_cv2)
    facecam = (
        Rect(x=int(fc.x * scale), y=int(fc.y * scale), w=int(fc.w * scale), h=int(fc.h * scale))
        if fc
        else None
    )
    if detector is None:
        return DetectResult(facecam=facecam, faces=[], n_stable_faces=0, faces_detected=False)

    times: list[float] = []
    per_frame: list[list[_Det]] = []
    prev: np.ndarray | None = None
    start = 0.0
    while start < duration_s:
        end = min(duration_s, start + chunk_s)
        for f in sample_range(video, start, end, size, tmp_dir, color=True):
            if f.bgr is None:
                continue
            dets = [
                _Det(b, lower_third_motion(prev, f.gray, b) if prev is not None else 0.0)
                for b in detector.detect(f.bgr)
            ]
            times.append(f.t)
            per_frame.append(dets)
            prev = f.gray
        start = end
    tracks = cluster_faces([*spread_dets, *[[d.box for d in fr] for fr in per_frame]], size[0])
    if not tracks:
        return DetectResult(facecam=facecam, faces=[], n_stable_faces=0, faces_detected=True)
    radius = FACE_RADIUS_FRAC * size[0]
    matched = [[_closest(fr, tr, radius) for tr in tracks] for fr in per_frame]
    rows = [[m.motion if m else 0.0 for m in row] for row in matched]
    speaking = speaking_from_motion(rows) if len(tracks) >= 2 else [None] * len(rows)
    faces = [
        FaceFrame(
            t=times[i],
            boxes=[_scale_box(m.box if m else tr.box, scale) for m, tr in zip(row, tracks, strict=True)],
            speaking=speaking[i],
        )
        for i, row in enumerate(matched)
    ]
    return DetectResult(facecam=facecam, faces=faces, n_stable_faces=len(tracks), faces_detected=True)


# ---------- per-clip layout choice (spec 23.5 step 3) ----------
def normalize_layout_id(name: str | None) -> str | None:
    """`clip_gaming_facecam` (format preset id) or `gaming_facecam` -> the template name; None if unknown."""
    if not name:
        return None
    n = name.strip().lower().removeprefix("clip_")
    return n if n in TEMPLATE_NEEDS else None


def decide_clip_template(
    user_template: str, model_layout: str | None, facecam: Rect | None, n_faces: int
) -> tuple[str, str | None]:
    """Template for one clip and a warning code.

    An explicit user template follows `decide_template` (not applicable -> center_crop, NS-W402). With
    `auto`, the model's layout wins when it applies; when it does not, the section 19.4 auto rule is the
    fallback and the warning is NS-W402.
    """
    if user_template != "auto":
        return decide_template(user_template, facecam, n_faces)
    auto, _ = decide_template("auto", facecam, n_faces)
    wanted = normalize_layout_id(model_layout)
    if wanted is None:
        return auto, ("NS-W402" if model_layout else None)
    chosen, warn = decide_template(wanted, facecam, n_faces)
    return (chosen, None) if warn is None else (auto, "NS-W402")
