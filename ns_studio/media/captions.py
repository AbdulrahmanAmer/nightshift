"""Caption alignment to the script, ASS subtitle builder and the captions stage (component C6).

Spec §7.6: ASR tokens are aligned to the script's normalized tokens with difflib; matched words take ASR
times, unmatched runs are interpolated; rendered words are always the script's.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Literal

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.paths import contracts_dir
from ns_common.textstats import normalize_tokens, split_sentences
from ns_studio.context import JobContext
from ns_studio.media.presets import FONTS_DIR, CaptionStyle, CaptionVariant, caption_style
from ns_studio.media.transcribe import Word
from ns_studio.stages import Tenant

DRIFT_LIMIT_MS = 120.0
MIN_MATCH_RATIO = 0.6
HOOK_GAP_S = 0.15
Variant = Literal["vertical", "landscape"]
_SENTENCE_END = re.compile(r"[.!?…][\"'”’)\]]*$")


@dataclass(frozen=True)
class AlignResult:
    """Outcome of aligning ASR words to the script."""

    words: list[Word]
    matched: int
    total: int
    drift_ms: float

    @property
    def match_ratio(self) -> float:
        """Fraction of script words that matched an ASR word."""
        return self.matched / self.total if self.total else 1.0


def script_words(text: str) -> list[tuple[str, int]]:
    """Split a script into display words with sentence indices; punctuation-only tokens join a neighbour."""
    out: list[tuple[str, int]] = []
    pending = ""
    for si, sentence in enumerate(split_sentences(text)):
        for tok in sentence.split():
            if not normalize_tokens(tok):
                if out:
                    out[-1] = (f"{out[-1][0]} {tok}", out[-1][1])
                else:
                    pending = f"{pending} {tok}".strip()
                continue
            if pending:
                tok, pending = f"{pending} {tok}", ""
            out.append((tok, si))
    return out


def _word_times_from_tokens(
    script: list[tuple[str, int]], asr: Sequence[Word]
) -> list[tuple[float, float] | None]:
    s_tokens: list[str] = []
    s_owner: list[int] = []
    for i, (w, _si) in enumerate(script):
        for t in normalize_tokens(w):
            s_tokens.append(t)
            s_owner.append(i)
    a_tokens: list[str] = []
    a_owner: list[int] = []
    for k, aw in enumerate(asr):
        for t in normalize_tokens(aw.w):
            a_tokens.append(t)
            a_owner.append(k)
    times: list[tuple[float, float] | None] = [None] * len(script)
    sm = SequenceMatcher(None, s_tokens, a_tokens, autojunk=False)
    for block in sm.get_matching_blocks():
        for off in range(block.size):
            wi = s_owner[block.a + off]
            aw = asr[a_owner[block.b + off]]
            cur = times[wi]
            times[wi] = (aw.start, aw.end) if cur is None else (min(cur[0], aw.start), max(cur[1], aw.end))
    return times


def _interpolate(
    script: list[tuple[str, int]], times: list[tuple[float, float] | None], total_s: float
) -> list[tuple[float, float]]:
    n = len(script)
    out: list[tuple[float, float] | None] = list(times)
    i = 0
    while i < n:
        if out[i] is not None:
            i += 1
            continue
        j = i
        while j < n and out[j] is None:
            j += 1
        left = out[i - 1] if i > 0 else None
        right = out[j] if j < n else None
        a = left[1] if left else 0.0
        b = right[0] if right else total_s
        if b <= a:
            b = a + 0.12 * (j - i)
        weights = [max(2, len("".join(normalize_tokens(script[k][0])))) for k in range(i, j)]
        cur = a
        for k, wgt in zip(range(i, j), weights, strict=True):
            share = (b - a) * wgt / sum(weights)
            out[k] = (cur, cur + share)
            cur += share
        i = j
    result: list[tuple[float, float]] = []
    prev_start = 0.0
    for t in out:
        assert t is not None
        s = max(t[0], prev_start)
        result.append((s, max(t[1], s)))
        prev_start = s
    return result


def drift_ms(caption_starts: Sequence[float], asr_starts: Sequence[float]) -> float:
    """Median |caption word start - ASR word start| over the words that matched an ASR word, in ms.

    The two sequences are paired by position (the i-th matched script word). Natural speech rhythm (short
    words, pauses) does not count: only the offset between where a caption word is shown and where the
    recogniser heard that word. No matched word at all gives infinity (no evidence of any alignment).
    """
    if not caption_starts:
        return float("inf")
    return statistics.median(abs(c - a) * 1000.0 for c, a in zip(caption_starts, asr_starts, strict=True))


def align_words(script: str, asr: Sequence[Word], *, total_s: float | None = None) -> AlignResult:
    """Align ASR words to `script` (spec §7.6) and return the script's words with ASR/interpolated times.

    `total_s` bounds trailing interpolation (default: end of the last ASR word).
    """
    words = script_words(script)
    if not words:
        return AlignResult([], 0, 0, 0.0)
    if total_s is None:
        total_s = max((w.end for w in asr), default=0.0)
    times = _word_times_from_tokens(words, asr)
    filled = _interpolate(words, times, max(total_s, 0.0))
    out = [Word(w, s, e, si) for (w, si), (s, e) in zip(words, filled, strict=True)]
    pairs = [(f[0], t[0]) for f, t in zip(filled, times, strict=True) if t is not None]
    drift = drift_ms([c for c, _a in pairs], [a for _c, a in pairs])
    return AlignResult(out, len(pairs), len(words), drift)


# ---------------------------------------------------------------------------------------------------------
# ASS builder
# ---------------------------------------------------------------------------------------------------------


def _ass_color(hex_rgb: str, alpha: int = 0) -> str:
    h = hex_rgb.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _ass_inline_color(hex_rgb: str) -> str:
    return _ass_color(hex_rgb)[0:2] + _ass_color(hex_rgb)[4:] + "&"


def _ass_time(t: float) -> str:
    cs = max(0, round(t * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def _escape(text: str) -> str:
    return text.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def resolve_font(style: CaptionStyle, fonts_dir: Path | None = None) -> str:
    """The preset font when a matching file is in the fonts folder, else the Windows fallback font."""
    folder = fonts_dir if fonts_dir is not None else FONTS_DIR
    if folder.is_dir() and any(p.name.lower().startswith(style.font.lower()) for p in folder.iterdir()):
        return style.font
    return style.font_fallback


@dataclass(frozen=True)
class Box:
    """A rectangle in canvas pixels (a detected face or facecam region)."""

    x: int
    y: int
    w: int
    h: int


DEFAULT_SAFE_ZONES = (220, 320)  # YouTube UI on 9:16: top and bottom pixels to keep clear
MIN_CONTRAST = 4.5
EMPHASIS_SCALE = 115
_WORDISH = re.compile(r"[A-Za-z0-9']+")


def free_bands(
    h_px: int, avoid: Sequence[Box], safe_top: int, safe_bottom: int, x0: int, x1: int
) -> list[tuple[int, int]]:
    """Vertical bands (top, bottom) inside the safe zones not covered by any region over [x0, x1]."""
    lo, hi = safe_top, h_px - safe_bottom
    blocks = sorted(
        (max(lo, b.y), min(hi, b.y + b.h))
        for b in avoid
        if b.x < x1 and b.x + b.w > x0 and b.y < hi and b.y + b.h > lo
    )
    bands: list[tuple[int, int]] = []
    cur = lo
    for s, e in blocks:
        if s > cur:
            bands.append((cur, s))
        cur = max(cur, e)
    if cur < hi:
        bands.append((cur, hi))
    return bands


def choose_margin_v(
    *,
    h_px: int,
    preferred_margin_v: int,
    block_h: int,
    x_range: tuple[int, int],
    avoid: Sequence[Box] = (),
    safe_zones: tuple[int, int] | None = None,
) -> int:
    """Bottom margin (px) for the caption block: the style's own place when it is free, else the largest band.

    A place is free when the block lies inside one free band (outside the safe zones and every region).
    With no free band tall enough the preferred margin is kept.
    """
    top, bottom = safe_zones or (0, 0)
    bands = free_bands(h_px, avoid, top, bottom, x_range[0], x_range[1])
    pref_bottom = h_px - preferred_margin_v
    pref_top = pref_bottom - block_h
    if any(s <= pref_top and pref_bottom <= e for s, e in bands):
        return preferred_margin_v
    fitting = [(e - s, s, e) for s, e in bands if e - s >= block_h]
    if not fitting:
        return preferred_margin_v
    _size, s, e = max(fitting)
    pad = min(12, (e - s - block_h) // 2)
    return h_px - (e - pad)


def caption_placement(
    geo: CaptionVariant,
    variant: Variant,
    *,
    avoid_regions: Sequence[Box] | None = None,
    safe_zones: tuple[int, int] | None = None,
    anchor_y_pct: float | None = None,
) -> tuple[int, int]:
    """(MarginV, block height) in canvas px of the caption block for a style geometry.

    The preferred place is the style's own `margin_v_pct`, or the text centred on `anchor_y_pct` of the
    canvas height; with avoid regions or safe zones it moves to the largest free band (spec 17.2).
    """
    w_px, h_px = (1080, 1920) if variant == "vertical" else (1920, 1080)
    if anchor_y_pct is None:
        margin_v = round(h_px * geo.margin_v_pct / 100.0)
    else:
        margin_v = max(20, round(h_px * (1 - anchor_y_pct / 100.0) - 0.6 * geo.font_size))
    block_h = int(geo.font_size * 1.2 * (2 if variant == "vertical" else 1))
    if avoid_regions or safe_zones:
        margin_v = choose_margin_v(
            h_px=h_px, preferred_margin_v=margin_v, block_h=block_h,
            x_range=(geo.margin_lr, w_px - geo.margin_lr), avoid=avoid_regions or (), safe_zones=safe_zones,
        )
    return margin_v, block_h


def caption_band(
    style: CaptionStyle | str,
    variant: Variant = "vertical",
    *,
    avoid_regions: Sequence[Box] | None = None,
    safe_zones: tuple[int, int] | None = None,
    anchor_y_pct: float | None = None,
) -> Box:
    """The canvas rectangle the caption block occupies (what the contrast check samples behind)."""
    st = caption_style(style) if isinstance(style, str) else style
    geo = st.vertical if variant == "vertical" else st.landscape
    w_px, h_px = (1080, 1920) if variant == "vertical" else (1920, 1080)
    margin_v, block_h = caption_placement(
        geo, variant, avoid_regions=avoid_regions, safe_zones=safe_zones, anchor_y_pct=anchor_y_pct
    )
    top = min(max(0, h_px - margin_v - block_h), h_px - block_h)
    return Box(geo.margin_lr, top, w_px - 2 * geo.margin_lr, block_h)


def _channel(v: int) -> float:
    c = v / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_rgb: str) -> float:
    """WCAG relative luminance of a #RRGGBB colour."""
    h = hex_rgb.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def luma_to_luminance(y: float) -> float:
    """Relative luminance (0-1) of a limited-range luma sample (16-235), gamma 2.2."""
    return min(1.0, max(0.0, (y - 16.0) / 219.0)) ** 2.2


def contrast_ratio(l1: float, l2: float) -> float:
    """WCAG contrast ratio of two relative luminances (1.0 to 21.0)."""
    hi, lo = max(l1, l2), min(l1, l2)
    return (hi + 0.05) / (lo + 0.05)


def boxed_variant(style: CaptionStyle) -> CaptionStyle:
    """The style with an opaque dark box behind the text (spec §17.2 low-contrast fallback)."""
    return style.model_copy(update={
        "box": True,
        "outline_color": "#101010",
        "vertical": style.vertical.model_copy(update={"outline": max(style.vertical.outline, 12.0)}),
        "landscape": style.landscape.model_copy(update={"outline": max(style.landscape.outline, 9.0)}),
    })


def ensure_contrast(style: CaptionStyle, backdrop_luma: Sequence[float]) -> CaptionStyle:
    """`style`, or its boxed variant when the text colour has under 4.5:1 contrast on any sampled backdrop.

    `backdrop_luma` holds luma values (16-235) of the frame region behind the caption (see `sample_luma`).
    """
    if style.box or not backdrop_luma:
        return style
    text = relative_luminance(style.primary)
    worst = min(contrast_ratio(text, luma_to_luminance(y)) for y in backdrop_luma)
    return style if worst >= MIN_CONTRAST else boxed_variant(style)


def sample_luma(video: Path, box: Box, times: Sequence[float], *, pre: str = "") -> list[float]:
    """Mean luma (YAVG) of `box` at each time, via FFmpeg crop + signalstats.

    `pre` is an optional filter chain (ending without a comma) applied first, so `box` can be given in the
    coordinates of the finished frame, for example `scale=1080:1920:force_original_aspect_ratio=increase,
    crop=1080:1920`.
    """
    out: list[float] = []
    head = f"{pre}," if pre else ""
    for t in times:
        proc = ff.run([
            "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", "-an", "-vf",
            f"{head}crop={box.w}:{box.h}:{box.x}:{box.y},signalstats,"
            "metadata=print:key=lavfi.signalstats.YAVG",
            "-f", "null", "-",
        ])
        m = re.search(r"lavfi\.signalstats\.YAVG=([\d.]+)", proc.stdout + proc.stderr)
        if m:
            out.append(float(m.group(1)))
    return out


@lru_cache(maxsize=1)
def profanity_words() -> frozenset[str]:
    """Lower-case words to mask, from contracts/policy/profanity.txt (blank lines and # comments ignored)."""
    f = contracts_dir() / "policy" / "profanity.txt"
    if not f.exists():
        return frozenset()
    lines = (ln.strip().lower() for ln in f.read_text(encoding="utf-8").splitlines())
    return frozenset(ln for ln in lines if ln and not ln.startswith("#"))


def mask_text(text: str, words: frozenset[str] | None = None) -> str:
    """Mask listed words except their first letter: "shit" -> "s***" (display only; audio is untouched)."""
    bad = profanity_words() if words is None else words

    def repl(m: re.Match[str]) -> str:
        word = m.group(0)
        return word[0] + "*" * (len(word) - 1) if word.lower() in bad else word

    return _WORDISH.sub(repl, text)


def _split_page(page: list[Word], max_dur_s: float) -> list[list[Word]]:
    if len(page) > 1 and page[-1].end - page[0].start > max_dur_s:
        mid = (len(page) + 1) // 2
        return _split_page(page[:mid], max_dur_s) + _split_page(page[mid:], max_dur_s)
    return [page]


def _merge_pages(
    pages: list[list[Word]], max_words: int, min_dur_s: float, max_dur_s: float
) -> list[list[Word]]:
    out: list[list[Word]] = []
    for page in pages:
        if out:
            prev = out[-1]
            short = (prev[-1].end - prev[0].start < min_dur_s) or (page[-1].end - page[0].start < min_dur_s)
            if short and len(prev) + len(page) <= max_words and page[-1].end - prev[0].start <= max_dur_s:
                out[-1] = prev + page
                continue
        out.append(page)
    return out


def group_words(
    words: Sequence[Word],
    style: CaptionStyle,
    *,
    max_pause_s: float = 0.6,
    min_dur_s: float = 0.9,
    max_dur_s: float = 2.2,
    breaks_before: Collection[int] = (),
) -> list[list[Word]]:
    """Split words into on-screen groups of at most `style.words_on_screen[1]` (<= 4) words.

    Groups never span a sentence end, a pause longer than `max_pause_s`, or a word index in `breaks_before`
    (speaker changes). A run is split into evenly sized pages; pages lasting more than `max_dur_s` are halved
    and neighbouring pages shorter than `min_dur_s` are merged while the group stays within the word limit
    (spec §17.2: 0.9-2.2 s on screen).
    """
    max_words = max(1, min(4, style.words_on_screen[1]))
    runs: list[list[Word]] = []
    cur: list[Word] = []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if (
            nxt is None
            or _SENTENCE_END.search(w.w)
            or nxt.start - w.end > max_pause_s
            or (i + 1) in breaks_before
        ):
            runs.append(cur)
            cur = []
    groups: list[list[Word]] = []
    for run in runs:
        pages = -(-len(run) // max_words)
        base, extra = divmod(len(run), pages)
        pos = 0
        split: list[list[Word]] = []
        for p in range(pages):
            size = base + (1 if p < extra else 0)
            split += _split_page(run[pos : pos + size], max_dur_s)
            pos += size
        groups += _merge_pages(split, max_words, min_dur_s, max_dur_s)
    return groups


def _is_emphasis(w: Word, emphasis: Mapping[int, str] | None) -> bool:
    if not emphasis or w.sentence_idx not in emphasis:
        return False
    target = normalize_tokens(emphasis[w.sentence_idx])
    return bool(target) and normalize_tokens(w.w) == target


def speaker_palette(style: CaptionStyle) -> list[str]:
    """Per-speaker text colours: the style's `palette`, else primary, accent and two fixed fallbacks."""
    if style.palette:
        return style.palette
    return [style.primary, style.accent or style.active, "#7CE38B", "#FF8FB1"]


def build_ass(
    words: Sequence[Word],
    style: CaptionStyle | str,
    *,
    variant: Variant = "vertical",
    fonts_dir: Path | None = None,
    hold_s: float = 0.35,
    first_at_zero: bool = True,
    avoid_regions: Sequence[Box] | None = None,
    safe_zones: tuple[int, int] | None = None,
    emphasis: Mapping[int, str] | None = None,
    speakers: Mapping[int, int] | None = None,
    speaker_breaks: Collection[int] = (),
    backdrop_luma: Sequence[float] | None = None,
    mask_profanity: bool = False,
    anchor_y_pct: float | None = None,
) -> str:
    """Build an ASS document: 2-4 words on screen, the active word in the style's highlight colour.

    The first event starts at 0.0 s (spec §18.5). 9:16 (`vertical`, 1080x1920) and 16:9 (`landscape`,
    1920x1080) variants come from the style. Animations: `pop` scales the active word, `karaoke` sweeps
    the highlight, `none` only recolours. Optional smart features (spec §17.2), all off by default:

    - `avoid_regions` / `safe_zones` (top, bottom px): the caption block moves to the largest free band.
    - `emphasis`: {sentence_idx: key word} shown in the accent colour at +15 % size.
    - `speakers`: {group_idx: speaker} gives each speaker's groups a palette colour; `speaker_breaks` are word
      indices where a new speaker starts (they end the current group).
    - `backdrop_luma`: luma samples behind the caption; low contrast (< 4.5:1) switches to the boxed variant.
    - `mask_profanity`: masks listed words (contracts/policy/profanity.txt) in the captions.
    - `anchor_y_pct`: the style's preferred place becomes "text centred on this % of the canvas height"
      (layouts with a caption anchor, spec 19.3); the free-band search above still applies.
    """
    st = caption_style(style) if isinstance(style, str) else style
    if backdrop_luma:
        st = ensure_contrast(st, backdrop_luma)
    geo = st.vertical if variant == "vertical" else st.landscape
    w_px, h_px = (1080, 1920) if variant == "vertical" else (1920, 1080)
    font = resolve_font(st, fonts_dir)
    border_style = 3 if st.box else 1
    outline_col = st.outline_color
    primary = st.active if st.animation == "karaoke" else st.primary
    secondary = st.primary
    margin_v, _block_h = caption_placement(
        geo, variant, avoid_regions=avoid_regions, safe_zones=safe_zones, anchor_y_pct=anchor_y_pct
    )
    header = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {w_px}",
        f"PlayResY: {h_px}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font},{geo.font_size},{_ass_color(primary)},{_ass_color(secondary)},"
        f"{_ass_color(outline_col)},{_ass_color(st.shadow_color, 100)},{-1 if st.bold else 0},"
        f"0,0,0,100,100,0,0,"
        f"{border_style},{geo.outline},{geo.shadow},2,{geo.margin_lr},{geo.margin_lr},{margin_v},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    groups = group_words(words, st, breaks_before=speaker_breaks)
    palette = speaker_palette(st)
    accent = st.accent or st.active
    masked = profanity_words() if mask_profanity else frozenset()
    events: list[str] = []
    reset = "\\fscx100\\fscy100" if emphasis else ""

    def label(w: Word) -> str:
        t = _escape(w.w)
        if mask_profanity:
            t = mask_text(t, masked)
        return t.upper() if st.uppercase else t

    for gi, g in enumerate(groups):
        base_col = palette[speakers[gi] % len(palette)] if speakers and gi in speakers else st.primary
        base_tag = f"{{\\c{_ass_inline_color(base_col)}{reset}}}"
        g_start = 0.0 if (gi == 0 and first_at_zero) else g[0].start
        next_start = groups[gi + 1][0].start if gi + 1 < len(groups) else None
        g_end = g[-1].end + hold_s
        if next_start is not None:
            g_end = min(g_end, next_start) if next_start > g[-1].end else g[-1].end + 0.04
        g_end = max(g_end, g_start + 0.08)
        if st.animation == "karaoke":
            parts: list[str] = []
            for k, w in enumerate(g):
                nxt_t = g[k + 1].start if k + 1 < len(g) else g_end
                cs = max(1, round((nxt_t - (g_start if k == 0 else w.start)) * 100))
                size = f"\\fscx{EMPHASIS_SCALE}\\fscy{EMPHASIS_SCALE}" if _is_emphasis(w, emphasis) else ""
                after = "\\fscx100\\fscy100" if size else ""
                parts.append(f"{{\\kf{cs}{size}}}{label(w)}" + (f"{{{after}}}" if after else ""))
            events.append(
                f"Dialogue: 0,{_ass_time(g_start)},{_ass_time(g_end)},Default,,0,0,0,,{' '.join(parts)}"
            )
            continue
        for k, w in enumerate(g):
            ev_start = g_start if k == 0 else w.start
            ev_end = g[k + 1].start if k + 1 < len(g) else g_end
            ev_end = max(ev_end, ev_start + 0.04)
            pieces: list[str] = []
            for j, other in enumerate(g):
                emph = _is_emphasis(other, emphasis)
                size = f"\\fscx{EMPHASIS_SCALE}\\fscy{EMPHASIS_SCALE}" if emph else ""
                if j == k:
                    col = accent if emph else st.active
                    pop = "\\fscx118\\fscy118\\t(0,90,\\fscx100\\fscy100)" if st.animation == "pop" else ""
                    pieces.append(f"{{\\c{_ass_inline_color(col)}{size}{pop}}}{label(other)}{base_tag}")
                elif emph:
                    pieces.append(f"{{\\c{_ass_inline_color(accent)}{size}}}{label(other)}{base_tag}")
                else:
                    pieces.append(label(other))
            prefix = base_tag if (speakers and gi in speakers) else ""
            events.append(
                f"Dialogue: 0,{_ass_time(ev_start)},{_ass_time(ev_end)},Default,,0,0,0,,"
                f"{prefix}{' '.join(pieces)}"
            )
    return "\n".join([*header, *events]) + "\n"


def first_event_start(ass_text: str) -> float:
    """Start time in seconds of the first Dialogue line of an ASS document (inf when there is none)."""
    m = re.search(r"^Dialogue: \d+,(\d+):(\d\d):(\d\d)\.(\d\d),", ass_text, re.MULTILINE)
    if not m:
        return float("inf")
    h, mi, s, c = (int(g) for g in m.groups())
    return h * 3600 + mi * 60 + s + c / 100.0


# ---------------------------------------------------------------------------------------------------------
# Timeline assembly and the stage
# ---------------------------------------------------------------------------------------------------------


def shift_words(words: Sequence[Word], offset: float, *, sentence_idx: int | None = None) -> list[Word]:
    """Copy of `words` moved by `offset` seconds (optionally forcing a sentence index)."""
    return [
        Word(w.w, w.start + offset, w.end + offset, w.sentence_idx if sentence_idx is None else sentence_idx)
        for w in words
    ]


class CaptionsStage:
    """Stage `captions` (tenant whisper): align ASR words to each part's script and write ASS files.

    For part n: `words/p<n>.json` (body, body-relative times), `words/p<n>_hook.json` when a voiced hook
    exists, `captions/s<n>.ass` and `captions/s<n>.json` (final timeline: hook, 150 ms gap, body) plus the
    summary `captions/report.json`. Raises NS-E403 when median drift exceeds 120 ms or too few words match.
    """

    name: str = "captions"
    tenant: Tenant = "whisper"
    inputs: tuple[str, ...] = ("audio/manifest.json", "story.json")
    outputs: tuple[str, ...] = ("captions/report.json",)

    def run(self, ctx: JobContext) -> None:
        """Transcribe, align and write captions for every part; fail with NS-E403 on drift."""
        if ctx.fmt == "master_first":
            from ns_studio.media.master import run_captions_master

            run_captions_master(ctx)
            return
        from ns_studio.media.caption_plan import (
            AssOptions,
            emphasis_from_beats,
            mask_profanity_setting,
            safe_zones_for,
        )

        parts = ctx.read_json("parts.json")
        manifest = ctx.read_json("audio/manifest.json")
        plan_file = ctx.file("visual_plan.json")
        beats: list[dict] = ctx.read_json("visual_plan.json") if plan_file.exists() else []
        mask = mask_profanity_setting(ctx.channel)
        tr = ctx.services.transcriber
        lang = ctx.channel.language
        ctx.subdir("words")
        ctx.subdir("captions")
        report: dict[str, dict] = {}
        failures: list[str] = []
        for p in parts:
            n = p["n"]
            body = manifest[f"p{n}"]
            body_wav = Path(body["path"])
            body_res = align_words(p["text"], tr.words(body_wav, lang), total_s=float(body["seconds"]))
            ctx.write_json(f"words/p{n}.json", [w.to_json() for w in body_res.words])
            timeline: list[Word] = []
            offset = 0.0
            drift = body_res.drift_ms
            ratio = body_res.match_ratio
            hook_entry = manifest.get(f"p{n}_hook")
            if hook_entry:
                hook_res = align_words(
                    p["hook"], tr.words(Path(hook_entry["path"]), lang), total_s=float(hook_entry["seconds"])
                )
                ctx.write_json(f"words/p{n}_hook.json", [w.to_json() for w in hook_res.words])
                timeline += shift_words(hook_res.words, 0.0, sentence_idx=-1)
                offset = float(hook_entry["seconds"]) + HOOK_GAP_S
                drift = max(drift, hook_res.drift_ms)
                ratio = min(ratio, hook_res.match_ratio)
            timeline += shift_words(body_res.words, offset)
            total_s = offset + float(body["seconds"])
            style_name = ctx.combo.caption_style
            opts = AssOptions(
                variant="vertical", safe_zones=safe_zones_for("vertical"),
                emphasis=emphasis_from_beats(beats, part=n), mask_profanity=mask,
            )
            ass = opts.build(timeline, style_name)
            ctx.file(f"captions/s{n}.ass").write_text(ass, encoding="utf-8")
            ctx.write_json(
                f"captions/s{n}.json",
                {
                    "k": n,
                    "style": style_name,
                    "hook_s": float(hook_entry["seconds"]) if hook_entry else 0.0,
                    "body_offset_s": offset,
                    "audio_s": round(total_s, 3),
                    "ass_opts": opts.to_json(),
                    "words": [w.to_json() for w in timeline],
                },
            )
            report[f"s{n}"] = {
                "ass": f"captions/s{n}.ass",
                "timeline": f"captions/s{n}.json",
                "drift_ms": None if drift == float("inf") else round(drift, 1),
                "match_ratio": round(ratio, 3),
                "words": len(timeline),
                "first_caption_s": first_event_start(ass),
            }
            if drift > DRIFT_LIMIT_MS or ratio < MIN_MATCH_RATIO:
                failures.append(f"s{n}: drift {drift:.0f} ms, matched {ratio:.0%}")
        ctx.write_json("captions/report.json", report)
        if failures:
            raise NsError("NS-E403", "; ".join(failures))
