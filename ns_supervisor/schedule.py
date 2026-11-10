"""C2 scheduled task `Nightshift Daily` (spec 15.9): Register-ScheduledTask through PowerShell.

Wakes the PC (`WakeToRun`), catches up after downtime (`StartWhenAvailable`), runs whether or not the
user is logged on (S4U, no stored password) when Windows allows it, else only while the user is logged on
(interactive logon, still waking the PC), and stops after 6 hours.
"""

from __future__ import annotations

import base64
import contextlib
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import NoReturn

from installer.stage2 import shortcut_owner
from ns_common.devmode import is_dev
from ns_common.errors import describe
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_common.singleton import AlreadyRunning, FileLock
from ns_supervisor import notify, pwsh
from ns_supervisor.schedule_cfg import (
    MODE_DISABLED,
    ON_MODES,
    TASK_NAME,
    clear_run_time,
    read_run_time,
    read_schedule_mode,
    remember_run_time,
    remembered_run_time,
    schedule_file,
    valid_time,
    write_run_time,
)
from ns_supervisor.schedule_cfg import is_on as record_says_on

DEFAULT_TIME = "03:00"
#: How long a change waits for another change (a second dashboard, `ns schedule`, an update) before it is
#: refused with nothing changed.
LOCK_WAIT_S = 10.0
#: The bound on one look at the task from a page or from `ns verify --quick`.
READ_TIMEOUT_S = 20.0


class ScheduleError(RuntimeError):
    """PowerShell refused to register, remove or read the task."""


class ScheduleBusy(ScheduleError):
    """Another change to the task holds `run/schedule.lock`; nothing was changed."""


class ScheduleFileError(ScheduleError):
    """A file of Nightshift's own (the lock, the record, the kept time) could not be opened or written."""


class ScheduleUndone(ScheduleFileError):
    """Windows took the change, the record could not be written, and Windows was put back: task and record
    agree again. `on` and `run_time` say what Automatic runs still are."""

    def __init__(self, message: str, *, on: bool, run_time: str | None, why: str = "") -> None:
        super().__init__(message)
        self.on, self.run_time, self.why = on, run_time, why


class ScheduleUnrecorded(ScheduleFileError):
    """NS-W208: Windows runs the task at `run_time`, the record could not be written and the change could
    not be undone, so Nightshift's pages say something else until `config` can be written again."""

    def __init__(self, message: str, *, run_time: str | None, why: str = "") -> None:
        super().__init__(message)
        self.run_time, self.why = run_time, why


class DevInstall(ScheduleError):
    """A dev install's task never runs (docs/DEV_MODE.md): Automatic runs cannot be switched on."""


def _q(text: str) -> str:
    """Single-quote a value for a PowerShell literal string."""
    return "'" + text.replace("'", "''") + "'"


def studio_exe(paths: Paths) -> str:
    """`studio.exe` of the studio venv, or plain `studio` (PATH) when the venv is missing."""
    exe = paths.venvs / "studio" / "Scripts" / "studio.exe"
    return str(exe) if exe.exists() else "studio"


def action_arguments(paths: Paths) -> str:
    r"""What the task passes to cmd.exe. NIGHTSHIFT_HOME is set inside the action because an S4U session
    does not reliably carry the user's environment (and every release's `task_owner` reads the home from it).

    The nightly run starts through `<home>\nightshift-launch.ps1 -Nightly` (`installer/launch.ps1`, which
    setup and the first start of a version copy beside the program folder): when the program folder is
    missing after an interrupted update, that script tells the owner, where `studio.exe` could only fail
    unseen. `if exist` keeps tonight's run alive when the script itself was deleted: then the action is
    what it always was."""
    stub, studio = paths.home / shortcut_owner.STUB_NAME, studio_exe(paths)
    return (
        f'/d /s /c "set "NIGHTSHIFT_HOME={paths.home}" && if exist "{stub}" (powershell.exe -NoProfile '
        f'-NonInteractive -ExecutionPolicy Bypass -File "{stub}" -Nightly -Studio "{studio}") '
        f'else ("{studio}" run --daily)"'
    )


def is_stub_action(arguments: str) -> bool:
    """True when a task's action starts the nightly run through the launch script."""
    return f'{shortcut_owner.STUB_NAME}" -nightly' in arguments.lower()


def build_install_script(
    paths: Paths, run_time: str, app_path: Path, task_name: str = TASK_NAME, *, disabled: bool = False
) -> str:
    """PowerShell that registers the daily task with the action `action_arguments` builds.

    S4U ("run whether the user is logged on or not", no stored password) is tried first. Registering it from a
    non-elevated shell is often refused with "Access is denied"; the script then registers the same task with
    an interactive logon ("run only when the user is logged on"; WakeToRun still wakes the PC). The last
    output line is `registered:s4u` or `registered:interactive`.

    The working directory is the install home, not ``app_path``: a process whose cwd is the program folder
    blocks the folder swap of an update (Windows refuses to rename it), and the 03:00 run keeps ``studio.exe``
    and its children alive for hours. The programs find the app through their editable installs.

    `disabled` (a dev install, docs/DEV_MODE.md; `ns schedule install --disabled`) registers the task disabled
    in the one call (`New-ScheduledTaskSettingsSet -Disable`): there is no moment in which it is enabled and
    can fire, and no second call that can fail and leave it on. `ns schedule show` and the uninstall still
    find it by name.
    """
    arguments = action_arguments(paths)
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
            "-ExecutionTimeLimit (New-TimeSpan -Hours 6)" + (" -Disable" if disabled else ""),
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
            "Write-Output ('registered:' + $mode)",
        ]
    )


def build_action_script(paths: Paths, task_name: str = TASK_NAME) -> str:
    """PowerShell that gives the existing task the action `action_arguments` builds and changes nothing
    else: `Set-ScheduledTask -Action` keeps its trigger, its logon type (the principal) and its settings,
    where registering it again would have to state them anew."""
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            f"$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument {_q(action_arguments(paths))} "
            f"-WorkingDirectory {_q(str(paths.home))}",
            f"Set-ScheduledTask -TaskName {_q(task_name)} -Action $action | Out-Null",
            "Write-Output 'action set'",
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


def _run(script: str, timeout_s: float = 60) -> str:
    try:
        r = pwsh.run_powershell(script, timeout_s=timeout_s)
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
#: What `install` answers when a dev install (or `--disabled`) finds the PC's task running another
#: Nightshift folder: the folder follows, then `;`. Setup's scheduler step reads the folder from it.
DEV_LEFT = "left as it is, it runs the Nightshift at "

KEPT_OFF_MARK = "kept SWITCHED OFF"
KEPT_OFF_NOTE = (
    f"{KEPT_OFF_MARK}, as it was in Task Scheduler (it does not run until you switch it on there "
    "or run 'ns schedule install --enable')"
)


#: Task Scheduler logon types that run the task only while the user (or a member of the group) is signed in.
#: Everything else runs signed out too: S4U (what Nightshift registers), Password and InteractiveOrPassword
#: ("run whether user is logged on or not" with a saved password, set by the owner in Task Scheduler),
#: ServiceAccount.
SIGNED_IN_ONLY_LOGONS = frozenset({"interactive", "group"})


@dataclass(frozen=True)
class TaskState:
    """What Windows has for the task now: switched on or off, the clock time of its daily trigger, whether
    it runs only while the owner is signed in (None when Windows did not say), and the install folder its
    action names (`home`; None when the action does not say, as in a task made by hand), and whether the
    action starts the run through the launch script (`stub`; 0.1.14's action did not). `logon` is the logon
    type as Windows names it (S4U, Interactive, Password, ...; None when it did not say)."""

    enabled: bool
    run_time: str | None
    interactive: bool | None = None
    home: str | None = None
    stub: bool = False
    logon: str | None = None


#: `2026-11-09T04:15:00`; `Z` or `+02:00` follows the seconds when the trigger synchronizes across time zones.
_START = re.compile(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(Z|[+-]\d{2}:\d{2})?")


def build_read_script(task_name: str = TASK_NAME) -> str:
    """PowerShell that prints the task's state, the start of its first trigger and its logon type.

    `not registered` means Windows has no such task (error category ObjectNotFound). Any other failure of
    `Get-ScheduledTask` (access denied, a corrupt task) prints `error: <category>: <message>` and is NOT read
    as "no task": the caller refuses instead of replacing a task it could not look at.

    The action is printed as base64 of its UTF-8 bytes (`args64`): Windows PowerShell 5.1 writes redirected
    output in the console's OEM code page, which turned a home with other characters into `?` or U+FFFD, and
    such a home then read as a deleted install.
    """
    return "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            "$e = $null",
            f"$t = Get-ScheduledTask -TaskName {_q(task_name)} -ErrorAction SilentlyContinue "
            "-ErrorVariable e",
            "if ($null -eq $t) {",
            "  $bad = @($e) | Where-Object { $_ -and $_.CategoryInfo.Category -ne 'ObjectNotFound' } "
            "| Select-Object -First 1",
            "  if ($bad) { Write-Output ('error: ' + $bad.CategoryInfo.Category + ': ' + "
            "$bad.Exception.Message); exit 0 }",
            "  Write-Output 'not registered'; exit 0",
            "}",
            "Write-Output ('state: ' + $t.State)",
            "$g = @($t.Triggers) | Select-Object -First 1",
            "if ($g -and $g.StartBoundary) { Write-Output ('start: ' + $g.StartBoundary) }",
            "if ($t.Principal) { Write-Output ('logon: ' + $t.Principal.LogonType) }",
            "$a = @($t.Actions) | Select-Object -First 1",
            "if ($a -and $a.Arguments) { Write-Output ('args64: ' + [Convert]::ToBase64String("
            "[Text.Encoding]::UTF8.GetBytes([string]$a.Arguments))) }",
        ]
    )


def _local_clock(value: str, local_tz: tzinfo | None) -> str | None:
    """The local `HH:MM` a StartBoundary means.

    Without a zone the clock time is local already. With `Z` or an offset the trigger synchronizes across time
    zones (one moment for every zone), so the moment is turned into this PC's clock time (`local_tz`, default
    the PC's own zone); reading the written clock time would shift the run by the offset.
    """
    m = _START.search(value)
    if m is None:
        return None
    year, month, day, hour, minute, second = (int(g) for g in m.groups()[:6])
    zone = m.group(7)
    when = datetime(year, month, day, hour, minute, second)
    if zone:
        sign = 1 if zone[0] != "-" else -1
        hours, minutes = (0, 0) if zone == "Z" else (int(zone[1:3]), int(zone[4:6]))
        offset = sign * timedelta(hours=hours, minutes=minutes)
        when = when.replace(tzinfo=timezone(offset)).astimezone(local_tz)
    clock = f"{when:%H:%M}"
    return clock if valid_time(clock) else None


def parse_task_state(out: str, local_tz: tzinfo | None = None) -> TaskState | None:
    """The `TaskState` a `build_read_script` run printed; None when it says there is no task.

    `ScheduleError` when it says the task could not be read (an `error:` line): that is not "no task".
    """
    state: str | None = None
    run_time: str | None = None
    interactive: bool | None = None
    home: str | None = None
    stub = False
    logon: str | None = None
    for line in out.splitlines():
        key, _, value = line.partition(":")
        if key.strip() == "error":
            raise ScheduleError(f"the nightly task could not be read ({value.strip()}); nothing was changed")
        if key.strip() == "state":
            state = value.strip()
        elif key.strip() == "start":
            run_time = _local_clock(value, local_tz)
        elif key.strip() == "logon" and value.strip():
            logon = value.strip()
            interactive = logon.lower() in SIGNED_IN_ONLY_LOGONS
        elif key.strip() in ("args", "args64"):
            if key.strip() == "args64":
                try:
                    value = base64.b64decode(value.strip(), validate=True).decode("utf-8")
                except ValueError:      # (UnicodeDecodeError is one): read as an action that names no home
                    value = ""
            named = _ACTION_HOME.search(value)
            home = named.group(1).strip() if named else None
            stub = is_stub_action(value)
    if not state:
        return None
    return TaskState(state.lower() != "disabled", run_time, interactive, home or None, stub, logon)


#: The install folder inside the action `build_install_script` writes (0.1.14 wrote it the same way).
_ACTION_HOME = re.compile(r'NIGHTSHIFT_HOME=([^"]+)"')

#: Whose task it is, seen from one install (`task_owner`). A PC has ONE 'Nightshift Daily' task and can
#: have several Nightshift folders: the task runs the install its action names.
MINE, GONE, OTHER, UNKNOWN, UNREACHABLE = "mine", "gone", "other", "unknown", "unreachable"


def task_owner(paths: Paths, task: TaskState) -> str:
    """`mine`: the task runs this install. `gone`: it names a folder that is no longer there (its Nightshift
    was deleted by hand; the task starts nothing). `other`: it runs another Nightshift folder that exists.
    `unreachable`: it names a folder on a drive or share that is not there now (it may come back: such a
    task is never removed and never pointed elsewhere without the owner's yes). `unknown`: its action names
    no folder (made or edited by hand), or a name that did not survive the read (U+FFFD): never `gone`."""
    if not task.home or "\ufffd" in task.home:
        return UNKNOWN
    return shortcut_owner.folder_state(task.home, paths.home)


def on_here(paths: Paths, task: TaskState | None) -> bool:
    """Automatic runs are on for THIS install: an enabled task that runs it (or does not say what it runs:
    such a task was always read as on). An enabled task of another Nightshift, or of a deleted one, is not."""
    return task is not None and task.enabled and task_owner(paths, task) in (MINE, UNKNOWN)


def _elsewhere(paths: Paths, task: TaskState | None) -> str | None:
    """The other Nightshift folder an enabled task runs (one that exists, or whose drive is not there
    now); None when there is no such task."""
    if task is not None and task.enabled and task.home and task_owner(paths, task) in (OTHER, UNREACHABLE):
        return task.home
    return None


def read_task(task_name: str = TASK_NAME, *, timeout_s: float | None = None) -> TaskState | None:
    """The task as Windows has it now, or None when not registered. `ScheduleError` if PowerShell fails."""
    script = build_read_script(task_name)
    return parse_task_state(_run(script) if timeout_s is None else _run(script, timeout_s))


# --------------------------------------------------------------------------------------------------------
# Automatic runs: on exactly while Windows holds an enabled task. The three rules, each kept in one place:
#   1. one source of truth: Windows. `schedule.json` is its record and `_record` is the only code that writes
#      or removes it for a real install, always from a `TaskState` Windows just gave (`_forget` removes it
#      ahead of a removal, which can only make the record say "off" early, never "on" without a task);
#   2. nothing says "on" without an enabled task: every reader asks `schedule_cfg` for the record, and
#      `sync_record` brings a record Windows no longer backs to "off" (pages: within a minute);
#   3. an update never changes the choice: `reregister` is the only thing an update calls, and it registers
#      only over a task that is enabled now.
#   4. a task Nightshift could not record does not stay: when the record cannot be written after Windows took
#      a registration, `_undo` puts Windows back, and only when that fails too is the owner told that Windows
#      runs something the pages do not show (NS-W208);
#   5. setup changes the choice only on a first install, or for a window answer made against the state Windows
#      still has (`setup_apply`); a Repair and a reinstall over an install leave it as Windows has it.
#   6. the PC has one task and may have several installs: "on" for an install means the task runs THAT install
#      (`on_here`). Another install's task is never removed, moved or re-registered without the owner's yes
#      (the switch, `ns schedule install`, setup's yes), and a yes that takes it over says so.
# Every change runs under `run/schedule.lock`, so two dashboards, `ns schedule` and an update take turns.
# --------------------------------------------------------------------------------------------------------
def lock_file(paths: Paths) -> Path:
    """`run/schedule.lock`: held for one read-change-record sequence; Windows drops it with the process."""
    return paths.home / "run" / "schedule.lock"


@contextlib.contextmanager
def _own_files() -> Iterator[None]:
    """Turn an `OSError` from Nightshift's own files (the record, the kept time, a note) into
    `ScheduleFileError`, so every caller that handles a refused change handles this too: the switch answers
    NS-E204, an update notes it and stands, the look changes nothing. The words do not claim more than is
    known: the write can fail after Windows already changed the task."""
    try:
        yield
    except OSError as e:
        raise ScheduleFileError(
            f"a file Nightshift keeps about the nightly task could not be read or written ({e}); Windows may "
            "already have the change, and the pages follow Windows once the Nightshift folders config and "
            "run can be written again (an antivirus or backup program, a full disk, or their permissions)"
        ) from e


@contextlib.contextmanager
def _locked(paths: Paths, wait_s: float | None = None) -> Iterator[None]:
    """Hold the schedule lock for one read-change-record sequence. Every way the lock can fail is a
    `ScheduleError` raised BEFORE anything is read or changed:

    - held by another change after `wait_s` (default LOCK_WAIT_S): `ScheduleBusy`;
    - the lock file cannot be created or opened (`run` is read-only or is not a folder, an antivirus or
      backup program holds the file, a lost permission): `ScheduleFileError`. Going ahead unlocked is not an
      option here: two interleaved Register and Unregister calls are what the lock exists to prevent.

    While it is held the file cannot vanish (the open handle keeps it); an error on release is swallowed,
    since the change is done and Windows drops the lock with the handle. An `OSError` from the body is
    `ScheduleFileError` too (`_own_files`).
    """
    lock = FileLock(lock_file(paths))
    deadline = time.monotonic() + (LOCK_WAIT_S if wait_s is None else wait_s)
    while True:
        try:
            lock.acquire()
            break
        except AlreadyRunning:
            if time.monotonic() >= deadline:
                raise ScheduleBusy(
                    "another change to the nightly task is still being made, so nothing was changed; "
                    "try again in a few seconds"
                ) from None
            time.sleep(0.05)
        except OSError as e:
            raise ScheduleFileError(
                f"the lock file {lock_file(paths)} could not be opened ({e}), so nothing was changed; make "
                f"sure the folder {lock_file(paths).parent} can be written (an antivirus or backup program, "
                "or its permissions), then try again"
            ) from e
    try:
        with _own_files():
            yield
    finally:
        with contextlib.suppress(OSError):
            lock.release()


def _waits(start: str) -> Callable[[list[notify.Note]], bool]:
    """For `notify.leave_note(once=)`: a note whose message begins with `start` still waits. The owner of the
    notes asks this between its read and its write, under its lock, so two checks at once say it once."""
    return lambda notes: any(n["message"].startswith(start) for n in notes)


def _note_switched_off_outside(paths: Paths, log: NsLogger, other: str | None = None) -> None:
    """NS-W207, once: a WARN in the log and a note the dashboard shows on its next page load. A note that
    still waits is not repeated (a pass killed after the note and before the record went must not say it
    twice); after the record is gone no later pass has anything to say. `other`: the task was not removed
    but now runs the Nightshift in that folder, and the note says that instead of the code's own hint."""
    try:
        d = describe("NS-W207")
        text = f"NS-W207: {d.message} {d.hint}"
        if other:
            text = (
                f"NS-W207: {d.message} This PC has one nightly task and it now runs the Nightshift at "
                f"{other} (Automatic runs were switched on there), so this one does not run by itself. "
                "Switch Automatic runs on in this one's Settings to move the task back."
            )
    except (KeyError, OSError, ValueError):
        text = "NS-W207: Automatic runs were switched off outside Nightshift."
    log.warn("automatic_runs_off_outside", text, code="NS-W207")
    try:
        notify.leave_note(paths, "Nightshift", text, once=_waits("NS-W207:"))
    except OSError as e:
        log.warn("morning_note_failed", str(e))


def _record(paths: Paths, task: TaskState | None, log: NsLogger, *, by_owner: bool = False) -> str:
    """Make `schedule.json` say what Windows has (`task` was just read): the mode it now records.

    Enabled task that runs this install (`on_here`): the record is on, at the task's own time,
    `interactive_only` when its logon is not S4U. No task, one disabled in Task Scheduler, or one that runs
    another Nightshift folder: no record; the time moves to `dashboard.json` first, so a kill between the two
    leaves a record the next pass removes again. When the record said on and this was not the owner's own
    change here (`by_owner`), the owner is told once (NS-W207).
    """
    mode, when = read_schedule_mode(paths), read_run_time(paths, include_disabled=True)
    if task is not None and on_here(paths, task):
        at = task.run_time or when or remembered_run_time(paths) or DEFAULT_TIME
        signed_in_only = mode == "interactive_only" if task.interactive is None else task.interactive
        now = "interactive_only" if signed_in_only else "registered"
        if (mode, when) != (now, at):
            write_run_time(paths, at, now)
        return now
    if schedule_file(paths).exists():
        ours = task is not None and task_owner(paths, task) in (MINE, UNKNOWN)   # its time is this install's
        kept = (task.run_time if task is not None and ours else None) or when
        if kept:
            remember_run_time(paths, kept)
        if mode in ON_MODES and not by_owner:
            _note_switched_off_outside(paths, log, _elsewhere(paths, task))
        clear_run_time(paths)
    return "none"


def _forget(paths: Paths, run_time: str | None) -> None:
    """The first step of a removal: keep the time, drop the record. Killed after this and before Windows
    removed the task, the next look finds an enabled task and records "on" again: what Windows has."""
    kept = run_time or read_run_time(paths, include_disabled=True)
    if kept:
        remember_run_time(paths, kept)
    clear_run_time(paths)


def _follow(paths: Paths, log: NsLogger) -> None:
    """After a change Windows refused: put the record back to what Windows has. A read that fails too leaves
    it to the next `sync_record`."""
    with contextlib.suppress(ScheduleError):
        _record(paths, read_task(), log, by_owner=True)


class _Unread:
    """The task was not read before the change (see `_switch_on`)."""


_UNREAD = _Unread()


def _one_line(why: object, limit: int = 300) -> str:
    return " ".join(str(why).split())[:limit]


def _note_unrecorded(paths: Paths, log: NsLogger, run_time: str | None) -> None:
    """NS-W208 for the owner who is not at the screen: a WARN in the log and one morning note (best effort:
    the note's own folder may be the one that cannot be written). A note that still waits is not repeated."""
    try:
        d = describe("NS-W208")
        text = f"NS-W208: {d.message} Windows runs it at {run_time}. {d.hint}"
    except (KeyError, OSError, ValueError):
        text = f"NS-W208: Windows runs the nightly task at {run_time}, but Nightshift could not record it."
    log.warn("schedule_unrecorded", text, code="NS-W208")
    try:
        notify.leave_note(paths, "Nightshift", text, once=_waits("NS-W208:"))
    except OSError as e:
        log.warn("morning_note_failed", str(e))


def _undo(
    paths: Paths, app_path: Path, before: TaskState | None | _Unread, at: str, log: NsLogger, why: OSError
) -> NoReturn:
    """The record could not be written after Windows took a registration: put Windows back, then say which
    of the two ends it is. Always raises.

    Back means the task read before the change: none (Unregister), enabled (registered again at its old
    time; Windows is asked for the unattended mode first, as always) or disabled (registered disabled).
    Then Windows is read once more and compared with the RECORD, which is what every page shows:

    - they agree (no enabled task and no record; or an enabled task at the recorded time): `ScheduleUndone`;
    - they do not (Windows refused the way back, the task could not be read before or after, or an enabled
      task was there before that the record never held): `ScheduleUnrecorded`, NS-W208, with one morning
      note. Nothing more is tried: the next look at Windows records it as soon as `config` can be written.
    """
    reason = _one_line(why)
    after: TaskState | None = None
    known = False
    try:
        if isinstance(before, _Unread):
            raise ScheduleError("the task was not read before the change")
        if before is None or (before.enabled and task_owner(paths, before) == GONE):
            _run(build_remove_script())     # nothing to give back: no task, or one whose Nightshift is gone
        else:
            old = before.run_time or read_run_time(paths, include_disabled=True) or at
            theirs = _elsewhere(paths, before)      # another install's task goes back to that install
            if theirs and task_owner(paths, before) == UNREACHABLE:
                # its studio.exe cannot be looked up now: registering a guess would not be giving it back
                raise ScheduleError("the Nightshift the task ran cannot be reached, so it was not given back")
            owner = Paths(Path(theirs)) if theirs else paths
            _run(build_install_script(owner, old, app_path, disabled=not before.enabled))
        after, known = read_task(), True
    except ScheduleError as e:
        log.warn("schedule_undo_failed", _one_line(e))
    runs = on_here(paths, after)
    runs_at = after.run_time if runs and after is not None else None
    recorded = record_says_on(paths)
    agree = known and ((recorded and read_run_time(paths) == runs_at) if runs else not recorded)
    if agree:
        still = f"on at {runs_at}" if runs else "off"
        log.warn("schedule_change_undone", f"{schedule_file(paths)}: {reason}; still {still}")
        raise ScheduleUndone(
            f"Nightshift could not write config/schedule.json ({reason}), so the change was undone: "
            f"Automatic runs are still {still}. Make the folder config writable (an antivirus or backup "
            "program, a full disk, or its permissions), then try again",
            on=runs, run_time=runs_at, why=reason,
        )
    when = runs_at or at
    _note_unrecorded(paths, log, when)
    raise ScheduleUnrecorded(
        f"Windows runs the nightly task at {when}, but Nightshift could not write config/schedule.json "
        f"({reason}) and could not undo the change",
        run_time=when, why=reason,
    )


def _switch_on(
    paths: Paths, when: str, app_path: Path, log: NsLogger, before: TaskState | None | _Unread = _UNREAD
) -> TaskState:
    """Register the task enabled at `when`, then read it back and record what Windows has.

    `before` is the task as it was read ahead of the change (None: there was none). `ScheduleError` when
    Windows refuses both logon types (the task it had, if any, is as it was) or when no enabled task is there
    afterwards. If only the read-back fails, or it leaves out the logon type or the trigger time, Register's
    own answer (`registered:s4u` or `registered:interactive`, at `when`) stands in for that part. When the
    record cannot be written the registration is undone (`_undo`): `ScheduleUndone`, or `ScheduleUnrecorded`
    (NS-W208) when Windows could not be put back.
    """
    out = _run(build_install_script(paths, when, app_path))
    answered = "registered:interactive" in out
    try:
        task = read_task()
    except ScheduleError:
        task = TaskState(enabled=True, run_time=when, interactive=answered)
    if task is not None and (task.interactive is None or task.run_time is None):
        signed_in_only = answered if task.interactive is None else task.interactive
        task = TaskState(task.enabled, task.run_time or when, signed_in_only)
    try:
        recorded = _record(paths, task, log, by_owner=True)
    except OSError as e:
        _undo(paths, app_path, before, (task.run_time if task is not None else None) or when, log, e)
    if recorded == "none" or task is None:
        raise ScheduleError(f"Windows accepted the task '{TASK_NAME}' but it is not there or not enabled now")
    return task


def _logger(paths: Paths, log: NsLogger | None) -> NsLogger:
    return log or NsLogger("schedule", paths.logs)


def install(
    paths: Paths,
    run_time: str | None,
    app_path: Path,
    *,
    disabled: bool = False,
    keep_state: bool = False,
    enable: bool = False,
    log: NsLogger | None = None,
) -> str:
    """`ns schedule install` and setup: register the task at `run_time` and record what Windows has.

    The time is `run_time`, else the task's own, the recorded one, the one kept in `dashboard.json`, 03:00.
    A dev install (`runtime.json` says `dev_mode`) or `disabled` registers the task switched off in the one
    call and records mode `disabled`. With `keep_state`, a task the owner disabled in Task Scheduler is left
    exactly as it is (read as off: no record; `run_time` is kept for later) unless `enable` (`--enable`) says
    to switch it on; a disabled task recorded as `disabled` is a dev install's, not a choice, and is
    registered enabled (an install for real over a dev one). No task at all is registered enabled. If the
    task cannot be read nothing is changed (`ScheduleError`). The message says when it runs only while
    signed in.

    A disabled registration reads the task first in every case, and leaves one that runs another Nightshift
    folder (one that exists, or whose drive is not there now) exactly as it is, enabled or not: the
    message carries `DEV_LEFT` and that folder. A task of this folder, of a deleted one, or one that does
    not say what it runs is registered disabled as before.
    """
    if run_time and not valid_time(run_time):
        raise ScheduleError(f"time must be HH:MM (24 h), got {run_time!r}")
    log = _logger(paths, log)
    with _locked(paths):
        off = disabled or is_dev(paths)
        # a disabled registration always reads first: it must never land on another install's task
        before = read_task() if keep_state or off else None
        when = (
            run_time or (before.run_time if before is not None and keep_state else None)
            or read_run_time(paths, include_disabled=True) or remembered_run_time(paths) or DEFAULT_TIME
        )
        if off:                                     # a dev install never gets an enabled task, whoever asks
            if before is not None and before.home and task_owner(paths, before) in (OTHER, UNREACHABLE):
                # The PC's one task runs another Nightshift folder (enabled or not): registering it disabled
                # for this one would switch the other install's nightly run off. Left, whatever was asked.
                log.warn("schedule_dev_left", f"'{TASK_NAME}' runs the Nightshift at {before.home}; left")
                return (
                    f"not changed ({TASK_NAME}: {DEV_LEFT}{before.home}; a dev install never takes another "
                    "install's nightly task)"
                )
            _run(build_install_script(paths, when, app_path, disabled=True))
            write_run_time(paths, when, MODE_DISABLED)
            return f"registered ({TASK_NAME} daily at {when}; {DISABLED_NOTE})"
        hand_off = before is not None and not before.enabled and read_schedule_mode(paths) != MODE_DISABLED
        if hand_off and not enable:
            _record(paths, before, log)
            if run_time:
                remember_run_time(paths, run_time)
            return f"not changed ({TASK_NAME}: {KEPT_OFF_NOTE})"
        task = _switch_on(paths, when, app_path, log, before if keep_state else _UNREAD)
        if keep_state:
            _log_kept(log, before, task.run_time or when)
        note = f"; {INTERACTIVE_NOTE}" if task.interactive else ""
        return f"registered ({TASK_NAME} daily at {task.run_time or when}{note})"


def _log_kept(log: NsLogger, before: TaskState | None, when: str) -> None:
    """One log line saying what a registration replaced (or that there was no task)."""
    if before is None:
        log.info(
            "schedule_state_no_task", f"no '{TASK_NAME}' task existed; registered it enabled at {when}",
            after_enabled=True, after_time=when,
        )
        return
    was = "enabled" if before.enabled else "disabled"
    log.info(
        "schedule_state_kept",
        f"'{TASK_NAME}' was {was} at {before.run_time}; registered it enabled at {when}",
        before_enabled=before.enabled, before_time=before.run_time, after_enabled=True, after_time=when,
    )


@dataclass(frozen=True)
class Outcome:
    """What Windows has after `set_automatic`: `mode` is `registered`, `interactive_only`, `none` (off) or
    `disabled` (a dev install); `changed` is False when no task was registered or removed."""

    on: bool
    mode: str
    run_time: str | None
    changed: bool
    took_from: str | None = None    # the other Nightshift folder whose task this change moved here


def set_automatic(
    paths: Paths, app_path: Path, *, on: bool | None = None, run_time: str | None = None,
    log: NsLogger | None = None,
) -> Outcome:
    """The Settings switch and its time: make Windows match, then answer with what Windows has.

    `on` None keeps the state and only moves the time. Off, or already off: no task is touched and `run_time`
    goes to `dashboard.json`; a switch-off removes the task (record first, see `_forget`). On: the task is
    registered at `run_time` (else the task's, the kept one, 03:00); same state and time runs no script,
    unless the task runs only while signed in and `on` is asked again (then Windows is asked again).
    `ScheduleError` with nothing changed when Windows refuses, the task cannot be read, another change is
    still running (`ScheduleBusy`) or the lock file cannot be opened; `ScheduleUndone` when the record could
    not be written and Windows was put back, `ScheduleUnrecorded` (NS-W208) when it could not be put back;
    `DevInstall` for the switch in a dev install.
    """
    if run_time and not valid_time(run_time):
        raise ScheduleError(f"time must be HH:MM (24 h), got {run_time!r}")
    log = _logger(paths, log)
    if is_dev(paths):
        if on:
            raise DevInstall("this is a dev install: its nightly task is registered disabled and never runs")
        if run_time:
            with _own_files():
                remember_run_time(paths, run_time)
        return Outcome(False, MODE_DISABLED, run_time or remembered_run_time(paths), False)
    with _locked(paths):
        return _set_locked(paths, app_path, read_task(), on, run_time, log)


def _set_locked(
    paths: Paths, app_path: Path, before: TaskState | None, on: bool | None, run_time: str | None,
    log: NsLogger, *, quiet: bool = False,
) -> Outcome:
    """`set_automatic` once the lock is held and the task was read (`before`). `quiet` is setup's use: a
    record Windows no longer backs goes without the NS-W207 note."""
    is_on = on_here(paths, before)      # another install's task is not "on" here, and is never removed
    if not (is_on if on is None else on):
        if before is not None and is_on:
            _forget(paths, run_time or before.run_time)
            try:
                _run(build_remove_script())
            except ScheduleError:
                _follow(paths, log)
                raise
            if _record(paths, read_task(), log, by_owner=True) != "none":
                raise ScheduleError(f"Windows did not remove the task '{TASK_NAME}'")
        else:
            _record(paths, before, log, by_owner=quiet)
            if run_time:
                remember_run_time(paths, run_time)
        return Outcome(False, "none", run_time or remembered_run_time(paths), is_on)
    when = (
        run_time or (before.run_time if before is not None and is_on else None)
        or read_run_time(paths, include_disabled=True) or remembered_run_time(paths) or DEFAULT_TIME
    )
    # Same state and time: no script. The one exception is the owner asking again (the switch or the
    # NS-W205 row's "Try again") while Windows runs the task only while signed in: registering again
    # tries the unattended mode first, and what Windows then has is the answer either way.
    again = on is True and before is not None and before.interactive is True
    if before is not None and is_on and before.run_time == when and not again:
        return Outcome(True, _record(paths, before, log, by_owner=quiet), when, False)
    theirs = _elsewhere(paths, before)
    task = _switch_on(paths, when, app_path, log, before)
    if theirs:
        log.warn("schedule_task_moved", f"'{TASK_NAME}' ran the Nightshift at {theirs}; it now runs this one")
    return Outcome(True, read_schedule_mode(paths), task.run_time or when, True, took_from=theirs)


@dataclass(frozen=True)
class SetupResult:
    """What setup's scheduler step did and what Windows has now (`setup_apply`); `ns schedule install
    --setup` prints it as one JSON line and `installer.stage2.web.nightly_view.outcome_line` words it.

    `case`: `first` (a first install: the answer, or off), `applied` (the window's answer, made against the
    state Windows still had), `kept` (left as Windows has it), `stale` (a window answer made against another
    state: nothing changed), `unread` (Windows or the lock did not answer: nothing changed), `refused`
    (Windows refused the change: as it was), `undone` and `unrecorded` (the record could not be written).
    `on` is None only for `unread`. `run_time` is the task's time when on, else the time kept for later.
    """

    case: str
    on: bool | None
    run_time: str | None
    signed_in_only: bool = False
    wanted_on: bool = False     # setup tried to switch them on (with `refused` or `unread`: NS-W206)
    wanted_off: bool = False    # setup tried to switch them off and Windows refused
    time_kept: str | None = None  # the time the answers name, which was not used
    seen: str | None = None
    reason: str = ""
    removed_old: bool = False       # a first install without a yes removed a task left by an earlier install
    other_home: str | None = None   # off here: the PC's task runs the Nightshift in this other folder (kept)
    took_from: str | None = None    # on here: the task ran the Nightshift in this other folder until now
    owner_unknown: bool = False     # the task found does not say which Nightshift it runs
    unreachable: bool = False       # `other_home` is on a drive that is not there now (the task was left)
    #: a Repair removed the task of a Nightshift whose folder was deleted (case `refused`: could not)
    gone_home: str | None = None


def setup_apply(
    paths: Paths, app_path: Path, *, first: bool, nightly: bool | None, seen: str | None, run_time: str,
    was: str | None = None, log: NsLogger | None = None,
) -> SetupResult:
    """Setup's one decision about Automatic runs (s09; a Repair and a reinstall are the same step again).

    The rule: Automatic runs change only by an owner's action made against the state the owner saw.

    - `first` (no install decided here before): `nightly` True switches them on at `run_time`; anything else
      registers nothing and keeps `run_time` for the switch. An enabled task that is there anyway is nobody's
      choice in this install. Without a yes: one that runs this folder, or a folder that no longer exists (the
      Nightshift was deleted by hand), is removed and its time kept; one that runs ANOTHER Nightshift folder
      is left exactly as it is (off here, `other_home` names it); one that does not say what it runs is left
      as it is and read on, as it always was (`owner_unknown`). With a yes the task is registered for this
      install, and `took_from` / `owner_unknown` say whose it was.
    - otherwise the window's answer (`nightly` with `seen`, the state the window showed) is applied only when
      Windows still has that state; an answer without `seen` (an answers file, which may be months old) and
      an answer against another state change nothing. A kept enabled task is registered again at its own
      time, so its action and settings are this version's.
    - the answers' time (`run_time`) moves a kept task, or the time kept while off, only when it differs
      from the time the answers had at the last setup (`was`) and the time now still is that one: a time set
      in Settings since stays (D-14). With the window's answer applied, a time changed in the window counts.

    Never raises for what Windows or the files do: the result says it (`SetupResult`). `ScheduleError` only
    for a bad `run_time`; `DevInstall` in a dev install (its task is registered disabled by `install`).
    """
    if not valid_time(run_time):
        raise ScheduleError(f"time must be HH:MM (24 h), got {run_time!r}")
    if is_dev(paths):
        raise DevInstall("this is a dev install: its nightly task is registered disabled and never runs")
    log = _logger(paths, log)
    try:
        with _locked(paths):
            return _setup_locked(paths, app_path, read_task(), first, nightly, seen, run_time, was, log)
    except ScheduleUndone as e:
        return SetupResult("undone", e.on, e.run_time, reason=e.why, seen=seen)
    except ScheduleUnrecorded as e:
        return SetupResult("unrecorded", True, e.run_time, reason=e.why, seen=seen)
    except ScheduleError as e:
        own = isinstance(e, (ScheduleFileError, ScheduleBusy))  # Nightshift's lock or files, not Windows
        reason = _one_line(e) if own else f"Windows did not answer: {_one_line(e)}"
        if first:
            with contextlib.suppress(OSError):
                remember_run_time(paths, run_time)
        wanted = first and nightly is True
        return SetupResult("unread", None, None, wanted_on=wanted, reason=reason, seen=seen)


def _setup_locked(
    paths: Paths, app_path: Path, before: TaskState | None, first: bool, nightly: bool | None,
    seen: str | None, run_time: str, was: str | None, log: NsLogger,
) -> SetupResult:
    there = before is not None and before.enabled           # some enabled task is in Windows
    owner = task_owner(paths, before) if there and before is not None else None
    on_now = on_here(paths, before)                          # ... and it is this install's
    theirs = _elsewhere(paths, before)
    unknown = owner == UNKNOWN
    now_time = (before.run_time or read_run_time(paths)) if on_now and before is not None else None
    only = bool(before.interactive) if on_now and before is not None else False
    answered = nightly is not None and seen is not None
    same = seen == f"on@{now_time}" if on_now else seen in ("off", "new")
    new_time = was is not None and run_time != was
    time_kept: str | None = None
    leftover = first and not nightly and owner in (MINE, GONE)   # an earlier install's: nobody's choice
    away = owner == UNREACHABLE
    if first and (nightly or not on_now or leftover):
        case, want_on, when = "first", bool(nightly), run_time
    elif not first and answered and same:
        case, want_on = "applied", bool(nightly)
        when = run_time if (new_time or not on_now) else (now_time or run_time)
    else:
        case, want_on = ("stale" if answered and not first else "kept"), on_now
        kept = remembered_run_time(paths) or read_run_time(paths, include_disabled=True)
        base = now_time if on_now else kept
        moves = case == "kept" and new_time and base in (was, None)
        when = run_time if moves or base is None else base
        time_kept = run_time if new_time and not moves and base != run_time else None
    # A Repair over an enabled task whose Nightshift folder was deleted: it starts nothing and wakes the PC
    # every night. Removed and said, unless this run switches Automatic runs on here (that replaces it) or
    # the window's answer was made against another state (then setup changes nothing).
    gone_task = not first and owner == GONE and not want_on and case != "stale"
    try:
        if (leftover or gone_task) and before is not None:
            # first install, no yes, and the task of an earlier install of this folder (or of a deleted one)
            keep = before.run_time or run_time      # its time waits for the Settings switch
            if gone_task:                           # (a time this install already keeps for the switch wins)
                keep = remembered_run_time(paths) or keep
            with contextlib.suppress(OSError):
                _forget(paths, keep)
            _run(build_remove_script())
            left = read_task()
            if left is not None and left.enabled:
                raise ScheduleError(f"Windows did not remove the task '{TASK_NAME}'")
            with contextlib.suppress(OSError):
                _record(paths, left, log, by_owner=True)
            log.warn("schedule_leftover_removed", f"'{TASK_NAME}' of an earlier install: {before.home}")
            if gone_task:
                return SetupResult(case, False, keep, seen=seen, gone_home=before.home)
            return SetupResult(case, False, keep, removed_old=True, seen=seen)
        if case == "kept" and on_now and not (first and unknown):
            task = _switch_on(paths, when, app_path, log, before)
            return SetupResult(
                case, True, task.run_time or when, bool(task.interactive), time_kept=time_kept, seen=seen
            )
        if case == "stale" or not (want_on or on_now) or (case == "kept" and on_now):
            # nothing to change in Windows: the record follows what was read, quietly (a record an uninstall
            # or a dev install left goes), and the time waits in dashboard.json for the switch. Also a first
            # install without a yes over a task that does not say whose it is: left exactly as it is.
            with contextlib.suppress(OSError):
                _record(paths, before, log, by_owner=True)
            if not on_now and when != remembered_run_time(paths):
                with contextlib.suppress(OSError):
                    remember_run_time(paths, when)
            at = now_time if on_now else when
            return SetupResult(
                case, on_now, at, only, time_kept=time_kept, seen=seen, other_home=theirs,
                owner_unknown=on_now and unknown and first, unreachable=away,
            )
        if want_on and on_now and unknown:
            # a yes over a task that does not say what it runs: registered for this install, whatever it ran
            task = _switch_on(paths, when, app_path, log, before)
            return SetupResult(
                case, True, task.run_time or when, bool(task.interactive), seen=seen, owner_unknown=True
            )
        out = _set_locked(paths, app_path, before, want_on, when, log, quiet=True)
        return SetupResult(
            case, out.on, out.run_time, out.mode == "interactive_only", time_kept=time_kept, seen=seen,
            took_from=out.took_from, other_home=None if out.on else theirs, unreachable=away and not out.on,
        )
    except ScheduleFileError:
        raise
    except ScheduleError as e:  # Windows refused: the task is as it was
        if gone_task and before is not None:
            return SetupResult(
                "refused", False, remembered_run_time(paths), reason=_one_line(e), seen=seen,
                gone_home=before.home,
            )
        if not on_now:
            with contextlib.suppress(OSError):
                remember_run_time(paths, run_time)
        still = on_now or leftover          # an enabled task is still there
        at = now_time if on_now else (before.run_time if leftover and before is not None else run_time)
        return SetupResult(
            "refused", still, at, only, wanted_on=want_on and not on_now, wanted_off=still and not want_on,
            reason=_one_line(e), seen=seen, other_home=None if want_on else theirs,
            unreachable=away and not want_on,
        )


def reregister(paths: Paths, app_path: Path, *, log: NsLogger | None = None) -> str | None:
    """What an update does to the task: nothing to the owner's choice (D-28). None: nothing to do or say.

    No record (off) and a dev install's `disabled` record: Windows is not even asked. A record that says on:
    the task is read; enabled, it is registered again at its own time; disabled or deleted in Task Scheduler,
    it is left exactly as it is and the record goes (off, NS-W207). `ScheduleError` when it cannot be read,
    Windows refuses the registration or the lock file cannot be opened: the task is as it was, and the update
    that called this stands (`cli._reregister` turns it into a note).
    """
    log = _logger(paths, log)
    if is_dev(paths) or read_schedule_mode(paths) in ("none", MODE_DISABLED):
        return None
    with _locked(paths):
        before = read_task()
        if before is None or not on_here(paths, before):
            _record(paths, before, log)
            return (
                "Automatic runs are off (the nightly task was switched off or removed in Task Scheduler); "
                "left as it is"
            )
        when = before.run_time or read_run_time(paths) or DEFAULT_TIME
        task = _switch_on(paths, when, app_path, log, before)
        _log_kept(log, before, task.run_time or "")
        return f"scheduled task re-registered (daily at {task.run_time})"


def refresh_action(paths: Paths, app_path: Path, *, log: NsLogger | None = None) -> bool:
    """Bring the action of THIS install's enabled task to `action_arguments` (the first start of a version
    that an older updater installed: `ns_supervisor.launch_reach`). True when the task was registered again.

    Nothing is touched, and False, for: no task, a disabled one (the owner's or a dev install's), one that
    runs another Nightshift folder or does not say which, and one whose action is this one already.

    Only the action is set (`build_action_script`), under the schedule lock; the task is not registered
    again, so its time, its logon type and its settings are not this code's to state. Then the task is read
    back and compared with what was read before: still enabled, the same time, the same logon type, the
    new action. `ScheduleError` when the task cannot be read, Windows refuses (the task is as it was), or
    the task read back differs in anything but the action (the message names what; the record follows
    what Windows has).
    """
    if is_dev(paths):
        return False
    log = _logger(paths, log)
    with _locked(paths):
        before = read_task()
        if before is None or not before.enabled or before.stub or task_owner(paths, before) != MINE:
            return False
        _run(build_action_script(paths))
        after = read_task()
        if after is None or not after.stub:
            raise ScheduleError(f"Windows took the new action of '{TASK_NAME}'; the old one is still there")
        changed = [
            f"{what} was {was} and is {now}"
            for what, was, now in (
                ("the time", before.run_time, after.run_time),
                ("the logon type", before.logon, after.logon),
                ("switched on", before.enabled, after.enabled),
            )
            if was != now
        ]
        if changed:
            _record(paths, after, log)
            raise ScheduleError(
                f"'{TASK_NAME}' has the new action, but more than its action changed: {'; '.join(changed)}"
            )
        log.info("schedule_action_refreshed", f"'{TASK_NAME}' now starts through the launch script")
        return True


def sync_record(
    paths: Paths, *, log: NsLogger | None = None, quiet: bool = False,
    read: Callable[[], TaskState | None] | None = None,
) -> bool:
    """One look at Windows; the record follows it. True when the record changed.

    For every install but a dev one (its `disabled` record is not a choice). A task deleted or disabled in
    Task Scheduler reads off (NS-W207 once); an enabled task with no record, a torn one or an older
    `switched_off` one reads on; a moved time is taken over. Never raises and never waits for another change:
    a look that fails, times out (READ_TIMEOUT_S) or finds the lock taken changes nothing.

    `quiet` is setup's own look before it asks its question: a record an uninstall left behind goes without
    the NS-W207 note (nobody switched anything off outside Nightshift; setup's summary says what it found).
    `read` replaces the look itself (setup runs PowerShell through its own runner, before the programs that
    hold `ns` exist); it raises `ScheduleError` when Windows did not answer.
    """
    if is_dev(paths) or read_schedule_mode(paths) == MODE_DISABLED:
        return False
    f = schedule_file(paths)

    def seen() -> tuple[bool, str, str | None]:
        return f.exists(), read_schedule_mode(paths), read_run_time(paths, include_disabled=True)

    try:
        with _locked(paths, 0):
            before = seen()
            task = read() if read is not None else read_task(timeout_s=READ_TIMEOUT_S)
            _record(paths, task, _logger(paths, log), by_owner=quiet)
            return seen() != before
    except (ScheduleError, OSError):
        return False


def remove(paths: Paths) -> str:
    """`ns schedule remove`: unregister the task (also one disabled by hand), keeping its time for later."""
    with _locked(paths):
        try:
            before = read_task()
        except ScheduleError:
            before = None
        _forget(paths, before.run_time if before is not None else None)
        try:
            out = _run(build_remove_script())
        except ScheduleError:
            _follow(paths, _logger(paths, None))
            raise
        return out or "removed"


def show(paths: Paths) -> str:
    """Human-readable task status."""
    saved = read_run_time(paths)
    out = _run(build_show_script())
    return out + (f"\nconfigured time: {saved}" if saved else "")
