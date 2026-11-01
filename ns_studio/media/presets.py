"""Preset loaders for caption styles and visual treatments (component C6)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml

from ns_common.models import Strict

PRESETS_DIR = Path(__file__).resolve().parents[1] / "presets"
FONTS_DIR = PRESETS_DIR / "fonts"


class CaptionVariant(Strict):
    """Geometry of a caption style for one video orientation."""

    font_size: int
    margin_v_pct: float
    margin_lr: int
    outline: float
    shadow: float


class CaptionStyle(Strict):
    """One entry of caption_styles.yaml (spec §17)."""

    name: str = ""
    font: str
    font_fallback: str
    bold: bool = False
    uppercase: bool = False
    animation: Literal["pop", "none", "karaoke"] = "none"
    words_on_screen: tuple[int, int] = (2, 3)
    primary: str
    active: str
    outline_color: str = "#000000"
    shadow_color: str = "#000000"
    box: bool = False
    accent: str | None = None
    palette: list[str] = []
    vertical: CaptionVariant
    landscape: CaptionVariant


class ZoomSpec(Strict):
    """Per-shot camera motion."""

    mode: Literal["in", "out", "static", "handheld"] = "static"
    rate: float = 0.0007
    max: float = 1.12
    base: float = 1.06
    amount: float = 12.0


class VisualTreatment(Strict):
    """One entry of visual_treatments.yaml (spec §17)."""

    name: str = ""
    zoom: ZoomSpec
    cut_range_s: tuple[float, float]
    long_cut_range_s: tuple[float, float] = (6.0, 9.0)
    eq: dict[str, float] = {}
    curves: str | None = None
    colorbalance: dict[str, float] | None = None
    vignette: float | None = None
    grain: int = 0


@lru_cache(maxsize=1)
def load_caption_styles() -> dict[str, CaptionStyle]:
    """All caption styles keyed by name."""
    raw = yaml.safe_load((PRESETS_DIR / "caption_styles.yaml").read_text(encoding="utf-8"))
    return {k: CaptionStyle.model_validate(v | {"name": k}) for k, v in raw.items()}


@lru_cache(maxsize=1)
def load_visual_treatments() -> dict[str, VisualTreatment]:
    """All visual treatments keyed by name."""
    raw = yaml.safe_load((PRESETS_DIR / "visual_treatments.yaml").read_text(encoding="utf-8"))
    return {k: VisualTreatment.model_validate(v | {"name": k}) for k, v in raw.items()}


def caption_style(name: str) -> CaptionStyle:
    """Look up a caption style; ValueError for an unknown name (a configuration bug, not a runtime fault)."""
    styles = load_caption_styles()
    if name not in styles:
        raise ValueError(f"unknown caption style {name!r}; known: {sorted(styles)}")
    return styles[name]


def visual_treatment(name: str) -> VisualTreatment:
    """Look up a visual treatment; ValueError for an unknown name."""
    items = load_visual_treatments()
    if name not in items:
        raise ValueError(f"unknown visual treatment {name!r}; known: {sorted(items)}")
    return items[name]


class ChapterCard(Strict):
    """One entry of chapter_cards.yaml (spec §17)."""

    name: str = ""
    background: str
    text: str
    accent: str
    font_size: int
    number_size: int = 32
    show_number: bool = True
    fade_ms: int = 250


@lru_cache(maxsize=1)
def load_chapter_cards() -> dict[str, ChapterCard]:
    """All chapter card styles keyed by name."""
    raw = yaml.safe_load((PRESETS_DIR / "chapter_cards.yaml").read_text(encoding="utf-8"))
    return {k: ChapterCard.model_validate(v | {"name": k}) for k, v in raw.items()}


def chapter_card(name: str) -> ChapterCard:
    """Look up a chapter card style; ValueError for an unknown name."""
    cards = load_chapter_cards()
    if name not in cards:
        raise ValueError(f"unknown chapter card {name!r}; known: {sorted(cards)}")
    return cards[name]


class ThumbnailLayout(Strict):
    """One entry of thumbnails.yaml (spec §17)."""

    name: str = ""
    position: Literal["bottom_left", "center", "top_left"]
    size_pct: float
    color: str
    stroke: str
    stroke_pct: float
    shade: float = 0.4


@lru_cache(maxsize=1)
def load_thumbnail_layouts() -> dict[str, ThumbnailLayout]:
    """All thumbnail layouts keyed by name (file order)."""
    raw = yaml.safe_load((PRESETS_DIR / "thumbnails.yaml").read_text(encoding="utf-8"))
    return {k: ThumbnailLayout.model_validate(v | {"name": k}) for k, v in raw.items()}
