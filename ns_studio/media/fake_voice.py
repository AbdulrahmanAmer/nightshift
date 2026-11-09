"""Engine test: say what the fake voice is, in the job log and in a captions failure (head order H-7, D-26).

An engine-test install (`tts.engine: fake`, docs/DEV_MODE.md "Real engines, fake voice") runs the real speech
recognition on a fake voice. The voice service speaks the real text with Windows speech when this PC has a
voice, and makes tone bursts when it has none. Real recognition cannot align tones, so a captions failure on
such a PC says nothing about the product. The voice stage asks the service how the fake spoke (`/health`),
keeps the answer in the job (`audio/voice_engine.json`), logs a warning when it was tone bursts, and the
captions stage adds one sentence to its NS-E403 detail so nobody reads that failure as a product defect.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from ns_studio.context import JobContext

VOICE_ENGINE_FILE = "audio/voice_engine.json"
FAKE_VOICE_TONES_CODE = "NS-W303"  # contracts/errors.json: the fake voice made tone bursts (CR-H7-1)


def is_fake_voice(ctx: JobContext) -> bool:
    """True on an engine-test install: the voice service runs the fake engine; a dev install is not one."""
    return ctx.runtime.tts.engine == "fake" and not ctx.runtime.dev_mode


def fetch_health(base_url: str) -> dict[str, Any] | None:
    """The voice service's /health body, or None when it does not answer or answers with something else."""
    try:
        body = httpx.get(f"{base_url}/health", timeout=5).json()
    except (httpx.HTTPError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def note_voice_engine(
    ctx: JobContext, fetch: Callable[[str], dict[str, Any] | None] = fetch_health
) -> dict[str, Any] | None:
    """On an engine test, record how the fake voice spoke this job; a warning in the log for tone bursts.

    Returns the record (also written to `audio/voice_engine.json`), or None when the voice is not the fake.
    Never raises: a diagnostic must not stop a job.
    """
    if not is_fake_voice(ctx):
        return None
    health = fetch(ctx.runtime.tts.base_url) or {}
    mode = health.get("voice_mode")  # "sapi", "tones", or absent (the service did not answer or is older)
    info: dict[str, Any] = {
        "engine": "fake",
        "mode": mode if mode in ("sapi", "tones") else None,
        "voice": health.get("voice"),
        "warning": health.get("voice_warning"),
    }
    if info["mode"] == "tones":  # an older voice service sends no code: the warning is the same one
        info["code"] = health.get("voice_warning_code") or FAKE_VOICE_TONES_CODE
    try:
        ctx.write_json(VOICE_ENGINE_FILE, info)
    except OSError:
        pass
    if info["mode"] == "tones":
        ctx.log.warn(
            "fake_voice_tones_only",
            info["warning"] or "the fake voice made tone bursts; speech recognition cannot align them",
            code=info["code"],
            job_id=ctx.job_id,
        )
    elif info["mode"] == "sapi":
        ctx.log.info("fake_voice_sapi", job_id=ctx.job_id, voice=info["voice"])
    return info


def captions_note(ctx: JobContext) -> str:
    """One sentence for the NS-E403 detail when the voice is the engine test's fake; "" for a real voice."""
    try:
        info = json.loads(ctx.file(VOICE_ENGINE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        info = None
    if not isinstance(info, dict):
        if not is_fake_voice(ctx):
            return ""
        info = {}
    base = "the voice is the fake one of this engine test"
    if info.get("mode") == "tones":
        return (
            f"{base} and it made tone bursts, which speech recognition cannot align "
            "(no Windows speech voice was available): this is the test voice, not a product fault"
        )
    if info.get("mode") == "sapi":
        return f"{base}, speaking with the Windows voice {info.get('voice')!r}; a mismatch may come from it"
    return f"{base} (tts.engine: fake); a mismatch may come from it, not from the product"


def e403_detail(ctx: JobContext, failures: Sequence[str]) -> str:
    """The detail of NS-E403: the failures, then `captions_note` when the voice is the fake."""
    note = captions_note(ctx)
    return "; ".join([*failures, note] if note else failures)
