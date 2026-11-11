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
        self.calls.append(("start_engine",))
        self.world.dashboard_down = False
        return True, "engine + dashboard up (headless)"

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
            self.paths, fake_services=True, link=SupervisorLink(self.supervisor.url, timeout_s=2.0)
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
        self.dashboard_down = False
        self.refuse_cancel = False
        self.jobs_route_times_out = False
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
            clock=self.clock, product=self.product, windows=self.windows, ask=self.ask, say=self.say
        )
        return gpukit.main(["--home", str(self.home), *extra], env=env)

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
    assert line.startswith("Pixabay: searched and downloaded one clip (0.0 MB, pixabay 701, 1920x1080)"), line
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
    assert "engine_started_by_kit" not in world.state()


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
