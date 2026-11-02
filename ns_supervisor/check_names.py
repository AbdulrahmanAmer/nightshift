"""C2: the names of the checks `ns verify` reports. Standard library only, so the installer can import it.

`ns_supervisor.verify` records checks under these names; `installer/stage2/steps/s08_smoke.py`, the setup
window labels and the installer's fake `ns verify` read them back. Never spell a check name as a literal in
a consumer: a typo there silently turns a failing check into a pass (the smoke test once matched `llm`
and `mtp`, which do not exist).
"""

from __future__ import annotations

SUPERVISOR_STATUS = "supervisor.status"
SUPERVISOR_COLD_START = "supervisor.cold_start"  # full run only
LLAMA_START = "llama.start"
LLAMA_HEALTH = "llama.health"
LLAMA_JSON = "llama.json_completion"
LLAMA_TOK_S = "llama.tok_s"  # full run only
EMBED_START = "embed.start"
EMBED_VECTOR = "embed.vector"
TTS_START = "tts.start"
TTS_VOICES = "tts.voices"
TTS_ENGINE = "tts.engine"  # only with the reference engine: a passing check that carries NS-W301
WHISPER_LOAD = "whisper.load"
NVENC_PROBE = "nvenc.probe"
JOB_FAKE_SERVICES = "job.fake_services"  # full run only
VRAM_IDLE = "vram.idle"

#: Prefix of the per-mode TTS checks (`tts.preset`, `tts.design`, `tts.clone`).
TTS_MODE_PREFIX = "tts."
TTS_PRESET = "tts.preset"

#: Area prefixes: every check of an area starts with its prefix.
LLAMA_PREFIX = "llama."
EMBED_PREFIX = "embed."
TTS_PREFIX = "tts."
WHISPER_PREFIX = "whisper."
NVENC_PREFIX = "nvenc."

#: The checks `ns verify --quick` records, in order, when everything works and a preset voice exists.
QUICK_CHECKS: tuple[str, ...] = (
    SUPERVISOR_STATUS,
    LLAMA_START,
    LLAMA_HEALTH,
    LLAMA_JSON,
    EMBED_START,
    EMBED_VECTOR,
    TTS_START,
    TTS_PRESET,
    TTS_VOICES,
    WHISPER_LOAD,
    NVENC_PROBE,
    VRAM_IDLE,
)


def tts_mode(mode: str) -> str:
    """Check name of one TTS mode (`preset`, `design`, `clone`)."""
    return f"{TTS_MODE_PREFIX}{mode}"
