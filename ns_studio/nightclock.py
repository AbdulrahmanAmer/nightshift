"""The night's clock: when the task started, the deadline it must end by, and the estimates it plans with.

Card night-fits (night-fits-a). Windows stops the nightly task `schedule_cfg.TASK_TIME_LIMIT_S` (6 h) after
it started it, whatever it is doing. A nightly run started by the task therefore never begins a job or a stage
whose estimate ends after `task start + limit - CLOSE_MARGIN_S` (the deadline): what does not fit is left for
the next night, at a stage boundary, and the owner reads in the morning what was left and why
(`notifications.time_note`). A run started by hand has no deadline and is never refused for time.

Time is an input: everything here reads the injected `Clock` (`now()` wall time for the deadline, which a
sleep shortens; `monotonic()` for durations); nothing reads the PC's clock in the middle of a decision.

Estimates come from this install's own ledger (`ledger.stage_samples`, the one rule for what counts) and
every timing decision is `nightfit.decide` (the one pure function): this module only gathers its inputs
(the time left, whether the night has begun a stage, the estimates, what a fresh night really has here, the
job's record) and acts on its answer. A stage of a plan is the WORST of its last `HISTORY_N` passes in jobs
of that plan that fit a fresh night (an estimate errs long); passes over that are evidence (`nightfit`,
rules 3 and 4), never a plan. A job is the sum of its stage estimates, never its waits; a plan this PC never
measured is `DEFAULT_JOB_S`, a stage of it `STAGE_FLOOR_S`, and the report says `basis: default`.

Within one plan a stage's time also follows the length of the story (voice, captions and render with its
words, clips and the Short combos with the number of Shorts); the channel's length band bounds that spread and
the ledger does not record those counts yet, so the worst recent pass of the same plan stands for it.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from ns_common.atomic import write_text_atomic
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_studio import ledger, nightfit
from ns_studio.errors import OUT_OF_TIME, StudioError
from ns_studio.nightfit import DEFAULT_JOB_S, DEFAULT_OTHER_JOB_S, HISTORY_N, STAGE_FLOOR_S, words

if TYPE_CHECKING:
    from ns_studio.context import JobContext

__all__ = ["DEFAULT_JOB_S", "DEFAULT_OTHER_JOB_S", "HISTORY_N", "STAGE_FLOOR_S", "words"]

#: Set by the launch script (`installer/launch.ps1 -Nightly`) to its own start, in UTC: the task's start.
STARTED_ENV = "NS_NIGHT_STARTED"
#: A shorter limit for a proof, read only on a dev or engine-test install (`devmode.test_install`).
TEST_LIMIT_ENV = "NS_TEST_NIGHT_LIMIT_S"
TEST_LIMIT_MIN_S, TEST_LIMIT_MAX_S = 60, 21600
#: Kept free at the end of the night: the report, the publish times, the backup and the morning note.
CLOSE_MARGIN_S = 10 * 60
#: The hard mark: still running this long before Windows' limit, the night says so (`cut_expected`).
HARD_MARK_S = 120
#: How often the night writes `run/night-beat.json` and looks at the hard mark.
BEAT_S = 30.0
BEAT_FILE = "night-beat.json"
#: A task start this far in the future is not this night's (a clock set back, a variable from elsewhere).
FUTURE_SLACK_S = 300
#: Card night-fits-b, 4b.4: the PC's sleep counter (`Clock.asleep`) grew by more than this between two beats:
#: the PC slept (a `slept` ledger line). A job whose error comes within `WAKE_GRACE_S` after the wake stopped
#: because of the sleep: it is interrupted. A later error is an ordinary failure.
SLEEP_GAP_S = 90
WAKE_GRACE_S = 120

START_STUB, START_ANCESTOR, START_PROCESS = "stub", "ancestor", "process"


class Clock(Protocol):
    """Wall time (aware, UTC) and a monotonic counter for durations. A clock may also have `asleep()`: the
    seconds the PC has spent asleep or hibernating since it started (None when unknown); without it the night
    finds no sleep (`Night.tick`)."""

    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    """The PC's clocks."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def asleep(self) -> float | None:
        """`ns_common.winevents.asleep_s`: the interrupt time minus the unbiased one (sleep and hibernation
        only; a change of the wall clock or a stalled process does not move it)."""
        from ns_common.winevents import asleep_s

        return asleep_s()


def _asleep(clock: Clock) -> float | None:
    read = getattr(clock, "asleep", None)
    if read is None:
        return None
    try:
        value = read()
    except Exception:  # noqa: BLE001 - unknown: no sleep is found
        return None
    return float(value) if isinstance(value, int | float) else None


@dataclass(frozen=True)
class Sleep:
    """A sleep of the PC during the night: between the last beat before it and the first one after it."""

    frm: datetime
    to: datetime
    slept_s: float


def limit_words(limit_s: int) -> str:
    """`6 hours` for a whole number of hours, else `words`."""
    if limit_s % 3600 == 0:
        n = limit_s // 3600
        return f"{n} hour" if n == 1 else f"{n} hours"
    return words(limit_s)


def limit_adjective(limit_s: int) -> str:
    """`6-hour`, `30-minute`: the limit inside a sentence."""
    if limit_s % 3600 == 0:
        return f"{limit_s // 3600}-hour"
    return f"{max(1, round(limit_s / 60))}-minute"


class OutOfTime(StudioError):
    """The night's decision before a stage or a wait for the graphics card was not BEGIN (`nightfit`): the
    job stops there (NS-W211), its finished stages kept; `daily._left_for_time` acts on `decision` (STOP: it
    is continued; CANNOT_FIT: it is given up with a card)."""

    def __init__(self, decision: nightfit.Decision) -> None:
        self.decision = decision
        self.stage = decision.stage
        super().__init__(OUT_OF_TIME, f"{decision.answer} before {decision.stage}: {decision.reason}")


# ---------------------------------------------------------------------------------------------- the start
@dataclass(frozen=True)
class TaskStart:
    """When the night's clock started and how that was found (`basis`), with what was said on the way."""

    at: datetime
    basis: str
    said: tuple[str, ...] = ()


def _parse(raw: str) -> datetime | None:
    try:
        when = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _plausible(when: datetime, now: datetime, limit_s: int) -> bool:
    """A start that can be this night's: not in the future, and not so old that Windows has stopped it."""
    return now - timedelta(seconds=limit_s) <= when <= now + timedelta(seconds=FUTURE_SLACK_S)


def _default_process() -> Any:
    import psutil

    return psutil.Process()


def task_start(
    environ: Mapping[str, str], now: datetime, limit_s: int, *, process: Any = None
) -> TaskStart:
    """S0: the night's start. `NS_NIGHT_STARTED` (the launch script's own start), else the creation time of
    the oldest ancestor `cmd.exe` whose command line holds `-Nightly` (the task's action: an older launch
    script, or none at all), else this process's own start: a run by hand, which has no deadline. A value
    that does not parse or cannot be this night's start falls through, and each fallback is said."""
    said: list[str] = []
    raw = environ.get(STARTED_ENV)
    if raw is not None and raw.strip():
        when = _parse(raw)
        if when is not None and _plausible(when, now, limit_s):
            return TaskStart(when.astimezone(UTC), START_STUB, tuple(said))
        said.append(f"{STARTED_ENV}={raw.strip()!r} is not this night's start; the task's cmd.exe is asked")
    else:
        said.append(f"{STARTED_ENV} is not set (an older launch script, or a run by hand)")
    try:
        proc = process if process is not None else _default_process()
    except Exception as e:  # noqa: BLE001 - no process table: a run by hand
        said.append(f"the process table cannot be read ({e}); no deadline")
        return TaskStart(now, START_PROCESS, tuple(said))
    try:
        parents = list(proc.parents())
    except Exception as e:  # noqa: BLE001
        parents = []
        said.append(f"the parent processes cannot be read ({e})")
    for parent in reversed(parents):  # the oldest first
        try:
            name = str(parent.name()).lower()
            line = " ".join(str(a) for a in parent.cmdline()).lower()
            created = datetime.fromtimestamp(float(parent.create_time()), UTC)
        except Exception:  # noqa: BLE001 - gone or not ours to read: not the task's
            continue
        if name == "cmd.exe" and "-nightly" in line:
            if _plausible(created, now, limit_s):
                return TaskStart(created, START_ANCESTOR, tuple(said))
            said.append(f"the task's cmd.exe started at {created:%Y-%m-%d %H:%M}, not this night")
    said.append("no nightly task above this process: a run by hand, with no deadline")
    try:
        own = datetime.fromtimestamp(float(proc.create_time()), UTC)
    except Exception:  # noqa: BLE001
        own = now
    return TaskStart(min(own, now), START_PROCESS, tuple(said))


def night_limit(paths: Paths, environ: Mapping[str, str]) -> tuple[int, str | None]:
    """How long Windows lets the nightly task run: `schedule_cfg.TASK_TIME_LIMIT_S` (the number the task is
    registered with), or `NS_TEST_NIGHT_LIMIT_S` on a dev or engine-test install (60 to 21600). The second
    value is what to say about a test limit that was not taken."""
    from ns_common import devmode
    from ns_supervisor import schedule_cfg  # C2's constant, read lazily (C7 reaches C2 lazily only)

    limit = int(schedule_cfg.TASK_TIME_LIMIT_S)
    raw = environ.get(TEST_LIMIT_ENV)
    if raw is None or not raw.strip():
        return limit, None
    if not devmode.test_install(paths):
        return limit, f"{TEST_LIMIT_ENV} is ignored: this is not a dev or engine-test install"
    try:
        value = int(raw.strip())
    except ValueError:
        value = 0
    if not TEST_LIMIT_MIN_S <= value <= TEST_LIMIT_MAX_S:
        return limit, f"{TEST_LIMIT_ENV}={raw.strip()!r} is not {TEST_LIMIT_MIN_S} to {TEST_LIMIT_MAX_S} s"
    return value, None


def margins(limit_s: int) -> tuple[int, int]:
    """(closing margin, hard mark) before the limit: `CLOSE_MARGIN_S` and `HARD_MARK_S`, scaled down for a
    test limit too short to hold them (a sixth of the limit at most)."""
    margin = min(CLOSE_MARGIN_S, limit_s // 6)
    return margin, min(HARD_MARK_S, max(1, margin // 2))


# ---------------------------------------------------------------------------------------------- estimates
def stage_estimate(
    lines: Sequence[Mapping[str, Any]], plan: str, stage: str, room_s: float | None = None
) -> tuple[float, str]:
    """(seconds, basis) one pass of `stage` needs in a job of `plan` on this install (`nightfit.estimate`:
    the worst of its last `HISTORY_N` passes inside a fresh night's `room_s`, else `STAGE_FLOOR_S`)."""
    e = _estimate(lines, plan, stage, room_s)
    return e.seconds, e.basis


def _estimate(
    lines: Sequence[Mapping[str, Any]], plan: str, stage: str, room_s: float | None
) -> nightfit.Estimate:
    passes = ledger.stage_passes(lines, plan, stage)
    return nightfit.estimate(stage, [s for s, _m in passes], room_s, [m for _s, m in passes])


def simulated_card_mark(paths: Paths) -> str | None:
    """H-10's mark (`gpu.simulated_mark`, the one source of the words) when this install runs on the simulated
    graphics card (`fake_gpu`), else None: every ledger line and every time shown to the owner that was
    measured here carries it, so the kit never reads a laptop's times on a `fake_gpu` install as the GPU PC's.
    `smi_source` reads runtime.json tolerantly and asks nothing (nvidia-smi is not run)."""
    from ns_supervisor import gpu  # the studio reaches the supervisor lazily

    return gpu.simulated_mark(gpu.smi_source(paths))


def marked(mark: str | None, text: str) -> str:
    """`text` with the simulated card's mark in front (`gpu.labelled`'s form): `simulated (fake_gpu): ...`."""
    return f"{mark}: {text}" if mark is not None else text


def plan_of(fmt: str, job_dir: Any) -> str:
    """The plan a job runs (`orchestrator.plan_format`: its format, or its script variant)."""
    from ns_studio.orchestrator import plan_format

    return plan_format(fmt, job_dir)


def _later_stages(ctx: JobContext, stage: str) -> list[str]:
    """The job's stages after `stage` that are not done (a stage of them that cannot fit a night gives the job
    up now, not after more work); none when the plan cannot be read."""
    from ns_studio.orchestrator import stage_names
    from ns_studio.stages import StageState

    try:
        names = stage_names(str(ctx.fmt), ctx.job_dir)
        done = {n for n, e in StageState.load(ctx.job_dir).entries.items() if e.get("status") == "done"}
    except Exception:  # noqa: BLE001 - the stage alone is still decided
        return []
    return [n for n in names[names.index(stage) + 1:] if n not in done] if stage in names else []


def _round(value: float | None) -> int | None:
    return None if value is None else round(value)


# ---------------------------------------------------------------------------------------------- the night
@dataclass
class Night:
    """One nightly run's clock: its deadline, its ledger lines, its beat and its hard mark.

    `JobContext.deadline` is this object while the night runs a job (None on every other route): the
    orchestrator asks it before every stage (`before_stage`) and before every wait for the graphics card
    (`wait_cap`)."""

    paths: Paths
    run_id: str
    clock: Clock
    start: TaskStart
    limit_s: int | None
    log: NsLogger | None = None
    lines: list[dict[str, Any]] = field(default_factory=list)
    current: tuple[str, str, str, str, float] | None = None  # (job, channel, stage, plan, began) under way
    sums: dict[str, float] = field(default_factory=dict)
    cut_written: bool = False
    #: what a night spends here before its first stage (`nightfit.overhead` of the ledger's `first_stage`
    #: lines, read once when the night opens)
    overhead_s: float | None = None
    #: the simulated card's mark (`simulated_card_mark`, read once when the night opens), or None
    card: str | None = None
    #: the PC's sleeps found during this night (4b.4), oldest first
    sleeps: list[Sleep] = field(default_factory=list)
    _beat_warned: bool = False
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    _prev: tuple[datetime, float] | None = None
    _tick_lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.card is None:
            self.card = simulated_card_mark(self.paths)
        if self.overhead_s is None and self.limit_s is not None:
            self.overhead_s = nightfit.overhead(
                ledger.overhead_samples(self.lines), nightfit.default_overhead(self.limit_s)
            )

    @property
    def deadline(self) -> datetime | None:
        if self.limit_s is None:
            return None
        return self.start.at + timedelta(seconds=self.limit_s - margins(self.limit_s)[0])

    @property
    def hard_mark(self) -> datetime | None:
        if self.limit_s is None:
            return None
        return self.start.at + timedelta(seconds=self.limit_s - margins(self.limit_s)[1])

    def left_s(self) -> float | None:
        """Seconds until the deadline (negative past it); None without one."""
        deadline = self.deadline
        return None if deadline is None else (deadline - self.clock.now()).total_seconds()

    @property
    def capacity_s(self) -> float | None:
        """From the task's start to the deadline (5 h 50 min): the limit minus the closing margin."""
        if self.limit_s is None:
            return None
        return float(self.limit_s - margins(self.limit_s)[0])

    @property
    def room_s(self) -> float | None:
        """What a fresh night really has for work here: the capacity minus the measured overhead."""
        capacity = self.capacity_s
        return None if capacity is None else nightfit.room(capacity, self.overhead_s or 0.0)

    @property
    def begun_tonight(self) -> bool:
        """This night has begun a stage (of any job): it is no longer a fresh night."""
        return self.sums.get("stages_begun", 0.0) > 0

    # ------------------------------------------------------------------ the one decision
    def job_facts(self, job_id: str | None) -> nightfit.Job:
        """What the decision knows of a job: its record (cuts, misses, last progress, age) and its stages
        finished in this night. A job not made yet (None) has the defaults."""
        from ns_studio import ownership

        max_age = float(ownership.RESUME_WITHIN_DAYS)
        if job_id is None:
            return nightfit.Job(max_age_days=max_age)
        rec = ownership.read_record(self.paths, job_id) or {}
        finished = sum(
            1 for x in self.lines if x.get("kind") == "stage" and x.get("outcome") == "done"
            and x.get("run_id") == self.run_id and x.get("job_id") == job_id
        )
        anchor = ownership.age_anchor(self.paths, {**rec, "id": job_id}, lines=self.lines)
        age = 0.0 if anchor is None else (self.clock.now() - anchor).total_seconds() / 86400
        misses, last = int(rec.get("misses") or 0), bool(rec.get("last_progressed", True))
        done_at, kept_at = ledger.last_done_at(self.lines, job_id), rec.get("kept_at")
        if done_at and kept_at and done_at > str(kept_at):
            # a stage of it finished after the night's last decision about it (a night killed after that
            # stage, or a Retry that stopped later): that night progressed, though no decision recorded it
            misses, last = 0, True
        return nightfit.Job(
            cuts=int(rec.get("cuts") or 0), misses=misses, last_progressed=last, finished_tonight=finished,
            age_days=age, max_age_days=max_age,
        )

    def ask(
        self,
        where: str,
        plan: str,
        names: Sequence[str],
        job_id: str | None = None,
        *,
        service: str | None = None,
        wait_cap_s: int = 7200,
    ) -> nightfit.Decision:
        """Gather the inputs and ask `nightfit.decide` (never raises a decision itself). A decision that is
        not a plain BEGIN is written to the ledger (`decision`) with the numbers it was taken on."""
        room = self.room_s
        stages = tuple(_estimate(self.lines, plan, n, room) for n in names)
        job_s, basis = nightfit.job_seconds(plan, stages, whole=where == nightfit.NEW)
        load = ledger.load_samples(self.lines, service) if service else []
        q = nightfit.Question(
            where, self.left_s(), self.begun_tonight, room, stages, job_s, basis, self.job_facts(job_id),
            load_s=nightfit.overhead(load, 0.0), wait_cap_s=wait_cap_s,
        )
        d = nightfit.decide(q)
        if d.answer != nightfit.BEGIN or d.reason in (nightfit.FIRST_WORK, nightfit.FIRST_WORK_LONG):
            self.write("decision", job_id=job_id, plan=plan, where=where, answer=d.answer, reason=d.reason,
                       stage=d.stage, need_s=_round(d.need_s), left_s=_round(d.left_s), room_s=_round(room),
                       basis=d.basis, passes=d.passes, over=d.over, misses=d.misses)
        miss = nightfit.miss_sample(d)
        if miss is not None and job_id is not None:  # evidence for the plan's stage, not only for this job
            self.write("missed", job_id=job_id, sample_id=f"{job_id}/miss{d.misses}", plan=plan,
                       stage=d.stage, seconds=round(miss, 3))
        return d

    # ------------------------------------------------------------------ the ledger
    def write(self, kind: str, **fields: Any) -> None:
        """One ledger line of this night (never raises)."""
        now = self.clock.now()
        line = {"kind": kind, "at": now.isoformat(timespec="seconds"), **fields}
        line.setdefault("run_id", self.run_id)
        if self.card is not None:  # measured on the simulated card: every line says so (H-10)
            line.setdefault("simulated_card", self.card)
        self.lines.append(line)
        if line.get("run_id") == self.run_id:
            for key in ("seconds", "wait_s", "load_s"):
                value = line.get(key)
                if kind in ("stage", "acquire") and isinstance(value, int | float):
                    name = f"{kind}_s" if key == "seconds" else key
                    self.sums[name] = self.sums.get(name, 0.0) + float(value)
            if kind == "stage" and line.get("outcome") == "done":
                self.sums["stages_done"] = self.sums.get("stages_done", 0.0) + 1
        if kind == "stage":
            self.current = None
        try:
            ledger.write(self.paths, line, when=now, log=self.log)
        except Exception as e:  # noqa: BLE001 - the ledger never stops a night
            if self.log is not None:
                self.log.warn("ledger_write_failed", str(e))

    def open(self, said: Sequence[str] = ()) -> None:
        """S1: the `night_start` line, then the beat."""
        deadline = self.deadline
        self.write(
            "night_start",
            task_start=self.start.at.isoformat(timespec="seconds"),
            start_basis=self.start.basis,
            limit="none" if self.limit_s is None else self.limit_s,
            deadline=None if deadline is None else deadline.isoformat(timespec="seconds"),
            overhead_s=_round(self.overhead_s), room_s=_round(self.room_s),
            simulated_card=self.card,
            said=[*self.start.said, *said],
        )
        if self.log is not None:
            for sentence in (*self.start.said, *said):
                self.log.info("night_start_basis", sentence)
        self.tick()
        self._thread = threading.Thread(target=self._beat_loop, name="ns-night-beat", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------ the checks
    def before_stage(self, ctx: JobContext, stage: str) -> None:
        """S6: called immediately before a stage really runs; asks the one decision. Not BEGIN: raises
        `OutOfTime` with it. BEGIN: the stage is under way (for the hard mark); the night's first begun stage
        writes `first_stage` with the seconds since the task's start (what `overhead_s` is measured from)."""
        plan = plan_of(str(ctx.fmt), ctx.job_dir)
        d = self.ask(nightfit.STAGE, plan, [stage, *_later_stages(ctx, stage)], ctx.job_id)
        if d.answer != nightfit.BEGIN:
            raise OutOfTime(d)
        if self.limit_s is not None and not self.begun_tonight:
            self.write("first_stage", job_id=ctx.job_id, plan=plan, stage=stage,
                       overhead_s=round((self.clock.now() - self.start.at).total_seconds(), 3))
        self.sums["stages_begun"] = self.sums.get("stages_begun", 0.0) + 1
        self.current = (ctx.job_id, ctx.channel.slug, stage, plan, self.clock.monotonic())

    def before_wait(self, ctx: JobContext, rest: Sequence[str], service: str, cap_s: int) -> int | None:
        """S7: before the wait for the graphics card ahead of `rest[0]` (`rest`: the job's stages still to
        run); asks the one decision with the model load. Not BEGIN: raises `OutOfTime`. BEGIN: how long the
        wait may take (None without a deadline: the acquirer's own cap)."""
        d = self.ask(nightfit.WAIT, plan_of(str(ctx.fmt), ctx.job_dir), rest, ctx.job_id, service=service,
                     wait_cap_s=cap_s)
        if d.answer != nightfit.BEGIN:
            raise OutOfTime(d)
        return d.wait_s

    def slept_through(self, job_id: str) -> Sleep | None:
        """4b.4, asked as a job's error comes out: the sleep that explains it, or None. A beat is taken first,
        so a sleep the beat thread has not seen yet (the error came the moment the PC woke) is found now. It
        explains the error only when the error came within `WAKE_GRACE_S` of the wake (the first beat after
        it): a stage that was under way across the sleep and fails later fails by the ordinary rules, since
        nothing shows the sleep caused it. (`job_id` is kept for the record of which job asked.)"""
        try:
            self.tick()
        except Exception as e:  # noqa: BLE001 - the sleeps already found still count
            if self.log is not None:
                self.log.warn("night_beat_failed", str(e))
        now = self.clock.now()
        for gap in reversed(self.sleeps):
            if 0 <= (now - gap.to).total_seconds() <= WAKE_GRACE_S:
                return gap
        return None

    # ------------------------------------------------------------------ the beat and the hard mark
    def beat_file(self) -> Any:
        return self.paths.home / "run" / BEAT_FILE

    def tick(self) -> None:
        """S2 and S8: write the beat; past the hard mark, say once which stage the night is still in.
        4b.4: a sleep since the last beat is written once (`slept`)."""
        with self._tick_lock:  # the beat thread and `slept_through` (the night's own thread)
            self._tick()

    def _tick(self) -> None:
        now = self.clock.now()
        asleep = _asleep(self.clock)
        prev, self._prev = self._prev, (now, asleep) if asleep is not None else None
        if prev is not None and asleep is not None and asleep - prev[1] > SLEEP_GAP_S:
            gap = Sleep(prev[0], now, round(asleep - prev[1], 3))
            self.sleeps.append(gap)
            job, _channel, stage, _plan, _began = self.current or (None, None, None, None, None)
            span = {"from": gap.frm.isoformat(timespec="seconds"), "to": gap.to.isoformat(timespec="seconds")}
            self.write("slept", job_id=job, stage=stage, slept_s=gap.slept_s, **span)
            if self.log is not None:
                self.log.warn("night_slept", "the PC slept during the night's run", slept_s=gap.slept_s,
                              stage=stage)
        beat = {"run_id": self.run_id, "at": now.isoformat(timespec="seconds"), "pid": os.getpid(),
                "asleep_s": asleep}
        try:
            write_text_atomic(self.beat_file(), json.dumps(beat) + "\n")
        except OSError as e:
            if not self._beat_warned and self.log is not None:
                self._beat_warned = True
                self.log.warn("night_beat_failed", str(e), file=str(self.beat_file()))
        mark = self.hard_mark
        if mark is not None and not self.cut_written and now >= mark:
            self.cut_written = True
            job, channel, stage, plan, began = self.current or (None, None, None, None, None)
            seconds = None if began is None else round(self.clock.monotonic() - began, 3)
            # `seconds` so far is a lower bound of the stage (`ledger.stage_samples` counts it as one)
            self.write("cut_expected", job_id=job, channel=channel, stage=stage, plan=plan, seconds=seconds)
            if self.log is not None:
                self.log.warn("night_cut_expected", "the night still works at the hard mark", stage=stage)

    def _beat_loop(self) -> None:
        while not self._stop.wait(BEAT_S):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 - the beat never stops a night
                if self.log is not None:
                    self.log.warn("night_beat_failed", str(e))

    def timing(self) -> dict[str, Any]:
        """The report's `timing` (and the `night_end` line's numbers)."""
        now = self.clock.now()
        left = self.left_s()
        deadline = self.deadline
        return {
            "task_start": self.start.at.isoformat(timespec="seconds"),
            "start_basis": self.start.basis,
            "limit_s": self.limit_s,
            "deadline": None if deadline is None else deadline.isoformat(timespec="seconds"),
            "used_s": round((now - self.start.at).total_seconds()),
            "left_s": None if left is None else round(left),
            "stages_s": round(self.sums.get("stage_s", 0.0)),
            "gpu_s": round(self.sums.get("acquire_s", 0.0)),
            "wait_s": round(self.sums["wait_s"]) if "wait_s" in self.sums else None,
            "load_s": round(self.sums["load_s"]) if "load_s" in self.sums else None,
            "simulated_card": self.card,
        }

    def close(self, **fields: Any) -> dict[str, Any]:
        """S9: stop the beat, write `night_end`, remove the beat file; the `timing` dict."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(5.0)
        timing = self.timing()
        self.write("night_end", **timing, **fields)
        with contextlib.suppress(OSError):
            self.beat_file().unlink(missing_ok=True)
        return timing


def open_night(
    paths: Paths,
    run_id: str,
    clock: Clock,
    environ: Mapping[str, str],
    *,
    process: Any = None,
    log: NsLogger | None = None,
) -> Night:
    """S0 and S1: find the task's start and the limit, read the ledger for the estimates, write `night_start`
    and start the beat. Only a night the task started (`stub`, `ancestor`) has a deadline."""
    limit, about_limit = night_limit(paths, environ)
    start = task_start(environ, clock.now(), limit, process=process)
    try:
        lines = ledger.read(paths)
    except Exception as e:  # noqa: BLE001 - no history: the defaults
        lines = []
        if log is not None:
            log.warn("ledger_unreadable", str(e))
    night = Night(paths, run_id, clock, start, limit if start.basis != START_PROCESS else None, log, lines)
    night.open([about_limit] if about_limit else [])
    return night


# ---------------------------------------------------------------------------------------------- every route
def origin_of(ctx: JobContext) -> str:
    """Who made the job (`daily`, `manual`, ...), from its owner record; `manual` when it does not say."""
    from ns_studio import ownership

    rec = ownership.read_record(ctx.paths, ctx.job_id) or {}
    return str(rec.get("origin") or "manual")


def job_line(ctx: JobContext, kind: str, **fields: Any) -> None:
    """A `stage`, `acquire` or `job_end` line of `ctx`'s job, from any route: through the night when one
    runs the job, else straight into the ledger with the context's clock. Never raises."""
    try:
        line = {"run_id": ctx.run_id, "job_id": ctx.job_id, "channel": ctx.channel.slug,
                "origin": origin_of(ctx), **fields}
        night = ctx.deadline
        if isinstance(night, Night):
            night.write(kind, **line)
            return
        now = ctx.clock.now()
        mark = simulated_card_mark(ctx.paths)
        if mark is not None:  # measured on the simulated card (H-10)
            line["simulated_card"] = mark
        ledger.write(ctx.paths, {"kind": kind, "at": now.isoformat(timespec="seconds"), **line}, when=now,
                     log=ctx.log)
    except Exception as e:  # noqa: BLE001 - the ledger never stops a job
        ctx.log.warn("ledger_write_failed", str(e), kind=kind)

