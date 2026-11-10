"""C2 `ns doctor [--bundle]`: quick diagnosis, and the diagnostics zip the partner sends back (J7).

The bundle holds logs, install_state, runtime, verify-report, install-report, the setup evidence (`setup/`:
wizard flags, answers without keys, the hardware check, `setup-server.err`, the bootstrap logs), nvidia-smi
output, `uv pip freeze` per venv, the last 3 job directories without media, and VERSION. It never holds API
keys, models or videos.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import zipfile
from collections.abc import Callable
from datetime import datetime
from functools import partial
from pathlib import Path

from ns_common import __version__, config_snapshot, ffmpeg, first_start, rollback_intent
from ns_common.atomic import read_text_retry, retry_busy, write_zip_atomic
from ns_common.config import load_install_state, load_runtime
from ns_common.devmode import is_dev
from ns_common.errors import NsError
from ns_common.paths import Paths, repo_root
from ns_common.uv import find_uv
from ns_supervisor import launch_reach, procs
from ns_supervisor.childenv import child_env, window_flags
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
            env=child_env(),
            creationflags=window_flags(),
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
            retry_busy(partial(z.write, src, arcname))
    except OSError as e:
        skipped.append(f"{arcname}: {e}")


def _last_jobs(paths: Paths, n: int = 3) -> list[Path]:
    if not paths.jobs_dir.exists():
        return []
    dirs = [p for p in paths.jobs_dir.iterdir() if p.is_dir()]
    return sorted(dirs, key=lambda p: p.stat().st_mtime, reverse=True)[:n]


def setup_data_dir() -> Path:
    """Where setup keeps its own files (setup-server.err, the bootstrap logs), outside the install folder."""
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) if local else Path.home() / "AppData" / "Local") / "Nightshift"


def _scrubbed_answers(src: Path) -> str | None:
    """answers.json text without any `*_api_key` field; None when the file is missing or not an object."""
    try:
        data = json.loads(read_text_retry(src, "utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    clean = {k: v for k, v in data.items() if not k.endswith("_api_key")}
    return json.dumps(clean, indent=2, ensure_ascii=False) + "\n"


def _add_setup_evidence(
    z: zipfile.ZipFile, paths: Paths, skipped: list[str], setup_dir: Path, temp_dir: Path
) -> None:
    """What a failed setup is diagnosed from: flags, answers (no keys), the hardware check, setup logs."""
    report = paths.logs / "install-report.json"
    if report.is_file():
        _add_file(z, report, "install-report.json", skipped)
    else:
        skipped.append("install-report.json: not found")
    flags = paths.config / "wizard_flags.json"
    if flags.is_file():
        _add_file(z, flags, "setup/wizard_flags.json", skipped)
    answers = _scrubbed_answers(paths.config / "answers.json")
    if answers is not None:
        z.writestr("setup/answers.json", answers)
    boot = setup_dir / "bootstrap"
    hardware = next(
        (p for p in (boot / "preflight.json", temp_dir / "nightshift-preflight.json") if p.is_file()), None
    )
    if hardware is not None:
        _add_file(z, hardware, "setup/preflight.json", skipped)
    else:
        skipped.append("setup/preflight.json: not found")
    err = setup_dir / "setup-server.err"
    if err.is_file():
        _add_file(z, err, "setup/setup-server.err", skipped)
    if boot.is_dir():
        for f in sorted(boot.iterdir()):
            if f.is_file() and f != hardware:
                _add_file(z, f, f"setup/bootstrap/{f.name}", skipped)


def build_bundle(
    paths: Paths,
    *,
    out_dir: Path | None = None,
    now: datetime | None = None,
    freeze: Callable[[Paths, str], str | None] = freeze_venv,
    smi: Callable[[], str | None] = procs.nvidia_smi_report,
    app: Path | None = None,
    setup_dir: Path | None = None,
    temp_dir: Path | None = None,
) -> Path:
    """Write `nightshift-diagnostics-<date>-<HHMM>.zip` (default: Desktop) and return its path.

    The name carries the minute so two zips on one day do not overwrite each other. `setup_dir` and `temp_dir`
    default to the Nightshift folder in LOCALAPPDATA and the temp folder; tests pass folders of their own.
    """
    when = now or datetime.now()
    target_dir = out_dir or desktop_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    out = target_dir / f"nightshift-diagnostics-{when:%Y-%m-%d-%H%M}.zip"
    skipped: list[str] = []
    version_text = __version__
    if app is not None and (app / "VERSION").exists():
        version_text = (app / "VERSION").read_text(encoding="utf-8").strip()

    def fill(z: zipfile.ZipFile) -> None:
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
        _add_setup_evidence(
            z, paths, skipped, setup_dir or setup_data_dir(), temp_dir or Path(tempfile.gettempdir())
        )
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

    # built beside the final name and swapped in whole: the Desktop copy (OneDrive, antivirus, the partner)
    # never meets a zip that is still being written (the freeze steps take seconds), and a second bundle in
    # the same minute does not tear the first
    write_zip_atomic(out, fill)
    return out


def diagnose(
    paths: Paths, client: SupervisorClient, on_wait: Callable[[], None] | None = None
) -> list[tuple[str, bool, str]]:
    """Quick health table for plain `ns doctor`: (check, ok, detail-or-fix-hint). The last row runs the
    start check (`program_folders`); `on_wait` is called once if it has to wait for another program."""
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
    rows.append(launch_reach.doctor_row(paths))
    rows.append(program_folders(paths, Path(st.app_path) if st.app_path else repo_root(), on_wait))
    return rows


#: The log events that say the start check, an update or a rollback changed something beside the program.
DONE_EVENTS = (first_start.SWAP_EVENT, "rollback_recovered")


def _last_done(paths: Paths) -> str:
    """The newest thing a start, an update or a rollback did about an interrupted rollback: a rename of a
    leftover `app.swap` (`first_start.SWAP_EVENT` with `settled` true) or a rollback finished, undone or its
    stale record removed (`rollback_recovered`); from the three newest log files of each, with its time.
    Empty when there is none."""
    newest: tuple[str, str] = ("", "")
    for component in (first_start.LOG_COMPONENT, "update"):
        try:
            files = sorted(paths.logs.glob(f"{component}-*.jsonl"))[-3:]
        except OSError:
            continue
        for f in files:
            try:
                lines = read_text_retry(f).splitlines()
            except OSError:
                continue
            for line in lines:
                if not any(e in line for e in DONE_EVENTS):
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get("event") not in DONE_EVENTS:
                    continue
                data = rec.get("data")
                held = isinstance(data, dict) and data.get("settled") is False  # (a rename that failed)
                if not held:
                    newest = max(newest, (str(rec.get("ts", "")), str(rec.get("msg", ""))))
    return f"{newest[1]} ({newest[0][:16].replace('T', ' ')} UTC)" if newest[1] else ""


def program_folders(
    paths: Paths, app: Path, on_wait: Callable[[], None] | None = None
) -> tuple[str, bool, str]:
    """The row "program folders": what an interrupted `ns update --rollback` left beside the program, and
    everything this run did about it.

    `ns doctor` is a start of the program like any other, so the start's own check runs first
    (`first_start.ensure`): a leftover `app.swap` is given its place, an interrupted rollback is finished or
    undone (settings files rewritten, its record deleted), a missing settings copy is taken. Every sentence
    of that check is in the row ("This check: ..."), not only in the log. While another program holds the
    copy's lock the check waits up to 60 s (`on_wait` is called once, so the terminal is not silent), and
    the row then says it did not look. FAIL while `app.swap` is still there (held open, or not a program
    folder), with the way out, or when a rollback could not be finished or undone; ok, naming each folder,
    while a folder that was set aside (`app.swap-left-<stamp>`) waits for the owner to delete it; else ok,
    with the last thing that was done before this run, if any."""
    told: list[tuple[str, str]] = []
    first_start.ensure(paths.config, told=told, on_wait=on_wait)
    if any(event == "first_start_lock_busy" for event, _ in told):
        return (
            "program folders",
            False,
            f"Another Nightshift program (an update or a rollback) kept the settings copy busy for "
            f"{config_snapshot.LOCK_WAIT_S:.0f} s, so this check did not look at the program folders and "
            "changed nothing. Run ns doctor again when that program has finished.",
        )
    did = " ".join(msg for _, msg in told)
    failed = any(event in ("rollback_recovery_failed", "first_start_check_failed") for event, _ in told)
    waiting = rollback_intent.swap_note(app)
    left = [rollback_intent.left_sentence(f) for f in rollback_intent.left_folders(app) if str(f) not in did]
    # (and the settings a stale rollback record held, saved beside the program before the record went)
    left += [
        rollback_intent.saved_settings_sentence(f, paths.config)
        for f in rollback_intent.saved_settings(app) if str(f) not in did
    ]
    now = f"This check: {did}" if did else ""
    if waiting or left or failed:
        said = [waiting, *left, now]
        return ("program folders", not (waiting or failed), " ".join(part for part in said if part))
    if now:
        return ("program folders", True, f"ok. {now}")
    last = _last_done(paths)
    return ("program folders", True, f"ok. Last: {last}" if last else "ok")
