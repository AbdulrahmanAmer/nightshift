"""One formula for how long a TTS batch may take, shared by the studio's client and the TTS server.

The studio gives up on a `POST /v1/audio/speech/batch` after `tts_timeout_s(lines)`. The server lets every
`qwen-tts` process run at least as long (`ns_tts.server` passes the client's own figure down), so it never
kills a batch the client is still waiting for, and the client never gives up while the server could still
finish. A `line` is one sentence (the studio's count) or one synthesis chunk (the server's count).
"""

from __future__ import annotations

from collections.abc import Iterable

from ns_common.textstats import split_sentences

TTS_TIMEOUT_BASE_S = 120.0  # model load, CUDA start-up, the codec
TTS_TIMEOUT_PER_LINE_S = 3.0  # one line of narration
TTS_TIMEOUT_CAP_S = 7200.0  # two hours, whatever the batch


def tts_timeout_s(lines: int) -> float:
    """Seconds a batch of `lines` lines may take: 120 s + 3 s per line, at most 2 h."""
    return min(TTS_TIMEOUT_CAP_S, TTS_TIMEOUT_BASE_S + TTS_TIMEOUT_PER_LINE_S * max(0, lines))


def count_lines(texts: Iterable[str]) -> int:
    """Sentences in `texts` (at least one per non-empty text): the studio's estimate of a batch's lines."""
    return sum(max(1, len(split_sentences(t))) for t in texts if t.strip())


def batch_timeout_s(texts: Iterable[str]) -> float:
    """`tts_timeout_s` of the narration `texts` of one batch."""
    return tts_timeout_s(count_lines(texts))
