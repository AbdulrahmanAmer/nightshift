"""C2: how long a service may take to answer its health check after it starts (`config/supervisor.json`).

A cold load of the 14 GB story model takes about 30 s from an SSD and several minutes from a hard disk, so the
installer (s08) writes 600 s for a hard disk (or an unknown drive type) and 300 s otherwise. Without the file
the supervisor waits `DEFAULT_HEALTH_TIMEOUT_S`.
"""

from __future__ import annotations

import json
from pathlib import Path

from ns_common.paths import Paths

DEFAULT_HEALTH_TIMEOUT_S = 300.0
HDD_HEALTH_TIMEOUT_S = 600.0


def health_file(paths: Paths) -> Path:
    """Location of supervisor.json."""
    return paths.config / "supervisor.json"


def timeout_for_drive(drive_type: str) -> float:
    """600 s unless the install drive is known to be an SSD, 300 s otherwise."""
    return DEFAULT_HEALTH_TIMEOUT_S if drive_type == "SSD" else HDD_HEALTH_TIMEOUT_S


def read_health_timeout(paths: Paths) -> float:
    """The configured wait in seconds; the default when the file is missing or unusable."""
    try:
        value = float(json.loads(health_file(paths).read_text(encoding="utf-8"))["health_timeout_s"])
    except (OSError, ValueError, KeyError, TypeError):
        return DEFAULT_HEALTH_TIMEOUT_S
    return value if 10.0 <= value <= 3600.0 else DEFAULT_HEALTH_TIMEOUT_S


def write_health_timeout(paths: Paths, seconds: float) -> None:
    """Persist the wait for the supervisor (and so for the nightly run) to read at start."""
    f = health_file(paths)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"health_timeout_s": seconds}, indent=2) + "\n", "utf-8")
