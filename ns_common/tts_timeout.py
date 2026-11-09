"""One formula for how long a TTS batch may take, shared by the studio's client and the TTS server.

The studio gives up on a `POST /v1/audio/speech/batch` after `tts_timeout_s(lines)`. The server lets every
`qwen-tts` process run at least as long (`ns_tts.server` passes the client's own figure down), so it never
kills a batch the client is still waiting for, and the client never gives up while the server could still
finish. A `line` is one sentence (the studio's count) or one synthesis chunk (the server's count).

The 3 s per line assume a voice engine at about half real time or faster. `ns verify` measures the real speed
of this PC (real-time factor, seconds of work per second of speech) and stores it in `config/tts_speed.json`
(`save_rtf`); the voice stage then passes `rtf` to `batch_timeout_s`, and a slower engine gets a longer limit
(gap L4). Without a measurement the figures are the ones above.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.textstats import split_sentences

if TYPE_CHECKING:
    from ns_common.paths import Paths

TTS_TIMEOUT_BASE_S = 120.0  # model load, CUDA start-up, the codec
TTS_TIMEOUT_PER_LINE_S = 3.0  # one line of narration: the floor, whatever the measured speed
TTS_TIMEOUT_CAP_S = 7200.0  # two hours, whatever the batch
TTS_LINE_AUDIO_S = 6.0  # seconds of speech in one line (12 to 14 words and the pause after it)
TTS_RTF_MARGIN = 1.5  # slack on top of the measured speed (a long batch runs hotter than a probe)
SPEED_FILE = "tts_speed.json"


def per_line_s(rtf: float | None = None) -> float:
    """Seconds one line may take: 3 s, or more when the measured real-time factor says the engine is slower.

    `max(3, 6 * rtf * 1.5)`: a line is about 6 s of speech, the engine needs `rtf` seconds per second of it,
    and the limit keeps 50 % in hand. An unknown, zero, negative or infinite `rtf` gives the floor.
    """
    if rtf is None or not math.isfinite(rtf) or rtf <= 0:
        return TTS_TIMEOUT_PER_LINE_S
    return max(TTS_TIMEOUT_PER_LINE_S, TTS_LINE_AUDIO_S * rtf * TTS_RTF_MARGIN)


def tts_timeout_s(lines: int, rtf: float | None = None) -> float:
    """Seconds a batch of `lines` lines may take: 120 s + `per_line_s` per line, at most 2 h."""
    return min(TTS_TIMEOUT_CAP_S, TTS_TIMEOUT_BASE_S + per_line_s(rtf) * max(0, lines))


def count_lines(texts: Iterable[str]) -> int:
    """Sentences in `texts` (at least one per non-empty text): the studio's estimate of a batch's lines."""
    return sum(max(1, len(split_sentences(t))) for t in texts if t.strip())


def batch_timeout_s(texts: Iterable[str], rtf: float | None = None) -> float:
    """`tts_timeout_s` of the narration `texts` of one batch."""
    return tts_timeout_s(count_lines(texts), rtf)


def speed_path(paths: Paths) -> Path:
    """Where the measured speed lives: `<home>/config/tts_speed.json`."""
    return paths.config / SPEED_FILE


def save_rtf(paths: Paths, rtf: float, *, quick: bool) -> None:
    """Store the real-time factor `ns verify` measured (the slowest voice mode). Best effort."""
    if not math.isfinite(rtf) or rtf <= 0:
        return
    doc = {
        "rtf": round(rtf, 3),
        "quick": quick,
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    try:
        write_text_atomic(speed_path(paths), json.dumps(doc, indent=2) + "\n")
    except OSError:
        pass  # a limit that stays at the floor is no reason to fail a verify run


def load_rtf(paths: Paths) -> float | None:
    """The stored real-time factor, or None when no measurement exists or the file is unreadable."""
    try:
        value = json.loads(read_text_retry(speed_path(paths), "utf-8-sig")).get("rtf")
    except (OSError, ValueError, AttributeError):
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value > 0 else None
