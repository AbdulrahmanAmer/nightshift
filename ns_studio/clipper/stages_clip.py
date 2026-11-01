"""C8 Clipper: the clip job stages, registered as `FORMAT_STAGES["clip"]` (spec 19.2).

import -> audio -> transcribe (whisper) -> select (llama) -> detect -> captions -> render -> seo (llama)
-> qc -> outbox. Job parameters live in `<job_dir>/clip_params.json` (`ClipParams`). Every stage is
idempotent through the C7 stage runner (inputs hash + outputs exist).
"""
from __future__ import annotations

import json
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
from ns_studio.clipper import detect, layout, render, select, seo, source, transcribe_stage
from ns_studio.clipper.models import (
    CANVAS_H,
    ClipItem,
    ClipLayout,
    ClipMetadata,
    ClipParams,
    Transcript,
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
    return [ClipItem.model_validate(d) for d in ctx.read_json("clip_plan.json")]


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
class ImportStage:
    """Copy the video into `imports/clipper/<source_id>/`, probe it, hash it, record the `sources` row."""

    name: str = "clip_import"
    tenant: Tenant = None
    inputs: tuple[str, ...] = ("clip_params.json",)
    outputs: tuple[str, ...] = ("source.json",)

    def run(self, ctx: JobContext) -> None:
        """Import the source (NS-E405 for unsupported files or unconfirmed rights)."""
        p = _params(ctx)
        if not p.rights_confirmed:
            raise NsError("NS-E405", "the owner has not confirmed rights to this video")
        existing = ctx.file("source.json")
        if existing.exists():
            prev = json.loads(existing.read_text(encoding="utf-8"))
            if prev.get("original_name") == Path(p.source).name and Path(prev["path"]).exists():
                return  # re-run with new options: keep source.json byte-identical so nothing re-transcribes
        info = source.import_source(
            ctx.paths,
            ctx.db,
            Path(p.source),
            channel=ctx.channel.slug,
            rights_confirmed=True,
            template=p.template,
        )
        ctx.write_json("source.json", asdict(info))


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


# ---------- C4 select ----------
class SelectStage:
    """Pick the clips: `clip_plan.json` (raw LLM answers are cached in `clip_candidates.json`)."""

    name: str = "clip_select"
    tenant: Tenant = "llama"
    inputs: tuple[str, ...] = ("transcript.json", "clip_params.json")
    outputs: tuple[str, ...] = ("clip_plan.json",)

    def run(self, ctx: JobContext) -> None:
        """Window, ask, merge, snap, filter, rank; NS-E407 when nothing survives."""
        tr = _transcript(ctx)
        p = _params(ctx)
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
            )

        try:
            clips = select.select_clips(tr, p, ask, cache)
        finally:
            ctx.write_json("clip_candidates.json", cache)
        ctx.write_json("clip_plan.json", [c.model_dump(mode="json") for c in clips])


# ---------- C5 detect ----------
def plan_all(template: str, w: int, h: int, base: ClipLayout, plan: list[ClipItem]) -> list[Any]:
    """Crops of every clip for `template` (ValueError when the detection data cannot support it)."""
    crops: list[Any] = []
    for c in plan:
        crops += layout.plan_layout(template, w, h, base, t_start=c.start_s, t_end=c.end_s)
    return crops


class DetectStage:
    """Sample frames, find the facecam and stable faces, choose the template, plan per-clip crops."""

    name: str = "clip_detect"
    tenant: Tenant = None
    inputs: tuple[str, ...] = ("source.json", "clip_plan.json", "clip_params.json")
    outputs: tuple[str, ...] = ("clip_layout.json",)

    def run(self, ctx: JobContext) -> None:
        """Write `clip_layout.json`; a template that does not apply falls back to center_crop (NS-W402)."""
        info = _source(ctx)
        p = _params(ctx)
        plan = _plan(ctx)
        w, h = int(info["width"]), int(info["height"])
        model = ctx.paths.home / "models" / "cv" / "yunet.onnx"
        detector = detect.load_detector(model)
        if detector is None:
            ctx.log.warn("faces_skipped", "OpenCV or the YuNet model is missing", model=str(model))
        tmp = ctx.subdir("tmp_detect")
        try:
            res = detect.run_detection(
                Path(info["path"]),
                [(c.start_s, c.end_s) for c in plan],
                float(info["duration_s"]),
                (w, h),
                tmp,
                detector=detector,
                requested=p.template,
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        template, note = res.template, None
        if res.warning:
            note = f"{res.warning}: {p.template} does not apply here, using center_crop"
        base = ClipLayout(template=template, facecam=res.facecam, faces=res.faces)
        try:
            crops = plan_all(template, w, h, base, plan)
        except ValueError as e:
            template, note = "center_crop", f"NS-W402: {e}; using center_crop"
            base = base.model_copy(update={"template": template})
            crops = plan_all(template, w, h, base, plan)
        if note:
            ctx.log.warn("template_fallback", note, code="NS-W402", job_id=ctx.job_id)
        out = base.model_copy(update={"template": template, "crops": crops, "note": note})
        ctx.write_json("clip_layout.json", out.model_dump(mode="json"))
        source.set_source_status(ctx.db, info["source_id"], "detected", template)


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
        tpl = layout.load_templates()[lay.template]
        ctx.subdir("captions")
        report: dict[str, Any] = {}
        for clip in plan:
            dur = clip.end_s - clip.start_s
            words = [
                tmod.Word(w.w, max(0.0, w.start - clip.start_s), min(dur, max(0.0, w.end - clip.start_s)), 0)
                for w in select.words_in(tr.words, clip.start_s, clip.end_s)
            ]
            ass = caps.build_ass(words, ctx.combo.caption_style, variant="vertical")
            ass = place_captions(ass, layout.caption_y_pct(tpl, _crop_at(lay, clip.start_s)))
            ctx.file(f"captions/c{clip.k}.ass").write_text(ass, encoding="utf-8")
            report[f"c{clip.k}"] = {
                "ass": f"captions/c{clip.k}.ass",
                "words": len(words),
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
        tpl = layout.load_templates()[lay.template]
        ctx.subdir("clips")
        report: dict[str, Any] = {}
        for clip in plan:
            out = ctx.file(f"clips/c{clip.k}.mp4")
            spec = render.RenderSpec(
                video=Path(info["path"]),
                out=out,
                start=clip.start_s,
                end=clip.end_s,
                template=tpl,
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
                "template": lay.template,
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


class OutboxStage:
    """Copy clips to `outbox/<channel>/<date>/clip-<source_id>/c<k>.{mp4,json,txt}`; record `outputs` rows."""

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
        date = ob.job_date(ctx.job_id).strftime("%Y-%m-%d")
        dest_dir = ctx.paths.outbox_dir(ctx.channel.slug, date, f"clip-{info['source_id']}")
        manifest: dict[str, Any] = {}
        now = datetime.now(UTC).isoformat()
        for item, rinfo in rendered.items():
            k = int(item[1:])
            s = seos[k]
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
                template=lay.template,
                ai_disclosure=advice["ai_disclosure"],
                made_for_kids=bool(advice["made_for_kids"]),
                age_restrict_suggested=bool(advice["age_restrict_suggested"]),
                combo_id=ctx.combo.combo_id,
                prompt_versions=ctx.prompt_versions,
            )
            mp4 = ob.copy_if_changed(Path(rinfo["path"]), dest_dir / f"{item}.mp4")
            (dest_dir / f"{item}.json").write_text(meta.model_dump_json(indent=2) + "\n", encoding="utf-8")
            (dest_dir / f"{item}.txt").write_text(clip_sheet_text(meta, mp4.name), encoding="utf-8")
            ctx.db.execute(
                "INSERT INTO outputs(output_id, job_id, channel, kind, k, title, path, duration_s, combo_id,"
                " status, source_id, created_at) VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?)"
                " ON CONFLICT(output_id) DO UPDATE SET title=excluded.title, path=excluded.path,"
                " duration_s=excluded.duration_s, source_id=excluded.source_id",
                (f"{ctx.job_id}/{item}", ctx.job_id, ctx.channel.slug, "clip", k, meta.title, str(mp4),
                 float(rinfo["duration_s"]), meta.combo_id, info["source_id"], now),
            )
            manifest[item] = {"mp4": str(mp4), "json": str(dest_dir / f"{item}.json"),
                              "txt": str(dest_dir / f"{item}.txt")}
        keep = {f"{item}.{ext}" for item in manifest for ext in ("mp4", "json", "txt")}
        for stale in dest_dir.glob("c*.*"):
            if stale.name not in keep:
                stale.unlink(missing_ok=True)
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
    TranscribeStage(),
    SelectStage(),
    DetectStage(),
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
