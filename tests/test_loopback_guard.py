# ruff: noqa: E501
"""FN-1: no web page can control the engine, the voice service or the dashboard.

Every test enters through a server's real `create_app` and sends a request shaped like the caller's: Starlette's
TestClient with a loopback `base_url` (so `Host` is what a program of this PC sends), the product's own clients
(`SupervisorClient`, `SupervisorLink`, `TtsClient`) on top of it, and for the callers that are not Python a real
uvicorn server on a free port (PowerShell 5.1 with the launchers' own functions; the client of release 0.1.14).
The routes are read from `app.routes`, so a route added later is covered.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import subprocess
import sys
import threading
import types
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
uvicorn = pytest.importorskip("uvicorn")

from fastapi.testclient import TestClient  # noqa: E402

from ns_common.errors import NsError, describe  # noqa: E402
from ns_common.logging import NsLogger  # noqa: E402
from ns_common.models import Runtime  # noqa: E402
from ns_common.paths import Paths  # noqa: E402
from ns_common.tts_client import TtsClient  # noqa: E402
from ns_dashboard.assistant.llm import ScriptedToolLlm, StaticProvider  # noqa: E402
from ns_dashboard.handlers import build_state  # noqa: E402
from ns_dashboard.supervisor_link import SupervisorLink  # noqa: E402
from ns_supervisor import notify, pwsh  # noqa: E402
from ns_supervisor.app import create_app as engine_app  # noqa: E402
from ns_supervisor.client import SupervisorClient  # noqa: E402
from ns_supervisor.core import Supervisor  # noqa: E402
from ns_supervisor.testing import StubTable, fast_config, wait_until  # noqa: E402
from ns_tts.engine import FakeEngine  # noqa: E402
from ns_tts.server import create_app as voice_app  # noqa: E402
from ns_tts.voices import VoiceLibrary  # noqa: E402

try:  # the base has no shared check: there each test fails at its assertion (the request is answered)
    from ns_common import loopback
except ImportError:
    loopback = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
PORT = 18473  # only a number in the Host header: a TestClient binds nothing
BASE = f"http://127.0.0.1:{PORT}"
EVIL = "https://evil.example"
CODE = "NS-E207"
FOREIGN_ORIGINS = (EVIL, "null", "http://127.0.0.1:8100")
FOREIGN_HOSTS = (
    f"rebind.example:{PORT}", "127.0.0.1.nip.io", "localhost.evil.example", "[::ffff:127.0.0.1]", "",
    "evil.example:8099", f"192.168.1.20:{PORT}", f"0.0.0.0:{PORT}", f"[::1]x:{PORT}", "127.0.0.1:80@evil.example",
)
OWN_HOSTS = (f"127.0.0.1:{PORT}", f"localhost:{PORT}", f"[::1]:{PORT}", "127.0.0.1")
BROWSER_MARKS = ("origin", "referer", "sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest", "sec-fetch-user")
#: What changes or reads the engine: none of it may run for a refused request.
SPIED = {
    "stop_all": {"ok": True}, "shutdown": None, "acquire": {"endpoint": "http://127.0.0.1:1", "model_alias": None},
    "release": True, "release_owner": True, "release_unless_alive": {"ok": True}, "heartbeat": True,
    "warmup": "ready", "note_run_finished": {"exit_scheduled": False}, "publish": None,
    "status": {"gpu_owner": None, "services": {}},
}
ANY_BODY = {"service": "embed", "owner": "a page", "kind": "job", "data": {}}
needs_powershell = pytest.mark.skipif(pwsh.find_powershell() is None, reason="powershell not on PATH")


# ---------------------------------------------------------------------------------------------- the servers
class Spy:
    """Replaces the engine's own methods: `calls` is what a request reached."""

    def __init__(self, sup: Supervisor, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[str] = []
        for name, answer in SPIED.items():
            monkeypatch.setattr(sup, name, self._fake(name, answer, inspect.iscoroutinefunction(getattr(sup, name))))

    def _fake(self, name: str, answer: Any, is_async: bool) -> Any:
        def now(*a: Any, **k: Any) -> Any:
            self.calls.append(name)
            return answer

        async def later(*a: Any, **k: Any) -> Any:
            return now()

        return later if is_async else now


def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch | None = None) -> tuple[Supervisor, Any, Spy | None]:
    """The engine's real app around a Supervisor on the fake services of tests/test_supervisor_app.py."""
    sup = Supervisor(
        Paths(tmp_path / "ns"), Runtime.default("gpu"), config=fast_config(idle_unload_s=60), resolver=StubTable()
    )
    spy = Spy(sup, monkeypatch) if monkeypatch is not None else None
    return sup, engine_app(sup, run_monitor=False), spy


def voice(tmp_path: Path) -> Any:
    eng = FakeEngine()
    eng.load()
    return voice_app(eng, VoiceLibrary(tmp_path / "voices"), log=NsLogger("tts", tmp_path / "tts-logs"))


def dashboard(tmp_home: Path) -> Any:
    """The dashboard's real app on fake services (as tests/test_dashboard_api.py builds it)."""
    static = tmp_home / "static"
    static.mkdir(exist_ok=True)
    (static / "index.html").write_text("<!doctype html><title>Nightshift</title>", encoding="utf-8")
    (static / "app.js").write_text("// ui", encoding="utf-8")
    from ns_dashboard.server import create_app

    state = build_state(Paths.from_env(), fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2))
    return create_app(
        state=state, start_worker=False, static_dir=static, heartbeat_s=0.01,
        llm_provider=StaticProvider(ScriptedToolLlm([])),
    )


def client(app: Any, host: str = f"127.0.0.1:{PORT}") -> TestClient:
    return TestClient(app, base_url=f"http://{host}")


def routes(app: Any) -> list[tuple[str, str]]:
    """Every (method, path) the app answers, read from the app; a path parameter becomes `x`."""
    found: list[tuple[str, str]] = []
    for route in app.routes:
        path = getattr(route, "path", None)
        if not isinstance(path, str):
            continue
        import re

        path = re.sub(r"\{[^}]+\}", "x", path) or "/"
        for method in sorted(getattr(route, "methods", None) or {"GET"}):
            found.append((method, path))
    assert len(found) >= 5, found
    return found


def send(tc: TestClient, method: str, path: str, headers: dict[str, str]) -> Any:
    once = {"limit": 1}  # where the request is answered (the base), the event stream ends after one event
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        return tc.request(method, path, headers=headers, json=ANY_BODY, params=once)
    return tc.request(method, path, headers=headers, params=once)


def raw(app: Any, headers: list[tuple[bytes, bytes]], *, path: str = "/status", kind: str = "http") -> list[dict]:
    """One ASGI call with exactly these headers (no client library adds or orders any)."""
    sent: list[dict] = []

    async def receive() -> dict[str, Any]:
        if kind == "websocket":
            return {"type": "websocket.connect"}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def keep(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": kind, "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"", "root_path": "", "headers": headers,
        "client": ("127.0.0.1", 50000), "server": ("127.0.0.1", PORT), "subprotocols": [],
    }
    asyncio.run(app(scope, receive, keep))
    return sent


def log_rows(folder: Path, event: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for file in sorted(folder.glob("*.jsonl")):
        for line in file.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row["event"] == event:
                rows.append(row)
    return rows


def settle() -> None:
    """Wait for the note writers the refusals started (they never run on the request path)."""
    if loopback is not None:
        assert loopback.settle(10.0)


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at

    def step(self, **delta: float) -> None:
        self.at += timedelta(**delta)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock(datetime(2026, 11, 10, 9, 0, 0))
    if loopback is not None:
        monkeypatch.setattr(loopback, "wall_clock", c)
    return c


class Live:
    """An app behind a real uvicorn on a free loopback port; `seen` is the headers of each request."""

    def __init__(self, app: Any) -> None:
        self.seen: list[dict[str, str]] = []

        async def recorded(scope: dict[str, Any], receive: Any, send_: Any) -> None:
            if scope["type"] == "http":
                self.seen.append({k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]})
            await app(scope, receive, send_)

        cfg = uvicorn.Config(recorded, host="127.0.0.1", port=0, log_level="warning", lifespan="off")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        assert wait_until(lambda: self.server.started, 15.0), "uvicorn did not start"
        self.port = int(self.server.servers[0].sockets[0].getsockname()[1])
        self.url = f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(15.0)


@pytest.fixture
def live_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Live, Spy]]:
    _sup, app, spy = engine(tmp_path, monkeypatch)
    assert spy is not None
    live = Live(app)
    yield live, spy
    live.stop()


def refused(r: Any, why: str | None = None) -> str | None:
    """None when `r` is the engine's (or voice service's) refusal; otherwise what is wrong with it."""
    if r.status_code != 403:
        return f"answered {r.status_code}"
    try:
        body = r.json()
    except ValueError:
        return "403 without a JSON body" if r.request.method != "HEAD" else None
    if set(body) != {"code", "message", "detail"} or body["code"] != CODE:
        return f"403 with {body}"
    if body["message"] != describe(CODE).message or (why is not None and body["detail"] != why):
        return f"403 with {body}"
    return None


# ------------------------------------------------------------------------------- 1. a page's Origin, every route
def test_every_engine_route_refuses_a_browser_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _sup, app, spy = engine(tmp_path, monkeypatch)
    assert spy is not None
    tc = client(app)
    wrong: list[str] = []
    for method, path in routes(app):
        for origin in FOREIGN_ORIGINS:
            bad = refused(send(tc, method, path, {"origin": origin}), "origin")
            if bad:
                wrong.append(f"{method} {path} from {origin}: {bad}")
    assert {("POST", "/stop_all"), ("POST", "/shutdown"), ("GET", "/events")} <= set(routes(app))
    assert wrong == [], "a web page was answered: " + "; ".join(wrong[:6])
    assert spy.calls == []


def test_every_voice_route_refuses_a_browser_origin(tmp_path: Path):
    app = voice(tmp_path)
    tc = client(app)
    wrong: list[str] = []
    for method, path in routes(app):
        for origin in FOREIGN_ORIGINS:
            bad = refused(send(tc, method, path, {"origin": origin}), "origin")
            if bad:
                wrong.append(f"{method} {path} from {origin}: {bad}")
    assert ("POST", "/v1/voices/clone") in routes(app)
    assert wrong == [], "a web page was answered: " + "; ".join(wrong[:6])
    # the form a page can post without a preflight (multipart) never reaches the route either
    form = tc.post("/v1/voices/clone", data={"name": "Mallory"}, files={"audio": ("a.wav", b"RIFF", "audio/wav")},
                   headers={"origin": EVIL})
    assert refused(form, "origin") is None
    assert not (tmp_path / "voices" / "Mallory").exists()


def test_a_preflight_gets_no_cors_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _sup, app, spy = engine(tmp_path, monkeypatch)
    ask = {"origin": EVIL, "access-control-request-method": "POST", "access-control-request-headers": "content-type"}
    for server in (app, voice(tmp_path), ):
        r = client(server).options("/stop_all", headers=ask)
        assert r.status_code == 403
        assert not [h for h in r.headers if h.lower().startswith("access-control-")]
    assert spy is not None and spy.calls == []


# ------------------------------------------------------------ 2. a form POST the browser sent without an Origin
@pytest.mark.parametrize("path", ["/stop_all", "/shutdown", "/run_finished"])
@pytest.mark.parametrize(
    ("headers", "why"),
    [
        ({"sec-fetch-site": "cross-site"}, "fetch-mark"),
        ({"sec-fetch-site": "same-origin"}, "fetch-mark"),
        ({"sec-fetch-mode": "no-cors"}, "fetch-mark"),
        ({"referer": "https://evil.example/night.html"}, "referer"),
        ({"referer": f"http://127.0.0.1:{PORT}/"}, "referer"),
    ],
)
def test_a_form_post_without_origin_but_with_a_fetch_mark_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, headers: dict[str, str], why: str
):
    _sup, app, spy = engine(tmp_path, monkeypatch)
    r = client(app).post(path, content=b"", headers={"content-type": "text/plain", **headers})
    assert refused(r, why) is None, refused(r, why)
    assert spy is not None and spy.calls == []


def test_a_form_post_with_only_a_foreign_referer_is_refused_by_every_server(
    tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """No `Origin` (an old browser, or a page that strips it) but the page's address in `Referer`."""
    form = {"content-type": "application/x-www-form-urlencoded", "referer": "https://evil.example/night.html"}
    _sup, app, spy = engine(tmp_path, monkeypatch)
    assert refused(client(app).post("/stop_all", content=b"a=1", headers=form), "referer") is None
    assert spy is not None and spy.calls == []
    assert refused(client(voice(tmp_path)).post("/v1/audio/speech/cancel", content=b"a=1", headers=form)) is None
    dash = client(dashboard(tmp_home))
    r = dash.post("/api/notes/clear", content=b"a=1", headers=form)
    assert r.status_code == 403 and r.json()["error"]["code"] == CODE, r.text
    # the dashboard's own page (its Referer is the dashboard) is answered
    own = {"content-type": "application/json", "referer": BASE + "/", "origin": BASE, "sec-fetch-site": "same-origin"}
    assert dash.post("/api/notes/clear", content=b'{"ids": []}', headers=own).status_code == 200


# -------------------------------------------------------------------------------- 3. a name that is not this PC
def test_dns_rebinding_is_refused_on_every_server(tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    _sup, eng, spy = engine(tmp_path, monkeypatch)
    servers = [(eng, "/status"), (dashboard(tmp_home), "/api/status"), (voice(tmp_path), "/v1/voices")]
    wrong: list[str] = []
    for app, path in servers:
        tc = client(app)
        for host in FOREIGN_HOSTS:
            r = tc.get(path, headers={"host": host})
            said = r.text
            if r.status_code != 403 or CODE not in said or "services" in said or "gpu" in said or "voices" in said:
                wrong.append(f"{path} as {host!r}: {r.status_code} {said[:80]}")
        # no Host at all, and two of them (whichever a parser would keep, a foreign one refuses)
        own = (b"host", f"127.0.0.1:{PORT}".encode())
        for headers in ([], [own, (b"host", b"rebind.example")], [(b"host", b"rebind.example"), own]):
            status = raw(app, headers, path=path)[0].get("status")
            if status != 403:
                wrong.append(f"{path} with {headers}: {status}")
    assert wrong == [], "; ".join(wrong[:6])
    assert spy is not None and spy.calls == []


def test_a_websocket_from_a_page_is_closed_before_it_opens(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _sup, app, _spy = engine(tmp_path, monkeypatch)
    for headers in ([(b"host", b"rebind.example")], [(b"host", b"127.0.0.1:8099"), (b"origin", EVIL.encode())]):
        sent = raw(app, headers, path="/events", kind="websocket")
        assert sent == [{"type": "websocket.close", "code": 1008}]


# ----------------------------------------------------------------------------- 4. the programs of this PC
@pytest.mark.parametrize("host", OWN_HOSTS)
def test_programs_of_this_pc_are_answered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str):
    """The product's own clients build the requests, so the headers are the product's."""
    _sup, app, spy = engine(tmp_path, monkeypatch)
    assert spy is not None
    tc = client(app, host)
    ns = SupervisorClient(f"http://{host}", client=tc)
    assert ns.is_up() and ns.status() == SPIED["status"]
    assert ns.warmup("embed") == "ready"
    ns.stop_all()
    ns.shutdown(1)
    assert ns.heartbeat("studio:j1") is True and ns.release_owner("studio:j1") is True
    assert ns.run_finished() is False
    ns.publish_event("job", {"stage": "voice"})
    assert ns.acquire("llama", "studio:j1", model_alias="code", pid=4321).base_url == "http://127.0.0.1:1"
    ns.release("llama", "studio:j1")
    link = SupervisorLink(f"http://{host}", client=tc)
    assert link.status() == SPIED["status"]
    assert set(spy.calls) >= {"status", "warmup", "stop_all", "shutdown", "heartbeat", "release_owner",
                              "note_run_finished", "publish", "acquire", "release"}
    assert tc.get("/health").json() == {"ok": True}                       # the readiness probe at engine start


def test_the_event_stream_still_reaches_the_dashboard_and_the_page(tmp_path: Path, tmp_home: Path):
    """Server-sent events: the dashboard's backend reads the engine's stream, the page reads the dashboard's."""
    _sup, app, _ = engine(tmp_path)
    tc = client(app)
    stop = threading.Event()
    first = next(iter(SupervisorLink(BASE, client=tc).events(stop)))
    stop.set()
    assert first["kind"] == "service" and first["data"]["event"] == "snapshot"
    assert refused(tc.get("/events", params={"limit": 1}, headers={"origin": EVIL}), "origin") is None
    page = {"sec-fetch-site": "same-origin", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty",
            "accept": "text/event-stream", "referer": BASE + "/"}
    r = client(dashboard(tmp_home)).get("/api/events", params={"limit": 1}, headers=page)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert "data:" in r.text


def test_the_voice_clients_inside_the_studio_are_answered(tmp_path: Path):
    app = voice(tmp_path)
    for host in OWN_HOSTS:
        tts = TtsClient(f"http://{host}", client=client(app, host))
        assert tts.health() is True
        assert isinstance(tts.voices(), list)
    # the supervisor's readiness probe of the service
    assert client(app).get("/health").json()["ok"] is True


def test_a_script_with_an_origin_reads_what_to_do(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A refused program gets the engine's own error shape: SupervisorClient raises the coded error."""
    _sup, app, spy = engine(tmp_path, monkeypatch)
    tc = TestClient(app, base_url=BASE, headers={"origin": BASE})
    with pytest.raises(NsError) as e:
        SupervisorClient(BASE, client=tc).stop_all()
    assert e.value.code == CODE and e.value.detail == "origin"
    hint = describe(CODE).hint
    assert "127.0.0.1" in hint and "Origin" in hint
    assert spy is not None and spy.calls == []


# --------------------------------------------------------------- 4b. release 0.1.14 against a new engine
def client_of_0_1_14() -> Any:
    """`ns_supervisor/client.py` as release 0.1.14 shipped it (from the tag); None where the tag is missing."""
    try:
        out = subprocess.run(
            ["git", "show", "v0.1.14:ns_supervisor/client.py"], cwd=ROOT, capture_output=True, timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or b"class SupervisorClient" not in out.stdout:
        return None
    mod = types.ModuleType("ns_supervisor_client_0_1_14")
    exec(compile(out.stdout.decode("utf-8"), "v0.1.14:ns_supervisor/client.py", "exec"), mod.__dict__)  # noqa: S102
    return mod


def as_the_updater_of_0_1_14(make: Any, live: Live, spy: Spy) -> None:
    """`relaunch.restart` of 0.1.14 after the swap: is the engine up (`GET /status`), then `POST /warmup`."""
    old = make(f"http://127.0.0.1:{live.port}")              # `SupervisorClient.from_runtime` builds this URL
    assert old.is_up() is True
    assert old.warmup("dashboard") == "ready"
    assert old.status() == SPIED["status"]
    old.stop_all()                                           # `control.stop` of the next update, by the old `ns`
    old.shutdown(1)
    assert [c for c in spy.calls if c != "status"] == ["warmup", "stop_all", "shutdown"]
    assert len(live.seen) == 5
    for headers in live.seen:
        assert headers["host"] == f"127.0.0.1:{live.port}"
        assert not [h for h in headers if h in BROWSER_MARKS or h.startswith("sec-")], headers
        assert headers["user-agent"].startswith("python-httpx/")


def test_the_updater_of_0_1_14_is_answered_by_a_new_engine(live_engine: tuple[Live, Spy]):
    """The worst outcome of this item would be a refusal here: every update from 0.1.14 would end without a
    Dashboard. The client is the code of the tag itself, over a real socket."""
    old = client_of_0_1_14()
    if old is None:
        pytest.skip("the tag v0.1.14 is not in this clone (the next test sends the same requests)")
    as_the_updater_of_0_1_14(old.SupervisorClient, *live_engine)


def test_what_0_1_14_sends_is_answered_by_a_new_engine(live_engine: tuple[Live, Spy]):
    """The same calls with today's client, whose requests differ from 0.1.14's only by `pid` in `/acquire`
    (`git diff v0.1.14 -- ns_supervisor/client.py`): runs where the tag is not fetched (CI)."""
    as_the_updater_of_0_1_14(SupervisorClient, *live_engine)


def test_a_new_client_asks_nothing_of_a_0_1_14_engine():
    """A rollback puts 0.1.14's servers back, which have no check: no client of ours may depend on one (no
    header of its own, no token)."""
    for module in ("ns_supervisor/client.py", "ns_dashboard/supervisor_link.py", "ns_common/tts_client.py"):
        text = (ROOT / module).read_text(encoding="utf-8")
        assert "headers=" not in text and "X-NS-" not in text, module
    common = (ROOT / "launchers" / "_common.ps1").read_text(encoding="ascii")
    post = common[common.index("function Invoke-NsPost"):]
    assert "-Headers" not in post[: post.index("\n}")]


# ------------------------------------------------------------------------------ 5. the dashboard's own rule
def test_the_dashboard_rule_is_unchanged(tmp_home: Path):
    assert loopback is not None, "the shared check does not exist"
    from ns_dashboard import security

    assert security.is_loopback_host is loopback.is_loopback_host
    assert security.is_loopback_origin is loopback.is_loopback_origin
    for host in ("127.0.0.1", "127.0.0.1:8100", "localhost", "LOCALHOST:8100", "[::1]:8100", "[::1]", "::1"):
        assert loopback.is_loopback_host(host), host
    for host in ("", None, "evil.example", "evil.example:8100", "127.0.0.1.evil.com", "192.168.1.5:8100", "0.0.0.0"):
        assert not loopback.is_loopback_host(host), host
    assert loopback.is_loopback_origin(None) and loopback.is_loopback_origin("")
    assert loopback.is_loopback_origin("http://127.0.0.1:8100") and loopback.is_loopback_origin("http://localhost:8100")
    for origin in ("null", "https://evil.example", "http://127.0.0.1.evil.example", "file://", "http://a@127.0.0.1"):
        assert not loopback.is_loopback_origin(origin), origin
    tc = client(dashboard(tmp_home))
    # D-45: the page's origin must be the one the request addressed (scheme, name AND port), not any loopback
    # one: the Vite dev server's proxy keeps the Host its page addressed, so it is the same origin
    for host in (f"127.0.0.1:{PORT}", "localhost:5173", "127.0.0.1:5173", "[::1]:8100"):
        page = {"host": host, "origin": f"http://{host}", "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors"}
        assert tc.get("/api/status", headers=page).status_code == 200, host
        assert tc.post("/api/notes/clear", headers=page, json={"ids": []}).status_code == 200, host
    for origin in ("http://localhost:5173", "http://127.0.0.1:5173", "http://[::1]:8100", f"http://localhost:{PORT}"):
        page = {"origin": origin, "sec-fetch-site": "same-site", "sec-fetch-mode": "cors"}
        assert tc.post("/api/notes/clear", headers=page, json={"ids": []}).status_code == 403, origin
    for host in (f"localhost:{PORT}", f"[::1]:{PORT}", "127.0.0.1"):
        assert tc.get("/api/status", headers={"host": host}).status_code == 200, host
        assert tc.get("/", headers={"host": host}).status_code == 200, host
    for origin in ("null", EVIL, "http://127.0.0.1.evil.example"):
        r = tc.get("/api/status", headers={"origin": origin})
        assert r.status_code == 403, origin
        assert set(r.json()) == {"error"} and set(r.json()["error"]) == {"code", "message", "hint"}
        assert r.json()["error"]["code"] == CODE and r.json()["error"]["hint"] == describe(CODE).hint


def test_the_dashboard_opens_from_a_link_but_answers_no_foreign_page(tmp_home: Path):
    tc = client(dashboard(tmp_home))
    # typed, a bookmark, the Desktop shortcut's window, a link on another site: the page opens
    for marks in ({}, {"sec-fetch-site": "none", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"},
                  {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document",
                   "referer": "https://search.example/"}):
        assert tc.get("/", headers=marks).status_code == 200, marks
    # what a page on another site can make the browser send without an Origin: an image, a script, a form
    for method, path, marks in (
        ("GET", "/api/update/check", {"sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors"}),
        ("GET", "/api/status", {"sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "image"}),
        ("POST", "/api/engine/stop_all", {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"}),
        ("POST", "/api/engine/stop_all", {"referer": "https://evil.example/"}),
        # a GET that does work (the update check asks GitHub, the status syncs the schedule record) cannot be
        # set off by a link, a pop-up or a frame on another site either: nothing under /api/ is a page
        ("GET", "/api/update/check", {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate"}),
        ("GET", "/api/status", {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "iframe"}),
        ("GET", "/api/update/check", {"referer": "https://evil.example/"}),
        ("HEAD", "/api/diagnostics/latest", {"referer": "https://evil.example/"}),
    ):
        r = tc.request(method, path, headers=marks)
        assert r.status_code == 403, (method, path, marks, r.status_code)
        assert method == "HEAD" or r.json()["error"]["code"] == CODE
    # the page's own requests for the same routes (same origin, its own Referer or none) are answered
    for marks in ({"sec-fetch-site": "same-origin", "sec-fetch-mode": "cors"}, {"referer": BASE + "/"}, {}):
        assert tc.get("/api/status", headers=marks).status_code == 200, marks
    # D-45: a page on another local port is another site's page (same-site is not same-origin)
    other = {"sec-fetch-site": "same-site", "sec-fetch-mode": "cors", "referer": "http://127.0.0.1:5173/"}
    assert tc.get("/api/status", headers=other).status_code == 403


# ------------------------------------------------------------------------- 6. PowerShell 5.1, the launchers
@needs_powershell
def test_powershell_callers_are_answered(tmp_path: Path, live_engine: tuple[Live, Spy]):
    live, spy = live_engine
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "runtime.json").write_text(json.dumps({"supervisor_port": live.port}), encoding="utf-8")
    script = (
        f". '{ROOT / 'launchers' / '_common.ps1'}'\n"
        "$h = Invoke-RestMethod -Uri ((Get-NsSupervisorUrl) + '/health') -TimeoutSec 20\n"
        "$up = Test-NsSupervisor\n"
        "$w = Invoke-NsPost -Path '/warmup' -Body @{ service = 'embed' }\n"
        "$a = Invoke-NsPost -Path '/acquire' -Body @{ service = 'llama'; owner = 'opencode'; wait = $false }\n"
        "$r = Invoke-NsPost -Path '/release' -Body @{ service = 'llama'; owner = 'opencode' }\n"
        "Write-Output ('health=' + $h.ok + ' up=' + $up + ' warmup=' + $w.state + ' release=' + $r.ok)\n"
    )
    out = pwsh.run_powershell(script, env_extra={"NIGHTSHIFT_HOME": str(home)}, timeout_s=120)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "health=True up=True warmup=ready release=True" in out.stdout
    assert [c for c in spy.calls if c != "status"] == ["warmup", "acquire", "release"]
    for headers in live.seen:
        assert headers["host"] == f"127.0.0.1:{live.port}"
        assert not [h for h in headers if h in BROWSER_MARKS], headers
    # and the same shell with a page's header is refused with the code
    bad = pwsh.run_powershell(
        "try { Invoke-RestMethod -Method Post -Uri ('http://127.0.0.1:' + $env:NS_T_PORT + '/stop_all') "
        "-Headers @{ Origin = 'https://evil.example' } -TimeoutSec 20; Write-Output 'answered' } "
        "catch { Write-Output ([int]$_.Exception.Response.StatusCode) }\n",
        env_extra={"NS_T_PORT": str(live.port)}, timeout_s=120,
    )
    assert bad.stdout.strip() == "403", bad.stdout + bad.stderr
    assert "stop_all" not in spy.calls


# ---------------------------------------------------------------------------------------- 7. the bind address
def test_each_server_binds_loopback_only(tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    import argparse

    from ns_common import first_start
    from ns_common.config import save_runtime
    from ns_dashboard import server as dash_server
    from ns_supervisor import app as engine_module
    from ns_supervisor import launch_reach
    from ns_tts import server as tts_server

    binds: dict[str, Any] = {}

    class Bound(Exception):
        pass

    def config(app: Any, **kw: Any) -> Any:
        binds["engine"] = kw.get("host")
        raise Bound

    # the engine
    fake = types.ModuleType("uvicorn")
    fake.Config = config  # type: ignore[attr-defined]
    fake.Server = lambda cfg: None  # type: ignore[attr-defined]
    fake.run = lambda app, **kw: binds.setdefault("run", []).append(kw.get("host"))  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    monkeypatch.setattr(first_start, "ensure", lambda *a, **k: None)
    monkeypatch.setattr(launch_reach, "start", lambda *a, **k: None)
    monkeypatch.setattr(engine_module, "relock_secret_files", lambda *a, **k: 0)
    paths = Paths.from_env()
    paths.ensure()
    wide = Runtime.default("cpu-dev")
    wide.tts.engine = "fake"
    wide.tts.host = "0.0.0.0"  # noqa: S104 - what a hand-edited runtime.json could say
    save_runtime(paths, wide)
    with pytest.raises(Bound):
        engine_module._serve(argparse.Namespace(port=PORT, headless=True), paths)  # noqa: SLF001
    assert binds["engine"] == "127.0.0.1"
    # the dashboard
    monkeypatch.setattr(dash_server, "create_app", lambda **kw: object())
    monkeypatch.setattr(dash_server, "configure_tmp", lambda p: None)
    assert dash_server.main(["--port", str(PORT)]) == 0
    # the voice service: a runtime.json that names every address is still bound to this PC only
    monkeypatch.setattr(sys, "argv", ["ns_tts.server", "--engine", "fake", "--port", str(PORT)])
    real_voice_app = tts_server.create_app
    built: list[Any] = []
    monkeypatch.setattr(tts_server, "create_app", lambda e, lib, log=None: built.append(real_voice_app(e, lib, log=log)))
    tts_server.main()
    assert binds["run"] == ["127.0.0.1", "127.0.0.1"], binds
    # and the check does not rest on the bind: a request addressed to this PC's LAN name is refused
    r = client(built[0], f"192.168.1.20:{PORT}").get("/v1/voices")
    assert r.status_code == 403 and r.json()["code"] == CODE
    # the settings a new install gets name this PC only (the model servers are started with these)
    fresh = Runtime.default("gpu")
    assert (fresh.tts.host, fresh.llama.host, fresh.embed.host) == ("127.0.0.1",) * 3
    # no server of ours is written to listen wider
    for module in ("ns_supervisor/app.py", "ns_dashboard/server.py", "ns_tts/server.py", "ns_supervisor/devfake.py"):
        text = (ROOT / module).read_text(encoding="utf-8")
        assert "0.0.0.0" not in text and 'host=""' not in text and "host='::'" not in text, module


# ------------------------------------------------------------------- 8 to 10. what the owner is told, and when
def notes_of(sup: Supervisor) -> list[str]:
    return [n["message"] for n in notify.read_notes(sup.paths)]


def knock(tc: TestClient, origin: str, times: int = 1, *, clock: Clock | None = None, apart_s: float = 0) -> None:
    for i in range(times):
        if clock is not None and i:
            clock.step(seconds=apart_s)
        r = tc.post("/stop_all", headers={"origin": origin})
        assert r.status_code == 403, r.text


def test_one_refusal_is_logged_once_and_tells_the_owner_nothing(tmp_path: Path, clock: Clock):
    sup, app, _ = engine(tmp_path)
    knock(client(app), EVIL)
    settle()
    rows = log_rows(sup.paths.logs, "request_refused")
    assert len(rows) == 1 and rows[0]["level"] == "WARN"
    assert rows[0]["data"] == {
        "code": CODE, "why": "origin", "host": f"127.0.0.1:{PORT}", "origin": EVIL, "method": "POST",
        "path": "/stop_all",
    }
    assert notes_of(sup) == []
    assert sup.status()["services"]["llama"]["state"] == "stopped"  # and the engine is as it was


def test_repeated_refusals_leave_one_note_per_page_per_day(tmp_path: Path, clock: Clock):
    sup, app, _ = engine(tmp_path)
    tc = client(app)
    knock(tc, EVIL, 3, clock=clock, apart_s=60)                      # 09:00, 09:01, 09:02
    settle()
    said = notes_of(sup)
    assert len(said) == 1, said
    assert said[0] == (
        "NS-E207: A web page open in your browser tried at least 3 times to control Nightshift (from "
        "https://evil.example, by 09:02). Nightshift refused every one of those requests, so none of them ran. "
        "If you do not know that page, close it."
    )
    assert notify.read_notes(sup.paths)[0]["at"] == "2026-11-10T09:02:00"
    # it keeps trying all morning: no second note, one first line, and a count at most every ten minutes
    for _ in range(50):
        clock.step(minutes=1)
        knock(tc, EVIL)
    settle()
    assert len(notes_of(sup)) == 1
    assert len([r for r in log_rows(sup.paths.logs, "request_refused") if r["data"]["origin"] == EVIL]) == 1
    again = log_rows(sup.paths.logs, "requests_refused")
    assert 4 <= len(again) <= 5, again
    assert all(r["level"] == "WARN" and r["data"]["code"] == CODE and r["data"]["page"] == EVIL for r in again)
    assert [r["data"]["count"] for r in again] == [10] * len(again)
    assert again[-1]["data"]["total"] <= 53
    # another page the same day: its own note
    knock(tc, "https://other.example", 3)
    settle()
    assert len(notes_of(sup)) == 2 and "https://other.example" in notes_of(sup)[1]
    # the next day the first page is still open: the owner is told again, once
    clock.step(days=1)
    knock(tc, EVIL, 3)
    knock(tc, EVIL, 20)
    settle()
    said = notes_of(sup)
    assert len(said) == 3 and "https://evil.example" in said[2] and "tried at least 3 times" in said[2]


def test_fewer_than_three_in_ten_minutes_is_not_a_page_that_keeps_trying(tmp_path: Path, clock: Clock):
    sup, app, _ = engine(tmp_path)
    tc = client(app)
    knock(tc, "https://twice.example", 2, clock=clock, apart_s=60)   # two in ten minutes
    knock(tc, "https://slow.example", 3, clock=clock, apart_s=330)   # three spread over eleven minutes
    settle()
    assert notes_of(sup) == []
    knock(tc, "https://edge.example", 3, clock=clock, apart_s=300)   # the third exactly ten minutes after the first
    settle()
    assert len(notes_of(sup)) == 1 and "https://edge.example" in notes_of(sup)[0]


def test_a_page_with_many_names_cannot_flood_the_notes(tmp_path: Path, clock: Clock):
    """Each sub-domain is its own origin: three pages are named, the rest share one note."""
    sup, app, _ = engine(tmp_path)
    tc = client(app)
    for i in range(12):
        knock(tc, f"https://n{i}.evil.example", 3)
        settle()   # one page's note writer at a time: which three are named is the order they came in
    said = notes_of(sup)
    assert len(said) == 4, said
    assert [f"https://n{i}.evil.example" in said[i] for i in range(3)] == [True] * 3
    assert said[3].startswith("NS-E207: Still more web pages or names tried to control Nightshift on 2026-11-10 "
                              "(among them https://n3.evil.example, by 09:00).")
    assert all(len(m) < 400 for m in said)


def test_a_name_that_is_not_this_pc_is_told_in_words_that_are_true(tmp_path: Path, clock: Clock):
    """No browser mark (a rebinding page's own requests carry none, and neither does a script that uses the
    PC's name): the note does not claim a web page, and says what a script must do."""
    sup, app, _ = engine(tmp_path)
    tc = client(app, f"rebind.example:{PORT}")
    for _ in range(3):
        assert tc.get("/status").status_code == 403
    settle()
    said = notes_of(sup)
    assert len(said) == 1 and said[0].startswith("NS-E207: Nightshift was called at least 3 times by the name rebind.example")
    assert "http://127.0.0.1" in said[0] and "refused every one of those requests, so none of them ran" in said[0]
    assert "nothing was changed" not in said[0]
    hostile = "evil.example\x1b[31m<script>" + "a" * 300   # what a page writes into its own name
    for _ in range(3):
        assert client(app).get("/status", headers={"host": hostile}).status_code == 403
    settle()
    assert len(notes_of(sup)) == 2
    assert "\x1b" not in notes_of(sup)[1] and len(notes_of(sup)[1]) < 500


def test_the_dashboard_tells_the_owner_the_same_way_and_a_restart_does_not_repeat_it(tmp_home: Path, clock: Clock):
    tc = client(dashboard(tmp_home))
    for _ in range(3):
        assert tc.post("/api/engine/stop_all", headers={"origin": EVIL}).status_code == 403
    settle()
    paths = Paths.from_env()
    said = [n["message"] for n in notify.read_notes(paths)]
    assert len(said) == 1 and said[0].startswith("NS-E207: A web page open in your browser tried at least 3 times")
    assert tc.get("/api/notes").json()[0]["message"] == said[0]   # the bell and the Queue page read this route
    # a restarted dashboard (its counters are at zero) meets the note that still waits: no second one that day
    again = client(dashboard(tmp_home))
    for _ in range(3):
        assert again.post("/api/engine/stop_all", headers={"origin": EVIL}).status_code == 403
    settle()
    assert len(notify.read_notes(paths)) == 1


def test_a_note_that_cannot_be_written_does_not_slow_the_answer(
    tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
):
    sup, app, _ = engine(tmp_path)
    tc = client(app)
    entered, leave = threading.Event(), threading.Event()
    real = notify.leave_chosen_note

    def stuck(*a: Any, **k: Any) -> Any:
        entered.set()
        assert leave.wait(30)
        return real(*a, **k)

    monkeypatch.setattr(notify, "leave_chosen_note", stuck)
    knock(tc, EVIL, 3)                                   # the third answer came back while the writer waits
    assert entered.wait(10) and not leave.is_set()
    knock(tc, EVIL, 5)                                   # and more are answered, with no second writer
    assert notes_of(sup) == []
    leave.set()
    settle()
    assert len(notes_of(sup)) == 1

    # a notes file that cannot be written: one WARN, never a 500, and another try ten minutes later
    tries: list[int] = []

    def broken(*a: Any, **k: Any) -> None:
        tries.append(1)
        raise OSError("the notes file is locked")

    monkeypatch.setattr(notify, "leave_chosen_note", broken)
    other = "https://other.example"
    knock(tc, other, 3)
    settle()
    failed = log_rows(sup.paths.logs, "morning_note_failed")
    assert len(failed) == 1 and failed[0]["level"] == "WARN" and "locked" in failed[0]["msg"]
    clock.step(minutes=9)
    knock(tc, other, 4)
    settle()
    assert len(tries) == 1 and len(log_rows(sup.paths.logs, "morning_note_failed")) == 1
    monkeypatch.setattr(notify, "leave_chosen_note", real)
    clock.step(minutes=2)
    knock(tc, other)                                     # the file can be written again: the note is left now
    settle()
    said = notes_of(sup)
    assert len(said) == 2 and other in said[1] and "tried at least 8 times" in said[1]


# ====================================================== round 2 (review of e62f26b): a form with no sender at all
# Internet Explorer, Edge's IE mode and old Firefox send a cross-site form POST with no Origin, no Sec-Fetch-*
# and (from an HTTPS page) no Referer. All such a page can send without a preflight is a plain HTML form.
FORM_BODIES = (
    ("application/x-www-form-urlencoded", b"a=1"),
    ("application/x-www-form-urlencoded", b""),                       # an empty form still says its type
    ("Application/X-WWW-Form-Urlencoded; charset=UTF-8", b"a=1"),
    ("multipart/form-data; boundary=xx", b'--xx\r\nContent-Disposition: form-data; name="a"\r\n\r\n1\r\n--xx--\r\n'),
    ("MULTIPART/FORM-DATA; boundary=xx", b'--xx\r\nContent-Disposition: form-data; name="a"\r\n\r\n1\r\n--xx--\r\n'),
    ("text/plain", b'{"service": "embed", "owner": "a page", "grace_s": 1, "a": "='),
    ("text/plain", b""),
    ("Text/Plain;charset=utf-8", b'{"a": 1}'),
    (" text/plain ", b'{"a": 1}'),
    (None, b'{"a": 1}'),                                              # a body with no content type at all
)
UNSAFE = ("POST", "PUT", "PATCH", "DELETE")
#: Origin absent, foreign, a file page or sandboxed frame, a foreign name that starts like ours, and loopback.
FORM_ORIGINS = (None, EVIL, "null", "http://127.0.0.1.evil.example", "", BASE, "http://localhost:5173")


def post_form(tc: TestClient, method: str, path: str, kind: str | None, body: bytes, origin: str | None) -> Any:
    headers: dict[str, str] = {}
    if kind is not None:
        headers["content-type"] = kind
    if origin is not None:
        headers["origin"] = origin
    return tc.request(method, path, content=body, headers=headers, params={"limit": 1})


def every_form(app: Any) -> Iterator[tuple[str, str, str | None, bytes, str | None]]:
    for method, path in routes(app):
        if method in UNSAFE:
            for kind, body in FORM_BODIES:
                for origin in FORM_ORIGINS:
                    if origin is None and not body:
                        continue   # D-50: no browser header and no body is a program's (test_d50_* below)
                    yield method, path, kind, body, origin


def test_a_form_with_no_sender_reaches_no_route_of_the_engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _sup, app, spy = engine(tmp_path, monkeypatch)
    assert spy is not None
    tc = client(app)
    unsafe = {(m, p) for m, p in routes(app) if m in UNSAFE}
    assert {("POST", "/stop_all"), ("POST", "/shutdown"), ("POST", "/run_finished")} <= unsafe and len(unsafe) >= 9
    wrong = [
        f"{method} {path} as {kind!r} ({len(body)} bytes) from {origin!r}: {bad}"
        for method, path, kind, body, origin in every_form(app)
        if (bad := refused(post_form(tc, method, path, kind, body, origin), "form" if origin is None else "origin"))
    ]
    assert wrong == [], f"{len(wrong)} forms were answered: " + "; ".join(wrong[:5])
    assert spy.calls == []


def test_a_form_with_no_sender_reaches_no_route_of_the_voice_service(tmp_path: Path):
    app = voice(tmp_path)
    tc = client(app)
    assert ("POST", "/v1/audio/speech/cancel") in routes(app)
    wrong = [
        f"{method} {path} as {kind!r} ({len(body)} bytes) from {origin!r}: {bad}"
        for method, path, kind, body, origin in every_form(app)
        if (bad := refused(post_form(tc, method, path, kind, body, origin), "form" if origin is None else "origin"))
    ]
    assert wrong == [], f"{len(wrong)} forms were answered: " + "; ".join(wrong[:5])


def test_a_form_reaches_the_dashboard_only_from_its_own_page(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """Every route that is not a GET x every content type a form can have x Origin absent, foreign, null, empty
    and loopback: only the dashboard's own page (a loopback Origin, which the browser states) gets past the
    check. The update route is the reason: it installs the zip it is given."""
    from ns_dashboard.svc_library import LibraryService

    installed: list[str] = []
    monkeypatch.setattr(
        LibraryService, "update_apply_file", lambda self, name, *files: installed.append(name) or {"ok": True}
    )
    app = dashboard(tmp_home)
    tc = TestClient(app, base_url=BASE, raise_server_exceptions=False)
    unsafe = {(m, p) for m, p in routes(app) if m in UNSAFE}
    assert ("POST", "/api/update/file") in unsafe and ("PUT", "/api/uploads/x") in unsafe and len(unsafe) >= 30
    wrong: list[str] = []
    own_pages = (BASE,)   # D-45: "http://localhost:5173" to this Host is another origin, refused like the rest
    for method, path, kind, body, origin in every_form(app):
        if origin in own_pages and path not in ("/api/notes/clear", "/api/update/file"):
            continue   # past the check the route itself runs: two routes show that the page's form passes
        r = post_form(tc, method, path, kind, body, origin)
        if origin in own_pages:
            if r.status_code == 403:
                wrong.append(f"{method} {path} as {kind!r} from the page {origin!r}: refused")
        elif r.status_code != 403 or r.json()["error"]["code"] != CODE:
            wrong.append(f"{method} {path} as {kind!r} ({len(body)} bytes) from {origin!r}: {r.status_code}")
    assert wrong == [], f"{len(wrong)} wrong answers: " + "; ".join(wrong[:5])
    assert installed == []
    # the attack itself: a page's own zip with its own .sha256, as a multipart form with no sender
    zip_bytes, digest = b"PK\x05\x06" + b"\x00" * 18, b"0" * 64
    upload = {"file": ("Nightshift-Update-9.9.9.zip", zip_bytes, "application/zip"),
              "checksum": ("Nightshift-Update-9.9.9.zip.sha256", digest, "text/plain")}
    for origin in (None, EVIL, "null"):
        r = tc.post("/api/update/file", files=upload, headers={"origin": origin} if origin else {})
        assert r.status_code == 403 and r.json()["error"]["code"] == CODE, origin
    assert installed == []
    # Settings, "From a file": the dashboard's page sends the same form with its Origin, and it is installed
    r = tc.post("/api/update/file", files=upload, headers={"origin": BASE, "sec-fetch-site": "same-origin"})
    assert r.status_code == 200 and installed == ["Nightshift-Update-9.9.9.zip"], r.text


def test_a_body_that_is_not_json_is_never_read_as_json(tmp_home: Path):
    """The parser cannot be the way round the check: JSON in a text/plain body is refused even from the
    dashboard's own origin; JSON as JSON and an empty body are read as before."""
    tc = client(dashboard(tmp_home))
    body = json.dumps({"channel": "after-dark", "format": "shorts_only"}).encode()
    for kind in ("text/plain", "text/plain; charset=utf-8", "application/x-www-form-urlencoded", None):
        r = post_form(tc, "POST", "/api/jobs/run", kind, body, BASE)
        assert r.status_code == 415 and r.json()["error"]["code"] == "not_json", (kind, r.status_code, r.text)
    for kind in ("application/json", "application/json; charset=utf-8", "Application/JSON"):
        r = post_form(tc, "POST", "/api/jobs/verify", kind, b"{}", None)        # a program: JSON, no Origin
        assert r.status_code == 201, (kind, r.text)
    assert tc.post("/api/jobs/verify").status_code == 201                       # no body at all
    # no body reaches the route, which wants the ids of the notes shown (FN-2): its own 422, not the check's
    r = tc.post("/api/notes/clear")
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_options", r.text


def test_the_programs_that_post_without_a_body_or_with_json_still_pass(
    tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """Each product caller of a route that is not a GET, with the body and content type it really sends."""
    _sup, app, spy = engine(tmp_path, monkeypatch)
    assert spy is not None
    live_headers: list[dict[str, str]] = []

    async def recorded(scope: dict[str, Any], receive: Any, send_: Any) -> None:
        if scope["type"] == "http" and scope["method"] != "GET":
            live_headers.append({k.decode().lower(): v.decode() for k, v in scope["headers"]})
        await app(scope, receive, send_)

    tc = TestClient(recorded, base_url=BASE)
    ns = SupervisorClient(BASE, client=tc)
    ns.stop_all()                                                     # no body, no content type
    assert ns.run_finished() is False                                 # no body, no content type
    ns.shutdown(1)
    assert ns.warmup("embed") == "ready"
    assert SupervisorLink(BASE, client=tc).warmup("dashboard") is not None
    kinds = [(h.get("content-type"), h.get("content-length")) for h in live_headers]
    assert kinds[:2] == [(None, "0"), (None, "0")], kinds
    assert all(kind == "application/json" for kind, _ in kinds[2:]) and len(kinds) == 5, kinds
    assert spy.calls == ["stop_all", "note_run_finished", "shutdown", "warmup", "warmup"]
    # curl -X POST and PowerShell's Invoke-RestMethod -Method Post with no body send no content type either
    assert tc.post("/stop_all", headers={"content-length": "0"}).status_code == 200
    # the studio's cancel of a batch: a POST with no body
    voices = voice(tmp_path)
    assert client(voices).post("/v1/audio/speech/cancel").status_code == 200
    TtsClient(BASE, client=client(voices)).cancel_batch()
    # the dashboard for a program (dev/prove_ctx.py sends JSON through urllib; `ns` posts no body)
    dash = client(dashboard(tmp_home))
    r = dash.post("/api/notes/clear")                                 # reaches the route (FN-2: ids wanted)
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_options", r.text
    assert dash.post("/api/notes/clear", json={"ids": []}).status_code == 200
    assert dash.post("/api/jobs/verify", json={}).status_code == 201
    # the page's file uploads: multipart and a chunk with no content type, both with the page's Origin
    page = {"origin": BASE, "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors"}
    r = dash.put("/api/uploads/nope-20260101-abcd", content=b"chunk", headers=page)
    assert r.status_code != 403, r.text
    r = dash.put("/api/uploads/nope-20260101-abcd", content=b"chunk")
    assert r.status_code == 403 and r.json()["error"]["code"] == CODE


def test_three_forms_with_no_sender_are_told_in_words_that_are_true(tmp_path: Path, clock: Clock):
    sup, app, _ = engine(tmp_path)
    tc = client(app)
    for _ in range(3):
        r = tc.post("/stop_all", content=b"a=1", headers={"content-type": "text/plain"})
        assert refused(r, "form") is None
    settle()
    said = notes_of(sup)
    assert len(said) == 1 and said[0] == (
        "NS-E207: Something on this PC sent Nightshift a web form at least 3 times (by 09:00). Nightshift refused "
        "every one of those requests, so none of them ran. No program of Nightshift does that; it is most likely a "
        "web page in an old browser or in Internet Explorer mode. Close the web pages you do not know. A script of "
        "yours must send JSON, or no body."
    )
    rows = log_rows(sup.paths.logs, "request_refused")
    assert len(rows) == 1 and rows[0]["data"]["why"] == "form" and rows[0]["data"]["origin"] is None


def test_a_file_page_or_a_sandboxed_frame_is_named_usefully(tmp_path: Path, clock: Clock):
    sup, app, _ = engine(tmp_path)
    knock(client(app), "null", 3)
    settle()
    said = notes_of(sup)
    assert len(said) == 1 and "null" not in said[0]
    assert "tried at least 3 times to control Nightshift (from a page opened from a file or an embedded frame, by" in said[0]


# ============================================== round 2: the bound on notes holds for the install, not a process
def other_note(sup: Supervisor, i: int) -> None:
    notify.leave_note(sup.paths, "Nightshift", f"NS-W209: the nightly run could not start ({i})")


def read_all(sup: Supervisor) -> None:
    """The owner presses Mark all read: every note the bell showed goes."""
    notify.dismiss(sup.paths, [n["id"] for n in notify.read_notes(sup.paths)])


def waiting_but_the_count(sup: Supervisor) -> list[dict[str, Any]]:
    """The notes file as stored, without the owner's one note that counts removed or kept-out notes."""
    stored = json.loads(notify.note_path(sup.paths).read_text(encoding="utf-8"))["notes"]
    return [n for n in stored if "removed" not in n]


def removed_count(sup: Supervisor) -> int:
    stored = json.loads(notify.note_path(sup.paths).read_text(encoding="utf-8"))["notes"]
    return sum(int(n["removed"]) for n in stored if "removed" in n)


def kinds_of(sup: Supervisor) -> tuple[int, int]:
    said = notes_of(sup)
    return len([m for m in said if m.startswith("NS-E207:")]), len([m for m in said if m.startswith("NS-W209:")])


def test_the_notes_of_a_day_are_bounded_across_servers_and_restarts(tmp_path: Path, clock: Clock):
    """A tab left open cycles sub-domains while the engine restarts and a second server refuses too: the
    notes file holds three notes that name a page and one that says there were more, whoever wrote them."""
    sup, first, _ = engine(tmp_path)
    second = engine_app(sup, run_monitor=False)                  # another server of the same install
    for i in range(2):
        knock(client(first), f"https://a{i}.evil.example", 3)
        settle()   # the writers run on threads of their own: one page at a time keeps the order asserted below
    for i in range(2):
        knock(client(second), f"https://b{i}.evil.example", 3)
        settle()
    assert kinds_of(sup) == (4, 0)
    for restart in range(4):                                     # each start counts from zero in memory
        again = engine_app(sup, run_monitor=False)
        for i in range(5):
            knock(client(again), f"https://r{restart}-{i}.evil.example", 3)
        knock(client(again), "https://a0.evil.example", 3)       # and a page already named today
        settle()
    said = notes_of(sup)
    assert len(said) == 4, said
    assert [("https://a0." in said[0]), ("https://a1." in said[1]), ("https://b0." in said[2])] == [True] * 3
    assert said[3].startswith("NS-E207: Still more web pages or names tried to control Nightshift on ")
    # the owner read them (the bell clears the file): the same day gets at most the same four again
    read_all(sup)
    for i in range(6):
        knock(client(first), f"https://c{i}.evil.example", 3)
    settle()
    assert len(notes_of(sup)) <= 4


def test_a_refusal_note_never_pushes_another_note_out(tmp_path: Path, clock: Clock):
    """Unread for days, refusal notes take at most 8 places; and a file full of the night's own notes (the
    owner's bound, `notify.NOTES_FULL`) is not touched by a refusal note, which is routine there."""
    assert loopback is not None and not hasattr(loopback, "NOTES_KEPT"), "the bound is the notes' owner's"
    assert notify.is_routine("NS-E207: A web page open in your browser tried at least 3 times to control Nightshift")
    sup, app, _ = engine(tmp_path)
    for i in range(6):
        other_note(sup, i)
    for day in range(5):                                         # a long weekend, nobody reads the notes
        for i in range(6):
            knock(client(app), f"https://d{day}-{i}.evil.example", 3)
        settle()
        clock.step(days=1)
    assert kinds_of(sup) == (8, 6)
    held = log_rows(sup.paths.logs, "morning_note_held")
    assert held and held[0]["level"] == "WARN" and "already wait unread" in held[0]["msg"]
    # a file that is full of the night's notes: no refusal note goes in, and none of them goes out
    read_all(sup)
    for i in range(notify.NOTES_FULL):
        other_note(sup, i)
    before = waiting_but_the_count(sup)
    for i in range(6):
        knock(client(app), f"https://e{i}.evil.example", 3)
    settle()
    # every note of the night stays as it was; the refusal notes kept out are only counted by the owner's one
    # count note (`notify._add`, block D's FN-2 review: "removed or kept out"), never added
    assert waiting_but_the_count(sup) == before and kinds_of(sup) == (0, notify.NOTES_FULL)
    assert removed_count(sup) >= 1
    full = [r for r in log_rows(sup.paths.logs, "morning_note_held") if "not routine" in r["msg"]]
    assert full and full[0]["data"]["code"] == CODE
    clock.step(minutes=11)
    for i in range(6):
        knock(client(app), f"https://e{i}.evil.example", 3)
    settle()
    assert waiting_but_the_count(sup) == before
    # the owner reads them: the page that still tries is told then (the note stayed owed)
    read_all(sup)
    clock.step(minutes=11)
    knock(client(app), "https://e1.evil.example")
    settle()
    assert kinds_of(sup) == (1, 0) and "https://e1.evil.example" in notes_of(sup)[0]


#: One server of the install in a process of its own: a guard that tells through the notes' owner, with the
#: step between its count and its write slowed (0.2 s) so the other process meets it there.
REFUSING_PROCESS = r"""
import sys, time
from datetime import datetime
from pathlib import Path
from ns_common import loopback
from ns_common.paths import Paths
from ns_supervisor import notify

home, go, tag = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
paths = Paths(home)

def tell(choose, at):
    def slow(waits):
        got = choose(waits)
        time.sleep(0.2)
        return got
    return notify.leave_chosen_note(paths, "Nightshift", slow, now=at)

guard = loopback.LoopbackGuard(None, rule=loopback.NO_BROWSER, tell=tell, clock=lambda: datetime(2026, 11, 10, 9, 0, 0))
pages = [f"https://{tag}{i}.evil.example" for i in range(6)]
while not go.exists():
    time.sleep(0.005)
for page in pages:
    headers = [(b"host", b"127.0.0.1:8099"), (b"origin", page.encode())]
    refusal = loopback.decide(headers, loopback.NO_BROWSER, "POST", "/stop_all")
    for _ in range(3):
        guard.record(refusal, "POST", "/stop_all")
assert loopback.settle(120)
"""


def test_two_processes_refusing_at_once_stay_within_the_days_cap(tmp_path: Path):
    """The engine and the dashboard are two processes: each counts today's NS-E207 notes and writes in one
    step under the notes lock, so together they leave three that name a page and one "more", not more."""
    home = tmp_path / "home"
    go = tmp_path / "go"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")])}
    procs = [
        subprocess.Popen(  # noqa: S603
            [sys.executable, "-c", REFUSING_PROCESS, str(home), str(go), tag], cwd=str(ROOT), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for tag in ("a", "b")
    ]
    try:
        go.write_text("go", encoding="ascii")
        outs = [p.communicate(timeout=180) for p in procs]
        assert [p.returncode for p in procs] == [0, 0], outs
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.communicate(timeout=30)
    said = [n["message"] for n in notify.read_notes(Paths(home)) if n["message"].startswith(f"{CODE}:")]
    assert len(said) == 4, said
    assert sum(1 for m in said if "Still more web pages" in m) == 1 and said[-1].startswith(
        "NS-E207: Still more web pages or names tried to control Nightshift on ")


# ======================================================== round 2: a new server cannot forget the check
def test_every_http_app_of_ours_is_wrapped_by_the_check():
    """Every module under ns_* that builds a FastAPI or Starlette app adds the guard to it, and every module
    that serves HTTP another way is on the list below with its reason."""
    import ast

    guards = {"LoopbackGuard", "LoopbackMiddleware"}
    #: HTTP servers that are not ASGI apps. Both are stand-ins for third-party model servers (llama-server and
    #: friends, which have no such check either) and exist only on fake-services installs and in tests.
    not_asgi = {"ns_supervisor/devfake.py", "ns_dashboard/fakes.py"}
    apps: dict[str, int] = {}
    raw_servers: set[str] = set()
    for file in sorted(ROOT.glob("ns_*/**/*.py")):
        rel = file.relative_to(ROOT).as_posix()
        if "/ui/" in rel or "node_modules" in rel:
            continue
        text = file.read_text(encoding="utf-8")
        if not any(word in text for word in ("FastAPI", "Starlette", "HTTPServer", "uvicorn", "socketserver")):
            continue
        tree = ast.parse(text)
        made = guarded = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name in ("FastAPI", "Starlette"):
                made += 1
            elif name in ("HTTPServer", "ThreadingHTTPServer", "TCPServer", "ThreadingTCPServer"):
                raw_servers.add(rel)
            elif name == "add_middleware" and node.args and getattr(node.args[0], "id", None) in guards:
                guarded += 1
        if made:
            apps[rel] = made
            assert guarded == made, f"{rel} builds {made} HTTP app(s) and adds the loopback check to {guarded}"
    assert set(apps) == {"ns_supervisor/app.py", "ns_tts/server.py", "ns_dashboard/server.py"}, apps
    assert raw_servers <= not_asgi, f"an HTTP server without the loopback check: {sorted(raw_servers - not_asgi)}"
    # and the check is what answers: each real app refuses a foreign Host before any route (the outermost layer)
    from ns_dashboard.server import LoopbackMiddleware

    assert loopback is not None and issubclass(LoopbackMiddleware, loopback.LoopbackGuard)


# ================================================== D-50 (block D reproof @ b8a2be4): what the note says, and when
#: A note's claim about how often a page tried: (the words before the number, "at least ", the number).
TRIED = r"(tried|sent Nightshift a web form|was called) (at least )?(\d+) times"


def claim_is_true(text: str, refused_by_now: int) -> str | None:
    """None when the note's count is true whenever it is read: "at least N" with N no more than the page's
    refusals so far, and every one of those requests refused (a count said as exact goes stale)."""
    import re

    m = re.search(TRIED, text)
    if m is None:
        return f"no count in {text!r}"
    if not m.group(2):
        return f"an exact count ({m.group(3)}) that stays in the note while the page goes on: {text!r}"
    if int(m.group(3)) > refused_by_now:
        return f"{m.group(3)} is more than the {refused_by_now} refused so far: {text!r}"
    if "Nightshift refused every one of those requests, so none of them ran." not in text:
        return f"it does not say every one was refused: {text!r}"
    return None


def test_d50_the_note_is_true_after_3_18_and_36_refusals_on_two_servers_for_two_pages_and_a_new_day(
    tmp_path: Path, clock: Clock
):
    """The reproof's pages: one on 127.0.0.1:51731 (18 refusals, then 18 more on the other server: 36) and one
    on localhost:51731 (18), trying for hours. The note is written once; read after 3, 18 and 36 refusals and
    the next day, it is true each time."""
    sup, first, _ = engine(tmp_path)
    second = engine_app(sup, run_monitor=False)              # the install's other server (the dashboard's place)
    a, b = "http://127.0.0.1:51731", "http://localhost:51731"
    sent = {a: 0, b: 0}
    wrong: list[str] = []

    def knock_page(app: Any, page: str, times: int, apart_s: float) -> None:
        knock(client(app), page, times, clock=clock, apart_s=apart_s)
        sent[page] += times
        settle()

    def read(when: str) -> list[str]:
        said = notes_of(sup)
        for text in said:
            page = a if f"(from {a}," in text else b if f"(from {b}," in text else None
            if page is None:
                wrong.append(f"{when}: a note about no page of this test: {text!r}")
            elif (bad := claim_is_true(text, sent[page])) is not None:
                wrong.append(f"{when}: {bad}")
        return said

    knock_page(first, a, 3, apart_s=60)                       # 09:00 to 09:02: the page keeps trying
    assert len(read("after 3")) == 1
    knock_page(first, a, 15, apart_s=600)                     # every ten minutes for two and a half hours
    assert len(read("after 18")) == 1
    knock_page(second, a, 18, apart_s=60)                     # the same page, the other server
    knock_page(second, b, 18, apart_s=30)                     # a second page
    said = read("after 36 and a second page")
    assert len(said) == 2 and sum(f"(from {b}," in t for t in said) == 1, said
    clock.step(days=1)                                       # the next morning the tab is still open
    sent[a] = 0                                              # (the new day's note counts the new day)
    knock_page(first, a, 5, apart_s=60)
    said = read("the next day")
    assert len(said) == 3 and notify.read_notes(sup.paths)[2]["at"].startswith("2026-11-11"), said
    assert wrong == [], "\n".join(wrong)


def test_d50_a_browser_request_with_no_address_is_never_named_by_nightshifts_own(
    tmp_path: Path, tmp_home: Path, clock: Clock
):
    """A file page's image, script or no-cors fetch: Sec-Fetch-* and neither Origin nor Referer. The note says
    the browser gave no address; it never names the address the request called (ours), under either of our
    names: one page that cannot be told apart, one note (written at its third refusal)."""
    image = {"sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "image"}
    sup, eng, _ = engine(tmp_path)
    for server, path, notes_at in ((eng, "/status", sup.paths), (dashboard(tmp_home), "/api/status", Paths.from_env())):
        for host in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            for _ in range(3):
                assert client(server, host).get(path, headers=image).status_code == 403
        settle()
        said = [n["message"] for n in notify.read_notes(notes_at)]
        assert said == [
            "NS-E207: A web page whose address the browser did not give tried at least 3 times to control Nightshift "
            "(by 09:00). Nightshift refused every one of those requests, so none of them ran. If you do not know which "
            "page that is, close the web pages you have open."
        ], said
        assert not [t for t in said if "127.0.0.1" in t or "localhost" in t or str(PORT) in t], said


def test_d50_a_post_without_a_body_is_a_programs_whatever_its_content_type_on_every_server(
    tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """Windows PowerShell 5.1's `Invoke-RestMethod -Method Post` without `-Body` states a urlencoded form with
    Content-Length 0. With no browser header it is answered by the engine, the voice service and the
    dashboard; the same request from a page (an Origin of another local port) is still refused."""
    from ns_dashboard.svc_engine import EngineService

    _sup, eng, spy = engine(tmp_path, monkeypatch)
    assert spy is not None
    stopped: list[str] = []
    monkeypatch.setattr(EngineService, "stop_all", lambda _self: stopped.append("stop_all") or {"ok": True})
    servers = ((eng, "/stop_all"), (voice(tmp_path), "/v1/audio/speech/cancel"), (dashboard(tmp_home), "/api/engine/stop_all"))
    kinds = ("application/x-www-form-urlencoded", "text/plain", "multipart/form-data; boundary=x")
    for kind in kinds:
        empty = {"content-type": kind, "content-length": "0"}
        for app, path in servers:
            tc = client(app)
            r = tc.post(path, content=b"", headers=empty)
            assert r.status_code == 200, (path, kind, r.status_code, r.text)
            page = tc.post(path, content=b"", headers={**empty, "origin": "http://127.0.0.1:51731"})
            assert page.status_code == 403 and CODE in page.text, (path, kind, page.status_code)
            form = tc.post(path, content=b"a=1", headers={"content-type": kind})   # a body: a form, as before
            assert form.status_code == 403 and CODE in form.text, (path, kind, form.status_code)
    assert spy.calls == ["stop_all"] * len(kinds) and stopped == ["stop_all"] * len(kinds)


@needs_powershell
def test_d50_powershell_5_1_posts_without_a_body_and_is_answered(live_engine: tuple[Live, Spy]):
    """The real caller: powershell.exe's Invoke-RestMethod -Method Post with no -Body, against a real server."""
    live, spy = live_engine
    script = (
        "$u = 'http://127.0.0.1:' + $env:NS_T_PORT + '/stop_all'\n"
        "try { $null = Invoke-RestMethod -Method Post -Uri $u -TimeoutSec 20; Write-Output 'answered' } "
        "catch { Write-Output ([int]$_.Exception.Response.StatusCode) }\n"
        "try { $null = Invoke-RestMethod -Method Post -Uri $u -Headers @{ Origin = 'http://127.0.0.1:51731' } "
        "-TimeoutSec 20; Write-Output 'answered' } catch { Write-Output ([int]$_.Exception.Response.StatusCode) }\n"
    )
    out = pwsh.run_powershell(script, env_extra={"NS_T_PORT": str(live.port)}, timeout_s=120)
    assert out.stdout.split() == ["answered", "403"], out.stdout + out.stderr
    assert spy.calls == ["stop_all"]
    posts = [h for h in live.seen if h.get("content-length") == "0"]
    assert posts and "form-urlencoded" in posts[0].get("content-type", ""), live.seen   # what 5.1 really sends


# ============================ old-browser-bodyless: a browser that says nothing at all cannot change anything
IE_MODE = "Mozilla/5.0 (Windows NT 10.0; WOW64; Trident/7.0; rv:11.0) like Gecko"
FIREFOX_68 = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:68.0) Gecko/20100101 Firefox/68.0"
FIREFOX_89 = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0"
OLD_BROWSERS = (IE_MODE, FIREFOX_68, FIREFOX_89)
#: What our own callers and an owner's scripts send (the first two begin with "Mozilla/5.0" as browsers do).
PROGRAM_AGENTS = (
    "Mozilla/5.0 (Windows NT; Windows NT 10.0; en-US) WindowsPowerShell/5.1.26100.1",
    "Mozilla/5.0 (Windows NT 10.0; Microsoft Windows 10.0.26100; en-US) PowerShell/7.4.6",
    "curl/8.9.1",
    "python-httpx/0.28.1",
    "Python-urllib/3.12",
    None,
)
EMPTY_FORM = {"content-type": "application/x-www-form-urlencoded", "content-length": "0"}
OLD_BROWSER_NOTE = (
    "NS-E207: An old web browser (Internet Explorer mode in Edge, or a Firefox older than version 90) tried at "
    "least 3 times to control Nightshift (by 09:00). Nightshift refused every one of those requests, so none of "
    "them ran. Close the pages open in that browser; open Nightshift in Edge or Chrome."
)


def as_agent(app: Any, agent: str | None) -> TestClient:
    """A client that sends exactly this User-Agent (None: no User-Agent header at all) and no browser header."""
    tc = client(app)
    if agent is None:
        tc.headers.pop("user-agent", None)
    else:
        tc.headers["user-agent"] = agent
    return tc


def three_servers(
    tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[tuple[tuple[Any, str], ...], Spy, list[str]]:
    """The engine, the voice service and the dashboard with one route each that acts on an empty body, and
    what a request reached: the engine's spied calls and the dashboard's `stop_all`."""
    from ns_dashboard.svc_engine import EngineService

    _sup, eng, spy = engine(tmp_path, monkeypatch)
    assert spy is not None
    stopped: list[str] = []
    monkeypatch.setattr(EngineService, "stop_all", lambda _self: stopped.append("stop_all") or {"ok": True})
    servers = (
        (eng, "/stop_all"),
        (voice(tmp_path), "/v1/audio/speech/cancel"),
        (dashboard(tmp_home), "/api/engine/stop_all"),
    )
    return servers, spy, stopped


def old_browser_bodies() -> tuple[tuple[str, dict[str, str], bytes | None], ...]:
    """(name, headers, body): no body at all; IE mode's empty HTML form from an HTTPS page (a urlencoded
    content type with Content-Length 0, no Origin, no Referer); a form with a body; a JSON body."""
    return (
        ("none", {}, None),
        ("empty form", dict(EMPTY_FORM), b""),
        ("form", {"content-type": "application/x-www-form-urlencoded"}, b"a=1"),
        ("json", {"content-type": "application/json"}, b"{}"),
    )


@pytest.mark.parametrize("agent", OLD_BROWSERS)
def test_an_old_browser_cannot_post_without_sec_fetch_on_any_server(
    tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch, agent: str
):
    """Edge's Internet Explorer mode and a Firefox older than 90 send no Sec-Fetch-*; from an HTTPS page, or
    with no-referrer, they name no page either. Such a POST, PUT or DELETE reaches no route of any server,
    whatever its body: also with none, which is all `/stop_all` needs."""
    servers, spy, stopped = three_servers(tmp_path, tmp_home, monkeypatch)
    for app, path in servers:
        tc = as_agent(app, agent)
        for method in ("POST", "PUT", "DELETE"):
            for name, headers, body in old_browser_bodies():
                r = tc.request(method, path, headers=headers, content=body)
                where = (path, method, name, r.status_code, r.text[:200])
                assert r.status_code == 403 and CODE in r.text, where
                if path.startswith("/api/"):
                    assert r.json()["error"]["code"] == CODE, where
                else:  # the engine's and the voice service's flat body says why
                    assert refused(r, "old-browser") is None, where
    assert spy.calls == [] and stopped == [], (spy.calls, stopped)


@pytest.mark.parametrize("agent", PROGRAM_AGENTS)
def test_programs_with_mozilla_in_their_user_agent_are_still_answered(
    tmp_path: Path, tmp_home: Path, monkeypatch: pytest.MonkeyPatch, agent: str | None
):
    """Windows PowerShell 5.1 and PowerShell 7 begin their User-Agent with "Mozilla/5.0": the mark of an old
    browser is `Trident/` or `Firefox/`, never that. Every program's request without a body is answered by
    every server, and one with a JSON body is not refused by the check."""
    servers, spy, stopped = three_servers(tmp_path, tmp_home, monkeypatch)
    for app, path in servers:
        tc = as_agent(app, agent)
        r = tc.post(path)
        assert r.status_code == 200, (path, agent, r.status_code, r.text[:200])
        r = tc.post(path, content=b"", headers=dict(EMPTY_FORM))
        assert r.status_code == 200, (path, agent, "empty form", r.status_code, r.text[:200])
        r = tc.post(path, json={})
        assert r.status_code != 403, (path, agent, "json", r.status_code, r.text[:200])
    assert len(spy.calls) >= 2 and set(spy.calls) == {"stop_all"}, spy.calls
    assert len(stopped) >= 2 and set(stopped) == {"stop_all"}, stopped


@pytest.mark.parametrize("agent", OLD_BROWSERS)
def test_an_old_browser_may_still_read(tmp_path: Path, tmp_home: Path, agent: str):
    """A GET or HEAD from such a browser is answered as before: the GET routes only read, another origin's
    page cannot read the answer, and the owner's own page opened in that browser still loads."""
    _sup, eng, _spy = engine(tmp_path)
    assert as_agent(eng, agent).get("/status").status_code == 200
    assert as_agent(eng, agent).head("/status").status_code != 403
    dash = as_agent(dashboard(tmp_home), agent)
    assert dash.get("/api/status").status_code == 200
    assert dash.get("/").status_code == 200


def test_the_dashboards_own_page_in_an_old_firefox_is_answered(tmp_path: Path, tmp_home: Path):
    """The dashboard's own page in a Firefox that sends no Sec-Fetch-*: its requests name the page (its own
    Origin, or its own Referer where that Firefox sends no Origin on a same-origin POST), and a page cannot
    forge either. A foreign Origin or Referer is refused as ever; the engine has no page of its own."""
    firefox_85 = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:85.0) Gecko/20100101 Firefox/85.0"
    dash = as_agent(dashboard(tmp_home), firefox_85)
    assert dash.post("/api/jobs/verify", json={}, headers={"origin": BASE}).status_code == 201
    assert dash.post("/api/jobs/verify", json={}, headers={"referer": f"{BASE}/queue"}).status_code == 201
    for foreign in ({"origin": EVIL}, {"origin": "http://127.0.0.1:51731"}, {"referer": f"{EVIL}/a"}):
        r = dash.post("/api/jobs/verify", json={}, headers=foreign)
        assert r.status_code == 403 and r.json()["error"]["code"] == CODE, (foreign, r.status_code, r.text[:200])
    r = dash.post("/api/jobs/verify", json={})  # the same browser naming no page at all
    assert r.status_code == 403 and r.json()["error"]["code"] == CODE, (r.status_code, r.text[:200])
    _sup, eng, _spy = engine(tmp_path)
    assert refused(as_agent(eng, firefox_85).post("/stop_all", headers={"origin": BASE}), "origin") is None
    assert refused(as_agent(eng, firefox_85).post("/stop_all", headers={"referer": f"{BASE}/"}), "referer") is None


def test_three_old_browser_posts_are_told_in_words_that_are_true(tmp_path: Path, clock: Clock):
    """One note a day for every such request, whichever old browser and page it was (it named none): the
    count is "at least", the words say what the browser is and what to do."""
    sup, app, _ = engine(tmp_path)
    for _ in range(3):
        r = as_agent(app, IE_MODE).post("/stop_all", content=b"", headers=dict(EMPTY_FORM))
        assert refused(r, "old-browser") is None
    settle()
    assert notes_of(sup) == [OLD_BROWSER_NOTE], notes_of(sup)
    assert claim_is_true(OLD_BROWSER_NOTE, 3) is None
    for _ in range(4):  # another old browser, the same day: the same note stands
        clock.step(seconds=30)
        assert refused(as_agent(app, FIREFOX_68).post("/stop_all"), "old-browser") is None
    settle()
    assert notes_of(sup) == [OLD_BROWSER_NOTE], notes_of(sup)
    rows = log_rows(sup.paths.logs, "request_refused")
    assert len(rows) == 1 and rows[0]["data"]["why"] == "old-browser" and rows[0]["data"]["origin"] is None, rows


def test_the_old_browser_rule_reads_only_what_a_page_cannot_change():
    """`decide` alone, header by header: the rule needs an unsafe method, no Sec-Fetch-*, no Origin, no Referer
    and a User-Agent naming Trident/ or Firefox/ (case as browsers send it; any of several values)."""
    assert loopback is not None
    host = (b"host", f"127.0.0.1:{PORT}".encode())

    def judged(method: str, *headers: tuple[bytes, bytes], rule: Any = None) -> str | None:
        r = loopback.decide([host, *headers], rule or loopback.NO_BROWSER, method, "/stop_all", "http")
        return None if r is None else r.why

    ie, fx = (b"user-agent", IE_MODE.encode()), (b"user-agent", FIREFOX_89.encode())
    ps = (b"user-agent", b"Mozilla/5.0 (Windows NT; Windows NT 10.0; en-US) WindowsPowerShell/5.1.26100.1")
    for old in (ie, fx):
        for method in ("POST", "PUT", "PATCH", "DELETE", "post"):
            assert judged(method, old) == "old-browser", (method, old)
        assert judged("GET", old) is None and judged("HEAD", old) is None
        assert judged("POST", ps, old) == "old-browser" and judged("POST", old, ps) == "old-browser"
        # a browser that DOES say which page is judged by that, as before
        assert judged("POST", old, (b"sec-fetch-site", b"cross-site")) == "fetch-mark"
        assert judged("POST", old, (b"origin", EVIL.encode())) == "origin"
        assert judged("POST", old, (b"referer", f"{EVIL}/a".encode())) == "referer"
        own = (b"origin", BASE.encode())
        assert judged("POST", old, own, (b"content-type", b"application/json"), rule=loopback.OWN_PAGE) is None
    assert judged("POST", ps) is None and judged("POST") is None
    assert judged("POST", (b"user-agent", b"my-tool trident/7.0 firefox/1")) is None  # not the browsers' case
    refusal = loopback.decide([host, ie], loopback.NO_BROWSER, "POST", "/stop_all", "http")
    assert refusal is not None and refusal.browser and refusal.origin is None
    assert refusal.page == loopback.OLD_BROWSER


def test_the_hint_names_the_user_agent_rule():
    """A script of the owner that fakes a browser's User-Agent is refused too: the hint says what to change."""
    hint = describe(CODE).hint or ""
    assert "Trident or Firefox" in hint and "User-Agent" in hint, hint
