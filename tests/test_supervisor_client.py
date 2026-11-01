import json
from pathlib import Path

import httpx
import pytest

from ns_common.acquire import Endpoint
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor.client import SupervisorClient, ensure_running


def make(handler) -> SupervisorClient:  # type: ignore[no-untyped-def]
    return SupervisorClient("http://sup:8099", client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_acquire_posts_contract_body_and_returns_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={"endpoint": "http://127.0.0.1:8080", "model_alias": "code"})

    ep = make(handler).acquire("llama", "opencode", model_alias="code", timeout_s=60)
    assert ep == Endpoint("http://127.0.0.1:8080", "code")
    req = seen[0]
    assert req.method == "POST" and req.url.path == "/acquire"
    assert json.loads(req.content) == {
        "service": "llama",
        "owner": "opencode",
        "model_alias": "code",
        "wait": True,
        "timeout_s": 60,
    }


def test_release_stop_all_warmup_shutdown_and_status() -> None:
    calls: list[tuple[str, str, dict]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content) if req.content else {}
        calls.append((req.method, req.url.path, body))
        if req.url.path == "/status":
            return httpx.Response(200, json={"gpu_owner": None, "services": {}})
        if req.url.path == "/warmup":
            return httpx.Response(200, json={"state": "ready"})
        return httpx.Response(200, json={"ok": True})

    c = make(handler)
    c.release("tts", "studio:j1")
    c.stop_all()
    assert c.warmup("llama") == "ready"
    c.shutdown(5)
    assert c.status() == {"gpu_owner": None, "services": {}}
    c.publish_event("job", {"stage": "voice"})
    assert calls[0] == ("POST", "/release", {"service": "tts", "owner": "studio:j1"})
    assert ("POST", "/stop_all", {}) in calls
    assert ("POST", "/warmup", {"service": "llama"}) in calls
    assert ("POST", "/shutdown", {"grace_s": 5}) in calls
    assert ("POST", "/events", {"kind": "job", "data": {"stage": "voice"}}) in calls


@pytest.mark.parametrize("code,status", [("NS-E201", 409), ("NS-E202", 502), ("NS-E203", 504)])
def test_error_bodies_become_ns_errors(code: str, status: int) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"code": code, "message": "m", "detail": "why"})

    with pytest.raises(NsError) as ei:
        make(handler).acquire("llama", "a")
    assert ei.value.code == code and ei.value.detail == "why"


def test_unparseable_error_is_e202() -> None:
    with pytest.raises(NsError) as ei:
        make(lambda req: httpx.Response(500, text="boom")).release("llama", "a")
    assert ei.value.code == "NS-E202" and "500" in ei.value.detail


def test_unreachable_supervisor_is_e202() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    c = make(handler)
    with pytest.raises(NsError) as ei:
        c.status()
    assert ei.value.code == "NS-E202"
    assert c.is_up() is False


def test_ensure_running_fast_path_does_not_spawn(tmp_path: Path) -> None:
    c = make(lambda req: httpx.Response(200, json={"services": {}}))
    spawned: list[int] = []
    ensure_running(c, Paths(tmp_path), spawn=lambda: spawned.append(1))
    assert spawned == []


def test_ensure_running_spawns_and_waits(tmp_path: Path) -> None:
    state = {"up": False}

    def handler(req: httpx.Request) -> httpx.Response:
        if state["up"]:
            return httpx.Response(200, json={"services": {}})
        raise httpx.ConnectError("refused")

    def spawn() -> None:
        state["up"] = True

    ensure_running(make(handler), Paths(tmp_path), spawn=spawn, wait_s=5)
    assert state["up"] is True


def test_ensure_running_gives_up_with_e202(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(NsError) as ei:
        ensure_running(make(handler), Paths(tmp_path), spawn=lambda: None, wait_s=0.6)
    assert ei.value.code == "NS-E202"
