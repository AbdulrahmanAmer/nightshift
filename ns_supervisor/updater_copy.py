r"""C2: the update worker runs from a private copy of the supervisor venv, not from `venvs\supervisor`.

Why: the worker re-installs the app into `venvs\supervisor` (`uv pip install -e`). A worker started with that
venv's own `python.exe` has the venv's interpreter, its DLLs and its `.pyd` files open while uv removes and
rewrites files in the same folders (`Scripts\ns.exe`, and any dependency a release bumps). Windows refuses to
replace a file that a running program has open (WinError 5 or 32), so the re-sync failed with NS-E702 and the
rollback's own re-sync hit the same lock.

So the caller makes a copy of the venv under `<home>\run\updater\venv-<stamp>` and starts the worker with the
copy's `python.exe`. The copy's `pyvenv.cfg` still names the base interpreter, and its editable-install `.pth`
points at the app folder, so the worker imports the same code as before; no file under `venvs\` is open in it.
The copy cannot be deleted by its own worker, so the next update removes it (and uninstall removes the home).
"""

from __future__ import annotations

import shutil
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
