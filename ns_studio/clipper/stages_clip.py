"""C8 Clipper: the clip job stages, registered as `FORMAT_STAGES["clip"]` (spec 19.2).

import -> audio -> analyze -> transcribe (whisper) -> select + judge (llama) -> layout -> captions -> render
-> seo (llama) -> qc -> outbox. Job parameters live in `<job_dir>/clip_params.json` (`ClipParams`).
Every stage is idempotent through the C7 stage runner (inputs hash + outputs exist).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Collection
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ns_common import ffmpeg
from ns_common.config import load_runtime
from ns_common.db import Db, new_job_id
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.models import Channel, Combo, PolicyAdvice, PolicyCheck, PolicyResult, Runtime
from ns_common.paths import Paths
from ns_studio.clipper import analyze, detect, layout, render, select, seo, source, transcribe_stage
from ns_studio.clipper.models import (
    CANVAS_H,
    Analysis,
    ClipItem,
    ClipLayout,
    ClipMetadata,
    ClipParams,
    Crop,
    Transcript,
    Word,
)
from ns_studio.context import FAKEABLE, JobContext, build_services
from ns_studio.media import captions as media_captions
from ns_studio.media import outbox as media_outbox
from ns_studio.media import qc as media_qc
from ns_studio.media import transcribe as media_transcribe
from ns_studio.orchestrator import FORMAT_STAGES
from ns_studio.stages import Stage, Tenant, register
from ns_studio.text.prompts import call_json

CAPTION_STYLE_FALLBACK = "clean_white"


# ---------- job directory helpers ----------
def _params(ctx: JobContext) -> ClipParams:
    return ClipParams.load(ctx.job_dir)


def _source(ctx: JobContext) -> dict[str, Any]:
    info: dict[str, Any] = ctx.read_json("source.json")
    return info


def _plan(ctx: JobContext) -> list[ClipItem]:
    items = [ClipItem.model_validate(d) for d in ctx.read_json("clip_plan.json")]
    return [i for i in items if i.status == "kept"]


def _transcript(ctx: JobContext) -> Transcript:
    return Transcript.model_validate(ctx.read_json("transcript.json"))


def _layout(ctx: JobContext) -> ClipLayout:
    return ClipLayout.model_validate(ctx.read_json("clip_layout.json"))


def default_combo(channel: Channel) -> Combo:
    """A combo for clip jobs (no variation engine): first option of each of the channel's pools."""
    p = channel.pools
    return Combo(
        combo_id="clip",
        hook=p.hooks[0],
        structure=p.structures[0],
        pov_tone=p.pov_tones[0],
        pacing=p.pacings[0],
        caption_style=p.caption_styles[0] if p.caption_styles else CAPTION_STYLE_FALLBACK,
        visual_treatment=p.visual_treatments[0],
        title_formula=p.title_formulas[0],
    )


def create_clip_job(
    paths: Paths,
    channel: Channel,
    params: ClipParams,
    *,
    fake: Collection[str] = (),
    runtime: Runtime | None = None,
    llm_fixtures: Path | None = None,
    job_id: str | None = None,
    keys: dict[str, str] | None = None,
) -> JobContext:
    """Create the job directory, write `clip_params.json` and return a ready `JobContext` (format "clip")."""
    paths.ensure()
    rt = runtime or (Runtime.default("cpu-dev") if set(fake) >= FAKEABLE else load_runtime(paths))
    services = build_services(rt, fake=set(fake), paths=paths, keys=keys, llm_fixtures=llm_fixtures)
    db = Db(paths.db_path)
    db.migrate()
    db.execute(
        "INSERT OR IGNORE INTO channels(slug, display_name, created_at) VALUES (?,?,?)",
        (channel.slug, channel.display_name, datetime.now(UTC).isoformat(timespec="seconds")),
    )
    jid = job_id or new_job_id(channel.slug)
    job_dir = paths.job_dir(jid)
    job_dir.mkdir(parents=True, exist_ok=True)
    params.save(job_dir)
    return JobContext(
        job_id=jid,
        channel=channel,
        combo=default_combo(channel),
        fmt="clip",  # type: ignore[arg-type]
        paths=paths,
        job_dir=job_dir,
        db=db,
        runtime=rt,
        services=services,
        log=NsLogger("studio", paths.logs, echo=False),
        prompt_versions={},
    )


# ---------- C1 import ----------
def _same_origin(prev: Any, source_ref: str) -> bool:
    """True when `source.json` was written for `source_ref` (same URL, or same local path)."""
    origin = prev.get("origin")
    if not origin:
        return False
    if source.is_url(source_ref):
        return bool(origin == source_ref)
    return Path(str(origin)) == Path(source_ref)


class ImportStage:
    """Copy the video (or download it from a URL), probe, hash, record the `sources` row.

    A URL source runs the pinned yt-dlp into `imports/clipper/_downloads/<id>/`, then goes through the same
    import; the video title becomes `sources.title` and the display name of the source.
    """

    name: str = "clip_import"
    tenant: Tenant = None
    inputs: tuple[str, ...] = ("clip_params.json",)
    outputs: tuple[str, ...] = ("source.json",)

    def run(self, ctx: JobContext) -> None:
        """Import the source (NS-E405 for unsupported files or unconfirmed rights, NS-E409 for downloads)."""
        p = _params(ctx)
        if not p.rights_confirmed:
            raise NsError("NS-E405", "the owner has not confirmed rights to this video")
        existing = ctx.file("source.json")
        if existing.exists():
            prev = json.loads(existing.read_text(encoding="utf-8"))
            if _same_origin(prev, p.source) and Path(prev["path"]).exists():
                return  # re-run with new options: keep source.json byte-identical so nothing re-transcribes
        title: str | None = None
        dl_dir: Path | None = None
        if source.is_url(p.source):
            dl_dir = ctx.paths.home / "imports" / "clipper" / "_downloads" / f"dl-{_short_hash(p.source)}"
            last = {"bucket": -1}

            def progress(pct: float) -> None:
                bucket = int(pct // 10)
                if bucket != last["bucket"]:
                    last["bucket"] = bucket
                    ctx.log.info("download_progress", job_id=ctx.job_id, percent=round(pct, 1))

            video = source.import_url(p.source, dl_dir, progress)
            title = source.read_download_title(dl_dir)
        else:
            video = Path(p.source)
        info = source.import_source(
            ctx.paths,
            ctx.db,
            video,
            channel=ctx.channel.slug,
            rights_confirmed=True,
            template=p.template,
            link=dl_dir is not None,
            title=title,
            original_name=f"{title}{video.suffix}" if title else None,
            origin=p.source,
        )
        if dl_dir is not None:
            shutil.rmtree(dl_dir, ignore_errors=True)
        ctx.write_json("source.json", asdict(info))


def _short_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:10]


# ---------- C2 audio ----------
class AudioStage:
    """Extract `audio.wav` (16 kHz mono)."""

    name: str = "clip_audio"
    tenant: Tenant = None
    inputs: tuple[str, ...] = ("source.json",)
    outputs: tuple[str, ...] = ("audio.wav",)

    def run(self, ctx: JobContext) -> None:
        """Run FFmpeg on the imported source."""
        source.extract_audio(Path(_source(ctx)["path"]), ctx.file("audio.wav"))


# ---------- C3 transcribe ----------
class TranscribeStage:
    """Transcribe `audio.wav` into `transcript.json` (sidecar in fake mode)."""

    name: str = "clip_transcribe"
    tenant: Tenant = "whisper"
    inputs: tuple[str, ...] = ("source.json", "audio.wav")
    outputs: tuple[str, ...] = ("transcript.json",)

    def run(self, ctx: JobContext) -> None:
        """Write the transcript with speaker indexes; NS-E406 when it has fewer than 30 words."""
        info = _source(ctx)
        sidecar = Path(info["sidecar"]) if info.get("sidecar") else None
        tr = transcribe_stage.run_transcription(
            ctx.services.transcriber,
            ctx.file("audio.wav"),
            sidecar=sidecar,
            source_name=info["original_name"],
            duration_s=float(info["duration_s"]),
            language=ctx.channel.language,
        )
        ctx.write_json("transcript.json", tr.model_dump(mode="json"))
        source.set_source_status(ctx.db, info["source_id"], "transcribed")


# ---------- analyze (spec 23.5 step 1) ----------
def _analysis(ctx: JobContext) -> Analysis:
    return Analysis.model_validate(ctx.read_json("analysis.json"))


class AnalyzeStage:
    """FFmpeg and face analysis of the whole video: `analysis.json` (cuts, silences, loudness, faces)."""

    name: str = "clip_analyze"
    tenant: Tenant = None
    inputs: tuple[str, ...] = ("source.json", "audio.wav")
    outputs: tuple[str, ...] = ("analysis.json",)

    def run(self, ctx: JobContext) -> None:
        """Scene cuts, silences, loudness peaks, facecam scan and faces at 1 fps over the whole video."""
        info = _source(ctx)
        video, wav = Path(info["path"]), ctx.file("audio.wav")
        dur, w, h = float(info["duration_s"]), int(info["width"]), int(info["height"])
        cuts = analyze.scene_cuts(video)
        silences, lufs, lra = analyze.audio_events(wav, dur)
        model = ctx.paths.home / "models" / "cv" / "yunet.onnx"
        detector = detect.load_detector(model)
        if detector is None:
            ctx.log.warn("faces_skipped", "OpenCV or the YuNet model is missing", model=str(model))
        tmp = ctx.subdir("tmp_detect")
        try:
            res = detect.detect_video(video, dur, (w, h), tmp, detector=detector)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        result = Analysis(
            width=w,
            height=h,
            aspect=round(w / h, 3),
            duration_s=dur,
            fps=round(float(info["fps"]), 2),
            scene_cuts=cuts,
            scene_cuts_per_min=round(len(cuts) / max(dur / 60.0, 1e-6), 2),
            silences=silences,
            loudness=analyze.media_loudness(wav, lufs, lra),
            facecam=res.facecam,
            faces=res.faces,
            n_stable_faces=res.n_stable_faces,
            faces_detected=res.faces_detected,
        )
        ctx.write_json("analysis.json", result.model_dump(mode="json"))
        source.set_source_status(ctx.db, info["source_id"], "analyzed")


# ---------- C4 select + judge ----------
def edge_sentences(words: list[Word]) -> tuple[str, str]:
    """First and last sentence text of a clip's words (sentences end at terminal punctuation or a pause)."""
    spans = select.sentence_spans(words)
    if not spans:
        return "", ""
    first_end, last_start = spans[0][1], spans[-1][0]
    first = " ".join(w.w for w in words if w.end <= first_end + 1e-6)
    last = " ".join(w.w for w in words if w.start >= last_start - 1e-6)
    return first, last


class SelectStage:
    """Pick and judge the clips: `clip_plan.json` lists kept clips (k in score order) and dropped ones.

    The model sees transcript windows and the analysis and proposes candidates with a layout each. Candidates
    are processed in score order; `clip_judge` checks each one before it can enter the plan, failures stay in
    the file with their reasons, until `max_clips` clips pass (NS-E407 when none do). Raw answers are cached
    in `clip_candidates.json`, so re-running with new options does not repeat the window calls.
    """

    name: str = "clip_select"
    tenant: Tenant = "llama"
    inputs: tuple[str, ...] = ("transcript.json", "analysis.json", "clip_params.json")
    outputs: tuple[str, ...] = ("clip_plan.json",)

    def run(self, ctx: JobContext) -> None:
        """Window, ask, merge, snap, filter, then judge in score order."""
        tr, p, an = _transcript(ctx), _params(ctx), _analysis(ctx)
        cache: dict[str, Any] = (
            json.loads(ctx.file("clip_candidates.json").read_text(encoding="utf-8"))
            if ctx.file("clip_candidates.json").exists()
            else {}
        )

        def ask(ws: float, we: float) -> dict[str, Any]:
            return call_json(
                ctx,
                "clip_select",
                segments=select.window_segments(tr, ws, we),
                window_start_s=round(ws, 1),
                window_end_s=round(we, 1),
                clip_min_s=p.min_s,
                clip_max_s=p.max_s,
                max_candidates=10,
                video_kind=p.video_kind if p.template == "auto" else p.template,
                analysis=analyze.prompt_view(an, ws, we),
            )

        judged = {"n": 0}

        def judge(c: select.Cand) -> tuple[bool, list[str]]:
            judged["n"] += 1
            words = select.words_in(tr.words, c.start, c.end)
            first, last = edge_sentences(words)
            raw = call_json(
                ctx,
                "clip_judge",
                clip={
                    "k": judged["n"],
                    "title": c.title,
                    "start_s": round(c.start, 1),
                    "end_s": round(c.end, 1),
                    "duration_s": round(c.duration, 1),
                },
                clip_min_s=p.min_s,
                clip_max_s=p.max_s,
                words_per_s=round(len(words) / max(c.duration, 1e-6), 2),
                snapped=True,
                first_sentence=first,
                last_sentence=last,
                clip_text=" ".join(w.w for w in words)[:3000],
            )
            return bool(raw.get("ok")), [str(r) for r in raw.get("reasons", [])]

        try:
            items = select.select_plan(tr, p, ask, cache, judge)
        finally:
            ctx.write_json("clip_candidates.json", cache)
        ctx.write_json("clip_plan.json", [c.model_dump(mode="json") for c in items])
        select.ensure_some_kept(items)


# ---------- C5 layout per clip ----------
def template_of(lay: ClipLayout, k: int) -> str:
    """Template used for clip `k` (falls back to the job-level template)."""
    return lay.per_clip.get(str(k), lay.template)


class LayoutStage:
    """Choose each clip's template (model layout, else the section 19.4 rule) and plan its crops."""

    name: str = "clip_layout"
    tenant: Tenant = None
    inputs: tuple[str, ...] = ("analysis.json", "clip_plan.json", "clip_params.json")
    outputs: tuple[str, ...] = ("clip_layout.json",)

    def run(self, ctx: JobContext) -> None:
        """Write `clip_layout.json`; a layout that does not apply falls back with NS-W402."""
        an, plan, p = _analysis(ctx), _plan(ctx), _params(ctx)
        w, h = an.width, an.height
        base = ClipLayout(template="center_crop", facecam=an.facecam, faces=an.faces)
        per_clip: dict[str, str] = {}
        notes: dict[str, str] = {}
        crops: list[Crop] = []
        for clip in plan:
            template, warn = detect.decide_clip_template(
                p.template, clip.layout, an.facecam, an.n_stable_faces
            )
            wanted = clip.layout or p.template
            note = f"{warn}: {wanted} does not apply here, used {template}" if warn else None
            try:
                got = layout.plan_layout(template, w, h, base, t_start=clip.start_s, t_end=clip.end_s)
            except ValueError as e:
                template, note = "center_crop", f"NS-W402: {e}; used center_crop"
                got = layout.plan_layout(template, w, h, base, t_start=clip.start_s, t_end=clip.end_s)
            crops += got
            per_clip[str(clip.k)] = template
            if note:
                notes[str(clip.k)] = note
                ctx.log.warn("template_fallback", note, code="NS-W402", job_id=ctx.job_id, k=clip.k)
        spans = [(c.start_s - 1.0, c.end_s + 1.0) for c in plan]
        faces = [f for f in an.faces if any(a <= f.t <= b for a, b in spans)]
        used = list(per_clip.values())
        main = max(set(used), key=used.count)
        out = ClipLayout(
            template=main,
            facecam=an.facecam,
            faces=faces,
            crops=crops,
            note="; ".join(notes.values()) or None,
            per_clip=per_clip,
            notes=notes,
        )
        ctx.write_json("clip_layout.json", out.model_dump(mode="json"))
        info = _source(ctx)
        source.set_source_status(ctx.db, info["source_id"], "laid_out", main)


# ---------- C6 captions ----------
def place_captions(ass: str, y_pct: float, canvas_h: int = CANVAS_H) -> str:
    """Move the bottom-centre caption so its text is centred on `y_pct` of the canvas height."""
    lines = ass.split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith("Style: Default,"):
            fields = ln.split(",")
            try:
                size = float(fields[2])
            except (ValueError, IndexError):
                return ass
            fields[-2] = str(max(20, round(canvas_h * (1 - y_pct / 100.0) - 0.6 * size)))
            lines[i] = ",".join(fields)
            break
    return "\n".join(lines)


def _crop_at(lay: ClipLayout, t: float) -> Any:
    for c in lay.crops:
        if c.t_start - 1e-6 <= t < c.t_end:
            return c
    return lay.crops[0]


class CaptionsStage:
    """ASS captions per clip from the transcript words (2-4 words on screen, active-word highlight)."""

    name: str = "clip_captions"
    tenant: Tenant = None
    inputs: tuple[str, ...] = ("transcript.json", "clip_plan.json", "clip_layout.json")
    outputs: tuple[str, ...] = ("captions/report.json",)

    def run(self, ctx: JobContext) -> None:
        """Build `captions/c<k>.ass` with C6's builder, anchored per template."""
        caps, tmod = media_captions, media_transcribe
        tr, plan, lay = _transcript(ctx), _plan(ctx), _layout(ctx)
        templates = layout.load_templates()
        ctx.subdir("captions")
        report: dict[str, Any] = {}
        for clip in plan:
            dur = clip.end_s - clip.start_s
            words = [
                tmod.Word(w.w, max(0.0, w.start - clip.start_s), min(dur, max(0.0, w.end - clip.start_s)), 0)
                for w in select.words_in(tr.words, clip.start_s, clip.end_s)
            ]
            style = (
                clip.caption_style
                if clip.caption_style in ctx.channel.pools.caption_styles
                else ctx.combo.caption_style
            )
            ass = caps.build_ass(words, style, variant="vertical")
            tpl = templates[template_of(lay, clip.k)]
            ass = place_captions(ass, layout.caption_y_pct(tpl, _crop_at(lay, clip.start_s)))
            ctx.file(f"captions/c{clip.k}.ass").write_text(ass, encoding="utf-8")
            report[f"c{clip.k}"] = {
                "ass": f"captions/c{clip.k}.ass",
                "words": len(words),
                "style": style,
                "first_caption_s": caps.first_event_start(ass),
                "drift_ms": 0.0,  # captions are the ASR words themselves
            }
        ctx.write_json("captions/report.json", report)


# ---------- C7 render ----------
class RenderStage:
    """Cut and render each clip with its template graph: `clips/c<k>.mp4`."""

    name: str = "clip_render"
    tenant: Tenant = None
    inputs: tuple[str, ...] = ("clip_plan.json", "clip_layout.json", "captions/report.json", "source.json")
    outputs: tuple[str, ...] = ("render/report.json",)

    def run(self, ctx: JobContext) -> None:
        """Render every clip and write `render/report.json`."""
        info, plan, lay = _source(ctx), _plan(ctx), _layout(ctx)
        templates = layout.load_templates()
        ctx.subdir("clips")
        report: dict[str, Any] = {}
        for clip in plan:
            out = ctx.file(f"clips/c{clip.k}.mp4")
            spec = render.RenderSpec(
                video=Path(info["path"]),
                out=out,
                start=clip.start_s,
                end=clip.end_s,
                template=templates[template_of(lay, clip.k)],
                crops=lay.crops,
                src_w=int(info["width"]),
                src_h=int(info["height"]),
                ass=ctx.file(f"captions/c{clip.k}.ass"),
                encoder=ctx.runtime.video.encoder,
                preset=ctx.runtime.video.preset,
            )
            render.render_clip(spec, ctx.log)
            report[f"c{clip.k}"] = {
                "path": str(out),
                "duration_s": round(ffmpeg.duration_s(out), 3),
                "template": template_of(lay, clip.k),
            }
        ctx.write_json("render/report.json", report)


# ---------- C8 seo ----------
class SeoStage:
    """Clip titles, descriptions and tags (`seo_clips.json`) with metadata-only policy checks."""

    name: str = "clip_seo"
    tenant: Tenant = "llama"
    inputs: tuple[str, ...] = ("clip_plan.json", "transcript.json", "source.json")
    outputs: tuple[str, ...] = ("seo_clips.json", "policy_text.json")

    def run(self, ctx: JobContext) -> None:
        """Generate, normalise and check the metadata of every clip (NS-E602/E603/E606 on failure)."""
        tr, plan, info = _transcript(ctx), _plan(ctx), _source(ctx)
        sheets: list[dict[str, Any]] = []
        merged: dict[str, list[tuple[bool, str]]] = {}
        for clip in plan:
            words = select.words_in(tr.words, clip.start_s, clip.end_s)
            excerpt = " ".join(w.w for w in words)[:1500]
            result, policy = seo.generate_seo(ctx, clip, excerpt=excerpt, source_title=str(info["title"]))
            sheets.append({"k": clip.k, **result.model_dump(mode="json")})
            for name, chk in policy.checks.items():
                detail = f"c{clip.k}: {chk.detail}" if chk.detail else ""
                merged.setdefault(name, []).append((chk.passed, detail))
        checks = {
            name: PolicyCheck(passed=all(ok for ok, _ in rows), detail="; ".join(d for _, d in rows if d))
            for name, rows in merged.items()
        }
        policy_all = PolicyResult(
            stage="text",
            passed=all(c.passed for c in checks.values()),
            checks=checks,
            advice=PolicyAdvice(ai_disclosure="no", made_for_kids=ctx.channel.made_for_kids),
        )
        ctx.write_json("seo_clips.json", sheets)
        ctx.write_json("policy_text.json", policy_all.model_dump(mode="json"))


# ---------- C9 qc + outbox ----------
class QcStage:
    """Run the spec 7.7 Short checks (C6) on every clip; NS-E404 when any fails."""

    name: str = "clip_qc"
    tenant: Tenant = None
    inputs: tuple[str, ...] = ("render/report.json", "captions/report.json")
    outputs: tuple[str, ...] = ("qc/report.json",)

    def run(self, ctx: JobContext) -> None:
        """Write `qc/c<k>.json` and `qc/report.json` before raising on failures."""
        qc = media_qc
        rendered: dict[str, Any] = ctx.read_json("render/report.json")
        captions: dict[str, Any] = ctx.read_json("captions/report.json")
        summary: dict[str, Any] = {}
        failed: list[str] = []
        for item, rinfo in rendered.items():
            cap = captions.get(item, {})
            result = qc.run_qc(
                item,
                Path(rinfo["path"]),
                "short",
                bucket_s=None,
                drift_ms=cap.get("drift_ms"),
                first_caption_s=cap.get("first_caption_s"),
            )
            ctx.write_json(f"qc/{item}.json", result.model_dump(mode="json"))
            summary[item] = {"passed": result.passed, "reason": result.reason}
            if not result.passed:
                failed.append(f"{item}: {result.reason}")
        ctx.write_json("qc/report.json", summary)
        if failed:
            raise NsError("NS-E404", "; ".join(failed))


def clip_sheet_text(meta: ClipMetadata, video_name: str) -> str:
    """Plain-text upload sheet of a clip."""
    yes_no = {True: "Yes", False: "No"}
    return (
        "\n".join(
            [
                f"FILE: {video_name}",
                f"TITLE: {meta.title}",
                "",
                "DESCRIPTION:",
                meta.description.rstrip(),
                "",
                " ".join(meta.hashtags),
                "",
                "TAGS: " + ", ".join(meta.tags),
                "",
                f"PUBLISH AFTER (local time): {meta.publish_after.replace('T', ' ')}",
                "",
                "SETTINGS:",
                f"  Altered or synthetic content: {meta.ai_disclosure.capitalize()}",
                f"  Made for kids: {yes_no[meta.made_for_kids]}",
                f"  Age restriction suggested: {yes_no[meta.age_restrict_suggested]}",
                f"  Source video: {meta.source_video} (template {meta.template})",
            ]
        )
        + "\n"
    )


_RESERVED_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)


def _local_safe_slug(text: str, max_len: int = 80) -> str:
    """Windows-safe file or folder name: no reserved characters or device names, at most 80 chars."""
    s = _RESERVED_CHARS.sub(" ", text)
    s = re.sub(r"\s+", " ", s).strip(" .")
    if s.lower() in _RESERVED_NAMES:
        s = f"{s}_"
    return s[:max_len].rstrip(" .")


def safe_slug(text: str, max_len: int = 80) -> str:
    """C6's `safe_slug` when `ns_studio.media.outbox` has one, else the local equivalent (spec 15.2a)."""
    fn = getattr(media_outbox, "safe_slug", None)
    out = str(fn(text)) if callable(fn) else _local_safe_slug(text, max_len)
    return out[:max_len].rstrip(" .")


def clip_folder(ctx: JobContext, source_id: str, original_name: str) -> Path:
    """`outbox/<channel>/clips/<source-video-name-slug>`; a different source with the same name gets `-2`."""
    base = ctx.paths.outbox / ctx.channel.slug / "clips"
    stem = safe_slug(Path(original_name).stem) or source_id
    cand, n = base / stem, 2
    while ctx.db.one(
        "SELECT 1 FROM outputs WHERE source_id != ? AND path LIKE ? LIMIT 1",
        (source_id, f"{cand}{os.sep}%"),
    ):
        cand = base / f"{stem}-{n}"
        n += 1
    return cand


def clip_file_stem(k: int, title: str) -> str:
    """`clip-01 <clip title>`: numbered in production order, at most 80 characters."""
    prefix = f"clip-{k:02d}"
    return f"{prefix} {safe_slug(title, 80 - len(prefix) - 1)}".strip()


class OutboxStage:
    """Copy clips to `outbox/<channel>/clips/<source name>/clip-01 <title>.{mp4,json,txt}` (spec 15.2a)."""

    name: str = "clip_outbox"
    tenant: Tenant = None
    inputs: tuple[str, ...] = ("qc/report.json", "seo_clips.json", "clip_plan.json")
    outputs: tuple[str, ...] = ("outbox/manifest.json",)

    def run(self, ctx: JobContext) -> None:
        """Publish every clip; `outputs.kind` is "clip" and `outputs.source_id` references the source."""
        ob = media_outbox
        info, lay = _source(ctx), _layout(ctx)
        rendered: dict[str, Any] = ctx.read_json("render/report.json")
        seos = {int(s["k"]): s for s in ctx.read_json("seo_clips.json")}
        advice = ctx.read_json("policy_text.json")["advice"]
        dest_dir = clip_folder(ctx, str(info["source_id"]), str(info["original_name"]))
        manifest: dict[str, Any] = {}
        now = datetime.now(UTC).isoformat()
        for item, rinfo in rendered.items():
            k = int(item[1:])
            s = seos[k]
            stem = clip_file_stem(k, s["title"])
            meta = ClipMetadata(
                channel=ctx.channel.slug,
                job_id=ctx.job_id,
                k=k,
                title=s["title"],
                description=s["description"],
                tags=s["tags"],
                hashtags=s["hashtags"],
                publish_after=ob.publish_after(
                    ctx.channel, ctx.job_id, item, day_offset=ob.short_day_offset(ctx.channel, k)
                ),
                source_video=str(info["original_name"]),
                template=template_of(lay, k),
                ai_disclosure=advice["ai_disclosure"],
                made_for_kids=bool(advice["made_for_kids"]),
                age_restrict_suggested=bool(advice["age_restrict_suggested"]),
                combo_id=ctx.combo.combo_id,
                prompt_versions=ctx.prompt_versions,
            )
            mp4 = ob.copy_if_changed(Path(rinfo["path"]), dest_dir / f"{stem}.mp4")
            json_path, txt_path = dest_dir / f"{stem}.json", dest_dir / f"{stem}.txt"
            json_path.write_text(meta.model_dump_json(indent=2) + "\n", encoding="utf-8")
            txt_path.write_text(clip_sheet_text(meta, mp4.name), encoding="utf-8")
            ctx.db.execute(
                "INSERT INTO outputs(output_id, job_id, channel, kind, k, title, path, duration_s, combo_id,"
                " status, source_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)"
                " ON CONFLICT(output_id) DO UPDATE SET title=excluded.title, path=excluded.path,"
                " duration_s=excluded.duration_s, source_id=excluded.source_id",
                (f"{ctx.job_id}/{item}", ctx.job_id, ctx.channel.slug, "clip", k, meta.title, str(mp4),
                 float(rinfo["duration_s"]), meta.combo_id, info["source_id"], now),
            )
            manifest[item] = {"mp4": str(mp4), "json": str(json_path), "txt": str(txt_path)}
        keep = {Path(p).name for v in manifest.values() for p in v.values()}
        for row in ctx.db.query("SELECT path FROM outputs WHERE job_id=? AND kind='clip'", (ctx.job_id,)):
            old = Path(row["path"])
            if old.name not in keep:
                for ext in (".mp4", ".json", ".txt"):
                    old.with_suffix(ext).unlink(missing_ok=True)
        ctx.db.execute(
            "DELETE FROM outputs WHERE job_id=? AND kind='clip' AND output_id NOT IN "
            f"({','.join('?' * len(manifest)) or 'NULL'})",
            (ctx.job_id, *[f"{ctx.job_id}/{i}" for i in manifest]),
        )
        ctx.write_json("outbox/manifest.json", manifest)
        source.set_source_status(ctx.db, info["source_id"], "done")
        ctx.log.info("clip_outbox_done", job_id=ctx.job_id, items=list(manifest))


# ---------- registry ----------
CLIP_STAGES: list[Stage] = [
    ImportStage(),
    AudioStage(),
    AnalyzeStage(),
    TranscribeStage(),
    SelectStage(),
    LayoutStage(),
    CaptionsStage(),
    RenderStage(),
    SeoStage(),
    QcStage(),
    OutboxStage(),
]


def register_clip_stages() -> None:
    """Add the clip stages to the C7 registry and set `FORMAT_STAGES["clip"]` (idempotent)."""
    for st in CLIP_STAGES:
        register(st)
    FORMAT_STAGES["clip"] = [st.name for st in CLIP_STAGES]


register_clip_stages()


def resume_clip_job(
    paths: Paths,
    job_id: str,
    channel: Channel,
    *,
    fake: Collection[str] = (),
    runtime: Runtime | None = None,
    llm_fixtures: Path | None = None,
) -> JobContext:
    """Rebuild the context of an existing clip job (e.g. to re-run selection with new options)."""
    params = ClipParams.load(paths.job_dir(job_id))
    return create_clip_job(
        paths, channel, params, fake=fake, runtime=runtime, llm_fixtures=llm_fixtures, job_id=job_id
    )
