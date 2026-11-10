"""F-H47: closing the Nightshift Code console frees the GPU within one supervisor monitor pass.

Acceptance tests written before the build (card refs/cards/F-H47.md, section 3 and the Cases table).

THE INTERFACE THE BUILDER PROVIDES (the tests below use exactly these names):

1. ``Supervisor(..., process_probe: Callable[[int], float | None] | None = None)``: a keyword next to
   ``clock``, ``starter``, ``gpu_probe`` and ``in_use``. ``process_probe(pid)`` answers the creation time of
   the RUNNING process with that pid (any float that stays the same for the life of one process), or None
   when no running process has that pid. None given = the real probe (``psutil``, which ns_supervisor already
   uses in ``presence.py``). The real probe must answer None for a process that has exited even while another
   program still holds a handle to it (Windows keeps such a pid allocated): the tests keep the ``Popen``
   handle open.
2. ``Supervisor.acquire(service, owner, model_alias=None, wait=True, timeout_s=7200, pid=None)``, ``pid: int |
   None``: when the GPU lock is granted to ``owner`` (before the model starts loading), the supervisor records
   ``pid`` and ``process_probe(pid)`` as the holder.
3. ``Supervisor.tick()`` (one monitor pass): when the lock holder was recorded with a pid and
   ``process_probe(pid)`` is None or differs from the recorded creation time (pid reuse), the holder is
   released like ``release_owner`` (a model still loading for it is abandoned), the log has a WARN
   ``owner_released`` with ``reason="holder_gone"``, and the event ``{"event": "owner_released", "owner": ...,
   "reason": "holder_gone"}`` (kind ``service``) is published. An owner without a pid is never probed.
4. ``Supervisor.status()["holder_pid"]`` (and ``GET /status``): the pid recorded for the current holder,
   None when nobody holds the GPU or the holder gave no pid.
5. ``POST /acquire`` (``AcquireBody``) takes ``pid: int | None``; missing means no pid check, a pid that is
   not a positive integer is refused with 422.
6. ``SupervisorClient.acquire(..., pid: int | None = None)`` sends ``"pid"`` in the ``/acquire`` body when
   given.
7. ``launchers/nightshift-code.ps1`` sends ``pid = $PID`` in its ``/acquire`` body (tests/test_launchers.py).

Controls that must pass before and after the build: an owner without a pid keeps the old short-call
semantics, and the ``studio:`` and Assistant leases still expire by heartbeat only.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from ns_common.errors import NsError
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_supervisor.client import SupervisorClient
from ns_supervisor.core import Supervisor
from ns_supervisor.gpu import NullGpuProbe
from ns_supervisor.testing import StubTable, fast_config, wait_until

HOURS_3 = 3 * 3600.0
STEP_S = 60.0  # one monitor pass a minute of injected time (not a suspend gap: suspend_gap_s is 60)

# ---------------------------------------------------------------- helpers


@pytest.fixture
def sleeper() -> Iterator[Callable[[], subprocess.Popen[bytes]]]:
    """Start dummy python children that sleep (stand-ins for the launcher's PowerShell); all killed after.

    The base interpreter, not the venv's redirector: killing a venv python.exe would leave its real child
    running, and the recorded pid would be the redirector's.
    """
    started: list[subprocess.Popen[bytes]] = []
    exe = getattr(sys, "_base_executable", None) or sys.executable

    def start() -> subprocess.Popen[bytes]:
        p = subprocess.Popen(
            [exe, "-I", "-c", "import time; time.sleep(120)"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        started.append(p)
        return p

    yield start
    for p in started:
        if p.poll() is None:
            p.kill()
        p.wait(timeout=10)


def pid_kw(pid: int) -> dict[str, Any]:
    """`pid=` for `Supervisor.acquire`, left out where it does not take one (the base: an assertion fails)."""
    return {"pid": pid} if "pid" in inspect.signature(Supervisor.acquire).parameters else {}


def client_pid_kw(pid: int) -> dict[str, Any]:
    """`pid=` for `SupervisorClient.acquire`, left out where it does not take one."""
    return {"pid": pid} if "pid" in inspect.signature(SupervisorClient.acquire).parameters else {}


def kill(p: subprocess.Popen[bytes]) -> None:
    """The console is closed with X: the process ends at once. The handle stays open on purpose."""
    p.kill()
    p.wait(timeout=10)
    assert p.poll() is not None


def make(
    tmp_path: Path,
    *,
    probe: Callable[[int], float | None] | None = None,
    table: StubTable | None = None,
    **cfg: Any,
) -> tuple[Supervisor, dict[str, float]]:
    """A supervisor on stub services with an injected clock; `probe` fakes the process table."""
    clock = {"t": 1000.0}
    takes_probe = "process_probe" in inspect.signature(Supervisor.__init__).parameters
    kw: dict[str, Any] = {"process_probe": probe} if probe is not None and takes_probe else {}
    sup = Supervisor(
        Paths(tmp_path / "ns"),
        Runtime.default("gpu"),
        config=fast_config(**{"lease_expiry_s": 600, **cfg}),
        resolver=table or StubTable(),
        clock=lambda: clock["t"],
        gpu_probe=NullGpuProbe(),
        **kw,
    )
    return sup, clock


async def close(sup: Supervisor) -> None:
    await sup.stop_all(include_persistent=True)
    sup.close_job()


def drain(q: asyncio.Queue[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def holder_gone_events(events: list[dict[str, Any]], owner: str) -> list[dict[str, Any]]:
    return [
        e
        for e in events
        if e["kind"] == "service"
        and e["data"].get("event") == "owner_released"
        and e["data"].get("owner") == owner
        and e["data"].get("reason") == "holder_gone"
    ]


def holder_gone_log(paths: Paths, owner: str) -> list[dict[str, Any]]:
    recs: list[dict[str, Any]] = []
    for f in paths.logs.glob("supervisor-*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            data = rec.get("data") or {}
            if (
                rec.get("event") == "owner_released"
                and data.get("reason") == "holder_gone"
                and data.get("owner") == owner
            ):
                recs.append(rec)
    return recs


async def advance(sup: Supervisor, clock: dict[str, float], seconds: float) -> None:
    """Run monitor passes over `seconds` of injected time, one every STEP_S."""
    end = clock["t"] + seconds
    while clock["t"] < end:
        clock["t"] += STEP_S
        await sup.tick()


# ---------------------------------------------------------------- section 3: the supervisor


def test_a_holder_whose_process_is_gone_loses_the_gpu_within_one_monitor_pass(
    tmp_path: Path, sleeper: Callable[[], subprocess.Popen[bytes]]
) -> None:
    sup, _clock = make(tmp_path)
    child = sleeper()

    async def main() -> None:
        q = sup.subscribe()
        try:
            await sup.acquire("llama", "opencode", "code", **pid_kw(child.pid))
            await sup.tick()
            assert sup.lock.state.owner == "opencode"  # alive: kept
            kill(child)
            await sup.tick()  # one monitor pass
            assert sup.lock.state.owner is None
            assert holder_gone_events(drain(q), "opencode"), "no owner_released event with reason holder_gone"
            assert holder_gone_log(sup.paths, "opencode"), "no owner_released WARN with reason holder_gone"
            await sup.acquire("llama", "studio:nightly-1", wait=False)  # the nightly is not made to wait
            assert sup.lock.state.owner == "studio:nightly-1"
        finally:
            sup.unsubscribe(q)
            await close(sup)

    asyncio.run(main())


def test_a_live_but_silent_holder_keeps_the_gpu_for_hours(
    tmp_path: Path, sleeper: Callable[[], subprocess.Popen[bytes]]
) -> None:
    """opencode idle while the person thinks: no heartbeat, never expired while its console lives."""
    sup, clock = make(tmp_path)
    child = sleeper()

    async def main() -> None:
        q = sup.subscribe()
        try:
            await sup.acquire("llama", "opencode", "code", **pid_kw(child.pid))
            await advance(sup, clock, HOURS_3)
            assert sup.lock.state.owner == "opencode"
            assert sup.status().get("holder_pid") == child.pid
            assert not [e for e in drain(q) if e["data"].get("event") in ("owner_released", "lease_expired")]
            with pytest.raises(NsError) as err:
                await sup.acquire("llama", "studio:nightly-1", wait=False)
            assert err.value.code == "NS-E201"  # the live holder keeps the card
        finally:
            sup.unsubscribe(q)
            await close(sup)

    asyncio.run(main())


def test_a_reused_pid_is_not_the_old_holder(tmp_path: Path) -> None:
    table = {4242: 100.0}  # fake process table: pid -> creation time
    sup, clock = make(tmp_path, probe=lambda pid: table.get(pid))

    async def main() -> None:
        q = sup.subscribe()
        try:
            await sup.acquire("llama", "opencode", "code", **pid_kw(4242))
            await advance(sup, clock, 600)
            assert sup.lock.state.owner == "opencode"
            table[4242] = 900.0  # the console died and another program now runs under the same pid
            await sup.tick()
            assert sup.lock.state.owner is None
            assert sup.status().get("holder_pid") is None
            assert holder_gone_events(drain(q), "opencode")
        finally:
            sup.unsubscribe(q)
            await close(sup)

    asyncio.run(main())


def test_an_owner_without_a_pid_behaves_as_before(tmp_path: Path) -> None:
    """Control (passes before and after the build): voice preview and `ns verify` give no pid."""
    sup, clock = make(tmp_path)

    async def main() -> None:
        try:
            for owner, service in (
                ("dashboard:preview-1", "tts"),
                ("verify", "llama"),
                ("opencode", "llama"),
            ):
                await sup.acquire(service, owner)
                await advance(sup, clock, HOURS_3)  # a short call is never expired and never probed
                assert sup.status()["gpu_owner"] == owner
                assert await sup.release(service, owner) is True
                assert sup.status()["gpu_owner"] is None
        finally:
            await close(sup)

    asyncio.run(main())


def test_studio_and_assistant_leases_still_expire_by_heartbeat_only(tmp_path: Path) -> None:
    """Control (passes before and after the build): `studio:` and the Assistant keep the heartbeat lease."""
    sup, clock = make(tmp_path, lease_expiry_s=600)

    async def main() -> None:
        q = sup.subscribe()
        try:
            for owner in ("studio:night-1", "dashboard:assistant"):
                await sup.acquire("llama", owner)
                await sup.tick()  # the monitor notices the holder
                for _ in range(15):  # 15 minutes, beating: kept
                    clock["t"] += STEP_S
                    assert sup.heartbeat(owner) is True
                    await sup.tick()
                assert sup.status()["gpu_owner"] == owner
                await advance(sup, clock, 660)  # silent for longer than lease_expiry_s: reclaimed
                assert sup.status()["gpu_owner"] is None
                expired = [e for e in drain(q) if e["data"].get("event") == "lease_expired"]
                assert [e["data"]["owner"] for e in expired] == [owner]
        finally:
            sup.unsubscribe(q)
            await close(sup)

    asyncio.run(main())


def test_the_dashboard_release_button_still_refuses_a_live_holder(
    tmp_path: Path, sleeper: Callable[[], subprocess.Popen[bytes]]
) -> None:
    """The pid check changes nothing in the Release button: a beating lease is refused, a foreign one not."""
    sup, clock = make(tmp_path)
    child = sleeper()

    async def main() -> None:
        try:
            await sup.acquire("llama", "studio:night-1", **pid_kw(child.pid))
            assert sup.heartbeat("studio:night-1") is True
            clock["t"] += 30
            await sup.tick()
            refused = await sup.release_unless_alive("studio:night-1")
            assert refused["ok"] is False and refused["refused"] == "alive"
            assert sup.status()["gpu_owner"] == "studio:night-1"
            await sup.release("llama", "studio:night-1")
            await sup.acquire("llama", "opencode", "code", **pid_kw(child.pid))
            await sup.tick()
            assert sup.lock.state.owner == "opencode"  # alive: the monitor keeps it
            assert await sup.release_unless_alive("opencode") == {
                "ok": True
            }  # the owner's button still frees it
            assert sup.status()["gpu_owner"] is None and sup.status().get("holder_pid") is None
        finally:
            await close(sup)

    asyncio.run(main())


def test_the_status_answer_names_the_holder_pid(
    tmp_path: Path, sleeper: Callable[[], subprocess.Popen[bytes]]
) -> None:
    sup, _clock = make(tmp_path)
    child = sleeper()

    async def main() -> None:
        try:
            assert "holder_pid" in sup.status() and sup.status()["holder_pid"] is None  # nobody holds the GPU
            await sup.acquire("llama", "opencode", "code", **pid_kw(child.pid))
            st = sup.status()
            assert st["gpu_owner"] == "opencode" and st.get("holder_pid") == child.pid
            assert await sup.release("llama", "opencode") is True
            assert sup.status().get("holder_pid") is None
            await sup.acquire("tts", "dashboard:preview-1")  # a holder without a pid
            assert sup.status().get("holder_pid") is None
        finally:
            await close(sup)

    asyncio.run(main())


# ---------------------------------------------------------------- section 4: cases


def test_second_run_after_the_holder_vanished_records_the_new_pid(
    tmp_path: Path, sleeper: Callable[[], subprocess.Popen[bytes]]
) -> None:
    """Case "second run": the console is opened again after the first one was closed with X."""
    sup, clock = make(tmp_path)
    first, second = sleeper(), sleeper()

    async def main() -> None:
        try:
            await sup.acquire("llama", "opencode", "code", **pid_kw(first.pid))
            kill(first)
            await sup.tick()
            assert sup.lock.state.owner is None
            await sup.acquire("llama", "opencode", "code", wait=False, **pid_kw(second.pid))
            assert sup.status().get("holder_pid") == second.pid
            await advance(sup, clock, 600)
            assert sup.lock.state.owner == "opencode"  # the new console is alive: kept
            kill(second)
            await sup.tick()
            assert sup.lock.state.owner is None
        finally:
            await close(sup)

    asyncio.run(main())


def test_a_console_closed_while_the_model_loads_frees_the_gpu_and_abandons_the_load(
    tmp_path: Path, sleeper: Callable[[], subprocess.Popen[bytes]]
) -> None:
    """Case "killed halfway": X during 'Loading the coding model'. The lock goes at once, the load stops."""
    table = StubTable(extra={"llama": ["--ready-delay", "20"]})
    sup, _clock = make(tmp_path, table=table, health_timeout_s=60)
    child = sleeper()

    async def main() -> None:
        loading = asyncio.create_task(sup.acquire("llama", "opencode", "code", **pid_kw(child.pid)))
        try:
            assert await asyncio.to_thread(
                wait_until,
                lambda: sup.lock.state.owner == "opencode" and sup.services["llama"].state == "starting",
                15,
            )
            kill(child)
            await sup.tick()  # one monitor pass, while llama is still loading
            assert sup.lock.state.owner is None
            # the nightly gets the card now, not when the abandoned load would have finished (20 s)
            await asyncio.wait_for(sup.acquire("tts", "studio:nightly-1", wait=False), 10)
            assert sup.lock.state.owner == "studio:nightly-1"
            with pytest.raises((NsError, asyncio.CancelledError)):
                await asyncio.wait_for(loading, 10)  # the dead console's acquire does not succeed
            assert sup.services["llama"].state != "ready" and sup.services["llama"].proc is None
        finally:
            if not loading.done():
                loading.cancel()
            await asyncio.gather(loading, return_exceptions=True)
            await close(sup)

    asyncio.run(main())


def test_with_no_holder_a_monitor_pass_does_nothing(tmp_path: Path) -> None:
    """Case "empty": no holder, or a holder without a pid: nothing is probed or released."""
    asked: list[int] = []

    def probe(pid: int) -> float | None:
        asked.append(pid)
        return 1.0

    sup, clock = make(tmp_path, probe=probe)

    async def main() -> None:
        q = sup.subscribe()
        try:
            await advance(sup, clock, 600)
            assert sup.lock.state.owner is None and asked == []
            assert not [e for e in drain(q) if e["data"].get("event") == "owner_released"]
            await sup.acquire("tts", "dashboard:preview-1")
            await advance(sup, clock, 600)
            assert asked == [] and sup.lock.state.owner == "dashboard:preview-1"
        finally:
            sup.unsubscribe(q)
            await close(sup)

    asyncio.run(main())


# ---------------------------------------------------------------- the HTTP app and the client


def test_the_acquire_route_takes_an_optional_integer_pid(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_supervisor.app import create_app

    sup, _clock = make(tmp_path)
    me = os.getpid()
    with TestClient(create_app(sup, run_monitor=False)) as tc:
        for bad in ("abc", 0, -5, 1.5):
            r = tc.post("/acquire", json={"service": "llama", "owner": "opencode", "pid": bad})
            assert r.status_code == 422, (bad, r.status_code, r.text)
        assert tc.get("/status").json()["gpu_owner"] is None  # a refused body takes nothing
        r = tc.post("/acquire", json={"service": "llama", "owner": "verify"})  # no pid: as before
        assert r.status_code == 200 and tc.get("/status").json().get("holder_pid") is None
        assert tc.post("/release", json={"service": "llama", "owner": "verify"}).json() == {"ok": True}
        r = tc.post(
            "/acquire", json={"service": "llama", "owner": "opencode", "model_alias": "code", "pid": me}
        )
        assert r.status_code == 200
        st = tc.get("/status").json()
        assert st["gpu_owner"] == "opencode" and st.get("holder_pid") == me


def test_end_to_end_a_closed_console_lets_the_nightly_acquire_at_once(
    tmp_path: Path, sleeper: Callable[[], subprocess.Popen[bytes]]
) -> None:
    """Card section 3, end to end: the real app with its monitor running on stub services."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_supervisor.app import create_app

    sup = Supervisor(
        Paths(tmp_path / "ns"),
        Runtime.default("gpu"),
        config=fast_config(),
        resolver=StubTable(),
        gpu_probe=NullGpuProbe(),
    )
    child = sleeper()
    with TestClient(create_app(sup)) as tc:
        body = {"service": "llama", "owner": "opencode", "model_alias": "code", "pid": child.pid}
        assert tc.post("/acquire", json=body).status_code == 200
        st = tc.get("/status").json()
        assert st["gpu_owner"] == "opencode" and st.get("holder_pid") == child.pid
        kill(child)
        assert wait_until(lambda: tc.get("/status").json()["gpu_owner"] is None, 10)
        assert tc.get("/status").json().get("holder_pid") is None
        nightly = {"service": "llama", "owner": "studio:nightly-1", "wait": False}
        r = tc.post("/acquire", json=nightly)  # wait=false: granted now instead of NS-E201 after 2 h
        assert r.status_code == 200, r.text
        assert tc.get("/status").json()["gpu_owner"] == "studio:nightly-1"
    assert holder_gone_log(sup.paths, "opencode")


def test_the_launchers_release_after_the_supervisor_released_is_harmless(
    tmp_path: Path, sleeper: Callable[[], subprocess.Popen[bytes]]
) -> None:
    """Case "failed, then retried": the launcher's finally posts /release after the monitor freed the GPU."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_supervisor.app import create_app

    sup, _clock = make(tmp_path)
    child = sleeper()
    with TestClient(create_app(sup, run_monitor=False)) as tc:
        body = {"service": "llama", "owner": "opencode", "model_alias": "code", "pid": child.pid}
        assert tc.post("/acquire", json=body).status_code == 200
        kill(child)
        tc.portal.call(sup.tick)  # type: ignore[union-attr]  # one monitor pass on the app's own loop
        assert tc.get("/status").json()["gpu_owner"] is None
        nightly = {"service": "llama", "owner": "studio:nightly-1", "wait": False}
        assert tc.post("/acquire", json=nightly).status_code == 200
        late = tc.post("/release", json={"service": "llama", "owner": "opencode"})
        assert late.status_code == 200 and late.json() == {"ok": False}  # no error, nothing freed
        assert tc.get("/status").json()["gpu_owner"] == "studio:nightly-1"  # the nightly keeps its card


def test_the_client_passes_the_pid_when_given() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content or b"{}"))
        return httpx.Response(200, json={"endpoint": "http://127.0.0.1:8080", "model_alias": "code"})

    client = SupervisorClient("http://sup.test", client=httpx.Client(transport=httpx.MockTransport(handler)))
    client.acquire("llama", "opencode", model_alias="code", **client_pid_kw(4242))
    client.acquire("tts", "dashboard:preview-1")
    assert sent[0]["owner"] == "opencode" and sent[0].get("pid") == 4242
    assert sent[1].get("pid") is None  # no pid given: none sent


# ---------------------------------------------------------------- both ends of the pid, a failing probe


def test_the_acquire_route_refuses_a_pid_past_the_windows_range_and_a_boolean(tmp_path: Path) -> None:
    """Windows pids are 32-bit: 2**32 and `true` are refused (422); 2**32 - 1 is the largest accepted."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_supervisor.app import create_app

    table = {2**32 - 1: 5.0}
    sup, _clock = make(tmp_path, probe=lambda pid: table.get(pid))
    with TestClient(create_app(sup, run_monitor=False)) as tc:
        for bad in (2**32, True, "7", 2.0):
            r = tc.post("/acquire", json={"service": "llama", "owner": "opencode", "pid": bad})
            assert r.status_code == 422, (bad, r.status_code, r.text)
        assert tc.get("/status").json()["gpu_owner"] is None
        r = tc.post("/acquire", json={"service": "llama", "owner": "opencode", "pid": 2**32 - 1})
        assert r.status_code == 200, r.text
        assert tc.get("/status").json().get("holder_pid") == 2**32 - 1


def test_a_probe_that_cannot_read_the_holder_keeps_it_and_says_so_once(tmp_path: Path) -> None:
    """Access denied is not "gone": the holder keeps the GPU, one holder_probe_failed WARN; once readable the
    process is checked as usual."""
    answers: dict[str, Any] = {"mode": "denied"}

    def probe(pid: int) -> float | None:
        if answers["mode"] == "denied":
            raise PermissionError("access denied")
        return None if answers["mode"] == "gone" else 50.0

    sup, clock = make(tmp_path, probe=probe)

    async def main() -> None:
        try:
            await sup.acquire("llama", "opencode", "code", **pid_kw(4343))
            await advance(sup, clock, 600)
            assert sup.lock.state.owner == "opencode" and sup.status().get("holder_pid") == 4343
            answers["mode"] = "alive"  # readable now: this creation time is the holder's
            await advance(sup, clock, 120)
            assert sup.lock.state.owner == "opencode"
            answers["mode"] = "gone"
            await sup.tick()
            assert sup.lock.state.owner is None
        finally:
            await close(sup)

    asyncio.run(main())
    warns = [
        json.loads(line)
        for f in sup.paths.logs.glob("supervisor-*.jsonl")
        for line in f.read_text(encoding="utf-8").splitlines()
    ]
    assert len([w for w in warns if w.get("event") == "holder_probe_failed"]) == 1
    assert holder_gone_log(sup.paths, "opencode")
