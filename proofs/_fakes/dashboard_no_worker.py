"""The install's own Dashboard app on a free port, with its job worker and event relay left stopped.

A proof that needs the Dashboard process to see a different environment (HTTPS_PROXY and SSL_CERT_FILE pointed
at a fake endpoint) cannot change the running dashboard's environment, and must not start a second job
worker. This runs `ns_dashboard.server.create_app(start_worker=False)` (the routes, the services and the
databases of the home in NIGHTSHIFT_HOME, exactly as `python -m ns_dashboard.server` builds them) under the
install's supervisor venv, and writes the port it listens on to the file named by argv[1].

    <home>/venvs/supervisor/Scripts/python.exe dashboard_no_worker.py <port file> [<temp folder>]

Unlike `server.main` it does not call `configure_tmp`, so it sets the temporary folder itself (Starlette
spools an upload to `tempfile.tempdir`): the folder of argv[2], else the TEMP env it was given. It refuses to
start when that folder is not set or is on C:, so no user of this helper can spool uploads to C:.

The proof that starts it stops exactly that pid.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
from pathlib import Path


def temp_folder(argv: list[str], env: dict[str, str]) -> Path | None:
    """The temp folder to use (argv[2], else TEMP), made and resolved; None when it is unset or on C:."""
    raw = argv[2] if len(argv) > 2 else (env.get("TEMP") or env.get("TMP") or "")
    if not raw:
        return None
    folder = Path(raw).resolve()
    if os.path.splitdrive(str(folder))[0].upper() == "C:":
        return None
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        sys.stderr.write("usage: dashboard_no_worker.py <port file> [<temp folder>]\n")
        return 2
    folder = temp_folder(argv, dict(os.environ))
    if folder is None:
        sys.stderr.write("refusing to start: the temp folder is not set or is on C: (give one on D:)\n")
        return 2
    tempfile.tempdir = str(folder)
    os.environ["TEMP"] = os.environ["TMP"] = str(folder)
    import uvicorn

    from ns_dashboard.server import create_app

    app = create_app(start_worker=False)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    port_file = Path(argv[1])
    tmp = port_file.with_name(port_file.name + ".tmp")
    tmp.write_text(str(sock.getsockname()[1]), encoding="ascii")
    tmp.replace(port_file)
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[sock])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
