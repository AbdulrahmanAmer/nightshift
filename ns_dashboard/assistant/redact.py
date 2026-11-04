"""Blank anything that looks like a pasted API key before it is stored or sent to the model (component C9).

Keys are entered only in Settings. If the owner pastes one into the chat anyway, `redact_text` replaces it
with `[key removed]` before the message reaches `assistant.db` or the language model, so the key is never
written to disk by the Assistant and never appears in a prompt. The patterns are deliberately plain: the known
key shapes (Pexels, Pixabay and the common vendor prefixes), a key that follows a label such as "my Pexels key
is", and any other long unbroken run of letters and digits.
"""

from __future__ import annotations

import re
from typing import Any

MARK = "[key removed]"

#: Shapes that are a key on their own.
_SHAPES: tuple[re.Pattern[str], ...] = (
    # Pixabay: digits, a dash and 24 hexadecimal characters
    re.compile(r"(?<![A-Za-z0-9])\d{6,10}-[0-9a-fA-F]{20,40}(?![A-Za-z0-9])"),
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{16,}"),
)
#: "my Pexels key is 5634...", "api_key: abc123...": the label stays, the value goes (it needs a digit).
_LABELLED = re.compile(
    r"(?i)\b((?:api[ _-]?key|secret|token|password|pexels|pixabay|key)\b\s*(?:is|are|=|:)\s*[`'\"]?)"
    r"((?=[A-Za-z0-9._\-+/=]*\d)[A-Za-z0-9._\-+/=]{12,})"
)
#: A long unbroken run of letters and digits (Pexels keys are 56 of them). Paths and ids have separators
#: around their runs, so the look-around keeps them out.
_RUN = re.compile(r"(?<![A-Za-z0-9_/\\.\-])[A-Za-z0-9]{32,}(?![A-Za-z0-9_/\\.\-])")


def _run(m: re.Match[str]) -> str:
    """Blank a long run only when it mixes letters and digits (a key), not a plain word or number."""
    s = m.group(0)
    return MARK if any(c.isdigit() for c in s) and any(c.isalpha() for c in s) else s


def redact_text(text: str) -> str:
    """`text` with every key-looking string replaced by `[key removed]` (idempotent)."""
    if not text:
        return text
    out = text
    for shape in _SHAPES:
        out = shape.sub(MARK, out)
    out = _LABELLED.sub(lambda m: m.group(1) + MARK, out)
    return _RUN.sub(_run, out)


def redact_obj(value: Any) -> Any:
    """`redact_text` applied to every string inside nested dicts and lists."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    return value


def has_mark(text: str) -> bool:
    """True when `text` already carries a `[key removed]` mark."""
    return MARK in text
