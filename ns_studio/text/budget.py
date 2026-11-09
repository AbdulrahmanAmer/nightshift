"""Word budgets derived from pacing and length buckets (component C5; spec 7.0)."""

from __future__ import annotations

import random
from dataclasses import dataclass

from ns_common.pacing import (  # PACING_WPM is re-exported: the voice stage and the tests import it here
    PACING_WPM,
    PAUSE_PER_WORD_S,
    SHORT_QC_MARGIN,
    master_fixed_seconds,
    words_for_seconds,
)
from ns_studio.text.story_profile import NORMAL, StoryProfile

SHORT_TOLERANCE = 0.15  # Shorts: words within +-15 % of the target, the same band QC applies to seconds
MASTER_MAX_MINUTES = 14  # the longest draw of `long_minutes`; the budget below already counts the pauses
MASTER_MAX_SECONDS = 870  # a finished master may estimate up to 14:30 (QC fails over 15:00 after the render)


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
    length_bucket_s: int,
    pacing: str,
    *,
    parts: int = 1,
    hook_words: int = 12,
    closing_words: int = 0,
    wpm: int | None = None,
    profile: StoryProfile = NORMAL,
) -> Budget:
    """Body budget of a Short: the words that fill `length_bucket_s` per part, pauses included, minus hook and
    closing words (min 40).

    `pacing` is a key of `PACING_WPM`; `wpm` replaces its figure with the pace measured for the job's voice
    (`ns_studio.voice_wpm`, spec 7.0). The TTS service leaves about 0.035 s of silence per word (350 ms per
    sentence, 700 ms per paragraph), so the words are `seconds / (60/wpm + 0.035)`, not `seconds*wpm/60`.
    Limits are +-15 % of the target, narrowed so a story at either edge is still within 12 % of the bucket in
    seconds (hook and closing included): inside QC's +-15 % duration band with room for sentence-length
    variation. A profile with a `short_band` (the engine test's tiny model) keeps the target and takes its
    limits from that band instead.
    """
    wpm = wpm or PACING_WPM[pacing]
    total = words_for_seconds(length_bucket_s * parts, wpm)
    words = max(40, total - hook_words * parts - closing_words)
    voiced_extra_s = (hook_words * parts + closing_words) * (60.0 / wpm + PAUSE_PER_WORD_S)
    qc_cap = words_for_seconds(length_bucket_s * parts * (1 + SHORT_QC_MARGIN) - voiced_extra_s, wpm)
    qc_floor = words_for_seconds(length_bucket_s * parts * (1 - SHORT_QC_MARGIN) - voiced_extra_s, wpm)
    lo = min(words, max(round(words * (1 - SHORT_TOLERANCE)), qc_floor))
    hi = max(words, min(round(words * (1 + SHORT_TOLERANCE)), qc_cap))
    if profile.short_band is not None:
        lo, hi = round(words * profile.short_band[0]), round(words * profile.short_band[1])
    return Budget(
        words=words,
        min_words=lo,
        max_words=hi,
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
    long_minutes: int | tuple[int, int],
    pacing: str,
    *,
    rng: random.Random | None = None,
    wpm: int | None = None,
    profile: StoryProfile = NORMAL,
) -> MasterBudget:
    """Budget of a master: the words that fill `minutes` of audio, pauses and cards included; 5-7 chapters.

    Speech alone would be `minutes * wpm` words, but the TTS service adds about 0.035 s of silence per word,
    1.2 s before each chapter's cliff line (instead of 0.35 s) and a 1.2 s title card between chapters, so a
    14-minute draw rendered at `minutes * wpm` words came out near 15:12. `min_words` is the same sum for the
    9-minute planning floor.

    `long_minutes` is a fixed number of minutes or the channel's `(lo, hi)` range: with `rng` the target is
    drawn with `rng.randint(lo, hi)`, without it the rounded-up midpoint is used (deterministic).
    `wpm` replaces the pacing table's figure with the pace measured for the job's voice (spec 7.0).
    Raises ValueError when the minutes are below the 9-minute planning floor.

    A profile with `master_minutes` (the engine test's tiny model) plans at most that many minutes, with
    `master_chapters` chapters when it names a count, and scales the planning floor by `master_floor_share`.
    """
    wpm = wpm or PACING_WPM[pacing]
    if isinstance(long_minutes, tuple):
        lo, hi = long_minutes
        minutes = rng.randint(lo, hi) if rng is not None else (lo + hi + 1) // 2
    else:
        minutes = long_minutes
    if minutes < MASTER_FLOOR_MINUTES:
        raise ValueError(f"long_minutes {minutes} is below the {MASTER_FLOOR_MINUTES}-minute floor")
    if minutes > MASTER_MAX_MINUTES:
        raise ValueError(f"long_minutes {minutes} is above the {MASTER_MAX_MINUTES}-minute ceiling")
    if profile.master_minutes is not None:
        minutes = min(minutes, profile.master_minutes)
    chapters = MIN_CHAPTERS
    if profile.master_chapters is not None:
        chapters = profile.master_chapters
    else:
        for _ in range(3):  # the chapter count depends on the words, the words on the chapter count
            target = words_for_seconds(minutes * 60, wpm, fixed_s=master_fixed_seconds(chapters))
            chapters = max(MIN_CHAPTERS, min(MAX_CHAPTERS, round(target / CHAPTER_WORDS_MID)))
    target = words_for_seconds(minutes * 60, wpm, fixed_s=master_fixed_seconds(chapters))
    floor_words = words_for_seconds(
        MASTER_FLOOR_MINUTES * 60, wpm, fixed_s=master_fixed_seconds(MIN_CHAPTERS)
    )
    min_words = min(floor_words, target)
    if profile.master_floor_share is not None:
        min_words = round(target * profile.master_floor_share)
    return MasterBudget(
        target_minutes=minutes,
        target_words=target,
        min_words=min_words,
        wpm=wpm,
        chapters=chapters,
        chapter_budgets=distribute_words(target, chapters),
    )


def chapter_budget(words: int, wpm: int, profile: StoryProfile = NORMAL) -> Budget:
    """Budget of one chapter: `words` within the profile's band (+-20 % normally), a single part."""
    lo, hi = profile.chapter_band
    return Budget(
        words=words,
        min_words=round(words * lo),
        max_words=round(words * hi),
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
    """Most words a master may hold: `MASTER_MAX_SECONDS` of audio at `wpm`, pauses and cards included."""
    return words_for_seconds(MASTER_MAX_SECONDS, wpm, fixed_s=master_fixed_seconds(MIN_CHAPTERS))


def moment_limits(body_words: int) -> tuple[int, int]:
    """Smallest and largest word count of a Short moment whose bucket asks for `body_words`."""
    return round(body_words * (1 - SHORT_TOLERANCE)), round(body_words * (1 + SHORT_TOLERANCE))


def short_body_words(bucket_s: int, wpm: int, *, hook_words: int = 12, closing_words: int = 12) -> int:
    """Words of narration a derived Short may take from the master for a `bucket_s` second bucket.

    The bucket's total words minus the freshly voiced hook and closing line (spec 7.0); at least 20.
    """
    return max(20, round(bucket_s * wpm / 60) - hook_words - closing_words)
