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
from dataclasses import dataclass
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


def engine_answers(home: Path, timeout_s: float = 2.0) -> bool:
    """True when the engine of this home answers ``GET /status`` on the port its ``config/runtime.json``
    names (8099 without one). A lock held by an engine that closed its port and never exited (D-46) is not
    an engine that runs. Standard library only (setup asks it)."""
    import urllib.error
    import urllib.request

    port = 8099
    try:
        port = int(json.loads(read_text_retry(Path(home) / "config" / "runtime.json", "utf-8-sig"))
                   .get("supervisor_port") or 8099)
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=timeout_s) as r:  # noqa: S310
            return int(r.status) == 200
    except (OSError, urllib.error.URLError, ValueError):
        return False


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

# ---------------------------------------------------------------- D-48: the update state, once it is finished
STATE_NAME = "update_state.json"
#: Words of a stored state that sends the owner to finish the environments (every wording since 0.1.15-rc3:
#: `ns_supervisor.update` SYNC_STOPPED, ROLLBACK_SYNC_STOPPED, FINISH_AT_START, `_unfinished`). The launch
#: script has the same list (`$finishMarks`).
FINISH_MARKS = (
    "finishes the Python environments", "To fill every environment", "To finish it:",
    "could not be refreshed",
)
#: Who finished an interrupted update, in the words of the closed state.
BY_SETUP = "setup (INSTALL.cmd, Repair)"


def needs_finish(state: Mapping[str, Any]) -> bool:
    """True when an update state says an update or rollback stopped with environments left to finish: a
    `running` state (whoever asks holds ``run/update.lock`` or has found its worker dead) or a `failed` one,
    written while a repair record waited (``record``), or naming environments left (``venvs_left``,
    ``record_left``), or telling the owner to finish them (``FINISH_MARKS``)."""
    if state.get("state") not in ("running", "failed"):
        return False
    if state.get("record") or state.get("venvs_left") or state.get("record_left"):
        return True
    text = f"{state.get('message') or ''} {state.get('if_stopped_here') or ''}"
    return any(mark in text for mark in FINISH_MARKS)


def finished_sentence(by: str, version: str, at: datetime) -> str:
    """What the closed state says (the launch script writes the same words)."""
    return (
        f"An update or rollback was interrupted and has since been finished by {by} "
        f"({at:%Y-%m-%d %H:%M}): Nightshift {version or 'unknown version'}, its program and Python "
        "environments, is in place."
    )


def close_update_state(home: Path, app: Path, by: str, *, now: datetime | None = None) -> bool:
    """D-48: whoever finishes the environments an interrupted update or rollback left (setup's Repair here;
    the launch script's replay in PowerShell; the next update writes its own state) brings
    ``run/update_state.json`` to the final state: `done`, saying who finished it and what is in place. Only
    a state that `needs_finish` is changed, and only by a caller that holds ``run/update.lock`` (no update
    process writes the file then). True when it was rewritten; a file that cannot be read or written stays
    (its readers say what is true: ``ns_supervisor.update_job.settled``)."""
    path = Path(home) / "run" / STATE_NAME
    try:
        raw = json.loads(read_text_retry(path))
    except (OSError, ValueError):
        return False
    if not isinstance(raw, dict) or not needs_finish(raw):
        return False
    at = now or datetime.now()
    version = program_version(app)
    data = {
        "state": "done", "old": str(raw.get("old") or ""), "new": version, "exit_code": 0,
        "message": finished_sentence(by, version, at), "updated_at": at.isoformat(timespec="seconds"),
        "finished_by": by, "log": str(raw.get("log") or ""),
    }
    try:
        write_text_atomic(path, json.dumps(data))
    except OSError:
        return False
    return True


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


def program_of(home: Path) -> Path:
    """The program folder this home runs (``config/install_state.json``'s ``app_path``, or its ``.bak``),
    else the program this code is in. Standard library only, like ``start_refusal``."""
    config = Path(home) / "config"
    for name in ("install_state.json", "install_state.json.bak"):
        try:
            app = json.loads(read_text_retry(config / name, "utf-8-sig")).get("app_path")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(app, str) and app:
            return Path(app)
    return Path(__file__).resolve().parents[3]


def home_refusal(home: Path) -> str | None:
    """D-48: THE start gate of every entry that starts a job, a model or the engine (``studio run`` and the
    other job and voice commands, ``ns tune``, the dashboard server, the dashboard's job runner, the clip
    runner, the assistant), whatever started the interpreter (``studio.exe``, ``python -m``, a shim): each
    calls this first, before it opens or writes anything, so the route cannot matter. ``start_refusal`` for
    the program of this home; None when it may start."""
    return start_refusal(Path(home), program_of(home))


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


#: Read-only: every process's id, image and command line (Windows PowerShell 5.1, CIM), then, after the
#: unit separator (character 31, never in a command line Windows starts), its parent's id and its start
#: time in milliseconds since 1970 (UTC). The folder looked for is not on this command line, so the
#: listing never names itself.
PROCESS_LIST = (
    "Get-CimInstance Win32_Process | ForEach-Object { $t = ''; "
    "if ($_.CreationDate) { $t = ([DateTimeOffset]$_.CreationDate).ToUnixTimeMilliseconds() }; "
    # one process, one line: a command line may hold line breaks (`python -c` with a program of several
    # lines) or the field separator itself; left in, they would cut the process's line short (its marker and
    # home after the break unseen) and let the rest be read as lines of other processes
    "'{0}|{1}|{2}{3}{4}{3}{5}' -f $_.ProcessId, $_.ExecutablePath, "
    "($_.CommandLine -replace '[\\r\\n\\x1f]+', ' '), [char]31, $_.ParentProcessId, $t }"
)
FIELD = "\x1f"


# ------------------------------------------- D-46 review: which running process is THIS install's own
@dataclass(frozen=True)
class Proc:
    """One running process as a listing gives it. ``created``: seconds since 1970 (None: not known);
    ``env_home``: its NIGHTSHIFT_HOME when it could be read (None: not read, or not set)."""

    pid: int
    ppid: int | None
    exe: str
    cmdline: str
    created: float | None
    env_home: str | None = None


#: A record written in the same second as a process started still counts it (coarse file times).
RECORD_SLACK_S = 2.0
#: The modules a process must run to be taken for this home's engine when it holds the home's engine lock.
ENGINE_MODULE = "ns_supervisor.app"


def parse_listing(listing: str) -> list[Proc]:
    """``PROCESS_LIST``'s lines as `Proc` (a line without the parent and time fields has neither)."""
    out: list[Proc] = []
    for line in listing.splitlines():
        main, *more = line.strip().split(FIELD)
        pid, _, rest = main.partition("|")
        exe, _, cmd = rest.partition("|")
        if not pid.isdigit():
            continue
        ppid = int(more[0]) if more and more[0].strip().isdigit() else None
        when = more[1].strip() if len(more) > 1 else ""
        out.append(Proc(int(pid), ppid, exe, cmd, int(when) / 1000.0 if when.isdigit() else None))
    return out


def _folder_in(home: Path, text: str) -> bool:
    """True when the whole folder ``home`` is named in ``text`` (``D:/NS2`` is not ``D:/NS``)."""
    root = os.path.normcase(os.path.abspath(home))
    low = os.path.normcase(text)
    at = low.find(root)
    while at >= 0:
        end = at + len(root)
        if end == len(low) or low[end] in (os.sep, '"', " ", "'"):
            return True
        at = low.find(root, at + 1)
    return False


def _inside(path: str, folder: Path) -> bool:
    """True when the file ``path`` is under ``folder``."""
    root = os.path.normcase(os.path.abspath(folder)) + os.sep
    return bool(path) and os.path.normcase(os.path.abspath(path)).startswith(root)


def is_this_install(home: Path, proc: Proc, record_written: float | None) -> bool:
    """D-46 review, blocker 1: a pid that ``run/supervisor.json`` records is this install's own process ONLY
    when both hold:

    A. it is tied to THIS install: its program file is under ``home`` (a venv's ``python.exe``, ``ns.exe``,
       ``bin\\llama-server.exe``), or ``home`` is on its command line (a path of this home as an argument),
       or its NIGHTSHIFT_HOME is ``home``. The first two come from any listing (CIM or psutil, no
       elevation); the third is read with psutil from the process's own environment, which Windows allows
       for a process of the same user that is not elevated (else it is not read, and only A1 or A2 can tie
       it). Naming a Nightshift module ties nothing: every install, and a checkout, runs the same modules.
    B. it started no later than the record was written (``RECORD_SLACK_S``): a pid Windows gave to a newer
       process after the record is not the one recorded.

    =====================  =====================  ====================  ===========  ======
    program under home     home on command line   NIGHTSHIFT_HOME=home  started <=   ours
                                                                        record
    =====================  =====================  ====================  ===========  ======
    yes                    any                    any                   yes          yes
    any                    yes                    any                   yes          yes
    any                    any                    yes                   yes          yes
    no                     no                     no or not read        yes          no
    any                    any                    any                   no / unknown no
    =====================  =====================  ====================  ===========  ======

    The engine itself is found by its lock instead (`own_processes`): only a live engine of this home can
    hold ``run/supervisor.lock``."""
    if proc.created is None or record_written is None or proc.created > record_written + RECORD_SLACK_S:
        return False
    tied = _inside(proc.exe, home) or _folder_in(home, proc.cmdline)
    if proc.env_home:
        tied = tied or os.path.normcase(os.path.abspath(proc.env_home)) == os.path.normcase(
            os.path.abspath(home))
    return tied


def engine_lock_owner(home: Path) -> int | None:
    """The pid in ``run/supervisor.lock`` while the lock is held (an engine of this home lives; 0.1.14 and
    every later version take this lock and write their pid after byte 0); None when it is free."""
    from ns_common.singleton import read_owner

    lock = Path(home) / "run" / "supervisor.lock"
    return read_owner(lock) if _held(lock) else None


def recorded(home: Path) -> tuple[list[tuple[int, str]], float | None]:
    """The pids ``run/supervisor.json`` records (the engine, then its services: the format of 0.1.14 to
    this version, ``{"pid", "children": {name: pid}}``) and when it was written; ([], None) without one."""
    f = Path(home) / "run" / "supervisor.json"
    try:
        written = f.stat().st_mtime
        data = json.loads(read_text_retry(f))
        named = [(int(data["pid"]), "the engine")]
        named += [(int(v), "the dashboard" if k == "dashboard" else f"the {k} service")
                  for k, v in dict(data.get("children") or {}).items()]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return [], None
    return named, written


def own_processes(home: Path, procs: Sequence[Proc]) -> dict[int, str]:
    """pid -> what it is, for every running process that is this install's own (the one rule both the
    update's stop, `ns_supervisor.stopping`, and setup's holder wait use):

    - the engine: the process holding this home's engine lock, as the lock names it, when it runs
      ``ns_supervisor.app``;
    - each pid ``supervisor.json`` records that `is_this_install`;
    - the engine's launcher: its parent, when that program is this home's ``venvs\\supervisor`` python;
    - every process started by one of those, and started after it (a parent id Windows reused is not a
      parent)."""
    by_pid = {p.pid: p for p in procs}
    own: dict[int, str] = {}
    holder = engine_lock_owner(home)
    if holder is not None and holder in by_pid and ENGINE_MODULE in by_pid[holder].cmdline:
        own[holder] = "the engine"
    named, written = recorded(home)
    for pid, what in named:
        p = by_pid.get(pid)
        if p is not None and pid not in own and is_this_install(home, p, written):
            own[pid] = what
    launcher = Path(home) / "venvs" / "supervisor"
    for pid, what in list(own.items()):
        parent = by_pid.get(by_pid[pid].ppid or -1)
        if what == "the engine" and parent is not None and _inside(parent.exe, launcher):
            own.setdefault(parent.pid, "the engine's launcher")
    grew = True
    while grew:
        grew = False
        for p in procs:
            mother = by_pid.get(p.ppid or -1)
            if p.pid in own or mother is None or mother.pid not in own:
                continue
            if p.created is not None and mother.created is not None and p.created < mother.created:
                continue
            own[p.pid] = "a process the engine started"
            grew = True
    return own


def holders_in(listing: str, venvs: Path, skip: Iterable[int] = ()) -> list[str]:
    """The programs of ``listing`` (``PROCESS_LIST``'s lines) that run from ``venvs``: the image is in it, or
    a path in it is on the command line (a console script runs as the base interpreter with
    ``<venv>/Scripts/ns.exe`` as its first argument, ``ns_supervisor.update.holders``). Whole folders only:
    ``D:/NS2/venvs`` is not ``D:/NS/venvs``."""
    root = os.path.normcase(os.path.abspath(venvs)) + os.sep
    found: list[str] = []
    for line in listing.splitlines():
        pid, _, rest = line.strip().split(FIELD)[0].partition("|")
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
