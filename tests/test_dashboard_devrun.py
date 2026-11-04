"""Found by the first dev-mode launch on a laptop: the supervisor probes the dashboard at /health (it had no
such route, so the engine showed "Loading dashboard..." for ever), and the stock-footage gate refused the
dev-mode job although its clip source is a fake."""

import json
from pathlib import Path

import pytest

from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_dashboard.handlers import build_state
from ns_dashboard.supervisor_link import SupervisorLink
from ns_supervisor.services import build_service

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ns_dashboard.server import create_app  # noqa: E402


def _client(tmp_home: Path) -> TestClient:
    static = tmp_home / "static"
    static.mkdir(exist_ok=True)
    (static / "index.html").write_text("<!doctype html><title>Nightshift</title>", encoding="utf-8")
    (static / "app.js").write_text("// ui", encoding="utf-8")
    link = SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    state = build_state(Paths.from_env(), fake_services=True, link=link)
    app = create_app(state=state, start_worker=False, static_dir=static, heartbeat_s=0.01)
    return TestClient(app, base_url="http://127.0.0.1:8100")


def test_the_dashboard_answers_the_supervisors_health_probe(tmp_home: Path):
    r = _client(tmp_home).get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_the_supervisor_probes_the_dashboard_at_health(tmp_home: Path):
    d = build_service(Runtime.default("cpu-dev"), Paths.from_env(), "dashboard")
    assert d.health_url.endswith(":8100/api/health")


def test_dev_mode_needs_no_stock_footage_key(tmp_home: Path):
    paths = Paths.from_env()
    paths.config.mkdir(parents=True, exist_ok=True)
    rt = paths.config / "runtime.json"
    data = json.loads(rt.read_text(encoding="utf-8")) if rt.is_file() else {}
    data["dev_mode"] = True
    rt.write_text(json.dumps(data), encoding="utf-8")
    from ns_dashboard.svc_channels import ChannelsService
    from ns_dashboard.svc_jobs import JobsService

    state = build_state(paths, fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2))
    state.fake_services = False                         # a real-adapter dashboard in dev mode
    JobsService(state, ChannelsService(state))._require_footage_key()   # no NS-E502


def test_dev_mode_ignores_past_openings_so_the_fake_story_can_repeat(tmp_home: Path):
    from ns_studio.text.stages_master import _past_openings
    from tests.helpers import make_ctx

    paths = Paths.from_env()
    paths.config.mkdir(parents=True, exist_ok=True)
    rt = paths.config / "runtime.json"
    data = json.loads(rt.read_text(encoding="utf-8")) if rt.is_file() else {}
    data["dev_mode"] = True
    rt.write_text(json.dumps(data), encoding="utf-8")
    assert _past_openings(make_ctx(tmp_home)) == []


def test_settings_clears_a_key_with_an_empty_value(tmp_home: Path):
    from ns_dashboard.handlers import Dashboard

    paths = Paths.from_env()
    state = build_state(paths, fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2))
    svc = Dashboard(state).settings
    assert svc.set_key("pixabay", "abc")["set"] is True
    assert state.adapters.keyring.get("pixabay_api_key") == "abc"
    assert svc.set_key("pixabay", "")["set"] is False
    assert state.adapters.keyring.get("pixabay_api_key") is None
