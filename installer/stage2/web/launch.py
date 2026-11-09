"""Opening the setup window, and the files that let a second INSTALL.cmd find the first (design doc 1.4, 1.9).

The order is the WebView2 host (``launchers/nightshift-host.ps1``), then an Edge or Chrome ``--app=`` window,
then the default browser. Standard library only; every outside effect is injectable so the tests need no
window.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ns_common.atomic import read_text_retry, sweep_stale_tmp, write_text_atomic

HOST_UNAVAILABLE = 3
HOST_WAIT_S = 20.0
WEBVIEW2_CORE_DLL = "Microsoft.Web.WebView2.Core.dll"
BROWSER_FLAGS = ("--no-first-run", "--no-default-browser-check")   # else Edge/Chrome may open a welcome page
WINDOW_SIZE = "1366,768"
SERVER_FILE = "setup-server.json"
ADDRESS_FILE = "setup-address.txt"
DEFAULT_STARTFILE: Callable[[str], None] | None = getattr(os, "startfile", None)   # Windows only


def data_dir(env: Mapping[str, str] | None = None) -> Path:
    """``%LOCALAPPDATA%\\Nightshift``: where the server, address and browser profile files live."""
    e = env if env is not None else os.environ
    base = e.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Nightshift"


def sweep_leftover_tmp(*, temp_dir: Path | None = None, env: Mapping[str, str] | None = None) -> int:
    """Remove the ``~ns*.tmp`` files a crashed stage 1 or folder dialog left (over an hour old); the count.

    ``installer/stage1.ps1`` (the preflight hand-over file) and ``installer/pick-folder.ps1`` (the dialog's
    result file) write through a temp file named ``~ns<pid>-0-<n>.tmp`` beside the target in ``%TEMP%``, and
    the server's own files live in ``%LOCALAPPDATA%\\Nightshift``. Only that exact name goes
    (``ns_common.atomic.sweep_stale_tmp``), so nothing else in the shared ``%TEMP%`` is ever touched.
    """
    folders = [temp_dir if temp_dir is not None else Path(tempfile.gettempdir()), data_dir(env)]
    return sum(sweep_stale_tmp(folder) for folder in folders)


def write_server_files(
    folder: Path, *, pid: int, port: int, token: str, app_path: str, url: str
) -> tuple[Path, Path]:
    """Write ``setup-server.json`` and ``setup-address.txt``."""
    folder.mkdir(parents=True, exist_ok=True)
    info = {
        "pid": pid, "port": port, "token": token, "started": datetime.now().isoformat(timespec="seconds"),
        "app_path": app_path,
    }
    server = folder / SERVER_FILE
    address = folder / ADDRESS_FILE
    write_text_atomic(server, json.dumps(info, indent=2) + "\n")
    write_text_atomic(address, url + "\n")
    return server, address


def remove_server_files(folder: Path) -> None:
    """Delete ``setup-server.json`` (clean exit). The address file stays for the next look."""
    (folder / SERVER_FILE).unlink(missing_ok=True)


def read_server_info(folder: Path) -> dict[str, Any] | None:
    """The running server's record, or None."""
    try:
        data = json.loads(read_text_retry(folder / SERVER_FILE, "utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@dataclass
class WindowHandle:
    """Which launcher worked, and the process to watch (None for the default browser)."""

    host: str                                   # webview2 | browser-app | browser | none
    proc: Any = None
    tried: list[str] = field(default_factory=list)
    browser: str = ""                           # Edge or Chrome when host is browser-app


def browser_candidates(env: Mapping[str, str]) -> list[tuple[str, Path]]:
    """Edge, then Chrome, in the install locations the spec lists."""
    pf86 = env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    pf = env.get("ProgramFiles", r"C:\Program Files")
    local = env.get("LOCALAPPDATA", "")
    out = [
        ("Edge", Path(pf86) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
        ("Edge", Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
        ("Chrome", Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe"),
    ]
    if local:
        out.append(("Chrome", Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    return out


def host_argv(app_path: Path, url: str) -> list[str]:
    """The WebView2 host command line. ``-Role setup`` keeps its single-instance mutex and state file apart
    from the dashboard window's, so opening the dashboard while setup is open starts a second window."""
    script = app_path / "launchers" / "nightshift-host.ps1"
    return [
        "powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-Url", url, "-Title", "Nightshift setup", "-AppPath", str(app_path), "-Role", "setup",
    ]


def home_of(env: Mapping[str, str]) -> Path:
    """The install folder the host looks in (NIGHTSHIFT_HOME, else the Nightshift folder in LOCALAPPDATA)."""
    raw = env.get("NIGHTSHIFT_HOME")
    return Path(raw) if raw else data_dir(env)


def webview2_dirs(app_path: Path, env: Mapping[str, str]) -> list[Path]:
    """Where ``nightshift-host.ps1`` looks for the WebView2 DLLs, in its order."""
    return [home_of(env) / "bin" / "webview2", app_path / "bin" / "webview2"]


def host_ready_file(env: Mapping[str, str]) -> Path:
    """``run/apphost-setup.json``: written by the host once its window exists (hwnd and pid)."""
    return home_of(env) / "run" / "apphost-setup.json"


def open_window(
    url: str,
    *,
    app_path: Path,
    env: Mapping[str, str] | None = None,
    popen: Callable[..., Any] = subprocess.Popen,
    exists: Callable[[Path], bool] = lambda p: p.is_file(),
    startfile: Callable[[str], None] | None = DEFAULT_STARTFILE,
    sleep: Callable[[float], None] = time.sleep,
    wait_s: float = HOST_WAIT_S,
    ready: Callable[[], bool] | None = None,
    skip_host: bool = False,
) -> WindowHandle:
    """Open ``url`` in the first launcher that works and say which one it was.

    The WebView2 host is tried only when its DLLs exist: in the install folder, or in ``<app>/bin/webview2``
    (the release bundle ships them there, so even the first window is the host). A source checkout or a
    bundle without them opens Edge or Chrome, and the host takes over once step s02 has downloaded the DLLs.
    The host must show its window (``ready``: it wrote its state file) within ``wait_s`` seconds, or exit 0
    (it raised a window that was already open). Exit 3, any other exit, or silence until the deadline falls
    through to the browser.
    """
    e = dict(env if env is not None else os.environ)
    handle = WindowHandle("none")
    is_ready = ready or (lambda: exists(host_ready_file(e)))

    # 1. the WebView2 host.
    host_script = app_path / "launchers" / "nightshift-host.ps1"
    have_dlls = any(exists(d / WEBVIEW2_CORE_DLL) for d in webview2_dirs(app_path, e))
    if not skip_host and exists(host_script) and have_dlls:
        handle.tried.append("webview2")
        try:
            proc = popen(host_argv(app_path, url), cwd=str(app_path))
            deadline = time.monotonic() + wait_s
            code = proc.poll()
            while code is None and not is_ready() and time.monotonic() < deadline:
                sleep(0.1)
                code = proc.poll()
            if code == 0 or (code is None and is_ready()):
                handle.host, handle.proc = "webview2", proc
                return handle
            if code is None:  # silent for the whole wait: no second window
                kill = getattr(proc, "kill", None)
                if kill is not None:
                    kill()
        except OSError:
            pass

    # 2 and 3. an app window in Edge or Chrome
    profile = data_dir(e) / "browser"
    for name, exe in browser_candidates(e):
        if not exists(exe):
            continue
        handle.tried.append(name.lower())
        try:
            proc = popen(
                [str(exe), f"--app={url}", f"--window-size={WINDOW_SIZE}", f"--user-data-dir={profile}",
                 *BROWSER_FLAGS]
            )
        except OSError:
            continue
        handle.host, handle.proc, handle.browser = "browser-app", proc, name
        return handle

    # 4. the default browser
    if startfile is not None:
        handle.tried.append("default browser")
        try:
            startfile(url)
        except OSError:
            return handle
        handle.host = "browser"
    return handle


def host_label(handle: WindowHandle) -> str:
    """The splash line text for the fourth status line."""
    if handle.host == "webview2":
        return "opened"
    if handle.host == "browser-app":
        return f"opened in {handle.browser}"
    if handle.host == "browser":
        return "opened in your browser"
    return "open the address below in Edge or Chrome"


def update_server_info(folder: Path, **values: Any) -> None:
    """Add keys (such as ``host``) to ``setup-server.json`` once they are known."""
    info = read_server_info(folder) or {}
    info.update(values)
    try:
        write_text_atomic(folder / SERVER_FILE, json.dumps(info, indent=2) + "\n")
    except OSError:
        pass
