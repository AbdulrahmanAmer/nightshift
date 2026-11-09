"""Master-first media, part 1: story adapters, master timeline, voice, duration check, captions, Shorts
timing (component C6, M2).

Timeline of the master: chapter 1 audio, then for every later chapter a silent title card of `CARD_S`
seconds followed by that chapter's audio. Chapter timestamps in `chapters.json` point at the card start.
"""

from __future__ import annotations

import dataclasses
import importlib
import math
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ns_common.acquire import Service
from ns_common.errors import NsError
from ns_common.pacing import PACING_WPM
from ns_common.textstats import split_sentences
from ns_common.tts_client import TtsItem
from ns_studio.context import JobContext
from ns_studio.media.caption_plan import (
    AssOptions,
    emphasis_from_beats,
    mask_profanity_setting,
    safe_zones_for,
)
from ns_studio.media.captions import (
    DRIFT_LIMIT_MS,
    MIN_MATCH_RATIO,
    align_words,
    build_ass,
    first_event_start,
    shift_words,
)
from ns_studio.media.transcribe import Word
from ns_studio.media.voice import job_speed, job_voice, synthesize
from ns_studio.stages import Tenant

CARD_S = 1.2
FLOOR_S = 540.0  # planning floor 9:00 (spec §7.0)
MAX_EXPANSIONS = 2
CLEAN_STYLES = ("clean_white", "minimal_lower")


@dataclass(frozen=True)
class Chapter:
    """A chapter of the master story."""

    n: int
    title: str
    text: str
    cliff_lines: tuple[str, ...] = ()  # the chapter's cliffhanger sentence; TTS pauses 1,200 ms before it


@dataclass(frozen=True)
class ShortSpec:
    """One selected Short: a sentence span of a chapter plus its fresh hook and closing line."""

    k: int
    chapter: int
    sentence_start: int
    sentence_end: int
    hook: str
    closing_line: str
    length_bucket: int | None = None
    caption_style: str | None = None
    visual_treatment: str | None = None
    combo_id: str | None = None


@dataclass(frozen=True)
class ChapterTiming:
    """Master-timeline seconds of a chapter: `seg_start` (card start), `audio_start`, `end`."""

    n: int
    title: str
    seg_start: float
    audio_start: float
    end: float


def cliff_lines_of(raw: Mapping[str, Any]) -> tuple[str, ...]:
    """The cliff line of a `story.json` chapter: its sentence number `cliff_line_idx` (the last chapter has
    none). Empty when the chapter carries no index or the index points nowhere."""
    idx = raw.get("cliff_line_idx")
    if not isinstance(idx, int) or idx < 0:
        return ()
    sentences = raw.get("sentences")
    if isinstance(sentences, list) and idx < len(sentences) and isinstance(sentences[idx], Mapping):
        text = str(sentences[idx].get("text", "")).strip()
    else:
        found = split_sentences(str(raw.get("text", raw.get("body", ""))))
        text = found[idx].strip() if idx < len(found) else ""
    return (text,) if text else ()


def load_chapters(ctx: JobContext) -> list[Chapter]:
    """Chapters from `story.json` (`chapters` list of {n, title, text}); numbering falls back to position."""
    data = ctx.read_json("story.json")
    raw = data["chapters"] if isinstance(data, dict) else data
    out: list[Chapter] = []
    for i, c in enumerate(raw, start=1):
        out.append(Chapter(int(c.get("n", c.get("chapter", i))), str(c.get("title", f"Chapter {i}")),
                           str(c.get("text", c.get("body", ""))), cliff_lines_of(c)))
    return sorted(out, key=lambda c: c.n)


def load_shorts(ctx: JobContext) -> list[ShortSpec]:
    """Selected Shorts from `shorts_plan.json` (a list, or {"shorts": [...]})."""
    if not ctx.file("shorts_plan.json").exists():
        return []
    data = ctx.read_json("shorts_plan.json")
    raw = data.get("shorts", data.get("moments", [])) if isinstance(data, dict) else data
    # Each Short's own combo (drawn by `shorts_select` / `short_combos`) is the one applied and recorded.
    stored = ctx.read_json("short_combos.json") if ctx.file("short_combos.json").exists() else {}
    out: list[ShortSpec] = []
    for i, s in enumerate(raw, start=1):
        k = int(s.get("k", i))
        combo = s.get("combo") or stored.get(str(k)) or {}
        out.append(
            ShortSpec(
                k=k,
                chapter=int(s["chapter"]),
                sentence_start=int(s["sentence_start"]),
                sentence_end=int(s["sentence_end"]),
                hook=str(s.get("hook") or s.get("chosen_hook") or "").strip(),
                closing_line=str(s.get("closing_line", s.get("closing", ""))).strip(),
                length_bucket=s.get("length_bucket", combo.get("length_bucket")),
                caption_style=s.get("caption_style", combo.get("caption_style")),
                visual_treatment=s.get("visual_treatment", combo.get("visual_treatment")),
                combo_id=combo.get("combo_id") or s.get("combo_id"),
            )
        )
    return out


def master_timeline(
    chapters: Sequence[Chapter], seconds: Mapping[int, float], card_s: float = CARD_S
) -> list[ChapterTiming]:
    """Chapter timings on the master timeline from per-chapter audio seconds."""
    out: list[ChapterTiming] = []
    cursor = 0.0
    for i, ch in enumerate(chapters):
        audio_start = cursor + (card_s if i > 0 else 0.0)
        end = audio_start + float(seconds[ch.n])
        out.append(ChapterTiming(ch.n, ch.title, cursor, audio_start, end))
        cursor = end
    return out


def timeline_from_manifest(ctx: JobContext) -> list[ChapterTiming]:
    """Master timeline from `audio/manifest.json` and `story.json`."""
    manifest = ctx.read_json("audio/manifest.json")
    chapters = load_chapters(ctx)
    return master_timeline(chapters, {c.n: manifest[f"c{c.n}"]["seconds"] for c in chapters})


def voice_items(ctx: JobContext) -> list[TtsItem]:
    """TTS items: `c<n>` chapters, `h<k>` hooks and `x<k>` closing lines of the selected Shorts."""
    voice = job_voice(ctx)
    name = voice.name or voice.speaker or "Ryan"
    speed = job_speed(ctx)
    lang = ctx.channel.language
    items = [TtsItem(f"c{c.n}", c.text, name, speed, c.cliff_lines, lang) for c in load_chapters(ctx)]
    for s in load_shorts(ctx):
        if s.hook:
            items.append(TtsItem(f"h{s.k}", s.hook, name, speed, language=lang))
        if s.closing_line:
            items.append(TtsItem(f"x{s.k}", s.closing_line, name, speed, language=lang))
    return items


def run_voice_master(ctx: JobContext) -> None:
    """Voice branch of the `voice` stage for master-first jobs (only changed items are re-synthesized)."""
    items = voice_items(ctx)
    manifest, synthesized = synthesize(ctx, items)
    ctx.write_json("audio/manifest.json", {it.id: manifest[it.id] for it in items})
    ctx.log.info("voice_done", job_id=ctx.job_id, items=len(items), synthesized=synthesized)


# ---------------------------------------------------------------------------------------------------------
# Duration check and the expansion loop
# ---------------------------------------------------------------------------------------------------------


def load_expand_chapter() -> Callable[[JobContext, int, int], None]:
    """C5's `expand_chapter(ctx, chapter_n, add_words)` (imported lazily; NS-E305 when unavailable)."""
    try:
        mod = importlib.import_module("ns_studio.text.stages_master")
        return mod.expand_chapter
    except (ImportError, AttributeError) as e:
        raise NsError("NS-E305", f"chapter expansion helper unavailable: {e}") from e


@contextmanager
def swapped_tenant(ctx: JobContext, release: Service, acquire: Service) -> Iterator[None]:
    """Hand the GPU from one tenant to another for the duration of the block, then take it back."""
    acq = ctx.services.acquirer
    owner = f"studio:{ctx.job_id}"
    acq.release(release, owner)
    acq.acquire(acquire, owner)
    try:
        yield
    finally:
        acq.release(acquire, owner)
        acq.acquire(release, owner)


class DurationCheckStage:
    """Stage `duration_check` (tenant tts): the master must reach 9:00 of audio (cards included).

    Below the floor the shortest chapter is expanded with C5's `expand_chapter` (+120 to 200 words, the llama
    tenant is borrowed for it) and only that chapter is re-voiced; after 2 expansions NS-E305 is raised.
    """

    name: str = "duration_check"
    tenant: Tenant = "tts"
    inputs: tuple[str, ...] = ("audio/manifest.json", "story.json")
    outputs: tuple[str, ...] = ("duration_check.json",)
    # An expansion rewrites the story and both plans (consistently); stages that ran before must not redo.
    rewrites: tuple[str, ...] = ("story.json", "shorts_plan.json", "visual_plan.json")

    def run(self, ctx: JobContext) -> None:
        """Measure, expand and re-voice until the floor is met or the budget of expansions is spent."""
        wpm = float(PACING_WPM.get(ctx.combo.pacing, 155))
        expansions: list[dict[str, Any]] = []
        while True:
            timeline = timeline_from_manifest(ctx)
            total = timeline[-1].end if timeline else 0.0
            if total >= FLOOR_S:
                break
            if ctx.owner_script:  # the owner's words are never extended with model text
                self._write(ctx, total, expansions)
                raise NsError(
                    "NS-E305", f"{total:.0f} s of narration, the floor is {FLOOR_S:.0f} s: script too short"
                )
            if len(expansions) >= MAX_EXPANSIONS:
                self._write(ctx, total, expansions)
                raise NsError(
                    "NS-E305", f"{total:.0f} s after {len(expansions)} expansions (floor {FLOOR_S:.0f} s)"
                )
            lengths = {t.n: t.end - t.audio_start for t in timeline}
            shortest = min(lengths, key=lambda n: lengths[n])
            deficit_words = math.ceil((FLOOR_S - total) / 60.0 * wpm)
            add_words = max(120, min(200, deficit_words))
            expand = load_expand_chapter()
            with swapped_tenant(ctx, "tts", "llama"):
                expand(ctx, shortest, add_words)
            run_voice_master(ctx)
            expansions.append({"chapter": shortest, "add_words": add_words, "before_s": round(total, 2)})
            ctx.log.info("expanded", job_id=ctx.job_id, chapter=shortest, add_words=add_words)
        self._write(ctx, total, expansions)

    @staticmethod
    def _write(ctx: JobContext, total: float, expansions: list[dict[str, Any]]) -> None:
        data = {"total_s": round(total, 2), "floor_s": FLOOR_S, "expansions": expansions}
        ctx.write_json("duration_check.json", data)


# ---------------------------------------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------------------------------------


def long_caption_style(ctx: JobContext) -> str:
    """Masters use a clean caption style (spec §18.5); the combo's style only when it is already clean."""
    return ctx.combo.caption_style if ctx.combo.caption_style in CLEAN_STYLES else "clean_white"


def load_words(ctx: JobContext, ident: str) -> list[Word]:
    """Aligned words of an audio item from `words/<ident>.json`."""
    return [Word.from_json(w) for w in ctx.read_json(f"words/{ident}.json")]


def _entry(drift: float, ratio: float, words: int, first: float | None, ass: str | None) -> dict[str, Any]:
    return {
        "drift_ms": None if drift == float("inf") else round(drift, 1),
        "match_ratio": round(ratio, 3),
        "words": words,
        "first_caption_s": first,
        "ass": ass,
    }


def run_captions_master(ctx: JobContext) -> None:
    """Captions branch for master-first jobs: align chapters, hooks and closing lines; write ASS files.

    Writes `words/c<n>.json`, `words/h<k>.json`, `words/x<k>.json`, `captions/long.ass` (landscape, master
    timeline), `captions/long_c<n>.ass` (segment-local, used by the renderer), `captions/long.json` and
    `captions/report.json`. NS-E403 on drift above 120 ms or too few matched words.
    """
    manifest = ctx.read_json("audio/manifest.json")
    tr = ctx.services.transcriber
    lang = ctx.channel.language
    chapters = load_chapters(ctx)
    timeline = timeline_from_manifest(ctx)
    ctx.subdir("words")
    ctx.subdir("captions")
    style = long_caption_style(ctx)
    plan_file = ctx.file("visual_plan.json")
    beats: list[dict[str, Any]] = ctx.read_json("visual_plan.json") if plan_file.exists() else []
    base_opts = AssOptions(
        variant="landscape", safe_zones=safe_zones_for("landscape"),
        mask_profanity=mask_profanity_setting(ctx.channel),
    )
    report: dict[str, Any] = {}
    failures: list[str] = []
    all_words: list[Word] = []
    segs: list[dict[str, Any]] = []
    drift_max, ratio_min = 0.0, 1.0
    for ch, t in zip(chapters, timeline, strict=True):
        entry = manifest[f"c{ch.n}"]
        res = align_words(ch.text, tr.words(Path(entry["path"]), lang), total_s=float(entry["seconds"]))
        ctx.write_json(f"words/c{ch.n}.json", [w.to_json() for w in res.words])
        drift_max, ratio_min = max(drift_max, res.drift_ms), min(ratio_min, res.match_ratio)
        if res.drift_ms > DRIFT_LIMIT_MS or res.match_ratio < MIN_MATCH_RATIO:
            failures.append(f"c{ch.n}: drift {res.drift_ms:.0f} ms, matched {res.match_ratio:.0%}")
        all_words += shift_words(res.words, t.audio_start)
        local = shift_words(res.words, t.audio_start - t.seg_start)
        ass_name = f"captions/long_c{ch.n}.ass"
        opts = dataclasses.replace(base_opts, emphasis=emphasis_from_beats(beats, chapter=ch.n))
        local_ass = build_ass(
            local, style, first_at_zero=t.audio_start == t.seg_start, **opts.kwargs()
        )
        ctx.file(ass_name).write_text(local_ass, encoding="utf-8")
        segs.append({"n": ch.n, "title": ch.title, "seg_start": t.seg_start, "audio_start": t.audio_start,
                     "end": t.end, "ass": ass_name})
    long_ass = base_opts.build(all_words, style)
    ctx.file("captions/long.ass").write_text(long_ass, encoding="utf-8")
    ctx.write_json("captions/long.json", {
        "style": style, "audio_s": timeline[-1].end if timeline else 0.0, "chapters": segs,
        "words": [w.to_json() for w in all_words],
    })
    first = first_event_start(long_ass)
    report["long"] = _entry(drift_max, ratio_min, len(all_words), first, "captions/long.ass")
    for s in load_shorts(ctx):
        for prefix, text in (("h", s.hook), ("x", s.closing_line)):
            ident = f"{prefix}{s.k}"
            if not text or ident not in manifest:
                continue
            e = manifest[ident]
            res = align_words(text, tr.words(Path(e["path"]), lang), total_s=float(e["seconds"]))
            ctx.write_json(f"words/{ident}.json", [w.to_json() for w in res.words])
            report[ident] = _entry(res.drift_ms, res.match_ratio, len(res.words), None, None)
            if res.drift_ms > DRIFT_LIMIT_MS or res.match_ratio < MIN_MATCH_RATIO:
                failures.append(f"{ident}: drift {res.drift_ms:.0f} ms, matched {res.match_ratio:.0%}")
    ctx.write_json("captions/report.json", report)
    if failures:
        raise NsError("NS-E403", "; ".join(failures))


# ---------------------------------------------------------------------------------------------------------
# Shorts timing (M11b)
# ---------------------------------------------------------------------------------------------------------

PAD_BEFORE_S = 0.05
PAD_AFTER_S = 0.25


def resolve_span(
    words: Sequence[Word], sentence_start: int, sentence_end: int, chapter_s: float
) -> tuple[float, float]:
    """(start, end) seconds in chapter time of a sentence span, snapped to sentence boundaries.

    The start is the first word of `sentence_start` (minus 50 ms); the end is the last word of `sentence_end`
    plus up to 250 ms, never reaching the next sentence or the end of the audio. NS-E303 for a missing span.
    """
    first = [w for w in words if w.sentence_idx == sentence_start]
    last = [w for w in words if w.sentence_idx == sentence_end]
    if not first or not last or sentence_end < sentence_start:
        raise NsError("NS-E303", f"Short span {sentence_start}-{sentence_end} not found in chapter words")
    start = max(0.0, first[0].start - PAD_BEFORE_S)
    nxt = [w.start for w in words if w.sentence_idx == sentence_end + 1]
    ceiling = nxt[0] if nxt else chapter_s
    end = min(last[-1].end + PAD_AFTER_S, ceiling, chapter_s)
    return start, max(end, last[-1].end)


class ShortsTimingStage:
    """Stage `shorts_timing` (M11b): sentence spans -> start/end seconds, from `words/`.

    Writes `shorts_timing.json`: per Short `k, chapter, rel_start_s, rel_end_s` (inside the chapter audio) and
    `start_s, end_s` (master timeline). `shorts_plan.json` itself is left untouched so that resuming does not
    invalidate its own input.
    """

    name: str = "shorts_timing"
    tenant: Tenant = None
    inputs: tuple[str, ...] = ("captions/report.json", "shorts_plan.json")
    outputs: tuple[str, ...] = ("shorts_timing.json",)

    def run(self, ctx: JobContext) -> None:
        """Resolve every selected Short."""
        timeline = {t.n: t for t in timeline_from_manifest(ctx)}
        manifest = ctx.read_json("audio/manifest.json")
        out: list[dict[str, Any]] = []
        for s in load_shorts(ctx):
            if s.chapter not in timeline:
                raise NsError("NS-E303", f"Short {s.k} points at missing chapter {s.chapter}")
            words = load_words(ctx, f"c{s.chapter}")
            rel_s, rel_e = resolve_span(words, s.sentence_start, s.sentence_end,
                                        float(manifest[f"c{s.chapter}"]["seconds"]))
            base = timeline[s.chapter].audio_start
            out.append({"k": s.k, "chapter": s.chapter, "rel_start_s": round(rel_s, 3),
                        "rel_end_s": round(rel_e, 3), "start_s": round(base + rel_s, 3),
                        "end_s": round(base + rel_e, 3)})
        ctx.write_json("shorts_timing.json", out)
        ctx.log.info("shorts_timed", job_id=ctx.job_id, shorts=len(out))


def sentence_count(text: str) -> int:
    """Number of sentences of a chapter text as indexed by visual and Shorts plans."""
    return len(split_sentences(text))
