"""C2 `ns doctor [--bundle]`: quick diagnosis, and the diagnostics zip the partner sends back (J7).

The bundle holds logs, install_state, runtime, verify-report, nvidia-smi output, `uv pip freeze` per
venv, the last 3 job directories without media, and VERSION. It never holds API keys, models or videos.
"""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ns_common import __version__, ffmpeg
from ns_common.config import load_install_state, load_runtime
from ns_common.devmode import is_dev
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_common.uv import find_uv
from ns_supervisor import procs
from ns_supervisor.client import SupervisorClient

MEDIA_SUFFIXES = frozenset(
    {".mp4", ".mkv", ".mov", ".webm", ".avi", ".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg"}
    | {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
)
MAX_LOG_BYTES = 5 * 1024 * 1024
VENVS = ("studio", "supervisor", "tts")


def desktop_dir() -> Path:
    """The user's Desktop (follows OneDrive redirection via the registry; falls back to ~/Desktop)."""
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


def freeze_venv(paths: Paths, venv: str) -> str | None:
    """`uv pip freeze` of a venv; None when uv or the venv is absent or freezing fails."""
    py = paths.venvs / venv / "Scripts" / "python.exe"
    uv = find_uv(paths)
    if uv is None or not py.exists():
        return None
    try:
        r = subprocess.run(
            [uv, "pip", "freeze", "--python", str(py)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def _add_file(z: zipfile.ZipFile, src: Path, arcname: str, skipped: list[str]) -> None:
    """Add a file; very large ones keep only their last MAX_LOG_BYTES."""
    try:
        size = src.stat().st_size
        if size > MAX_LOG_BYTES:
            with src.open("rb") as fh:
                fh.seek(size - MAX_LOG_BYTES)
                z.writestr(arcname, fh.read())
            skipped.append(f"{arcname}: truncated to last {MAX_LOG_BYTES} bytes")
        else:
            z.write(src, arcname)
    except OSError as e:
        skipped.append(f"{arcname}: {e}")


def _last_jobs(paths: Paths, n: int = 3) -> list[Path]:
    if not paths.jobs_dir.exists():
        return []
    dirs = [p for p in paths.jobs_dir.iterdir() if p.is_dir()]
    return sorted(dirs, key=lambda p: p.stat().st_mtime, reverse=True)[:n]


def build_bundle(
    paths: Paths,
    *,
    out_dir: Path | None = None,
    now: datetime | None = None,
    freeze: Callable[[Paths, str], str | None] = freeze_venv,
    smi: Callable[[], str | None] = procs.nvidia_smi_report,
    app: Path | None = None,
) -> Path:
    """Write `nightshift-diagnostics-<date>.zip` (default: Desktop) and return its path."""
    when = now or datetime.now()
    target_dir = out_dir or desktop_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    out = target_dir / f"nightshift-diagnostics-{when:%Y-%m-%d}.zip"
    skipped: list[str] = []
    version_text = __version__
    if app is not None and (app / "VERSION").exists():
        version_text = (app / "VERSION").read_text(encoding="utf-8").strip()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        if paths.logs.exists():
            for f in sorted(paths.logs.rglob("*")):
                if f.is_file():
                    _add_file(z, f, "logs/" + f.relative_to(paths.logs).as_posix(), skipped)
        for src, arc in (
            (paths.install_state_file, "install_state.json"),
            (paths.runtime_file, "runtime.json"),
            (paths.logs / "verify-report.json", "verify-report.json"),
            (paths.config / "schedule.json", "schedule.json"),
        ):
            if src.is_file():
                _add_file(z, src, arc, skipped)
            elif arc in ("install_state.json", "runtime.json", "verify-report.json"):
                skipped.append(f"{arc}: not found")
        report = smi()
        z.writestr("nvidia-smi.txt", report if report is not None else "nvidia-smi is not available\n")
        for venv in VENVS:
            text = freeze(paths, venv)
            if text is None:
                skipped.append(f"pip-freeze/{venv}.txt: venv or uv not available")
            else:
                z.writestr(f"pip-freeze/{venv}.txt", text)
        for job in _last_jobs(paths):
            for f in sorted(job.rglob("*")):
                if f.is_file() and f.suffix.lower() not in MEDIA_SUFFIXES:
                    _add_file(z, f, f"jobs/{job.name}/{f.relative_to(job).as_posix()}", skipped)
        z.writestr("VERSION", version_text + "\n")
        info = {"created": when.isoformat(timespec="seconds"), "version": version_text, "notes": skipped}
        z.writestr("bundle-info.json", json.dumps(info, indent=2) + "\n")
    return out


def diagnose(paths: Paths, client: SupervisorClient) -> list[tuple[str, bool, str]]:
    """Quick health table for plain `ns doctor`: (check, ok, detail-or-fix-hint)."""
    rows: list[tuple[str, bool, str]] = []
    try:
        rt = load_runtime(paths)
        rows.append(("runtime.json", True, str(paths.runtime_file)))
        for label, model in (("llm model", rt.llama.model_path), ("embed model", rt.embed.model_path)):
            ok = bool(model) and Path(model).is_file()
            rows.append((label, ok, model or "not configured; run the installer or 'ns tune'"))
    except NsError as e:
        rows.append(("runtime.json", False, f"{e} | Hint: {e.definition.hint}"))
    st = load_install_state(paths)
    rows.append(("install_state", bool(st.app_path), st.app_path or "installer has not recorded app_path"))
    for venv in ("studio", "supervisor"):
        py = paths.venvs / venv / "Scripts" / "python.exe"
        rows.append((f"venv {venv}", py.exists(), str(py)))
    up = client.is_up()
    where = client.base_url if up else "not running (opening the Dashboard starts it)"
    rows.append(("supervisor", up, where))
    if is_dev(paths):       # docs/DEV_MODE.md: a dev install has no graphics card to ask
        rows.append(("nvidia-smi", True, "not needed (dev mode)"))
    else:
        smi = procs.nvidia_smi_report()
        rows.append(("nvidia-smi", smi is not None, "ok" if smi is not None else "no NVIDIA driver found"))
    have_ffmpeg = ffmpeg.available()
    rows.append(("ffmpeg", have_ffmpeg, "on PATH" if have_ffmpeg else "ffmpeg/ffprobe not found"))
    return rows
