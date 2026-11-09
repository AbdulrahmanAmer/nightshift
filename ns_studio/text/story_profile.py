"""The story profile: the one place the text stages read their thresholds from (H-8; docs/DEV_MODE.md).

Every limit a story has to meet in the text stages (the word band of a Short or a chapter, the reading grade,
how many chapters and beats a plan has, how short a master may be planned, how many Shorts a master must
yield) is a field of `StoryProfile`. The budgets (`ns_studio.text.budget`), the validators
(`ns_studio.text.validators`), the stages and the prompts take the profile of the job (`profile_of(ctx)`) and
read these fields; nothing else in the pipeline knows a profile exists.

`NORMAL` is every install: its fields are the numbers the stages always used. `TINY` is an install made for
testing with a 1 to 2 GB language model (`tiny_story: true` in `answers.json`, then in `runtime.json`; setup
refuses it without `allow_cpu`; the dashboard cannot set it). It moves thresholds and nothing else: every
validator still runs and can still fail, and the policy gates, the safety checks, QC and the banned phrases
do not read this module at all.

Where the tiny numbers come from. Logged (`test-loop-2026-11-08.md` in the refs folder, the engine-test
Shorts jobs of 2026-11-09): a Short of 40 words at reading grade 0.21 against a budget of 54 to 72
(Qwen3-1.7B) and one of 104 words at grade 10.1 against 60 to 80 (Qwen3-0.6B). The chapter band (0.5 to 2
times the budget) and the grade floor (-2) are design choices that leave a wide margin around those; the
sizes of master chapters from a real 1.7B run are not in any log, so no number here claims them.

A tiny master is planned as the shortest master the pipeline accepts (9:00) in the most chapters its plan
allows (7), about 179 words each, so a chapter the model can write is also a chapter the master needs; the
band around it (0.5 to 2 times) leaves room for a model that lands well off its budget. A Short is still
asked for the words that fill its length bucket (QC measures the rendered Short against that bucket); only
the floor under it comes down.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ns_common.models import Runtime


@dataclass(frozen=True)
class StoryProfile:
    """The thresholds of the text stages. The defaults are the numbers of a normal install."""

    name: str = "normal"
    # Reading level: the Flesch-Kincaid grade a text must lie in, and how the prompts ask for it.
    grade_lo: float = 5.0
    grade_hi: float = 8.0
    grade_ask: str = "5-8"
    # How many concrete events the prompts ask for per chapter (long plan, series outline) and per story.
    chapter_beats_lo: int = 2
    chapter_beats_hi: int = 4
    story_beats_lo: int = 4
    story_beats_hi: int = 7
    # A Short story: the words it may have as shares of the words it is asked for. None keeps the rule of the
    # voice (+-15 %, narrowed so the rendered Short stays inside QC's duration band).
    short_band: tuple[float, float] | None = None
    # A chapter: the words it may have as shares of its word budget (spec 7.0: +-20 %).
    chapter_band: tuple[float, float] = (0.8, 1.2)
    # A master plan. None keeps the channel's draw of minutes, the chapter count its words give (5 to 7),
    # the 9:00 planning floor and the 9:00 floor of the duration check.
    master_minutes: int | None = None
    master_chapters: int | None = None
    master_floor_share: float | None = None
    master_floor_s: float | None = None
    # A finished master with fewer words than this share of its target is reported (a warning, not a stop).
    master_short_share: float = 0.8
    # The most moments a master has to yield to be accepted as Shorts material (None: the channel's number).
    moments_cap: int | None = None
    # An expansion must add at least a 1/n of the words it asked for.
    expand_divisor: int = 2
    # A series part: the words of a chapter (350: a 20 minute part at 155 wpm gives 9 chapters, spec 23.3)
    # and the share of the part's target below which it is rejected with NS-E303.
    part_chapter_words: int = 350
    part_hard_floor: float = 0.75

    @property
    def is_tiny(self) -> bool:
        """True for the engine-test profile."""
        return self.name == "tiny"

    def moments_min(self, requested: int) -> int:
        """The fewest moments a master must yield: the channel's number, capped by the profile."""
        return requested if self.moments_cap is None else min(requested, self.moments_cap)


NORMAL = StoryProfile()

TINY = StoryProfile(
    name="tiny",
    grade_lo=-2.0,  # the 1.7B wrote grade 0.21; fragments of one-syllable words are below -2
    grade_hi=8.0,  # unchanged: the ceiling is not what a small model misses
    grade_ask="3-8",
    chapter_beats_lo=1,
    chapter_beats_hi=2,
    story_beats_lo=3,
    story_beats_hi=4,
    short_band=(0.35, 1.6),
    chapter_band=(0.5, 2.0),
    master_minutes=9,
    master_chapters=7,
    master_floor_share=0.5,
    master_floor_s=520.0,  # QC's own 8:30 plus 10 s for the voice: the 30 s planning margin goes, QC stays
    master_short_share=0.5,
    moments_cap=2,
    expand_divisor=4,
    part_chapter_words=190,
    part_hard_floor=0.4,
)


def for_runtime(runtime: Runtime) -> StoryProfile:
    """The profile of an install: `TINY` when its `runtime.json` says `tiny_story`, else `NORMAL`."""
    return TINY if runtime.tiny_story else NORMAL


class HasRuntime(Protocol):
    """What `profile_of` needs of a job context: the install's runtime."""

    runtime: Runtime


def profile_of(ctx: HasRuntime) -> StoryProfile:
    """The profile of a job (a `JobContext`, or the workshop's scratch context)."""
    return for_runtime(ctx.runtime)
