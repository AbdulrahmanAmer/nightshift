"""C2 PowerShell runner: scripts go through `-EncodedCommand`, so no quoting or policy issues."""

from __future__ import annotations

import base64
import shutil
import subprocess

from ns_common.testguard import refuse_machine_writes, refuse_task_scheduler
from ns_supervisor.childenv import child_env


class PowerShellMissing(RuntimeError):
    """PowerShell is not on PATH (non-Windows machine or stripped image)."""


def find_powershell() -> str | None:
    """Windows PowerShell 5.1 (preferred) or PowerShell 7 on PATH."""
    return shutil.which("powershell") or shutil.which("pwsh")


def encode(script: str) -> str:
    """Base64 of the UTF-16LE script, as `-EncodedCommand` expects."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def run_powershell(
    script: str, *, env_extra: dict[str, str] | None = None, timeout_s: float = 60
) -> subprocess.CompletedProcess[str]:
    """Run `script` in a fresh non-interactive PowerShell; raises `PowerShellMissing` if there is none.

    This is the one place the supervisor starts PowerShell, so it is where a test run is refused the real
    Task Scheduler (also to read it), a real notification and every other change to this PC
    (`ns_common.testguard.RealMachineUnderTest`), before anything is started.
    """
    refuse_task_scheduler(script)
    refuse_machine_writes(script)
    exe = find_powershell()
    if exe is None:
        raise PowerShellMissing("powershell was not found on PATH")
    env = child_env(env_extra)
    return subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encode(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout_s,
        check=False,
    )
