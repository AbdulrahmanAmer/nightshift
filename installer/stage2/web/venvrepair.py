"""D-39b: an environment an update, rollback or resync left half-filled is found and finished at the next
start.

The repair record ``<home>/run/venv-repair.json`` holds the uv command lines of a re-sync, written by the
update worker BEFORE it changes the program (``ns_supervisor.update``: before the first rename of an update
or a rollback, after the checkout of ``--git``) and deleted after the last venv. While it is there, the launch
script (``installer/launch.ps1``, beside the program folder, no Python) replays it before any shortcut or the
nightly task starts anything, holding ``run/update.lock`` as every updater and setup do. It works whatever
program is in ``<app>`` then (0.1.14 has no repair code; the script needs none).

A record belongs to one program: ``for_app`` is that program's fingerprint, the text of its ``.ns-app.done``
marker (``match`` = ``marker``); a cloned-repo install has no marker and is matched by its VERSION
(``version``). A record of another program (a later INSTALL.cmd or update) is stale and is deleted.

An environment left mixed by an updater that writes no record (0.1.14, the 0.1.15 candidates up to rc3) is
found by what it holds (``mixed``): the version of its ``nightshift-*.dist-info`` against the program's
VERSION, or two such folders. Its commands are not known, so it is said, with INSTALL.cmd's Repair as the way
out (``ns doctor``, setup's checks, the launch script).

The record's life (D-39b review @ e2d2bb0, blocker 1). It gates every start, so a record left behind by a
clear that failed would refuse every start, the update's own ``ns verify --quick`` included, and a good
update would be rolled back. Every writer and clearer holds ``run/update.lock`` (the update worker, setup's
s04, the launch script), so no two of them act at once; readers take no lock.

* write (``write``): a temp file swapped in (``write_text_atomic``): a reader sees the old record, the new
  one or none, never a part. Each record carries a fresh ``id``.
* clear (``clear``): the delete is retried for up to ``CLEAR_WAIT_S`` while Windows refuses it. What refuses
  a delete is a program that has the file open without delete sharing (Python's ``open``, PowerShell's
  ``ReadAllText``, a virus scanner), a read-only attribute, or an access rule on the file. None of those
  stops a NEW file beside it, so a delete that still fails writes the sibling ``venv-repair.done`` holding
  the record's ``id``: the record is then FINISHED, and every reader takes it as "nothing pending". (Writing
  the finished state INTO the record would be refused by the same open file: a swap needs delete access to
  the target.) Only when that write fails too (a read-only ``run`` folder, a full disk) does ``clear`` raise,
  and the caller stops with a sentence that names the file and the way out. Killed between the failed delete
  and the marker: the record stays unfinished, which is the state of a sync killed just before its clear
  (the next start replays it: uv finds nothing to do).
* read (``read``): a record that cannot be read at this instant (held without read sharing, replaced right
  then) is read again for up to ``READ_WAIT_S`` before it counts as unreadable; a finished record and a
  record deleted between the look and the read are None. A leftover marker never matches a newer record
  (another ``id``), and the next writer removes it.

Standard library only: the setup server and the wizard read it too.
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import installer.stage2.shortcut_owner as shortcut_owner
from installer.stage2.web.deplock import version_key
from ns_common.atomic import read_text_retry, write_text_atomic

RECORD_NAME = "venv-repair.json"
#: The sibling that says the record of this ``id`` is finished (its delete was refused).
DONE_NAME = "venv-repair.done"
#: How long a refused delete of the record is retried (a scanner or a reader lets go within seconds).
CLEAR_WAIT_S = 15.0
#: How long a record that cannot be read at this instant is read again before it counts as unreadable.
READ_WAIT_S = 2.0
SCHEMA = 1
#: The launch script format that replays a record (``installer/launch.ps1``: ``# STUB_SCHEMA``).
REPLAY_SCHEMA = 5
VENVS = ("studio", "supervisor", "tts")
MARKER = ".ns-app.done"
CODE = "NS-W210"


def record_path(home: Path) -> Path:
    """``<home>/run/venv-repair.json``."""
    return Path(home) / "run" / RECORD_NAME


def write(
    home: Path, *, app: Path, for_app: str, version: str, uv: str, env: Mapping[str, str],
    commands: Sequence[tuple[str, Sequence[str]]], by: str, match: str = "marker",
) -> None:
    """Write the record atomically with a fresh ``id`` (``OSError`` when it cannot be: the caller refuses
    before any change). A finished-marker of an earlier record is removed after it (it names another id, so
    it never matched this one: a removal that fails changes nothing)."""
    data: dict[str, Any] = {
        "schema": SCHEMA, "id": uuid.uuid4().hex, "app": str(app), "match": match, "for_app": for_app,
        "version": version, "by": by,
        "written": datetime.now().isoformat(timespec="seconds"), "uv": uv, "env": dict(env),
        "commands": [{"venv": venv, "argv": [str(a) for a in argv]} for venv, argv in commands],
    }
    target = record_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(target, json.dumps(data, indent=2) + "\n")
    _forget_done(home)


def done_path(home: Path) -> Path:
    """``<home>/run/venv-repair.done``: the id of a record that is finished, delete refused."""
    return Path(home) / "run" / DONE_NAME


def _forget_done(home: Path) -> None:
    try:
        done_path(home).unlink(missing_ok=True)
    except OSError:
        pass        # (it names an id no record will have again)


def _read_json(path: Path, wait_s: float) -> Any:
    """The JSON in ``path``, read again for up to ``wait_s`` while it cannot be read at this instant (held,
    swapped right then). ``FileNotFoundError`` when it is gone; the last ``OSError``/``ValueError`` after
    the wait."""
    deadline = time.monotonic() + wait_s
    while True:
        try:
            return json.loads(read_text_retry(path))
        except FileNotFoundError:
            raise
        except (OSError, ValueError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.1)


def finished(home: Path, record: Mapping[str, Any]) -> bool:
    """True when the sibling ``venv-repair.done`` names this record's ``id``: its sync was finished and only
    its delete was refused. A record without an id is never finished this way."""
    rid = str(record.get("id") or "")
    if not rid:
        return False
    try:
        done = _read_json(done_path(home), READ_WAIT_S)
    except (OSError, ValueError):
        return False
    return isinstance(done, dict) and str(done.get("id") or "") == rid


def read_raw(home: Path, wait_s: float = READ_WAIT_S) -> dict[str, Any] | None:
    """The record as it is on disk, finished or not; None when there is none. ``ValueError`` when it is
    there and still cannot be read whole after ``wait_s``."""
    path = record_path(home)
    if not path.is_file():
        return None
    try:
        data = _read_json(path, wait_s)
    except FileNotFoundError:
        return None         # deleted between the look and the read: nothing waits
    except (OSError, ValueError) as e:
        raise ValueError(f"{path} cannot be read ({e})") from e
    if not isinstance(data, dict) or not isinstance(data.get("commands"), list):
        raise ValueError(f"{path} is not a repair record")
    return data


def read(home: Path) -> dict[str, Any] | None:
    """The record that still waits; None when there is none or it is finished (``finished``). ``ValueError``
    when it is there and cannot be read whole, even after ``READ_WAIT_S``. Every Python reader comes here
    (the start gate, setup's checks, ``ns doctor``, the updater)."""
    data = read_raw(home)
    if data is None or finished(home, data):
        return None
    return data


def _unlink_waiting(path: Path, wait_s: float) -> None:
    """Delete ``path`` (gone already counts), retried while Windows refuses it, for up to ``wait_s``."""
    deadline = time.monotonic() + wait_s
    pause = 0.01
    while True:
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(pause)
            pause = min(pause * 2, 0.25)


def clear(home: Path, wait_s: float | None = None) -> str:
    """The record goes: ``"deleted"`` (or there was none), or ``"marked"`` when its delete stayed refused
    for ``wait_s`` (default ``CLEAR_WAIT_S``) and ``venv-repair.done`` now says it is finished. ``OSError``
    when neither could be done: the record still waits, and the caller must not go on as if it did not
    (see the module docstring: the record's life)."""
    path = record_path(home)
    try:
        raw: dict[str, Any] | None = read_raw(home)
    except ValueError:
        raw = None          # (no id to mark: only the delete can end it)
    try:
        _unlink_waiting(path, CLEAR_WAIT_S if wait_s is None else wait_s)
    except OSError as e:
        rid = str((raw or {}).get("id") or "")
        if not rid:
            raise OSError(f"{path} could not be deleted ({e}), and it has no id to mark it finished") from e
        try:
            write_text_atomic(
                done_path(home),
                json.dumps({"id": rid, "finished": datetime.now().isoformat(timespec="seconds"),
                            "delete_refused": str(e)}, indent=2) + "\n",
            )
        except OSError as e2:
            raise OSError(
                f"{path} could not be deleted ({e}) nor marked finished in {done_path(home)} ({e2})"
            ) from e2
        return "marked"
    _forget_done(home)
    return "deleted"


def left_sentence(home: Path, app: Path, problem: str) -> str:
    """What the owner reads when a finished record could neither be deleted nor marked finished: every
    start refuses until it is gone, so it is named, with the way out."""
    shortcut = (
        "open Nightshift from a Desktop shortcut: it removes the record first. If that does not work, "
        if stub_ready(home) else ""
    )
    return (
        f"Its Python environments are filled, but the repair record {record_path(home)} could not be "
        f"removed ({problem}), and every start of Nightshift refuses while it is there ({CODE}). Close every "
        f"Nightshift window, then {shortcut}restart the PC and run INSTALL.cmd from the Nightshift "
        f"{program_version(app) or 'unknown version'} zip and choose this folder (Repair)."
    )


def update_running(home: Path) -> bool:
    """True while another process holds ``run/update.lock`` (an update, rollback, setup or the launch
    script's replay): byte 0 is locked then, and reading it is refused. It only looks: taking the lock, even
    for a moment, could make a starting update refuse itself. Off Windows: False (nothing to look at)."""
    return _held(Path(home) / "run" / "update.lock")


def engine_running(home: Path) -> bool:
    """True while an engine of this home lives: it holds ``run/supervisor.lock`` from its start to its last
    instant (``ns_supervisor.app.main``). Looks only, like ``update_running``."""
    return _held(Path(home) / "run" / "supervisor.lock")


def _held(lock: Path) -> bool:
    if sys.platform != "win32":
        return False
    try:
        with open(lock, "rb") as fh:
            fh.read(1)
    except PermissionError:
        return True         # (a lock violation: another process holds byte 0)
    except OSError:
        return False
    return False


def program_version(app: Path) -> str:
    """The VERSION file of the program folder, "" when it has none."""
    try:
        return (Path(app) / "VERSION").read_text(encoding="utf-8-sig").strip()
    except OSError:
        return ""


def _marker(app: Path) -> str | None:
    try:
        return (Path(app) / MARKER).read_text(encoding="utf-8-sig").strip()
    except OSError:
        return None


def _same(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def matches(record: Mapping[str, Any], app: Path) -> bool:
    """True when the record was written for the program now in ``app`` (the launch script's rule too)."""
    if not _same(str(record.get("app") or ""), app):
        return False
    marker = _marker(app) if record.get("match") == "marker" else None
    if marker is not None:
        return marker == str(record.get("for_app") or "")
    return bool(record.get("version")) and version_key(program_version(app)) == version_key(
        str(record.get("version"))
    )


def venv_names(record: Mapping[str, Any]) -> list[str]:
    """The environments a record refreshes, in its order."""
    names: list[str] = []
    for c in record.get("commands") or []:
        name = str(c.get("venv") or "") if isinstance(c, dict) else ""
        if name and name not in names:
            names.append(name)
    return names


def installed(home: Path, venv: str) -> list[str] | None:
    """The versions of the ``nightshift`` package installed in ``<home>/venvs/<venv>`` (one, unless a sync
    broke off); None when that venv has no interpreter."""
    folder = Path(home) / "venvs" / venv
    if not (folder / "Scripts" / "python.exe").exists():
        return None
    found: list[str] = []
    for info in sorted((folder / "Lib" / "site-packages").glob("nightshift-*.dist-info")):
        version = info.name[len("nightshift-"):-len(".dist-info")]
        try:
            for line in (info / "METADATA").read_text(encoding="utf-8", errors="replace").splitlines():
                if line.lower().startswith("version:"):
                    version = line.split(":", 1)[1].strip()
                    break
        except OSError:
            pass
        found.append(version)
    return found


def mixed(home: Path, app: Path, venvs: Iterable[str] = VENVS) -> list[tuple[str, list[str]]]:
    """Each venv whose installed Nightshift is not the program in ``app`` (another version, or two of them).
    A venv without an interpreter or without any Nightshift package is left to the callers' own checks."""
    want = version_key(program_version(app))
    out: list[tuple[str, list[str]]] = []
    for venv in venvs:
        have = installed(home, venv)
        if have and (len(have) > 1 or version_key(have[0]) != want):
            out.append((venv, have))
    return out


def mixed_sentence(found: Sequence[tuple[str, list[str]]], app: Path) -> str:
    """The words for ``mixed`` (card D-39 4b.5, S5): the same in ``ns doctor`` and the launch script."""
    here = program_version(app) or "unknown version"
    parts = [f"The Python environment '{venv}' holds Nightshift {' and '.join(have)}" for venv, have in found]
    return (
        f"{'; '.join(parts)}, but the program is {here} (an update by an older version was interrupted). "
        f"Run INSTALL.cmd from the Nightshift {here} zip and choose this folder (Repair)."
    )


def unfinished(home: Path, app: Path, venvs: Iterable[str] = VENVS) -> str | None:
    """Why setup must fill the environments again although its own flags say they are done: a repair record
    is there (readable or not, for this program or another), or a venv holds another Nightshift than
    ``app`` (``mixed``). None when neither."""
    try:
        record = read(home)
    except ValueError as e:
        return str(e)
    if record is not None:
        return f"an interrupted update or rollback left {record_path(home)}"
    found = mixed(home, app, venvs)
    return mixed_sentence(found, app) if found else None


def pending_sentence(home: Path, app: Path, record: Mapping[str, Any] | None, problem: str = "") -> str:
    """What a record that waits means for the owner (``ns doctor``): which environments, and the way out."""
    names = venv_names(record) if record else []
    which = ", ".join(f"'{n}'" for n in names) or "of this install"
    what = f"the record {record_path(home)} cannot be read ({problem})" if problem else (
        f"an interrupted update or rollback left the Python environment {which} unfinished"
    )
    return (
        f"{what} ({CODE}). Nightshift finishes it the next time you open it from a Desktop shortcut, before "
        f"anything else starts. If that fails, close every Nightshift window and run "
        f"INSTALL.cmd from the Nightshift {program_version(app) or 'unknown version'} zip and choose this "
        "folder (Repair)."
    )


#: Exit code of `main` when a start is refused (the launchers' `Start-NsSupervisor` and the engine use it).
REFUSED_EXIT = 4


def start_refusal(home: Path, app: Path) -> str | None:
    """The one start gate of the Python side (D-39b review): None when Nightshift may start from the program
    in ``app``; else the sentence that says why not. Refused while a repair record waits for that program,
    or when the record cannot be read (the launch script treats it as present too). A record of another
    program is stale and does not stop a start (the next update, setup or shortcut start deletes it).

    Standard library only, and reads nothing but two files: a process that already runs from a half-filled
    environment got this far, so its own imports worked, but this check needs nothing from those
    environments (``main`` runs it under ``python -I -S``)."""
    try:
        record = read(home)
    except ValueError as e:
        why = pending_sentence(home, app, None, str(e))
    else:
        if record is None or not matches(record, app):
            return None
        why = pending_sentence(home, app, record)
    if update_running(home):    # (review note: a live update is not an interrupted one: other words)
        why = running_sentence(home)
    return "Nightshift was not started: " + why.replace(f" ({CODE})", "", 1)


def running_sentence(home: Path) -> str:
    """While an update, rollback or repair holds ``run/update.lock`` and is refreshing the environments."""
    state = Path(home) / "run" / "update_state.json"
    progress = str(state)
    try:
        data = json.loads(read_text_retry(state))
        log = data.get("log") if data.get("state") == "running" else None
        if isinstance(log, str) and log:
            progress = log      # (the running worker's own log)
    except (OSError, ValueError, AttributeError):
        pass
    return (
        "an update, rollback or repair of Nightshift is running now and is refreshing its Python "
        f"environments ({CODE}). Start Nightshift again when it has finished: its window 'Nightshift update' "
        f"shows the result, and so does {progress}; an update starts Nightshift by itself when it is done."
    )


def main(argv: Sequence[str]) -> int:
    """``python -I -S -c <import this> <app> <home>``: print the refusal and return ``REFUSED_EXIT``, or 0.
    Run by the launchers before they start the engine, with the interpreter of the supervisor venv but none
    of its packages (``-S``)."""
    if len(argv) != 2:
        return 2
    why = start_refusal(Path(argv[1]), Path(argv[0]))
    if why is None:
        return 0
    sys.stdout.write(f"{CODE}: {why}\n")
    return REFUSED_EXIT


#: Read-only: every process's id, image and command line (Windows PowerShell 5.1, CIM). The folder looked
#: for is not on this command line, so the listing never names itself.
PROCESS_LIST = (
    "Get-CimInstance Win32_Process | ForEach-Object { '{0}|{1}|{2}' -f $_.ProcessId, $_.ExecutablePath, "
    "$_.CommandLine }"
)


def holders_in(listing: str, venvs: Path, skip: Iterable[int] = ()) -> list[str]:
    """The programs of ``listing`` (``PROCESS_LIST``'s lines) that run from ``venvs``: the image is in it, or
    a path in it is on the command line (a console script runs as the base interpreter with
    ``<venv>/Scripts/ns.exe`` as its first argument, ``ns_supervisor.update.holders``). Whole folders only:
    ``D:/NS2/venvs`` is not ``D:/NS/venvs``."""
    root = os.path.normcase(os.path.abspath(venvs)) + os.sep
    found: list[str] = []
    for line in listing.splitlines():
        pid, _, rest = line.strip().partition("|")
        exe, _, cmd = rest.partition("|")
        if not pid.isdigit() or int(pid) in set(skip):
            continue
        if not (os.path.normcase(exe).startswith(root) or root in os.path.normcase(cmd)):
            continue
        name = Path(exe).name if exe else (cmd.split('"')[1] if cmd.startswith('"') else cmd.split(" ")[0])
        found.append(f"{Path(name).name or 'a program'} (process {pid})")
    return found


def stub_ready(home: Path) -> bool:
    """True when the launch script in the home replays a record (format ``REPLAY_SCHEMA`` or later): only
    then may a message say that opening Nightshift from a shortcut finishes the environments."""
    return shortcut_owner.stub_schema(Path(home) / shortcut_owner.STUB_NAME) >= REPLAY_SCHEMA
