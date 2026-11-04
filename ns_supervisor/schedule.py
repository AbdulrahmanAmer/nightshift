"""C2 scheduled task `Nightshift Daily` (spec 15.9): Register-ScheduledTask through PowerShell.

Wakes the PC (`WakeToRun`), catches up after downtime (`StartWhenAvailable`), runs whether or not the
user is logged on (S4U, no stored password) when Windows allows it, else only while the user is logged on
(interactive logon, still waking the PC), and stops after 6 hours.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ns_common.devmode import is_dev
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


def build_install_script(
    paths: Paths, run_time: str, app_path: Path, task_name: str = TASK_NAME, *, disabled: bool = False
) -> str:
    """PowerShell that registers the daily task. NIGHTSHIFT_HOME is set inside the action because an
    S4U session does not reliably carry the user's environment.

    S4U ("run whether the user is logged on or not", no stored password) is tried first. Registering it from a
    non-elevated shell is often refused with "Access is denied"; the script then registers the same task with
    an interactive logon ("run only when the user is logged on"; WakeToRun still wakes the PC). The last
    output line is `registered:s4u` or `registered:interactive`.

    `disabled` (a dev install, docs/DEV_MODE.md) registers the same task and then disables it, so it never
    fires on the development PC; `ns schedule show` and the uninstall still find it by name.
    """
    arguments = f'/d /s /c "set "NIGHTSHIFT_HOME={paths.home}" && "{studio_exe(paths)}" run --daily"'
    register = (
        f"Register-ScheduledTask -TaskName {_q(task_name)} -Action $action -Trigger $trigger "
        "-Settings $settings -Principal $principal -Description 'Nightshift nightly run' -Force | Out-Null"
    )
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument {_q(arguments)} "
            f"-WorkingDirectory {_q(str(app_path))}",
            f"$trigger = New-ScheduledTaskTrigger -Daily -At ([datetime]{_q(run_time)})",
            "$settings = New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable "
            "-AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
            "-ExecutionTimeLimit (New-TimeSpan -Hours 6)",
            "$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name",
            "$mode = 's4u'",
            "try {",
            "  $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType S4U -RunLevel Limited",
            f"  {register}",
            "} catch {",
            "  $mode = 'interactive'",
            "  $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive "
            "-RunLevel Limited",
            f"  {register}",
            "}",
            *([f"Disable-ScheduledTask -TaskName {_q(task_name)} | Out-Null"] if disabled else []),
            "Write-Output ('registered:' + $mode)",
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


INTERACTIVE_MARK = "only while you are logged on"
INTERACTIVE_NOTE = f"runs {INTERACTIVE_MARK} (Windows refused the unattended mode)"


DISABLED_NOTE = "DISABLED: dev mode, it never runs by itself"


def install(paths: Paths, run_time: str | None, app_path: Path, *, disabled: bool = False) -> str:
    """Register the task at `run_time` (default: the saved time, else 03:00) and remember it.

    The message says when the task could only be registered for "run only when the user is logged on".
    With `disabled` the task is registered and switched off, and `schedule.json` says so (mode `disabled`: no
    run is coming, so the supervisor and the dashboard do not expect one). A dev install (`runtime.json` says
    `dev_mode`) is always registered disabled, also when the dashboard or `ns update` re-registers it.
    """
    when = run_time or read_run_time(paths) or DEFAULT_TIME
    if not valid_time(when):
        raise ScheduleError(f"time must be HH:MM (24 h), got {when!r}")
    disabled = disabled or is_dev(paths)        # a dev install never gets an enabled task, whoever asks
    out = _run(build_install_script(paths, when, app_path, disabled=disabled))
    interactive = "registered:interactive" in out
    mode = "disabled" if disabled else "interactive_only" if interactive else "registered"
    write_run_time(paths, when, mode)
    if disabled:
        return f"registered ({TASK_NAME} daily at {when}; {DISABLED_NOTE})"
    note = f"; {INTERACTIVE_NOTE}" if interactive else ""
    return f"registered ({TASK_NAME} daily at {when}{note})"


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
