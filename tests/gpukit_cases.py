"""Card gpu-kit: the one-command GPU kit (`dev/kit/gpukit.py`), run end to end on a fake home.

The cases of the kit and their world. pytest does not collect this file by its name: the part files
tests/test_gpu_kit.py and tests/test_gpu_kit_2.py to _4.py import the cases by name, so that CI's by-file
shards can spread them (tests/test_gpu_kit_parts.py checks that each case is in exactly one of them).

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
import os
import random
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from typer.testing import CliRunner

from ns_common.config import save_channel, save_runtime
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
#: Keys of each provider's REAL shape (no pattern of the scrubber matches them): the owner's Pixabay key is 8
#: digits, a hyphen and 25 hex characters; Pexels keys are longer mixed strings.
PIXABAY_REAL = "48213907-3fa9c0e1d2b4a5968f7e6d5c4"
PEXELS_REAL = "Xk9mQ2wE7rT4yU1iO8pA3sD6fG0hJ5kL2zX9cV4bN7mQ1wE3rT6yU8iO0pA2sD5f"
NEW_STEPS = ("KS", "KC", "KV", "KN", "KA")


def tone_bursts(path: Path, seconds: float = 8.0) -> Path:
    """Speech-like sound through the product's WAV writer: half-second tones and pauses (the product's sample
    check wants a level and a quiet floor)."""
    import numpy as np

    from ns_tts.audio import write_wav

    sr = 24000
    n = np.arange(int(sr * seconds))
    x = 0.3 * np.sin(2 * np.pi * 220 * n / sr) * ((n // (sr // 2)) % 2 == 0)
    path.parent.mkdir(parents=True, exist_ok=True)
    write_wav(path, x.astype(np.float32), sr)
    return path


class StubFootage:
    """A keyed stock source that never uses the network (the Dashboard's `footage_sources` seam): one
    candidate per search, a file of a few KB; `refuse` makes it answer as the provider did."""

    def __init__(self, name: str) -> None:
        self.name, self.n = name, 0
        self.refuse: Exception | None = None

    def search(self, keyword: str, *, orientation: Any, per_page: int = 80) -> list[Any]:
        from ns_studio.media.clip_sources import ClipCandidate

        if self.refuse is not None:
            raise self.refuse
        self.n += 1
        return [ClipCandidate(source=self.name, id=str(700 + self.n), page_url=f"https://{self.name}.example/v",
                              download_url="https://cdn.example/v.mp4", width=1920, height=1080, duration=9.0,
                              tags=("fog",), title="fog", license=f"{self.name} licence")]  # fmt: skip

    def download(self, cand: Any, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"v" * 5370)
        return dest


class Killed(BaseException):
    """The kit's process is ended there (Task Manager, a power cut): none of its code runs after it."""


class Traps:
    """Named points where a test closes the kit's window (a KeyboardInterrupt) or ends its process (`kills`:
    `Killed`), each once."""

    def __init__(self) -> None:
        self.armed: set[str] = set()
        self.kills: set[str] = set()

    def hit(self, name: str) -> None:
        if name in self.kills:
            self.kills.discard(name)
            raise Killed(name)
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
        if w.hold_at == self.name:  # the job is in this stage until the test lets it go on
            w.stage_held.set()
            w.stage_may_go.wait(120)
        if w.out_of_time_at == self.name and ctx.channel.slug == "gpu-kit" and getattr(ctx, "run_id", None):
            from ns_studio import nightclock, nightfit  # the night's own time rule stops the kit's video here

            raise nightclock.OutOfTime(nightfit.Decision(
                answer=nightfit.STOP, reason=nightfit.STOP_NO_ROOM, where=nightfit.STAGE, stage=self.name,
                need_s=3600.0, left_s=60.0, basis=nightfit.HISTORY))  # fmt: skip
        ctx.write_json(self.outputs[0], {"stage": self.name})
        if self.name == "kit_story":  # the narration, as the voice stage writes it (audio/<segment>.wav)
            tone_bursts(Path(ctx.file("audio/s001.wav")))
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
        try:
            return gpukit.InstallProduct(self.world.home, self.world.server.port, self.world.engine_port)
        except TypeError:  # (a kit that never asks the engine itself takes no engine port)
            return gpukit.InstallProduct(self.world.home, self.world.server.port)

    def api(self, method: str, path: str, body: Any = None, timeout: float = 60) -> tuple[int | None, Any]:
        self.calls.append(("api", method, path, body))
        self.world.traps.hit(f"api {method} {path}")
        if self.world.dashboard_down:  # Stop All between two runs: nothing answers until it is started
            return None, None
        if self.world.refuse_cancel and method == "POST" and path.endswith("/cancel"):
            return None, None
        if self.world.jobs_route_times_out and method == "GET" and path.startswith("/api/jobs/"):
            return None, None  # the job route times out while /api/health answers
        out = self.http.api(method, path, body, timeout)
        if method == "PUT" and path == "/api/settings":
            self.world.traps.hit("settings applied")
        if method == "POST" and path == "/api/jobs/clip" and out[0] == 201:
            self.world.finish_clip(out[1]["job_id"])
        self.world.traps.hit(f"after api {method} {path}")
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
        """The kit's nsup.ps1: the engine is started when it does not answer (with a real engine: the
        product's own, in this process), then its Dashboard service."""
        self.calls.append(("start_engine",))
        w = self.world
        if w.real_engine and w.engine is None:
            w.start_nightshift()
        w.dashboard_down = False
        hook, w.after_engine_start = w.after_engine_start, None
        if hook is not None:
            hook()
        return True, "engine + dashboard up (headless)"

    # What the product says about who uses it: the kit's own readers, on this home and its engine.
    def engine_api(
        self, method: str, path: str, body: Any = None, timeout: float = 30
    ) -> tuple[int | None, Any]:
        self.calls.append(("engine_api", method, path, body))
        return self.world.engine_http(method, path, body, timeout)

    def engine_record(self) -> dict[str, Any] | None:
        return self.http.engine_record()

    def live_work(self) -> list[dict[str, Any]] | None:
        return self.http.live_work()

    def dashboard_in_use(self) -> bool | None:
        return self.http.dashboard_in_use()

    def update_running(self) -> bool:
        return self.http.update_running()

    def night_now(self, limit_s: int, timeout: float) -> tuple[int | None, str]:
        self.calls.append(("night_now", limit_s))
        self.world.night(self.world.clock.now())
        self.world.clock.t += timedelta(minutes=15)  # a night takes its time
        return 0, "night ran"

    def frame_grid(self, video: Path, out: Path, timeout: float) -> str | None:
        out.write_bytes(b"\x89PNG grid")  # (the install's FFmpeg; not on the laptop's test PATH)
        return None

    def cut(self, src: Path, out: Path, start_s: float, seconds: float, audio: bool) -> str | None:
        """(the install's FFmpeg; not on the laptop's test PATH): the cut's file, sound through the product's
        WAV writer."""
        self.calls.append(("cut", src.name, start_s, seconds, audio))
        if audio:
            tone_bursts(out, seconds)
        else:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(src.read_bytes())
        return None

    def start_code(self, launcher: Path) -> Any:
        """The launcher's effect on the engine: the card taken as owner `opencode` (the model loaded)."""
        self.calls.append(("start_code", launcher.name))
        self.world.traps.hit("code started")
        self.world.supervisor.state.update(
            {"gpu_owner": "opencode", "services": {"llama": {"state": "ready"}}}
        )
        proc = CodeProc(80000 + len(self.calls))
        self.world.code = proc
        return proc

    def opencode_prompt(self, exe: Path, prompt: str, cwd: Path, timeout: float) -> tuple[int | None, str]:
        self.calls.append(("opencode_prompt", exe.name, prompt))
        return 0, "Two plus two is four."

    def kill_tree(self, pid: int, created: float | None) -> None:
        """The window closed: the launcher's process ends and the engine frees the card (holder gone)."""
        self.calls.append(("kill_tree", pid))
        if self.world.code is not None and self.world.code.pid == pid:
            self.world.code.rc = 1
            if self.world.frees_card:
                self.world.supervisor.state.update({"gpu_owner": None, "services": {}})

    def count(self, *prefix: Any) -> int:
        return sum(1 for c in self.calls if c[: len(prefix)] == prefix)


class CodeProc:
    def __init__(self, pid: int) -> None:
        self.pid, self.rc = pid, None

    def poll(self) -> int | None:
        return self.rc

    def kill(self) -> None:
        self.rc = -9


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


class Engine:
    """Nightshift's REAL engine in this process: the product's `Supervisor` (its GPU lock, holder record and
    services table) behind the product's own app (`ns_supervisor.app.create_app`: the lifespan writes and
    removes the start record `run/supervisor.json`; the routes are /status, /acquire, /release, /stop_all,
    /shutdown), listening on the home's engine port. A model service is the supervisor tests' stub process
    (`ns_supervisor.testing.StubTable`), started and ended by the supervisor itself. `seen` lists every
    request that reached the engine."""

    def __init__(self, world: World) -> None:
        import uvicorn

        from ns_common.config import load_runtime
        from ns_supervisor.app import create_app
        from ns_supervisor.core import Supervisor
        from ns_supervisor.gpu import NullGpuProbe
        from ns_supervisor.testing import StubTable, fast_config

        self.sup = Supervisor(world.paths, load_runtime(world.paths), resolver=StubTable(),
                              config=fast_config(idle_unload_s=3600.0), gpu_probe=NullGpuProbe())  # fmt: skip
        self.seen: list[tuple[str, str]] = []
        app = create_app(self.sup)

        async def recorded(scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] == "http":
                self.seen.append((scope["method"], scope["path"]))
            await app(scope, receive, send)

        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", world.engine_port))
        self.sock.listen(64)
        self.server = uvicorn.Server(uvicorn.Config(recorded, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.sock]}, daemon=True)
        self.thread.start()
        end = time.monotonic() + 30
        while not self.server.started:
            assert self.thread.is_alive() and time.monotonic() < end, "the engine did not start"
            time.sleep(0.02)
        self.started_at = self.sup.started_wall

    def stop(self) -> None:
        """The engine ends as the product ends it: its services stopped, its start record removed."""
        self.server.should_exit = True
        self.thread.join(60)
        self.sock.close()


class World:
    """One install home made by the product's code, its real dashboard, and `run()`: one double-click of
    GPU-KIT.cmd (the product's state stays between two runs, as on the PC)."""

    def __init__(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        *,
        test_install: bool = False,
        real_engine: bool = False,
    ) -> None:
        self.home = tmp_path / "home"
        monkeypatch.setenv("NIGHTSHIFT_HOME", str(self.home))
        self.paths = Paths(self.home)
        self.paths.ensure()
        # `real_engine`: Nightshift is not running until somebody starts it, and what is then started is the
        # product's own engine (`Engine`); else the Dashboard's canned stand-in for it, always up
        self.real_engine = real_engine
        self.engine: Engine | None = None
        self.after_engine_start: Any = None
        self.job_may_end = threading.Event()
        self.job_thread: threading.Thread | None = None
        if real_engine:
            from ns_supervisor.testing import free_port

            self.supervisor: Any = None
            port = free_port()
        else:
            self.supervisor = FakeSupervisor().start()
            port = int(self.supervisor.url.rsplit(":", 1)[1])
        self.engine_port = port
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
        self.hold_at: str | None = None
        self.stage_held, self.stage_may_go = threading.Event(), threading.Event()
        self.out_of_time_at: str | None = None
        self.say_hooks: dict[str, Any] = {}
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
            self.paths, fake_services=True, link=SupervisorLink(f"http://127.0.0.1:{port}", timeout_s=2.0)
        )
        state.adapters.schedule = RealScheduleAdapter(self.paths)
        static = tmp_path / "static"
        static.mkdir()
        (static / "index.html").write_text("<!doctype html><title>Nightshift</title>", encoding="utf-8")
        from ns_dashboard.assistant.llm import ScriptedToolLlm, StaticProvider, say

        self.llm = ScriptedToolLlm([say("The Outbox holds your finished videos and their sheets.")] * 20)
        app = create_app(state=state, start_worker=False, static_dir=static, heartbeat_s=0.01,
                         llm_provider=StaticProvider(self.llm))  # fmt: skip
        self.dash_state = state
        self.footage = {"pixabay": StubFootage("pixabay"), "pexels": StubFootage("pexels")}
        app.state.dashboard.library.footage_sources = lambda keys, _p: [self.footage[n] for n in keys]
        state.adapters.keyring.set("pixabay_api_key", PIXABAY_REAL)  # the owner's key (Pexels: none)
        self.server = _Server(app)
        self.code: CodeProc | None = None
        self.frees_card = True
        self.clip_status = "done"
        self.dashboard_down = real_engine
        self.refuse_cancel = False
        self.jobs_route_times_out = False
        self.traps, self.clock = Traps(), Clock()
        self.product = LocalProduct(self)
        self.windows = CannedWindows(self)
        self.answers: list[str | None] = []
        self.asked: list[str] = []
        self.stand_in_for_the_person = True  # False: the kit gets no `ask` (it would read its own input)
        self.out: list[str] = []
        self.say_trap: str | None = None

    def close(self) -> None:
        self.job_may_end.set()
        self.stage_may_go.set()
        if self.job_thread is not None:
            self.job_thread.join(30)
        self.server.stop()
        if self.engine is not None:
            self.engine.stop()
        if self.supervisor is not None:
            self.supervisor.stop()

    # ---- Nightshift's real engine and what its owner does with it (`real_engine`) ----
    def engine_http(self, method: str, path: str, body: Any = None, timeout: float = 30) -> Any:
        """One request to the engine's own API on this home's engine port (the kit's loopback client)."""
        return gpukit.InstallProduct(self.home, self.engine_port).api(method, path, body, timeout)

    def start_nightshift(self) -> Engine:
        """Nightshift is started (by the kit's nsup, or by its owner from the Desktop)."""
        assert self.engine is None
        self.engine = Engine(self)
        self.dashboard_down = False
        return self.engine

    def stop_nightshift(self) -> None:
        """The owner ends Nightshift: the engine stops as the product stops (its start record goes)."""
        assert self.engine is not None
        self.engine.stop()
        self.engine, self.dashboard_down = None, True

    def owner_opens_code(self) -> None:
        """The owner opens Nightshift Code: the launcher's own request (launchers/nightshift-code.ps1: owner
        `opencode`, the coding model, the pid of its console, here this live process) to the engine's own
        /acquire route. The engine records the holder and loads the model."""
        body = {"service": "llama", "owner": "opencode", "model_alias": "code", "wait": True,
                "timeout_s": 7200, "pid": os.getpid()}  # fmt: skip
        status, answer = self.engine_http("POST", "/acquire", body, 120)
        assert status == 200, (status, answer)
        assert self.holder() == ("opencode", os.getpid())

    def owner_closes_code(self) -> None:
        """The owner closes it again (the launcher's own /release)."""
        assert self.engine_http("POST", "/release", {"service": "llama", "owner": "opencode"})[0] == 200

    def holder(self) -> tuple[Any, Any]:
        """Who holds the graphics card and with which process, as the engine's own /status says."""
        status, body = self.engine_http("GET", "/status")
        assert status == 200, (status, body)
        return body["gpu_owner"], body["holder_pid"]

    def owner_starts_job(self, channel: str = "after-dark") -> str:
        """The owner starts a video in Create: the Dashboard's own route queues it and the Dashboard's own
        worker runs it. Only the job's work is a stand-in: it lasts until the test lets it end."""

        def work(ctl: Any) -> dict[str, Any]:
            self.job_may_end.wait(120)
            ctl.check_cancel()
            return {}

        self.dash_state.worker.handlers["run"] = work
        status, job = self.product.http.api("POST", "/api/jobs/run", {"channel": channel})
        assert status == 201, (status, job)
        self.job_thread = threading.Thread(target=self.dash_state.worker.run_next, daemon=True)
        self.job_thread.start()
        end = time.monotonic() + 30
        while self.job(job["job_id"])["status"] != "running":
            assert time.monotonic() < end, self.job(job["job_id"])
            time.sleep(0.05)
        return str(job["job_id"])

    def job(self, job_id: str) -> dict[str, Any]:
        status, body = self.product.http.api("GET", f"/api/jobs/{job_id}")
        assert status == 200, (status, body)
        return body

    def never_stopped(self) -> bool:
        """No Stop All and no shutdown reached the engine, and it is not stopping."""
        assert self.engine is not None
        asked = {("POST", "/stop_all"), ("POST", "/shutdown")} & set(self.engine.seen)
        return not asked and not self.engine.sup.shutting_down

    def said(self, start: str) -> str:
        """The one line of the last run that starts with `start` ('' when none does)."""
        return next((line for line in self.out if line.startswith(start)), "")

    def finish_clip(self, job_id: str) -> None:
        """What the Dashboard's worker records for a clip job: the studio job's QC report (as the clipper's QC
        stage writes it), its outputs row, a warning in the product's log, the job's end with its ids."""
        from ns_common.logging import NsLogger

        sid = f"clip-{job_id[-6:]}"
        if self.clip_status == "running":  # the worker never ends it (the kit's bound)
            return
        if self.clip_status == "done":
            report = self.home / "data" / "jobs" / sid / "qc" / "report.json"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text(json.dumps({"c1": {"passed": True, "reason": None}}), encoding="utf-8")
            Db(self.paths.db_path).execute(
                "insert into outputs (output_id, job_id, channel, kind, k, title, path, duration_s, "
                "created_at) "
                "values (?, ?, 'gpu-kit', 'clip', 1, 'Clip 1', ?, 41.0, ?)",
                (
                    f"{sid}/c1",
                    sid,
                    str(self.paths.outbox / "gpu-kit" / "clip1.mp4"),
                    datetime.now(UTC).isoformat(),
                ),
            )
            NsLogger("studio", self.paths.logs).warn("whisper_cpu", "captions on the CPU", code="NS-W404")
            self.dash_state.store.finish(
                job_id, "done", "Done", {"studio_job_ids": [sid], "output_ids": [f"{sid}/c1"]}
            )
        else:
            self.dash_state.store.finish(job_id, "failed", "NS-E404: every clip failed QC", None)

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
        for prefix, hook in list(self.say_hooks.items()):
            if line.startswith(prefix):
                del self.say_hooks[prefix]
                hook()
        if self.say_trap and line.startswith(self.say_trap):
            self.say_trap = None
            raise KeyboardInterrupt(line)

    def run(self, *extra: str) -> int:
        kit_here()
        self.out = []
        env = gpukit.KitEnv(
            clock=self.clock, product=self.product, windows=self.windows, say=self.say,
            ask=self.ask if self.stand_in_for_the_person else None,
        )  # fmt: skip
        try:
            return gpukit.main(["--home", str(self.home), *extra], env=env)
        except SystemExit as e:  # an option this kit does not know: argparse's own exit, never a kit code
            self.out.append(f"argparse exit {e.code}")
            return 1000 + int(e.code or 0)

    def last(self) -> dict[str, Any]:
        """The last line of the last run as the JSON object an unattended run ends with ({} when it is
        not one)."""
        try:
            got = json.loads(self.out[-1]) if self.out else {}
        except ValueError:
            return {}
        return got if isinstance(got, dict) else {}

    def night(self, started: datetime, *, as_task: bool = True) -> dict[str, Any]:
        """One night of the product (`daily.run_daily`, fake services, the stub plan) started at `started`."""
        clock = NightClock(started)
        env = jobs.open_env(frozenset(FAKEABLE))
        from ns_studio import cli as studio_cli

        kit = studio_cli._kit_nights(env, clock.now())  # what `studio run --daily` does around the night
        report = daily.run_daily(
            env,
            now=clock.now(),
            rng=random.Random(1),
            notifier=None,
            clock=clock,
            environ={} if not as_task else {"NS_NIGHT_STARTED": started.astimezone(UTC).isoformat()},
        )
        studio_cli._kit_nights_over(env, kit, clock.now())
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
    assert world.results() == {"K0": "PASS", "K1": "PASS", "K2": "PASS", "KS": "PASS", "K3": "PASS",
                               "KC": "PASS", "KV": "PASS", "KN": "NOT RUN", "KA": "PASS", "K4": "PASS",
                               "K5": "WAIT", "K6": "WAIT", "K7": "WAIT"}  # fmt: skip
    steps = world.state()["steps"]
    assert all(steps[k]["done"] and steps[k]["version"] == VERSION
               for k in ("K0", "K1", "K2", "KS", "K3", "KC", "KV", "KN", "KA", "K4"))  # fmt: skip
    assert world.out[-1] == "Run GPU-KIT.cmd again after Saturday 14 November 09:00."
    assert any("Friday 13 November 03:00 and Saturday 14 November 03:00" in line for line in world.out)
    machine = json.loads((world.ev / "machine.json").read_text(encoding="utf-8"))
    assert machine["gpu"] == {"name": "NVIDIA GeForce RTX 5060 Ti", "driver": "580.88", "cuda": "13.0",
                              "vram_total": "16311 MiB", "vram_used": "812 MiB"}  # fmt: skip
    assert machine["modern_standby"] is False and machine["fake_gpu_typed_on_a_real_install"] is True
    assert world.task.has == ("Ready", "03:00")  # the Settings switch registered the task
    assert world.channel()["status"] == "active"  # the two armed nights make its video
    change = world.state()["changes"]["channel_for_nights"]
    assert (change["was"], change["will_be"], change["put_back"]) == ("paused", "active", False)
    said = next(x for x in world.out if x.startswith("Each of the next two nights"))
    assert "makes one full story video on the kit's channel, about " in said and "night 1: After Dark" in said


# ------------------------------------------------------------------------------------------------ 2
INTERRUPTS = {  # step -> how the window is closed inside it
    "K0": ("trap", "api GET /api/status"),
    "K1": ("trap", "nvidia_smi"),
    "K2": ("trap", "prove"),
    "KS": ("trap", "api POST /api/library/footage/fetch"),
    "K3": ("trap", "studio running"),
    "KC": ("trap", "api POST /api/jobs/clip"),
    "KV": ("trap", "api POST /api/voices/clone"),  # after the designed voice was made: it is removed
    "KA": ("trap", "api POST /api/assistant/chat"),
    "K4": ("trap", "settings applied"),
    "K5": ("say", "Step 11 of 13 (night 1): starting."),
    "K6": ("say", "Step 12 of 13 (night 2): starting."),
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
        assert not list(world.paths.models_tts_voices.glob("gpukit-*"))  # a test voice never stays
    assert world.run() == 0, world.out
    assert world.results()["K4"] == "PASS"
    world.nights_pass()
    if late:
        assert world.run() == 130, world.out
        assert not world.state()["steps"][step]["done"]
    assert world.run() == 0, world.out
    assert {k: v for k, v in world.results().items() if v != "PASS"} == {"KN": "NOT RUN"}  # (no Code here)
    assert len(world.dash_state.store.list_jobs(None, "clip", 50)) == 1  # one clip job, whatever stopped it
    assert not list(world.paths.models_tts_voices.glob("gpukit-*")) and not list(
        world.paths.cache_clips.rglob("*.mp4")
    )
    # done once, whatever stopped it: one kit channel, one story job, one switch each way, one zip
    assert world.product.count("api", "POST", "/api/channels") == 1
    assert world.product.count("studio", "run", "--channel") == 1
    assert world.product.count("studio", "run", "--resume") == (1 if step == "K3" else 0)
    assert [p["automatic_runs"] for p in world.puts()] == [True, False]
    assert len(world.zips()) == 1 and world.out[-1].startswith("Send this one file: ")
    assert world.channel()["status"] == "paused" and world.task.has is None
    assert not (world.ev / "kit.lock").exists() and kitnights.read(world.paths, START).kind == "none"
    assert [s[:13] for s in (world.results()["K5"], world.results()["K6"])] == ["PASS", "PASS"]


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
    assert world.channel()["status"] == "active"  # armed
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
    assert not re.search(r"for (?!led\b)\w+ in (?:self\.ledger\(\)|lines\b)", source)  # one name for them
    read = set(re.findall(r'\bled\.get\("(\w+)"\)', source))
    assert read and read <= written, f"the kit reads ledger keys the product never writes: {read - written}"
    kinds = set(re.findall(r'led\.get\("kind"\) == "(\w+)"', source))
    assert kinds - {"slept", "decision"} <= {line["kind"] for line in lines}, (
        kinds
    )  # ("slept": written only after a sleep)
    clock_src = (ROOT / "ns_studio" / "nightclock.py").read_text(encoding="utf-8")
    assert '"slept"' in clock_src and 'self.write("decision"' in clock_src  # written only when they happen


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
    assert world.run() == 0 and world.channel()["status"] == "active"  # armed for the nights


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
                record = kitnights.read(w.paths, START)  # the one reader
                assert record.kind == "valid" and record.record["until"].startswith("2026-11-15T02:30")
                assert record.record["automatic_runs_before"] is (case == "on")
                if case == "on":
                    assert w.puts() == []
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
                assert kitnights.read(w.paths, START).kind == "none"
            finally:
                w.close()


@pytest.mark.parametrize("owners", [False, True])
def test_the_night_after_the_two_puts_back_first_and_makes_nothing_unless_the_runs_are_the_owners(
    world: World, owners: bool
) -> None:
    """The kit is never run again. Its runs (off before it, nobody chose them since): the night after the two
    switches them off and pauses the kit's channel BEFORE it plans, and makes nothing. The owner's runs (they
    chose them after the kit armed): the night runs their channels as usual, with no kit video."""
    assert world.run() == 0
    assert world.task.has == ("Ready", "03:00") and world.channel()["status"] == "active"  # armed
    now = datetime.now().astimezone()  # (the nightly reads the PC's clock)
    rec = kitnights.read(world.paths, START).record
    if owners:
        rec = {**rec, "owner_chose_at": now.isoformat(), "owner_route": "Settings"}
    kitnights.write(world.paths, {**rec, "until": (now - timedelta(hours=1)).isoformat(),
                                  "nights": [{**n, "from": (now - timedelta(days=3 - i)).isoformat(),
                                              "to": (now - timedelta(days=2 - i)).isoformat()}
                                             for i, n in enumerate(rec["nights"])]})  # fmt: skip
    res = CliRunner().invoke(_studio_app(), ["run", "--daily", "--fake-services"])
    assert res.exit_code == 0, res.output
    runs = Db(world.paths.db_path).query("select summary_json from runs")
    assert world.channel()["status"] == "paused" and kitnights.read(world.paths, now).kind == "none"
    if owners:
        assert len(runs) == 1 and "after-dark" in runs[0]["summary_json"]  # their night, as without a record
        assert '"channel": "gpu-kit"' not in runs[0]["summary_json"] and world.task.has == ("Ready", "03:00")
    else:
        assert kitnights.EXPIRED_NOTE in res.output and runs == [] and world.task.has is None  # nothing made
        notes = (world.home / "run" / "morning_note.json").read_text(encoding="utf-8")
        assert kitnights.EXPIRED_NOTE in notes


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
    assert world.state()["steps"]["K5"]["line"].startswith("Night 1: run daily-")
    assert "made the kit's video, job gpu-kit-" in world.state()["steps"]["K5"]["line"]
    assert "the night used " in world.state()["steps"]["K5"]["line"]
    assert world.out[-1] == "Run GPU-KIT.cmd again after Saturday 14 November 09:00."
    world.clock.t = local(2026, 11, 14, 9, 1)  # night 2 has no row after its run time + 6 h
    assert world.run() == 1  # a night that made nothing is a FAIL; the kit still collects
    assert world.results()["K6"] == "FAIL"
    assert "Night 2: did not run, so it made nothing" in world.state()["steps"]["K6"]["line"]
    assert (
        world.results()["K7"] == "FAIL"
        and "night 1 PASS (daily-20261113-" in world.state()["steps"]["K7"]["line"]
        and "night 2 FAIL (no run)" in world.state()["steps"]["K7"]["line"]
    )
    assert world.state()["nights"][1]["schedule"][0] == 0  # `ns schedule show`'s answer, kept
    with zipfile.ZipFile(world.zips()[0]) as z:
        names = set(z.namelist())
        assert "logs/nightly-20261113.json" in names and "morning-notes.json" in names
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
    n = z_path.stat().st_size  # Windows units (binary), a file under 1 MB in KB: never "(0.0 MB)" (G-33)
    size = f"{n / 1024:.1f} KB" if n < 1024**2 else f"{n / 1024**2:.1f} MB"
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
        "morning-notes.json",
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
    assert world.results() == {"K0": "PASS", "K1": "FAIL", **dict.fromkeys(gpukit.STEPS[2:], "NOT RUN")}
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
        f"url file:///{profile.replace(chr(92), '/')}/x and %2F{SECRET_USER} /{SECRET_USER.upper()}",
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
        assert {k: v for k, v in w.results().items() if v != "PASS"} == {"KN": "NOT RUN"}  # (no Code here)
        assert w.product.count("night_now") == 2 and w.puts() == []  # no task is armed
        mark = "simulated (fake_gpu)"
        assert w.out[1] == f"SIMULATED graphics card ({mark}): a test install; nothing measured here " \
            "is evidence of the GPU PC."  # fmt: skip
        assert all(w.state()["steps"][k]["line"].startswith(mark) for k in ("K3", "K5", "K6"))
        for k in ("K5", "K6"):  # each dry-run night MADE the kit's video (fake services)
            assert "made the kit's video, job gpu-kit-" in w.state()["steps"][k]["line"], w.state()["steps"][
                k
            ]
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
        r"profile C:\Users\Ahmed\Desktop, C:\USERS\AHMED~1\x, USERNAME=Ahmed on AHMED-PC\ahmed owner: Ahmed",
        "sha256 " + "f" * 64,
    ])  # fmt: skip
    out = gpukit.scrub_text(text, masks)
    for gone in (PEXELS, HF, "12345678-0123", "secret-value", "abc123", "abcdef&", "abcdefghijklmnop", "ghp_",
                 "S-1-5-21", "Nightshift", "nightshift", "Users", "Ahmed", "ahmed", "AHMED"):  # fmt: skip
        assert gone not in out, gone
    assert out.count("<home>") == 4 and out.count("<profile>") == 2
    assert "USERNAME=<user> on <pc>\\<user> owner: <user>" in out
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


# ------------------------------------------------------------------------------------------------ round 3
def test_a_night_that_makes_nothing_for_the_kit_is_a_fail_with_its_reason(world: World) -> None:
    assert world.run() == 0
    kitnights.record_file(world.paths).write_text("{torn", encoding="utf-8")  # the planner cannot read it
    world.nights_pass()
    assert world.run() == 1, world.out
    assert world.results()["K5"] == "FAIL" and world.results()["K6"] == "FAIL"
    line = world.state()["steps"]["K5"]["line"]
    assert "made nothing for the kit (the night could not read the kit's record of its nights: " in line, line
    assert world.results()["K7"] == "FAIL" and world.out[-1].startswith("Send this one file: ")
    notes = (world.home / "run" / "morning_note.json").read_text(encoding="utf-8")  # once, naming the file
    assert notes.count("The GPU kit's record of its two test nights cannot be read") == 1


def test_the_owners_own_routes_to_automatic_runs_are_theirs(world: World) -> None:
    from ns_supervisor import schedule

    assert world.run() == 0
    res = CliRunner().invoke(ns_app, ["schedule", "install"])  # the owner types it after the kit armed
    assert res.exit_code == 0, res.output
    rec = kitnights.read(world.paths, START).record
    assert rec["owner_route"] == "ns schedule install" and rec["owner_chose_at"]
    kitnights.write(world.paths, {k: v for k, v in rec.items() if not k.startswith("owner_")})
    schedule.setup_apply(
        world.paths, ROOT, first=True, nightly=True, seen=None, run_time="03:00"
    )  # INSTALL.cmd
    assert kitnights.read(world.paths, START).record["owner_route"] == "setup"
    kitnights.write(world.paths, {k: v for k, v in rec.items() if not k.startswith("owner_")})
    world.product.http.api("PUT", "/api/settings", {"automatic_runs": True})  # the Settings click
    assert kitnights.read(world.paths, START).record["owner_route"] == "Settings"
    world.product.http.api("PUT", "/api/settings", {"automatic_runs": True, "by": kitnights.BY_KIT})
    assert (
        kitnights.read(world.paths, START).record["owner_route"] == "Settings"
    )  # the kit's own: not counted


RECORDS = ("none", "valid night 1", "valid night 2", "past until", "torn", "no time zone", "newer version")
READERS = ("nightly", "planner", "Settings", "K7")


def _plant(world: World, kind: str, now: datetime) -> bytes | None:
    """The record of `kind`, relative to `now` (the PC's clock: the nightly reads it)."""
    path = kitnights.record_file(world.paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    if kind == "none":
        return None
    if kind == "torn":
        path.write_text('{"until": "2026', encoding="utf-8")
        return path.read_bytes()
    start = {"valid night 1": -1, "valid night 2": -25, "past until": -50}.get(kind, -1)
    lo = now + timedelta(hours=start)
    nights = [{"n": 1, "from": lo, "to": lo + timedelta(days=1)},
              {"n": 2, "from": lo + timedelta(days=1), "to": lo + timedelta(days=2)}]  # fmt: skip
    if kind == "no time zone":
        stamp = lambda d: d.replace(tzinfo=None).isoformat()  # noqa: E731
    else:
        stamp = lambda d: d.isoformat()  # noqa: E731
    record = {
        "until": stamp(lo + timedelta(days=2)),
        "automatic_runs_before": False,
        "run_time": "03:00",
        "nights": [{**n, "from": stamp(n["from"]), "to": stamp(n["to"]), "makes": "x"} for n in nights],
    }
    kitnights.write(world.paths, record)
    if kind == "newer version":
        data = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**data, "schema": 99}), encoding="utf-8")
    return path.read_bytes()


@pytest.mark.parametrize("reader", READERS)
@pytest.mark.parametrize("kind", RECORDS)
def test_the_one_reader_of_the_nights_record(world: World, kind: str, reader: str) -> None:
    """Each record state x each reader (round 3, B): the one reader never raises; unreadable or expired runs
    as if there were no record (expired then puts back); the kit's channel is planned on a valid night."""
    kit_here()
    now = datetime.now().astimezone()
    db = Db(world.paths.db_path)
    created = (datetime.now(UTC) - timedelta(weeks=11)).isoformat(timespec="seconds")
    save_channel(
        world.paths,
        make_channel(
            slug="gpu-kit", display_name="GPU kit (test video)", format="master_first", created_at=created
        ),
    )  # active
    world.product.http.api("PUT", "/api/settings", {"automatic_runs": True, "by": kitnights.BY_KIT})
    planted = _plant(world, kind, now)
    expected = {"none": "none", "valid night 1": "valid", "valid night 2": "valid", "past until": "expired"}
    result = kitnights.read(world.paths, now)
    assert result.kind == expected.get(kind, "unreadable"), result
    assert result.night == {"valid night 1": 1, "valid night 2": 2}.get(kind)
    valid = kind.startswith("valid")
    if reader == "nightly":  # the owner's ordinary night, with whatever record is there
        res = CliRunner().invoke(_studio_app(), ["run", "--daily", "--fake-services"])
        assert res.exit_code == 0, res.output
        rows = db.query("select summary_json from runs")
        if kind == "past until":  # the kit's runs: put back BEFORE the night, which then makes nothing
            assert rows == [] and world.task.has is None and world.channel()["status"] == "paused"
            assert not kitnights.record_file(world.paths).exists()
        else:
            summary = rows[0]["summary_json"]
            assert "after-dark" in summary  # the owner's channel ran
            assert ('"channel": "gpu-kit"' in summary) is valid  # the kit's video only on an armed night
            assert world.task.has == ("Ready", "03:00")
        if planted is not None and not valid and kind != "past until":
            assert kitnights.record_file(world.paths).read_bytes() == planted  # unreadable: left as it is
    elif reader == "planner":
        planned = [j.channel.slug for j in cadence.plan_daily(world.paths, db, now.astimezone(UTC))]
        assert ("gpu-kit" in planned) is valid and "after-dark" in planned
    elif reader == "Settings":
        status, body = world.product.http.api("PUT", "/api/settings", {"automatic_runs": True})
        assert status == 200 and body["schedule_registered"] is True
        after = kitnights.read(world.paths, now)
        if result.armed:
            assert after.record.get("owner_route") == "Settings"
        elif planted is not None:
            assert kitnights.record_file(world.paths).read_bytes() == planted
        else:
            assert not kitnights.record_file(world.paths).exists()
    else:  # K7: the kit's own collect step reads it through the same reader, never raises, puts back
        world.clock.t = now
        args = gpukit.build_parser().parse_args(["--home", str(world.home)])
        env = gpukit.KitEnv(clock=world.clock, product=world.product, windows=world.windows, ask=world.ask,
                            say=world.say)  # fmt: skip
        kit = gpukit.start(args, env)
        try:
            kit.state.update({"turned_on": True, "automatic_runs_before": False, "nights": []})
            res7 = kit.k7()
        finally:
            kit.close()
        assert kit.state["nights_record_at_collect"]["kind"] == result.kind
        assert "Automatic runs are off again" in res7.line and world.task.has is None
        if result.kind == "unreadable":
            assert "the nights record could not be read" in res7.line
        assert not kitnights.record_file(world.paths).exists()


# ------------------------------------------------------------------------------------------------ additions
def test_each_stored_stock_key_is_tried_through_the_products_route_before_the_video(world: World) -> None:
    assert world.run() == 0, world.out
    line = world.state()["steps"]["KS"]["line"]
    assert line.startswith("Pixabay: searched and downloaded one clip (5.3 KB, pixabay 701, 1920x1080)"), line
    assert "Pexels: NOT RUN (no Pexels key is stored)" in line
    stock = json.loads((world.ev / "stock.json").read_text(encoding="utf-8"))
    assert stock["pixabay"]["size_bytes"] == 5382 and stock["pixabay"]["source"] == "pixabay"
    calls = [
        c for c in world.product.calls if c[:2] == ("api", "POST") and c[2] == "/api/library/footage/fetch"
    ]
    assert [c[3]["count"] for c in calls] == [1] and calls[0][3]["source"] == "pixabay"
    # the clip left the cache again (the product's own delete route), recorded as put back
    assert world.product.count("api", "DELETE", "/api/library/footage/pixabay/701") == 1
    assert not list(world.paths.cache_clips.rglob("*.mp4"))
    assert world.state()["changes"]["stock_clip_pixabay"]["put_back"] is True
    steps = [c for c in world.product.calls if c[0] in ("studio", "api") and c[1:3] in (
        ("POST", "/api/library/footage/fetch"), ("run", "--channel"))]  # fmt: skip
    assert steps[0][2] == "/api/library/footage/fetch"  # before the story video


@pytest.mark.parametrize(("refusal", "says"), [
    (("NS-E502", "HTTP 400"), "Pixabay did not accept the key. What to do: copy the key again"),
    (("NS-E502", "rate limited (429) after 4 attempts"), "the rate limit. What to do: wait an hour"),
    (("NS-E503", "pixabay.com is not answering right now; Nightshift will try again (HTTP 503 after 3 "
                 "attempts)"), "Pixabay or the connection did not answer; this is NOT the key"),
])  # fmt: skip
def test_a_refused_key_says_why_and_the_video_does_not_start(
    world: World, refusal: tuple[str, str], says: str
) -> None:
    world.footage["pixabay"].refuse = NsError(*refusal)
    assert world.run() == 1, world.out
    assert world.results()["KS"] == "FAIL" and not world.state()["steps"]["KS"]["done"]
    assert says in world.state()["steps"]["KS"]["line"], world.state()["steps"]["KS"]["line"]
    assert world.results()["K3"] == "NOT RUN" and world.product.count("studio") == 0
    world.footage["pixabay"].refuse = None  # the owner fixed it: the next run tries again, then the video
    assert world.run() == 0 and world.results()["KS"] == "PASS" and world.results()["K3"] == "PASS"


def test_no_key_stored_is_not_run_and_the_video_goes_on(world: World) -> None:
    world.dash_state.adapters.keyring.delete("pixabay_api_key")
    assert world.run() == 0, world.out
    assert world.results()["KS"] == "NOT RUN" and world.results()["K3"] == "PASS"
    assert "no stock-footage key is stored" in world.state()["steps"]["KS"]["line"]


def test_prove_all_never_selects_the_footage_proofs_on_a_real_install(world: World) -> None:
    """Where the exit-3 claim came from: `prove.py --all` on a DEV home selects f-b13-b14 (install "dev"),
    which skips when a real key is stored. On this real (gpu) install `--all` leaves it out quietly
    (prove.py: a wrong kind is not selected), so the kit never sees that skip: no branch for it."""
    import contextlib
    import io
    import shutil

    from dev import prove

    folder = world.home.parent / "only-footage-proof"
    folder.mkdir()
    (folder / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy(ROOT / "proofs" / "f_b13_b14_footage_429.py", folder / "f_b13_b14_footage_429.py")
    if (ROOT / "proofs" / "_lib").is_dir():
        shutil.copytree(ROOT / "proofs" / "_lib", folder / "_lib")
    lock = world.home.parent / "kit.lock"  # the lock the kit itself holds when it runs prove.py
    lock.write_text(f"kit | GPU kit pid 1 | {datetime.now().astimezone().isoformat()}\n", encoding="utf-8")
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        rc = prove.main(["--home", str(world.home), "--dept", "kit", "--all", "--strict", "--no-window",
                         "--lock-file", str(lock),
                         "--runs", str(world.home.parent / "runs"), "--proofs-dir", str(folder)],
                        runner_options={"commit": "test", "ps": nothing_runs})  # fmt: skip
    said = out.getvalue()  # nothing selected, so nothing skipped: --strict does not exit 3
    assert rc == 0 and "0 passed, 0 failed, 0 skipped" in said and "f-b13" not in said, said


def test_a_skip_of_a_selected_proof_fails_k2(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    output = ("SKIPPED kit-reads-status [F-X99] 0.1s  no FFmpeg\n"
              "0 passed, 0 failed, 1 skipped; evidence in x\n"
              "1 skipped: a skip is not a pass (--strict). Skipped, and why:\n")  # fmt: skip
    monkeypatch.setattr(world.product, "prove", lambda args, timeout: (3, output))
    assert world.run() == 1, world.out
    assert world.results()["K2"] == "FAIL"


def test_the_stored_values_are_masked_whatever_their_shape(world: World) -> None:
    """The scrubber reads the ACTUAL stored values when it writes the zip: a key of each provider's real
    shape (no pattern matches it), planted where no named-value rule sees it, in every encoding."""
    import base64
    from urllib.parse import quote

    from ns_common.secret_file import store_secret

    kit_here()
    store_secret(world.paths, "pixabay_api_key", PIXABAY_REAL, lock_down=lambda _p: None)  # DPAPI file
    (world.paths.config / "answers.json").write_text(json.dumps({"pexels_api_key": PEXELS_REAL}), "utf-8")
    assert set(gpukit.stored_secrets(world.home)) == {PIXABAY_REAL, PEXELS_REAL}
    for raw in (PIXABAY_REAL, PEXELS_REAL):  # no shape rule would have caught them
        assert (
            gpukit.scrub_text(f"clip from {raw} failed", gpukit.Masks("", "", "", ""))
            != "clip from *** failed"
        )
    planted = "\n".join([
        f"clip from {PIXABAY_REAL} failed; pexels said {PEXELS_REAL}",
        json.dumps({"seen": [PIXABAY_REAL.upper(), quote(PEXELS_REAL)]}),
        base64.b64encode(PIXABAY_REAL.encode()).decode(),
        base64.b64encode(PEXELS_REAL.encode("utf-16-le")).decode(),
    ])  # fmt: skip
    (world.home / "logs" / "studio-20261113.jsonl").write_text(planted + "\n", encoding="utf-8")
    (world.home / "logs" / "launch-20261113.jsonl").write_text(PEXELS_REAL + "\r\n", encoding="utf-16")
    assert world.run() == 0
    world.nights_pass()
    assert world.run() == 0
    entries = _all_bytes(world.zips()[0])
    planted_in = [n for n, data in entries if b"clip from *** failed; pexels said ***" in data]
    assert planted_in  # the planted log is in the zip, masked
    for name, data in entries:
        if data[:2] == b"PK" or name.endswith(".wav"):
            continue
        for s in (PIXABAY_REAL, PEXELS_REAL):
            forms = [*_encodings(s), base64.b64encode(s.encode()), base64.b64encode(s.encode("utf-16-le"))]
            for form in forms:
                assert form not in data, (name, form)
    readme_bytes = next(d for n, d in entries if n.endswith("README.txt") and "diagnostics" not in n)
    readme = readme_bytes.decode("utf-8")
    assert "stored on this PC (read from Credential Manager, the key file and answers.json" in readme


def test_the_day_one_features_each_have_their_own_result(world: World) -> None:
    assert world.run() == 0, world.out
    steps = world.state()["steps"]
    clip = steps["KC"]["line"]
    assert re.search(
        r"clip job \S+ \(studio job clip-[\w-]+\) cut 3 minutes of the story video into 1 clip\(s\), "
        r"QC 1 of 1 passed",
        clip,
    ), clip
    assert "logged since it was queued: NS-W404 (whisper_cpu)" in clip and "left on the PC: the clip " in clip
    assert world.product.count("cut", "The House That Waited.mp4", 60, 180, False) == 1
    voices = steps["KV"]["line"]
    assert (
        voices.startswith("design: PASS (gpukit-design-")
        and "clone: PASS (gpukit-clone-" in voices
        and "spoke the sentence: " in voices
    )
    assert voices.endswith("both test voices removed") and not list(
        world.paths.models_tts_voices.glob("gpukit-*")
    )
    body = next(c[3] for c in world.product.calls if c[:3] == ("api", "POST", "/api/voices/clone"))
    assert body["consent"] is True and body["sample_path"].endswith("sample.wav")  # the kit's own narration
    assert (world.ev / "voices" / "design-preview.wav").is_file()
    assert steps["KN"]["line"].startswith("Nightshift Code is not installed on this PC")
    assert re.match(
        r'conversation \S+ answered in 0 min 0 s: "The Outbox holds your finished videos', steps["KA"]["line"]
    ), steps["KA"]["line"]
    asked = json.loads((world.ev / "assistant.json").read_text(encoding="utf-8"))
    assert "confirm" not in asked["events"] and asked["events"][-1] == "done"


def test_a_clip_job_that_fails_is_a_fail_and_the_kit_goes_on(world: World) -> None:
    world.clip_status = "failed"
    assert world.run() == 1, world.out
    assert world.results()["KC"] == "FAIL" and world.results()["K4"] == "PASS"
    assert re.search(
        r"the clip job \S+ ended failed: NS-E404: every clip failed QC", world.state()["steps"]["KC"]["line"]
    )


@pytest.mark.parametrize("frees", [True, False])
def test_nightshift_code_is_opened_prompted_closed_and_frees_the_card(world: World, frees: bool) -> None:
    exe = world.home / "bin" / "opencode" / "opencode.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ")
    world.frees_card = frees
    assert world.run() == (0 if frees else 1), world.out
    line = world.state()["steps"]["KN"]["line"]
    assert world.results()["KN"] == ("PASS" if frees else "FAIL"), line
    assert world.product.count("start_code", "nightshift-code.ps1") == 1
    assert world.product.count("opencode_prompt") == 1 and world.product.count("kill_tree") >= 1
    assert "the prompt was answered" in line
    assert ("the card was freed in 0.0 s (stated: within 5 s)" in line) is frees
    code = json.loads((world.ev / "code" / "code.json").read_text(encoding="utf-8"))
    released = 0 if frees else 16  # (not freed: read after the 15 s wait)
    assert code["vram_before"] == 812 and code["memory_released_s"] == released
    assert code["freed_s"] == (0.0 if frees else None)
    change = world.state()["changes"]["code_window"]
    assert world.code is not None
    assert change["put_back"] is True and change["pid"] == world.code.pid  # closed by its recorded pid
    world.supervisor.state.update({"gpu_owner": None, "services": {}})


def test_a_code_window_left_by_a_closed_kit_is_closed_on_the_next_run(world: World) -> None:
    exe = world.home / "bin" / "opencode" / "opencode.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ")
    world.traps.armed.add("api POST /api/assistant/chat")  # closed after the Code check ended
    assert world.run() == 130
    assert world.state()["changes"]["code_window"]["put_back"] is True
    first = world.code
    assert first is not None and first.rc is not None  # its window was closed on the way out


# ------------------------------------------------------------------------------------------------ round 4
def _kit_combos(world: World) -> list[str]:
    return [r["job_id"] for r in Db(world.paths.db_path).query(
        "select distinct job_id from combos where channel = 'gpu-kit' order by created_at")]  # fmt: skip


def test_a_night_whose_time_rule_stops_the_kit_video_passes_with_the_summarys_own_reason(
    world: World,
) -> None:
    assert world.run() == 0
    world.out_of_time_at = "kit_qc"  # night 1: the night's time rule stops the kit's video at a stage
    world.night(local(2026, 11, 13, 3, 0))
    world.out_of_time_at = None  # night 2 continues that job (and makes no second one)
    world.night(local(2026, 11, 14, 3, 0))
    world.clock.t = local(2026, 11, 14, 9, 30)
    assert world.run() == 0, world.out
    one, two = world.state()["steps"]["K5"]["line"], world.state()["steps"]["K6"]["line"]
    assert (
        world.results()["K5"] == "PASS"
        and "time rule did not finish the kit's video (stop, stop_no_room" in one
    )
    assert world.results()["K6"] == "PASS" and "(continued from the night before)" in two, two
    nightly = [j for j in _kit_combos(world) if j != world.state()["job"]["id"]]
    assert len(nightly) == 1  # night 2 resumed night 1's kit job: one kit video, never two


def test_an_old_decision_of_another_night_never_passes_a_night_that_made_nothing(world: World) -> None:
    from ns_studio import ledger

    assert world.run() == 0
    old = datetime(2026, 11, 1, 2, 0, tzinfo=UTC)  # a decision line of an older night, no job id, no channel
    line = {"kind": "decision", "at": old.isoformat(), "run_id": "daily-old", "job_id": None,
            "answer": "stop", "reason": "new_no_room", "where": "new"}  # fmt: skip
    ledger.write(world.paths, line, when=old)
    kitnights.record_file(world.paths).write_text(
        "{torn", encoding="utf-8"
    )  # the kit's video is never planned
    world.nights_pass()
    assert world.run() == 1
    assert (
        world.results()["K5"] == "FAIL" and "made nothing for the kit" in world.state()["steps"]["K5"]["line"]
    )


def test_a_night_that_knew_no_time_limit_is_a_fail(world: World) -> None:
    assert world.run() == 0
    world.night(local(2026, 11, 13, 3, 0), as_task=False)  # run by hand: no task start, no deadline
    world.night(local(2026, 11, 14, 3, 0))
    world.clock.t = local(2026, 11, 14, 9, 30)
    assert world.run() == 1
    assert world.results()["K5"] == "FAIL" and world.results()["K6"] == "PASS"
    assert "it knew no time limit" in world.state()["steps"]["K5"]["line"]


def test_once_a_night_two_runs_in_one_armed_night_make_one_kit_video(world: World) -> None:
    assert world.run() == 0
    world.night(local(2026, 11, 13, 3, 0))
    world.night(local(2026, 11, 13, 4, 0))  # `studio run --daily` again, inside the same armed night
    nightly = [j for j in _kit_combos(world) if j != world.state()["job"]["id"]]
    assert len(nightly) == 1


def test_the_owners_open_code_window_is_never_touched(world: World) -> None:
    exe = world.home / "bin" / "opencode" / "opencode.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ")

    def owner_opens_code() -> None:  # the owner opened Nightshift Code before the kit's Code step
        world.supervisor.state.update({"gpu_owner": "opencode", "services": {"llama": {"state": "ready"}}})

    world.say_hooks["Step 8 of 13 (Nightshift Code"] = owner_opens_code
    world.run()
    line = world.state()["steps"]["KN"]["line"]
    assert world.results()["KN"] == "NOT RUN" and line.startswith("Nightshift Code is open: close it and run "
                                                                  "the kit again"), line  # fmt: skip
    assert world.product.count("start_code") == 0 and world.product.count("kill_tree") == 0
    assert world.supervisor.state["gpu_owner"] == "opencode"  # the owner's session holds the card still
    assert not world.state()["steps"]["KN"]["done"]  # asked again on the next run
    world.supervisor.state.update({"gpu_owner": None, "services": {}})


def test_a_kill_mid_download_leaves_no_stock_clip_in_the_cache(world: World) -> None:
    world.traps.armed.add("after api POST /api/library/footage/fetch")  # downloaded; the pointer not saved
    assert world.run() == 130
    change = world.state()["changes"]["stock_clip_pixabay"]
    assert change["put_back"] is True and change["removed_after_kill"] == ["701"]
    assert not list(world.paths.cache_clips.rglob("*.mp4"))
    assert world.run() == 0 and world.results()["KS"] == "PASS"


def test_a_kill_after_the_clip_post_makes_no_second_clip_job(world: World) -> None:
    world.traps.armed.add("after api POST /api/jobs/clip")  # posted; the job id not saved
    assert world.run() == 130
    assert "job_id" not in world.state()["clip"]
    assert world.run() == 0, world.out
    assert (
        len(world.dash_state.store.list_jobs(None, "clip", 50)) == 1 and world.state()["clip"]["found_again"]
    )
    assert world.results()["KC"] == "PASS"


def test_a_clip_job_past_its_bound_is_cancelled(world: World) -> None:
    world.clip_status = "running"
    assert world.run() == 1
    line = world.state()["steps"]["KC"]["line"]
    assert world.results()["KC"] == "FAIL" and "cancel asked (HTTP 200)" in line, line
    jid = world.state()["clip"]["job_id"]
    assert world.product.count("api", "POST", f"/api/jobs/{jid}/cancel") == 1


# ------------------------------------------------------------------------------------------------ round 5
def _label(step: str) -> str:
    return f"Step {gpukit.STEPS.index(step) + 1} of {len(gpukit.STEPS)} ({gpukit.TITLES[step]}): starting."


@pytest.mark.parametrize("step", ["K1", "K2", "KS", "K3", "KC", "KV", "KN", "KA", "K4", "K5", "K6", "K7"])
def test_a_run_that_starts_cold_at_each_step_brings_the_engine_up_first(world: World, step: str) -> None:
    """The state says the steps before `step` are done; the engine and the Dashboard are stopped (Stop All
    between the runs). The run's first step goes through the ONE entry: it runs, or gives its true NOT RUN,
    never 'the Dashboard is down'."""
    kit_here()
    if step in ("K5", "K6", "K7"):
        assert world.run() == 0
        world.nights_pass()
    world.say_trap = _label(step)
    assert world.run() == 130, world.out[-5:]
    assert not world.state()["steps"].get(step, {}).get("done")
    world.dashboard_down = True
    before = world.product.count("start_engine")
    world.run()
    rec = world.state()["steps"][step]
    for down in ("did not answer", "did not say who holds", "did not start"):
        assert down not in rec["line"], (step, rec["line"])
    if gpukit.ENTRY[step] != "none":
        assert world.product.count("start_engine") > before  # the entry brought it up first
    if step == "KN":
        assert rec["result"] == "NOT RUN" and rec["line"].startswith("Nightshift Code is not installed")
    else:
        assert rec["result"] == "PASS", (step, rec)


def test_a_saved_clip_job_is_followed_after_a_cold_start_not_polled_dead(world: World) -> None:
    world.clip_status = "running"  # the worker has not ended it yet
    world.say_trap = "  the clip job: "  # the window closed while the kit followed the job
    assert world.run() == 130
    jid = world.state()["clip"]["job_id"]
    world.clip_status = "done"
    world.finish_clip(jid)  # the worker finished it while the kit was closed
    world.dashboard_down = True  # and the engine was stopped
    assert world.run() == 0, world.out
    assert world.results()["KC"] == "PASS" and jid in world.state()["steps"]["KC"]["line"]
    assert len(world.dash_state.store.list_jobs(None, "clip", 50)) == 1


def test_an_unanswered_cancel_never_says_cancelled(world: World) -> None:
    world.clip_status = "running"
    world.refuse_cancel = True
    assert world.run() == 1
    line = world.state()["steps"]["KC"]["line"]
    said = ("the cancel request was not answered (HTTP None): the job may still be running: open "
            "Nightshift's Dashboard, Queue, and press Cancel on clip job")  # fmt: skip
    assert said in line, line
    assert "cancel asked" not in line


# ------------------------------------------------------------------------------------------------ round 6
def test_an_unfinished_update_is_the_products_own_refusal_and_nothing_is_started(world: World) -> None:
    """The product's start gate (`ns_supervisor.client.start_gate`): a repair record that waits (here one
    that cannot be read, which the launch script treats as present) refuses every start with NS-W210. The
    first step that needs the engine is NOT RUN with the product's sentence and hint; nothing is started."""
    from ns_common.errors import describe_shown

    (world.home / "run" / "venv-repair.json").write_text("{torn", encoding="utf-8")
    world.dashboard_down = True
    assert world.run() == 0, world.out
    rec = world.state()["steps"]["K2"]
    hint = describe_shown("NS-W210", world.home).hint
    assert rec["result"] == "NOT RUN" and not rec["done"], rec
    assert (
        rec["line"].startswith("NS-W210: ")
        and f"What to do: {hint} Then run GPU-KIT.cmd again." in rec["line"]
    )
    assert "Nightshift was not started: " in rec["line"]
    assert world.product.count("start_engine") == 0 and world.task.has is None
    assert "engine" not in world.state()  # no start was asked for: nothing of Nightshift is the kit's


def test_a_coded_start_failure_shows_that_codes_hint(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    from ns_common.errors import describe_shown

    def refuses(timeout: float) -> tuple[bool, str]:
        raise NsError("NS-E202", "the engine did not answer within 30 s")

    monkeypatch.setattr(world.product, "start_engine", refuses)
    world.dashboard_down = True
    assert world.run() == 1
    rec = world.state()["steps"]["K2"]
    assert rec["result"] == "FAIL" and rec["line"].startswith("NS-E202: "), rec
    assert f"What to do: {describe_shown('NS-E202', world.home).hint}" in rec["line"]


def test_a_job_route_that_always_times_out_still_ends_by_time(world: World) -> None:
    world.clip_status = "running"
    world.jobs_route_times_out = True
    assert world.run() == 1
    line = world.state()["steps"]["KC"]["line"]
    assert (
        world.results()["KC"] == "FAIL"
        and "did not finish in 1 h 30 min" in line
        and "cancel asked (HTTP 200)" in line
    )


# ------------------------------------------------------------------------------------------------ round 7
# THE KIT ENDS ONLY WHAT THE KIT STARTED, AND ONLY WHILE NOTHING ELSE DEPENDS ON IT (review of 8913c4e, G).
# Nightshift is not running when the kit starts, so the kit starts it: the product's real engine (`Engine`).
# What its owner then does goes through the product's own routes: Nightshift Code is the launcher's /acquire
# with a live pid, a video is POST /api/jobs/run run by the Dashboard's own worker, a restart is the engine's
# own end and a new engine with a new start record.
LAST_STEP = f"{'WAIT':7} Step 13 of 13"  # the line of the last step: after it the run only puts back and ends
CODE_OPEN = "Nightshift was left running because Nightshift Code is open"


@pytest.fixture
def nightshift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A home where Nightshift is not running. The evening is late, so step 5 says WAIT and a run ends
    after its first steps (the engine is needed, and started, at step 3)."""
    w = World(tmp_path, monkeypatch, real_engine=True)
    w.clock.t = datetime(2026, 11, 12, 23, 30, 1).astimezone()
    try:
        yield w
    finally:
        w.close()


def test_the_kits_own_engine_is_stopped_when_nothing_else_uses_it(nightshift: World) -> None:
    w = nightshift
    assert w.run() == 0, w.out
    eng = w.engine
    assert eng is not None and w.product.count("start_engine") == 1  # the kit started Nightshift
    assert ("POST", "/shutdown") in eng.seen and eng.sup.exit_requested.is_set()  # and ended that engine
    assert ("POST", "/stop_all") not in eng.seen  # by asking that one engine: never Stop All
    assert not (w.home / "run" / "supervisor.json").exists()
    line = "Nightshift was stopped again: the kit had started it and nothing else was using it."
    assert line in w.out and w.out.index(line) < w.out.index(w.said("The zip so far: "))
    assert w.state()["engine"] is None and w.state()["engine_end"]["result"] == "stopped"
    readme = zipfile.ZipFile(w.zips()[0]).read("README.txt").decode("utf-8")
    assert f"Nightshift itself at the end of the kit's last run: {line}" in readme


def test_the_owners_code_survives_a_kit_run_that_ends_normally(nightshift: World) -> None:
    w = nightshift
    w.say_hooks[LAST_STEP] = w.owner_opens_code  # every step has had its line; then the owner opens Code
    assert w.run() == 0, w.out
    assert w.product.count("start_engine") == 1 and w.engine is not None  # (the kit had started Nightshift)
    assert w.holder() == ("opencode", os.getpid())  # the owner's Code holds the card still
    assert w.engine.sup.services["llama"].state == "ready" and w.never_stopped()  # and its model is loaded
    assert w.said(CODE_OPEN), w.out[-6:]
    assert w.state()["engine"] is None  # the kit's claim on that engine is used once
    w.owner_closes_code()
    assert w.run() == 0 and w.never_stopped()  # nothing uses it now: it is still not the kit's to stop
    assert not w.said("Nightshift was")  # and the kit, which started nothing this time, says nothing of it


def test_the_owners_code_survives_a_kit_run_that_ends_on_not_run(nightshift: World) -> None:
    w = nightshift
    w.after_engine_start = w.owner_opens_code  # the kit started Nightshift for step 3; the owner opens Code
    assert w.run() == 0, w.out
    rec = w.state()["steps"]["K2"]
    assert rec["result"] == "NOT RUN" and rec["line"].endswith("(nothing was started or ended)"), rec
    assert ("cli", "ns", "verify") not in w.product.calls  # the model test was not started
    assert w.holder() == ("opencode", os.getpid()) and w.never_stopped()
    assert w.said(CODE_OPEN), w.out[-6:]


def test_the_owners_code_survives_a_kit_run_that_is_killed_and_run_again(nightshift: World) -> None:
    w = nightshift
    w.traps.kills.add("ns verify")  # the kit started Nightshift; inside step 3 its process is ended
    with pytest.raises(Killed):
        w.run()
    assert w.product.count("start_engine") == 1 and w.engine is not None
    w.owner_opens_code()  # the owner uses the Nightshift that is running
    assert w.run() == 0, w.out
    assert w.results()["K2"] == "NOT RUN" and w.product.count("start_engine") == 1
    assert w.holder() == ("opencode", os.getpid()) and w.never_stopped()  # (it IS the engine the kit started)
    assert w.said(CODE_OPEN), w.out[-6:]
    w.owner_closes_code()
    assert w.run() == 0 and w.never_stopped()  # a third run, nobody using it: left to itself all the same


def test_a_job_the_owner_started_while_the_kit_ran_is_not_cancelled_by_the_kits_exit(
    nightshift: World,
) -> None:
    w = nightshift
    started: list[str] = []
    w.say_hooks[LAST_STEP] = lambda: started.append(w.owner_starts_job())
    assert w.run() == 0, w.out
    job = w.job(started[0])
    assert job["status"] == "running" and not job["cancel_requested"], job
    assert w.never_stopped()
    assert w.said(f"Nightshift was left running because the run job {started[0]} is running"), w.out[-6:]
    w.job_may_end.set()
    assert w.job_thread is not None
    w.job_thread.join(30)
    assert w.job(started[0])["status"] == "done"  # it ran to its own end


def test_an_engine_the_owner_restarted_after_a_killed_kit_run_is_not_stopped_by_the_next_run(
    nightshift: World,
) -> None:
    w = nightshift
    w.traps.kills.add("ns verify")
    with pytest.raises(Killed):
        w.run()
    assert w.engine is not None and w.product.count("start_engine") == 1
    first = w.engine.started_at
    w.stop_nightshift()  # the owner ends Nightshift ...
    while datetime.now().isoformat(timespec="seconds") == first:  # (the start record is to the second)
        time.sleep(0.05)
    w.start_nightshift()  # ... and starts it again: an engine with its own start record, used by nobody now
    assert w.run() == 0, w.out
    assert w.product.count("start_engine") == 1 and w.never_stopped()
    assert w.said("Nightshift was left running: it was started again after the kit had started it")
    assert w.state()["engine"] is None


def test_the_model_test_does_not_start_while_a_job_of_the_owner_runs(nightshift: World) -> None:
    """Step 3's `ns verify` stops every model twice by the product's own design: the kit does not start it
    while the product's own tables show anybody else at work, card held or not."""
    w = nightshift
    started: list[str] = []
    w.after_engine_start = lambda: started.append(w.owner_starts_job())  # (in a stage that holds no model)
    assert w.run() == 0, w.out
    rec = w.state()["steps"]["K2"]
    assert ("cli", "ns", "verify") not in w.product.calls, rec  # the model test was not started
    assert rec["result"] == "NOT RUN" and not rec["done"]
    said = (f"the run job {started[0]} is running: the model test stops and reloads every model, so it "
            "was not started (nothing was started or ended)")  # fmt: skip
    assert rec["line"].startswith(said), rec
    assert w.job(started[0])["status"] == "running" and w.never_stopped()


def test_a_night_still_at_work_is_waited_for_and_nothing_is_switched_off_under_it(world: World) -> None:
    """Night 2 was started late by Windows (the PC slept at its run time) and is in a stage when the kit is
    run after 'run time + 6 h'. The product's own owner table says a live process holds that run: the kit
    waits, and neither judges it 'never closed' nor removes the task Windows is running."""
    assert world.run() == 0  # armed: Automatic runs were off before, the kit switched them on
    world.night(local(2026, 11, 13, 3, 0))
    world.hold_at = "kit_story"
    night2 = threading.Thread(target=world.night, args=(local(2026, 11, 14, 8, 55),), daemon=True)
    night2.start()
    try:
        assert world.stage_held.wait(60)
        world.clock.t = local(2026, 11, 14, 9, 30)
        code = world.run()
        k6 = world.state()["steps"]["K6"]
        assert k6["result"] == "WAIT" and "is working now" in k6["line"], k6
        assert code == 0 and world.results()["K7"] == "WAIT"
        assert world.task.has == ("Ready", "03:00") and world.channel()["status"] == "active"
        assert kitnights.read(world.paths, world.clock.now()).kind == "valid"
    finally:
        world.hold_at = None
        world.stage_may_go.set()
        night2.join(120)
    assert world.run() == 0, world.out  # the night has ended: judged from its own closed record
    assert world.results()["K6"] == "PASS" and world.results()["K7"] == "PASS" and world.task.has is None


def test_automatic_runs_the_night_after_settled_as_the_owners_are_not_switched_off(world: World) -> None:
    """The owner switched Automatic runs on themselves after the kit had; the kit was not run until after
    the night that follows the two. That night settles the kit's record as the product does (the runs stay,
    as the owner's; the record goes). The kit's own 'I switched them on' is then evidence of nothing."""
    assert world.run() == 0
    world.product.http.api("PUT", "/api/settings", {"automatic_runs": True})  # the owner's own route
    world.nights_pass()
    now = datetime.now().astimezone()  # (the nightly reads the PC's clock)
    rec = kitnights.read(world.paths, START).record
    assert rec["owner_chose_at"]
    kitnights.write(world.paths, {**rec, "until": (now - timedelta(hours=1)).isoformat(),
                                  "nights": [{**n, "from": (now - timedelta(days=3 - i)).isoformat(),
                                              "to": (now - timedelta(days=2 - i)).isoformat()}
                                             for i, n in enumerate(rec["nights"])]})  # fmt: skip
    assert CliRunner().invoke(_studio_app(), ["run", "--daily", "--fake-services"]).exit_code == 0
    assert kitnights.read(world.paths, now).kind == "none" and world.task.has == ("Ready", "03:00")
    assert world.run() == 0, world.out
    assert world.task.has == ("Ready", "03:00")  # still on: they are the owner's
    assert [p["automatic_runs"] for p in world.puts() if p.get("by")] == [True]  # the kit's one switch
    assert "Automatic runs were left on: the night after the kit's two already put back what the kit" in (
        world.state()["steps"]["K7"]["line"]
    )


def test_a_clip_that_came_for_the_same_words_later_is_not_the_kits_to_remove(world: World) -> None:
    """The kit's process is ended during its download (the clip is in the cache, its id not yet saved).
    Later the owner's own fetch for the same words brings another clip. The next run removes the ONE clip
    of the kit's own request, not everything that is new for those words."""
    world.traps.kills.add("after api POST /api/library/footage/fetch")
    with pytest.raises(Killed):
        world.run()
    ask = {"q": gpukit.STOCK_QUERIES[0], "orientation": "landscape", "count": 1, "source": "pixabay"}
    status, body = world.product.http.api("POST", "/api/library/footage/fetch", ask)  # the Library's route
    assert status == 200 and [(c["clip_id"], c["new"]) for c in body["clips"]] == [("702", True)]
    assert world.run() == 0, world.out
    status, rows = world.product.http.api("GET", "/api/library/footage?limit=200")
    assert status == 200 and [r["clip_id"] for r in rows] == ["702"]  # the owner's clip is in the cache still
    assert world.state()["changes"]["stock_clip_pixabay"]["removed_after_kill"] == ["701"]


def test_a_test_voice_a_channel_was_given_is_left(world: World) -> None:
    """The kit's process is ended after it made its designed test voice; the owner then gives a channel
    that voice. The next run does not remove it under the channel, and says so."""
    from ns_common.config import load_channel
    from ns_common.models import Voice

    world.traps.kills.add("api POST /api/voices/clone")
    with pytest.raises(Killed):
        world.run()
    (folder,) = world.paths.models_tts_voices.glob("gpukit-design-*")
    channel = load_channel(world.paths, "after-dark")
    save_channel(world.paths, channel.model_copy(update={"voice": Voice(mode="design", name=folder.name)}))
    assert world.run() == 0, world.out
    assert folder.is_dir()
    line = world.state()["steps"]["KV"]["line"]
    assert f"left on the PC: the test voice {folder.name} (the channel After Dark uses it as its" in line
    assert not list(world.paths.models_tts_voices.glob("gpukit-clone-*"))  # its other test voice is removed


def test_the_kits_channel_is_not_paused_under_a_job_the_owner_queued_on_it(world: World) -> None:
    queued: list[str] = []
    seen: dict[str, str] = {}

    def owner_queues() -> None:  # while the kit's video is made, the owner asks for one on that channel
        status, job = world.product.http.api("POST", "/api/jobs/run", {"channel": "gpu-kit"})
        assert status == 201, (status, job)
        queued.append(job["job_id"])

    world.say_hooks["Making the story video"] = owner_queues
    world.say_hooks[_label("KC")] = lambda: seen.update(status=world.channel()["status"])
    assert world.run() == 0, world.out
    assert seen["status"] == "active" and world.job(queued[0])["status"] == "queued"
    assert (f"the kit's channel was left active because the run job {queued[0]} on it is waiting in the "
            "Queue") in world.state()["steps"]["K3"]["line"]  # fmt: skip


def test_the_dashboard_open_in_a_browser_keeps_nightshift_running(nightshift: World) -> None:
    """The engine's own question before its idle exit (`ns_supervisor.presence`): a page that is open holds
    a connection to the Dashboard. The kit asks the same and leaves Nightshift under an open page."""
    w = nightshift
    pages: list[socket.socket] = []
    w.say_hooks[LAST_STEP] = lambda: pages.append(socket.create_connection(("127.0.0.1", w.server.port)))
    try:
        assert w.run() == 0, w.out
        assert w.never_stopped()
        assert w.said("Nightshift was left running because Nightshift's Dashboard is open"), w.out[-6:]
    finally:
        for page in pages:
            page.close()


def test_a_nightly_run_at_work_keeps_nightshift_running(nightshift: World) -> None:
    w = nightshift
    w.hold_at = "kit_story"  # the night is in a stage (one that holds no model) when the kit's run ends
    night = threading.Thread(target=w.night, args=(local(2026, 11, 13, 3, 0),), daemon=True)

    def the_night_starts() -> None:
        night.start()
        assert w.stage_held.wait(60)

    w.say_hooks[LAST_STEP] = the_night_starts
    try:
        assert w.run() == 0, w.out
        assert w.never_stopped()
        line = w.said("Nightshift was left running because the nightly run daily-")
        assert " is working (and 1 more)." in line, w.out[-6:]  # (and the video job that run is making)
    finally:
        w.hold_at = None
        w.stage_may_go.set()
        night.join(120)


def test_close_to_a_nightly_run_the_model_test_waits_and_nightshift_is_left_for_the_night(
    nightshift: World,
) -> None:
    """02:10, Automatic runs on (the owner's), the nightly run at 03:00. Step 3's model test, which stops
    every model, has no room before the run and waits; and the kit does not end the engine it started
    within the hour before a night (the engine's own rule for its idle exit)."""
    w = nightshift
    assert w.product.http.api("PUT", "/api/settings", {"automatic_runs": True})[0] == 200
    w.clock.t = datetime(2026, 11, 13, 2, 10).astimezone()
    assert w.run() == 0, w.out
    rec = w.state()["steps"]["K2"]
    assert ("cli", "ns", "verify") not in w.product.calls, rec
    assert rec["result"] == "WAIT" and "the model test and the proofs takes up to 1 h 40 min" in rec["line"]
    assert w.product.count("start_engine") == 1 and w.never_stopped()
    assert w.said("Nightshift was left running because the nightly run starts at 03:00, within the hour")


def test_an_update_in_progress_keeps_the_kit_from_stopping_nightshift(nightshift: World) -> None:
    from ns_supervisor import update_job

    w = nightshift
    lock = update_job.update_lock(w.paths)  # the lock an update, a rollback or setup holds while it works
    w.say_hooks[LAST_STEP] = lock.acquire
    try:
        assert w.run() == 0, w.out
        assert w.never_stopped()
        assert w.said("Nightshift was left running because an update of Nightshift is in progress")
    finally:
        lock.release()


# ------------------------------------------------------------------------------------------------ round 8
# The kit is driven by an AGENT'S SHELL as well as by a person. Such a shell often gives a child process a
# pipe nobody writes to as its input: every place the kit reads input or waits for a key ends by time
# whatever the input is, and -Unattended reads none at all. The children below are REAL processes with that
# kind of input, each ended by its recorded pid (its children too) on every path.
def end_tree(proc: subprocess.Popen[bytes]) -> None:
    """End the child (when it still runs) and what it started, by the recorded pid."""
    import psutil

    if proc.poll() is None:
        family = []
        try:
            family = psutil.Process(proc.pid).children(recursive=True)
        except psutil.Error:
            pass
        for p in family:
            try:
                p.kill()
            except psutil.Error:
                pass
        proc.kill()
    proc.wait(30)


def child_with(
    argv: list[str], give: str, tmp: Path, wait_s: float, env: dict[str, str] | None = None
) -> tuple[bool, int | None, str]:
    """Run `argv` as a real child whose input is `give`; (it ended by itself within `wait_s`, its exit code,
    what it printed). A pipe that is not closed here stays OPEN and silent until the child has ended."""
    out = tmp / "child-output.txt"
    empty = tmp / "empty-input.txt"
    empty.write_bytes(b"")
    source: Any = subprocess.PIPE
    opened = None
    if give == "the NUL device":
        source = subprocess.DEVNULL
    elif give == "an empty file":
        source = opened = empty.open("rb")
    ended, code = False, None
    with out.open("wb") as sink:
        proc = subprocess.Popen(argv, stdin=source, stdout=sink, stderr=subprocess.STDOUT, env=env)
        try:
            if proc.stdin is not None:
                if give == "a pipe with a whole line":
                    proc.stdin.write(b"yes it does\r\n")
                elif give == "a pipe with two lines at once":
                    proc.stdin.write(b"a\ny\n")
                elif give == "a pipe that closes in the middle of a line":
                    proc.stdin.write(b"y")
                if give in ("a pipe that closes in the middle of a line", "a pipe that is closed"):
                    proc.stdin.close()
                else:
                    proc.stdin.flush()  # (and it stays open: "a pipe that stays silent" gets nothing)
            try:
                code = proc.wait(wait_s)
                ended = True
            except subprocess.TimeoutExpired:
                pass
        finally:
            end_tree(proc)
            if proc.stdin is not None and not proc.stdin.closed:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            if opened is not None:
                opened.close()
    return ended, code, out.read_text(encoding="utf-8", errors="replace")


ASK = ("import sys; sys.path.insert(0, sys.argv[1]); from dev.kit import gpukit\n"
       "{before}\n"
       "for _ in range({times}):\n"
       "    print('ANSWER', repr(gpukit.timed_input('Q?', {bound})), flush=True)\n")  # fmt: skip
#: (the child's input, what the child does first, prompts, each prompt's bound in seconds, the answers)
INPUTS = [
    ("a pipe that stays silent", "", 1, 2, ["None"]),  # ends by time, at its bound
    ("a pipe that closes in the middle of a line", "", 1, 60, ["None"]),  # at once: a cut line is no answer
    ("a pipe that is closed", "", 1, 60, ["None"]),
    ("a pipe with a whole line", "", 1, 60, ["'yes it does'"]),
    ("a pipe with two lines at once", "", 2, 60, ["'a'", "'y'"]),  # the second line is the next prompt's
    ("an empty file", "", 1, 60, ["None"]),
    ("the NUL device", "", 1, 60, ["None"]),  # at once: it is no keyboard, whatever isatty says
    ("a closed handle", "import os; os.close(0)", 1, 60, ["None"]),
    ("no input at all", "sys.stdin = None", 1, 60, ["None"]),
]


@pytest.mark.parametrize(("give", "before", "times", "bound", "answers"), INPUTS, ids=[i[0] for i in INPUTS])
def test_every_prompt_ends_by_time_whatever_the_input_is(
    tmp_path: Path, give: str, before: str, times: int, bound: int, answers: list[str]
) -> None:
    kit_here()
    code = ASK.format(before=before, times=times, bound=bound)
    pipe = give if give.startswith(("a pipe", "the NUL", "an empty")) else "a pipe that stays silent"
    # (20 s: far more than a bound of 2 s needs, far less than a bound of 60 s: those must not wait at all)
    ended, rc, out = child_with([sys.executable, "-c", code, str(ROOT)], pipe, tmp_path, 20)
    assert ended, f"with {give} as its input the kit's prompt never ended (the child was ended by its pid)"
    assert rc == 0 and re.findall(r"ANSWER (.+)", out) == answers, (rc, out)


def test_at_a_keyboard_a_prompt_ends_at_its_bound_and_takes_what_is_typed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    kit_here()
    assert hasattr(gpukit, "_keyboard"), "the kit has no seam for the console's keyboard"
    keys = list("n too dark\r")
    monkeypatch.setattr(gpukit, "_stdin_kind", lambda: "console")
    monkeypatch.setattr(gpukit, "_keyboard", lambda: (lambda: bool(keys), lambda: keys.pop(0)))
    monkeypatch.setattr(sys, "platform", "win32")
    assert gpukit.timed_input("Q?", 5) == "n too dark"
    begun = time.monotonic()
    assert gpukit.timed_input("Q?", 0.3) is None  # nobody types: it ends by time
    assert 0.25 <= time.monotonic() - begun < 5
    assert "(no answer: left for the next run)" in capsys.readouterr().out


def test_what_the_kit_starts_never_gets_the_kits_own_input(tmp_path: Path) -> None:
    """`opencode run`, `ns`, `studio`, the proof runner and the PowerShell tools are started through `_run`.
    Under an agent's shell the kit's input is a pipe nobody writes to: a program that reads its input would
    wait there for its whole bound. Each gets the NUL device instead."""
    kit_here()
    reads = "import sys; sys.stdin.read(); print('the end of its input')"
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from dev.kit import gpukit\n"
            f"print(gpukit._run([sys.executable, '-c', {reads!r}], 8))\n")  # fmt: skip
    argv = [sys.executable, "-c", code, str(ROOT)]
    ended, rc, out = child_with(argv, "a pipe that stays silent", tmp_path, 60)
    assert ended and rc == 0, out
    assert out.startswith("(0, 'the end of its input"), out


def test_a_line_the_kit_says_reaches_a_file_or_a_pipe_at_once(tmp_path: Path) -> None:
    """An agent's shell reads the kit through a pipe or a file, for hours: a line must be there when it is
    said, not when the kit ends (or never, when the shell ends the kit first). The child says one line and
    then stays for a minute; its output is a file."""
    kit_here()
    code = ("import sys, time; sys.path.insert(0, sys.argv[1]); from dev.kit import gpukit\n"
            "gpukit.KitEnv().say('Step 5 of 13 (make one story video): starting.')\n"
            "time.sleep(60)\n")  # fmt: skip
    out = tmp_path / "said.txt"
    with out.open("wb") as sink:
        child = subprocess.Popen([sys.executable, "-c", code, str(ROOT)], stdin=subprocess.DEVNULL,
                                 stdout=sink, stderr=subprocess.STDOUT)  # fmt: skip
        try:
            end = time.monotonic() + 15
            while time.monotonic() < end and child.poll() is None and not out.stat().st_size:
                time.sleep(0.1)
            said = out.read_text(encoding="utf-8", errors="replace")
            running = child.poll() is None
        finally:
            end_tree(child)
    assert running and said.strip() == "Step 5 of 13 (make one story video): starting.", (running, said)


POWERSHELL = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe and Windows PowerShell")
@pytest.mark.parametrize("switch", [[], ["-Unattended"]], ids=["as a person starts it", "unattended"])
def test_gpu_kit_cmd_never_waits_for_a_key_nobody_can_press(tmp_path: Path, switch: list[str]) -> None:
    """The real GPU-KIT.cmd beside a stand-in for dev\\kit\\gpukit.ps1 (it prints two lines and ends with 7),
    started by cmd.exe with a pipe nobody writes to as its input."""
    here = tmp_path / "zip"
    (here / "dev" / "kit").mkdir(parents=True)
    (here / "GPU-KIT.cmd").write_bytes((ROOT / "GPU-KIT.cmd").read_bytes())
    stand_in = ("param([switch]$Unattended)\r\nWrite-Output 'the kit ran'\r\n"
                "Write-Output '{\"state\": \"waiting\"}'\r\nexit 7\r\n")  # fmt: skip
    (here / "dev" / "kit" / "gpukit.ps1").write_bytes(stand_in.encode("ascii"))
    argv = ["cmd.exe", "/d", "/c", str(here / "GPU-KIT.cmd"), *switch]
    ended, rc, out = child_with(argv, "a pipe that stays silent", tmp_path, 45)
    assert ended, "GPU-KIT.cmd waited for a key on an input that is no keyboard (ended by its pid)"
    assert rc == 7, (rc, out)  # the kit's own exit code
    assert out.splitlines()[-1] == '{"state": "waiting"}', out  # nothing is printed after the kit's last line
    text = (ROOT / "GPU-KIT.cmd").read_text(encoding="ascii")
    assert not re.search(r"(?im)^\s*pause\b", text)  # (pause reads its input: it never ends on a silent pipe)
    # a person's window still waits for a key: Windows' own timeout.exe, whatever else PATH calls timeout
    assert '\n"%SystemRoot%\\System32\\timeout.exe" /t 99999 >nul 2>nul\n' in text


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe and a console window")
def test_at_a_console_of_its_own_gpu_kit_cmd_waits_for_a_person_and_not_for_an_agent(tmp_path: Path) -> None:
    """The real GPU-KIT.cmd (beside a stand-in for gpukit.ps1 that ends with 7) at a console of its own,
    where nothing is typed (the console's window is hidden, so it never takes the keyboard from whoever
    works at this PC, and no key of theirs can end the wait). As a person starts it, it stays (it waits for
    a key); with -Unattended it ends by itself with the kit's exit code. Each is ended by its recorded pid."""
    probe = tmp_path / "probe.cmd"
    probe.write_bytes(b'@echo off\r\n"%SystemRoot%\\System32\\timeout.exe" /t 0 >nul 2>nul\r\n'
                      b'echo %errorlevel% > "%~dp0probe.txt"\r\n')  # fmt: skip
    own_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    hidden = subprocess.STARTUPINFO()
    hidden.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    hidden.wShowWindow = subprocess.SW_HIDE
    look = ["cmd.exe", "/d", "/c", str(probe)]
    looked = subprocess.Popen(look, creationflags=own_console, startupinfo=hidden)
    try:
        looked.wait(60)
    finally:
        end_tree(looked)
    said = tmp_path / "probe.txt"
    answer = said.read_text(encoding="ascii").strip() if said.exists() else ""
    if answer != "0":
        pytest.skip(f"no console with a keyboard can be opened here (timeout.exe said {answer!r})")
    here = tmp_path / "zip"
    (here / "dev" / "kit").mkdir(parents=True)
    (here / "GPU-KIT.cmd").write_bytes((ROOT / "GPU-KIT.cmd").read_bytes())
    (here / "dev" / "kit" / "gpukit.ps1").write_bytes(b"param([switch]$Unattended)\r\nexit 7\r\n")
    argv = ["cmd.exe", "/d", "/c", str(here / "GPU-KIT.cmd")]
    agent = subprocess.Popen([*argv, "-Unattended"], creationflags=own_console, startupinfo=hidden)
    try:
        try:
            code = agent.wait(60)
        except subprocess.TimeoutExpired:
            code = None
    finally:
        end_tree(agent)
    assert code == 7, "unattended, GPU-KIT.cmd waited for a key at the end (ended by its pid)"
    person = subprocess.Popen(argv, creationflags=own_console, startupinfo=hidden)
    try:
        try:
            person.wait(12)
            stayed = False
        except subprocess.TimeoutExpired:
            stayed = True
    finally:
        end_tree(person)
    assert stayed, "a person's window closed by itself: its last lines could not be read"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell")
def test_gpukit_ps1_asks_for_the_folder_only_at_a_keyboard_and_by_time(tmp_path: Path) -> None:
    script = ROOT / "dev" / "kit" / "gpukit.ps1"
    code = "\n".join(line.split("#", 1)[0] for line in script.read_text(encoding="ascii").splitlines())
    assert "Read-Host" not in code  # (it reads the input whatever it is: on a silent pipe it never ends)
    ask = ("$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:NS_PS1, [ref]$null, "
           "[ref]$null); $fn = $ast.Find({ param($n) $n -is "
           "[System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Read-KitLine' }, "
           "$true); if ($null -eq $fn) { 'NO FUNCTION'; exit 3 }; Invoke-Expression $fn.Extent.Text; "
           "$r = Read-KitLine -Question 'Which folder?' -Prompt 'Folder' -TimeoutSec 60; "
           "if ($null -eq $r) { 'NO ANSWER' } else { 'ANSWER ' + $r }")  # fmt: skip
    env = {**os.environ, "NS_PS1": str(script)}
    ended, rc, out = child_with([*POWERSHELL, "-Command", ask], "a pipe that stays silent", tmp_path, 40, env)
    assert ended and rc == 0 and out.split() == ["NO", "ANSWER"], (ended, rc, out)  # not asked, not read
    # the whole script, unattended, with a folder that is no install: refused at once, the JSON line last
    nothing = tmp_path / "not an install"
    nothing.mkdir()
    argv = [*POWERSHELL, "-File", str(script), "-Home", str(nothing), "-Unattended"]
    ended, rc, out = child_with(argv, "a pipe that stays silent", tmp_path, 60)
    assert ended and rc == 2, (ended, rc, out)
    last = json.loads(out.splitlines()[-1])
    assert (last["state"], last["exit_code"], last["step"]) == ("refused", 2, "K0")
    assert last["open_questions"] == [] and last["zip"] is None
    assert "is not a Nightshift install" in last["reason"] and out.startswith("REFUSED ")


def test_the_kit_itself_unattended_reads_nothing_and_ends_with_the_json_line(tmp_path: Path) -> None:
    """The real dev/kit/gpukit.py as a child with a pipe nobody writes to (a folder that is no install: it
    refuses, which needs no product)."""
    argv = [sys.executable, str(ROOT / "dev" / "kit" / "gpukit.py"), "--home", str(tmp_path / "none"),
            "--unattended"]  # fmt: skip
    ended, rc, out = child_with(argv, "a pipe that stays silent", tmp_path, 60)
    lines = out.splitlines()
    last = json.loads(lines[-1]) if lines and lines[-1].startswith("{") else {}
    assert ended and rc == 2 and last.get("state") == "refused", (ended, rc, out)
    assert lines[0].startswith("REFUSED ") and last["reason"] in lines[0] and last["zip"] is None
    assert set(last) == {"kit", "step", "step_title", "state", "exit_code", "next_run_after",
                         "open_questions", "zip", "failed_steps", "reason"}  # fmt: skip


def test_the_help_says_what_unattended_does_and_what_its_exit_codes_mean() -> None:
    kit_here()
    text = gpukit.build_parser().format_help()
    assert "--unattended" in text and "reads no input" in text and "waits for no key" in text
    assert getattr(gpukit, "EXIT", None) == {"done": 0, "failed": 1, "refused": 2, "waiting": 3, "locked": 4,
                                             "blocked": 5, "interrupted": 130}  # fmt: skip
    for word, code in gpukit.EXIT.items():
        assert re.search(rf"(?m)^\s+{code}  {word}", text), (word, code)
    script = (ROOT / "dev" / "kit" / "gpukit.ps1").read_text(encoding="ascii")
    assert ".PARAMETER Unattended" in script and "[switch]$Unattended" in script


DAY_ONE = {"K0": "PASS", "K1": "PASS", "K2": "PASS", "KS": "PASS", "K3": "PASS", "KC": "PASS", "KV": "PASS",
           "KN": "NOT RUN", "KA": "PASS", "K4": "PASS", "K5": "WAIT", "K6": "WAIT", "K7": "WAIT"}  # fmt: skip


def test_an_unattended_run_asks_nothing_keeps_the_owners_questions_open_and_ends_with_one_json_line(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    kit_here()
    read: list[str] = []
    monkeypatch.setattr(gpukit, "timed_input", lambda prompt, timeout_s=0: read.append(prompt))
    world.stand_in_for_the_person = False  # (the kit would read its own input, if it read any)
    code = world.run("--unattended")
    last = world.last()
    assert code == 3 and last.get("state") == "waiting", (code, world.out[-3:])  # it waits for the nights
    assert read == [] and world.asked == []  # nothing was asked, not even "Press Enter to start"
    assert world.results() == DAY_ONE  # the same steps and results as a person's first run
    state = world.state()
    assert (last["step"], last["exit_code"], last["failed_steps"]) == ("K5", 3, [])
    assert last["next_run_after"] == state["come_back"] == local(2026, 11, 14, 9, 0).isoformat()
    assert last["zip"] == str(world.zips()[0])
    assert world.out[-1] == json.dumps(last)  # the last line is that one object, and nothing else
    # every question only a person can answer: open, with its exact words and the file to look at
    video = json.loads((world.ev / "story" / "story.json").read_text(encoding="utf-8"))["video"]
    asked = last["open_questions"]
    assert len(asked) == 19 and state["owner_questions_open"] == asked
    assert asked[0] == {"group": "watch", "id": "q1", "question": gpukit.QUESTIONS["watch"][0],
                        "look_at": video}  # fmt: skip
    assert {q["group"]: q["look_at"] for q in asked} == {
        "watch": video, "paste": str(gpukit.KIT_DIR / "K8_PASTE_CHECK.md"),
        "task_end": str(gpukit.KIT_DIR / "night-task-end.txt"),
        "standby": str(gpukit.KIT_DIR / "night-task-end.txt")}  # fmt: skip
    heading = world.out.index(world.said("For the owner: 19 question(s) only a person can answer are open."))
    listed = world.out[heading + 1 : heading + 20]
    assert listed[0] == f"  1. [the video] {gpukit.QUESTIONS['watch'][0]} Look at: {video}"
    words = [line.split("] ", 1)[1].rsplit(" Look at: ", 1)[0] for line in listed]
    assert words == [q["question"] for q in asked]
    # not answered: no answer of any kind is recorded, and no group is PASS or FAIL
    assert not state.get("answers") and {c["result"] for c in state["checklists"].values()} == {"WAIT"}
    assert "later" not in json.dumps(state.get("answers", {}))
    # the second morning, unattended again: done; the morning note's questions are the owner's too
    world.nights_pass()
    assert world.run("--unattended") == 0, world.out[-4:]
    last = world.last()
    assert (last["state"], last["step"], last["next_run_after"]) == ("done", "done", None)
    assert last["failed_steps"] == []
    assert len(last["open_questions"]) == 21 and last["open_questions"][-1]["group"] == "morning"
    assert last["zip"] == str(world.zips()[0]) and world.said("Send this one file: ")
    assert read == [] and world.asked == [] and not world.state().get("answers")
    # a later run by a person (no switch) asks them, and what the person says is what is recorded
    world.stand_in_for_the_person = True
    world.answers = ["y"] * 21
    assert world.run() == 0, world.out[-4:]
    put = [a for a in world.asked if "[y/n/later]" in a or "(or later)" in a]
    assert len(put) == 21 and put[0] == f"{gpukit.QUESTIONS['watch'][0]} [y/n/later]"
    assert world.state()["answers"]["watch"]["q1"]["answer"] == "y"
    assert world.state()["owner_questions_open"] == []


def test_an_unattended_runs_exit_code_says_what_happened(world: World) -> None:
    kit_here()
    # 2 refused: nothing was started or changed
    assert world.run("--unattended", "--night-now") == 2
    last = world.last()
    assert (last.get("state"), last.get("step"), last.get("zip")) == ("refused", "K0", None), world.out
    assert "-NightNow and -Clock are for a dry run only" in last["reason"] and not world.ev.exists()
    # 4 locked: another run of the kit holds the kit lock (an attended run says the same with code 2)
    holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        world.ev.mkdir(parents=True)
        now = datetime.now().astimezone().isoformat()
        (world.ev / "kit.lock").write_text(f"kit | GPU kit pid {holder.pid} | {now}\n", "ascii")
        assert world.run("--unattended") == 4 and world.last().get("state") == "locked"
        assert f"Another GPU kit is running (pid {holder.pid})" in world.last()["reason"]
        assert world.run() == 2
    finally:
        end_tree(holder)
    # 5 blocked: a step could not start now (the owner's Nightshift Code holds the card); no time to wait for
    world.supervisor.state.update({"gpu_owner": "opencode", "services": {"llama": {"state": "ready"}}})
    assert world.run("--unattended") == 5, world.out[-4:]
    last = world.last()
    assert (last["state"], last["step"], last["next_run_after"]) == ("blocked", "K2", None)
    assert last["failed_steps"] == []
    world.supervisor.state.update({"gpu_owner": None, "services": {}})
    # 1 failed: a step's result is FAIL; the kit still goes on with the nights, and says when
    world.fail_at = "kit_story"
    assert world.run("--unattended") == 1, world.out[-4:]
    last = world.last()
    assert (last["state"], last["failed_steps"], last["step"]) == ("failed", ["K3"], "K5")
    assert last["next_run_after"] == local(2026, 11, 14, 9, 0).isoformat()


def test_an_unattended_wait_always_names_the_time_to_run_it_again(world: World) -> None:
    """A wait whose end the kit cannot know (a nightly run still at work) is looked at again in half an
    hour: the exit code 'waiting' always comes with next_run_after, in the last line and in the state."""
    assert world.run("--unattended") == 3
    world.night(local(2026, 11, 13, 3, 0))
    world.hold_at = "kit_story"
    night2 = threading.Thread(target=world.night, args=(local(2026, 11, 14, 8, 55),), daemon=True)
    night2.start()
    try:
        assert world.stage_held.wait(60)
        world.clock.t = local(2026, 11, 14, 9, 30)
        code = world.run("--unattended")
        last = world.last()
        assert code == 3 and (last["state"], last["step"]) == ("waiting", "K6"), (code, world.out[-4:])
        assert last["next_run_after"] == world.state()["come_back"] == local(2026, 11, 14, 10, 0).isoformat()
        assert world.out[-2] == "Run GPU-KIT.cmd again after Saturday 14 November 10:00."
    finally:
        world.hold_at = None
        world.stage_may_go.set()
        night2.join(120)


# ============================================ the kit's output on a file (block E final review, blocker 1)
MOON = "\U0001f319"


def test_a_line_with_a_character_the_output_cannot_take_is_written_escaped_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent's `> log 2>&1` is a file in the PC's ANSI code page; step 5's PASS line prints the video's
    path (its title, every character kept) and step 9's the Assistant's answer. One emoji must not turn a
    finished step into "an error it did not expect": the line is written, the character escaped."""
    log = tmp_path / "kit.log"
    with log.open("w", encoding="cp1252", errors="strict", newline="\n") as ansi:
        monkeypatch.setattr(sys, "stdout", ansi)
        gpukit.say_line(f"PASS    step 5 (story video): D:/out/Night {MOON} story.mp4")
        gpukit.say_line("plain")
    escaped = "\\" + "U0001f319"   # the eight-digit escape Python writes for a character cp1252 cannot take
    assert log.read_text(encoding="cp1252").splitlines() == [
        f"PASS    step 5 (story video): D:/out/Night {escaped} story.mp4", "plain",
    ]


def test_the_kit_as_a_program_writes_utf8_to_a_file_whatever_the_code_page(tmp_path: Path) -> None:
    """The program itself, as a child whose output is a file (what GPU-KIT.cmd > log gives it), with Python's
    UTF-8 mode off as on a stock Windows: `utf8_output` makes the file UTF-8, so the line arrives whole and
    `Get-Content -Encoding UTF8` reads it; without it Python writes ANSI, strictly, and the line raises."""
    kit = Path(gpukit.__file__).resolve()
    code = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('gpukit_child', {str(kit)!r})\n"
        "kit = importlib.util.module_from_spec(spec)\n"
        "sys.modules['gpukit_child'] = kit\n"
        "spec.loader.exec_module(kit)\n"
        "kit.utf8_output()\n"
        f"kit.say_line('Night {MOON} story.mp4')\n"
        "print('err line \u2019', file=sys.stderr, flush=True)\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")}
    env["PYTHONUTF8"] = "0"
    log = tmp_path / "child.log"
    with log.open("wb") as out:
        done = subprocess.run(  # noqa: S603 - this interpreter, a fixed script
            [sys.executable, "-X", "utf8=0", "-c", code], stdout=out, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, env=env, cwd=str(tmp_path), timeout=120, check=False,
        )
    text = log.read_bytes().decode("utf-8")
    assert done.returncode == 0, text
    assert text.splitlines() == [f"Night {MOON} story.mp4", "err line \u2019"]


def test_the_unattended_last_line_is_ascii_whatever_the_state_holds(world: World) -> None:
    """The one line an agent parses is readable under any code page: `json.dumps` escapes every character
    that is not ASCII (a zip path or a reason with the owner's own letters in it)."""
    code = world.run("--unattended")
    raw_last = world.out[-1]
    assert code in gpukit.EXIT.values() and raw_last.isascii(), (code, raw_last)
    assert json.loads(raw_last)["kit"] == "nightshift-gpu-kit"
