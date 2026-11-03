"""Friendly names for steps, downloads and checks (docs/design/installer-ui.md 2.4 to 2.6)."""

from __future__ import annotations

from typing import Any

STEP_TITLES: dict[str, str] = {
    "b01_tools": "Free tools",
    "b02_home": "Install folder",
    "b03_setup_env": "Setup environment",
    "s01_questions": "Questions",
    "s02_download_bins": "Download programs",
    "s03_download_models": "Download AI models",
    "s04_venvs": "Python environments",
    "s05_opencode_prewarm": "Nightshift Code first start",
    "s06_tune": "Tune for this graphics card",
    "s07_configs": "Settings and keys",
    "s08_smoke": "Test the story model, voice and video",
    "v01_samples": "Make a test voice and video",
    "s09_shortcuts_scheduler": "Shortcuts and nightly task",
    "s10_summary": "Summary and diagnostics zip",
}

GROUP_LABELS: dict[str, str] = {
    "tools": "Free tools",
    "python": "Python packages",
    "programs": "Programs",
    "models": "AI models",
}

# plan key -> label for the downloads that have a fixed key
DOWNLOAD_LABELS: dict[str, str] = {
    "vcruntime": "Visual C++ runtime",
    "ffmpeg": "FFmpeg",
    "terminal": "Windows Terminal",
    "setup_env": "Python packages for setup",
    "llama_cuda": "Model runner (llama.cpp)",
    "llama_cuda13": "Model runner (llama.cpp)",
    "llama_cpu": "Model runner (llama.cpp)",
    "cudart": "CUDA runtime",
    "cudart13": "CUDA runtime",
    "qwentts": "Voice engine",
    "opencode": "Nightshift Code",
    "yt_dlp": "Video downloader for Clipper",
    "webview2": "Setup window files",
    "embed": "Memory index",
    "whisper": "Caption timing",
    "yunet": "Face finder for Clipper",
    "spacy": "Language rules",
}
TIER_NAMES = {"small": "Small", "balanced": "Balanced", "best": "Best", "custom": "Custom"}


def llm_label(tier: str) -> str:
    """Label of the story model row."""
    return f"Story model ({TIER_NAMES.get(tier, 'Small')})"


def label_for_file(key: str) -> str:
    """Best label for a download key P1 did not plan itself (a row the wizard added, such as a fallback)."""
    low = key.lower()
    if key in DOWNLOAD_LABELS:
        return DOWNLOAD_LABELS[key]
    if low.startswith("whisper"):
        return DOWNLOAD_LABELS["whisper"]
    if "customvoice" in low:
        return "Voice: preset narrators"
    if "talker" in low and "base" in low:
        return "Voice: your own voices"
    if "voicedesign" in low:
        return "Voice: designed voices"
    if "tokenizer" in low:
        return "Voice codec"
    if "nomic-embed" in low:
        return DOWNLOAD_LABELS["embed"]
    if low.endswith(".gguf"):
        return "Story model"
    if "qwen3-tts" in low or "safetensors" in low:
        return "Backup voice engine files"
    return key


def row_key(key: str) -> str:
    """The plan row a wizard file key belongs to: every whisper side file reports into one row."""
    return "whisper" if key.lower().startswith("whisper") else key


# Names in verify-report.json (the real ones). This module runs on the bare Python of the setup server and
# cannot import ns_supervisor.check_names; tests/test_verify_check_names.py asserts each name below equals it.
LLAMA_PREFIX = "llama."
EMBED_PREFIX = "embed."
TTS_PREFIX = "tts."
WHISPER_PREFIX = "whisper."
NVENC_PREFIX = "nvenc."
LLAMA_TOK_S = "llama.tok_s"
EMBED_VECTOR = "embed.vector"
TTS_START = "tts.start"
TTS_VOICES = "tts.voices"
WHISPER_LOAD = "whisper.load"
NVENC_PROBE = "nvenc.probe"

# prefix -> (row key, label)
# One setup row per area: llama.start, llama.health and llama.json_completion all report into "Story model".
VERIFY_AREAS: tuple[tuple[str, str, str], ...] = (
    (LLAMA_PREFIX, "llm", "Story model"),
    (EMBED_PREFIX, "embed", "Memory index"),
    (TTS_PREFIX, "tts", "Voice engine"),
    (WHISPER_PREFIX, "whisper", "Captions"),
    (NVENC_PREFIX, "nvenc", "Video encoder"),
    ("supervisor.", "engine", "Engine"),
    ("vram.", "vram", "Graphics memory"),
    ("job.", "job", "Test job"),
)
#: Areas where a failure is a handled fallback (libx264, the PyTorch voice engine), not a stop.
VERIFY_FALLBACK_ROWS = frozenset({"nvenc", "tts"})
VERIFY_HIDDEN_PREFIXES = ("bench",)


def _area(name: str) -> tuple[str, str] | None:
    low = name.lower()
    for prefix, key, label in VERIFY_AREAS:
        if low.startswith(prefix):
            return key, label
    return None


def verify_label(name: str) -> str | None:
    """Label for a verify check name, or None when the check is not shown (``bench.*``)."""
    if name.lower().startswith(VERIFY_HIDDEN_PREFIXES):
        return None
    area = _area(name)
    return area[1] if area else name


def verify_key(name: str) -> str:
    """The setup row a check reports into (``llama.health`` -> ``llm``); unknown names are their own row."""
    area = _area(name)
    return area[0] if area else name


def verify_value(name: str, passed: bool, value: str | None, speaker: str | None = None) -> str | None:
    """The value column contribution of one verify check (spec 2.6); None when it has nothing to say."""
    low = name.lower()
    if low == LLAMA_TOK_S:
        return f"{value} tokens/s" if value else None
    if low == EMBED_VECTOR:
        return "ready" if passed else None
    if low.startswith(TTS_PREFIX) and low not in (TTS_START, TTS_VOICES):
        who = speaker or "voice"      # tts.preset / tts.design / tts.clone carry the real-time factor
        return f"{who}, {value} RTF" if value else who
    if low == WHISPER_LOAD:
        return "graphics card" if passed else "processor"
    if low == NVENC_PROBE:
        return "NVENC" if passed else "libx264 (slower)"
    return None


def verify_rows(checks: list[dict[str, Any]], speaker: str | None = None) -> list[dict[str, Any]]:
    """Setup rows from the check rows of verify-report.json: one row per area, in first-seen order.

    A row passes when all its checks pass; a failed nvenc or tts row is a ``fallback``, anything else
    ``failed``. ``bench.*`` checks are not shown.
    """
    rows: dict[str, dict[str, Any]] = {}
    for c in checks:
        name = str(c.get("name", ""))
        label = verify_label(name)
        if label is None:
            continue
        key = verify_key(name)
        passed = bool(c.get("passed"))
        row = rows.setdefault(
            key, {"name": key, "label": label, "status": "passed", "value": None, "note": None}
        )
        if not passed:
            row["status"] = "fallback" if key in VERIFY_FALLBACK_ROWS else "failed"
        value = verify_value(name, passed, c.get("value"), speaker)
        if value is not None and (row["value"] is None or not passed):
            row["value"] = value
    return list(rows.values())


#: The rows the setup screen shows (as "running") before the first verify report arrives.
VERIFY_PENDING_ROWS: tuple[tuple[str, str], ...] = tuple((key, label) for _, key, label in VERIFY_AREAS[:5])
