"""Caption alignment to the script, ASS subtitle builder and the captions stage (component C6).

Spec §7.6: ASR tokens are aligned to the script's normalized tokens with difflib; matched words take ASR
times, unmatched runs are interpolated; rendered words are always the script's.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Literal

from ns_common.errors import NsError
from ns_common.textstats import normalize_tokens, split_sentences
from ns_studio.context import JobContext
from ns_studio.media.presets import FONTS_DIR, CaptionStyle, caption_style
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


def drift_ms(starts: Sequence[float], matched: Sequence[bool]) -> float:
    """Median |ASR start - start interpolated from the nearest matched neighbours| over matched words, in ms.

    Words without a matched neighbour on each side are skipped; fewer than three matched words give 0.0
    (no evidence); no matched word at all gives infinity.
    """
    idx = [i for i, m in enumerate(matched) if m]
    if not idx:
        return float("inf")
    errors: list[float] = []
    for pos in range(1, len(idx) - 1):
        lo, mid, hi = idx[pos - 1], idx[pos], idx[pos + 1]
        frac = (mid - lo) / (hi - lo)
        predicted = starts[lo] + (starts[hi] - starts[lo]) * frac
        errors.append(abs(starts[mid] - predicted) * 1000.0)
    return statistics.median(errors) if errors else 0.0


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
    matched = [t is not None for t in times]
    filled = _interpolate(words, times, max(total_s, 0.0))
    out = [Word(w, s, e, si) for (w, si), (s, e) in zip(words, filled, strict=True)]
    return AlignResult(out, sum(matched), len(words), drift_ms([s for s, _e in filled], matched))


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


def group_words(words: Sequence[Word], style: CaptionStyle, *, max_pause_s: float = 0.6) -> list[list[Word]]:
    """Split words into on-screen groups of 2 to `style.words_on_screen[1]` words.

    Groups never span a sentence end or a pause longer than `max_pause_s`; a run is split into evenly sized
    pages so no page is a lone word unless the sentence is.
    """
    max_words = max(1, min(4, style.words_on_screen[1]))
    runs: list[list[Word]] = []
    cur: list[Word] = []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1] if i + 1 < len(words) else None
        if nxt is None or _SENTENCE_END.search(w.w) or nxt.start - w.end > max_pause_s:
            runs.append(cur)
            cur = []
    groups: list[list[Word]] = []
    for run in runs:
        pages = -(-len(run) // max_words)
        base, extra = divmod(len(run), pages)
        pos = 0
        for p in range(pages):
            size = base + (1 if p < extra else 0)
            groups.append(run[pos : pos + size])
            pos += size
    return groups


def build_ass(
    words: Sequence[Word],
    style: CaptionStyle | str,
    *,
    variant: Variant = "vertical",
    fonts_dir: Path | None = None,
    hold_s: float = 0.35,
    first_at_zero: bool = True,
) -> str:
    """Build an ASS document: 2-4 words on screen, the active word in the style's highlight colour.

    The first event starts at 0.0 s (spec §18.5). 9:16 (`vertical`, 1080x1920) and 16:9 (`landscape`,
    1920x1080) variants come from the style. Animations: `pop` scales the active word, `karaoke` sweeps
    the highlight, `none` only recolours.
    """
    st = caption_style(style) if isinstance(style, str) else style
    geo = st.vertical if variant == "vertical" else st.landscape
    w_px, h_px = (1080, 1920) if variant == "vertical" else (1920, 1080)
    font = resolve_font(st, fonts_dir)
    border_style = 3 if st.box else 1
    outline_col = st.outline_color
    primary = st.active if st.animation == "karaoke" else st.primary
    secondary = st.primary
    margin_v = round(h_px * geo.margin_v_pct / 100.0)
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
    groups = group_words(words, st)
    events: list[str] = []
    active_tag = f"{{\\c{_ass_inline_color(st.active)}"
    base_tag = f"{{\\c{_ass_inline_color(st.primary)}}}"

    def label(w: Word) -> str:
        t = _escape(w.w)
        return t.upper() if st.uppercase else t

    for gi, g in enumerate(groups):
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
                parts.append(f"{{\\kf{cs}}}{label(w)}")
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
                if j == k:
                    pop = "\\fscx118\\fscy118\\t(0,90,\\fscx100\\fscy100)" if st.animation == "pop" else ""
                    pieces.append(f"{active_tag}{pop}}}{label(other)}{base_tag}")
                else:
                    pieces.append(label(other))
            events.append(
                f"Dialogue: 0,{_ass_time(ev_start)},{_ass_time(ev_end)},Default,,0,0,0,,{' '.join(pieces)}"
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
        parts = ctx.read_json("parts.json")
        manifest = ctx.read_json("audio/manifest.json")
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
            ass = build_ass(timeline, style_name, variant="vertical")
            ctx.file(f"captions/s{n}.ass").write_text(ass, encoding="utf-8")
            ctx.write_json(
                f"captions/s{n}.json",
                {
                    "k": n,
                    "style": style_name,
                    "hook_s": float(hook_entry["seconds"]) if hook_entry else 0.0,
                    "body_offset_s": offset,
                    "audio_s": round(total_s, 3),
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
