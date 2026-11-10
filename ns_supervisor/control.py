"""C2 control helpers behind `ns stop`, plus shared lookups for the CLI tools."""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psutil

from ns_common.atomic import read_text_retry
from ns_common.config import load_install_state
from ns_common.errors import NsError
from ns_common.models import Runtime
from ns_common.paths import Paths, repo_root
from ns_common.runtime_read import read_runtime as read_runtime  # the one reading for the commands
from ns_supervisor import procs, stopping
from ns_supervisor.childenv import child_env, window_flags
from ns_supervisor.client import SupervisorClient


def app_path(paths: Paths) -> Path:
    """The extracted repo (`install_state.app_path`), or this checkout when the installer has not run."""
    st = load_install_state(paths)
    return Path(st.app_path) if st.app_path else repo_root()


def runtime_or_default(paths: Paths) -> Runtime:
    """`runtime.json`, or the gpu defaults when it cannot be used (missing, damaged, newer, re-encoded or
    unreadable: `read_runtime`). Only the engine's address is taken from it, so a command that must still
    answer (`ns doctor`, `ns stop`) finds an engine on the default port instead of stopping on the file; a
    command that needs the settings calls `read_runtime` and says NS-E125."""
    try:
        return read_runtime(paths)
    except NsError:
        return Runtime.default("gpu")


def image_name(pid: int) -> str | None:
    """Executable name of `pid` via tasklist; None if there is no such process."""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=child_env(),
            creationflags=window_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
    if not line.startswith('"'):
        return None
    return line.split('","')[0].strip('"').lower()


def wait_down(
    client: SupervisorClient,
    wait_s: float = 45,
    *,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Poll until the supervisor stops answering `/status`; True when it did within `wait_s`."""
    deadline = clock() + wait_s
    while client.is_up():
        if clock() >= deadline:
            return False
        sleep(0.25)
    return True


#: After `/status` went quiet, how long `stop` waits for the engine's recorded processes to be gone.
ENGINE_EXIT_WAIT_S = 20.0


def engine_pids(paths: Paths) -> list[int]:
    """The supervisor's pid and its children's, as `run/supervisor.json` records them (empty: no record)."""
    try:
        data = json.loads(read_text_retry(paths.home / "run" / "supervisor.json"))
        return [int(data["pid"]), *(int(v) for v in dict(data.get("children", {})).values())]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def wait_gone(
    pids: list[int],
    wait_s: float = ENGINE_EXIT_WAIT_S,
    *,
    alive: Callable[[int], bool] = psutil.pid_exists,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> list[int]:
    """Wait until none of `pids` exists any more; returns the ones still there after `wait_s`.

    An engine that stopped answering `/status` has not ended yet: its venv interpreters stay loaded for a
    moment, and an update that goes on at once finds their files in use (D-39)."""
    deadline = clock() + wait_s
    left = [p for p in pids if alive(p)]
    while left and clock() < deadline:
        sleep(0.25)
        left = [p for p in left if alive(p)]
    return left


def stop(
    paths: Paths,
    client: SupervisorClient,
    *,
    kill: Callable[[int], None] = procs.kill_tree,
    identify: Callable[[int], str | None] = image_name,
    wait_s: float = 45,
    exit_wait_s: float = ENGINE_EXIT_WAIT_S,
    holders: Callable[[], list[Any]] | None = None,
    say: Callable[[str], None] | None = None,
) -> list[str]:
    """Stop the engine; returns report lines for the console.

    D-46: when the engine answers, or does not answer but its own processes still run (one that closed its
    port and never exited holds `run/supervisor.lock`), this is `stopping.stop_nightshift`: what is ours is
    noted, the engine is asked, everything is waited for (with `holders`, also every program that runs from
    the environments), and what is ours and still there after `stopping.STOP_WAIT_S` is ended by its
    recorded process. `say` gets each progress line. Otherwise the pids recorded in run/supervisor.json are
    killed when their image is still the recorded kind (an engine that died and left its record).
    """
    if client.is_up() or stopping.engine_holder(paths) is not None:
        done = stopping.stop_nightshift(paths, client, holders=holders, say=say)
        lines = list(done.lines)
        if done.not_ended:
            lines.append("Still running, and could not be ended: " + "; ".join(done.not_ended) + ".")
        return lines
    state_file = paths.home / "run" / "supervisor.json"
    if not state_file.exists():
        return ["The engine is not running."]
    try:
        data = json.loads(read_text_retry(state_file))
        sup_pid = int(data["pid"])
        children = {str(k): int(v) for k, v in dict(data.get("children", {})).items()}
    except (OSError, ValueError, KeyError, TypeError):
        state_file.unlink(missing_ok=True)
        return ["Removed an unreadable engine state file; nothing to stop."]
    lines: list[str] = []
    for name, pid in children.items():
        if identify(pid) is None:
            continue
        kill(pid)
        lines.append(f"Killed {name} (pid {pid}).")
    img = identify(sup_pid)
    if img is not None and "python" in img:
        kill(sup_pid)
        lines.append(f"Killed the supervisor (pid {sup_pid}).")
    else:
        lines.append(f"Supervisor pid {sup_pid} is not running (stale state file).")
    state_file.unlink(missing_ok=True)
    return lines
