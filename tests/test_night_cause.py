# ruff: noqa: E501
"""Card night-fits-b: a night that died is told by what stopped it, a sleep is found as it happens, and a stage
the sleep stopped is continued the next night instead of failed.

Before: every dead night read "The night's run was interrupted (power cut, crash or restart)", whatever stopped
it (Windows at the task's 6-hour limit, a restart for updates, a power cut); and a stage that failed after the
PC woke from a sleep was `failed`, which the next night does not continue.

Each dead night here is written by the product's own writer (`nightclock.Night`: `night_start`, the beat,
`cut_expected` at the hard mark) on a tmp home, with the run and its job left `running` by a process that is
gone, and closed by the one recovery (`ownership.recover`, which the dashboard, `studio` and the next night
call). Windows' System log is the one fake `FakeEventLog`: the events of a capture of this laptop's own log
(`tests/fixtures/winevents/system-1074-41-6008-6006.xml`, read by the real parser) moved to the test's
times, filtered by time as wevtutil's query is. What the fake cannot show: the order and the delay with which
a real shutdown writes 1074 and 6006, a 6008 whose binary has a different layout, and a log the owner
cleared or a policy keeps from this user (all of them read as "could not be read" or as no event).
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ns_common.db import Db
from ns_common.paths import Paths
from ns_studio import jobs, ledger, nightclock, ownership
from ns_studio.channels import crud
from ns_studio.errors import StudioError
from ns_studio.stages import StageState
from tests.helpers import make_channel
from tests.test_night_ledger import FakeClock, World

try:  # reached through a guarded import so the code before this card fails at an assertion
    from ns_common import winevents
except ImportError:  # pragma: no cover - the base
    winevents = None  # type: ignore[assignment]

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "winevents" / "system-1074-41-6008-6006.xml"
CH = "after-dark"
LIMIT = 6 * 3600
OLD = "The night's run was interrupted (power cut, crash or restart)"
TAIL = "It continues at the next nightly run, or press Retry in the Queue. " + ownership.WHILE_NIGHTLY_ON


def hhmm(when: datetime) -> str:
    return when.astimezone().strftime("%H:%M")


# ------------------------------------------------------------------ the clock, the event log
class SleepyClock(FakeClock):
    """`FakeClock` with the PC's sleep counter (`SystemClock.asleep`): `sleep(s)` moves wall time, the
    monotonic counter (GetTickCount64 counts sleep) and the sleep counter on together."""

    def __init__(self, start: datetime) -> None:
        super().__init__(start)
        self.slept = 0.0

    def asleep(self) -> float:
        with self._mu:
            return self.slept

    def sleep(self, seconds: float) -> None:
        with self._mu:
            self._t += seconds
            self.slept += seconds


def captured() -> dict[int, Any]:
    """One event of each id, as the real parser reads the capture (the last of each id)."""
    assert winevents is not None, "no reader of Windows' event log (ns_common.winevents)"
    return {e.id: e for e in winevents.parse(FIXTURE.read_text(encoding="utf-8"))}


class FakeEventLog:
    """`winevents.SystemLog` with the captured events at the test's times: `read(since, until)` answers what
    wevtutil's query answers (`since <= TimeCreated <= until`, `/rd:false`: oldest first, `/c:20`), with
    `capped` as `winevents.parse` sets it."""

    def __init__(self, events: list[Any]) -> None:
        self.events: list[Any] = events
        self.asked: list[tuple[datetime, datetime]] = []

    def read(self, since: datetime, until: datetime) -> list[Any]:
        self.asked.append((since, until))
        inside = sorted((e for e in self.events if since <= e.at <= until), key=lambda e: e.at)
        cap = winevents.MAX_EVENTS if winevents is not None else 20
        out = winevents.Events(inside[:cap]) if winevents is not None else inside[:cap]
        if winevents is not None:
            out.capped = len(inside) >= cap  # type: ignore[attr-defined]
        return out


def at(event_id: int, when: datetime, **change: Any) -> Any:
    if winevents is None:  # the base: no reader, so no event shape (the test fails at its note)
        return SimpleNamespace(id=event_id, at=when, **change)
    return dataclasses.replace(captured()[event_id], at=when, **change)


# ------------------------------------------------------------------ a dead night, written by the product
def _setup() -> tuple[Paths, Db]:
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    created = (datetime.now(UTC) - timedelta(weeks=11)).isoformat(timespec="seconds")
    if CH not in crud.list_channels(paths):
        crud.add_channel(paths, db, make_channel(format="master_first", created_at=created))
    return paths, db


class DeadNight:
    """A nightly run and its half-made job whose process is gone, with what the night wrote before it died."""

    def __init__(self, paths: Paths, db: Db, clock: FakeClock, *, n: int = 1) -> None:
        self.paths, self.db, self.clock = paths, db, clock
        self.task = clock.now()
        self.run_id = f"daily-test-{n:04d}"
        db.execute("insert into runs(run_id, started_at, mode) values (?,?,?)",
                   (self.run_id, self.task.isoformat(timespec="seconds"), "daily"))
        ctx = jobs.make_job(jobs.open_env(light=True), crud.get_channel(paths, CH), "master_first",
                            origin="daily", run_id=self.run_id)
        self.job_id = ctx.job_id
        state = StageState()
        state.mark_done("ideas", "h")
        state.save(ctx.job_dir)
        self.stage = ownership.reached_stage(paths, self.job_id, "master_first")
        ownership.write_record(paths, self.run_id, kind="run", state="running", origin="daily", pid=2**22,
                               started_at=self.task.isoformat(timespec="seconds"),
                               jobs=[{"job_id": self.job_id, "channel": CH}])
        ownership.write_record(paths, self.job_id, state="running", pid=2**22)
        start = nightclock.TaskStart(self.task, nightclock.START_STUB)
        self.night = nightclock.Night(paths, self.run_id, clock, start, LIMIT, None, [])
        self.night.open()
        self.night._stop.set()  # the beat is driven by the test (`beat`), never by the thread
        assert self.night._thread is not None
        self.night._thread.join(5)

    def beat(self, after_start_s: float) -> datetime:
        """The night's beat (and its hard mark) at `after_start_s` seconds after the task's start."""
        self.clock.advance(after_start_s - (self.clock.now() - self.task).total_seconds())
        self.night.tick()
        return self.clock.now()

    def recover(self, events: Any, boot: datetime | None) -> ownership.Recovery:
        return ownership.recover(self.paths, self.db, now=self.clock.now(),
                                 **{k: v for k, v in {"events": events, "boot": boot}.items()
                                    if k in inspect.signature(ownership.recover).parameters})

    def note(self) -> str:
        from ns_supervisor.notify import read_notes

        return str(read_notes(self.paths)[-1]["message"])

    def card(self) -> dict[str, Any]:
        rows = [r for r in self.db.query("select * from dashboard_jobs where kind='run'")
                if self.job_id in r["params_json"]]
        assert len(rows) == 1, rows
        return {**dict(rows[0]), "result": json.loads(rows[0]["params_json"])["result"]}


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 12, 2, 0, tzinfo=UTC))


# ------------------------------------------------------------------ 16. Windows' limit
@pytest.mark.parametrize("after_limit_s, told", [(-10, True), (120, True), (121, False)])
def test_a_dead_night_with_a_cut_mark_is_told_as_the_time_limit(
    tmp_home: Path, clock: FakeClock, after_limit_s: int, told: bool
) -> None:
    paths, db = _setup()
    dead = DeadNight(paths, db, clock)
    dead.beat(LIMIT - 110)  # the hard mark: still running two minutes before the limit
    assert [x["kind"] for x in ledger.read(paths) if x.get("kind") == "cut_expected"] == ["cut_expected"]
    last = dead.beat(LIMIT + after_limit_s)  # the last beat before the processes ended
    limit_at = dead.task + timedelta(seconds=LIMIT)
    dead.recover(FakeEventLog([]), dead.task - timedelta(days=1))  # nothing in the log, no restart since
    head = f"Windows stopped the night's run at {hhmm(limit_at)}, at the 6-hour limit it allows the nightly task"
    if not told:  # it lived on after the limit (the documented end leaves the studio running): not the limit
        unknown = (f"The night's run was interrupted at about {hhmm(last)}, and what stopped it cannot be told: "
                   "the PC did not restart, so the run's own process was ended: a crash, a person or a program "
                   "ended it")
        assert dead.note() == f"{unknown}, while {CH}'s video was at {dead.stage}. {TAIL}"
        return
    assert dead.note() == f"{head}, while {CH}'s video was at {dead.stage}. {TAIL}"
    card = dead.card()
    assert card["message"] == (
        f"Interrupted at {dead.stage}: {head}. The next nightly run continues it, or press Retry (if its channel "
        f"is paused by then, both wait for the resume). {ownership.WHILE_NIGHTLY_ON}"
    )
    assert card["result"]["error"]["hint"] == ownership.INTERRUPTED_HINT_KNOWN
    assert card["result"]["interrupted"]["cause"] == "time_limit"
    row = db.one("select * from runs where run_id=?", (dead.run_id,))
    assert row is not None
    summary = json.loads(row["summary_json"])
    assert ownership.run_state(paths, dead.run_id, row["started_at"], row["finished_at"], summary) == (
        "interrupted", f"interrupted: {head}")
    assert OLD not in dead.note()


# ------------------------------------------------------------------ 17. a restart for updates, and twice
@pytest.mark.parametrize("program", ["TrustedInstaller.exe", "MoUsoCoreWorker.exe", "usoclient.exe"])
def test_a_restart_for_updates_is_told_with_its_time(tmp_home: Path, clock: FakeClock, program: str) -> None:
    paths, db = _setup()
    dead = DeadNight(paths, db, clock)
    last = dead.beat(2 * 3600)
    asked = last + timedelta(seconds=20)
    log = FakeEventLog([at(1074, asked, process=program), at(6006, asked + timedelta(seconds=40))])
    dead.recover(log, last + timedelta(minutes=3))
    # the window: 2 min before the last beat to 10 min after the PC's next start
    assert log.asked == [(last - timedelta(seconds=120), last + timedelta(minutes=3) + timedelta(seconds=600))]
    head = f"Windows restarted the PC at {hhmm(asked)} to finish installing updates, during the night's run"
    assert dead.note() == f"{head}, while {CH}'s video was at {dead.stage}. {TAIL}"
    assert "NS-W212" not in dead.note()
    assert dead.card()["message"].startswith(f"Interrupted at {dead.stage}: {head}.")


@pytest.mark.parametrize("own_events", [True, False])
def test_a_recovery_days_later_reads_the_nights_own_events_first_and_a_full_read_cannot_tell(
    tmp_home: Path, clock: FakeClock, own_events: bool
) -> None:
    """Review blocker (night-fits-b @ 0196be1): the recovery runs four days later (Automatic runs off, the
    Dashboard not opened) and the PC was restarted 12 more times since the night (25 later events). The read
    is oldest first from just before the last beat, so the night's own events are read; without them the read
    stops at its cap of 20 and the sentence says it cannot tell, never that the log names nothing."""
    paths, db = _setup()
    dead = DeadNight(paths, db, clock)
    last = dead.beat(2 * 3600)
    own = [at(1074, last + timedelta(seconds=20)), at(6006, last + timedelta(seconds=50))] if own_events else []
    later: list[Any] = []
    for n in range(25):  # restarts on the following days, asked by a person: 1074 + 6006, one 1074 extra
        when = last + timedelta(hours=8 + 3 * n)  # all 25 inside the window
        later.append(at(1074, when, process="explorer.exe", action="restart", reason="Other (Planned)")
                     if n % 2 == 0 else at(6006, when))
    log = FakeEventLog([*own, *later])
    boot = last + timedelta(days=4)  # the PC's last start: the read reaches past all 25
    dead.recover(log, boot)
    [(since, until)] = log.asked
    assert since == last - timedelta(seconds=120) and until == boot + timedelta(seconds=600)
    if own_events:
        assert dead.note().startswith(
            f"Windows restarted the PC at {hhmm(last + timedelta(seconds=20))} to finish installing updates")
        return
    assert "names no shutdown" not in dead.note()
    assert dead.note().startswith(
        f"The night's run was interrupted at about {hhmm(last)}, and what stopped it cannot be told: Windows' "
        "event log holds more shutdowns and restarts after that time than Nightshift reads (20), and none of "
        f"those it read explains it; the PC was started again at {hhmm(boot)}, while")


def test_a_restart_for_updates_named_only_by_its_reason_and_one_not_restarted(
    tmp_home: Path, clock: FakeClock
) -> None:
    """A 1074 whose program is not Windows Update's but whose reason names an update is one; a 1074 with no
    start of the PC after it (the restart never happened) explains nothing."""
    paths, db = _setup()
    dead = DeadNight(paths, db, clock)
    last = dead.beat(3600)
    log = FakeEventLog([at(1074, last + timedelta(seconds=5), process="svchost.exe",
                           reason="Operating System: Security fix (Planned)")])
    dead.recover(log, last - timedelta(hours=5))  # the PC did not start again after the last beat
    assert "Windows restarted the PC" not in dead.note()
    assert "the PC did not restart" in dead.note()
    dead2 = DeadNight(paths, db, clock, n=2)
    last2 = dead2.beat(3600)
    log2 = FakeEventLog([at(1074, last2 + timedelta(seconds=5), process="svchost.exe",
                            reason="Operating System: Security fix (Planned)")])
    dead2.recover(log2, last2 + timedelta(minutes=2))
    assert dead2.note().startswith(f"Windows restarted the PC at {hhmm(last2 + timedelta(seconds=5))} to finish")


@pytest.mark.parametrize("days, repeated", [(14, True), (15, False)])
def test_a_second_restart_for_updates_within_two_weeks_adds_ns_w212_once(
    tmp_home: Path, clock: FakeClock, days: int, repeated: bool
) -> None:
    paths, db = _setup()

    def night(n: int) -> DeadNight:
        dead = DeadNight(paths, db, clock, n=n)
        last = dead.beat(2 * 3600)
        dead.recover(FakeEventLog([at(1074, last + timedelta(seconds=10))]), last + timedelta(minutes=3))
        return dead

    first = night(1)
    assert "NS-W212" not in first.note()
    clock.advance(days * 86400 - 2 * 3600 - 10)  # the second restart `days` days after the first
    second = night(2)
    w212 = ("Windows did this twice in two weeks. To keep it away from the night: Settings > Windows Update > "
            f"Advanced options > Active hours and include {hhmm(second.task)}, when the nightly run starts "
            "(NS-W212).")
    assert (w212 in second.note()) is repeated
    clock.advance(86400 - 2 * 3600)
    third = night(3)  # a third within the window: said once, so not again when it was said
    assert ("NS-W212" in third.note()) is (not repeated)


# ------------------------------------------------------------------ 18. a power cut, a shutdown, unknown
def test_a_power_cut_is_told_as_such(tmp_home: Path, clock: FakeClock) -> None:
    paths, db = _setup()
    dead = DeadNight(paths, db, clock)
    last = dead.beat(4000)
    cut = last + timedelta(seconds=15)
    boot = last + timedelta(hours=1)
    log = FakeEventLog([at(41, boot + timedelta(seconds=20)), at(6008, boot + timedelta(seconds=40),
                                                                     stopped_at=cut)])
    dead.recover(log, boot)
    head = f"The PC lost power or stopped unexpectedly at about {hhmm(cut)}, during the night's run"
    assert dead.note() == f"{head}, while {CH}'s video was at {dead.stage}. {TAIL}"
    # a 41 alone (no 6008 time): it happened after the last beat, and the words say no more than that
    dead2 = DeadNight(paths, db, clock, n=2)
    last2 = dead2.beat(4000)
    dead2.recover(FakeEventLog([at(41, last2 + timedelta(hours=2))]), last2 + timedelta(hours=2))
    assert dead2.note().startswith(
        f"The PC lost power or stopped unexpectedly some time after {hhmm(last2)}, during the night's run, while")
    # a shutdown asked by another program
    dead3 = DeadNight(paths, db, clock, n=3)
    last3 = dead3.beat(4000)
    off = last3 + timedelta(seconds=30)
    dead3.recover(FakeEventLog([at(1074, off, process="explorer.exe", action="power off",
                                   reason="Other (Unplanned)")]), off + timedelta(hours=3))
    assert dead3.note().startswith(f"The PC was shut down at {hhmm(off)} (asked by explorer.exe), during the "
                                   "night's run, while")


def test_the_real_event_log_reader_is_refused_under_pytest_and_reads_the_captured_shape() -> None:
    since = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)
    assert winevents is not None, "no reader of Windows' event log (ns_common.winevents)"
    until = since + timedelta(hours=1)
    assert winevents.query_argv(since, until) == [
        "wevtutil", "qe", "System",
        "/q:*[System[(EventID=1074 or EventID=41 or EventID=6008 or EventID=6006) and "
        "TimeCreated[@SystemTime>='2026-11-12T03:00:00.000Z' and @SystemTime<='2026-11-12T04:00:00.000Z']]]",
        "/rd:false", "/c:20", "/f:xml",  # oldest first: later events never push the window's first ones out
    ]
    with pytest.raises(winevents.EventLogUnavailable, match="refused under pytest"):
        winevents.SystemLog().read(since, until)
    got = winevents.parse(FIXTURE.read_text(encoding="utf-8"))
    assert [e.id for e in got] == [1074, 6006, 41, 6008, 1074, 6006]  # oldest first
    restart = got[0]
    assert (restart.process, restart.action, restart.reason) == (
        "TrustedInstaller.exe", "restart", "Operating System: Upgrade (Planned)")
    assert got[3].stopped_at == datetime(2026, 11, 5, 22, 53, 3, tzinfo=UTC)  # 6008's UTC SYSTEMTIME
    assert winevents.parse("") == [] and got.capped is False
    raw = FIXTURE.read_text(encoding="utf-8").strip()
    assert winevents.parse(raw * 4).capped is True  # 24 elements: the read stopped at its cap of 20


def test_an_unknown_end_keeps_todays_words(tmp_home: Path, clock: FakeClock) -> None:
    """A night of an older version left no ledger and no beat: today's words, word for word. A night with a
    beat but nothing that explains it says when it was last alive and exactly what is not known."""
    paths, db = _setup()
    run_id = "daily-rc3-0001"
    db.execute("insert into runs(run_id, started_at, mode) values (?,?,?)",
               (run_id, clock.now().isoformat(timespec="seconds"), "daily"))
    ctx = jobs.make_job(jobs.open_env(light=True), crud.get_channel(paths, CH), "master_first", origin="daily",
                        run_id=run_id)
    state = StageState()
    state.mark_done("ideas", "h")
    state.save(ctx.job_dir)
    ownership.write_record(paths, run_id, kind="run", state="running", origin="daily", pid=2**22,
                           jobs=[{"job_id": ctx.job_id, "channel": CH}])
    ownership.write_record(paths, ctx.job_id, state="running", pid=2**22)
    seams = {"events": FakeEventLog([]), "boot": clock.now() + timedelta(hours=1)}
    ownership.recover(paths, db, **{k: v for k, v in seams.items()
                                    if k in inspect.signature(ownership.recover).parameters})
    from ns_supervisor.notify import read_notes

    stage = ownership.reached_stage(paths, ctx.job_id, "master_first")
    assert read_notes(paths)[-1]["message"] == f"{OLD}. {CH} stopped at {stage}. {TAIL}"
    [row] = [r for r in db.query("select * from dashboard_jobs") if ctx.job_id in r["params_json"]]
    assert json.loads(row["params_json"])["result"]["error"]["hint"] == ownership.INTERRUPTED_HINT

    dead = DeadNight(paths, db, clock, n=2)
    last = dead.beat(1800)
    boot = last + timedelta(minutes=30)
    dead.recover(FakeEventLog([]), boot)
    assert dead.note() == (
        f"The night's run was interrupted at about {hhmm(last)}, and what stopped it cannot be told: Windows' "
        f"event log names no shutdown, restart or power loss at that time; the PC was started again at "
        f"{hhmm(boot)}, while {CH}'s video was at {dead.stage}. {TAIL}"
    )
    dead3 = DeadNight(paths, db, clock, n=3)
    last3 = dead3.beat(1800)
    dead3.recover(None, last3 + timedelta(minutes=30))  # the real reader: refused under pytest, never read
    assert "Windows' event log could not be read" in dead3.note()


# ------------------------------------------------------------------ 19. a sleep, as it happens
@pytest.mark.parametrize("gap_s, found", [(90, False), (91, True)])
def test_a_sleep_is_found_and_written_to_the_ledger(
    tmp_home: Path, gap_s: int, found: bool
) -> None:
    paths, _db = _setup()
    clock = SleepyClock(datetime(2026, 11, 12, 2, 0, tzinfo=UTC))
    start = nightclock.TaskStart(clock.now(), nightclock.START_STUB)
    night = nightclock.Night(paths, "daily-sleep", clock, start, LIMIT, None, [])
    night.open()
    night._stop.set()
    assert night._thread is not None
    night._thread.join(5)
    for _ in range(3):  # beats 30 s apart
        clock.advance(30)
        night.tick()
    clock.advance(7200)  # the wall clock jumps 2 h with no sleep (set forward, or the process stalled)
    night.tick()
    before = clock.now()
    clock.sleep(gap_s)
    night.tick()
    clock.advance(30)
    night.tick()
    slept = [x for x in ledger.read(paths) if x.get("kind") == "slept"]
    beat = json.loads((tmp_home / "run" / "night-beat.json").read_text(encoding="utf-8"))
    assert beat.get("asleep_s") == gap_s, "the beat carries the PC's sleep counter"
    if not found:
        assert slept == [] and getattr(night, "sleeps", None) == []
        return
    assert len(slept) == 1, slept
    assert (slept[0]["from"], slept[0]["to"], slept[0]["slept_s"]) == (
        before.isoformat(timespec="seconds"), (before + timedelta(seconds=gap_s)).isoformat(timespec="seconds"),
        gap_s)
    assert [(s.frm, s.to) for s in night.sleeps] == [(before, before + timedelta(seconds=gap_s))]


# ------------------------------------------------------------------ 20. a stage that fails after a wake
@pytest.fixture
def sleepy(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    world = World(tmp_home, monkeypatch)
    world.clock = SleepyClock(world.clock.start)
    world.acquirer.clock = world.clock
    return world


def _sleeps(world: World) -> None:
    """The PC sleeps 2 h: a beat just before (the beat thread's last), one just after the wake (its first)."""
    night = world.night_obj  # type: ignore[attr-defined]
    night.tick()
    world.clock.sleep(2 * 3600)  # type: ignore[attr-defined]
    night.tick()


def _sleep_in(world: World, stage: str) -> None:
    world.hold[stage] = lambda: _sleeps(world)


def _fail_after(world: World, stage: str, seconds: float, *, sleep_first: bool = False) -> None:
    def hold() -> None:
        if sleep_first:
            _sleeps(world)
        world.clock.advance(seconds)
        raise StudioError("NS-E301", "the language model answered nothing")
    world.hold[stage] = hold


def _night(world: World, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> dict[str, Any]:
    """`World.night`, keeping the night's clock object where the stub stages can beat it."""
    real = nightclock.open_night

    def keep(*a: Any, **k: Any) -> nightclock.Night:
        n = real(*a, **k)
        world.night_obj = n  # type: ignore[attr-defined]
        return n

    monkeypatch.setattr(nightclock, "open_night", keep)
    return world.night(**kw)


@pytest.mark.parametrize("after_wake_s, interrupted", [(119, True), (121, False)])
def test_a_stage_that_fails_right_after_a_wake_leaves_the_job_interrupted_not_failed(
    sleepy: World, monkeypatch: pytest.MonkeyPatch, after_wake_s: int, interrupted: bool
) -> None:
    world = sleepy
    world.seconds = {"nf_a": 300, "nf_b": 0, "nf_c": 60, "nf_d": 60}
    _sleep_in(world, "nf_a")  # the PC sleeps during nf_a, which finishes after the wake
    _fail_after(world, "nf_b", after_wake_s)  # nf_b begins after the wake and fails `after_wake_s` after it
    report = _night(world, monkeypatch)
    [job] = [r for r in ownership.list_records(world.paths) if r.get("kind") == "job"]
    rec = ownership.read_record(world.paths, str(job["id"])) or {}
    slept_lines = [x for x in ledger.read(world.paths) if x.get("kind") == "slept"]
    assert len(slept_lines) == 1 and slept_lines[0]["stage"] == "nf_a"
    if not interrupted:
        assert [f["code"] for f in report["failures"]] == ["NS-E301"] and not report.get("slept")
        assert rec["state"] == "failed"
        return
    assert report["failures"] == [] and report["jobs"] == []
    [slept] = report["slept"]
    assert (slept["job_id"], slept["stage"], slept["error"]) == (job["id"], "nf_b", "NS-E301")
    assert (rec["state"], rec["owner"], rec["interrupted_at_stage"]) == ("interrupted", ownership.SLEPT_OWNER,
                                                                         "nf_b")
    rows = world.db.query("select * from dashboard_jobs where kind='run'")
    assert [r["message"] for r in rows] == [
        "Interrupted at nf_b: the PC went to sleep during the night's run, and the stage stopped as it woke. "
        "The next nightly run continues it, or press Retry (if its channel is paused by then, both wait for the "
        f"resume). {ownership.WHILE_NIGHTLY_ON}"
    ]
    assert json.loads(rows[0]["params_json"])["result"]["error"]["hint"] == ownership.SLEPT_HINT
    frm, to = datetime.fromisoformat(slept["from"]), datetime.fromisoformat(slept["to"])
    note = (f"The PC went to sleep at about {hhmm(frm)} during the night's run and woke at about {hhmm(to)} "
            f"({ownership.SLEEP_WAYS}). {CH}'s video stopped at nf_b as it woke, and the next nightly run "
            "continues it (while Automatic runs are on in Settings), or press Retry in the Queue.")
    assert [n["message"] for n in world.notes()] == [note]

    # the next night continues it from nf_b, first (failed then retried)
    world.hold.clear()
    world.clock.advance(24 * 3600)
    report2 = _night(world, monkeypatch)
    assert [(j["job_id"], j.get("resumed")) for j in report2["jobs"]] == [(job["id"], True)]
    assert world.ran[-3:] == ["nf_b", "nf_c", "nf_d"]


@pytest.mark.parametrize("after_wake_s, interrupted", [(60, True), (3 * 3600, False)])
def test_a_stage_under_way_when_the_pc_slept_is_interrupted_only_when_it_fails_as_it_wakes(
    sleepy: World, monkeypatch: pytest.MonkeyPatch, after_wake_s: int, interrupted: bool
) -> None:
    """Review note (a): a stage that began before the sleep and fails within the window after the wake
    stopped as it woke; one that fails hours later is an ordinary failure (nothing shows the sleep caused it,
    and "stopped as it woke" would be untrue)."""
    world = sleepy
    world.seconds = {"nf_a": 300, "nf_b": 600, "nf_c": 60, "nf_d": 60}
    _fail_after(world, "nf_b", after_wake_s, sleep_first=True)  # began before the sleep
    report = _night(world, monkeypatch)
    if interrupted:
        assert report["failures"] == [] and [s["stage"] for s in report["slept"]] == ["nf_b"]
    else:
        assert [f["code"] for f in report["failures"]] == ["NS-E301"] and not report.get("slept")
    # and a stage the sleep did not stop just finishes: nothing is interrupted, the night says nothing of it
    world.hold.clear()
    world.clock.advance(24 * 3600)
    _sleep_in(world, "nf_c")
    report2 = _night(world, monkeypatch)
    assert report2["failures"] == [] and not report2.get("slept") and len(report2["jobs"]) == 1


# ------------------------------------------------------------------ a night that ran past the limit
@pytest.mark.parametrize("used_s, said", [(LIMIT, False), (LIMIT + 60, True)])
def test_a_night_that_ran_past_the_limit_is_said_in_the_morning_note(used_s: int, said: bool) -> None:
    """Review note (b): the run, not Windows, ended a night that went past the task's limit (Windows' end of
    the task does not stop what runs under cmd.exe); the morning note says so in one sentence."""
    from ns_studio import notifications

    note = notifications.time_note({"timing": {"used_s": used_s, "limit_s": LIMIT}})
    if not said:
        assert note is None
        return
    assert note == ("Last night's run worked 6 h 1 min, 1 min past the 6 hours Windows allows the nightly task: "
                    "the run went on past that limit and ended by itself.")


def test_a_night_whose_stage_ran_past_the_limit_says_so_with_what_it_left(
    world_plain: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = world_plain
    world.seconds = {"nf_a": 3600, "nf_b": 60, "nf_c": 60, "nf_d": 60}  # nf_a's estimate is the 10-min floor
    report = world.night(left_s=900)
    assert report["timing"]["used_s"] == LIMIT + 2100
    [note] = [n["message"] for n in world.notes() if "Last night's run" in n["message"]]
    assert note.startswith("Last night's run worked 6 h 35 min, 35 min past the 6 hours Windows allows the "
                           "nightly task: the run went on past that limit and ended by itself.")
    assert note.endswith("(NS-W211)")  # it also left the job's next stage for the next night


@pytest.fixture
def world_plain(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_home, monkeypatch)


# ------------------------------------------------------------------ the toast and the Queue are not misled
def test_a_dead_night_through_the_nightly_itself_tells_the_cause_once(
    tmp_home: Path, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next night's own recovery (`_close_dead_work`) closes the dead night with its cause; the note says
    the cause and that the night continued the job; the run's line says the cause."""
    paths, db = _setup()
    dead = DeadNight(paths, db, clock)
    dead.beat(LIMIT - 110)
    dead.beat(LIMIT - 5)
    calls: list[dict[str, Any]] = []
    real = ownership.recover

    def recover(*a: Any, **k: Any) -> ownership.Recovery:
        calls.append(k)
        k.setdefault("events", FakeEventLog([]))
        k.setdefault("boot", dead.task - timedelta(days=1))
        return real(*a, **k)

    monkeypatch.setattr(ownership, "recover", recover)
    from ns_studio import daily

    report = daily.run_daily(jobs.open_env(light=True), rng=random.Random(1),
                             run_fn=lambda ctx: [], awake=_nothing)
    assert calls and report["jobs"][0]["job_id"] == dead.job_id and report["jobs"][0]["resumed"] is True
    head = (f"Windows stopped the night's run at {hhmm(dead.task + timedelta(seconds=LIMIT))}, at the 6-hour "
            "limit it allows the nightly task")
    assert dead.note().startswith(f"{head}, while {CH}'s video was at {dead.stage}. ")


class _Nothing:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *a: object) -> None:
        return None


def _nothing() -> _Nothing:
    return _Nothing()
