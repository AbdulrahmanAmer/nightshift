"""The setup server's HTTP side (docs/design/installer-ui.md 1.4). Standard library only.

``ThreadingHTTPServer`` on 127.0.0.1 with a random port, a one-time token that becomes an HttpOnly cookie, a
Host check against DNS rebinding, a strict Content-Security-Policy, the static UI and the JSON API.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from installer.stage2.web.model import ACTIONS, UI_STEPS

MAX_BODY = 64 * 1024
COOKIE = "ns_setup"
CSP = (
    "default-src 'self'; img-src 'self' data:; media-src 'self'; "
    "style-src 'self' 'unsafe-inline'; script-src 'self'"
)
MIME = {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml",
        ".json": "application/json", ".wav": "audio/wav", ".mp4": "video/mp4", ".jpg": "image/jpeg",
        ".png": "image/png", ".woff2": "font/woff2", ".woff": "font/woff", ".ico": "image/x-icon"}


class SetupHTTPServer(ThreadingHTTPServer):
    """A ThreadingHTTPServer that carries the things the handler needs."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], app: SetupServer
    ) -> None:
        super().__init__(address, handler)
        self.app = app


class Handler(BaseHTTPRequestHandler):
    """One request."""

    protocol_version = "HTTP/1.1"
    server_version = "NightshiftSetup"
    server: SetupHTTPServer
    _body_read = False

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature of the base class
        return None

    # ---- entry points ----
    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        self._body_read = False
        try:
            if not self._host_ok():
                self._text(HTTPStatus.MISDIRECTED_REQUEST, "Wrong host")
                return
            parts = urlsplit(self.path)
            path, query = parts.path, parse_qs(parts.query)
            if method == "GET" and path == "/" and "t" in query:
                self._token_landing(query["t"][0])
                return
            if path == "/api/internal/open-window":
                self._open_window(method)
                return
            if path.startswith("/api/"):
                if not self._cookie_ok():
                    message = "Open the setup window again"
                    self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "message": message})
                    return
                self._api(method, path[len("/api/"):], query)
                return
            if method != "GET":
                self._text(HTTPStatus.METHOD_NOT_ALLOWED, "GET only")
                return
            self._static(path)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as e:  # noqa: BLE001 - the window must get a JSON answer, not a dropped connection
            try:
                message = f"{type(e).__name__}: {e}"
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "message": message})
            except OSError:
                return

    # ---- checks ----
    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") == f"127.0.0.1:{self.server.server_address[1]}"

    def _cookie_ok(self) -> bool:
        jar = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = jar.get(COOKIE)
        if not morsel:
            return False
        return secrets.compare_digest(morsel.value, self.server.app.token)

    def _body(self) -> dict[str, Any] | None:
        """The JSON body, or None after an error answer was sent."""
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"ok": False, "message": "Send application/json"})
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            message = "That request is too big"
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "message": message})
            return None
        self._body_read = True
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": "That is not valid JSON"})
            return None
        if not isinstance(data, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": "Send a JSON object"})
            return None
        return data

    # ---- answers ----
    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self._common(len(body))
        self.wfile.write(body)

    def _text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self._common(len(body))
        self.wfile.write(body)

    def _common(self, length: int) -> None:
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Length", str(length))
        if self.command == "POST" and not self._body_read:
            self.send_header("Connection", "close")     # the request body was not read: do not reuse
        self.end_headers()

    def _token_landing(self, token: str) -> None:
        if not secrets.compare_digest(token, self.server.app.token):
            self._text(HTTPStatus.FORBIDDEN, "That address has expired. Run INSTALL.cmd again.")
            return
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Set-Cookie", f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/")
        self.send_header("Location", "/")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _open_window(self, method: str) -> None:
        if method != "POST":
            self._text(HTTPStatus.METHOD_NOT_ALLOWED, "POST only")
            return
        if not secrets.compare_digest(self.headers.get("X-NS-Token", ""), self.server.app.token):
            self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "message": "Wrong token"})
            return
        threading.Thread(target=self.server.app.on_open_window, name="open-window", daemon=True).start()
        self._json(HTTPStatus.OK, {"ok": True})

    # ---- static files ----
    def _static(self, path: str) -> None:
        root = self.server.app.static_dir
        rel = unquote(path).lstrip("/") or "index.html"
        target = (root / rel).resolve()
        if root.resolve() not in target.parents and target != root.resolve():
            self._text(HTTPStatus.NOT_FOUND, "Not found")
            return
        if target.is_dir():
            target = target / "index.html"
        if not target.is_file():
            self._text(HTTPStatus.NOT_FOUND, "Not found")
            return
        self._file(target, no_store=target.name == "index.html")

    def _file(self, target: Path, *, no_store: bool = False) -> None:
        size = target.stat().st_size
        ctype = MIME.get(target.suffix.lower()) or mimetypes.guess_type(target.name)[0]
        ctype = ctype or "application/octet-stream"
        start, end, status = 0, size - 1, HTTPStatus.OK
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes=") and "," not in rng:
            first, _, last = rng[6:].partition("-")
            try:
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                else:
                    start = max(0, size - int(last))
                end = min(end, size - 1)
                if start > end:
                    raise ValueError
                status = HTTPStatus.PARTIAL_CONTENT
            except ValueError:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self._common(0)
                return
        length = end - start + 1 if size else 0
        self.send_response(int(status))
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if no_store:
            self.send_header("Cache-Control", "no-store")
        self._common(length)
        if self.command == "HEAD" or not length:
            return
        with target.open("rb") as fh:
            fh.seek(start)
            left = length
            while left > 0:
                block = fh.read(min(1 << 16, left))
                if not block:
                    break
                self.wfile.write(block)
                left -= len(block)

    # ---- the API ----
    def _api(self, method: str, name: str, query: dict[str, list[str]]) -> None:
        app = self.server.app
        ctl = app.controller
        if name == "ping" and method == "GET":
            self._json(HTTPStatus.OK, {"ok": True, "pid": app.pid, "version": app.version})
        elif name == "state" and method == "GET" and ctl is not None:
            since = int(query["since"][0]) if query.get("since", [""])[0].isdigit() else None
            self._json(HTTPStatus.OK, ctl.wait_state(since, app.long_poll_s))
        elif name == "ui-step" and method == "POST" and ctl is not None:
            body = self._body()
            if body is not None:
                step = body.get("uiStep")
                if step not in UI_STEPS:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "message": "Unknown screen"})
                else:
                    status, payload = ctl.set_ui_step(step)
                    self._json(status, payload)
        elif name == "answers" and method == "POST" and ctl is not None:
            body = self._body()
            if body is not None:
                self._json(HTTPStatus.OK, ctl.submit_answers(body))
        elif name == "check-key" and method == "POST" and ctl is not None:
            body = self._body()
            if body is not None:
                service, key = str(body.get("service", "")), str(body.get("key", ""))
                self._json(HTTPStatus.OK, ctl.check_key(service, key))
        elif name == "model-check" and method == "GET" and ctl is not None:
            self._json(HTTPStatus.OK, ctl.check_model(query.get("repo", [""])[0], query.get("file", [""])[0]))
        elif name == "disk" and method == "GET" and ctl is not None:
            self._json(HTTPStatus.OK, ctl.disk(query.get("path", [""])[0], query.get("tier", ["small"])[0],
                                               query.get("source", ["0"])[0] == "1"))
        elif name.startswith("action/") and method == "POST" and ctl is not None:
            action = name[len("action/"):]
            body = self._body()
            if body is not None:
                if action not in ACTIONS:
                    self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": f"Unknown action {action}"})
                else:
                    status, payload = ctl.do_action(action, body)
                    self._json(status, payload)
        elif name.startswith("file/") and method == "GET" and ctl is not None:
            target = ctl.file_path(name[len("file/"):])
            if target is None or not target.is_file():
                self._text(HTTPStatus.NOT_FOUND, "Not found")
            else:
                self._file(target)
        elif name == "uninstall" and app.uninstall is not None:
            if method == "GET":
                self._json(HTTPStatus.OK, app.uninstall.view())
            else:
                body = self._body()
                if body is not None:
                    models, data = bool(body.get("deleteModels")), bool(body.get("deleteData"))
                    status, payload = app.uninstall.run(models, data)
                    self._json(status, payload)
        elif name.startswith("action/") and method == "POST" and app.uninstall is not None:
            body = self._body()
            if body is not None:
                status, payload = app.uninstall.action(name[len("action/"):], body)
                self._json(status, payload)
        else:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "message": "Not found"})


class SetupServer:
    """Owns the listening socket, the token and the pieces the handler needs."""

    def __init__(
        self,
        controller: Any | None,
        *,
        static_dir: Path,
        port: int = 0,
        token: str | None = None,
        version: str = "0.0.0",
        uninstall: Any | None = None,
        on_open_window: Callable[[], None] | None = None,
        long_poll_s: float = 15.0,
    ) -> None:
        self.controller = controller
        self.uninstall = uninstall
        self.static_dir = static_dir
        self.token = token or secrets.token_urlsafe(16)
        self.version = version
        self.pid = os.getpid()
        self.long_poll_s = long_poll_s
        self.on_open_window: Callable[[], None] = on_open_window or (lambda: None)
        self.httpd = SetupHTTPServer(("127.0.0.1", port), Handler, self)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """The port the OS gave us."""
        return int(self.httpd.server_address[1])

    @property
    def url(self) -> str:
        """The address with the one-time token."""
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def start(self) -> None:
        """Serve on a background thread."""
        self._thread = threading.Thread(target=self.httpd.serve_forever, name="setup-http", daemon=True,
                                        kwargs={"poll_interval": 0.2})
        self._thread.start()

    def stop(self) -> None:
        """Stop serving."""
        if self._thread is not None:
            self.httpd.shutdown()
        self.httpd.server_close()
