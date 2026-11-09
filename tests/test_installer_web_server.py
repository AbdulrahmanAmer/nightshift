"""C1: the setup server over real HTTP on 127.0.0.1 (a stub controller; no window, no install)."""

import collections
import http.client
import json
import socket
import threading
import time
from pathlib import Path

import pytest

from installer.stage2.web import server as server_module
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

    def disk(self, path, tier, source, custom=None, draft=None):
        return {"path": path, "tier": tier, "source": source, "custom": custom, "draft": draft}

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
    assert disk == {"path": "D:\\Nightshift", "tier": "best", "source": True, "custom": None, "draft": {}}
    # the draft's toggles that change the number reach the controller too (Install checks the draft)
    named = "path=D%3A%5CN&tier=small&code=0&voicedesign=1&msKind=folder&msPath=E%3A%5CModels"
    seen = json.loads(client.call("GET", f"/api/disk?{named}")[2])["draft"]
    assert seen == {"installCode": False, "includeVoicedesign": True,
                    "modelsSource": {"kind": "folder", "path": "E:\\Models"}}
    unusable = "/api/disk?path=D%3A%5CN&tier=small&code=x&voicedesign=2"
    assert json.loads(client.call("GET", unusable)[2])["draft"] == {}
    # a custom model in the query reaches the controller as one (the setup window sends it, so the number the
    # picker and the folder error show is the server's, not a rule of the page's own)
    named = "path=D%3A%5CNightshift&tier=small&repo=a%2Fb&file=tiny.gguf&size=1000000000"
    custom = json.loads(client.call("GET", f"/api/disk?{named}")[2])["custom"]
    assert custom == {"repo": "a/b", "file": "tiny.gguf", "size": 1_000_000_000}
    for broken in ("size=abc", "size=-5", "size=0", "size=", "file=x.gguf", "repo=a%2Fb&size=5"):
        seen = json.loads(client.call("GET", f"/api/disk?path=D%3A%5CN&tier=small&{broken}")[2])
        assert seen["custom"] is None, broken
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


ROUNDS = 100


def _padded_json(size: int) -> bytes:
    """A JSON object of about ``size`` bytes."""
    if size <= 2:
        return b"{}"
    return json.dumps({"pad": "x" * (size - 12)}).encode("utf-8")


def _post_rounds(server: SetupServer, path: str, body: bytes, headers: dict[str, str]) -> collections.Counter:
    """POST ``body`` ROUNDS times on new connections; count each status, or the error that replaced it."""
    outcomes: collections.Counter = collections.Counter()
    for _ in range(ROUNDS):
        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        try:
            conn.request("POST", path, body=body, headers={"Host": f"127.0.0.1:{server.port}", **headers})
            resp = conn.getresponse()
            resp.read()
            outcomes[resp.status] += 1
        except OSError as e:
            outcomes[f"{type(e).__name__} (winerror {getattr(e, 'winerror', None)})"] += 1
        finally:
            conn.close()
    return outcomes


# (path, who asks, content type, body bytes, status the answer must carry). Every one of these is answered
# before the route reads the body (F-2: closing a socket with the body unread made Windows reset the
# connection, and the client got WinError 10053 instead of the answer).
UNREAD_BODY_ANSWERS = [
    ("/api/internal/open-window", "nobody", "application/json", 2, 401),
    ("/api/internal/open-window", "wrong token", "application/json", 10_000, 401),
    ("/api/internal/open-window", "nobody", "application/json", 200_000, 401),
    ("/api/internal/open-window", "token", "application/json", 10_000, 200),
    ("/api/answers", "nobody", "application/json", 60_000, 401),
    ("/api/answers", "cookie", "text/plain", 10_000, 415),
    ("/api/answers", "cookie", "application/json", 70_000, 413),
    ("/api/answers", "cookie", "application/json", 200_000, 413),
    ("/api/answers", "bad host", "application/json", 10_000, 421),
]


@pytest.mark.parametrize(
    ("path", "who", "ctype", "size", "status"),
    UNREAD_BODY_ANSWERS,
    ids=[f"{c[0].rsplit('/', 1)[1]}-{c[1].replace(' ', '-')}-{c[3]}B-{c[4]}" for c in UNREAD_BODY_ANSWERS],
)
def test_an_answer_given_before_the_body_was_read_is_still_delivered(env, path, who, ctype, size, status):
    _, server, client, opened = env
    client.login()
    headers = {"Content-Type": ctype}
    if who == "cookie":
        headers["Cookie"] = f"ns_setup={client.cookie}"
    elif who == "wrong token":
        headers["X-NS-Token"] = "wrong"
    elif who == "token":
        headers["X-NS-Token"] = server.token
    elif who == "bad host":
        headers["Host"] = "evil.example"
    outcomes = _post_rounds(server, path, _padded_json(size), headers)
    assert outcomes == {status: ROUNDS}  # the status every time, never a reset connection
    assert opened.is_set() == (who == "token")  # and the route did what the status says
    assert client.call("GET", "/api/ping")[0] == 200  # the server is still serving afterwards


def test_a_body_nobody_read_does_not_spoil_the_next_request_on_the_same_connection(env):
    _, server, client, _ = env
    client.login()
    headers = {"Host": f"127.0.0.1:{server.port}", "Cookie": f"ns_setup={client.cookie}"}
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        for _ in range(20):
            conn.request("GET", "/api/ping", body=b"x" * 3000, headers=headers)  # a GET route reads no body
            resp = conn.getresponse()
            assert resp.status == 200 and json.loads(resp.read())["ok"] is True
    finally:
        conn.close()


def test_a_body_that_never_arrives_does_not_hold_the_connection_open(env, monkeypatch):
    _, server, _, _ = env
    monkeypatch.setattr(server_module, "DRAIN_SECONDS", 0.3)
    head = (
        "POST /api/internal/open-window HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{server.port}\r\n"
        "Content-Type: application/json\r\n"
        "Content-Length: 1000\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", server.port), timeout=10) as sock:
        sock.sendall(head.encode("ascii") + b"x" * 10)  # the other 990 bytes never come
        t0 = time.monotonic()
        data = b""
        while True:  # until the server closes: a hang would end in the 10 s socket timeout
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        waited = time.monotonic() - t0
    assert data.startswith(b"HTTP/1.1 401") and b"Wrong token" in data
    assert 0.25 <= waited < 5  # it waited for the body (the drain), then gave up and closed


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


def test_static_files_other_than_index_are_revalidated_not_reused_blindly(env):
    """D-29: only `index.html` was marked; app.js and the others carried no rule at all."""
    _, _, client, _ = env
    for path in ("/app.js", "/app.css"):
        status, headers, _ = client.call("GET", path)
        assert status == 200 and headers["cache-control"] == "no-cache", path
        assert headers["content-security-policy"] == CSP and headers["x-content-type-options"] == "nosniff"
    assert client.call("GET", "/")[1]["cache-control"] == "no-store"


def test_the_setup_page_names_the_bundle_with_a_version_token_that_follows_the_files(env):
    """D-29: a profile that cached the old app.js under `./app.js` must be sent to a URL it has never seen."""
    _, server, client, _ = env
    static = server.static_dir
    page = '<script type="module" src="./app.js"></script><link rel="stylesheet" href="./app.css">'
    (static / "index.html").write_text(page, encoding="utf-8")
    status, headers, body = client.call("GET", "/")
    text = body.decode("utf-8")
    assert status == 200 and headers["cache-control"] == "no-store"
    assert '"./app.js?v=' in text and '"./app.css?v=' in text
    token = text.split("app.js?v=")[1].split('"')[0]
    assert token and client.call("GET", f"/app.js?v={token}")[2] == b"console.log(1)"  # the query is ignored
    (static / "app.js").write_text("console.log('the new version')", encoding="utf-8")
    again = client.call("GET", "/")[2].decode("utf-8").split("app.js?v=")[1].split('"')[0]
    assert again and again != token
