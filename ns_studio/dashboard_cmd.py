"""`studio dashboard`: make sure the engine and the dashboard service are up, then say where it is (C7).

Spec 15.9 names `studio dashboard [--port 8100] [--open]`. The command is thin on purpose: it starts the
engine the way the nightly run does (detached, headless, through `ns_supervisor.client.ensure_running`), asks
the engine to start the dashboard service (`POST /warmup`, a no-op when it already runs: an engine left by
the nightly task is headless and never starts it by itself) and waits for `GET /api/health`.

The dashboard's port is the `DASHBOARD_PORT` constant of the supervisor's service table, not a setting, so
`--port` only changes the address that is probed and printed (for a forwarded or proxied port).

`ns_supervisor` is imported lazily, inside functions: C7 reaches C2 over HTTP only (docs/ENGINEERING 2.2).
"""

from __future__ import annotations

import importlib
import os
import time
from typing import Any, Protocol

from ns_common.config import load_runtime
from ns_common.errors import NsError
from ns_common.models import Runtime
from ns_common.paths import Paths

DEFAULT_PORT = 8100  # `ns_supervisor.services.DASHBOARD_PORT`; a test keeps the two equal
HEALTH_PATH = "/api/health"
HEALTH_WAIT_S = 30.0


class EngineApi(Protocol):
    """The part of `ns_supervisor.client.SupervisorClient` this command uses."""

    def warmup(self, service: str) -> str: ...


def dashboard_url(port: int) -> str:
    """The address the dashboard answers on."""
    return f"http://127.0.0.1:{port}"


def open_engine(paths: Paths) -> EngineApi:
    """The supervisor client, after making sure the engine answers (it is started headless when it is down).

    Raises `NS-E202` when the engine cannot be started or never answers.
    """
    try:
        client_module: Any = importlib.import_module("ns_supervisor.client")
    except ImportError as e:
        raise NsError("NS-E202", f"the engine is not installed here: {e}") from e
    try:
        runtime = load_runtime(paths)
    except NsError:  # no runtime.json yet: the engine falls back to the default ports as well
        runtime = Runtime.default("gpu")
    client = client_module.SupervisorClient.from_runtime(runtime)
    client_module.ensure_running(client, paths, headless=True)
    return client  # type: ignore[no-any-return]


def probe_health(port: int, wait_s: float = HEALTH_WAIT_S) -> bool:
    """True once `GET /api/health` on `port` answers 200, trying again for up to `wait_s` seconds."""
    import httpx

    url = dashboard_url(port) + HEALTH_PATH
    deadline = time.monotonic() + wait_s
    while True:
        try:
            if httpx.get(url, timeout=2.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.25)


def ensure_dashboard(port: int) -> str:
    """Engine up, dashboard service ready; returns the URL. Raises `NsError` (code, hint) when it cannot."""
    paths = Paths.from_env()
    engine = open_engine(paths)
    state = engine.warmup("dashboard")  # a no-op when it already runs
    if not probe_health(port):
        raise NsError(
            "NS-E203",
            f"the dashboard (service state: {state}) does not answer on {dashboard_url(port)}; "
            f"it always runs on port {DEFAULT_PORT}"
            + ("" if port == DEFAULT_PORT else f", so --port {port} only works through a forwarded port"),
        )
    return dashboard_url(port)


def open_in_browser(url: str) -> bool:
    """Open `url` in the default browser (Windows `os.startfile`); False when that is not possible."""
    starter = getattr(os, "startfile", None)
    if starter is None:
        return False
    try:
        starter(url)
    except OSError:
        return False
    return True
