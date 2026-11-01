import json
from pathlib import Path

import httpx

from ns_common.paths import Paths
from ns_supervisor import control
from ns_supervisor.client import SupervisorClient


def client_for(handler) -> SupervisorClient:  # type: ignore[no-untyped-def]
    return SupervisorClient("http://sup", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_stop_uses_the_api_when_the_engine_answers(tmp_path: Path) -> None:
    calls: list[str] = []
    state = {"down": False}

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path == "/shutdown":
            state["down"] = True
            return httpx.Response(200, json={"ok": True})
        if state["down"]:
            raise httpx.ConnectError("gone")
        return httpx.Response(200, json={"services": {}})

    lines = control.stop(Paths(tmp_path), client_for(handler), wait_s=5)
    assert "/shutdown" in calls
    assert any("API" in ln for ln in lines)


def _down(req: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused")


def test_stop_reports_when_nothing_runs(tmp_path: Path) -> None:
    assert control.stop(Paths(tmp_path), client_for(_down)) == ["The engine is not running."]


def _write_state(paths: Paths, pid: int, children: dict[str, int]) -> Path:
    f = paths.home / "run" / "supervisor.json"
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"pid": pid, "port": 8099, "children": children}), encoding="utf-8")
    return f


def test_stop_falls_back_to_recorded_pids(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    f = _write_state(paths, 100, {"llama": 201, "tts": 202})
    killed: list[int] = []
    images = {100: "python.exe", 201: "llama-server.exe"}  # 202 already gone

    lines = control.stop(paths, client_for(_down), kill=killed.append, identify=images.get)
    assert killed == [201, 100]
    assert any("llama" in ln for ln in lines) and not any("tts" in ln for ln in lines)
    assert not f.exists()


def test_stop_never_kills_a_reused_pid(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    _write_state(paths, 100, {})
    killed: list[int] = []
    lines = control.stop(paths, client_for(_down), kill=killed.append, identify=lambda pid: "chrome.exe")
    assert killed == []
    assert any("stale" in ln for ln in lines)


def test_stop_survives_a_corrupt_state_file(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    f = paths.home / "run" / "supervisor.json"
    f.parent.mkdir(parents=True)
    f.write_text("{not json", encoding="utf-8")
    assert "unreadable" in control.stop(paths, client_for(_down))[0]
    assert not f.exists()


def test_gate_runs_the_script_and_reports_missing(tmp_path: Path) -> None:
    code, msg = control.run_gate("m9", tmp_path)
    assert code == 2 and "gate_m9.py" in msg
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / "gate_m9.py").write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
    assert control.run_gate("m9", tmp_path)[0] == 3
    assert control.run_gate("9", tmp_path)[0] == 3
