"""Friendly names for steps, downloads and checks (docs/design/installer-ui.md 2.4 to 2.6)."""

from __future__ import annotations

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
    "git": "Git",
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
TIER_NAMES = {"small": "Small", "balanced": "Balanced", "best": "Best"}


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


# Names in verify-report.json: prefix -> label (spec 2.6). Anything else shows as it is.
VERIFY_LABELS: tuple[tuple[str, str], ...] = (
    ("llm", "Story model"),
    ("embed", "Memory index"),
    ("tts", "Voice engine"),
    ("whisper", "Captions"),
    ("nvenc", "Video encoder"),
    ("mtp", "Faster decoding"),
)
VERIFY_HIDDEN_PREFIXES = ("bench",)


def verify_label(name: str) -> str | None:
    """Label for a verify check name, or None when the check is not shown (``bench.*``)."""
    low = name.lower()
    if low.startswith(VERIFY_HIDDEN_PREFIXES):
        return None
    for prefix, label in VERIFY_LABELS:
        if low.startswith(prefix):
            return label
    return name


def verify_value(name: str, passed: bool, value: str | None, speaker: str | None = None) -> str | None:
    """The value column of a verify row (spec 2.6)."""
    low = name.lower()
    if low.startswith("llm"):
        return f"{value} tokens/s" if value else None
    if low.startswith("embed"):
        return "ready" if passed else None
    if low.startswith("tts"):
        who = speaker or "voice"
        return f"{who}, {value} RTF" if value else who
    if low.startswith("whisper"):
        return "graphics card" if passed else "processor"
    if low.startswith("nvenc"):
        return "NVENC" if passed else "libx264 (slower)"
    if low.startswith("mtp"):
        return "on" if passed else "off"
    return value
