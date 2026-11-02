"""Gap 25: the engine leaves after the nightly run unless the dashboard is open."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from ns_supervisor.client import SupervisorClient
from tests.test_supervisor_core import make, running, until


def test_engine_leaves_after_the_nightly_run(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, post_run_grace_s=0.3, idle_unload_s=60)

    async def main() -> None:
        async with running(sup):
            await sup.acquire("llama", "studio:j1")
            await sup.release("llama", "studio:j1")
            await asyncio.sleep(0.6)
            assert not sup.exit_requested.is_set()  # nobody said the run is over: plain 30 min idle rule
            assert sup.note_run_finished() == {"exit_scheduled": True}
            assert await until(sup.exit_requested.is_set, 10)

    asyncio.run(main())


def test_engine_stays_while_the_dashboard_is_open(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, post_run_grace_s=0.2, idle_unload_s=60)

    async def main() -> None:
        async with running(sup):
            assert await sup.warmup("dashboard") == "ready"
            assert sup.note_run_finished() == {"exit_scheduled": False}
            await asyncio.sleep(1.0)
            assert not sup.exit_requested.is_set()

    asyncio.run(main())


def test_new_work_after_the_run_cancels_the_early_exit(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, post_run_grace_s=0.6, idle_unload_s=60)

    async def main() -> None:
        async with running(sup):
            sup.note_run_finished()
            await sup.acquire("llama", "dashboard:assistant")  # e.g. the owner opened the Assistant
            await asyncio.sleep(1.2)
            assert not sup.exit_requested.is_set() and sup.run_finished_at is None

    asyncio.run(main())


def test_exit_waits_for_a_busy_gpu(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, post_run_grace_s=0.1, idle_unload_s=60)

    async def main() -> None:
        async with running(sup):
            await sup.acquire("llama", "opencode")
            sup.run_finished_at = sup.clock() - 5
            await asyncio.sleep(0.6)
            assert not sup.exit_requested.is_set()

    asyncio.run(main())


def test_client_posts_run_finished() -> None:
    seen: list[tuple[str, str]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json={"exit_scheduled": True})

    c = SupervisorClient("http://sup:8099", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert c.run_finished() is True and seen == [("POST", "/run_finished")]
    json.dumps(seen)


def test_the_route_exists_and_tells_the_dashboard_state(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from ns_supervisor.app import create_app

    sup, _, _ = make(tmp_path, post_run_grace_s=60, idle_unload_s=60)
    with TestClient(create_app(sup)) as tc:
        assert tc.post("/run_finished").json() == {"exit_scheduled": True}
        assert sup.run_finished_at is not None
