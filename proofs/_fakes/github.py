"""Fake GitHub releases for the update proofs (H-15.9): the standard library only, on 127.0.0.1 only.

    python github.py --dir <folder> [--port 0] [--prerelease]

`<folder>` holds what `dev/make_bundle.ps1` makes: `Nightshift-Update-<version>.zip` and, when there is one,
`Nightshift-Update-<version>.zip.sha256`. The fake serves the NEWEST version in the folder as the latest
published release, the way api.github.com does:

    GET /repos/<owner>/<repo>/releases/latest    the release as JSON (404 while the folder has no package)
    GET /download/<file>                         that zip or its .sha256, from the folder

The checksum is the folder's: with a `.sha256` beside the zip the release lists that file and gives no digest
of its own, so a wrong file is served as it is; without one the answer carries `digest: "sha256:<of the zip>"`
computed at that moment. `--prerelease` marks the release a prerelease (the updater must refuse it, H-12).
The folder is read at every request, so a proof can put the next candidate there while the fake runs.

Once it listens it writes `<folder>/ready.json` ({"port": N, "pid": N, "url": "<the latest address>"}) and it
appends one line per request to `<folder>/requests.jsonl`. Stop it by the pid in `ready.json`.

A dev or engine-test install is aimed at it with `NS_RELEASE_SOURCE=<url>` (docs/DEV_MODE.md, "A fake GitHub
for the updater"); every other install refuses that variable with NS-E704 and sends nothing here.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

REPO = "AbdulrahmanAmer/nightshift"
LATEST = re.compile(r"^/repos/[^/]+/[^/]+/releases/latest$")
PACKAGE = re.compile(r"^Nightshift-Update-(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.]+))?\.zip$")
SERVED = re.compile(r"^Nightshift-Update-[0-9A-Za-z.-]+\.zip(\.sha256)?$")


def version_key(name: str) -> tuple[int, int, int, int, str] | None:
    """Sort key of a package name (as `ns_supervisor.releases.parse_version`: a final is above its own
    candidates, rc10 above rc2); None for any other file."""
    m = PACKAGE.match(name)
    if not m:
        return None
    pre = re.sub(r"\d+", lambda d: d.group().zfill(9), m.group(4) or "")
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), 0 if pre else 1, pre


def newest(folder: Path) -> Path | None:
    """The package with the highest version in the folder."""
    try:
        found = [p for p in folder.iterdir() if p.is_file() and version_key(p.name)]
    except OSError:
        return None
    return max(found, key=lambda p: version_key(p.name) or (), default=None)


def release(folder: Path, base: str, prerelease: bool = False) -> dict[str, object] | None:
    """The JSON of `/releases/latest` for the folder; `base` is `http://<host>:<port>` as the client put it"""
    zip_path = newest(folder)
    if zip_path is None:
        return None
    version = zip_path.name[len("Nightshift-Update-") : -len(".zip")]
    asset: dict[str, object] = {
        "name": zip_path.name,
        "browser_download_url": f"{base}/download/{zip_path.name}",
        "size": zip_path.stat().st_size,
    }
    assets = [asset]
    sums = zip_path.with_name(zip_path.name + ".sha256")
    if sums.is_file():
        assets.append({"name": sums.name, "browser_download_url": f"{base}/download/{sums.name}"})
    else:
        with zip_path.open("rb") as fh:
            asset["digest"] = "sha256:" + hashlib.file_digest(fh, "sha256").hexdigest()
    return {
        "tag_name": f"v{version}",
        "name": f"Nightshift {version}",
        "draft": False,
        "prerelease": prerelease,
        "html_url": f"{base}/releases/tag/v{version}",
        "assets": assets,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def fake(self) -> Server:
        assert isinstance(self.server, Server)
        return self.server

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002  (the stdlib's own name)
        return  # the stdlib access log is off; requests.jsonl has the lines

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self) -> None:
        self._send(404, b'{"message": "Not Found"}', "application/json")

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        self.fake.note("GET", path)
        if LATEST.match(path):
            host = self.headers.get("Host") or f"127.0.0.1:{self.fake.server_address[1]}"
            data = release(self.fake.folder, f"http://{host}", self.fake.prerelease)
            if data is None:
                return self._not_found()
            return self._send(200, json.dumps(data).encode("utf-8"), "application/json")
        name = unquote(path[len("/download/") :]) if path.startswith("/download/") else ""
        target = self.fake.folder / name if SERVED.match(name) else None
        if target is None or not target.is_file():
            return self._not_found()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()
        with target.open("rb") as fh:
            while chunk := fh.read(1024 * 1024):
                self.wfile.write(chunk)
        return None


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # on Windows SO_REUSEADDR would let a second copy bind the same port
    folder: Path
    prerelease: bool
    seen: list[tuple[str, str]]
    _lock: threading.Lock

    def note(self, method: str, path: str) -> None:
        """Remember a request (`seen`) and append it to `requests.jsonl`."""
        with self._lock:
            self.seen.append((method, path))
            try:
                with (self.folder / "requests.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"method": method, "path": path}) + "\n")
            except OSError:
                pass  # the folder is being emptied by the proof: the list in memory still has the request


def serve(folder: Path, port: int = 0, prerelease: bool = False) -> Server:
    """A server for `folder`, bound to 127.0.0.1 (port 0: the system picks one) and not yet serving: call
    `serve_forever()` in a thread, and `shutdown()` and `server_close()` when done."""
    srv = Server(("127.0.0.1", port), Handler)
    srv.folder = Path(folder).resolve()
    srv.prerelease = prerelease
    srv.seen = []
    srv._lock = threading.Lock()
    return srv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="fake GitHub releases on 127.0.0.1 for the update proofs")
    ap.add_argument("--dir", required=True, help="folder with Nightshift-Update-<version>.zip, its .sha256")
    ap.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: chosen by the system)")
    ap.add_argument("--prerelease", action="store_true", help="mark the release a prerelease")
    args = ap.parse_args(argv)
    folder = Path(args.dir).resolve()
    if not folder.is_dir():
        sys.stderr.write(f"github fake could not start: {folder} is not a folder\n")
        return 3
    try:
        srv = serve(folder, args.port, args.prerelease)
    except OSError as e:
        sys.stderr.write(f"github fake could not start: {e}\n")
        return 3
    port = int(srv.server_address[1])
    url = f"http://127.0.0.1:{port}/repos/{REPO}/releases/latest"
    tmp = folder / "ready.json.tmp"
    tmp.write_text(json.dumps({"port": port, "pid": os.getpid(), "url": url}), encoding="ascii")
    tmp.replace(folder / "ready.json")
    sys.stdout.write(f"github fake listening: {url}\n")
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
