"""Voice stage: batch TTS, per-item loudness normalization, script sidecars (component C6)."""

from __future__ import annotations

import hashlib
import zlib
from pathlib import Path

from ns_common import ffmpeg as ff
from ns_common.textstats import split_sentences
from ns_common.tts_client import TtsItem
from ns_studio.context import JobContext
from ns_studio.media.audio import loudnorm, measure_lufs
from ns_studio.stages import Tenant

__all__ = ["VoiceStage", "loudnorm", "measure_lufs", "narration_items"]


def narration_items(parts: list[dict], voice_name: str, speed: float) -> list[TtsItem]:
    """One TTS item per part body (`p<n>`) plus `p<n>_hook` when the hook is not the first sentence."""
    items: list[TtsItem] = []
    for p in parts:
        items.append(TtsItem(f"p{p['n']}", p["text"], voice_name, speed))
        hook = (p.get("hook") or "").strip()
        sentences = split_sentences(p["text"])
        if hook and (not sentences or hook != sentences[0]):
            items.append(TtsItem(f"p{p['n']}_hook", hook, voice_name, speed))
    return items


def _signature(it: TtsItem) -> str:
    return hashlib.sha256(f"{it.voice}|{it.speed}|{it.text}".encode()).hexdigest()


class VoiceStage:
    """Stage `voice` (tenant tts): spec §7.5, loudness -16 LUFS per item; writes `audio/manifest.json`.

    Each item also gets a `<id>.txt` sidecar with its script (read by `FakeTranscriber`). Re-running with
    unchanged text, voice and speed re-uses the existing files (no TTS call).
    """

    name: str = "voice"
    tenant: Tenant = "tts"
    inputs: tuple[str, ...] = ("parts.json",)
    outputs: tuple[str, ...] = ("audio/manifest.json",)

    def run(self, ctx: JobContext) -> None:
        """Synthesize, normalize and measure every narration item of the job."""
        parts = ctx.read_json("parts.json")
        voice = ctx.channel.voice
        voice_name = voice.name or voice.speaker or "Ryan"
        items = narration_items(parts, voice_name, voice.speed)
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
            seed = zlib.crc32(ctx.job_id.encode()) & 0x7FFFFFFF
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
        changed = False
        for p in parts:
            secs = manifest[f"p{p['n']}"]["seconds"]
            if abs(float(p.get("est_seconds", 0.0)) - secs) > 1e-6:
                p["est_seconds"] = secs
                changed = True
        if changed:
            ctx.write_json("parts.json", parts)
        ctx.write_json("audio/manifest.json", {it.id: manifest[it.id] for it in items})
        ctx.log.info("voice_done", job_id=ctx.job_id, items=len(items), synthesized=len(todo))
