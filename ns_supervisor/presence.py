"""C2: is somebody looking at the dashboard? (the engine's idle exit must not pull the page away)

The 30-minute idle exit only counted GPU work. Reading the Library or Settings for half an hour made the
engine shut down and kill the dashboard under an open window. Two signs of a person at the dashboard,
neither of which needs the dashboard's cooperation:

* the app window host (launchers/nightshift-host.ps1) is running: `run/apphost-dashboard.json` names a live
  process;
* a browser holds an established connection to the dashboard port (any open page keeps one: its event stream,
  or at least a keep-alive connection while it polls). Read from the OS connection table with `psutil` (status
  constants, so the Windows display language does not matter); `netstat -ano` text is only the fallback for a
  table `psutil` cannot read, and only understands the English state names.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Iterable
from typing import Any

import psutil

from ns_common.paths import Paths

HOST_RUN_FILE = "apphost-dashboard.json"
_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def host_window_open(paths: Paths, image: Callable[[int], str | None] | None = None) -> bool:
    """True when the dashboard window host named in `run/apphost-dashboard.json` is still running."""
    if image is None:
        from ns_supervisor.control import image_name as image
    try:
        data = json.loads((paths.home / "run" / HOST_RUN_FILE).read_text(encoding="utf-8-sig"))
        pid = int(data["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    name = image(pid)
    return name is not None and ("powershell" in name or "pwsh" in name)


def count_established_connections(connections: Iterable[Any], port: int) -> int:
    """Established connections whose local end is `port`, from `psutil.net_connections()` entries.

    Only the listening side counts (`laddr.port == port`): the browser's own end has the port as `raddr`.
    """
    count = 0
    for conn in connections:
        laddr = getattr(conn, "laddr", None)
        if laddr and laddr.port == port and getattr(conn, "status", "") == psutil.CONN_ESTABLISHED:
            count += 1
    return count


def tcp_connections() -> list[Any]:
    """The system's TCP connection table. Raises `psutil.Error` or `OSError` when it cannot be read."""
    return list(psutil.net_connections(kind="tcp"))


def count_established(netstat_text: str, port: int) -> int:
    """Fallback: established connections whose local end is `port`, from `netstat -ano -p TCP` output."""
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


def browser_connected(
    port: int,
    read: Callable[[], str] = netstat_text,
    connections: Callable[[], Iterable[Any]] = tcp_connections,
) -> bool:
    """True when something holds an established connection to the dashboard port.

    Uses `connections` (psutil); when the table cannot be read (access denied, an unsupported platform) it
    falls back to parsing `read()`, the text of `netstat -ano -p TCP`.
    """
    try:
        return count_established_connections(connections(), port) > 0
    except (psutil.Error, OSError, NotImplementedError):
        return count_established(read(), port) > 0


def dashboard_in_use(
    paths: Paths,
    port: int,
    *,
    image: Callable[[int], str | None] | None = None,
    read: Callable[[], str] = netstat_text,
    connections: Callable[[], Iterable[Any]] = tcp_connections,
) -> bool:
    """A window host is running, or a browser is connected to the dashboard."""
    return host_window_open(paths, image) or browser_connected(port, read, connections)


def default_probe(paths: Paths, port_of: Callable[[], int]) -> Callable[[], bool]:
    """The probe the engine uses: `dashboard_in_use` for the dashboard's current port."""
    return lambda: dashboard_in_use(paths, port_of())


__all__ = [
    "HOST_RUN_FILE", "browser_connected", "count_established", "count_established_connections",
    "dashboard_in_use", "default_probe", "host_window_open", "netstat_text", "tcp_connections",
]
