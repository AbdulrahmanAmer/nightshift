"""Fake Pexels and Pixabay for the proofs: a local intercepting HTTPS proxy (standard library, plus
`cryptography` or `openssl` once, to make a throw-away CA and leaf certificate).

Ported from runs/qa/fakefootage/fakefootage.py (QA, 2026-11-09). A client is pointed at it with
HTTPS_PROXY=http://127.0.0.1:<port>, NO_PROXY=127.0.0.1,localhost and SSL_CERT_FILE=<folder>/certs/bundle.pem
(`client_env`). CONNECT to an intercepted host is ended here with a leaf certificate signed by the fake CA and
answered by the fake API; every other host is tunnelled to the real server untouched.

    python footage.py --dir <folder> [--good-key GOODKEY]

It writes `<folder>/certs/*`, `<folder>/media/*`, `<folder>/requests.jsonl` and, once it listens,
`<folder>/ready.json` ({"port": N, "pid": N}). The port is chosen by the system.

Control (plain proxy port, no proxy in front): /__ctl?mode=normal|burst429|always429|down|slow
    &n=<burst count>&retry_after=<s>&delay=<s> ; /__ctl?stats=1 ; /__ctl?reset=1 ; /__ctl?quit=1
Only API requests count against 429/503; downloads are never rate limited. Auth is checked before the mode.
The control calls are not counted in the stats.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import http.client
import json
import os
import re
import select
import shutil
import socket
import ssl
import subprocess  # noqa: TID251  (a fake endpoint makes its certificates and clips with openssl and FFmpeg)
import sys
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit

INTERCEPT = (
    "api.pexels.com",
    "www.pexels.com",
    "videos.pexels.com",
    "pixabay.com",
    "cdn.pixabay.com",
    "player.vimeo.com",
)
MODES = ("normal", "burst429", "always429", "down", "slow")
HOP = {
    "connection",
    "proxy-connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "proxy-authorization",
    "proxy-authenticate",
}
CLIP_SECONDS = 3
CERT_DAYS = 30


# ------------------------------------------------------------------------------------------ certificates
def _git_openssl() -> list[Path]:
    """openssl.exe beside Git for Windows (not on PATH in a plain PowerShell), found from git itself."""
    git = shutil.which("git")
    if not git:
        return []
    root = Path(git).resolve().parent.parent
    return [root / "mingw64" / "bin" / "openssl.exe", root / "usr" / "bin" / "openssl.exe"]


def find_openssl() -> str | None:
    on_path = shutil.which("openssl")
    if on_path:
        return on_path
    return next((str(p) for p in _git_openssl() if p.is_file()), None)


def _has_cryptography() -> bool:
    try:
        import cryptography  # noqa: F401
    except ImportError:
        return False
    return True


def cert_tool_problem() -> str | None:
    """None when a CA can be made here, else the reason (for ctx.skip)."""
    if _has_cryptography() or find_openssl():
        return None
    return (
        "no way to make a certificate here: the `cryptography` package is not importable and `openssl` is "
        "neither on PATH nor beside Git for Windows"
    )


def _make_certs_cryptography(folder: Path) -> None:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.UTC)
    start = now - datetime.timedelta(minutes=5)
    end = now + datetime.timedelta(days=CERT_DAYS)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Nightshift Proof Fake CA")])
    ca_usage = x509.KeyUsage(
        digital_signature=False,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=True,
        crl_sign=True,
        encipher_only=False,
        decipher_only=False,
    )
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(ca_usage, critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, INTERCEPT[0])])
    leaf = (
        x509.CertificateBuilder()
        .subject_name(leaf_name)
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(h) for h in INTERCEPT]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    (folder / "ca.pem").write_bytes(ca.public_bytes(pem))
    (folder / "leaf.pem").write_bytes(leaf.public_bytes(pem))
    key_bytes = leaf_key.private_bytes(
        pem, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    )
    (folder / "leaf.key").write_bytes(key_bytes)


def _make_certs_openssl(folder: Path, exe: str) -> None:
    # A config file of our own, so no system openssl.cnf is needed; relative names, so a path style
    # (Windows or MSYS) never matters.
    (folder / "ssl.cnf").write_text("[req]\ndistinguished_name=dn\nprompt=no\n[dn]\nCN=x\n", encoding="ascii")
    san = ",".join(f"DNS:{h}" for h in INTERCEPT)
    (folder / "leaf.ext").write_text(
        f"basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n"
        f"extendedKeyUsage=serverAuth\nsubjectAltName={san}\n",
        encoding="ascii",
    )
    env = {**os.environ, "OPENSSL_CONF": "ssl.cnf", "MSYS_NO_PATHCONV": "1", "MSYS2_ARG_CONV_EXCL": "*"}

    def ssl_run(*args: str) -> None:
        done = subprocess.run(
            [exe, *args], cwd=folder, env=env, capture_output=True, text=True, timeout=120, check=False
        )
        if done.returncode != 0:
            raise RuntimeError(f"openssl {args[0]} failed ({done.returncode}): {done.stderr.strip()[-300:]}")

    days = str(CERT_DAYS)
    ssl_run(
        *("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-config", "ssl.cnf"),
        *("-keyout", "ca.key", "-out", "ca.pem", "-days", days, "-subj", "/CN=Nightshift Proof Fake CA"),
        *("-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign,cRLSign"),
    )
    ssl_run(
        *("req", "-newkey", "rsa:2048", "-nodes", "-config", "ssl.cnf"),
        *("-keyout", "leaf.key", "-out", "leaf.csr", "-subj", f"/CN={INTERCEPT[0]}"),
    )
    ssl_run(
        *("x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial"),
        *("-out", "leaf.pem", "-days", days, "-extfile", "leaf.ext"),
    )
    ssl_run("verify", "-CAfile", "ca.pem", "leaf.pem")


def make_certs(folder: Path) -> Path:
    """Make `<folder>/certs/{ca.pem, leaf.pem, leaf.key, bundle.pem}` and return the bundle's path.

    `bundle.pem` is what SSL_CERT_FILE points at: httpx 0.28 treats that file as a replacement for certifi,
    so it holds certifi's roots too when the package can be imported here, and always the fake CA.
    """
    certs = folder / "certs"
    certs.mkdir(parents=True, exist_ok=True)
    if _has_cryptography():
        _make_certs_cryptography(certs)
    else:
        exe = find_openssl()
        if not exe:
            raise RuntimeError(cert_tool_problem() or "no certificate tool")
        _make_certs_openssl(certs, exe)
    parts: list[str] = []
    try:
        import certifi

        parts.append(Path(certifi.where()).read_text(encoding="ascii", errors="replace"))
    except (ImportError, OSError):
        pass
    parts.append((certs / "ca.pem").read_text(encoding="ascii"))
    bundle = certs / "bundle.pem"
    bundle.write_text("\n".join(parts), encoding="ascii")
    return bundle


def client_env(folder: Path, port: int) -> dict[str, str]:
    """The environment that points a Python client (urllib, httpx) at a running fake."""
    proxy = f"http://127.0.0.1:{port}"
    return {
        "HTTPS_PROXY": proxy,
        "HTTP_PROXY": proxy,
        "NO_PROXY": "127.0.0.1,localhost",
        "SSL_CERT_FILE": str(folder / "certs" / "bundle.pem"),
    }


def media_path(folder: Path, portrait: bool) -> Path:
    w, h = (1080, 1920) if portrait else (1920, 1080)
    return folder / "media" / f"clip_{w}x{h}.mp4"


def make_media(folder: Path) -> None:
    """Two short H.264 test clips (1080x1920 and 1920x1080) with FFmpeg."""
    exe = shutil.which("ffmpeg")
    if not exe:
        raise RuntimeError("ffmpeg is not on PATH, cannot make the fake clips")
    (folder / "media").mkdir(parents=True, exist_ok=True)
    for portrait in (True, False):
        path = media_path(folder, portrait)
        w, h = (1080, 1920) if portrait else (1920, 1080)
        tmp = path.with_suffix(".tmp.mp4")
        src = f"testsrc2=size={w}x{h}:rate=10:duration={CLIP_SECONDS}"
        cmd = [exe, "-y", "-loglevel", "error", "-f", "lavfi", "-i", src, "-c:v", "libx264"]
        cmd += ["-preset", "ultrafast", "-crf", "35", "-pix_fmt", "yuv420p", "-an"]
        cmd += ["-movflags", "+faststart", str(tmp)]
        subprocess.run(cmd, check=True, timeout=300)
        tmp.replace(path)


# ------------------------------------------------------------------------------------------------ state
class State:
    """Mode, counters and the request log; one instance shared by all threads."""

    def __init__(self, good_keys: set[str], log_path: Path) -> None:
        self.lock = threading.Lock()
        self.good_keys = good_keys
        self.mode = "normal"
        self.burst_left = 0
        self.retry_after = 2.0
        self.delay = 0.0
        self.stats: Counter[str] = Counter()
        self.log_path = log_path

    def set_mode(self, mode: str, n: int | None, retry_after: float | None, delay: float | None) -> None:
        with self.lock:
            self.mode = mode
            if retry_after is not None:
                self.retry_after = retry_after
            if delay is not None:
                self.delay = delay
            if mode == "burst429":
                self.burst_left = n if n is not None else 1
            elif mode == "slow" and self.delay <= 0:
                self.delay = 1.0
            elif mode == "normal":
                self.burst_left = 0
                self.delay = delay if delay is not None else 0.0

    def gate(self) -> tuple[str, float]:
        """('ok'|'429'|'503', retry_after) for one authenticated API request."""
        with self.lock:
            if self.mode == "down":
                return "503", 0
            if self.mode == "always429":
                return "429", self.retry_after
            if self.mode == "burst429" and self.burst_left > 0:
                self.burst_left -= 1
                if self.burst_left == 0:
                    self.mode = "normal"  # served normally after N; the 429 itself is logged as burst429
                return "429", self.retry_after
            return "ok", 0

    def log(self, entry: dict) -> None:
        entry = {"ts": round(time.time(), 3), **entry}
        line = json.dumps(entry, separators=(",", ":"))
        with self.lock:
            if "status" in entry:
                self.stats[f"{entry.get('host')}|{entry['status']}"] += 1
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def snapshot(self) -> dict:
        with self.lock:
            hosts: dict[str, dict[str, int]] = {}
            for k, v in self.stats.items():
                host, status = k.rsplit("|", 1)
                hosts.setdefault(host, {})[status] = v
            return {
                "mode": self.mode,
                "burst_left": self.burst_left,
                "retry_after": self.retry_after,
                "delay": self.delay,
                "counts": hosts,
            }


class App:
    def __init__(self, folder: Path, good_keys: set[str]) -> None:
        self.folder = folder
        self.state = State(good_keys, folder / "requests.jsonl")
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(folder / "certs" / "leaf.pem", folder / "certs" / "leaf.key")


def mask_query(q: list[tuple[str, str]]) -> dict[str, str]:
    return {k: ("***" if k.lower() in ("key", "api_key") else v) for k, v in q}


def _num(seed: str) -> int:
    return int(hashlib.sha1(seed.encode()).hexdigest()[:6], 16)


def _words(q: str) -> list[str]:
    return [x for x in re.sub(r"[^a-z0-9]+", " ", q.lower()).split() if x] or ["clip"]


# ----------------------------------------------------------------------------------------------- fake API
def pexels_search(query: dict[str, str]) -> dict:
    q = query.get("query", "nature")
    portrait = query.get("orientation") == "portrait"
    w, h = (1080, 1920) if portrait else (1920, 1080)
    slug = "-".join(_words(q))
    n = min(4, int(query.get("per_page") or 4))
    base = 3_000_000 + _num(q) % 500_000
    vids = []
    for i in range(n):
        vid = base + i
        hd = f"https://videos.pexels.com/video-files/{vid}/{vid}-hd_{w}_{h}.mp4"
        sd = f"https://videos.pexels.com/video-files/{vid}/{vid}-sd_{w // 2}_{h // 2}.mp4"
        vids.append(
            {
                "id": vid,
                "width": w,
                "height": h,
                "duration": CLIP_SECONDS,
                "url": f"https://www.pexels.com/video/{slug}-{vid}/",
                "image": f"https://images.pexels.com/videos/{vid}/free-video-{vid}.jpg",
                "user": {"id": 1, "name": "Proof Fake", "url": "https://www.pexels.com/@proof-fake"},
                "video_files": [
                    {
                        "id": vid * 10 + 1,
                        "quality": "hd",
                        "file_type": "video/mp4",
                        "width": w,
                        "height": h,
                        "fps": 10.0,
                        "link": hd,
                    },
                    {
                        "id": vid * 10 + 2,
                        "quality": "sd",
                        "file_type": "video/mp4",
                        "width": w // 2,
                        "height": h // 2,
                        "fps": 10.0,
                        "link": sd,
                    },
                ],
            }
        )
    return {
        "page": int(query.get("page") or 1),
        "per_page": n,
        "total_results": n,
        "videos": vids,
        "url": f"https://www.pexels.com/videos/search/{quote(q)}/",
    }


def pixabay_search(query: dict[str, str]) -> dict:
    """Six hits, ids alternate portrait (even) and landscape (odd) so both orientations get candidates."""
    q = query.get("q", "nature")
    words = _words(q)
    n = min(6, max(1, int(query.get("per_page") or 6)))
    base = (_num(q) % 200_000) * 2 + 100_000
    hits = []
    for i in range(n):
        vid = base + i  # even id -> portrait, odd -> landscape
        portrait = vid % 2 == 0
        dims = {"large": (2160, 3840), "medium": (1440, 2560), "small": (1080, 1920), "tiny": (720, 1280)}
        videos = {}
        for name, (w, h) in dims.items():
            if not portrait:
                w, h = h, w
            videos[name] = {
                "url": f"https://cdn.pixabay.com/video/{vid}/{name}.mp4",
                "width": w,
                "height": h,
                "size": w * h // 40,
                "thumbnail": f"https://cdn.pixabay.com/video/{vid}/{name}.jpg",
            }
        hits.append(
            {
                "id": vid,
                "pageURL": f"https://pixabay.com/videos/{'-'.join(words)}-{vid}/",
                "type": "film",
                "tags": ", ".join([*words[:3], "fake", "footage"]),
                "duration": CLIP_SECONDS,
                "views": 1000,
                "downloads": 100,
                "likes": 10,
                "user": "proof-fake",
                "videos": videos,
            }
        )
    return {"total": 600, "totalHits": 500, "hits": hits}


class Api(BaseHTTPRequestHandler):
    """Fake Pexels/Pixabay on a TLS-terminated CONNECT socket (keep-alive, several requests)."""

    protocol_version = "HTTP/1.1"
    server_version = "footage-fake"
    timeout = 60

    def log_message(self, *a) -> None:  # the stdlib access log is off; requests.jsonl has the lines
        pass

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionError, TimeoutError, ssl.SSLError, OSError):
            pass

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def send(
        self,
        status: int,
        body: bytes | str = b"",
        ctype: str = "application/json",
        headers: dict[str, str] | None = None,
    ) -> None:
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.app.state.log(
            {
                "host": self.host,
                "method": self.command,
                "path": self.url.path,
                "query": mask_query(self.qs),
                "status": status,
                "mode": self.mode_at_start,
            }
        )

    def json(self, status: int, obj: dict) -> None:
        self.send(status, json.dumps(obj))

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        st = self.app.state
        self.url = urlsplit(self.path)
        self.qs = parse_qsl(self.url.query, keep_blank_values=True)
        self.host = (self.headers.get("Host") or "").split(":")[0].lower()
        self.mode_at_start = st.mode
        if st.delay > 0:
            time.sleep(st.delay)
        path = self.url.path
        query = dict(self.qs)
        try:
            pexels_paths = ("/videos/search", "/v1/videos/search", "/v1/search")
            if self.host == "api.pexels.com" and path in pexels_paths:
                if self.headers.get("Authorization") not in st.good_keys:
                    return self.send(401, "Unauthorized", "text/plain")
                if not self.gate_ok(pexels=True):
                    return None
                if "videos" in path:
                    return self.json(200, pexels_search(query))
                photos = [{"id": 1, "width": 100, "height": 100}]
                return self.json(200, {"page": 1, "per_page": 1, "total_results": 1, "photos": photos})
            if self.host == "pixabay.com" and path in ("/api/videos/", "/api/videos", "/api/"):
                if query.get("key") not in st.good_keys:
                    return self.send(400, '[ERROR 400] "key" is invalid', "text/plain")
                if not self.gate_ok(pexels=False):
                    return None
                if path.startswith("/api/videos"):
                    return self.json(200, pixabay_search(query))
                return self.json(200, {"total": 1, "totalHits": 1, "hits": [{"id": 1, "tags": "nature"}]})
            if self.host == "videos.pexels.com" and path.endswith(".mp4"):
                m = re.search(r"_(\d+)_(\d+)\.mp4$", path)
                return self.send_media(bool(m) and int(m.group(2)) > int(m.group(1)))
            if self.host == "cdn.pixabay.com" and path.endswith(".mp4"):
                m = re.match(r"/video/(\d+)/", path)
                return self.send_media(bool(m) and int(m.group(1)) % 2 == 0)
            if self.host == "www.pexels.com":
                return self.send(200, "<html><body>fake pexels</body></html>", "text/html")
            return self.send(404, "not found", "text/plain")
        except OSError as e:
            return self.send(500, str(e), "text/plain")

    def gate_ok(self, *, pexels: bool) -> bool:
        verdict, ra = self.app.state.gate()
        if verdict == "ok":
            return True
        if verdict == "503":
            self.send(503, "Service Unavailable", "text/plain")
            return False
        h = {"Retry-After": str(int(ra) if float(ra).is_integer() else ra), "X-Ratelimit-Remaining": "0"}
        if pexels:
            h["X-Ratelimit-Reset"] = str(int(time.time() + ra))
            h["X-Ratelimit-Limit"] = "200"
        else:
            h["X-RateLimit-Reset"] = str(int(ra))
            h["X-RateLimit-Limit"] = "100"
        self.send(429, "Too Many Requests" if pexels else "API rate limit exceeded", "text/plain", h)
        return False

    def send_media(self, portrait: bool) -> None:
        data = media_path(self.app.folder, portrait).read_bytes()
        self.send(200, data, "video/mp4", {"Accept-Ranges": "none"})


# ---------------------------------------------------------------------------------------------- the proxy
class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "footage-fake-proxy"
    timeout = 120

    def log_message(self, *a) -> None:
        pass

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def handle(self) -> None:
        try:
            super().handle()
        except (ConnectionError, TimeoutError, ssl.SSLError, OSError):
            pass

    def reply(self, status: int, body: bytes | str = b"", ctype: str = "text/plain") -> None:
        if isinstance(body, str):
            body = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_CONNECT(self) -> None:
        host, _, port_s = self.path.rpartition(":")
        host = host.strip("[]").lower()
        port = int(port_s or 443)
        st = self.app.state
        if host in INTERCEPT and port == 443:
            self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            self.wfile.flush()
            self.close_connection = True
            try:
                tls = self.app.ctx.wrap_socket(self.connection, server_side=True)
            except (ssl.SSLError, OSError) as e:
                entry = {"host": host, "method": "CONNECT", "event": "tls_handshake_failed"}
                st.log({**entry, "error": str(e)[:120], "mode": st.mode})
                return
            Api(tls, self.client_address, self.server)  # serves until the client closes (keep-alive loop)
            return
        tunnel = {"host": f"{host}:{port}", "method": "CONNECT", "tunnel": True, "mode": st.mode}
        try:
            up = socket.create_connection((host, port), timeout=15)
        except OSError as e:
            st.log({**tunnel, "status": 502, "error": str(e)[:80]})
            return self.reply(502, f"cannot reach {host}:{port}")
        st.log({**tunnel, "status": 200})
        self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
        self.wfile.flush()
        self.close_connection = True
        self.pump(self.connection, up)

    @staticmethod
    def pump(a: socket.socket, b: socket.socket) -> None:
        a.settimeout(None)
        socks = [a, b]
        try:
            while True:
                r, _, x = select.select(socks, [], socks, 120)
                if x or not r:
                    break
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
        except OSError:
            pass
        finally:
            b.close()

    def do_GET(self) -> None:
        url = urlsplit(self.path)
        if url.path == "/__ctl" and not url.netloc:
            return self.control(dict(parse_qsl(url.query)))
        if not url.netloc:
            return self.reply(400, "proxy request expected an absolute URL")
        host = url.hostname or ""
        port = url.port or 80
        st = self.app.state
        hdrs = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        where = {"host": f"{host}:{port}", "method": "GET", "path": url.path, "mode": st.mode}
        try:
            conn = http.client.HTTPConnection(host, port, timeout=30)
            conn.request("GET", (url.path or "/") + (f"?{url.query}" if url.query else ""), headers=hdrs)
            resp = conn.getresponse()
            body = resp.read()
        except (OSError, http.client.HTTPException) as e:
            st.log({**where, "status": 502, "error": str(e)[:80]})
            return self.reply(502, f"cannot reach {host}:{port}")
        self.send_response(resp.status)
        for k, v in resp.getheaders():
            if k.lower() not in HOP and k.lower() != "content-length":
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        st.log({**where, "query": mask_query(parse_qsl(url.query)), "status": resp.status, "forwarded": True})
        conn.close()

    do_HEAD = do_GET

    def control(self, q: dict[str, str]) -> None:
        st = self.app.state
        if q.get("stats"):
            return self.reply(200, json.dumps(st.snapshot()), "application/json")
        if q.get("reset"):
            with st.lock:
                st.stats.clear()
        if q.get("quit"):
            self.reply(200, "bye")
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if "mode" in q:
            if q["mode"] not in MODES:
                return self.reply(400, f"mode must be one of {MODES}")
            st.set_mode(
                q["mode"],
                int(q["n"]) if "n" in q else None,
                float(q["retry_after"]) if "retry_after" in q else None,
                float(q["delay"]) if "delay" in q else None,
            )
        return self.reply(200, json.dumps(st.snapshot()), "application/json")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # on Windows SO_REUSEADDR would let a second copy bind the same port
    request_queue_size = 64


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="fake Pexels and Pixabay behind an intercepting HTTPS proxy")
    ap.add_argument("--dir", required=True, help="folder for certs/, media/, requests.jsonl and ready.json")
    ap.add_argument("--good-key", default="GOODKEY", help="accepted key(s), comma separated")
    args = ap.parse_args(argv)
    folder = Path(args.dir).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    try:
        make_certs(folder)
        make_media(folder)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        sys.stderr.write(f"footage fake could not start: {e}\n")
        return 3
    app = App(folder, {k for k in args.good_key.split(",") if k})
    srv = Server(("127.0.0.1", 0), Proxy)
    srv.app = app  # type: ignore[attr-defined]
    port = int(srv.server_address[1])
    tmp = folder / "ready.json.tmp"
    tmp.write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="ascii")
    tmp.replace(folder / "ready.json")
    sys.stdout.write(f"footage fake listening on 127.0.0.1:{port}\n")
    sys.stdout.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
