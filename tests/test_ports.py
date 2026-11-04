"""C2/C1: a busy port is NS-E116 with the port and the owner named (setup smoke test, engine start)."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from installer.stage2.steps import s08_smoke
from installer.stage2.testing import example_answers, make_test_context
from ns_common import ports
from ns_common.errors import NsError
from ns_supervisor import client as client_mod
from ns_supervisor import procs
from ns_supervisor.services import DASHBOARD_PORT
from ns_supervisor.testing import stub_def


def listening_socket() -> tuple[socket.socket, int]:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen()
    return s, s.getsockname()[1]


def test_a_listening_port_is_not_free_and_a_closed_one_is() -> None:
    s, port = listening_socket()
    try:
        assert ports.is_free(port) is False
    finally:
        s.close()
    assert ports.is_free(port) is True


def test_the_message_names_the_port_the_owner_and_the_fix() -> None:
    msg = ports.port_message("story model", 8080, "node.exe (pid 4242)")
    assert "Port 8080 (story model)" in msg and "node.exe (pid 4242)" in msg
    assert "Close that program or change the port in Settings" in msg
    assert "Hyper-V" in ports.port_message("story model", 8080, None)


def test_ensure_free_raises_ns_e116_for_every_busy_port() -> None:
    owners = {8080: "node.exe (pid 1)", 8100: "httpd.exe (pid 2)"}
    with pytest.raises(NsError) as ei:
        ports.ensure_free(
            {"llama": 8080, "embed": 8081, "dashboard": 8100},
            free=lambda port, host: port not in owners, owner=owners.get,
        )
    err = ei.value
    assert err.code == "NS-E116" and "Port 8080 (story model)" in err.detail
    assert "Port 8100 (dashboard)" in err.detail and "8081" not in err.detail
    assert "node.exe (pid 1)" in err.detail and "httpd.exe (pid 2)" in err.detail


def test_ensure_free_is_silent_when_everything_is_free() -> None:
    ports.ensure_free({"llama": 8080, "tts": 8091}, free=lambda port, host: True)


def test_the_real_check_finds_a_real_listener() -> None:
    s, port = listening_socket()
    try:
        with pytest.raises(NsError) as ei:
            ports.ensure_free({"llama": port}, owner=lambda p: None)
        assert ei.value.code == "NS-E116" and f"Port {port}" in ei.value.detail
    finally:
        s.close()


def test_the_owner_comes_from_psutil() -> None:
    pytest.importorskip("psutil")
    s, port = listening_socket()
    try:
        who = ports.owner_of(port)
    finally:
        s.close()
    assert who is not None and "pid" in who


# ---- the engine start path --------------------------------------------------------------------------
def test_starting_a_service_on_a_busy_port_is_ns_e116_and_starts_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s, port = listening_socket()
    launched: list[int] = []
    monkeypatch.setattr(procs.subprocess, "Popen", lambda *a, **k: launched.append(1))
    try:
        with pytest.raises(NsError) as ei:
            procs.start(stub_def("llama", port), tmp_path / "logs")
    finally:
        s.close()
    assert ei.value.code == "NS-E116" and f"Port {port} (story model)" in ei.value.detail and launched == []


def test_the_engine_is_not_spawned_when_a_foreign_program_holds_its_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx

    from ns_common.paths import Paths

    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    spawned: list[int] = []
    monkeypatch.setattr(client_mod, "spawn_supervisor", lambda *a, **k: spawned.append(1))
    monkeypatch.setattr(ports, "is_free", lambda port, host="127.0.0.1": False)
    http = httpx.Client(transport=httpx.MockTransport(refuse))
    c = client_mod.SupervisorClient("http://127.0.0.1:8099", client=http)
    with pytest.raises(NsError) as ei:
        client_mod.ensure_running(c, Paths(tmp_path), wait_s=0.2)
    assert ei.value.code == "NS-E116" and "Port 8099 (engine)" in ei.value.detail and spawned == []


# ---- the setup smoke test ---------------------------------------------------------------------------
def smoke_ctx(tmp_path: Path, simulate: bool = False):
    return make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, simulate=simulate)


def test_the_smoke_step_lists_the_five_ports(tmp_path: Path) -> None:
    ctx = smoke_ctx(tmp_path)
    assert s08_smoke.runtime_ports(ctx) == {
        "llama": 8080, "embed": 8081, "tts": 8091, "supervisor": 8099, "dashboard": DASHBOARD_PORT,
    }


def test_the_smoke_step_stops_with_ns_e116_before_starting_anything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = smoke_ctx(tmp_path)
    monkeypatch.setattr(ports, "is_free", lambda port, host="127.0.0.1": port != 8080)
    monkeypatch.setattr(ports, "owner_of", lambda port: "python.exe (pid 99)")
    with pytest.raises(NsError) as ei:
        s08_smoke.check_ports(ctx)
    assert ei.value.code == "NS-E116" and "Port 8080 (story model)" in ei.value.detail
    assert "python.exe (pid 99)" in ei.value.detail and "8081" not in ei.value.detail


def test_a_simulation_does_not_judge_the_ports_of_the_target_pc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = smoke_ctx(tmp_path, simulate=True)
    monkeypatch.setattr(ports, "is_free", lambda port, host="127.0.0.1": False)
    s08_smoke.check_ports(ctx)

