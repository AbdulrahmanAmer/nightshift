"""Card gpu-kit: the one-command GPU kit (`dev/kit/gpukit.py`), run end to end on a fake home.

No fixture the kit reads is written by hand (review of 4b58eda, blocker 1). The home is made by the
product's own code; the kit talks to the REAL dashboard app (loopback HTTP, fake services, the real Settings
switch over the faithful Task Scheduler fake of tests/test_nightly_optional.py); `studio run` / `--resume`,
`ns schedule show` and `ns doctor` are the real CLIs in this process, `ns doctor --bundle` is the product's
`doctor.build_bundle`; the story job runs the product's stage runner over three stub stages that write
through the product's writers (the ledger lines come from the runner itself, the QC files from `QcResult`,
`loudness_check` and `summary_entry`, the outputs row and sheet from `outbox.record_output` and
`write_sheet_txt`); the nights are the product's `daily.run_daily`; `ns verify`'s report is
`verify.write_report`; nvidia-smi is the product's simulated card (`gpu.FakeSmi`) in the GPU PC's recorded
state; powercfg, Get-PhysicalDisk and wevtutil answers are captured outputs (tests/fixtures/kit, captured
on the authoring laptop, Windows 11 build 26200, PC and user names and the account id replaced). Only the
proof runner's summary line and `ns verify --downloads`' exit code are stand-ins (the kit reads nothing
else of them). The real Windows readers refuse to run under pytest.
"""
# ruff: noqa: F811  (a fixture imported by name is redefined by the test argument that asks for it)

from __future__ import annotations

import json
import random
import re
import signal
import socket
import subprocess
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from ns_common.config import save_runtime
from ns_common.db import Db
from ns_common.errors import NsError
from ns_common.models import GateCheck, GateReport, Metadata, QcResult, Runtime
from ns_common.paths import Paths
from ns_dashboard import state as dash_state
from ns_dashboard.adapters import RealScheduleAdapter
from ns_dashboard.fakes import FakeSupervisor
from ns_dashboard.handlers import build_state
from ns_dashboard.server import create_app
from ns_dashboard.supervisor_link import SupervisorLink
from ns_studio import daily, jobs
from ns_studio import orchestrator as orch
from ns_studio.channels import cadence, crud
from ns_studio.context import FAKEABLE
from ns_studio.media import outbox, qc
from ns_studio.media.audio import Loudness
from ns_studio.stages import REGISTRY
from ns_supervisor import gpu, pwsh, verify
from ns_supervisor.cli import app as ns_app
from tests.helpers import make_channel
from tests.proofhome import _Server
from tests.test_nightly_optional import FakeWindows

if TYPE_CHECKING:
    from dev.kit import gpukit
    from ns_supervisor import kitnights
else:
    try:
        from dev.kit import gpukit
        from ns_supervisor import kitnights
    except ImportError:  # a base without the kit: every test then fails in its body, at `kit_here()`
        gpukit = kitnights = None


def kit_here() -> None:
    assert gpukit is not None, "dev/kit/gpukit.py does not exist: there is no GPU kit"


ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
CAPTURED = ROOT / "tests" / "fixtures" / "kit"  # real outputs of this PC's tools (see the module doc)


def local(*parts: int) -> datetime:
    return datetime(*parts).astimezone()  # type: ignore[arg-type]


START = local(2026, 11, 12, 10, 0)  # a Thursday morning; the nightly run is at 03:00
PEXELS = "A" * 20 + "b" * 20 + "7" * 16  # 56 letters and digits: the shape of a Pexels key
HF = "hf_" + "Q" * 34
STUBS = ("kit_story", "kit_qc", "kit_outbox")


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


class NightClock:
    """`nightclock.Clock` for the product's `run_daily`: UTC wall time and a monotonic counter."""

    def __init__(self, at: datetime) -> None:
        self.at, self.t = at.astimezone(UTC), 0.0

    def now(self) -> datetime:
        return self.at + timedelta(seconds=self.t)

    def monotonic(self) -> float:
        return self.t


class KitStage:
    """A stub stage of the master plan. The runner around it is the product's (it writes the ledger's
    stage and job_end lines); `kit_qc` writes the QC files and `kit_outbox` the video, its sheet and its
    outputs row through the product's own functions. `world.kill_at` closes the window inside the stage
    (KeyboardInterrupt), `world.fail_at` fails it with NS-E303."""

    def __init__(self, name: str, tenant: str | None, inputs: tuple[str, ...], world: World) -> None:
        self.name, self.tenant, self.inputs, self.outputs, self.world = (
            name,
            tenant,
            inputs,
            (f"{name}.json",),
            world,
        )

    def run(self, ctx: Any) -> None:
        w = self.world
        if w.kill_at == self.name:
            w.kill_at = None
            raise KeyboardInterrupt("the window was closed during " + self.name)
        if w.fail_at == self.name:
            raise NsError("NS-E303", "the story failed its checks")
        ctx.write_json(self.outputs[0], {"stage": self.name})
        if self.name == "kit_qc":
            loud = qc.loudness_check(Loudness(-14.2, -1.4))
            result = QcResult(item="long", passed=True, checks={"loudness": loud}, reason=None)
            ctx.write_json("qc/long.json", result.model_dump(mode="json"))
            ctx.write_json("qc/report.json", {"long": qc.summary_entry(result)})
        if self.name == "kit_outbox":
            video = ctx.paths.outbox / ctx.channel.slug / "2026-11-12" / "The House That Waited.mp4"
            video.parent.mkdir(parents=True, exist_ok=True)
            video.write_bytes(b"mp4")
            meta = Metadata(channel=ctx.channel.slug, job_id=ctx.job_id, kind="long",
                            title="The House That Waited", description="A story.", tags=["horror"],
                            hashtags=["#horror"], publish_after="2026-11-13T18:00:00+00:00",
                            ai_disclosure="no", made_for_kids=False, age_restrict_suggested=False, clips=[],
                            combo_id=ctx.combo.combo_id, prompt_versions={})  # fmt: skip
            video.with_suffix(".json").write_text(meta.model_dump_json(indent=2), encoding="utf-8")
            outbox.write_sheet_txt(meta, video.name, video.with_suffix(".txt"))
            outbox.record_output(ctx, kind="long", k=None, item="long", meta=meta, path=video,
                                 duration_s=760.0, shots=[])  # fmt: skip


class StudioProc:
    """`studio run ...` of the install: the real studio CLI in this process, run at the kit's first look."""

    def __init__(self, product: LocalProduct, pid: int, args: list[str], log: Path) -> None:
        self.product, self.pid, self.args, self.log, self.rc = product, pid, args, log, None

    def poll(self) -> int | None:
        if self.rc is not None:
            return self.rc
        w = self.product.world
        closing = "studio running" in w.traps.armed  # the window closes in the middle of the job: both go
        if closing:
            w.traps.armed.discard("studio running")
            w.kill_at = "kit_qc"
        try:
            res = CliRunner().invoke(_studio_app(), [*self.args, "--fake-services"])
        except KeyboardInterrupt:
            res = None
        if closing or res is None:  # (the studio's own Ctrl+C handling ends it; the kit gets the interrupt)
            self.rc = -1
            raise KeyboardInterrupt("the window was closed")
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write(res.output)
        self.rc = res.exit_code
        w.traps.hit("studio finished")
        return self.rc

    def kill(self) -> None:
        self.rc = -9


PROOF_THAT_READS = """
PROOF = {"id": "kit-reads-status", "boxes": ["F-X99"], "install": "any", "window": False, "needs": []}


def run(ctx):
    ctx.check((ctx.home / "config" / "runtime.json").is_file(), "the install's runtime.json is there")
"""


def nothing_runs(script: Path, args: list[str], timeout: float) -> tuple[int, str]:
    return 0, "CLEAN: no process of this install runs"


def no_freeze(paths: Paths, venv: str) -> str | None:
    return None  # (no venv in a test home)


def no_card() -> str | None:
    return None  # (the zip's nvidia-smi.txt: no card in the test home)


def _studio_app() -> Any:
    from ns_studio.cli import app

    return app


class LocalProduct:
    """The install's routes, in this process: the REAL dashboard over loopback, the real `ns` and `studio`
    CLIs; every call recorded. `prove` (its summary line) and `ns verify --downloads` (its exit code) are the
    only stand-ins."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.calls: list[tuple[Any, ...]] = []
        self.procs: dict[int, StudioProc] = {}

    @property
    def http(self) -> Any:
        """The kit's own HTTP client to the dashboard (made when first used: the fixture builds on a base
        without the kit, and the test body then fails at its first assertion)."""
        kit_here()
        return gpukit.InstallProduct(self.world.home, self.world.server.port)

    def api(self, method: str, path: str, body: Any = None, timeout: float = 60) -> tuple[int | None, Any]:
        self.calls.append(("api", method, path, body))
        self.world.traps.hit(f"api {method} {path}")
        out = self.http.api(method, path, body, timeout)
        if method == "PUT" and path == "/api/settings":
            self.world.traps.hit("settings applied")
        return out

    def cli(self, program: str, args: list[str], timeout: float) -> tuple[int | None, str]:
        self.calls.append(("cli", program, *args))
        self.world.traps.hit(f"{program} {' '.join(args[:2])}")
        if program == "ns" and args[:2] == ["verify", "--downloads"]:
            if self.world.real_downloads:  # the product's own checker of the downloads on this home
                return self.real_downloads()
            # The one stand-in: `ns verify --downloads` (installer/stage2/verify_downloads.run) hashes the
            # install's whole model set (about 20 GB) against manifest.json; a test home cannot hold it, so
            # the product's only true answer here is a failure (test_a_home_without_its_models_fails_k1_on_
            # the_products_own_check). The kit reads nothing of it but the exit code.
            return 0, "(stand-in: every downloaded file matches manifest.json)"
        if program == "ns" and args == ["verify"]:  # the report through the product's writer and lines
            report = GateReport(
                slice="verify",
                date="2026-11-12",
                machine={"gpu": "NVIDIA GeForce RTX 5060 Ti"},
                checks=[GateCheck(name="llm.tok_s", passed=True, value=41.0)],
            )
            verify.write_report(self.world.paths, report)
            return 0, "\n".join(verify.format_lines(report))
        if program == "ns" and args[:2] == ["doctor", "--bundle"]:  # the product's zip writer, this home only
            from ns_supervisor import doctor

            empty = self.world.home.parent / "no-setup-folder"
            out = doctor.build_bundle(
                self.world.paths,
                out_dir=Path(args[args.index("--out-dir") + 1]),
                freeze=no_freeze,
                smi=no_card,
                setup_dir=empty,
                temp_dir=empty,
            )
            return 0, f"Diagnostics written to {out}"
        res = CliRunner().invoke(ns_app if program == "ns" else _studio_app(), args)
        return res.exit_code, res.output

    def start_studio(self, args: list[str], log: Path) -> StudioProc:
        self.calls.append(("studio", *args))
        log.parent.mkdir(parents=True, exist_ok=True)
        proc = StudioProc(self, 90000 + len(self.calls), args, log)
        self.procs[proc.pid] = proc
        return proc

    def alive(self, pid: int, created: float | None) -> bool:
        return pid in self.procs and self.procs[pid].rc is None

    def created(self, pid: int) -> float | None:
        return 1.0

    def prove(self, args: list[str], timeout: float) -> tuple[int | None, str]:
        """The REAL proof runner (`dev/prove.py:main`) with the kit's arguments, its lock and runs folder, on
        one proof of its own that only reads (install `any`, no engine). Only the process listing of the
        kit's nsproc.ps1 is answered here (no PowerShell in a unit test)."""
        import contextlib
        import io

        from dev import prove

        self.calls.append(("prove", *args))
        self.world.traps.hit("prove")
        folder = self.world.home.parent / "kitproofs"
        if not folder.is_dir():
            folder.mkdir()
            (folder / "__init__.py").write_text("", encoding="utf-8")
            (folder / "kit_reads_status.py").write_text(PROOF_THAT_READS, encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            rc = prove.main([*args, "--proofs-dir", str(folder)],
                            runner_options={"commit": "test", "ps": nothing_runs})  # fmt: skip
        return rc, out.getvalue()

    def real_downloads(self) -> tuple[int | None, str]:
        from installer.stage2 import verify_downloads

        lines: list[str] = []
        rc = verify_downloads.run(self.world.paths, ROOT, lines.append)
        return rc, "\n".join(lines)

    def start_engine(self, timeout: float) -> tuple[bool, str]:
        self.calls.append(("start_engine",))
        return True, "engine + dashboard up (headless)"

    def night_now(self, limit_s: int, timeout: float) -> tuple[int | None, str]:
        self.calls.append(("night_now", limit_s))
        self.world.night(self.world.clock.now())
        return 0, "night ran"

    def frame_grid(self, video: Path, out: Path, timeout: float) -> str | None:
        out.write_bytes(b"\x89PNG grid")  # (the install's FFmpeg; not on the laptop's test PATH)
        return None

    def count(self, *prefix: Any) -> int:
        return sum(1 for c in self.calls if c[: len(prefix)] == prefix)


class CannedWindows:
    """nvidia-smi: the product's simulated card in the GPU PC's recorded state; disk, powercfg and the System
    log: captured outputs."""

    def __init__(self, world: World) -> None:
        self.world = world

    def nvidia_smi(self, args: list[str]) -> str | None:
        self.world.traps.hit("nvidia_smi")
        try:
            done = gpu.FakeSmi(self.world.paths)(args, 30)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout if done.returncode == 0 else None

    def physical_disk(self, drive: str) -> str | None:
        return (CAPTURED / "physical-disk.json").read_text(encoding="utf-8")

    def powercfg(self, args: list[str]) -> str | None:
        name = "powercfg-a.txt" if args == ["/a"] else "powercfg-q-rtcwake.txt"
        return (CAPTURED / name).read_text(encoding="utf-8")

    def events(self, since: datetime, until: datetime) -> str | None:
        return (CAPTURED / "wevtutil-system.txt").read_text(encoding="utf-8")


class World:
    """One install home made by the product's code, its real dashboard, and `run()`: one double-click of
    GPU-KIT.cmd (the product's state stays between two runs, as on the PC)."""

    def __init__(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, test_install: bool = False
    ) -> None:
        self.home = tmp_path / "home"
        monkeypatch.setenv("NIGHTSHIFT_HOME", str(self.home))
        self.paths = Paths(self.home)
        self.paths.ensure()
        self.supervisor = FakeSupervisor().start()
        port = int(self.supervisor.url.rsplit(":", 1)[1])
        switches = {"fake_footage": True, "fake_gpu": True} if test_install else {}
        save_runtime(
            self.paths, Runtime.default("gpu").model_copy(update={"supervisor_port": port, **switches})
        )
        (self.home / "app").mkdir()
        (self.home / "app" / "VERSION").write_text(VERSION + "\n", encoding="utf-8")
        db = Db(self.paths.db_path)
        db.migrate()
        created = (datetime.now(UTC) - timedelta(weeks=11)).isoformat(timespec="seconds")
        crud.add_channel(
            self.paths, db, make_channel(format="master_first", created_at=created)
        )  # the owner's
        db.connect().close()
        # the card: the product's simulated nvidia-smi in the GPU PC's recorded state (812 MiB, 2 %, 38 C)
        (self.home / "run" / "fake_gpu.json").parent.mkdir(exist_ok=True)
        (self.home / "run" / "fake_gpu.json").write_text(
            json.dumps({"name": "NVIDIA GeForce RTX 5060 Ti", "base_mb": 812, "load_pct": 2, "temp_c": 38}),
            "utf-8",
        )
        self.kill_at: str | None = None
        self.fail_at: str | None = None
        self.real_downloads = False
        prev: tuple[str, ...] = ()
        for name in STUBS:
            monkeypatch.setitem(
                REGISTRY, name, KitStage(name, "llama" if name == "kit_story" else None, prev, self)
            )
            prev = (f"{name}.json",)
        monkeypatch.setitem(orch.FORMAT_STAGES, "master_first", list(STUBS))
        monkeypatch.setattr(jobs, "STAGE_MODULES", ())
        monkeypatch.setattr("ns_studio.policy.gate.available", lambda: False)
        self.task = FakeWindows()  # the Task Scheduler behind the real Settings switch and `ns schedule show`
        monkeypatch.setattr(pwsh, "run_powershell", self.task)
        monkeypatch.setattr(dash_state, "SYNC_INLINE", True, raising=False)
        monkeypatch.setattr(dash_state, "_record_checked", {})
        state = build_state(
            self.paths, fake_services=True, link=SupervisorLink(self.supervisor.url, timeout_s=2.0)
        )
        state.adapters.schedule = RealScheduleAdapter(self.paths)
        static = tmp_path / "static"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html><title>Nightshift</title>", encoding="utf-8")
        self.server = _Server(
            create_app(state=state, start_worker=False, static_dir=static, heartbeat_s=0.01)
        )
        self.traps, self.clock = Traps(), Clock()
        self.product = LocalProduct(self)
        self.windows = CannedWindows(self)
        self.answers: list[str | None] = []
        self.asked: list[str] = []
        self.out: list[str] = []
        self.say_trap: str | None = None

    def close(self) -> None:
        self.server.stop()
        self.supervisor.stop()

    def ask(self, prompt: str) -> str | None:
        self.asked.append(prompt)
        if prompt.startswith("Press Enter"):
            return ""
        if "question(s) only you can answer are open" in prompt:
            return "a" if self.answers or "ask" in self.traps.armed else ""
        self.traps.hit("ask")
        return self.answers.pop(0) if self.answers else None

    def say(self, line: str) -> None:
        self.out.append(line)
        if self.say_trap and line.startswith(self.say_trap):
            self.say_trap = None
            raise KeyboardInterrupt(line)

    def run(self, *extra: str) -> int:
        kit_here()
        self.out = []
        env = gpukit.KitEnv(
            clock=self.clock, product=self.product, windows=self.windows, ask=self.ask, say=self.say
        )
        return gpukit.main(["--home", str(self.home), *extra], env=env)

    def night(self, started: datetime) -> dict[str, Any]:
        """One night of the product (`daily.run_daily`, fake services, the stub plan) started at `started`."""
        clock = NightClock(started)
        env = jobs.open_env(frozenset(FAKEABLE))
        report = daily.run_daily(
            env, now=clock.now(), rng=random.Random(1), notifier=None, clock=clock, environ={}
        )
        from ns_studio import notifications
        from ns_supervisor import notify

        done = [x["job_id"] for x in report["jobs"]]
        message = notifications.nightly_message(report, *notifications.output_counts(env.db, done))
        if message:
            notify.leave_note(self.paths, "Nightshift", message)  # the note the Queue shows in the morning
        return report

    def nights_pass(self) -> None:
        """Both nights ran; the clock after night 2's run time + 6 h."""
        self.night(local(2026, 11, 13, 3, 0))
        self.night(local(2026, 11, 14, 3, 0))
        self.clock.t = local(2026, 11, 14, 9, 30)

    @property
    def ev(self) -> Path:
        return self.home / "kit"

    def state(self) -> dict[str, Any]:
        return json.loads((self.ev / "kit-state.json").read_text(encoding="utf-8"))

    def results(self) -> dict[str, str]:
        return {k: v["result"] for k, v in self.state()["steps"].items()}

    def zips(self) -> list[Path]:
        return sorted(self.ev.glob("*.zip"))

    def channel(self, slug: str = "gpu-kit") -> dict[str, Any]:
        status, body = self.product.http.api("GET", f"/api/channels/{slug}")
        assert status == 200, (status, body)
        return body["profile"]

    def puts(self) -> list[Any]:
        return [c[3] for c in self.product.calls if c[:3] == ("api", "PUT", "/api/settings")]


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    w = World(tmp_path, monkeypatch)
    try:
        yield w
    finally:
        w.close()


# ------------------------------------------------------------------------------------------------ 1
def test_a_first_run_goes_k0_to_k4_and_stops_with_the_morning_date(world: World) -> None:
    runtime = json.loads(world.paths.runtime_file.read_text(encoding="utf-8"))
    world.paths.runtime_file.write_text(
        json.dumps({**runtime, "fake_gpu": True}), encoding="utf-8"
    )  # typed in
    assert world.run() == 0, world.out
    assert world.results() == {"K0": "PASS", "K1": "PASS", "K2": "PASS", "K3": "PASS", "K4": "PASS",
                               "K5": "WAIT", "K6": "WAIT", "K7": "WAIT"}  # fmt: skip
    steps = world.state()["steps"]
    assert all(steps[k]["done"] and steps[k]["version"] == VERSION for k in ("K0", "K1", "K2", "K3", "K4"))
    assert world.out[-1] == "Run GPU-KIT.cmd again after Saturday 14 November 09:00."
    assert any("Friday 13 November 03:00 and Saturday 14 November 03:00" in line for line in world.out)
    machine = json.loads((world.ev / "machine.json").read_text(encoding="utf-8"))
    assert machine["gpu"] == {"name": "NVIDIA GeForce RTX 5060 Ti", "driver": "580.88", "cuda": "13.0",
                              "vram_total": "16311 MiB", "vram_used": "812 MiB"}  # fmt: skip
    assert machine["modern_standby"] is False and machine["fake_gpu_typed_on_a_real_install"] is True
    assert world.task.has == ("Ready", "03:00")  # the Settings switch registered the task
    assert world.channel()["status"] == "paused"


# ------------------------------------------------------------------------------------------------ 2
INTERRUPTS = {  # step -> how the window is closed inside it
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
def test_each_step_interrupted_resumes_on_the_next_run(world: World, step: str) -> None:
    how, where = INTERRUPTS[step]
    late = step in ("K5", "K6", "K7")
    if how == "say":
        world.say_trap = where
    else:
        world.traps.armed.add(where)
    if not late:
        assert world.run() == 130, world.out[-8:]
        assert any(line.startswith("Stopped by you") for line in world.out)
    assert world.run() == 0, world.out
    assert world.results()["K4"] == "PASS"
    world.nights_pass()
    if late:
        assert world.run() == 130, world.out
        assert not world.state()["steps"][step]["done"]
    assert world.run() == 0, world.out
    assert set(world.results().values()) == {"PASS"}
    # done once, whatever stopped it: one kit channel, one story job, one switch each way, one zip
    assert world.product.count("api", "POST", "/api/channels") == 1
    assert world.product.count("studio", "run", "--channel") == 1
    assert world.product.count("studio", "run", "--resume") == (1 if step == "K3" else 0)
    assert [p["automatic_runs"] for p in world.puts()] == [True, False]
    assert len(world.zips()) == 1 and world.out[-1].startswith("Send this one file: ")
    assert world.channel()["status"] == "paused" and world.task.has is None
    assert not (world.ev / "kit.lock").exists() and kitnights.read(world.paths) is None


# ------------------------------------------------------------------------------------------------ 3
def test_the_story_job_is_resumed_not_made_again(world: World) -> None:
    world.traps.armed.add("studio running")
    assert world.run() == 130
    (job_dir,) = (world.home / "data" / "jobs").iterdir()  # the one job, made before the window closed
    job_id = job_dir.name
    assert world.product.count("studio") == 1 and world.state()["job"].get("id") in (None, job_id)
    assert world.channel()["status"] == "paused"  # put back on the way out (Ctrl+C)
    assert world.run() == 0, world.out
    assert [c[1:] for c in world.product.calls if c[0] == "studio"] == [
        ("run", "--channel", "gpu-kit", "--count", "1", "--format", "master_first"),
        ("run", "--resume", job_id)]  # fmt: skip
    assert world.run() == 0 and world.product.count("studio") == 2  # a finished one is not touched


def test_a_job_that_finished_while_the_kit_was_closed_is_not_resumed(world: World) -> None:
    world.traps.armed.add("studio finished")  # the job's last stage ends, then the window closes
    assert world.run() == 130
    assert world.run() == 0
    assert world.product.count("studio") == 1 and world.results()["K3"] == "PASS"


def test_a_failed_story_job_is_recorded_with_its_code_and_the_kit_goes_on(world: World) -> None:
    world.fail_at = "kit_story"
    assert world.run() == 1
    assert world.results()["K3"] == "FAIL" and world.results()["K4"] == "PASS"
    assert world.state()["job"]["code"] == "NS-E303"
    assert "Nothing to do now" in world.state()["steps"]["K3"]["line"]
    assert world.out[-1] == "Run GPU-KIT.cmd again after Saturday 14 November 09:00."
    assert world.channel()["status"] == "paused"
    assert world.run() == 0 and world.product.count("studio") == 1  # not made again


# ------------------------------------------------------------------------------------------------ blocker 1
def test_the_kit_reads_what_the_product_writes(world: World) -> None:
    """A real job of the product (its stage runner and writers): the kit's stage times, loudness, QC and
    length come from what was really written, and every ledger key the kit reads is one the product writes."""
    assert world.run() == 0, world.out
    story = json.loads((world.ev / "story" / "story.json").read_text(encoding="utf-8"))
    assert [s["stage"] for s in story["stages"]] == list(STUBS)
    assert all(s["outcome"] == "done" and isinstance(s["seconds"], int | float) for s in story["stages"])
    assert (story["lufs"], story["true_peak_dbtp"], story["qc_passed"]) == (-14.2, -1.4, True)
    assert story["length_s"] == 760.0 and story["video"].endswith("The House That Waited.mp4")
    assert (
        (world.ev / "story" / "sheet.txt")
        .read_text(encoding="utf-8")
        .startswith("FILE: The House That Waited.mp4\nTITLE: The House That Waited")
    )
    world.nights_pass()  # the product's nights write their own ledger lines too
    lines = [json.loads(x) for f in sorted((world.home / "data" / "ledger").glob("*.jsonl"))
             for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]  # fmt: skip
    written = set().union(*(line.keys() for line in lines))
    source = (ROOT / "dev" / "kit" / "gpukit.py").read_text(encoding="utf-8")
    read = set(re.findall(r'\bx\.get\("(\w+)"\)', source))
    assert read and read <= written, f"the kit reads ledger keys the product never writes: {read - written}"
    kinds = set(re.findall(r'x\.get\("kind"\) == "(\w+)"', source))
    assert kinds - {"slept"} <= {line["kind"] for line in lines}, (
        kinds
    )  # ("slept": written only after a sleep)
    assert '"slept"' in (ROOT / "ns_studio" / "nightclock.py").read_text(encoding="utf-8")


# ------------------------------------------------------------------------------------------------ blocker 2
def test_the_kit_channel_is_never_planned_and_is_put_back_on_every_exit(world: World) -> None:
    world.traps.armed.add("studio running")  # Ctrl+C in the middle of the video
    assert world.run() == 130
    changes = world.state()["changes"]["channel"]
    assert (changes["was"], changes["will_be"], changes["put_back"]) == ("absent", "active", True)
    assert world.channel()["status"] == "paused"
    # a window closed or a restart runs no code: the channel stays active, and a night still never plans it
    world.product.http.api("POST", "/api/channels/gpu-kit/resume", {})
    assert world.channel()["status"] == "active"
    db = Db(world.paths.db_path)
    assert "gpu-kit" not in {j.channel.slug for j in cadence.plan_daily(world.paths, db)}
    assert cadence.KIT_CHANNEL == gpukit.CHANNEL
    report = world.night(local(2026, 11, 13, 3, 0))
    assert [c["channel"] for c in report["channels"]] == ["after-dark"]
    assert world.run() == 0 and world.channel()["status"] == "paused"


def test_an_error_the_kit_did_not_expect_still_puts_back_and_writes_the_zip(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit_here()
    monkeypatch.setattr(gpukit.Kit, "k4", lambda self: 1 / 0)
    assert world.run() == 1
    assert world.results()["K4"] == "FAIL" and "ZeroDivisionError" in world.state()["steps"]["K4"]["line"]
    assert world.channel()["status"] == "paused" and len(world.zips()) == 1


def test_closing_the_window_is_an_interrupt() -> None:
    kit_here()
    if not hasattr(signal, "SIGBREAK"):
        pytest.skip("Windows only: CTRL_CLOSE_EVENT reaches Python as SIGBREAK")
    before = signal.getsignal(signal.SIGBREAK)
    with pytest.raises(KeyboardInterrupt), gpukit._window_close_is_an_interrupt():
        signal.raise_signal(signal.SIGBREAK)
    assert signal.getsignal(signal.SIGBREAK) == before


# ------------------------------------------------------------------------------------------------ 5
def test_automatic_runs_are_put_back_as_they_were(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for case in ("off", "on", "killed", "owner"):
        with monkeypatch.context() as mp:
            w = World(tmp_path / case, mp)
            try:
                if case == "on":
                    w.product.http.api("PUT", "/api/settings", {"automatic_runs": True})  # the owner's own
                if case == "killed":  # the switch went through; the window closed before the state was saved
                    w.traps.armed.add("settings applied")
                    assert w.run() == 130 and w.state()["automatic_runs_before"] is False
                assert w.run() == 0, w.out
                record = kitnights.read(w.paths)
                if case == "on":
                    assert record is None and w.puts() == []
                else:
                    assert record is not None and record["automatic_runs_before"] is False
                    assert record["until"].startswith("2026-11-14T09:00")
                if case == "owner":  # the owner decides after the kit armed: theirs from now on
                    w.product.http.api("PUT", "/api/settings", {"automatic_runs": True})
                w.nights_pass()
                assert w.run() == 0, w.out
                line = w.state()["steps"]["K7"]["line"]
                if case in ("off", "killed"):
                    assert [p["automatic_runs"] for p in w.puts()] == [True, False] and w.task.has is None
                    assert "off again, as they were before the kit" in line
                    assert any(x.startswith("Switching Automatic runs off again") for x in w.out)
                if case == "on":
                    assert w.puts() == [] and w.task.has is not None and "left as you had them" in line
                if case == "owner":
                    assert w.task.has is not None and "you switched Automatic runs yourself" in line
                assert kitnights.read(w.paths) is None
            finally:
                w.close()


def test_the_third_night_puts_automatic_runs_back_when_the_kit_is_never_run_again(world: World) -> None:
    assert world.run() == 0
    assert world.task.has == ("Ready", "03:00")
    night2_end = local(2026, 11, 14, 9, 0)
    assert (
        kitnights.expire_if_due(world.paths, night2_end - timedelta(minutes=1)) is None
    )  # nights 1 and 2 run
    note = kitnights.expire_if_due(world.paths, night2_end + timedelta(hours=18))  # the third night
    assert note == kitnights.EXPIRED_NOTE and world.task.has is None and kitnights.read(world.paths) is None
    notes = json.loads((world.home / "run" / "morning_note.json").read_text(encoding="utf-8"))
    assert any(
        kitnights.EXPIRED_NOTE in json.dumps(n) for n in (notes if isinstance(notes, list) else [notes])
    )
    # the nightly's own route: a record past its end makes `studio run --daily` do nothing but that
    kitnights.write(world.paths, {"until": "2026-01-01T00:00:00+00:00", "automatic_runs_before": False,
                                  "run_time": "03:00"})  # fmt: skip
    world.product.http.api("PUT", "/api/settings", {"automatic_runs": True, "by": kitnights.BY_KIT})
    res = CliRunner().invoke(_studio_app(), ["run", "--daily", "--fake-services"])
    assert res.exit_code == 0 and "The GPU kit had switched Automatic runs on" in res.output, res.output
    assert world.task.has is None and not Db(world.paths.db_path).query("select * from runs")


# ------------------------------------------------------------------------------------------------ 6
@pytest.mark.parametrize(("start", "starts"), [
    (datetime(2026, 11, 12, 23, 30, 0), True),  # 3 h estimate + 30 min ends exactly at 03:00: it starts
    (datetime(2026, 11, 12, 23, 30, 1), False),  # one second later it would end after the run time
])  # fmt: skip
def test_too_close_to_the_run_time_the_video_waits(world: World, start: datetime, starts: bool) -> None:
    world.clock.t = start.astimezone()
    assert world.run() == 0
    assert (world.product.count("studio") == 1) is starts
    if not starts:
        line = world.state()["steps"]["K3"]["line"]
        assert world.results()["K3"] == "WAIT" and not world.state()["steps"]["K3"]["done"]
        assert "about 3.0 h here plus 30 minutes" in line and "Friday 13 November 03:00" in line
        assert world.out[-1] == "Run GPU-KIT.cmd again after Friday 13 November 09:00."
        assert world.results()["K4"] == "WAIT" and world.task.has is None


# ------------------------------------------------------------------------------------------------ 7
def test_the_nights_are_read_from_the_runs_rows_and_the_ledger(world: World) -> None:
    assert world.run() == 0
    world.night(local(2026, 11, 13, 3, 0))
    world.clock.t = local(2026, 11, 13, 12, 0)  # a third run before night 2 ended: it waits
    assert world.run() == 0
    assert world.results()["K5"] == "PASS" and world.results()["K6"] == "WAIT"
    assert world.state()["steps"]["K5"]["line"].startswith("Night 1: ran ")
    assert world.out[-1] == "Run GPU-KIT.cmd again after Saturday 14 November 09:00."
    world.clock.t = local(2026, 11, 14, 9, 1)  # night 2 has no row after its run time + 6 h
    assert world.run() == 0
    assert "Night 2: did not run" in world.state()["steps"]["K6"]["line"]
    assert world.state()["nights"][1]["schedule"][0] == 0  # `ns schedule show`'s answer, kept
    with zipfile.ZipFile(world.zips()[0]) as z:
        names = set(z.namelist())
        assert "logs/nightly-20261113.json" in names and "run/morning_note.json" in names
        ledger = z.read("ledger/ledger-202611.jsonl").decode("utf-8")
        assert '"kind": "night_start"' in ledger and '"kind": "job_end"' in ledger


# ------------------------------------------------------------------------------------------------ blocker 3
def test_a_newer_kit_collects_the_nights_an_older_one_armed(world: World) -> None:
    assert world.run() == 0  # version A arms the nights (Automatic runs were off)
    (world.home / "app" / "VERSION").write_text("9.9.9-rc1\n", encoding="utf-8")  # a newer candidate
    world.nights_pass()
    assert world.run() == 0, world.out
    state = world.state()
    assert state["steps"]["K4"]["version"] == VERSION and state["steps"]["K7"]["version"] == "9.9.9-rc1"
    assert state["automatic_runs_before"] is False and state["versions"] == [VERSION, "9.9.9-rc1"]
    assert "going on from the kit of " + VERSION in state["steps"]["K0"]["line"]
    assert [p["automatic_runs"] for p in world.puts()] == [True, False] and world.task.has is None
    assert world.product.count("studio") == 1 and len(world.zips()) == 1


# ------------------------------------------------------------------------------------------------ 8
def test_the_collected_folder_and_zip_hold_every_part(world: World) -> None:
    old = world.ev / "0.1.15-rc3" / "kit-state.json"  # a folder an earlier build of the kit wrote
    old.parent.mkdir(parents=True)
    old.write_text('{"version": "0.1.15-rc3"}', encoding="utf-8")
    bad = world.ev / "collected" / "broken.zip"  # one bad file among good ones
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"not a zip")
    assert world.run() == 0
    world.nights_pass()
    assert world.run() == 0
    z_path = world.zips()[0]
    assert z_path.name == "nightshift-gpu-kit-20261112.zip" and z_path.parent == world.ev
    size = f"{z_path.stat().st_size / 1e6:.1f} MB"
    assert world.out[-1] == f"Send this one file: {z_path} ({size}). The kit sent nothing anywhere."
    with zipfile.ZipFile(z_path) as z:
        names = set(z.namelist())
        readme = z.read("README.txt").decode("utf-8")
        missing = z.read("MISSING.txt").decode("utf-8")
    for part in (
        "machine.json",
        "verify.json",
        "proofs.json",
        "story/story.json",
        "story/frames.png",
        "story/sheet.txt",
        "story/sheet.json",
        "story/studio-run.log",
        "ledger/ledger-202611.jsonl",
        "logs/nightly-20261113.json",
        "logs/nightly-20261114.json",
        "logs/verify-report.json",
        "run/morning_note.json",
        "collected/windows-events-night1.txt",
        "collected/windows-events-night2.txt",
        "collected/doctor-bundle.txt",
        "kit-state.json",
    ):
        assert part in names, part
    assert any(
        n.startswith("collected/nightshift-diagnostics-") and "/" in n[10:] for n in names
    )  # the real zip
    assert "run/vram-need.json: no model load was measured yet" in missing
    assert "collected/broken.zip: " in missing  # named, the rest is there
    assert readme.splitlines()[0] == "Real graphics card: the GPU PC's own readings."
    assert "K7 collect everything and put Automatic runs back: PASS" in readme
    assert json.loads(old.read_text(encoding="utf-8")) == {"version": "0.1.15-rc3"}  # left alone
    assert not [p for p in world.ev.iterdir() if p.name.endswith(".tmp")]


def test_a_home_without_its_models_fails_k1_on_the_products_own_check(world: World) -> None:
    world.real_downloads = True  # installer/stage2/verify_downloads.run on a home with no model set
    assert world.run() == 1
    assert (
        world.results()["K1"] == "FAIL"
        and "a model file is missing or damaged" in world.state()["steps"]["K1"]["line"]
    )
    machine = json.loads((world.ev / "machine.json").read_text(encoding="utf-8"))
    assert machine["models"][0] != 0 and "INSTALL.cmd" in machine["models"][1]  # the product's own words
    assert world.product.count("studio") == 0 and len(world.zips()) == 1


def test_a_failing_step_still_yields_the_bundle(world: World) -> None:
    card = world.home / "run" / "fake_gpu.json"
    card.write_text('{"fail": "missing"}', encoding="utf-8")  # no driver: nvidia-smi is not there
    assert world.run() == 1
    assert world.results() == {"K0": "PASS", "K1": "FAIL", "K2": "NOT RUN", "K3": "NOT RUN", "K4": "NOT RUN",
                               "K5": "NOT RUN", "K6": "NOT RUN", "K7": "NOT RUN"}  # fmt: skip
    assert "install the newest NVIDIA driver" in world.state()["steps"]["K1"]["line"]
    assert world.product.count("studio") == 0 and world.puts() == []
    with zipfile.ZipFile(world.zips()[0]) as z:
        assert "machine.json" in z.namelist() and "K1 record this PC: FAIL" in z.read("README.txt").decode()
    assert (
        world.out[-1].startswith("The zip so far: ") and "Do what the FAIL line above says" in world.out[-1]
    )
    card.write_text('{"name": "NVIDIA GeForce RTX 5060 Ti"}', encoding="utf-8")
    assert world.run() == 0 and world.results()["K1"] == "PASS"  # redone whole on the next run


# ------------------------------------------------------------------------------------------------ blocker 4
SECRET_USER = "Jösé_عمر"
SECRET_PC = "JOSE-PC"


def _encodings(secret: str) -> list[bytes]:
    from urllib.parse import quote

    forms = {secret, secret.lower(), secret.upper(), json.dumps(secret)[1:-1], quote(secret, safe=""),
             quote(secret, safe="").lower()}  # fmt: skip
    out: list[bytes] = []
    for f in forms:
        out += [f.encode("utf-8"), f.encode("utf-16-le"), f.encode("utf-16-be")]
    return [b for b in out if len(b) >= 4]


def _all_bytes(path: Path) -> list[tuple[str, bytes]]:
    """Every entry of a zip, nested zips opened too."""
    import io

    out: list[tuple[str, bytes]] = []
    todo: list[tuple[str, bytes]] = [("", path.read_bytes())]
    while todo:
        prefix, data = todo.pop()
        out.append((prefix or path.name, data))
        if data[:2] == b"PK":
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                todo += [(f"{prefix}/{i.filename}", z.read(i)) for i in z.infolist() if not i.is_dir()]
    return out


def test_no_secret_leaves_in_the_zip(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = rf"C:\Users\{SECRET_USER}"
    monkeypatch.setenv("USERNAME", SECRET_USER)
    monkeypatch.setenv("USERPROFILE", profile)
    monkeypatch.setenv("COMPUTERNAME", SECRET_PC)
    home = str(world.home)
    planted = "\n".join([
        f'{{"path": "{profile}\\\\Desktop", "home": {json.dumps(home)}, "pc": "{SECRET_PC}"}}',
        json.dumps({"profile": profile, "user": SECRET_USER, "home": home}),  # \\u escapes
        f"GET https://pixabay.com/api/?key=1234567-0123456789abcdef0123456789abcdef&q=rain {PEXELS} {HF}",
        f"url file:///{profile.replace(chr(92), '/')}/x and %2F{SECRET_USER} {SECRET_USER.upper()}",
    ])  # fmt: skip
    (world.home / "logs" / "studio-20261113.jsonl").write_text(planted + "\n", encoding="utf-8")
    (world.home / "answers.json").write_text(json.dumps({"pexels_api_key": PEXELS, "hf_token": HF}), "utf-8")
    hand = world.ev / "hand-checks"
    hand.mkdir(parents=True)
    (hand / "tree-after.txt").write_text(f"{SECRET_PC}\\{SECRET_USER} {profile} {HF}\r\n", encoding="utf-16")
    from urllib.parse import quote

    (world.home / "logs" / "launch-20261113.jsonl").write_text(
        quote(profile, safe="") + "\n", encoding="utf-8"
    )
    assert world.run() == 0
    world.nights_pass()
    assert world.run() == 0
    entries = _all_bytes(world.zips()[0])
    assert any("collected/nightshift-diagnostics" in n for n, _ in entries)  # the doctor zip is inside
    assert any(n.endswith("hand-checks/tree-after.txt") for n, _ in entries)
    secrets = [SECRET_USER, SECRET_PC, profile, home, PEXELS, HF, "0123456789abcdef0123456789abcdef"]
    for name, data in entries:
        if data[:2] == b"PK":
            continue
        for s in secrets:
            for form in _encodings(s):
                assert form not in data, (name, s, form)
            assert s.lower().encode() not in name.lower().encode(), name


# ------------------------------------------------------------------------------------------------ 9
_CONNECTS: list[Any] = []
_RECORD = [False]


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if _RECORD[0] and event == "socket.connect":
        _CONNECTS.append(args[1])


sys.addaudithook(_audit)  # (records only while a test of this file switches it on)


def test_the_kit_makes_no_network_request(world: World) -> None:
    _CONNECTS.clear()
    _RECORD[0] = True
    try:
        assert world.run() == 0
    finally:
        _RECORD[0] = False
    assert _CONNECTS, "the kit asked the dashboard nothing: the test sees nothing"
    assert {a[0] for a in _CONNECTS if isinstance(a, tuple)} == {"127.0.0.1"}
    for f in sorted((ROOT / "dev" / "kit").rglob("*")):
        if f.suffix not in (".py", ".ps1"):
            continue
        text = f.read_text(encoding="utf-8")
        assert not re.search(r"https://|\brequests\b|httpx|http\.client|Invoke-WebRequest|Invoke-RestMethod|"
                             r"WebClient|socket\.", text), f.name  # fmt: skip
        assert set(re.findall(r"http://[\w.]+", text)) <= {"http://127.0.0.1"}, f.name
        lines = text.splitlines()
        uses = [i for i, line in enumerate(lines) if "urllib.request" in line or "urllib.error" in line]
        if f.name != "gpukit.py":
            assert not uses, f.name
            continue
        real = lines.index("class InstallProduct:")
        api = next(i for i in range(real, len(lines)) if lines[i].startswith("    def api(self, method: str"))
        end = next(i for i in range(api + 1, len(lines)) if lines[i].startswith("    def "))
        assert uses and all(api < i < end for i in uses), "urllib outside InstallProduct.api"
        assert 'f"http://127.0.0.1:{self.port}{path}"' in "\n".join(lines[api:end])
    assert socket  # (the loopback dashboard is the test's own server)


# ------------------------------------------------------------------------------------------------ 10
def test_a_second_kit_at_the_same_time_is_refused(world: World) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        world.ev.mkdir(parents=True)
        lock = world.ev / "kit.lock"
        now = datetime.now().astimezone().isoformat()
        lock.write_text(f"kit | GPU kit pid {child.pid} | {now}\n", "ascii")
        assert world.run() == 2
        assert world.out[-1].startswith(f"REFUSED Another GPU kit is running (pid {child.pid}).")
        assert lock.read_text(encoding="ascii").startswith(f"kit | GPU kit pid {child.pid}")
        assert not (world.ev / "kit-state.json").exists()
    finally:
        child.kill()
        child.wait(30)
    assert world.run() == 0  # its kit is gone: the lock is taken over


# ------------------------------------------------------------------------------------------------ 11
def test_the_paste_answers_are_recorded_and_later_keeps_it_open(world: World) -> None:
    world.answers = [
        "y",
        "y",
        "y",  # the three watch questions
        "y",
        "n Studio cut the description at 4,900 characters",
        "later",
        "y",
        "y",
        "Altered content",
        "101/100, Save refused",
        "later",
        "later",
        "later",
        "y",
        "n",
        "nothing changed",
        None,
    ]  # task_end q1, q2 (+ its note), then nobody answers
    assert world.run() == 0
    assert any(str(gpukit.KIT_DIR / "K8_PASTE_CHECK.md") in a for a in world.asked)  # the full path
    assert sum("question(s) only you can answer are open" in a for a in world.asked) == 1  # one bounded wait
    paste = json.loads((world.ev / "paste.json").read_text(encoding="utf-8"))
    assert [paste[f"q{n}"]["answer"] for n in range(1, 6)] == ["y", "n", "later", "y", "y"]
    assert paste["q2"]["note"] == "Studio cut the description at 4,900 characters"
    assert paste["extra"]["field_name"] == "Altered content" and paste["extra"]["E2"] == "later"
    assert world.state()["checklists"]["paste"]["result"] == "WAIT"
    world.asked.clear()
    world.answers = ["y", "130/500", "", ""]
    assert world.run() == 0
    asked = [a for a in world.asked if "[y/n/later]" in a or "(or later)" in a]
    assert asked[0].startswith("Were the tags accepted") and [a[:2] for a in asked[1:4]] == ["E2", "E3", "E4"]
    paste = json.loads((world.ev / "paste.json").read_text(encoding="utf-8"))
    assert paste["q3"]["answer"] == "y" and paste["q1"]["answer"] == "y" and paste["extra"]["E2"] == "130/500"


# ------------------------------------------------------------------------------------------------ blocker 5
def test_the_hand_checks_are_steps_with_questions_observations_and_files(world: World) -> None:
    hand = world.ev / "hand-checks"
    hand.mkdir(parents=True)
    (hand / "tree-after.txt").write_text("cmd.exe 4242\r\n", encoding="utf-16")  # as PowerShell 5.1 saves it
    (hand / "sleep-states.txt").write_text(
        (CAPTURED / "powercfg-a.txt").read_text("utf-8"), encoding="utf-16"
    )
    world.answers = ["later"] * 13 + [None]  # the video and paste: later; then the person walks away
    assert world.run() == 0
    first = next(a for a in world.asked if a.startswith("Hand check 1"))
    assert str(gpukit.KIT_DIR / "night-task-end.txt") in first and str(hand) in first
    task_end = next(x for x in world.out if "hand check 1, the task's end" in x)
    standby = next(x for x in world.out if "hand check 2, sleep during a night" in x)
    assert (
        task_end.startswith("WAIT") and "files in hand-checks: sleep-states.txt, tree-after.txt" in task_end
    )
    assert "modern standby (S0) no" in standby and '"slept" lines in the nights 0' in standby
    world.nights_pass()
    world.answers = ["y"] * 21  # every open question: the video, the paste, both hand checks, the morning
    assert world.run() == 0
    assert world.state()["checklists"]["task_end"]["result"] == "PASS"
    obs = json.loads((world.ev / "standby.json").read_text(encoding="utf-8"))["kit_observed"]
    assert obs["modern_standby"] is False and obs["sleeps_in_the_nights"] == 0
    with zipfile.ZipFile(world.zips()[0]) as z:
        assert z.read("hand-checks/tree-after.txt").decode("utf-8").startswith("cmd.exe 4242")  # UTF-16 read
        assert "task_end.json" in z.namelist() and "standby.json" in z.namelist()


# ------------------------------------------------------------------------------------------------ 14
@pytest.mark.parametrize("switch", [["--night-now"], ["--clock", "2026-11-12T22:00"], ["--dry-run"]])
def test_the_dry_run_switches_work_only_on_a_test_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, switch: list[str]
) -> None:
    real = World(tmp_path / "real", monkeypatch)
    try:
        assert real.run(*switch) == 2 and real.out[-1].startswith("REFUSED ")
        assert not real.ev.exists() and real.product.calls == []
    finally:
        real.close()
    with monkeypatch.context() as mp:
        test = World(tmp_path / "test", mp, test_install=True)
        try:
            assert test.run() == 2 and "is not a GPU install" in test.out[-1]
            assert test.run("--dry-run", *[a for a in switch if a != "--dry-run"]) == 0, test.out
        finally:
            test.close()


def test_a_dry_run_on_the_simulated_card_is_marked_and_runs_both_nights_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = World(tmp_path, monkeypatch, test_install=True)
    try:
        assert w.run("--dry-run", "--night-now") == 0, w.out
        assert set(w.results().values()) == {"PASS"}
        assert w.product.count("night_now") == 2 and w.puts() == []  # no task is armed
        mark = "simulated (fake_gpu)"
        assert w.out[1] == f"SIMULATED graphics card ({mark}): a test install; nothing measured here " \
            "is evidence of the GPU PC."  # fmt: skip
        assert all(w.state()["steps"][k]["line"].startswith(mark) for k in ("K3", "K5", "K6"))
        with zipfile.ZipFile(w.zips()[0]) as z:
            assert z.read("README.txt").decode().startswith(f"SIMULATED GRAPHICS CARD ({mark})")
            assert json.loads(z.read("machine.json"))["simulated"] == mark
    finally:
        w.close()


# --------------------------------------------------------------------------------------------- the scrubber
def test_the_scrubber_masks_every_form_of_keys_tokens_the_user_the_home_and_the_pc() -> None:
    kit_here()
    masks = gpukit.Masks(home=r"D:\Nightshift", user="Ahmed", profile=r"C:\Users\Ahmed", computer="AHMED-PC",
                         short_profile=r"C:\Users\AHMED~1")  # fmt: skip
    text = "\n".join([
        f"pexels {PEXELS} pixabay 12345678-0123456789abcdef0123456789abcdef {HF}",
        '{"pexels_api_key": "secret-value", "X-NS-Token": "abc123", "keys": {"pexels": true}}',
        "GET https://example.invalid/api?key=abcdef&q=rain Authorization: Bearer abcdefghijklmnop",
        "token ghp_" + "x" * 36 + " sid S-1-5-21-111-222-333-1001",
        r"home D:\Nightshift\logs and D:\\Nightshift\\data and d:/nightshift/run and D%3A%5CNightshift",
        r"profile C:\Users\Ahmed\Desktop, C:\USERS\AHMED~1\x, user Ahmed on AHMED-PC (ahmed)",
        "sha256 " + "f" * 64,
    ])  # fmt: skip
    out = gpukit.scrub_text(text, masks)
    for gone in (PEXELS, HF, "12345678-0123", "secret-value", "abc123", "abcdef&", "abcdefghijklmnop", "ghp_",
                 "S-1-5-21", "Nightshift", "nightshift", "Users", "Ahmed", "ahmed", "AHMED"):  # fmt: skip
        assert gone not in out, gone
    assert out.count("<home>") == 4 and out.count("<profile>") == 2 and "user <user> on <pc> (<user>)" in out
    assert '"keys": {"pexels": true}' in out and "f" * 64 in out  # not a secret: kept
    assert len(gpukit.MASKED) == 5


def test_a_nested_zip_is_scrubbed_entry_by_entry(tmp_path: Path) -> None:
    kit_here()
    src = tmp_path / "diag.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("logs/Ahmed.jsonl", '{"path": "C:/Users/Ahmed/x", "token": "t0k3n"}')
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
