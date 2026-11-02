"""C2 SupervisorClient: the real `ns_common.acquire.Acquirer` over HTTP (spec 15.3)."""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

import httpx

from ns_common.acquire import Endpoint, Service
from ns_common.errors import NsError, load_errors
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_supervisor.services import venv_python

DEFAULT_URL = "http://127.0.0.1:8099"


class SupervisorClient:
    """Blocking client for the supervisor API. Implements the `Acquirer` protocol."""

    def __init__(
        self, base_url: str = DEFAULT_URL, *, client: httpx.Client | None = None, timeout_s: float = 30
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=timeout_s)

    @classmethod
    def from_runtime(cls, rt: Runtime) -> SupervisorClient:
        """Client for the supervisor port in `runtime.json`."""
        return cls(f"http://127.0.0.1:{rt.supervisor_port}")

    # ------------------------------------------------------------------ plumbing
    @staticmethod
    def _error(r: httpx.Response) -> NsError:
        try:
            body = r.json()
            code = str(body["code"])
            if code in load_errors():
                return NsError(code, str(body.get("detail", "")))
        except (ValueError, KeyError, TypeError):
            pass
        return NsError("NS-E202", f"supervisor answered HTTP {r.status_code}: {r.text[:200]}")

    def _call(
        self, method: str, path: str, *, body: dict[str, Any] | None = None, timeout_s: float | None = None
    ) -> Any:
        try:
            if timeout_s is None:
                r = self.client.request(method, f"{self.base_url}{path}", json=body)
            else:
                r = self.client.request(method, f"{self.base_url}{path}", json=body, timeout=timeout_s)
        except httpx.HTTPError as e:
            raise NsError("NS-E202", f"supervisor unreachable at {self.base_url}: {e}") from e
        if r.status_code >= 400:
            raise self._error(r)
        return r.json()

    # ------------------------------------------------------------------ Acquirer
    def acquire(
        self, service: Service, owner: str, *, model_alias: str | None = None, timeout_s: int = 7200
    ) -> Endpoint:
        """Acquire `service` for `owner`; blocks while the GPU is busy (up to `timeout_s`)."""
        body = {
            "service": service,
            "owner": owner,
            "model_alias": model_alias,
            "wait": True,
            "timeout_s": timeout_s,
        }
        r = self._call("POST", "/acquire", body=body, timeout_s=timeout_s + 240)
        return Endpoint(str(r["endpoint"]), r.get("model_alias"))

    def release(self, service: Service, owner: str) -> None:
        """Release a service acquired earlier."""
        self._call("POST", "/release", body={"service": service, "owner": owner})

    # ------------------------------------------------------------------ extras
    def status(self) -> dict[str, Any]:
        """`GET /status`."""
        return dict(self._call("GET", "/status", timeout_s=10))

    def is_up(self) -> bool:
        """True when the supervisor answers `/status`."""
        try:
            self.status()
        except NsError:
            return False
        return True

    def stop_all(self) -> None:
        """`POST /stop_all`: terminate every model service."""
        self._call("POST", "/stop_all", timeout_s=120)

    def shutdown(self, grace_s: float = 30) -> None:
        """`POST /shutdown`: stop children and exit the supervisor."""
        self._call("POST", "/shutdown", body={"grace_s": grace_s}, timeout_s=grace_s + 120)

    def warmup(self, service: str) -> str:
        """`POST /warmup`: start a service without an owner; returns its state."""
        r = self._call("POST", "/warmup", body={"service": service}, timeout_s=300)
        return str(r["state"])

    def run_finished(self) -> bool:
        """`POST /run_finished`: the nightly run is done; True when the engine will now leave on its own
        (it stays when the dashboard is open)."""
        return bool(self._call("POST", "/run_finished", timeout_s=10).get("exit_scheduled"))

    def publish_event(self, kind: str, data: dict[str, Any]) -> None:
        """`POST /events`: publish job progress to the dashboard status strip."""
        self._call("POST", "/events", body={"kind": kind, "data": data}, timeout_s=10)


def spawn_supervisor(paths: Paths, *, headless: bool = True) -> None:
    """Start `python -m ns_supervisor.app` detached from the caller (survives its exit)."""
    cmd = [venv_python(paths, "supervisor"), "-m", "ns_supervisor.app"]
    if headless:
        cmd.append("--headless")
    flags = 0
    if sys.platform == "win32":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | 0x08000000
    try:
        subprocess.Popen(  # detached on purpose: the engine outlives this command
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    except OSError as e:
        raise NsError("NS-E202", f"cannot start the supervisor: {e}") from e


def ensure_running(
    client: SupervisorClient,
    paths: Paths,
    *,
    headless: bool = True,
    wait_s: float = 30,
    spawn: Callable[[], None] | None = None,
) -> None:
    """Start the supervisor if `/status` is down and wait for it. Raises `NS-E202` if it never answers."""
    if client.is_up():
        return
    (spawn or (lambda: spawn_supervisor(paths, headless=headless)))()
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if client.is_up():
            return
        time.sleep(0.25)
    raise NsError("NS-E202", f"supervisor did not answer at {client.base_url} within {wait_s:.0f}s")
