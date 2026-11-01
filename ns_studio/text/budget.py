"""Word budgets derived from pacing and length buckets (component C5; spec 7.0)."""

from __future__ import annotations

from dataclasses import dataclass

PACING_WPM: dict[str, int] = {"brisk": 175, "standard": 155, "measured": 140}


@dataclass(frozen=True)
class Budget:
    """Word budget of a story or part: target, +-20 % limits, part count and the wpm it assumes."""

    words: int
    min_words: int
    max_words: int
    parts: int
    words_per_part: int
    wpm: int


def short_budget(
    length_bucket_s: int, pacing: str, *, parts: int = 1, hook_words: int = 12, closing_words: int = 0
) -> Budget:
    """Body budget of a Short: `round(seconds*wpm/60)` per part minus hook and closing words (min 40).

    `pacing` is a key of `PACING_WPM`; limits are +-20 % of the target.
    """
    wpm = PACING_WPM[pacing]
    total = round(length_bucket_s * wpm / 60) * parts
    words = max(40, total - hook_words * parts - closing_words)
    return Budget(
        words=words,
        min_words=round(words * 0.8),
        max_words=round(words * 1.2),
        parts=parts,
        words_per_part=words // parts,
        wpm=wpm,
    )
