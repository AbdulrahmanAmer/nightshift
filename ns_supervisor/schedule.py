"""C2 scheduled task `Nightshift Daily` (spec 15.9): Register-ScheduledTask through PowerShell.

Wakes the PC (`WakeToRun`), catches up after downtime (`StartWhenAvailable`), runs whether or not the
user is logged on (S4U, no stored password) when Windows allows it, else only while the user is logged on
(interactive logon, still waking the PC), and stops after 6 hours.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ns_common.devmode import is_dev
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_supervisor import pwsh
from ns_supervisor.schedule_cfg import (
    TASK_NAME,
    clear_run_time,
    read_run_time,
    read_schedule_mode,
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

    The working directory is the install home, not ``app_path``: a process whose cwd is the program folder
    blocks the folder swap of an update (Windows refuses to rename it), and the 03:00 run keeps ``studio.exe``
    and its children alive for hours. The programs find the app through their editable installs.

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
            f"-WorkingDirectory {_q(str(paths.home))}",
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

KEPT_OFF_MARK = "kept SWITCHED OFF"
KEPT_OFF_NOTE = f"{KEPT_OFF_MARK}, as it was in Task Scheduler (it does not run until you switch it on there)"


@dataclass(frozen=True)
class TaskState:
    """What Windows has for the task now: switched on or off, and the clock time of its daily trigger."""

    enabled: bool
    run_time: str | None


_START_TIME = re.compile(r"T(\d{2}):(\d{2})")


def build_read_script(task_name: str = TASK_NAME) -> str:
    """PowerShell that prints the task's state and the start of its first trigger (nothing if no task)."""
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"$t = Get-ScheduledTask -TaskName {_q(task_name)} -ErrorAction SilentlyContinue",
            "if ($null -eq $t) { Write-Output 'not registered'; exit 0 }",
            "Write-Output ('state: ' + $t.State)",
            "$g = @($t.Triggers) | Select-Object -First 1",
            "if ($g -and $g.StartBoundary) { Write-Output ('start: ' + $g.StartBoundary) }",
        ]
    )


def parse_task_state(out: str) -> TaskState | None:
    """The `TaskState` a `build_read_script` run printed; None when it says there is no task."""
    state: str | None = None
    run_time: str | None = None
    for line in out.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "state":
            state = value.strip()
        elif key.strip() == "start":
            m = _START_TIME.search(value)         # the clock time as written, whatever offset follows it
            if m is not None and valid_time(f"{m.group(1)}:{m.group(2)}"):
                run_time = f"{m.group(1)}:{m.group(2)}"
    if not state:
        return None
    return TaskState(enabled=state.lower() != "disabled", run_time=run_time)


def read_task(task_name: str = TASK_NAME) -> TaskState | None:
    """The task as Windows has it now, or None when not registered. `ScheduleError` if PowerShell fails."""
    return parse_task_state(_run(build_read_script(task_name)))


def install(
    paths: Paths,
    run_time: str | None,
    app_path: Path,
    *,
    disabled: bool = False,
    keep_state: bool = False,
    log: NsLogger | None = None,
) -> str:
    """Register the task at `run_time` (default: the saved time, else 03:00) and remember it.

    The message says when the task could only be registered for "run only when the user is logged on".
    With `disabled` the task is registered and switched off, and `schedule.json` says so (mode `disabled`: no
    run is coming, so the supervisor and the dashboard do not expect one). A dev install (`runtime.json` says
    `dev_mode`) is always registered disabled, also when the dashboard or `ns update` re-registers it.

    With `keep_state` (an update, the Settings page, `ns schedule install`: anything that replaces a task the
    owner may have changed in Task Scheduler) the task is read BEFORE it is replaced: a task that was switched
    off is registered switched off again, and without an explicit `run_time` the task's own trigger time is
    kept. The task in Windows is the truth; `schedule.json` is rewritten to match it. When there is no task
    the record in `schedule.json` is all there is: mode `disabled` registers it off, anything else on, as a
    fresh install does. If the task cannot be read nothing is replaced (`ScheduleError`).
    """
    before = read_task() if keep_state else None
    recorded = read_run_time(paths, include_disabled=keep_state)
    if run_time:
        when, source = run_time, "argument"
    elif before is not None and before.run_time:
        when, source = before.run_time, "task"
    elif recorded:
        when, source = recorded, "schedule.json"
    else:
        when, source = DEFAULT_TIME, "default"
    if not valid_time(when):
        raise ScheduleError(f"time must be HH:MM (24 h), got {when!r}")
    forced_off = disabled or is_dev(paths)      # a dev install never gets an enabled task, whoever asks
    if before is not None:
        kept_off = not before.enabled
    else:
        kept_off = keep_state and read_schedule_mode(paths) == "disabled"
    disabled = forced_off or kept_off
    out = _run(build_install_script(paths, when, app_path, disabled=disabled))
    interactive = "registered:interactive" in out
    mode = "disabled" if disabled else "interactive_only" if interactive else "registered"
    write_run_time(paths, when, mode)
    if keep_state:
        _log_kept(log or NsLogger("schedule", paths.logs), before, when, source, enabled=not disabled)
    if forced_off:
        return f"registered ({TASK_NAME} daily at {when}; {DISABLED_NOTE})"
    if kept_off:
        return f"registered ({TASK_NAME} daily at {when}; {KEPT_OFF_NOTE})"
    note = f"; {INTERACTIVE_NOTE}" if interactive else ""
    return f"registered ({TASK_NAME} daily at {when}{note})"


def _log_kept(log: NsLogger, before: TaskState | None, when: str, source: str, *, enabled: bool) -> None:
    """One log line saying what a re-registration kept (or that there was nothing to keep)."""
    after = "enabled" if enabled else "disabled"
    if before is None:
        log.info(
            "schedule_state_no_task",
            f"no '{TASK_NAME}' task existed; registered it {after} at {when} (time from {source})",
            after_enabled=enabled, after_time=when, time_source=source,
        )
        return
    was = "enabled" if before.enabled else "disabled"
    log.info(
        "schedule_state_kept",
        f"'{TASK_NAME}' was {was} at {before.run_time}; registered it {after} at {when} (time from {source})",
        before_enabled=before.enabled, before_time=before.run_time,
        after_enabled=enabled, after_time=when, time_source=source,
    )


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
