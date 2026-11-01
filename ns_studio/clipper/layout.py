"""C8 Clipper: template loading and the layout engine (spec 19.3).

`plan_layout` turns a template plus detection results into per-time source regions (`Crop`s, in source
pixels and source seconds). `canvas_panels` maps a crop to the output rectangles on the 1080x1920 canvas.
Both are pure functions; the render module turns them into FFmpeg filter graphs.
"""
from __future__ import annotations

import statistics
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from ns_common.models import Format
from ns_studio.clipper.models import (
    CANVAS_H,
    CANVAS_W,
    ClipLayout,
    ClipTemplate,
    Crop,
    FaceFrame,
    Rect,
    Region,
)
from ns_studio.media.captions import Box
from ns_studio.media.formats import get_format

TRACK_STEP_S = 0.25
SPEAKER_HOLD_FRAMES = 2


def templates_path() -> Path:
    """Location of `clip_templates.yaml` (the compatibility map from template ids to format presets)."""
    return Path(__file__).resolve().parents[1] / "presets" / "clip_templates.yaml"


@lru_cache(maxsize=1)
def _compat() -> dict[str, dict[str, Any]]:
    raw = yaml.safe_load(templates_path().read_text(encoding="utf-8"))
    return {str(name): dict(body) for name, body in raw.items()}


def format_id_for(template: str) -> str:
    """Format preset id behind a template name (`gaming_facecam` or already `clip_gaming_facecam`)."""
    name = template.removeprefix("clip_") if template.removeprefix("clip_") in _compat() else template
    try:
        return str(_compat()[name]["format"])
    except KeyError as e:
        raise ValueError(f"unknown clip template {template!r}") from e


def format_for(template: str) -> Format:
    """The format preset (`presets/formats/clip_*.yaml`) that draws `template` (spec 17.1, CR-C8-8)."""
    return get_format(format_id_for(template))


def _template_regions(fmt: Format) -> list[Region]:
    """The format regions as clipper `Region`s: facecam height `auto` (capped), the panel below it `seam`."""
    cam = next((r for r in fmt.layout.regions if r.source == "facecam"), None)
    out: list[Region] = []
    for r in fmt.layout.regions:
        if cam is not None and r is cam:
            out.append(Region(id=r.name, x=r.x, y=r.y, w=r.w, h="auto", max_h=r.h))
        elif cam is not None and r.y == cam.y + cam.h:
            reaches_bottom = r.y + r.h == fmt.canvas.h
            out.append(Region(id=r.name, x=r.x, y="seam", w=r.w, h="rest" if reaches_bottom else r.h))
        else:
            out.append(Region(id=r.name, x=r.x, y=r.y, w=r.w, h=r.h))
    return out


@lru_cache(maxsize=1)
def load_templates() -> dict[str, ClipTemplate]:
    """`ClipTemplate`s keyed by name: the clipper's view of the six `clip_*` format presets.

    Geometry, caption anchor and safe margins come from the preset; `clip_templates.yaml` only adds what a
    preset does not describe (layout kind, detection needs, tracking constants).
    """
    out: dict[str, ClipTemplate] = {}
    for name, body in _compat().items():
        fmt = get_format(str(body["format"]))
        extra = {k: v for k, v in body.items() if k != "format"}
        out[name] = ClipTemplate(
            name=name,
            regions=_template_regions(fmt),
            caption_anchor_y_pct="seam" if fmt.layout.seam_captions else fmt.captions.anchor_y_pct,
            safe_top=fmt.safe_margins.top,
            safe_bottom=fmt.safe_margins.bottom,
            **extra,
        )
    return out


# ---------- small geometry helpers ----------
def _even(v: float) -> int:
    n = int(round(v))
    return max(2, n - (n % 2))


def center_crop(area: Rect, aspect: float) -> Rect:
    """Largest centred rectangle of `aspect` (w/h) inside `area`, with even dimensions."""
    if area.w / area.h > aspect:
        h = area.h - (area.h % 2)
        w = _even(h * aspect)
    else:
        w = area.w - (area.w % 2)
        h = _even(w / aspect)
    w, h = min(w, area.w), min(h, area.h)
    return Rect(x=area.x + (area.w - w) // 2, y=area.y + (area.h - h) // 2, w=w, h=h)


def face_crop(cx: float, cy: float, src_w: int, src_h: int, aspect: float) -> Rect:
    """Largest crop of `aspect` inside the frame, centred on (cx, cy) and pushed back inside the frame."""
    if src_w >= src_h * aspect:
        ch = src_h - (src_h % 2)
        cw = _even(ch * aspect)
    else:
        cw = src_w - (src_w % 2)
        ch = _even(cw / aspect)
    cw, ch = min(cw, src_w), min(ch, src_h)
    x = int(min(max(cx - cw / 2.0, 0), src_w - cw))
    y = int(min(max(cy - ch / 2.0, 0), src_h - ch))
    return Rect(x=x, y=y, w=cw, h=ch)


def camera_crop(cam: Rect, max_h: int) -> Rect:
    """The facecam region cropped (centred) so that scaled to 1080 px wide it is at most `max_h` tall."""
    nat_h = CANVAS_W * cam.h / cam.w
    if nat_h <= max_h:
        return cam
    new_h = _even(cam.w * max_h / CANVAS_W)
    new_h = min(new_h, cam.h)
    return Rect(x=cam.x, y=cam.y + (cam.h - new_h) // 2, w=cam.w, h=new_h)


def game_source(cam: Rect, src_w: int, src_h: int, aspect: float) -> Rect:
    """Source frame minus the facecam: the largest strip clear of the camera, centre-cropped to `aspect`."""
    options = [
        Rect(x=0, y=0, w=cam.x, h=src_h) if cam.x > 0 else None,
        Rect(x=cam.right, y=0, w=src_w - cam.right, h=src_h) if src_w - cam.right > 0 else None,
        Rect(x=0, y=0, w=src_w, h=cam.y) if cam.y > 0 else None,
        Rect(x=0, y=cam.bottom, w=src_w, h=src_h - cam.bottom) if src_h - cam.bottom > 0 else None,
    ]
    valid = [r for r in options if r is not None and r.w >= 16 and r.h >= 16]
    area = max(valid, key=lambda r: r.area) if valid else Rect(x=0, y=0, w=src_w, h=src_h)
    return center_crop(area, aspect)


def cam_panel_height(cam_src: Rect, max_h: int) -> int:
    """Output height of the camera panel (1080 px wide), even and at most `max_h`."""
    return min(_even(CANVAS_W * cam_src.h / cam_src.w), max_h - (max_h % 2))


# ---------- panels on the output canvas ----------
def canvas_panels(tpl: ClipTemplate, crop: Crop) -> dict[str, Rect]:
    """Output rectangles of the template's regions for `crop` (seam and auto heights resolved)."""
    cam_h = 0
    for r in tpl.regions:
        if r.h == "auto":
            if crop.region_b is None:
                raise ValueError(f"template {tpl.name} needs a camera crop")
            cam_h = cam_panel_height(crop.region_b, r.max_h or 640)
    out: dict[str, Rect] = {}
    for r in tpl.regions:
        y = cam_h if r.y == "seam" else r.y
        h = cam_h if r.h == "auto" else (CANVAS_H - y if r.h == "rest" else r.h)
        out[r.id] = Rect(x=r.x, y=y, w=r.w, h=h)
    return out


def caption_y_pct(tpl: ClipTemplate, crop: Crop) -> float:
    """Caption anchor as a percentage of the canvas height."""
    if tpl.caption_anchor_y_pct == "seam":
        panels = canvas_panels(tpl, crop)
        seam = next(iter(panels.values())).h
        return 100.0 * seam / CANVAS_H
    return float(tpl.caption_anchor_y_pct)


# ---------- tracking ----------
def smooth_track(
    samples: list[tuple[float, float]],
    crop_w: float,
    frame_w: float,
    t0: float,
    t1: float,
    *,
    deadband: float = 0.2,
    ramp_s: float = 1.0,
    step: float = TRACK_STEP_S,
) -> list[tuple[float, float]]:
    """Crop centre x over [t0, t1] following the face samples (t, x).

    The crop stays put until the face leaves the middle `2 * deadband` of the crop width, then glides to
    the face over `ramp_s` seconds (smoothstep).
    """
    lo, hi = crop_w / 2.0, frame_w - crop_w / 2.0

    def clamp(v: float) -> float:
        return float(min(max(v, lo), hi))

    if not samples:
        return [(t0, clamp(frame_w / 2.0)), (t1, clamp(frame_w / 2.0))]
    ts = np.array([s[0] for s in samples])
    xs = np.array([s[1] for s in samples])

    def face_x(t: float) -> float:
        return float(np.interp(t, ts, xs))

    start = clamp(face_x(t0))
    from_x, to_x, ramp_t0 = start, start, t0 - ramp_s
    out: list[tuple[float, float]] = []
    n = max(1, int(np.ceil((t1 - t0) / step)))
    for i in range(n + 1):
        t = min(t0 + i * step, t1)
        fx = face_x(t)
        p = min(max((t - ramp_t0) / ramp_s, 0.0), 1.0)
        pos = from_x + (to_x - from_x) * (p * p * (3 - 2 * p))
        if abs(fx - to_x) > deadband * crop_w:
            from_x, to_x, ramp_t0 = pos, clamp(fx), t
            pos = from_x
        out.append((t, clamp(pos)))
    return out


def _pick_box(f: FaceFrame, prev_x: float | None) -> Rect | None:
    if not f.boxes:
        return None
    if f.speaking is not None and f.speaking < len(f.boxes):
        return f.boxes[f.speaking]
    if prev_x is not None and len(f.boxes) > 1:
        return min(f.boxes, key=lambda b: abs(b.center[0] - prev_x))
    return max(f.boxes, key=lambda b: b.area)


def _merge(crops: list[Crop]) -> list[Crop]:
    out: list[Crop] = []
    for c in crops:
        last = out[-1] if out else None
        if last and (last.region, last.region_b, last.speaking) == (c.region, c.region_b, c.speaking):
            out[-1] = out[-1].model_copy(update={"t_end": c.t_end})
        else:
            out.append(c)
    return out


# ---------- layouts ----------
def _frames_in(layout: ClipLayout, t0: float, t1: float) -> list[FaceFrame]:
    inside = sorted((f for f in layout.faces if t0 - 0.5 <= f.t <= t1 + 0.5), key=lambda f: f.t)
    return inside or sorted(layout.faces, key=lambda f: f.t)


def _plan_gaming(
    tpl: ClipTemplate, src_w: int, src_h: int, layout: ClipLayout, t0: float, t1: float
) -> list[Crop]:
    if layout.facecam is None:
        raise ValueError(f"template {tpl.name} needs a facecam")
    cam_region = next(r for r in tpl.regions if r.h == "auto")
    cam = camera_crop(layout.facecam, cam_region.max_h or 640)
    cam_h = cam_panel_height(cam, cam_region.max_h or 640)
    if tpl.layout == "screen_cam":
        main = Rect(x=0, y=0, w=src_w, h=src_h)
    else:
        main = game_source(layout.facecam, src_w, src_h, CANVAS_W / (CANVAS_H - cam_h))
    return [Crop(t_start=t0, t_end=t1, region=main, region_b=cam)]


def _plan_stacked(src_w: int, src_h: int, layout: ClipLayout, t0: float, t1: float) -> list[Crop]:
    frames = [f for f in _frames_in(layout, t0, t1) if len(f.boxes) >= 2]
    if not frames:
        raise ValueError("podcast_stacked needs two faces")
    aspect = CANVAS_W / (CANVAS_H / 2)
    centers = []
    for k in (0, 1):
        cx = statistics.median(f.boxes[k].center[0] for f in frames)
        cy = statistics.median(f.boxes[k].center[1] for f in frames)
        centers.append((cx, cy))
    top = face_crop(centers[0][0], centers[0][1], src_w, src_h, aspect)
    bottom = face_crop(centers[1][0], centers[1][1], src_w, src_h, aspect)
    speakers: list[int | None] = []
    current: int | None = None
    run_val: int | None = None
    run_n = 0
    for f in frames:
        v = f.speaking if f.speaking in (0, 1) else None
        if v is not None and v != current:
            run_n = run_n + 1 if v == run_val else 1
            run_val = v
            if current is None or run_n >= SPEAKER_HOLD_FRAMES:
                current, run_val, run_n = v, None, 0
        else:
            run_val, run_n = None, 0
        speakers.append(current)
    crops: list[Crop] = []
    for i, f in enumerate(frames):
        start = t0 if i == 0 else max(t0, f.t)
        end = t1 if i == len(frames) - 1 else min(t1, frames[i + 1].t)
        if end > start:
            crops.append(Crop(t_start=start, t_end=end, region=top, region_b=bottom, speaking=speakers[i]))
    if not crops:
        crops = [Crop(t_start=t0, t_end=t1, region=top, region_b=bottom, speaking=None)]
    return _merge(crops)


def _plan_tracked(
    tpl: ClipTemplate, src_w: int, src_h: int, layout: ClipLayout, t0: float, t1: float
) -> list[Crop]:
    aspect = CANVAS_W / CANVAS_H
    base = face_crop(src_w / 2.0, src_h / 2.0, src_w, src_h, aspect)
    samples: list[tuple[float, float]] = []
    prev: float | None = None
    for f in _frames_in(layout, t0, t1):
        box = _pick_box(f, prev)
        if box is not None:
            prev = box.center[0]
            samples.append((f.t, prev))
    track = smooth_track(
        samples, base.w, src_w, t0, t1, deadband=tpl.track_deadband, ramp_s=tpl.track_ramp_s
    )
    crops: list[Crop] = []
    for (ta, xa), (tb, _) in zip(track, track[1:], strict=False):
        x = int(round(xa - base.w / 2.0))
        x = min(max(x, 0), src_w - base.w)
        if tb > ta:
            crops.append(Crop(t_start=ta, t_end=tb, region=Rect(x=x, y=base.y, w=base.w, h=base.h)))
    return _merge(crops) or [Crop(t_start=t0, t_end=t1, region=base)]


def _plan_center(src_w: int, src_h: int, t0: float, t1: float) -> list[Crop]:
    fg = center_crop(Rect(x=0, y=0, w=src_w, h=src_h), 776 / 1380)
    return [Crop(t_start=t0, t_end=t1, region=fg)]


def plan_layout(
    template: str,
    source_w: int,
    source_h: int,
    layout: ClipLayout,
    *,
    t_start: float = 0.0,
    t_end: float | None = None,
) -> list[Crop]:
    """Per-time source regions for `template` over [t_start, t_end] (source seconds).

    Raises ValueError when the detection data does not support the template (callers then fall back to
    `center_crop` and log NS-W402).
    """
    tpl = load_templates()[template]
    t1 = t_end if t_end is not None else max([f.t for f in layout.faces], default=t_start) + 1.0
    if t1 <= t_start:
        raise ValueError("empty time range")
    if tpl.layout in ("gaming", "screen_cam"):
        return _plan_gaming(tpl, source_w, source_h, layout, t_start, t1)
    if tpl.layout == "stacked":
        return _plan_stacked(source_w, source_h, layout, t_start, t1)
    if tpl.layout == "tracked":
        return _plan_tracked(tpl, source_w, source_h, layout, t_start, t1)
    return _plan_center(source_w, source_h, t_start, t1)


# ---------- caption placement inputs (spec 17.2) ----------
def panel_pairs(tpl: ClipTemplate, crop: Crop) -> list[tuple[str, Rect, Rect]]:
    """(panel id, panel on the canvas, source region shown in it) for every panel that shows source pixels."""
    panels = canvas_panels(tpl, crop)
    pairs: list[tuple[str, Rect, Rect]] = []
    if tpl.layout in ("gaming", "screen_cam"):
        main_id = "game" if tpl.layout == "gaming" else "screen"
        if crop.region_b is not None:
            pairs.append(("cam", panels["cam"], crop.region_b))
        pairs.append((main_id, panels[main_id], crop.region))
    elif tpl.layout == "stacked":
        pairs.append(("top", panels["top"], crop.region))
        if crop.region_b is not None:
            pairs.append(("bottom", panels["bottom"], crop.region_b))
    elif tpl.layout == "tracked":
        pairs.append(("main", panels["main"], crop.region))
    else:
        pairs.append(("fg", panels["fg"], crop.region))
    return pairs


def _union(boxes: list[Rect]) -> Rect:
    x0, y0 = min(b.x for b in boxes), min(b.y for b in boxes)
    x1, y1 = max(b.right for b in boxes), max(b.bottom for b in boxes)
    return Rect(x=x0, y=y0, w=x1 - x0, h=y1 - y0)


def face_tracks(layout: ClipLayout, t0: float, t1: float) -> list[Rect]:
    """One box per face (left to right): the union of its boxes over the frames around [t0, t1]."""
    frames = _frames_in(layout, t0, t1)
    out: list[Rect] = []
    for k in range(max((len(f.boxes) for f in frames), default=0)):
        boxes = [f.boxes[k] for f in frames if k < len(f.boxes)]
        if boxes:
            out.append(_union(boxes))
    return out


def _to_panel(box: Rect, region: Rect, panel: Rect) -> Rect | None:
    x0, y0 = max(box.x, region.x), max(box.y, region.y)
    x1, y1 = min(box.right, region.right), min(box.bottom, region.bottom)
    if x1 <= x0 or y1 <= y0:
        return None
    sx, sy = panel.w / region.w, panel.h / region.h
    return Rect(
        x=panel.x + round((x0 - region.x) * sx),
        y=panel.y + round((y0 - region.y) * sy),
        w=max(1, round((x1 - x0) * sx)),
        h=max(1, round((y1 - y0) * sy)),
    )


def avoid_boxes(tpl: ClipTemplate, crop: Crop, layout: ClipLayout, t0: float, t1: float) -> list[Box]:
    """Canvas rectangles captions must stay off: the facecam panel and every detected face (spec 17.2).

    Faces are mapped from source pixels into the panel that shows them; the facecam is avoided as a whole
    panel (gaming layouts put captions on the gameplay region, never over the camera).
    """
    out: list[Box] = []
    faces = face_tracks(layout, t0, t1)
    for panel_id, panel, region in panel_pairs(tpl, crop):
        if panel_id == "cam":
            out.append(Box(panel.x, panel.y, panel.w, panel.h))
            continue
        for f in faces:
            mapped = _to_panel(f, region, panel)
            if mapped is not None:
                out.append(Box(mapped.x, mapped.y, mapped.w, mapped.h))
    return out


def band_in_source(tpl: ClipTemplate, crop: Crop, band: Box, src_w: int, src_h: int) -> list[Box]:
    """Where the caption band's pixels come from in the source frame: one box per overlapped panel."""
    out: list[Box] = []
    for _pid, panel, region in panel_pairs(tpl, crop):
        x0, y0 = max(band.x, panel.x), max(band.y, panel.y)
        x1, y1 = min(band.x + band.w, panel.right), min(band.y + band.h, panel.bottom)
        if x1 <= x0 or y1 <= y0:
            continue
        sx, sy = region.w / panel.w, region.h / panel.h
        bx = min(max(0, region.x + int((x0 - panel.x) * sx)), src_w - 2)
        by = min(max(0, region.y + int((y0 - panel.y) * sy)), src_h - 2)
        bw = max(2, min(int((x1 - x0) * sx), src_w - bx))
        bh = max(2, min(int((y1 - y0) * sy), src_h - by))
        out.append(Box(bx, by, bw - bw % 2 or 2, bh - bh % 2 or 2))
    return out
