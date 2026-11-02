"""Pacing options: words per minute and the TTS speed that makes the voice speak at that pace (spec 7.0)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from ns_common.textstats import split_sentences, word_count

#: Speaking rate of each `pacing` option (spec section 17).
PACING_WPM: dict[str, int] = {"brisk": 175, "standard": 155, "measured": 140}
#: The rate a voice speaks at `Voice.speed == 1.0` (the budget's "standard" pace).
NOMINAL_WPM = 155
SPEED_MIN, SPEED_MAX = 0.7, 1.3  # the range `Voice.speed` and the TTS API accept


def pacing_speed(voice_speed: float, pacing: str) -> float:
    """TTS speed for a job: the channel voice's own speed scaled by the pacing's rate, clamped to 0.7-1.3.

    `brisk` (175 wpm) speaks 13 % faster than `standard`, `measured` (140 wpm) 10 % slower, so the seconds a
    word budget produces match the seconds QC expects. Raises KeyError for an unknown pacing.
    """
    speed = voice_speed * PACING_WPM[pacing] / NOMINAL_WPM
    return round(min(SPEED_MAX, max(SPEED_MIN, speed)), 3)


# ---- pauses -------------------------------------------------------------------------------------------
# The TTS service (`ns_tts.audio.chunk_text`) puts silence between its chunks; a word budget that ignores it
# runs about 8 % long, which breaks the 15:00 QC ceiling of a 14-minute master and the +-15 % Short band.
PAUSE_SENTENCE_S = 0.35
PAUSE_PARAGRAPH_S = 0.70
PAUSE_CLIFF_S = 1.20
CARD_S = 1.2  # the silent title card between two chapters of a master
MIN_CHUNK_CHARS = 25  # `chunk_text` merges a shorter sentence into the next one of its paragraph
WORDS_PER_SENTENCE = 12  # typical for the stories we ask for; used to budget before any text exists
SENTENCES_PER_PARAGRAPH = 5
#: Expected silence per word of narration, from the typical sentence and paragraph length.
PAUSE_PER_WORD_S = (
    PAUSE_SENTENCE_S + (PAUSE_PARAGRAPH_S - PAUSE_SENTENCE_S) / SENTENCES_PER_PARAGRAPH
) / WORDS_PER_SENTENCE
#: A rendered Short must stay inside the +-15 % QC band; the budget keeps 3 points for TTS variation.
SHORT_QC_MARGIN = 0.12

_PARAGRAPHS = re.compile(r"\n\s*\n")


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def pause_plan(text: str, cliff_lines: Iterable[str] = ()) -> list[tuple[str, float]]:
    """(chunk text, silence after it in seconds) the TTS service produces for `text`.

    Mirrors `ns_tts.audio.chunk_text` for sentences up to its length cap: 350 ms after a sentence, 700 ms
    before a new paragraph, 1,200 ms before a cliff line, nothing after the last chunk; a sentence under 25
    characters is merged into the next one of its paragraph.
    """
    cliffs = {_norm(c) for c in cliff_lines}
    units: list[tuple[str, bool, bool]] = []  # (text, first in paragraph, cliff line)
    for para in _PARAGRAPHS.split(text.replace("\r\n", "\n")):
        if not para.strip():
            continue
        merged: list[tuple[str, bool]] = []
        for s in split_sentences(para):
            is_cliff = _norm(s) in cliffs
            if merged and len(merged[-1][0]) < MIN_CHUNK_CHARS and not is_cliff:
                merged[-1] = (f"{merged[-1][0]} {s}", merged[-1][1])
            else:
                merged.append((s, is_cliff))
        units.extend((s, i == 0, c) for i, (s, c) in enumerate(merged))
    plan: list[tuple[str, float]] = []
    for k, (chunk, _first, _cliff) in enumerate(units):
        if k + 1 == len(units):
            pause = 0.0
        elif units[k + 1][2]:
            pause = PAUSE_CLIFF_S
        elif units[k + 1][1]:
            pause = PAUSE_PARAGRAPH_S
        else:
            pause = PAUSE_SENTENCE_S
        plan.append((chunk, pause))
    return plan


def narration_seconds(text: str, wpm: float, cliff_lines: Iterable[str] = ()) -> float:
    """Seconds `text` takes to narrate at `wpm`: its words plus the silence between its chunks."""
    return word_count(text) / wpm * 60.0 + sum(p for _t, p in pause_plan(text, cliff_lines))


def master_seconds(chapters: Sequence[str], wpm: float, *, cliffs: bool = True) -> float:
    """Estimated length of a master: every chapter's narration (the last sentence of a chapter is its cliff
    line, preceded by 1.2 s, except in the last chapter) plus the title card between chapters."""
    total = 0.0
    for i, text in enumerate(chapters):
        last = split_sentences(text)[-1:] if cliffs and i + 1 < len(chapters) else ()
        total += narration_seconds(text, wpm, last)
    return total + CARD_S * max(0, len(chapters) - 1)


def master_fixed_seconds(chapters: int) -> float:
    """Silence of a master that does not scale with its words: cliff pauses and title cards."""
    return max(0, chapters - 1) * (CARD_S + PAUSE_CLIFF_S - PAUSE_SENTENCE_S)


def words_for_seconds(seconds: float, wpm: float, *, fixed_s: float = 0.0) -> int:
    """Words whose narration (speech plus the expected silence per word, plus `fixed_s`) fills `seconds`."""
    return max(0, int((seconds - fixed_s) / (60.0 / wpm + PAUSE_PER_WORD_S)))
