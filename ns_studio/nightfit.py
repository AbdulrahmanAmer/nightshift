"""The night's ONE time decision (card night-fits, night-fits-a): for one job, at one moment of one night,
BEGIN its next stage, STOP it here for tonight (it is continued), give it up because a stage CANNOT FIT a
night on this PC, or do NOT start it TONIGHT.

Pure: no clock read, no file read. Everything it needs comes in (`Question`) and everything the callers do
and say goes out (`Decision`): the answer, its reason (one of `REASONS`; `say` renders each reason as exactly
one sentence, the one the morning note, the Last night card and the job's card show), the numbers it was
decided on, and what the job's record keeps (`misses`, `cuts`, whether its age restarts). The callers only
gather the inputs and act on the answer: `daily._tonight` (a new job, S5), `daily._resume_interrupted` (a
half-made job, S3), `nightclock.Night.before_wait` (the wait for the graphics card, S7) and
`nightclock.Night.before_stage` (from `stages.run_stage`, S6). No caller compares times itself.

The rules, in order (the truth table is in the item's report):
1. No deadline (a run started by hand): BEGIN (`no_limit`).
2. A half-made job no night has moved on (no stage finished) for more than `max_age_days`: NOT tonight
   (`too_old`; `ownership.resume_problem` uses the same `too_old` and leaves it a Retry card).
3. A remaining stage CANNOT FIT a night here when at least `OVER_MAX` (2) of its last `HISTORY_N` (5) passes
   took longer than what a fresh night really has (`room`: the limit minus the closing margin minus the
   overhead this PC measured before a night's first stage) AND the newest pass is one of them. One such pass
   alone is a RETEST: the stage is begun again, but only as the first work of a night, so a real long stage
   is measured over the room a second time (then rule 3) and a freak one is replaced by a normal pass (then it
   is planned with the normal passes again). A mark heals the same way: one newer pass inside the room (Retry
   on the given-up job's card, or a video made from Create, both without a time limit) is the newest.
4. A retest stage on a night that has begun work: STOP / NOT tonight (it waits for a night's first place).
5. A new job: BEGIN when its whole work fits what is left; else, as the night's first work, when its first
   stage fits (it is made over nights); else NOT tonight.
6. Any other job: BEGIN when the next stage (and, before a wait, the model load) fits what is left.
7. Else, as the night's first work (a fresh miss): the second such night in a row with no stage finished
   gives the job up (`missed_twice`); the first is STOP / NOT tonight and the job goes first next time.
8. Else (the night used its time on other work): STOP / NOT tonight.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field, replace

#: The passes of a stage the estimates look at (the newest last).
HISTORY_N = 5
#: Passes over a fresh night's room, the newest among them, that give a stage up (one is a freak: a retest).
OVER_MAX = 2
#: Nights a job was the night's first work since its last progress and still had too little time
#: at its next stage: the second gives it up.
MISSES_MAX = 2
#: A stage's estimate before this install has run it (or when its only pass was longer than a night).
STAGE_FLOOR_S = 10 * 60
#: What a night spends before its first stage (start, recovery, the wait for the card, the model load) on a
#: PC that has not measured it yet; a test limit keeps at most a twelfth of itself.
DEFAULT_OVERHEAD_S = 15 * 60
#: A job's estimate before this install has finished one of its plan (`basis: default`), by plan.
DEFAULT_JOB_S: dict[str, int] = {"master_first": 3 * 3600, "master_from_script": 9000, "shorts_only": 3600}
DEFAULT_OTHER_JOB_S = 3600
#: Plans whose job makes a full video (and its Shorts); every other plan makes one Short a job.
FULL_VIDEO_PLANS = ("master_first", "master_from_script", "master_from_owner_script")

HISTORY, DEFAULT = "history", "default"
BEGIN, STOP, CANNOT_FIT, NOT_TONIGHT = "begin", "stop", "cannot_fit", "not_tonight"
#: Where the night asks: before making a new job, before continuing a half-made one, before the wait for the
#: graphics card, before a stage of a job under way.
NEW, RESUME, WAIT, STAGE = "new", "resume", "wait", "stage"

# The reasons: each belongs to one answer and has exactly one sentence (`say`).
NO_LIMIT, FITS, FIRST_WORK, FIRST_WORK_LONG = "no_limit", "fits", "first_work", "first_work_long"
STOP_NO_ROOM, STOP_RETEST_WAITS, STOP_FIRST_MISS = "stop_no_room", "stop_retest_waits", "stop_first_miss"
NEW_NO_ROOM, NEW_RETEST_WAITS = "new_no_room", "new_retest_waits"
RESUME_NO_ROOM, RESUME_RETEST_WAITS = "resume_no_room", "resume_retest_waits"
RESUME_FIRST_MISS, TOO_OLD = "resume_first_miss", "too_old"
CANNOT_FIT_NEW, CANNOT_FIT_JOB, MISSED_TWICE = "cannot_fit_new", "cannot_fit_job", "missed_twice"

REASONS: dict[str, str] = {
    NO_LIMIT: BEGIN, FITS: BEGIN, FIRST_WORK: BEGIN, FIRST_WORK_LONG: BEGIN,
    STOP_NO_ROOM: STOP, STOP_RETEST_WAITS: STOP, STOP_FIRST_MISS: STOP,
    NEW_NO_ROOM: NOT_TONIGHT, NEW_RETEST_WAITS: NOT_TONIGHT, RESUME_NO_ROOM: NOT_TONIGHT,
    RESUME_RETEST_WAITS: NOT_TONIGHT, RESUME_FIRST_MISS: NOT_TONIGHT, TOO_OLD: NOT_TONIGHT,
    CANNOT_FIT_NEW: CANNOT_FIT, CANNOT_FIT_JOB: CANNOT_FIT, MISSED_TWICE: CANNOT_FIT,
}
#: Reasons after which the job asks for the night's first place (a retest, or a fresh miss to try again).
FIRST_NEXT = frozenset({STOP_RETEST_WAITS, RESUME_RETEST_WAITS, STOP_FIRST_MISS, RESUME_FIRST_MISS})


def words(seconds: float) -> str:
    """`5 h 40 min`, `52 min`, `40 s`: a duration as the owner reads it."""
    s = max(0, round(seconds))
    if s < 60:
        return f"{s} s"
    hours, minutes = divmod(s // 60, 60)  # whole minutes, never more than there were
    if hours and minutes:
        return f"{hours} h {minutes} min"
    return f"{hours} h" if hours else f"{minutes} min"


# ---------------------------------------------------------------------------------------------- estimates
@dataclass(frozen=True)
class Estimate:
    """What one pass of a stage needs on this PC, and the evidence behind it."""

    stage: str
    seconds: float  # what the night plans with
    basis: str = DEFAULT  # `history`: from passes of this plan here; `default`: `STAGE_FLOOR_S`
    passes: int = 0  # the measurements behind it (of the last `HISTORY_N`)
    over: int = 0  # of those, passes longer than a fresh night's room
    newest_over: bool = False  # the newest pass is one of them
    longest: float | None = None  # the longest of those passes

    @property
    def cannot_fit(self) -> bool:
        """Evidence that survives one freak pass: two over the room, the newest among them."""
        return self.over >= OVER_MAX and self.newest_over

    @property
    def retest(self) -> bool:
        """The newest pass alone was over the room: tried again, as a night's first work only."""
        return self.newest_over and not self.cannot_fit


def estimate(stage: str, samples: Sequence[float], room_s: float | None) -> Estimate:
    """The estimate of `stage` from its passes (oldest first; `ledger.stage_samples`): the worst of the last
    `HISTORY_N` that fit a fresh night's `room_s` (an estimate errs long); a pass over the room is evidence
    for rules 3 and 4, never a plan. No pass, or none inside the room: `STAGE_FLOOR_S`."""
    last = [float(s) for s in samples][-HISTORY_N:]
    if not last:
        return Estimate(stage, float(STAGE_FLOOR_S))
    room = math.inf if room_s is None else room_s
    normal = [s for s in last if s <= room]
    seconds, basis = (max(normal), HISTORY) if normal else (float(STAGE_FLOOR_S), DEFAULT)
    return Estimate(stage, seconds, basis, len(last), len(last) - len(normal), last[-1] > room, max(last))


def default_overhead(limit_s: int) -> float:
    return float(min(DEFAULT_OVERHEAD_S, limit_s // 12))


def overhead(samples: Sequence[float], default_s: float) -> float:
    """What a night spends before its first stage here: the median of the last `HISTORY_N` nights (one slow
    wait for the graphics card does not move it), else `default_s`."""
    last = sorted(float(s) for s in samples[-HISTORY_N:])
    if not last:
        return default_s
    mid = len(last) // 2
    return last[mid] if len(last) % 2 else (last[mid - 1] + last[mid]) / 2


def room(capacity_s: float, overhead_s: float) -> float:
    """What a fresh night really has for work: the limit minus the closing margin minus the overhead."""
    return max(0.0, capacity_s - overhead_s)


def job_seconds(plan: str, stages: Sequence[Estimate], *, whole: bool) -> tuple[float, str]:
    """(seconds, basis) of the work of `stages`: their sum; a whole new job of a plan this PC measured none
    of is `DEFAULT_JOB_S` (a first night under-fills rather than gets killed)."""
    if whole and not any(e.passes for e in stages):
        return float(DEFAULT_JOB_S.get(plan, DEFAULT_OTHER_JOB_S)), DEFAULT
    basis = DEFAULT if any(e.basis == DEFAULT for e in stages) else HISTORY
    return float(sum(e.seconds for e in stages)), basis


def too_old(age_days: float, max_age_days: float) -> bool:
    """The age rule: a job no night has moved on for more than `max_age_days` is left for Retry."""
    return age_days > max_age_days


# ---------------------------------------------------------------------------------------------- the decision
@dataclass(frozen=True)
class Job:
    """What the night knows of the job (its owner record; a new job has the defaults)."""

    cuts: int = 0  # nights it was already left for time
    misses: int = 0  # nights since its last progress it was the first work and still had too little time
    last_progressed: bool = True  # its last night finished at least one of its stages (or it is new)
    finished_tonight: int = 0  # its stages finished in this night so far
    age_days: float = 0.0  # since its creation or the last night that finished one of its stages
    max_age_days: float = 3


@dataclass(frozen=True)
class Question:
    where: str  # NEW, RESUME, WAIT, STAGE
    left_s: float | None  # seconds to the deadline now (after all the night spent); None: no deadline
    begun_tonight: bool  # this night has begun a stage (of any job)
    room_s: float | None  # what a fresh night really has here
    stages: tuple[Estimate, ...]  # the job's stages still to run, the next first
    job_s: float = 0.0  # the work of those stages (a new job: `job_seconds(whole=True)`)
    job_basis: str = HISTORY
    job: Job = field(default_factory=Job)
    load_s: float = 0.0  # WAIT: the model load after the wait (measured; 0 when unknown)
    wait_cap_s: int = 7200  # WAIT: the acquirer's own cap


@dataclass(frozen=True)
class Decision:
    answer: str
    reason: str
    where: str
    stage: str | None
    need_s: float | None  # what was compared with `left_s` (the next stage, a new job's whole work)
    left_s: float | None
    basis: str
    room_s: float | None = None
    passes: int = 0
    over: int = 0
    longest: float | None = None
    wait_s: int | None = None  # BEGIN at WAIT: how long the wait for the card may take
    nights: int | None = None  # FIRST_WORK: the nights the job is made over
    cuts: int = 0  # the job's record after this decision
    misses: int = 0
    refresh_age: bool = False  # its age restarts: a stage of it finished this night
    first_next: bool = False  # it asks for the next night's first place
    max_age_days: float = 3  # the job's age rule (for the words)


def decide(q: Question) -> Decision:
    """The night's one answer for one job now (module docstring, rules 1 to 8)."""
    nxt = q.stages[0] if q.stages else None
    progressed = q.job.finished_tonight > 0
    need = (nxt.seconds if nxt is not None else 0.0) + (q.load_s if q.where == WAIT else 0.0)
    base = Decision(
        BEGIN, FITS, q.where, nxt.stage if nxt else None, need, q.left_s,
        nxt.basis if nxt else q.job_basis, q.room_s,
        cuts=q.job.cuts, misses=0 if progressed else q.job.misses, refresh_age=progressed,
        max_age_days=q.job.max_age_days,
    )

    def answer(reason: str, *, about: Estimate | None = None, **more: object) -> Decision:
        out = replace(base, answer=REASONS[reason], reason=reason, **more)  # type: ignore[arg-type]
        if about is not None:
            out = replace(out, stage=about.stage, passes=about.passes, over=about.over, longest=about.longest)
        if out.answer != BEGIN:
            out = replace(out, cuts=q.job.cuts + 1, first_next=reason in FIRST_NEXT)
        return out

    if q.left_s is None:  # 1
        return answer(NO_LIMIT)  # (a wait has the acquirer's own cap: `wait_s` None)
    left = q.left_s
    if q.where == RESUME and too_old(q.job.age_days, q.job.max_age_days):  # 2
        return answer(TOO_OLD)
    blocked = next((e for e in q.stages if e.cannot_fit), None)  # 3
    if blocked is not None:
        return answer(CANNOT_FIT_NEW if q.where == NEW else CANNOT_FIT_JOB, about=blocked,
                      need_s=blocked.longest, basis=HISTORY)
    if nxt is None:  # nothing left to run: nothing to refuse
        return answer(FITS, wait_s=min(q.wait_cap_s, max(0, int(left))) if q.where == WAIT else None)
    fresh = not q.begun_tonight
    if nxt.retest and not fresh:  # 4
        waits = {NEW: NEW_RETEST_WAITS, RESUME: RESUME_RETEST_WAITS}.get(q.where, STOP_RETEST_WAITS)
        return answer(waits, about=nxt)
    if q.where == NEW:  # 5
        if q.job_s <= left:
            return answer(FITS, need_s=q.job_s, basis=q.job_basis)
        if fresh and need <= left:
            nights = 1 + math.ceil((q.job_s - left) / q.room_s) if q.room_s else None
            long = nights is not None and nights > q.job.max_age_days
            return answer(FIRST_WORK_LONG if long else FIRST_WORK, about=nxt if nxt.retest else None,
                          need_s=q.job_s, basis=q.job_basis, nights=nights)
        return answer(NEW_NO_ROOM, need_s=q.job_s, basis=q.job_basis)
    if need <= left:  # 6
        if q.where != WAIT:
            return answer(FITS, about=nxt if nxt.retest else None)
        after_load = left - q.load_s
        keep = q.job_s if q.job_s + q.load_s <= left else nxt.seconds
        return answer(FITS, about=nxt if nxt.retest else None,
                      wait_s=max(0, min(q.wait_cap_s, int(after_load - keep))))
    if fresh:  # 7
        misses = q.job.misses + 1
        if misses >= MISSES_MAX and not q.job.last_progressed and not progressed:
            return answer(MISSED_TWICE, misses=misses)
        return answer(RESUME_FIRST_MISS if q.where == RESUME else STOP_FIRST_MISS, misses=misses)
    return answer(RESUME_NO_ROOM if q.where == RESUME else STOP_NO_ROOM)  # 8


def resume_key(first_next: bool, anchor: str) -> tuple[bool, str]:
    """The order the night takes half-made jobs up in: those that asked for a night's first place first, then
    the one no night moved on for longest (`anchor`: its last progress or its creation, ISO time)."""
    return (not first_next, anchor)


# ---------------------------------------------------------------------------------------------- the words
#: The Automatic-runs condition (`ownership.IF_NIGHTLY_ON`, repeated so this module imports nothing of the
#: studio; `tests/test_night_fit_decision.py` keeps the two equal).
IF_NIGHTLY_ON = " (while Automatic runs are on in Settings)"

SENTENCES: dict[str, str] = {}
SENTENCES[NO_LIMIT] = "A run started by hand has no time limit, so {stage} is begun"
SENTENCES[FITS] = (
    "{stage} takes about {need} here and {left} {were} left before the night's deadline, so it is begun"
)
SENTENCES[FIRST_WORK] = (
    "{channel}'s {what} takes about {need} of work here, more than the {left} this night had left, so it "
    f"is begun as the night's first work and made over about {{nights}} nights: each nightly run after this "
    f"one continues it where the last one stopped{IF_NIGHTLY_ON}"
)
SENTENCES[FIRST_WORK_LONG] = (
    "{channel}'s {what} takes about {need} of work here, more than the {left} this night had left, so it "
    "is begun as the night's first work and made over about {nights} nights: each nightly run after this "
    f"one continues it where the last one stopped{IF_NIGHTLY_ON}. That is more than {{max_age}} nights for "
    "one video. Once it is left half made, Retry on its Queue card finishes it in one go (a run you start "
    "has no time limit). To have {channel}'s next videos in one night, make them from Create, or set "
    "Default type to Shorts only for {channel} in Channels (the nightly run then no longer continues this "
    "half-made full video; Retry on its card still finishes it)"
)
SENTENCES[STOP_NO_ROOM] = (
    "{channel}'s video stopped before {stage}: that stage takes about {need} here and {left} {were} left; "
    f"the next nightly run goes back to it before starting new videos{IF_NIGHTLY_ON}"
)
SENTENCES[STOP_RETEST_WAITS] = (
    "{channel}'s video stopped before {stage}: that stage took {longest} once, longer than the {room} a "
    "night has here, so it is tried again only as the first work of a night; the next nightly run goes back "
    f"to it before starting new videos{IF_NIGHTLY_ON}"
)
SENTENCES[STOP_FIRST_MISS] = (
    "{channel}'s video stopped before {stage}: even as the night's first work only {left} {were} left for "
    "that stage, which takes about {need} here; the nightly run tries it once more as the first work of a "
    f"night and gives it up for Retry if that time is too short too{IF_NIGHTLY_ON}"
)
SENTENCES[NEW_NO_ROOM] = (
    "{channel}'s {what} {was} not started: {took}, and {left} {were} left; the channel stays due, so the "
    f"next nightly run plans it again, first{IF_NIGHTLY_ON}"
)
SENTENCES[NEW_RETEST_WAITS] = (
    "{channel}'s {what} {was} not started: its first stage, {stage}, took {longest} once, longer than the "
    "{room} a night has here, so it is tried again only as the first work of a night; the channel stays due, "
    f"so the next nightly run plans it again{IF_NIGHTLY_ON}"
)
SENTENCES[RESUME_NO_ROOM] = (
    "{channel}'s half-made video was not continued: its next stage, {stage}, takes about {need} here and "
    f"{{left}} {{were}} left; the next nightly run goes back to it before starting new videos{IF_NIGHTLY_ON}"
)
SENTENCES[RESUME_RETEST_WAITS] = (
    "{channel}'s half-made video was not continued: its next stage, {stage}, took {longest} once, longer "
    "than the {room} a night has here, so it is tried again only as the first work of a night; the next "
    f"nightly run goes back to it before starting new videos{IF_NIGHTLY_ON}"
)
SENTENCES[RESUME_FIRST_MISS] = (
    "{channel}'s half-made video was not continued: even as the night's first work only {left} {were} left "
    "for its next stage, {stage}, which takes about {need} here; the nightly run tries it once more as the "
    f"first work of a night and gives it up for Retry if that time is too short too{IF_NIGHTLY_ON}"
)
SENTENCES[TOO_OLD] = (
    "{channel}'s half-made video was not continued: no night has finished a stage of it for more than "
    "{max_age} days, so it is left for Retry on its Queue card (a run you start has no time limit)"
)
SENTENCES[CANNOT_FIT_NEW] = (
    "{channel}'s {whats} are not made at night: their {stage} stage took longer than the {room} a night "
    "has here on {over} of its last {passes} passes, the longest {longest}. {ways}; one pass of that stage "
    f"inside a night's time, made from Create, lets the nightly run make them again{IF_NIGHTLY_ON}"
)
SENTENCES[CANNOT_FIT_JOB] = (
    "{channel}'s half-made video is given up by the nightly run: its {stage} stage took longer than the "
    "{room} a night has here on {over} of its last {passes} passes, the longest {longest}. Retry on its "
    "Queue card finishes it (a run you start has no time limit)"
)
SENTENCES[MISSED_TWICE] = (
    "{channel}'s half-made video is given up by the nightly run: it was the night's first work on {misses} "
    "nights since a stage of it was last finished, each time with too little time left for its {stage} "
    "stage, which takes about {need} here ({left} {were} left tonight). Retry on its Queue card finishes it "
    "(a run you start has no time limit)"
)


def say(d: Decision, *, channel: str, plan: str, jobs: int = 1) -> str:
    """The one sentence of `d.reason`, with the decision's own numbers (no full stop at the end)."""
    full = plan in FULL_VIDEO_PLANS
    one, many = ("full video", "full videos") if full else ("Short", "Shorts")
    what = one if jobs == 1 else f"{jobs} {many}"
    need = words(d.need_s or 0)
    if d.basis == DEFAULT and d.where == NEW:
        one_or_each = "one" if jobs == 1 else "each"
        took = f"Nightshift plans {need} for {one_or_each} until it has timed one on this PC"
    else:
        took = f"{'it' if jobs == 1 else 'each'} takes about {need} here"
    ways = (
        "Make them from Create (a run you start has no time limit), or set Default type to Shorts only for "
        f"{channel} in Channels" if full else "Make them from Create (a run you start has no time limit)"
    )
    left = d.left_s if d.left_s is not None else 0.0
    return SENTENCES[d.reason].format(
        channel=channel, stage=d.stage or "its next stage", what=what, whats=many, need=need, took=took,
        left=words(left), were="was" if 0 <= left < 120 else "were", was="was" if jobs == 1 else "were",
        room=words(d.room_s or 0), longest=words(d.longest or 0), over=d.over, passes=d.passes,
        nights=d.nights or 2, misses=d.misses, max_age=_whole(d.max_age_days), ways=ways,
    )


def _whole(n: float) -> str:
    return str(int(n)) if float(n).is_integer() else str(n)
