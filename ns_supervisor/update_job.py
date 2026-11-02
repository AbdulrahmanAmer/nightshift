"""C2: `ns update` runs as a worker process that outlives the engine, the dashboard and the caller.

Why: the dashboard's "Update now" runs `ns update --github` as a child of the dashboard service, which is a
child of the engine, inside the engine's Job Object (`KILL_ON_JOB_CLOSE`). The update stops the engine
(`POST /shutdown`), and the engine's shutdown then kills the dashboard's whole process tree, including the
update itself, half way through the swap. So `ns update` re-launches itself as a *worker*: detached
(`DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP`), broken away from the job (`CREATE_BREAKAWAY_FROM_JOB`, which
the engine's job allows), with the install home as its working directory (a process whose cwd is the app
folder blocks the folder swap). When the engine's job does not allow breakaway (an engine started by an
older version), the worker is created through WMI (`Win32_Process.Create`), which starts it outside the job.

Progress is visible in two places: the log file (`logs\\update-<time>.log`) and `run\\update_state.json`
(`running`, then `done` or `failed`; the dashboard serves it at /api/update/state).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, TextIO

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor import pwsh
from ns_supervisor.procs import UNDER_ENGINE_ENV

CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
DETACH_FLAGS = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS

#: After the caller exits, wait this long so its answer reaches the browser before the engine is stopped.
PARENT_GRACE_S = 2.0
PARENT_WAIT_MAX_S = 120.0

DONE_STATES = ("done", "failed")

PopenFn = Callable[..., Any]

RESTART_HINT = "Nightshift could not restart by itself; click the Nightshift Dashboard shortcut."


class Relauncher(Protocol):
    """Starts Nightshift again after the update (`ns_supervisor.relaunch.Relaunch`)."""

    def observe(self) -> None:
        """Remember what is running now (called before the update stops the engine)."""

    def restart(self) -> None:
        """Start it again and return once the engine answers `/status`; raises `NsError` otherwise."""


def under_engine(env: Mapping[str, str] | None = None) -> bool:
    """True when this process runs below the engine (so the engine's shutdown would kill it)."""
    return (os.environ if env is None else env).get(UNDER_ENGINE_ENV) == "1"


def status_file(paths: Paths) -> Path:
    """`run\\update_state.json`."""
    return paths.home / "run" / "update_state.json"


def write_status(paths: Paths, state: str, message: str = "", **extra: Any) -> None:
    """Replace the state file atomically: `{state, old, new, message, updated_at}` plus log/pid extras."""
    target = status_file(paths)
    data = {
        "state": state, "old": "", "new": "", "message": message,
        "updated_at": datetime.now().isoformat(timespec="seconds"), **extra,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.replace(tmp, target)


def read_status(paths: Paths) -> dict[str, Any]:
    """The status file as a dict (empty when absent or unreadable)."""
    try:
        data = json.loads(status_file(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def new_log_path(paths: Paths, now: datetime | None = None) -> Path:
    """`logs\\update-<timestamp>.log`."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return paths.logs / f"update-{stamp}.log"


def worker_argv(
    forwarded: list[str], *, log: Path, home: Path, wait_pid: int | None, python: str | None = None
) -> list[str]:
    """Command line of the worker: `ns update <forwarded> --worker --log ... --home ... [--wait-pid ...]`."""
    argv = [python or sys.executable, "-m", "ns_supervisor.cli", "update", *forwarded]
    argv += ["--worker", "--log", str(log), "--home", str(home)]
    if wait_pid is not None:
        argv += ["--wait-pid", str(wait_pid)]
    return argv


def _cim_create(argv: list[str], cwd: Path) -> int | None:
    """Start `argv` through WMI (`Win32_Process.Create`): the new process is outside any Job Object."""
    script = (
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        "-Arguments @{ CommandLine = $env:NS_CIM_CMD; CurrentDirectory = $env:NS_CIM_CWD }\n"
        "if ($r.ReturnValue -ne 0) { exit 1 }\n"
        "Write-Output $r.ProcessId"
    )
    try:
        r = pwsh.run_powershell(
            script, env_extra={"NS_CIM_CMD": subprocess.list2cmdline(argv), "NS_CIM_CWD": str(cwd)}
        )
    except (pwsh.PowerShellMissing, OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    text = (r.stdout or "").strip().splitlines()
    return int(text[-1]) if text and text[-1].strip().isdigit() else None


def spawn_detached(
    argv: list[str],
    *,
    cwd: Path,
    popen: PopenFn | None = None,
    cim: Callable[[list[str], Path], int | None] | None = None,
) -> int:
    """Start `argv` so that it survives its caller, the engine and the engine's Job Object; returns its pid.

    Order: detached + broken away from the job; WMI (outside every job) when the job refuses a breakaway;
    plain detached as the last resort. Raises `NS-E702` when nothing could be started.
    """
    popen = popen or subprocess.Popen
    cim = cim or _cim_create
    common: dict[str, Any] = {
        "cwd": str(cwd),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    try:
        return int(popen(argv, creationflags=DETACH_FLAGS | CREATE_BREAKAWAY_FROM_JOB, **common).pid)
    except OSError:
        pass  # ERROR_ACCESS_DENIED: the job does not allow breakaway
    pid = cim(argv, cwd)
    if pid is not None:
        return pid
    try:
        return int(popen(argv, creationflags=DETACH_FLAGS, **common).pid)
    except OSError as e:
        raise NsError("NS-E702", f"could not start the update process: {e}") from e


def pid_alive(pid: int) -> bool:
    """True while a process with this pid exists (via tasklist; never `os.kill`, which kills on Windows)."""
    from ns_supervisor import control

    return control.image_name(pid) is not None


def wait_for_parent(
    pid: int,
    *,
    alive: Callable[[int], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    grace_s: float = PARENT_GRACE_S,
    max_s: float = PARENT_WAIT_MAX_S,
) -> None:
    """Wait until the process that started this worker has exited, then a short grace period."""
    alive = alive or pid_alive
    waited = 0.0
    while alive(pid) and waited < max_s:
        sleep(0.5)
        waited += 0.5
    sleep(grace_s)


def launch(
    paths: Paths,
    forwarded: list[str],
    *,
    wait_pid: int | None,
    spawn: Callable[..., int] | None = None,
    now: datetime | None = None,
) -> tuple[int, Path]:
    """Start the worker for `forwarded` arguments; returns (pid, log path). Writes the `running` state."""
    log = new_log_path(paths, now)
    log.parent.mkdir(parents=True, exist_ok=True)
    argv = worker_argv(forwarded, log=log, home=paths.home, wait_pid=wait_pid)
    write_status(paths, "running", "Starting the update.", log=str(log))
    cwd = paths.home if paths.home.is_dir() else Path(os.path.expanduser("~"))
    pid = (spawn or spawn_detached)(argv, cwd=cwd)
    return pid, log


def follow(
    paths: Paths,
    pid: int,
    log: Path,
    echo: Callable[[str], None],
    *,
    alive: Callable[[int], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_s: float = 4 * 3600,
) -> int:
    """Echo the worker's log until it finishes; returns the exit code (0 done or up to date, 1 failed)."""
    alive = alive or pid_alive
    offset = 0
    waited = 0.0
    while True:
        offset = _echo_new(log, offset, echo)
        state = str(read_status(paths).get("state", ""))
        if state in DONE_STATES:
            _echo_new(log, offset, echo)
            return 1 if state == "failed" else 0
        if not alive(pid):
            _echo_new(log, offset, echo)
            echo("The update process ended without reporting a result. See " + str(log))
            return 1
        if waited >= max_s:
            echo("Still running in the background. Progress: " + str(log))
            return 0
        sleep(0.5)
        waited += 0.5


def _echo_new(log: Path, offset: int, echo: Callable[[str], None]) -> int:
    try:
        with log.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
    except OSError:
        return offset
    for line in chunk.decode("utf-8", errors="replace").splitlines():
        echo(line)
    return offset + len(chunk)


def _guarded(fn: Callable[[], None]) -> str | None:
    """Run `fn`; the error text when it raised, else None (a worker must not die on a side issue)."""
    try:
        fn()
    except Exception as e:
        sys.stderr.write(f"{type(e).__name__}: {e}\n")
        return f"{type(e).__name__}: {e}"
    return None


def _restart(relaunch: Relauncher | None) -> str | None:
    """Start Nightshift again; the problem as text, or None (also when there is nothing to start)."""
    return None if relaunch is None else _guarded(relaunch.restart)


@dataclass
class Outcome:
    """What an update did: a one-line sentence, the versions, and whether anything changed."""

    message: str
    old: str = ""
    new: str = ""
    updated: bool = True


def run_worker(
    paths: Paths,
    body: Callable[[], Outcome],
    *,
    log: Path | None = None,
    wait_pid: int | None = None,
    wait: Callable[[int], None] | None = None,
    old_version: str = "",
    relaunch: Relauncher | None = None,
) -> int:
    """Run the update in this (detached) process; returns the exit code.

    stdout and stderr go to `log` first thing: a detached process has no console. `body` returns an `Outcome`
    and raises `NsError` on failure. The state file says `running` BEFORE the engine is stopped, then `done`
    or `failed` (`{state, old, new, message, updated_at}`, read by the dashboard).

    With `relaunch`, the engine (and the dashboard window, when one was open) is started again after the
    update, whether it applied or was rolled back, and `done` / `failed` is written only after the engine answers
    `/status`, so a page that reconnects finds a live dashboard and the final state. When the engine does not
    come back the state is `failed` with `restart_failed` set (and `updated` true when the new version is in
    place), so the page can say what happened instead of waiting for ever.
    """
    sink: TextIO | None = None
    saved = (sys.stdout, sys.stderr)
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        sink = log.open("a", encoding="utf-8", buffering=1)
        sys.stdout = sink
        sys.stderr = sink
    base: dict[str, Any] = {"log": str(log) if log else "", "pid": os.getpid(), "old": old_version}
    try:
        write_status(paths, "running", "Waiting for Nightshift to close.", **base)
        if wait_pid is not None:
            (wait or wait_for_parent)(wait_pid)
        if relaunch is not None:
            _guarded(relaunch.observe)
        write_status(paths, "running", "Updating.", **base)
        try:
            out = body()
        except NsError as e:
            sys.stderr.write(f"{e}\nHint: {e.definition.hint}\n")
            message = f"{e.code}: {e.detail}"
        except Exception as e:  # a worker has nobody to show a traceback to: log it, report failure
            sys.stderr.write(f"{type(e).__name__}: {e}\n")
            message = f"{type(e).__name__}: {e}"
        else:
            versions = {**base, "old": out.old or old_version, "new": out.new, "updated": out.updated}
            if _restart(relaunch):
                write_status(paths, "failed", f"{out.message} {RESTART_HINT}", exit_code=1,
                             restart_failed=True, **versions)
                return 1
            write_status(paths, "done", out.message, exit_code=0, **versions)
            return 0
        # rolled back (or never started): the old version comes back up
        if _restart(relaunch):
            write_status(paths, "failed", f"{message} {RESTART_HINT}", exit_code=1,
                         restart_failed=True, **base)
        else:
            write_status(paths, "failed", message, exit_code=1, **base)
        return 1
    finally:
        if sink is not None:
            sink.flush()
            sys.stdout, sys.stderr = saved
            sink.close()
