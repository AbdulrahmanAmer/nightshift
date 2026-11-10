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
A copy made for the backup by the updater, by INSTALL.cmd or by a rollback is never replaced, and the zip in
the copy's place is written over only by the rules an update follows (`config_snapshot.before_update`): a
missing or damaged one, this program's own copy (for going forward again: it runs now), a first-start copy
an earlier install of this program took; never a whole copy of another program (it may be the only copy of
its settings), one a rollback could not put back, or one that cannot be read.

Where it runs: `guard(path)` is called by every writer of a file in `config_snapshot.FILES` / `TREES`
(`ns_common.config` for runtime.json, install_state.json and the channels; the setup steps, the setup window,
the dashboard, `ns schedule`, `ns restore`; tests/test_first_start_snapshot.py finds them all). The answer is
kept for the process: after the first call it costs one dictionary lookup and no file access. A process that
runs the updater or the rollback calls `settled` instead (it manages the copy itself).

When the copy cannot be made (a file locked, the disk full, the home not writable, a zip in its place that
must not be written over) the write still goes ahead: an unwritable copy must not stop the product. One
plain line goes to `logs\first_start-<date>.jsonl`, and `<app>\.ns-first-start.json` records it (or, when the
program folder cannot be written, `config\.ns-first-start.json`), so a later start does not take a copy of
settings this version has already changed: `ns update --rollback` then says it has no copy and removes the
settings the older version does not know instead (`ns_common.config_compat`). A later start also finds the
log line; only when neither record nor log line could be written does the log say that a later start cannot
tell.

Two processes may start at once (the engine and a CLI): the whole decision, from looking at the copy's place
to publishing it, runs while holding `<app>.prev-config.zip.lock` (`config_snapshot.locked`), so the second
process looks again after the first one has published, and leaves that copy alone.

The same check finishes or undoes a rollback that was interrupted (`ns_common.rollback_intent`): under the
same lock, a folder `<app>.swap` left in the middle of the renames is given its place first (`settle_swap`),
then the record `<app>.rollback-pending.json` is read, before anything else.

Standard library plus `ns_common` modules that are themselves standard library only: the setup window's bare
Python calls it too.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import ns_common.config_snapshot as config_snapshot
import ns_common.rollback_intent as rollback_intent
from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.logging import NsLogger
from ns_common.paths import repo_root

#: `taken_by` of a copy made here.
TAKEN_BY = "first-start"
#: In the program folder: an earlier start of this program could not make the copy (and went on anyway).
STATE_NAME = ".ns-first-start.json"
#: Written into a finished program folder by every updater, INSTALL.cmd and the rollback (update.APP_MARKER).
APP_MARKER = config_snapshot.APP_MARKER
LOG_COMPONENT = "first_start"
#: The log event for what `rollback_intent.settle_swap` said (here and in the update log; `ns doctor` shows
#: the newest one).
SWAP_EVENT = "rollback_swap_folder"

#: After this long behind another program's lock, a caller that asked to be told is told (`ensure(on_wait=)`).
WAIT_NOTICE_S = 2.0

_decided: set[str] = set()
_lock = threading.RLock()
#: For the one `ensure` call that runs the check (set and cleared under `_lock`): where every sentence of the
#: check also goes as (event, sentence), and what is called once when the check has to wait for the lock.
_told: list[tuple[str, str]] | None = None
_on_wait: Callable[[], None] | None = None


def program_dir() -> Path:
    r"""The program this process runs: the folder `ns_common` came from (`<home>\app` in an install)."""
    return repo_root()


def backup_of(program: Path) -> Path:
    """`<app>.prev`, the program backup every updater leaves beside the program."""
    return program.with_name(program.name + ".prev")


def install_stamp(program: Path) -> str:
    """Which install of `program` this is (`config_snapshot.install_stamp`: fingerprint and marker time)."""
    return config_snapshot.install_stamp(program)


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


def ensure(
    config: Path,
    *,
    told: list[tuple[str, str]] | None = None,
    on_wait: Callable[[], None] | None = None,
) -> None:
    """The first-start check for the settings folder `config`, once per process (see the module text).

    Never raises: whatever goes wrong is logged and the caller's write goes ahead. A caller that shows the
    owner what happened (`ns doctor`) passes `told`: every sentence this call logs is appended to it as
    (event, sentence), nothing when the check already ran in this process; and `on_wait`, called once when
    the check has waited `WAIT_NOTICE_S` for another program that holds the copy's lock (it then waits on,
    `config_snapshot.LOCK_WAIT_S` in all).
    """
    global _told, _on_wait
    key = _key(config)
    if key in _decided:
        return
    with _lock:
        if key in _decided:
            return
        _told, _on_wait = told, on_wait
        try:
            _first_start(Path(config))
        except Exception as e:  # never stops a write: the product must keep working
            _say(Path(config), "WARN", "first_start_check_failed", f"the first-start check failed: {e!r}")
        finally:
            _told, _on_wait = None, None
        _decided.add(key)


@contextlib.contextmanager
def _copy_lock(zip_path: Path) -> Iterator[bool]:
    """`config_snapshot.locked`, telling `_on_wait` once when another program keeps the lock."""
    hook = _on_wait
    if hook is not None:
        with config_snapshot.locked(zip_path, wait_s=WAIT_NOTICE_S) as held:
            if held:
                yield True
                return
        hook()
    wait = None if hook is None else max(config_snapshot.LOCK_WAIT_S - WAIT_NOTICE_S, 0.0)
    with config_snapshot.locked(zip_path, wait_s=wait) as held:
        yield held


def settled(config: Path) -> None:
    """This process manages the settings copy itself (`ns update`, `ns update --rollback`): never take one.

    A rollback changes the settings before it swaps the programs and the copy after; a check made in between
    would take a copy of settings that are being changed. It finishes or undoes an interrupted rollback
    itself (`rollback_intent.recover`, under the copy's lock).
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
    pending = rollback_intent.path_for(program).exists()
    swap = rollback_intent.swap_waits(program)  # a rollback was interrupted in the middle of its renames
    if not pending and not swap and not backup_of(program).is_dir():
        return  # never updated, a git or dev install, or the update was undone
    zip_path = config_snapshot.path_for(program)
    with _copy_lock(zip_path) as held:
        if not held:
            _say(
                config,
                "WARN",
                "first_start_lock_busy",
                "Another Nightshift program kept the settings copy busy for "
                f"{config_snapshot.LOCK_WAIT_S:.0f} s (a rollback or another first start); this one did not "
                "look at the copy and goes on. If a rollback is running, this program's settings write lands "
                "in the settings the rollback is changing: close Nightshift windows before ns update "
                "--rollback.",
                copy=str(zip_path),
            )
            return
        if swap:  # before the record is read: `recover` then meets folders something looks at
            said = rollback_intent.settle_swap(program)
            if said:  # once per process (`ensure`); a folder that stays is asked about again at the next
                # start, and `ns doctor` (row "program folders") names it with the way out
                _say(
                    config,
                    "WARN",
                    SWAP_EVENT,
                    said,
                    folder=str(rollback_intent.swap_for(program)),
                    settled=not rollback_intent.swap_waits(program),
                )
        if pending:
            rec = rollback_intent.recover(program, config)
            said = rec.sentence
            if rec.pending:  # the settings may be half put back: no copy is taken of them
                _say(
                    config,
                    "WARN",
                    "rollback_recovery_failed",
                    said,
                    record=str(rollback_intent.path_for(program)),
                )
                return
            if said:
                _say(
                    config, "WARN", "rollback_recovered", said, record=str(rollback_intent.path_for(program))
                )
        if backup_of(program).is_dir():
            _take_copy(program, config)  # looked at again inside the lock: another process may have made it


def _take_copy(program: Path, config: Path) -> None:
    prev = backup_of(program)
    backup = config_snapshot.program_fingerprint(prev)
    stamp = install_stamp(program)
    zip_path = config_snapshot.path_for(program)
    slot = config_snapshot.look(zip_path)
    snap = slot.copy
    unrestored = snap is not None and config_snapshot.is_unrestored(zip_path)
    if snap is not None and snap.fingerprint == backup:
        if snap.taken_by != TAKEN_BY or snap.for_program == stamp or unrestored:
            # the backup's own copy: made by the updater, INSTALL.cmd, a rollback or an earlier start of this
            # install, or one a rollback could not put back (then the settings now may be a newer version's)
            return
    state = read_state(program, config)
    if (state.get("backup") == backup and state.get("installed") == stamp) or _failed_in_log(
        config, backup, stamp
    ):
        # An earlier start of this program could not take the copy and went on: the settings have changed
        # since, so a copy taken now would hand the older version what this one wrote.
        return
    version = config_snapshot.program_version(prev)
    now = config_snapshot.program_version(program)
    running = config_snapshot.program_fingerprint(program)
    why = _left_alone(slot, zip_path, unrestored, backup=backup, running=running)
    try:
        if why:
            raise config_snapshot.SnapshotError(why)
        files = config_snapshot.capture(config)
        data = config_snapshot.build(
            files, version=version, fingerprint=backup, taken_by=TAKEN_BY, for_program=stamp
        )
        config_snapshot.publish(zip_path, data)
    except config_snapshot.SnapshotError as e:
        why = str(e)
        record = {"backup": backup, "installed": stamp, "outcome": "no copy", "why": why}
        where, problems = write_state(program, config, record)
        if where is not None:
            remembered = (
                f" This is recorded in {where}, so no later start takes a copy of settings {now} changed."
            )
        else:
            remembered = (
                f" It could not be recorded either ({'; '.join(problems)}): a later start finds only this "
                f"log line; if this line is missing too, a later start may save settings {now} has already "
                f"changed as {version}'s copy."
            )
        _say(
            config,
            "WARN",
            "first_start_copy_failed",
            f"This is the first start of {now} after an update that kept no copy of the settings for "
            f"{version}, and no copy was made now: {why}. Nightshift goes on; `ns update --rollback` will "
            f"have no copy and will remove the settings {version} does not know instead.{remembered}",
            copy=str(zip_path),
            backup=backup,
            installed=stamp,
        )
        return
    replaced = f" (the damaged copy that was there was replaced: {slot.problem})" if slot.damaged else ""
    _say(
        config,
        "INFO",
        "first_start_copy_saved",
        f"First start of {now} after an update that kept no copy of the settings: saved them as {version} "
        f"left them, for `ns update --rollback`{replaced}.",
        files=sorted(files),
        copy=str(zip_path),
    )


def _left_alone(
    slot: config_snapshot.Slot, zip_path: Path, unrestored: bool, *, backup: str, running: str
) -> str:
    """Why the zip in the copy's place must not be written over (empty: it may be). The same rules as an
    update (`config_snapshot.before_update`): a missing or damaged zip may be replaced; so may this program's
    own copy (the one for going forward again: it runs now) and a first-start copy of the backup that an
    earlier install of this program took (a failed update that was tried again: the settings now are the
    backup's own), unless a rollback recorded that it could not put that copy back. A whole copy of any other
    program may be the only copy of its settings, and a zip that cannot be read may still be whole."""
    snap = slot.copy
    if snap is None:
        if slot.free:
            return ""
        return f"the settings copy {zip_path} could not be read ({slot.problem}) and was left as it is"
    if unrestored:
        return (
            f"the settings copy for {snap.version} in {zip_path} is one a rollback could not put back, and "
            "it stays as that version's copy"
        )
    if snap.fingerprint in (backup, running):
        return ""
    return config_snapshot.foreign_note(zip_path, snap, leaving=running, backup=backup)


def _state_places(program: Path, config: Path | None) -> list[Path]:
    """Where the record goes: the program folder (it goes with the program), else the settings folder."""
    return [program / STATE_NAME, (program.parent / "config" if config is None else config) / STATE_NAME]


def read_state(program: Path, config: Path | None = None) -> dict[str, Any]:
    """The first start's record for `program` (`<app>/.ns-first-start.json`, else the one in the settings
    folder that names the same install); empty when there is none or it cannot be read."""
    first: dict[str, Any] = {}
    for i, place in enumerate(_state_places(program, config)):
        try:
            data = json.loads(read_text_retry(place))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        if i == 0:
            first = data
            if data.get("installed") == install_stamp(program):
                return data
        elif data.get("installed") == install_stamp(program):
            return data
    return first


def write_state(program: Path, config: Path | None, data: dict[str, Any]) -> tuple[Path | None, list[str]]:
    """Record what the first start did: in the program folder, else in the settings folder. Returns (where
    it was written, None when nowhere; why each place failed)."""
    record = {**data, "at": datetime.now().isoformat(timespec="seconds")}
    problems: list[str] = []
    for place in _state_places(program, config):
        try:
            write_text_atomic(place, json.dumps(record, indent=2) + "\n")
            return place, problems
        except OSError as e:
            problems.append(f"{place}: {e}")
    return None, problems


def _failed_in_log(config: Path, backup: str, stamp: str) -> bool:
    """True when the first-start log says this install of the program already could not take the copy (the
    record of `write_state` could be written nowhere). Only read on the way to taking a copy."""
    try:
        logs = sorted((config.parent / "logs").glob(f"{LOG_COMPONENT}-*.jsonl"))
    except OSError:
        return False
    for f in logs:
        try:
            lines = read_text_retry(f).splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            data = rec.get("data") if isinstance(rec, dict) else None
            if (
                rec.get("event") == "first_start_copy_failed"
                and isinstance(data, dict)
                and data.get("backup") == backup
                and data.get("installed") == stamp
            ):
                return True
    return False


def _say(config: Path, level: str, event: str, msg: str, **data: Any) -> None:
    if _told is not None:
        _told.append((event, msg))
    try:
        log = NsLogger(LOG_COMPONENT, config.parent / "logs")
        if level == "WARN":
            log.warn(event, msg, **data)
        else:
            log.info(event, msg, **data)
    except OSError:
        pass
