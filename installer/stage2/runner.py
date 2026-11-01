"""Subprocess access for the installer (component C1).

Steps never call ``subprocess`` themselves: they go through a ``Runner`` so CI can replace it with the
scripted ``FakeRunner`` from ``simulate``. ``tag`` names the logical command (for example ``ns tune``) and
``meta`` carries structured arguments for the fake; the real runner ignores both.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


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
    """The real runner. ``capture=False`` lets the child write straight to the terminal."""

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
            )
        except FileNotFoundError as e:
            return RunResult(127, "", str(e))
        except subprocess.TimeoutExpired:
            return RunResult(124, "", f"timed out after {timeout} s")
        return RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")
