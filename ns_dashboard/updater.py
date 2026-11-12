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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ns_common.atomic import read_text_retry
from ns_common.commandline import render
from ns_common.paths import Paths

Runner = Callable[[list[str], int], tuple[int, str, str]]
NOW_ON_RE = re.compile(r"Now on (\S+) \(was (\S+?)\)")
VERSION_RE = re.compile(r"\b(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.]+)?)\b")
CODED_RE = re.compile(r"NS-[EW]\d{3}: ")  # how `ns` starts the line of a coded error
TAIL = 2000


def stream_command(cmd: list[str], on_line: Callable[[str], None]) -> tuple[int, str]:
    """Run `cmd` and hand each output line (stdout and stderr together) to `on_line` as it appears."""
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace"
        )
    except OSError as e:
        return 127, str(e)
    last = ""
    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if line:
            last = line
            on_line(line)
    return proc.wait(), last


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
                    {k: v for k, v in data.items() if k in ("update_available", "current", "latest", "notes", "updated", "started", "old", "new", "release_notes")}
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

    def apply_file(self, zip_path: Path) -> dict[str, Any]:
        """Install an update zip from disk (`ns update <zip>`). The command checks the zip, its `.sha256` and
        (once this version trusts a release key) its `.sig` before it starts the detached worker, so a bad
        package is answered here and nothing is stopped."""
        return classify_update(parse_update_output(*self.runner(self._command("update", str(zip_path)), 3600)))

    def models_available(self) -> bool:
        """True when this `ns` has the `models` command (the installer/supervisor builder adds `ns models get`)."""
        code, out, err = self.runner(self._command("models", "--help"), 60)
        return code == 0 and "no such command" not in (out + err).lower()

    def model_unavailable(self, name: str) -> str | None:
        """`ns_supervisor.models_get.unavailable` on this install's manifest and `runtime.json`.

        Nothing is decided (None) when `runtime.json`, `install_state.json` or `manifest.json` is missing, torn
        or invalid (NsError, OSError, or ValueError, which a pydantic ValidationError and a bad UTF-8 read
        are): the request goes on and `ns models get` reports the problem itself (NS-E203 naming the file, or NS-E123
        when no install is found at all; it never assumes a profile, defect D-35).
        """
        from ns_common.config import load_manifest, load_runtime
        from ns_common.errors import NsError
        from ns_supervisor import models_get
        from ns_supervisor.control import app_path

        try:
            manifest = load_manifest(app_path(self.paths) / "manifest.json")
            rt = load_runtime(self.paths)
        except (NsError, OSError, ValueError):
            return None
        return models_get.unavailable(name, manifest, rt.profile, rt.tts.engine)

    def get_model(self, name: str, on_line: Callable[[str], None] = lambda _l: None) -> tuple[int, str]:
        """`ns models get <name>` (a long download); every output line goes to `on_line`. Returns (exit code, last line)."""
        return stream_command(self._command("models", "get", name), on_line)


def venv_python(paths: Paths) -> Path:
    """Python of the supervisor venv (for callers that need it)."""
    return paths.venvs / "supervisor" / "Scripts" / "python.exe"


MISSING_MARKERS = (
    "no such option", "no such command", "give either a zip", "missing argument", "got unexpected extra",
    "no module named", "is not recognized",
)
UNAVAILABLE_NOTE = (
    "Checking GitHub for updates is not part of this version yet. "
    "Use the Update shortcut on your desktop with the Nightshift-Update zip you were given."
)


def error_line(stderr: str) -> str:
    """The line of `ns` stderr that says what went wrong: `NS-E118: ...`, not the `Hint:` line after it.

    The CLI prints `<code>: <message> (<detail>)` and then `Hint: <the code's generic advice>` (`cli._die`;
    an older form put `error ` in front). The last line used to be shown, so a GitHub rate limit read "Check
    the internet connection" and a refused package (`NS-E701`, `NS-E707`) only its hint, without which of
    its reasons it was (card update-signing).
    """
    lines = [x.strip() for x in stderr.strip().splitlines() if x.strip()]
    for line in lines:
        if line.lower().startswith("error "):
            return line[6:].strip()
        if CODED_RE.match(line):
            return line
    return lines[-1] if lines else "The update check failed."


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
        state, message = "error", error_line(str(result.get("error") or ""))
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
    notes = result.get("notes")
    if isinstance(notes, list):         # an update's JSON is not rendered by `ns`: its commands are, here (FN-3)
        result = {**result, "notes": [render(n) if isinstance(n, str) else n for n in notes]}
    return {**result, "state": state, "up_to_date": state == "up_to_date", "message": render(message)}


def ns_command(paths: Paths, *args: str) -> list[str]:
    """The `ns` command line (`ns.exe` of the supervisor venv, else `python -m ns_supervisor.cli`) plus `args`."""
    exe = paths.venvs / "supervisor" / "Scripts" / "ns.exe"
    base = [str(exe)] if exe.exists() else [sys.executable, "-m", "ns_supervisor.cli"]
    return [*base, *args]


WRITTEN_RE = re.compile(r"Backup written to (.+)")


def failure_hint(text: str, limit: int = 400) -> str:
    """What the owner reads when `ns backup` / `ns restore` failed.

    A coded failure (`NS-E120: message` then `Hint: ...`) is kept from its first character, so the code and
    the message survive and only a very long hint is cut. Anything else (a traceback) keeps its last
    characters, where the error is.
    """
    text = text.strip()
    if re.match(r"NS-[EW]\d+", text):
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "..."
    return text[-300:]


class RealBackupAdapter:
    """Export / import of settings through `ns backup` and `ns restore`, and the place of UNINSTALL.cmd."""

    def __init__(self, paths: Paths, runner: Runner = default_runner) -> None:
        self.paths = paths
        self.runner = runner
        #: the lines `ns backup` printed after `Backup written to`: what it left out on purpose (the keys file,
        #: for a zip on a drive without permissions, NS-W116); Settings > Export shows them
        self.last_notes: list[str] = []

    def export(self) -> Path:
        """`ns backup`: settings, voices, the database and exemplars into a zip on the Desktop; returns its path."""
        from ns_dashboard.errors import ApiError

        self.last_notes = []
        code, out, err = self.runner(ns_command(self.paths, "backup"), 600)
        m = WRITTEN_RE.search(out)
        if code != 0 or m is None:
            raise ApiError(500, "backup_failed", "The backup could not be made.", hint=failure_hint(err or out))
        self.last_notes = [ln.strip() for ln in out.splitlines() if ln.strip() and WRITTEN_RE.search(ln) is None]
        return Path(m.group(1).strip())

    def restore(self, zip_path: Path) -> list[str]:
        """`ns restore <zip>`: puts the settings back (what it replaces is kept in data/backup/pre-restore-*)."""
        from ns_dashboard.errors import ApiError

        code, out, err = self.runner(ns_command(self.paths, "restore", str(zip_path)), 600)
        if code != 0:
            raise ApiError(422, "restore_failed", "That file could not be restored.", hint=failure_hint(err or out))
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


#: A `running` state that has not changed for this long, with no updater process behind it, is not running.
STALE_AFTER = timedelta(minutes=30)
STALE_MESSAGE = "The update stopped without finishing. Nothing was lost; see the newest update-....log in the logs folder."


def read_update_state(
    paths: Paths,
    *,
    alive: Callable[[int], bool] | None = None,
    now: Callable[[], datetime] = datetime.now,
) -> dict[str, Any]:
    """What the detached updater says it is doing: `{state, old, new, message, updated_at}`.

    The updater (which outlives the dashboard it replaces) writes `{"state": "running" | "done" | "failed",
    "old": "0.1.0", "new": "0.1.1", "message": "...", "updated_at": "<ISO time>"}`; the Settings page polls
    this after Install, also across the dashboard restart. No file, or one that cannot be read, is `idle`.

    A `running` state whose updater process is gone (the PC lost power, the worker was killed; the supervisor's
    `effective_status` decides, with `alive` and `now` for tests) is reported as `failed` with `stale: true`, so
    the page does not show "Installing the update" for ever. Without `ns_supervisor`, a state untouched for
    30 minutes counts as gone.
    """
    out: dict[str, Any] = {"state": "idle", "old": None, "new": None, "message": None, "updated_at": None}
    try:
        raw = json.loads(read_text_retry(update_state_path(paths)))  # the updater swaps the file in under us
    except (OSError, ValueError):
        return out
    if not isinstance(raw, dict) or raw.get("state") not in UPDATE_STATES:
        return out
    was_running = raw["state"] == "running"
    try:
        from ns_supervisor.update_job import effective_status, settled, where

        # `running` with a dead worker is `failed`, said by the step it died in or read from the disk; one
        # whose environments were finished since (D-48: nothing is left to do) is `done`, said as such
        raw = settled(paths, effective_status(raw, alive=alive, now=now(), describe=lambda: where(paths)))
    except ImportError:
        if was_running and _stale(raw, now):
            raw = {**raw, "state": "failed", "message": STALE_MESSAGE}
    for key in ("old", "new", "message", "updated_at"):
        value = raw.get(key)
        out[key] = str(value) if value is not None else None
    if out["message"]:      # the page shows it now: stored with the plain words, rendered here (FN-3)
        out["message"] = render(out["message"], paths.home)
    out["state"] = raw["state"]
    if isinstance(raw.get("release_notes"), str) and raw["release_notes"]:
        out["release_notes"] = raw["release_notes"]       # added only when there is some: the shape is unchanged
    if was_running and out["state"] == "failed":
        out["stale"] = True
    return out


def _stale(raw: dict[str, Any], now: Callable[[], datetime]) -> bool:
    """Without the supervisor package: a `running` state untouched for `STALE_AFTER` has no updater behind it."""
    try:
        touched = datetime.fromisoformat(str(raw.get("updated_at")))
    except ValueError:
        return False
    if touched.tzinfo is not None:
        touched = touched.astimezone().replace(tzinfo=None)
    return now() - touched > STALE_AFTER
