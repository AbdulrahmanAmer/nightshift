"""The uninstall flow of the setup window (docs/design/installer-ui.md 2.10). Standard library only.

``UNINSTALL.cmd -Ui`` starts the server with ``--uninstall``. It shows what goes and what stays, then runs
``installer\\uninstall.ps1`` with ``NS_NONINTERACTIVE=1`` so the script never asks.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

SIZE_BUDGET_S = 5.0
ALWAYS = (
    ("The nightly task", None),
    ("Desktop shortcuts", None),
    ("Programs and Python environments", ("venvs", "bin")),
    ("The install folder setting", None),
)
DATA_DIRS = ("outbox", "rejected", "data", "imports", "cache")


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
    return argv


class UninstallApp:
    """State and the one action of the uninstall window."""

    def __init__(
        self,
        app_path: Path,
        home: Path | None,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        log: Callable[[str], None] = lambda text: None,
    ) -> None:
        self.app_path = app_path
        self.home = home
        self.shutdown = threading.Event()
        self._popen = popen
        self._log = log
        self._lock = threading.Lock()
        self._running = False
        self._result: dict[str, Any] | None = None
        self._sizes = self._measure()

    def _measure(self) -> dict[str, Any]:
        if self.home is None or not self.home.is_dir():
            return {"programs": 0, "models": 0, "data": 0}
        return {
            "programs": sum(dir_bytes(self.home / d) for d in ("venvs", "bin")),
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
        env = {**os.environ, "NS_NONINTERACTIVE": "1"}
        removed = ["The nightly task", "Desktop shortcuts", "Programs and Python environments",
                   "The install folder setting"]
        try:
            proc = self._popen(
                uninstall_argv(self.app_path, self.home, delete_models, delete_data), env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace",
            )
            for line in proc.stdout:
                if line.strip():
                    self._log(line.rstrip())
            proc.wait()
        except OSError as e:
            self._log(f"The uninstall program could not start: {e}")
        kept = []
        if not delete_models:
            kept.append({"label": "AI models", "path": str(self.home / "models")})
        else:
            removed.append("AI models")
        if not delete_data:
            kept.append({"label": "Videos and history", "path": str(self.home / "outbox")})
        else:
            removed.append("Videos and history")
        with self._lock:
            self._result = {"removed": removed, "kept": kept}
            self._running = False

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
