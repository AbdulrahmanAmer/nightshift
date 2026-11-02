"""Resumable, verified downloads for the installer (component C1, spec 21.7).

Every download: HEAD for the size, stream to ``<file>.part`` with HTTP ``Range`` resume, SHA256 verify
(``NS-E109``), atomic rename, three attempts with exponential backoff, two files at a time.
``models_source`` folder mode copies files whose name and SHA256 match instead of downloading them.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import zipfile
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from installer.stage2.errors import make_error
from ns_common.errors import NsError
from ns_common.models import HfFile, LlmCustom, Manifest
from ns_common.paths import Paths

HF = "https://huggingface.co"
CHUNK = 1 << 20                  # hashing and copying
STREAM_CHUNK = 1 << 16           # download loop: the pause switch is checked after every 64 KiB
STALL_TIMEOUT_S = 60.0           # no bytes for this long aborts the attempt (network error, retried)
ProgressFn = Callable[[int, int | None], None]
RetryFn = Callable[[int, str, float], None]
Tier = Literal["small", "balanced", "best"]

# Whisper (CTranslate2) side files next to model.bin. The manifest pins only model.bin.
WHISPER_REQUIRED = ("config.json", "tokenizer.json")
WHISPER_OPTIONAL = ("preprocessor_config.json", "vocabulary.json", "vocabulary.txt")


@dataclass(frozen=True)
class DownloadItem:
    """One file to fetch. ``kind="zip"`` items are extracted into ``extract_to`` and ``expect`` is checked."""

    name: str
    url: str
    dest: Path
    sha256: str | None = None
    size: int | None = None
    kind: Literal["file", "zip", "nupkg"] = "file"
    extract_to: Path | None = None
    expect: tuple[str, ...] = ()
    optional: bool = False
    # kind="nupkg": (file name written into ``extract_to``, candidate archive paths in preference order).
    members: tuple[tuple[str, tuple[str, ...]], ...] = ()


@dataclass
class PlanResult:
    """What ``run_plan`` did, by item name."""

    downloaded: list[str] = field(default_factory=list)
    copied: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)
    failed: dict[str, NsError] = field(default_factory=dict)


class Reporter(Protocol):
    """Receives progress for the overall and per-file bars (rich implementation lives in ``ui``)."""

    def start(self, name: str, total: int | None) -> None:
        """A file started."""

    def update(self, name: str, done: int, total: int | None) -> None:
        """Bytes done so far for a file."""

    def finish(self, name: str, outcome: str) -> None:
        """A file ended with outcome downloaded, copied, skipped, missing or failed."""

    # Optional: ``retrying(name, attempt, reason, wait_s)`` is called before a retry wait (reason stall,
    # network or hash). A reporter without it simply is not told.


# --------------------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------------------
def hf_url(repo: str, revision: str, file: str) -> str:
    """Hugging Face file URL: https://huggingface.co/<repo>/resolve/<revision>/<file>."""
    return f"{HF}/{repo}/resolve/{revision}/{file}"


def sha256_file(path: Path, chunk: int = CHUNK) -> str:
    """Hex SHA256 of a file."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                return h.hexdigest()
            h.update(block)


def _part(dest: Path) -> Path:
    return dest.with_name(dest.name + ".part")


def _marker(dest: Path) -> Path:
    return dest.with_name(dest.name + ".ok")


def write_marker(dest: Path, digest: str | None) -> None:
    """Record that ``dest`` was verified (size + mtime), so reruns skip it without rehashing."""
    st = dest.stat()
    _marker(dest).write_text(
        json.dumps({"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": digest}), encoding="utf-8"
    )


def is_verified(dest: Path, sha256: str | None) -> bool:
    """True when ``dest`` exists and its marker matches size, mtime and the expected hash."""
    try:
        st = dest.stat()
        marker = json.loads(_marker(dest).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if marker.get("size") != st.st_size or marker.get("mtime_ns") != st.st_mtime_ns:
        return False
    if sha256 is not None and (marker.get("sha256") or "").lower() != sha256.lower():
        return False
    return True


def human_bytes(n: float) -> str:
    """Readable size such as ``9.0 GB``."""
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def eta_text(total_bytes: int, mbit_per_s: float = 50.0) -> str:
    """Rough download time at an assumed line speed, for the pre-download message."""
    seconds = total_bytes * 8 / (mbit_per_s * 1_000_000)
    if seconds < 90:
        return f"{seconds:.0f} seconds"
    if seconds < 5400:
        return f"{seconds / 60:.0f} minutes"
    return f"{seconds / 3600:.1f} hours"


def _new_client() -> httpx.Client:
    return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=STALL_TIMEOUT_S))


# --------------------------------------------------------------------------------------------------------
# single file
# --------------------------------------------------------------------------------------------------------
def head_info(http: httpx.Client, url: str) -> tuple[int, int | None]:
    """HEAD ``url``: ``(status, size in bytes)``; the size is None when the server does not say.

    Hugging Face answers a ``resolve`` URL with a redirect whose ``x-linked-size`` header is the real file
    size, so redirects are not followed when that header is there. Raises ``httpx.HTTPError`` when the site
    cannot be reached. ``s03`` and the custom-model check of the setup window share this logic.
    """
    r = http.head(url, follow_redirects=False)
    if r.is_redirect:
        linked = r.headers.get("x-linked-size", "")
        if linked.isdigit():
            return 200, int(linked)
        r = http.head(url, follow_redirects=True)
    length = r.headers.get("content-length", "")
    return r.status_code, int(length) if length.isdigit() else None


def _fetch_once(
    http: httpx.Client,
    url: str,
    dest: Path,
    sha256: str | None,
    *,
    resume: bool,
    progress: ProgressFn | None,
    size: int | None,
    chunk: int,
    gate: threading.Event | None = None,
    stall_timeout: float = STALL_TIMEOUT_S,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = _part(dest)
    total = size
    try:
        status, head_size = head_info(http, url)
        if status == 404:
            raise make_error("NS-E105", f"{url}: HTTP 404", status=404)
        if status < 400 and head_size:
            total = head_size
    except httpx.HTTPError:
        pass  # the GET below decides whether the site is really unreachable

    if not resume and part.exists():
        part.unlink()
    have = part.stat().st_size if part.exists() else 0
    if total is not None and have > total:
        part.unlink()
        have = 0

    if progress:
        progress(have, total)
    # No bytes for ``stall_timeout`` seconds raises httpx.ReadTimeout: the attempt is a network error.
    timeout = httpx.Timeout(30.0, read=stall_timeout)
    while total is None or have < total:
        paused = False
        headers = {"Range": f"bytes={have}-"} if have else {}
        with http.stream("GET", url, headers=headers, timeout=timeout) as r:
            if r.status_code == 404:
                raise make_error("NS-E105", f"{url}: HTTP 404", status=404)
            if r.status_code == 416:
                part.unlink(missing_ok=True)
                raise OSError("server rejected the resume range; restarting the file")
            r.raise_for_status()
            if have and r.status_code != 206:
                have = 0  # the server ignored Range and is sending the whole file
            if total is None and r.headers.get("content-length"):
                total = have + int(r.headers["content-length"])
            done = have
            with part.open("ab" if have else "wb") as fh:
                for block in r.iter_bytes(chunk):
                    fh.write(block)
                    done += len(block)
                    if gate is not None and not gate.is_set():
                        paused = True       # Pause: stop after this chunk and close the connection
                        break
                    if progress:
                        progress(done, total)
        have = part.stat().st_size
        if not paused or gate is None:
            break
        if progress:
            progress(have, total)
        gate.wait()                         # Resume: reconnect with Range from the bytes on disk

    got = part.stat().st_size
    if total is not None and got != total:
        raise OSError(f"incomplete download: {got} of {total} bytes")
    digest = sha256_file(part)
    if sha256 is not None and digest != sha256.lower():
        part.unlink(missing_ok=True)
        raise make_error("NS-E109", f"{dest.name}: expected {sha256[:12]}..., got {digest[:12]}...")
    os.replace(part, dest)
    write_marker(dest, digest)


def download(
    url: str,
    dest: Path,
    sha256: str | None,
    *,
    resume: bool = True,
    progress: ProgressFn | None = None,
    client: httpx.Client | None = None,
    size: int | None = None,
    retries: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    chunk: int = STREAM_CHUNK,
    gate: threading.Event | None = None,
    stall_timeout: float = STALL_TIMEOUT_S,
    on_retry: RetryFn | None = None,
) -> Path:
    """Download ``url`` to ``dest`` with resume, SHA256 check and atomic rename.

    Raises NS-E109 when the hash is wrong after ``retries`` attempts and NS-E105 when the site cannot be
    reached (or answers 404, which is not retried).

    ``gate`` is the setup window's pause switch (set = running, cleared = paused). It is checked after every
    ``chunk`` bytes (64 KiB), so Pause takes effect inside the file: the connection is closed and, on Resume,
    reopened with ``Range`` from the bytes on disk. It is also waited on before every attempt, and an attempt
    that failed while it was cleared does not count, so a long pause cannot use up the retries.

    A transfer that delivers no bytes for ``stall_timeout`` seconds (60 by default) is aborted and counts as a
    network error: the next attempt follows after the backoff and resumes with ``Range``. ``on_retry`` is
    told ``(attempt, reason, wait seconds)`` before each wait; reason is ``stall``, ``network`` or ``hash``.
    """
    dest = Path(dest)
    if is_verified(dest, sha256):
        if progress:
            n = dest.stat().st_size
            progress(n, n)
        return dest
    http = client or _new_client()
    last: NsError | None = None
    reason = "network"
    attempt = 0
    try:
        while attempt < retries:
            if gate is not None:
                gate.wait()
            try:
                _fetch_once(
                    http, url, dest, sha256, resume=resume, progress=progress, size=size, chunk=chunk,
                    gate=gate, stall_timeout=stall_timeout,
                )
                return dest
            except NsError as e:
                if e.code != "NS-E109":
                    raise
                last, reason = e, "hash"
            except httpx.ReadTimeout:
                last = make_error("NS-E105", f"{url}: no data for {stall_timeout:g} s")
                reason = "stall"
            except (httpx.HTTPError, OSError) as e:
                last, reason = make_error("NS-E105", f"{url}: {e}"), "network"
            if gate is not None and not gate.is_set():
                continue  # failed while paused: not a real attempt
            attempt += 1
            if attempt < retries:
                wait = 2**attempt
                if on_retry is not None:
                    on_retry(attempt, reason, float(wait))
                sleep(wait)
    finally:
        if client is None:
            http.close()
    assert last is not None
    raise last


def _find_in_source(root: Path, name: str, max_depth: int = 4) -> list[Path]:
    direct = root / name
    found = [direct] if direct.is_file() else []
    base = len(root.parts)
    for current, dirs, files in os.walk(root):
        if len(Path(current).parts) - base >= max_depth:
            dirs[:] = []
        if name in files:
            p = Path(current) / name
            if p not in found:
                found.append(p)
    return found


def copy_from_source(
    models_source: Path | str, dest: Path, sha256: str | None, *, progress: ProgressFn | None = None
) -> bool:
    """Copy ``dest.name`` from a pre-downloaded folder when its SHA256 matches. True when copied.

    A file with no pinned hash matches on name alone. A mismatching candidate is ignored (the caller then
    downloads the file), never copied.
    """
    root = Path(models_source)
    dest = Path(dest)
    if not root.is_dir():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = _part(dest)
    for src in _find_in_source(root, dest.name):
        total = src.stat().st_size
        h = hashlib.sha256()
        done = 0
        with src.open("rb") as fin, part.open("wb") as fout:
            while True:
                block = fin.read(CHUNK)
                if not block:
                    break
                h.update(block)
                fout.write(block)
                done += len(block)
                if progress:
                    progress(done, total)
        if sha256 is None or h.hexdigest() == sha256.lower():
            os.replace(part, dest)
            write_marker(dest, h.hexdigest())
            return True
        part.unlink(missing_ok=True)
    return False


def extract_zip(zip_path: Path, target: Path) -> list[str]:
    """Extract ``zip_path`` into ``target`` refusing paths that escape it (zip-slip). Returns member names."""
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        for member in names:
            out = (root / member).resolve()
            if out != root and root not in out.parents:
                raise make_error("NS-E109", f"unsafe path in archive: {member}")
        zf.extractall(root)
    return names


def extract_members(archive: Path, target: Path, members: Iterable[tuple[str, tuple[str, ...]]]) -> list[str]:
    """Write only the named files of a zip/nupkg into ``target`` (flat). Returns the file names written.

    ``members`` is ``(file name, candidate archive paths)``; the first candidate present wins (archive paths
    compare case-insensitively). A missing file is NS-E109. Nothing else in the archive is touched.
    """
    target.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    with zipfile.ZipFile(archive) as zf:
        index = {n.lower(): n for n in zf.namelist()}
        for name, candidates in members:
            found = next((index[c.lower()] for c in candidates if c.lower() in index), None)
            if found is None:
                raise make_error("NS-E109", f"{archive.name}: lacks {' or '.join(candidates)}")
            (target / name).write_bytes(zf.read(found))
            written.append(name)
    return written


def pinned_sha256(value: str | None) -> str | None:
    """The pin's hash when it is a real 64-digit SHA256, else None (``TBD-release`` and friends)."""
    if value is not None and len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value):
        return value.lower()
    return None


# NuGet package Microsoft.Web.WebView2: managed assemblies and the native loader the frameless host needs.
WEBVIEW2_MEMBERS: tuple[tuple[str, tuple[str, ...]], ...] = tuple(
    (name, tuple(f"{folder}/{name}" for folder in folders))
    for name, folders in (
        ("Microsoft.Web.WebView2.Core.dll", ("lib/net462", "lib/net45")),
        ("Microsoft.Web.WebView2.Wpf.dll", ("lib/net462", "lib/net45")),
        ("WebView2Loader.dll", ("runtimes/win-x64/native",)),
    )
)


# --------------------------------------------------------------------------------------------------------
# plans
# --------------------------------------------------------------------------------------------------------
def _hf_item(f: HfFile, dest_dir: Path, revision: str = "main") -> DownloadItem:
    name = Path(f.file).name
    return DownloadItem(
        name=name, url=hf_url(f.repo, revision, f.file), dest=dest_dir / name, sha256=f.sha256, size=f.bytes
    )


def custom_item(custom: LlmCustom, dest_dir: Path) -> DownloadItem:
    """The user-named GGUF: downloaded from the repository's main branch, with no pinned SHA256 (NS-W113)."""
    name = Path(custom.file).name
    return DownloadItem(
        name=name, url=hf_url(custom.repo, "main", custom.file), dest=dest_dir / name, sha256=None,
        size=custom.size,
    )


def plan_bins(
    manifest: Manifest,
    profile: str,
    home: Path,
    *,
    include_code: bool = False,
    llama_pair: str | None = None,
) -> list[DownloadItem]:
    """Binaries for s02: llama.cpp build + cudart (or the CPU build), OpenCode when chosen, yt-dlp.exe."""
    paths = Paths(Path(home))
    dl = paths.home / "bin" / "_downloads"
    keys: list[tuple[str, tuple[str, ...]]]
    if profile == "cpu-dev":
        keys = [("llama_cpu", ("llama-server.exe",))]
    elif llama_pair == "cuda13":
        keys = [("llama_cuda13", ("llama-server.exe",)), ("cudart13", ())]
    else:
        keys = [("llama_cuda", ("llama-server.exe",)), ("cudart", ())]
    items: list[DownloadItem] = []
    for key, expect in keys:
        pin = manifest.bins.get(key)
        if pin is None:
            continue
        items.append(
            DownloadItem(
                name=key, url=pin.url, dest=dl / Path(pin.url).name, sha256=pin.sha256, kind="zip",
                extract_to=paths.bin_llama, expect=expect,
            )
        )
    if include_code and (pin := manifest.bins.get("opencode")) is not None:
        items.append(
            DownloadItem(
                name="opencode", url=pin.url, dest=dl / Path(pin.url).name, sha256=pin.sha256, kind="zip",
                extract_to=paths.bin_opencode, expect=("opencode.exe",),
            )
        )
    if (pin := manifest.bins.get("yt_dlp")) is not None:
        # One exe, not a zip: the Clipper's URL sources run bin\yt-dlp\yt-dlp.exe (CR-C8-10).
        items.append(
            DownloadItem(
                name="yt_dlp", url=pin.url, dest=paths.home / "bin" / "yt-dlp" / "yt-dlp.exe",
                sha256=pin.sha256,
            )
        )
    if (item := webview2_item(manifest, paths.home)) is not None:
        items.append(item)
    return items


def webview2_item(manifest: Manifest, home: Path) -> DownloadItem | None:
    """The WebView2 NuGet package, unpacked to ``bin\\webview2`` (three DLLs only).

    None when ``bins.webview2`` is absent or its sha256 is not pinned yet (``TBD-release``): the installer
    then skips it and the dashboard launcher uses the Edge/Chrome ``--app=`` window instead.
    """
    pin = manifest.bins.get("webview2")
    if pin is None or pinned_sha256(pin.sha256) is None:
        return None
    root = Path(home) / "bin"
    return DownloadItem(
        name="webview2", url=pin.url,
        dest=root / "_downloads" / f"microsoft.web.webview2.{pin.version}.nupkg",
        sha256=pin.sha256, kind="nupkg", extract_to=root / "webview2",
        expect=tuple(name for name, _ in WEBVIEW2_MEMBERS), members=WEBVIEW2_MEMBERS,
    )


def qwentts_item(manifest: Manifest, home: Path) -> DownloadItem | None:
    """The pinned qwentts.cpp zip, or None when the manifest pins no binary (bins.qwentts is null)."""
    pin = manifest.bins.get("qwentts")
    if pin is None:
        return None
    return release_zip_item("qwentts", pin.url, pin.sha256, Path(home))


#: Programs in the qwentts.cpp release zip (build-qwentts.yml packages them flat; there is no qwentts.exe).
QWENTTS_EXES = ("qwen-tts.exe", "qwen-codec.exe")


def release_zip_item(name: str, url: str, sha256: str | None, home: Path) -> DownloadItem:
    """A qwentts zip item (pinned or from the GitHub Releases fallback URL) extracted to bin\\qwentts."""
    root = Path(home) / "bin"
    return DownloadItem(
        name=name, url=url, dest=root / "_downloads" / f"{name}.zip", sha256=sha256, kind="zip",
        extract_to=root / "qwentts", expect=QWENTTS_EXES,
    )


def plan_models(
    manifest: Manifest,
    profile: str,
    tier: str,
    home: Path,
    *,
    include_voicedesign: bool = False,
    custom: LlmCustom | None = None,
) -> list[DownloadItem]:
    """Model files for s03: the LLM (the tier's, or the custom GGUF), embed, TTS GGUFs, whisper, YuNet."""
    paths = Paths(Path(home))
    pm = manifest.profiles[profile]
    llm = pm.llm
    if profile == "gpu":
        if tier == "balanced" and pm.llm_balanced is not None:
            llm = pm.llm_balanced
        elif tier == "best" and pm.llm_best is not None:
            llm = pm.llm_best
    llm_item = custom_item(custom, paths.models_llm) if custom else _hf_item(llm, paths.models_llm)
    items = [llm_item, _hf_item(pm.embed, paths.models_embed)]
    items += [_hf_item(f, paths.models_tts) for f in pm.tts]
    if include_voicedesign:
        items += [_hf_item(f, paths.models_tts) for f in pm.tts_optional]

    wdir = paths.models_whisper / pm.whisper_model
    repo = pm.whisper_repo
    pinned = dict(repo.files_sha256)
    for fname in (*pinned, *[f for f in WHISPER_REQUIRED if f not in pinned]):
        items.append(
            DownloadItem(
                name=f"whisper/{fname}", url=hf_url(repo.repo, repo.revision, fname), dest=wdir / fname,
                sha256=pinned.get(fname),
            )
        )
    for fname in WHISPER_OPTIONAL:
        items.append(
            DownloadItem(
                name=f"whisper/{fname}", url=hf_url(repo.repo, repo.revision, fname), dest=wdir / fname,
                optional=True,
            )
        )
    yunet = manifest.bins.get("yunet")
    if yunet is not None:
        items.append(
            DownloadItem(
                name="yunet", url=yunet.url, dest=paths.home / "models" / "cv" / "yunet.onnx",
                sha256=yunet.sha256,
            )
        )
    return items


def plan_from_manifest(
    manifest: Manifest,
    profile: str,
    tier: str,
    home: Path,
    *,
    include_code: bool = False,
    llama_pair: str | None = None,
    include_voicedesign: bool = False,
    custom: LlmCustom | None = None,
) -> list[DownloadItem]:
    """Everything s02 and s03 download for ``profile`` and LLM ``tier`` (spec 21.2, 21.3)."""
    return plan_bins(manifest, profile, home, include_code=include_code, llama_pair=llama_pair) + plan_models(
        manifest, profile, tier, home, include_voicedesign=include_voicedesign, custom=custom
    )


def plan_from_hf_tree(
    repo: str, revision: str, dest_dir: Path, client: httpx.Client
) -> list[DownloadItem]:
    """List a Hugging Face repo through the tree API (SHA256 from the LFS oid) as download items."""
    url = f"{HF}/api/models/{repo}/tree/{revision}"
    try:
        r = client.get(url, params={"recursive": "true"})
        r.raise_for_status()
        entries = r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise make_error("NS-E105", f"{url}: {e}") from e
    items: list[DownloadItem] = []
    for e in entries:
        if e.get("type") != "file" or e["path"] in (".gitattributes", "README.md"):
            continue
        lfs = e.get("lfs") or {}
        items.append(
            DownloadItem(
                name=f"{repo}/{e['path']}",
                url=hf_url(repo, revision, e["path"]),
                dest=Path(dest_dir) / e["path"],
                sha256=lfs.get("oid"),
                size=int(lfs.get("size") or e.get("size") or 0) or None,
            )
        )
    return items


def total_bytes(items: Iterable[DownloadItem]) -> int:
    """Sum of the known sizes."""
    return sum(i.size or 0 for i in items)


def is_done(item: DownloadItem) -> bool:
    """True when the item is already in place (verified file, or zip extracted with its files present)."""
    if item.kind in ("zip", "nupkg"):
        target = item.extract_to
        if target is None:
            return False
        flag = target / f".ns-{item.name}.done"
        return flag.exists() and all((target / e).exists() for e in item.expect)
    return is_verified(item.dest, item.sha256)


def pending(items: Iterable[DownloadItem]) -> list[DownloadItem]:
    """Items that still need work."""
    return [i for i in items if not is_done(i)]


# --------------------------------------------------------------------------------------------------------
# running a plan
# --------------------------------------------------------------------------------------------------------
class _NullReporter:
    def start(self, name: str, total: int | None) -> None:
        return None

    def update(self, name: str, done: int, total: int | None) -> None:
        return None

    def finish(self, name: str, outcome: str) -> None:
        return None


def _extract_item(item: DownloadItem) -> None:
    assert item.extract_to is not None
    if item.kind == "nupkg":
        extract_members(item.dest, item.extract_to, item.members)
    else:
        extract_zip(item.dest, item.extract_to)
    missing = [e for e in item.expect if not (item.extract_to / e).exists()]
    if missing:
        raise make_error("NS-E109", f"{item.name}: archive lacks {', '.join(missing)}")
    (item.extract_to / f".ns-{item.name}.done").write_text("ok", encoding="utf-8")
    item.dest.unlink(missing_ok=True)
    _marker(item.dest).unlink(missing_ok=True)


def run_plan(
    items: list[DownloadItem],
    *,
    client: httpx.Client | None = None,
    concurrency: int = 2,
    models_source: Path | str | None = None,
    reporter: Reporter | None = None,
    sleep: Callable[[float], None] = time.sleep,
    retries: int = 3,
    fetch: Callable[..., Any] | None = None,
    gate: threading.Event | None = None,
) -> PlanResult:
    """Fetch every item (copy from ``models_source`` when possible), two at a time, then extract zips.

    All items are attempted even if one fails; the first failure is raised afterwards so a rerun resumes.
    ``fetch`` replaces ``download`` (simulate mode and tests). ``gate`` is the pause switch.
    """
    rep: Reporter = reporter or _NullReporter()
    result = PlanResult()
    lock = threading.Lock()
    http = client or _new_client()
    do_fetch = fetch or download
    extra: dict[str, Any] = {"gate": gate} if gate is not None else {}

    def outcome(item: DownloadItem, kind: str) -> None:
        with lock:
            getattr(result, kind).append(item.name)
        rep.finish(item.name, kind)

    def work(item: DownloadItem) -> None:
        rep.start(item.name, item.size)
        if is_done(item):
            outcome(item, "skipped")
            return

        def progress(done: int, total: int | None) -> None:
            rep.update(item.name, done, total)

        retrying = getattr(rep, "retrying", None)

        def on_retry(attempt: int, reason: str, wait_s: float) -> None:
            if retrying is not None:
                retrying(item.name, attempt, reason, wait_s)

        try:
            copied = False
            if models_source and item.kind == "file" and not is_verified(item.dest, item.sha256):
                copied = copy_from_source(models_source, item.dest, item.sha256, progress=progress)
            if copied:
                outcome(item, "copied")
                return
            do_fetch(
                item.url, item.dest, item.sha256, progress=progress, client=http, size=item.size,
                retries=retries, sleep=sleep, on_retry=on_retry, **extra,
            )
            if item.kind in ("zip", "nupkg"):
                _extract_item(item)
            outcome(item, "downloaded")
        except NsError as e:
            if item.optional and e.data.get("status") == 404:
                outcome(item, "missing_optional")
                return
            with lock:
                result.failed[item.name] = e
            rep.finish(item.name, "failed")

    try:
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
            list(pool.map(work, items))
    finally:
        if client is None:
            http.close()
    if result.failed:
        raise next(iter(result.failed.values()))
    return result
