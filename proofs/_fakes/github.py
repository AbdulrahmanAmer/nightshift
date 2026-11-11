"""Fake GitHub releases for the update proofs (H-15.9): the standard library only, on 127.0.0.1 only.

    python github.py --dir <folder> [--port 0] [--prerelease] [--key-file <file> | --unsigned]

`<folder>` holds what `dev/make_bundle.ps1` makes: `Nightshift-Update-<version>.zip` and, when there is one,
`Nightshift-Update-<version>.zip.sha256`. The fake serves the NEWEST version in the folder as the latest
published release, the way api.github.com does:

    GET /repos/<owner>/<repo>/releases/latest    the release as JSON (404 while the folder has no package)
    GET /download/<file>                         that zip, its .sha256 or its .sig, from the folder

Who made it (card update-signing): the fake is the release run of the proofs, so it can sign, with a
THROWAWAY key the proof or test made for this run and hands over with `--key-file <file>` (a file outside the
repository: base64 of 32 bytes on its first line that is not a `#` comment). No key lives in the repository
and the product trusts no test key: the program under test must itself pin that key's public half
(`ns_common/release_keys.py` `TRUSTED` of the build the proof installs, or a test's patch of it). With a key,
before it answers `/releases/latest` it writes `<zip>.sig` for each package that has
none, ONCE per file name for as long as it runs: a zip that is changed afterwards keeps the signature of the
old bytes (a forged release: refused, "does not match"), and a `.sig` that is deleted is not made again (a
release without one: refused, "is missing"). Without `--key-file` it signs nothing and says so when it starts
(`--unsigned` states that on purpose: a release from before the key).

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
import base64
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
SERVED = re.compile(r"^Nightshift-Update-[0-9A-Za-z.-]+\.zip(\.sha256|\.sig)?$")
ROOT = Path(__file__).resolve().parents[2]


def key_seed(key_file: Path) -> bytes:
    """The 32 bytes of the throwaway key in `key_file`. `ValueError` or `OSError` when the file is not there
    or holds no key."""
    lines = [ln.strip() for ln in Path(key_file).read_text(encoding="ascii").splitlines()]
    keys = [ln for ln in lines if ln and not ln.startswith("#")]
    seed = base64.b64decode(keys[0], validate=True) if len(keys) == 1 else b""
    if len(seed) != 32:
        raise ValueError(f"{key_file} does not hold one key of 32 bytes")
    return seed


def sign_with(seed: bytes, zip_path: Path) -> Path:
    """Write `<zip>.sig` made with the private key `seed` (whole, or not at all) and return it. `ValueError`
    or `OSError` when the zip is not a readable package (one that is still being copied into the folder)."""
    if str(ROOT) not in sys.path:  # started as a script: the checkout this file is in
        sys.path.insert(0, str(ROOT))
    from ns_common import sigcheck  # the standard library only

    version, _prefix = sigcheck.package_version(zip_path)
    signed = sigcheck.message(zip_path.name, version, sigcheck.sha256_file(zip_path))
    text = sigcheck.sig_text(sigcheck.public_key(seed), sigcheck.sign(seed, signed))
    sig = sigcheck.sig_path(zip_path)
    tmp = sig.with_name(sig.name + ".tmp")
    tmp.write_bytes(text.encode("ascii"))
    tmp.replace(sig)
    return sig


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
    sig = zip_path.with_name(zip_path.name + ".sig")
    if sig.is_file():
        assets.append({"name": sig.name, "browser_download_url": f"{base}/download/{sig.name}"})
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
            self.fake.sign_new()
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
    seed: bytes | None
    signed: set[str]
    _lock: threading.Lock
    _sign_lock: threading.Lock

    def sign_new(self) -> None:
        """With a key (`seed`): `<zip>.sig` for each package of the folder that this run has not
        met yet and that has none. A file name is met once: a zip changed later keeps the signature of the old
        bytes, and a `.sig` deleted later stays away (the module text says what each proves). A zip that
        cannot be read yet (still being copied in) is met again at the next request."""
        seed = self.seed
        if seed is None:
            return
        with self._sign_lock:
            try:
                found = sorted(p for p in self.folder.iterdir() if p.is_file() and version_key(p.name))
            except OSError:
                return
            for zip_path in found:
                if zip_path.name in self.signed:
                    continue
                self.signed.add(zip_path.name)
                if zip_path.with_name(zip_path.name + ".sig").exists():
                    continue
                try:
                    sign_with(seed, zip_path)
                except (OSError, ValueError):
                    self.signed.discard(zip_path.name)

    def note(self, method: str, path: str) -> None:
        """Remember a request (`seen`) and append it to `requests.jsonl`."""
        with self._lock:
            self.seen.append((method, path))
            try:
                with (self.folder / "requests.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"method": method, "path": path}) + "\n")
            except OSError:
                pass  # the folder is being emptied by the proof: the list in memory still has the request


def serve(folder: Path, port: int = 0, prerelease: bool = False, seed: bytes | None = None) -> Server:
    """A server for `folder`, bound to 127.0.0.1 (port 0: the system picks one) and not yet serving: call
    `serve_forever()` in a thread, and `shutdown()` and `server_close()` when done. `seed` is the throwaway
    private key it signs with (32 bytes, the caller's own); None is `--unsigned`: no `.sig` is made
    (`Server.sign_new`)."""
    srv = Server(("127.0.0.1", port), Handler)
    srv.folder = Path(folder).resolve()
    srv.prerelease = prerelease
    srv.seen = []
    srv.seed = seed
    srv.signed = set()
    srv._lock = threading.Lock()
    srv._sign_lock = threading.Lock()
    return srv


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="fake GitHub releases on 127.0.0.1 for the update proofs")
    ap.add_argument("--dir", required=True, help="folder with Nightshift-Update-<version>.zip, its .sha256")
    ap.add_argument("--port", type=int, default=0, help="port on 127.0.0.1 (default: chosen by the system)")
    ap.add_argument("--prerelease", action="store_true", help="mark the release a prerelease")
    how = ap.add_mutually_exclusive_group()
    how.add_argument("--key-file", type=Path, help="sign with the throwaway private key in this file")
    how.add_argument("--unsigned", action="store_true", help="make no .sig (a release from before the key)")
    args = ap.parse_args(argv)
    folder = Path(args.dir).resolve()
    if not folder.is_dir():
        sys.stderr.write(f"github fake could not start: {folder} is not a folder\n")
        return 3
    seed: bytes | None = None
    if args.key_file is not None:  # told to sign and unable to: it would serve unsigned releases, silently
        try:
            seed = key_seed(args.key_file)
        except (OSError, ValueError) as e:
            sys.stderr.write(f"github fake could not start: the key file cannot be read ({e})\n")
            return 3
    elif not args.unsigned:
        sys.stderr.write("github fake: no --key-file, so its releases carry no .sig (as with --unsigned)\n")
    try:
        srv = serve(folder, args.port, args.prerelease, seed=seed)
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
