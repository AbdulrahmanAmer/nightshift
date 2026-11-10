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

D-39: no command waits for the worker from inside a venv. A console command started as
`venvs\\supervisor\\Scripts\\ns.exe` runs as `python.exe <that file>`, and the first pydantic model makes
`importlib.metadata` open every `sys.path` entry that is a file, `ns.exe` included, and keep it open without
delete sharing: uv cannot remove it (os error 32) while that command lives. So every route starts the worker
with `--wait-pid <the caller>` and returns; a terminal gets a progress window that is a plain PowerShell
(`follower_script`), which runs nothing from the program or the venvs. One worker at a time holds
`run\\update.lock` (`update_lock`), and only that worker writes the state file. Every `running` state carries
`if_stopped_here`: the sentence that is true if the worker dies before its next write (`effective_status`).
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

import psutil

from ns_common import testguard
from ns_common.atomic import REPLACE_WAIT_S, read_text_retry, write_text_atomic
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_common.singleton import AlreadyRunning, FileLock
from ns_supervisor import pwsh, updater_copy
from ns_supervisor.childenv import child_env
from ns_supervisor.procs import UNDER_ENGINE_ENV

CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_NEW_CONSOLE = 0x00000010
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
DETACH_FLAGS = CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS

#: After the caller exits, wait this long so its answer reaches the browser before the engine is stopped.
PARENT_GRACE_S = 2.0
#: The longest the worker waits for the command that started it to end; after that it refuses (NS-E706):
#: that command runs from `venvs\supervisor` and holds files the re-sync has to replace.
PARENT_WAIT_MAX_S = 120.0

#: Exit code of a worker that found another one holding `run\update.lock`; it wrote only its own log.
LOCKED_EXIT = 5
#: `busy` takes the lock for an instant to probe it, so a worker tries for LOCK_TRIES * LOCK_PAUSE_S (2 s).
LOCK_TRIES = 10
LOCK_PAUSE_S = 0.2
BUSY = "Another update, rollback or repair of this Nightshift is running"
WINDOW_TITLE = "Nightshift update"
#: The longest a progress window waits for Enter after the result (then it closes by itself).
CLOSE_AFTER_S = 1800
#: The morning note's title when a run ends with Nightshift not running (`_tell`): it says whether the
#: program was changed, because "did not complete" is untrue of an update that applied and only the restart
#: failed (D-39a review note).
NOT_COMPLETE_TITLE = "The last update or rollback did not complete"
APPLIED_TITLE = "The update or rollback was applied, but Nightshift did not start again"
NOT_RESTARTED_TITLE = "Nothing was updated, and Nightshift did not start again"
NOT_STARTED = "Nightshift was not started."
OPEN_IT = "Open Nightshift from a Desktop shortcut."
NO_RESTART = "The update process stopped before it restarted Nightshift."

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


def write_status(
    paths: Paths, state: str, message: str = "", *, wait_s: float = REPLACE_WAIT_S, **extra: Any
) -> None:
    """Replace the state file atomically: `{state, old, new, message, updated_at}` plus log/pid extras.

    Goes through `write_text_atomic`, which retries `os.replace` while Windows refuses it because the
    dashboard has the file open for its 3-second poll, for at most `wait_s` (then `NS-E119`).
    """
    data = {
        "state": state, "old": "", "new": "", "message": message,
        "updated_at": datetime.now().isoformat(timespec="seconds"), **extra,
    }
    write_text_atomic(status_file(paths), json.dumps(data), wait_s=wait_s)


#: The longest one progress line or one try at the final line waits for a program that holds the state file.
#: A holder that keeps it for the whole update therefore costs the updater PROGRESS_WAIT_S per progress line
#: (three of the worker's own and at most four steps of an update or rollback, `step`) and, at the end, at
#: most FINAL_TRIES * FINAL_WAIT_S + (FINAL_TRIES - 1) * FINAL_PAUSE_S (about 13 s).
PROGRESS_WAIT_S = 1.0
FINAL_WAIT_S = 1.0


def _progress(paths: Paths, state: str, message: str, **extra: Any) -> None:
    """A progress line in the state file. Failing to write it must not stop the update, nor stall it long."""
    try:
        write_status(paths, state, message, wait_s=PROGRESS_WAIT_S, **extra)
    except OSError as e:
        sys.stderr.write(f"could not write {status_file(paths).name}: {e}\n")


FINAL_TRIES = 10
FINAL_PAUSE_S = 0.3


def _final(
    paths: Paths, state: str, message: str, *, sleep: Callable[[float], None] = time.sleep, **extra: Any
) -> None:
    """The `done` / `failed` line: retried for a few seconds, then given up on (readers notice a dead pid)."""
    for attempt in range(FINAL_TRIES):
        try:
            write_status(paths, state, message, wait_s=FINAL_WAIT_S, **extra)
            return
        except OSError as e:
            if attempt == FINAL_TRIES - 1:
                sys.stderr.write(f"could not write {status_file(paths).name}: {e}\n")
                return
            sleep(FINAL_PAUSE_S)


def read_status(paths: Paths) -> dict[str, Any]:
    """The status file as a dict (empty when absent or unreadable)."""
    try:
        data = json.loads(read_text_retry(status_file(paths)))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def step(paths: Paths, message: str, if_stopped_here: str) -> None:
    """A progress line from inside the update (`ns_supervisor.update`): what it does now, and the sentence
    that is true if this process dies before its next line. Written only by the worker that owns the state
    file (its pid is in it); anything else that runs the same code (a test, a repair) writes nothing."""
    raw = read_status(paths)
    if raw.get("state") != "running" or raw.get("pid") != os.getpid():
        return
    keep = {k: raw[k] for k in ("log", "pid", "old") if k in raw}
    _progress(paths, "running", message, if_stopped_here=if_stopped_here, **keep)


def unchanged_sentence(old_version: str) -> str:
    """`if_stopped_here` of every step before the first change on disk."""
    return (
        f"The update process stopped before it changed anything. {old_version or 'The version you had'} "
        "is in place."
    )


# ------------------------------------------------------------------------------------- one at a time
def update_lock(paths: Paths) -> FileLock:
    """`run\\update.lock`: held by the one worker that may change the program, the settings copy and the
    venvs (the operating system drops it when that process ends, however it ends)."""
    return FileLock(paths.home / "run" / "update.lock")


def _started(pid: int | None) -> str:
    if pid is None:
        return "its process number is not known"
    try:
        at = datetime.fromtimestamp(psutil.Process(pid).create_time()).strftime("%H:%M:%S")
    except (psutil.Error, OSError, ValueError):
        return f"process {pid}"
    return f"process {pid}, started {at}"


def busy(paths: Paths) -> str | None:
    """None when no update, rollback or repair holds the lock; else who holds it and where its progress is
    (the detail of NS-E705). The lock is taken and dropped at once: nothing is written but the lock file."""
    lock = update_lock(paths)
    try:
        lock.acquire()
    except AlreadyRunning as e:
        raw = read_status(paths)
        log = raw.get("log") if raw.get("state") == "running" else None
        return f"{_started(e.owner)}. Nothing was changed. Its progress: {log or status_file(paths)}"
    except OSError:
        return None             # the lock cannot be probed from here: the worker decides (exit 5)
    lock.release()
    return None


#: A `running` state without a worker pid (written by the `launch` of 0.1.15-rc3 and older) older than this:
#: the worker never started.
START_GRACE_S = 120.0
STOPPED_MESSAGE = "The update process stopped before it finished."


def where(paths: Paths) -> str:
    """What is in place now, read from the disk (`update.where_now`); empty when that cannot be told."""
    try:
        from ns_supervisor import control, update

        return update.where_now(paths, control.app_path(paths))
    except Exception:  # a reader of the state file must get an answer
        return ""


def worker_alive(pid: int) -> bool:
    """True while `pid` is a live Python process of ours (a reused pid of another program counts as gone)."""
    try:
        proc = psutil.Process(pid)
        if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
            return False
        cmd = " ".join(proc.cmdline())
    except psutil.NoSuchProcess:
        return False
    except psutil.Error:
        return True                  # access denied and the like: do not call a live worker dead
    return not cmd or "ns_supervisor" in cmd or "python" in cmd.lower()


def effective_status(
    raw: dict[str, Any],
    *,
    alive: Callable[[int], bool] | None = None,
    now: datetime | None = None,
    describe: Callable[[], str] | None = None,
) -> dict[str, Any]:
    """The state a reader should believe: a `running` state whose worker is gone is `failed`.

    A worker killed hard (power loss, Task Manager) leaves `running` behind for ever, and the next dashboard
    start used to show "Installing the update" indefinitely. With a recorded pid the process must exist; a
    state an older release's `launch` wrote before the worker started (no pid) is believed for
    `START_GRACE_S`.

    The message is the worker's own `if_stopped_here`: what is true of the disk when it died in the step
    it had announced. A state without one (written by 0.1.15-rc3 or older) gets `describe()`, which reads
    the disk (`where`). It never says a version "is still in place" on its own authority.
    """
    if raw.get("state") != "running":
        return raw
    pid = raw.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        gone = not (alive or worker_alive)(pid)
    else:
        try:
            wrote = datetime.fromisoformat(str(raw.get("updated_at")))
        except ValueError:
            return raw                # nothing to judge by
        gone = ((now or datetime.now()) - wrote).total_seconds() > START_GRACE_S
    if not gone:
        return raw
    left = raw.get("if_stopped_here")
    if not (isinstance(left, str) and left.strip()):
        left = " ".join(s for s in (STOPPED_MESSAGE, describe() if describe else "") if s)
    return {**raw, "state": "failed", "exit_code": 1, "message": left}


def read_status_checked(paths: Paths, **kw: Any) -> dict[str, Any]:
    """`read_status` with `effective_status` applied (what the dashboard shows)."""
    kw.setdefault("describe", lambda: where(paths))
    return effective_status(read_status(paths), **kw)


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
        "env": child_env(),  # D-3: the update worker never inherits NS_DASHBOARD_NO_HOST
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
        raise NsError("NS-E702", f"could not start the update process: {e}. Nothing was changed.") from e


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
    """Wait until the process that started this worker has exited, then a short grace period.

    `NS-E706` when it has not ended after `max_s`: that command runs from `venvs\\supervisor` and holds
    `Scripts\\ns.exe` open, so the update would fail in the middle (D-39). Nothing has changed by then."""
    alive = alive or pid_alive
    waited = 0.0
    while alive(pid):
        if waited >= max_s:
            raise NsError(
                "NS-E706",
                f"the command that started this update (process {pid}) did not end within {max_s:g} s. "
                "Nothing was changed. Close the window it runs in, then run the update again.",
            )
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
    stage: Callable[[Paths], Path | None] | None = None,
) -> tuple[int, Path]:
    """Start the worker for `forwarded` arguments; returns (pid, log path).

    The worker runs from a private copy of the supervisor venv (`updater_copy`), so that no file in
    `venvs\\supervisor` is open while it re-installs into that venv. Without a venv, or when the copy fails,
    it runs with this process's interpreter as before (and then refuses at its holder check when that
    interpreter is a venv's). The state file is not written here: only the worker that holds
    `update_lock` writes it, so a second `ns update` cannot overwrite the state of the one that runs.
    """
    log = new_log_path(paths, now)
    log.parent.mkdir(parents=True, exist_ok=True)
    staged = (stage or updater_copy.stage_worker_python)(paths)
    argv = worker_argv(
        forwarded, log=log, home=paths.home, wait_pid=wait_pid, python=str(staged) if staged else None
    )
    cwd = paths.home if paths.home.is_dir() else Path(os.path.expanduser("~"))
    pid = (spawn or spawn_detached)(argv, cwd=cwd)
    return pid, log


# --------------------------------------------------------------------- waiting for the result (--wait)
def runs_from_venvs(paths: Paths) -> bool:
    """True when this process, or the chain that started it, runs from this install's Python environments
    (`ns.exe`, a venv's `python.exe`): it then holds files the re-sync replaces and must end before the
    worker goes on, so it cannot be the one that waits for the result."""
    root = os.path.normcase(os.path.abspath(paths.venvs)) + os.sep
    try:
        me = psutil.Process()
        chain = [me, *me.parents()[:3]]
    except psutil.Error:
        chain = []
    texts = [sys.executable, *sys.argv[:1]]
    for proc in chain:
        try:
            texts += [proc.exe(), *proc.cmdline()[:2]]
        except (psutil.Error, OSError):
            continue
    return any(t and os.path.normcase(os.path.abspath(t)).startswith(root) for t in texts)


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
    """`ns update --wait` from a program outside the venvs: echo the worker's log, then its result;
    returns 0 when it is `done`, else 1 (failed, died: the `if_stopped_here` it left, or still running
    after `max_s`). Only a state that names this run's log is believed."""
    alive = alive or pid_alive
    offset, waited, quiet = 0, 0.0, 0
    while True:
        offset = _echo_new(log, offset, echo)
        raw = read_status(paths)
        mine = raw.get("log") == str(log)
        if mine and raw.get("state") in DONE_STATES:
            _echo_new(log, offset, echo)
            echo("Result: " + str(raw.get("message", "")))
            return 0 if raw.get("state") == "done" else 1
        told = raw.get("pid")
        worker = told if mine and isinstance(told, int) else pid
        quiet = quiet + 1 if not alive(worker) else 0
        if quiet >= 3:                  # its last write can land a moment after the process is gone
            _echo_new(log, offset, echo)
            left = raw.get("if_stopped_here") if mine else None
            echo("Result: " + str(left or f"The update process ended without reporting a result. See {log}"))
            return 1
        if waited >= max_s:
            echo(f"The update is still running. Progress: {log}")
            return 1
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


WAIT_PREFIX = "update-wait-"


def write_wait_script(paths: Paths, pid: int, log: Path) -> Path | None:
    """`run\\update-wait-<stamp>.ps1`: the follower as a file, for a script that needs the result of an
    update it started with `--detach` from the install's own `ns` (which cannot wait itself):
    `powershell -NoProfile -ExecutionPolicy Bypass -File <it>` prints the log and the result and exits 0
    (done) or 1. Scripts of earlier runs are removed. None when it could not be written."""
    folder = paths.home / "run"
    target = folder / (WAIT_PREFIX + log.stem.replace("update-", "") + ".ps1")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for old in folder.glob(WAIT_PREFIX + "*.ps1"):
            old.unlink(missing_ok=True)
        write_text_atomic(target, follower_script(status_file(paths), log, pid, wait=False) + "\n")
    except OSError:
        return None
    return target


# ----------------------------------------------------------------- the progress window (a terminal's route)
def _ps(text: object) -> str:
    return "'" + str(text).replace("'", "''") + "'"


def follower_script(status: Path, log: Path, pid: int, *, wait: bool = True) -> str:
    """PowerShell 5.1 (pure ASCII) that shows the worker's log, then its result, and returns 0 or 1.

    It reads two files and asks whether a process is alive: nothing of it comes from the program folder or
    a venv, so it holds nothing an update replaces, and closing it stops nothing. It believes only a state
    that names this run's log (the state file still holds the last update's result until the worker's
    first write). A worker that died is described by the `if_stopped_here` it left. `wait`: keep the
    window open until Enter."""
    return "\n".join([
        "$ErrorActionPreference = 'Continue'",
        f"try {{ $Host.UI.RawUI.WindowTitle = {_ps(WINDOW_TITLE)} }} catch {{ }}",
        f"$statusFile = {_ps(status)}",
        f"$logFile = {_ps(log)}",
        f"$startPid = {int(pid)}",
        "$seen = 0; $code = -1; $quiet = 0; $ticks = 0",
        "function Show-NewLines {",
        "    if (-not (Test-Path -LiteralPath $logFile)) { return }",
        "    $lines = @(Get-Content -LiteralPath $logFile -Encoding UTF8 -ErrorAction SilentlyContinue)",
        "    for ($i = $script:seen; $i -lt $lines.Count; $i++) { Write-Host $lines[$i] }",
        "    if ($lines.Count -gt $script:seen) { $script:seen = $lines.Count }",
        "}",
        "Write-Host 'This window only shows the progress: closing it does not stop the update.'",
        "Write-Host ('Log: ' + $logFile)",
        "while ($code -lt 0) {",
        "    Show-NewLines",
        "    $state = $null",
        "    try { $state = Get-Content -LiteralPath $statusFile -Raw -ErrorAction Stop | ConvertFrom-Json }",
        "    catch { $state = $null }",
        "    $mine = $false",
        "    if ($null -ne $state -and $state.PSObject.Properties['log']) {",
        "        $mine = ([string]$state.log -eq $logFile)",
        "    }",
        "    $workerPid = $startPid",
        "    if ($mine -and $state.PSObject.Properties['pid'] -and [int]$state.pid -gt 0) {",
        "        $workerPid = [int]$state.pid",
        "    }",
        "    if ($mine -and ($state.state -eq 'done' -or $state.state -eq 'failed')) {",
        "        Show-NewLines",
        "        Write-Host ''",
        "        Write-Host ('Result: ' + [string]$state.message)",
        "        if ($state.state -eq 'done') { $code = 0 } else { $code = 1 }",
        "    }",
        "    elseif (-not (Get-Process -Id $workerPid -ErrorAction SilentlyContinue)) {",
        "        $quiet++",        # its last write can land a moment after the process is gone
        "        if ($quiet -ge 3) {",
        "            Show-NewLines",
        "            Write-Host ''",
        "            $left = 'The update process ended without reporting a result. See ' + $logFile",
        "            if ($mine -and $state.PSObject.Properties['if_stopped_here']) {",
        "                if ($state.if_stopped_here) { $left = 'Result: ' + [string]$state.if_stopped_here }",
        "            }",
        "            Write-Host $left",
        "            $code = 1",
        "        }",
        "    }",
        "    else { $quiet = 0 }",
        "    if ($code -lt 0) {",
        "        Start-Sleep -Milliseconds 500",
        "        $ticks++",
        "        if ($ticks -gt 28800) {",       # four hours
        "            Write-Host ('The update is still running. Progress: ' + $logFile)",
        "            $code = 0",
        "        }",
        "    }",
        "}",
        # D-39a review: a window nobody can see (no desktop, no console) must not wait for ever: it waits for
        # Enter at most CLOSE_AFTER_S, and not at all where no key can be read
        *([
            f"Write-Host 'Press Enter to close this window (it closes by itself in {CLOSE_AFTER_S // 60} "
            "minutes).'",
            f"$until = [DateTime]::UtcNow.AddSeconds({CLOSE_AFTER_S})",
            "try {",
            "    while ([DateTime]::UtcNow -lt $until) {",
            "        if ([Console]::KeyAvailable) {",
            "            if ([Console]::ReadKey($true).Key -eq 'Enter') { break }",
            "        }",
            "        else { Start-Sleep -Milliseconds 250 }",
            "    }",
            "} catch { }",
        ] if wait else []),
        "exit $code",
    ])


def follower_argv(script: str) -> list[str]:
    """The command line of the progress window: Windows PowerShell and the script, nothing else."""
    exe = pwsh.find_powershell() or "powershell.exe"
    return [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-EncodedCommand", pwsh.encode(script)]


def open_window(argv: list[str], cwd: Path) -> int:
    """Start `argv` in a console window of its own that outlives this process; returns its pid."""
    testguard.refuse_real("a console window on this PC", "Replace update_job.open_window in the test.")
    common: dict[str, Any] = {"cwd": str(cwd), "env": child_env(), "close_fds": True}
    try:
        flags = CREATE_NEW_CONSOLE | CREATE_BREAKAWAY_FROM_JOB
        return int(subprocess.Popen(argv, creationflags=flags, **common).pid)
    except OSError:             # the caller's Job Object does not allow a breakaway
        return int(subprocess.Popen(argv, creationflags=CREATE_NEW_CONSOLE, **common).pid)


def has_desktop() -> bool:
    """False in session 0 (a service, an engine started behind the dashboard route there, an OpenSSH
    command): a window opened there is invisible. The notes owner's public check."""
    from ns_supervisor import notify

    return notify.session_is_interactive()


def start_follower(paths: Paths, pid: int, log: Path) -> bool:
    """Open the progress window for the worker `pid`; False when it could not be opened (no PowerShell, no
    desktop session: nothing is started there). The worker runs either way."""
    if not has_desktop():
        return False
    cwd = paths.home if paths.home.is_dir() else Path(os.path.expanduser("~"))
    try:
        open_window(follower_argv(follower_script(status_file(paths), log, pid)), cwd)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def handover_sentence(what: str, log: Path, *, window: bool) -> str:
    """What a terminal prints before it returns. `what`: "The rollback", "The update to 0.1.15"."""
    noun = "the rollback" if "rollback" in what else "the update"
    if window:
        return (
            f"{what} runs in its own window, '{WINDOW_TITLE}', which shows each step and the result. This "
            f"terminal is free: closing it, or that window, does not stop {noun}. This command ends now, "
            f"before the result is known: the result is the last line of that window and of the log. "
            f"Log: {log}"
        )
    return (
        f"{what} runs in the background (the progress window could not be opened): Settings > Updates and "
        f"the log show each step and the result. This terminal is free: closing it does not stop {noun}. "
        f"This command ends now, before the result is known: the result is the last line of the log. "
        f"Log: {log}"
    )


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
    release_notes: str = ""         # the new version's CHANGELOG section, for the Settings > Update card


def _take(lock: FileLock, sleep: Callable[[float], None]) -> AlreadyRunning | None:
    """Take the update lock; the refusal when another process still holds it after `LOCK_TRIES` tries.
    `OSError`: the lock file cannot be made at all (the caller reports it; nothing has changed)."""
    refusal: AlreadyRunning | None = None
    for attempt in range(LOCK_TRIES):
        try:
            lock.acquire()
            return None
        except AlreadyRunning as e:
            refusal = e
            if attempt < LOCK_TRIES - 1:
                sleep(LOCK_PAUSE_S)
    return refusal


def _tell(
    paths: Paths, log: Path | None, watched: bool, message: str, *, title: str = NOT_COMPLETE_TITLE
) -> None:
    """The run ends with Nightshift NOT running (an environment left unfinished, or the engine did not come
    back): no page can show the result. So it is left as a morning note (the Queue page shows it at the
    next start, whoever starts it) and, unless a window already follows this run (`watched`: a terminal's
    progress window, the Update shortcut), the progress window is opened now; it shows the final line. In a
    session without a desktop no window is opened (`start_follower`): the note is the way it is told."""

    def note() -> None:
        from ns_supervisor import notify

        notify.leave_note(paths, title, message)

    def window() -> None:
        if not watched and log is not None:
            start_follower(paths, os.getpid(), log)

    _guarded(note)
    _guarded(window)


def run_worker(
    paths: Paths,
    body: Callable[[], Outcome],
    *,
    log: Path | None = None,
    wait_pid: int | None = None,
    wait: Callable[[int], None] | None = None,
    old_version: str = "",
    relaunch: Relauncher | None = None,
    cleanup: Callable[[], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    watched: bool = True,
) -> int:
    """Run the update in this (detached) process; returns the exit code.

    stdout and stderr go to `log` first thing: a detached process has no console. Then the worker takes
    `update_lock` and keeps it to its last line; one that cannot (another update, rollback or repair runs)
    writes that to its own log, touches nothing else and returns `LOCKED_EXIT`. `body` returns an `Outcome`
    and raises `NsError` on failure. The state file says `running` BEFORE the engine is stopped, then `done`
    or `failed` (`{state, old, new, message, updated_at}`, read by the dashboard); every `running` line
    carries `if_stopped_here`.

    `wait_pid`: the command that started this worker. It runs from the supervisor venv, so the worker waits
    until it has ended and refuses (NS-E706, nothing changed) when it does not (`wait_for_parent`).

    With `relaunch`, the engine (and the dashboard window, when one was open) is started again after the
    update, whether it applied or was put back, and `done` / `failed` is written only after the engine
    answers `/status`, so a page that reconnects finds a live dashboard and the final state. When the engine
    does not come back the state is `failed` with `restart_failed` set (and `updated` true when the new
    version is in place), so the page can say what happened instead of waiting for ever. A failure that
    left a Python environment unfinished (`NsError.data["venvs_left"]`) starts nothing: the state then
    carries `venvs_left`, and its message says so. Whenever the run ends with Nightshift not running, the
    result is also left as a morning note, and with `watched` false (no window follows this run: the
    Settings page, `--detach` from a script) the progress window is opened for it (`_tell`).

    `cleanup` runs once the engine is back (the update applied, or was put back and the old version is up):
    the worker cannot delete the private venv copy it runs from, so this starts a detached helper that does
    it after the worker exits (`updater_copy.discard_own_copy`).
    """
    sink: TextIO | None = None
    saved = (sys.stdout, sys.stderr)
    if log is not None:
        log.parent.mkdir(parents=True, exist_ok=True)
        sink = log.open("a", encoding="utf-8", buffering=1)
        sys.stdout = sink
        sys.stderr = sink
    lock = update_lock(paths)
    try:
        try:
            refused = _take(lock, sleep)
        except OSError as e:    # no lock, no update: said in the state file, which nobody else can own now
            message = f"NS-E702: could not take the update lock {lock.path} ({e}). Nothing was changed."
            sys.stderr.write(message + "\n")
            _final(paths, "failed", message, exit_code=1, log=str(log) if log else "", pid=os.getpid(),
                   old=old_version)
            return 1
        if refused is not None:
            sys.stderr.write(f"{BUSY} ({_started(refused.owner)}). This one did nothing.\n")
            return LOCKED_EXIT
        return _work(paths, body, log, wait_pid, wait, old_version, relaunch, cleanup, watched)
    finally:
        lock.release()
        if sink is not None:
            sink.flush()
            sys.stdout, sys.stderr = saved
            sink.close()


def _work(
    paths: Paths,
    body: Callable[[], Outcome],
    log: Path | None,
    wait_pid: int | None,
    wait: Callable[[int], None] | None,
    old_version: str,
    relaunch: Relauncher | None,
    cleanup: Callable[[], None] | None,
    watched: bool = True,
) -> int:
    """`run_worker` under the lock."""
    base: dict[str, Any] = {"log": str(log) if log else "", "pid": os.getpid(), "old": old_version}
    unchanged = unchanged_sentence(old_version)
    left: list[str] = []
    _progress(paths, "running", "Waiting for the command that started the update to end.",
              if_stopped_here=unchanged, **base)
    try:
        if wait_pid is not None:
            (wait or wait_for_parent)(wait_pid)
        if relaunch is not None:
            _guarded(relaunch.observe)
        _progress(paths, "running", "Updating.", if_stopped_here=unchanged, **base)
        out = body()
    except NsError as e:
        sys.stderr.write(f"{e}\nHint: {e.definition.hint}\n")
        message = f"{e.code}: {e.detail}"
        left = [str(v) for v in e.data.get("venvs_left") or []]
    except Exception as e:  # a worker has nobody to show a traceback to: log it, report failure
        sys.stderr.write(f"{type(e).__name__}: {e}\n")
        message = f"{type(e).__name__}: {e}"
    else:
        versions = {**base, "old": out.old or old_version, "new": out.new, "updated": out.updated}
        if out.release_notes:
            versions["release_notes"] = out.release_notes
        _progress(      # (`updated` and `new`: a reader of a worker that dies here knows the program changed)
            paths, "running", "Starting Nightshift again.",
            if_stopped_here=f"{out.message} {NO_RESTART} {OPEN_IT}",
            **{k: v for k, v in versions.items() if k != "release_notes"},
        )
        if _restart(relaunch):
            _tell(paths, log, watched, f"{out.message} {RESTART_HINT}",
                  title=APPLIED_TITLE if out.updated else NOT_RESTARTED_TITLE)
            _final(paths, "failed", f"{out.message} {RESTART_HINT}", exit_code=1,
                   restart_failed=True, **versions)
            return 1
        _final(paths, "done", out.message, exit_code=0, **versions)
        if cleanup is not None:
            _guarded(cleanup)
        return 0
    if left:    # an environment is half-filled: nothing is started on it (the message says what to do)
        _tell(paths, log, watched, message)
        _final(paths, "failed", message, exit_code=1, venvs_left=left, relaunched=False, **base)
        return 1
    # put back (or never started): the old version comes back up
    _progress(
        paths, "running", "Starting Nightshift again.",
        if_stopped_here=f"{message} {NO_RESTART} {OPEN_IT}",
        **base,
    )
    if _restart(relaunch):
        _tell(paths, log, watched, f"{message} {RESTART_HINT}")
        _final(paths, "failed", f"{message} {RESTART_HINT}", exit_code=1, restart_failed=True, **base)
    else:
        _final(paths, "failed", message, exit_code=1, **base)
        if cleanup is not None and relaunch is not None:
            _guarded(cleanup)
    return 1
