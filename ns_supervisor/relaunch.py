"""C2: bring Nightshift back after a detached update.

The update worker stops the engine (and with it the dashboard service) to swap the app folder. Nothing used to
start them again, so the dashboard window or browser tab that began the update polled a dead port. The worker
now remembers what was running before the update (`observe`) and, when it is done, starts the same things
again (`restart`) the way the shortcuts do: the engine, as `Start-NsSupervisor` does, and the dashboard
launcher when a window host was open. It returns only once the engine answers `/status`, so the worker can
write the `done` state after that and the reconnecting page finds a live dashboard.

D-51: "as it was found" is what the owner knows, not only what answers. An engine that hung before the
update (it holds this home's engine lock, `run/supervisor.lock`, and answers nothing) was running as far as
the owner knows: its dashboard may still answer. The update ends it by its recorded process (`stopping`), so
it is counted as running and started again. The table (`observe`, `restart`):

=======================  ========================  ==================================================
engine before            dashboard before          started again (when the run may start anything)
=======================  ========================  ==================================================
answers `/status`        up (its service, a live   the engine, which starts its dashboard (as the
                         dashboard process of THIS  Dashboard shortcut: the launcher when a window host
                         install, or a window      was open), then `warmup("dashboard")`
                         host)
answers `/status`        not up                    the engine without its dashboard (`--headless`)
hung (lock held)         up / not up               the same two rows (the dashboard: a live process of
                                                   this install, `stopping.ours`, or a window host)
not running              up (a dashboard left by   the engine and its dashboard
                         an engine that died)
not running              not up                    nothing
=======================  ========================  ==================================================

`started` says whether `restart` started anything; `running` asks `/status` now. The worker's result ends
with the sentence they decide (`update_job.now_running`).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor import control, presence, pwsh
from ns_supervisor.childenv import LAUNCHER_ONLY_ENV, scrubbed
from ns_supervisor.client import SupervisorClient, ensure_running, spawn_supervisor
from ns_supervisor.procs import UNDER_ENGINE_ENV

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
DASHBOARD_LAUNCHER = ("launchers", "nightshift-dashboard.ps1")
ENGINE_WAIT_S = 90.0
#: Environment the relaunched processes must not inherit from the worker: the worker may run below the engine
#: (marked as such) and the setup window's "no host window" switch.
SCRUBBED_ENV = (UNDER_ENGINE_ENV, *LAUNCHER_ONLY_ENV)

PopenFn = Callable[..., object]


def clean_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment without the variables in `SCRUBBED_ENV`."""
    return scrubbed(os.environ if env is None else env, also=SCRUBBED_ENV)


def start_dashboard_launcher(paths: Paths, app: Path, *, popen: PopenFn | None = None) -> None:
    """Run `launchers\nightshift-dashboard.ps1` of `app` detached, with the install home as its directory.

    The launcher starts the engine when it is down, asks it for the dashboard service and raises (or opens)
    the window host, exactly like the Dashboard shortcut.
    """
    script = app.joinpath(*DASHBOARD_LAUNCHER)
    exe = pwsh.find_powershell()
    if exe is None or not script.is_file():
        raise NsError("NS-E202", f"cannot start the dashboard launcher ({script})")
    cwd = paths.home if paths.home.is_dir() else Path.home()
    argv = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script)]
    try:
        (popen or subprocess.Popen)(
            argv, cwd=str(cwd), env=clean_env(), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, close_fds=True,
            creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
        )
    except OSError as e:
        raise NsError("NS-E202", f"cannot start the dashboard launcher: {e}") from e


def _service_up(status: Mapping[str, Any], name: str) -> bool:
    """`/status` says the service runs (starting or ready)."""
    services = status.get("services")
    info = services.get(name) if isinstance(services, Mapping) else None
    return isinstance(info, Mapping) and info.get("state") in ("starting", "ready")


def own_dashboard_alive(paths: Paths) -> bool:
    """A dashboard process of THIS install runs (`stopping.ours`: recorded in `run/supervisor.json` and tied
    to this home by `venvrepair.is_this_install`); the answer of a hung engine's `/status` is not needed."""
    from ns_supervisor import stopping

    return any(o.what == "the dashboard" for o in stopping.ours(paths))


def engine_lock_held(paths: Paths) -> bool:
    """An engine of this home holds its lock (it may answer nothing: D-46, D-51)."""
    from ns_supervisor import stopping

    return stopping.engine_lives(paths)


@dataclass
class Relaunch:
    """What was running before the update, and how to start it again. Every side effect is a field (tests).

    `engine_held` and `dashboard_alive` default to the real checks of `paths` (`engine_lock_held`,
    `own_dashboard_alive`); `start_quiet` starts the engine without its dashboard (None: `start_engine`)."""

    paths: Paths
    client: SupervisorClient
    window_open: Callable[[], bool]
    start_engine: Callable[[], None]
    start_window: Callable[[], None]
    wait_s: float = ENGINE_WAIT_S
    engine_held: Callable[[], bool] | None = None
    dashboard_alive: Callable[[], bool] | None = None
    start_quiet: Callable[[], None] | None = None
    engine_was_up: bool = field(default=False, init=False)
    window_was_open: bool = field(default=False, init=False)
    dashboard_was_up: bool = field(default=False, init=False)
    started: bool = field(default=False, init=False)

    @property
    def wanted(self) -> bool:
        """True when something of Nightshift was running (as far as the owner knows) that the update stops."""
        return self.engine_was_up or self.window_was_open or self.dashboard_was_up

    def _ask(self, check: Callable[[], bool] | None, real: Callable[[Paths], bool]) -> bool:
        try:
            return bool(check()) if check is not None else real(self.paths)
        except Exception:  # noqa: BLE001 - a check that fails says "not seen"; the update goes on
            return False

    def observe(self) -> None:
        """Remember, before the update stops the engine, what of Nightshift runs: the engine (it answers
        `/status`, or an engine of this home holds its lock), its dashboard (the engine says its service runs;
        or, when it does not answer, a live dashboard process of this install) and a dashboard window."""
        try:
            status: dict[str, Any] | None = self.client.status()
        except NsError:
            status = None
        self.window_was_open = self._ask(self.window_open, lambda _p: False)
        self.engine_was_up = status is not None or self._ask(self.engine_held, engine_lock_held)
        if status is not None:
            dashboard = _service_up(status, "dashboard")
        else:
            dashboard = self._ask(self.dashboard_alive, own_dashboard_alive)
        self.dashboard_was_up = self.window_was_open or dashboard

    def running(self) -> bool:
        """The engine answers `/status` now."""
        return self.client.is_up()

    def restart(self) -> None:
        """Start again what `observe` saw (the table in the module docstring) and wait for `/status`.

        Does nothing when nothing was running before. An engine that is up already (an update that changed
        nothing) is left alone. Raises `NS-E202` when the engine does not answer in `wait_s`. `started` is set
        before the spawn: an engine that was started and does not answer was still started (and the caller
        says that it could not restart by itself).
        """
        if not self.wanted or self.client.is_up():
            return
        if self.window_was_open:
            spawn = self.start_window
        elif self.dashboard_was_up or self.start_quiet is None:
            spawn = self.start_engine
        else:
            spawn = self.start_quiet
        self.started = True
        ensure_running(self.client, self.paths, headless=not self.dashboard_was_up, wait_s=self.wait_s,
                       spawn=spawn)
        if not self.dashboard_was_up:
            return
        try:
            self.client.warmup("dashboard")     # (the window's launcher asks too; twice is fine)
        except NsError:
            pass                                # the page shows the dashboard's own error when it matters


def default_relaunch(paths: Paths) -> Relaunch:
    """The real thing: the supervisor client of `runtime.json`, `spawn_supervisor`, the dashboard launcher."""
    client = SupervisorClient.from_runtime(control.runtime_or_default(paths))
    return Relaunch(
        paths=paths,
        client=client,
        window_open=lambda: presence.host_window_open(paths),
        start_engine=lambda: spawn_supervisor(paths, headless=False),
        start_window=lambda: start_dashboard_launcher(paths, control.app_path(paths)),
        start_quiet=lambda: spawn_supervisor(paths, headless=True),
    )
