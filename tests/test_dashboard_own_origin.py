# ruff: noqa: E501
"""D-45: the dashboard answers a browser only for its OWN origin, and a refusal note says only what happened.

The install proof of block D (`runs/block-d-proof/s2/08-origin-second-server.txt`) served a page from a second
local server (http://127.0.0.1:50288): it ran the dashboard's `stop_all` four times, because the rule took any
loopback origin, any port, for the dashboard's own page. Then the morning note said "Every request was refused
and nothing was changed". An origin is scheme, host AND port: the dashboard is addressed as `<scheme>://<Host>`
of the request itself, and a browser request is answered only when its `Origin` (or, without one, its
`Sec-Fetch-Site` and `Referer`) says that origin.

Every test enters through the dashboard's real `create_app` on fake services (`tests/test_loopback_guard.py`'s
`dashboard`); the routes are read from `app.routes`, so a route added later is covered.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from ns_common.paths import Paths  # noqa: E402
from ns_supervisor import notify  # noqa: E402
from tests.test_loopback_guard import (  # noqa: E402
    BASE,
    CODE,
    PORT,
    Clock,
    dashboard,
    loopback,
    raw,
    routes,
    settle,
)

OTHER = PORT + 1  # another local server's port: only a number in the headers, nothing binds it
OWN_HOST = f"127.0.0.1:{PORT}"
#: (the Host the browser addressed, the Origin it states, its Sec-Fetch-Site): a page that is not the dashboard's.
FOREIGN_PAGES = (
    (OWN_HOST, f"http://127.0.0.1:{OTHER}", "same-site"),     # another local server's page (the proof's case)
    (OWN_HOST, f"http://localhost:{OTHER}", "cross-site"),
    (OWN_HOST, f"http://localhost:{PORT}", "cross-site"),     # the same port under the other name
    (f"localhost:{PORT}", f"http://127.0.0.1:{PORT}", "cross-site"),
    (OWN_HOST, f"https://127.0.0.1:{PORT}", "cross-site"),    # https, the same host and port
    (OWN_HOST, "null", "cross-site"),                         # a file, a sandboxed frame
)
#: What another local page makes a browser send WITHOUT an Origin: an image or script tag, a frame, and an old
#: browser's request that names the page only in Referer.
NO_ORIGIN_PAGES = (
    {"sec-fetch-site": "same-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "image", "referer": f"http://127.0.0.1:{OTHER}/"},
    {"sec-fetch-site": "same-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "iframe"},
    {"sec-fetch-site": "none", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "script"},
    {"referer": f"http://127.0.0.1:{OTHER}/page.html"},
)
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
BODIES = (("application/json", b'{"ids": [], "a": 1}'), ("application/x-www-form-urlencoded", b"a=1"))
OWN_PAGE_MARKS = {"sec-fetch-site": "same-origin", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty"}


# ---------------------------------------------------------------------------------------------- helpers
@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """The check's clock, fixed (the notes say "last at 09:00")."""
    c = Clock(datetime(2026, 11, 10, 9, 0, 0))
    if loopback is not None:
        monkeypatch.setattr(loopback, "wall_clock", c)
    return c


class Reached:
    """Records every request the check let through to the app (`answer`: the routes behind it) and the
    dashboard's actions."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ns_dashboard.server import LoopbackMiddleware
        from ns_dashboard.svc_engine import EngineService
        from ns_dashboard.svc_library import LibraryService

        self.routes: list[str] = []
        self.actions: list[str] = []
        real = LoopbackMiddleware.answer

        async def answer(guard: Any, scope: Any, receive: Any, send: Any) -> None:
            self.routes.append(f"{scope.get('method')} {scope.get('path')}")
            await real(guard, scope, receive, send)

        monkeypatch.setattr(LoopbackMiddleware, "answer", answer)
        monkeypatch.setattr(EngineService, "stop_all", lambda _self: self.actions.append("stop_all") or {"ok": True})
        monkeypatch.setattr(
            LibraryService, "update_apply_file",
            lambda _self, name, zip_fh, sha_fh: self.actions.append(f"update {name}") or {"ok": True},
        )


def home_state(home: Path) -> dict[str, str]:
    """Every file of the home but the logs and the notes (a refusal writes those): what a request could change."""
    out: dict[str, str] = {}
    for file in sorted(home.rglob("*")):
        rel = file.relative_to(home).as_posix()
        if file.is_file() and not rel.startswith("logs/") and not rel.startswith("run/morning_note"):
            out[rel] = hashlib.sha256(file.read_bytes()).hexdigest()
    return out


def requests(app: Any) -> Iterator[tuple[str, str, dict[str, str], bytes | None, str]]:
    """Every route path x every method x (no body for GET and OPTIONS, else JSON and a form) x every foreign
    page: (method, path, headers, body, what it is)."""
    paths = sorted({path for _m, path in routes(app)})
    for path in paths:
        for method in METHODS:
            bodies: tuple[tuple[str | None, bytes | None], ...] = (
                ((None, None),) if method in ("GET", "OPTIONS") else BODIES
            )
            for kind, body in bodies:
                base: dict[str, str] = {"content-type": kind} if kind else {}
                if method == "OPTIONS":
                    base = {"access-control-request-method": "POST", "access-control-request-headers": "content-type"}
                for host, origin, site in FOREIGN_PAGES:
                    headers = {**base, "host": host, "origin": origin, "sec-fetch-site": site, "sec-fetch-mode": "cors"}
                    yield method, path, headers, body, f"{origin} to {host}"
                for marks in NO_ORIGIN_PAGES:
                    navigation = "referer" in marks and len(marks) == 1
                    if navigation and method == "GET" and not path.startswith("/api/"):
                        continue   # an old browser following a link to the page: it opens (see the next test)
                    yield method, path, {**base, "host": OWN_HOST, **marks}, body, f"no Origin, {marks}"


async def send_all(app: Any, batch: list[tuple[str, str, dict[str, str], bytes | None, str]]) -> list[str]:
    """Send the batch on one event loop (thousands of requests); what is wrong with each answer."""
    wrong: list[str] = []
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=BASE) as client:
        for method, path, headers, body, what in batch:
            r = await client.request(method, path, headers=headers, content=body, params={"limit": 1})
            cors = [h for h in r.headers if h.lower().startswith("access-control-")]
            if r.status_code != 403:
                wrong.append(f"{method} {path} from {what}: answered {r.status_code}")
            elif r.json().get("error", {}).get("code") != CODE:
                wrong.append(f"{method} {path} from {what}: 403 with {r.text[:80]}")
            if cors:
                wrong.append(f"{method} {path} from {what}: CORS headers {cors}")
    return wrong


# ------------------------------------------------------------------- 1. every route, every foreign page
def test_a_page_on_another_local_port_reaches_no_route_of_the_dashboard(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, clock: Clock
):
    """The defect itself, on every route the app has: any page that is not the dashboard's own (another local
    port, the other host name, https, a file) gets 403 NS-E207 for every method and content type, reads
    nothing, changes nothing, and is never handed a CORS header."""
    app = dashboard(tmp_home)
    reached = Reached(monkeypatch)
    paths = sorted({path for _m, path in routes(app)})
    assert "/api/engine/stop_all" in paths and "/api/update/file" in paths and "/api/settings" in paths
    assert "/api/uploads/x" in paths and "/api/events" in paths and "/" in paths and len(paths) >= 70
    before = home_state(tmp_home)
    batch = list(requests(app))
    wrong = asyncio.run(send_all(app, batch))
    settle()
    assert wrong == [], f"{len(wrong)} of {len(batch)} wrong: " + "; ".join(wrong[:6])
    assert reached.routes == [], f"routes ran for a foreign page: {reached.routes[:6]}"
    assert reached.actions == []
    assert home_state(tmp_home) == before


def test_the_dashboard_answers_its_own_page_under_each_name(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """Its own page (the same scheme, name and port it was addressed on), the window host, the Vite dev server
    (its proxy keeps the Host the page addressed), a typed address and a link that opens the page."""
    app = dashboard(tmp_home)
    reached = Reached(monkeypatch)
    for host in (OWN_HOST, f"localhost:{PORT}", f"[::1]:{PORT}", "localhost:5173"):
        tc = TestClient(app, base_url=f"http://{host}")
        page = {"origin": f"http://{host}", **OWN_PAGE_MARKS}
        assert tc.post("/api/engine/stop_all", headers=page).status_code == 200, host
        assert tc.get("/api/settings", headers=OWN_PAGE_MARKS).status_code == 200, host   # a GET states no Origin
        assert tc.post("/api/notes/clear", headers=page, json={"ids": []}).status_code == 200, host
    assert reached.actions == ["stop_all"] * 4
    tc = TestClient(app, base_url=BASE)
    # a page opens: typed or a bookmark (none), a link on another local page or site, an old browser's link
    for marks in (
        {"sec-fetch-site": "none", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document", "sec-fetch-user": "?1"},
        {"sec-fetch-site": "same-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document", "referer": f"http://127.0.0.1:{OTHER}/"},
        {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"},
        {"referer": f"http://127.0.0.1:{OTHER}/"},
        {},
    ):
        r = tc.get("/", headers=marks)
        assert r.status_code == 200 and "frame-ancestors 'none'" in r.headers["content-security-policy"], marks
        assert not [h for h in r.headers if h.lower().startswith("access-control-")]
    # the owner's own navigation (no page can make a browser say `none`) opens an API GET too: the window host
    # sends "Open thumbnail" (/api/thumbs/...) to the default browser, which opens it as a typed address
    typed = {"sec-fetch-site": "none", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}
    assert tc.get("/api/settings", headers=typed).status_code == 200
    # ... but never a POST, and a link on another page never opens an API route (a GET there may do work)
    assert tc.post("/api/engine/stop_all", headers=typed).status_code == 403
    for site in ("same-site", "cross-site"):
        link = {**typed, "sec-fetch-site": site}
        for path in ("/api/update/check", "/api/settings", "/api/engine/stop_all"):
            r = tc.get(path, headers=link)
            assert r.status_code == 403 and r.json()["error"]["code"] == CODE, (site, path)
    assert reached.actions == ["stop_all"] * 4


def test_the_pages_uploads_and_event_stream_still_work(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """Settings > Updates > From a file (a multipart form), the clip upload (start, a chunk with no content
    type, complete) and the server-sent events, each as the dashboard's own page sends them."""
    app = dashboard(tmp_home)
    reached = Reached(monkeypatch)
    tc = TestClient(app, base_url=BASE, raise_server_exceptions=False)
    page = {"origin": BASE, **OWN_PAGE_MARKS}
    upload = {"file": ("Nightshift-Update-9.9.9.zip", b"PK\x05\x06" + b"\x00" * 18, "application/zip"),
              "checksum": ("Nightshift-Update-9.9.9.zip.sha256", b"0" * 64, "text/plain")}
    r = tc.post("/api/update/file", files=upload, headers=page)
    assert r.status_code == 200 and reached.actions == ["update Nightshift-Update-9.9.9.zip"], r.text
    started = tc.post("/api/uploads", json={"filename": "clip.mp4", "size": 5}, headers=page)
    assert started.status_code == 201, started.text
    up = started.json()["upload_id"]
    chunk = tc.put(f"/api/uploads/{up}", params={"offset": 0}, content=b"12345", headers=page)
    assert chunk.status_code == 200, chunk.text
    done = tc.post(f"/api/uploads/{up}/complete", headers=page)
    assert done.status_code == 200 and done.json()["path"], done.text
    # the same chunk from a page on another port, or with no Origin (a form with no sender), is refused
    for headers in ({"origin": f"http://127.0.0.1:{OTHER}", **OWN_PAGE_MARKS}, {}):
        r = tc.put(f"/api/uploads/{up}", params={"offset": 5}, content=b"6", headers=headers)
        assert r.status_code == 403 and r.json()["error"]["code"] == CODE, headers
    events = tc.get("/api/events", params={"limit": 1}, headers={**OWN_PAGE_MARKS, "accept": "text/event-stream"})
    assert events.status_code == 200 and events.headers["content-type"].startswith("text/event-stream")
    assert "data:" in events.text


def test_programs_of_this_pc_still_reach_the_dashboard(tmp_home: Path):
    """No Origin, no Referer, no Sec-Fetch-*: `ns`, the launchers, setup's check and prove_ctx (urllib, JSON)."""
    tc = TestClient(dashboard(tmp_home), base_url=BASE)
    for host in (OWN_HOST, f"localhost:{PORT}", "127.0.0.1"):
        assert tc.get("/api/status", headers={"host": host}).status_code == 200, host
        assert tc.get("/", headers={"host": host}).status_code == 200, host
    assert tc.post("/api/notes/clear", json={"ids": []}).status_code == 200
    assert tc.post("/api/jobs/verify", json={}).status_code == 201


def test_a_websocket_upgrade_from_another_local_page_is_closed(tmp_home: Path):
    """The check runs on the upgrade request too (a browser always states the Origin of a websocket)."""
    app = dashboard(tmp_home)
    for origin in (f"http://127.0.0.1:{OTHER}", f"http://localhost:{PORT}", "null"):
        headers = [(b"host", OWN_HOST.encode()), (b"origin", origin.encode())]
        assert raw(app, headers, path="/api/events", kind="websocket") == [{"type": "websocket.close", "code": 1008}]


# ----------------------------------------------------------------------------- 2. the rule, row by row
#: The origin table of the report: (rule, Host, headers, method, path, answered?). One `decide` for every
#: server: the engine and the voice service have no own origin, so the same rows refuse every browser there.
OWN = f"http://{OWN_HOST}"
TABLE: tuple[tuple[str, str, dict[str, str], str, str, bool], ...] = (
    ("program, no marks", OWN_HOST, {}, "POST", "/api/engine/stop_all", True),
    ("program, a form body", OWN_HOST, {"content-type": "text/plain"}, "POST", "/api/engine/stop_all", False),
    ("own page, POST", OWN_HOST, {"origin": OWN, "sec-fetch-site": "same-origin"}, "POST", "/api/engine/stop_all", True),
    ("own page, upload form", OWN_HOST, {"origin": OWN, "sec-fetch-site": "same-origin", "content-type": "multipart/form-data; boundary=x"}, "POST", "/api/update/file", True),
    ("own page, GET (no Origin)", OWN_HOST, {"sec-fetch-site": "same-origin"}, "GET", "/api/settings", True),
    ("own page as localhost", f"localhost:{PORT}", {"origin": f"http://localhost:{PORT}", "sec-fetch-site": "same-origin"}, "POST", "/api/engine/stop_all", True),
    ("Vite dev server, proxied", "localhost:5173", {"origin": "http://localhost:5173", "sec-fetch-site": "same-origin"}, "POST", "/api/engine/stop_all", True),
    ("another local port", OWN_HOST, {"origin": f"http://127.0.0.1:{OTHER}", "sec-fetch-site": "same-site"}, "POST", "/api/engine/stop_all", False),
    ("another local port, GET", OWN_HOST, {"origin": f"http://127.0.0.1:{OTHER}", "sec-fetch-site": "same-site"}, "GET", "/api/settings", False),
    ("the other name, same port", OWN_HOST, {"origin": f"http://localhost:{PORT}"}, "POST", "/api/engine/stop_all", False),
    ("https, same host and port", OWN_HOST, {"origin": f"https://127.0.0.1:{PORT}"}, "POST", "/api/engine/stop_all", False),
    ("null origin", OWN_HOST, {"origin": "null", "sec-fetch-site": "cross-site"}, "POST", "/api/engine/stop_all", False),
    ("own Origin, foreign fetch-site", OWN_HOST, {"origin": OWN, "sec-fetch-site": "same-site"}, "POST", "/api/engine/stop_all", False),
    ("image tag on another port", OWN_HOST, {"sec-fetch-site": "same-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "image"}, "GET", "/api/update/check", False),
    ("typed address of a page", OWN_HOST, {"sec-fetch-site": "none", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}, "GET", "/", True),
    ("typed address of an API GET", OWN_HOST, {"sec-fetch-site": "none", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}, "GET", "/api/thumbs/1/x", True),
    ("a POST marked as typed", OWN_HOST, {"sec-fetch-site": "none", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}, "POST", "/api/engine/stop_all", False),
    ("link from another local page to an API GET", OWN_HOST, {"sec-fetch-site": "same-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}, "GET", "/api/update/check", False),
    ("script tag marked none", OWN_HOST, {"sec-fetch-site": "none", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "script"}, "GET", "/api/settings", False),
    ("form a page posts (navigation)", OWN_HOST, {"origin": f"http://127.0.0.1:{OTHER}", "sec-fetch-site": "same-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document", "content-type": "application/x-www-form-urlencoded"}, "POST", "/api/engine/stop_all", False),
    ("link from another site to a page", OWN_HOST, {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document"}, "GET", "/", True),
    ("frame on another site", OWN_HOST, {"sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "iframe"}, "GET", "/", False),
    ("old browser, foreign Referer, page", OWN_HOST, {"referer": f"http://127.0.0.1:{OTHER}/"}, "GET", "/", True),
    ("old browser, foreign Referer, API", OWN_HOST, {"referer": f"http://127.0.0.1:{OTHER}/"}, "GET", "/api/settings", False),
    ("old browser, own Referer", OWN_HOST, {"referer": OWN + "/"}, "GET", "/api/settings", True),
)


def test_the_origin_table_row_by_row():
    assert loopback is not None
    dash = vars(loopback).get("OWN_PAGE") or vars(loopback)["LOOPBACK_PAGES"]   # the base names it LOOPBACK_PAGES
    wrong: list[str] = []
    for what, host, marks, method, path, answered in TABLE:
        headers = [(b"host", host.encode()), *((k.encode(), v.encode()) for k, v in marks.items())]
        if method != "GET" and "content-type" not in marks:
            headers.append((b"content-type", b"application/json"))
        got = loopback.decide(headers, dash, method, path)
        if (got is None) != answered:
            wrong.append(f"{what}: {'answered' if got is None else 'refused (' + got.why + ')'}")
        # the engine and the voice service: the same code, no own origin; a browser is never answered there
        engine = loopback.decide(headers, loopback.NO_BROWSER, method, path)
        if (engine is None) != (answered and not [k for k in marks if k != "content-type"]):
            wrong.append(f"{what} at the engine: {'answered' if engine is None else 'refused'}")
    assert wrong == [], "; ".join(wrong)


def test_no_server_of_ours_sends_a_cors_header():
    """A cross-origin read is impossible only while no answer grants one: no Access-Control-* anywhere."""
    root = Path(__file__).resolve().parents[1]
    for file in sorted(root.glob("ns_*/**/*.py")):
        if "/ui/" in file.as_posix() or "node_modules" in file.as_posix():
            continue
        text = file.read_text(encoding="utf-8")
        assert "access-control-allow" not in text.lower() and "CORSMiddleware" not in text, file


# ------------------------------------------------------------------------------------- 3. the note's words
def test_the_note_counts_only_the_refused_and_claims_nothing_else(tmp_home: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch):
    """A page on another local port opened the dashboard by a link (answered: a page opens) and then posted
    three times (refused). The note counts the three refusals and says only that they did not run."""
    app = dashboard(tmp_home)
    reached = Reached(monkeypatch)
    tc = TestClient(app, base_url=BASE)
    page = f"http://127.0.0.1:{OTHER}"
    link = {"sec-fetch-site": "same-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document", "referer": page + "/"}
    assert tc.get("/", headers=link).status_code == 200
    for _ in range(3):
        r = tc.post("/api/engine/stop_all", headers={"origin": page, "sec-fetch-site": "same-site", "sec-fetch-mode": "cors"})
        assert r.status_code == 403
    settle()
    said = [n["message"] for n in notify.read_notes(Paths.from_env())]
    assert said == [
        f"NS-E207: A web page open in your browser tried 3 times to control Nightshift (from {page}, last at "
        "09:00). Nightshift refused those 3 requests, so none of them ran. If you do not know that page, close it."
    ]
    assert reached.actions == []
    for text in said:
        assert "nothing was changed" not in text and "Every request" not in text


def test_no_note_text_claims_more_than_the_refusals():
    """Every wording the check can write (a page, a form with no sender, a foreign name, the day's last
    note) says the counted requests were refused, and nothing about requests it did not refuse."""
    assert loopback is not None
    at = datetime(2026, 11, 10, 9, 0)
    cases = [
        loopback.Refusal("origin", OWN_HOST, f"http://127.0.0.1:{OTHER}", f"http://127.0.0.1:{OTHER}", True),
        loopback.Refusal("origin", OWN_HOST, "null", "null", True),
        loopback.Refusal("form", OWN_HOST, None, "a web form without a sender", False),
        loopback.Refusal("host", "rebind.example", None, "rebind.example", False),
    ]
    for refusal in cases:
        for more in (False, True):
            text, _mark = loopback.note_text(refusal, 4, at, more=more)
            assert "Nightshift refused those 4 requests, so none of them ran." in text, text
            assert "nothing was changed" not in text and "Every request" not in text, text
