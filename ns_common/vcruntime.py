"""C3: the Microsoft Visual C++ 2015-2022 runtime check (standard library only: the setup server uses it).

The llama.cpp and qwentts.cpp programs are built against the dynamic MSVC runtime. On a clean Windows 11
without the redistributable they do not start ("MSVCP140.dll was not found"), and the symptom first shows
at the smoke test after the whole download. Setup checks these three files in ``System32`` up front.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Files the redistributable puts in System32 (x64): the C++ library, the CRT and its second half.
REQUIRED_DLLS: tuple[str, ...] = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")
#: Where the current redistributable comes from; ``manifest.json`` ``bins.vcredist`` pins the resolved URL.
REDIST_ALIAS = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
WINGET_ID = "Microsoft.VCRedist.2015+.x64"


def system_dir() -> Path:
    """System32 under ``%SystemRoot%`` (``C:/Windows/System32`` when the variable is missing)."""
    return Path(os.environ.get("SystemRoot") or os.environ.get("windir") or r"C:\Windows") / "System32"


def missing(directory: Path | str | None = None) -> list[str]:
    """The runtime DLLs that are not in ``directory`` (default: System32). Empty when the runtime is there."""
    base = Path(directory) if directory is not None else system_dir()
    return [name for name in REQUIRED_DLLS if not (base / name).is_file()]
