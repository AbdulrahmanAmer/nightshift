"""C8 Clipper: clip selection (spec 19.2 C4).

Pipeline: windows of at most 12 min with 2 min overlap -> LLM candidates per window -> merge overlapping
candidates -> snap edges to sentence boundaries from the word timings -> drop filler -> keep the best
`max_clips` with at least `spacing_s` between starts and no overlap. Raises NS-E407 when nothing is left.
Everything except the LLM call is pure, so a selection can be re-run from `transcript.json` with new
parameters; raw LLM answers are cached per window by the stage.
"""
from __future__ import annotations

import re
import string
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from ns_common.errors import NsError
from ns_common.models import ClipPlanItem, Transcript, Word
from ns_studio.clipper.models import ClipParams

FILLER_TOKENS = frozenset({"um", "uh", "uhm", "umm", "er", "erm", "like"})
MIN_WORDS_PER_S = 2.2
MAX_FILLER_RATIO = 0.4
SNAP_TOL_S = 5.0
PAD_BEFORE_S = 0.10
PAD_AFTER_S = 0.25
MERGE_OVERLAP_RATIO = 0.5
SENTENCE_PAUSE_S = 1.0
WINDOW_S = 720.0
OVERLAP_S = 120.0
_TERMINAL = re.compile(r"[.!?…][\"')\]]*$")


@dataclass
class Cand:
    """A candidate moment in source seconds."""

    start: float
    end: float
    title: str = "Clip"
    hook_line: str | None = None
    why: str = ""
    score: float = 0.5
    keywords: list[str] = field(default_factory=list)
    speakers: list[int] = field(default_factory=list)
    layout: str | None = None
    layout_reason: str | None = None
    caption_style: str | None = None  # chosen by the model among the channel's caption styles

    @property
    def duration(self) -> float:
        """Length in seconds."""
        return self.end - self.start


# ---------- windows and raw answers ----------
def make_windows(
    duration_s: float, window_s: float = WINDOW_S, overlap_s: float = OVERLAP_S
) -> list[tuple[float, float]]:
    """Cover [0, duration_s] with windows of at most `window_s` that overlap by `overlap_s`."""
    if duration_s <= window_s:
        return [(0.0, float(duration_s))]
    step = window_s - overlap_s
    out: list[tuple[float, float]] = []
    start = 0.0
    while True:
        end = min(start + window_s, duration_s)
        out.append((start, end))
        if end >= duration_s:
            return out
        start += step


def format_window(tr: Transcript, w_start: float, w_end: float) -> str:
    """Transcript lines `[start-end] (Sn) text` of the segments inside a window, for the prompt."""
    lines: list[str] = []
    for s in tr.segments:
        if s.end < w_start or s.start > w_end:
            continue
        who = f" (S{s.speaker})" if s.speaker is not None else ""
        lines.append(f"[{s.start:.1f}-{s.end:.1f}]{who} {s.text.strip()}")
    return "\n".join(lines)


def window_segments(tr: Transcript, w_start: float, w_end: float) -> list[dict[str, Any]]:
    """Segments inside a window as `{i, start, end, text}` dicts, the shape `clip_select.md` iterates."""
    return [
        {"i": s.i, "start": round(s.start, 1), "end": round(s.end, 1), "text": s.text.strip()}
        for s in tr.segments
        if s.end >= w_start and s.start <= w_end
    ]


def extract_items(raw: Any) -> list[dict[str, Any]]:
    """Tolerantly pull the candidate list out of an LLM answer (a list or an object holding one)."""
    items: list[Any] = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        for key in ("clips", "candidates", "moments", "clip_plan", "items"):
            val = raw.get(key)
            if isinstance(val, list):
                items = val
                break
    return [i for i in items if isinstance(i, dict)]


def _num(d: dict[str, Any], *keys: str) -> float | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, int | float) and not isinstance(v, bool):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                continue
    return None


def _opt_str(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def to_cand(d: dict[str, Any], duration_s: float) -> Cand | None:
    """Convert one raw LLM item to a `Cand` clamped to the video; None when it has no usable span."""
    start = _num(d, "start_s", "start")
    end = _num(d, "end_s", "end")
    if start is None or end is None:
        return None
    start = max(0.0, start)
    end = min(duration_s, end)
    if end <= start:
        return None
    score = _num(d, "score")
    kws = d.get("keywords")
    spk = d.get("speakers")
    hook = d.get("hook_line")
    return Cand(
        start=start,
        end=end,
        title=str(d.get("title") or "Clip").strip()[:100] or "Clip",
        hook_line=str(hook).strip() if isinstance(hook, str) and hook.strip() else None,
        why=str(d.get("why") or "").strip(),
        score=min(1.0, max(0.0, 0.5 if score is None else score)),
        keywords=[str(k) for k in kws] if isinstance(kws, list) else [],
        speakers=[int(s) for s in spk if isinstance(s, int)] if isinstance(spk, list) else [],
        layout=_opt_str(d.get("layout")),
        layout_reason=_opt_str(d.get("layout_reason") or d.get("reason")),
        caption_style=_opt_str(d.get("caption_style")),
    )


def collect_candidates(
    tr: Transcript,
    ask: Callable[[float, float], Any],
    cache: dict[str, Any] | None = None,
    *,
    cache_tag: str = "",
) -> list[Cand]:
    """Ask the LLM once per window (answers cached under `cache`) and return every raw candidate."""
    cache = cache if cache is not None else {}
    out: list[Cand] = []
    for ws, we in make_windows(tr.duration_s):
        key = f"{ws:.1f}-{we:.1f}-{cache_tag}"
        if key not in cache:
            cache[key] = ask(ws, we)
        for d in extract_items(cache[key]):
            c = to_cand(d, tr.duration_s)
            if c is not None:
                out.append(c)
    return out


# ---------- merge ----------
def _overlap(a: Cand, b: Cand) -> float:
    return max(0.0, min(a.end, b.end) - max(a.start, b.start))


def _merge_pair(a: Cand, b: Cand, max_s: float) -> Cand:
    best, other = (a, b) if a.score >= b.score else (b, a)
    start, end = min(a.start, b.start), max(a.end, b.end)
    if end - start > max_s:
        start, end = best.start, best.end
    return Cand(
        start=start,
        end=end,
        title=best.title,
        hook_line=best.hook_line or other.hook_line,
        why=best.why or other.why,
        score=best.score,
        keywords=list(dict.fromkeys([*best.keywords, *other.keywords])),
        speakers=sorted({*best.speakers, *other.speakers}),
        layout=best.layout or other.layout,
        layout_reason=best.layout_reason or other.layout_reason,
        caption_style=best.caption_style or other.caption_style,
    )


def merge_overlaps(cands: list[Cand], max_s: float) -> list[Cand]:
    """Merge candidates that overlap by more than half of the shorter one (duplicates from window overlap)."""
    merged = sorted(cands, key=lambda c: (c.start, c.end))
    changed = True
    while changed:
        changed = False
        out: list[Cand] = []
        for c in merged:
            for i, m in enumerate(out):
                shorter = min(m.duration, c.duration)
                if shorter > 0 and _overlap(m, c) / shorter > MERGE_OVERLAP_RATIO:
                    out[i] = _merge_pair(m, c, max_s)
                    changed = True
                    break
            else:
                out.append(c)
        merged = sorted(out, key=lambda c: (c.start, c.end))
    return merged


# ---------- sentences ----------
def sentence_spans(words: list[Word]) -> list[tuple[float, float]]:
    """(start, end) seconds of each sentence: ends at terminal punctuation or a pause of 1 s or more."""
    spans: list[tuple[float, float]] = []
    first = 0
    for i, w in enumerate(words):
        last = i == len(words) - 1
        gap = (words[i + 1].start - w.end) if not last else 0.0
        if last or _TERMINAL.search(w.w.strip()) or gap >= SENTENCE_PAUSE_S:
            spans.append((words[first].start, w.end))
            first = i + 1
    return spans


def _nearest(values: list[float], target: float, tol: float) -> float | None:
    best = min(values, key=lambda v: abs(v - target), default=None)
    return best if best is not None and abs(best - target) <= tol else None


def snap_to_sentences(
    c: Cand, words: list[Word], *, min_s: float, max_s: float, duration_s: float
) -> Cand | None:
    """Move the edges onto sentence boundaries and enforce [min_s, max_s]; None when impossible."""
    spans = sentence_spans(words)
    if not spans:
        return c if min_s <= c.duration <= max_s else None
    starts = [s for s, _ in spans]
    ends = [e for _, e in spans]
    start = _nearest(starts, c.start, SNAP_TOL_S)
    start = c.start if start is None else start
    end = _nearest(ends, c.end, SNAP_TOL_S)
    end = c.end if end is None else end
    if end - start > max_s:
        ok = [e for e in ends if start + min_s <= e <= start + max_s]
        if not ok:
            return None
        end = max(ok)
    elif end - start < min_s:
        ok = [e for e in ends if start + min_s <= e <= start + max_s]
        if not ok:
            return None
        end = min(ok)
    pad_a, pad_b = PAD_BEFORE_S, PAD_AFTER_S
    if end + pad_b - (start - pad_a) > max_s:
        pad_a = pad_b = 0.0
    return replace(
        c,
        start=max(0.0, round(start - pad_a, 3)),
        end=min(duration_s, round(end + pad_b, 3)),
    )


# ---------- filler ----------
def _norm(token: str) -> str:
    return token.lower().strip(string.punctuation + " ")


def words_in(words: list[Word], start: float, end: float) -> list[Word]:
    """Words whose midpoint lies inside [start, end]."""
    return [w for w in words if start <= (w.start + w.end) / 2.0 <= end]


def passes_filler_filter(c: Cand, words: list[Word]) -> bool:
    """False when the span is sparse (< 2.2 words/s) or more than 40 % of its tokens are filler."""
    inside = words_in(words, c.start, c.end)
    if not inside or c.duration <= 0:
        return False
    if len(inside) / c.duration < MIN_WORDS_PER_S:
        return False
    fillers = sum(1 for w in inside if _norm(w.w) in FILLER_TOKENS)
    return fillers / len(inside) <= MAX_FILLER_RATIO


# ---------- ranking ----------
def pick_top(cands: list[Cand], *, max_clips: int, spacing_s: float) -> list[Cand]:
    """Best-scoring first; skip candidates that overlap or start within `spacing_s` of a kept one."""
    kept: list[Cand] = []
    for c in sorted(cands, key=lambda x: (-x.score, x.start)):
        if len(kept) >= max_clips:
            break
        clash = any(
            abs(c.start - k.start) < spacing_s or (c.start < k.end and k.start < c.end) for k in kept
        )
        if not clash:
            kept.append(c)
    return sorted(kept, key=lambda x: x.start)


def _speakers(tr: Transcript, start: float, end: float) -> list[int]:
    found: set[int] = set()
    for s in tr.segments:
        if s.speaker is not None and s.end >= start and s.start <= end:
            found.add(s.speaker)
    return sorted(found)


@dataclass
class Dropped:
    """A candidate that did not make the plan and why."""

    cand: Cand
    reasons: list[str]


Judge = Callable[[Cand], tuple[bool, list[str]]]


def prepare_candidates(
    tr: Transcript,
    params: ClipParams,
    ask: Callable[[float, float], Any],
    cache: dict[str, Any] | None = None,
) -> tuple[list[Cand], list[Dropped]]:
    """Ask per window, merge, snap and filter. Returns survivors in score order plus the filtered-out ones."""
    tag = f"{params.min_s:g}-{params.max_s:g}"
    raw = collect_candidates(tr, ask, cache, cache_tag=tag)
    merged = merge_overlaps(raw, params.max_s)
    ranked: list[Cand] = []
    dropped: list[Dropped] = []
    for c in merged:
        s = snap_to_sentences(c, tr.words, min_s=params.min_s, max_s=params.max_s, duration_s=tr.duration_s)
        if s is None:
            dropped.append(Dropped(c, ["no sentence boundary fits the length range"]))
        elif not passes_filler_filter(s, tr.words):
            dropped.append(Dropped(s, ["mostly filler or fewer than 2.2 words per second"]))
        else:
            ranked.append(s)
    ranked.sort(key=lambda x: (-x.score, x.start))
    return ranked, dropped


def judge_loop(
    ranked: list[Cand], params: ClipParams, judge: Judge | None = None
) -> tuple[list[Cand], list[Dropped]]:
    """Walk candidates in score order; skip clashes, ask `judge` (ok, reasons) and stop at `max_clips`."""
    kept: list[Cand] = []
    dropped: list[Dropped] = []
    for c in ranked:
        if len(kept) >= params.max_clips:
            break
        clash = any(
            abs(c.start - k.start) < params.spacing_s or (c.start < k.end and k.start < c.end) for k in kept
        )
        if clash:
            dropped.append(Dropped(c, [f"overlaps or starts within {params.spacing_s:g} s of a better clip"]))
            continue
        if judge is not None:
            ok, reasons = judge(c)
            if not ok:
                dropped.append(Dropped(c, reasons or ["rejected by the judge"]))
                continue
        kept.append(c)
    return kept, dropped


def _item(tr: Transcript, c: Cand, k: int, status: str, reasons: list[str]) -> ClipPlanItem:
    return ClipPlanItem(
        k=k,
        start_s=round(c.start, 3),
        end_s=round(c.end, 3),
        title=c.title,
        hook_line=c.hook_line,
        why=c.why,
        score=c.score,
        speakers=c.speakers or _speakers(tr, c.start, c.end) or None,
        keywords=c.keywords,
        layout=c.layout,
        layout_reason=c.layout_reason,
        caption_style=c.caption_style,
        status="kept" if status == "kept" else "dropped",
        reasons=reasons,
    )


def select_plan(
    tr: Transcript,
    params: ClipParams,
    ask: Callable[[float, float], Any],
    cache: dict[str, Any] | None = None,
    judge: Judge | None = None,
) -> list[ClipPlanItem]:
    """Full `clip_plan.json` content: kept clips (k from 1, score order), then dropped ones with reasons."""
    ranked, filtered = prepare_candidates(tr, params, ask, cache)
    kept, judged = judge_loop(ranked, params, judge)
    items = [_item(tr, c, i, "kept", []) for i, c in enumerate(kept, start=1)]
    extra = [*judged, *filtered]
    items += [_item(tr, d.cand, len(kept) + j, "dropped", d.reasons) for j, d in enumerate(extra, start=1)]
    return items


def ensure_some_kept(items: list[ClipPlanItem]) -> list[ClipPlanItem]:
    """The kept clips of a plan; NS-E407 (with the drop reasons) when there are none."""
    kept = [i for i in items if i.status == "kept"]
    if not kept:
        why = "; ".join(f"{i.start_s:.0f}-{i.end_s:.0f}s: {', '.join(i.reasons)}" for i in items[:5])
        raise NsError("NS-E407", why or "the model returned no candidates")
    return kept


def select_clips(
    tr: Transcript,
    params: ClipParams,
    ask: Callable[[float, float], Any],
    cache: dict[str, Any] | None = None,
    judge: Judge | None = None,
) -> list[ClipPlanItem]:
    """Kept clips only (NS-E407 when none); see `select_plan` for the full plan with drop reasons."""
    return ensure_some_kept(select_plan(tr, params, ask, cache, judge))
