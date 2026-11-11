# ruff: noqa: E501
"""Stand-ins for the nightly task's process tree, for tests/test_night_kill_tree.py (card night-fits-b).

The real tree on the GPU PC: Task Scheduler starts `cmd.exe /d /s /c "... powershell.exe -File
nightshift-launch.ps1 -Nightly -Studio studio.exe"` (`schedule.action_arguments`); the launch script runs
`& $Studio run --daily` and waits; `studio.exe` (a console-script launcher) runs the studio's python; the studio
runs FFmpeg as a plain child (`ns_common.ffmpeg`), and starts the engine when none answers
(`client.spawn_supervisor`: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW, no breakaway); the
engine starts llama-server and the TTS in its own Job Object with KILL_ON_JOB_CLOSE (`procs.JobObject`).

Here: a real `cmd.exe /d /s /c` (the task's action), `stub` (the launch script: starts the studio and waits),
the studio (`tests.d31_driver`, the real `run_daily` with stub stages, its owner locks and its beat), and,
started by the driver's hanging stage (`start_children`): `ffmpeg` (a plain child holding an output file
open, as FFmpeg holds the file it writes), `child` (another plain child of the studio), and `engine` (started
with the engine's flags; it starts `llama` and `tts` in a `procs.JobObject`). Every process writes
`<dir>/<name>.pid` the moment it runs, so the test ends each one by its recorded pid.

    python -m tests.night_tree stub|engine <dir>
    python -m tests.night_tree sleeper <name> <dir> [<file to hold open>]
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
NAMES = ("stub", "studio", "ffmpeg", "child", "engine", "llama", "tts")
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
#: `client.spawn_supervisor`'s flags (DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW)
ENGINE_FLAGS = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0) | (
    _NO_WINDOW)


def _mark(name: str, folder: Path) -> None:
    (folder / f"{name}.pid").write_text(str(os.getpid()), encoding="utf-8")


def _py(*args: str) -> list[str]:
    return [sys.executable, "-m", "tests.night_tree", *args]


def pids(folder: Path) -> dict[str, int]:
    """Every recorded pid of the tree, by name."""
    out: dict[str, int] = {}
    for f in folder.glob("*.pid"):
        try:
            out[f.stem] = int(f.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            continue
    return out


def start_children(folder: Path, hold: Path) -> None:
    """Called inside the studio's hanging stage: its FFmpeg, another child, and the engine with its two."""
    env = {**os.environ}
    subprocess.Popen(_py("sleeper", "ffmpeg", str(folder), str(hold)), cwd=REPO, env=env, creationflags=_NO_WINDOW)
    subprocess.Popen(_py("sleeper", "child", str(folder)), cwd=REPO, env=env, creationflags=_NO_WINDOW)
    subprocess.Popen(_py("engine", str(folder)), cwd=REPO, env=env, creationflags=ENGINE_FLAGS,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 60
    while not all((folder / f"{n}.pid").exists() for n in ("ffmpeg", "child", "engine", "llama", "tts")):
        if time.monotonic() > deadline:
            raise RuntimeError("the tree's children did not start")
        time.sleep(0.05)


def main(argv: list[str]) -> None:
    kind, rest = argv[0], argv[1:]
    if kind == "sleeper":
        name, folder = rest[0], Path(rest[1])
        held = open(rest[2], "wb") if len(rest) > 2 else None  # noqa: SIM115 - held until the process ends
        _mark(name, folder)
        time.sleep(600)
        if held is not None:
            held.close()
        return
    folder = Path(rest[0])
    if kind == "stub":  # the launch script: `& $Studio run --daily`, then its exit code
        _mark("stub", folder)
        studio = subprocess.Popen([sys.executable, "-m", "tests.night_tree", "studio", str(folder)], cwd=REPO,
                                  env={**os.environ}, creationflags=_NO_WINDOW)
        sys.exit(studio.wait())
    if kind == "studio":  # the studio's python: the real nightly with the stub stages
        _mark("studio", folder)
        from tests import d31_driver

        d31_driver.main()
        return
    if kind == "engine":  # the engine: its services in a Job Object that dies with it
        _mark("engine", folder)
        from ns_supervisor.procs import JobObject

        job = JobObject()
        for name in ("llama", "tts"):
            p = subprocess.Popen(_py("sleeper", name, str(folder)), cwd=REPO, env={**os.environ},
                                 creationflags=_NO_WINDOW)
            job.assign(p.pid)
        time.sleep(600)
        return
    raise SystemExit(f"unknown stand-in {kind!r}")


if __name__ == "__main__":
    main(sys.argv[1:])
