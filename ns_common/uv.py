"""C3: the one place that finds the `uv` executable.

The installer puts uv at `<home>/bin/uv/uv.exe` and never adds it to PATH, so every caller (installer
context, `ns update`, `ns doctor`) looks there first and only then falls back to PATH.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from ns_common.paths import Paths


def home_uv(home: Path) -> Path:
    """Where the installer places uv: `<home>/bin/uv/uv.exe`."""
    return home / "bin" / "uv" / "uv.exe"


def find_uv(paths: Paths | Path, *, which: Callable[[str], str | None] | None = None) -> str | None:
    """Path of uv: the install folder's copy first, then PATH; None when neither has it."""
    home = paths.home if isinstance(paths, Paths) else paths
    local = home_uv(home)
    if local.is_file():
        return str(local)
    return (which or shutil.which)("uv")
