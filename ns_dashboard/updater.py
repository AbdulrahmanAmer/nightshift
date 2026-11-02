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
NOW_ON_RE = re.compile(r"Now on (\S+) \(was (\S+?)\)")
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
                out.update(
                    {k: v for k, v in data.items() if k in ("update_available", "current", "latest", "notes", "updated", "started", "old", "new")}
                )
                return out
    low = stdout.lower()
    done = NOW_ON_RE.search(stdout)
    if done:  # `ns update --github` prints "Now on X (was Y)" when it finished
        out.update({"updated": True, "new": done.group(1), "old": done.group(2)})
        return out
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
        return ns_command(self.paths, *args)

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
    elif result.get("updated") is True:
        state, message = "updated", f"Now on {result.get('new') or 'the newest version'} (was {result.get('old') or 'older'})."
    elif result.get("started") is True:
        state, message = "started", "The update is running. Nightshift restarts by itself when it is done."
    elif result.get("update_available") is True:
        state, message = "available", f"Version {result.get('latest') or 'newer'} is ready."
    elif result.get("update_available") is False:
        state, message = "up_to_date", "You have the newest version."
    else:
        state, message = "unknown", "The update check finished but did not say whether a new version exists."
    return {**result, "state": state, "up_to_date": state == "up_to_date", "message": message}


def ns_command(paths: Paths, *args: str) -> list[str]:
    """The `ns` command line (`ns.exe` of the supervisor venv, else `python -m ns_supervisor.cli`) plus `args`."""
    exe = paths.venvs / "supervisor" / "Scripts" / "ns.exe"
    base = [str(exe)] if exe.exists() else [sys.executable, "-m", "ns_supervisor.cli"]
    return [*base, *args]


WRITTEN_RE = re.compile(r"Backup written to (.+)")


class RealBackupAdapter:
    """Export / import of settings through `ns backup` and `ns restore`, and the place of UNINSTALL.cmd."""

    def __init__(self, paths: Paths, runner: Runner = default_runner) -> None:
        self.paths = paths
        self.runner = runner

    def export(self) -> Path:
        """`ns backup`: settings, voices, the database and exemplars into a zip on the Desktop; returns its path."""
        from ns_dashboard.errors import ApiError

        code, out, err = self.runner(ns_command(self.paths, "backup"), 600)
        m = WRITTEN_RE.search(out)
        if code != 0 or m is None:
            raise ApiError(500, "backup_failed", "The backup could not be made.", hint=(err or out).strip()[-300:])
        return Path(m.group(1).strip())

    def restore(self, zip_path: Path) -> list[str]:
        """`ns restore <zip>`: puts the settings back (what it replaces is kept in data/backup/pre-restore-*)."""
        from ns_dashboard.errors import ApiError

        code, out, err = self.runner(ns_command(self.paths, "restore", str(zip_path)), 600)
        if code != 0:
            raise ApiError(422, "restore_failed", "That file could not be restored.", hint=(err or out).strip()[-300:])
        return [ln for ln in out.splitlines() if ln.strip()]

    def uninstall_path(self) -> Path:
        """`UNINSTALL.cmd` in the Nightshift app folder (`ns_supervisor.control.app_path`)."""
        from ns_supervisor.control import app_path

        return app_path(self.paths) / "UNINSTALL.cmd"


UPDATE_STATE_FILE = "update_state.json"
UPDATE_STATES = ("running", "done", "failed")


def update_state_path(paths: Paths) -> Path:
    """`<home>/run/update_state.json`, written by the detached updater (`ns update --github`)."""
    return paths.home / "run" / UPDATE_STATE_FILE


def read_update_state(paths: Paths) -> dict[str, Any]:
    """What the detached updater says it is doing: `{state, old, new, message, updated_at}`.

    The updater (which outlives the dashboard it replaces) writes `{"state": "running" | "done" | "failed",
    "old": "0.1.0", "new": "0.1.1", "message": "...", "updated_at": "<ISO time>"}`; the Settings page polls
    this after Install, also across the dashboard restart. No file, or one that cannot be read, is `idle`.
    """
    out: dict[str, Any] = {"state": "idle", "old": None, "new": None, "message": None, "updated_at": None}
    try:
        raw = json.loads(update_state_path(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    if not isinstance(raw, dict) or raw.get("state") not in UPDATE_STATES:
        return out
    for key in ("old", "new", "message", "updated_at"):
        value = raw.get(key)
        out[key] = str(value) if value is not None else None
    out["state"] = raw["state"]
    return out
