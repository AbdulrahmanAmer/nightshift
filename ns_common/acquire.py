from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from ns_common.errors import NsError
from ns_common.models import Runtime

Service = Literal["llama", "embed", "tts", "whisper"]
GPU_SERVICES: tuple[Service, ...] = ("llama", "tts", "whisper")


@dataclass(frozen=True)
class Endpoint:
    base_url: str
    model_alias: str | None = None


class Acquirer(Protocol):
    def acquire(
        self, service: Service, owner: str, *, model_alias: str | None = None, timeout_s: int = 7200
    ) -> Endpoint: ...

    def release(self, service: Service, owner: str) -> None: ...


class DevAcquirer:
    """No real process management: hands out the configured endpoints and enforces the
    one-GPU-tenant rule in memory."""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime
        self.held: dict[Service, str] = {}
        self.events: list[tuple[str, Service, str]] = []

    def _gpu_owner(self) -> tuple[Service, str] | None:
        for s in GPU_SERVICES:
            if s in self.held:
                return s, self.held[s]
        return None

    def acquire(
        self, service: Service, owner: str, *, model_alias: str | None = None, timeout_s: int = 7200
    ) -> Endpoint:
        if service in GPU_SERVICES:
            cur = self._gpu_owner()
            if cur and cur[1] != owner:
                raise NsError("NS-E201", f"GPU held by {cur[1]} via {cur[0]}; {owner} wanted {service}")
            if cur and cur[0] != service:
                raise NsError("NS-E201", f"{owner} already holds {cur[0]}; release it before {service}")
            self.held[service] = owner
        self.events.append(("acquire", service, owner))
        if service == "llama":
            return Endpoint(self.runtime.llama.base_url, model_alias or "story")
        if service == "embed":
            return Endpoint(self.runtime.embed.base_url)
        if service == "tts":
            return Endpoint(self.runtime.tts.base_url)
        return Endpoint("inproc")

    def release(self, service: Service, owner: str) -> None:
        if self.held.get(service) == owner:
            del self.held[service]
        self.events.append(("release", service, owner))
