"""C2: is somebody looking at the dashboard? (the engine's idle exit must not pull the page away)

The 30-minute idle exit only counted GPU work. Reading the Library or Settings for half an hour made the engine
shut down and kill the dashboard under an open window. Two signs of a person at the dashboard, neither of
which needs the dashboard's cooperation:

* the app window host (`launchers\nightshift-host.ps1`) is running: `run\apphost-dashboard.json` names a live
  process;
* a browser holds an established connection to the dashboard port (any open page keeps one: its event stream,
  or at least a keep-alive connection while it polls).
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable

from ns_common.paths import Paths

HOST_RUN_FILE = "apphost-dashboard.json"
_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def host_window_open(paths: Paths, image: Callable[[int], str | None] | None = None) -> bool:
    """True when the dashboard window host named in `run\apphost-dashboard.json` is still running."""
    if image is None:
        from ns_supervisor.control import image_name as image
    try:
        data = json.loads((paths.home / "run" / HOST_RUN_FILE).read_text(encoding="utf-8-sig"))
        pid = int(data["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    name = image(pid)
    return name is not None and ("powershell" in name or "pwsh" in name)


def count_established(netstat_text: str, port: int) -> int:
    """Established connections whose local end is `port`, from `netstat -ano -p TCP` output."""
    count = 0
    for line in netstat_text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].upper() == "TCP" and parts[3].upper() == "ESTABLISHED":
            if parts[1].rsplit(":", 1)[-1] == str(port):
                count += 1
    return count


def netstat_text() -> str:
    """Output of `netstat -ano -p TCP` ("" where it is unavailable)."""
    if sys.platform != "win32":
        return ""
    try:
        r = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"], capture_output=True, text=True, timeout=20, check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout


def browser_connected(port: int, read: Callable[[], str] = netstat_text) -> bool:
    """True when something holds an established connection to the dashboard port."""
    return count_established(read(), port) > 0


def dashboard_in_use(
    paths: Paths,
    port: int,
    *,
    image: Callable[[int], str | None] | None = None,
    read: Callable[[], str] = netstat_text,
) -> bool:
    """A window host is running, or a browser is connected to the dashboard."""
    return host_window_open(paths, image) or browser_connected(port, read)


def default_probe(paths: Paths, port_of: Callable[[], int]) -> Callable[[], bool]:
    """The probe the engine uses: `dashboard_in_use` for the dashboard's current port."""
    return lambda: dashboard_in_use(paths, port_of())


__all__ = [
    "HOST_RUN_FILE", "browser_connected", "count_established", "dashboard_in_use", "default_probe",
    "host_window_open", "netstat_text",
]
