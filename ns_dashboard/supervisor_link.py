"""Loopback client for the supervisor API (spec 15.3) used by the dashboard (component C9).

This is the only module in `ns_dashboard` that imports an HTTP client.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator
from typing import Any

import httpx

from ns_common.acquire import Acquirer, Endpoint, Service
from ns_common.config import load_runtime
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_dashboard.errors import ApiError, SupervisorUnavailable
from ns_dashboard.events import EventHub

DEFAULT_SUPERVISOR_PORT = 8099
RESERVED_HUB_KINDS = frozenset({"dashboard_job"})  # published by the dashboard itself, never relayed
GPU_IN_USE = "A video job is still using the GPU; stop it from the Queue instead."


def supervisor_url(paths: Paths) -> str:
    """`NS_SUPERVISOR_URL`, else the supervisor port from `runtime.json`, else 8099 on loopback."""
    env = os.environ.get("NS_SUPERVISOR_URL")
    if env:
        return env.rstrip("/")
    port = DEFAULT_SUPERVISOR_PORT
    try:
        port = load_runtime(paths).supervisor_port
    except (NsError, ValueError, OSError):
        pass
    return f"http://127.0.0.1:{port}"


class SupervisorLink:
    """Thin client: status, warmup, shutdown, stop_all and the `/events` stream."""

    def __init__(self, base_url: str, *, client: httpx.Client | None = None, timeout_s: float = 2.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._client = client or httpx.Client()

    def status(self) -> dict[str, Any] | None:
        """`GET /status`, or None when the supervisor does not answer."""
        try:
            r = self._client.get(f"{self.base_url}/status", timeout=self.timeout_s)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _post(self, path: str, body: dict[str, Any], timeout_s: float | None = None) -> dict[str, Any]:
        try:
            wait = timeout_s or self.timeout_s * 5
            r = self._client.post(f"{self.base_url}{path}", json=body, timeout=wait)
        except httpx.HTTPError as e:
            raise SupervisorUnavailable(str(e)) from e
        if r.status_code >= 400:
            msg = f"Engine answered {r.status_code} for {path}"
            raise ApiError(502, "engine_error", msg, hint=r.text[:200])
        try:
            data = r.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def warmup(self, service: str = "llama") -> dict[str, Any]:
        """`POST /warmup`; returns `{state}`."""
        return self._post("/warmup", {"service": service})

    def shutdown(self, grace_s: int = 30) -> dict[str, Any]:
        """`POST /shutdown`; the supervisor exits after stopping its children."""
        return self._post("/shutdown", {"grace_s": grace_s})

    def stop_all(self) -> dict[str, Any]:
        """`POST /stop_all`."""
        return self._post("/stop_all", {})

    def heartbeat(self, owner: str) -> bool:
        """`POST /heartbeat`: tell the engine `owner` is alive (restarts its idle clock). Never raises;
        False when the engine does not answer."""
        try:
            return bool(self._post("/heartbeat", {"owner": owner}).get("ok"))
        except (SupervisorUnavailable, ApiError):
            return False

    def release_owner(self, owner: str, *, unless_alive: bool = False) -> bool:
        """`POST /release_owner`: free the lease and queue place of `owner`; True if it freed something.

        With `unless_alive` (the Release button) the supervisor refuses while `owner` still sends heartbeats,
        and this raises ApiError 409 `gpu_in_use`.
        """
        body: dict[str, Any] = {"owner": owner}
        if unless_alive:
            body["unless_alive"] = True
        data = self._post("/release_owner", body)
        if data.get("refused"):
            raise ApiError(409, "gpu_in_use", GPU_IN_USE)
        return bool(data.get("ok"))

    def events(self, stop: threading.Event) -> Iterator[dict[str, Any]]:
        """Parse the `/events` SSE stream into dicts until `stop` is set or the stream ends.

        Raises httpx.HTTPError when the connection fails.
        """
        timeout = httpx.Timeout(5.0, read=20.0)
        with self._client.stream("GET", f"{self.base_url}/events", timeout=timeout) as r:
            r.raise_for_status()
            for line in r.iter_lines():
                if stop.is_set():
                    return
                if not line.startswith("data:"):
                    continue
                try:
                    item = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(item, dict):
                    yield item


class EventRelay:
    """Background thread copying supervisor events into the dashboard `EventHub`."""

    def __init__(self, link: SupervisorLink, hub: EventHub, log: NsLogger, *, retry_s: float = 3.0) -> None:
        self.link = link
        self.hub = hub
        self.log = log
        self.retry_s = retry_s
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start relaying (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="ns-dashboard-relay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop relaying."""
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(1.0)
        self._thread = None

    def relay_once(self) -> int:
        """Relay one connection's worth of events; returns how many were published.

        The supervisor's own kind (`service`, `job`, `update`, `run`) is kept so consumers can tell them
        apart. Only a missing kind, or the dashboard's reserved `dashboard_job` kind (which a supervisor
        event must never be able to pose as), is published as `service`.
        """
        n = 0
        for item in self.link.events(self._stop):
            kind = item.get("kind")
            if not isinstance(kind, str) or not kind or kind in RESERVED_HUB_KINDS:
                kind = "service"
            self.hub.publish(kind, item)
            n += 1
        return n

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.relay_once()
            except (httpx.HTTPError, OSError) as e:
                self.log.debug("relay_retry", str(e))
            self._stop.wait(self.retry_s)


def _code(r: httpx.Response) -> str | None:
    """The `code` of an engine error answer, None when it has none."""
    try:
        data = r.json()
    except ValueError:
        return None
    return str(data["code"]) if isinstance(data, dict) and data.get("code") else None


def _detail(r: httpx.Response) -> str:
    """The `detail` of an engine error answer (`{code, message, detail}`), else its first 200 characters."""
    try:
        data = r.json()
    except ValueError:
        return r.text[:200]
    if isinstance(data, dict) and data.get("detail"):
        return str(data["detail"])
    return r.text[:200]


class HttpAcquirer:
    """`ns_common.acquire.Acquirer` over the supervisor HTTP API (used when SupervisorClient is absent)."""

    def __init__(self, base_url: str, *, client: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client()

    def acquire(
        self,
        service: Service,
        owner: str,
        *,
        model_alias: str | None = None,
        timeout_s: int = 7200,
        pid: int | None = None,
        vram_wait: bool = False,
    ) -> Endpoint:
        """Block until the supervisor grants `service` to `owner`; `pid`, `vram_wait`: as SupervisorClient."""
        body: dict[str, Any] = {"service": service, "owner": owner, "wait": True, "timeout_s": timeout_s}
        if model_alias:
            body["model_alias"] = model_alias
        if pid is not None:
            body["pid"] = pid
        if vram_wait:
            body["vram_wait"] = True
        try:
            r = self._client.post(f"{self.base_url}/acquire", json=body, timeout=timeout_s + 30)
            if r.status_code == 409:
                # the engine's NS-E201 (the graphics card is held by another task) or NS-E208 (other programs
                # hold its memory): not a crashed service
                code = "NS-E208" if _code(r) == "NS-E208" else "NS-E201"
                raise NsError(code, f"acquire {service}: {_detail(r)}")
            r.raise_for_status()
            data = r.json()
        except httpx.TimeoutException as e:
            raise NsError("NS-E201", f"{service} not granted within {timeout_s} s") from e
        except (httpx.HTTPError, ValueError) as e:
            raise NsError("NS-E202", f"acquire {service}: {e}") from e
        return Endpoint(base_url=data["endpoint"], model_alias=data.get("model_alias"))

    def release(self, service: Service, owner: str) -> None:
        """Give `service` back; failures are ignored (the supervisor also reaps on its own)."""
        try:
            self._client.post(
                f"{self.base_url}/release", json={"service": service, "owner": owner}, timeout=10.0
            )
        except httpx.HTTPError:
            pass


def make_acquirer(base_url: str) -> Acquirer:
    """`ns_supervisor.client.SupervisorClient` when importable, else `HttpAcquirer`."""
    try:
        from ns_supervisor.client import SupervisorClient  # type: ignore[import-not-found]

        return SupervisorClient(base_url)
    except (ImportError, TypeError):
        return HttpAcquirer(base_url)
