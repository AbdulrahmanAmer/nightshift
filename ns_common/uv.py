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


#: Words of uv's (and Windows') message when the disk is full.
FULL_DISK_HINTS = ("no space left", "os error 112", "not enough space on the disk", "disk is full")


def looks_full(text: str) -> bool:
    """True when uv output says the disk is full."""
    low = text.lower()
    return any(hint in low for hint in FULL_DISK_HINTS)


def cache_dir(home: Path) -> Path:
    """uv's package cache on the install drive: `<home>/cache/uv`."""
    return home / "cache" / "uv"


def uv_env(home: Path) -> dict[str, str]:
    """Environment of every uv call that fills a venv: copy mode and the cache on the install drive.

    uv's default cache is `%LOCALAPPDATA%/uv/cache` on the system drive. It holds the unpacked cuBLAS/cuDNN
    and the other wheels (about 3 GB), which filled a small C: while the install drive had room. When the
    cache folder cannot be made, `UV_CACHE_DIR` is left out and uv uses its default.

    `UV_PYTHON_PREFERENCE=only-managed` makes every venv use the Python that uv installed itself (stage 1
    runs `uv python install 3.12`), never a Python 3.12 that happens to be on the PC: a Microsoft Store Python
    virtualizes writes under %LOCALAPPDATA%, and the setup environment, the studio and supervisor venvs and
    `ns update` must all run on the same interpreter.
    """
    env = {"UV_LINK_MODE": "copy", "UV_PYTHON_PREFERENCE": "only-managed"}
    cache = cache_dir(home)
    try:
        cache.mkdir(parents=True, exist_ok=True)
    except OSError:
        return env
    env["UV_CACHE_DIR"] = str(cache)
    return env


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
