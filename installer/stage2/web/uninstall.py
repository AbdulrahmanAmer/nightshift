"""The uninstall flow of the setup window (docs/design/installer-ui.md 2.10). Standard library only.

``UNINSTALL.cmd -Ui`` starts the server with ``--uninstall``. It shows what goes and what stays, then runs
``installer\\uninstall.ps1`` with ``NS_NONINTERACTIVE=1`` so the script never asks.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from installer.stage2.web.bootstrap import HOME_VAR, get_user_variable

SIZE_BUDGET_S = 5.0
NOTE_LABEL = "Windows' note of where Nightshift is installed"      # the NIGHTSHIFT_HOME variable
ALWAYS = (
    ("The nightly task", None),
    ("Desktop shortcuts", None),
    ("Programs and Python environments", ("venvs", "bin", "run")),
    (NOTE_LABEL, None),
)
DATA_DIRS = ("outbox", "rejected", "data", "imports", "cache")
#: `launchers\\nightshift-host.ps1` holds this named mutex for as long as a dashboard window is open.
HOST_MUTEX = "Global\\Nightshift.AppHost.dashboard"
HOST_WAIT_MIN = 10
SETUP_WAIT_S = 1800
LEFTOVERS_FILE = "Nightshift-uninstall-leftovers.txt"
#: Tries (one per second) the deferred cleanup makes per folder: two minutes, for a console that was
#: opened inside it or a window that is slow to exit.
REMOVE_TRIES = 120


def stays_on_pc(home: Path) -> list[str]:
    """What an uninstall never removes (see uninstall.ps1), in plain words, for the closing screen."""
    return [
        f"Your settings and logs, in {home / 'config'} and {home / 'logs'}",
        "Your stock footage keys, in Windows Credential Manager",
        "The Visual C++ runtime, FFmpeg and Windows Terminal, which other programs use",
        "The Python that setup fetched and its download cache (several GB), in your user folders",
    ]


def dir_bytes(path: Path, budget_s: float = SIZE_BUDGET_S) -> int:
    """Total size of a folder tree; stops counting after ``budget_s`` seconds."""
    total = 0
    stop = time.monotonic() + budget_s
    stack = [path]
    while stack and time.monotonic() < stop:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                for entry in it:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def uninstall_argv(app_path: Path, home: Path, delete_models: bool, delete_data: bool) -> list[str]:
    """The uninstall.ps1 command line. A switch is passed only when it is ticked."""
    argv = [
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
        str(app_path / "installer" / "uninstall.ps1"), "-HomeDir", str(home),
    ]
    if delete_models:
        argv.append("-DeleteModels")
    if delete_data:
        argv.append("-DeleteData")
    argv.append("-KeepBin")  # the window holds bin DLLs: bin goes after it exits
    return argv


def ps_quote(value: str) -> str:
    """Escape a value for a single-quoted PowerShell string."""
    return value.replace("'", "''")


def leftovers_path() -> Path:
    """Where the deferred script lists what it could not remove."""
    return Path(tempfile.gettempdir()) / LEFTOVERS_FILE


def deferred_script(home: Path, wait_pids: list[int], local_root: Path | None = None) -> str:
    """PowerShell for the last deletion: wait for every window, then remove ``bin`` and ``run`` (and the
    browser files).

    The WebView2 host (a PowerShell process) keeps its DLLs under ``bin`` loaded while it runs, so the
    uninstall script cannot delete them. This script runs detached, after the window has exited. It waits
    for the setup window's processes (up to 30 minutes) and then for a *dashboard* window, which loads the
    same DLLs, by polling its named mutex (up to ``HOST_WAIT_MIN`` minutes). What still exists after the
    retries is written to ``LEFTOVERS_FILE`` in the temp folder, so nothing is silently left behind.

    ``local_root`` is ``%LOCALAPPDATA%\\Nightshift``: its ``browser`` profile (the setup window may run in
    Edge from it) and ``webview2`` folder go here too, not in the uninstall script.
    """
    ids = ",".join(str(p) for p in wait_pids) or "0"
    # the program folder is a copy of the zip (setup copies it to <home>/app): it goes with the programs
    targets = [home / "bin", home / "run", home / "app", home / "app.new", home / "app.prev"]
    if local_root is not None:
        targets += [local_root / "browser", local_root / "webview2"]
    listed = ", ".join(f"'{ps_quote(str(t))}'" for t in targets)
    home_dir = ps_quote(str(home))
    lines = [
        "$ErrorActionPreference = 'SilentlyContinue'",
        f"foreach ($id in @({ids})) {{ if ($id -gt 0) {{ Wait-Process -Id $id -Timeout {SETUP_WAIT_S} }} }}",
        f"$mutexName = '{HOST_MUTEX}'",
        f"$until = (Get-Date).AddMinutes({HOST_WAIT_MIN})",
        "while ((Get-Date) -lt $until) {",
        "    $m = $null",
        "    try { $open = [System.Threading.Mutex]::TryOpenExisting($mutexName, [ref]$m) }",
        "    catch { $open = $true }",
        "    if ($m) { $m.Dispose() }",
        "    if (-not $open) { break }",
        "    Start-Sleep -Seconds 2",
        "}",
        f"$targets = @({listed})",
        "foreach ($t in $targets) {",
        f"    for ($i = 0; $i -lt {REMOVE_TRIES}; $i++) {{",
        "        if (-not (Test-Path -LiteralPath $t)) { break }",
        "        Remove-Item -LiteralPath $t -Recurse -Force",
        "        if (Test-Path -LiteralPath $t) { Start-Sleep -Seconds 1 }",
        "    }",
        "}",
    ]
    if local_root is not None:
        root = ps_quote(str(local_root))
        lines += [
            f"$localRoot = '{root}'",
            "if ((Test-Path -LiteralPath $localRoot) -and "
            "-not (Get-ChildItem -LiteralPath $localRoot -Force)) {",
            "    Remove-Item -LiteralPath $localRoot -Force",
            "}",
        ]
    lines += [
        f"$home_ = '{home_dir}'",
        "if ((Test-Path -LiteralPath $home_) -and -not (Get-ChildItem -LiteralPath $home_ -Force)) {",
        "    Remove-Item -LiteralPath $home_ -Force",
        "}",
        "$left = @($targets | Where-Object { Test-Path -LiteralPath $_ })",
        f"$report = Join-Path $env:TEMP '{LEFTOVERS_FILE}'",
        "if ($left.Count -gt 0) {",
        "    $text = @('Nightshift could not remove these folders because a Nightshift window or program was "
        "still using them. Close it and delete them by hand:') + $left",
        "    Set-Content -LiteralPath $report -Value $text -Encoding UTF8",
        "} else {",
        "    Remove-Item -LiteralPath $report -Force",
        "}",
        "exit 0",          # a failed Remove-Item above (no report to delete) must not become the exit code
    ]
    return "\n".join(lines)


def local_nightshift() -> Path | None:
    """``%LOCALAPPDATA%\\Nightshift`` (bootstrap, browser profile, webview2), or None without LOCALAPPDATA."""
    base = os.environ.get("LOCALAPPDATA")
    return Path(base) / "Nightshift" if base else None


def host_pids(home: Path) -> list[int]:
    """Pids that may hold files under ``bin``: this server and the setup window host (if it recorded one)."""
    pids = [os.getpid()]
    try:
        data = json.loads((home / "run" / "apphost-setup.json").read_text(encoding="utf-8-sig"))
        pids.append(int(data["pid"]))
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return pids


def spawn_deferred(home: Path, popen: Callable[..., Any] = subprocess.Popen) -> bool:
    """Start the detached PowerShell that finishes the removal after this window exits. True if started."""
    import base64

    script = deferred_script(home, host_pids(home), local_nightshift())
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    flags = 0x00000008 | 0x00000200 | 0x08000000      # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW
    try:
        popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand",
             encoded],
            creationflags=flags, cwd=str(home.parent), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
        )
    except OSError:
        return False
    return True


class UninstallApp:
    """State and the one action of the uninstall window."""

    def __init__(
        self,
        app_path: Path,
        home: Path | None,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        log: Callable[[str], None] = lambda text: None,
        deferred: Callable[[Path], bool] = spawn_deferred,
        env_var: Callable[[str], str | None] = get_user_variable,
    ) -> None:
        self.app_path = app_path
        self.home = home
        self.shutdown = threading.Event()
        self._popen = popen
        self._log = log
        self._deferred = deferred
        self._env_var = env_var
        self._lock = threading.Lock()
        self._running = False
        self._result: dict[str, Any] | None = None
        self._had_updater_copy = False
        self._sizes = self._measure()

    def _measure(self) -> dict[str, Any]:
        if self.home is None or not self.home.is_dir():
            return {"programs": 0, "models": 0, "data": 0}
        return {
            "programs": sum(dir_bytes(self.home / d) for d in ("venvs", "bin", "run")),
            "models": dir_bytes(self.home / "models"),
            "data": sum(dir_bytes(self.home / d) for d in DATA_DIRS),
        }

    def view(self) -> dict[str, Any]:
        """UninstallView."""
        home = self.home
        return {
            "home": str(home) if home else None,
            "always": [
                {"label": label, "bytes": self._sizes["programs"] if dirs else None} for label, dirs in ALWAYS
            ],
            "models": {"path": str(home / "models") if home else "", "bytes": self._sizes["models"]},
            "data": {"path": str(home / "outbox") if home else "", "bytes": self._sizes["data"]},
            "running": self._running,
            "result": self._result,
        }

    def run(self, delete_models: bool, delete_data: bool) -> tuple[int, dict[str, Any]]:
        """``POST /api/uninstall``."""
        with self._lock:
            if self._running or self._result is not None:
                return 409, {"ok": False, "message": "Removal already started", "state": None}
            if self.home is None:
                return 409, {"ok": False, "message": "Nightshift is not installed on this PC", "state": None}
            self._running = True
        threading.Thread(target=self._work, args=(delete_models, delete_data), name="uninstall",
                         daemon=True).start()
        return 200, {"ok": True, "message": None, "state": None}

    def _work(self, delete_models: bool, delete_data: bool) -> None:
        assert self.home is not None
        home = self.home
        self._had_updater_copy = (home / "run" / "updater").exists()
        env = {**os.environ, "NS_NONINTERACTIVE": "1"}
        failure = ""
        try:
            proc = self._popen(
                uninstall_argv(self.app_path, home, delete_models, delete_data), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace",
            )
            for line in proc.stdout:
                if line.strip():
                    self._log(line.rstrip())
            code = proc.wait()
            if code:
                failure = f"The uninstall program stopped with exit code {code}."
        except OSError as e:
            failure = f"The uninstall program could not start: {e}"
        if failure:
            self._log(failure)
        removed, kept = self._outcome(home, delete_models, delete_data, failed=bool(failure))
        with self._lock:
            self._result = {"removed": removed, "kept": kept, "stays": stays_on_pc(home)}
            self._running = False

    def _outcome(
        self, home: Path, delete_models: bool, delete_data: bool, *, failed: bool
    ) -> tuple[list[str], list[dict[str, str]]]:
        """What really went and what really stayed, from the disk after the script finished."""
        removed: list[str] = []
        kept: list[dict[str, str]] = []
        if failed:
            kept.append({"label": "Nightshift (the removal did not finish; run UNINSTALL.cmd again)",
                         "path": str(home)})
        elif self._env_var(HOME_VAR) is None:
            removed += ["The nightly task", "Desktop shortcuts", NOTE_LABEL]
        else:
            kept.append({"label": f"{NOTE_LABEL} (could not be removed)", "path": str(home)})
        if (home / "venvs").exists():
            kept.append({"label": "Python environments (a file is in use)", "path": str(home / "venvs")})
        programs = "Programs and Python environments"
        if (home / "bin").exists():
            # The window itself holds bin\webview2 DLLs: hand the last deletion to a detached PowerShell.
            if self._deferred(home):
                removed.append(
                    f"{programs} (the last files go as soon as every Nightshift window is closed; "
                    f"anything still in use then is listed in {leftovers_path()})"
                )
            else:
                kept.append({"label": "Programs (delete this folder by hand)", "path": str(home / "bin")})
        elif not (home / "venvs").exists():
            removed.append(programs)
        if self._had_updater_copy:  # the private venv copy of the update worker (150-300 MB)
            if (home / "run" / "updater").exists():
                where = str(home / "run" / "updater")
                kept.append({"label": "Update files (a file is in use)", "path": where})
            else:
                removed.append("Update files (the private Python copy that updates use)")
        for name, wiped, folder, label in (
            ("models", delete_models, "models", "AI models"),
            ("outbox", delete_data, "outbox", "Videos and history"),
        ):
            if not wiped:
                kept.append({"label": label, "path": str(home / name)})
            elif (home / folder).exists():
                kept.append({"label": f"{label} (a file is in use)", "path": str(home / folder)})
            else:
                removed.append(label)
        return removed, kept

    def action(self, name: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """``POST /api/action/<name>`` in uninstall mode: close-setup and reveal."""
        if name == "close-setup":
            if self._running:
                return 409, {"ok": False, "message": "Removal is still running", "state": None}
            self.shutdown.set()
            return 200, {"ok": True, "message": None, "state": None}
        if name == "reveal":
            path = body.get("path")
            allowed = {k["path"] for k in (self._result or {}).get("kept", [])}
            if isinstance(path, str) and path in allowed:
                subprocess.Popen(["explorer.exe", path])    # noqa: S603 - fixed program, allow-listed path
                return 200, {"ok": True, "message": None, "state": None}
            return 409, {"ok": False, "message": "That folder cannot be opened from here", "state": None}
        return 409, {"ok": False, "message": f"{name} is not available here", "state": None}
