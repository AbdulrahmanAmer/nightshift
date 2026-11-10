"""D-46: stopping Nightshift for an update, a rollback, a resync or setup's Repair, as one sequence.

The proof of block D found an engine that was asked to stop (`POST /shutdown`), stopped its services and
closed its port, and then never exited: the update's holder check, rightly, refused to rewrite the Python
environments while it lived, named it (its venv launcher) as "a program to close", and nothing ended it, so
Nightshift was left down. Asking is not enough. The sequence here is:

1. Note what is OURS before anything is asked (the engine removes its pid file on its way out):
   - the process that holds this home's `run/supervisor.lock` (only a live engine of this home can hold it;
     the lock file names its pid, `singleton.read_owner`), when its command line runs `ns_supervisor.app`;
   - each pid `run/supervisor.json` records (the engine and the services it started), when that process was
     created no later than the file was last written (a pid Windows gave to another program after the record
     is newer than the record) and its command line names this home or one of the engine's own modules;
   - every live descendant of those (a studio child of the dashboard, a server a service started);
   - the parent of the engine when its image is this home's `venvs\\supervisor\\Scripts\\python.exe` (the venv
     launcher, which only waits for the engine).
   Each is kept as a `psutil.Process`, which refuses to signal a pid that was reused since (it compares the
   creation time), so "ours" never means "a pid that merely looks like one".
2. Ask the engine to stop (`/shutdown`), when it answers. A request that fails or times out does not stop the
   sequence: the waiting below decides.
3. Wait up to `STOP_WAIT_S` until none of ours is alive and no program runs from the environments (`holders`,
   the holder check's own list), saying who is still there when that changes (and every
   `PROGRESS_EVERY_S`): "Waiting for Nightshift to stop: ...".
4. End what is ours and still alive, by its recorded process, and wait up to `END_WAIT_S` for it to be gone.
   Each one ended is said ("Ended Nightshift's own ...").
5. Return. The caller's holder check (`update.wait_no_holders`) then waits for the rest, which are not ours,
   and refuses naming THEM.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_common.singleton import read_owner

#: After the engine was asked to stop, how long it and every program of the environments get to end.
STOP_WAIT_S = 30.0
#: After what is ours was ended by its process, how long Windows gets to let it go.
END_WAIT_S = 10.0
#: How often an unchanged "Waiting for Nightshift to stop" line is said again.
PROGRESS_EVERY_S = 10.0
#: How long the engine gets to answer `/shutdown` (its own grace for a GPU holder is `SHUTDOWN_GRACE_S`).
SHUTDOWN_GRACE_S = 30.0
#: The modules the engine runs as, or starts its services with: a recorded pid whose command line names none
#: of them (and not this home either) is not ours.
OWN_MODULES = ("ns_supervisor.app", "ns_dashboard.server", "ns_tts.server", "ns_supervisor.devfake")
#: A record written in the same second as a process started counts it (file times are coarse on some disks).
RECORD_SLACK_S = 2.0

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
        """True while THIS process (same pid and creation time) runs."""
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


def _cmdline(proc: psutil.Process) -> str:
    try:
        return " ".join(proc.cmdline())
    except psutil.Error:
        return ""


def _image(proc: psutil.Process) -> str:
    try:
        return Path(proc.exe()).name or proc.name()
    except psutil.Error:
        try:
            return proc.name()
        except psutil.Error:
            return "a program"


def _under(path: str, folder: Path) -> bool:
    root = os.path.normcase(os.path.abspath(folder)) + os.sep
    return bool(path) and os.path.normcase(os.path.abspath(path)).startswith(root)


def lock_held(lock: Path) -> bool:
    """True while another process holds the lock file `lock` (byte 0 locked: reading it is refused). It only
    looks: taking the lock, even for an instant, could make an engine that is starting refuse itself."""
    try:
        with open(lock, "rb") as fh:
            fh.read(1)
    except PermissionError:
        return True
    except OSError:
        return False
    return False


def engine_holder(paths: Paths) -> psutil.Process | None:
    """The live engine of this home: the process that holds `run/supervisor.lock`, as the file names it, when
    its command line runs `ns_supervisor.app`. None when nobody holds it."""
    lock = paths.home / "run" / "supervisor.lock"
    if not lock_held(lock):
        return None
    pid = read_owner(lock)
    if pid is None:
        return None
    try:
        proc = psutil.Process(pid)
    except psutil.Error:
        return None
    return proc if "ns_supervisor.app" in _cmdline(proc) else None


def _recorded(paths: Paths) -> list[tuple[psutil.Process, str]]:
    """The processes `run/supervisor.json` records that are still the processes it recorded."""
    f = paths.home / "run" / "supervisor.json"
    try:
        written = f.stat().st_mtime
        data = json.loads(f.read_text(encoding="utf-8"))
        named = [(int(data["pid"]), "the engine")]
        named += [(int(v), f"the {k} service" if k != "dashboard" else "the dashboard")
                  for k, v in dict(data.get("children") or {}).items()]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    home = str(paths.home)
    found: list[tuple[psutil.Process, str]] = []
    for pid, what in named:
        try:
            proc = psutil.Process(pid)
            created = proc.create_time()
        except psutil.Error:
            continue
        if created > written + RECORD_SLACK_S:
            continue            # a newer process got the number: not the one recorded
        cmd = _cmdline(proc)
        exe = ""
        with contextlib.suppress(psutil.Error):
            exe = proc.exe()
        names_us = any(m in cmd for m in OWN_MODULES) or _under(exe, paths.home) or (
            os.path.normcase(home) in os.path.normcase(cmd)
        )
        if names_us:
            found.append((proc, what))
    return found


def ours(paths: Paths) -> list[Own]:
    """What this install started and is alive now (the rules are in the module docstring), engine first."""
    seen: dict[int, Own] = {}

    def add(proc: psutil.Process, what: str) -> None:
        if proc.pid not in seen and proc.pid != os.getpid():
            seen[proc.pid] = Own(proc, what, _image(proc))

    roots: list[tuple[psutil.Process, str]] = []
    holder = engine_holder(paths)
    if holder is not None:
        roots.append((holder, "the engine"))
    roots += _recorded(paths)
    for proc, what in roots:
        add(proc, what)
    for proc, _what in roots:
        with contextlib.suppress(psutil.Error):
            for child in proc.children(recursive=True):
                add(child, "a process the engine started")
    engines = [proc for proc, what in roots if what == "the engine"]
    launcher = paths.venvs / "supervisor"
    for proc in engines:
        with contextlib.suppress(psutil.Error):
            parent = proc.parent()
            if parent is not None and _under(parent.exe(), launcher):
                add(parent, "the engine's launcher")
    return list(seen.values())


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
        try:
            client.shutdown(SHUTDOWN_GRACE_S)
            out.lines.append("Engine stopped through the supervisor API.")
        except NsError as e:                    # (it may still be going: the wait decides)
            out.lines.append(f"The engine did not answer its stop request ({e.detail}).")
    elif mine:
        out.lines.append("The engine does not answer; its own processes are still running.")
    else:
        out.lines.append("The engine is not running.")
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
