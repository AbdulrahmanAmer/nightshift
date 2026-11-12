"""WP3 (G-51, F70): Nightshift Code's lease and its model.

- F70: Code ended (OpenCode ended by its pid, the window closed): the engine and the 14 GB model stayed, with
  no release in the log. Whichever way the lease ends (the launcher's `/release`, its console gone, Release on
  the Queue page), the model is unloaded by the next monitor pass unless another owner took it, not after the
  10-minute idle unload. Other owners keep the idle rule (the studio releases between stages).
- G-51: llama exited with code 1 under a Code lease and nothing said what the PC looked like. A crash the
  monitor sees is logged with the seconds since ready, the log's last write and the PC's memory; under the
  Code lease the model is loaded again (OpenCode talks to llama-server directly: no request reaches the
  engine to do it).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

import ns_supervisor.core as core_mod
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_supervisor.core import Supervisor
from ns_supervisor.gpu import NullGpuProbe
from ns_supervisor.ramroom import Room
from ns_supervisor.testing import StubTable, fast_config

ALIVE = 7.0  # the fake creation time of a holder process that runs


def make(tmp_path: Path, table: dict[int, float]) -> tuple[Supervisor, dict[str, float]]:
    """Stub services, an injected clock, the 10-minute idle unload of the product (so an unload seen within
    one pass is this work's, never the idle rule's) and a fake process table for the holder pid."""
    clock = {"t": 1000.0}
    sup = Supervisor(
        Paths(tmp_path / "ns"),
        Runtime.default("gpu"),
        config=fast_config(idle_unload_s=600.0, lease_expiry_s=600.0),
        resolver=StubTable(),
        clock=lambda: clock["t"],
        gpu_probe=NullGpuProbe(),
        process_probe=lambda pid: table.get(pid),
    )
    return sup, clock


def records(sup: Supervisor, event: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in sup.paths.logs.glob("supervisor-*.jsonl"):
        for line in f.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec.get("event") == event:
                out.append(rec)
    return out


def llama_pid(sup: Supervisor) -> int | None:
    p = sup.services["llama"].proc
    return p.pid if p is not None else None


async def close(sup: Supervisor) -> None:
    for t in list(sup._crash_tasks):
        t.cancel()
    await asyncio.gather(*sup._crash_tasks, return_exceptions=True)
    await sup.stop_all(include_persistent=True)
    sup.close_job()


async def settle(sup: Supervisor) -> None:
    """Wait for the follow-ups of the crashes the monitor saw (memory figures, Code's reload)."""
    await asyncio.wait_for(asyncio.gather(*list(sup._crash_tasks)), 60)


def crash_llama(sup: Supervisor) -> int:
    p = sup.services["llama"].proc
    assert p is not None
    p.popen.kill()
    p.popen.wait(timeout=10)
    return p.pid


def test_a_code_console_that_ends_frees_the_card_and_unloads_the_model_in_one_pass(tmp_path: Path) -> None:
    table = {4242: ALIVE}
    sup, clock = make(tmp_path, table)

    async def main() -> None:
        try:
            await sup.acquire("llama", "opencode", "code", pid=4242)
            await sup.tick()
            assert llama_pid(sup) is not None and sup.lock.state.owner == "opencode"
            del table[4242]                                   # the console is closed with X (or killed)
            clock["t"] += 1.0                                 # one monitor interval, nowhere near 600 s
            await sup.tick()
            assert sup.lock.state.owner is None
            assert llama_pid(sup) is None and sup.services["llama"].state == "stopped"
            unload = records(sup, "code_unload")
            assert unload and unload[-1]["data"]["reason"] == "holder_gone", unload
            assert not records(sup, "idle_unload")
        finally:
            await close(sup)

    asyncio.run(main())


def test_the_launchers_release_unloads_at_the_next_pass_and_other_owners_keep_the_idle_rule(
    tmp_path: Path,
) -> None:
    sup, clock = make(tmp_path, {4242: ALIVE})

    async def main() -> None:
        try:
            await sup.acquire("llama", "studio:night-1")
            assert await sup.release("llama", "studio:night-1") is True     # a stage boundary
            clock["t"] += 1.0
            await sup.tick()
            assert llama_pid(sup) is not None, "a studio release must not unload the model"
            await sup.acquire("llama", "opencode", "code", pid=4242)
            assert await sup.release("llama", "opencode") is True          # the launcher's finally
            assert records(sup, "code_lease_ended")                        # F70: the release is in the log
            clock["t"] += 1.0
            await sup.tick()
            assert llama_pid(sup) is None
            assert records(sup, "code_unload")[-1]["data"]["reason"] == "released"
        finally:
            await close(sup)

    asyncio.run(main())


def test_the_queue_pages_release_of_code_unloads_too(tmp_path: Path) -> None:
    sup, clock = make(tmp_path, {4242: ALIVE})

    async def main() -> None:
        try:
            await sup.acquire("llama", "opencode", "code", pid=4242)
            assert await sup.release_unless_alive("opencode") == {"ok": True}
            clock["t"] += 1.0
            await sup.tick()
            assert llama_pid(sup) is None
        finally:
            await close(sup)

    asyncio.run(main())


def test_a_model_another_owner_took_before_the_pass_is_kept(tmp_path: Path) -> None:
    sup, clock = make(tmp_path, {4242: ALIVE})

    async def main() -> None:
        try:
            await sup.acquire("llama", "opencode", "code", pid=4242)
            before = llama_pid(sup)
            await sup.release("llama", "opencode")
            await sup.acquire("llama", "studio:nightly-1", "code")         # the same load, reused
            clock["t"] += 1.0
            await sup.tick()
            assert llama_pid(sup) == before
            await sup.release("llama", "studio:nightly-1")
            clock["t"] += 1.0
            await sup.tick()
            assert llama_pid(sup) == before, "the nightly's release follows the idle rule, not Code's"
            assert not records(sup, "code_unload")
        finally:
            await close(sup)

    asyncio.run(main())


def test_a_crash_under_the_code_lease_is_logged_with_the_pcs_memory_and_reloaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup, clock = make(tmp_path, {4242: ALIVE})
    monkeypatch.setattr(core_mod.ramroom, "read", lambda: Room(4100, 40000, 41400, 50400))

    async def main() -> None:
        try:
            await sup.acquire("llama", "opencode", "code", pid=4242)
            await sup.tick()                                    # the monitor sees it ready
            clock["t"] += 806.0                                 # G-51: 13 min 26 s idle
            dead = crash_llama(sup)
            await sup.tick()
            await settle(sup)
            (ctx,) = records(sup, "crash_context")
            data = ctx["data"]
            assert data["pid"] == dead and data["since_ready_s"] == 806.0 and data["owner"] == "opencode"
            assert data["ram_avail_mb"] == 4100 and data["commit_mb"] == 40000
            assert data["commit_limit_mb"] == 41400
            assert "exited with code" in ctx["msg"] and "4100 MiB of memory free" in ctx["msg"]
            assert records(sup, "service_crash")
            # loaded again for Code, which still holds the card: OpenCode's next request finds it
            assert records(sup, "code_reloaded")
            assert llama_pid(sup) not in (None, dead) and sup.services["llama"].state == "ready"
            assert sup.lock.state.owner == "opencode"
        finally:
            await close(sup)

    asyncio.run(main())


def test_a_crash_with_nobody_holding_is_logged_not_reloaded_and_the_next_acquire_loads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sup, clock = make(tmp_path, {4242: ALIVE})
    monkeypatch.setattr(core_mod.ramroom, "read", lambda: Room(18600, 23000, 41400, None))

    async def main() -> None:
        try:
            await sup.acquire("llama", "studio:night-1")
            await sup.release("llama", "studio:night-1")
            await sup.tick()
            clock["t"] += 5.0
            crash_llama(sup)
            await sup.tick()
            await settle(sup)
            (ctx,) = records(sup, "crash_context")
            assert ctx["data"]["since_ready_s"] == 5.0 and ctx["data"]["owner"] is None
            assert not records(sup, "code_reload") and llama_pid(sup) is None
            await sup.acquire("llama", "opencode", "code", pid=4242)       # Code's next acquire loads it
            assert sup.services["llama"].state == "ready" and llama_pid(sup) is not None
        finally:
            await close(sup)

    asyncio.run(main())


def test_a_code_console_closed_after_a_crash_gets_no_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The crash and the console's end in the same pass: the lease ends first (holder gone), so the model is
    not loaded again for a Code window that is no longer there."""
    table = {4242: ALIVE}
    sup, clock = make(tmp_path, table)
    monkeypatch.setattr(core_mod.ramroom, "read", lambda: Room())

    async def main() -> None:
        try:
            await sup.acquire("llama", "opencode", "code", pid=4242)
            await sup.tick()
            crash_llama(sup)
            del table[4242]
            clock["t"] += 1.0
            await sup.tick()
            await settle(sup)
            assert records(sup, "crash_context") and not records(sup, "code_reloaded")
            assert llama_pid(sup) is None and sup.lock.state.owner is None
        finally:
            await close(sup)

    asyncio.run(main())
