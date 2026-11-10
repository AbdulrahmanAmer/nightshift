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

Standard library only: the setup server and the wizard read it too.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import installer.stage2.shortcut_owner as shortcut_owner
from installer.stage2.web.deplock import version_key
from ns_common.atomic import read_text_retry, write_text_atomic

RECORD_NAME = "venv-repair.json"
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
    """Write the record atomically (``OSError`` when it cannot be: the caller refuses before any change)."""
    data: dict[str, Any] = {
        "schema": SCHEMA, "app": str(app), "match": match, "for_app": for_app, "version": version, "by": by,
        "written": datetime.now().isoformat(timespec="seconds"), "uv": uv, "env": dict(env),
        "commands": [{"venv": venv, "argv": [str(a) for a in argv]} for venv, argv in commands],
    }
    target = record_path(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(target, json.dumps(data, indent=2) + "\n")


def read(home: Path) -> dict[str, Any] | None:
    """The record; None when there is none. ``ValueError`` when it is there and cannot be read whole."""
    path = record_path(home)
    if not path.is_file():
        return None
    try:
        data = json.loads(read_text_retry(path))
    except OSError as e:
        raise ValueError(f"{path} cannot be read ({e})") from e
    if not isinstance(data, dict) or not isinstance(data.get("commands"), list):
        raise ValueError(f"{path} is not a repair record")
    return data


def clear(home: Path) -> None:
    """Delete the record (``OSError`` when it is there and cannot be deleted)."""
    record_path(home).unlink(missing_ok=True)


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
    return "Nightshift was not started: " + why.replace(f" ({CODE})", "", 1)


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
