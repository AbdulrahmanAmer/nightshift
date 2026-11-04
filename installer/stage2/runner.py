"""Subprocess access for the installer (component C1).

Steps never call ``subprocess`` themselves: they go through a ``Runner`` so CI can replace it with the
scripted ``FakeRunner`` from ``simulate``. ``tag`` names the logical command (for example ``ns tune``) and
``meta`` carries structured arguments for the fake; the real runner ignores both.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ns_common.avblock import blocked_hint, is_blocked_error


@dataclass(frozen=True)
class RunResult:
    """Outcome of a command: exit code and captured text (empty when output was not captured)."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        """True when the exit code is 0."""
        return self.returncode == 0

    @property
    def text(self) -> str:
        """stdout and stderr joined."""
        return (self.stdout + "\n" + self.stderr).strip()


class Runner(Protocol):
    """Runs one external command."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        capture: bool = True,
        tag: str = "",
        meta: Mapping[str, str] | None = None,
    ) -> RunResult:
        """Run ``argv``; never raises for a non-zero exit (see ``RunResult``)."""
        ...


class SubprocessRunner:
    """The real runner. ``capture=False`` lets the child write straight to the terminal.

    With ``bridge=True`` (the wizard talks JSON lines on its own stdout and reads commands from its stdin)
    children get no stdin and write to this process's stderr instead, so they cannot corrupt the stream.
    """

    def __init__(self, *, bridge: bool = False) -> None:
        self.bridge = bridge

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        capture: bool = True,
        tag: str = "",
        meta: Mapping[str, str] | None = None,
    ) -> RunResult:
        """Run ``argv`` without a shell. Exit code 127: not found, 124: timeout."""
        full_env = {**os.environ, **(env or {})}
        extra: dict[str, object] = {}
        if self.bridge:
            extra["stdin"] = subprocess.DEVNULL
            if not capture:
                extra["stdout"] = sys.stderr
                extra["stderr"] = sys.stderr
        try:
            proc = subprocess.run(
                list(argv),
                cwd=str(cwd) if cwd else None,
                env=full_env,
                capture_output=capture,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
                **extra,  # type: ignore[arg-type]
            )
        except FileNotFoundError as e:
            return RunResult(127, "", str(e))
        except OSError as e:
            if is_blocked_error(e):         # Smart App Control / antivirus refused to start the program
                return RunResult(126, "", f"{blocked_hint(argv[0])} ({e})")
            raise
        except subprocess.TimeoutExpired:
            return RunResult(124, "", f"timed out after {timeout} s")
        return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")
