"""C2 test helpers: free ports, polling, stub service definitions and a sync facade over the core.

Used by the supervisor, verify, tune and doctor tests. Not imported by production code.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
import threading
import time
from collections.abc import Callable, Coroutine
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from ns_common.acquire import Endpoint, Service
from ns_common.models import Runtime
from ns_common.paths import Paths, repo_root
from ns_supervisor.core import Supervisor, SupervisorConfig
from ns_supervisor.services import ServiceDef


def free_port() -> int:
    """An unused loopback TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_until(pred: Callable[[], bool], timeout_s: float = 30.0, interval_s: float = 0.05) -> bool:
    """Poll `pred` until true or timeout; returns the final truth value."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(interval_s)
    return pred()


def stub_script() -> Path:
    """Path of the stub service script under tests/fixtures/supervisor."""
    return repo_root() / "tests" / "fixtures" / "supervisor" / "stub_service.py"


def stub_def(
    name: str,
    port: int,
    *,
    gpu: bool = True,
    extra: list[str] | None = None,
    variant: str = "default",
    model_alias: str | None = None,
    persistent: bool = False,
) -> ServiceDef:
    """A ServiceDef that runs the stub service on `port`."""
    cmd = [sys.executable, str(stub_script()), "--port", str(port), "--name", name, *(extra or [])]
    return ServiceDef(
        name=name,
        cmd=cmd,
        host="127.0.0.1",
        port=port,
        gpu=gpu,
        variant=variant,
        model_alias=model_alias,
        persistent=persistent,
    )


class StubTable:
    """Resolver that maps service names to stub definitions on fresh ports."""

    def __init__(
        self,
        *,
        extra: dict[str, list[str]] | None = None,
        gpu: dict[str, bool] | None = None,
        two_model: bool = False,
    ) -> None:
        self.ports = {n: free_port() for n in ("llama", "embed", "tts", "dashboard")}
        self.extra = extra or {}
        self.gpu = {"llama": True, "embed": False, "tts": True, "dashboard": False, **(gpu or {})}
        self.two_model = two_model

    def __call__(self, name: str, alias: str | None) -> ServiceDef:
        variant = "default"
        model_alias = None
        if name == "llama":
            if self.two_model:
                variant = alias or "story"
                model_alias = variant
            else:
                model_alias = "code,story"
        return stub_def(
            name,
            self.ports[name],
            gpu=self.gpu[name],
            extra=list(self.extra.get(name, [])),
            variant=variant,
            model_alias=model_alias,
            persistent=name == "dashboard",
        )


def fast_config(**over: Any) -> SupervisorConfig:
    """Short timers for tests."""
    base: dict[str, Any] = {
        "idle_unload_s": 0.6,
        "health_interval_s": 0.05,
        "health_timeout_s": 20.0,
        "term_grace_s": 5.0,
        "monitor_interval_s": 0.05,
        "auto_exit_idle_s": 3600.0,
    }
    base.update(over)
    return SupervisorConfig(**base)


class SyncSupervisor:
    """Runs a `Supervisor` on a private event-loop thread and exposes blocking calls.

    Implements the `Acquirer` protocol plus the control calls `ns verify` and `ns tune` need, so those
    tools can be tested against real stub processes without FastAPI.
    """

    def __init__(
        self,
        paths: Paths,
        runtime: Runtime,
        *,
        resolver: Callable[[str, str | None], ServiceDef] | None = None,
        config: SupervisorConfig | None = None,
    ) -> None:
        self.sup = Supervisor(paths, runtime, config=config or fast_config(), resolver=resolver)
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()
        self._monitor: Future[None] = asyncio.run_coroutine_threadsafe(self.sup.run_monitor(), self.loop)

    def _run(self, coro: Coroutine[Any, Any, Any], timeout_s: float = 180.0) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout_s)

    def acquire(
        self, service: Service, owner: str, *, model_alias: str | None = None, timeout_s: int = 7200
    ) -> Endpoint:
        """Blocking acquire (Acquirer protocol)."""
        r = self._run(self.sup.acquire(service, owner, model_alias=model_alias, timeout_s=timeout_s))
        return Endpoint(r["endpoint"], r["model_alias"])

    def release(self, service: Service, owner: str) -> None:
        """Blocking release (Acquirer protocol)."""
        self._run(self.sup.release(service, owner))

    def stop_all(self) -> None:
        """Stop every service and clear errors."""
        self._run(self.sup.stop_all())

    def warmup(self, service: str) -> str:
        """Start a service without an owner; returns its state."""
        return str(self._run(self.sup.warmup(service)))

    def status(self) -> dict[str, Any]:
        """Snapshot of `/status`."""
        return self._run(self._status())

    async def _status(self) -> dict[str, Any]:
        return self.sup.status()

    def close(self) -> None:
        """Stop services, the monitor and the loop thread."""
        try:
            self._run(self.sup.stop_all(), 60)
        finally:
            self._monitor.cancel()
            with contextlib.suppress(Exception):
                self._run(asyncio.sleep(0.1), 10)  # let the cancelled monitor task finish
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(timeout=10)
            self.loop.close()
            self.sup.close_job()
