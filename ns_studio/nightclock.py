"""The night's clock: when the task started, the deadline it must end by, and the estimates it plans with.

Card night-fits (night-fits-a). Windows stops the nightly task `schedule_cfg.TASK_TIME_LIMIT_S` (6 h) after
it started it, whatever it is doing. A nightly run started by the task therefore never begins a job or a stage
whose estimate ends after `task start + limit - CLOSE_MARGIN_S` (the deadline): what does not fit is left for
the next night, at a stage boundary, and the owner reads in the morning what was left and why
(`notifications.time_note`). A run started by hand has no deadline and is never refused for time.

Time is an input: everything here reads the injected `Clock` (`now()` wall time for the deadline, which a
sleep shortens; `monotonic()` for durations); nothing reads the PC's clock in the middle of a decision.

Estimates come from this install's own ledger (`ledger.py`): a job is `ESTIMATE_FACTOR` x the median of the
last `HISTORY_N` finished jobs of its plan, a stage the same over that stage's last runs. Without a
measurement (the first nights on a new PC) a job is `DEFAULT_JOB_S` of its plan and a stage `STAGE_FLOOR_S`,
deliberately large, and the report says `basis: default`.
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
from statistics import median
from typing import TYPE_CHECKING, Any, Protocol

from ns_common.atomic import write_text_atomic
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_studio import ledger
from ns_studio.errors import OUT_OF_TIME, StudioError

if TYPE_CHECKING:
    from ns_studio.context import JobContext

#: Set by the launch script (`installer/launch.ps1 -Nightly`) to its own start, in UTC: the task's start.
STARTED_ENV = "NS_NIGHT_STARTED"
#: A shorter limit for a proof, read only on a dev or engine-test install (`devmode.test_install`).
TEST_LIMIT_ENV = "NS_TEST_NIGHT_LIMIT_S"
TEST_LIMIT_MIN_S, TEST_LIMIT_MAX_S = 60, 21600
#: Kept free at the end of the night: the report, the publish times, the backup and the morning note.
CLOSE_MARGIN_S = 10 * 60
#: The hard mark: still running this long before Windows' limit, the night says so (`cut_expected`).
HARD_MARK_S = 120
ESTIMATE_FACTOR = 1.2
HISTORY_N = 5
#: A job's estimate before this install has finished one of its plan (`basis: default`), by plan.
DEFAULT_JOB_S: dict[str, int] = {"master_first": 3 * 3600, "master_from_script": 9000, "shorts_only": 3600}
DEFAULT_OTHER_JOB_S = 3600
#: A stage's estimate before this install has run it.
STAGE_FLOOR_S = 10 * 60
#: How often the night writes `run/night-beat.json` and looks at the hard mark.
BEAT_S = 30.0
BEAT_FILE = "night-beat.json"
#: A task start this far in the future is not this night's (a clock set back, a variable from elsewhere).
FUTURE_SLACK_S = 300

START_STUB, START_ANCESTOR, START_PROCESS = "stub", "ancestor", "process"


class Clock(Protocol):
    """Wall time (aware, UTC) and a monotonic counter for durations."""

    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    """The PC's clocks."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


def words(seconds: float) -> str:
    """`5 h 40 min`, `52 min`, `40 s`: a duration as the owner reads it."""
    s = max(0, round(seconds))
    if s < 60:
        return f"{s} s"
    hours, minutes = divmod(s // 60, 60)  # whole minutes, never more than there were
    if hours and minutes:
        return f"{hours} h {minutes} min"
    return f"{hours} h" if hours else f"{minutes} min"


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
    """A stage would end after the night's deadline: the job stops before it (NS-W211), its finished stages
    kept, and the next night continues it from this stage."""

    def __init__(self, stage: str, need_s: float, left_s: float, basis: str) -> None:
        self.stage, self.need_s, self.left_s, self.basis = stage, need_s, left_s, basis
        super().__init__(
            OUT_OF_TIME, f"{stage} needs about {words(need_s)}, {words(left_s)} were left before the deadline"
        )


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
def job_estimate(lines: Sequence[Mapping[str, Any]], plan: str) -> tuple[float, str]:
    """(seconds, basis) one job of `plan` needs on this install."""
    history = ledger.job_totals(lines, plan)[-HISTORY_N:]
    if history:
        return ESTIMATE_FACTOR * median(history), "history"
    return float(DEFAULT_JOB_S.get(plan, DEFAULT_OTHER_JOB_S)), "default"


def stage_estimate(lines: Sequence[Mapping[str, Any]], stage: str) -> tuple[float, str]:
    """(seconds, basis) one run of `stage` needs on this install."""
    history = ledger.stage_seconds(lines, stage)[-HISTORY_N:]
    if history:
        return ESTIMATE_FACTOR * median(history), "history"
    return float(STAGE_FLOOR_S), "default"


def stages_estimate(lines: Sequence[Mapping[str, Any]], stages: Sequence[str]) -> tuple[float, str]:
    """(seconds, basis) of a run of `stages` (what a resumed job has left): the sum of their estimates;
    `default` when any of them has no measurement."""
    total, basis = 0.0, "history"
    for name in stages:
        need, how = stage_estimate(lines, name)
        total += need
        if how == "default":
            basis = "default"
    return total, basis


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
    current: tuple[str, str, str] | None = None  # (job, channel, stage) of the stage under way
    sums: dict[str, float] = field(default_factory=dict)
    cut_written: bool = False
    _beat_warned: bool = False
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

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

    def fits(self, need_s: float) -> bool:
        """True when work of `need_s` seconds ends by the deadline (both ends: exactly at it fits)."""
        left = self.left_s()
        return left is None or need_s <= left

    # ------------------------------------------------------------------ the ledger
    def write(self, kind: str, **fields: Any) -> None:
        """One ledger line of this night (never raises)."""
        now = self.clock.now()
        line = {"kind": kind, "at": now.isoformat(timespec="seconds"), **fields}
        line.setdefault("run_id", self.run_id)
        self.lines.append(line)
        if line.get("run_id") == self.run_id:
            for key in ("seconds", "wait_s", "load_s"):
                value = line.get(key)
                if kind in ("stage", "acquire") and isinstance(value, int | float):
                    name = f"{kind}_s" if key == "seconds" else key
                    self.sums[name] = self.sums.get(name, 0.0) + float(value)
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
        """S6: called immediately before a stage really runs. Raises `OutOfTime` when its estimate ends
        after the deadline; else the stage is the one under way (for the hard mark)."""
        self.current = (ctx.job_id, ctx.channel.slug, stage)
        left = self.left_s()
        if left is None:
            return
        need, basis = stage_estimate(self.lines, stage)
        if need > left:
            self.current = None
            raise OutOfTime(stage, need, left, basis)

    def wait_cap(self, rest: Sequence[str], cap_s: int) -> int | None:
        """S7: how long the wait for the graphics card may take: `cap_s` (the acquirer's own cap), or the time
        left after the estimate of `rest` (the stages the job still has to run) when that is less. At or under
        0 nothing can be waited for. None without a deadline."""
        left = self.left_s()
        if left is None:
            return None
        need, _basis = stages_estimate(self.lines, rest)
        return min(int(cap_s), int(left - need))

    # ------------------------------------------------------------------ the beat and the hard mark
    def beat_file(self) -> Any:
        return self.paths.home / "run" / BEAT_FILE

    def tick(self) -> None:
        """S2 and S8: write the beat; past the hard mark, say once which stage the night is still in."""
        now = self.clock.now()
        beat = {"run_id": self.run_id, "at": now.isoformat(timespec="seconds"), "pid": os.getpid()}
        try:
            write_text_atomic(self.beat_file(), json.dumps(beat) + "\n")
        except OSError as e:
            if not self._beat_warned and self.log is not None:
                self._beat_warned = True
                self.log.warn("night_beat_failed", str(e), file=str(self.beat_file()))
        mark = self.hard_mark
        if mark is not None and not self.cut_written and now >= mark:
            self.cut_written = True
            job, channel, stage = self.current or (None, None, None)
            self.write("cut_expected", job_id=job, channel=channel, stage=stage)
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
        ledger.write(ctx.paths, {"kind": kind, "at": now.isoformat(timespec="seconds"), **line}, when=now,
                     log=ctx.log)
    except Exception as e:  # noqa: BLE001 - the ledger never stops a job
        ctx.log.warn("ledger_write_failed", str(e), kind=kind)

