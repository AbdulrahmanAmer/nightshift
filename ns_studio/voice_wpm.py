"""Words per minute each voice really speaks, measured from the audio of finished jobs (spec 7.0 bullet 1).

`PACING_WPM` is the pace a voice is asked for; the TTS engine speaks a little faster or slower than that, and
each voice differently. After the voice stage `record_voice_wpm` divides the words of every narration item by
the seconds the engine spent speaking them (the pauses the service inserts are taken out, they are budgeted
separately), normalizes that to speed 1.0 and keeps a running figure per voice in `config/voice_wpm.json`.
`job_wpm` gives the next job that voice's measured pace at the job's speed instead of the table figure, and
pins the answer in `wpm_used.json` so every stage of one job plans with the same number.

Everything here is best effort: a missing, damaged or unwritable file means the pacing table is used.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ns_common.pacing import NOMINAL_WPM, PACING_WPM, pause_plan
from ns_common.textstats import word_count

if TYPE_CHECKING:
    from ns_common.paths import Paths
    from ns_common.tts_client import TtsItem
    from ns_studio.context import JobContext

VOICE_WPM_FILE = "voice_wpm.json"
PINNED_FILE = "wpm_used.json"
MIN_SAMPLE_WORDS = 120  # a job that voiced fewer words says nothing about the voice
MAX_SAMPLES = 12  # jobs kept per voice: the figure follows the voice, not the first week
SANE_RATIO = (0.5, 1.6)  # a measured pace outside this band of the nominal 155 wpm is a broken sample
CLAMP_RATIO = (0.6, 1.6)  # a job's planning pace stays within this band of its pacing table figure


def voice_wpm_path(paths: Paths) -> Path:
    """Where the measurements live: `<home>/config/voice_wpm.json`."""
    return paths.config / VOICE_WPM_FILE


def load_voice_wpm(paths: Paths) -> dict[str, Any]:
    """The stored measurements as `{"voices": {name: {...}}}`; empty when the file is missing or damaged."""
    try:
        doc = json.loads(voice_wpm_path(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"voices": {}}
    voices = doc.get("voices") if isinstance(doc, dict) else None
    return {"voices": voices if isinstance(voices, dict) else {}}


def measured_wpm_1x(paths: Paths, voice_name: str) -> float | None:
    """Words per minute `voice_name` speaks at TTS speed 1.0, or None when no job has measured it yet."""
    entry = load_voice_wpm(paths)["voices"].get(voice_name)
    try:
        value = float(entry["wpm_1x"]) if isinstance(entry, dict) else 0.0
    except (KeyError, TypeError, ValueError):
        return None
    lo, hi = (NOMINAL_WPM * r for r in SANE_RATIO)
    return value if lo <= value <= hi else None


def item_speech_seconds(item: TtsItem, seconds: float) -> float:
    """Seconds of speech in a voiced item: its audio minus the silences between the service's chunks."""
    pauses = sum(p for _chunk, p in pause_plan(item.text, item.cliff_lines))
    return seconds - pauses


def job_sample(items: list[TtsItem], manifest: dict[str, dict]) -> tuple[int, float] | None:
    """(words, speech seconds at speed 1.0) of the voiced `items`; None when too little to measure."""
    words, speech_1x = 0, 0.0
    for it in items:
        entry = manifest.get(it.id)
        if not entry or it.speed <= 0:
            continue
        speech = item_speech_seconds(it, float(entry.get("seconds", 0.0)))
        if speech <= 0:
            continue
        words += word_count(it.text)
        speech_1x += speech * it.speed  # a voice at speed 2 takes half the time: back to the 1.0 figure
    if words < MIN_SAMPLE_WORDS or speech_1x <= 0:
        return None
    return words, speech_1x


def _write_atomic(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def record_voice_wpm(
    ctx: JobContext, voice_name: str, items: list[TtsItem], manifest: dict[str, dict]
) -> float | None:
    """Fold this job's narration into the voice's stored pace; returns the voice's wpm at speed 1.0.

    One sample per job id (a second pass of the voice stage, e.g. after an expansion, replaces the first),
    the last `MAX_SAMPLES` jobs count. Returns None, and stores nothing, when the job voiced too little or
    measured an impossible pace. Never raises: a measurement must not stop a job.
    """
    sample = job_sample(items, manifest)
    if sample is None:
        return None
    words, speech_1x = sample
    lo, hi = (NOMINAL_WPM * r for r in SANE_RATIO)
    if not lo <= words / speech_1x * 60.0 <= hi:
        ctx.log.warn(
            "voice_wpm_discarded",
            "measured pace outside the believable range",
            job_id=ctx.job_id,
            voice=voice_name,
            wpm_1x=round(words / speech_1x * 60.0, 1),
        )
        return None
    try:
        doc = load_voice_wpm(ctx.paths)
        entry = doc["voices"].get(voice_name)
        samples: dict[str, Any] = dict(entry.get("samples", {})) if isinstance(entry, dict) else {}
        samples.pop(ctx.job_id, None)  # re-recording a job moves it to the newest position
        samples[ctx.job_id] = {"words": words, "speech_s_1x": round(speech_1x, 3)}
        while len(samples) > MAX_SAMPLES:
            samples.pop(next(iter(samples)))
        total_words = sum(int(s["words"]) for s in samples.values())
        total_s = sum(float(s["speech_s_1x"]) for s in samples.values())
        wpm_1x = round(total_words / total_s * 60.0, 2)
        doc["voices"][voice_name] = {
            "wpm_1x": wpm_1x,
            "jobs": len(samples),
            "words": total_words,
            "samples": samples,
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
        _write_atomic(voice_wpm_path(ctx.paths), {"version": 1, **doc})
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
        ctx.log.warn("voice_wpm_not_stored", str(e)[:200], job_id=ctx.job_id, voice=voice_name)
        return None
    ctx.log.info("voice_wpm", job_id=ctx.job_id, voice=voice_name, wpm_1x=wpm_1x, jobs=len(samples))
    return wpm_1x


def job_wpm(ctx: JobContext) -> int:
    """Words per minute the job plans with: its voice's measured pace at the job's speed, else the table.

    The first call of a job decides and writes `wpm_used.json`; later calls (other stages, a resume) read it,
    so a measurement that lands while the job runs does not change its plan half way.
    """
    pinned = ctx.file(PINNED_FILE)
    try:
        value = int(json.loads(pinned.read_text(encoding="utf-8"))["wpm"])
        if value > 0:
            return value
    except (OSError, ValueError, KeyError, TypeError):
        pass
    table = PACING_WPM[ctx.combo.pacing]
    wpm, source, voice_name, speed, wpm_1x = table, "table", None, None, None
    try:
        from ns_studio.media.voice import job_speed, job_voice

        voice = job_voice(ctx)
        voice_name = voice.name or voice.speaker or "Ryan"
        speed = job_speed(ctx)
        wpm_1x = measured_wpm_1x(ctx.paths, voice_name)
        if wpm_1x is not None:
            lo, hi = (table * r for r in CLAMP_RATIO)
            wpm, source = round(min(hi, max(lo, wpm_1x * speed))), "measured"
    except (OSError, ValueError, KeyError, AttributeError):
        pass  # no readable measurement: the pacing table stays
    doc = {
        "wpm": wpm,
        "table_wpm": table,
        "source": source,
        "voice": voice_name,
        "speed": speed,
        "wpm_1x": wpm_1x,
    }
    try:
        pinned.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass  # the plan still uses this call's figure
    return wpm
