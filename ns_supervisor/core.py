"""C2 supervisor core: GPU lock, tenant swap, health polling, crash policy, idle unload, auto-exit.

Plain asyncio, no web framework: `app.py` wraps it for HTTP (spec 15.3). Timers live in
`SupervisorConfig` so tests can shrink them.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from ns_common.atomic import write_text_atomic
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_supervisor import notify, presence, procs
from ns_supervisor.gpu import GpuProbe, NullGpuProbe, NvidiaSmiGpuProbe
from ns_supervisor.lock import GpuLock
from ns_supervisor.schedule_cfg import read_run_time, seconds_until_next_run
from ns_supervisor.services import SERVICES, ServiceDef, build_service

Resolver = Callable[[str, str | None], ServiceDef]


@dataclass(frozen=True)
class SupervisorConfig:
    """Timers and limits (seconds). Defaults are the spec 6.1 / 6.2 values."""

    idle_unload_s: float = 600.0
    health_interval_s: float = 0.5
    health_timeout_s: float = 120.0
    term_grace_s: float = 10.0
    crash_window_s: float = 600.0
    crash_max: int = 3
    monitor_interval_s: float = 1.0
    auto_exit_idle_s: float = 1800.0
    post_run_grace_s: float = 60.0  # after the nightly run reports it is done, before the engine leaves
    update_poll_s: float = 3600.0  # how often the weekly update check is asked whether it is due
    schedule_lookahead_s: float = 3600.0
    lease_expiry_s: float = 600.0  # a `studio:` or Assistant GPU lease silent for this long is reclaimed
    suspend_gap_s: float = 60.0  # a monitor pass this late means the PC slept: leases start over
    release_alive_s: float = 90.0  # the dashboard's Release is refused while the holder beat this recently


#: Owners that must heartbeat while they hold the GPU (the studio does, see `ns_studio.acquirer`; so does the
#: Assistant during a turn, see `ns_dashboard.assistant.llm`). A hold that stays silent for `lease_expiry_s`
#: is reclaimed. Other owners (voice previews, `ns verify`) hold the lock for one short call and never expire.
LEASED_PREFIXES: tuple[str, ...] = ("studio:", "dashboard:assistant")
#: Owners whose heartbeat also counts as activity for the idle exit, whether or not they hold the GPU at that
#: moment: a nightly run or a dashboard job spends most of its time in clips, render and QC with no service
#: held, and the engine must not leave under it (gap L1).
ACTIVITY_PREFIXES: tuple[str, ...] = ("studio:", "dashboard:job")
#: `/status` says `busy` for this long after the last acquire, release or warmup (a run between two stages).
BUSY_RECENT_S = 120.0
#: How long `write_state` waits for a program that holds `run\supervisor.json` open before it fails (NS-E119).
STATE_WAIT_S = 2.0


@dataclass
class ServiceRuntime:
    """Mutable state of one service."""

    name: str
    definition: ServiceDef
    state: str = "stopped"  # stopped | starting | ready | stopping | error
    proc: procs.Proc | None = None
    owners: set[str] = field(default_factory=set)
    crashes: deque[float] = field(default_factory=deque)
    idle_since: float | None = None
    starting_since: float | None = None
    vram_mb: int | None = None
    error: str | None = None
    abort: bool = False
    op_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Supervisor:
    """Owns the service processes and the GPU lock."""

    def __init__(
        self,
        paths: Paths,
        runtime: Runtime,
        *,
        config: SupervisorConfig | None = None,
        resolver: Resolver | None = None,
        job: procs.JobObject | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = datetime.now,
        starter: Callable[..., procs.Proc] | None = None,
        gpu_probe: GpuProbe | None = None,
        update_check: Callable[[], dict[str, Any] | None] | None = None,
        in_use: Callable[[], bool] | None = None,
    ) -> None:
        self.paths = paths
        self.runtime = runtime
        self.cfg = config or SupervisorConfig()
        self.resolver: Resolver = resolver or (lambda name, alias: build_service(runtime, paths, name, alias))
        self.job = job if job is not None else procs.JobObject()
        self.clock = clock
        self.now = now
        self._start_proc = starter or procs.start
        self.gpu_probe: GpuProbe = gpu_probe or (
            NvidiaSmiGpuProbe() if runtime.profile == "gpu" else NullGpuProbe()
        )
        self.log = NsLogger("supervisor", paths.logs)
        self.lock = GpuLock()
        self.services: dict[str, ServiceRuntime] = {
            n: ServiceRuntime(n, self.resolver(n, None)) for n in SERVICES
        }
        self.exit_requested = asyncio.Event()
        self.shutting_down = False
        self.started_at = clock()
        self.started_wall = datetime.now().isoformat(timespec="seconds")
        self.last_activity = clock()
        self.run_finished_at: float | None = None  # set by the nightly run (gap 25); cleared by new work
        # Weekly `ns update --check` (gap 16): a blocking callable that decides for itself whether a check
        # is due, runs at most once at a time in a worker thread, and returns the result when it checked.
        self.update_check = update_check
        # Is a person looking at the dashboard (window host running, browser connected)? Asked only when the
        # idle timer has run out, so a quiet engine never spawns netstat.
        self.in_use: Callable[[], bool] = in_use or (
            lambda: presence.dashboard_in_use(self.paths, self.services["dashboard"].definition.port)
        )
        self._next_update_poll = 0.0
        # GPU lease: who held the lock at the last look, when it last showed a sign of life, and when the
        # monitor last ran (a long gap means the PC slept, which must not expire a live lease).
        self._lease_owner: str | None = None
        self._lease_beat = clock()
        self._last_tick: float | None = None
        self._update_task: asyncio.Task[None] | None = None
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()

    # ------------------------------------------------------------------ events
    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """New event queue (SSE consumer). Call `unsubscribe` when done."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Drop an event queue."""
        self._subs.discard(q)

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        """Fan out `{ts, kind, data}` to all subscribers (dropped for slow consumers)."""
        ev = {"ts": datetime.now().isoformat(timespec="milliseconds"), "kind": kind, "data": data}
        for q in list(self._subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass

    def _emit(self, rt: ServiceRuntime, event: str, **extra: Any) -> None:
        self.publish("service", {"service": rt.name, "event": event, "state": rt.state, **extra})

    # ------------------------------------------------------------------ state file
    @property
    def state_file(self) -> Path:
        """`run\\supervisor.json`: supervisor pid and child pids, used by `ns stop` when the API is down."""
        return self.paths.home / "run" / "supervisor.json"

    def write_state(self) -> None:
        """Record supervisor and child pids."""
        if self.shutting_down:  # shutdown removes the file; a late start must not bring it back
            return
        children = {n: r.proc.pid for n, r in self.services.items() if r.proc is not None}
        data = {
            "pid": os.getpid(),
            "port": self.runtime.supervisor_port,
            "started_at": self.started_wall,
            "children": children,
        }
        # `ns stop` and `ns status` read it. Called on the event loop, so a program that holds the file (they
        # open it only briefly) costs STATE_WAIT_S at most, then the write fails with NS-E119.
        write_text_atomic(self.state_file, json.dumps(data, indent=2) + "\n", wait_s=STATE_WAIT_S)

    def remove_state(self) -> None:
        """Delete the pid file (clean exit)."""
        self.state_file.unlink(missing_ok=True)

    def close_job(self) -> None:
        """Close the Job Object: the OS kills any child still alive."""
        self.job.close()

    # ------------------------------------------------------------------ helpers
    def _touch(self) -> None:
        self.last_activity = self.clock()

    def _whisper_gpu(self) -> bool:
        return self.runtime.whisper.device == "cuda"

    def _prune(self, rt: ServiceRuntime) -> None:
        cutoff = self.clock() - self.cfg.crash_window_s
        while rt.crashes and rt.crashes[0] < cutoff:
            rt.crashes.popleft()

    def _record_crash(self, rt: ServiceRuntime, why: str) -> None:
        self.log.warn("service_crash", why, service=rt.name)
        rt.crashes.append(self.clock())
        self._prune(rt)
        rt.error = why
        rt.proc = None
        rt.vram_mb = None
        rt.starting_since = None
        rt.state = "error" if len(rt.crashes) >= self.cfg.crash_max else "stopped"
        self.write_state()
        self._emit(rt, "crash", error=why)

    def _reap(self, rt: ServiceRuntime) -> None:
        """Notice a child that exited on its own (not through `_stop_locked`)."""
        p = rt.proc
        if p is not None and not p.alive() and rt.state == "ready":
            code = p.returncode()
            self._record_crash(rt, f"{rt.name} exited with code {code}")

    def _owners_of(self, rt: ServiceRuntime) -> list[str]:
        st = self.lock.state
        owners = list(rt.owners)
        if rt.definition.gpu and st.owner is not None and st.service == rt.name:
            owners.append(st.owner)
        return owners

    def _has_owner(self, rt: ServiceRuntime) -> bool:
        if rt.definition.gpu:
            return self.lock.state.owner is not None and self.lock.state.service == rt.name
        return bool(rt.owners)

    # ------------------------------------------------------------------ start / stop
    async def _stop_locked(self, rt: ServiceRuntime) -> None:
        proc = rt.proc
        if proc is None:
            if rt.state != "error":
                rt.state = "stopped"
            return
        rt.state = "stopping"
        self._emit(rt, "stopping")
        await asyncio.to_thread(procs.terminate, proc, self.cfg.term_grace_s)
        rt.proc = None
        rt.state = "stopped"
        rt.vram_mb = None
        rt.idle_since = None
        rt.starting_since = None
        self.write_state()
        self.log.info("service_stopped", service=rt.name)
        self._emit(rt, "stopped")

    async def _stop(self, rt: ServiceRuntime) -> None:
        async with rt.op_lock:
            await self._stop_locked(rt)

    async def _start_locked(self, rt: ServiceRuntime, d: ServiceDef) -> None:
        rt.definition = d
        rt.state = "starting"
        rt.abort = False
        rt.error = None
        rt.starting_since = self.clock()
        self._emit(rt, "starting")
        self.log.info("service_start", service=rt.name, cmd=d.cmd)
        try:
            proc = await asyncio.to_thread(self._start_proc, d, self.paths.logs, job=self.job)
        except NsError as e:
            self._record_crash(rt, str(e))
            raise
        rt.proc = proc
        self.write_state()
        deadline = self.clock() + self.cfg.health_timeout_s
        try:
            async with httpx.AsyncClient(timeout=2.0) as http:
                while True:
                    if rt.abort:
                        raise NsError("NS-E202", f"start of {rt.name} aborted")
                    if not proc.alive():
                        raise NsError("NS-E202", f"{rt.name} exited with code {proc.returncode()} at startup")
                    try:
                        r = await http.get(d.health_url)
                        if r.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if self.clock() >= deadline:
                        raise NsError(
                            "NS-E203", f"{rt.name} not healthy after {self.cfg.health_timeout_s:.0f}s"
                        )
                    await asyncio.sleep(self.cfg.health_interval_s)
        except BaseException as e:
            await asyncio.to_thread(procs.terminate, proc, self.cfg.term_grace_s)
            if isinstance(e, NsError) and not rt.abort:
                self._record_crash(rt, str(e))
            else:
                rt.proc = None
                rt.state = "stopped"
                rt.starting_since = None
                self.write_state()
                self._emit(rt, "stopped")
            raise
        rt.state = "ready"
        rt.starting_since = None
        rt.vram_mb = await asyncio.to_thread(procs.vram_mb, proc.pid)
        self.write_state()
        self.log.info("service_ready", service=rt.name, pid=proc.pid, vram_mb=rt.vram_mb)
        self._emit(rt, "ready", pid=proc.pid, vram_mb=rt.vram_mb)

    async def _ensure(self, rt: ServiceRuntime, d: ServiceDef) -> None:
        """Make `rt` ready with definition `d`: reuse, restart on variant change, or start."""
        async with rt.op_lock:
            self._reap(rt)
            self._prune(rt)
            p = rt.proc
            if p is not None and p.alive() and rt.state == "ready":
                if rt.definition.variant == d.variant:
                    return
                await self._stop_locked(rt)
            elif p is not None:
                await self._stop_locked(rt)
            if len(rt.crashes) >= self.cfg.crash_max:
                rt.state = "error"
                self._emit(rt, "error")
                raise NsError(
                    "NS-E202",
                    f"{rt.name} crashed {len(rt.crashes)}x in {self.cfg.crash_window_s:.0f}s: {rt.error}",
                )
            await self._start_locked(rt, d)

    async def _swap_to(self, service: str, d: ServiceDef) -> None:
        """Stop every other GPU tenant, then bring `service` up (whisper has no process)."""
        for other, ort in self.services.items():
            if other != service and ort.proc is not None and ort.definition.gpu:
                await self._stop(ort)
        if service != "whisper":
            await self._ensure(self.services[service], d)

    # ------------------------------------------------------------------ API
    def _endpoint(self, service: str, d: ServiceDef | None, alias: str | None) -> dict[str, Any]:
        if d is None:
            return {"endpoint": "inproc", "model_alias": None}
        return {"endpoint": d.base_url, "model_alias": alias}

    def _leave_if_shutting_down(self, owner: str) -> None:
        """Called right after the GPU lock was granted: during shutdown give it back and refuse.

        A waiter queued before `shutdown()` is granted the lock when `stop_all` releases the holder; without
        this it would start a new model process after everything was stopped.
        """
        if self.shutting_down:
            self.lock.release(owner)
            raise NsError("NS-E202", "supervisor is shutting down")

    async def acquire(
        self,
        service: str,
        owner: str,
        model_alias: str | None = None,
        wait: bool = True,
        timeout_s: float = 7200,
    ) -> dict[str, Any]:
        """Hand `owner` the service. GPU services queue FIFO behind the GPU lock (`NS-E201` on timeout),
        stop any other GPU tenant, and start/health-poll the process (`NS-E203`, `NS-E202`)."""
        if self.shutting_down:
            raise NsError("NS-E202", "supervisor is shutting down")
        self._touch()
        self.run_finished_at = None  # somebody is working again: back to the plain idle rule
        if service == "whisper":
            if not self._whisper_gpu():
                return self._endpoint("whisper", None, None)
            await self.lock.acquire(owner, "whisper", timeout_s if wait else 0)
            self._leave_if_shutting_down(owner)
            try:
                await self._swap_to("whisper", self.services["llama"].definition)
            except BaseException:
                self.lock.release(owner)
                raise
            self.publish("service", {"service": "whisper", "event": "acquired", "owner": owner})
            return self._endpoint("whisper", None, None)
        if service not in self.services:
            raise NsError("NS-E202", f"unknown service {service!r}")
        alias = (model_alias or "story") if service == "llama" else None
        d = self.resolver(service, alias)
        rt = self.services[service]
        if not d.gpu:
            rt.owners.add(owner)
            rt.idle_since = None
            try:
                await self._ensure(rt, d)
            except BaseException:
                rt.owners.discard(owner)
                raise
            return self._endpoint(service, d, alias)
        await self.lock.acquire(owner, service, timeout_s if wait else 0)
        self._leave_if_shutting_down(owner)
        rt.idle_since = None
        try:
            await self._swap_to(service, d)
        except BaseException:
            self.lock.release(owner)
            raise
        self.publish("service", {"service": service, "event": "acquired", "owner": owner})
        return self._endpoint(service, d, alias)

    async def release(self, service: str, owner: str) -> bool:
        """Give the service back; the idle timer starts when nobody holds it. False if not held."""
        self._touch()
        st = self.lock.state
        if st.owner == owner and st.service == service:
            self.lock.release(owner)
            rt = self.services.get(service)
            if rt is not None:
                rt.idle_since = self.clock()
            self.publish("service", {"service": service, "event": "released", "owner": owner})
            return True
        rt = self.services.get(service)
        if rt is not None and owner in rt.owners:
            rt.owners.discard(owner)
            if not rt.owners:
                rt.idle_since = self.clock()
            self.publish("service", {"service": service, "event": "released", "owner": owner})
            return True
        return False

    async def release_owner(self, owner: str) -> bool:
        """Free everything `owner` holds or waits for, whatever the service (its process is gone or stuck).

        The lock if it holds it, a place in the queue, and the non-GPU services it leased (`embed`). The idle
        timer of a service nobody holds starts now. False when `owner` held and awaited nothing.
        """
        self._touch()
        freed = False
        st = self.lock.state
        if st.owner == owner:
            service = st.service
            self.lock.release(owner)
            rt = self.services.get(service or "")
            if rt is not None:
                rt.idle_since = self.clock()
            freed = True
        if self.lock.drop_waiters(owner):
            freed = True
        for rt in self.services.values():
            if owner in rt.owners:
                rt.owners.discard(owner)
                if not rt.owners:
                    rt.idle_since = self.clock()
                freed = True
        if freed:
            self.log.warn("owner_released", f"released everything held by {owner}", owner=owner)
            self.publish("service", {"event": "owner_released", "owner": owner})
        return freed

    def beat_age(self, owner: str) -> float | None:
        """Seconds since `owner` last sent a heartbeat while holding the GPU.

        None when `owner` does not hold the lock or is not a leased owner (a foreign program such as
        `opencode` never beats). A lease the monitor has not seen yet counts as just started (0).
        """
        if self.lock.state.owner != owner or not owner.startswith(LEASED_PREFIXES):
            return None
        if self._lease_owner != owner:
            return 0.0
        return max(0.0, self.clock() - self._lease_beat)

    async def release_unless_alive(self, owner: str) -> dict[str, Any]:
        """The dashboard's Release button: `release_owner`, but refused while `owner` is still beating.

        A nightly run that overruns into the morning is not a dashboard job and shows nothing in the Queue;
        freeing its lease would let the next tenant load beside it (or llama idle-unload under it). Refused
        means a heartbeat arrived within `release_alive_s`: `{ok: False, refused: "alive", silent_s}`.
        """
        age = self.beat_age(owner)
        if age is not None and age < self.cfg.release_alive_s:
            self.log.warn("release_refused", f"{owner} sent a heartbeat {age:.0f}s ago", owner=owner)
            return {"ok": False, "refused": "alive", "silent_s": round(age, 1)}
        return {"ok": await self.release_owner(owner)}

    def heartbeat(self, owner: str) -> bool:
        """The holder of the GPU lease is alive. False when `owner` does not hold the lock (any more).

        A beat from a `studio:` run or the dashboard's job worker also restarts the idle clock when that owner
        holds nothing right now (download, render, QC): the 30-minute idle exit would otherwise stop the
        engine under a running job.
        """
        if owner.startswith(ACTIVITY_PREFIXES):
            self._touch()
        if self.lock.state.owner != owner:
            return False
        self._lease_owner = owner
        self._lease_beat = self.clock()
        return True

    async def _expire_lease(self, now: float) -> None:
        """Reclaim the GPU from a leased owner (`studio:`, the Assistant) silent for `lease_expiry_s`.

        The studio child sends one every few seconds while it holds a service (also during a long stage).
        Silence means the process was killed, crashed or hung; without this the lock would stay with a dead
        owner and every later job would wait two hours for nothing.
        """
        st = self.lock.state
        owner = st.owner
        if owner != self._lease_owner:  # the lock changed hands: the new lease starts now
            self._lease_owner = owner
            self._lease_beat = now
            return
        if owner is None or not owner.startswith(LEASED_PREFIXES):
            return
        if any(r.state == "starting" for r in self.services.values()):  # a model load is not silence
            self._lease_beat = now
            return
        silent = now - self._lease_beat
        if silent < self.cfg.lease_expiry_s:
            return
        self.log.warn("lease_expired", f"{owner} sent no heartbeat for {silent:.0f}s", owner=owner)
        await self.release_owner(owner)
        self.publish("service", {"event": "lease_expired", "owner": owner, "silent_s": round(silent, 1)})

    async def stop_all(self, *, include_persistent: bool = False) -> None:
        """Terminate every model service and clear crash state, owners and the GPU lock holder."""
        targets = [
            r for r in self.services.values() if include_persistent or not r.definition.persistent
        ]
        for r in targets:
            if r.state == "starting":
                r.abort = True
        for r in targets:
            await self._stop(r)
            r.crashes.clear()
            r.error = None
            r.owners.clear()
            if r.state == "error":
                r.state = "stopped"
        holder = self.lock.state.owner
        if holder is not None:
            self.lock.release(holder)
        self.publish("service", {"event": "stop_all"})
        self.write_state()

    async def shutdown(self, grace_s: float = 30) -> None:
        """Refuse new work, give holders `grace_s` to release, stop everything, request exit."""
        self.shutting_down = True
        self.publish("service", {"event": "shutdown", "grace_s": grace_s})
        deadline = self.clock() + grace_s
        while self.clock() < deadline and (
            self.lock.state.owner is not None
            or any(r.owners for r in self.services.values() if not r.definition.persistent)
        ):
            await asyncio.sleep(0.05)
        await self.stop_all(include_persistent=True)
        self.remove_state()
        self.exit_requested.set()

    async def warmup(self, service: str) -> str:
        """Start `service` without an owner (idle timer applies). Skipped while the GPU is busy."""
        if self.shutting_down:
            raise NsError("NS-E202", "supervisor is shutting down")
        self._touch()
        if service == "whisper":
            return "stopped"
        if service not in self.services:
            raise NsError("NS-E202", f"unknown service {service!r}")
        alias = "story" if service == "llama" else None
        d = self.resolver(service, alias)
        rt = self.services[service]
        if not d.gpu:
            await self._ensure(rt, d)
            return rt.state
        try:
            await self.lock.acquire("warmup", service, 0)
        except NsError:
            return rt.state
        try:
            await self._swap_to(service, d)
        finally:
            self.lock.release("warmup")
            rt.idle_since = self.clock()
        return rt.state

    # ------------------------------------------------------------------ status
    def status(self) -> dict[str, Any]:
        """The `/status` payload."""
        now = self.clock()
        st = self.lock.state
        gpu = self.gpu_probe.sample()
        services: dict[str, Any] = {}
        for n, r in self.services.items():
            info: dict[str, Any] = {
                "state": r.state,
                "pid": r.proc.pid if r.proc is not None else None,
                "port": r.definition.port,
                "vram_mb": r.vram_mb,
                "model_alias": r.definition.model_alias,
                "owners": sorted(self._owners_of(r)),
                "crashes": len(r.crashes),
                "error": r.error,
            }
            if r.state == "starting" and r.starting_since is not None:
                info["elapsed_s"] = round(now - r.starting_since, 1)
            services[n] = info
        whisper_held = st.owner is not None and st.service == "whisper"
        services["whisper"] = {
            "state": "ready" if whisper_held else "stopped",
            "pid": None,
            "port": None,
            "vram_mb": None,
            "model_alias": None,
            "owners": [st.owner] if whisper_held and st.owner else [],
            "crashes": 0,
            "error": None,
        }
        return {
            "gpu_owner": st.owner,
            "gpu_service": st.service,
            "waiting": [{"owner": o, "service": s} for o, s in self.lock.waiters()],
            "shutting_down": self.shutting_down,
            "uptime_s": round(now - self.started_at, 1),
            "services": services,
            "gpu": gpu.model_dump(mode="json") if gpu is not None else None,
            # which Windows session this engine runs in: the 03:00 task (S4U) starts it in session 0, which
            # has no desktop, so its dashboard cannot open folders or reach the Credential Manager
            "session": {"id": notify.session_id(), "interactive": notify.session_is_interactive()},
            "busy": self.busy(),
        }

    def busy(self) -> bool:
        """True while a run is using the engine: somebody holds or waits for the GPU, a service has an owner
        (the dashboard does not count), or anything asked for a service in the last `BUSY_RECENT_S`."""
        if self.lock.state.owner is not None or self.lock.waiters():
            return True
        if any(self._has_owner(rt) for name, rt in self.services.items() if name != "dashboard"):
            return True
        return self.clock() - self.last_activity < BUSY_RECENT_S

    # ------------------------------------------------------------------ monitor
    async def tick(self) -> None:
        """One monitor pass: reap crashes, idle-unload, auto-exit."""
        now = self.clock()
        if self._last_tick is not None and now - self._last_tick > self.cfg.suspend_gap_s:
            self._lease_beat = now  # the PC slept: the studio could not beat either
        self._last_tick = now
        await self._expire_lease(now)
        for rt in self.services.values():
            if rt.op_lock.locked():
                continue
            self._reap(rt)
            if rt.state == "error":
                self._prune(rt)
                if len(rt.crashes) < self.cfg.crash_max:
                    rt.state = "stopped"
            if rt.proc is None or rt.state != "ready" or rt.definition.persistent:
                continue
            if self._has_owner(rt):
                rt.idle_since = None
                continue
            if rt.idle_since is None:
                rt.idle_since = now
                continue
            if now - rt.idle_since >= self.cfg.idle_unload_s:
                self.log.info("idle_unload", service=rt.name, idle_s=round(now - rt.idle_since, 1))
                await self._stop(rt)
        self._poll_update_check(now)
        await self._check_auto_exit(now)

    def _poll_update_check(self, now: float) -> None:
        """Start the weekly update check when its poll time has come and the last one has finished."""
        if self.update_check is None or self.shutting_down or now < self._next_update_poll:
            return
        if self._update_task is not None and not self._update_task.done():
            return
        self._next_update_poll = now + self.cfg.update_poll_s
        self._update_task = asyncio.create_task(self._run_update_check(self.update_check))

    async def _run_update_check(self, job: Callable[[], dict[str, Any] | None]) -> None:
        try:
            result = await asyncio.to_thread(job)
        except Exception as e:  # a failed check must never hurt the engine
            self.log.warn("update_check_error", str(e))
            return
        if result is not None:
            self.publish("update", dict(result))

    def _busy(self) -> bool:
        return (
            self.lock.state.owner is not None
            or bool(self.lock.waiters())
            or any(r.owners or r.state == "starting" for r in self.services.values())
        )

    def run_due_soon(self) -> bool:
        """True when `config\\schedule.json` says a run starts within the look-ahead window."""
        run_time = read_run_time(self.paths)
        if run_time is None:
            return False
        return seconds_until_next_run(self.now(), run_time) <= self.cfg.schedule_lookahead_s

    def dashboard_open(self) -> bool:
        """True while the dashboard service runs (the Dashboard shortcut starts it; Stop All ends it)."""
        return self.services["dashboard"].state in ("starting", "ready")

    def dashboard_in_use(self) -> bool:
        """True while the dashboard service runs and a window host or a browser is attached to it.

        Reading the Library or Settings is activity: without this the engine's 30-minute idle exit stopped the
        dashboard under an open window. A failing probe counts as "not in use".
        """
        if not self.dashboard_open():
            return False
        try:
            return bool(self.in_use())
        except Exception as e:  # the probe must never break the monitor
            self.log.warn("presence_probe_error", str(e))
            return False

    def note_run_finished(self) -> dict[str, Any]:
        """The nightly run reports it is done (`POST /run_finished`): the engine now leaves after
        `post_run_grace_s` unless the dashboard is open or somebody starts new work (gap 25)."""
        self._touch()
        self.run_finished_at = self.clock()
        self.publish("run", {"event": "finished"})
        self.log.info("run_finished", dashboard_open=self.dashboard_open())
        return {"exit_scheduled": not self.dashboard_open()}

    async def _check_auto_exit(self, now: float) -> None:
        if self.exit_requested.is_set() or self.shutting_down:
            return
        if self._busy():
            self.last_activity = now
            return
        if (
            self.run_finished_at is not None
            and not self.dashboard_open()
            and now - self.run_finished_at >= self.cfg.post_run_grace_s
        ):
            waited = round(now - self.run_finished_at, 1)
            self.log.info("auto_exit", reason="nightly run finished", after_s=waited)
            await self.shutdown(0)
            return
        if now - self.last_activity < self.cfg.auto_exit_idle_s:
            return
        if self.run_due_soon():
            return
        if await asyncio.to_thread(self.dashboard_in_use):     # netstat and tasklist: off the event loop
            idle = round(now - self.last_activity, 1)
            self.log.info("auto_exit_skipped", reason="dashboard is open", idle_s=idle)
            self.last_activity = now         # the page is being read: the idle clock starts again
            return
        self.log.info("auto_exit", idle_s=round(now - self.last_activity, 1))
        await self.shutdown(0)

    async def run_monitor(self) -> None:
        """Background loop; ends when exit is requested."""
        while not self.exit_requested.is_set():
            await asyncio.sleep(self.cfg.monitor_interval_s)
            try:
                await self.tick()
            except Exception as e:  # keep the monitor alive; the failure is logged
                self.log.error("monitor_error", str(e))
