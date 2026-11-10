"""Pre-tag item 2 (system-gaps row 2, final-engine/dashboard row 2): the Queue's Release button.

The supervisor refuses while the holder still beats; a Nightshift holder that is silent is stopped through the
jobs worker; a foreign holder (opencode) is only released and the answer says it may still be in use.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_dashboard.events import EventHub
from ns_dashboard.fakes import FakeSupervisor
from ns_dashboard.jobs import JobControl
from ns_dashboard.supervisor_link import GPU_IN_USE, SupervisorLink
from ns_supervisor.core import Supervisor
from ns_supervisor.testing import StubTable, fast_config
from tests.test_dashboard_api import make_env

# ---------------------------------------------------------------- the supervisor


def clocked(tmp_path: Path) -> tuple[Supervisor, dict[str, float]]:
    clock = {"t": 1000.0}
    sup = Supervisor(
        Paths(tmp_path / "ns"), Runtime.default("gpu"), config=fast_config(lease_expiry_s=600),
        resolver=StubTable(), clock=lambda: clock["t"],
    )
    return sup, clock


def test_release_is_refused_while_the_owner_beats_and_allowed_after_90_s(tmp_path: Path):
    sup, clock = clocked(tmp_path)

    async def main() -> None:
        try:
            await sup.acquire("llama", "studio:night-1")
            assert sup.heartbeat("studio:night-1") is True
            clock["t"] += 89
            refused = await sup.release_unless_alive("studio:night-1")
            assert refused["ok"] is False and refused["refused"] == "alive" and refused["silent_s"] == 89.0
            assert sup.status()["gpu_owner"] == "studio:night-1"  # nothing was freed
            clock["t"] += 2  # 91 s of silence
            assert await sup.release_unless_alive("studio:night-1") == {"ok": True}
            assert sup.status()["gpu_owner"] is None
        finally:
            await sup.stop_all(include_persistent=True)
            sup.close_job()

    asyncio.run(main())


def test_a_foreign_holder_is_released_at_once(tmp_path: Path):
    sup, _clock = clocked(tmp_path)

    async def main() -> None:
        try:
            await sup.acquire("llama", "opencode")
            assert sup.beat_age("opencode") is None  # a foreign program never beats
            assert await sup.release_unless_alive("opencode") == {"ok": True}
            assert await sup.release_unless_alive("opencode") == {"ok": False}  # already free
        finally:
            await sup.stop_all(include_persistent=True)
            sup.close_job()

    asyncio.run(main())


def test_the_release_route_takes_unless_alive(tmp_path: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_supervisor.app import create_app

    sup = Supervisor(
        Paths(tmp_path / "ns"), Runtime.default("gpu"), config=fast_config(idle_unload_s=60),
        resolver=StubTable(),
    )
    with TestClient(create_app(sup), base_url="http://127.0.0.1:18099") as tc:
        tc.post("/acquire", json={"service": "llama", "owner": "studio:j1"})
        assert tc.post("/heartbeat", json={"owner": "studio:j1"}).json() == {"ok": True}
        r = tc.post("/release_owner", json={"owner": "studio:j1", "unless_alive": True}).json()
        assert r["ok"] is False and r["refused"] == "alive"
        assert tc.get("/status").json()["gpu_owner"] == "studio:j1"
        assert tc.post("/release_owner", json={"owner": "studio:j1"}).json() == {"ok": True}  # unguarded


# ---------------------------------------------------------------- the dashboard


def post_release(client: Any, owner: str) -> Any:
    return client.post("/api/engine/release_gpu", json={"owner": owner})


def test_a_holder_that_still_beats_is_not_released(tmp_home: Path):
    with FakeSupervisor() as sup:
        client, _state = make_env(tmp_home, SupervisorLink(sup.url, timeout_s=2))
        sup.state = {"gpu_owner": "studio:nightly-1", "services": {}, "lease_alive": True}
        r = post_release(client, "studio:nightly-1")
        assert r.status_code == 409 and r.json()["error"]["code"] == "gpu_in_use"
        assert r.json()["error"]["message"] == GPU_IN_USE == (
            "A video job is still using the GPU; stop it from the Queue instead."
        )
        assert sup.state["gpu_owner"] == "studio:nightly-1"
        assert ("POST", "/release_owner", {"owner": "studio:nightly-1", "unless_alive": True}) in sup.calls


def test_a_silent_job_of_this_dashboard_is_cancelled_through_the_worker(tmp_home: Path):
    with FakeSupervisor() as sup:
        client, state = make_env(tmp_home, SupervisorLink(sup.url, timeout_s=2))
        rec = state.store.create("run", "after-dark", {})
        assert state.store.mark_running(rec.job_id)
        ctl = JobControl(state.store.get(rec.job_id), state.store, EventHub())  # type: ignore[arg-type]
        ctl.extras["studio_job_ids"] = ["after-dark-1"]
        state.worker._running_ctl = ctl
        sup.state = {"gpu_owner": "studio:after-dark-1", "services": {}}
        body = post_release(client, "studio:after-dark-1").json()
        assert body["released"] is True and body["stopped_job"] == rec.job_id and body["foreign"] is False
        assert "note" not in body
        assert state.store.get(rec.job_id).cancel_requested is True  # type: ignore[union-attr]
        assert sup.state["gpu_owner"] is None


def test_a_nightshift_holder_that_is_not_this_dashboards_job_is_only_released(tmp_home: Path):
    with FakeSupervisor() as sup:
        client, state = make_env(tmp_home, SupervisorLink(sup.url, timeout_s=2))
        rec = state.store.create("run", "after-dark", {})
        assert state.store.mark_running(rec.job_id)
        ctl = JobControl(state.store.get(rec.job_id), state.store, EventHub())  # type: ignore[arg-type]
        ctl.extras["studio_job_ids"] = ["after-dark-1"]
        state.worker._running_ctl = ctl
        sup.state = {"gpu_owner": "studio:the-nightly-run", "services": {}}
        body = post_release(client, "studio:the-nightly-run").json()
        assert body["released"] is True and body["stopped_job"] is None
        assert state.store.get(rec.job_id).cancel_requested is False  # another job is not touched


def test_a_foreign_holder_is_released_and_the_answer_says_it_may_still_run(tmp_home: Path):
    with FakeSupervisor() as sup:
        client, state = make_env(tmp_home, SupervisorLink(sup.url, timeout_s=2))
        rec = state.store.create("run", "after-dark", {})
        assert state.store.mark_running(rec.job_id)
        sup.state = {"gpu_owner": "opencode", "services": {}, "lease_alive": True}  # the guard does not apply
        body = post_release(client, "opencode").json()
        assert body["ok"] is True and body["released"] is True and body["foreign"] is True
        assert body["stopped_job"] is None
        assert body["note"] == "The GPU lock is released, but opencode may still be using the GPU."
        assert ("POST", "/release_owner", {"owner": "opencode"}) in sup.calls  # a plain release
        assert state.store.get(rec.job_id).cancel_requested is False
