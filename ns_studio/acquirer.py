"""The studio's real GPU acquirer: asks the supervisor for services (component C7, gap 1).

`studio run` used to build `DevAcquirer`, which only hands out port numbers and starts nothing, so the
nightly run (and any real `studio run`) hit a closed port at 03:00. `StudioAcquirer` is the
`ns_common.acquire.Acquirer` for real runs: on first use it makes sure the supervisor answers (starting it
detached when it does not), then forwards `acquire` / `release` / `warmup` over HTTP (the plumbing panel
"GPU tenant swap" of `docs/design/system-diagram.md`).

J7 (a busy GPU, spec section 22): an acquire waits at most `WAIT_CAP_S` (2 hours) for the GPU; after that
the supervisor answers `NS-E201` and the nightly run skips the night (`ns_studio.daily`).

`ns_supervisor` is imported lazily, inside functions, because C7 reaches C2 over HTTP only (docs/ENGINEERING
section 2.2); the client and the spawn helper are injectable so tests never start a process.
"""

from __future__ import annotations

import importlib
import os
import threading
from collections.abc import Callable
from typing import Any, Protocol

from ns_common.acquire import Endpoint, Service
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.models import Runtime
from ns_common.paths import Paths

WAIT_CAP_S = 7200  # J7: the nightly run waits up to 2 h for a busy GPU
BEAT_S = 30.0  # how often a held lease is renewed; the supervisor reclaims it after 10 minutes of silence
# Services that dev/fake runs take in process; faking any of them means there is nothing for the supervisor
# to start, so `build_services` keeps `DevAcquirer` for those runs.
SUPERVISED: frozenset[str] = frozenset({"llm", "tts", "whisper", "embed"})


class SupervisorApi(Protocol):
    """The part of `ns_supervisor.client.SupervisorClient` the studio uses."""

    def acquire(
        self, service: Service, owner: str, *, model_alias: str | None = None, timeout_s: int = 7200,
        **night: Any,
    ) -> Endpoint: ...

    def release(self, service: Service, owner: str) -> None: ...

    def warmup(self, service: str) -> str: ...


def wants_supervisor(fake: set[str] | frozenset[str]) -> bool:
    """True when no model service is faked: only then is there a real service to start."""
    return not (set(fake) & SUPERVISED)


class StudioAcquirer:
    """`Acquirer` over the supervisor HTTP API; connects (and starts the supervisor) on first use."""

    def __init__(
        self,
        runtime: Runtime,
        paths: Paths,
        *,
        client: SupervisorApi | None = None,
        ensure: Callable[[Any], None] | None = None,
        wait_cap_s: int = WAIT_CAP_S,
        beat_s: float = BEAT_S,
        log: NsLogger | None = None,
        on_owner: Callable[[str], None] | None = None,
    ) -> None:
        self.runtime = runtime
        self.paths = paths
        self.wait_cap_s = wait_cap_s
        self.beat_s = beat_s
        self._held: set[tuple[str, str]] = set()
        # Told about each owner the first time it asks for a service, before the lease can exist: the
        # dashboard's child reports it so Cancel frees every owner of the job, not only `studio:<job id>`.
        self._on_owner = on_owner
        self._announced: set[str] = set()
        self._run_owner: str | None = None  # the owner that last acquired: it keeps beating between services
        self._beat: threading.Thread | None = None
        self._beat_mu = threading.Lock()
        self._beat_stop = threading.Event()
        self.log = log or NsLogger("studio", paths.logs, echo=False)
        self._client: SupervisorApi | None = client
        self._ensure = ensure
        self._ready = False
        # vram-busy: `vram_wait` is set by the nightly (`daily.run_daily`), a run nobody watches: its loads
        # may wait for the graphics card's memory inside the acquire's own time. Every other run (a dashboard
        # job, `studio run` by hand) is told at once (NS-E208). `send_pid` (the nightly, and a dashboard job's
        # child: `ns_dashboard.studio_job`) names this process at each acquire, so the engine never counts the
        # studio's own memory (whisper's context) as another program's, never names it "python.exe", and
        # frees the card at once when this process dies.
        self.vram_wait = False
        self.send_pid = False

    # ------------------------------------------------------------------ connection
    def _connect(self) -> SupervisorApi:
        """The client, after making sure the supervisor answers (NS-E202 when it cannot be reached)."""
        client = self._client
        if client is None:
            client = self._supervisor_module().SupervisorClient.from_runtime(self.runtime)
            self._client = client
        if not self._ready:
            if self._ensure is not None:
                self._ensure(client)
            else:
                self._supervisor_module().ensure_running(client, self.paths, headless=True)
            self._ready = True
            self.log.info("supervisor_ready")
        return client

    @staticmethod
    def _supervisor_module() -> Any:
        try:
            return importlib.import_module("ns_supervisor.client")
        except ImportError as e:
            raise NsError("NS-E202", f"the supervisor client is not installed here: {e}") from e

    @property
    def client(self) -> SupervisorApi | None:
        """The connected client, or None before the first call (used by the end-of-run signal)."""
        return self._client if self._ready else None

    # ------------------------------------------------------------------ Acquirer
    def acquire(
        self, service: Service, owner: str, *, model_alias: str | None = None, timeout_s: int = WAIT_CAP_S
    ) -> Endpoint:
        """Hand `owner` the service; GPU services wait for a busy GPU for at most `wait_cap_s` seconds."""
        self._announce(owner)
        client = self._connect()
        wait = max(1, min(int(timeout_s), self.wait_cap_s))
        try:
            ep = self._acquire_once(client, service, owner, model_alias, wait, self._night())
        except NsError as e:
            if not self._unreachable(e):
                self.log.warn(
                    "acquire_failed", str(e), service=service, owner=owner, code=e.code, wait_s=wait
                )
                raise
            # The engine is gone (it left on its idle timer, or crashed). Connect again: `ensure_running`
            # starts a new one in the nightly run (gap L1), so one lost engine does not fail every later job.
            self.log.warn("supervisor_lost", str(e), service=service, owner=owner)
            self._ready = False
            client = self._connect()
            try:
                ep = self._acquire_once(client, service, owner, model_alias, wait, self._night())
            except NsError as e2:
                self.log.warn(
                    "acquire_failed", str(e2), service=service, owner=owner, code=e2.code, wait_s=wait
                )
                raise
        self._hold(service, owner)
        self.log.info("acquired", service=service, owner=owner)
        return ep

    def _announce(self, owner: str) -> None:
        """Call `on_owner` once per owner. It runs before the acquire: a child killed while it waits for the
        lease would otherwise leave an owner nobody knows to release."""
        if self._on_owner is None or owner in self._announced:
            return
        self._announced.add(owner)
        self._on_owner(owner)

    def _night(self) -> dict[str, Any]:
        """The keywords only the engine's own clients take: `vram_wait`, `pid` (none: a plain acquire)."""
        night: dict[str, Any] = {}
        if self.vram_wait:
            night["vram_wait"] = True
        if self.send_pid or self.vram_wait:
            night["pid"] = os.getpid()
        return night

    @staticmethod
    def _acquire_once(
        client: SupervisorApi, service: Service, owner: str, model_alias: str | None, wait: int,
        night: dict[str, Any],
    ) -> Endpoint:
        return client.acquire(service, owner, model_alias=model_alias, timeout_s=wait, **night)

    @staticmethod
    def _unreachable(e: NsError) -> bool:
        """True for the client's "supervisor unreachable" answer (a transport failure, not a refusal)."""
        return e.code == "NS-E202" and "unreachable" in str(e)

    def release(self, service: Service, owner: str) -> None:
        """Give the service back. A supervisor that is gone must not hide the error that got us here."""
        if not self._ready or self._client is None:
            return
        self._drop(service, owner)
        try:
            self._client.release(service, owner)
        except NsError as e:
            self.log.warn("release_failed", str(e), service=service, owner=owner)

    # ------------------------------------------------------------------ lease heartbeat
    def _hold(self, service: str, owner: str) -> None:
        """Remember a held service and make sure the heartbeat thread runs."""
        with self._beat_mu:
            self._held.add((service, owner))
            self._run_owner = owner
            if self._beat is None or not self._beat.is_alive():
                self._beat_stop.clear()
                self._beat = threading.Thread(target=self._beat_loop, name="ns-lease-beat", daemon=True)
                self._beat.start()

    def _drop(self, service: str, owner: str) -> None:
        with self._beat_mu:
            self._held.discard((service, owner))

    def stop_beat(self) -> None:
        """End the heartbeat thread (the run is finished); a later `acquire` starts it again."""
        self._beat_stop.set()
        thread = self._beat
        if thread is not None and thread is not threading.current_thread():
            thread.join(1.0)

    def _beat_loop(self) -> None:
        """Renew the lease of every owner that holds a service, from the first `acquire` until the run ends.

        This runs on its own thread so a stage that takes an hour still beats. It keeps going when nothing is
        held (clip download, render, QC, outbox): the owner of the run still beats, which tells the engine the
        run is alive and restarts its idle clock (gap L1). A failed beat is only logged: the supervisor may be
        restarting, and the next beat will tell.
        """
        while not self._beat_stop.wait(self.beat_s):
            with self._beat_mu:
                owners = sorted({o for _s, o in self._held})
                if not owners and self._run_owner is not None:
                    owners = [self._run_owner]
            beat = getattr(self._client, "heartbeat", None)
            if beat is None:
                continue
            for owner in owners:
                try:
                    if not beat(owner):
                        self.log.debug("lease_beat_unheld", owner=owner)
                except NsError as e:
                    self.log.warn("lease_beat_failed", str(e), owner=owner)

    def run_finished(self) -> bool:
        """Tell the supervisor the nightly run is done so the engine can leave (gap 25). Best effort; False
        when this run never used the supervisor or it is gone."""
        self.stop_beat()
        client = self.client
        signal = getattr(client, "run_finished", None)
        if signal is None:
            return False
        try:
            return bool(signal())
        except NsError as e:
            self.log.warn("run_finished_failed", str(e))
            return False

    def warmup(self, service: str) -> str:
        """Start `service` ahead of time (`POST /warmup`); the state, or `unavailable` when it cannot."""
        try:
            return self._connect().warmup(service)
        except NsError as e:
            self.log.warn("warmup_failed", str(e), service=service)
            return "unavailable"
