import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from ns_common.errors import NsError
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_supervisor.core import Supervisor
from ns_supervisor.schedule_cfg import write_run_time
from ns_supervisor.testing import StubTable, fast_config


def make(
    tmp_path: Path,
    *,
    extra: dict[str, list[str]] | None = None,
    gpu: dict[str, bool] | None = None,
    two_model: bool = False,
    now: Callable[[], datetime] = datetime.now,
    **cfg: Any,
) -> tuple[Supervisor, StubTable, Paths]:
    paths = Paths(tmp_path / "ns")
    table = StubTable(extra=extra, gpu=gpu, two_model=two_model)
    sup = Supervisor(paths, Runtime.default("gpu"), config=fast_config(**cfg), resolver=table, now=now)
    return sup, table, paths


@contextlib.asynccontextmanager
async def running(sup: Supervisor) -> AsyncIterator[Supervisor]:
    mon = asyncio.create_task(sup.run_monitor())
    try:
        yield sup
    finally:
        mon.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await mon
        await sup.stop_all(include_persistent=True)
        sup.close_job()


async def until(pred: Callable[[], bool], timeout: float = 25.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return pred()


def state(sup: Supervisor, name: str) -> str:
    return str(sup.status()["services"][name]["state"])


def pid(sup: Supervisor, name: str) -> int | None:
    return sup.status()["services"][name]["pid"]


def test_acquire_starts_service_and_returns_endpoint(tmp_path: Path) -> None:
    sup, table, _ = make(tmp_path)

    async def main() -> None:
        async with running(sup):
            r = await sup.acquire("llama", "studio:j1")
            assert r == {"endpoint": f"http://127.0.0.1:{table.ports['llama']}", "model_alias": "story"}
            st = sup.status()
            assert st["gpu_owner"] == "studio:j1" and st["gpu_service"] == "llama"
            assert st["services"]["llama"]["state"] == "ready"
            assert st["services"]["llama"]["owners"] == ["studio:j1"]
            async with httpx.AsyncClient() as http:
                assert (await http.get(f"{r['endpoint']}/health")).status_code == 200
            assert await sup.release("llama", "studio:j1") is True
            assert sup.status()["gpu_owner"] is None
            assert state(sup, "llama") == "ready"  # stays warm until the idle timer fires

    asyncio.run(main())


def test_second_owner_waits_for_first_and_reuses_warm_process(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, idle_unload_s=60)

    async def main() -> None:
        async with running(sup):
            await sup.acquire("llama", "A")
            first_pid = pid(sup, "llama")
            task_b = asyncio.create_task(sup.acquire("llama", "B"))
            await asyncio.sleep(0.3)
            assert not task_b.done()
            assert sup.status()["waiting"] == [{"owner": "B", "service": "llama"}]
            await sup.release("llama", "A")
            await task_b
            assert sup.status()["gpu_owner"] == "B"
            assert pid(sup, "llama") == first_pid

    asyncio.run(main())


def test_tenant_swap_terminates_resident_service(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, idle_unload_s=60)

    async def main() -> None:
        async with running(sup):
            await sup.acquire("llama", "A")
            llama = sup.services["llama"].proc
            assert llama is not None
            await sup.release("llama", "A")
            r = await sup.acquire("tts", "B")
            assert r["model_alias"] is None
            assert not llama.alive()
            assert state(sup, "llama") == "stopped" and state(sup, "tts") == "ready"
            tts = sup.services["tts"].proc
            assert tts is not None
            await sup.release("tts", "B")
            w = await sup.acquire("whisper", "C")
            assert w == {"endpoint": "inproc", "model_alias": None}
            assert not tts.alive()
            st = sup.status()
            assert st["services"]["whisper"]["state"] == "ready"
            assert st["services"]["whisper"]["owners"] == ["C"]
            assert st["gpu_service"] == "whisper"
            await sup.release("whisper", "C")
            assert sup.status()["services"]["whisper"]["state"] == "stopped"

    asyncio.run(main())


def test_idle_unload_after_release(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, idle_unload_s=0.6)

    async def main() -> None:
        async with running(sup):
            await sup.acquire("llama", "A")
            proc = sup.services["llama"].proc
            assert proc is not None
            await sup.release("llama", "A")
            assert await until(lambda: state(sup, "llama") == "stopped")
            assert not proc.alive()

    asyncio.run(main())


def test_held_service_is_not_unloaded(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, idle_unload_s=0.4)

    async def main() -> None:
        async with running(sup):
            await sup.acquire("llama", "A")
            await asyncio.sleep(1.5)
            assert state(sup, "llama") == "ready"

    asyncio.run(main())


def test_embed_never_takes_the_gpu_lock_and_refcounts(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, idle_unload_s=0.8)

    async def main() -> None:
        async with running(sup):
            await sup.acquire("llama", "A")
            await sup.acquire("embed", "B")
            await sup.acquire("embed", "C")
            st = sup.status()
            assert st["gpu_owner"] == "A"
            assert st["services"]["embed"]["owners"] == ["B", "C"]
            await sup.release("embed", "B")
            await asyncio.sleep(1.6)  # longer than the idle timer, but C still holds it
            assert state(sup, "embed") == "ready"
            await sup.release("embed", "C")
            assert await until(lambda: state(sup, "embed") == "stopped")
            assert state(sup, "llama") == "ready"  # still held by A

    asyncio.run(main())


def test_crash_is_restarted_on_next_acquire(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, idle_unload_s=60)

    async def main() -> None:
        async with running(sup):
            await sup.acquire("llama", "A")
            first = sup.services["llama"].proc
            assert first is not None
            await sup.release("llama", "A")
            first.popen.kill()
            assert await until(lambda: state(sup, "llama") == "stopped")
            assert sup.status()["services"]["llama"]["crashes"] == 1
            await sup.acquire("llama", "B")
            assert state(sup, "llama") == "ready"
            assert pid(sup, "llama") != first.pid

    asyncio.run(main())


def test_three_crashes_in_window_put_service_in_error(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, idle_unload_s=60)

    async def main() -> None:
        async with running(sup):
            for i in range(3):
                await sup.acquire("llama", "A")
                proc = sup.services["llama"].proc
                assert proc is not None
                await sup.release("llama", "A")
                proc.popen.kill()
                want = "error" if i == 2 else "stopped"
                assert await until(lambda w=want: state(sup, "llama") == w)
            with pytest.raises(NsError) as ei:
                await sup.acquire("llama", "A")
            assert ei.value.code == "NS-E202"
            assert pid(sup, "llama") is None
            assert sup.status()["gpu_owner"] is None
            await sup.stop_all()
            assert state(sup, "llama") == "stopped"
            await sup.acquire("llama", "A")
            assert state(sup, "llama") == "ready"

    asyncio.run(main())


def test_start_failure_is_e202_and_frees_the_lock(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, extra={"llama": ["--exit-immediately"]})

    async def main() -> None:
        async with running(sup):
            with pytest.raises(NsError) as ei:
                await sup.acquire("llama", "A")
            assert ei.value.code == "NS-E202"
            assert sup.status()["gpu_owner"] is None
            assert sup.status()["services"]["llama"]["crashes"] == 1

    asyncio.run(main())


def test_health_timeout_is_e203_and_child_is_killed(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, extra={"llama": ["--never-ready"]}, health_timeout_s=1.5)

    async def main() -> None:
        async with running(sup):
            with pytest.raises(NsError) as ei:
                await sup.acquire("llama", "A")
            assert ei.value.code == "NS-E203"
            assert pid(sup, "llama") is None
            assert sup.status()["gpu_owner"] is None

    asyncio.run(main())


def test_lock_timeout_is_e201(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, idle_unload_s=60)

    async def main() -> None:
        async with running(sup):
            await sup.acquire("llama", "A")
            with pytest.raises(NsError) as ei:
                await sup.acquire("tts", "B", timeout_s=0.3)
            assert ei.value.code == "NS-E201"
            with pytest.raises(NsError) as ei2:
                await sup.acquire("tts", "B", wait=False)
            assert ei2.value.code == "NS-E201"
            assert sup.status()["gpu_owner"] == "A" and sup.status()["waiting"] == []

    asyncio.run(main())


def test_two_model_mode_swaps_on_alias_change(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, two_model=True, idle_unload_s=60)

    async def main() -> None:
        async with running(sup):
            r = await sup.acquire("llama", "A", model_alias="story")
            assert r["model_alias"] == "story"
            story_pid = pid(sup, "llama")
            story_proc = sup.services["llama"].proc
            assert story_proc is not None
            await sup.release("llama", "A")
            r2 = await sup.acquire("llama", "B", model_alias="code")
            assert r2["model_alias"] == "code"
            assert pid(sup, "llama") != story_pid
            assert not story_proc.alive()
            code_pid = pid(sup, "llama")
            await sup.release("llama", "B")
            await sup.acquire("llama", "C", model_alias="code")
            assert pid(sup, "llama") == code_pid

    asyncio.run(main())


def test_non_gpu_llama_takes_no_lock(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, gpu={"llama": False}, idle_unload_s=60)

    async def main() -> None:
        async with running(sup):
            await asyncio.gather(sup.acquire("llama", "A"), sup.acquire("llama", "B"))
            st = sup.status()
            assert st["gpu_owner"] is None
            assert st["services"]["llama"]["owners"] == ["A", "B"]

    asyncio.run(main())


def test_stop_all_clears_everything(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, idle_unload_s=60)

    async def main() -> None:
        async with running(sup):
            await sup.acquire("llama", "A")
            await sup.acquire("embed", "B")
            await sup.stop_all()
            st = sup.status()
            assert st["gpu_owner"] is None
            for name in ("llama", "embed", "tts"):
                assert st["services"][name]["state"] == "stopped"
                assert st["services"][name]["owners"] == []

    asyncio.run(main())


def test_dashboard_is_persistent_and_survives_stop_all(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, idle_unload_s=0.4)

    async def main() -> None:
        async with running(sup):
            assert await sup.warmup("dashboard") == "ready"
            await asyncio.sleep(1.2)
            assert state(sup, "dashboard") == "ready"
            await sup.stop_all()
            assert state(sup, "dashboard") == "ready"
            await sup.stop_all(include_persistent=True)
            assert state(sup, "dashboard") == "stopped"

    asyncio.run(main())


def test_warmup_starts_without_owner_and_yields_to_busy_gpu(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, idle_unload_s=60)

    async def main() -> None:
        async with running(sup):
            assert await sup.warmup("llama") == "ready"
            assert sup.status()["gpu_owner"] is None
            await sup.acquire("tts", "A")
            assert await sup.warmup("llama") == "stopped"  # GPU is busy: nothing is touched
            assert state(sup, "tts") == "ready"

    asyncio.run(main())


def test_shutdown_stops_children_requests_exit_and_refuses_new_work(tmp_path: Path) -> None:
    sup, _, paths = make(tmp_path, idle_unload_s=60)

    async def main() -> None:
        async with running(sup):
            await sup.acquire("llama", "A")
            proc = sup.services["llama"].proc
            assert proc is not None
            assert (paths.home / "run" / "supervisor.json").exists()
            await sup.shutdown(grace_s=0.2)
            assert sup.exit_requested.is_set()
            assert not proc.alive()
            assert not (paths.home / "run" / "supervisor.json").exists()
            with pytest.raises(NsError) as ei:
                await sup.acquire("llama", "B")
            assert ei.value.code == "NS-E202"

    asyncio.run(main())


def test_auto_exit_when_idle_and_no_run_scheduled(tmp_path: Path) -> None:
    now = datetime(2026, 11, 2, 2, 30)
    sup, _, paths = make(tmp_path, auto_exit_idle_s=0.4, now=lambda: now)
    write_run_time(paths, "10:00")  # 7.5 h away: not "due soon"

    async def main() -> None:
        async with running(sup):
            assert await until(sup.exit_requested.is_set)

    asyncio.run(main())


def test_no_auto_exit_when_run_due_within_an_hour(tmp_path: Path) -> None:
    now = datetime(2026, 11, 2, 2, 30)
    sup, _, paths = make(tmp_path, auto_exit_idle_s=0.4, now=lambda: now)
    write_run_time(paths, "03:00")  # 30 minutes away

    async def main() -> None:
        async with running(sup):
            await asyncio.sleep(1.5)
            assert not sup.exit_requested.is_set()
            assert sup.run_due_soon() is True

    asyncio.run(main())


def test_activity_defers_auto_exit(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, auto_exit_idle_s=0.8, idle_unload_s=60)

    async def main() -> None:
        async with running(sup):
            await sup.acquire("llama", "A")
            await asyncio.sleep(1.6)  # held the whole time: busy, so never idle
            assert not sup.exit_requested.is_set()

    asyncio.run(main())


def test_events_are_published(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, idle_unload_s=60)

    async def main() -> None:
        q = sup.subscribe()
        async with running(sup):
            await sup.acquire("llama", "A")
            await sup.release("llama", "A")
        sup.unsubscribe(q)
        seen: list[dict[str, Any]] = []
        while not q.empty():
            seen.append(q.get_nowait())
        pairs = {(e["data"].get("service"), e["data"].get("event")) for e in seen}
        want = {("llama", "starting"), ("llama", "ready"), ("llama", "acquired"), ("llama", "released")}
        assert want <= pairs
        assert all(e["kind"] == "service" and "ts" in e for e in seen)

    asyncio.run(main())


def test_state_file_records_child_pids(tmp_path: Path) -> None:
    sup, _, paths = make(tmp_path, idle_unload_s=60)

    async def main() -> None:
        async with running(sup):
            await sup.acquire("llama", "A")
            data = json.loads((paths.home / "run" / "supervisor.json").read_text(encoding="utf-8"))
            assert data["children"]["llama"] == pid(sup, "llama")
            assert data["port"] == 8099 and isinstance(data["pid"], int)

    asyncio.run(main())


def test_unknown_service_is_e202(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path)

    async def main() -> None:
        async with running(sup):
            with pytest.raises(NsError) as ei:
                await sup.acquire("bogus", "A")
            assert ei.value.code == "NS-E202"
            assert await sup.release("llama", "nobody") is False

    asyncio.run(main())


def test_process_output_reaches_the_service_log(tmp_path: Path) -> None:
    sup, _, paths = make(tmp_path, idle_unload_s=60)

    async def main() -> None:
        async with running(sup):
            await sup.acquire("llama", "A")

    asyncio.run(main())
    files = list(paths.logs.glob("llama-*.jsonl"))
    assert files, "service output should be logged"
    assert list(paths.logs.glob("supervisor-*.jsonl")), "supervisor events should be logged"


def test_waiter_granted_the_gpu_during_shutdown_starts_nothing(tmp_path: Path) -> None:
    # B queued behind A; shutdown() releases A, which grants B. B must give the lock back and refuse
    # instead of starting a model process after stop_all and re-creating run/supervisor.json.
    sup, _, paths = make(tmp_path, idle_unload_s=60)

    async def main() -> None:
        async with running(sup):
            await sup.acquire("llama", "A")
            waiter = asyncio.create_task(sup.acquire("tts", "B"))
            assert await until(lambda: ("B", "tts") in sup.lock.waiters())
            await sup.shutdown(grace_s=0.1)
            with pytest.raises(NsError) as ei:
                await asyncio.wait_for(waiter, 10)
            assert ei.value.code == "NS-E202" and "shutting down" in ei.value.detail
            await asyncio.sleep(0.3)
            assert sup.services["tts"].proc is None and state(sup, "tts") == "stopped"
            assert sup.lock.state.owner is None
            assert not (paths.home / "run" / "supervisor.json").exists()

    asyncio.run(main())


def test_write_state_is_a_noop_once_shutting_down(tmp_path: Path) -> None:
    sup, _, paths = make(tmp_path)
    sup.write_state()
    assert (paths.home / "run" / "supervisor.json").exists()
    sup.remove_state()
    sup.shutting_down = True
    sup.write_state()
    assert not (paths.home / "run" / "supervisor.json").exists()
    sup.close_job()


def test_warmup_is_refused_while_shutting_down(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path)

    async def main() -> None:
        sup.shutting_down = True
        with pytest.raises(NsError) as ei:
            await sup.warmup("llama")
        assert ei.value.code == "NS-E202"
        assert sup.services["llama"].proc is None

    asyncio.run(main())
    sup.close_job()


# ---------------------------------------------------------------- GPU lease (dashboard re-review row 1)


def test_a_studio_lease_without_heartbeat_is_reclaimed(tmp_path: Path) -> None:
    """The studio child was killed while it held the GPU: the lock must not stay with a dead owner."""
    sup, _table, _ = make(tmp_path, lease_expiry_s=0.5)

    async def main() -> None:
        async with running(sup):
            await sup.acquire("llama", "studio:dead-job")
            assert sup.status()["gpu_owner"] == "studio:dead-job"
            assert await until(lambda: sup.status()["gpu_owner"] is None, 10)
            await sup.acquire("llama", "studio:next-job", timeout_s=2)  # no two-hour wait for a dead owner
            assert sup.status()["gpu_owner"] == "studio:next-job"

    asyncio.run(main())


def test_heartbeats_keep_a_lease_alive_and_other_owners_never_expire(tmp_path: Path) -> None:
    sup, _table, _ = make(tmp_path, lease_expiry_s=0.5)

    async def main() -> None:
        async with running(sup):
            await sup.acquire("llama", "studio:live-job")
            for _ in range(15):  # 1.5 s, three times the expiry
                assert sup.heartbeat("studio:live-job") is True
                await asyncio.sleep(0.1)
            assert sup.status()["gpu_owner"] == "studio:live-job"
            assert sup.heartbeat("studio:someone-else") is False  # not the holder
            await sup.release("llama", "studio:live-job")
            await sup.acquire("llama", "dashboard:assistant")  # the Assistant does not beat
            await asyncio.sleep(1.2)
            assert sup.status()["gpu_owner"] == "dashboard:assistant"

    asyncio.run(main())


def test_release_owner_frees_the_lock_and_drops_the_queue_place(tmp_path: Path) -> None:
    sup, _table, _ = make(tmp_path)

    async def main() -> None:
        async with running(sup):
            await sup.acquire("llama", "studio:a")
            waiter = asyncio.create_task(sup.acquire("tts", "studio:b", timeout_s=30))
            assert await until(lambda: bool(sup.lock.waiters()))
            assert await sup.release_owner("studio:b") is True  # the killed child waited in the queue
            with pytest.raises(NsError) as err:
                await waiter
            assert err.value.code == "NS-E201"
            assert sup.lock.waiters() == []
            assert await sup.release_owner("studio:a") is True
            assert sup.status()["gpu_owner"] is None
            assert await sup.release_owner("studio:a") is False  # nothing left to free
            await sup.acquire("llama", "studio:c", timeout_s=2)

    asyncio.run(main())


def test_a_sleeping_pc_does_not_expire_a_live_lease(tmp_path: Path) -> None:
    clock = {"t": 1000.0}
    paths = Paths(tmp_path / "ns")
    table = StubTable()
    sup = Supervisor(
        paths, Runtime.default("gpu"), config=fast_config(lease_expiry_s=600, suspend_gap_s=150),
        resolver=table, clock=lambda: clock["t"],
    )

    async def main() -> None:
        try:
            await sup.acquire("llama", "studio:j1")
            await sup.tick()  # the monitor notices the holder
            clock["t"] += 599
            await sup.tick()  # one pass 599 s later: a sleep, not silence
            assert sup.status()["gpu_owner"] == "studio:j1"
            for _ in range(7):  # now the monitor runs every 100 s and the studio stays silent
                clock["t"] += 100
                await sup.tick()
            assert sup.status()["gpu_owner"] is None
        finally:
            await sup.stop_all(include_persistent=True)
            sup.close_job()

    asyncio.run(main())


def test_status_reports_the_windows_session_and_whether_a_run_is_using_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ns_supervisor import core

    sup, _, _ = make(tmp_path)
    monkeypatch.setattr(core.notify, "session_id", lambda: 0)           # the 03:00 task (S4U) runs here
    st = sup.status()
    assert st["session"] == {"id": 0, "interactive": False}
    assert st["busy"] is True                                          # a fresh engine counts as busy
    later = sup.clock() + core.BUSY_RECENT_S + 1
    monkeypatch.setattr(sup, "clock", lambda: later)
    assert sup.status()["busy"] is False                               # nothing asked for anything since
    monkeypatch.setattr(core.notify, "session_id", lambda: 3)          # the Dashboard shortcut starts it here
    assert sup.status()["session"] == {"id": 3, "interactive": True}
    monkeypatch.setattr(core.notify, "session_id", lambda: None)       # unknown: assume a desktop
    assert sup.status()["session"] == {"id": None, "interactive": True}


def test_busy_while_somebody_holds_the_gpu_even_when_nothing_happened_lately(tmp_path: Path) -> None:
    from ns_supervisor import core

    sup, _, _ = make(tmp_path)

    async def main() -> None:
        async with running(sup):
            await sup.acquire("llama", "studio:j1")
            later = time.monotonic() + core.BUSY_RECENT_S + 1000
            sup.clock = lambda: later
            assert sup.status()["busy"] is True
            await sup.release("llama", "studio:j1")
            later2 = later + core.BUSY_RECENT_S + 1                # the release itself is activity
            sup.clock = lambda: later2
            assert sup.status()["busy"] is False

    asyncio.run(main())
