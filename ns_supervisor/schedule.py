"""C2 scheduled task `Nightshift Daily` (spec 15.9): Register-ScheduledTask through PowerShell.

Wakes the PC (`WakeToRun`), catches up after downtime (`StartWhenAvailable`), runs whether or not the
user is logged on (S4U, no stored password) and stops after 6 hours.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ns_common.paths import Paths
from ns_supervisor import pwsh
from ns_supervisor.schedule_cfg import (
    TASK_NAME,
    clear_run_time,
    read_run_time,
    valid_time,
    write_run_time,
)

DEFAULT_TIME = "03:00"


class ScheduleError(RuntimeError):
    """PowerShell refused to register, remove or read the task."""


def _q(text: str) -> str:
    """Single-quote a value for a PowerShell literal string."""
    return "'" + text.replace("'", "''") + "'"


def studio_exe(paths: Paths) -> str:
    """`studio.exe` of the studio venv, or plain `studio` (PATH) when the venv is missing."""
    exe = paths.venvs / "studio" / "Scripts" / "studio.exe"
    return str(exe) if exe.exists() else "studio"


def build_install_script(paths: Paths, run_time: str, app_path: Path, task_name: str = TASK_NAME) -> str:
    """PowerShell that registers the daily task. NIGHTSHIFT_HOME is set inside the action because an
    S4U session does not reliably carry the user's environment."""
    arguments = f'/d /s /c "set "NIGHTSHIFT_HOME={paths.home}" && "{studio_exe(paths)}" run --daily"'
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument {_q(arguments)} "
            f"-WorkingDirectory {_q(str(app_path))}",
            f"$trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]{_q(run_time)})",
            "$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable "
            "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
            "-ExecutionTimeLimit (New-TimeSpan -Hours 6)",
            "$principal = New-ScheduledTaskPrincipal "
            "-UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) "
            "-LogonType S4U -RunLevel Limited",
            f"Register-ScheduledTask -TaskName {_q(task_name)} -Action $action -Trigger $trigger "
            "-Settings $settings -Principal $principal -Description 'Nightshift nightly run' -Force | Out-Null",
            "Write-Output 'registered'",
        ]
    )


def build_remove_script(task_name: str = TASK_NAME) -> str:
    """PowerShell that unregisters the task if it exists."""
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"if (Get-ScheduledTask -TaskName {_q(task_name)} -ErrorAction SilentlyContinue) {{",
            f"  Unregister-ScheduledTask -TaskName {_q(task_name)} -Confirm:$false",
            "  Write-Output 'removed'",
            "} else {",
            "  Write-Output 'not registered'",
            "}",
        ]
    )


def build_show_script(task_name: str = TASK_NAME) -> str:
    """PowerShell that prints the task state, next run and wake setting."""
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"$t = Get-ScheduledTask -TaskName {_q(task_name)} -ErrorAction SilentlyContinue",
            "if ($null -eq $t) { Write-Output 'not registered'; exit 0 }",
            f"$i = Get-ScheduledTaskInfo -TaskName {_q(task_name)}",
            "Write-Output ('state: ' + $t.State)",
            "Write-Output ('next run: ' + $i.NextRunTime)",
            "Write-Output ('last run: ' + $i.LastRunTime)",
            "Write-Output ('last result: ' + $i.LastTaskResult)",
            "Write-Output ('wake to run: ' + $t.Settings.WakeToRun)",
        ]
    )


def _run(script: str) -> str:
    try:
        r = pwsh.run_powershell(script)
    except pwsh.PowerShellMissing as e:
        raise ScheduleError(str(e)) from e
    except subprocess.TimeoutExpired as e:
        raise ScheduleError("PowerShell timed out") from e
    if r.returncode != 0:
        raise ScheduleError((r.stderr or r.stdout).strip() or f"PowerShell exited {r.returncode}")
    return r.stdout.strip()


def install(paths: Paths, run_time: str | None, app_path: Path) -> str:
    """Register the task at `run_time` (default: the saved time, else 03:00) and remember it."""
    when = run_time or read_run_time(paths) or DEFAULT_TIME
    if not valid_time(when):
        raise ScheduleError(f"time must be HH:MM (24 h), got {when!r}")
    out = _run(build_install_script(paths, when, app_path))
    write_run_time(paths, when)
    return f"{out or 'registered'} ({TASK_NAME} daily at {when})"


def remove(paths: Paths) -> str:
    """Unregister the task and forget the schedule."""
    out = _run(build_remove_script())
    clear_run_time(paths)
    return out or "removed"


def show(paths: Paths) -> str:
    """Human-readable task status."""
    saved = read_run_time(paths)
    out = _run(build_show_script())
    return out + (f"\nconfigured time: {saved}" if saved else "")
