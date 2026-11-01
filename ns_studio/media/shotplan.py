"""Shot planning: beats -> time spans -> shot slots, and the `Shot` record shared by clips and render (C6)."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from ns_studio.media.transcribe import Word


@dataclass(frozen=True)
class ShotSlot:
    """A time slot [start, end) of the final timeline assigned to beat index `beat`."""

    beat: int
    start: float
    end: float

    @property
    def length(self) -> float:
        """Slot length in seconds."""
        return self.end - self.start


@dataclass(frozen=True)
class Shot:
    """A slot with its chosen clip: `ss` is the in-point into the clip file at `path`."""

    beat: int
    start: float
    end: float
    source: str
    clip_id: str
    path: str
    ss: float
    width: int
    height: int
    url: str
    license: str
    keyword: str

    @property
    def length(self) -> float:
        """Shot length in seconds."""
        return self.end - self.start

    def to_json(self) -> dict[str, Any]:
        """Plain dict for `clips/shots.json`."""
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Shot:
        """Inverse of `to_json`."""
        return cls(**d)


def sentence_starts(words: Sequence[Word]) -> dict[int, float]:
    """Start time of the first word of every sentence index >= 0."""
    out: dict[int, float] = {}
    for w in words:
        if w.sentence_idx >= 0 and w.sentence_idx not in out:
            out[w.sentence_idx] = w.start
    return out


def beat_spans(
    beats: Sequence[dict[str, Any]], words: Sequence[Word], total_s: float
) -> list[tuple[int, float, float]]:
    """(beat position, start, end) per beat, contiguous over [0, total_s].

    A beat starts when its first sentence is first spoken (the first beat starts at 0.0 so a voiced hook is
    covered) and ends where the next beat starts (the last at `total_s`).
    """
    order = sorted(range(len(beats)), key=lambda i: int(beats[i]["sentence_start"]))
    starts = sentence_starts(words)
    known = sorted(starts)

    def start_of(sentence: int) -> float:
        if sentence in starts:
            return starts[sentence]
        prior = [s for s in known if s <= sentence]
        return starts[prior[-1]] if prior else 0.0

    bounds = [0.0 if pos == 0 else start_of(int(beats[i]["sentence_start"])) for pos, i in enumerate(order)]
    out: list[tuple[int, float, float]] = []
    for pos, i in enumerate(order):
        t0 = bounds[pos]
        t1 = bounds[pos + 1] if pos + 1 < len(order) else total_s
        out.append((i, t0, max(t1, t0)))
    return out


def split_span(length: float, lo: float, hi: float) -> list[float]:
    """Equal shot lengths for a span, each within [lo, hi] when the span allows it."""
    if length <= 0:
        return []
    n_min = max(1, math.ceil(length / hi - 1e-9))
    n_max = math.floor(length / lo + 1e-9)
    if n_max >= n_min:
        n = min(n_max, max(n_min, round(length / ((lo + hi) / 2))))
    else:
        n = n_min
    return [length / n] * n


def plan_slots(spans: Sequence[tuple[int, float, float]], cut_range: tuple[float, float]) -> list[ShotSlot]:
    """Cut every beat span into slots of `cut_range` seconds (spec §7.4: Shorts 1.5-3 s, masters 5-9 s)."""
    lo, hi = cut_range
    slots: list[ShotSlot] = []
    for beat, t0, t1 in spans:
        cursor = t0
        lengths = split_span(t1 - t0, lo, hi)
        for k, ln in enumerate(lengths):
            end = t1 if k == len(lengths) - 1 else cursor + ln
            slots.append(ShotSlot(beat, cursor, end))
            cursor = end
    return slots


def frame_edges(ends: Sequence[float], fps: int) -> list[int]:
    """Cumulative frame boundaries (len = shots + 1) from shot end times; every shot gets >= 1 frame."""
    edges = [0]
    for end in ends:
        edges.append(max(edges[-1] + 1, round(end * fps)))
    return edges
