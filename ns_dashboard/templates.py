"""Clip templates for the Clipper page: yaml loader with a built-in fallback, Pillow previews (C9)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ns_common.paths import Paths

FRAME_W = 1080
FRAME_H = 1920


def _box(name: str, x: float, y: float, w: float, h: float) -> dict[str, Any]:
    return {"name": name, "x": x, "y": y, "w": w, "h": h}


# Spec 19.3 layouts as fractions of the 1080x1920 frame.
BUILTIN: list[dict[str, Any]] = [
    {
        "id": "auto",
        "label": "Let Nightshift choose",
        "description": "Looks at the video and picks the layout that fits.",
        "when": "Default",
        "regions": [],
        "caption_y_pct": 70,
        "title_card": False,
    },
    {
        "id": "gaming_facecam",
        "label": "Gameplay with face cam",
        "description": "Your face cam on top, the game filling the rest.",
        "when": "A face cam is found in a corner",
        "regions": [_box("facecam", 0, 0, 1, 0.3333), _box("gameplay", 0, 0.3333, 1, 0.6667)],
        "caption_y_pct": 33,
        "title_card": False,
    },
    {
        "id": "podcast_stacked",
        "label": "Two people, stacked",
        "description": "Both speakers one above the other; the talker gets a thin border.",
        "when": "Two faces on screen",
        "regions": [_box("speaker 1", 0, 0, 1, 0.5), _box("speaker 2", 0, 0.5, 1, 0.5)],
        "caption_y_pct": 72,
        "title_card": False,
    },
    {
        "id": "podcast_single",
        "label": "One person, follows the face",
        "description": "A single tall crop that follows whoever is on screen.",
        "when": "Two faces, but you want one",
        "regions": [_box("face", 0, 0, 1, 1)],
        "caption_y_pct": 68,
        "title_card": False,
    },
    {
        "id": "talking_head",
        "label": "Talking head",
        "description": "A tall crop that follows one speaker.",
        "when": "One face on screen",
        "regions": [_box("face", 0, 0, 1, 1)],
        "caption_y_pct": 68,
        "title_card": False,
    },
    {
        "id": "center_crop",
        "label": "Centre crop on blur",
        "description": "The picture in the middle over a soft blurred background.",
        "when": "Slides, screen recordings, no faces",
        "regions": [_box("blurred fill", 0, 0, 1, 1), _box("picture", 0, 0.34, 1, 0.32)],
        "caption_y_pct": 72,
        "title_card": False,
    },
    {
        "id": "screen_plus_cam",
        "label": "Screen with small cam",
        "description": "Face cam on top, the whole screen below it.",
        "when": "Tutorials with a small cam",
        "regions": [_box("facecam", 0, 0, 1, 0.3333), _box("screen", 0, 0.3333, 1, 0.3167)],
        "caption_y_pct": 70,
        "title_card": False,
    },
]


def yaml_path() -> Path | None:
    """`ns_studio/presets/clip_templates.yaml` when C8 has added it."""
    try:
        import ns_studio
    except ImportError:
        return None
    f = Path(ns_studio.__file__).resolve().parent / "presets" / "clip_templates.yaml"
    return f if f.exists() else None


def _fraction(value: Any, full: int) -> float:
    v = float(value)
    return round(v / full if v > 1.5 else v, 4)


def _normalise_box(name: str, raw: Any) -> dict[str, Any] | None:
    d: dict[str, Any]
    if isinstance(raw, dict):
        d = raw
    elif isinstance(raw, list | tuple) and len(raw) == 4:
        d = dict(zip(("x", "y", "w", "h"), raw, strict=True))
    else:
        return None
    try:
        return {
            "name": str(d.get("name", name)),
            "x": _fraction(d["x"], FRAME_W),
            "y": _fraction(d["y"], FRAME_H),
            "w": _fraction(d["w"], FRAME_W),
            "h": _fraction(d["h"], FRAME_H),
        }
    except (KeyError, TypeError, ValueError):
        return None


def _resolve_list(regions: list[Any]) -> tuple[list[dict[str, Any]], float | None]:
    """Pixel-resolve a region list: `y: seam` follows the previous panel, `h: auto|rest` fill the rest."""
    out: list[dict[str, Any]] = []
    seam: float | None = None
    cursor = 0.0
    for i, r in enumerate(regions):
        if not isinstance(r, dict):
            continue
        y: Any = r.get("y", 0)
        if y == "seam":
            y, seam = cursor, cursor
        h: Any = r.get("h")
        try:
            if h == "auto":
                h = r.get("max_h") or 640
            elif h == "rest":
                h = FRAME_H - float(y)
            cursor = float(y) + float(h)
        except (TypeError, ValueError):
            pass
        out.append({**r, "y": y, "h": h, "name": r.get("name") or r.get("id") or f"region {i + 1}"})
    return out, seam


def _titled(template_id: str) -> str:
    return template_id.replace("_", " ").title()


def normalise_template(template_id: str, raw: dict[str, Any], *, auto_label: bool = True) -> dict[str, Any]:
    """Convert one yaml template (several shapes accepted) into the API shape."""
    regions: list[dict[str, Any]] = []
    seam_y: float | None = None
    raw_regions = raw.get("regions") or {}
    if isinstance(raw_regions, dict):
        items = list(raw_regions.items())
    elif isinstance(raw_regions, list):
        resolved, seam_y = _resolve_list(raw_regions)
        items = [(str(d["name"]), d) for d in resolved]
    else:
        items = []
    for name, value in items:
        box = _normalise_box(str(name), value)
        if box:
            regions.append(box)
    caption = raw.get("caption")
    cap = raw.get("caption_y_pct", raw.get("caption_anchor_y_pct"))
    if cap == "seam":
        cap = (seam_y / FRAME_H * 100) if seam_y is not None else 33.0
    if cap is None and isinstance(caption, dict):
        cap = caption.get("y_pct", caption.get("y"))
    try:
        cap_f = float(cap) if cap is not None else 70.0
    except (TypeError, ValueError):
        cap_f = 70.0
    if cap_f <= 1.0:
        cap_f *= 100
    title_card = raw.get("title_card", False)
    if isinstance(title_card, dict):
        title_card = bool(title_card.get("enabled", False))
    return {
        "id": template_id,
        "label": str(raw.get("label") or raw.get("name") or (_titled(template_id) if auto_label else "")),
        "description": str(raw.get("description") or ""),
        "when": str(raw.get("when") or ""),
        "regions": regions,
        "caption_y_pct": round(cap_f, 1),
        "title_card": bool(title_card),
    }


def _from_yaml(f: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(f.read_text(encoding="utf-8"))
    if isinstance(data, dict) and isinstance(data.get("templates"), dict | list):
        data = data["templates"]
    out: list[dict[str, Any]] = []
    if isinstance(data, dict):
        for tid, raw in data.items():
            if isinstance(raw, dict):
                out.append(normalise_template(str(tid), raw, auto_label=False))
    elif isinstance(data, list):
        for raw in data:
            if isinstance(raw, dict) and raw.get("id"):
                out.append(normalise_template(str(raw["id"]), raw, auto_label=False))
    return out


def load_templates(paths: Paths | None = None) -> list[dict[str, Any]]:
    """Built-in templates overlaid with the C8 yaml (by id)."""
    by_id: dict[str, dict[str, Any]] = {t["id"]: dict(t) for t in BUILTIN}
    f = yaml_path()
    if f is not None:
        try:
            for t in _from_yaml(f):
                base = by_id.get(t["id"], {})
                merged = {**base, **{k: v for k, v in t.items() if v not in ("", [], None)}}
                by_id[t["id"]] = merged
        except (OSError, ValueError, yaml.YAMLError):
            pass
    out = []
    for t in by_id.values():
        t.setdefault("regions", [])
        if not t.get("label"):
            t["label"] = t["id"].replace("_", " ").title()
        out.append(t)
    return out
