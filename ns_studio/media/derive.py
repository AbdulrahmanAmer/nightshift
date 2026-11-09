"""Derived Shorts of a master-first job: audio cut, voiced hook, closing line, fresh edit (C6, M2).

Each Short is a new edit: its own vertical clips (never a crop of the master), its own caption style and
treatment when the plan carries them, a freshly voiced hook and a closing line that points to the master.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_studio.context import JobContext
from ns_studio.media.audio import assemble_audio, cut_audio, loudnorm
from ns_studio.media.caption_plan import (
    AssOptions,
    emphasis_from_beats,
    mask_profanity_setting,
)
from ns_studio.media.captions import first_event_start, shift_words
from ns_studio.media.clips import (
    ClipCache,
    Searcher,
    assign_clips,
    blocked_for_job,
    clip_seed,
    fallback_beat,
    make_lemmatizer,
)
from ns_studio.media.formats import footage_orientation, shot_range, style_for_format
from ns_studio.media.job_formats import job_format
from ns_studio.media.master import ShortSpec, load_shorts, load_words
from ns_studio.media.presets import FONTS_DIR, visual_treatment
from ns_studio.media.render import (
    FINAL_LUFS,
    FINAL_TP,
    fit_caption_contrast,
    render_video,
    video_for_format,
)
from ns_studio.media.shotplan import beat_spans, plan_slots
from ns_studio.media.transcribe import Word
from ns_studio.stages import Tenant

HOOK_GAP_S = 0.15
CLOSE_GAP_S = 0.35
SHORT_MAX_S = 179.0


def short_beats(
    plan: list[dict[str, Any]], spec: ShortSpec, fallback: dict[str, Any]
) -> list[dict[str, Any]]:
    """Visual beats overlapping the Short's span, re-indexed so sentence 0 is the span's first sentence."""
    out: list[dict[str, Any]] = []
    for b in plan:
        if b.get("chapter") != spec.chapter or not b.get("need_vertical", True):
            continue
        bs, be = int(b["sentence_start"]), int(b["sentence_end"])
        if be < spec.sentence_start or bs > spec.sentence_end:
            continue
        out.append(dict(b, sentence_start=max(0, bs - spec.sentence_start),
                        sentence_end=min(be, spec.sentence_end) - spec.sentence_start))
    return out or [fallback]


def span_words(chapter_words: list[Word], spec: ShortSpec, rel_start: float, offset: float) -> list[Word]:
    """Chapter words of the span on the Short timeline; sentence indices count from the span start."""
    return [
        Word(w.w, w.start - rel_start + offset, w.end - rel_start + offset,
             w.sentence_idx - spec.sentence_start)
        for w in chapter_words
        if spec.sentence_start <= w.sentence_idx <= spec.sentence_end
    ]


def derive_short(
    ctx: JobContext,
    spec: ShortSpec,
    timing: dict[str, Any],
    plan: list[dict[str, Any]],
    manifest: dict[str, Any],
    captions_report: dict[str, Any],
    searcher: Searcher,
    cache: ClipCache,
    blocked: set[tuple[str, str]],
    used: set[tuple[str, str]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build one derived Short; returns (render report entry, shots entry)."""
    item = f"s{spec.k}"
    out_dir = ctx.subdir(f"shorts/{item}")
    rel_start, rel_end = float(timing["rel_start_s"]), float(timing["rel_end_s"])
    body = cut_audio(Path(manifest[f"c{spec.chapter}"]["path"]), out_dir / "body.wav", rel_start, rel_end)
    body_s = ff.duration_s(body)
    hook = manifest.get(f"h{spec.k}")
    close = manifest.get(f"x{spec.k}")
    segments: list[Path | float] = []
    words: list[Word] = []
    offset = 0.0
    if hook:
        segments += [Path(hook["path"]), HOOK_GAP_S]
        words += shift_words(load_words(ctx, f"h{spec.k}"), 0.0, sentence_idx=-1)
        offset = float(hook["seconds"]) + HOOK_GAP_S
    segments.append(body)
    words += span_words(load_words(ctx, f"c{spec.chapter}"), spec, rel_start, offset)
    total = offset + body_s
    if close:
        segments += [CLOSE_GAP_S, Path(close["path"])]
        words += shift_words(load_words(ctx, f"x{spec.k}"), total + CLOSE_GAP_S, sentence_idx=-2)
        total += CLOSE_GAP_S + float(close["seconds"])
    if total > SHORT_MAX_S:
        raise NsError("NS-E404", f"{item}: {total:.0f} s exceeds the {SHORT_MAX_S:.0f} s Shorts limit")
    raw = assemble_audio(segments, out_dir / "audio_raw.wav")
    audio = out_dir / "audio.wav"
    loudnorm(raw, audio, i=FINAL_LUFS, tp=FINAL_TP, log=ctx.log)
    total = ff.duration_s(audio)

    fmt = job_format(ctx, "short")
    canvas = (fmt.canvas.w, fmt.canvas.h)
    style, swapped = style_for_format(fmt, spec.caption_style or ctx.combo.caption_style)
    if swapped:
        ctx.log.info("caption_style_not_in_format", f"{style} is used: {fmt.id} limits the caption styles",
                     format_id=fmt.id, item=item)
    opts = AssOptions.for_format(
        fmt,
        emphasis=emphasis_from_beats(plan, chapter=spec.chapter, shift=spec.sentence_start,
                                     last=spec.sentence_end),
        mask_profanity=mask_profanity_setting(ctx.channel),
    )
    ass_text = opts.build(words, style)
    ass = ctx.subdir("captions") / f"{item}.ass"
    ass.write_text(ass_text, encoding="utf-8")
    ctx.write_json(f"captions/{item}.json", {
        "k": spec.k, "style": style, "hook_s": float(hook["seconds"]) if hook else 0.0,
        "body_offset_s": offset, "audio_s": round(total, 3), "ass_opts": opts.to_json(),
        "words": [w.to_json() for w in words],
    })

    treatment = visual_treatment(spec.visual_treatment or ctx.combo.visual_treatment)
    beats = short_beats(plan, spec, fallback_beat(ctx))
    spans = beat_spans(beats, words, total)
    slots = plan_slots(spans, shot_range(treatment.cut_range_s, fmt))
    seed = clip_seed(ctx, item)
    shots = assign_clips(
        beats=beats, slots=slots, searcher=searcher, cache=cache, blocked=blocked, seed=seed,
        wanted=footage_orientation(fmt), used=used, extra_keywords=ctx.channel.seed_keywords,
    )
    luma = fit_caption_contrast(
        words=words, style=style, opts=opts, shots=shots, total_s=total, size=canvas, ass_path=ass,
        log=ctx.log,
    )
    out = render_video(
        shots=shots, audio=audio, out=out_dir / "short.mp4", size=canvas, treatment=treatment,
        video=video_for_format(ctx.runtime.video, fmt), ass_path=ass, fonts_dir=FONTS_DIR, log=ctx.log,
        fmt=fmt,
    )
    keys = [k for k in ("long", f"h{spec.k}", f"x{spec.k}") if k in captions_report]
    vals = [captions_report[k].get("drift_ms") for k in keys]
    drift = None if any(v is None for v in vals) else max(vals, default=0.0)
    report = {
        "path": str(out), "audio": str(audio), "duration_s": round(ff.duration_s(out), 3),
        "shots": len(shots),
        "encoder": ctx.runtime.video.encoder, "drift_ms": drift,
        "first_caption_s": first_event_start(ass_text), "backdrop_luma": luma,
        "format": fmt.id, "canvas": list(canvas),
    }
    return report, {"total_s": total, "treatment": treatment.name, "shots": [s.to_json() for s in shots]}


class ShortsDeriveStage:
    """Stage `shorts_derive` (S1 + S2): one fresh vertical Short per selected moment.

    Writes `shorts/s<k>/{audio.wav,short.mp4}`, `captions/s<k>.{ass,json}`, `clips/shorts_shots.json` and
    `render/report.json` (per Short, with the caption drift QC reads).
    """

    name: str = "shorts_derive"
    tenant: Tenant = None
    inputs: tuple[str, ...] = (
        "shorts_timing.json", "visual_plan.json", "captions/report.json", "short_combos.json",
    )
    outputs: tuple[str, ...] = ("render/report.json",)

    def run(self, ctx: JobContext) -> None:
        """Derive every selected Short."""
        timings = {int(t["k"]): t for t in ctx.read_json("shorts_timing.json")}
        manifest = ctx.read_json("audio/manifest.json")
        captions_report = ctx.read_json("captions/report.json")
        plan = ctx.read_json("visual_plan.json")
        cache = ClipCache(ctx.db, ctx.paths, ctx.services.clip_sources, log=ctx.log)
        searcher = Searcher(ctx.services.clip_sources, log=ctx.log, lemma=make_lemmatizer(ctx.paths))
        blocked = blocked_for_job(ctx, ("clips/shorts_shots.json",))
        used: set[tuple[str, str]] = set()  # one set for all Shorts: siblings never share a clip (NS-E611)
        report: dict[str, Any] = {}
        shots_out: dict[str, Any] = {}
        for spec in load_shorts(ctx):
            item = f"s{spec.k}"
            report[item], shots_out[item] = derive_short(
                ctx, spec, timings[spec.k], plan, manifest, captions_report, searcher, cache, blocked, used
            )
            ctx.log.info("render_done", job_id=ctx.job_id, item=item, duration_s=report[item]["duration_s"],
                         shots=report[item]["shots"])
        ctx.write_json("clips/shorts_shots.json", shots_out)
        ctx.write_json("render/report.json", report)
