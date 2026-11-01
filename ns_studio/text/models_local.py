"""Pydantic models for the master-first text artifacts (component C5).

Temporary until the change request "master-first text schemas" (contracts/CHANGE_REQUESTS.md) moves them
into `ns_common.models`; field names and constraints follow spec 15.6 exactly.
"""

from __future__ import annotations

from pydantic import Field

from ns_common.models import Seo, Strict


class PlanChapter(Strict):
    """One chapter of the long plan: mini-arc beats, the cliff it ends on and its word budget."""

    n: int = Field(ge=1)
    title: str
    beats: list[str] = Field(min_length=1)
    cliff: str
    word_budget: int = Field(ge=1)


class Plan(Strict):
    """`plan.json`: target length and 5-7 chapters."""

    target_minutes: int = Field(ge=9)
    target_words: int = Field(ge=1)
    chapters: list[PlanChapter] = Field(min_length=5, max_length=7)


class Sentence(Strict):
    """A sentence of a chapter with its 0-based index (the unit Shorts selection and beats address)."""

    idx: int = Field(ge=0)
    text: str


class StoryChapter(Strict):
    """A drafted chapter with running-summary text and its sentence index."""

    n: int = Field(ge=1)
    title: str
    text: str
    summary: str
    word_count: int = Field(ge=1)
    est_seconds: float = Field(gt=0)
    cliff_line_idx: int | None = Field(default=None, ge=0)
    sentences: list[Sentence] = Field(min_length=1)


class Story(Strict):
    """`story.json`: the master story by chapter."""

    working_title: str
    chapters: list[StoryChapter] = Field(min_length=1)
    revisions: int = Field(default=0, ge=0)


class ShortsPlanItem(Strict):
    """One derived Short: a sentence span of one chapter plus its hook candidates and closing line."""

    k: int = Field(ge=1)
    chapter: int = Field(ge=1)
    sentence_start: int = Field(ge=0)
    sentence_end: int = Field(ge=0)
    start_s: float | None = None
    end_s: float | None = None
    hooks: list[str] = Field(min_length=1, max_length=5)
    chosen_hook: str
    closing_line: str
    length_bucket: int = Field(ge=1)
    combo_id: str
    why_standalone: str


class ChapterTitle(Strict):
    """Chapter title used in the description's chapter list."""

    n: int = Field(ge=1)
    title: str


class SeoLong(Seo):
    """`seo_long.json`: `Seo` plus the chapter titles (timestamps are filled in after rendering)."""

    chapter_titles: list[ChapterTitle] = Field(min_length=1)


class ClipPlanItem(Strict):
    """One candidate clip of the Clipper's selection (spec 15.6 `clip_plan`)."""

    k: int = Field(ge=1)
    start_s: float = Field(ge=0)
    end_s: float = Field(gt=0)
    title: str = Field(max_length=100)
    hook_line: str | None = None
    why: str
    score: float = Field(ge=0, le=1)
    speakers: list[str] | None = None
    keywords: list[str] = []


class PolicyCheckResult(Strict):
    """Answer of `policy_check.md`: matched unsafe categories and the honesty / standalone verdicts."""

    unsafe_categories: list[str]
    hook_honest: bool
    title_honest: bool
    standalone: bool | None
    notes: str


class ChapterDraft(Strict):
    """Answer of `chapter_draft.md`: the chapter text and its running-summary facts."""

    text: str = Field(min_length=1)
    summary: str = Field(min_length=1)
