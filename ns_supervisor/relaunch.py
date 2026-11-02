"""C2: bring Nightshift back after a detached update.

The update worker stops the engine (and with it the dashboard service) to swap the app folder. Nothing used to
start them again, so the dashboard window or browser tab that began the update polled a dead port. The worker
now remembers what was running before the update (`observe`) and, when it is done, starts the same things
again (`restart`) the way the shortcuts do: the engine, as `Start-NsSupervisor` does, and the dashboard
launcher when a window host was open. It returns only once the engine answers `/status`, so the worker can
write the `done` state after that and the reconnecting page finds a live dashboard.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor import control, presence, pwsh
from ns_supervisor.client import SupervisorClient, ensure_running, spawn_supervisor
from ns_supervisor.procs import UNDER_ENGINE_ENV

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
DASHBOARD_LAUNCHER = ("launchers", "nightshift-dashboard.ps1")
ENGINE_WAIT_S = 90.0
#: Environment the relaunched processes must not inherit from the worker: the worker may run below the engine
#: (marked as such) and the setup window's "no host window" switch.
SCRUBBED_ENV = (UNDER_ENGINE_ENV, "NS_DASHBOARD_NO_HOST")

PopenFn = Callable[..., object]


def clean_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment without the variables in `SCRUBBED_ENV`."""
    return {k: v for k, v in (os.environ if env is None else env).items() if k not in SCRUBBED_ENV}


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


@dataclass
class Relaunch:
    """What was running before the update, and how to start it again. Every side effect is a field (tests)."""

    paths: Paths
    client: SupervisorClient
    window_open: Callable[[], bool]
    start_engine: Callable[[], None]
    start_window: Callable[[], None]
    wait_s: float = ENGINE_WAIT_S
    engine_was_up: bool = field(default=False, init=False)
    window_was_open: bool = field(default=False, init=False)

    @property
    def wanted(self) -> bool:
        """True when something was running that the update stopped."""
        return self.engine_was_up or self.window_was_open

    def observe(self) -> None:
        """Remember, before the update stops the engine, whether it and a dashboard window are running."""
        self.engine_was_up = self.client.is_up()
        self.window_was_open = self.window_open()

    def restart(self) -> None:
        """Start the engine (and the dashboard window when one was open) and wait for `/status`.

        Does nothing when nothing was running before. An engine that is up already (an update that changed
        nothing) is left alone. Raises `NS-E202` when the engine does not answer in `wait_s`.
        """
        if not self.wanted or self.client.is_up():
            return
        spawn = self.start_window if self.window_was_open else self.start_engine
        ensure_running(self.client, self.paths, headless=False, wait_s=self.wait_s, spawn=spawn)
        try:
            self.client.warmup("dashboard")     # a headless engine does not start it by itself
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
    )
