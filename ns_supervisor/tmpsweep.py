"""Start-up sweep of the temporary files a crashed writer left in the home's state folders.

The PowerShell launchers (`launchers/_common.ps1` `Write-NsFileAtomic`) and the Python writers
(`ns_common.atomic`) put a new file in `~ns<pid>-<thread>-<n>.tmp` beside its target and swap it in. A crash
or a power cut between the two leaves that temporary file behind. `run` (the window host's run files, the
engine's state) and `config` (window.json, opencode.json, runtime.json) are written that way and no writer
there ever comes back to clean up, so the engine does it once when it starts (every launch starts or reuses
it).

Only the product's own temp name goes (`ns_common.atomic.sweep_stale_tmp`) and only when it is older than an
hour, so a file a launcher is writing right now is never touched.
"""

from __future__ import annotations

from ns_common.atomic import sweep_stale_tmp
from ns_common.paths import Paths


def sweep_state_tmp(paths: Paths) -> int:
    """Remove stale `~ns*.tmp` files from `<home>/run` and `<home>/config`; returns how many went."""
    return sum(sweep_stale_tmp(folder) for folder in (paths.home / "run", paths.config))
