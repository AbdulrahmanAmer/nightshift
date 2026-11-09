"""Install the pinned FFmpeg for a CI job, and prove it runs before any test starts (H-9).

``choco install ffmpeg`` once got an HTTP 504 from the Chocolatey feed, printed "Chocolatey installed 0/0
packages", exited 0, and a dozen render tests then failed with NS-E401 as if the code were wrong. This script
is the one place that installs FFmpeg for ci.yml and release.yml (through the composite action
``.github/actions/install-ffmpeg``), and it cannot do that silently::

    python dev/ci_install_ffmpeg.py pin       # key / dir / version for the cache steps ($GITHUB_OUTPUT)
    python dev/ci_install_ffmpeg.py install   # the install itself; exit 1 and a plain message on failure

What ``install`` does, in order:

1. The folder the Actions cache restored (``dir`` of ``pin``) is used when ``ffmpeg -version`` and
   ``ffprobe -version`` both run from it.
2. Otherwise the pinned build is downloaded: the one the product itself falls back to (``manifest.json``
   ``bins.ffmpeg``: url, sha256 and, if the manifest gives them, bytes). It is verified against the
   manifest and only ``ffmpeg.exe`` and ``ffprobe.exe`` are unpacked. The retry rules are those of
   ``tests/online.py``: a transient failure (HTTP 5xx or 429, a timeout, a reset or refused connection) is
   retried 3 times after 1, 2 and 4 s; 404, 403, a wrong size and a wrong hash fail at once, untouched.
3. Only when that fails, Chocolatey is the second source (not after a wrong hash or size: that is a pin
   to fix, or a file to distrust, not an outage). Its exit code means nothing: the last step decides.
4. The step ends with ``ffmpeg -version`` and ``ffprobe -version`` on the programs the next steps will
   find. If either is missing or does not run, the step fails with
   ``FFmpeg could not be installed: <source> answered <status>`` (one clause per source tried) and, when
   the pinned download failed for a transient reason, with ``upstream outage: ...`` and the advice to
   rerun. The same one-line reason goes to the job summary. A missing FFmpeg never reaches pytest.

On success the folder is appended to ``$GITHUB_PATH`` (cache and download) and the source is written to
``$GITHUB_OUTPUT`` as ``source=cache|download|chocolatey``; the action saves the cache only after a
``download``.

Standard library only (no import of ``tests/`` or of the product): ``tests/test_ci_install_ffmpeg.py``
shows that the retry rules equal the ones in ``tests/online.py``. The log is bounded (a few lines per
attempt, the last 20 lines of Chocolatey's output).
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.client
import json
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "manifest.json"

PROGRAMS = ("ffmpeg.exe", "ffprobe.exe")
TOOLS = ("ffmpeg", "ffprobe")

#: Same numbers as tests/online.py (RETRIES, BACKOFF_S, OUTAGE_MARK); the test file holds them equal.
RETRIES = 3
BACKOFF_S = (1.0, 2.0, 4.0)
OUTAGE_MARK = "upstream outage"

#: Bump to start a new cache line (for instance when a restored folder is broken and cannot be deleted).
CACHE_GENERATION = 1

REQUEST_TIMEOUT_S = 60.0
VERSION_TIMEOUT_S = 60.0
CHOCO_TIMEOUT_S = 600.0
CHUNK = 1 << 20
TAIL_LINES = 20
USER_AGENT = "nightshift-ci-install-ffmpeg"
DISK = "the runner disk"


class SourceError(Exception):
    """One source of FFmpeg failed. The text is ``<source> answered <status>``.

    ``transient``: the failure was an upstream outage (still failing after every retry).
    ``integrity``: the download arrived but is not the pinned file (wrong hash or size)."""

    def __init__(self, source: str, status: str, *, transient: bool = False, integrity: bool = False) -> None:
        self.source = source
        self.status = status
        self.transient = transient
        self.integrity = integrity
        super().__init__(f"{source} answered {status}")


class PinError(Exception):
    """``manifest.json`` has no usable ``bins.ffmpeg`` entry."""


# ---- the pin ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Pin:
    version: str
    url: str
    sha256: str
    size: int | None = None

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or self.url


def load_pin(manifest: Path = MANIFEST) -> Pin:
    """``bins.ffmpeg`` of the manifest: the build the product downloads when winget cannot install FFmpeg."""
    try:
        entry = json.loads(manifest.read_text(encoding="utf-8"))["bins"]["ffmpeg"]
        version, url, digest = str(entry["version"]), str(entry["url"]), str(entry["sha256"]).lower()
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise PinError(f"{manifest} has no usable bins.ffmpeg pin ({type(e).__name__}: {e})") from e
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", version):
        raise PinError(f"bins.ffmpeg.version {version!r} is not a plain version string")
    if not url.startswith("https://"):
        raise PinError(f"bins.ffmpeg.url {url!r} is not an https URL")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PinError("bins.ffmpeg.sha256 is not 64 hex digits")
    size = entry.get("bytes")
    return Pin(version, url, digest, size if isinstance(size, int) and size > 0 else None)


def cache_folder(pin: Pin, env: Mapping[str, str]) -> Path:
    """Where the unpacked programs live, and what the Actions cache stores."""
    return Path(env.get("RUNNER_TEMP") or tempfile.gettempdir()) / "ns-ffmpeg" / pin.version


def cache_key(pin: Pin, env: Mapping[str, str]) -> str:
    """Keyed on the pinned version and the runner OS; the first digits of the hash keep a changed file from
    answering to an old key that has the same version string."""
    runner_os = env.get("RUNNER_OS") or platform.system()
    arch = env.get("RUNNER_ARCH") or platform.machine()
    return f"ns-ffmpeg-g{CACHE_GENERATION}-{runner_os}-{arch}-{pin.version}-{pin.sha256[:12]}"


# ---- the retry rules of tests/online.py -------------------------------------------------------------------
def is_transient_status(code: int) -> bool:
    return code == 429 or 500 <= code <= 599


def _causes(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    todo: list[BaseException] = [exc]
    while todo:
        cur = todo.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        if isinstance(cur, urllib.error.URLError) and isinstance(cur.reason, BaseException):
            todo.append(cur.reason)
        for nxt in (cur.__cause__, cur.__context__):
            if nxt is not None:
                todo.append(nxt)


def transient_detail(exc: BaseException) -> str | None:
    """What to say about ``exc`` when it is a transient failure; None when it is not (it is a wrong pin)."""
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}" if is_transient_status(exc.code) else None
    chain = list(_causes(exc))
    if any(isinstance(c, socket.gaierror) for c in chain):
        return None  # a host name that does not resolve is a wrong pin, not an outage
    if any(isinstance(c, ssl.SSLCertVerificationError) for c in chain):
        return None  # a certificate that does not verify is not an outage either
    for c in chain:
        if isinstance(c, OSError | http.client.HTTPException) and not isinstance(c, urllib.error.URLError):
            return f"{type(c).__name__}: {c}".rstrip(": ")
    return None


def _describe(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"HTTP {exc.code}"
    if any(isinstance(c, socket.gaierror) for c in _causes(exc)):
        return f"no such host ({exc})"
    return f"{type(exc).__name__}: {exc}".rstrip(": ")


def _close(exc: BaseException) -> None:
    """An HTTPError holds the response's socket open; let it go."""
    if isinstance(exc, urllib.error.HTTPError):
        with contextlib.suppress(Exception):  # nothing useful to do if closing a failed response fails
            exc.close()


def retry[T](
    fn: Callable[[], T],
    *,
    source: str,
    retries: int = RETRIES,
    backoff: Sequence[float] = BACKOFF_S,
    sleep: Callable[[float], None] = time.sleep,
    say: Callable[[str], None] = print,
) -> T:
    """Run ``fn``; retry a transient failure ``retries`` times; raise ``SourceError(transient=True)`` if it
    lasts. Anything else becomes a ``SourceError`` at once (one from ``fn`` itself passes unchanged)."""
    attempts = retries + 1
    last = ""
    for n in range(attempts):
        try:
            return fn()
        except SourceError:
            raise
        except Exception as e:
            detail = transient_detail(e)
            _close(e)
            if detail is None:
                raise SourceError(source, _describe(e)) from e
            last = detail
        if n < attempts - 1:
            wait = backoff[min(n, len(backoff) - 1)]
            say(f"{source}: {last}; attempt {n + 1} of {attempts} failed, trying again in {wait:g} s")
            sleep(wait)
    raise SourceError(source, f"{last} after {attempts} attempts", transient=True)


# ---- the programs that run the checks (all replaceable in tests) -------------------------------------------
def open_url(url: str, timeout: float) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return urllib.request.urlopen(request, timeout=timeout)


def run_version(exe: str) -> tuple[int, str]:
    """``exe -version`` as (exit code, first output line). Never raises: a program that cannot start is -1."""
    try:
        done = subprocess.run(
            [exe, "-version"],
            capture_output=True,
            text=True,
            timeout=VERSION_TIMEOUT_S,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return -1, f"no answer within {VERSION_TIMEOUT_S:g} s"
    except OSError as e:
        return -1, str(e)
    text = (done.stdout or done.stderr or "").strip()
    return done.returncode, text.splitlines()[0].strip() if text else ""


def run_choco() -> tuple[int | None, str]:
    """``choco install ffmpeg``: (exit code, its output); the code is None when Chocolatey is not there."""
    exe = shutil.which("choco")
    if exe is None:
        return None, ""
    try:
        done = subprocess.run(
            [exe, "install", "ffmpeg", "-y", "--no-progress"],
            capture_output=True,
            text=True,
            timeout=CHOCO_TIMEOUT_S,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return -1, f"no answer within {CHOCO_TIMEOUT_S:g} s"
    except OSError as e:
        return -1, str(e)
    return done.returncode, f"{done.stdout or ''}\n{done.stderr or ''}"


def default_which(name: str) -> str | None:
    return shutil.which(name)


def default_say(text: str) -> None:
    print(text, flush=True)


@dataclass
class Deps:
    """Everything the install touches outside its own folder; a test replaces what it needs."""

    opener: Callable[[str, float], Any] = open_url
    which: Callable[[str], str | None] = default_which
    run: Callable[[str], tuple[int, str]] = run_version
    choco: Callable[[], tuple[int | None, str]] = run_choco
    sleep: Callable[[float], None] = time.sleep
    say: Callable[[str], None] = default_say
    env: Mapping[str, str] = field(default_factory=lambda: os.environ)


# ---- GitHub's files ---------------------------------------------------------------------------------------
def _append(name: str, text: str, deps: Deps) -> bool:
    target = deps.env.get(name)
    if not target:
        return False
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(text)
    return True


def write_output(key: str, value: str, deps: Deps) -> None:
    _append("GITHUB_OUTPUT", f"{key}={value}\n", deps)


def write_summary(line: str, deps: Deps) -> None:
    _append("GITHUB_STEP_SUMMARY", f"### FFmpeg\n\n{line}\n\n", deps)


# ---- download and unpack ----------------------------------------------------------------------------------
def _content_length(resp: Any) -> int | None:
    value = resp.headers.get("Content-Length") if getattr(resp, "headers", None) is not None else None
    return int(value) if isinstance(value, str) and value.isdigit() else None


def download_once(pin: Pin, dest: Path, deps: Deps) -> None:
    """One attempt: the zip into ``dest``, its size and sha256 checked against the pin. Transient trouble
    raises as it happens (``retry`` classifies it); a file that is not the pinned one is ``integrity``."""
    digest = hashlib.sha256()
    total = 0
    try:
        out = dest.open("wb")
    except OSError as e:
        raise SourceError(DISK, f"could not write {dest} ({e})") from e
    with out, deps.opener(pin.url, REQUEST_TIMEOUT_S) as resp:
        status = getattr(resp, "status", 200)
        expected = _content_length(resp)
        while chunk := resp.read(CHUNK):
            digest.update(chunk)
            total += len(chunk)
            try:
                out.write(chunk)
            except OSError as e:
                raise SourceError(DISK, f"could not write {dest} ({e})") from e
    if expected is not None and total != expected:
        raise ConnectionResetError(f"the connection closed after {total} of {expected} bytes")
    if pin.size is not None and total != pin.size:
        raise SourceError(
            pin.host, f"HTTP {status} with {total} bytes, the manifest pins {pin.size}", integrity=True
        )
    got = digest.hexdigest()
    if got != pin.sha256:
        wrong = f"got {got[:12]}..., the manifest pins {pin.sha256[:12]}..."
        raise SourceError(pin.host, f"HTTP {status} with the wrong sha256 ({wrong})", integrity=True)
    deps.say(f"downloaded {total} bytes from {pin.host}; the sha256 matches the manifest ({got[:12]}...)")


def unpack(archive: Path, folder: Path, source: str) -> list[str]:
    """Write ``ffmpeg.exe`` and ``ffprobe.exe`` (from a ``bin`` folder) of the zip into ``folder``, and
    nothing else. Same rule as ``unpack_ffmpeg`` in ``installer/stage2/web/bootstrap.py``."""
    folder.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    try:
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                parts = PurePosixPath(info.filename).parts
                leaf = parts[-1].lower() if parts else ""
                if info.is_dir() or leaf not in PROGRAMS or leaf in written:
                    continue
                if len(parts) < 2 or parts[-2].lower() != "bin":
                    continue
                part = folder / f"{leaf}.part"
                with z.open(info) as src, part.open("wb") as dst:
                    shutil.copyfileobj(src, dst, CHUNK)
                os.replace(part, folder / leaf)
                written.append(leaf)
    except zipfile.BadZipFile as e:
        raise SourceError(source, f"HTTP 200 and the pinned sha256, but the zip cannot be read ({e})") from e
    except OSError as e:
        raise SourceError(DISK, f"could not unpack into {folder} ({e})") from e
    missing = [p for p in PROGRAMS if p not in written]
    if missing:
        raise SourceError(
            source, f"HTTP 200 and the pinned sha256, but no {' or '.join(missing)} in a bin folder"
        )
    return written


# ---- the proof --------------------------------------------------------------------------------------------
@dataclass
class Check:
    ok: bool
    problems: list[str]
    banners: dict[str, str]


def verify(folder: Path | None, deps: Deps) -> Check:
    """Run ``ffmpeg -version`` and ``ffprobe -version`` on the programs the next steps will find: those in
    ``folder`` (which goes on PATH), or those on PATH when ``folder`` is None."""
    problems: list[str] = []
    banners: dict[str, str] = {}
    for tool in TOOLS:
        if folder is not None:
            candidate = folder / f"{tool}.exe"
            exe = str(candidate) if candidate.is_file() else None
            where = f"in {folder}"
        else:
            exe = deps.which(tool)
            where = "on PATH"
        if exe is None:
            problems.append(f"{tool} is not {where}")
            continue
        code, line = deps.run(exe)
        if code != 0 or not line.lower().startswith(f"{tool} version"):
            problems.append(f"{tool} -version did not run (exit code {code}: {line or 'no output'})")
            continue
        banners[tool] = line
    return Check(not problems, problems, banners)


# ---- Chocolatey, the second source ------------------------------------------------------------------------
_CHOCO_SUMMARY = re.compile(r"Chocolatey installed \d+/\d+ packages?\.?", re.IGNORECASE)
_CHOCO_TROUBLE = re.compile(
    r"\(\s*[1-5]\d\d\s*\)|timed out|unable to connect|could not resolve|error", re.IGNORECASE
)


def choco_status(code: int | None, output: str) -> str:
    """What Chocolatey said, for the message: its exit code, its own count, the first line naming trouble."""
    if code is None:
        return "nothing (Chocolatey is not installed on this runner)"
    bits = [f"exit code {code}"]
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    summary = [ln for ln in lines if _CHOCO_SUMMARY.search(ln)]
    if summary:
        bits.append(f'"{summary[-1][:80]}"')
    trouble = [ln for ln in lines if _CHOCO_TROUBLE.search(ln) and not _CHOCO_SUMMARY.search(ln)]
    if trouble:
        bits.append(f'and "{trouble[0][:140]}"')
    return " ".join(bits)


# ---- the install ------------------------------------------------------------------------------------------
@dataclass
class Result:
    ok: bool
    source: str = ""
    message: str = ""
    advice: str = ""
    outage: bool = False
    banners: dict[str, str] = field(default_factory=dict)


def _success(pin: Pin, source: str, folder: Path | None, check: Check, deps: Deps) -> Result:
    say = deps.say
    banner = check.banners.get("ffmpeg", "")
    for tool in TOOLS:
        say(check.banners[tool])
    if source == "chocolatey":
        say(
            f"::warning::FFmpeg comes from Chocolatey, not from the pinned build {pin.version}: "
            "the pinned download failed (see above). Tests ran on whatever version the feed served."
        )
    elif pin.version not in banner:
        say(f"::warning::ffmpeg reports '{banner}', which does not contain the pinned version {pin.version}")
    if folder is not None and _append("GITHUB_PATH", f"{folder}\n", deps):
        say(f"{folder} is on PATH for the next steps")
    write_output("source", source, deps)
    write_summary(f"FFmpeg ready from {source}: `{banner}` (pin {pin.version})", deps)
    return Result(True, source=source, banners=check.banners)


def _failure(failures: list[SourceError], deps: Deps) -> Result:
    message = "FFmpeg could not be installed: " + "; ".join(
        f"{f.source} answered {f.status}" for f in failures
    )
    first = failures[0]
    outage = first.transient
    if outage:
        advice = (
            f"{OUTAGE_MARK}: {first.source} {first.status}. This is not a failure of the code and no test "
            "was run: rerun the failed jobs."
        )
        lead = f"**FFmpeg install failed, {OUTAGE_MARK}: rerun the failed jobs.**"
    else:
        advice = (
            "Not an outage: the pinned build in manifest.json (bins.ffmpeg) could not be used. "
            "Rerunning will not help until the pin is fixed."
        )
        lead = "**FFmpeg install failed, not an outage: the pin or the file is wrong.**"
    deps.say(f"::error::{message}")
    deps.say(advice)
    write_output("source", "none", deps)
    write_summary(f"{lead} {message}", deps)
    return Result(False, message=message, advice=advice, outage=outage)


def install(pin: Pin, folder: Path, deps: Deps | None = None) -> Result:
    deps = deps or Deps()
    say = deps.say
    say(f"FFmpeg pin: {pin.version} from {pin.host}; folder {folder}")

    # 1. what the Actions cache restored
    if all((folder / p).is_file() for p in PROGRAMS):
        check = verify(folder, deps)
        if check.ok:
            return _success(pin, "cache", folder, check, deps)
        say(
            "::warning::the FFmpeg restored from the cache does not run "
            f"({'; '.join(check.problems)}); downloading the pinned build instead"
        )
    shutil.rmtree(folder, ignore_errors=True)

    # 2. the pinned download
    failures: list[SourceError] = []
    try:
        scratch_root = Path(deps.env.get("RUNNER_TEMP") or tempfile.gettempdir())
        scratch_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ns-ffmpeg-dl-", dir=scratch_root) as tmp:
            archive = Path(tmp) / "ffmpeg.zip"
            say(f"downloading {pin.url}")
            retry(
                lambda: download_once(pin, archive, deps),
                source=pin.host,
                sleep=deps.sleep,
                say=say,
            )
            unpack(archive, folder, pin.host)
        check = verify(folder, deps)
        if not check.ok:
            raise SourceError(pin.host, "HTTP 200 and the pinned sha256, but " + " and ".join(check.problems))
        return _success(pin, "download", folder, check, deps)
    except SourceError as e:
        failures.append(e)
        shutil.rmtree(folder, ignore_errors=True)
        say(f"::warning::the pinned FFmpeg could not be installed: {e}")
        if e.integrity:
            say("Chocolatey is not tried after a wrong hash or size: that is a pin to fix, not an outage.")
            return _failure(failures, deps)

    # 3. Chocolatey, the second source. Its exit code is not believed: the proof below decides.
    say("trying Chocolatey as the second source (its exit code proves nothing; ffmpeg -version decides)")
    code, output = deps.choco()
    for line in [ln.rstrip() for ln in output.splitlines() if ln.strip()][-TAIL_LINES:]:
        say(f"  choco: {line[:200]}")
    check = verify(None, deps)
    if check.ok:
        return _success(pin, "chocolatey", None, check, deps)
    status = f"{choco_status(code, output)}, but afterwards {' and '.join(check.problems)}"
    failures.append(SourceError("chocolatey", status))
    return _failure(failures, deps)


# ---- command line -----------------------------------------------------------------------------------------
def main(argv: Sequence[str] | None = None, deps: Deps | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install the pinned FFmpeg for a CI job.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (
        ("pin", "write key, dir and version of the pinned build to $GITHUB_OUTPUT"),
        ("install", "install FFmpeg and prove it runs"),
    ):
        p = sub.add_parser(name, help=text)
        p.add_argument("--manifest", type=Path, default=MANIFEST)
        p.add_argument(
            "--dir", type=Path, default=None, help="the folder for the programs (default: under RUNNER_TEMP)"
        )
    args = parser.parse_args(sys.argv[1:] if argv is None else list(argv))
    deps = deps or Deps()
    try:
        pin = load_pin(args.manifest)
    except PinError as e:
        message = f"FFmpeg could not be installed: {e}"
        deps.say(f"::error::{message}")
        write_summary(f"**FFmpeg install failed, not an outage: the pin is unusable.** {message}", deps)
        return 1
    folder: Path = args.dir or cache_folder(pin, deps.env)
    if args.command == "pin":
        values = {"version": pin.version, "key": cache_key(pin, deps.env), "dir": str(folder)}
        for key, value in values.items():
            deps.say(f"{key}={value}")
            write_output(key, value, deps)
        return 0
    try:
        return 0 if install(pin, folder, deps).ok else 1
    except Exception as e:  # a bug in this script must still fail the step in plain words, never reach pytest
        message = f"FFmpeg could not be installed: this script failed ({type(e).__name__}: {e})"
        deps.say(f"::error::{message}")
        write_summary(f"**FFmpeg install failed, not an outage: the install script broke.** {message}", deps)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
