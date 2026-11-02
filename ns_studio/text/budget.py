"""Word budgets derived from pacing and length buckets (component C5; spec 7.0)."""

from __future__ import annotations

import random
from dataclasses import dataclass

from ns_common.pacing import PACING_WPM as PACING_WPM  # re-exported: shared with the voice stage

SHORT_TOLERANCE = 0.15  # Shorts: words within +-15 % of the target, the same band QC applies to seconds
CHAPTER_TOLERANCE = 0.2  # chapters: spec 7.0 allows +-20 %
MASTER_MAX_MINUTES = 14  # QC fails a master over 15:00; pauses and chapter cards need the last minute


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
        min_words=round(words * (1 - SHORT_TOLERANCE)),
        max_words=round(words * (1 + SHORT_TOLERANCE)),
        parts=parts,
        words_per_part=words // parts,
        wpm=wpm,
    )


CHAPTER_WORDS_MID = 375  # midpoint of the 300-450 words a chapter should have (spec 7.0)
MIN_CHAPTERS = 5
MAX_CHAPTERS = 7
MASTER_FLOOR_MINUTES = 9  # planning floor: rendered master must reach 8:30, planned words reach 9:00


@dataclass(frozen=True)
class MasterBudget:
    """Word plan of a long-form master: target, planning floor and one budget per chapter."""

    target_minutes: int
    target_words: int
    min_words: int
    wpm: int
    chapters: int
    chapter_budgets: tuple[int, ...]


def distribute_words(total: int, n: int) -> tuple[int, ...]:
    """Split `total` words over `n` chapters as evenly as possible; the parts sum to exactly `total`."""
    base, rem = divmod(total, n)
    return tuple(base + (1 if i < rem else 0) for i in range(n))


def master_budget(
    long_minutes: int | tuple[int, int], pacing: str, *, rng: random.Random | None = None
) -> MasterBudget:
    """Budget of a master: `target_words = minutes * wpm`, `min_words = 9 * wpm`, 5-7 chapters.

    `long_minutes` is a fixed number of minutes or the channel's `(lo, hi)` range: with `rng` the target is
    drawn with `rng.randint(lo, hi)`, without it the rounded-up midpoint is used (deterministic).
    Raises ValueError when the minutes are below the 9-minute planning floor.
    """
    wpm = PACING_WPM[pacing]
    if isinstance(long_minutes, tuple):
        lo, hi = long_minutes
        minutes = rng.randint(lo, hi) if rng is not None else (lo + hi + 1) // 2
    else:
        minutes = long_minutes
    if minutes < MASTER_FLOOR_MINUTES:
        raise ValueError(f"long_minutes {minutes} is below the {MASTER_FLOOR_MINUTES}-minute floor")
    if minutes > MASTER_MAX_MINUTES:
        raise ValueError(f"long_minutes {minutes} is above the {MASTER_MAX_MINUTES}-minute ceiling")
    target = minutes * wpm
    chapters = max(MIN_CHAPTERS, min(MAX_CHAPTERS, round(target / CHAPTER_WORDS_MID)))
    return MasterBudget(
        target_minutes=minutes,
        target_words=target,
        min_words=MASTER_FLOOR_MINUTES * wpm,
        wpm=wpm,
        chapters=chapters,
        chapter_budgets=distribute_words(target, chapters),
    )


def chapter_budget(words: int, wpm: int) -> Budget:
    """Budget of one chapter: `words` +-20 %, a single part."""
    return Budget(
        words=words,
        min_words=round(words * (1 - CHAPTER_TOLERANCE)),
        max_words=round(words * (1 + CHAPTER_TOLERANCE)),
        parts=1,
        words_per_part=words,
        wpm=wpm,
    )


def master_as_budget(mb: MasterBudget) -> Budget:
    """The master seen as a `Budget` (for prompts that only need `words`, `parts` and `wpm`)."""
    return Budget(
        words=mb.target_words,
        min_words=mb.min_words,
        max_words=round(mb.target_words * 1.2),
        parts=mb.chapters,
        words_per_part=mb.target_words // mb.chapters,
        wpm=mb.wpm,
    )


def master_max_words(wpm: int) -> int:
    """Most words a master may hold: `MASTER_MAX_MINUTES` of narration at `wpm` (QC rejects over 15:00)."""
    return MASTER_MAX_MINUTES * wpm


def moment_limits(body_words: int) -> tuple[int, int]:
    """Smallest and largest word count of a Short moment whose bucket asks for `body_words`."""
    return round(body_words * (1 - SHORT_TOLERANCE)), round(body_words * (1 + SHORT_TOLERANCE))


def short_body_words(bucket_s: int, wpm: int, *, hook_words: int = 12, closing_words: int = 12) -> int:
    """Words of narration a derived Short may take from the master for a `bucket_s` second bucket.

    The bucket's total words minus the freshly voiced hook and closing line (spec 7.0); at least 20.
    """
    return max(20, round(bucket_s * wpm / 60) - hook_words - closing_words)
