# ruff: noqa: E501
"""Package P10b: the safe zones scale with the frame of the chosen format, and the reference frame is written once.

Safe margins (a format's `safe_margins`, and the platform zones 220/320 and 54/54 a file without margins falls
back to) are pixels at the reference frame of the format's variant: 1080x1920 for a vertical frame, 1920x1080
for a landscape one. A duplicate at another frame size must not keep pixels meant for 1080x1920: they are scaled
by the same factor as the caption font, side margin and outline (frame width over reference width).
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from ns_common.models import Format
from ns_studio.media import formats as F
from ns_studio.media import qc as Q
from ns_studio.media import render as R
from ns_studio.media.caption_plan import AssOptions
from ns_studio.media.captions import canvas_geometry
from ns_studio.media.presets import caption_style
from ns_studio.media.transcribe import Word


def shipped(fid: str) -> dict[str, Any]:
    return yaml.safe_load((F.FORMATS_DIR / f"{fid}.yaml").read_text(encoding="utf-8"))


def variant(base: str, w: int, h: int, **changes: Any) -> Format:
    """A copy of a shipped preset at another frame size (one full-frame region)."""
    doc = copy.deepcopy(shipped(base))
    doc["canvas"] = {"w": w, "h": h}
    doc["layout"] = {
        "seam_captions": False,
        "regions": [{"name": "main", "x": 0, "y": 0, "w": w, "h": h, "source": "full"}],
    }
    doc.update(changes)
    return Format.model_validate(doc)


def words() -> list[Word]:
    return [Word(f"w{i}", i * 0.3, i * 0.3 + 0.25, 0) for i in range(8)]


def test_the_shipped_presets_keep_their_margins_exactly() -> None:
    assert AssOptions.for_format(F.get_format("short_9x16")).safe_zones == (220, 320)
    assert AssOptions.for_format(F.get_format("long_16x9")).safe_zones == (54, 54)
    assert AssOptions.for_format(F.get_format("square_1x1")).safe_zones == (54, 54)


@pytest.mark.parametrize(
    ("base", "w", "h", "expected"),
    [
        ("short_9x16", 720, 1280, (147, 213)),  # 220 and 320 px at 1080 wide, as pixels of a 720-wide frame
        ("short_9x16", 540, 960, (110, 160)),
        ("long_16x9", 1280, 720, (36, 36)),
        ("long_16x9", 3840, 2160, (108, 108)),
    ],
)
def test_written_safe_margins_scale_with_the_frame_size(
    base: str, w: int, h: int, expected: tuple[int, int]
) -> None:
    fmt = variant(base, w, h)
    assert F.written(fmt, "safe_margins")
    assert AssOptions.for_format(fmt).safe_zones == expected


def test_platform_zones_scale_too_when_the_file_writes_no_margins() -> None:
    doc = shipped("short_9x16")
    del doc["safe_margins"]
    doc["canvas"] = {"w": 720, "h": 1280}
    doc["layout"] = {
        "seam_captions": False,
        "regions": [{"name": "main", "x": 0, "y": 0, "w": 720, "h": 1280, "source": "full"}],
    }
    fmt = Format.model_validate(doc)
    assert not F.written(fmt, "safe_margins")
    assert AssOptions.for_format(fmt).safe_zones == (147, 213)
    land = shipped("long_16x9")
    del land["safe_margins"]
    land["canvas"] = {"w": 1280, "h": 720}
    land["layout"] = {
        "seam_captions": False,
        "regions": [{"name": "main", "x": 0, "y": 0, "w": 1280, "h": 720, "source": "full"}],
    }
    assert AssOptions.for_format(Format.model_validate(land)).safe_zones == (36, 36)


def test_a_zero_margin_still_means_no_zone_at_any_frame_size() -> None:
    fmt = variant("short_9x16", 720, 1280, safe_margins={"top": 0, "bottom": 0})
    assert AssOptions.for_format(fmt).safe_zones is None


def test_the_caption_block_keeps_out_of_the_scaled_bottom_zone() -> None:
    """A 720x1280 duplicate with captions asked for at 95 % of the height: the block sits above 213 px, not 320."""
    fmt = variant("short_9x16", 720, 1280, captions={"anchor_y_pct": 95, "styles_allowed": ["clean_white"]})
    opts = AssOptions.for_format(fmt)
    ass = opts.build(words(), "clean_white")
    margin_v = int(next(ln for ln in ass.splitlines() if ln.startswith("Style: Default,")).split(",")[-2])
    assert 213 <= margin_v < 320, margin_v
    unscaled = AssOptions(variant="vertical", safe_zones=(220, 320), anchor_y_pct=95, canvas=(720, 1280))
    old = int(
        next(
            ln
            for ln in unscaled.build(words(), "clean_white").splitlines()
            if ln.startswith("Style: Default,")
        ).split(",")[-2]
    )
    assert old >= 320 > margin_v  # what a duplicate used to get: the pixels of the 1080x1920 frame


def test_the_scaled_zones_are_what_the_job_stores_and_reads_back() -> None:
    fmt = variant("short_9x16", 720, 1280)
    opts = AssOptions.for_format(fmt)
    again = AssOptions.from_json(opts.to_json())
    assert again.safe_zones == (147, 213) and again == opts  # stored once, scaled once


def test_the_safe_margin_rule_compares_the_scaled_margins_with_the_frame() -> None:
    names = set(F.load_caption_styles()) if hasattr(F, "load_caption_styles") else {"clean_white"}
    big = variant(
        "short_9x16", 720, 1280, safe_margins={"top": 600, "bottom": 700}
    )  # 1300 px at 1080x1920 is not 1300 px here
    assert not [p for p in F.format_problems(big, names) if "safe margins" in p]
    full = variant("square_1x1", 1080, 1080, safe_margins={"top": 600, "bottom": 600})
    assert any("safe margins leave no room" in p for p in F.format_problems(full, names))
    tall = variant("short_9x16", 720, 1280, safe_margins={"top": 1000, "bottom": 1000})
    assert any("safe margins leave no room" in p for p in F.format_problems(tall, names))


# ---------- the reference frame is written once ----------
def test_one_reference_frame_serves_captions_renders_and_qc(monkeypatch: pytest.MonkeyPatch) -> None:
    assert F.reference_canvas("vertical") == R.VERTICAL == (1080, 1920)
    assert F.reference_canvas("landscape") == R.LANDSCAPE == (1920, 1080)
    geo = caption_style("clean_white").vertical
    # the caption geometry reads the same table: change it and the scaling follows
    monkeypatch.setitem(F.REFERENCE_CANVAS, "vertical", (540, 960))
    scaled, w, h = canvas_geometry(geo, "vertical", (1080, 1920))
    assert (w, h) == (1080, 1920) and scaled.font_size == round(geo.font_size * 2)


def test_no_module_writes_the_reference_frame_a_second_time() -> None:
    root = Path(F.__file__).resolve().parent
    pattern = re.compile(r"\(\s*1080\s*,\s*1920\s*\)|\(\s*1920\s*,\s*1080\s*\)")
    found = {
        p.name: [m.group(0) for m in pattern.finditer(p.read_text(encoding="utf-8"))]
        for p in sorted(root.glob("*.py"))
    }
    offenders = {name: hits for name, hits in found.items() if hits and name != "formats.py"}
    assert offenders == {}, offenders
    assert Q.aspect_check is not None
