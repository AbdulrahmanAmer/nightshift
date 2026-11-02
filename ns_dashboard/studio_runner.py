"""Runs studio work as a child process in the studio venv (component C9).

`python -m ns_dashboard.server` runs in the supervisor venv. That venv gets only `[supervisor,tts,dashboard]`:
no faster-whisper, no cuBLAS/cuDNN wheels, no spaCy, no OpenCV. Everything that needs them (a story run, a
clip job, a series part, the transcript of a voice sample, a link download) is therefore started here as

    <home>\\venvs\\studio\\Scripts\\python.exe -m ns_dashboard.studio_job <kind> --args <file.json>

with the app folder as working directory and `NIGHTSHIFT_HOME` in the environment (UTF-8 on). The child prints
one JSON object per line behind the prefix `NSJOB ` (see `ns_dashboard.studio_job`); the runner turns them into
callbacks and the final payload. Cancelling kills the whole process tree with the supervisor's own helper
(`ns_supervisor.procs.kill_tree`), so a stage, FFmpeg or yt-dlp never outlives the job.

The interpreter comes from the paths the installer writes (`ns_supervisor.services.venv_python`) and is
`sys.executable` when that venv does not exist (CI, development). Tests pass `python=[sys.executable, fake.py]`.
"""

from __future__ import annotations

import collections
import contextlib
import json
import os
import queue
import subprocess  # noqa: TID251  (the child's pipe is read here; the kill goes through the supervisor)
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO, Any

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_dashboard.lazyimport import lazy
from ns_dashboard.studio_job import PREFIX
from ns_dashboard.uploads import tmp_dir

ENTRY = "ns_dashboard.studio_job"
#: Variables of the dashboard's own interpreter that must not leak into the studio venv.
DROP_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__")
TAIL_LINES = 30
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

Event = dict[str, Any]


def studio_python(paths: Paths) -> str:
    """Interpreter of the studio venv; `sys.executable` when the installer has not created it (CI, dev)."""
    venv_python = lazy("ns_supervisor.services", "venv_python")
    return str(venv_python(paths, "studio"))


def app_dir(paths: Paths) -> Path:
    """The extracted app (`install_state.app_path`), or this checkout when the installer has not run."""
    app_path = lazy("ns_supervisor.control", "app_path")
    return Path(app_path(paths))


def kill_tree(proc: subprocess.Popen[str]) -> None:
    """Kill `proc` and everything it started (the supervisor's `taskkill /T /F`), then reap it."""
    helper = lazy("ns_supervisor.procs", "kill_tree")
    if proc.poll() is None:
        helper(proc.pid)
        with contextlib.suppress(OSError):
            proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=15)


def parse_event(line: str) -> Event | None:
    """The event of a protocol line, or None for any other output of the child."""
    if not line.startswith(PREFIX):
        return None
    try:
        data = json.loads(line[len(PREFIX) :])
    except ValueError:
        return None
    return data if isinstance(data, dict) and isinstance(data.get("ev"), str) else None


def failure_error(event: Event) -> Exception:
    """The exception for an `error` event: `NsError` for a known `NS-` code, else `RuntimeError`."""
    code = event.get("code")
    if isinstance(code, str):
        with contextlib.suppress(KeyError):
            return NsError(code, str(event.get("detail") or ""))
    return RuntimeError(str(event.get("message") or "The studio process failed."))


def _read_lines(stream: IO[str], out: queue.Queue[str | None]) -> None:
    """Thread body: every line of the child's output into `out`, then None at the end."""
    try:
        for line in stream:
            out.put(line)
    finally:
        out.put(None)


class StudioRunner:
    """Starts one studio child per call and follows it until it ends or the job is cancelled."""

    def __init__(
        self,
        paths: Paths,
        *,
        python: Sequence[str] | str | None = None,
        cwd: Path | None = None,
        poll_s: float = 0.25,
        kill: Callable[[subprocess.Popen[str]], None] = kill_tree,
    ) -> None:
        self.paths = paths
        self._python = python
        self._cwd = cwd
        self.poll_s = poll_s
        self._kill = kill

    # --- how the child is started ---
    def interpreter(self) -> list[str]:
        """The command that starts Python (one or more words)."""
        if self._python is None:
            return [studio_python(self.paths)]
        return [self._python] if isinstance(self._python, str) else list(self._python)

    def command(self, kind: str, args_file: Path) -> list[str]:
        """Full argv of the child."""
        return [*self.interpreter(), "-m", ENTRY, kind, "--args", str(args_file)]

    def environment(self) -> dict[str, str]:
        """Environment of the child: the dashboard's own, without its interpreter variables, plus the home."""
        env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
        env.update(
            {
                "NIGHTSHIFT_HOME": str(self.paths.home),
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
            }
        )
        return env

    def workdir(self) -> Path:
        """Working directory of the child: the app folder."""
        return self._cwd or app_dir(self.paths)

    def _args_file(self, kind: str, args: dict[str, Any]) -> Path:
        folder = tmp_dir(self.paths)
        folder.mkdir(parents=True, exist_ok=True)
        f = folder / f"studio-{kind}-{os.getpid()}-{threading.get_ident()}-{os.urandom(3).hex()}.json"
        f.write_text(json.dumps(args, ensure_ascii=False, default=str), encoding="utf-8")
        return f

    # --- running ---
    def run(
        self,
        kind: str,
        args: dict[str, Any],
        *,
        on_event: Callable[[Event], None] = lambda _e: None,
        check_cancel: Callable[[], None] = lambda: None,
    ) -> Event:
        """Run `kind` to the end and return its `result` payload.

        `on_event(event)` gets every progress event; `check_cancel()` is called after each line and every
        `poll_s` seconds while the child is quiet, and may raise (the dashboard's `JobCancelled`): the child's
        process tree is killed first and the exception passes on. A failure in the child becomes `NsError`
        when it carries an `NS-` code, else `RuntimeError`.
        """
        args_file = self._args_file(kind, args)
        lines: queue.Queue[str | None] = queue.Queue()
        tail: collections.deque[str] = collections.deque(maxlen=TAIL_LINES)
        result: Event | None = None
        failure: Event | None = None
        try:
            proc = subprocess.Popen(
                self.command(kind, args_file),
                cwd=str(self.workdir()),
                env=self.environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=NO_WINDOW,
            )
        except OSError as e:
            args_file.unlink(missing_ok=True)
            raise RuntimeError(f"The studio process could not be started ({e}).") from e
        assert proc.stdout is not None
        reader = threading.Thread(target=_read_lines, args=(proc.stdout, lines), daemon=True)
        reader.start()
        try:
            while True:
                try:
                    line = lines.get(timeout=self.poll_s)
                except queue.Empty:
                    check_cancel()
                    continue
                if line is None:
                    break
                event = parse_event(line)
                if event is None:
                    tail.append(line.rstrip())
                else:
                    name = event.get("ev")
                    if name == "result":
                        result = event
                    elif name == "error":
                        failure = event
                    else:
                        on_event(event)
                check_cancel()
            proc.wait(timeout=60)
        except BaseException:
            self._kill(proc)
            raise
        finally:
            reader.join(timeout=5)
            args_file.unlink(missing_ok=True)
        if failure is not None:
            raise failure_error(failure)
        if result is None or proc.returncode != 0:
            last = " | ".join(list(tail)[-5:])
            raise RuntimeError(f"The studio process stopped (exit {proc.returncode}). {last}".strip())
        return {k: v for k, v in result.items() if k != "ev"}
