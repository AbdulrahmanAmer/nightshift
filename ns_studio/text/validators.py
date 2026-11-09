"""Hard validators for generated text (component C5; spec 16.3)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

from ns_common.embed_client import Embed, cosine
from ns_common.errors import NsError
from ns_common.pacing import master_seconds
from ns_common.paths import contracts_dir
from ns_common.textstats import fk_grade, split_sentences, word_count
from ns_studio.text.budget import MASTER_MAX_SECONDS, Budget


@dataclass(frozen=True)
class Violation:
    """One failed validator rule with a human-readable detail."""

    rule: str
    detail: str


@lru_cache(maxsize=1)
def load_banned() -> list[str]:
    """Lower-cased banned phrases from `contracts/policy/banned_phrases.txt`."""
    lines = (contracts_dir() / "policy" / "banned_phrases.txt").read_text(encoding="utf-8").splitlines()
    return [ln.strip().lower() for ln in lines if ln.strip() and not ln.startswith("#")]


def check_length(text: str, budget: Budget) -> Violation | None:
    """Word count of `text` must lie in `[budget.min_words, budget.max_words]`."""
    n = word_count(text)
    if n < budget.min_words or n > budget.max_words:
        return Violation("length", f"{n} words, allowed {budget.min_words}-{budget.max_words}")
    return None


def check_master_length(chapters: Sequence[str], wpm: float) -> Violation | None:
    """The chapters may add up to at most 14:30 of audio at `wpm`: QC fails anything over 15:00 (NS-E404).

    The estimate counts the TTS pauses (350 ms per sentence, 700 ms per paragraph, 1.2 s before each cliff
    line) and the 1.2 s title cards, not just the words.
    """
    seconds = master_seconds(chapters, wpm)
    if seconds > MASTER_MAX_SECONDS:
        return Violation(
            "master_length", f"about {seconds:.0f} s of audio at {wpm} wpm, at most {MASTER_MAX_SECONDS} s"
        )
    return None


def check_banned(text: str, banned: list[str]) -> Violation | None:
    """No banned phrase may occur (case-insensitive substring match)."""
    low = text.lower()
    hits = [b for b in banned if b in low]
    return Violation("banned_phrase", ", ".join(hits)) if hits else None


def check_reading_level(text: str, lo: float = 5.0, hi: float = 8.0) -> Violation | None:
    """Flesch-Kincaid grade of `text` must lie in `[lo, hi]`."""
    letters = [c for c in text if c.isalpha()]
    if letters and sum(c.isascii() for c in letters) / len(letters) < 0.8:
        return None  # Flesch-Kincaid is an English formula; other scripts would always read as "too easy"
    g = fk_grade(text)
    if g < lo or g > hi:
        return Violation("reading_level", f"grade {g}, allowed {lo}-{hi}")
    return None


def check_sentence_length(text: str, max_words: int = 35) -> Violation | None:
    """No sentence may exceed `max_words` words (spoken rhythm)."""
    for s in split_sentences(text):
        if word_count(s) > max_words:
            return Violation("sentence_length", f"{word_count(s)} words: {s[:60]}")
    return None


def check_opening_similarity(
    opening: str, past_openings: list[str], embed: Embed, threshold: float = 0.9
) -> Violation | None:
    """The opening line must differ (cosine < `threshold`) from every past opening."""
    if not past_openings:
        return None
    vecs = embed.embed([opening, *past_openings])
    best = max(cosine(vecs[0], v) for v in vecs[1:])
    if best >= threshold:
        return Violation("opening_similarity", f"cosine {best:.2f} >= {threshold}")
    return None


def validate_story(
    text: str, budget: Budget, *, past_openings: list[str], embed: Embed
) -> list[Violation]:
    """Run every story validator and return the violations (empty when the text passes)."""
    opening = split_sentences(text)[0] if text.strip() else ""
    checks = [
        check_length(text, budget),
        check_banned(text, load_banned()),
        check_reading_level(text),
        check_sentence_length(text),
        check_opening_similarity(opening, past_openings, embed),
    ]
    return [c for c in checks if c is not None]


def raise_if(violations: list[Violation]) -> None:
    """Raise NS-E303 listing every violation; do nothing for an empty list."""
    if violations:
        raise NsError("NS-E303", "; ".join(f"{v.rule}: {v.detail}" for v in violations))


def validate_chapter(
    text: str,
    budget: Budget,
    *,
    chapter_n: int,
    past_openings: list[str],
    embed: Embed,
) -> list[Violation]:
    """Validators for one chapter: length against its budget, banned phrases, reading level, rhythm.

    The opening-similarity rule applies to chapter 1 only (the story's opening line).
    """
    opening = split_sentences(text)[0] if text.strip() else ""
    checks = [
        check_length(text, budget),
        check_banned(text, load_banned()),
        check_reading_level(text),
        check_sentence_length(text),
        check_opening_similarity(opening, past_openings, embed) if chapter_n == 1 else None,
    ]
    return [c for c in checks if c is not None]


def check_cliff_variety(endings: list[str], embed: Embed, threshold: float = 0.92) -> Violation | None:
    """No two consecutive chapters may end on (near) the same line: cosine of endings < `threshold`."""
    if len(endings) < 2:
        return None
    vecs = embed.embed(endings)
    for i in range(len(vecs) - 1):
        sim = cosine(vecs[i], vecs[i + 1])
        if sim >= threshold:
            return Violation("cliff_repeat", f"chapters {i + 1} and {i + 2} end alike (cosine {sim:.2f})")
    return None
