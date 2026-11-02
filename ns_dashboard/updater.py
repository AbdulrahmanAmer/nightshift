"""Update check and apply through the `ns update` CLI of the supervisor venv (component C9).

This is the only dashboard module that starts a subprocess (list arguments, never a shell, with a
timeout). The runner is injectable so tests never start a process.
"""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ns_common.paths import Paths

Runner = Callable[[list[str], int], tuple[int, str, str]]
VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?)\b")
TAIL = 2000


def default_runner(cmd: list[str], timeout_s: int) -> tuple[int, str, str]:
    """Run `cmd` and return (returncode, stdout, stderr); a timeout is reported as returncode 124."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s, encoding="utf-8", errors="replace"
        )
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {timeout_s} s"
    except OSError as e:
        return 127, "", str(e)
    return p.returncode, p.stdout, p.stderr


def parse_update_output(code: int, stdout: str, stderr: str) -> dict[str, Any]:
    """Structured result from `ns update` output: JSON when it prints JSON, else a best-effort read."""
    out: dict[str, Any] = {
        "ok": code == 0,
        "returncode": code,
        "update_available": None,
        "current": None,
        "latest": None,
        "output": stdout.strip()[-TAIL:],
        "error": stderr.strip()[-TAIL:],
    }
    for line in reversed([x.strip() for x in stdout.splitlines() if x.strip()]):
        if line.startswith("{"):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                out.update({k: v for k, v in data.items() if k in ("update_available", "current", "latest", "notes")})
                return out
    low = stdout.lower()
    versions = VERSION_RE.findall(stdout)
    if "up to date" in low or "no update" in low:
        out["update_available"] = False
    elif "available" in low or "new version" in low:
        out["update_available"] = True
    if versions:
        out["latest"] = versions[-1]
        if len(versions) > 1:
            out["current"] = versions[0]
    return out


class RealUpdateAdapter:
    """Runs `ns update --check` and `ns update --github` from the supervisor venv."""

    def __init__(self, paths: Paths, runner: Runner = default_runner) -> None:
        self.paths = paths
        self.runner = runner

    def _command(self, *args: str) -> list[str]:
        exe = self.paths.venvs / "supervisor" / "Scripts" / "ns.exe"
        base = [str(exe)] if exe.exists() else [sys.executable, "-m", "ns_supervisor.cli"]
        return [*base, *args]

    def check(self) -> dict[str, Any]:
        """Is a newer version on GitHub? (`ns update --check`)"""
        return classify_update(parse_update_output(*self.runner(self._command("update", "--check"), 120)))

    def apply(self) -> dict[str, Any]:
        """Download and install the newest version (`ns update --github`); rolls back on failure."""
        return classify_update(parse_update_output(*self.runner(self._command("update", "--github"), 3600)))


def venv_python(paths: Paths) -> Path:
    """Python of the supervisor venv (for callers that need it)."""
    return paths.venvs / "supervisor" / "Scripts" / "python.exe"


MISSING_MARKERS = (
    "no such option", "no such command", "give either a zip", "missing argument", "got unexpected extra",
    "no module named", "is not recognized",
)
UNAVAILABLE_NOTE = (
    "Checking GitHub for updates is not part of this version yet. "
    "Use the Update shortcut on your desktop with the nightshift-app zip you were given."
)


def classify_update(result: dict[str, Any]) -> dict[str, Any]:
    """Add the fields the Settings page needs to a parsed `ns update` result.

    `state` is `up_to_date`, `available`, `unavailable` (the command is missing or does not take the option,
    which is the case until `ns update --check/--github` ships), `error` or `unknown`. `up_to_date` is a bool
    for the page and `message` is a sentence in plain words.
    """
    code = int(result.get("returncode") or 0)
    err = str(result.get("error") or "").lower()
    out = str(result.get("output") or "").lower()
    if code in (2, 127) or (code != 0 and any(m in err or m in out for m in MISSING_MARKERS)):
        state, message = "unavailable", UNAVAILABLE_NOTE
    elif code != 0:
        state, message = "error", (str(result.get("error") or "").strip().splitlines() or ["The update check failed."])[-1]
    elif result.get("update_available") is True:
        state, message = "available", f"Version {result.get('latest') or 'newer'} is ready."
    elif result.get("update_available") is False:
        state, message = "up_to_date", "You have the newest version."
    else:
        state, message = "unknown", "The update check finished but did not say whether a new version exists."
    return {**result, "state": state, "up_to_date": state == "up_to_date", "message": message}
