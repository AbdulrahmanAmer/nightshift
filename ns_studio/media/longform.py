"""Master-first media, part 2: landscape clips, cards, master render, chapters, mid-rolls (C6, M2)."""

from __future__ import annotations

import dataclasses
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_studio.context import JobContext
from ns_studio.media.audio import assemble_audio, loudnorm
from ns_studio.media.caption_plan import (
    AssOptions,
    emphasis_from_beats,
    mask_profanity_setting,
    safe_zones_for,
)
from ns_studio.media.captions import _ass_color as ass_color
from ns_studio.media.captions import _ass_time as ass_time
from ns_studio.media.captions import build_ass, caption_band, ensure_contrast, shift_words
from ns_studio.media.clips import ClipCache, Searcher, assign_clips, blocked_for_job, clip_seed, fallback_beat
from ns_studio.media.master import (
    CARD_S,
    ChapterTiming,
    load_chapters,
    long_caption_style,
    load_words,
    timeline_from_manifest,
)
from ns_studio.media.presets import (
    FONTS_DIR,
    ChapterCard,
    VisualTreatment,
    caption_style,
    chapter_card,
    load_chapter_cards,
    visual_treatment,
)
from ns_studio.media.render import (
    FINAL_LUFS,
    FINAL_TP,
    LANDSCAPE,
    backdrop_luma,
    encoder_args,
    escape_filter_path,
    post_chain,
    shot_chain,
)
from ns_studio.media.shotplan import Shot, beat_spans, plan_slots
from ns_studio.stages import Tenant

MIDROLL_EDGE_S = 120.0


def fmt_timestamp(seconds: float) -> str:
    """`MM:SS`, or `H:MM:SS` from one hour up (YouTube chapter format)."""
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def chapters_json(timeline: Sequence[ChapterTiming], total_s: float) -> dict[str, Any]:
    """The `long/chapters.json` document: chapter starts (card start) and mid-roll suggestions."""
    return {
        "total_s": round(total_s, 3),
        "card_s": CARD_S,
        "chapters": [
            {"n": t.n, "title": t.title, "start_s": round(t.seg_start, 3),
             "audio_start_s": round(t.audio_start, 3),
             "end_s": round(t.end, 3), "timestamp": fmt_timestamp(t.seg_start)}
            for t in timeline
        ],
        "midroll_suggestions_s": midroll_suggestions(timeline, total_s),
    }


def midroll_suggestions(
    timeline: Sequence[ChapterTiming], total_s: float, edge_s: float = MIDROLL_EDGE_S
) -> list[float]:
    """Chapter breaks (starts of chapters 2..N) at least `edge_s` from both ends of the video."""
    return [round(t.seg_start, 3) for t in timeline[1:] if edge_s <= t.seg_start <= total_s - edge_s]


def pick_card(job_id: str) -> ChapterCard:
    """A chapter card style chosen deterministically from the job id."""
    names = sorted(load_chapter_cards())
    return chapter_card(names[zlib.crc32(job_id.encode()) % len(names)])


def card_ass(title: str, n: int, card: ChapterCard, duration_s: float = CARD_S) -> str:
    """ASS document for a title card (1920x1080): the title centred, an optional small chapter number."""
    fade = f"{{\\fad({card.fade_ms},{card.fade_ms})}}"
    header = [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 1920", "PlayResY: 1080", "WrapStyle: 0", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Title,Arial,{card.font_size},{ass_color(card.text)},{ass_color(card.text)},"
        f"{ass_color('#000000')},{ass_color('#000000')},-1,0,0,0,100,100,2,0,1,0,0,5,120,120,0,1",
        f"Style: Number,Arial,{card.number_size},{ass_color(card.accent)},{ass_color(card.accent)},"
        f"{ass_color('#000000')},{ass_color('#000000')},-1,0,0,0,100,100,6,0,1,0,0,5,120,120,0,1",
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    end = ass_time(duration_s)
    safe = title.replace("{", "(").replace("}", ")").replace("\n", " ")
    events = [f"Dialogue: 0,{ass_time(0)},{end},Title,,0,0,0,,{fade}{safe}"]
    if card.show_number:
        events.append(f"Dialogue: 0,{ass_time(0)},{end},Number,,0,0,170,,{fade}CHAPTER {n}")
    return "\n".join([*header, *events]) + "\n"


def _shift(s: Shot, dt: float) -> Shot:
    return dataclasses.replace(s, start=s.start + dt, end=s.end + dt)


def run_clips_master(ctx: JobContext) -> None:
    """Clips branch for master-first jobs: landscape shots of 5-9 s per beat, chapter by chapter.

    Writes `clips/shots.json` = {"long": {"total_s", "treatment", "shots": [...]}} with master-timeline times.
    A clip is used once per video and clips used recently on the channel are skipped (spec §7.4).
    """
    chapters = load_chapters(ctx)
    timeline = timeline_from_manifest(ctx)
    plan = ctx.read_json("visual_plan.json")
    treatment = visual_treatment(ctx.combo.visual_treatment)
    cache = ClipCache(ctx.db, ctx.paths, ctx.services.clip_sources, log=ctx.log)
    cache.purge_expired()
    searcher = Searcher(ctx.services.clip_sources, log=ctx.log)
    blocked = blocked_for_job(ctx, ("clips/shots.json",))
    used: set[tuple[str, str]] = set()
    shots: list[Shot] = []
    for ch, t in zip(chapters, timeline, strict=True):
        beats = [b for b in plan if b.get("chapter") == ch.n] or [fallback_beat(ctx)]
        words = load_words(ctx, f"c{ch.n}")
        spans = beat_spans(beats, words, t.end - t.audio_start)
        slots = plan_slots(spans, treatment.long_cut_range_s)
        seed = clip_seed(ctx, f"long|{ch.n}")
        got = assign_clips(
            beats=beats, slots=slots, searcher=searcher, cache=cache, blocked=blocked, seed=seed,
            wanted="landscape", used=used, allow_other=False,
        )
        shots += [_shift(s, t.audio_start) for s in got]
        ctx.log.info("clips_selected", job_id=ctx.job_id, item="long", chapter=ch.n, shots=len(got))
    ctx.write_json("clips/shots.json", {"long": {
        "total_s": timeline[-1].end, "treatment": treatment.name, "shots": [s.to_json() for s in shots],
    }})


# ---------------------------------------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------------------------------------


def segment_edges(shots: Sequence[Shot], audio_start_f: int, content_frames: int, fps: int) -> list[int]:
    """Local cumulative frame edges of a chapter's shots; the last edge is exactly `content_frames`."""
    edges = [0]
    for i, s in enumerate(shots):
        e = round(s.end * fps) - audio_start_f
        if i == len(shots) - 1:
            e = content_frames
        edges.append(max(edges[-1] + 1, e))
    return edges


def build_segment_graph(
    shots: Sequence[Shot],
    edges: Sequence[int],
    card_frames: int,
    fps: int,
    treatment: VisualTreatment,
    chapter_ass: Path | None,
    card_ass_path: Path | None,
    fonts_dir: Path | None = None,
) -> str:
    """Filter graph of one chapter segment: optional title card, then its shots, then colour and captions.

    With a card, input 0 is the colour source and shot k is input k+1.
    """
    w, h = LANDSCAPE
    off = 1 if card_frames > 0 else 0
    chains = [
        shot_chain(i + off, s, w, h, fps, edges[i + 1] - edges[i], treatment.zoom)
        for i, s in enumerate(shots)
    ]
    labels = "".join(f"[v{i + off}]" for i in range(len(shots)))
    chains.append(f"{labels}concat=n={len(shots)}:v=1:a=0[cat]")
    chains.append(f"[cat]{post_chain(treatment, None, None)}[vs]")  # colour/grain never touch the card
    last = "[vs]"
    if card_frames > 0:
        sub = f",ass=filename={escape_filter_path(card_ass_path)}" if card_ass_path else ""
        chains.insert(
            0,
            f"[0:v]format=yuv420p{sub},trim=end_frame={card_frames},setpts=PTS-STARTPTS,"
            f"setsar=1,fps={fps}[vc]",
        )
        chains.append("[vc][vs]concat=n=2:v=1:a=0[cc]")
        last = "[cc]"
    if chapter_ass is not None:
        sub = f"ass=filename={escape_filter_path(chapter_ass)}"
        if fonts_dir is not None and fonts_dir.is_dir():
            sub += f":fontsdir={escape_filter_path(fonts_dir)}"
        chains.append(f"{last}{sub},format=yuv420p[vout]")
    else:
        chains.append(f"{last}format=yuv420p[vout]")
    return ";".join(chains)


def fit_chapter_contrast(ctx: JobContext, t: ChapterTiming, shots: Sequence[Shot]) -> list[float]:
    """Rewrite a chapter's caption file in the boxed variant when the footage behind it is too bright (17.2).

    The captions stage built `captions/long_c<n>.ass` without looking at the footage; here the clips are known.
    Returns the luma samples (empty when sampling failed: best effort, a render never fails on it).
    """
    plan_file = ctx.file("visual_plan.json")
    beats: list[dict[str, Any]] = ctx.read_json("visual_plan.json") if plan_file.exists() else []
    style = long_caption_style(ctx)
    st = caption_style(style)
    opts = AssOptions(
        variant="landscape", safe_zones=safe_zones_for("landscape"),
        mask_profanity=mask_profanity_setting(ctx.channel), emphasis=emphasis_from_beats(beats, chapter=t.n),
    )
    band = caption_band(st, opts.variant, safe_zones=opts.safe_zones)
    local_shots = [_shift(s, -t.seg_start) for s in shots]
    try:
        luma = backdrop_luma(local_shots, band, LANDSCAPE, t.end - t.seg_start)
    except (NsError, OSError) as e:
        ctx.log.warn("caption_contrast_skipped", str(e)[:200])
        return []
    if luma and ensure_contrast(st, luma) is not st:
        words = shift_words(load_words(ctx, f"c{t.n}"), t.audio_start - t.seg_start)
        ctx.file(f"captions/long_c{t.n}.ass").write_text(
            build_ass(words, style, first_at_zero=t.audio_start == t.seg_start, backdrop_luma=luma,
                      **opts.kwargs()),
            encoding="utf-8",
        )
    return luma


def render_segment(
    ctx: JobContext,
    t: ChapterTiming,
    shots: Sequence[Shot],
    treatment: VisualTreatment,
    card: ChapterCard,
    out: Path,
) -> Path:
    """Render one chapter (card + shots, captions burned in) to a video-only mp4."""
    fps = ctx.runtime.video.fps
    seg_f, aud_f, end_f = round(t.seg_start * fps), round(t.audio_start * fps), round(t.end * fps)
    card_frames, content = aud_f - seg_f, end_f - aud_f
    if not shots:
        raise NsError("NS-E401", f"chapter {t.n} has no shots")
    edges = segment_edges(shots, aud_f, content, fps)
    fit_chapter_contrast(ctx, t, shots)
    card_path = None
    inputs: list[str] = []
    if card_frames > 0:
        card_path = ctx.file(f"captions/card_c{t.n}.ass")
        card_path.write_text(card_ass(t.title, t.n, card), encoding="utf-8")
        inputs += ["-f", "lavfi", "-t", f"{card_frames / fps + 0.5:.2f}", "-i",
                   f"color=c={card.background.replace('#', '0x')}:s=1920x1080:r={fps}"]
    for i, s in enumerate(shots):
        need = (edges[i + 1] - edges[i]) / fps + 0.5
        inputs += ["-ss", f"{s.ss:.2f}", "-t", f"{need:.2f}", "-i", s.path]
    graph = build_segment_graph(
        shots, edges, card_frames, fps, treatment, ctx.file(f"captions/long_c{t.n}.ass"), card_path, FONTS_DIR
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    video = ctx.runtime.video

    def attempt(force_cpu: bool) -> None:
        ff.run([*inputs, "-filter_complex", graph, "-map", "[vout]", "-an",
                *encoder_args(video, force_cpu=force_cpu), "-r", str(fps), "-pix_fmt", "yuv420p", str(out)],
               log=ctx.log)

    try:
        attempt(False)
    except NsError:
        if video.encoder != "h264_nvenc":
            raise
        ctx.log.warn("nvenc_failed", code="NS-W401", msg="falling back to libx264")
        attempt(True)
    return out


def concat_list_text(paths: Sequence[Path]) -> str:
    """Body of an FFmpeg concat-demuxer list file; a quote in a path is written close-escape-reopen."""
    quote = chr(39)
    escaped = quote + chr(92) + quote + quote
    return "".join(f"file {quote}{p.as_posix().replace(quote, escaped)}{quote}\n" for p in paths)


class RenderMasterStage:
    """Stage `render_master`: master audio (-14 LUFS), per-chapter video segments, concat, `chapters.json`.

    Segments are video-only; audio is encoded once from the normalized master audio, so there are no AAC
    seams. Chapter frame boundaries are rounded globally, so video and audio stay in sync to a frame.
    """

    name: str = "render_master"
    tenant: Tenant = None
    inputs: tuple[str, ...] = ("clips/shots.json", "captions/report.json")
    outputs: tuple[str, ...] = ("long/master.mp4", "long/chapters.json")

    def run(self, ctx: JobContext) -> None:
        """Render the whole master."""
        manifest = ctx.read_json("audio/manifest.json")
        chapters = load_chapters(ctx)
        timeline = timeline_from_manifest(ctx)
        entry = ctx.read_json("clips/shots.json")["long"]
        shots_all = [Shot.from_json(s) for s in entry["shots"]]
        treatment = visual_treatment(ctx.combo.visual_treatment)
        card = pick_card(ctx.job_id)
        out_dir = ctx.subdir("long")
        parts: list[Path | float] = []
        for i, ch in enumerate(chapters):
            if i > 0:
                parts.append(CARD_S)
            parts.append(Path(manifest[f"c{ch.n}"]["path"]))
        raw = assemble_audio(parts, out_dir / "audio_raw.wav")
        audio = out_dir / "audio.wav"
        loudnorm(raw, audio, i=FINAL_LUFS, tp=FINAL_TP)
        segs: list[Path] = []
        for t in timeline:
            mine = [s for s in shots_all if t.audio_start - 1e-6 <= s.start < t.end - 1e-6]
            segs.append(render_segment(ctx, t, mine, treatment, card, out_dir / f"seg_{t.n}.mp4"))
        lst = out_dir / "segments.txt"
        lst.write_text(concat_list_text(segs), encoding="utf-8")
        master = out_dir / "master.mp4"
        ff.run(["-f", "concat", "-safe", "0", "-i", str(lst), "-i", str(audio), "-map", "0:v", "-map", "1:a",
                "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "1",
                "-movflags", "+faststart", str(master)], log=ctx.log)
        total = timeline[-1].end
        ctx.write_json("long/chapters.json", chapters_json(timeline, total))
        for p in segs:
            p.unlink(missing_ok=True)
        ctx.write_json("render/long.json", {
            "path": str(master), "audio": str(audio), "duration_s": round(ff.duration_s(master), 3),
            "chapters": len(timeline), "shots": len(shots_all), "encoder": ctx.runtime.video.encoder,
        })
        ctx.log.info("render_done", job_id=ctx.job_id, item="long", duration_s=round(total, 2),
                     shots=len(shots_all))
