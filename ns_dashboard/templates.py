"""Clip layouts for the Clipper page, read from the `clip` format presets (spec 17.1).

Ids, labels, regions and caption anchors come from `ns_studio.media.formats.load_formats`, so a layout the
owner edited or added in Library > Formats (`<home>\\config\\formats`) shows up here with no change in code.
Only the plain-language description and "when" lines of the six shipped layouts are kept below: a preset
carries no prose of its own.
"""

from __future__ import annotations

from typing import Any

from ns_common.models import Format
from ns_common.paths import Paths
from ns_studio.errors import StudioError
from ns_studio.media.formats import FORMATS_DIR, forget_loaded_formats, load_formats

# A layout id is its preset id without the `clip_` prefix, the name the Clipper and the saved jobs use.
PREFIX = "clip_"

AUTO: dict[str, Any] = {
    "id": "auto",
    "label": "Let Nightshift choose",
    "description": "Looks at the video and picks the layout that fits.",
    "when": "Default",
    "regions": [],
    "caption_y_pct": 70,
    "title_card": False,
}

# Shipped layouts in the order the page shows them, with their owner-facing wording.
SHIPPED: dict[str, tuple[str, str]] = {
    "gaming_facecam": ("Your face cam on top, the game filling the rest.", "A face cam is found in a corner"),
    "podcast_stacked": (
        "Both speakers one above the other; the talker gets a thin border.", "Two faces on screen",
    ),
    "podcast_single": (
        "A single tall crop that follows whoever is on screen.", "Two faces, but you want one",
    ),
    "talking_head": ("A tall crop that follows one speaker.", "One face on screen"),
    "center_crop": (
        "The picture in the middle over a soft blurred background.", "Slides, screen recordings, no faces",
    ),
    "screen_plus_cam": ("Face cam on top, the whole screen below it.", "Tutorials with a small cam"),
}


def _label(fmt: Format) -> str:
    name = fmt.name.strip()
    name = name[len("Clip:"):].strip() if name.lower().startswith("clip:") else name
    return (name[:1].upper() + name[1:]) or fmt.id.removeprefix(PREFIX).replace("_", " ").capitalize()


def _caption_y_pct(fmt: Format) -> float:
    """Where captions sit; on the seam below the first panel when the preset puts them there."""
    first = fmt.layout.regions[0]
    if fmt.layout.seam_captions and len(fmt.layout.regions) > 1:
        return round((first.y + first.h) / fmt.canvas.h * 100, 1)
    return round(fmt.captions.anchor_y_pct, 1)


def _entry(fmt: Format) -> dict[str, Any]:
    layout_id = fmt.id.removeprefix(PREFIX)
    regions = [
        {
            "name": r.name,
            "x": round(r.x / fmt.canvas.w, 4),
            "y": round(r.y / fmt.canvas.h, 4),
            "w": round(r.w / fmt.canvas.w, 4),
            "h": round(r.h / fmt.canvas.h, 4),
        }
        for r in fmt.layout.regions
    ]
    description, when = SHIPPED.get(
        layout_id, ("Your own layout: " + ", ".join(r.name for r in fmt.layout.regions) + ".", "Your choice")
    )
    return {
        "id": layout_id,
        "label": _label(fmt),
        "description": description,
        "when": when,
        "regions": regions,
        "caption_y_pct": _caption_y_pct(fmt),
        "title_card": False,
    }


def load_templates(paths: Paths | None = None) -> list[dict[str, Any]]:
    """`auto` followed by every `clip` format: the shipped layouts first, then the owner's own by id.

    An owner file with a shipped id replaces that preset, so the edit reaches the list at once. A format file
    that fails to load (hand-edited yaml) must not empty the Create page: the shipped presets are listed then.
    """
    forget_loaded_formats()  # the dashboard outlives edits made in Library > Formats
    try:
        formats = load_formats(paths=paths)
    except StudioError:
        formats = load_formats(FORMATS_DIR)
    clips = {f.id.removeprefix(PREFIX): f for f in formats.values() if f.kind == "clip"}
    order = [i for i in SHIPPED if i in clips] + sorted(i for i in clips if i not in SHIPPED)
    return [dict(AUTO), *(_entry(clips[i]) for i in order)]
