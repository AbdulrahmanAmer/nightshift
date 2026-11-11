"""C2 supervisor core: GPU lock, tenant swap, health polling, crash policy, idle unload, auto-exit.

Plain asyncio, no web framework: `app.py` wraps it for HTTP (spec 15.3). Timers live in
`SupervisorConfig` so tests can shrink them.
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import math
import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import psutil

from ns_common import devmode
from ns_common.atomic import write_text_atomic
from ns_common.errors import NsError, describe
from ns_common.logging import NsLogger
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_supervisor import modelcheck, notify, presence, procs, vram
from ns_supervisor.gpu import GpuProbe, NullGpuProbe, SmiSource, simulated_mark, smi_source, source_probe
from ns_supervisor.lock import GpuLock
from ns_supervisor.schedule_cfg import read_run_time, seconds_until_next_run
from ns_supervisor.services import SERVICES, ServiceDef, build_service
from ns_supervisor.services import fake_gpu as fake_gpu_on

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
    vram_poll_s: float = vram.VRAM_POLL_S  # a nightly load waiting for the card's memory looks again then
    vram_settle_poll_s: float = vram.SETTLE_POLL_S  # re-reads while our own stopped tenant gives memory back


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
#: How long `write_state` waits for a program that holds `run\supervisor.json` open before it gives up
#: (NS-E119 in the log; it never raises).
STATE_WAIT_S = 2.0


def process_started(pid: int) -> float | None:
    """Creation time of the running process `pid`, None when no running process has that pid.

    A process that has exited is None even while another program still holds a handle to it (Windows keeps
    such a pid allocated; `psutil.pid_exists` asks for the exit code). Access denied and other psutil errors
    raise: the caller cannot tell, so it must keep the holder.
    """
    try:
        if not psutil.pid_exists(pid):
            return None
        return psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:  # also ZombieProcess, its subclass
        return None


@dataclass
class _Holder:
    """The GPU lock holder that passed its own pid with the acquire (F-H47: a console closed with X)."""

    owner: str
    pid: int
    created: float | None  # creation time at grant; None: not running then, or not readable (see `known`)
    known: bool = True  # False when the probe failed at grant time: the first readable answer is adopted
    warned: bool = False  # a probe failure is logged once per holder


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
        process_probe: Callable[[int], float | None] | None = None,
        model_files: modelcheck.FileCheck | None = None,
    ) -> None:
        self.paths = paths
        # model-file-damaged: the check of a failed load's model files, once per file in this engine
        self.model_files = model_files or modelcheck.FileCheck(paths)
        self.runtime = runtime
        self.cfg = config or SupervisorConfig()
        self.resolver: Resolver = resolver or (lambda name, alias: build_service(runtime, paths, name, alias))
        self.job = job if job is not None else procs.JobObject()
        self.clock = clock
        self.now = now
        self._start_proc = starter or procs.start
        self.log = NsLogger("supervisor", paths.logs)
        # H-10, decided once per engine start from this engine's settings: the simulated graphics card (the
        # switch on a dev or engine-test install). Only nvidia-smi's answers and the service table's `gpu`
        # flags follow it; the lock, guard, lease and release code is the same either way.
        self.fake_gpu = fake_gpu_on(runtime)
        self.fake_gpu_ignored = runtime.fake_gpu and not self.fake_gpu  # NS-W121
        self.smi: SmiSource = smi_source(paths, runtime, tenants=self._gpu_tenants, log=self.log)
        self.gpu_probe: GpuProbe = gpu_probe or (
            source_probe(self.smi) if runtime.profile == "gpu" or self.fake_gpu else NullGpuProbe()
        )
        if self.fake_gpu:
            lease, why = devmode.fake_gpu_lease_s(os.environ, self.cfg.lease_expiry_s)
            if why is not None:
                self.log.warn("fake_gpu_lease", why, variable=devmode.FAKE_GPU_LEASE_ENV)
            self.cfg = dataclasses.replace(self.cfg, lease_expiry_s=lease)
            self.log.info(
                "fake_gpu_on",
                f"the engine runs on the simulated graphics card (fake_gpu); its numbers come from "
                f"{devmode.fake_gpu_file(paths)}; GPU lease time {lease:.0f} s",
                lease_s=lease,
            )
        elif self.fake_gpu_ignored:
            self.log.warn("fake_gpu_ignored", describe("NS-W121").message, code="NS-W121")
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
        # The holder's own process (F-H47): pid -> creation time of the running process, None when none runs.
        self.process_probe: Callable[[int], float | None] = process_probe or process_started
        self._holder: _Holder | None = None
        # GPU lease: who held the lock at the last look, when it last showed a sign of life, and when the
        # monitor last ran (a long gap means the PC slept, which must not expire a live lease).
        self._lease_owner: str | None = None
        self._lease_beat = clock()
        self._last_tick: float | None = None
        self._update_task: asyncio.Task[None] | None = None
        self._subs: set[asyncio.Queue[dict[str, Any]]] = set()
        # vram-busy: the memory check before every load (`_vram_room`). How long a nightly load may wait for
        # memory (a proof shortens it on a test install only), what each acquire in progress may wait
        # (owner -> (may wait, the acquire's own deadline on `clock`)), the wait in progress (`/status`
        # `gpu_wait`) and what ends it early (Stop All, Release, shutdown).
        self.vram_wait_s, why = vram.test_wait_s(os.environ, lambda: devmode.test_install(paths))
        if why is not None:
            self.log.warn("vram_wait_ignored", why, variable=vram.WAIT_ENV)
        self._vram_plan: dict[str, tuple[bool, float]] = {}
        self._vram_wait: dict[str, Any] | None = None
        self._vram_since = 0.0
        self._vram_wake: asyncio.Event | None = None
        self._vram_unchecked_told = False
        # the memory of a GPU tenant of ours just stopped (`vram.Release`): the next check waits for it to
        # come back before it calls anything "another program's"
        self._release: vram.Release | None = None
        self._release_at = 0.0

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

    def write_state(self) -> bool:
        """Record supervisor and child pids; True when the file was written.

        The file is advisory (`ns stop` and `ns status` fall back to the API without it), so a refused write
        never raises: a failure here, right after a child was spawned, used to leave that child running and
        unsupervised (`_start_locked` terminates it only inside its try), or to turn a healthy start into a
        failure, or to hide the real start error behind `NS-E119`. It is a WARN in the log (`NS-E119`, the
        file keeps its last whole content) and the next state change writes it again.
        """
        if self.shutting_down:  # shutdown removes the file; a late start must not bring it back
            return False
        children = {n: r.proc.pid for n, r in self.services.items() if r.proc is not None}
        data = {
            "pid": os.getpid(),
            "port": self.runtime.supervisor_port,
            "started_at": self.started_wall,
            "children": children,
        }
        # `ns stop` and `ns status` read it. Called on the event loop, so a program that holds the file (they
        # open it only briefly) costs STATE_WAIT_S at most, then the write is given up with NS-E119 (logged).
        try:
            write_text_atomic(self.state_file, json.dumps(data, indent=2) + "\n", wait_s=STATE_WAIT_S)
        except OSError as e:  # FileInUseError (NS-E119) is one; a full disk or a locked folder is another
            self.log.warn("state_file_not_written", str(e), path=str(self.state_file))
            return False
        return True

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
        return self.runtime.whisper.device == "cuda" or self.fake_gpu

    def _gpu_tenants(self) -> dict[int, str]:
        """pid -> service of every running process that holds the card (what the simulated card lists)."""
        running = [r for r in list(self.services.values()) if r.proc is not None and r.definition.gpu]
        return {r.proc.pid: r.name for r in running if r.proc is not None}

    def tell_fake_gpu_ignored(self) -> None:
        """At engine start: `fake_gpu` set on an install that is not made for testing (NS-W121) is said once
        as a morning note (the Queue page shows it) while an earlier one still waits; the WARN is in the log
        and `ns doctor` shows the row. Never raises."""
        if not self.fake_gpu_ignored:
            return
        info = describe("NS-W121")
        line = f"NS-W121: {info.message} {info.hint}"
        try:
            notify.leave_note(self.paths, "Nightshift", line, once=lambda notes: any(
                n["message"].startswith("NS-W121:") for n in notes
            ))
        except OSError as e:
            self.log.warn("morning_note_failed", str(e), message=line)

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
        await self._note_release(rt)
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

    async def _start_locked(self, rt: ServiceRuntime, d: ServiceDef, owner: str | None = None) -> None:
        await self._guard(owner, rt.name, "start")  # nothing is spawned for an acquire that lost the GPU
        # vram-busy: no model process starts while other programs hold the memory it was measured to need
        # (refused at once, or a nightly waits); `before` is the settled reading the measurement starts from
        before = await self._vram_room(rt, d, owner) if d.gpu else None
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
                    try:
                        await self._guard(owner, rt.name, "load")  # every health poll
                    except NsError:
                        rt.abort = True  # an abandoned load, not a crash of the service
                        raise
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
            failed = isinstance(e, NsError) and e.code in ("NS-E202", "NS-E203")
            note = ""
            if isinstance(e, NsError) and failed and not rt.abort and not self.shutting_down:
                # model-file-damaged M6: the file decides first (a hash that does not match is proof); a
                # damaged file is never told as memory, and keeps no lower bound of memory (no vram record)
                found = await asyncio.to_thread(self.model_files.after_failure, rt.name, d.cmd)
                if found.damaged is not None:
                    self.log.error("model_file_damaged", found.damaged.detail, code=found.damaged.code,
                                   service=rt.name, original=e.code)  # fmt: skip
                    raise found.damaged from e
                note = found.note
            if isinstance(e, NsError) and failed and not rt.abort and before is not None:
                said = await self._vram_after_failure(rt, d, e, before)  # V7: the failure says why, truly
                if said is not None:
                    raise said from e
            if isinstance(e, NsError) and note:
                raise NsError(e.code, f"{e.detail}; model files: {note}") from e
            raise
        rt.state = "ready"
        rt.starting_since = None
        rt.vram_mb = await asyncio.to_thread(procs.vram_mb, proc.pid, self.smi)
        if before is not None:
            await self._vram_measure(d, before.reading.used_mb)
        self.write_state()
        mark = simulated_mark(self.smi)  # a simulated card's number says so wherever it is written (H-10)
        card = {} if mark is None else {"card": mark}
        self.log.info("service_ready", service=rt.name, pid=proc.pid, vram_mb=rt.vram_mb, **card)
        self._emit(rt, "ready", pid=proc.pid, vram_mb=rt.vram_mb, **card)

    async def _ensure(self, rt: ServiceRuntime, d: ServiceDef, owner: str | None = None) -> None:
        """Make `rt` ready with definition `d`: reuse, restart on variant change, or start."""
        async with rt.op_lock:
            await self._guard(owner, rt.name, "ensure")  # the wait for op_lock may have been long
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
                # (the files of a failed start were checked then: the verdict is known, nothing is hashed)
                found = await asyncio.to_thread(self.model_files.after_failure, rt.name, d.cmd)
                if found.damaged is not None:
                    raise found.damaged
                raise NsError(
                    "NS-E202",
                    f"{rt.name} crashed {len(rt.crashes)}x in {self.cfg.crash_window_s:.0f}s: {rt.error}",
                )
            await self._start_locked(rt, d, owner)

    async def _swap_to(self, service: str, d: ServiceDef, owner: str | None = None) -> None:
        """Stop every other GPU tenant, then bring `service` up (whisper has no process).

        `owner` (an acquire): `_guard` runs before each stop and before `service` is brought up (and again in
        `_ensure` and at every health poll of the load). None: a warmup, which has no owner to check.
        """
        for other, ort in self.services.items():
            if other != service and ort.proc is not None and ort.definition.gpu:
                await self._guard(owner, service, f"stop {other}")
                await self._stop(ort)
        await self._guard(owner, service, "bring up")
        if service != "whisper":
            await self._ensure(self.services[service], d, owner)

    # ------------------------------------------------------------------ the card's memory (vram-busy)
    def _vram_on(self) -> bool:
        """The check has a card to read: the gpu profile, or the simulated card (cpu-dev has none)."""
        return self.runtime.profile == "gpu" or self.fake_gpu

    async def _vram_check(
        self, d: ServiceDef, *, release: vram.Release | None = None, programs: bool = False
    ) -> vram.Verdict | None:
        """`vram.check` off the event loop. Ours, never named nor held against a load: every process this
        engine runs and the GPU holder's own process; a studio holder's own CUDA context (whisper ran in it,
        WDDM never reports it) is allowed for (`HOLDER_CONTEXT_MB`)."""
        ours = {r.proc.pid for r in list(self.services.values()) if r.proc is not None}
        holder = self._current_holder()
        if holder is not None:
            ours.add(holder.pid)
        studio = holder is not None and holder.owner.startswith("studio:")
        model_dir = self.runtime.tts.model_dir if d.name == "tts" else None
        return await asyncio.to_thread(
            vram.check, self.smi, self.paths, d, ours, model_dir=model_dir, release=release,
            programs=programs,
            holder_mb=vram.HOLDER_CONTEXT_MB if studio else 0, poll_s=self.cfg.vram_settle_poll_s,
        )

    async def _note_release(self, rt: ServiceRuntime) -> None:
        """Before a GPU tenant of ours stops: what the card holds now and what its model was measured to take,
        so the next check waits (at most `vram.SETTLE_S`) for that memory to come back (`vram.settled`)."""
        if not (rt.definition.gpu and self._vram_on()) or self.shutting_down:
            return
        mem = await asyncio.to_thread(vram.read_memory, self.smi)
        if mem is None:
            return
        n = await asyncio.to_thread(vram.need, self.paths, rt.definition, None, simulated_mark(self.smi))
        gone = n.mb if n.measured and not n.lower else None
        if self._release is not None and self.clock() - self._release_at > vram.RELEASE_STALE_S:
            self._release = None
        if self._release is not None:  # two of ours stopped before one load: from the first reading, both MiB
            before, earlier = self._release
            gone = None if earlier is None or gone is None else earlier + gone
            self._release = (before, gone)
        else:
            self._release = (mem[0], gone)
        self._release_at = self.clock()

    def _vram_not_checked(self, why: str) -> None:
        """V0: no reading; the load goes ahead as before, said once per engine start."""
        if not self._vram_unchecked_told:
            self._vram_unchecked_told = True
            self.log.info("vram_not_checked", f"graphics memory not checked: {why}")

    def _wake_vram(self) -> None:
        if self._vram_wake is not None:
            self._vram_wake.set()

    async def _vram_room(self, rt: ServiceRuntime, d: ServiceDef, owner: str | None) -> vram.Verdict | None:
        """V0 to V6 of card vram-busy, right before `d`'s process would start: the settled verdict the load
        goes ahead on (its reading is where the measurement starts), None without a reading. Refused (NS-E208)
        only by the rule of `vram.judge`: a MEASURED need, a SETTLED reading, memory held by OTHERS that an
        idle card would have free. Then at once, unless the acquire said it may wait (`vram_wait`, the
        nightly): it waits up to `vram_wait_s`, never past the acquire's own `timeout_s` (the night's time
        decision gave it), looking again every `vram_poll_s` with `_guard` before each look. Stop All,
        Release, a gone holder and shutdown end the wait at once (`_wake_vram`)."""
        release, self._release = self._release, None
        if release is not None and self.clock() - self._release_at > vram.RELEASE_STALE_S:
            release = None  # (an idle unload long ago: its memory had its time; what is held now is not ours)
        if not self._vram_on():
            self._vram_not_checked("the cpu-dev profile has no graphics card to read (fake_gpu is off)")
            return None
        v = await self._vram_check(d, release=release)
        if v is None:
            self._vram_not_checked(
                f"nvidia-smi gave no answer within {vram.READ_TIMEOUT_S:.0f} s (not installed, failing or "
                "too slow)"
            )
            return None
        if v.ok:
            self._vram_idle(v, release)
            why = v.why_tried()
            if why is not None:  # it does not fit the reading, and still goes ahead: said in the log, why
                self.log.info("vram_tried", why, owner=owner, service=rt.name, **v.numbers())
            return v
        may_wait, until = self._vram_plan.get(owner, (False, 0.0)) if owner is not None else (False, 0.0)
        bound = min(self.vram_wait_s, until - self.clock()) if may_wait else 0.0
        if bound <= 0:  # V4: an interactive owner (or a warmup) is refused at once
            self.log.warn("vram_busy", v.detail(), code="NS-E208", owner=owner, service=rt.name,
                          **v.numbers())  # fmt: skip
            raise NsError("NS-E208", v.detail())
        rt.abort = False
        self._vram_since, self._vram_wake = self.clock(), asyncio.Event()
        wake = self._vram_wake
        since = self.now().isoformat(timespec="seconds")
        said = v.waiting()
        info: dict[str, Any] = {"service": rt.name, "owner": owner, "since": since}
        self._vram_wait = {**info, "said": said, **v.numbers()}
        self.log.info("vram_wait", said, owner=owner, service=rt.name, bound_s=round(bound), **v.numbers())
        self.publish("service", {"event": "vram_wait", "service": rt.name, "owner": owner, "said": said,
                                 "bound_s": round(bound), **v.numbers()})  # fmt: skip
        # bounded three ways, whatever the clock or the card do: `bound` on the engine's clock, the same
        # `bound` (+ one poll) of real time on `time.monotonic` (no injected or stuck clock holds it), and
        # `turns` looks at the card. It logs once at its start and once at its end, never per turn.
        real_end = time.monotonic() + bound + self.cfg.vram_poll_s
        turns = math.ceil(bound / max(self.cfg.vram_poll_s, 1e-3)) + 2
        try:
            while True:
                waited = self.clock() - self._vram_since
                turns -= 1
                if waited >= bound or turns < 0 or time.monotonic() >= real_end:  # V6: the bound passed
                    detail = v.detail(waited)
                    self.log.warn("vram_busy", detail, code="NS-E208", owner=owner, service=rt.name,
                                  waited_s=round(waited), **v.numbers())  # fmt: skip
                    raise NsError("NS-E208", detail)
                wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), min(self.cfg.vram_poll_s, bound - waited))
                if self.shutting_down:
                    raise NsError("NS-E202", "supervisor is shutting down")
                if rt.abort:
                    raise NsError("NS-E202", f"the wait of {rt.name} for the card's memory was stopped")
                await self._guard(owner, rt.name, "memory wait")
                again = await self._vram_check(d)
                if again is None or again.ok:
                    waited = self.clock() - self._vram_since
                    self.log.info("vram_wait_ended", f"the memory came free after {waited:.0f} s",
                                  owner=owner, service=rt.name, waited_s=round(waited))  # fmt: skip
                    return again
                v = again
                self._vram_wait = {**info, "said": v.waiting(), **v.numbers()}
        finally:
            self._vram_wait, self._vram_wake = None, None

    def _vram_idle(self, v: vram.Verdict, release: vram.Release | None) -> None:
        """Keep the reading as this PC's idle card when nothing of ours was on it (no process of ours, no
        holder process, no memory of ours on its way out): the smallest such reading is the idle baseline."""
        if release is not None or not v.settled and not v.fits:
            return
        if any(r.proc is not None and r.definition.gpu for r in self.services.values()):
            return
        if self._current_holder() is not None:
            return
        try:
            vram.record_idle(self.paths, v.reading.used_mb, simulated_mark(self.smi), self.now())
        except OSError as e:  # the allowance or the older baseline stands; the next quiet reading tries again
            self.log.warn("vram_need_not_written", str(e))

    async def _vram_after_failure(
        self, rt: ServiceRuntime, d: ServiceDef, e: NsError, before: vram.Verdict
    ) -> NsError | None:
        """V7: a load that failed at startup says why, truly. Read again (settled, who is on the card). When
        a MEASURED need did not fit either reading, it is more than what was free: that lower bound is kept
        (`record_failure`), so the next check compares it. Others held memory an idle card would have had
        free: NS-E208 with the original in its detail. The need does not fit even an idle card: NS-E203 (a
        tuning matter: its hint says ns doctor, then ns tune). Else None: the original error stands."""
        after = await self._vram_check(d, programs=True)
        # a lower bound only for a need that was MEASURED: the first start of a model only estimated may have
        # failed for a damaged file (NS-E202's ground), never to be told later as "too big" (NS-E203)
        short = [v for v in (before, after) if v is not None and not v.fits and v.need.measured]
        if short:
            try:
                vram.record_failure(self.paths, d, min(v.free_mb for v in short), simulated_mark(self.smi),
                                    self.now())  # fmt: skip
            except OSError as err:
                self.log.warn("vram_need_not_written", str(err))
        blame = next((v for v in (after, before) if v is not None and not v.fits and not v.oversize
                      and v.others_mb > 0), None)  # fmt: skip
        if blame is not None:
            detail = f"{blame.detail()}; the start failed: {e.detail}"
            self.log.warn("vram_busy", detail, code="NS-E208", service=rt.name, original=e.code,
                          **blame.numbers())  # fmt: skip
            return NsError("NS-E208", detail)
        big = next((v for v in (before, after) if v is not None and v.need.measured and v.oversize), None)
        if big is not None:
            detail = f"{big.too_big()}; the start failed: {e.detail}"
            self.log.warn("vram_too_big", detail, code="NS-E203", service=rt.name, original=e.code)
            return NsError("NS-E203", detail)
        return None

    async def _vram_measure(self, d: ServiceDef, before: int) -> None:
        """What the load took: total used now minus total used just before the spawn (works under WDDM)."""
        mem = await asyncio.to_thread(vram.read_memory, self.smi)
        if mem is None:
            return
        try:
            vram.record(self.paths, d, mem[0] - before, simulated_mark(self.smi), self.now())
        except OSError as e:  # the next load measures again; the estimate or older numbers stand meanwhile
            self.log.warn("vram_need_not_written", str(e))

    async def _guard(self, owner: str | None, service: str, step: str) -> None:
        """The GPU handover invariant: an acquire acts on the GPU only while it is the lock's recorded owner,
        and, when it named its process, only while that process still runs (F-H47).

        Called between the steps of an acquire (granted, before each stop, before the bring-up, in `_ensure`,
        before the spawn, at every health poll, after ready). Another owner holds the lock: `NS-E202`, and
        nothing is released (the lock is not ours). Our process is gone: the lock is freed as the monitor
        would (`owner_released`, `holder_gone`), then `NS-E202`. None: no owner to check (a warmup).
        """
        if owner is None:
            return
        if self.lock.state.owner != owner:
            raise NsError("NS-E202", f"{owner} no longer holds the GPU ({step}, {service})")
        h = self._current_holder()
        if h is not None and not self._holder_alive(h):
            self._holder = None
            await self.release_owner(owner, reason="holder_gone")
            raise NsError("NS-E202", f"the process of {owner} is gone ({step}, {service})")

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

    def _record_holder(self, owner: str, pid: int | None, fresh: bool) -> None:
        """Called right after the GPU lock was granted to `owner`, before a model loads (F-H47).

        With a pid: the holder is that process, as it runs now (pid and creation time). Without one: a fresh
        grant has no holder process (never probed); a re-acquire by the holder keeps the record it has.
        """
        if pid is None:
            if fresh:
                self._holder = None
            return
        try:
            created, known = self.process_probe(pid), True
        except Exception as e:  # cannot read the process now (access denied): adopt the first readable answer
            created, known = None, False
            self.log.warn("holder_probe_failed", str(e), owner=owner, pid=pid)
        self._holder = _Holder(owner, pid, created, known=known, warned=not known)

    def _current_holder(self) -> _Holder | None:
        """The holder record, if it still belongs to the lock's owner (a release leaves it stale)."""
        h = self._holder
        return h if h is not None and h.owner == self.lock.state.owner else None

    async def acquire(
        self,
        service: str,
        owner: str,
        model_alias: str | None = None,
        wait: bool = True,
        timeout_s: float = 7200,
        pid: int | None = None,
        vram_wait: bool = False,
    ) -> dict[str, Any]:
        """Hand `owner` the service. GPU services queue FIFO behind the GPU lock (`NS-E201` on timeout),
        stop any other GPU tenant, and start/health-poll the process (`NS-E203`, `NS-E202`).

        `pid`: the owner's own process. When the GPU lock is granted, its pid and creation time are recorded,
        and the monitor frees the lock once that process is gone (`_release_gone_holder`). No pid: no probe.
        `vram_wait`: the caller is a run nobody watches (the nightly): when other programs hold the card's
        memory, its load waits for it (`_vram_room`) inside this same `timeout_s`; anyone else gets NS-E208
        at once.
        """
        if self.shutting_down:
            raise NsError("NS-E202", "supervisor is shutting down")
        t0 = self.clock()
        self._touch()
        self.run_finished_at = None  # somebody is working again: back to the plain idle rule
        fresh = self.lock.state.owner != owner  # False: the holder re-acquires (a swap), it keeps its record
        if service == "whisper":
            if not self._whisper_gpu():
                return self._endpoint("whisper", None, None)
            await self.lock.acquire(owner, "whisper", timeout_s if wait else 0)
            self._leave_if_shutting_down(owner)
            self._record_holder(owner, pid, fresh)
            try:
                await self._guard(owner, "whisper", "granted")
                await self._swap_to("whisper", self.services["llama"].definition, owner)
                await self._guard(owner, "whisper", "ready")
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
        self._record_holder(owner, pid, fresh)
        rt.idle_since = None
        self._vram_plan[owner] = (vram_wait, t0 + timeout_s)  # the wait for memory ends inside `timeout_s`
        try:
            await self._guard(owner, service, "granted")  # a console closed while it waited in the queue
            await self._swap_to(service, d, owner)
            await self._guard(owner, service, "ready")  # the ready step awaits too (VRAM sample)
        except BaseException:
            self.lock.release(owner)  # changes nothing when the lock is no longer `owner`'s
            raise
        finally:
            self._vram_plan.pop(owner, None)
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

    async def release_owner(self, owner: str, reason: str | None = None) -> bool:
        """Free everything `owner` holds or waits for, whatever the service (its process is gone or stuck).

        The lock if it holds it, a place in the queue, and the non-GPU services it leased (`embed`). The idle
        timer of a service nobody holds starts now. False when `owner` held and awaited nothing. `reason`
        (such as `holder_gone`) goes into the `owner_released` WARN and event; none: neither has the key.
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
            self._wake_vram()  # a load waiting for memory for `owner` stops at its guard now
            why: dict[str, Any] = {} if reason is None else {"reason": reason}
            msg = f"released everything held by {owner}" + ("" if reason is None else f" ({reason})")
            self.log.warn("owner_released", msg, owner=owner, **why)
            self.publish("service", {"event": "owner_released", "owner": owner, **why})
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
        if any(r.state == "starting" for r in self.services.values()) or self._vram_wait is not None:
            # a model load is not silence, nor the bounded wait for the card's memory before one
            self._lease_beat = now
            return
        silent = now - self._lease_beat
        if silent < self.cfg.lease_expiry_s:
            return
        self.log.warn("lease_expired", f"{owner} sent no heartbeat for {silent:.0f}s", owner=owner)
        await self.release_owner(owner)
        self.publish("service", {"event": "lease_expired", "owner": owner, "silent_s": round(silent, 1)})

    async def _release_gone_holder(self) -> None:
        """Free the GPU when the process the holder named at its acquire is gone (F-H47).

        Gone: no running process has the pid, or one runs with another creation time (pid reuse). This is how
        a Nightshift Code console closed with X (its `finally` never runs) gives the card back, while a live
        but idle console keeps it for as long as it runs. An acquire still running for it (stopping a tenant,
        loading its model) stops at its next `_guard`, because the lock is no longer its own.
        """
        h = self._holder
        if h is None:
            return
        if h.owner != self.lock.state.owner:  # released or handed on since: the record is stale
            self._holder = None
            return
        if self._holder_alive(h):
            return
        self._holder = None
        await self.release_owner(h.owner, reason="holder_gone")

    def _holder_alive(self, h: _Holder) -> bool:
        """False when the holder's process is gone: no process runs with its pid, or one with another creation
        time (pid reuse). A probe that raises (access denied) answers True: the holder is kept until the
        dashboard's Release button frees it, with no time bound, and one `holder_probe_failed` WARN each.
        """
        try:
            created = self.process_probe(h.pid)
        except Exception as e:
            if not h.warned:
                h.warned = True
                self.log.warn("holder_probe_failed", str(e), owner=h.owner, pid=h.pid)
            return True
        if created is not None and not h.known:  # unreadable at grant time: this is the holder as it runs
            h.created, h.known = created, True
            return True
        return created is not None and created == h.created

    async def stop_all(self, *, include_persistent: bool = False) -> None:
        """Terminate every model service and clear crash state, owners and the GPU lock holder."""
        targets = [
            r for r in self.services.values() if include_persistent or not r.definition.persistent
        ]
        for r in targets:
            if r.state == "starting":
                r.abort = True
        if self._vram_wait is not None:  # a load waiting for memory: it ends now, nothing is started
            self.services[self._vram_wait["service"]].abort = True
            self._wake_vram()
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
        self._wake_vram()  # a load waiting for memory gives the card back now
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
        except NsError as e:
            if e.code != "NS-E208":  # no room: the check logged it (`vram_busy`); a warmup never waits
                raise
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
                # the context llama-server is (or will be) started with: this engine's copy of runtime.json,
                # read when the engine started. Nightshift Code compares it with what OpenCode needs (D-34).
                "ctx": self.runtime.llama.flags.ctx if n == "llama" else None,
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
            "ctx": None,
        }
        holder = self._current_holder()
        extra: dict[str, Any] = {}
        if self._vram_wait is not None:  # only while a load waits for the card's memory (vram-busy)
            extra["gpu_wait"] = {**self._vram_wait, "waited_s": round(now - self._vram_since)}
        return {
            **extra,
            "gpu_owner": st.owner,
            "gpu_service": st.service,
            "holder_pid": holder.pid if holder is not None else None,  # the holder's own process (F-H47)
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
        """One monitor pass: expire leases, free a gone holder, reap crashes, idle-unload, auto-exit."""
        now = self.clock()
        if self._last_tick is not None and now - self._last_tick > self.cfg.suspend_gap_s:
            self._lease_beat = now  # the PC slept: the studio could not beat either
        self._last_tick = now
        await self._expire_lease(now)
        await self._release_gone_holder()
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
