r"""C2: the update worker runs from a private copy of the supervisor venv, not from `venvs\supervisor`.

Why: the worker re-installs the app into `venvs\supervisor` (`uv pip install -e`). A worker started with that
venv's own `python.exe` has the venv's interpreter, its DLLs and its `.pyd` files open while uv removes and
rewrites files in the same folders (`Scripts\ns.exe`, and any dependency a release bumps). Windows refuses to
replace a file that a running program has open (WinError 5 or 32), so the re-sync failed with NS-E702 and the
rollback's own re-sync hit the same lock.

So the caller makes a copy of the venv under `<home>\run\updater\venv-<stamp>` and starts the worker with the
copy's `python.exe`. The copy's `pyvenv.cfg` still names the base interpreter, and its editable-install `.pth`
points at the app folder, so the worker imports the same code as before; no file under `venvs\` is open in it.
The copy cannot be deleted by its own worker, but a detached helper does it once the worker has exited after a
successful relaunch (`discard_own_copy`); if that did not happen the next update removes it, and uninstall
removes the whole `run` folder.
"""

from __future__ import annotations

import base64
import os
import shutil
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ns_common.paths import Paths

COPY_PREFIX = "venv-"


def updater_root(paths: Paths) -> Path:
    """`<home>\\run\\updater`."""
    return paths.home / "run" / "updater"


def venv_python(venv: Path) -> Path:
    """`Scripts\\python.exe` of a venv folder."""
    return venv / "Scripts" / "python.exe"


def stage_worker_python(
    paths: Paths,
    *,
    stamp: str | None = None,
    copytree: Callable[..., object] = shutil.copytree,
    rmtree: Callable[..., object] = shutil.rmtree,
) -> Path | None:
    """Copy `venvs\\supervisor` to `run\\updater\\venv-<stamp>`; returns the copy's `python.exe`.

    Returns None when there is no supervisor venv or the copy failed (disk full, a file in use): the caller
    then starts the worker with its own interpreter, as before. Copies left by earlier updates are removed
    first (best effort; one that is still in use stays).
    """
    src = paths.venvs / "supervisor"
    if not venv_python(src).is_file():
        return None
    root = updater_root(paths)
    dest = root / (COPY_PREFIX + (stamp or datetime.now().strftime("%Y%m%d-%H%M%S")))
    try:
        root.mkdir(parents=True, exist_ok=True)
        for old in root.glob(COPY_PREFIX + "*"):
            rmtree(old, ignore_errors=True)
        copytree(src, dest)
    except OSError:                       # shutil.Error is an OSError too
        rmtree(dest, ignore_errors=True)
        return None
    return venv_python(dest)


def is_inside(path: Path | str, folder: Path) -> bool:
    """True when `path` is `folder` or below it (case-insensitive on Windows)."""
    try:
        Path(path).resolve().relative_to(folder.resolve())
    except (ValueError, OSError):
        return False
    return True


def own_copy(paths: Paths, python: str | None = None) -> Path | None:
    """The `venv-<stamp>` folder the interpreter `python` (default: this one) runs from, else None.

    None for a worker that runs from `venvs\supervisor` itself (the copy failed): nothing to delete then.
    """
    root = updater_root(paths)
    exe = Path(python or sys.executable)
    if not is_inside(exe, root):
        return None
    try:
        first = exe.resolve().relative_to(root.resolve()).parts[0]
    except (ValueError, OSError, IndexError):
        return None
    return root / first if first.startswith(COPY_PREFIX) else None


def _quote(text: str) -> str:
    return text.replace("'", "''")


def cleanup_script(folder: Path, pid: int) -> str:
    """PowerShell that waits for process `pid` to exit, then removes `folder` (retried: the files of a
    process that just ended can stay locked for a moment)."""
    return "\n".join(
        [
            "$ErrorActionPreference = 'SilentlyContinue'",
            f"Wait-Process -Id {int(pid)} -Timeout 300",
            f"$t = '{_quote(str(folder))}'",
            "for ($i = 0; $i -lt 60; $i++) {",
            "    if (-not (Test-Path -LiteralPath $t)) { break }",
            "    Remove-Item -LiteralPath $t -Recurse -Force",
            "    if (Test-Path -LiteralPath $t) { Start-Sleep -Seconds 2 }",
            "}",
            "exit 0",
        ]
    )


def discard_own_copy(
    paths: Paths,
    *,
    spawn: Callable[..., int],
    pid: int | None = None,
    python: str | None = None,
) -> Path | None:
    """After a successful relaunch: start a detached helper that deletes the venv copy this worker runs from.

    A process cannot delete its own interpreter, so the helper waits for this worker (`pid`) to exit first.
    `spawn(argv, cwd=...)` starts it detached (`update_job.spawn_detached`). Returns the folder it will
    delete, or None when this worker does not run from a copy. A failure to start the helper is not fatal:
    the next update removes the copy, as before.
    """
    folder = own_copy(paths, python)
    if folder is None:
        return None
    script = cleanup_script(folder, pid if pid is not None else os.getpid())
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden"]
    argv += ["-EncodedCommand", encoded]
    try:
        spawn(argv, cwd=paths.home if paths.home.is_dir() else folder.parent)
    except OSError:
        return None
    return folder
