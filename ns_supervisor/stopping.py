"""D-46: stopping Nightshift for an update, a rollback, a resync or setup's Repair, as one sequence.

The proof of block D found an engine that was asked to stop (`POST /shutdown`), stopped its services and
closed its port, and then never exited: the update's holder check, rightly, refused to rewrite the Python
environments while it lived, named it (its venv launcher) as "a program to close", and nothing ended it, so
Nightshift was left down. Asking is not enough. The sequence here is:

1. Note what is OURS before anything is asked (the engine removes its pid file on its way out), by the one
   rule setup's holder wait uses too (`venvrepair.own_processes`, with `venvrepair.is_this_install` and its
   truth table): the holder of this home's engine lock; a pid `run/supervisor.json` records only when it is
   tied to THIS install (its program under the home, the home on its command line, or its NIGHTSHIFT_HOME,
   read here from the process's environment) and started before the record was written; the engine's venv
   launcher; their descendants. Each is kept as a `psutil.Process`, which refuses to signal a pid that was
   reused since (it compares the creation time).
2. Ask the engine to stop (`/shutdown`), when it answers, for at most `SHUTDOWN_WAIT_S`, with a progress line
   every `PROGRESS_EVERY_S`. A request that fails or is still out does not stop the sequence.
3. Wait up to `STOP_WAIT_S` until none of ours is alive and no program runs from the environments (`holders`,
   the holder check's own list), saying who is still there when that changes (and every
   `PROGRESS_EVERY_S`): "Waiting for Nightshift to stop: ...".
4. End what is ours and still alive, by its noted process, and wait up to `END_WAIT_S` for it to be gone.
   Each one ended is said ("Ended Nightshift's own ...").
5. Return. The caller's holder check (`update.wait_no_holders`) then waits for the rest, which are not ours,
   and refuses naming THEM.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from installer.stage2.web import venvrepair
from ns_common.errors import NsError
from ns_common.paths import Paths

#: After the engine was asked to stop, how long it and every program of the environments get to end.
STOP_WAIT_S = 30.0
#: After what is ours was ended by its process, how long Windows gets to let it go.
END_WAIT_S = 10.0
#: How often an unchanged "Waiting for Nightshift to stop" line is said again.
PROGRESS_EVERY_S = 10.0
#: The engine's own grace for a GPU holder, sent with `/shutdown`.
SHUTDOWN_GRACE_S = 30.0
#: How long the stop request is waited for before the sequence goes on without its answer (review note: the
#: request alone may take 150 s; the waiting below decides anyway).
SHUTDOWN_WAIT_S = SHUTDOWN_GRACE_S + 15.0

Say = Callable[[str], None]


@dataclass(frozen=True)
class Own:
    """A process this install started, as it was when it was noted."""

    proc: psutil.Process
    what: str           # "the engine", "the dashboard", "the engine's launcher", ...
    program: str        # its image name

    @property
    def pid(self) -> int:
        return int(self.proc.pid)

    def alive(self) -> bool:
        """True while THIS process (same pid and creation time) runs.

        On Windows the answer is Windows' own (`core._windows_started`: the exit code and the creation time
        from one handle). psutil 7.2.2 calls an ended process running for as long as Windows still lists its
        pid (another program holds a handle to it), so right after a kill this said "alive" for seconds on a
        busy PC and the stop sequence reported "could not be ended" (NS-E706) for a process that HAD ended (CI
        of PR 216, shard 5; the same fault as the GPU holder probe, refs/reviews/block-e3-fullpass.md
        failure 1).
        When Windows gives no answer (access denied, a failed call), psutil decides, as before."""
        if sys.platform == "win32" and 0 < self.pid < 2**32:
            from ns_supervisor.core import _windows_started

            answered, created = _windows_started(self.pid)
            if answered:
                if created is None:
                    return False                # no process has the pid, or the one that has it ended
                try:
                    noted = self.proc.create_time()     # as noted when it was listed (psutil keeps it)
                except psutil.Error:
                    return False
                return abs(created - noted) < 0.01      # another creation time: the pid was reused
        try:
            return self.proc.is_running() and self.proc.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    def __str__(self) -> str:
        return f"{self.program} (process {self.pid}, {self.what})"


@dataclass
class Stopped:
    """What the sequence did: lines for the console, what it ended, and what is ours but would not end."""

    lines: list[str] = field(default_factory=list)
    ended: list[str] = field(default_factory=list)
    not_ended: list[str] = field(default_factory=list)
    own_pids: set[int] = field(default_factory=set)


def _image(proc: psutil.Process) -> str:
    try:
        return Path(proc.exe()).name or proc.name()
    except psutil.Error:
        try:
            return proc.name()
        except psutil.Error:
            return "a program"


def snapshot(paths: Paths) -> list[venvrepair.Proc]:
    """Every running process as `venvrepair.Proc`. The NIGHTSHIFT_HOME of each pid the record names is read
    from its environment (psutil; Windows allows it for a process of the same user that is not elevated;
    otherwise None, and only its program file or command line can tie it to this install)."""
    named, _written = venvrepair.recorded(paths.home)
    asked = {pid for pid, _what in named}
    out: list[venvrepair.Proc] = []
    for proc in psutil.process_iter(["pid", "ppid", "exe", "cmdline", "create_time"]):
        info = proc.info
        env_home = None
        if info["pid"] in asked:
            with contextlib.suppress(psutil.Error, OSError):
                env_home = proc.environ().get("NIGHTSHIFT_HOME")
        out.append(venvrepair.Proc(
            int(info["pid"]), info.get("ppid"), str(info.get("exe") or ""),
            " ".join(str(a) for a in info.get("cmdline") or []), info.get("create_time"), env_home,
        ))
    return out


def ours(paths: Paths) -> list[Own]:
    """What this install started and is alive now (`venvrepair.own_processes`), the engine first, each as
    the `psutil.Process` that was listed (one whose creation time differs is another process: skipped)."""
    procs = snapshot(paths)
    by_pid = {p.pid: p for p in procs}
    found: list[Own] = []
    for pid, what in venvrepair.own_processes(paths.home, procs).items():
        if pid == os.getpid():
            continue
        try:
            proc = psutil.Process(pid)
            created = proc.create_time()
        except psutil.Error:
            continue
        listed = by_pid[pid].created
        if listed is not None and abs(created - listed) > 0.5:
            continue
        found.append(Own(proc, what, _image(proc)))
    return sorted(found, key=lambda o: o.what != "the engine")


def engine_lives(paths: Paths) -> bool:
    """True while an engine of this home holds its lock (it may have closed its port: D-46)."""
    return venvrepair.engine_lock_owner(paths.home) is not None


def _names(own: Iterable[Own], holders: Iterable[Any], skip: set[int]) -> list[str]:
    out = [str(o) for o in own]
    pids = {o.pid for o in own}
    out += [str(h) for h in holders if getattr(h, "pid", None) not in pids | skip]
    return out


def _family() -> set[int]:
    """This process and the ones that started it (a holder check never waits for itself)."""
    try:
        me = psutil.Process()
        return {me.pid, *(p.pid for p in me.parents())}
    except psutil.Error:
        return {os.getpid()}


def _ask(client: Any, tell: Say, sleep: Callable[[float], None], clock: Callable[[], float]) -> str:
    """`POST /shutdown`, waited for at most `SHUTDOWN_WAIT_S` with a progress line every `PROGRESS_EVERY_S`.
    Its result line; a request still out is left to end on its own (a daemon thread)."""
    answer: dict[str, Any] = {}

    def run() -> None:
        try:
            client.shutdown(SHUTDOWN_GRACE_S)
            answer["ok"] = True
        except NsError as e:
            answer["error"] = e.detail
        except Exception as e:  # noqa: BLE001 - a thread must not die silently
            answer["error"] = f"{type(e).__name__}: {e}"

    thread = threading.Thread(target=run, name="ns-stop-request", daemon=True)
    thread.start()
    start = said = clock()
    while thread.is_alive() and clock() - start < SHUTDOWN_WAIT_S:
        thread.join(0.25)
        if thread.is_alive() and clock() - said >= PROGRESS_EVERY_S:
            tell("Waiting for Nightshift to stop: the engine has not answered its stop request yet "
                 f"({round(clock() - start)} s; a running job gets up to {SHUTDOWN_GRACE_S:g} s).")
            said = clock()
        sleep(0)
    if answer.get("ok"):
        return "Engine stopped through the supervisor API."
    if "error" in answer:
        return f"The engine did not answer its stop request ({answer['error']})."
    return f"The engine had not answered its stop request after {SHUTDOWN_WAIT_S:g} s."


def stop_nightshift(
    paths: Paths,
    client: Any,
    *,
    holders: Callable[[], list[Any]] | None = None,
    say: Say | None = None,
    stop_wait_s: float | None = None,
    end_wait_s: float | None = None,
    note: Callable[[Paths], list[Own]] = ours,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Stopped:
    """Steps 1 to 4 of the module docstring. `holders`: the holder check's list (the update passes
    `update.holders`); without it only our own processes are waited for (`ns stop`). Never raises for a
    process: what could not be ended is in `not_ended`, and the caller's holder check names it."""
    tell = say or (lambda _line: None)
    stop_wait_s = STOP_WAIT_S if stop_wait_s is None else stop_wait_s
    end_wait_s = END_WAIT_S if end_wait_s is None else end_wait_s
    out = Stopped()
    mine = note(paths)                          # before the engine is asked: it removes its pid file
    out.own_pids = {o.pid for o in mine}
    skip = _family()
    if client is not None and client.is_up():
        out.lines.append(_ask(client, tell, sleep, clock))
    else:   # D-51: the cause is said first, before the lines that wait and end (with `say`, at once)
        cause = ("The engine does not answer; its own processes are still running." if mine
                 else "The engine is not running.")
        if say is not None:
            say(cause)
        else:
            out.lines.append(cause)
    look = holders or (lambda: [])

    def left() -> tuple[list[Own], list[str]]:
        alive = [o for o in mine if o.alive()]
        try:
            others = look()
        except Exception:  # noqa: BLE001 - a listing that failed is decided by the holder check after this
            others = []
        names = _names(alive, others, skip)
        if not names and client is not None and client.is_up():
            names = ["the engine (it still answers)"]      # (not noted as ours: it is waited for, not ended)
        return alive, names

    start = clock()
    alive, names = left()
    said, said_at = "", start
    while names:
        now = clock()
        if now - start >= stop_wait_s:
            break
        line = "Waiting for Nightshift to stop: " + "; ".join(names)
        if line != said or now - said_at >= PROGRESS_EVERY_S:
            tell(line)
            said, said_at = line, now
        sleep(0.25)
        alive, names = left()
    if not alive:
        return out
    waited = round(clock() - start)
    for o in alive:
        try:
            o.proc.kill()           # psutil refuses a pid that was reused since it was noted
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as e:
            out.not_ended.append(f"{o} ({type(e).__name__})")
            continue
        line = (f"Ended Nightshift's own {o.program} (process {o.pid}, {o.what}): it was still running "
                f"{waited} s after the stop began.")
        out.ended.append(str(o))
        if say is not None:
            say(line)
        else:
            out.lines.append(line)
    deadline = clock() + end_wait_s
    while any(o.alive() for o in alive) and clock() < deadline:
        sleep(0.25)
    for o in alive:
        if o.alive() and str(o) in out.ended:
            out.ended.remove(str(o))
            out.not_ended.append(f"{o} (still running {round(end_wait_s)} s after it was ended)")
    return out
