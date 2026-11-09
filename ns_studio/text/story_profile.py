"""The story profile: the one place the stages read their length thresholds from (H-8, H-8b; DEV_MODE.md).

Every limit a story has to meet is a field of `StoryProfile`: the word band of a Short or a chapter, the
reading grade, how many chapters and beats a plan has, how short a master may be planned, how many Shorts a
master must yield (H-8) and, since H-8b, the DURATION thresholds that follow from them: the longest master
the text estimate accepts, the shortest master the duration check accepts (NS-E305), QC's duration window of
a master and of a Short, and the runtime floor of the final policy gate. The budgets
(`ns_studio.text.budget`), the validators (`ns_studio.text.validators`), the stages, QC
(`ns_studio.media.qc`) and the prompts take the profile of the job (`profile_of(ctx)`, or
`duration_profile(ctx)` for the duration numbers) and read these fields.

`NORMAL` is every install: its fields are the numbers the stages always used (a field that is `None` keeps
the module constant it replaces, so a test that patches `qc.LONG_RANGE_S` still works). `TINY` is an install
made for testing with a 1 to 2 GB language model (`tiny_story: true` in `answers.json`, then in
`runtime.json`; setup refuses it without `allow_cpu`; the dashboard cannot set it). LENGTH is part of the
profile and quality is not: a tiny install is held to a shorter video, but every validator still runs and
can still fail, and QC's loudness and true peak, resolution, caption drift, black-frame, hook and file
checks, the policy and safety gates and the banned phrases do not read this module. A video made under the
profile carries `profile: tiny_story` (`mark`) in its render report, its QC report, its outbox sidecar and
its `outputs` row, so it is never mistaken for a real one.

Where the tiny numbers come from. Logged (`test-loop-2026-11-08.md` in the refs folder, the engine-test
Shorts jobs of 2026-11-09): a Short of 40 words at reading grade 0.21 against a budget of 54 to 72
(Qwen3-1.7B) and one of 104 words at grade 10.1 against 60 to 80 (Qwen3-0.6B). Logged (job `engine-
test-20261109-8071`, the 1.7B on the processor, asked for 5 chapters of 394 words): it wrote chapters of
146, 99, 126, 162 and 323 words (856 in all, median 146, the stage measured chapter 1 at 120 after the
critic's rewrite). The chapter band (0.5 to 3 times the budget) and the grade floor (-2) are design choices
that contain those figures with a margin.

A tiny master is therefore asked for what such a model writes: 4 minutes in the fewest chapters the plan
allows (5), about 110 words each (549 words at 155 wpm: 549 x (60/155 + 0.035) s of speech and pauses plus 4
x 2.05 s of cards and cliff pauses is 240 s). It is accepted from half of it, 2:00 (120 s, `master_floor_s`;
half the words is `master_floor_share`), up to twice it, 8:00 (480 s, `master_max_s`: five chapters at the
top of their band would be longer, and a runaway is still a failure). QC's window is those numbers less 10 s
at the low end (the voice differs from the estimate by a few seconds) and plus 30 s at the high end, exactly
the margins a normal master has (8:30 against 9:00, 15:00 against 14:30): 110 to 510 s. The 1.7B's 856 words
are 6:09, inside all of them; a tenth of that is far under every floor. A Short is still asked for the words
that fill its length bucket; the text band is 0.35 to 1.6 times them, and QC measures the rendered Short
against the same shares of its bucket (a Short at either edge of the text band, voiced with its 12 word
hook, lands inside the window of every bucket and pace; `tests/test_tiny_story_length.py`).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from ns_common.models import Runtime

#: The value of `profile` in every report, sidecar and row a tiny install's video leaves behind.
Mark = Literal["tiny_story"]
MARK_TINY: Mark = "tiny_story"
#: The job-folder note that keeps the mark when the switch is turned off and the job is resumed.
PROFILE_FILE = "profile.json"


@dataclass(frozen=True)
class StoryProfile:
    """The thresholds of the text and duration stages. The defaults are the numbers of a normal install."""

    name: str = "normal"
    #: What a video made under this profile is marked with (None: nothing is written anywhere).
    mark: Mark | None = None
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
    # voice (+-15 %, narrowed so the rendered Short stays inside QC's duration band). The same shares of its
    # length bucket are QC's duration window of the rendered Short (None: the bucket +-15 %).
    short_band: tuple[float, float] | None = None
    # A chapter: the words it may have as shares of its word budget (spec 7.0: +-20 %).
    chapter_band: tuple[float, float] = (0.8, 1.2)
    # A master plan. None keeps the channel's draw of minutes, the chapter count its words give (5 to 7),
    # the 9:00 planning floor and the 9:00 floor of the duration check.
    master_minutes: int | None = None
    master_chapters: int | None = None
    master_floor_share: float | None = None
    master_floor_s: float | None = None
    # Seconds, H-8b. The longest master the text estimate accepts (None: 14:30) and QC's duration window of a
    # master (None: 8:30 to 15:00).
    master_max_s: float | None = None
    master_qc_s: tuple[float, float] | None = None
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
    mark=MARK_TINY,
    grade_lo=-2.0,  # the 1.7B wrote grade 0.21; fragments of one-syllable words are below -2
    grade_hi=8.0,  # unchanged: the ceiling is not what a small model misses
    grade_ask="3-8",
    chapter_beats_lo=1,
    chapter_beats_hi=2,
    story_beats_lo=3,
    story_beats_hi=4,
    short_band=(0.35, 1.6),
    chapter_band=(0.5, 3.0),  # 99 to 323 words were written against a budget of 110
    master_minutes=4,  # the model writes about 120 words a chapter: 5 chapters are 4 minutes
    master_chapters=5,  # the fewest the Plan model takes
    master_floor_share=0.5,
    master_floor_s=120.0,  # half the target: 2:00
    master_max_s=480.0,  # twice the target: 8:00
    master_qc_s=(110.0, 510.0),  # the floor less 10 s, the ceiling plus 30 s
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
    """The profile of a job (a `JobContext`, or the workshop's scratch context): the install's, as now."""
    return for_runtime(ctx.runtime)


def duration_profile(ctx: HasRuntime) -> StoryProfile:
    """The profile whose DURATION numbers apply to the videos of this job.

    An owner's own script is not a model-size problem: its length is checked when the job is made, so it keeps
    the normal floor, ceiling and QC window on every install."""
    if getattr(ctx, "owner_script", False) is True:
        return NORMAL
    return profile_of(ctx)


# ------------------------------------------------------------------------------------------ the marker
def job_mark(ctx: HasRuntime) -> Mark | None:
    """`tiny_story` when this job's videos are test videos, else None.

    That is when the install's switch is on now, or when a run of this job noted it in the job folder
    (`note_job`): a job started with the switch on and resumed after it was turned off stays a test video."""
    if getattr(ctx, "fmt", None) == "clip":
        return None  # a clip is a cut of the owner's own video, not a model's story
    live = profile_of(ctx).mark
    if live is not None:
        return live
    job_dir = getattr(ctx, "job_dir", None)
    if job_dir is None:
        return None
    try:
        data = json.loads((Path(job_dir) / PROFILE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    mark = data.get("profile") if isinstance(data, dict) else None
    return MARK_TINY if mark == MARK_TINY else None


def note_job(ctx: Any) -> None:
    """Leave `profile.json` in the job folder when a run starts with the switch on (never for a clip job)."""
    if getattr(ctx, "fmt", None) == "clip" or profile_of(ctx).mark is None:
        return
    note = Path(ctx.job_dir) / PROFILE_FILE
    if not note.exists():
        ctx.write_json(PROFILE_FILE, {"profile": profile_of(ctx).mark})


def with_mark(ctx: HasRuntime, report: dict[str, Any]) -> dict[str, Any]:
    """`report` (item -> entry) with `profile` in every entry if this job is marked; else `report` itself."""
    mark = job_mark(ctx)
    if mark is None:
        return report
    return {item: {**entry, "profile": mark} for item, entry in report.items()}


def entry_with_mark(ctx: HasRuntime, entry: dict[str, Any]) -> dict[str, Any]:
    """One report entry with `profile` in it when this job is marked; else `entry` itself."""
    mark = job_mark(ctx)
    return entry if mark is None else {**entry, "profile": mark}
