"""Card gpu-kit: the one-command GPU kit (`dev/kit/gpukit.py`), run end to end on a fake home.

The home is made by the product's own code (`Paths`, `save_runtime`, the studio database's schema) as a real
GPU install (profile gpu, no test switch) or, for the dry run, an engine-test install with `fake_gpu`. The kit
gets three stand-ins (`gpukit.KitEnv`): a clock the test moves, `FakeProduct` (the dashboard API, `ns`,
`studio`, the proof runner, FFmpeg and the nightly stub, each call recorded; what the product would write, it
writes into the home with the product's file layout) and `CannedWindows` (nvidia-smi as recorded on the RTX
5060 Ti, the disk, powercfg, the System log). The boundary test drives the REAL dashboard routes of
`tests/proofhome.py` over loopback HTTP. The real Windows readers refuse to run under pytest.
"""
# ruff: noqa: F811  (a fixture imported by name is redefined by the test argument that asks for it)

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from ns_common.config import save_runtime
from ns_common.db import Db
from ns_common.models import Runtime
from ns_common.paths import Paths
from tests.proofhome import ProofHome, proof_home  # noqa: F401  (`proof_home` is the fixture, used by name)

if TYPE_CHECKING:
    from dev.kit import gpukit
else:
    try:
        from dev.kit import gpukit
    except ImportError:  # a base without the kit: every test then fails in its body, at `kit_here()`
        gpukit = None


def kit_here() -> None:
    assert gpukit is not None, "dev/kit/gpukit.py does not exist: there is no GPU kit"

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
SMI_TABLE = (ROOT / "tests" / "fixtures" / "supervisor" / "nvidia-smi-rtx-5060-ti.txt").read_text("utf-8")
START = datetime(2026, 11, 12, 10, 0).astimezone()  # a Thursday morning; the nightly run is at 03:00
def local(*parts: int) -> datetime:
    return datetime(*parts).astimezone()  # type: ignore[arg-type]


PEXELS = "A" * 20 + "b" * 20 + "7" * 16  # 56 letters and digits: the shape of a Pexels key


def make_home(tmp_path: Path, *, test_install: bool = False) -> Path:
    home = tmp_path / "home"
    paths = Paths(home)
    paths.ensure()
    rt = Runtime.default("gpu")
    if test_install:
        rt = rt.model_copy(update={"fake_footage": True, "fake_gpu": True})
    save_runtime(paths, rt)
    (home / "app").mkdir()
    (home / "app" / "VERSION").write_text(VERSION + "\n", encoding="utf-8")
    db = Db(paths.db_path)
    db.migrate()
    db.connect().close()
    return home


class Traps:
    """Named points where a test closes the kit's window (a KeyboardInterrupt), each once."""

    def __init__(self) -> None:
        self.armed: set[str] = set()

    def hit(self, name: str) -> None:
        if name in self.armed:
            self.armed.discard(name)
            raise KeyboardInterrupt(name)


class Clock:
    def __init__(self, start: datetime = START) -> None:
        self.t = start

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=seconds)


class FakeProc:
    def __init__(self, fp: FakeProduct, pid: int, job_id: str, polls: int = 2) -> None:
        self.fp, self.pid, self.job_id, self.polls, self.rc = fp, pid, job_id, polls, None

    def poll(self) -> int | None:
        if self.rc is not None:
            return self.rc
        if "studio running" in self.fp.traps.armed:
            self.rc = -1  # the window closed: its child goes with it
            self.fp.traps.hit("studio running")
        self.polls -= 1
        if self.polls > 0:
            return None
        self.rc = self.fp.studio_rc
        if self.rc == 0:
            self.fp.finish_job(self.job_id)
        self.fp.traps.hit("studio finished")
        return self.rc

    def kill(self) -> None:
        self.rc = -9


class FakeProduct:
    """The install's routes, recorded: the channels, Automatic runs, the engine; files as the product."""

    def __init__(self, home: Path, traps: Traps, clock: Clock) -> None:
        self.home, self.traps, self.clock = home, traps, clock
        self.calls: list[tuple[Any, ...]] = []
        self.channels: dict[str, dict[str, Any]] = {
            "after-dark": {"slug": "after-dark", "display_name": "After Dark", "status": "active",
                           "niche": "horror", "language": "en",
                           "voice": {"mode": "clone", "name": "owner-voice"}}}  # fmt: skip
        self.registered, self.up, self.studio_rc = False, False, 0
        self.procs: dict[int, FakeProc] = {}
        self.doctor_zip_bad = False

    # --- the dashboard
    def api(self, method: str, path: str, body: Any = None, timeout: float = 60) -> tuple[int | None, Any]:
        self.calls.append(("api", method, path, body))
        self.traps.hit(f"api {method} {path}")
        if path == "/api/health":
            return (200, {"ok": True}) if self.up else (None, None)
        if not self.up:
            return None, None
        if path == "/api/status":
            return 200, {"schedule_time": "03:00", "disk": {"home": str(self.home)}}
        if path == "/api/settings" and method == "GET":
            return 200, {"schedule_registered": self.registered}
        if path == "/api/settings":
            self.registered = bool(body["automatic_runs"])
            self.traps.hit("settings applied")
            return 200, {"schedule_registered": self.registered, "schedule_result": {"applied": True}}
        if path == "/api/channels" and method == "GET":
            return 200, list(self.channels.values())
        if path == "/api/channels":
            if body["slug"] in self.channels:
                return 409, {"error": {"code": "channel_exists"}}
            self.channels[body["slug"]] = dict(body)
            return 201, {"profile": body}
        m = re.fullmatch(r"/api/channels/([\w-]+)(?:/(pause|resume))?", path)
        if m and m.group(1) in self.channels:
            if m.group(2):
                self.channels[m.group(1)]["status"] = "paused" if m.group(2) == "pause" else "active"
            return 200, {"profile": self.channels[m.group(1)]}
        if path == "/api/engine/stop_all":
            self.up = False
            return 200, {}
        return 404, {"error": {"code": "not_found"}}

    def start_engine(self, timeout: float) -> tuple[bool, str]:
        self.calls.append(("start_engine",))
        self.up = True
        return True, "engine + dashboard up (headless)"

    # --- ns, studio, the proof runner
    def cli(self, program: str, args: list[str], timeout: float) -> tuple[int | None, str]:
        self.calls.append(("cli", program, *args))
        self.traps.hit(f"{program} {' '.join(args[:2])}")
        if args[:2] == ["doctor", "--bundle"]:
            out = Path(args[args.index("--out-dir") + 1]) / "nightshift-diagnostics.zip"
            if self.doctor_zip_bad:
                out.write_bytes(b"not a zip")
            else:
                with zipfile.ZipFile(out, "w") as z:
                    line = f'{{"home": "{self.home.as_posix()}", "key": "{PEXELS}"}}\n'
                    z.writestr("logs/supervisor.jsonl", line)
            return 0, f"Diagnostics written to {out}"
        if args[:2] == ["schedule", "show"]:
            return 0, "Nightshift Daily: Ready, next 03:00, last result 0x41303 (the task has not run)"
        return 0, "[ok] everything"

    def prove(self, args: list[str], timeout: float) -> tuple[int | None, str]:
        self.calls.append(("prove", *args))
        self.traps.hit("prove")
        return 0, "PASS    f-c1-status-events [F-C1] 3.1s\n1 passed, 0 failed, 0 skipped; evidence in x"

    def start_studio(self, args: list[str], log: Path) -> FakeProc:
        self.calls.append(("studio", *args))
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write("studio started\n")
        if "--resume" in args:
            job_id = args[args.index("--resume") + 1]
        else:
            job_id = f"gpu-kit-20261112-{len(self.calls):04x}"
            job = self.home / "data" / "jobs" / job_id / "job.json"
            job.parent.mkdir(parents=True)
            job.write_text(json.dumps({"job_id": job_id, "channel": "gpu-kit", "fmt": "master_first",
                                       "created_at": self.clock.now().isoformat()}), "utf-8")  # fmt: skip
        proc = FakeProc(self, 90000 + len(self.calls), job_id)
        self.procs[proc.pid] = proc
        return proc

    def alive(self, pid: int, created: float | None) -> bool:
        return pid in self.procs and self.procs[pid].rc is None

    def created(self, pid: int) -> float | None:
        return 1.0

    def frame_grid(self, video: Path, out: Path, timeout: float) -> str | None:
        out.write_bytes(b"\x89PNG grid")
        return None

    def night_now(self, limit_s: int, timeout: float) -> tuple[int | None, str]:
        self.calls.append(("night_now", limit_s))
        now = self.clock.now()
        self.night(now, now + timedelta(minutes=15))
        return 0, "night ran"

    # --- what the product writes
    def ledger(self, **line: Any) -> None:
        f = self.home / "data" / "ledger" / f"ledger-{self.clock.now():%Y%m}.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": self.clock.now().isoformat(), **line}) + "\n")

    def sql(self, statement: str, params: tuple[Any, ...]) -> None:
        con = sqlite3.connect(self.home / "data" / "studio.db")
        try:
            con.execute(statement, params)
            con.commit()
        finally:
            con.close()

    def finish_job(self, job_id: str) -> None:
        video = self.home / "outbox" / "gpu-kit" / "2026-11-12" / "The House That Waited.mp4"
        video.parent.mkdir(parents=True, exist_ok=True)
        video.write_bytes(b"mp4")
        video.with_suffix(".txt").write_text("TITLE: The House That Waited\n", encoding="utf-8")
        video.with_suffix(".json").write_text('{"title": "The House That Waited"}', encoding="utf-8")
        qc = self.home / "data" / "jobs" / job_id / "qc" / "qc.json"
        qc.parent.mkdir(parents=True, exist_ok=True)
        qc.write_text('{"audio": {"lufs": -14.2}}', "utf-8")
        self.sql("insert into outputs(output_id, job_id, channel, kind, title, path, duration_s, created_at) "
                 "values (?,?,?,?,?,?,?,?)", (f"{job_id}/long", job_id, "gpu-kit", "long",
                                              "The House That Waited", str(video), 760.0,
                                              self.clock.now().isoformat()))  # fmt: skip
        for stage in ("story", "voice", "render"):
            self.ledger(kind="stage", job=job_id, stage=stage, seconds=1200, outcome="done")
        self.ledger(kind="job_end", job=job_id, plan="master_first", seconds=3600, outcome="done")

    def night(self, started: datetime, finished: datetime | None) -> str:
        run_id = f"daily-{started:%Y%m%d}-{len(self.calls):04x}"
        summary = {"run_id": run_id, "jobs": [{"job_id": "after-dark-1"}], "failures": []}
        self.sql("insert into runs(run_id, started_at, finished_at, mode, summary_json) values (?,?,?,?,?)",
                 (run_id, started.isoformat(timespec="seconds"),
                  finished.isoformat(timespec="seconds") if finished else None, "daily", json.dumps(summary)))
        (self.home / "logs" / f"nightly-{started:%Y%m%d}.json").write_text(json.dumps(summary), "utf-8")
        (self.home / "logs" / f"studio-{started:%Y%m%d}.jsonl").write_text('{"event": "daily"}\n', "utf-8")
        (self.home / "run").mkdir(exist_ok=True)
        (self.home / "run" / "morning_note.json").write_text('[{"message": "Last night: 1 video"}]', "utf-8")
        self.ledger(kind="night_start", run=run_id, limit=21600)
        return run_id

    # --- what a test counts
    def count(self, *prefix: Any) -> int:
        return sum(1 for c in self.calls if c[: len(prefix)] == prefix)


class CannedWindows:
    """nvidia-smi as the RTX 5060 Ti recording says, and canned disk, powercfg and System log answers."""

    def __init__(self, traps: Traps, *, driver: bool = True) -> None:
        self.traps, self.driver = traps, driver

    def nvidia_smi(self, args: list[str]) -> str | None:
        self.traps.hit("nvidia_smi")
        if not self.driver:
            return None
        if not args:
            return SMI_TABLE
        if args[0].startswith("--query-gpu"):
            return "NVIDIA GeForce RTX 5060 Ti, 580.88, 16311 MiB, 812 MiB, 15499 MiB\n"
        return ""

    def physical_disk(self, drive: str) -> str | None:
        return '{"FriendlyName":"Samsung SSD 990","MediaType":"SSD","BusType":"NVMe"}'

    def powercfg(self, args: list[str]) -> str | None:
        return "Standby (S3)\nHibernate\n" if args == ["/a"] else "Allow wake timers: Enable\n"

    def events(self, since: datetime, until: datetime) -> str | None:
        return "Event[0]: Log Name: System Event ID: 42 Description: The system is entering sleep.\n"


class Rig:
    """One home, its stand-ins, and `run()`: one double-click of GPU-KIT.cmd (fresh stand-in objects keep the
    product's state, as the PC does between two runs)."""

    def __init__(self, tmp_path: Path, *, test_install: bool = False) -> None:
        self.home = make_home(tmp_path, test_install=test_install)
        self.traps, self.clock = Traps(), Clock()
        self.product = FakeProduct(self.home, self.traps, self.clock)
        self.windows = CannedWindows(self.traps)
        self.answers: list[str] = []
        self.asked: list[str] = []
        self.out: list[str] = []
        self.say_trap: str | None = None

    def ask(self, prompt: str) -> str:
        self.asked.append(prompt)
        if prompt.startswith(("Open the kit's video", "Paste check")):
            self.traps.hit("ask")
        return self.answers.pop(0) if self.answers and not prompt.startswith("Press Enter") else (
            "" if prompt.startswith("Press Enter") else "later")  # fmt: skip

    def say(self, line: str) -> None:
        self.out.append(line)
        if self.say_trap and line.startswith(self.say_trap):
            self.say_trap = None
            raise KeyboardInterrupt(line)

    def run(self, *extra: str) -> int:
        kit_here()
        self.out = []
        env = gpukit.KitEnv(clock=self.clock, product=self.product, windows=self.windows, ask=self.ask,
                            say=self.say)  # fmt: skip
        return gpukit.main(["--home", str(self.home), *extra], env=env)

    @property
    def ev(self) -> Path:
        return self.home / "kit" / VERSION

    def state(self) -> dict[str, Any]:
        return json.loads((self.ev / "kit-state.json").read_text(encoding="utf-8"))

    def results(self) -> dict[str, str]:
        return {k: v["result"] for k, v in self.state()["steps"].items()}

    def zips(self) -> list[Path]:
        return sorted((self.home / "kit").glob("*.zip"))

    def nights_pass(self) -> None:
        """Both nights ran and were read: the clock after night 2's run time + 6 h."""
        self.product.night(local(2026, 11, 13, 3, 0), local(2026, 11, 13, 6, 40))
        self.product.night(local(2026, 11, 14, 3, 0), local(2026, 11, 14, 7, 5))
        self.clock.t = datetime(2026, 11, 14, 9, 30).astimezone()


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


# ------------------------------------------------------------------------------------------------ 1
def test_a_first_run_goes_k0_to_k4_and_stops_with_the_morning_date(rig: Rig) -> None:
    assert rig.run() == 0
    assert rig.results() == {"K0": "PASS", "K1": "PASS", "K2": "PASS", "K3": "PASS", "K4": "PASS",
                             "K5": "WAIT", "K6": "WAIT", "K7": "WAIT"}  # fmt: skip
    steps = rig.state()["steps"]
    assert all(steps[k]["done"] for k in ("K0", "K1", "K2", "K3", "K4"))
    assert not any(steps[k]["done"] for k in ("K5", "K6", "K7"))
    # the second run time is Saturday 03:00 (armed Thursday 10:00), + 6 h: the last line names it
    assert rig.out[-1] == "Run GPU-KIT.cmd again after Saturday 14 November 09:00."
    assert "nightshift-gpu-kit" in rig.out[-2] and "do not send it yet" in rig.out[-2]
    assert any("Friday 13 November 03:00 and Saturday 14 November 03:00" in line for line in rig.out)
    machine = json.loads((rig.ev / "machine.json").read_text(encoding="utf-8"))
    assert machine["gpu"] == {"name": "NVIDIA GeForce RTX 5060 Ti", "driver": "580.88", "cuda": "13.0",
                              "vram_total": "16311 MiB", "vram_used": "812 MiB"}  # fmt: skip
    assert machine["simulated"] is None and "Standby (S3)" in machine["power"]["sleep_states"]
    assert rig.product.count("cli", "ns", "verify") == 2  # --downloads (models whole), then each model once


# ------------------------------------------------------------------------------------------------ 2
INTERRUPTS = {  # step -> how the window is closed inside it (a trap) and in which run
    "K0": ("trap", "api GET /api/status"),
    "K1": ("trap", "nvidia_smi"),
    "K2": ("trap", "prove"),
    "K3": ("trap", "studio running"),
    "K4": ("trap", "settings applied"),
    "K5": ("say", "Step 6 of 8 (night 1): starting."),
    "K6": ("say", "Step 7 of 8 (night 2): starting."),
    "K7": ("trap", "ns doctor --bundle"),
    "K8": ("trap", "ask"),
}


@pytest.mark.parametrize("step", list(INTERRUPTS))
def test_each_step_interrupted_resumes_on_the_next_run(rig: Rig, step: str) -> None:
    how, where = INTERRUPTS[step]
    late = step in ("K5", "K6", "K7")
    if how == "say":
        rig.say_trap = where
    else:
        rig.traps.armed.add(where)
    if not late:
        assert rig.run() == 130, rig.out
        assert any(line.startswith("Stopped by you") for line in rig.out)
    assert rig.run() == 0, rig.out
    assert rig.results()["K4"] == "PASS"
    rig.nights_pass()
    if late:
        assert rig.run() == 130, rig.out
        done_before = {k for k, v in rig.state()["steps"].items() if v.get("done")}
        assert step not in done_before and "K4" in done_before
    assert rig.run() == 0, rig.out
    assert set(rig.results().values()) == {"PASS"}
    # done once, whatever stopped it: one kit channel, one story job, one settings change each way, one zip
    assert rig.product.count("api", "POST", "/api/channels") == 1
    assert rig.product.count("studio", "run", "--channel") == 1
    assert rig.product.count("studio", "run", "--resume") == (1 if step == "K3" else 0)
    puts = [c[3] for c in rig.product.calls if c[:3] == ("api", "PUT", "/api/settings")]
    assert puts == [{"automatic_runs": True}, {"automatic_runs": False}]
    assert len(rig.zips()) == 1 and rig.out[-1].startswith("Send this one file: ")
    assert rig.product.channels["gpu-kit"]["status"] == "paused" and rig.product.registered is False
    assert not (rig.ev / "kit.lock").exists()


# ------------------------------------------------------------------------------------------------ 3
def test_the_story_job_is_resumed_not_made_again(rig: Rig) -> None:
    rig.traps.armed.add("studio running")
    assert rig.run() == 130
    job_id = rig.state()["job"]["id"]
    assert job_id and rig.product.count("studio") == 1
    assert rig.run() == 0
    assert [c[1:] for c in rig.product.calls if c[0] == "studio"] == [
        ("run", "--channel", "gpu-kit", "--count", "1", "--format", "master_first"),
        ("run", "--resume", job_id)]
    story = json.loads((rig.ev / "story" / "story.json").read_text(encoding="utf-8"))
    assert story["outcome"] == "done" and story["lufs"] == -14.2 and story["length_s"] == 760.0
    assert {s["stage"] for s in story["stages"]} == {"story", "voice", "render"}
    assert rig.run() == 0 and rig.product.count("studio") == 2  # a finished one is not touched


def test_a_job_that_finished_while_the_kit_was_closed_is_not_resumed(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.traps.armed.add("studio finished")  # the job's last stage ends, then the window closes
    assert rig.run() == 130
    assert rig.run() == 0
    assert rig.product.count("studio") == 1 and rig.results()["K3"] == "PASS"


def test_a_failed_story_job_is_recorded_with_its_code_and_the_kit_goes_on(rig: Rig) -> None:
    rig.product.studio_rc = 1
    (rig.ev / "story").mkdir(parents=True)
    (rig.ev / "story" / "studio-run.log").write_text("Error NS-E303: the story failed its checks\n", "utf-8")
    assert rig.run() == 1
    assert rig.results()["K3"] == "FAIL" and rig.results()["K4"] == "PASS"
    assert rig.state()["job"]["code"] == "NS-E303"
    assert "Nothing to do now" in rig.state()["steps"]["K3"]["line"]
    assert rig.out[-1] == "Run GPU-KIT.cmd again after Saturday 14 November 09:00."  # not a stop
    assert rig.product.channels["gpu-kit"]["status"] == "paused"
    assert rig.run() == 0 and rig.product.count("studio") == 1  # not made again


# ------------------------------------------------------------------------------------------------ 4
class LiveDashboard(FakeProduct):
    """The REAL dashboard routes of the fake-services home (loopback HTTP); the rest recorded."""

    def __init__(self, ph: ProofHome, traps: Traps, clock: Clock) -> None:
        super().__init__(ph.home, traps, clock)
        self.http = gpukit.InstallProduct(ph.home, ph.server.port)
        self.up = True

    def api(self, method: str, path: str, body: Any = None, timeout: float = 60) -> tuple[int | None, Any]:
        self.calls.append(("api", method, path, body))
        return self.http.api(method, path, body, timeout)


def test_the_kit_channel_is_paused_and_never_planned_by_a_night(proof_home: ProofHome) -> None:
    kit_here()
    from ns_studio.channels import cadence

    clock, traps = Clock(), Traps()
    product = LiveDashboard(proof_home, traps, clock)
    env = gpukit.KitEnv(clock=clock, product=product, windows=CannedWindows(traps), ask=lambda p: "",
                        say=print)  # fmt: skip
    args = gpukit.build_parser().parse_args(["--home", str(proof_home.home), "--dry-run"])
    kit = gpukit.start(args, env)
    try:
        assert kit.channel() is None  # made through POST /api/channels, from the first active channel
        _st, mine = product.http.api("GET", "/api/channels/gpu-kit")
        _st, owners = product.http.api("GET", "/api/channels/after-dark")
        assert mine["profile"]["status"] == "active" and mine["profile"]["format"] == "master_first"
        assert mine["profile"]["niche"] == owners["profile"]["niche"]
        assert mine["profile"]["voice"] == owners["profile"]["voice"]
        planned = {j.channel.slug for j in cadence.plan_daily(proof_home.paths, Db(proof_home.paths.db_path))}
        assert "gpu-kit" in planned  # active, as while its video is made
        # killed between the job's end and the pause: the next run pauses it first
        kit.state["job"] = {"id": "gpu-kit-x", "outcome": "failed", "code": "NS-E303",
                            "started": START.isoformat()}
        res = kit.k3()
        assert res.result == "FAIL" and "stopped (NS-E303)" in res.line and res.done
        status, body = product.http.api("GET", "/api/channels/gpu-kit")
        assert status == 200 and body["profile"]["status"] == "paused"
        planned = {j.channel.slug for j in cadence.plan_daily(proof_home.paths, Db(proof_home.paths.db_path))}
        assert "gpu-kit" not in planned
        assert product.count("api", "POST", "/api/channels/gpu-kit/pause") == 1
        assert kit.k3().done  # run twice: paused once
        assert product.count("api", "POST", "/api/channels/gpu-kit/pause") == 1
    finally:
        kit.close()


# ------------------------------------------------------------------------------------------------ 5
def test_automatic_runs_are_put_back_as_they_were(tmp_path: Path) -> None:
    off = Rig(tmp_path / "off")
    assert off.run() == 0 and off.product.registered is True
    off.nights_pass()
    assert off.run() == 0 and off.product.registered is False
    assert "Automatic runs are off again, as they were before the kit" in off.state()["steps"]["K7"]["line"]

    on = Rig(tmp_path / "on")
    on.product.registered = True
    assert on.run() == 0
    on.nights_pass()
    assert on.run() == 0 and on.product.registered is True
    assert on.product.count("api", "PUT", "/api/settings") == 0
    assert "left as you had them" in on.state()["steps"]["K7"]["line"]

    killed = Rig(tmp_path / "killed")  # the PUT went through; the window closed before the state was saved
    killed.traps.armed.add("settings applied")
    assert killed.run() == 130 and killed.state()["automatic_runs_before"] is False
    assert killed.run() == 0
    killed.nights_pass()
    assert killed.run() == 0 and killed.product.registered is False
    puts = [c[3] for c in killed.product.calls if c[:3] == ("api", "PUT", "/api/settings")]
    assert puts == [{"automatic_runs": True}, {"automatic_runs": False}]


# ------------------------------------------------------------------------------------------------ 6
@pytest.mark.parametrize(("start", "starts"), [
    (datetime(2026, 11, 12, 23, 30, 0), True),  # 3 h estimate + 30 min ends exactly at 03:00: it starts
    (datetime(2026, 11, 12, 23, 30, 1), False),  # one second later it would end after the run time
])  # fmt: skip
def test_too_close_to_the_run_time_the_video_waits(tmp_path: Path, start: datetime, starts: bool) -> None:
    rig = Rig(tmp_path)
    rig.clock.t = start.astimezone()
    assert rig.run() == 0
    assert (rig.product.count("studio") == 1) is starts
    if not starts:
        line = rig.state()["steps"]["K3"]["line"]
        assert rig.results()["K3"] == "WAIT" and not rig.state()["steps"]["K3"]["done"]
        assert "about 3.0 h here plus 30 minutes" in line and "Friday 13 November 03:00" in line
        assert rig.out[-1] == "Run GPU-KIT.cmd again after Friday 13 November 09:00."
        assert rig.results()["K4"] == "WAIT" and rig.product.registered is False


# ------------------------------------------------------------------------------------------------ 7
def test_the_nights_are_read_from_the_runs_rows_and_the_ledger(rig: Rig) -> None:
    assert rig.run() == 0
    rig.product.night(datetime(2026, 11, 13, 3, 0).astimezone(), datetime(2026, 11, 13, 6, 40).astimezone())
    rig.clock.t = datetime(2026, 11, 13, 12, 0).astimezone()  # a third run before night 2 ended: it waits
    assert rig.run() == 0
    assert rig.results()["K5"] == "PASS" and rig.results()["K6"] == "WAIT"
    assert "Night 1: ran 03:00 to 06:40, 1 job(s) done" in rig.state()["steps"]["K5"]["line"]
    assert rig.out[-1] == "Run GPU-KIT.cmd again after Saturday 14 November 09:00."
    rig.clock.t = datetime(2026, 11, 14, 9, 1).astimezone()  # night 2 has no row after its run time + 6 h
    assert rig.run() == 0
    assert "Night 2: did not run" in rig.state()["steps"]["K6"]["line"]
    assert rig.state()["nights"][1]["schedule"][1].startswith("Nightshift Daily: Ready")
    with zipfile.ZipFile(rig.zips()[0]) as z:
        names = set(z.namelist())
        assert {"logs/nightly-20261113.json", "logs/studio-20261113.jsonl", "run/morning_note.json"} <= names
        ledger = z.read("ledger/ledger-202611.jsonl").decode("utf-8")
        assert '"kind": "night_start"' in ledger and '"kind": "job_end"' in ledger


# ------------------------------------------------------------------------------------------------ 8
def test_the_collected_folder_and_zip_hold_every_part(rig: Rig) -> None:
    old = rig.home / "kit" / "0.1.15-rc3" / "kit-state.json"  # written by an earlier candidate's kit
    old.parent.mkdir(parents=True)
    old.write_text('{"version": "0.1.15-rc3"}', encoding="utf-8")
    rig.product.doctor_zip_bad = True  # one bad file among good ones
    assert rig.run() == 0
    rig.nights_pass()
    assert rig.run() == 0
    z_path = rig.zips()[0]
    assert z_path.name == f"nightshift-gpu-kit-{VERSION}-20261112.zip" and z_path.parent == rig.home / "kit"
    size = f"{z_path.stat().st_size / 1e6:.1f} MB"
    assert rig.out[-1] == f"Send this one file: {z_path} ({size}). The kit sent nothing anywhere."
    with zipfile.ZipFile(z_path) as z:
        names = set(z.namelist())
        readme = z.read("README.txt").decode("utf-8")
        missing = z.read("MISSING.txt").decode("utf-8")
    for part in ("machine.json", "verify.json", "proofs.json", "story/story.json", "story/frames.png",
                 "story/sheet.txt", "story/sheet.json", "story/studio-run.log", "ledger/ledger-202611.jsonl",
                 "logs/nightly-20261113.json", "logs/nightly-20261114.json", "run/morning_note.json",
                 "collected/windows-events-night1.txt", "collected/windows-events-night2.txt",
                 "collected/doctor-bundle.txt", "kit-state.json", "watch.json", "paste.json"):  # fmt: skip
        assert part in names or part.split("/")[-1].split(".")[0] in missing, part
    assert "run/vram-need.json: no model load was measured yet" in missing
    assert "collected/nightshift-diagnostics.zip: " in missing  # the bad zip is named, the rest is there
    assert readme.splitlines()[0] == "Real graphics card: the GPU PC's own readings."
    assert "K7 collect everything and put Automatic runs back: PASS" in readme
    assert json.loads(old.read_text(encoding="utf-8")) == {"version": "0.1.15-rc3"}  # left alone
    assert not [p for p in (rig.home / "kit").iterdir() if p.name.endswith(".tmp")]


def test_a_failing_step_still_yields_the_bundle(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.windows.driver = False  # nvidia-smi does not answer: a cheap failure first
    assert rig.run() == 1
    assert rig.results() == {"K0": "PASS", "K1": "FAIL", "K2": "NOT RUN", "K3": "NOT RUN", "K4": "NOT RUN",
                             "K5": "NOT RUN", "K6": "NOT RUN", "K7": "NOT RUN"}  # fmt: skip
    assert "install the newest NVIDIA driver" in rig.state()["steps"]["K1"]["line"]
    assert rig.product.count("studio") == 0 and rig.product.count("api", "PUT") == 0
    with zipfile.ZipFile(rig.zips()[0]) as z:
        assert "machine.json" in z.namelist() and "K1 record this PC: FAIL" in z.read("README.txt").decode()
    assert rig.out[-1].startswith("The zip so far: ") and "Do what the FAIL line above says" in rig.out[-1]
    rig.windows.driver = True
    assert rig.run() == 0 and rig.results()["K1"] == "PASS"  # redone whole on the next run


# ------------------------------------------------------------------------------------------------ 9
_CONNECTS: list[Any] = []
_RECORD = [False]


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if _RECORD[0] and event == "socket.connect":
        _CONNECTS.append(args[1])


sys.addaudithook(_audit)  # (records only while a test of this file switches it on)


def test_the_kit_makes_no_network_request(proof_home: ProofHome) -> None:
    kit_here()
    clock, traps = Clock(), Traps()
    product = LiveDashboard(proof_home, traps, clock)
    env = gpukit.KitEnv(clock=clock, product=product, windows=CannedWindows(traps), ask=lambda p: "",
                        say=print)  # fmt: skip
    _CONNECTS.clear()
    _RECORD[0] = True
    try:
        assert gpukit.main(["--home", str(proof_home.home), "--dry-run", "--night-now"], env=env) in (0, 1)
    finally:
        _RECORD[0] = False
    assert _CONNECTS, "the kit asked the dashboard nothing: the test sees nothing"
    assert {a[0] for a in _CONNECTS if isinstance(a, tuple)} == {"127.0.0.1"}
    # the source: the only HTTP client is the loopback dashboard; no other address appears
    for f in sorted((ROOT / "dev" / "kit").rglob("*")):
        if f.suffix not in (".py", ".ps1"):
            continue
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"https://|\brequests\b|httpx|http\.client|Invoke-WebRequest|Invoke-RestMethod|"
                             r"WebClient|socket\.", text), f.name  # fmt: skip
        assert set(re.findall(r"http://[\w.]+", text)) <= {"http://127.0.0.1"}, f.name
        lines = text.splitlines()
        uses = [i for i, line in enumerate(lines) if "urllib" in line]
        if f.name != "gpukit.py":
            assert not uses, f.name
            continue
        real = lines.index("class InstallProduct:")
        api = next(i for i in range(real, len(lines)) if lines[i].startswith("    def api(self, method: str"))
        end = next(i for i in range(api + 1, len(lines)) if lines[i].startswith("    def "))
        assert uses and all(api < i < end for i in uses), "urllib outside InstallProduct.api"
        assert 'f"http://127.0.0.1:{self.port}{path}"' in "\n".join(lines[api:end])


# ------------------------------------------------------------------------------------------------ 10
def test_a_second_kit_at_the_same_time_is_refused(rig: Rig) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        rig.ev.mkdir(parents=True)
        lock = rig.ev / "kit.lock"
        now = datetime.now().astimezone().isoformat()
        lock.write_text(f"kit | GPU kit pid {child.pid} | {now}\n", "ascii")
        assert rig.run() == 2
        assert rig.out[-1].startswith(f"REFUSED Another GPU kit is running (pid {child.pid}).")
        assert lock.read_text(encoding="ascii").startswith(f"kit | GPU kit pid {child.pid}")
        assert not (rig.ev / "kit-state.json").exists()
        assert rig.product.calls[-1][:3] == ("api", "GET", "/api/status")
    finally:
        child.kill()
        child.wait(30)
    assert rig.run() == 0  # its kit is gone: the lock is taken over


# ------------------------------------------------------------------------------------------------ 11
def test_the_paste_answers_are_recorded_and_later_keeps_it_open(rig: Rig) -> None:
    rig.answers = ["y", "y", "y",  # the three watch questions
                   "y", "n Studio cut the description at 4,900 characters", "later", "y", "y",
                   "Altered content", "101/100, Save refused", "later", "later", "later"]  # fmt: skip
    assert rig.run() == 0
    paste = json.loads((rig.ev / "paste.json").read_text(encoding="utf-8"))
    assert [paste[f"q{n}"]["answer"] for n in range(1, 6)] == ["y", "n", "later", "y", "y"]
    assert paste["q2"]["note"] == "Studio cut the description at 4,900 characters"
    assert paste["extra"]["field_name"] == "Altered content" and paste["extra"]["E2"] == "later"
    rig.asked.clear()
    rig.answers = ["y", "130/500", "", ""]
    assert rig.run() == 0
    asked = [a for a in rig.asked if not a.startswith("Press Enter")]
    assert asked[0].startswith("Were the tags accepted") and len(asked) == 4  # only what is still open
    assert [a[:2] for a in asked[1:]] == ["E2", "E3", "E4"]
    paste = json.loads((rig.ev / "paste.json").read_text(encoding="utf-8"))
    assert paste["q3"]["answer"] == "y" and paste["q1"]["answer"] == "y" and paste["extra"]["E2"] == "130/500"


# ------------------------------------------------------------------------------------------------ 14
@pytest.mark.parametrize("switch", [["--night-now"], ["--clock", "2026-11-12T22:00"], ["--dry-run"]])
def test_the_dry_run_switches_work_only_on_a_test_install(tmp_path: Path, switch: list[str]) -> None:
    real = Rig(tmp_path / "real")
    assert real.run(*switch) == 2 and real.out[-1].startswith("REFUSED ")
    assert not (real.home / "kit").exists() and real.product.calls == []
    test = Rig(tmp_path / "test", test_install=True)
    assert test.run() == 2 and "is not a GPU install" in test.out[-1]
    assert test.run("--dry-run", *[a for a in switch if a != "--dry-run"]) == 0, test.out


def test_a_dry_run_on_the_simulated_card_is_marked_and_runs_both_nights_at_once(tmp_path: Path) -> None:
    rig = Rig(tmp_path, test_install=True)
    assert rig.run("--dry-run", "--night-now") == 0, rig.out
    assert set(rig.results().values()) == {"PASS"}
    assert rig.product.count("night_now") == 2 and rig.product.count("api", "PUT") == 0  # no task is armed
    assert rig.out[1] == "SIMULATED graphics card (simulated (fake_gpu)): a test install; nothing measured " \
        "here is evidence of the GPU PC."  # fmt: skip
    with zipfile.ZipFile(rig.zips()[0]) as z:
        assert z.read("README.txt").decode().startswith("SIMULATED GRAPHICS CARD (simulated (fake_gpu))")
        assert json.loads(z.read("machine.json"))["simulated"] == "simulated (fake_gpu)"


# --------------------------------------------------------------------------------------------- the scrubber
def test_the_scrubber_masks_keys_tokens_the_user_the_home_and_the_pc() -> None:
    kit_here()
    masks = gpukit.Masks(home=r"D:\Nightshift", user="Ahmed", profile=r"C:\Users\Ahmed", computer="AHMED-PC")
    text = "\n".join([
        f"pexels {PEXELS} pixabay 12345678-0123456789abcdef0123456789abcdef",
        '{"pexels_api_key": "secret-value", "X-NS-Token": "abc123", "keys": {"pexels": true}}',
        "GET https://example.invalid/api?key=abcdef&q=rain Authorization: Bearer abcdefghijklmnop",
        "token ghp_" + "x" * 36,
        r"home D:\Nightshift\logs and D:\\Nightshift\\data and d:/nightshift/run",
        r"profile C:\Users\Ahmed\Desktop, user Ahmed on AHMED-PC (ahmed)",
        "sha256 " + "f" * 64,
    ])  # fmt: skip
    out = gpukit.scrub_text(text, masks)
    for gone in (PEXELS, "12345678-0123", "secret-value", "abc123", "abcdef&", "abcdefghijklmnop", "ghp_",
                 "Nightshift", "nightshift", "Users", "Ahmed", "ahmed", "AHMED-PC"):  # fmt: skip
        assert gone not in out, gone
    assert out.count("<home>") == 3 and "<profile>\\Desktop" in out and "user <user> on <pc> (<user>)" in out
    assert '"keys": {"pexels": true}' in out and "f" * 64 in out  # not a secret: kept
    assert len(gpukit.MASKED) == 5


def test_a_nested_zip_is_scrubbed_entry_by_entry(tmp_path: Path) -> None:
    kit_here()
    src = tmp_path / "diag.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr(r"logs/Ahmed.jsonl", '{"path": "C:/Users/Ahmed/x", "token": "t0k3n"}')
        z.writestr("frames.png", b"\x89PNG Ahmed")
    masks = gpukit.Masks(home=r"D:\Nightshift", user="Ahmed", profile=r"C:\Users\Ahmed", computer="")
    out_path = tmp_path / "out.zip"
    with zipfile.ZipFile(out_path, "w") as out:
        gpukit.scrub_zip(src, out, "collected/diag/", masks)
    with zipfile.ZipFile(out_path) as z:
        assert z.namelist() == ["collected/diag/logs/<user>.jsonl", "collected/diag/frames.png"]
        text = z.read("collected/diag/logs/<user>.jsonl").decode()
        assert text == '{"path": "<profile>/x", "token": "***"}'
        assert z.read("collected/diag/frames.png") == b"\x89PNG Ahmed"  # an image is copied as it is


def test_the_real_windows_readers_are_refused_under_pytest(tmp_path: Path) -> None:
    kit_here()
    from ns_common.testguard import RealMachineUnderTest

    real = gpukit.RealWindows(tmp_path)
    for call in (lambda: real.nvidia_smi([]), lambda: real.physical_disk("D:"), lambda: real.powercfg(["/a"]),
                 lambda: real.events(START, START)):  # fmt: skip
        with pytest.raises(RealMachineUnderTest):
            call()
