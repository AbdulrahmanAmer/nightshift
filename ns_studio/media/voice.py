"""Voice stage: batch TTS, per-item loudness normalization, script sidecars (component C6)."""

from __future__ import annotations

import hashlib
import zlib
from pathlib import Path

from ns_common import ffmpeg as ff
from ns_common.languages import qwen_language
from ns_common.models import Voice
from ns_common.pacing import pacing_speed
from ns_common.textstats import split_sentences
from ns_common.tts_client import TtsItem
from ns_studio.context import JobContext
from ns_studio.media.audio import loudnorm, measure_lufs
from ns_studio.stages import Tenant

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
    parts: list[dict], voice_name: str, speed: float, language: str | None = None
) -> list[TtsItem]:
    """One TTS item per part body (`p<n>`) plus `p<n>_hook` when the hook is not the first sentence.

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
    return items


def _signature(it: TtsItem) -> str:
    base = f"{it.voice}|{it.speed}|{it.text}"
    if it.cliff_lines:  # unchanged for items without one, so earlier jobs keep their cache
        base += "|cliff:" + "|".join(it.cliff_lines)
    if qwen_language(it.language)[0] != "English":  # English items keep the signature (and cache) they had
        base += f"|lang:{qwen_language(it.language)[0]}"
    return hashlib.sha256(base.encode()).hexdigest()


def synthesize(ctx: JobContext, items: list[TtsItem]) -> tuple[dict[str, dict], int]:
    """Speak, normalize (-16 LUFS) and measure `items`; reuse files whose text, voice and speed are unchanged.

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
        seed = (zlib.crc32(ctx.job_id.encode()) + ctx.attempt) & 0x7FFFFFFF  # a retry sounds different
        files = ctx.services.tts.speak_batch(todo, raw_dir, seed=seed)
        by_id = {it.id: it for it in todo}
        for f in files:
            out = ctx.file(f"audio/{f.id}.wav")
            loudnorm(f.path, out)
            lufs, tp = measure_lufs(out)
            out.with_suffix(".txt").write_text(by_id[f.id].text, encoding="utf-8")
            manifest[f.id] = {
                "path": str(out),
                "seconds": round(ff.duration_s(out), 3),
                "lufs": lufs,
                "tp": tp,
                "sig": _signature(by_id[f.id]),
            }
    return manifest, len(todo)


class VoiceStage:
    """Stage `voice` (tenant tts): spec §7.5, loudness -16 LUFS per item; writes `audio/manifest.json`.

    Each item also gets a `<id>.txt` sidecar with its script (read by `FakeTranscriber`). Re-running with
    unchanged text, voice and speed re-uses the existing files (no TTS call). For `master_first` jobs the
    items are the chapters (`c<n>`) and each Short's hook (`h<k>`) and closing line (`x<k>`).
    """

    name: str = "voice"
    tenant: Tenant = "tts"
    inputs: tuple[str, ...] = ("parts.json", "story.json", "shorts_plan.json")
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
        items = narration_items(parts, voice_name, job_speed(ctx), ctx.channel.language)
        manifest, synthesized = synthesize(ctx, items)
        # Measured seconds live in audio/manifest.json only: rewriting parts.json (an input of hooks, seo,
        # visual_plan and the policy gate) made `--resume` redo those LLM stages.
        ctx.write_json("audio/manifest.json", {it.id: manifest[it.id] for it in items})
        ctx.log.info("voice_done", job_id=ctx.job_id, items=len(items), synthesized=synthesized)
