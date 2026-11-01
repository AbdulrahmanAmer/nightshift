"""Clip templates for the Clipper page: yaml loader with a built-in fallback, Pillow previews (C9)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from ns_common.paths import Paths
from ns_dashboard.errors import AdapterUnavailable

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


def normalise_template(template_id: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Convert one yaml template (several shapes accepted) into the API shape."""
    regions: list[dict[str, Any]] = []
    raw_regions = raw.get("regions") or {}
    if isinstance(raw_regions, dict):
        items = list(raw_regions.items())
    elif isinstance(raw_regions, list):
        items = [(str(r.get("name", f"region {i + 1}")) if isinstance(r, dict) else f"region {i + 1}", r)
                 for i, r in enumerate(raw_regions)]
    else:
        items = []
    for name, value in items:
        box = _normalise_box(str(name), value)
        if box:
            regions.append(box)
    caption = raw.get("caption")
    cap = raw.get("caption_y_pct", raw.get("caption_anchor_y_pct"))
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
        "label": str(raw.get("label") or template_id.replace("_", " ").title()),
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
                out.append(normalise_template(str(tid), raw))
    elif isinstance(data, list):
        for raw in data:
            if isinstance(raw, dict) and raw.get("id"):
                out.append(normalise_template(str(raw["id"]), raw))
    return out


def load_templates(paths: Paths | None = None) -> list[dict[str, Any]]:
    """Built-in templates overlaid with the C8 yaml (by id), each with its `preview_url`."""
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
        t["preview_url"] = f"/api/templates/{t['id']}/preview.png"
        out.append(t)
    return out


PALETTE = ["#3b82f6", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6"]


def render_preview(paths: Paths, template_id: str, *, scale: int = 5) -> Path:
    """PNG of the layout (regions plus caption bar) drawn with Pillow; cached by content hash.

    Raises KeyError for an unknown template and AdapterUnavailable when Pillow is missing.
    """
    tpl = next((t for t in load_templates(paths) if t["id"] == template_id), None)
    if tpl is None:
        raise KeyError(template_id)
    key = hashlib.sha1(json.dumps(tpl, sort_keys=True).encode()).hexdigest()[:10]
    out = paths.home / "cache" / "dashboard" / "templates" / f"{template_id}-{key}.png"
    if out.exists():
        return out
    try:
        from PIL import Image, ImageDraw
    except ImportError as e:
        raise AdapterUnavailable("Pillow is not installed") from e
    w, h = FRAME_W // scale, FRAME_H // scale
    img = Image.new("RGB", (w, h), "#0f141a")
    draw = ImageDraw.Draw(img)
    for i, r in enumerate(tpl["regions"]):
        x0, y0 = int(r["x"] * w), int(r["y"] * h)
        x1, y1 = int((r["x"] + r["w"]) * w) - 1, int((r["y"] + r["h"]) * h) - 1
        draw.rectangle([x0, y0, x1, y1], fill=PALETTE[i % len(PALETTE)], outline="#ffffff")
        draw.text((x0 + 4, y0 + 4), str(r["name"]), fill="#ffffff")
    cy = int(tpl["caption_y_pct"] / 100 * h)
    draw.rectangle([int(w * 0.1), cy - 6, int(w * 0.9), cy + 6], fill="#ffffff")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp.png")
    img.save(tmp, "PNG")
    tmp.replace(out)
    return out
