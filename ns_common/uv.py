"""C3: the one place that finds the `uv` executable.

The installer puts uv at `<home>/bin/uv/uv.exe` and never adds it to PATH. Before that copy exists (the
setup window runs the bootstrap steps first) the only uv is the one stage 1 downloaded to
`%LOCALAPPDATA%/Nightshift/bootstrap/uv/uv.exe`; stage 1 hands that path over in
`NIGHTSHIFT_BOOTSTRAP_UV`.
Every caller (installer context, setup window, `ns update`, `ns doctor`) therefore looks, in order, at the
install folder's copy, the hand-over path, the bootstrap folder and only then PATH.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

from ns_common.paths import Paths

BOOTSTRAP_ENV = "NIGHTSHIFT_BOOTSTRAP_UV"


def home_uv(home: Path) -> Path:
    """Where the installer places uv: `<home>/bin/uv/uv.exe`."""
    return home / "bin" / "uv" / "uv.exe"


def bootstrap_uv(env: Mapping[str, str] | None = None) -> Path | None:
    """The uv stage 1 downloaded: the `NIGHTSHIFT_BOOTSTRAP_UV` hand-over, else the bootstrap folder."""
    e = os.environ if env is None else env
    handed = e.get(BOOTSTRAP_ENV)
    if handed and Path(handed).is_file():
        return Path(handed)
    local = e.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    candidate = Path(local) / "Nightshift" / "bootstrap" / "uv" / "uv.exe"
    return candidate if candidate.is_file() else None


def find_uv(
    paths: Paths | Path,
    *,
    which: Callable[[str], str | None] | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Path of uv: install folder, stage-1 hand-over, bootstrap folder, then PATH; None when none has it."""
    home = paths.home if isinstance(paths, Paths) else paths
    local = home_uv(home)
    if local.is_file():
        return str(local)
    boot = bootstrap_uv(env)
    if boot is not None:
        return str(boot)
    return (which or shutil.which)("uv")


def venv_args(venv_dir: Path, python_version: str) -> list[str]:
    """Arguments of ``uv venv`` for a folder whose ``Scripts/python.exe`` is missing.

    uv 0.10 and later refuse to create a venv in a folder that exists ("A virtual environment already exists
    ... use --clear"). A half venv (an uninstall that could not remove all of it, a scanner that quarantined
    ``python.exe``, a crash while it was created) therefore failed every Retry and every ``INSTALL.cmd``.
    The caller only gets here when there is no ``python.exe``, so what is in the folder is garbage that
    belongs to Nightshift: ``--clear`` it, once. A fresh folder gets no flag (older uv versions lack it).
    """
    args = ["venv", "--python", python_version]
    if venv_dir.exists():
        args.append("--clear")
    return [*args, str(venv_dir)]
