"""Language names the `qwen-tts` binary accepts for `--lang` (C3).

Upstream `qwen-tts --help` says only "--lang <name>  Language label (default: auto)". The names are the
`qwen3-tts.codec.language_names` of the talker GGUF (qwentts.cpp `docs/ARCHITECTURE.md`, `convert.py`, from the
checkpoint's `codec_language_id`), matched case-insensitively, and an unknown one is a fatal error
("unknown language"). Qwen3-TTS speaks these ten languages (plus Mandarin dialect speakers):
Chinese, English, German, Italian, Portuguese, Spanish, Japanese, Korean, French, Russian.

Channels carry an ISO code (`Channel.language`, "en" by default). `qwen_language` maps it to the name; a code
the model does not speak falls back to English, and the caller logs a warning (never `auto`: the model would
guess from the text, and a wrong guess is a different accent for a whole channel).
"""

from __future__ import annotations

QWEN_LANGUAGES: tuple[str, ...] = (
    "Chinese", "English", "German", "Italian", "Portuguese", "Spanish", "Japanese", "Korean", "French",
    "Russian",
)

_BY_CODE: dict[str, str] = {
    "zh": "Chinese", "zho": "Chinese", "chi": "Chinese", "cmn": "Chinese", "yue": "Chinese",
    "en": "English", "eng": "English",
    "de": "German", "deu": "German", "ger": "German",
    "it": "Italian", "ita": "Italian",
    "pt": "Portuguese", "por": "Portuguese",
    "es": "Spanish", "spa": "Spanish",
    "ja": "Japanese", "jpn": "Japanese", "jp": "Japanese",
    "ko": "Korean", "kor": "Korean",
    "fr": "French", "fra": "French", "fre": "French",
    "ru": "Russian", "rus": "Russian",
}
_BY_NAME = {n.lower(): n for n in QWEN_LANGUAGES}


def qwen_language(code: str | None) -> tuple[str, bool]:
    """(name for `qwen-tts --lang`, known). `known` is False when the code fell back to English.

    Accepts an ISO 639-1/-2 code (`ja`), a locale tag (`pt-BR`, `zh_CN`) or the English name (`Japanese`), in
    any case. An empty value means English and counts as known.
    """
    raw = (code or "").strip()
    if not raw:
        return "English", True
    low = raw.lower()
    if low in _BY_NAME:
        return _BY_NAME[low], True
    primary = low.replace("_", "-").split("-")[0]
    if primary in _BY_CODE:
        return _BY_CODE[primary], True
    return "English", False
