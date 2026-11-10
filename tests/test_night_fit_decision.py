# ruff: noqa: E501
"""Card night-fits (night-fits-a), the night's ONE time decision: `ns_studio.nightfit.decide`.

Pure: every input is given here, nothing reads a clock or a file. Two parts:
- a table over the boundaries (exactly at the margin and one second either side; a fresh night and a used
  one; no measurement, a first one, five with one outlier, two outliers; the misses; the age rule);
- a property test over a generated grid of every combination (no property-testing library in this
  repository), asserting the item's invariants for each, and a simulation of night after night for the
  progress invariant.
"""

from __future__ import annotations

import functools
import importlib
import itertools
import re
from dataclasses import replace
from typing import Any

import pytest


class _Need:
    """`ns_studio.nightfit`, reached when a test uses it: before the card the module is missing and each
    test fails at this assertion (not at its import)."""

    def __getattr__(self, name: str) -> Any:
        try:
            module = importlib.import_module("ns_studio.nightfit")
        except ImportError:
            module = None
        assert module is not None, "ns_studio.nightfit (the night's one time decision) is missing"
        return getattr(module, name)


nf: Any = _Need()

CAPACITY = 21000.0  # 6 h minus the 10-min closing margin
OVERHEAD = 900.0
ROOM = CAPACITY - OVERHEAD  # 20100: what a fresh night really has here
FLOOR = 600.0  # nightfit.STAGE_FLOOR_S


def est(samples: list[float], stage: str = "voice") -> Any:
    return nf.estimate(stage, samples, ROOM)


def ask(
    where: str,
    left: float | None,
    *,
    begun: bool = False,
    stages: tuple[Any, ...] = (),
    job_s: float | None = None,
    job: Any = None,
    load: float = 0.0,
) -> Any:
    job_s = sum(e.seconds for e in stages) if job_s is None else job_s
    return nf.decide(nf.Question(where, left, begun, ROOM, stages, job_s, nf.HISTORY, job or nf.Job(),
                                 load_s=load))


# ------------------------------------------------------------------ the estimate, the overhead, the room
def test_the_estimate_plans_with_passes_inside_the_room_and_keeps_the_rest_as_evidence() -> None:
    assert nf.STAGE_FLOOR_S == FLOOR and est([]) == nf.Estimate("voice", FLOOR)
    first = est([1200])
    assert (first.seconds, first.basis, first.passes, first.over) == (1200, nf.HISTORY, 1, 0)
    assert est([ROOM]).over == 0 and est([ROOM + 1]).over == 1, "exactly the room is inside it"
    once = est([100, 100, 100, 100, ROOM + 1])
    assert (once.seconds, once.over, once.retest, once.cannot_fit) == (100, 1, True, False)
    old = est([ROOM + 1, 100, 100, 100, 100])
    assert (old.seconds, old.retest, old.cannot_fit) == (100, False, False), "a freak older than a pass: healed"
    two = est([100, ROOM + 1, 100, 100, ROOM + 2])
    assert (two.over, two.cannot_fit, two.longest) == (2, True, ROOM + 2)
    healed = est([ROOM + 1, ROOM + 2, 100])
    assert (healed.cannot_fit, healed.seconds) == (False, 100), "one newer pass inside the room heals"
    alone = est([ROOM + 1])
    assert (alone.seconds, alone.basis, alone.retest) == (FLOOR, nf.DEFAULT, True)
    assert est([1, 2, 3, 4, 5, 6]).passes == 5, "the last five passes only"


def test_the_overhead_is_the_median_so_one_slow_wait_does_not_move_it() -> None:
    assert nf.overhead([], 900.0) == 900.0
    assert nf.overhead([60, 70, 7200], 900.0) == 70, "a 2-hour wait for the card is one night out of three"
    assert nf.overhead([60, 80], 900.0) == 70
    assert nf.default_overhead(21600) == 900 and nf.default_overhead(1800) == 150
    assert nf.room(CAPACITY, OVERHEAD) == ROOM and nf.room(100, 500) == 0


# ------------------------------------------------------------------ the table: the boundaries
def V() -> Any:  # one measured stage: 1200 s
    return est([1200])


def MISSED() -> Any:  # a job the last night missed once as its first work
    return nf.Job(cuts=1, misses=1, last_progressed=False)


TABLE: list[tuple[str, Any, str, str]] = [
    # (case, the ask(...) arguments, answer, reason)
    ("no deadline", lambda: {"where": "stage", "left": None, "stages": (V(),)}, "begin", "no_limit"),
    ("at the margin exactly", lambda: {"where": "stage", "left": 1200, "begun": True, "stages": (V(),)}, "begin", "fits"),
    ("one second more", lambda: {"where": "stage", "left": 1201, "begun": True, "stages": (V(),)}, "begin", "fits"),
    ("one second short, used night", lambda: {"where": "stage", "left": 1199, "begun": True, "stages": (V(),)},
     "stop", "stop_no_room"),
    ("one second short, fresh night", lambda: {"where": "stage", "left": 1199, "stages": (V(),)},
     "stop", "stop_first_miss"),
    ("resume one second short, used", lambda: {"where": "resume", "left": 1199, "begun": True, "stages": (V(),)},
     "not_tonight", "resume_no_room"),
    ("resume one second short, fresh", lambda: {"where": "resume", "left": 1199, "stages": (V(),)},
     "not_tonight", "resume_first_miss"),
    ("resume at the margin", lambda: {"where": "resume", "left": 1200, "begun": True, "stages": (V(),)}, "begin", "fits"),
    ("no measurement: the floor fits", lambda: {"where": "stage", "left": FLOOR, "begun": True, "stages": (est([]),)},
     "begin", "fits"),
    ("no measurement: one second short", lambda: {"where": "stage", "left": FLOOR - 1, "begun": True,
                                           "stages": (est([]),)}, "stop", "stop_no_room"),
    ("first pass over the room, fresh: retest", lambda: {"where": "stage", "left": ROOM, "stages": (est([ROOM + 1]),)},
     "begin", "fits"),
    ("first pass over the room, used: waits", lambda: {"where": "stage", "left": ROOM, "begun": True,
                                                "stages": (est([ROOM + 1]),)}, "stop", "stop_retest_waits"),
    ("first pass exactly the room, fresh", lambda: {"where": "resume", "left": ROOM, "stages": (est([ROOM]),)},
     "begin", "fits"),
    ("first pass exactly the room, fresh one second short", lambda: {"where": "resume", "left": ROOM - 1,
                                                              "stages": (est([ROOM]),)},
     "not_tonight", "resume_first_miss"),
    ("five passes, one outlier newest, used", lambda: {"where": "resume", "left": 5000, "begun": True,
                                                "stages": (est([100] * 4 + [ROOM + 1]),)},
     "not_tonight", "resume_retest_waits"),
    ("five passes, one outlier newest, fresh", lambda: {"where": "resume", "left": 5000,
                                                 "stages": (est([100] * 4 + [ROOM + 1]),)}, "begin", "fits"),
    ("five passes, one outlier older, used", lambda: {"where": "stage", "left": 100, "begun": True,
                                               "stages": (est([ROOM + 1] + [100] * 4),)}, "begin", "fits"),
    ("two outliers, the newest among them", lambda: {"where": "stage", "left": ROOM,
                                             "stages": (est([100, ROOM + 1, 100, 100, ROOM + 1]),)},
     "cannot_fit", "cannot_fit_job"),
    ("two outliers, a newer pass inside", lambda: {"where": "stage", "left": 100, "begun": True,
                                           "stages": (est([ROOM + 1, ROOM + 1, 100]),)}, "begin", "fits"),
    ("two outliers, a new job", lambda: {"where": "new", "left": ROOM,
                                 "stages": (V(), est([ROOM + 1, ROOM + 2], "render"))},
     "cannot_fit", "cannot_fit_new"),
    ("second fresh miss gives up", lambda: {"where": "resume", "left": 1199, "stages": (V(),), "job": MISSED()},
     "cannot_fit", "missed_twice"),
    ("a miss then a used night: no give-up", lambda: {"where": "resume", "left": 1199, "begun": True, "stages": (V(),),
                                               "job": MISSED()}, "not_tonight", "resume_no_room"),
    ("age exactly the rule", lambda: {"where": "resume", "left": 5000, "stages": (V(),),
                              "job": nf.Job(age_days=3.0)}, "begin", "fits"),
    ("age one second past the rule", lambda: {"where": "resume", "left": 5000, "stages": (V(),),
                                      "job": nf.Job(age_days=3.0 + 1 / 86400)}, "not_tonight", "too_old"),
    ("new job exactly fits", lambda: {"where": "new", "left": 2400, "begun": True, "stages": (V(), V())}, "begin", "fits"),
    ("new job one second short, used", lambda: {"where": "new", "left": 2399, "begun": True, "stages": (V(), V())},
     "not_tonight", "new_no_room"),
    ("new job one second short, fresh: first work", lambda: {"where": "new", "left": 2399, "stages": (V(), V())},
     "begin", "first_work"),
    ("new job of more than three nights", lambda: {"where": "new", "left": ROOM, "stages": (V(),), "job_s": 4 * ROOM},
     "begin", "first_work_long"),
    ("new job, its first stage a retest, used", lambda: {"where": "new", "left": ROOM, "begun": True,
                                                 "stages": (est([ROOM + 1]),)}, "not_tonight",
     "new_retest_waits"),
    ("wait: stage and load exactly", lambda: {"where": "wait", "left": 1260, "begun": True, "stages": (V(),), "load": 60},
     "begin", "fits"),
    ("wait: one second short", lambda: {"where": "wait", "left": 1259, "begun": True, "stages": (V(),), "load": 60},
     "stop", "stop_no_room"),
]


@pytest.mark.parametrize(("case", "args", "answer", "reason"), TABLE, ids=[t[0] for t in TABLE])
def test_the_decision_at_each_boundary(case: str, args: Any, answer: str, reason: str) -> None:
    d = ask(**args())
    assert (d.answer, d.reason) == (answer, reason), case
    assert nf.REASONS[d.reason] == d.answer


def test_the_wait_leaves_the_rest_of_the_job_or_else_the_next_stage() -> None:
    rest = (V(), est([2000], "render"))
    assert ask(nf.WAIT, 20000, stages=rest, load=60).wait_s == 7200, "the acquirer's own cap"
    assert ask(nf.WAIT, 5000, stages=rest, load=60).wait_s == 5000 - 60 - 3200
    assert ask(nf.WAIT, 3000, stages=rest, load=60).wait_s == 3000 - 60 - 1200, "the rest does not fit"
    assert ask(nf.WAIT, 1260, stages=rest, load=60).wait_s == 0
    assert ask(nf.WAIT, None, stages=rest).wait_s is None


def test_the_reviewers_band_is_tried_once_more_then_given_up_with_its_sentence() -> None:
    """Review blocker 1 at the proof's test limit (1800 s: capacity 1500 s), 70 s of start-up measured: a
    fresh night has 1430 s. A stage measured once at 1440 s is a retest, begun on a fresh night; a second
    pass over the room gives it up, and the sentence says so with the numbers."""
    room = nf.room(1500, nf.overhead([70, 70, 70], nf.default_overhead(1800)))
    assert room == 1430
    once = nf.estimate("voice", [1440], room)
    q = nf.Question(nf.RESUME, 1430, False, room, (once,), once.seconds)
    assert nf.decide(q).answer == nf.BEGIN
    twice = nf.estimate("voice", [1440, 1440], room)
    d = nf.decide(replace(q, stages=(twice,)))
    assert (d.answer, d.reason) == (nf.CANNOT_FIT, nf.CANNOT_FIT_JOB)
    assert nf.say(d, channel="after-dark", plan="master_first") == (
        "after-dark's half-made video is given up by the nightly run: its voice stage took longer than the "
        "23 min a night has here on 2 of its last 2 passes, the longest 24 min. Retry on its Queue card "
        "finishes it (a run you start has no time limit)"
    )


def test_the_one_freak_pass_is_said_as_once() -> None:
    d = ask(nf.STAGE, 5000, begun=True, stages=(est([100] * 4 + [7 * 3600]),))
    assert nf.say(d, channel="after-dark", plan="master_first") == (
        "after-dark's video stopped before voice: that stage took 7 h once, longer than the 5 h 35 min a "
        "night has here, so it is tried again only as the first work of a night; the next nightly run goes "
        "back to it before starting new videos (while Automatic runs are on in Settings)"
    )
    assert d.first_next and d.cuts == 1


def test_half_made_jobs_are_taken_up_first_by_need_then_by_age() -> None:
    jobs = [(False, "2026-11-09"), (True, "2026-11-10"), (False, "2026-11-01"), (True, "2026-11-02")]
    assert sorted(jobs, key=lambda j: nf.resume_key(*j)) == [
        (True, "2026-11-02"), (True, "2026-11-10"), (False, "2026-11-01"), (False, "2026-11-09")
    ]


def test_the_condition_is_the_ownership_constant() -> None:
    from ns_studio import ownership

    assert nf.IF_NIGHTLY_ON == ownership.IF_NIGHTLY_ON


# ------------------------------------------------------------------ the property test: every combination
SAMPLE_SETS: dict[str, list[float]] = {
    "none": [],
    "first": [1200],
    "first_at_room": [ROOM],
    "first_over": [ROOM + 1],
    "five": [900, 1000, 1100, 1200, 1150],
    "five_outlier_newest": [900, 1000, 1100, 1200, 7 * 3600],
    "five_outlier_old": [7 * 3600, 1000, 1100, 1200, 1150],
    "two_outliers_newest": [900, 7 * 3600, 1100, 1200, ROOM + 1],
    "two_outliers_old": [7 * 3600, ROOM + 1, 1100, 1200, 1150],
    "all_over": [ROOM + 1] * 5,
}
LATER = {"none": [], "fits": [300.0], "cannot": [ROOM + 5, ROOM + 9]}  # the passes of a later stage
LEFTS = [None, -5.0, 0.0, 599.0, 600.0, 601.0, 1199.0, 1200.0, 1201.0, ROOM - 1, ROOM, CAPACITY]
JOBS = [  # (misses, last_progressed): the states a record can hold
    (0, True), (0, False), (1, False),
]
WHERES = ["new", "resume", "wait", "stage"]
PROMISE = re.compile(r"next nightly run|nightly run tries|nightly run after|make them again|plans it again")


@functools.cache
def _grid() -> list[tuple[Any, str]]:
    out = []
    for (name, samples), later, left, begun, where, (misses, last), finished, age, load, big in itertools.product(
        SAMPLE_SETS.items(), LATER.values(), LEFTS, (False, True), WHERES, JOBS, (0, 1), (0.0, 3.0, 3.5),
        (0.0, 60.0), (False, True),
    ):
        if finished and not begun:
            continue  # a stage of it finished tonight: the night has begun work
        if where == "new" and (misses or finished or age or not last):
            continue  # a new job has no record
        if big and where != "new":
            continue  # only a new job's whole work is weighed (a video of more than three nights)
        stages = (est(samples), *((est(later, "render"),) if later else ()))
        job_s = sum(e.seconds for e in stages) + (3 * ROOM if big else 0)
        job = nf.Job(cuts=misses, misses=misses, last_progressed=last, finished_tonight=finished, age_days=age)
        out.append((nf.Question(where, left, begun, ROOM, stages, job_s, nf.HISTORY, job, load_s=load), name))
    return out



def test_the_invariants_hold_for_every_combination() -> None:
    grid = _grid()
    assert len(grid) > 50000, "the grid covers the combinations"
    reasons_seen: set[str] = set()
    for q, name in grid:
        d = nf.decide(q)
        reasons_seen.add(d.reason)
        nxt = q.stages[0]
        where = f"{name} {q}"
        # every reason belongs to one answer and renders one sentence with no blank left
        assert nf.REASONS[d.reason] == d.answer, where
        text = nf.say(d, channel="ch", plan="master_first")
        assert text and "{" not in text and "}" not in text, where
        if PROMISE.search(text):
            assert "while Automatic runs are on in Settings" in text, (d.reason, text)
        # 1. no stage is begun whose estimate ends after the limit minus the margin
        if d.answer == nf.BEGIN and q.left_s is not None:
            if q.where == nf.NEW:
                assert q.job_s <= q.left_s or (not q.begun_tonight and nxt.seconds <= q.left_s), where
            else:
                assert nxt.seconds + (q.load_s if q.where == nf.WAIT else 0) <= q.left_s, where
            if q.where == nf.WAIT:
                assert d.wait_s is not None and d.wait_s + q.load_s + nxt.seconds <= q.left_s, where
            assert not any(e.cannot_fit for e in q.stages), where
        # 3. given up as "cannot fit" only on evidence that survives one freak measurement
        if d.answer == nf.CANNOT_FIT:
            measured = any(e.over >= nf.OVER_MAX and e.newest_over for e in q.stages)
            missed = d.reason == nf.MISSED_TWICE and q.job.misses >= 1 and not q.begun_tonight
            assert measured or missed, where
        if all(e.over <= 1 for e in q.stages) and q.job.misses == 0:
            assert d.answer != nf.CANNOT_FIT, where
        #    one freak pass alone: the stage is still begun on a fresh night with its room
        if (q.where in (nf.RESUME, nf.STAGE) and not q.begun_tonight and q.left_s is not None
                and q.left_s >= ROOM and all(not e.cannot_fit for e in q.stages)
                and not nf.too_old(q.job.age_days, q.job.max_age_days)):
            assert d.answer == nf.BEGIN, where
        # 4. the age of a job advances unless a stage of it FINISHED this night
        assert d.refresh_age == (q.job.finished_tonight > 0), where
        if q.job.finished_tonight:
            assert d.misses == 0, where
        if d.answer != nf.BEGIN:
            assert d.cuts == q.job.cuts + 1, where
    assert reasons_seen == set(nf.REASONS), set(nf.REASONS) - reasons_seen


def test_a_freak_mark_heals_with_one_newer_pass_inside_the_room() -> None:
    """For every evidence that gives a stage up, one newer pass inside the room (Retry or Create, both with no
    time limit: the stage is measured without being begun at night) makes it plannable again."""
    for name, samples in SAMPLE_SETS.items():
        if not est(samples).cannot_fit:
            continue
        healed = est([*samples, 1000.0])
        assert not healed.cannot_fit and not healed.retest and healed.seconds <= ROOM, name
        assert ask(nf.STAGE, ROOM, stages=(healed,)).answer == nf.BEGIN, name


def test_every_reason_has_exactly_one_sentence() -> None:
    assert set(nf.SENTENCES) == set(nf.REASONS)
    assert len(set(nf.SENTENCES.values())) == len(nf.SENTENCES), "two reasons share a sentence"


# ------------------------------------------------------------------ progress, night after night
NIGHTS = {
    "fresh_full": (False, ROOM),  # the job is the night's first work and the night has its whole room
    "fresh_short": (False, 1100.0),  # first work, but a slow start left less than its stage needs
    "used": (True, 1100.0),  # other work used the night first
}


def _simulate(samples: list[float], nights: tuple[str, ...]) -> tuple[str, int]:
    """One half-made job asked at S3 night after night (one day apart); its record kept as the night keeps
    it (`daily._keep`). Returns (how it ended: begun, given_up, too_old or waiting, the night it ended)."""
    stage = est(samples)
    job = nf.Job()
    age = 0.0
    for n, kind in enumerate(nights, start=1):
        begun, left = NIGHTS[kind]
        age += 1.0
        q = nf.Question(nf.RESUME, left, begun, ROOM, (stage,), stage.seconds, nf.HISTORY,
                        replace(job, age_days=age))
        d = nf.decide(q)
        if d.answer == nf.BEGIN:
            return "begun", n
        if d.answer == nf.CANNOT_FIT:
            return "given_up", n
        if d.reason == nf.TOO_OLD:
            return "too_old", n
        job = nf.Job(cuts=d.cuts, misses=d.misses, last_progressed=d.refresh_age)
    return "waiting", len(nights)


def test_progress_every_job_is_begun_or_given_up_with_a_card_within_a_bounded_number_of_nights() -> None:
    bound = int(nf.Job().max_age_days) + 1
    cases = 0
    for name, samples in SAMPLE_SETS.items():
        stage = est(samples)
        for nights in itertools.product(NIGHTS, repeat=bound):
            cases += 1
            how, at = _simulate(samples, nights)
            assert how != "waiting", (name, nights)  # never skipped or cut for ever
            assert at <= bound, (name, nights)
            first_full = next((i + 1 for i, k in enumerate(nights) if k == "fresh_full"), None)
            if first_full is not None and stage.seconds <= ROOM and not stage.cannot_fit:
                # a stage that fits a fresh night is begun on the first night that gives it one, unless the
                # job was given up before on evidence (two fresh misses) or by age
                assert at <= first_full, (name, nights, how)
                if at == first_full:  # that night: begun, unless the age rule ended it first
                    assert how in ("begun", "too_old"), (name, nights, how)
    assert cases == len(SAMPLE_SETS) * 3**bound

