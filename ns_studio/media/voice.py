"""Voice stage: batch TTS, per-item loudness normalization, script sidecars (component C6)."""

from __future__ import annotations

import hashlib
import zlib
from pathlib import Path

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.languages import qwen_language
from ns_common.models import Voice
from ns_common.pacing import pacing_speed
from ns_common.textstats import split_sentences
from ns_common.tts_client import TtsFile, TtsItem
from ns_common.tts_timeout import TTS_TIMEOUT_CAP_S, batch_timeout_s, load_rtf
from ns_studio.context import JobContext
from ns_studio.media.audio import VOICE_LUFS, VOICE_TP, loudnorm, measure_lufs
from ns_studio.media.fake_voice import note_voice_engine
from ns_studio.stages import Tenant
from ns_studio.voice_wpm import record_voice_wpm

__all__ = ["VoiceStage", "loudnorm", "measure_lufs", "narration_items", "synthesize"]


def job_voice(ctx: JobContext) -> Voice:
    """The voice of the job: a series design's voice when the job is a series part, else the channel's."""
    design_voice = ctx.series.get("design", {}).get("voice")
    if design_voice:
        try:
            return Voice.model_validate(design_voice)
        except ValueError:
            pass  # a damaged design must not stop the part: use the channel voice
    return ctx.channel.voice


def job_speed(ctx: JobContext) -> float:
    """TTS speed of every item of the job: the voice's speed scaled to the combo's pacing (spec 7.0)."""
    return pacing_speed(job_voice(ctx).speed, ctx.combo.pacing)


def narration_items(
    parts: list[dict],
    voice_name: str,
    speed: float,
    language: str | None = None,
    closings: dict[str, str] | None = None,
) -> list[TtsItem]:
    """One TTS item per part body (`p<n>`) plus `p<n>_hook` when the hook is not the first sentence and
    `p<n>_close` for the part's closing line (`closings` maps the part number as text to the line).

    `language` is the channel's ISO code; it travels with every item to `qwen-tts --lang`.
    """
    items: list[TtsItem] = []
    for p in parts:
        sentences = split_sentences(p["text"])
        cliff = (sentences[-1],) if p.get("cliffhanger") and sentences else ()
        items.append(TtsItem(f"p{p['n']}", p["text"], voice_name, speed, cliff, language))
        hook = (p.get("hook") or "").strip()
        if hook and (not sentences or hook != sentences[0]):
            items.append(TtsItem(f"p{p['n']}_hook", hook, voice_name, speed, language=language))
        closing = ((closings or {}).get(str(p["n"])) or "").strip()
        if closing:
            items.append(TtsItem(f"p{p['n']}_close", closing, voice_name, speed, language=language))
    return items


def tts_seed(ctx: JobContext) -> int | None:
    """Seed sent with the batch: None on the first pass, so the TTS service uses the voice's own fixed seed
    and the timbre stays the same from job to job (spec 7.5). After the policy gate sent the job back
    (`ctx.attempt` > 0) a seed of that attempt, so the regenerated voice sounds different."""
    if ctx.attempt == 0:
        return None
    return (zlib.crc32(ctx.job_id.encode()) + ctx.attempt) & 0x7FFFFFFF

def _signature(it: TtsItem) -> str:
    base = f"{it.voice}|{it.speed}|{it.text}"
    if it.cliff_lines:  # unchanged for items without one, so earlier jobs keep their cache
        base += "|cliff:" + "|".join(it.cliff_lines)
    if qwen_language(it.language)[0] != "English":  # English items keep the signature (and cache) they had
        base += f"|lang:{qwen_language(it.language)[0]}"
    return hashlib.sha256(base.encode()).hexdigest()


def _speak(
    ctx: JobContext, todo: list[TtsItem], raw_dir: Path, seed: int | None, limit: float
) -> list[TtsFile]:
    """One batch; when the limit passes (NS-E310 after the client asked the service to cancel) one more try.

    The retry runs at twice the limit (at most 2 h) and `resume` makes the service keep the items it had
    already finished, so only the rest is spoken again. Anything else (a missing voice, a failed process)
    is not a matter of time and is raised at once.
    """
    try:
        return ctx.services.tts.speak_batch(todo, raw_dir, seed=seed, timeout_s=limit)
    except NsError as e:
        timed_out = bool(e.data.get("timed_out")) or "timed out" in e.detail  # the client's, or the service's
        if e.code != "NS-E310" or not timed_out:
            raise
        again = min(TTS_TIMEOUT_CAP_S, limit * 2)
        ctx.log.warn(
            "voice_batch_retry", str(e), job_id=ctx.job_id, items=len(todo), timeout_s=round(again)
        )
        return ctx.services.tts.speak_batch(todo, raw_dir, seed=seed, timeout_s=again)


def synthesize(ctx: JobContext, items: list[TtsItem]) -> tuple[dict[str, dict], int]:
    """Speak, normalize (-16 LUFS, peaks held for the final -14) and measure `items`; reuse unchanged files.

    Peaks stay under `VOICE_TP` so the Short and master stages lift by 2 dB without a second limiter.

    Writes `audio/<id>.wav` and the `audio/<id>.txt` script sidecar. Returns (manifest entries by id, number
    of items actually synthesized). Does not write `audio/manifest.json`.
    """
    manifest_path = ctx.file("audio/manifest.json")
    prev: dict[str, dict] = ctx.read_json("audio/manifest.json") if manifest_path.exists() else {}
    todo = [
        it
        for it in items
        if not (prev.get(it.id, {}).get("sig") == _signature(it) and Path(prev[it.id]["path"]).exists())
    ]
    todo_ids = {it.id for it in todo}
    raw_dir = ctx.subdir("audio/raw")
    manifest: dict[str, dict] = {it.id: prev[it.id] for it in items if it.id not in todo_ids}
    if todo:
        seed = tts_seed(ctx)
        rtf = load_rtf(ctx.paths)  # the speed `ns verify` measured on this PC (None: the default figures)
        limit = batch_timeout_s((it.text for it in todo), rtf)  # 120 s + 3 s or more per line, at most 2 h
        ctx.log.info("voice_batch", job_id=ctx.job_id, items=len(todo), timeout_s=round(limit), rtf=rtf)
        files = _speak(ctx, todo, raw_dir, seed, limit)
        note_voice_engine(ctx)  # an engine test: how the fake voice spoke (job log, NS-E403 detail)
        by_id = {it.id: it for it in todo}
        for f in files:
            out = ctx.file(f"audio/{f.id}.wav")
            # the one limiting of the narration: the Short and master stages only lift it linearly
            loudnorm(f.path, out, i=VOICE_LUFS, tp=VOICE_TP, log=ctx.log)
            lufs, tp = measure_lufs(out)
            out.with_suffix(".txt").write_text(by_id[f.id].text, encoding="utf-8")
            manifest[f.id] = {
                "path": str(out),
                "seconds": round(ff.duration_s(out), 3),
                "lufs": lufs,
                "tp": tp,
                "sig": _signature(by_id[f.id]),
            }
    if items:
        record_voice_wpm(ctx, items[0].voice, items, manifest)  # words per minute this voice really speaks
    return manifest, len(todo)


class VoiceStage:
    """Stage `voice` (tenant tts): spec §7.5, loudness -16 LUFS per item; writes `audio/manifest.json`.

    Each item also gets a `<id>.txt` sidecar with its script (read by `FakeTranscriber`). Re-running with
    unchanged text, voice and speed re-uses the existing files (no TTS call). For `master_first` jobs the
    items are the chapters (`c<n>`) and each Short's hook (`h<k>`) and closing line (`x<k>`).
    """

    name: str = "voice"
    tenant: Tenant = "tts"
    inputs: tuple[str, ...] = ("parts.json", "story.json", "shorts_plan.json", "closings.json")
    outputs: tuple[str, ...] = ("audio/manifest.json",)

    def run(self, ctx: JobContext) -> None:
        """Synthesize, normalize and measure every narration item of the job."""
        if ctx.fmt == "master_first":
            from ns_studio.media.master import run_voice_master

            run_voice_master(ctx)
            return
        parts = ctx.read_json("parts.json")
        voice = job_voice(ctx)
        voice_name = voice.name or voice.speaker or "Ryan"
        closings = ctx.read_json("closings.json") if ctx.file("closings.json").exists() else None
        if not isinstance(closings, dict):
            closings = None  # no hooks stage ran (an owner's script) or the file is damaged: no closing line
        items = narration_items(parts, voice_name, job_speed(ctx), ctx.channel.language, closings)
        manifest, synthesized = synthesize(ctx, items)
        # Measured seconds live in audio/manifest.json only: rewriting parts.json (an input of hooks, seo,
        # visual_plan and the policy gate) made `--resume` redo those LLM stages.
        ctx.write_json("audio/manifest.json", {it.id: manifest[it.id] for it in items})
        ctx.log.info("voice_done", job_id=ctx.job_id, items=len(items), synthesized=synthesized)
