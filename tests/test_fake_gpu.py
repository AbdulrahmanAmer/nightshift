# ruff: noqa: E501
"""H-10: the simulated graphics card (`fake_gpu`), so the GPU hand-over is proven on a PC without one.

Acceptance tests of refs/cards/H-10.md, written before the build. The switch replaces only what the product
asks the hardware (nvidia-smi, through `ns_supervisor.gpu.SmiSource`) and which services count as tenants of
the card; the lock, the guard before every step of an acquire, the holder's pid, the lease and every release
are the real code, and these tests drive it through the real `Supervisor` (and `create_app` where the card
says so), real stub service processes and real child processes ended by their pid.

On the base (no switch) a test cannot write `fake_gpu` at all (the strict models refuse an unknown key), so
the helpers leave it out there and each test fails at its first assertion about the effect.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from installer.stage2.answers import InstallerAnswers
from installer.stage2.runtime_cfg import build_runtime, ensure_baseline
from installer.stage2.testing import example_answers, make_test_context
from ns_common import devmode
from ns_common.config import load_runtime, save_runtime
from ns_common.errors import NsError, describe
from ns_common.logging import NsLogger
from ns_common.models import GpuStatus, Runtime
from ns_common.paths import Paths, repo_root
from ns_supervisor import doctor, gpu, notify, procs
from ns_supervisor.core import Supervisor
from ns_supervisor.services import SERVICES, ServiceDef, build_service
from ns_supervisor.testing import fast_config, free_port, stub_def, wait_until

#: False on the base: `Runtime` has no `fake_gpu` field, so nothing can set the switch.
SWITCH = "fake_gpu" in Runtime.model_fields
FAKE_FILE = ("run", "fake_gpu.json")
#: nvidia-smi's own line for the GPU PC's card (tests/fixtures/supervisor: RTX 5060 Ti, driver 580.88).
LINE_5060 = "NVIDIA GeForce RTX 5060 Ti, 2, 812, 16311, 38\n"
FIXTURE = repo_root() / "tests" / "fixtures" / "supervisor" / "nvidia-smi-rtx-5060-ti.txt"


@pytest.fixture(autouse=True)
def _no_lease_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NS_FAKE_GPU_LEASE_S", raising=False)


# ---------------------------------------------------------------------------------------------- homes
def engine_test_home(tmp_path: Path, *, switch: bool = True) -> Paths:
    """An engine-test install (fake voice, fake footage, cpu-dev: llama `-ngl 0`, whisper on the CPU) whose
    runtime.json is written by setup's own `build_runtime` from raw answers."""
    raw = example_answers(allow_cpu=True, fake_voice=True, fake_footage=True)
    if switch and SWITCH:
        raw["fake_gpu"] = True
    ctx = make_test_context(
        tmp_path, raw_answers=raw, resolve=True, dev_mode=False, preflight={"profile": "cpu-dev"}
    )
    save_runtime(ctx.paths, build_runtime(ctx))
    return ctx.paths


def real_home(tmp_path: Path, *, by_hand: bool = True) -> Paths:
    """A normal GPU install (no test switch); `by_hand`: `fake_gpu: true` typed into its runtime.json."""
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True)
    save_runtime(ctx.paths, build_runtime(ctx))
    if by_hand and SWITCH:
        data = json.loads(ctx.paths.runtime_file.read_text(encoding="utf-8"))
        data["fake_gpu"] = True
        ctx.paths.runtime_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return ctx.paths


def fake_file(paths: Paths) -> Path:
    """`run\\fake_gpu.json` of the home (its folder made, as a proof finds it on an install)."""
    f = paths.home.joinpath(*FAKE_FILE)
    f.parent.mkdir(parents=True, exist_ok=True)
    return f


def stub_resolver(
    rt: Runtime, paths: Paths, extra: dict[str, list[str]] | None = None
) -> Callable[[str, str | None], ServiceDef]:
    """The REAL service table's decisions (gpu flag, variant, alias) on stub processes (no llama-server)."""
    ports = {n: free_port() for n in SERVICES}
    more = extra if extra is not None else {}

    def resolve(name: str, alias: str | None) -> ServiceDef:
        real = build_service(rt, paths, name, alias)
        return stub_def(
            name, ports[name], gpu=real.gpu, extra=list(more.get(name, [])), variant=real.variant,
            model_alias=real.model_alias, persistent=real.persistent,
        )  # fmt: skip

    return resolve


def engine(paths: Paths, *, extra: dict[str, list[str]] | None = None, **kw: Any) -> Supervisor:
    rt = load_runtime(paths)
    cfg = kw.pop("config", None) or fast_config(lease_expiry_s=600, idle_unload_s=60)
    return Supervisor(paths, rt, config=cfg, resolver=stub_resolver(rt, paths, extra), **kw)


@pytest.fixture
def sleeper() -> Iterator[Callable[[], subprocess.Popen[bytes]]]:
    """Stand-ins for the Code console's PowerShell: base-interpreter children that sleep; all ended after."""
    started: list[subprocess.Popen[bytes]] = []
    exe = getattr(sys, "_base_executable", None) or sys.executable

    def start() -> subprocess.Popen[bytes]:
        p = subprocess.Popen(
            [exe, "-I", "-c", "import time; time.sleep(120)"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )  # fmt: skip
        started.append(p)
        return p

    yield start
    for p in started:
        if p.poll() is None:
            p.kill()
        p.wait(timeout=10)


def end_by_pid(p: subprocess.Popen[bytes], pid: int | None = None) -> None:
    """The console is closed with X: its process ends at once, by the pid the engine recorded."""
    os.kill(pid or p.pid, signal.SIGTERM)  # TerminateProcess on Windows
    p.wait(timeout=10)


def drain(q: asyncio.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def gone(events: list[dict[str, Any]], owner: str) -> bool:
    return any(
        e["data"].get("event") == "owner_released" and e["data"].get("owner") == owner
        and e["data"].get("reason") == "holder_gone"
        for e in events
    )  # fmt: skip


def log_records(paths: Paths, event: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in sorted(paths.logs.glob("supervisor-*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("event") == event:
                out.append(rec)
    return out


async def until(pred: Callable[[], bool], timeout_s: float = 20.0) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return pred()


async def close(sup: Supervisor) -> None:
    await sup.stop_all(include_persistent=True)
    sup.close_job()


def smi_of(sup: Supervisor) -> list[Any]:
    """`[sup.smi]` for the procs helpers, `[]` where the engine has no source (the base: the real tool)."""
    s = getattr(sup, "smi", None)
    return [s] if s is not None else []


# --------------------------------------------------------------------------- 1. the target's tenants
def test_fake_gpu_makes_the_target_gpu_services_take_the_lock(tmp_path: Path) -> None:
    paths = engine_test_home(tmp_path)
    rt = load_runtime(paths)
    assert (rt.llama.flags.n_gpu_layers, rt.tts.engine, rt.whisper.device) == (0, "fake", "cpu")
    assert build_service(rt, paths, "llama").gpu, "fake_gpu does not make llama a GPU tenant"
    assert build_service(rt, paths, "tts").gpu, "fake_gpu does not make the voice service a GPU tenant"
    assert not build_service(rt, paths, "embed").gpu  # off the card, as on the GPU PC
    sup = engine(paths)
    assert sup._whisper_gpu(), "fake_gpu does not make whisper a GPU tenant"

    async def main() -> None:
        try:
            await sup.acquire("llama", "opencode", "code")
            assert sup.status()["gpu_owner"] == "opencode"
            queued = asyncio.create_task(sup.acquire("tts", "studio:j1"))
            assert await until(lambda: sup.lock.waiters() == [("studio:j1", "tts")])
            with pytest.raises(NsError) as ei:
                await sup.acquire("whisper", "studio:j2", wait=False)  # busy: refused at once
            assert ei.value.code == "NS-E201"
            await sup.release("llama", "opencode")
            await asyncio.wait_for(queued, 20)
            assert sup.lock.state.owner == "studio:j1"  # the queue hands it on
            await sup.release("tts", "studio:j1")
            await sup.acquire("whisper", "studio:j2", wait=False)  # failed, then retried: granted
            assert sup.status()["gpu_owner"] == "studio:j2" and sup.status()["gpu_service"] == "whisper"
            await sup.release("whisper", "studio:j2")
        finally:
            await close(sup)

    asyncio.run(main())


def test_the_busy_answer_over_http_is_the_real_409(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_supervisor.app import create_app

    paths = engine_test_home(tmp_path)
    assert build_service(load_runtime(paths), paths, "llama").gpu, "fake_gpu does not make llama a GPU tenant"
    sup = engine(paths)
    with TestClient(create_app(sup), base_url="http://127.0.0.1:18099") as tc:
        assert tc.post("/acquire", json={"service": "llama", "owner": "opencode"}).status_code == 200
        r = tc.post("/acquire", json={"service": "tts", "owner": "studio:j1", "wait": False})
        assert r.status_code == 409 and r.json()["code"] == "NS-E201"
        st = tc.get("/status").json()
        assert st["gpu_owner"] == "opencode" and st["gpu"]["simulated"] is True


# ------------------------------------------------------------------------- 2. a real install ignores it
def test_fake_gpu_is_ignored_on_a_real_install(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_supervisor.app import create_app

    paths = real_home(tmp_path / "set")
    plain = real_home(tmp_path / "plain", by_hand=False)
    rt, rt0 = load_runtime(paths), load_runtime(plain)
    assert getattr(rt, "fake_gpu", False) is True, "runtime.json cannot carry fake_gpu"
    for name in ("llama", "tts", "embed", "dashboard"):
        assert build_service(rt, paths, name).gpu == build_service(rt0, plain, name).gpu, name
    assert not devmode.fake_gpu(paths) and devmode.fake_gpu_ignored(paths)

    def start() -> Supervisor:
        sup = engine(paths, gpu_probe=gpu.NvidiaSmiGpuProbe(runner=lambda: LINE_5060))
        assert sup.smi is gpu.REAL_SMI and not sup.fake_gpu  # the real tool, never the simulated card
        assert sup._whisper_gpu() == (rt.whisper.device == "cuda")
        with TestClient(create_app(sup), base_url="http://127.0.0.1:18099") as tc:
            assert "simulated" not in tc.get("/status").json()["gpu"]
        return sup

    start()
    warns = [r for r in log_records(paths, "fake_gpu_ignored") if (r.get("data") or {}).get("code") == "NS-W121"]
    assert len(warns) == 1 and warns[0]["level"] == "WARN", warns  # one per engine start
    start()  # a second engine start: a second WARN, but the morning note is not left twice
    assert len(log_records(paths, "fake_gpu_ignored")) == 2
    said = [n for n in notify.read_notes(paths) if n["message"].startswith("NS-W121:")]
    assert len(said) == 1 and "restart Nightshift" in said[0]["message"], said

    class Down:
        def is_up(self) -> bool:
            return False

        base_url = "http://127.0.0.1:9"

    rows = {name: (ok, detail) for name, ok, detail in doctor.diagnose(paths, Down())}  # type: ignore[arg-type]
    info = describe("NS-W121")
    assert rows["fake_gpu"][0] is False and info.message in rows["fake_gpu"][1] and info.hint in rows["fake_gpu"][1]
    plain_rows = {name for name, _ok, _d in doctor.diagnose(plain, Down())}  # type: ignore[arg-type]
    assert "fake_gpu" not in plain_rows  # no row where nobody set it


# --------------------------------------------------------------- 3. F-H47 through the simulated card
def test_a_closed_holder_releases_the_card(
    tmp_path: Path, sleeper: Callable[[], subprocess.Popen[bytes]]
) -> None:
    """The card's test 3 over HTTP with the engine's own monitor: Code takes the card with its pid, the
    console is ended by the pid `/status` names, one monitor pass frees it, the next job is granted at once."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_supervisor.app import create_app

    paths = engine_test_home(tmp_path)
    assert build_service(load_runtime(paths), paths, "llama").gpu, "fake_gpu does not make llama a GPU tenant"
    sup = engine(paths)
    child = sleeper()
    with TestClient(create_app(sup), base_url="http://127.0.0.1:18099") as tc:
        r = tc.post("/acquire", json={"service": "llama", "owner": "opencode", "model_alias": "code",
                                      "pid": child.pid})  # fmt: skip
        assert r.status_code == 200
        st = tc.get("/status").json()
        assert st["gpu_owner"] == "opencode" and st["holder_pid"] == child.pid and st["gpu"]["simulated"]
        end_by_pid(child, st["holder_pid"])
        # one monitor pass is 0.05 s here (fast_config); 2 s is forty of them
        assert wait_until(lambda: tc.get("/status").json()["gpu_owner"] is None, 2.0)
        recs = [r for r in log_records(paths, "owner_released") if r["data"].get("reason") == "holder_gone"]
        assert recs and recs[0]["data"]["owner"] == "opencode"
        r2 = tc.post("/acquire", json={"service": "llama", "owner": "studio:night-1", "wait": False})
        assert r2.status_code == 200 and tc.get("/status").json()["gpu_owner"] == "studio:night-1"


STEPS = ("queued", "stop", "spawn", "load", "ready", "held")


@pytest.mark.parametrize("step", STEPS)
def test_a_holder_gone_at_any_step_of_its_acquire_frees_the_card_for_the_next_job(
    tmp_path: Path, sleeper: Callable[[], subprocess.Popen[bytes]], step: str
) -> None:
    """F-H47's hand-over, step by step, with the REAL holder probe (psutil) and a real process ended by its pid.

    Each step is held open long enough to hit: queued (another job holds the card), stop (the previous tenant
    takes 1.5 s to stop), spawn (starting the process takes 1.5 s), load (the model is not healthy for 20 s),
    ready (the simulated card's nvidia-smi answers the VRAM sample 1.5 s late: `delay_s`), held (after the
    acquire returned). Then: the acquire ends with NS-E202 and never announces `acquired`, the card is free
    with `owner_released` `holder_gone`, and the next job's acquire (wait false) is granted at once.
    """
    paths = engine_test_home(tmp_path)
    rt = load_runtime(paths)
    assert build_service(rt, paths, "llama").gpu, "fake_gpu does not make llama a GPU tenant"
    extra: dict[str, list[str]] = {}
    slow_spawn = {"on": False}

    def starter(d: ServiceDef, logs: Path, *, job: Any = None) -> procs.Proc:
        if slow_spawn["on"] and d.name == "llama":
            time.sleep(1.5)
        return procs.start(d, logs, job=job)

    sup = engine(paths, extra=extra, starter=starter)
    child = sleeper()

    async def main() -> None:
        q = sup.subscribe()
        dead: asyncio.Task[dict[str, Any]] | None = None
        try:
            if step in ("queued", "stop"):
                await sup.acquire("tts", "studio:night-1")
            if step == "stop":
                real_stop = sup._stop_locked

                async def slow_stop(r: Any) -> None:
                    await asyncio.sleep(1.5)
                    await real_stop(r)

                sup._stop_locked = slow_stop  # type: ignore[method-assign]  # removed below
            slow_spawn["on"] = step == "spawn"
            if step == "load":
                extra["llama"] = ["--ready-delay", "20"]
            if step == "ready":
                fake_file(paths).parent.mkdir(parents=True, exist_ok=True)
                fake_file(paths).write_text('{"delay_s": 1.5}', encoding="utf-8")
            dead = asyncio.create_task(sup.acquire("llama", "opencode", "code", pid=child.pid))
            llama = sup.services["llama"]
            reached = {
                "queued": lambda: bool(sup.lock.waiters()),
                "stop": lambda: bool(sup.lock.waiters()),
                "spawn": lambda: llama.state == "starting" and llama.proc is None,
                "load": lambda: llama.state == "starting" and llama.proc is not None,
                "ready": lambda: llama.state == "ready" and not dead.done(),
                "held": lambda: dead.done(),
            }[step]
            assert await until(reached), f"the acquire never reached the {step} step"
            if step == "stop":
                await sup.release("tts", "studio:night-1")  # the stage boundary: the console's turn
                assert await until(lambda: sup.services["tts"].op_lock.locked())
                assert sup.lock.state.owner == "opencode"
            if step == "held":
                assert dead.result()["endpoint"].startswith("http://127.0.0.1:")
            end_by_pid(child)
            if step == "queued":
                await sup.release("tts", "studio:night-1")  # its turn comes: the grant finds it gone
            else:
                await sup.tick()  # one monitor pass
                assert sup.lock.state.owner is None, f"{step}: the card is still held by a closed console"
            if step != "held":
                (ended,) = await asyncio.wait_for(asyncio.gather(dead, return_exceptions=True), 30)
                assert isinstance(ended, NsError) and ended.code == "NS-E202", ended
            assert sup.lock.state.owner is None, f"{step}: the card is still held by a closed console"
            events = drain(q)
            assert gone(events, "opencode"), f"{step}: no owner_released holder_gone"
            announced = [e for e in events if e["data"].get("event") == "acquired"
                         and e["data"].get("owner") == "opencode"]  # fmt: skip
            assert (len(announced) == 1) == (step == "held"), announced
            extra.clear()
            slow_spawn["on"] = False
            fake_file(paths).unlink(missing_ok=True)
            await asyncio.wait_for(sup.acquire("llama", "studio:nightly-2", wait=False), 30)
            assert sup.lock.state.owner == "studio:nightly-2"
            assert sup.services["llama"].state == "ready"
        finally:
            if "_stop_locked" in vars(sup):
                delattr(sup, "_stop_locked")
            if dead is not None and not dead.done():
                dead.cancel()
            if dead is not None:
                await asyncio.gather(dead, return_exceptions=True)
            sup.unsubscribe(q)
            await close(sup)

    asyncio.run(main())


# ---------------------------------------------------------------------- 4. the lease time for a proof
@pytest.mark.parametrize(
    ("value", "lease", "warned"),
    [("29", 600.0, True), ("30", 30.0, False), ("600", 600.0, False), ("601", 600.0, True),
     ("abc", 600.0, True), ("", 600.0, False)],
)  # fmt: skip
def test_the_lease_time_can_be_shortened_for_a_proof_only_with_fake_gpu(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str, lease: float, warned: bool
) -> None:
    monkeypatch.setenv("NS_FAKE_GPU_LEASE_S", value)
    paths = engine_test_home(tmp_path / "e")
    clock = {"t": 1000.0}
    sup = engine(paths, clock=lambda: clock["t"])
    assert sup.cfg.lease_expiry_s == lease
    assert len(log_records(paths, "fake_gpu_lease")) == (1 if warned else 0)
    real = engine(real_home(tmp_path / "r"), clock=lambda: clock["t"])
    assert real.cfg.lease_expiry_s == 600.0  # set on a real install: ignored

    async def advance(seconds: float) -> None:
        """Monitor passes over `seconds` of the injected clock, 20 s apart (under the 60 s suspend gap)."""
        while seconds > 0:
            step = min(20.0, seconds)
            clock["t"] += step
            seconds -= step
            await sup.tick()

    async def main() -> None:
        try:
            q = sup.subscribe()
            await sup.acquire("tts", "studio:killed-1")  # the studio child is killed: it never beats
            await sup.tick()
            await advance(lease - 1)
            assert sup.lock.state.owner == "studio:killed-1", "reclaimed before the lease ran out"
            await advance(1)
            assert sup.lock.state.owner is None, "a silent studio lease was not reclaimed"
            assert any(e["data"].get("event") == "lease_expired" for e in drain(q))
            await sup.acquire("tts", "studio:killed-1", wait=False)  # run again: the retry continues
            assert sup.lock.state.owner == "studio:killed-1"
        finally:
            await close(sup)
            await close(real)

    asyncio.run(main())


# ----------------------------------------------------------------- 5. nvidia-smi from the fake file
def test_nvidia_smi_is_answered_from_the_fake_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_run = subprocess.run
    asked: list[Any] = []

    def spy(argv: Any, *a: Any, **kw: Any) -> Any:
        if "nvidia-smi" in " ".join(str(x) for x in (argv if isinstance(argv, list | tuple) else [argv])):
            asked.append(argv)
        return real_run(argv, *a, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    paths = engine_test_home(tmp_path)
    sup = engine(paths)
    st = sup.status()["gpu"]
    assert st is not None, "the simulated card gives no reading"
    assert st == {"name": "Simulated GPU (fake_gpu)", "load_pct": 0.0, "vram_used_mb": 800, "vram_total_mb": 16311,
                  "temp_c": 40.0, "simulated": True}  # fmt: skip  # no file: the defaults (800 MiB base in use)
    others = [{"pid": 4242, "name": "game.exe", "mb": 9000}]

    async def main() -> None:
        try:
            # the default: per-process memory is [N/A], as the GPU PC prints under WDDM
            fake_file(paths).write_text(json.dumps({"name": "Simulated RTX", "services": {"llama": 3000},
                                                    "others": others}), encoding="utf-8")  # fmt: skip
            await sup.acquire("llama", "opencode", "code")
            pid = sup.services["llama"].proc.pid  # type: ignore[union-attr]
            smi = smi_of(sup)
            assert sup.services["llama"].vram_mb is None  # unknown, as on the target
            assert procs.vram_mb(pid, *smi) is None and procs.vram_mb(4242, *smi) is None
            assert procs.gpu_memory_used_mb(*smi) == 12800  # the base, llama and the game, per process or not
            await sup.release("llama", "opencode")
            await sup.stop_all()
            # "mb": the MiB per process (a TCC or Linux driver; never the GPU PC)
            fake_file(paths).write_text(json.dumps({
                "name": "Simulated RTX", "total_mb": 16311, "per_process": "mb", "services": {"llama": 3000},
                "others": others,
            }), encoding="utf-8")  # fmt: skip
            await sup.acquire("llama", "opencode", "code")
            pid = sup.services["llama"].proc.pid  # type: ignore[union-attr]
            assert sup.services["llama"].vram_mb == 3000  # the ready step's sample
            assert procs.vram_mb(pid, *smi) == 3000 and procs.vram_mb(4242, *smi) == 9000
            assert procs.vram_mb(1, *smi) == 0
            assert procs.gpu_memory_used_mb(*smi) == 12800 and procs.gpu_name(*smi) == "Simulated RTX"
            report = procs.nvidia_smi_report(*smi)
            assert report is not None and "game.exe" in report and "12800MiB /  16311MiB" in report
            sup.gpu_probe = gpu.source_probe(sup.smi)  # a fresh probe: no 2 s cache in the way
            assert sup.status()["gpu"] == {"name": "Simulated RTX", "load_pct": 0.0, "vram_used_mb": 12800,
                                           "vram_total_mb": 16311, "temp_c": 40.0, "simulated": True}  # fmt: skip
            ready = [r for r in log_records(paths, "service_ready") if r["data"].get("vram_mb") == 3000]
            assert ready and ready[-1]["data"].get("card") == "simulated (fake_gpu)"  # the number says so
        finally:
            await close(sup)

    asyncio.run(main())
    assert asked == [], "the real nvidia-smi was run while the switch is in effect"
    fake_file(paths).unlink()
    assert procs.gpu_memory_used_mb(*smi_of(sup)) == 800  # without the file: the base, nothing of ours runs


def test_the_simulated_card_answers_in_nvidia_smis_own_shapes(tmp_path: Path) -> None:
    """The fake's text is what the product's parsers read from the real tool: the RTX 5060 Ti's own report is
    the reference for the layout, and every failure comes out as the real tool's does."""
    paths = engine_test_home(tmp_path)
    assert hasattr(gpu, "FakeSmi"), "there is no simulated card"
    slept: list[float] = []
    fake = gpu.FakeSmi(paths, tenants=lambda: {4242: "llama"}, sleep=slept.append)
    src = gpu.SmiSource(fake, simulated=True)
    mine = (src.query([]) or "").splitlines()
    assert "CUDA Version: 13.0" in mine[2] and "16311MiB" in mine[9]
    assert "llama-server.exe" in mine[18] and mine[18].rstrip().endswith("N/A      |")  # WDDM: N/A
    compute = ["--query-compute-apps=pid,used_memory", "--format=csv,noheader"]
    assert src.query(compute) == "4242, [N/A]\n"  # what the GPU PC prints for a compute app under WDDM
    fake_file(paths).write_text('{"per_process": "mb"}', encoding="utf-8")
    assert re.fullmatch(r"4242, \d+ MiB\n", src.query(compute) or "")
    fake_file(paths).write_text('{"load_pct": null, "temp_c": null}', encoding="utf-8")
    assert src.query(["--query-gpu=utilization.gpu,temperature.gpu", "--format=csv,noheader"]) == "[N/A], [N/A]\n"
    assert (gpu.parse_nvidia_smi(src.query(gpu.PROBE_ARGS) or "") or GpuStatus(name="-")).load_pct is None
    fake_file(paths).unlink()
    assert gpu.parse_nvidia_smi(src.query(gpu.PROBE_ARGS) or "") is not None
    assert src.query(["--query-gpu=bogus", "--format=csv,noheader"]) is None  # exit code 2, as the real one
    for fail, waited in (("missing", []), ("driver", []), ("hang", [5.0])):
        fake_file(paths).write_text(json.dumps({"fail": fail}), encoding="utf-8")
        slept.clear()
        assert src.query(gpu.PROBE_ARGS, 5.0) is None and slept == waited, fail
        if fail == "driver":
            done = fake(list(gpu.PROBE_ARGS), 5.0)
            assert done.returncode == 9 and done.stdout.startswith("NVIDIA-SMI has failed")
    # one bad file among good ones: the defaults for what is wrong, one WARN per content
    log = NsLogger("supervisor", paths.logs)
    fake2 = gpu.SmiSource(gpu.FakeSmi(paths, tenants=dict, log=log), simulated=True)
    fake_file(paths).write_text('{"total_mb": -5, "others": [{"pid": 7, "mb": 99999}, "x"]}', encoding="utf-8")
    assert fake2.query(["--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"]) == "16311, 16311\n"
    fake2.query([])
    fake_file(paths).write_text("{not json", encoding="utf-8")
    assert fake2.query(["--query-gpu=name", "--format=csv,noheader"]) == "Simulated GPU (fake_gpu)\n"
    assert len(log_records(paths, "fake_gpu_file")) == 2  # two contents, one WARN each


#: The GPU PC's state in the recording (tests/fixtures/supervisor/nvidia-smi-rtx-5060-ti.txt): one RTX 5060 Ti,
#: driver 580.88 / CUDA 13.0, 812 of 16311 MiB in use, 2 %, 38 C, and dwm.exe (C+G, N/A) on it.
RECORDED_STATE = {
    "name": "NVIDIA GeForce RTX 5060 Ti", "base_mb": 812, "load_pct": 2, "temp_c": 38,
    "others": [{"pid": 1234, "type": "C+G", "name": "C:\\Windows\\System32\\dwm.exe"}],
}  # fmt: skip


def test_for_the_recorded_state_the_fake_prints_the_recording_and_parses_the_same(tmp_path: Path) -> None:
    """Faithfulness (review of fc689b8): the GPU PC's own nvidia-smi output and the fake's output for the same
    state are the same bytes, and every parser of the product reads the same from both."""
    from datetime import datetime

    assert hasattr(gpu, "FakeSmi"), "there is no simulated card"
    paths = engine_test_home(tmp_path)
    fake_file(paths).write_text(json.dumps(RECORDED_STATE), encoding="utf-8")
    src = gpu.SmiSource(
        gpu.FakeSmi(paths, tenants=dict, now=lambda: datetime(2026, 11, 9, 3, 30, 11)), simulated=True
    )
    recorded = FIXTURE.read_text(encoding="utf-8")
    assert src.query([]) == recorded  # the whole table, byte for byte (date, padding, N/A, widths)
    # the numbers the table shows, read the same way out of both
    cells = re.compile(r"(\d+)C\s+P\d.*?\|\s+(\d+)MiB /\s+(\d+)MiB \|\s+(\d+)%")
    assert cells.search(recorded) is not None
    assert cells.findall(src.query([]) or "") == cells.findall(recorded) == [("38", "812", "16311", "2")]
    # the parsers, fed the recorded line shapes and the fake's answers for the same state
    probe = gpu.parse_nvidia_smi(src.query(gpu.PROBE_ARGS) or "")
    assert probe == gpu.parse_nvidia_smi(LINE_5060) == GpuStatus(
        name="NVIDIA GeForce RTX 5060 Ti", load_pct=2.0, vram_used_mb=812, vram_total_mb=16311, temp_c=38.0
    )

    def answers(text: dict[str, str]) -> Callable[..., str | None]:
        return lambda args, smi=None: text[args[0]] if args else recorded

    real = answers({  # the recorded shapes: test_supervisor_procs.py and the fixture
        "--query-gpu=name": "NVIDIA GeForce RTX 5060 Ti\n", "--query-gpu=memory.used": "812\n",
        "--query-compute-apps=pid,used_memory": "",  # dwm.exe is C+G, not a compute app
    })  # fmt: skip
    for fn, args in ((procs.gpu_name, ()), (procs.gpu_memory_used_mb, ()), (procs.vram_mb, (1234,))):
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(procs, "_nvsmi", real)
            from_real = fn(*args)  # type: ignore[operator]
        assert fn(*args, src) == from_real, fn.__name__  # type: ignore[operator]
    # a compute app under WDDM: the recorded "77, [N/A]" and the fake's default give the same unknown
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(procs, "_nvsmi", lambda args, smi=None: "77, [N/A]\n")
        assert procs.vram_mb(77) is None
    on_card = gpu.SmiSource(gpu.FakeSmi(paths, tenants=lambda: {77: "llama"}), simulated=True)
    assert procs.vram_mb(77, on_card) is None


def test_ns_verify_never_gives_a_simulated_number_as_evidence(tmp_path: Path) -> None:
    """Review blocker 2: on a GPU-profile test install with the switch, `ns verify`'s idle-VRAM check is
    "n/a: simulated graphics card (fake_gpu)" (no benchmark), and `machine.gpu` carries the mark."""
    from ns_supervisor import verify
    from ns_supervisor.testing import StubTable, SyncSupervisor

    paths = Paths(tmp_path / "ns")
    rt = Runtime.default("gpu").model_copy(update={"fake_footage": True, **({"fake_gpu": True} if SWITCH else {})})
    save_runtime(paths, rt)
    sync = SyncSupervisor(paths, rt, resolver=StubTable())
    try:
        deps = verify.Deps(whisper=lambda p, r: 1.5, nvenc=lambda: None, job=lambda p: 3.0, ram=lambda: 32.0,
                           sleep=lambda s: None)  # fmt: skip
        report = verify.run_verify(paths, rt, sync, deps=deps)
    finally:
        sync.close()
    idle = next(c for c in report.checks if c.name == "vram.idle")
    assert idle.value == "n/a: simulated graphics card (fake_gpu)", idle
    assert "idle_vram_mb" not in report.benchmarks
    assert report.machine["gpu"] == "simulated (fake_gpu): Simulated GPU (fake_gpu)"


def test_every_reader_of_the_card_marks_a_simulated_reading() -> None:
    """Review blocker 2, the class: a module that asks the card (`smi_source`, or a `procs` reader given a
    source) must take the mark from `gpu.simulated_mark` (directly, or through `gpu.source_probe`). A new
    reader that does not fails here."""
    root = repo_root()
    asks = re.compile(r"\bsmi_source\(|procs\.(?:vram_mb|gpu_memory_used_mb|nvidia_smi_report|gpu_name)\(")
    out = subprocess.run(["git", "ls-files", "ns_*/*.py", "ns_*/**/*.py", "installer/*.py", "installer/**/*.py",
                          "dev/*.py", "proofs/*.py"], cwd=root, capture_output=True, text=True,
                         check=True).stdout.split()  # fmt: skip
    readers = {}
    for rel in sorted(set(out)):
        if rel in ("ns_supervisor/gpu.py", "ns_supervisor/procs.py"):
            continue
        text = (root / rel).read_text(encoding="utf-8")
        if asks.search(text):
            readers[rel] = "simulated_mark(" in text or "source_probe(" in text
    assert readers, "no reader of the card found: the pattern is stale"
    assert set(readers) >= {"ns_supervisor/core.py", "ns_supervisor/doctor.py", "ns_supervisor/verify.py"}
    unmarked = sorted(rel for rel, ok in readers.items() if not ok)
    assert not unmarked, f"these read the card but never ask gpu.simulated_mark: {unmarked}"


# ----------------------------------------------------------------- 6. simulated, and only then
def test_status_says_simulated_only_when_it_is(tmp_path: Path) -> None:
    from ns_dashboard.svc_engine import EngineService

    fake = engine(engine_test_home(tmp_path / "f"))
    st = fake.status()
    assert st["gpu"] is not None and st["gpu"].get("simulated") is True, "/status does not say simulated"
    assert (EngineService._gpu(st["gpu"]) or {}).get("simulated") is True  # /api/status passes it on
    # every other install: the block is byte for byte what the base answers (snapshot of the base)
    real = engine(real_home(tmp_path / "r", by_hand=False), gpu_probe=gpu.NvidiaSmiGpuProbe(runner=lambda: LINE_5060))
    rst = real.status()
    assert json.dumps(rst["gpu"]) == (
        '{"name": "NVIDIA GeForce RTX 5060 Ti", "load_pct": 2.0, "vram_used_mb": 812, '
        '"vram_total_mb": 16311, "temp_c": 38.0}'
    )
    assert sorted(rst) == ["busy", "gpu", "gpu_owner", "gpu_service", "holder_pid", "services", "session",
                           "shutting_down", "uptime_s", "waiting"]  # fmt: skip
    assert "simulated" not in (EngineService._gpu(rst["gpu"]) or {"simulated": "no block"})
    assert GpuStatus(name="x").model_dump(mode="json") == {
        "name": "x", "load_pct": None, "vram_used_mb": None, "vram_total_mb": None, "temp_c": None,
    }  # fmt: skip
    dev = engine(engine_test_home(tmp_path / "off", switch=False))
    assert dev.status()["gpu"] is None  # an engine test without the switch: no card, as before


# ---------------------------------------------------------- 7. setup's answer, Repair and rollback
def test_the_switch_comes_from_setup_answers_and_survives_a_repair_and_a_rollback(tmp_path: Path) -> None:
    from installer.stage2 import reinstall
    from installer.stage2.steps.s01_questions import S01Questions
    from installer.stage2.steps.s07_configs import apply_engine_test
    from ns_common.config_compat import for_version

    assert "fake_gpu" in InstallerAnswers.model_fields, "answers.json has no fake_gpu answer"
    raw = example_answers(allow_cpu=True, fake_voice=True, fake_gpu=True)
    ctx = make_test_context(tmp_path / "e", raw_answers=raw, resolve=True, preflight={"profile": "cpu-dev"})
    assert ctx.fake_gpu is True
    ensure_baseline(ctx)
    on_disk = json.loads(ctx.paths.runtime_file.read_text(encoding="utf-8"))
    assert on_disk["fake_gpu"] is True and devmode.fake_gpu(ctx.paths)
    # Repair with an answers file that leaves it out: the kept answers give it back
    assert "fake_gpu" in reinstall.SIMPLE_KEYS and "fake_gpu" in reinstall.INHERITED
    again = make_test_context(tmp_path / "r", raw_answers=example_answers(allow_cpu=True, fake_voice=True),
                              preflight={"profile": "cpu-dev"})  # fmt: skip
    reinstall.inherit(again, {"fake_gpu": True})
    assert again.raw_answers["fake_gpu"] is True
    # s07 puts the answer into an existing runtime.json, both ways
    rt = Runtime.default("cpu-dev")
    apply_engine_test(ctx, rt)
    assert rt.fake_gpu is True
    off = make_test_context(tmp_path / "o", raw_answers=example_answers(allow_cpu=True, fake_voice=True),
                            resolve=True, preflight={"profile": "cpu-dev"})  # fmt: skip
    apply_engine_test(off, rt)
    assert rt.fake_gpu is False
    # a normal answers file: refused, and setup says NS-W121 on its console
    normal = make_test_context(tmp_path / "n", raw_answers=example_answers(fake_gpu=True))
    normal.free_gb_fn = lambda _p: 500.0
    S01Questions().run(normal)
    assert normal.fake_gpu is False and build_runtime(normal).fake_gpu is False
    assert [c for c, _d in normal.warnings].count("NS-W121") == 1
    # a rollback to 0.1.14 takes it out of runtime.json and answers.json, and 0.1.14 loads both
    from tests import frozen_config_models_0_1_14 as old_cfg
    from tests import frozen_models_0_1_14 as old

    data = dict(on_disk)
    assert for_version("runtime.json", data, "0.1.14") == ["fake_gpu"]
    old.Runtime.model_validate(data)
    answers = {**example_answers(allow_cpu=True, fake_voice=True), "fake_gpu": True}
    assert "fake_gpu" in for_version("answers.json", answers, "0.1.14")
    old_cfg.InstallerAnswers.model_validate(answers)


# ------------------------------------------------------------------ 8. never from the dashboard
def test_the_dashboard_cannot_set_it(tmp_home: Path) -> None:
    from ns_dashboard.errors import ApiError
    from ns_dashboard.handlers import Dashboard, build_state
    from ns_dashboard.runtime_flags import HIDDEN, TEST_SWITCHES
    from ns_dashboard.supervisor_link import SupervisorLink

    assert "fake_gpu" in TEST_SWITCHES and "fake_gpu" in HIDDEN
    paths = Paths.from_env()
    dash = Dashboard(build_state(paths, fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)))
    save_runtime(paths, Runtime.default("cpu-dev").model_copy(update={"fake_footage": True}))
    before = paths.runtime_file.read_text(encoding="utf-8")
    for body in ({"flags": {"fake_gpu": True}, "unlock": True}, {"flags": {"fake_gpu": "true"}, "unlock": True}):
        with pytest.raises(ApiError) as ei:
            dash.settings.put_settings(body)
        assert ei.value.status == 422 and ei.value.code == "test_switch" and "fake_gpu" in ei.value.message
    assert paths.runtime_file.read_text(encoding="utf-8") == before
    assert dash.settings.get_settings()["fake_gpu"] is False
    save_runtime(paths, Runtime.default("cpu-dev").model_copy(update={"fake_footage": True, "fake_gpu": True}))
    s = dash.settings.get_settings()
    assert s["fake_gpu"] is True and not any(k.startswith("fake_gpu") for k in s["flags"])  # read-only row
    save_runtime(paths, Runtime.default("gpu").model_copy(update={"fake_gpu": True}))
    assert dash.settings.get_settings()["fake_gpu"] is False  # a real install: not in effect, not shown on


# ------------------------------------------------------------- 9. without the switch, as today
def test_with_fake_gpu_off_a_cpu_install_behaves_as_today(tmp_path: Path) -> None:
    """The empty case: an engine-test install without the switch takes no lock and has no card, exactly as
    before H-10 (tests/test_gpu_release.py and tests/test_supervisor_gpu.py run unchanged beside this)."""
    paths = engine_test_home(tmp_path, switch=False)
    rt = load_runtime(paths)
    assert not any(build_service(rt, paths, n).gpu for n in SERVICES)
    sup = engine(paths)
    assert not sup._whisper_gpu() and sup.status()["gpu"] is None
    assert getattr(sup, "smi", None) is getattr(gpu, "REAL_SMI", None)  # the real tool (nothing asks it here)

    async def main() -> None:
        try:
            await sup.acquire("llama", "opencode", "code")
            assert sup.lock.state.owner is None  # no GPU service: no lock, as on the base
            await sup.acquire("whisper", "studio:j1")
            assert sup.lock.state.owner is None
        finally:
            await close(sup)

    asyncio.run(main())


# --------------------------------------------------------------- one faithful fake of the card
#: Everything else in the tree that stands in for the graphics card or nvidia-smi, and why it may.
#: A new one fails `test_there_is_one_fake_of_the_graphics_card`: fold it into `ns_supervisor.gpu.FakeSmi`.
ALLOWED: dict[tuple[str, str], str] = {
    ("tests/test_supervisor_procs.py", "seam"):
        "replays nvidia-smi's own recorded output (the RTX 5060 Ti fixture, a WDDM [N/A] line) into the parsers",
    ("tests/test_supervisor_gpu.py", "dep"):
        "feeds nvidia-smi's own recorded lines to the status parser and its cache; no card state is invented",
    ("tests/test_supervisor_holder_pid.py", "seam"):
        "slows the ready step of a real-card install (it answers None, unknown): a timing seam, no numbers",
    ("tests/test_dev_mode_core.py", "seam"): "the tool-missing answer on a real install with no card",
    ("tests/test_dev_mode_core.py", "dep"): "ns verify's machine probes on a dev install, which asks no card",
    ("tests/test_ns_doctor.py", "dep"): "the zip's nvidia-smi.txt is copied as given: the caller's own seam",
    ("tests/test_ns_verify.py", "dep"): "ns verify's own probes of a real-card install (the gate's numbers)",
    ("tests/test_atomic_writes.py", "dep"): "the tool-missing answer while the zip is written atomically",
    ("tests/test_supervisor_child_env.py", "seam"): "the REAL runner's environment: which() only names it",
    ("installer/stage2/sysinfo.py", "card"):
        "stage 1's preflight hand-over for --simulate-downloads (a PowerShell probe, before any Python runs)",
    ("installer/stage2/web/fakes.py", "card"): "the setup window's sample hand-over (stage 1, as above)",
    ("ns_dashboard/ui/src/lib/demo.ts", "card"): "the dashboard's demo mode (no engine at all), screenshots",
    ("ns_supervisor/gpu.py", "dep"): "the simulated card itself",
}
_GPU = r"(?:Gpu|GPU|Smi|SMI|Nvidia|NVIDIA|Vram|VRAM|Cuda|CUDA)"
RULES = {
    "class": re.compile(rf"^\s*class\s+(\w*(?:Fake|Stub|Mock|Dummy|Sim)\w*{_GPU}\w*|\w*{_GPU}\w*(?:Fake|Stub|Mock|Dummy))\b",
                        re.M),  # fmt: skip
    "seam": re.compile(r"""setattr\(\s*[\w.]+,\s*["'](_nvsmi|run_nvidia_smi|run_real|REAL_SMI|vram_mb|"""
                       r"""gpu_memory_used_mb|nvidia_smi_report|gpu_name|smi_source|source_probe)["']"""
                       r"""|setattr\(\s*gpu\.shutil,\s*["']which["']"""),  # fmt: skip
    "dep": re.compile(r"\b(?:smi|vram_used|gpu_name|gpu_probe)\s*=\s*(?:lambda|Fake\w*)"
                      r"|NvidiaSmiGpuProbe\([^)]*runner\s*=|SmiSource\("),  # fmt: skip
    "card": re.compile(r"""["']?name["']?\s*:\s*["'][^"'\n]*(?:RTX|GeForce|Quadro|Simulated)"""),
}


def test_there_is_one_fake_of_the_graphics_card() -> None:
    """ONE faithful fake per external system: `ns_supervisor/gpu.py:FakeSmi` is the only stand-in for the
    card that invents its state, and every other place that touches the card's answers is on `ALLOWED` with
    the reason it is not a second fake (a recording of the real tool, a caller's own seam, stage 1's probe)."""
    root = repo_root()
    assert getattr(gpu, "FakeSmi", None) is not None, "ns_supervisor/gpu.py has no FakeSmi"
    out = subprocess.run(["git", "ls-files", "*.py", "*.ts", "*.tsx", "*.ps1"], cwd=root, capture_output=True,
                         text=True, check=True).stdout.split()  # fmt: skip
    found: set[tuple[str, str]] = set()
    classes: list[str] = []
    for rel in out:
        if rel == "tests/test_fake_gpu.py":
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for kind, rx in RULES.items():
            if kind == "card" and rel.startswith("tests/"):
                continue  # parser inputs in tests are recordings, not stand-ins
            for m in rx.finditer(text):
                if kind == "class":
                    classes.append(f"{rel}:{m.group(1)}")
                else:
                    found.add((rel, kind))
    assert classes == ["ns_supervisor/gpu.py:FakeSmi"], f"another fake of the graphics card: {classes}"
    unknown = sorted(found - set(ALLOWED))
    assert not unknown, f"stand-ins for the card outside FakeSmi (fold them in, or say why in ALLOWED): {unknown}"
    stale = sorted(set(ALLOWED) - found)
    assert not stale, f"ALLOWED names places that no longer stand in for the card: {stale}"
