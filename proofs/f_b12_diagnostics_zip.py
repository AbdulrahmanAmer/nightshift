"""F-B12: the diagnostics zip, checked entry by entry, with both footage keys stored.

Stores fake keys (GOODKEY) through POST /api/settings/keys on a second dashboard app that talks to the QA fake
Pexels/Pixabay endpoint (proofs/_fakes/footage.py and proofs/_fakes/dashboard_no_worker.py, started through
ctx.spawn with HTTPS_PROXY and SSL_CERT_FILE, as f_b13_b14_footage_429.py does), so the live key check never
leaves the machine: the fake key is not sent to the real api.pexels.com or pixabay.com. The keys live in the
user's keyring, which every dashboard of the home shares. The bundle is made with POST /api/diagnostics on the
live dashboard and the zip it reports is read: logs, settings (install_state, runtime, answers) without keys,
install and verify reports, the nvidia-smi.txt placeholder, VERSION and bundle-info, and then every entry is
scanned for the key value and for key-shaped strings. The keys are cleared in a `finally`.

The proof skips at the start when a footage key is already stored (it never replaces a real key). The zip the
product writes to the Desktop is moved into scratch before any check on it. Ported from runs/qa/croutes
(F-C5 makes the bundle) and the fb12 entries of the evidence log (2026-11-09 08:57, 0.1.12; regression 14:44,
0.1.14).
"""

from __future__ import annotations

import io
import json
import re
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from proofs._fakes import dashboard_no_worker, footage
from proofs._lib import api_no_deadline, ffmpeg_path, short, take_off_desktop

PROOF = {
    "id": "f-b12-diagnostics-zip",
    "boxes": ["F-B12"],
    "install": "dev",
    "window": False,
    "needs": ["engine"],
    "timeout_s": 600,
    "fakes": ["footage"],
}

FAKE_KEY = "GOODKEY"  # the fake footage endpoint accepts this one
KEYS = ("pexels", "pixabay")
HOSTS = {"pexels": "api.pexels.com", "pixabay": "pixabay.com"}
PLACEHOLDER = "nvidia-smi is not available\n"
BUNDLE_PREFIX = "nightshift-diagnostics-"
MEDIA_SUFFIXES = frozenset(
    ".mp4 .mkv .mov .webm .avi .wav .mp3 .flac .m4a .aac .ogg .jpg .jpeg .png .gif .webp .bmp "
    ".gguf .safetensors .onnx".split()
)
# what a leaked secret looks like, besides the value we stored
SECRET_PATTERNS = {
    "authorization header": re.compile(rb"authorization\s*:", re.I),
    "bearer token": re.compile(rb"bearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    "github token": re.compile(rb"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "sk- key": re.compile(rb"\bsk-[A-Za-z0-9]{20,}"),
    "non-empty *_api_key value": re.compile(rb"_api_key[\"']?\s*[:=]\s*[\"'][^\"']+[\"']", re.I),
}
_NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _http(method: str, url: str, body: Any = None, timeout: float = 120) -> tuple[int, Any]:
    """One HTTP call with no proxy: (status, parsed JSON or text). Network errors are raised."""
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with _NO_PROXY.open(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read()
    text = raw.decode("utf-8", errors="replace")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def _tail(path: Path, limit: int = 1500) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


# ---------------------------------------------------------------------------- the Desktop is not ours
def _desktop() -> Path:
    """Where the product writes bundles: the user's Desktop (the same lookup the product uses)."""
    if sys.platform == "win32":
        import winreg

        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "Desktop")
            if isinstance(value, str) and value:
                return Path(value)
        except OSError:
            pass
    return Path.home() / "Desktop"


def _free_minute(ctx: Any) -> None:
    """The bundle's name carries the minute; the product would overwrite an owner's zip of this minute.
    Wait for the next minute rather than touch it."""

    def free() -> bool:
        return not (_desktop() / f"{BUNDLE_PREFIX}{datetime.now():%Y-%m-%d-%H%M}.zip").exists()

    if not free():
        ctx.step("a diagnostics zip of this minute is on the Desktop (not the proof's): waiting for the next")
        ctx.wait(free, 75, every=5, what="the minute of an existing diagnostics zip to pass")


def _take(ctx: Any, src: Path, tag: str) -> Path | None:
    """Move the product's file off the Desktop into scratch (fails if it stays on C:); None if absent."""
    if not src.is_file():
        ctx.step("no file at the path the product reported", path=str(src))
        return None
    return take_off_desktop(ctx, src, tag)


# ---------------------------------------------------------------------------- the fake and its dashboard
class _Fake:
    def __init__(self, folder: Path, port: int) -> None:
        self.folder = folder
        self.port = port

    def ctl(self, ctx: Any, query: str) -> dict[str, Any]:
        status, out = _http("GET", f"http://127.0.0.1:{self.port}/__ctl?{query}", timeout=30)
        ctx.check(status == 200 and isinstance(out, dict), f"fake control {query}", status=status)
        return out

    def counts(self, ctx: Any, host: str) -> dict[str, int]:
        return dict(self.ctl(ctx, "stats=1").get("counts", {}).get(host, {}))


def _start_fake(ctx: Any) -> _Fake:
    folder = ctx.scratch / "footage-fake"
    folder.mkdir(parents=True, exist_ok=True)
    proc = ctx.spawn(
        [sys.executable, str(Path(footage.__file__).resolve()), "--dir", str(folder), "--good-key", FAKE_KEY],
        name="footage-fake",
    )
    ready = folder / "ready.json"

    def listening() -> dict[str, Any] | None:
        if proc.poll() is not None:
            ctx.check(
                False,
                "the fake footage endpoint started",
                exit_code=proc.returncode,
                log=_tail(ctx.scratch / "footage-fake.log", 600),
            )
        return json.loads(ready.read_text(encoding="ascii")) if ready.is_file() else None

    info = ctx.wait(listening, 180, every=1, what="the fake footage endpoint to listen")
    ctx.step("fake Pexels/Pixabay listening (own CA made in the scratch folder)", port=info["port"])
    return _Fake(folder, int(info["port"]))


def _start_dashboard(ctx: Any, fake: _Fake, python: Path) -> str:
    """The install's dashboard app (no job worker) with the fake's proxy variables; returns its base URL."""
    tmp = ctx.scratch / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    env = footage.client_env(fake.folder, fake.port)
    env.update({"TEMP": str(tmp), "TMP": str(tmp)})
    port_file = ctx.scratch / "dashboard.port"
    script = Path(dashboard_no_worker.__file__).resolve()
    proc = ctx.spawn([str(python), str(script), str(port_file)], env=env, name="dashboard-proof")

    def listening() -> str | None:
        if proc.poll() is not None:
            ctx.check(
                False,
                "the dashboard app started",
                exit_code=proc.returncode,
                log=_tail(ctx.scratch / "dashboard-proof.log"),
            )
        if not port_file.is_file():
            return None
        base = f"http://127.0.0.1:{port_file.read_text(encoding='ascii').strip()}"
        try:
            status, _ = _http("GET", base + "/api/health", timeout=5)
        except OSError:
            return None
        return base if status == 200 else None

    base = ctx.wait(listening, 120, every=1, what="the dashboard app to answer /api/health")
    ctx.step(
        "install's dashboard app started with the fake as its proxy (no job worker)",
        url=base,
        proxy=env["HTTPS_PROXY"],
        ca_bundle=env["SSL_CERT_FILE"],
    )
    return base


def _post_key(ctx: Any, base: str, name: str, value: str) -> tuple[int, Any]:
    try:
        return _http("POST", base + "/api/settings/keys", {"name": name, "value": value}, 120)
    except OSError as exc:
        ctx.check(
            False, f"POST /api/settings/keys on the proof's dashboard app answered ({name})", error=str(exc)
        )
        raise


def _store_keys(ctx: Any, fake: _Fake, base: str) -> str | None:
    """Store the fake key under both names through the fake-backed dashboard app.

    Returns None when both are stored, or why they could not be (no keyring): the caller still checks the
    bundle and ends the proof as skipped naming it.
    """
    fake.ctl(ctx, "reset=1&mode=normal")
    for name in KEYS:
        status, answer = _post_key(ctx, base, name, FAKE_KEY)
        if status == 501:
            code = answer.get("error", {}).get("code") if isinstance(answer, dict) else None
            ctx.step("key not stored: no keyring", name=name, status=status, code=code)
            return f"the key store is unavailable here (POST /api/settings/keys answered 501 {code})"
        ctx.check(
            status == 200 and isinstance(answer, dict) and answer.get("set") is True,
            f"{name} key stored through POST /api/settings/keys (fake value, fake-backed dashboard app)",
            name=name,
            status=status,
            answer=short(answer),
        )
        if isinstance(answer, dict) and "checked" in answer:
            seen = fake.counts(ctx, HOSTS[name])
            ctx.check(
                answer.get("checked") is True and sum(seen.values()) >= 1,
                f"the {name} key check was answered by the fake endpoint, not by the real service",
                counts=seen,
            )
    return None


def _clear_keys(ctx: Any, base: str) -> None:
    """Clear both keys (an empty value clears): through the fake-backed app, else the live dashboard."""
    for name in KEYS:
        try:
            try:
                status, answer = _http("POST", base + "/api/settings/keys", {"name": name, "value": ""})
            except OSError:
                status, answer = api_no_deadline(
                    ctx, "POST", "/api/settings/keys", {"name": name, "value": ""}
                )
            ctx.step("key cleared", name=name, status=status, answer=short(answer))
        except Exception as exc:  # noqa: BLE001  (cleanup must try the next one)
            ctx.step("key clear failed", name=name, error=str(exc))


def _make_bundle(ctx: Any) -> tuple[bytes, Path]:
    """POST /api/diagnostics on the live dashboard; returns the zip's bytes and the copy kept in ctx.scratch.

    The product writes the zip to the Desktop. The proof moves it into scratch before it checks anything on
    it, so nothing stays on the Desktop.
    """
    _free_minute(ctx)
    status, answer = ctx.api("POST", "/api/diagnostics", timeout=600)
    if status == 501:
        ctx.skip(
            f"POST /api/diagnostics answered 501 ({short(answer)}): the doctor cannot run on this install"
        )
    reported = answer.get("path") if status == 200 and isinstance(answer, dict) else None
    src = Path(str(reported)) if reported else None
    on_disk = src.stat().st_size if src is not None and src.is_file() else None
    kept = _take(ctx, src, "f-b12") if src is not None else None
    ctx.check(
        status == 200
        and isinstance(answer, dict)
        and str(answer.get("name", "")).startswith(BUNDLE_PREFIX)
        and str(answer.get("name", "")).endswith(".zip")
        and answer.get("download_url") == "/api/diagnostics/latest",
        "POST /api/diagnostics answers 200 with a nightshift-diagnostics zip and its download URL",
        status=status,
        answer=short(answer),
    )
    ctx.check(kept is not None and on_disk is not None, "the reported zip existed on disk", path=str(src))
    assert kept is not None and isinstance(answer, dict)
    raw = kept.read_bytes()
    ctx.check(
        len(raw) == answer.get("size") == on_disk,
        "the file's size is the size the route reported",
        file_bytes=len(raw),
        reported=answer.get("size"),
    )
    return raw, kept


def _log_snapshot(ctx: Any) -> list[str]:
    root = ctx.home / "logs"
    if not root.is_dir():
        return []
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def _scan_secrets(z: zipfile.ZipFile, names: list[str]) -> tuple[dict[str, list[str]], int]:
    found: dict[str, list[str]] = {}
    scanned = 0
    for name in names:
        data = z.read(name)
        scanned += len(data)
        hits = [label for label, rx in SECRET_PATTERNS.items() if rx.search(data)]
        if FAKE_KEY.encode() in data:
            hits.append("the stored key value")
        if hits:
            found[name] = hits
    return found, scanned


def run(ctx: Any) -> None:
    if ffmpeg_path() is None:
        ctx.skip("FFmpeg is not on PATH: the fake endpoint cannot make its clips (hold the laptop lock)")
    problem = footage.cert_tool_problem()
    if problem:
        ctx.skip(problem)
    python = ctx.home / "venvs" / "supervisor" / "Scripts" / "python.exe"
    if not python.is_file():
        ctx.skip(f"{python} does not exist: the dashboard app cannot be started with the proxy variables")
    status, settings = ctx.api("GET", "/api/settings")
    flags: Any = settings.get("keys") if isinstance(settings, dict) else None
    ctx.check(
        status == 200 and isinstance(flags, dict) and set(KEYS) <= set(flags),
        "GET /api/settings reports the key flags",
        status=status,
        keys=flags,
    )
    if any(flags[k] for k in KEYS):
        ctx.skip(
            "a footage key is already stored in this user's keyring; the proof would have to overwrite it "
            f"(flags {flags}). Clear it in Settings first."
        )
    before_logs = _log_snapshot(ctx)
    ctx.step("proof creates its own state", keys=list(KEYS), value="fake", note="cleared again at the end")

    fake = _start_fake(ctx)
    base = _start_dashboard(ctx, fake, python)
    not_stored: str | None = None
    try:
        not_stored = _store_keys(ctx, fake, base)
        if not_stored is None:
            status, settings = ctx.api("GET", "/api/settings")
            ctx.check(
                status == 200 and all(settings["keys"].get(k) is True for k in KEYS),
                "both keys are stored (the live dashboard's Settings flags are true)",
                keys=settings.get("keys"),
            )
        raw, kept = _make_bundle(ctx)
    finally:
        _clear_keys(ctx, base)
    status, settings = ctx.api("GET", "/api/settings")
    ctx.check(
        status == 200 and not any(settings["keys"].get(k) for k in KEYS),
        "keys cleared again: the Settings flags are false",
        keys=settings.get("keys"),
    )

    z = zipfile.ZipFile(io.BytesIO(raw))
    names = z.namelist()
    ctx.check(z.testzip() is None and bool(names), "the zip is readable and complete", entries=len(names))
    ctx.step("entries", names=names[:120])

    info: Any = json.loads(z.read("bundle-info.json")) if "bundle-info.json" in names else {}
    notes = [str(n) for n in (info.get("notes") or [])] if isinstance(info, dict) else []
    ctx.check(
        isinstance(info, dict)
        and info.get("created")
        and info.get("version")
        and isinstance(info.get("notes"), list),
        "bundle-info.json: created, version and the notes of what could not be added",
        info=short(info),
    )
    version = z.read("VERSION").decode("utf-8").strip() if "VERSION" in names else ""
    ctx.check(
        version == info["version"] == ctx.version,
        "VERSION is the install's version and bundle-info says the same",
        VERSION=version,
        bundle_info=info["version"],
        install=ctx.version,
    )

    # logs: every file that was there before the bundle is in it under logs/
    log_entries = sorted(n for n in names if n.startswith("logs/"))
    missing = [
        f
        for f in before_logs
        if f"logs/{f}" not in names and not any(n.startswith(f"logs/{f}:") for n in notes)
    ]
    ctx.check(
        any(n.endswith(".jsonl") for n in log_entries) and not missing,
        "logs/: the component logs on disk are all in the zip",
        in_zip=len(log_entries),
        on_disk=len(before_logs),
        missing=missing,
    )

    # settings: the install's own files, same content as on disk
    for arc, disk in (
        ("install_state.json", ctx.home / "config" / "install_state.json"),
        ("runtime.json", ctx.home / "config" / "runtime.json"),
    ):
        ctx.check(arc in names, f"{arc} is in the zip", entry=arc)
        ctx.check(
            json.loads(z.read(arc)) == _read_json(disk),
            f"{arc} equals the file on disk",
            entry=arc,
        )

    # answers.json: the setup answers without any *_api_key field
    source = _read_json(ctx.home / "config" / "answers.json")
    if isinstance(source, dict):
        ctx.check(
            "setup/answers.json" in names, "setup/answers.json is in the zip", entry="setup/answers.json"
        )
        answers = json.loads(z.read("setup/answers.json"))
        expected = {k: v for k, v in source.items() if not k.endswith("_api_key")}
        ctx.check(
            answers == expected and not any(k.endswith("_api_key") for k in answers),
            "setup/answers.json is the install's answers without any *_api_key field",
            fields=sorted(answers),
            key_fields_on_disk=sorted(k for k in source if k.endswith("_api_key")),
        )
    else:
        ctx.check(
            "setup/answers.json" not in names,
            "no config/answers.json on this install, and none invented in the zip",
        )

    # reports: present when the install has them, named in bundle-info otherwise
    for report in ("install-report.json", "verify-report.json"):
        disk = _read_json(ctx.home / "logs" / report)
        if disk is not None:
            ctx.check(
                report in names and json.loads(z.read(report)) == disk,
                f"{report} is in the zip and equals the install's report",
                entry=report,
            )
        else:
            ctx.check(
                report not in names and any(n.startswith(f"{report}: ") for n in notes),
                f"{report} is not on this install: absent from the zip and named in bundle-info notes",
                notes=notes,
            )

    # nvidia-smi.txt: the placeholder where there is no nvidia-smi, a report where there is
    ctx.check("nvidia-smi.txt" in names, "nvidia-smi.txt is in the zip", entry="nvidia-smi.txt")
    smi = z.read("nvidia-smi.txt").decode("utf-8", errors="replace")
    if shutil.which("nvidia-smi") is None:
        ctx.check(smi == PLACEHOLDER, "no nvidia-smi on this machine: the placeholder text", text=smi)
    else:
        ctx.check(
            bool(smi.strip()),
            "nvidia-smi exists on this machine: nvidia-smi.txt holds its report",
            chars=len(smi),
        )

    # no media, models or key stores
    bad = [
        n
        for n in names
        if PurePosixPath(n).suffix.lower() in MEDIA_SUFFIXES or "dpapi" in n.lower() or "secret" in n.lower()
    ]
    ctx.check(not bad, "no media, model or key-store file in the zip", offenders=bad)

    # the scan: the stored key value and key-shaped strings, in every entry
    found, scanned = _scan_secrets(z, names)
    ctx.check(
        not found,
        f"secret scan over all {len(names)} entries: no key value, no Authorization or Bearer value, "
        "no non-empty *_api_key field",
        entries=len(names),
        bytes_scanned=scanned,
        stored_key_value_in_files=sum("the stored key value" in v for v in found.values()),
        offenders=found,
        zip=str(kept),
    )
    if not_stored is not None:
        ctx.skip(
            f"the secret scan with both keys stored: {not_stored}. Every other check of the box passed "
            "(the bundle was made without keys)."
        )
