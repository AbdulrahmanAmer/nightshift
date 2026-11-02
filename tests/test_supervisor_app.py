import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

if importlib.util.find_spec("fastapi") is None and os.environ.get("CI") == "true":
    # Temporary, until CR-C2-1 puts fastapi into the dev extra: CI installs only .[dev], so install it
    # here to exercise app.py. Local runs without fastapi skip this module.
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "fastapi>=0.115"], check=False)
    importlib.invalidate_caches()
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from ns_common.errors import NsError  # noqa: E402
from ns_common.models import Runtime  # noqa: E402
from ns_common.paths import Paths  # noqa: E402
from ns_supervisor.app import create_app  # noqa: E402
from ns_supervisor.client import SupervisorClient  # noqa: E402
from ns_supervisor.core import Supervisor  # noqa: E402
from ns_supervisor.testing import StubTable, fast_config  # noqa: E402


def build(tmp_path: Path, **kw: object) -> tuple[Supervisor, TestClient]:
    table = StubTable(**kw)  # type: ignore[arg-type]
    sup = Supervisor(
        Paths(tmp_path / "ns"), Runtime.default("gpu"), config=fast_config(idle_unload_s=60), resolver=table
    )
    return sup, TestClient(create_app(sup))


def test_status_health_and_acquire_release_roundtrip(tmp_path: Path) -> None:
    sup, tc = build(tmp_path)
    with tc:
        assert tc.get("/health").json() == {"ok": True}
        st = tc.get("/status").json()
        assert st["gpu_owner"] is None and set(st["services"]) >= {"llama", "embed", "tts", "whisper"}
        r = tc.post("/acquire", json={"service": "llama", "owner": "studio:j1", "model_alias": "code"})
        assert r.status_code == 200 and r.json()["model_alias"] == "code"
        assert tc.get("/status").json()["services"]["llama"]["state"] == "ready"
        assert tc.post("/release", json={"service": "llama", "owner": "studio:j1"}).json() == {"ok": True}
        assert tc.post("/release", json={"service": "llama", "owner": "nobody"}).json() == {"ok": False}
        assert tc.post("/stop_all").json() == {"ok": True}
        assert tc.get("/status").json()["services"]["llama"]["state"] == "stopped"


def test_supervisor_client_works_over_the_app(tmp_path: Path) -> None:
    _, tc = build(tmp_path)
    with tc:
        c = SupervisorClient("http://testserver", client=tc)
        ep = c.acquire("tts", "verify")
        assert ep.base_url.startswith("http://127.0.0.1:")
        assert c.status()["gpu_owner"] == "verify"
        c.release("tts", "verify")
        assert c.warmup("embed") == "ready"


def test_lock_timeout_maps_to_409(tmp_path: Path) -> None:
    _, tc = build(tmp_path)
    with tc:
        tc.post("/acquire", json={"service": "llama", "owner": "A"})
        r = tc.post("/acquire", json={"service": "tts", "owner": "B", "timeout_s": 0.2})
        assert r.status_code == 409 and r.json()["code"] == "NS-E201"
        c = SupervisorClient("http://testserver", client=tc)
        with pytest.raises(NsError) as ei:
            c.acquire("tts", "B", timeout_s=0)
        assert ei.value.code == "NS-E201"


def test_start_failure_maps_to_502_and_health_timeout_to_504(tmp_path: Path) -> None:
    _, tc = build(tmp_path, extra={"llama": ["--exit-immediately"], "tts": ["--never-ready"]})
    with tc:
        r = tc.post("/acquire", json={"service": "llama", "owner": "A"})
        assert r.status_code == 502 and r.json()["code"] == "NS-E202"
    sup2 = Supervisor(
        Paths(tmp_path / "ns2"),
        Runtime.default("gpu"),
        config=fast_config(health_timeout_s=1.0),
        resolver=StubTable(extra={"tts": ["--never-ready"]}),
    )
    with TestClient(create_app(sup2)) as tc2:
        r2 = tc2.post("/acquire", json={"service": "tts", "owner": "A"})
        assert r2.status_code == 504 and r2.json()["code"] == "NS-E203"


def test_warmup_and_shutdown(tmp_path: Path) -> None:
    sup, tc = build(tmp_path)
    with tc:
        assert tc.post("/warmup", json={"service": "llama"}).json() == {"state": "ready"}
        assert tc.post("/shutdown", json={"grace_s": 0.1}).json() == {"ok": True}
        assert sup.exit_requested.is_set()
        assert tc.post("/acquire", json={"service": "llama", "owner": "A"}).status_code == 502


def test_shutdown_without_body_uses_default_grace(tmp_path: Path) -> None:
    sup, tc = build(tmp_path)
    with tc:
        assert tc.post("/shutdown").json() == {"ok": True}
        assert sup.exit_requested.is_set()


def test_events_stream_starts_with_a_snapshot_and_accepts_posted_events(tmp_path: Path) -> None:
    _, tc = build(tmp_path)
    with tc:
        assert tc.post("/events", json={"kind": "job", "data": {"stage": "voice"}}).json() == {"ok": True}
        with tc.stream("GET", "/events", params={"limit": 1}) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            lines = [ln for ln in r.iter_lines() if ln.startswith("data:")]
        ev = json.loads(lines[0][len("data:") :])
        assert ev["kind"] == "service" and ev["data"]["event"] == "snapshot"
        assert "services" in ev["data"]


def test_heartbeat_and_release_owner_routes(tmp_path: Path) -> None:
    _, tc = build(tmp_path)
    with tc:
        c = SupervisorClient("http://testserver", client=tc)
        c.acquire("llama", "studio:j1")
        assert tc.post("/heartbeat", json={"owner": "studio:j1"}).json() == {"ok": True}
        assert c.heartbeat("studio:j1") is True and c.heartbeat("studio:other") is False
        assert tc.post("/release_owner", json={"owner": "studio:j1"}).json() == {"ok": True}
        assert c.status()["gpu_owner"] is None
        assert c.release_owner("studio:j1") is False  # already free
