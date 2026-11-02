"""P1's handle on the wizard process P2 (docs/design/installer-ui.md 1.6, 1.7). Standard library only.

``EngineProcess`` starts ``python -m installer.stage2.wizard --ui-bridge stdio``, reads its JSON-line events
on stdout, sends commands on its stdin, forwards stderr to the console log and reports the exit code.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

EventFn = Callable[[dict[str, Any]], None]
LineFn = Callable[[str], None]
ExitFn = Callable[[int], None]


def wizard_argv(
    python: str, *, app_path: Path, home: Path, answers: Path, preflight: Path, bridge: bool,
    simulate: bool = False, halt_after: str | None = None,
) -> list[str]:
    """The wizard command line (with ``--ui-bridge stdio`` for the window, without it for headless runs)."""
    argv = [
        python, "-X", "utf8", "-m", "installer.stage2.wizard", "--app-path", str(app_path),
        "--home", str(home),
        "--answers", str(answers), "--preflight", str(preflight),
    ]
    if bridge:
        argv += ["--ui-bridge", "stdio"]
    if simulate:
        argv += ["--simulate-downloads", "--no-head-checks"]
    if halt_after:
        argv += ["--halt-after", halt_after]
    return argv


def wizard_env(app_path: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for the wizard: UTF-8 everywhere and the app folder on PYTHONPATH."""
    env = dict(base if base is not None else os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONPATH"] = str(app_path)
    return env


class EngineProcess:
    """One wizard process. Events, stderr lines and the exit code arrive on background threads."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str],
        cwd: Path,
        on_event: EventFn,
        on_line: LineFn,
        on_exit: ExitFn,
        popen: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self._argv = list(argv)
        self._env = dict(env)
        self._cwd = cwd
        self._on_event = on_event
        self._on_line = on_line
        self._on_exit = on_exit
        self._popen = popen
        self._proc: Any = None
        self._readers: list[threading.Thread] = []
        self._lock = threading.Lock()
        self.exit_code: int | None = None

    @property
    def alive(self) -> bool:
        """True while the process runs."""
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        """Start the process and its three reader threads."""
        self._proc = self._popen(
            self._argv, cwd=str(self._cwd), env=self._env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        self._readers = [
            threading.Thread(target=self._read_events, name="engine-events", daemon=True),
            threading.Thread(target=self._read_stderr, name="engine-stderr", daemon=True),
        ]
        for t in self._readers:
            t.start()
        threading.Thread(target=self._wait, name="engine-wait", daemon=True).start()

    def send(self, command: dict[str, Any]) -> bool:
        """Write one command line. False when the process is gone."""
        line = json.dumps(command, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                self._proc.stdin.write(line)
                self._proc.stdin.flush()
            except (OSError, ValueError, AttributeError):
                return False
        return True

    def kill(self) -> None:
        """End the process now (the console is closing)."""
        try:
            self._proc.kill()
        except (OSError, AttributeError):
            pass

    def _read_events(self) -> None:
        try:
            for raw in self._proc.stdout:
                text = raw.strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except ValueError:
                    self._on_line(text)         # not ours: show it in the log
                    continue
                if isinstance(event, dict):
                    self._on_event(event)
        except (OSError, ValueError):
            pass

    def _read_stderr(self) -> None:
        try:
            for raw in self._proc.stderr:
                text = raw.rstrip()
                if text:
                    self._on_line(text)
        except (OSError, ValueError):
            pass

    def _wait(self) -> None:
        code = self._proc.wait()
        self.exit_code = int(code)
        for t in self._readers:
            t.join(timeout=2.0)         # let the last events land before the exit is reported
        self._on_exit(self.exit_code)
