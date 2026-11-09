r"""F-10c: the first start of a version that an OLDER updater installed takes the settings copy itself.

Why: `ns update --rollback` gives the older program back the settings files it wrote (`config_snapshot`,
F-10a), but only an updater that has that code takes the copy. The usual way to update (Settings > Update,
`ns update`, `ns update --github`) runs the OLD version's updater, and 0.1.14's takes no copy; so after
0.1.14 goes to 0.1.15 that way, the first time 0.1.15 re-saved `runtime.json` a rollback would leave 0.1.14
with a file it refuses. The first start after an update runs the NEW version's code, so the new version makes
the copy the older updater did not, before it changes any settings file.

How it notices: the program this process runs (`program_dir()`, the folder `ns_common` was imported from:
`<home>\app` in an install) has a program backup beside it (`<app>.prev`, what every updater leaves) and no
copy that belongs to that backup (`<app>.prev-config.zip` with the backup's fingerprint). Then this is the
first start after an update that took no copy, and the copy is taken now: the settings are still exactly as
the older version (and its updater) left them, because every writer of a settings file calls `guard` first.
A copy made by the updater, by INSTALL.cmd or by a rollback is never replaced.

Where it runs: `guard(path)` is called by every writer of a file in `config_snapshot.FILES` / `TREES`
(`ns_common.config` for runtime.json, install_state.json and the channels; the setup steps, the setup window,
the dashboard, `ns schedule`, `ns restore`; tests/test_first_start_snapshot.py finds them all). The answer is
kept for the process: after the first call it costs one dictionary lookup and no file access. A process that
runs the updater or the rollback calls `settled` instead (it manages the copy itself).

When the copy cannot be made (a file locked, the disk full, the home not writable) the write still goes
ahead: an unwritable copy must not stop the product. One plain line goes to `logs\first_start-<date>.jsonl`,
and `<app>\.ns-first-start.json` records it, so a later start does not take a copy of settings this version
has already changed: `ns update --rollback` then says it has no copy and removes the settings the older
version does not know instead (`ns_common.config_compat`).

Standard library plus `ns_common` modules that are themselves standard library only: the setup window's bare
Python calls it too.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import ns_common.config_snapshot as config_snapshot
from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.logging import NsLogger
from ns_common.paths import repo_root

#: `taken_by` of a copy made here.
TAKEN_BY = "first-start"
#: In the program folder: an earlier start of this program could not make the copy (and went on anyway).
STATE_NAME = ".ns-first-start.json"
#: Written into a finished program folder by every updater, INSTALL.cmd and the rollback (update.APP_MARKER).
APP_MARKER = ".ns-app.done"
LOG_COMPONENT = "first_start"

_decided: set[str] = set()
_lock = threading.RLock()


def program_dir() -> Path:
    r"""The program this process runs: the folder `ns_common` came from (`<home>\app` in an install)."""
    return repo_root()


def backup_of(program: Path) -> Path:
    """`<app>.prev`, the program backup every updater leaves beside the program."""
    return program.with_name(program.name + ".prev")


def install_stamp(program: Path) -> str:
    """Which install of `program` this is: its fingerprint and when its completion marker was written.

    Every updater, INSTALL.cmd and every rollback write the marker again, so a program folder put in place
    again (a second try of a failed update, a roll forward by an older version's rollback) has a new stamp.
    """
    try:
        written = str((program / APP_MARKER).stat().st_mtime_ns)
    except OSError:
        written = "0"
    return f"{config_snapshot.program_fingerprint(program)}@{written}"


def config_of(path: Path) -> Path | None:
    """The settings folder when `path` is a file the settings copy holds (`config_snapshot.FILES`, a yaml in
    one of its `TREES`), else None."""
    p = Path(path)
    if p.parent.name.lower() == "config" and p.name.lower() in config_snapshot.FILES:
        return p.parent
    folder = p.parent
    if (
        folder.parent.name.lower() == "config"
        and folder.name.lower() in config_snapshot.TREES
        and p.suffix.lower() == ".yaml"
    ):
        return folder.parent
    return None


def guard(path: Path) -> None:
    """Call before writing, replacing or deleting `path`. When it is a settings file and this is the first
    start after an update an older updater made, the settings copy is taken first. Never raises."""
    config = config_of(path)
    if config is not None:
        ensure(config)


def _key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _same(a: Path, b: Path) -> bool:
    try:
        return os.path.normcase(str(a.resolve())) == os.path.normcase(str(b.resolve()))
    except OSError:
        return _key(a) == _key(b)


def ensure(config: Path) -> None:
    """The first-start check for the settings folder `config`, once per process (see the module text).

    Never raises: whatever goes wrong is logged and the caller's write goes ahead.
    """
    key = _key(config)
    if key in _decided:
        return
    with _lock:
        if key in _decided:
            return
        try:
            _first_start(Path(config))
        except Exception as e:  # never stops a write: the product must keep working
            _say(Path(config), "WARN", "first_start_check_failed", f"the first-start check failed: {e!r}")
        _decided.add(key)


def settled(config: Path) -> None:
    """This process manages the settings copy itself (`ns update`, `ns update --rollback`): never take one.

    A rollback swaps the programs and only then puts the copy back; a check made in between would take the
    copy of a program that is not the one in `<app>.prev` any more.
    """
    with _lock:
        _decided.add(_key(config))


def reset() -> None:
    """Forget what this process decided (tests: the next call behaves like the first one of a new process)."""
    with _lock:
        _decided.clear()


def _first_start(config: Path) -> None:
    program = program_dir()
    if not _same(program.parent / "config", config):
        return  # the settings of another install, or a checkout that is not installed
    prev = backup_of(program)
    if not prev.is_dir():
        return  # never updated, a git or dev install, or the update was undone
    backup = config_snapshot.program_fingerprint(prev)
    stamp = install_stamp(program)
    zip_path = config_snapshot.path_for(program)
    try:
        snap: config_snapshot.Snapshot | None = config_snapshot.read(zip_path)
    except config_snapshot.SnapshotError:
        snap = None  # none, damaged or unreadable: nothing a rollback could use
    if snap is not None and snap.fingerprint == backup:
        if snap.taken_by != TAKEN_BY or snap.for_program == stamp:
            return  # the updater, INSTALL.cmd, a rollback or an earlier start of this install made it
    state = read_state(program)
    if state.get("backup") == backup and state.get("installed") == stamp:
        # An earlier start of this program could not take the copy and went on: the settings have changed
        # since, so a copy taken now would hand the older version what this one wrote.
        return
    version = config_snapshot.program_version(prev)
    try:
        files = config_snapshot.capture(config)
        data = config_snapshot.build(
            files, version=version, fingerprint=backup, taken_by=TAKEN_BY, for_program=stamp
        )
        config_snapshot.publish(zip_path, data)
    except config_snapshot.SnapshotError as e:
        why = str(e)
        now = config_snapshot.program_version(program)
        _say(
            config,
            "WARN",
            "first_start_copy_failed",
            f"This is the first start of {now} after an update that kept no copy of the settings for "
            f"{version}, and the copy could not be made now ({why}). Nightshift goes on; `ns update "
            f"--rollback` will have no copy and will remove the settings {version} does not know instead.",
            copy=str(zip_path),
        )
        write_state(program, {"backup": backup, "installed": stamp, "outcome": "no copy", "why": why})
        return
    _say(
        config,
        "INFO",
        "first_start_copy_saved",
        f"First start of {config_snapshot.program_version(program)} after an update that kept no copy of "
        f"the settings: saved them as {version} left them, for `ns update --rollback`.",
        files=sorted(files),
        copy=str(zip_path),
    )


def read_state(program: Path) -> dict[str, Any]:
    """`<app>/.ns-first-start.json` (empty when there is none or it cannot be read)."""
    try:
        data = json.loads(read_text_retry(program / STATE_NAME))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(program: Path, data: dict[str, Any]) -> None:
    """Record what the first start did (best effort: a program folder that cannot be written is told in the
    log already)."""
    try:
        record = {**data, "at": datetime.now().isoformat(timespec="seconds")}
        write_text_atomic(program / STATE_NAME, json.dumps(record, indent=2) + "\n")
    except OSError:
        pass


def _say(config: Path, level: str, event: str, msg: str, **data: Any) -> None:
    try:
        log = NsLogger(LOG_COMPONENT, config.parent / "logs")
        if level == "WARN":
            log.warn(event, msg, **data)
        else:
            log.info(event, msg, **data)
    except OSError:
        pass
