"""C3: is a local TCP port free, and if not, which program holds it (standard library; psutil when installed).

llama-server, the voice service, the supervisor and the dashboard each bind a fixed port on 127.0.0.1. When
another program already has one (a dev server on 8080, an antivirus web shield, a range Hyper-V or WSL
reserved) the service cannot start, or a foreign server answers its health check, and the failure used to be
"the story model did not pass" with no port named. ``ensure_free`` turns that into ``NS-E116`` that names the
port and the program and says how to fix it.
"""

from __future__ import annotations

import socket
import sys
from collections.abc import Callable, Mapping

from ns_common.errors import NsError

#: The label shown to the user for each port's owner service.
LABELS = {
    "llama": "story model", "embed": "memory index", "tts": "voice", "supervisor": "engine",
    "dashboard": "dashboard",
}


def is_free(port: int, host: str = "127.0.0.1") -> bool:
    """True when ``host:port`` can be bound right now (nothing listens there, the range is not reserved)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if sys.platform != "win32":
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)   # a closed port in TIME_WAIT is free
        try:
            s.bind((host, port))
        except OSError:
            return False
    return True


def owner_of(port: int) -> str | None:
    """``"name.exe (pid 1234)"`` of the program listening on ``port``; None when psutil cannot tell."""
    try:
        import psutil
    except ImportError:
        return None
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status != psutil.CONN_LISTEN or not conn.laddr or conn.laddr.port != port:
                continue
            if conn.pid is None:
                return None
            try:
                return f"{psutil.Process(conn.pid).name()} (pid {conn.pid})"
            except psutil.Error:
                return f"a program (pid {conn.pid})"
    except (psutil.Error, OSError):
        return None
    return None


def port_message(label: str, port: int, owner: str | None) -> str:
    """One sentence: which port, for what, who holds it, what to do."""
    who = owner or "another program or a range Windows reserved (Hyper-V, WSL)"
    return (
        f"Port {port} ({label}) is already in use by {who}. "
        "Close that program, or restart the PC, then press Retry"
    )


def ensure_free(
    ports: Mapping[str, int],
    *,
    host: str = "127.0.0.1",
    free: Callable[[int, str], bool] | None = None,
    owner: Callable[[int], str | None] | None = None,
) -> None:
    """Raise ``NS-E116`` naming every port in ``ports`` (service name -> port) that cannot be used."""
    check = free or is_free
    who = owner or owner_of
    taken = [
        port_message(LABELS.get(name, name), port, who(port))
        for name, port in ports.items() if not check(port, host)
    ]
    if taken:
        raise NsError("NS-E116", " ".join(taken))
