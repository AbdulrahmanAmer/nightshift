"""Runs studio work as a child process in the studio venv (component C9).

`python -m ns_dashboard.server` runs in the supervisor venv. That venv gets only `[supervisor,tts,dashboard]`:
no faster-whisper, no cuBLAS/cuDNN wheels, no spaCy, no OpenCV. Everything that needs them (a story run, a
clip job, a series part, the transcript of a voice sample, a link download) is therefore started here as

    <home>\\venvs\\studio\\Scripts\\python.exe -m ns_dashboard.studio_job <kind> --args <file.json>

with the app folder as working directory and `NIGHTSHIFT_HOME` in the environment (UTF-8 on). The child prints
one JSON object per line behind the prefix `NSJOB ` (see `ns_dashboard.studio_job`); the runner turns them
into callbacks and the final payload. Cancelling first asks the child to stop (it checks a flag file between
stages and releases its GPU lease itself), waits `grace_s` seconds, and then kills the whole process tree with
the supervisor's own helper (`ns_supervisor.procs.kill_tree`), so a stage, FFmpeg or yt-dlp never outlives
the job.

The interpreter comes from the paths the installer writes (`ns_supervisor.services.venv_python`) and is
`sys.executable` when that venv does not exist (CI, development). Tests pass `python=[sys.executable, f.py]`.
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
from ns_dashboard.studio_job import CANCEL_ENV, CARRIED, PREFIX
from ns_dashboard.uploads import tmp_dir

ENTRY = "ns_dashboard.studio_job"
#: Variables of the dashboard's own interpreter that must not leak into the studio venv.
DROP_ENV = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__")
TAIL_LINES = 30
GRACE_S = 10.0
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


def failure_error(event: Event) -> NsError:
    """The exception for an `error` event: `NsError` with the child's own code when it is a known one.

    A failure without a code (a bug or a library error in the child) or with a code `errors.json` does not
    know becomes NS-E327 with the exception type and message as detail, so the Queue card always shows a
    code and the hint to open the log, never a bare `null`.
    """
    code = event.get("code")
    detail = str(event.get("detail") or "")
    hint = event.get("hint")
    # the job's own hint (D-25) and what a stopped request still owes (P47)
    extra: dict[str, Any] = {"hint": hint} if isinstance(hint, str) and hint else {}
    extra.update({k: event[k] for k in CARRIED if isinstance(event.get(k), (int, str))})
    if isinstance(code, str):
        with contextlib.suppress(KeyError):
            return NsError(code, detail, **extra)
    message = str(event.get("message") or "The studio process failed.")
    if isinstance(code, str):
        message = f"{message} (unknown code {code})"
    return NsError("NS-E327", message)


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
        grace_s: float = GRACE_S,
        kill: Callable[[subprocess.Popen[str]], None] = kill_tree,
        cancel_grace_s: float = 3.0,
    ) -> None:
        self.paths = paths
        self._python = python
        self._cwd = cwd
        self.poll_s = poll_s
        self.grace_s = grace_s
        self._kill = kill
        self.cancel_grace_s = cancel_grace_s

    # --- how the child is started ---
    def interpreter(self) -> list[str]:
        """The command that starts Python (one or more words)."""
        if self._python is None:
            return [studio_python(self.paths)]
        return [self._python] if isinstance(self._python, str) else list(self._python)

    def command(self, kind: str, args_file: Path) -> list[str]:
        """Full argv of the child."""
        return [*self.interpreter(), "-m", ENTRY, kind, "--args", str(args_file)]

    def environment(self, cancel_file: Path | None = None) -> dict[str, str]:
        """Environment of the child: the dashboard's own, without its interpreter variables, plus the home
        and, when given, the flag file that asks it to stop."""
        env = {k: v for k, v in os.environ.items() if k not in DROP_ENV}
        env.update(
            {
                "NIGHTSHIFT_HOME": str(self.paths.home),
                "PYTHONUTF8": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUNBUFFERED": "1",
            }
        )
        if cancel_file is not None:
            env[CANCEL_ENV] = str(cancel_file)
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

    def _stop(
        self, proc: subprocess.Popen[str], cancel_file: Path, on_cancel: Callable[[], None] | None = None
    ) -> None:
        """Ask the child to stop by itself, wait a little, then kill what is left (always).

        Every child is asked the same way: the flag file named by `NS_JOB_CANCEL_FILE` is created, which the
        child sees between stages (`studio_job.report`), so its `finally` blocks give the GPU lease back; it
        has `grace_s` (10 s). A kind with its own signal (`on_cancel`, the link download's flag that cancels
        the Clipper token) gets that signal too and `cancel_grace_s` (3 s). The caller releases the lease
        through the supervisor afterwards, for a child that was in the middle of a stage and had to be killed.
        """
        if proc.poll() is None:
            grace = self.grace_s
            with contextlib.suppress(OSError):
                cancel_file.write_text("cancel", encoding="utf-8")
            if on_cancel is not None:
                grace = self.cancel_grace_s
                with contextlib.suppress(Exception):
                    on_cancel()
            if grace > 0:
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=grace)
        self._kill(proc)

    @staticmethod
    def _drain_announcements(lines: queue.Queue[str | None], on_event: Callable[[Event], None]) -> None:
        """Hand over the lease owners and job ids the child announced that were still unread when the run
        was cut short (a cancel is checked after every line; the child may also announce while it stops).
        Stage and progress events are dropped: only what the caller must release or show is passed on."""
        while True:
            try:
                line = lines.get_nowait()
            except queue.Empty:
                return
            event = parse_event(line) if line is not None else None
            if event is not None and event.get("ev") in ("owner", "job"):
                with contextlib.suppress(Exception):
                    on_event(event)

    # --- running ---
    def run(
        self,
        kind: str,
        args: dict[str, Any],
        *,
        on_event: Callable[[Event], None] = lambda _e: None,
        check_cancel: Callable[[], None] = lambda: None,
        on_cancel: Callable[[], None] | None = None,
    ) -> Event:
        """Run `kind` to the end and return its `result` payload.

        `on_event(event)` gets every progress event; `check_cancel()` is called after each line and every
        `poll_s` seconds while the child is quiet, and may raise (the dashboard's `JobCancelled`): the child's
        process tree is killed and the exception passes on.
        When `on_cancel()` is given it runs first and tells the child to stop by itself (the download kind
        cancels the Clipper `CancelToken`, which kills yt-dlp and removes its folder); the tree is killed once
        `cancel_grace_s` has passed without the child ending. A failure in the child becomes `NsError`: with
        its own `NS-` code when it carries a known one, else NS-E327 (also when the child ends without a
        `result` or an `error`).
        """
        args_file = self._args_file(kind, args)
        cancel_file = args_file.with_suffix(".cancel")
        lines: queue.Queue[str | None] = queue.Queue()
        tail: collections.deque[str] = collections.deque(maxlen=TAIL_LINES)
        result: Event | None = None
        failure: Event | None = None
        try:
            proc = subprocess.Popen(
                self.command(kind, args_file),
                cwd=str(self.workdir()),
                env=self.environment(cancel_file),
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
            self._stop(proc, cancel_file, on_cancel)
            reader.join(timeout=5)
            self._drain_announcements(lines, on_event)
            raise
        finally:
            reader.join(timeout=5)
            args_file.unlink(missing_ok=True)
            cancel_file.unlink(missing_ok=True)
        if failure is not None:
            raise failure_error(failure)
        if result is None or proc.returncode != 0:
            last = " | ".join(list(tail)[-5:])
            raise NsError("NS-E327", f"The studio process stopped (exit {proc.returncode}). {last}".strip())
        return {k: v for k, v in result.items() if k != "ev"}
