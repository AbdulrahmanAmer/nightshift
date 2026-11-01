"""Deprecated alias of the master-first text models (component C5).

The models moved to `ns_common.models` (CR-C5-1, applied in e0c34f5). Import them from there; this module only
re-exports them for one release and will be deleted afterwards. `ChapterDraft` (the answer of
`chapter_draft.md`, a prompt-local shape without a published schema) stays here.
"""

from __future__ import annotations

from pydantic import Field

from ns_common.models import (
    ChapterTitle,
    ClipPlanItem,
    Plan,
    PlanChapter,
    PolicyCheckResult,
    Sentence,
    SeoLong,
    ShortsPlanItem,
    Story,
    StoryChapter,
    Strict,
)

__all__ = [
    "ChapterDraft",
    "ChapterTitle",
    "ClipPlanItem",
    "Plan",
    "PlanChapter",
    "PolicyCheckResult",
    "Sentence",
    "SeoLong",
    "ShortsPlanItem",
    "Story",
    "StoryChapter",
]


class ChapterDraft(Strict):
    """Answer of `chapter_draft.md`: the chapter text and its running-summary facts."""

    text: str = Field(min_length=1)
    summary: str = Field(min_length=1)
