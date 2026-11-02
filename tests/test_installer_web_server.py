"""C1: the setup server over real HTTP on 127.0.0.1 (a stub controller; no window, no install)."""

import http.client
import json
import threading
import time
from pathlib import Path

import pytest

from installer.stage2.web.server import CSP, SetupServer
from installer.stage2.web.store import SetupStore


class StubController:
    """The few methods the HTTP layer calls."""

    version = "9.9.9"

    def __init__(self, wav: Path) -> None:
        self.store = SetupStore({"rev": 1, "phase": "checks"}, None)
        self.shutdown = threading.Event()
        self.wav = wav
        self.actions: list[tuple[str, dict]] = []

    def wait_state(self, since, timeout=15.0):
        return self.store.wait_newer(since, timeout)

    def set_ui_step(self, step):
        return 200, {"ok": True, "message": step, "state": self.store.snapshot()}

    def submit_answers(self, draft):
        return {"ok": True, "echo": draft}

    def check_key(self, service, key):
        return {"ok": key == "good", "code": None, "message": service}

    def check_model(self, repo, file):
        return {"ok": bool(repo and file), "size": 9, "message": f"{repo}|{file}", "code": None}

    def disk(self, path, tier, source):
        return {"path": path, "tier": tier, "source": source}

    def do_action(self, name, body=None):
        self.actions.append((name, body or {}))
        if name == "cancel":
            return 409, {"ok": False, "message": "no"}
        return 200, {"ok": True, "message": name}

    def file_path(self, name):
        return self.wav if name == "voice-sample.wav" else None


class Client:
    def __init__(self, server: SetupServer) -> None:
        self.server = server
        self.cookie: str | None = None

    def call(self, method, path, body=None, headers=None, host=None, cookie=True, raw_length=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        hdrs = {"Host": host or f"127.0.0.1:{self.server.port}"}
        if cookie and self.cookie:
            hdrs["Cookie"] = f"ns_setup={self.cookie}"
        hdrs.update(headers or {})
        payload = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        if raw_length is not None:
            hdrs["Content-Length"] = str(raw_length)
            hdrs.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        out = (resp.status, {k.lower(): v for k, v in resp.getheaders()}, data)
        conn.close()
        return out

    def login(self):
        status, headers, _ = self.call("GET", f"/?t={self.server.token}", cookie=False)
        assert status == 302
        self.cookie = headers["set-cookie"].split(";")[0].split("=", 1)[1]


@pytest.fixture
def env(tmp_path: Path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>setup</title>", encoding="utf-8")
    (static / "app.js").write_text("console.log(1)", encoding="utf-8")
    (static / "app.css").write_text("body{}", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("top secret", encoding="utf-8")
    wav = tmp_path / "voice.wav"
    wav.write_bytes(b"RIFF0123456789")
    ctl = StubController(wav)
    opened = threading.Event()
    server = SetupServer(ctl, static_dir=static, version="9.9.9", long_poll_s=0.6, on_open_window=opened.set)
    server.start()
    client = Client(server)
    yield ctl, server, client, opened
    server.stop()


def test_api_needs_the_cookie_and_the_host_must_match(env):
    ctl, server, client, _ = env
    assert client.call("GET", "/api/ping")[0] == 401
    assert client.call("GET", "/api/ping", host="evil.example")[0] == 421
    assert client.call("GET", "/", host=f"localhost:{server.port}")[0] == 421
    assert client.call("GET", "/api/ping", host="127.0.0.1:1")[0] == 421


def test_the_token_becomes_an_http_only_same_site_cookie(env):
    _, server, client, _ = env
    assert client.call("GET", "/?t=wrong", cookie=False)[0] == 403
    status, headers, _ = client.call("GET", f"/?t={server.token}", cookie=False)
    assert status == 302 and headers["location"] == "/"
    cookie = headers["set-cookie"]
    assert cookie.startswith(f"ns_setup={server.token}") and "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie and "Path=/" in cookie
    client.login()
    status, _, body = client.call("GET", "/api/ping")
    assert status == 200
    data = json.loads(body)
    assert data["ok"] is True and data["version"] == "9.9.9" and isinstance(data["pid"], int)
    assert client.call("GET", "/api/ping", headers={"Cookie": "ns_setup=nope"}, cookie=False)[0] == 401


def test_static_files_carry_the_csp_and_index_is_not_cached(env):
    _, _, client, _ = env
    status, headers, body = client.call("GET", "/")
    assert status == 200 and b"<title>setup</title>" in body
    assert headers["content-security-policy"] == CSP
    assert "script-src 'self'" in CSP and "default-src 'self'" in CSP
    assert headers["cache-control"] == "no-store"
    status, headers, _ = client.call("GET", "/app.js")
    assert status == 200 and headers["content-type"].startswith("text/javascript")
    assert client.call("GET", "/app.css")[1]["content-type"].startswith("text/css")
    assert client.call("GET", "/missing.js")[0] == 404
    assert "access-control-allow-origin" not in headers                      # no CORS at all


def test_static_files_cannot_escape_the_folder(env):
    _, _, client, _ = env
    for path in ("/../secret.txt", "/..%2Fsecret.txt", "/assets/../../secret.txt"):
        status, _, body = client.call("GET", path)
        assert status == 404 and b"top secret" not in body, path


def test_post_bodies_must_be_small_json(env):
    _, _, client, _ = env
    client.login()
    assert client.call("POST", "/api/answers", headers={"Content-Type": "text/plain"})[0] == 415
    assert client.call("POST", "/api/answers", raw_length=70_000)[0] == 413
    assert client.call("POST", "/api/answers", body={"channel": {"displayName": "x"}})[0] == 200


def test_api_routes_reach_the_controller(env):
    ctl, _, client, _ = env
    client.login()
    status, _, body = client.call("POST", "/api/ui-step", {"uiStep": "choices"})
    assert status == 200 and json.loads(body)["message"] == "choices"
    assert client.call("POST", "/api/ui-step", {"uiStep": "nowhere"})[0] == 400
    assert json.loads(client.call("POST", "/api/check-key", {"service": "pexels", "key": "good"})[2])["ok"]
    assert not json.loads(client.call("POST", "/api/check-key", {"service": "pexels", "key": "bad"})[2])["ok"]
    disk = json.loads(client.call("GET", "/api/disk?path=D%3A%5CNightshift&tier=best&source=1")[2])
    assert disk == {"path": "D:\\Nightshift", "tier": "best", "source": True}
    query = "repo=Qwen%2FQwen3-14B-GGUF&file=Qwen3-14B-Q4_K_M.gguf"
    checked = json.loads(client.call("GET", f"/api/model-check?{query}")[2])
    assert checked["ok"] is True and checked["message"] == "Qwen/Qwen3-14B-GGUF|Qwen3-14B-Q4_K_M.gguf"
    assert json.loads(client.call("GET", "/api/model-check")[2])["ok"] is False        # nothing named yet
    status, _, body = client.call("POST", "/api/action/pause", {"path": "x"})
    assert status == 200 and ctl.actions[-1] == ("pause", {"path": "x"})
    assert client.call("POST", "/api/action/cancel", {})[0] == 409                    # a refused precondition
    assert client.call("POST", "/api/action/format-disk", {})[0] == 404
    assert client.call("GET", "/api/nothing")[0] == 404
    assert client.call("GET", "/api/uninstall")[0] == 404                             # not in uninstall mode


def test_state_long_poll_returns_on_a_change_and_after_its_timeout(env):
    ctl, _, client, _ = env
    client.login()
    status, _, body = client.call("GET", "/api/state")
    assert status == 200 and json.loads(body)["rev"] == 1                             # no cursor: at once
    t0 = time.monotonic()
    assert json.loads(client.call("GET", "/api/state?since=1")[2])["rev"] == 1    # unchanged: the timeout
    assert 0.4 <= time.monotonic() - t0 < 5
    threading.Timer(0.2, lambda: ctl.store.update(lambda s: s.__setitem__("phase", "choices"))).start()
    t0 = time.monotonic()
    data = json.loads(client.call("GET", "/api/state?since=1")[2])
    assert data["rev"] == 2 and data["phase"] == "choices" and time.monotonic() - t0 < 0.55


def test_only_allow_listed_files_are_served_and_ranges_work(env):
    _, _, client, _ = env
    client.login()
    assert client.call("GET", "/api/file/secret.txt")[0] == 404
    assert client.call("GET", "/api/file/..%2Fsecret.txt")[0] == 404
    assert client.call("GET", "/api/file/render-test.mp4")[0] == 404              # allowed name, no file
    status, headers, body = client.call("GET", "/api/file/voice-sample.wav")
    assert status == 200 and body == b"RIFF0123456789" and headers["accept-ranges"] == "bytes"
    status, headers, body = client.call("GET", "/api/file/voice-sample.wav", headers={"Range": "bytes=0-3"})
    assert status == 206 and body == b"RIFF" and headers["content-range"] == "bytes 0-3/14"
    status, _, body = client.call("GET", "/api/file/voice-sample.wav", headers={"Range": "bytes=-4"})
    assert status == 206 and body == b"6789"
    assert client.call("GET", "/api/file/voice-sample.wav", headers={"Range": "bytes=99-"})[0] == 416


def test_a_second_install_cmd_can_reopen_the_window_with_the_token_only(env):
    _, server, client, opened = env
    assert client.call("POST", "/api/internal/open-window", {}, cookie=False)[0] == 401
    wrong = {"X-NS-Token": "wrong"}
    assert client.call("POST", "/api/internal/open-window", {}, headers=wrong, cookie=False)[0] == 401
    status, _, _ = client.call("POST", "/api/internal/open-window", {}, headers={"X-NS-Token": server.token},
                               cookie=False)
    assert status == 200 and opened.wait(5)
    assert client.call("POST", "/api/internal/open-window", {}, headers={"X-NS-Token": server.token},
                       host="evil.example", cookie=False)[0] == 421


def test_uninstall_mode_serves_its_own_endpoints(tmp_path: Path):
    class Uninstall:
        shutdown = threading.Event()

        def view(self):
            return {"home": "D:\\Nightshift", "running": False}

        def run(self, models, data):
            return 200, {"ok": True, "echo": [models, data]}

        def action(self, name, body):
            return 200, {"ok": True, "message": name}

    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text("x", encoding="utf-8")
    server = SetupServer(None, static_dir=static, uninstall=Uninstall())
    server.start()
    try:
        client = Client(server)
        client.login()
        assert json.loads(client.call("GET", "/api/uninstall")[2])["home"] == "D:\\Nightshift"
        post = client.call("POST", "/api/uninstall", {"deleteModels": True, "deleteData": False})
        assert json.loads(post[2])["echo"] == [True, False]
        assert json.loads(client.call("POST", "/api/action/close-setup", {})[2])["message"] == "close-setup"
        assert client.call("GET", "/api/state")[0] == 404                       # no setup state in this mode
    finally:
        server.stop()
