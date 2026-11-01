"""C2 control helpers behind `ns stop` and `ns gate`, plus shared lookups for the CLI tools."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

from ns_common.config import load_install_state, load_runtime
from ns_common.errors import NsError
from ns_common.models import Runtime
from ns_common.paths import Paths, repo_root
from ns_supervisor import procs
from ns_supervisor.client import SupervisorClient


def app_path(paths: Paths) -> Path:
    """The extracted repo (`install_state.app_path`), or this checkout when the installer has not run."""
    st = load_install_state(paths)
    return Path(st.app_path) if st.app_path else repo_root()


def runtime_or_default(paths: Paths) -> Runtime:
    """`runtime.json`, or the gpu defaults when it does not exist yet."""
    try:
        return load_runtime(paths)
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
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = r.stdout.strip().splitlines()[0] if r.stdout.strip() else ""
    if not line.startswith('"'):
        return None
    return line.split('","')[0].strip('"').lower()


def stop(
    paths: Paths,
    client: SupervisorClient,
    *,
    kill: Callable[[int], None] = procs.kill_tree,
    identify: Callable[[int], str | None] = image_name,
    wait_s: float = 45,
) -> list[str]:
    """Stop the engine: `POST /shutdown` if it answers, else kill the pids recorded in run/supervisor.json.

    Returns report lines for the console.
    """
    if client.is_up():
        client.shutdown(30)
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline and client.is_up():
            time.sleep(0.25)
        return ["Engine stopped through the supervisor API."]
    state_file = paths.home / "run" / "supervisor.json"
    if not state_file.exists():
        return ["The engine is not running."]
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
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


def run_gate(milestone: str, app: Path) -> tuple[int, str]:
    """Run `dev/gate_<milestone>.py`; returns (exit code, message). 2 when the script is missing."""
    name = milestone.lower()
    if name.isdigit():
        name = f"m{name}"
    script = app / "dev" / f"gate_{name}.py"
    if not script.exists():
        return 2, f"No gate script at {script}"
    r = subprocess.run([sys.executable, str(script)], cwd=str(app), check=False)
    return r.returncode, f"gate {name} exited {r.returncode}"
