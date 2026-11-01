from __future__ import annotations

import os
import sysconfig
from pathlib import Path

# Folders of the nvidia-cublas-cu12 / nvidia-cudnn-cu12 wheels that hold the DLLs faster-whisper
# (CTranslate2) needs on Windows (NS-E112), relative to site-packages.
_DLL_SUBDIRS = (("nvidia", "cublas", "bin"), ("nvidia", "cudnn", "bin"))

_handles: list[object] = []   # keep os.add_dll_directory handles alive


def dll_dirs(venv_site_packages: Path | None = None) -> list[Path]:
    """The existing cuBLAS / cuDNN `bin` folders under `venv_site_packages` (default: this interpreter's)."""
    base = venv_site_packages or Path(sysconfig.get_paths()["purelib"])
    return [d for d in (base.joinpath(*parts) for parts in _DLL_SUBDIRS) if d.is_dir()]


def ensure_on_path(venv_site_packages: Path | None = None) -> list[Path]:
    """Prepend the cuBLAS and cuDNN `bin` folders to PATH when they exist; return the folders added.

    Idempotent: folders already on PATH are not added twice. On Windows the folders are also
    registered with `os.add_dll_directory` so the loader finds them for Python 3.8+ extension modules.
    """
    entries = os.environ.get("PATH", "").split(os.pathsep)
    present = {os.path.normcase(e) for e in entries if e}
    added: list[Path] = []
    for d in dll_dirs(venv_site_packages):
        if os.path.normcase(str(d)) in present:
            continue
        entries.insert(len(added), str(d))
        added.append(d)
        if hasattr(os, "add_dll_directory"):
            try:
                _handles.append(os.add_dll_directory(str(d)))
            except OSError:
                pass
    if added:
        os.environ["PATH"] = os.pathsep.join(e for e in entries if e)
    return added
