"""Card night-fits (night-fits-a): a night is measured, and fits the nightly task's time limit.

Windows stops the nightly task 6 hours after it started it (`schedule_cfg.TASK_TIME_LIMIT_S`). Before:
nothing timed a night beyond log lines, nothing checked the time before a job or a stage, and the GPU wait
could take 2 of the 6 hours. After: every stage, wait for the graphics card and job is in the night ledger
(`data/ledger/ledger-<yyyymm>.jsonl`); a night the task started never begins a job or a stage that would end
after `task start + limit - 10 min`; what it left continues the next night first; and the owner reads what
was left, with the numbers, in the morning.

Every night here is the real `daily.run_daily` on a tmp home (fake services, the real ownership records and
orchestrator, stub stages registered for the plan) with an injected clock: wall time and the monotonic counter
come from one number, and each stub stage moves it on by the seconds the test names. New names are reached
through `getattr` or the signature, so the code before the card fails at an assertion.
"""

from __future__ import annotations

import importlib
import inspect
import json
import random
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ns_common.acquire import Endpoint
from ns_common.db import Db
from ns_common.paths import Paths
from ns_studio import daily, jobs, notifications, ownership
from ns_studio import orchestrator as orch
from ns_studio.channels import cadence, crud
from ns_studio.stages import REGISTRY, StageState
from tests.helpers import make_channel

LIMIT_S = 6 * 3600
MARGIN_S = 600
REPO = Path(__file__).resolve().parents[1]


def _module(name: str) -> Any:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


# ------------------------------------------------------------------ the clock, the stages, the GPU
class FakeClock:
    """Wall time and the monotonic counter from one number; thread-safe (the night's beat reads it too)."""

    def __init__(self, start: datetime) -> None:
        self.start, self._t, self._mu = start, 0.0, threading.Lock()

    def now(self) -> datetime:
        with self._mu:
            return self.start + timedelta(seconds=self._t)

    def monotonic(self) -> float:
        with self._mu:
            return self._t

    def advance(self, seconds: float) -> None:
        with self._mu:
            self._t += seconds


class Stub:
    """A stage that moves the clock on by `seconds[name]`; the last one records the finished video."""

    def __init__(
        self, name: str, tenant: str | None, inputs: tuple[str, ...], world: World, last: bool
    ) -> None:
        self.name, self.tenant, self.inputs, self.outputs = name, tenant, inputs, (f"{name}.json",)
        self.world, self.last = world, last

    def run(self, ctx: Any) -> None:
        self.world.ran.append(self.name)
        self.world.clock.advance(self.world.seconds.get(self.name, 60))
        hold = self.world.hold.get(self.name)
        if hold is not None:
            hold()
        ctx.write_json(self.outputs[0], {"stage": self.name})
        if self.last:
            kind = "long" if ctx.fmt == "master_first" else "short"
            ctx.db.execute(
                "insert or ignore into outputs(output_id, job_id, channel, kind, title, path, created_at)"
                " values (?,?,?,?,?,?,?)",
                (
                    f"{ctx.job_id}/{kind}",
                    ctx.job_id,
                    ctx.channel.slug,
                    kind,
                    "A finished video",
                    str(ctx.file(self.outputs[0])),
                    self.world.clock.now().isoformat(timespec="seconds"),
                ),
            )


class FakeAcquirer:
    """Grants every service at once after moving the clock by the wait and the load it was told; it says
    both apart (`last_timing`) and records the `timeout_s` each call was given (None: the default)."""

    def __init__(self, clock: FakeClock, wait_s: float = 0, load_s: float = 0) -> None:
        self.clock, self.wait_s, self.load_s = clock, wait_s, load_s
        self.calls: list[tuple[str, int | None]] = []
        self.last_timing: tuple[float, float] | None = None

    def acquire(
        self, service: str, owner: str, *, model_alias: str | None = None, timeout_s: int | None = None
    ) -> Endpoint:
        self.calls.append((service, timeout_s))
        if service in ("llama", "tts", "whisper"):
            self.clock.advance(self.wait_s + self.load_s)
            self.last_timing = (self.wait_s, self.load_s)
        return Endpoint("inproc")

    def release(self, service: str, owner: str) -> None:
        return None


PLAN = (("nf_a", "llama"), ("nf_b", "llama"), ("nf_c", None), ("nf_d", None))


class World:
    """One install home with the stub plan for master_first and shorts_only."""

    def __init__(self, tmp_home: Path, monkeypatch: pytest.MonkeyPatch, **channel: Any) -> None:
        self.home = tmp_home
        self.paths = Paths.from_env()
        self.paths.ensure()
        self.db = Db(self.paths.db_path)
        self.db.migrate()
        self.clock = FakeClock(datetime.now(UTC).replace(microsecond=0))
        self.seconds: dict[str, float] = {}
        self.hold: dict[str, Any] = {}
        self.ran: list[str] = []
        self.old_jobs = 0
        self.monkeypatch = monkeypatch
        prev = ()
        for i, (name, tenant) in enumerate(PLAN):
            monkeypatch.setitem(REGISTRY, name, Stub(name, tenant, prev, self, i == len(PLAN) - 1))
            prev = (f"{name}.json",)
        for fmt in ("master_first", "shorts_only"):
            monkeypatch.setitem(orch.FORMAT_STAGES, fmt, [n for n, _t in PLAN])
        self.add_channel(**channel)
        self.env = jobs.open_env(light=True)
        self.acquirer = FakeAcquirer(self.clock)
        self.env.services.acquirer = self.acquirer  # type: ignore[assignment]

    def add_channel(self, **kw: Any) -> None:
        created = (datetime.now(UTC) - timedelta(weeks=11)).isoformat(timespec="seconds")
        crud.add_channel(
            self.paths, self.db, make_channel(**{"format": "master_first", "created_at": created, **kw})
        )

    def night(
        self,
        *,
        left_s: float | None = None,
        started: Any = "none",
        process: Any = None,
        environ: dict[str, str] | None = None,
        notifier: Any = None,
    ) -> dict[str, Any]:
        """One `run_daily`. `left_s`: the task started so that this much is left before the deadline."""
        env = dict(environ or {})
        if left_s is not None:
            started = self.clock.now() - timedelta(seconds=LIMIT_S - MARGIN_S - left_s)
        if isinstance(started, datetime):
            env["NS_NIGHT_STARTED"] = started.strftime("%Y-%m-%dT%H:%M:%SZ")
        params = inspect.signature(daily.run_daily).parameters
        extra = {
            k: v
            for k, v in {
                "clock": self.clock,
                "environ": env,
                "process": process or FakeProcess(self.clock.now()),
            }.items()
            if k in params
        }
        return daily.run_daily(
            self.env, now=self.clock.now(), rng=random.Random(1), notifier=notifier, **extra
        )

    def history(
        self,
        *,
        stage: tuple[str, float] | None = None,
        each_s: float | None = None,
        plan: str = "master_first",
        n: int = 5,
        kind: str = "stage",
    ) -> None:
        """Ledger lines an earlier night of this install wrote, in jobs of `plan`: `n` finished passes of
        one `stage`, or of every stage of the plan at `each_s` seconds (a whole job's work)."""
        at = (self.clock.now() - timedelta(days=2)).isoformat(timespec="seconds")
        runs = [stage] if stage is not None else [(name, each_s) for name, _t in PLAN]
        lines = []
        for _i in range(n):
            self.old_jobs += 1  # every pass is a job of its own (the samples are one per job)
            lines += [
                {"kind": kind, "at": at, "job_id": f"old-{plan}-{self.old_jobs}", "plan": plan, "stage": name,
                 "seconds": seconds, "outcome": "done", "origin": "daily"}
                for name, seconds in runs
            ]
        f = self.ledger_dir() / f"ledger-{self.clock.now():%Y%m}.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            fh.write("".join(json.dumps(x) + "\n" for x in lines))

    def ledger_dir(self) -> Path:
        return self.home / "data" / "ledger"

    def lines(self, kind: str | None = None, run_id: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for f in sorted(self.ledger_dir().glob("*.jsonl")) if self.ledger_dir().exists() else []:
            for text in f.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(text)
                except ValueError:
                    continue
                if (kind is None or item.get("kind") == kind) and (
                    run_id is None or item.get("run_id") == run_id
                ):
                    out.append(item)
        return out

    def notes(self) -> list[dict[str, Any]]:
        f = self.home / "run" / "morning_note.json"
        return json.loads(f.read_text(encoding="utf-8"))["notes"] if f.exists() else []

    def cards(self) -> list[dict[str, Any]]:
        out = []
        for r in self.db.query("select * from dashboard_jobs where kind='run' order by created_at, rowid"):
            payload = json.loads(r["params_json"])
            out.append({**dict(r), "result": payload["result"] or {}})
        return out


class FakeProcess:
    """`psutil.Process()` of a run with the given parent chain (nearest first)."""

    def __init__(
        self,
        created: datetime,
        parents: list[Any] | None = None,
        name: str = "studio.exe",
        cmdline: list[str] | None = None,
    ) -> None:
        self._created, self._parents, self._name = created, parents or [], name
        self._cmdline = cmdline or [name]

    def parents(self) -> list[Any]:
        return self._parents

    def name(self) -> str:
        return self._name

    def cmdline(self) -> list[str]:
        return self._cmdline

    def create_time(self) -> float:
        return self._created.timestamp()


def _one(items: list[Any]) -> Any:
    """The one item of `items` (an assertion, not an unpacking error, when there are more or none)."""
    assert len(items) == 1, items
    return items[0]


@pytest.fixture
def world(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> World:
    return World(tmp_home, monkeypatch)


# ------------------------------------------------------------------ 1. every route into the ledger
def test_every_stage_acquire_and_load_is_written_to_the_ledger(world: World) -> None:
    world.seconds = {"nf_a": 300, "nf_b": 600, "nf_c": 900, "nf_d": 120}
    world.acquirer.wait_s, world.acquirer.load_s = 40, 20
    report = world.night(left_s=5 * 3600)
    run_id = report["run_id"]
    assert [j["job_id"] for j in report["jobs"]] and report["failures"] == []
    (job_id,) = [j["job_id"] for j in report["jobs"]]

    start = _one(world.lines("night_start"))
    assert start["run_id"] == run_id and start["limit"] == LIMIT_S and start["start_basis"] == "stub"
    assert datetime.fromisoformat(start["deadline"]) - datetime.fromisoformat(
        start["task_start"]
    ) == timedelta(seconds=LIMIT_S - MARGIN_S)
    assert "simulated_card" in start and start["room_s"] == LIMIT_S - MARGIN_S - 900, "default overhead"
    # the real card (no `fake_gpu`): `night_start` and the timing say null, no line carries a mark (H-10)
    assert start["simulated_card"] is None and report["timing"]["simulated_card"] is None
    assert not [x for x in world.lines(run_id=run_id) if x.get("simulated_card") is not None]
    first_stage = _one(world.lines("first_stage", run_id))
    assert (first_stage["stage"], first_stage["overhead_s"]) == ("nf_a", LIMIT_S - MARGIN_S - 5 * 3600 + 60)
    stages = world.lines("stage", run_id)
    assert [(s["stage"], s["seconds"], s["job_id"], s["origin"]) for s in stages] == [
        (n, world.seconds[n], job_id, "daily") for n, _t in PLAN
    ]
    acquires = world.lines("acquire", run_id)
    assert [(a["service"], a["seconds"], a["wait_s"], a["load_s"]) for a in acquires] == [
        ("llama", 60, 40, 20)
    ]
    end_job = _one(world.lines("job_end", run_id))
    assert end_job["outcome"] == "done" and end_job["seconds"] == 300 + 600 + 900 + 120 + 60
    end = _one(world.lines("night_end"))
    assert end["used_s"] == report["timing"]["used_s"] and end["left_s"] == report["timing"]["left_s"]
    used = LIMIT_S - MARGIN_S - 5 * 3600 + 1980
    assert report["timing"]["used_s"] == used and report["timing"]["left_s"] == 5 * 3600 - 1980
    assert report["timing"]["gpu_s"] == 60 and report["timing"]["stages_s"] == 1920

    # a job started outside the nightly (`studio run`, a dashboard job, a Retry) writes its stage lines too
    ctx = jobs.make_job(
        world.env, crud.get_channel(world.paths, "after-dark"), "master_first", origin="manual"
    )
    ctx.clock = world.clock  # type: ignore[attr-defined]
    orch.run_job(ctx)
    by_hand = [s for s in world.lines("stage") if s["job_id"] == ctx.job_id]
    assert [(s["stage"], s["origin"], s["run_id"]) for s in by_hand] == [
        (n, "manual", None) for n, _t in PLAN
    ]


# ------------------------------------------------------------------ 2, 3. the job budget, both ends
def _a_short_first(world: World) -> None:
    """A Shorts channel that comes first by name: its Short (4 x 25 s) is made before the master is weighed,
    so the master is not the night's first work (a night's first job is always begun: rule (b))."""
    world.add_channel(slug="a-shorts", display_name="Shorts", format="shorts_only", shorts_per_day_max=1,
                      niche="true stories of small towns that lost something")
    world.seconds = {name: 25 for name, _t in PLAN}


@pytest.mark.parametrize("left_min, started", [(121, True), (120, True), (119, False)])
def test_a_job_that_would_end_after_the_deadline_is_not_started(
    world: World, left_min: int, started: bool
) -> None:
    _a_short_first(world)
    world.history(each_s=30 * 60)  # every master stage took at most 30 min: the job's work is 2 h
    world.history(each_s=25, plan="shorts_only")
    report = world.night(left_s=left_min * 60 + 100)  # the Short takes 100 s first
    assert [j["channel"] for j in report["jobs"]][:1] == ["a-shorts"]
    if started:
        assert len(report["jobs"]) == 2 and not report.get("out_of_time")
        return
    assert [j["channel"] for j in report["jobs"]] == ["a-shorts"] and report["failures"] == []
    left = _one(report.get("out_of_time") or [])
    assert {k: left.get(k) for k in ("channel", "fmt", "jobs", "need_s", "left_s", "basis", "reason")} == {
        "channel": "after-dark", "fmt": "master_first", "jobs": 1, "need_s": 7200, "left_s": 7140,
        "basis": "history", "reason": "new_no_room",
    }
    masters = world.db.query("select * from combos where channel='after-dark'")
    assert masters == [], "nothing of the master was created"
    note = notifications.time_note(report) or ""
    assert (
        "After-dark's full video was not started: it takes about 2 h here, and 1 h 59 min were left; the "
        "channel stays due, so the next nightly run plans it again, first (while Automatic runs are on in "
        "Settings)." in note
    )
    assert len([n for n in world.notes() if "(NS-W211)" in n["message"]]) == 1
    world.clock.advance(24 * 3600)
    due = cadence.plan_daily(world.paths, world.db, world.clock.now())
    assert "after-dark" in [d.channel.slug for d in due], "the channel is planned again the next night"


def test_a_night_on_the_simulated_card_marks_every_ledger_line_and_the_note(world: World) -> None:
    """H-10 x night-fits: on an install that runs on the simulated graphics card (`fake_gpu` on an engine-test
    install) every ledger line of the night, the report's `timing`, each decision's sentence and the morning
    note's timing sentence carry `gpu.simulated_mark`, so a laptop's times are never read as the GPU PC's."""
    from ns_common.config import save_runtime
    from ns_common.models import Runtime
    from ns_supervisor.gpu import SIMULATED as MARK

    rt = Runtime.default("cpu-dev").model_copy(update={"fake_footage": True, "fake_gpu": True})
    save_runtime(world.paths, rt)
    _a_short_first(world)
    world.history(each_s=30 * 60)
    world.history(each_s=25, plan="shorts_only")
    report = world.night(left_s=119 * 60 + 100)  # the master does not fit after the Short (as above)
    tonight = world.lines(run_id=report["run_id"])
    kinds = {"night_start", "first_stage", "stage", "job_end", "decision", "night_end"}
    assert {x["kind"] for x in tonight} >= kinds
    unmarked = [x for x in tonight if x.get("simulated_card") != MARK]
    assert not unmarked, unmarked
    assert report["timing"]["simulated_card"] == MARK
    left = _one(report.get("out_of_time") or [])
    assert left["said"].startswith(f"{MARK}: after-dark's full video was not started"), left
    note = notifications.time_note(report) or ""
    assert note.startswith(f"{MARK}: Last night's run used "), note
    assert f"{MARK}: after-dark's full video was not started: it takes about 2 h here" in note, note
    left_notes = [n["message"] for n in world.notes() if "(NS-W211)" in n["message"]]
    assert len(left_notes) == 1 and left_notes[0].startswith(f"{MARK}: "), left_notes
    # a job made outside the night (Create) on this install: its ledger lines carry the mark too
    channel = crud.get_channel(world.paths, "after-dark")
    ctx = jobs.make_job(world.env, channel, "master_first", origin="manual")
    ctx.clock = world.clock  # type: ignore[attr-defined]
    orch.run_job(ctx)
    by_hand = [s for s in world.lines("stage") if s["job_id"] == ctx.job_id]
    assert by_hand and all(s.get("simulated_card") == MARK for s in by_hand), by_hand
    # the history this install wrote before it ran on the simulated card keeps no mark
    old = [x for x in world.lines() if str(x.get("job_id", "")).startswith("old-")]
    assert old and all("simulated_card" not in x for x in old)


@pytest.mark.parametrize("left_min, started", [(180, True), (179, False)])
def test_with_no_history_the_default_estimate_is_used_and_said(
    world: World, left_min: int, started: bool
) -> None:
    nightclock = _module("ns_studio.nightclock")
    default = getattr(nightclock, "DEFAULT_JOB_S", {}).get("master_first")
    assert default == 3 * 3600, "a first night on a new PC plans a full video at 3 h"
    _a_short_first(world)
    world.history(each_s=25, plan="shorts_only")  # the Shorts are measured, the masters never were
    report = world.night(left_s=left_min * 60 + 100)
    if started:
        assert len(report["jobs"]) == 2
        return
    left = _one(report.get("out_of_time") or [])
    assert (left["basis"], left["need_s"], left["left_s"]) == ("default", default, left_min * 60)
    note = notifications.time_note(report) or ""
    assert "Nightshift plans 3 h for one until it has timed one on this PC, and 2 h 59 min were left" in note


def test_a_job_longer_than_what_is_left_is_begun_when_it_is_the_nights_first(world: World) -> None:
    """Rule (b): a fresh night always begins its first job; the stage check cuts it at a boundary and the
    next night continues it, so an estimate longer than what is left never locks a plan out."""
    world.history(each_s=30 * 60)  # 2 h of work
    world.seconds = {name: 30 * 60 for name, _t in PLAN}
    report = world.night(left_s=3700)  # the first stage (30 min) fits, the job (2 h) does not
    assert world.ran == ["nf_a", "nf_b"], "begun, then cut before the stage that no longer fits"
    left = _one(report.get("out_of_time") or [])
    assert (left["stage"], left.get("resumed")) == ("nf_c", None) and report["failures"] == []
    world.clock.advance(24 * 3600)
    world.ran.clear()
    world.night(left_s=5 * 3600)
    assert world.ran == ["nf_c", "nf_d"]


def test_a_video_longer_than_three_nights_is_said_and_never_ages_out(world: World) -> None:
    """Rule (c): a job left for time ages from the night that last worked on it, so a video that needs four
    nights is continued every night (the 3-day rule counts from its creation for every other job)."""
    world.history(each_s=16000)  # 4 x 16000 s: more than three nights of 5 h 50 min
    world.seconds = {name: 16000 for name, _t in PLAN}
    first = world.night(left_s=21000)
    long_job = _one(first.get("long_jobs") or [])
    assert (long_job["need_s"], long_job["nights"], long_job["reason"]) == (64000, 4, "first_work_long")
    note = notifications.time_note(first) or ""
    assert (
        "After-dark's full video takes about 17 h 46 min of work here, more than the 5 h 50 min this night "
        "had left, so it is begun as the night's first work and made over about 4 nights: each nightly run "
        "after this one continues it where the last one stopped (while Automatic runs are on in Settings)."
        in note
    )
    # the ways out that exist (the Queue's Retry, Create, Channels > Default type) and what each does to it
    assert (
        "That is more than 3 nights for one video. Once it is left half made, Retry on its Queue card "
        "finishes it in one go (a run you start has no time limit). To have after-dark's next videos in one "
        "night, make them from Create, or set Default type to Shorts only for after-dark in Channels (the "
        "nightly run then no longer continues this half-made full video; Retry on its card still finishes "
        "it)." in note
    )
    assert world.ran == ["nf_a"]
    for night in range(2, 5):  # 25 h apart: the fourth night is more than 3 days after the job was made
        world.clock.advance(25 * 3600)
        report = world.night(left_s=21000)
        assert report["failures"] == [] and not report.get("long_jobs"), night
    assert world.ran == ["nf_a", "nf_b", "nf_c", "nf_d"], "continued every night until it finished"
    assert len(world.db.query("select * from outputs where kind='long'")) == 1


def test_one_pass_over_a_night_is_said_once_tried_as_a_nights_first_work_and_heals(world: World) -> None:
    """Review blocker 2: one pass over what a fresh night has (here a by-hand master that slept 7 h in nf_c)
    does not stop nightly masters. It is a retest: begun only as a night's first work, where it is measured
    again; the normal pass then replaces it and masters are made in one night again."""
    nightfit = _module("ns_studio.nightfit")
    for seconds in (100, 100, 100, 100, 7 * 3600):
        world.history(stage=("nf_c", seconds), n=1)
    first = world.night(left_s=5 * 3600)
    assert world.ran == ["nf_a", "nf_b"], "begun; stopped before the retest stage (the night has begun work)"
    left = _one(first.get("out_of_time") or [])
    assert (left["stage"], left["reason"], left["answer"]) == ("nf_c", "stop_retest_waits", "stop")
    note = notifications.time_note(first) or ""
    assert "After-dark's video stopped before nf_c: that stage took 7 h once, longer than the 5 h 35 min a " \
           "night has here, so it is tried again only as the first work of a night" in note
    world.clock.advance(24 * 3600)
    world.ran.clear()
    second = world.night(left_s=5 * 3600)
    assert world.ran == ["nf_c", "nf_d"] and len(second["jobs"]) == 1, "the next night's first work"
    assert nightfit is not None
    e = nightfit.estimate("nf_c", ledger_samples(world, "nf_c"), 20100)
    assert (e.retest, e.cannot_fit, e.seconds) == (False, False, 100), "healed: planned with normal passes"


def ledger_samples(world: World, stage: str, plan: str = "master_first") -> list[float]:
    from ns_studio import ledger

    return ledger.stage_samples(ledger.read(world.paths), plan, stage)


def test_two_passes_over_a_night_give_the_stage_up_said_once_and_one_pass_from_create_heals(
    world: World,
) -> None:
    """Rule 3: two of the last five passes over a fresh night's room, the newest among them: no master is
    begun at night, said once in the morning note (the Last night card says it each night); one pass inside a
    night's time from Create (no time limit) is the newest, and the next night makes the master again."""
    world.history(stage=("nf_c", 21600), kind="cut_expected", n=2)
    report = world.night(left_s=21000)
    assert world.ran == [] and report["jobs"] == [] and report["failures"] == []
    left = _one(report.get("out_of_time") or [])
    assert (left["reason"], left["stage"], left["told"]) == ("cannot_fit_new", "nf_c", True)
    note = notifications.time_note(report) or ""
    assert (
        "After-dark's full videos are not made at night: their nf_c stage took longer than the 5 h 35 min a "
        "night has here on 2 of its last 2 passes, the longest 6 h. Make them from Create (a run you start "
        "has no time limit), or set Default type to Shorts only for after-dark in Channels" in note
    )
    world.clock.advance(24 * 3600)
    again = world.night(left_s=21000)
    assert _one(again.get("out_of_time") or [])["told"] is False, "the card says it each night"
    assert notifications.time_note(again) is None, "the note said it once"
    channel = crud.get_channel(world.paths, "after-dark")
    ctx = jobs.make_job(world.env, channel, "master_first", origin="manual")
    ctx.clock = world.clock  # type: ignore[attr-defined]
    orch.run_job(ctx)  # made from Create: nf_c takes 60 s, the newest pass
    nightfit = _module("ns_studio.nightfit")
    assert nightfit is not None
    e = nightfit.estimate("nf_c", ledger_samples(world, "nf_c"), 20100)
    assert (e.cannot_fit, e.retest, e.seconds) == (False, False, 60), "healed by one pass inside a night"
    # the night's decision for the channel's next master (the one just made from Create makes it not due)
    from ns_studio import ledger, nightclock

    start = nightclock.TaskStart(world.clock.now(), nightclock.START_STUB)
    lines = ledger.read(world.paths)
    night = nightclock.Night(world.paths, "probe", world.clock, start, LIMIT_S, None, lines)
    d = night.ask(nightfit.NEW, "master_first", [n for n, _t in PLAN])
    assert (d.answer, d.reason) == (nightfit.BEGIN, nightfit.FITS)


def _first_stage_lines(world: World, overhead_s: float) -> None:
    """Five earlier nights that each spent `overhead_s` before their first stage: what a fresh night has
    here is 21000 - overhead_s."""
    at = (world.clock.now() - timedelta(days=3)).isoformat(timespec="seconds")
    f = world.ledger_dir() / f"ledger-{world.clock.now():%Y%m}.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        for _ in range(5):
            fh.write(json.dumps({"kind": "first_stage", "at": at, "overhead_s": overhead_s}) + "\n")


@pytest.mark.parametrize("pass_s, given_up", [(20000, False), (20001, True)])
def test_a_stage_that_fits_the_full_night_but_not_a_fresh_ones_room_is_begun_or_given_up(
    world: World, pass_s: int, given_up: bool
) -> None:
    """Review blocker 1, both ends of the band: the capacity is 21000 s, a fresh night here has 20000 s (5
    nights measured 1000 s before their first stage). A half-made job's next stage newly measured twice at
    20000 s is begun on a fresh night with 20000 s left; at 20001 s it can never fit what a fresh night
    has: it is given up at once, with its card and its sentence, never skipped as "continues next"."""
    _first_stage_lines(world, 1000)
    _stopped_before_nf_c(world, 1199)  # night 1: nf_a and nf_b done, stopped before nf_c
    job_id = _one([r["job_id"] for r in world.db.query("select distinct job_id from combos")])
    world.history(stage=("nf_c", pass_s), n=2)  # two newer passes (from Retry or Create elsewhere)
    world.clock.advance(24 * 3600)
    world.ran.clear()
    world.seconds["nf_c"] = 100
    report = world.night(left_s=20000)
    if not given_up:
        assert world.ran == ["nf_c", "nf_d"] and [j["job_id"] for j in report["jobs"]] == [job_id]
        return
    assert world.ran == [] and report["jobs"] == [] and report["failures"] == []
    left = _one(report.get("out_of_time") or [])
    assert (left["job_id"], left["answer"], left["reason"]) == (job_id, "cannot_fit", "cannot_fit_job")
    rec = ownership.read_record(world.paths, job_id) or {}
    assert rec.get("cannot_fit") == left["said"]
    assert ownership.resume_problem(world.paths, rec) == left["said"]
    (card,) = world.cards()
    assert card["result"]["interrupted"]["resume"] == "needs_retry" and left["said"] in card["message"]
    assert "continues" not in left["said"]
    world.clock.advance(24 * 3600)
    third = world.night(left_s=20000)
    assert world.ran == [] and not [x for x in third.get("out_of_time") or [] if x.get("job_id")], "said once"


def test_a_night_that_finishes_no_stage_does_not_make_the_job_younger_and_two_give_it_up(
    world: World,
) -> None:
    """Review blocker 1, the zero-progress loop: a cut that finished no stage of the job leaves its age
    where it was (`progressed_at`), and the second night it is the first work and still has too little time
    gives it up with its card (rule 7), instead of cutting it with nothing done for ever."""
    _stopped_before_nf_c(world, 1199)
    job_id = _one([r["job_id"] for r in world.db.query("select distinct job_id from combos")])
    rec = ownership.read_record(world.paths, job_id) or {}
    progressed = rec.get("progressed_at")
    assert progressed and (rec.get("cuts"), rec.get("misses")) == (1, 0), "night 1 finished nf_a and nf_b"
    world.clock.advance(24 * 3600)
    world.ran.clear()
    second = world.night(left_s=1199)
    rec = ownership.read_record(world.paths, job_id) or {}
    assert world.ran == []
    assert (rec.get("progressed_at"), rec.get("misses"), rec.get("cuts")) == (progressed, 1, 2)
    assert _one(second["out_of_time"])["reason"] == "resume_first_miss"
    world.clock.advance(24 * 3600)
    third = world.night(left_s=1199)
    left = _one(third["out_of_time"])
    assert (left["answer"], left["reason"]) == ("cannot_fit", "missed_twice")
    assert (
        "After-dark's half-made video is given up by the nightly run: it was the night's first work on 2 "
        "nights since a stage of it was last finished" in (notifications.time_note(third) or "")
    )
    (card,) = world.cards()
    assert card["result"]["interrupted"]["resume"] == "needs_retry"


def _given_up_by_two_misses(world: World) -> str:
    """Night 1 leaves the job before nf_c; nights 2 and 3 begin with it and have too little time: given up."""
    _stopped_before_nf_c(world, 1199)
    job_id = _one([r["job_id"] for r in world.db.query("select distinct job_id from combos")])
    for _night in (2, 3):
        world.clock.advance(24 * 3600)
        world.night(left_s=1199)
    assert (ownership.read_record(world.paths, job_id) or {}).get("cannot_fit"), "given up after two misses"
    return job_id


def test_the_two_misses_mark_the_plans_stage_so_the_next_job_is_not_begun_and_create_heals(
    world: World,
) -> None:
    """Review note (same-stage cycle): the misses that give a job up are evidence for its PLAN's stage (ledger
    `missed`, over the room by the shortfall), so the next job of the plan is answered CANNOT_FIT before it
    redoes nf_a and nf_b, with the sentence that says why; one pass of nf_c from Create heals it."""
    _given_up_by_two_misses(world)
    missed = world.lines("missed")
    marks = [(m["stage"], m["sample_id"].rsplit("/", 1)[1]) for m in missed]
    assert marks == [("nf_c", "miss1"), ("nf_c", "miss2")]
    world.clock.advance(24 * 3600)
    world.ran.clear()
    report = world.night(left_s=5 * 3600)
    assert world.ran == [] and report["jobs"] == [], "the earlier stages are not redone"
    left = _one([x for x in report.get("out_of_time") or [] if not x.get("job_id")])
    assert (left["answer"], left["reason"], left["stage"]) == ("cannot_fit", "cannot_fit_new", "nf_c")
    assert "their nf_c stage did not fit the" in left["said"]
    assert "each time, even as a night's first work, too little time was left for it" in left["said"]
    channel = crud.get_channel(world.paths, "after-dark")
    ctx = jobs.make_job(world.env, channel, "master_first", origin="manual")
    ctx.clock = world.clock  # type: ignore[attr-defined]
    orch.run_job(ctx)  # made from Create: nf_c runs 500 s, the newest pass
    from ns_studio import ledger, nightclock, nightfit

    start = nightclock.TaskStart(world.clock.now(), nightclock.START_STUB)
    lines = ledger.read(world.paths)
    night = nightclock.Night(world.paths, "probe", world.clock, start, LIMIT_S, None, lines)
    d = night.ask(nightfit.NEW, "master_first", [n for n, _t in PLAN])
    assert (d.answer, d.reason) == (nightfit.BEGIN, nightfit.FITS), "healed by one pass inside a night"


@pytest.mark.parametrize("healed", [True, False])
def test_an_interrupted_retry_of_a_given_up_job_leaves_it_to_the_same_decision(
    world: World, healed: bool
) -> None:
    """Review note (interrupted Retry): a Retry clears the give-up when it begins (`ownership.hold`); if the
    Retry is itself interrupted, the job is an interrupted nightly job like any other, and the nightly decides
    about it again: continued when the stage has healed, given up again (with its card) when it has not."""
    job_id = _given_up_by_two_misses(world)
    with pytest.raises(KeyboardInterrupt), ownership.hold_job(world.paths, job_id):
        raise KeyboardInterrupt  # the Retry's process stopped before any stage finished
    ownership.write_record(world.paths, job_id, pid=4_000_000)  # ... and is gone (not this test's process)
    rec = ownership.read_record(world.paths, job_id) or {}
    assert (rec.get("cannot_fit"), rec.get("resume_problem"), rec.get("misses")) == (None, None, 0)
    world.seconds = {"nf_a": 60, "nf_b": 60, "nf_c": 500, "nf_d": 60}
    if healed:  # a video made from Create: its nf_c pass (500 s) is the newest, inside the room
        channel = crud.get_channel(world.paths, "after-dark")
        ctx = jobs.make_job(world.env, channel, "master_first", origin="manual")
        ctx.clock = world.clock  # type: ignore[attr-defined]
        orch.run_job(ctx)
    world.clock.advance(20 * 3600)  # the next night, inside 3 days of its last progress (night 1)
    world.ran.clear()
    report = world.night(left_s=5 * 3600)
    if healed:
        assert world.ran == ["nf_c", "nf_d"] and [j["job_id"] for j in report["jobs"]] == [job_id]
        return
    assert world.ran == []
    left = _one([x for x in report.get("out_of_time") or [] if x.get("job_id") == job_id])
    assert (left["answer"], left["reason"]) == ("cannot_fit", "cannot_fit_job")
    assert (ownership.read_record(world.paths, job_id) or {}).get("cannot_fit") == left["said"]


def test_a_night_killed_after_it_finished_stages_still_counts_as_progress(world: World) -> None:
    """Review note (kill after progress): Windows kills the night after nf_c finished (no decision is written
    then); the job's age counts from that finished stage in the ledger, not from the night before."""
    _stopped_before_nf_c(world, 1199)
    job_id = _one([r["job_id"] for r in world.db.query("select distinct job_id from combos")])
    first = ownership.read_record(world.paths, job_id) or {}
    night1 = datetime.fromisoformat(str(first.get("progressed_at")))
    world.clock.advance(24 * 3600)
    world.ran.clear()

    def killed() -> None:
        raise KeyboardInterrupt  # stands for Windows ending the task while nf_d runs

    world.hold["nf_d"] = killed
    with pytest.raises(KeyboardInterrupt):
        world.night(left_s=5 * 3600)
    assert world.ran == ["nf_c", "nf_d"]
    rec = ownership.read_record(world.paths, job_id) or {}
    assert rec.get("progressed_at") == night1.isoformat(timespec="seconds"), "no decision wrote the progress"
    anchor = ownership.age_anchor(world.paths, rec)
    assert anchor is not None and anchor > night1 + timedelta(hours=23), "nf_c's finished line counts"
    assert ownership.resume_problem(world.paths, rec, night1 + timedelta(days=3, hours=1)) is None


def test_a_half_made_job_ages_from_its_last_progress_not_from_its_last_cut(world: World) -> None:
    """The age rule: a job ages unless a night FINISHED one of its stages; past `RESUME_WITHIN_DAYS` without
    one it is left for Retry, and its card says so in those words."""
    _stopped_before_nf_c(world, 1199)
    job_id = _one([r["job_id"] for r in world.db.query("select distinct job_id from combos")])
    rec = ownership.read_record(world.paths, job_id) or {}
    assert rec.get("progressed_at"), "the night that finished nf_a and nf_b is the job's last progress"
    anchor = datetime.fromisoformat(str(rec["progressed_at"]))
    assert ownership.resume_problem(world.paths, rec, anchor + timedelta(days=3)) is None
    assert ownership.resume_problem(world.paths, rec, anchor + timedelta(days=3, seconds=1)) == (
        "no night has finished a stage of it for more than 3 days"
    )


def test_a_stage_is_estimated_from_jobs_of_its_own_plan_at_its_worst(world: World) -> None:
    """Blocker 2: `nf_c` is a stage of Shorts and of masters; a master's is not estimated from Shorts, and
    the estimate is the worst recent pass, not the mean."""
    nightclock = _module("ns_studio.nightclock")
    world.history(stage=("nf_c", 100), plan="shorts_only")
    for seconds in (100, 100, 100, 100, 2500):
        world.history(stage=("nf_c", seconds), n=1)
    lines = [x for x in world.lines() if x.get("kind") == "stage"]
    assert nightclock.stage_estimate(lines, "master_first", "nf_c") == (2500, "history")
    assert nightclock.stage_estimate(lines, "shorts_only", "nf_c") == (100, "history")
    assert nightclock.stage_estimate(lines, "master_first", "nf_a") == (600.0, "default")
    world.seconds = {"nf_a": 4000, "nf_b": 10860 - 4000 - 2000}
    report = world.night(left_s=10860)  # 2000 s left before nf_c: a Short's 100 s would say it fits
    assert world.ran == ["nf_a", "nf_b"] and _one(report["out_of_time"])["stage"] == "nf_c"


# ------------------------------------------------------------------ 4, 5. the stage budget and the next night
def _stopped_before_nf_c(world: World, left_before_c: int) -> dict[str, Any]:
    """A night whose job reaches nf_c (estimate 1.2 x 1000 = 1200 s) with `left_before_c` seconds left."""
    world.history(stage=("nf_c", 1200))
    world.seconds = {"nf_a": 4830, "nf_b": 10860 - 4830 - left_before_c, "nf_c": 500, "nf_d": 100}
    return world.night(left_s=10860)  # 181 min: the job's default estimate (3 h) fits


@pytest.mark.parametrize("left_before_c, stops", [(1200, False), (1199, True)])
def test_a_stage_that_would_end_after_the_deadline_leaves_the_job_interrupted(
    world: World, left_before_c: int, stops: bool
) -> None:
    report = _stopped_before_nf_c(world, left_before_c)
    if not stops:
        assert world.ran == ["nf_a", "nf_b", "nf_c", "nf_d"] and len(report["jobs"]) == 1
        return
    assert world.ran == ["nf_a", "nf_b"], "the job stops BEFORE the stage"
    assert report["jobs"] == [] and report["failures"] == [], "left for time is not a failure"
    (job_id,) = [r["job_id"] for r in world.db.query("select distinct job_id from combos")]
    left = _one(report.get("out_of_time") or [])
    assert (left["job_id"], left["stage"], left["need_s"], left["left_s"]) == (job_id, "nf_c", 1200, 1199)
    entries = StageState.load(world.paths.job_dir(job_id)).entries
    assert [n for n, e in entries.items() if e.get("status") == "done"] == ["nf_a", "nf_b"]
    rec = ownership.read_record(world.paths, job_id) or {}
    assert (rec.get("state"), rec.get("interrupted_at_stage"), rec.get("reason")) == (
        "interrupted",
        "nf_c",
        "out_of_time",
    )
    (card,) = world.cards()
    assert card["message"] == (
        "Left at nf_c so the night's run would end inside Windows' 6-hour limit for the nightly task. The "
        "next nightly run continues it (while Automatic runs are on in Settings), or press Retry."
    )
    assert (
        card["result"]["error"]["code"] == "NS-W211" and card["result"]["interrupted"]["resume"] == "pending"
    )


def test_the_next_night_continues_the_job_left_for_time_first(world: World) -> None:
    from ns_dashboard.handlers import Dashboard, build_state
    from ns_dashboard.supervisor_link import SupervisorLink

    told: list[str] = []
    first = _stopped_before_nf_c(world, 1199)
    (job_id,) = [r["job_id"] for r in world.db.query("select distinct job_id from combos")]
    world.clock.advance(24 * 3600)
    world.ran.clear()
    second = world.night(left_s=5 * 3600, notifier=lambda title, message: told.append(message) or True)

    assert first["run_id"] != second["run_id"]
    assert [j["job_id"] for j in second["jobs"]] == [job_id] and second["jobs"][0].get("resumed") is True
    assert world.ran == ["nf_c", "nf_d"], "continued from the stage it was left at, before any planning"
    assert [s["stage"] for s in world.lines("stage", second["run_id"])] == ["nf_c", "nf_d"]
    assert len(world.lines("night_end")) == 2
    assert len(list(world.paths.backup_dir.glob("studio-*.db"))) == 2
    (card,) = world.cards()
    assert card["status"] == "done" and "The nightly run continued it and it finished." in card["message"]
    # one note for the night that left work, none for the night that left nothing; its toast says no more
    w211 = [n for n in world.notes() if "(NS-W211)" in n["message"]]
    assert len(w211) == 1 and (
        "After-dark's video stopped before nf_c: that stage takes about 20 min here and 19 min were left; "
        "the next nightly run goes back to it before starting new videos (while Automatic runs are on in "
        "Settings)." in w211[0]["message"]
    )
    assert told == ["1 master ready for review"], "the job left for time was no interrupted night"
    dash = Dashboard(
        build_state(
            Paths.from_env(), fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
        )
    )
    (last,) = dash.engine.runs(1)
    assert last["run_id"] == second["run_id"] and last["summary"]["timing"]["left_s"] is not None


@pytest.mark.parametrize("left_s, continued", [(1800, True), (1200, True), (1199, False)])
def test_a_half_made_job_that_would_not_fit_tonight_waits_for_the_next_night(
    world: World, left_s: int, continued: bool
) -> None:
    """S3 (`nightfit` rules 6 and 7): its next stage (nf_c, 1200 s by its history) fits what is left (1800,
    1200: both ends), else (1199), as the night's first work, it is a fresh miss: not continued, its record
    keeps the miss and asks for the next night's first place, and the next night continues it."""
    _stopped_before_nf_c(world, 1199)
    job_id = _one([r["job_id"] for r in world.db.query("select distinct job_id from combos")])
    world.clock.advance(24 * 3600)
    world.ran.clear()
    report = world.night(left_s=left_s)
    if continued:
        assert world.ran == ["nf_c", "nf_d"] and [j["job_id"] for j in report["jobs"]] == [job_id]
        # its finished llama group is still taken (as before) but never refused for time, at most 1 s of wait
        assert world.acquirer.calls[-1] == ("llama", 1)
        return
    assert world.ran == [] and report["jobs"] == [] and report["failures"] == []
    left = _one(report.get("out_of_time") or [])
    assert (left["job_id"], left["resumed"], left["need_s"], left["left_s"]) == (job_id, True, 1200, 1199)
    assert left["reason"] == "resume_first_miss"
    assert "after-dark" in report["not_due"], "no new video of the channel starts beside the half-made one"
    assert world.db.query("select count(*) n from combos where item='long'")[0]["n"] == 1
    rec = ownership.read_record(world.paths, job_id) or {}
    assert (rec.get("state"), rec.get("interrupted_at_stage")) == ("interrupted", "nf_c")
    assert (rec.get("misses"), rec.get("cuts"), rec.get("first_next")) == (1, 2, True)
    words = (
        "After-dark's half-made video was not continued: even as the night's first work only 19 min were "
        "left for its next stage, nf_c, which takes about 20 min here; the nightly run tries it once more as "
        "the first work of a night and gives it up for Retry if that time is too short too (while Automatic "
        "runs are on in Settings)."
    )
    assert words in (notifications.time_note(report) or "")
    world.clock.advance(24 * 3600)
    world.night(left_s=5 * 3600)
    assert world.ran == ["nf_c", "nf_d"], "the next night continues it"


def test_the_channel_left_for_time_goes_first_the_next_night(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World(tmp_home, monkeypatch, slug="a-first", format="shorts_only", shorts_per_day_max=1)
    world.add_channel(slug="b-second", display_name="B", format="shorts_only", shorts_per_day_max=1,
                      niche="true stories of small towns that lost something")
    world.history(each_s=250, plan="shorts_only")  # one Short's work is 1000 s here, 250 s a stage
    world.seconds = {"nf_a": 250, "nf_b": 250, "nf_c": 250, "nf_d": 250}  # it takes 1000 s
    one = world.night(left_s=1700)  # room for one: a-first comes first by name, b-second is left
    assert [j["channel"] for j in one["jobs"]] == ["a-first"]
    assert [x["channel"] for x in one["out_of_time"]] == ["b-second"]
    world.clock.advance(24 * 3600)
    two = world.night(left_s=1700)
    assert [j["channel"] for j in two["jobs"]] == ["b-second"], "the channel left last night goes first"
    assert [x["channel"] for x in two["out_of_time"]] == ["a-first"]


# ------------------------------------------------------------------ 6. two nights in a row
def test_two_nights_in_a_row_leave_nothing_behind(world: World) -> None:
    def nothing_left() -> None:
        owners = world.home / "data" / "owners"
        open_records = [
            r for r in ownership.list_records(world.paths) if r.get("state") in ("starting", "running")
        ]
        held = [f.name for f in owners.glob("*.lock") if f.name != "recover.lock"]  # (the scan's own guard)
        assert open_records == [] and held == []
        assert not (world.home / "run" / "night-beat.json").exists()

    first = world.night(left_s=5 * 3600)
    assert len(first["jobs"]) == 1 and (first.get("timing") or {}).get("left_s", 0) > 0
    nothing_left()
    world.clock.advance(24 * 3600)
    second = world.night(left_s=5 * 3600)
    nothing_left()
    assert second["jobs"] == [] and "after-dark" in second["not_due"], "night 2 saw night 1's master"
    assert [x["run_id"] for x in world.lines("night_end")] == [first["run_id"], second["run_id"]]
    assert all("cut_expected" != x["kind"] for x in world.lines())


# ------------------------------------------------------------------ 7. the GPU wait
@pytest.mark.parametrize("nf_a_history, timeout", [(None, 7200), (5000, 4060)])
def test_the_gpu_wait_never_runs_past_what_the_night_can_spend(
    world: World, nf_a_history: int | None, timeout: int
) -> None:
    if nf_a_history is not None:  # the rest of the job: nf_a's worst pass + 3 x 600 s (never measured)
        world.history(stage=("nf_a", nf_a_history))
    world.night(left_s=10860)
    assert world.acquirer.calls == [("llama", timeout)]


@pytest.mark.parametrize("nf_d_history, timeout", [(2000, 860), (None, None)])
def test_a_wait_the_rest_of_the_job_has_no_time_for_waits_only_for_the_next_stage_or_not_at_all(
    world: World, monkeypatch: pytest.MonkeyPatch, nf_d_history: int | None, timeout: int | None
) -> None:
    """S7 between two tenant groups (nf_c takes the tts tenant here), 4860 s left after nf_a and nf_b.
    nf_c 4000 + nf_d 2000: the rest of the job does not fit, so the wait only leaves room for nf_c: 860 s.
    nf_c 5000 (+ nf_d 600, never measured): not even nf_c fits, so no wait is begun: NS-E201 at once, and the
    job is left at nf_c for the next night."""
    monkeypatch.setattr(REGISTRY["nf_c"], "tenant", "tts")
    world.history(stage=("nf_c", 4000 if nf_d_history else 5000))
    if nf_d_history:
        world.history(stage=("nf_d", nf_d_history))
    world.seconds = {"nf_a": 3000, "nf_b": 3000}
    report = world.night(left_s=10860)
    assert world.acquirer.calls[0][0] == "llama" and world.ran[:2] == ["nf_a", "nf_b"]
    if timeout is not None:
        assert world.acquirer.calls[1] == ("tts", timeout)
        return
    assert [c[0] for c in world.acquirer.calls] == ["llama"] and world.ran == ["nf_a", "nf_b"]
    assert report["failures"] == [] and report.get("skipped") is None
    left = _one(report.get("out_of_time") or [])
    assert left["stage"] == "nf_c"
    assert [a["service"] for a in world.lines("acquire")] == ["llama"], "no wait was begun for tts"
    rec = ownership.read_record(world.paths, left["job_id"]) or {}
    assert (rec.get("state"), rec.get("interrupted_at_stage")) == ("interrupted", "nf_c")


# ------------------------------------------------------------------ 8, 9. the task's start
def test_a_night_started_by_hand_has_no_deadline(world: World) -> None:
    world.history(each_s=30 * 3600)  # a 120-hour estimate: no timed night could make it
    report = world.night(started=None)
    start = _one(world.lines("night_start"))
    assert start["limit"] == "none" and start["deadline"] is None and start["start_basis"] == "process"
    assert len(report["jobs"]) == 1 and not report.get("out_of_time"), "nothing is refused for time"
    assert report["timing"]["limit_s"] is None and report["timing"]["left_s"] is None


class Parent(FakeProcess):
    pass


def test_the_task_start_is_read_from_the_stub_then_the_ancestor_then_the_process(world: World) -> None:
    now = world.clock.now()
    task = now - timedelta(minutes=7)
    chain = [
        Parent(now - timedelta(minutes=6), name="powershell.exe", cmdline=["powershell.exe", "-File", "x"]),
        Parent(task, name="cmd.exe", cmdline=["cmd.exe", "/d", "/s", "/c", "... -Nightly -Studio ..."]),
        Parent(now - timedelta(days=3), name="svchost.exe"),
    ]
    stub = now - timedelta(minutes=5)
    world.night(started=stub, process=FakeProcess(now, chain))
    world.clock.advance(24 * 3600)
    now = world.clock.now()
    chain = [
        Parent(now - timedelta(minutes=1), name="powershell.exe"),
        Parent(now - timedelta(minutes=2), name="cmd.exe", cmdline=["cmd.exe", "/c", "x -Nightly"]),
    ]
    world.night(environ={"NS_NIGHT_STARTED": "last tuesday"}, process=FakeProcess(now, chain))
    world.clock.advance(24 * 3600)
    now = world.clock.now()
    world.night(process=FakeProcess(now - timedelta(seconds=3), [Parent(now, name="cmd.exe")]))
    starts = world.lines("night_start")
    assert [(s["start_basis"], s["task_start"]) for s in starts] == [
        ("stub", stub.isoformat(timespec="seconds")),
        ("ancestor", (now - timedelta(days=1, minutes=2)).isoformat(timespec="seconds")),
        ("process", (now - timedelta(seconds=3)).isoformat(timespec="seconds")),
    ]
    assert any("'last tuesday' is not this night's start" in s for s in starts[1]["said"]), starts[1]["said"]
    assert [s["limit"] for s in starts] == [LIMIT_S, LIMIT_S, "none"]


@pytest.mark.parametrize(
    "raw, test_install, limit, said",
    [
        ("1800", True, 1800, False),
        ("60", True, 60, False),
        ("59", True, LIMIT_S, True),
        ("21601", True, LIMIT_S, True),
        ("21600", True, 21600, False),
        ("1800", False, LIMIT_S, True),
    ],
)
def test_the_test_limit_is_taken_only_on_a_test_install_and_inside_its_range(
    world: World, raw: str, test_install: bool, limit: int, said: bool
) -> None:
    world.paths.runtime_file.parent.mkdir(parents=True, exist_ok=True)
    world.paths.runtime_file.write_text(json.dumps({"dev_mode": test_install}), encoding="utf-8")
    started = world.clock.now() - timedelta(seconds=30)
    world.night(started=started, environ={"NS_TEST_NIGHT_LIMIT_S": raw})
    start = _one(world.lines("night_start"))
    assert start["limit"] == limit and start["start_basis"] == "stub"
    margin = min(MARGIN_S, limit // 6)  # a test limit too short for 10 min keeps a sixth of it
    deadline = datetime.fromisoformat(start["deadline"]) - datetime.fromisoformat(start["task_start"])
    assert deadline == timedelta(seconds=limit - margin)
    assert bool([s for s in start["said"] if "NS_TEST_NIGHT_LIMIT_S" in s]) == said


# ------------------------------------------------------------------ 10. the hard mark
def test_the_hard_mark_is_written_two_minutes_before_the_limit_when_still_running(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    nightclock = _module("ns_studio.nightclock")
    if nightclock is not None:
        monkeypatch.setattr(nightclock, "BEAT_S", 0.01)

    def wait_for_the_mark() -> None:  # the stage runs on past the mark; the beat thread looks meanwhile
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not world.lines("cut_expected"):
            time.sleep(0.02)

    # nf_d begins with exactly its floor (600 s) left and runs 1100 s: 20 s past the hard mark
    world.seconds = {"nf_a": 3000, "nf_b": 3000, "nf_c": 10860 - 6000 - 600, "nf_d": 1100}
    world.hold["nf_d"] = wait_for_the_mark
    report = world.night(left_s=10860)
    assert len(report["jobs"]) == 1
    cuts = world.lines("cut_expected")
    assert [(c["stage"], c["job_id"]) for c in cuts] == [("nf_d", report["jobs"][0]["job_id"])], cuts
    # a night that ends before the mark writes none
    world.clock.advance(24 * 3600)
    world.night(left_s=10860)
    assert len(world.lines("cut_expected")) == 1


# ------------------------------------------------------------------ 11. what the owner reads
REPORT = {
    "jobs": [{"job_id": "j1", "channel": "after-dark", "stages": 19}],
    "failures": [],
    "out_of_time": [
        {
            "channel": "quiet-hours",
            "fmt": "master_first",
            "jobs": 1,
            "need_s": 11400,
            "left_s": 3120,
            "basis": "history",
        },
        {
            "channel": "after-dark",
            "job_id": "j2",
            "stage": "voice",
            "jobs": 1,
            "need_s": 900,
            "left_s": 600,
            "basis": "history",
        },
    ],
    "timing": {"limit_s": 21600, "used_s": 20400, "left_s": 600, "gpu_s": 4200},
}


def test_the_morning_note_and_toast_say_what_was_left_for_time(world: World) -> None:
    note = getattr(notifications, "time_note", lambda r: None)(REPORT)
    assert note == (
        "Last night's run used 5 h 40 min of the 6 hours Windows allows it, 1 h 10 min of it getting the "
        "graphics card (waiting for it and loading its models). Quiet-hours's full video was not started: it "
        "takes about 3 h 10 min here, and 52 min were left; the channel stays due, so the next nightly run "
        "plans it again, first (while Automatic runs are on in Settings). After-dark's video stopped before "
        "voice: that stage takes about 15 min here and 10 min were left; the next nightly run goes back to "
        "it before starting new videos (while Automatic runs are on in Settings). To fit more into one "
        "night, set fewer Full videos per week for a channel in Channels, or pause a channel. (NS-W211)"
    )
    toast = notifications.nightly_message(REPORT, 1, 4)
    assert toast == (
        "1 master + 4 Shorts ready for review; 2 videos not finished tonight, to end inside "
        "Windows' 6-hour limit (NS-W211)"
    )
    from ns_supervisor import notify

    assert not notify.is_routine(toast) and not notify.is_routine(note), "never pushed out by routine news"
    assert (
        notifications.nightly_message({**REPORT, "out_of_time": []}, 1, 4)
        == "1 master + 4 Shorts ready for review"
    )
    # (through a night with its own numbers: test_a_job_that_would_end_after_the_deadline_is_not_started)


def test_the_doctor_says_when_the_task_limit_differs(tmp_path: Path) -> None:
    from ns_supervisor import doctor, schedule

    row = getattr(doctor, "task_limit_row", None)
    assert row is not None, "ns doctor has no row for the task's limit"
    paths = Paths(tmp_path / "ns")

    def task(limit: int | None) -> Any:
        return lambda: schedule.TaskState(True, "03:00", limit_s=limit)

    assert row(paths, task(21600)) == (
        "nightly task limit",
        True,
        "6 h, the time each night is planned inside",
    )
    name, ok, words = row(paths, task(3 * 3600))
    assert (
        ok
        and words.startswith("WARN: Windows stops the nightly task after 3 h")
        and "Settings > Schedule" in words
    )
    assert "WARN" not in row(paths, task(0))[2] and "WARN" not in row(paths, task(72 * 3600))[2]
    assert "not read" in row(paths)[2], "a test run never asks Task Scheduler"
    # the ledger goes into the diagnostics zip the partner sends back
    import zipfile

    (paths.data / "ledger").mkdir(parents=True)
    (paths.data / "ledger" / "ledger-202611.jsonl").write_text('{"kind": "night_end"}\n', encoding="utf-8")
    out = doctor.build_bundle(paths, out_dir=tmp_path / "out", freeze=lambda p, v: None, smi=lambda: None,
                              setup_dir=tmp_path / "setup", temp_dir=tmp_path / "temp")
    with zipfile.ZipFile(out) as z:
        assert "ledger/ledger-202611.jsonl" in z.namelist()


# ------------------------------------------------------------------ 14. one bad file among good ones
def test_the_ledger_survives_a_torn_last_line_and_a_locked_file(world: World) -> None:
    month = world.ledger_dir() / f"ledger-{world.clock.now():%Y%m}.jsonl"
    month.parent.mkdir(parents=True, exist_ok=True)
    month.write_bytes(b'{"kind": "stage", "stage": "nf_a", "seco')  # a kill in the middle of a line
    first = world.night(left_s=5 * 3600)
    assert [x["kind"] for x in world.lines(run_id=first["run_id"])][:1] == ["night_start"], (
        "starts on its own line"
    )
    assert len(world.lines("stage", first["run_id"])) == 4
    ledger = _module("ns_studio.ledger")
    assert ledger is not None and len([x for x in ledger.read(world.paths) if x.get("kind") == "stage"]) == 4
    if sys.platform != "win32":
        return
    import ctypes
    from ctypes import wintypes

    create = ctypes.windll.kernel32.CreateFileW  # type: ignore[attr-defined]
    create.restype = wintypes.HANDLE
    handle = create(str(month), 0x80000000, 0, None, 3, 0x80, None)  # GENERIC_READ, no sharing, OPEN_EXISTING
    assert handle not in (None, wintypes.HANDLE(-1).value)
    try:
        world.clock.advance(3600)
        second = world.night(started=None)
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    spare = month.with_name(month.stem + "-2.jsonl")
    assert spare.exists() and any(
        json.loads(x).get("run_id") == second["run_id"]
        for x in spare.read_text(encoding="utf-8").splitlines()
    )
    warns = [
        json.loads(x)
        for f in world.paths.logs.glob("studio-*.jsonl")
        for x in f.read_text(encoding="utf-8").splitlines()
        if "ledger_file_busy" in x
    ]
    assert len(warns) == 1, warns


# ------------------------------------------------------------------ 15. the Last night card
def test_the_last_night_card_reads_what_was_left_and_the_time() -> None:
    queue = (REPO / "ns_dashboard" / "ui" / "src" / "pages" / "Queue.tsx").read_text(encoding="utf-8")
    types = (REPO / "ns_dashboard" / "ui" / "src" / "lib" / "types.ts").read_text(encoding="utf-8")
    assert "out_of_time?: LeftForTime[]" in types and "timing?: NightTiming" in types
    assert "r.summary?.out_of_time" in queue and "r.summary?.timing" in queue
    bundle = (REPO / "ns_dashboard" / "static" / "app.js").read_text(encoding="utf-8")
    assert "Left for time: " in bundle and "until it has timed one on this PC" in bundle
