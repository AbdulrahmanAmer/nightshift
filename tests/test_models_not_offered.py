"""D-36: getting a model this install cannot use is refused up front, with its own code (NS-E122).

A cpu-dev install has no voice design talker and no story model in `manifest.json`. `plan_items` used to
raise NS-E109 (a failed SHA256) for that, after the dashboard had already answered 202 "Downloading".
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ns_common.config import load_manifest, save_runtime
from ns_common.errors import NsError, describe
from ns_common.models import Runtime
from ns_common.paths import Paths, repo_root
from ns_dashboard.handlers import build_state
from ns_dashboard.supervisor_link import SupervisorLink
from ns_dashboard.updater import RealUpdateAdapter
from ns_supervisor import models_get

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ns_dashboard.server import create_app  # noqa: E402

BASE = "http://127.0.0.1:8100"


def manifest():
    return load_manifest(repo_root() / "manifest.json")


def cpu_dev_runtime() -> Runtime:
    """A cpu-dev `runtime.json` on the GGUF voice engine (`Runtime.default("cpu-dev")` says `reference`, whose
    voice design weights come from Hugging Face on any profile, so it is never refused)."""
    rt = Runtime.default("cpu-dev")
    rt.tts.engine = "qwentts"
    return rt


@pytest.mark.parametrize("kind", ["voicedesign", "story_llm"])
def test_plan_items_refuses_a_model_the_profile_lacks_with_ns_e122(tmp_path: Path, kind: str) -> None:
    with pytest.raises(NsError) as ei:
        models_get.plan_items(kind, manifest(), "cpu-dev", Paths(tmp_path), engine="qwentts")
    assert ei.value.code == "NS-E122"
    assert kind in ei.value.detail and "cpu-dev" in ei.value.detail
    assert describe("NS-E109").message not in str(ei.value)


def test_the_gpu_profile_still_plans_the_voice_design_files(tmp_path: Path) -> None:
    assert hasattr(models_get, "unavailable"), "the rule lives in models_get.unavailable"
    paths = Paths(tmp_path)
    assert len(models_get.plan_items("voicedesign", manifest(), "gpu", paths, engine="qwentts")) == 1
    assert len(models_get.plan_items("story_llm", manifest(), "gpu", paths)) == 1
    assert models_get.unavailable("voicedesign", manifest(), "gpu", "qwentts") is None
    assert models_get.unavailable("story_llm", manifest(), "gpu", "qwentts") is None
    # the PyTorch engine brings its own weights from Hugging Face, on any profile
    assert models_get.unavailable("voicedesign", manifest(), "cpu-dev", "reference") is None
    reason = models_get.unavailable("voicedesign", manifest(), "cpu-dev", "qwentts")
    assert reason is not None and "voicedesign" in reason and "cpu-dev" in reason


class RecordingUpdateAdapter(RealUpdateAdapter):
    """The real refusal rule (manifest and runtime.json read from disk); no process is ever started."""

    def __init__(self, paths: Paths) -> None:
        super().__init__(paths, runner=lambda cmd, timeout: (0, "", ""))
        self.downloads: list[str] = []

    def models_available(self) -> bool:
        return True

    def get_model(self, name: str, on_line: Callable[[str], None] = lambda _l: None) -> tuple[int, str]:
        self.downloads.append(name)
        return 0, f"{name} is ready"


def _client(tmp_home: Path) -> tuple[TestClient, RecordingUpdateAdapter]:
    static = tmp_home / "static"
    static.mkdir(exist_ok=True)
    (static / "index.html").write_text("<!doctype html><title>Nightshift</title>", encoding="utf-8")
    (static / "app.js").write_text("// ui", encoding="utf-8")
    paths = Paths.from_env()
    paths.ensure()
    save_runtime(paths, cpu_dev_runtime())
    state = build_state(paths, fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2))
    adapter = RecordingUpdateAdapter(paths)
    state.adapters.update = adapter
    app = create_app(state=state, start_worker=False, static_dir=static, heartbeat_s=0.01)
    return TestClient(app, base_url=BASE), adapter


def test_the_route_refuses_before_any_download_starts(tmp_home: Path) -> None:
    client, adapter = _client(tmp_home)
    for _ in range(2):  # asking twice answers the same refusal twice
        r = client.post("/api/models/get", json={"name": "voicedesign"})
        assert r.status_code == 409, r.text
        err: dict[str, Any] = r.json()["error"]
        assert err["code"] == "NS-E122"
        assert describe("NS-E122").message in err["message"] and "voicedesign" in err["message"]
        assert "cpu-dev" in err["message"]
        assert err["hint"] == describe("NS-E122").hint
        assert client.get("/api/models/state").json() == {}
    assert adapter.downloads == []
    story = client.post("/api/models/get", json={"name": "story_llm"})
    assert story.status_code == 409 and story.json()["error"]["code"] == "NS-E122"
    assert client.get("/api/models/state").json() == {} and adapter.downloads == []


def test_the_route_still_starts_a_model_this_install_can_use(tmp_home: Path) -> None:
    client, adapter = _client(tmp_home)
    save_runtime(Paths.from_env(), Runtime.default("gpu"))
    r = client.post("/api/models/get", json={"name": "voicedesign"})
    assert r.status_code == 202 and r.json()["state"] == "running"


def test_ns_models_get_prints_ns_e122_and_exits_non_zero(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from installer.stage2 import downloader
    from ns_supervisor.cli import app

    paths = Paths.from_env()
    paths.ensure()
    save_runtime(paths, cpu_dev_runtime())
    monkeypatch.setattr(downloader, "run_plan", lambda *a, **k: pytest.fail("nothing may be downloaded"))
    result = CliRunner().invoke(app, ["models", "get", "voicedesign"], env={"NIGHTSHIFT_HOME": str(tmp_home)})
    assert result.exit_code != 0
    assert "NS-E122" in result.output and "NS-E109" not in result.output
    assert describe("NS-E122").hint in result.output
    assert [f for f in (paths.home / "models").rglob("*") if f.is_file()] == []  # no file, not even a .part


def test_the_fake_update_adapter_refuses_only_when_told() -> None:
    from ns_dashboard.fakes import FakeUpdateAdapter

    fake = FakeUpdateAdapter()
    assert fake.model_unavailable("voicedesign") is None
    reason = "The voicedesign model is not available for the cpu-dev profile."
    fake.unavailable_models = {"voicedesign": reason}  # type: ignore[attr-defined]
    assert fake.model_unavailable("voicedesign") is not None and fake.model_unavailable("story_llm") is None
