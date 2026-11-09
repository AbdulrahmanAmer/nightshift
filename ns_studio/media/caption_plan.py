"""Inputs for the smart caption features of spec 17.2, gathered from job files and channel settings.

`AssOptions` bundles everything `build_ass` takes beyond the words and the style (safe zones, regions to
avoid, key-word emphasis, speaker colours, profanity masking, an anchor). It round-trips through JSON so the
renderer can rebuild a caption file with the measured backdrop luma (`captions/<item>.json["ass_opts"]`).
Everything here is pure.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from ns_common.models import Channel, Format
from ns_studio.media.captions import DEFAULT_SAFE_ZONES, Box, Variant, build_ass, group_words
from ns_studio.media.formats import reference_canvas, scaled_margins, variant_for, written
from ns_studio.media.layout import caption_anchor_pct
from ns_studio.media.presets import CaptionStyle, caption_style
from ns_studio.media.transcribe import Word

# 16:9 has no platform UI over the picture; keep captions inside a 5 % title-safe margin.
LANDSCAPE_SAFE_ZONES = (54, 54)


def safe_zones_for(variant: Variant) -> tuple[int, int]:
    """(top, bottom) px the captions must stay out of: YouTube UI on 9:16, title-safe margin on 16:9."""
    return DEFAULT_SAFE_ZONES if variant == "vertical" else LANDSCAPE_SAFE_ZONES


# ---------------------------------------------------------------------------------------------------------
# Channel settings
# ---------------------------------------------------------------------------------------------------------


def mask_profanity_setting(channel: Channel) -> bool:
    """The channel's `captions.mask_profanity` option (default off)."""
    return channel.captions.mask_profanity


# ---------------------------------------------------------------------------------------------------------
# Emphasis
# ---------------------------------------------------------------------------------------------------------


def parse_emphasis(raw: Any) -> dict[int, str]:
    """{sentence index: key word} from a plan entry's `emphasis`.

    Accepted shapes: a mapping `{"2": "knife"}`, or a list of `{"sentence": 2, "word": "knife"}` (also
    `sentence_idx`) or `[2, "knife"]` pairs. Malformed entries are skipped; at most one word per sentence.
    """
    out: dict[int, str] = {}

    def put(idx: Any, word: Any) -> None:
        try:
            i = int(idx)
        except (TypeError, ValueError):
            return
        if isinstance(word, str) and word.strip() and i not in out:
            out[i] = word.strip()

    if isinstance(raw, Mapping):
        for k, v in raw.items():
            put(k, v)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, Mapping):
                put(item.get("sentence", item.get("sentence_idx")), item.get("word"))
            elif isinstance(item, list | tuple) and len(item) == 2:
                put(item[0], item[1])
    return out


def emphasis_from_beats(
    beats: Sequence[Mapping[str, Any]], *, chapter: int | None = None, part: int | None = None,
    shift: int = 0, last: int | None = None,
) -> dict[int, str]:
    """Merged `emphasis` of the visual-plan beats of one chapter (or part), re-based by `shift` sentences.

    `shift` is the first sentence of a derived Short's span (its sentence 0); entries outside
    [`shift`, `last`] are dropped.
    """
    out: dict[int, str] = {}
    for b in beats:
        if chapter is not None and b.get("chapter") != chapter:
            continue
        if part is not None and b.get("part") != part:
            continue
        for i, word in parse_emphasis(b.get("emphasis")).items():
            j = i - shift
            if j < 0 or (last is not None and i > last):
                continue
            out.setdefault(j, word)
    return out


def emphasis_from_list(words: Sequence[str] | None) -> dict[int, str]:
    """Clip plan `emphasis`: one key word per sentence in order (an empty string skips a sentence)."""
    return {i: w.strip() for i, w in enumerate(words or []) if isinstance(w, str) and w.strip()}


# ---------------------------------------------------------------------------------------------------------
# Speakers
# ---------------------------------------------------------------------------------------------------------


def speaker_plan(
    words: Sequence[Word], speaker_of: Sequence[int | None], style: CaptionStyle | str
) -> tuple[dict[int, int], list[int]]:
    """({group index: speaker}, word indices where a new speaker starts) for `build_ass`.

    Words without a speaker continue the previous one. Fewer than two distinct speakers give ({}, []): a
    single speaker keeps the style's own colours.
    """
    st = caption_style(style) if isinstance(style, str) else style
    if len(speaker_of) != len(words):
        raise ValueError("speaker_of must have one entry per word")
    filled: list[int | None] = []
    prev: int | None = None
    for s in speaker_of:
        prev = s if s is not None else prev
        filled.append(prev)
    first = next((s for s in filled if s is not None), None)
    filled = [first if s is None else s for s in filled]
    if len({s for s in filled if s is not None}) < 2:
        return {}, []
    breaks = [i for i in range(1, len(words)) if filled[i] != filled[i - 1]]
    mapping: dict[int, int] = {}
    pos = 0
    for gi, g in enumerate(group_words(words, st, breaks_before=breaks)):
        sp = filled[pos]
        if sp is not None:
            mapping[gi] = sp
        pos += len(g)
    return mapping, breaks


# ---------------------------------------------------------------------------------------------------------
# Bundle
# ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class AssOptions:
    """Everything `build_ass` takes besides words and style, for one caption file."""

    variant: Variant = "vertical"
    safe_zones: tuple[int, int] | None = None
    avoid_regions: tuple[Box, ...] = ()
    emphasis: dict[int, str] = field(default_factory=dict)
    speakers: dict[int, int] = field(default_factory=dict)
    speaker_breaks: tuple[int, ...] = ()
    mask_profanity: bool = False
    anchor_y_pct: float | None = None
    #: The frame size when it is not the variant's own, and the most words on screen: both come from a format.
    canvas: tuple[int, int] | None = None
    max_words: int | None = None

    @classmethod
    def for_format(cls, fmt: Format, **rest: Any) -> AssOptions:
        """Options a format dictates: geometry variant, safe margins, caption anchor, frame size, word cap.

        The anchor is the seam between the first two regions when the format puts captions on it, else the
        file's own `captions.anchor_y_pct` when it writes one, else None (the caption style's own place). The
        shipped Short gives exactly the options the renders used before formats were read. `rest` (emphasis,
        avoid regions, speakers, ...) is passed on and wins over what the format says.
        """
        variant = variant_for(fmt)
        size = (fmt.canvas.w, fmt.canvas.h)
        # a file that writes no safe_margins keeps the platform zones of its variant, as before formats were
        # read. Written or platform, the pixels are those of the reference frame (1080x1920, 1920x1080) and
        # scale with the frame, like the caption font: a duplicate at 720x1280 must not keep 1080x1920 pixels.
        top, bottom = (
            (fmt.safe_margins.top, fmt.safe_margins.bottom)
            if written(fmt, "safe_margins")
            else safe_zones_for(variant)
        )
        zones = scaled_margins(top, bottom, variant, size)
        if fmt.layout.seam_captions and len(fmt.layout.regions) > 1:
            anchor: float | None = caption_anchor_pct(fmt)
        else:
            anchor = fmt.captions.anchor_y_pct if written(fmt, "captions.anchor_y_pct") else None
        args: dict[str, Any] = {
            "variant": variant,
            "safe_zones": zones if any(zones) else None,
            "anchor_y_pct": anchor,
            "canvas": None if size == reference_canvas(variant) else size,
            "max_words": fmt.captions.words_on_screen if written(fmt, "captions.words_on_screen") else None,
        }
        args.update(rest)
        return cls(**args)

    def kwargs(self) -> dict[str, Any]:
        """Keyword arguments for `build_ass` (empty collections become None, the builder's "off")."""
        return {
            "variant": self.variant,
            "safe_zones": self.safe_zones,
            "avoid_regions": list(self.avoid_regions) or None,
            "emphasis": self.emphasis or None,
            "speakers": self.speakers or None,
            "speaker_breaks": self.speaker_breaks,
            "mask_profanity": self.mask_profanity,
            "anchor_y_pct": self.anchor_y_pct,
            "canvas": self.canvas,
            "max_words": self.max_words,
        }

    def build(
        self, words: Sequence[Word], style: CaptionStyle | str, *,
        backdrop_luma: Sequence[float] | None = None,
    ) -> str:
        """The ASS document for `words` with these options (and optional contrast samples)."""
        return build_ass(words, style, backdrop_luma=backdrop_luma, **self.kwargs())

    def to_json(self) -> dict[str, Any]:
        """Plain dict stored next to the caption timeline."""
        return {
            "variant": self.variant,
            "safe_zones": list(self.safe_zones) if self.safe_zones else None,
            "avoid_regions": [[b.x, b.y, b.w, b.h] for b in self.avoid_regions],
            "emphasis": {str(k): v for k, v in self.emphasis.items()},
            "speakers": {str(k): v for k, v in self.speakers.items()},
            "speaker_breaks": list(self.speaker_breaks),
            "mask_profanity": self.mask_profanity,
            "anchor_y_pct": self.anchor_y_pct,
            "canvas": list(self.canvas) if self.canvas else None,
            "max_words": self.max_words,
        }

    @classmethod
    def from_json(cls, d: Mapping[str, Any] | None) -> AssOptions:
        """Inverse of `to_json`; None or {} give the defaults."""
        if not d:
            return cls()
        zones = d.get("safe_zones")
        canvas = d.get("canvas")
        return cls(
            variant="landscape" if d.get("variant") == "landscape" else "vertical",
            safe_zones=(int(zones[0]), int(zones[1])) if zones else None,
            avoid_regions=tuple(Box(*(int(v) for v in b)) for b in d.get("avoid_regions", [])),
            emphasis={int(k): str(v) for k, v in (d.get("emphasis") or {}).items()},
            speakers={int(k): int(v) for k, v in (d.get("speakers") or {}).items()},
            speaker_breaks=tuple(int(i) for i in d.get("speaker_breaks", [])),
            mask_profanity=bool(d.get("mask_profanity", False)),
            anchor_y_pct=None if d.get("anchor_y_pct") is None else float(d["anchor_y_pct"]),
            canvas=(int(canvas[0]), int(canvas[1])) if canvas else None,
            max_words=int(d["max_words"]) if d.get("max_words") else None,
        )
