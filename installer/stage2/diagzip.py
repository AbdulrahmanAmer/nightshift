"""A diagnostics zip the wizard can write by itself (standard library only).

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
from pathlib import Path

from ns_common.paths import Paths

__all__ = ["write_console_install_report", "write_fallback_bundle"]


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
        state = json.loads(state_file.read_text(encoding="utf-8")) if state_file.is_file() else {}
    except (OSError, ValueError):
        state = {}
    steps = state.get("steps") if isinstance(state.get("steps"), dict) else {}
    answers_file = paths.config / "answers.json"
    answers: dict[str, object] = {}
    try:
        if answers_file.is_file():
            raw = json.loads(answers_file.read_text(encoding="utf-8"))
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
        "steps": {sid: {"status": (st or {}).get("status"), "finished_at": (st or {}).get("finished_at"),
                        "error": (st or {}).get("error")} for sid, st in steps.items()},
        "last_error": last_error,
        "answers": answers,
    }
    try:
        paths.logs.mkdir(parents=True, exist_ok=True)
        out = paths.logs / "install-report.json"
        out.write_text(json.dumps(data, indent=2), encoding="utf-8")
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
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for arc, f in members:
            try:
                z.write(f, arc)
            except OSError:
                continue
    home = desktop if desktop is not None else Path.home() / "Desktop"
    if home.is_dir():
        try:
            (home / out.name).write_bytes(out.read_bytes())
        except OSError:
            pass
    return out
