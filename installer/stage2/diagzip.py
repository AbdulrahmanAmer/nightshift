"""A diagnostics zip the wizard can write by itself (standard library plus two stdlib-only ns_common modules).

``ns doctor --bundle`` lives in the engine's venv, which does not exist before step s04, so a setup that fails
early (the welcome step, the program copy, the setup environment) could not produce the file OPERATOR.md
tells the partner to send. This writes the same evidence the setup window's fallback collects: the installer
logs, ``install_state.json``, ``answers.json`` (never holds keys on disk), the stage 1 hardware check and
the bootstrap logs from ``%LOCALAPPDATA%\\Nightshift``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import zipfile
from functools import partial
from pathlib import Path

from ns_common.atomic import (
    copy_atomic,
    read_text_retry,
    retry_busy,
    sweep_stale_tmp,
    write_text_atomic,
    write_zip_atomic,
)
from ns_common.paths import Paths

__all__ = ["write_console_install_report", "write_fallback_bundle"]


def _voice_warnings(paths: Paths) -> list[str]:
    """The sentences s08 kept in ``config/wizard_flags.json``.

    Read here, not through ``state``, so this module stays standard library plus ``ns_common.atomic`` and
    ``ns_common.paths`` (both standard library only)."""
    try:
        flags = json.loads(read_text_retry(paths.config / "wizard_flags.json"))
        found = flags.get("voice_warnings") if isinstance(flags, dict) else None
    except (OSError, ValueError):
        return []
    return [str(w) for w in found] if isinstance(found, list) else []


def _step_row(st: object) -> dict[str, object]:
    """One step of the report. A step that finished carries no error: what it kept in ``error`` is a note
    (a damaged file setup set aside), and says so under ``note``."""
    row = st if isinstance(st, dict) else {}
    done, text = row.get("status") == "done", row.get("error")
    out: dict[str, object] = {"status": row.get("status"), "finished_at": row.get("finished_at"),
                              "error": None if done else text}
    if done and text:
        out["note"] = text
    return out


def write_console_install_report(
    paths: Paths, *, outcome: str, version: str, last_error: dict[str, object] | None = None,
    when: dt.datetime | None = None,
) -> Path | None:
    """``logs\\install-report.json`` for a console or unattended run (the setup window writes its own).

    Built from ``config\\install_state.json`` (every step with its status and finish time) so the owner gets
    the same file whichever way setup ran; ``None`` when there is no state yet or the folder is not writable.
    """
    state_file = paths.config / "install_state.json"
    try:
        state = json.loads(read_text_retry(state_file)) if state_file.is_file() else {}
    except (OSError, ValueError):
        state = {}
    steps = state.get("steps") if isinstance(state.get("steps"), dict) else {}
    answers_file = paths.config / "answers.json"
    answers: dict[str, object] = {}
    try:
        if answers_file.is_file():
            raw = json.loads(read_text_retry(answers_file))
            if isinstance(raw, dict):
                answers = {k: v for k, v in raw.items() if not str(k).endswith("_api_key")}
    except (OSError, ValueError):
        answers = {}
    data = {
        "schema": 1,
        "written_at": (when or dt.datetime.now()).isoformat(timespec="seconds"),
        "version": version,
        "outcome": outcome,
        "mode": "console",
        "dev_mode": bool(state.get("dev_mode")),
        "profile": state.get("profile"),
        "app_path": state.get("app_path"),
        "started_at": state.get("started_at"),
        "completed_at": state.get("completed_at"),
        "steps": {sid: _step_row(st) for sid, st in steps.items()},
        "last_error": last_error,
        # designed or cloned voices that did not speak in the setup test (s08): the voice, why, the way out
        "voice_warnings": _voice_warnings(paths),
        "answers": answers,
    }
    try:
        paths.logs.mkdir(parents=True, exist_ok=True)
        out = paths.logs / "install-report.json"
        write_text_atomic(out, json.dumps(data, indent=2))
    except OSError:
        return None
    return out


def _bootstrap_dir() -> Path | None:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        return None
    d = Path(base) / "Nightshift"
    return d if d.is_dir() else None


def write_fallback_bundle(
    paths: Paths, *, when: dt.datetime | None = None, desktop: Path | None = None
) -> Path:
    """Write ``logs\\nightshift-diagnostics-<stamp>.zip`` (and a copy on the Desktop when it exists)."""
    stamp = (when or dt.datetime.now()).strftime("%Y%m%d-%H%M")
    paths.logs.mkdir(parents=True, exist_ok=True)
    out = paths.logs / f"nightshift-diagnostics-{stamp}.zip"
    members: list[tuple[str, Path]] = []
    for pattern in ("*.jsonl", "*.json", "*.err"):
        for f in sorted(paths.logs.glob(pattern)):
            members.append((f"logs/{f.name}", f))
    for name in ("install_state.json", "answers.json", "runtime.json", "supervisor.json"):
        f = paths.config / name
        if f.is_file():
            members.append((f"config/{name}", f))
    boot = _bootstrap_dir()
    if boot is not None:
        for f in sorted(boot.rglob("*")):
            small = f.is_file() and f.stat().st_size < 2_000_000
            if small and f.suffix.lower() in (".json", ".jsonl", ".err", ".txt", ".log"):
                members.append((f"bootstrap/{f.relative_to(boot).as_posix()}", f))

    def fill(z: zipfile.ZipFile) -> None:
        for arc, f in members:
            try:
                retry_busy(partial(z.write, f, arc))
            except OSError:
                continue

    sweep_stale_tmp(out.parent)  # a crash in an earlier build left its temporary zip behind
    write_zip_atomic(out, fill)  # built beside the final name: nobody meets a zip that is half written
    home = desktop if desktop is not None else Path.home() / "Desktop"
    if home.is_dir():
        sweep_stale_tmp(home)
        try:
            copy_atomic(out, home / out.name)
        except OSError:
            pass
    return out
