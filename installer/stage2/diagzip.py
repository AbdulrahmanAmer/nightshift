"""A diagnostics zip the wizard can write by itself (standard library only).

``ns doctor --bundle`` lives in the engine's venv, which does not exist before step s04, so a setup that fails
early (the welcome step, the program copy, the setup environment) could not produce the file OPERATOR.md
tells the partner to send. This writes the same evidence the setup window's fallback collects: the installer
logs, ``install_state.json``, ``answers.json`` (never holds keys on disk), the stage 1 hardware check and
the bootstrap logs from ``%LOCALAPPDATA%\\Nightshift``.
"""

from __future__ import annotations

import datetime as dt
import os
import zipfile
from pathlib import Path

from ns_common.paths import Paths

__all__ = ["write_fallback_bundle"]


def _bootstrap_dir() -> Path | None:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    d = Path(base) / "Nightshift"
    return d if d.is_dir() else None


def write_fallback_bundle(
    paths: Paths, *, when: dt.datetime | None = None, desktop: Path | None = None
) -> Path:
    """Write ``logs\\nightshift-diagnostics-<stamp>.zip`` (and a copy on the Desktop when it exists)."""
    stamp = (when or dt.datetime.now()).strftime("%Y%m%d-%H%M")
    paths.logs.mkdir(parents=True, exist_ok=True)
    out = paths.logs / f"nightshift-diagnostics-{stamp}.zip"
    members: list[tuple[str, Path]] = []
    for pattern in ("*.jsonl", "*.json", "*.err"):
        for f in sorted(paths.logs.glob(pattern)):
            members.append((f"logs/{f.name}", f))
    for name in ("install_state.json", "answers.json", "runtime.json", "supervisor.json"):
        f = paths.config / name
        if f.is_file():
            members.append((f"config/{name}", f))
    boot = _bootstrap_dir()
    if boot is not None:
        for f in sorted(boot.rglob("*")):
            small = f.is_file() and f.stat().st_size < 2_000_000
            if small and f.suffix.lower() in (".json", ".jsonl", ".err", ".txt", ".log"):
                members.append((f"bootstrap/{f.relative_to(boot).as_posix()}", f))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, f in members:
            try:
                z.write(f, arc)
            except OSError:
                continue
    home = desktop if desktop is not None else Path.home() / "Desktop"
    if home.is_dir():
        try:
            (home / out.name).write_bytes(out.read_bytes())
        except OSError:
            pass
    return out
