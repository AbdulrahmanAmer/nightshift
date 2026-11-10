"""C2 Windows toast through PowerShell `Windows.UI.Notifications` (spec J10). No extra package.

Title and message travel in environment variables, never inside the script text, so they cannot
break out of the PowerShell source. Everything is best effort: unavailable means no-op.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import itertools
import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from ns_common.atomic import (
    REPLACE_WAIT_S,
    create_text_atomic,
    read_bytes_retry,
    read_text_retry,
    retry_busy,
    sweep_stale_tmp,
    write_bytes_atomic,
    write_text_atomic,
)
from ns_common.commandline import render
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_common.singleton import AlreadyRunning, FileLock
from ns_common.testguard import RealMachineUnderTest
from ns_supervisor import pwsh

#: AppUserModelID the toasts are sent from. Windows shows a toast only for an AUMID that is registered
#: (key AppUserModelId under HKCU Software Classes); installer step s09 registers it, uninstall removes it.
AUMID = "Nightshift"

TOAST_SCRIPT = "\n".join(
    [
        "$ErrorActionPreference = 'Stop'",
        "[void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
        "ContentType = WindowsRuntime]",
        "[void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, "
        "ContentType = WindowsRuntime]",
        "$tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
        "[Windows.UI.Notifications.ToastTemplateType]::ToastText02)",
        "$text = $tpl.GetElementsByTagName('text')",
        "[void]$text.Item(0).AppendChild($tpl.CreateTextNode($env:NS_TOAST_TITLE))",
        "[void]$text.Item(1).AppendChild($tpl.CreateTextNode($env:NS_TOAST_MESSAGE))",
        "$toast = [Windows.UI.Notifications.ToastNotification]::new($tpl)",
        f"[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{AUMID}').Show($toast)",
    ]
)


def toast(title: str, message: str) -> bool:
    """Show a toast; True if PowerShell accepted it. Never raises (no PowerShell, headless: no-op).

    In a test run the real `pwsh.run_powershell` refuses the toast script before it starts anything; that is
    "unavailable" here, so the caller leaves its morning note in the test's home instead.
    """
    message = render(message)               # shown now: as this process's owner types it (FN-3)
    try:
        r = pwsh.run_powershell(
            TOAST_SCRIPT, env_extra={"NS_TOAST_TITLE": title, "NS_TOAST_MESSAGE": message}, timeout_s=30
        )
    except (pwsh.PowerShellMissing, OSError, subprocess.SubprocessError, RealMachineUnderTest):
        return False
    return r.returncode == 0


# --- the nightly task may run in session 0 (no desktop): no toast can show there, so leave a morning note ---
def session_id() -> int | None:
    """Windows session number of this process (0 = services and S4U tasks); None when it cannot be read."""
    if sys.platform != "win32":
        return None
    try:
        sid = ctypes.c_ulong()
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        ok = kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
    except (AttributeError, OSError):
        return None
    return int(sid.value) if ok else None


def session_is_interactive() -> bool:
    """False in session 0 (services, S4U tasks): there is no desktop to show a toast on. True elsewhere."""
    return session_id() != 0


# --- the morning notes: this module owns `run/morning_note.json` -------------------------------------------
# Every part of Nightshift that has something to tell the owner who was not at the screen leaves it here: the
# nightly run, the engine's start, the schedule check, the update check, `ns verify`, setup's voice step, the
# studio. The dashboard's bell shows the notes and removes the ones the owner saw. They are separate processes
# (the engine, a studio child, the dashboard, a CLI, and the launch script `nightshift-launch.ps1`, which is
# PowerShell and runs when no Python does), so:
#   - the file is changed only here (`leave_note`, `dismiss`, `drain_spool` and the mending of a damaged file)
#     and in the launch script's one function `Add-NsMorningNote`; tests/test_morning_notes_owner.py fails on
#     any other;
#   - each change is one read-modify-write under ONE lock: byte 0 of `run/morning_note.lock`, locked by the
#     operating system (`ns_common.singleton.FileLock`: `LockFile` through `msvcrt.locking`; the launch
#     script takes the same byte with .NET `FileStream.Lock(0, 1)`). The lock belongs to an open handle, so
#     a holder that is killed loses it at once: there is no stale lock to detect and no pid to check, and the
#     file `morning_note.lock` left on disk is not the lock;
#   - a holder keeps the lock for `NOTES_HOLD_MAX_S` at most (every wait inside it is bounded, see below), and
#     a writer outside a web request waits `NOTES_WAIT_S`, longer than that: a writer that is merely slow
#     never makes another one give up. A web request waits `NOTES_REQUEST_WAIT_S` and is told "busy";
#   - a note that cannot be written (the lock still held, the file held, a full disk, a read-only folder, the
#     copy of a damaged file failing) is never only lost: one log line `morning_note_failed` with the reason
#     and the note's text, and the note goes to the spool `run/morning_note.spool/` (one small file per
#     note, written without the lock: a temp name, then a rename). The next writer that holds the lock, the
#     dashboard's dismiss and the engine's start move the spool into the file (`_drain`), each note once;
#   - a reader takes no lock (the file is swapped whole, so it reads the old or the new one) and so is never
#     kept from the notes by a writer that hangs;
#   - a note leaves the file when the owner dismissed it, by id, or at the bound (`NOTES_FULL` notes wait):
#     then the oldest ROUTINE note goes (`is_routine`), a failure or a note that asks something of the owner
#     only to make room for a newer one of its kind, never for a routine one (that one is kept out); one note
#     at the top of the list says how many were removed or kept out (`removed_message`), updated in place;
#   - the file's format is what 0.1.14 wrote and reads (`{"notes": [{"at", "title", "message"}]}`, UTF-8
#     without a byte order mark), plus two keys 0.1.14 ignores: `routine: true` on a routine note (written
#     here from `is_routine`, so the launch script needs no copy of the rule) and `removed: <n>` on the one
#     note that counts the removed ones. A note's id is derived from at, title and message when it is read
#     and never stored, so notes of 0.1.14, of the launch script and of this module all have one.
NOTE_FILE = "morning_note.json"
NOTE_LOCK = "morning_note.lock"
#: The folder of notes that could not be written when they were left (see `_spool`, `_drain`).
NOTE_SPOOL = "morning_note.spool"
#: The swap of the notes file inside the lock waits this long for a reader or a virus scan that holds the
#: file; then the write fails (the note goes to the spool) and the lock is let go.
NOTES_SWAP_WAIT_S = 5.0
#: The time a holder spends moving spooled notes into the file; what is left waits for the next holder.
NOTES_DRAIN_S = 1.0
#: The longest a holder keeps the lock: the copy of a damaged file aside (`write_bytes_atomic`, which waits
#: up to `REPLACE_WAIT_S` for its swap), the drain, the swap of the notes file, and one second for the reads
#: (`retry_busy`: about 0.1 s each), the log line and the removal of the drained spool files (one try each).
#: The launch script holds it for its swap (20 tries, about 1.5 s) and two reads.
NOTES_HOLD_MAX_S = REPLACE_WAIT_S + NOTES_DRAIN_S + NOTES_SWAP_WAIT_S + 1.0
#: How long a writer outside a web request (the night, the engine's start, a CLI, a studio job) waits for
#: the lock: longer than the longest hold, so only a holder that hangs makes it spool its note.
NOTES_WAIT_S = NOTES_HOLD_MAX_S + 3.0
#: How long a web request (the bell's Mark all read) waits before it answers "busy": a page must not hang.
NOTES_REQUEST_WAIT_S = 5.0
#: How long a reader that found a damaged file waits to mend it (the next reader tries again).
NOTES_MEND_WAIT_S = 1.0
#: The most notes that wait (the note that counts the removed ones aside). At the bound the oldest routine
#: note goes first; see `_add`.
NOTES_FULL = 200
#: The most notes that wait in the spool. Reaching it takes a holder that hangs for days; then a note that
#: cannot be written is only in the log (`morning_note_failed`, level ERROR, with its text).
SPOOL_MAX = 50
_RETRY_S = 0.05
_spool_counter = itertools.count(1)

#: THE definition of a routine note (the one place; the launch script reads the `routine` mark this module
#: writes): news that asks nothing of the owner. Each `; `-separated part of the message must be one of
#: these, so the nightly summary "1 master + 5 Shorts ready for review" is routine and "1 master ready for
#: review; 1 job failed (NS-E203)" is not ...
ROUTINE_PARTS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\d+ masters?( \+ \d+ Shorts?)? ready for review"),
    re.compile(r"\d+ Shorts? ready for review"),
    re.compile(r"\d+ jobs? finished, open Dashboard to review"),
)
#: ... and the codes whose notes are routine: `NS-E207`, requests the loopback check refused (nothing was
#: changed; the owner is told a pattern, at most four notes a day, `ns_common.loopback`).
ROUTINE_CODES = ("NS-E207",)


def is_routine(message: str) -> bool:
    """True when a note with this message is routine (`ROUTINE_PARTS`, `ROUTINE_CODES`): at the bound it
    goes first, and it never makes room by pushing out a note that is not routine."""
    if any(message.startswith(f"{code}:") for code in ROUTINE_CODES):
        return True
    parts = message.split("; ")
    return bool(message) and all(any(p.fullmatch(part) for p in ROUTINE_PARTS) for part in parts)


class Note(TypedDict):
    """One waiting note as readers get it. `id` names it for `dismiss` (see `note_id`)."""

    id: str
    at: str
    title: str
    message: str


class NotesBusy(OSError):
    """The notes are held by another writer for longer than the wait, or their lock cannot be opened.
    Nothing was written."""


def note_path(paths: Paths) -> Path:
    """`run/morning_note.json`: what a toast could not show; the dashboard displays and dismisses them."""
    return paths.home / "run" / NOTE_FILE


def spool_dir(paths: Paths) -> Path:
    """`run/morning_note.spool/`: notes that could not be written when they were left, one file each."""
    return paths.home / "run" / NOTE_SPOOL


def note_id(at: str, title: str, message: str) -> str:
    """The id of a note: the first 16 hex characters of SHA-1 over its time, title and message. Two notes
    that are the same to the second share it and are dismissed together."""
    text = "\n".join((at, title, message)).encode("utf-8", errors="surrogatepass")
    return hashlib.sha1(text, usedforsecurity=False).hexdigest()[:16]


def damaged_message(kept: Path) -> str:
    """What the owner reads after the notes file was found damaged (the same words in the launch script)."""
    return (
        "Nightshift could not read its waiting notes: the file that holds them was damaged. The notes that "
        f"could still be read are listed here. The damaged file was kept as {kept}; open it in Notepad to "
        "see what else it held."
    )


def removed_message(paths: Paths, count: int) -> str:
    """The one note that says how many notes the bound removed unread (the launch script: the same words)."""
    many = "1 unread note was" if count == 1 else f"{count} unread notes were"
    return (
        f"{many} removed to keep at most {NOTES_FULL} notes here: routine news such as \"ready for review\" "
        "goes first, and a failure or a note that asks something of you only makes room for a newer one. "
        f"Each removed note is in the log files in {paths.logs} (morning_note_dropped). Press Mark all read "
        "once you have read the notes, so that nothing more is removed."
    )


def _as_note(raw: dict[str, Any]) -> Note:
    at, title, message = (str(raw.get(key) or "") for key in ("at", "title", "message"))
    return {"id": note_id(at, title, message), "at": at, "title": title, "message": message}


def _is_count(entry: dict[str, Any]) -> bool:
    """The note that counts the removed notes (`removed: <n>`)."""
    n = entry.get("removed")
    return isinstance(n, int) and not isinstance(n, bool)


def _routine(entry: dict[str, Any]) -> bool:
    return entry.get("routine") is True or is_routine(str(entry.get("message") or ""))


def _log(paths: Paths) -> NsLogger | None:
    try:
        return NsLogger("supervisor", paths.logs, echo=False)
    except Exception:  # noqa: BLE001 - a log folder that cannot be made: the note itself is the telling
        return None


def _say(paths: Paths, level: str, event: str, msg: str, **data: Any) -> None:
    log = _log(paths)
    if log is not None:
        with contextlib.suppress(Exception):
            log.log("ERROR" if level == "ERROR" else "WARN", event, msg, **data)


def _load(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """The entries of the file that are notes, and whether the file is damaged (not JSON, not the shape, or
    holding an entry that is no note). No file: no notes. A file that cannot be opened raises `OSError`:
    that is not damage, and nobody may write over it."""
    try:
        text = read_text_retry(path, encoding="utf-8-sig")
    except FileNotFoundError:
        return [], False
    except UnicodeDecodeError:
        return [], True
    try:
        data = json.loads(text)
    except ValueError:
        return [], True
    items = data.get("notes") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return [], True
    good = [n for n in items if isinstance(n, dict)]
    return good, len(good) != len(items)


def _store(path: Path, entries: list[dict[str, Any]]) -> None:
    """Swap the file in. A routine note gets its `routine` mark here (also one an older version wrote), so
    the launch script, which has no copy of `is_routine`, applies the same rule at the bound."""
    for entry in entries:
        if not _is_count(entry) and entry.get("routine") is not True and _routine(entry):
            entry["routine"] = True
    write_text_atomic(path, json.dumps({"notes": entries}, indent=2) + "\n", wait_s=NOTES_SWAP_WAIT_S)


@contextlib.contextmanager
def _locked(paths: Paths, wait_s: float) -> Iterator[None]:
    """Hold the notes lock for the block; `NotesBusy` when it is not free within `wait_s`."""
    lock = FileLock(paths.home / "run" / NOTE_LOCK)
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        try:
            lock.acquire()
            break
        except AlreadyRunning:
            if time.monotonic() >= deadline:
                raise NotesBusy(
                    f"the morning notes are held by another program (waited {wait_s:g} s for {lock.path})"
                ) from None
            time.sleep(_RETRY_S)
        except OSError as e:
            raise NotesBusy(f"the lock of the morning notes cannot be opened ({lock.path}): {e}") from e
    try:
        yield
    finally:
        lock.release()


def _waiting(paths: Paths) -> list[dict[str, Any]]:
    """Under the lock: the entries on disk. A damaged file is first copied aside, then replaced by what
    could be read of it plus a note that says so. The copy comes first and is named after the damaged file's
    own time, so a kill or a failed write between the two steps leaves the damaged file where it was and the
    next caller does both again, with one copy and one note in the end."""
    path = note_path(paths)
    entries, damaged = _load(path)
    if damaged:
        stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y%m%d-%H%M%S")
        kept = path.with_name(f"{path.name}.bad-{stamp}")
        write_bytes_atomic(kept, read_bytes_retry(path))
        said = damaged_message(kept)
        at = datetime.now().isoformat(timespec="seconds")
        entries.append({"at": at, "title": "Nightshift", "message": said})
        _store(path, entries)
        _say(paths, "WARN", "morning_note_damaged", said, kept=str(kept))
    return entries


Entries = list[dict[str, Any]]


def _add(paths: Paths, entries: Entries, new: dict[str, Any]) -> tuple[Entries, bool]:
    """`entries` with `new` added and the bound kept; and whether `new` is among them.

    The rule, while more than `NOTES_FULL` notes would wait: the oldest routine note goes; when none is
    left, the oldest note goes (a failure makes room for a newer failure). A routine `new` never pushes out
    a note that is not routine: when the routine notes that wait cannot make its room, it is not added (logged
    with its text). So the newest note that matters is never the one lost. Every removed note, and every
    routine note kept out, is logged with its text (`morning_note_dropped`) and counted by one note
    (`removed: <n>`, first in the file, so the bell lists it last); it is replaced, never repeated, and goes
    only when the owner dismisses it. The entries returned are always to be stored: the count may have moved
    even when `new` is not among them.
    """
    count = next((e for e in entries if _is_count(e)), None)
    kept = [e for e in entries if not _is_count(e)]
    over = len(kept) + 1 - NOTES_FULL
    gone: list[dict[str, Any]] = []
    added = not (over > 0 and _routine(new) and sum(1 for e in kept if _routine(e)) < over)
    if added:
        kept.append(new)
        while len(kept) > NOTES_FULL:
            drop = next((i for i, e in enumerate(kept[:-1]) if _routine(e)), 0)
            gone.append(kept.pop(drop))
        if not gone:
            return ([count] if count else []) + kept, True
    else:
        gone.append(new)
    total = (int(count["removed"]) if count else 0) + len(gone)
    at = datetime.now().isoformat(timespec="seconds")
    _say(
        paths, "WARN", "morning_note_dropped",
        f"{len(gone)} unread note(s) removed, bound {NOTES_FULL}" if added
        else f"a routine note was not added: {NOTES_FULL} notes wait",
        notes=[{k: e.get(k) for k in ("at", "title", "message")} for e in gone], removed_in_all=total,
    )
    said = {"at": at, "title": "Nightshift", "message": removed_message(paths, total), "removed": total}
    return [said, *kept], added


# --- the spool: notes that could not be written when they were left ----------------------------------------
def _spool(paths: Paths, entry: dict[str, Any], once: bool) -> Path:
    """Put one note in the spool, without the lock: the whole file appears at once under a new name (a temp
    file beside it, then a rename that never replaces), so a reader never meets half of one. A temp file a
    kill left behind is swept by the drain after an hour. Raises `OSError` when it cannot be written."""
    folder = spool_dir(paths)
    folder.mkdir(parents=True, exist_ok=True)
    if sum(1 for _ in folder.glob("*.json")) >= SPOOL_MAX:
        raise OSError(f"{SPOOL_MAX} notes wait in {folder} already")
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    target = folder / f"{stamp}-{os.getpid()}-{threading.get_ident()}-{next(_spool_counter)}.json"
    if not create_text_atomic(target, json.dumps({**entry, "once": once}) + "\n"):
        raise OSError(f"{target} exists already")
    return target


def _spooled(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    fields = {k: raw.get(k) for k in ("at", "title", "message")}
    if not all(isinstance(v, str) for v in fields.values()) or not fields["message"]:
        return None
    return {**fields, **({"routine": True} if raw.get("routine") is True else {})}


def _drain(paths: Paths, entries: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[Path]]:
    """Under the lock: the spooled notes added to `entries` (oldest first, for `NOTES_DRAIN_S` at most), and
    the spool files they came from, which the caller removes AFTER its write went through.

    Each note is delivered once: one whose id already waits was moved in by a holder killed between its write
    and the removal of the file (it is not added again); a note left with `once=` is not added when a note
    with the same title and words waits. A file that is not a note (cut off, not JSON, not the shape) is
    renamed `<name>.bad` and logged once: it is never read again. A file that cannot be read just now stays
    for the next holder.
    """
    folder = spool_dir(paths)
    with contextlib.suppress(OSError):
        sweep_stale_tmp(folder)
    try:
        files = sorted(folder.glob("*.json"))
    except OSError:
        return entries, []
    taken: list[Path] = []
    ids = {_as_note(e)["id"] for e in entries}
    deadline = time.monotonic() + NOTES_DRAIN_S
    for file in files:
        if time.monotonic() > deadline:
            break
        try:
            raw = json.loads(read_text_retry(file, encoding="utf-8"))
        except FileNotFoundError:
            continue
        except ValueError:
            raw = None
        except OSError:
            continue
        entry = _spooled(raw)
        if entry is None:
            aside = file.with_name(file.name + ".bad")
            try:
                file.replace(aside)
            except OSError as e:
                _say(paths, "WARN", "morning_note_spool_damaged", f"{file} is no note, not renamed: {e}")
                continue
            _say(paths, "WARN", "morning_note_spool_damaged", f"{file} is no note; kept as {aside}")
            continue
        taken.append(file)
        nid = _as_note(entry)["id"]
        if nid in ids:
            continue
        if isinstance(raw, dict) and raw.get("once") is True and any(
            e.get("title") == entry["title"] and e.get("message") == entry["message"] for e in entries
        ):
            continue
        entries, _ = _add(paths, entries, entry)
        ids.add(nid)
    return entries, taken


def _remove(files: list[Path]) -> None:
    """The spool files whose notes are in the file now. One try each: a file that stays (held) is not added
    again, its id waits."""
    for file in files:
        with contextlib.suppress(OSError):
            file.unlink(missing_ok=True)


def _not_written(paths: Paths, entry: dict[str, Any], once: bool, spool: bool, err: OSError) -> bool:
    """The note could not be written: log it with its text, then spool it. True when spooled; raises `err`
    when the spool cannot take it either (the log line, level ERROR, is then all that is left of it)."""
    reason = f"{type(err).__name__}: {err}"
    text = {"title": entry["title"], "message": entry["message"], "at": entry["at"]}
    if spool:
        try:
            kept = _spool(paths, entry, once)
        except OSError as e:
            _say(paths, "ERROR", "morning_note_failed", reason, **text, spooled=None, spool_error=str(e))
            raise err from None
        _say(paths, "WARN", "morning_note_failed", reason, **text, spooled=str(kept))
        return True
    _say(paths, "WARN", "morning_note_failed", reason, **text, spooled=None)
    raise err


def read_notes(paths: Paths) -> list[Note]:
    """The waiting notes in the order they came (oldest first; the bell shows them newest first), each with
    its `id`. Takes no lock and never raises.

    A damaged file is mended here too (see `_waiting`), so the owner who opens the bell is told at once and
    not at the next night's first note; when the lock is busy just then, what could be read is returned and
    the next read mends.
    """
    try:
        entries, damaged = _load(note_path(paths))
    except OSError:
        return []
    if damaged:
        try:
            with _locked(paths, NOTES_MEND_WAIT_S):
                entries = _waiting(paths)
        except OSError:
            pass
    return [_as_note(n) for n in entries]


def shown_notes(paths: Paths) -> list[Note]:
    """`read_notes` as the owner is shown them NOW: each message with the `ns ...` and `studio ...` it names
    given as this install's owner types them (`ns_common.commandline.render`). The id stays the one of the
    stored (raw) note, so `dismiss` finds it. For a page or a console; never written back."""
    return [
        Note(id=n["id"], at=n["at"], title=n["title"], message=render(n["message"], paths.home))
        for n in read_notes(paths)
    ]


def _leave(
    paths: Paths, title: str, decide: Callable[[list[Note]], str | None], at: str, wait_s: float
) -> tuple[str | None, bool]:
    """One locked step: read (mending a damaged file), move the spool in, `decide` on the waiting notes,
    add what it chose, keep the bound, write. Returns what `decide` chose and whether it was added."""
    with _locked(paths, wait_s):
        entries = _waiting(paths)
        entries, taken = _drain(paths, entries)
        # stored RAW (the plain `ns ...` words, FN-3): the readers render when they show it, and the
        # callers' `once` checks and the note ids compare the words they gave
        message = decide([_as_note(n) for n in entries])
        if message is None:
            if taken:
                try:
                    _store(note_path(paths), entries)
                except OSError as e:  # the spooled notes stay spooled for the next holder
                    _say(paths, "WARN", "morning_note_spool_kept", f"{type(e).__name__}: {e}")
                else:
                    _remove(taken)
            return None, False
        entry: dict[str, Any] = {"at": at, "title": title, "message": message}
        if is_routine(message):
            entry["routine"] = True
        # (a routine note the bound kept out is not added, but the count note says one more: stored too)
        entries, added = _add(paths, entries, entry)
        _store(note_path(paths), entries)
        _remove(taken)
    return message, added


def leave_note(
    paths: Paths,
    title: str,
    message: str,
    *,
    once: Callable[[list[Note]], bool] | None = None,
    now: datetime | None = None,
    wait_s: float | None = None,
    spool: bool = True,
) -> bool:
    """Queue a message for the next time somebody opens the dashboard. True when it was added or spooled.

    `once`: called with the waiting notes, under the lock, between the read and the write; when it answers
    True (this was said already and still waits) nothing is added and the answer is False. A caller that
    read the notes itself and then called this would decide on a file another writer is about to change.
    False also when the bound kept a routine note out (`_add`).

    `wait_s`: how long to wait for the lock (default `NOTES_WAIT_S`; a web request passes
    `NOTES_REQUEST_WAIT_S`). When the note cannot be written (the lock or the file held past the waits, a full
    disk, a folder that cannot be written, the copy of a damaged file failing) it is logged with its text
    (`morning_note_failed`) and spooled (`_spool`), and the next writer delivers it; True. `spool=False`: the
    log line, then the `OSError`. An `OSError` is raised only when neither the file nor the spool could take
    the note; the notes that waited are still there in every case. A caller whose rule is more than "said
    once" (a count, a text that depends on what waits) uses `leave_chosen_note`.
    """
    at = (now or datetime.now()).isoformat(timespec="seconds")

    def decide(notes: list[Note]) -> str | None:
        return None if once is not None and once(notes) else message

    try:
        _, added = _leave(paths, title, decide, at, NOTES_WAIT_S if wait_s is None else wait_s)
    except OSError as e:
        entry: dict[str, Any] = {"at": at, "title": title, "message": message}
        if is_routine(message):
            entry["routine"] = True
        return _not_written(paths, entry, once is not None, spool, e)
    return added


def leave_chosen_note(
    paths: Paths,
    title: str,
    choose: Callable[[list[Note]], str | None],
    *,
    now: datetime | None = None,
    wait_s: float | None = None,
) -> str | None:
    """Leave the note `choose` picks from the waiting notes, in ONE locked step: `choose` is called under the
    notes lock (which every writer of every process takes) with the notes that wait, and answers the message
    to add or None. So a count of the waiting notes and the note it allows cannot be split by another writer:
    two servers that refuse at the same moment do not both pass a daily cap. Returns the message when it was
    added; None when `choose` chose none or the bound kept a routine note out (`_add`).

    Never spooled (the spool could not ask `choose` again): a note that cannot be written is logged
    (`morning_note_failed`) and the `OSError` is raised, so the caller keeps it owed. Waits `NOTES_WAIT_S` by
    default: call it off the request path.
    """
    at = (now or datetime.now()).isoformat(timespec="seconds")
    try:
        message, added = _leave(paths, title, choose, at, NOTES_WAIT_S if wait_s is None else wait_s)
    except OSError as e:
        reason = f"{type(e).__name__}: {e}"
        _say(paths, "WARN", "morning_note_failed", reason, title=title, at=at, spooled=None)
        raise
    return message if added else None


def drain_spool(paths: Paths) -> int:
    """The engine's start: move the spooled notes into the file (see `_drain`). How many spool files were
    taken; never raises (a busy lock or a failed write leaves them for the next writer, with a log line)."""
    try:
        if not any(spool_dir(paths).glob("*.json")):
            return 0
        with _locked(paths, NOTES_WAIT_S):
            entries = _waiting(paths)
            entries, taken = _drain(paths, entries)
            if taken:
                _store(note_path(paths), entries)
                _remove(taken)
        return len(taken)
    except OSError as e:
        where = str(spool_dir(paths))
        _say(paths, "WARN", "morning_note_spool_kept", f"{type(e).__name__}: {e}", spool=where)
        return 0


def dismiss(paths: Paths, ids: Iterable[str], *, wait_s: float | None = None) -> int:
    """The owner saw the notes with these ids: remove exactly those. Returns how many went.

    An id that is not there any more (another tab dismissed it) is ignored; a note that arrived after the
    page was loaded has an id the page never sent, and stays. The spool is drained first, so a spool file
    whose note waits already goes before that note can be dismissed (it is never delivered twice). The file is
    removed with its last note. Waits `NOTES_REQUEST_WAIT_S` by default (a page asks); raises `NotesBusy` or
    another `OSError` with nothing removed.
    """
    gone = set(ids)
    path = note_path(paths)
    with _locked(paths, NOTES_REQUEST_WAIT_S if wait_s is None else wait_s):
        entries = _waiting(paths)
        entries, taken = _drain(paths, entries)
        left = [n for n in entries if _as_note(n)["id"] not in gone]
        removed = len(entries) - len(left)
        if left and (removed or taken):
            _store(path, left)
        elif removed:
            retry_busy(lambda: path.unlink(missing_ok=True))
        _remove(taken)
    return removed


def toast_or_note(
    title: str,
    message: str,
    *,
    paths: Paths | None = None,
    interactive: Callable[[], bool] = session_is_interactive,
    show: Callable[[str, str], bool] | None = None,
) -> bool:
    """Toast when this session has a desktop; otherwise, or when the toast fails, leave a morning note.

    Returns True when the user will see it now (toast shown). The note is the fallback, never a duplicate.
    Never raises: a note that cannot be written is spooled by `leave_note`, and one that cannot be spooled
    either is in the log with its text (`morning_note_failed`, ERROR).
    """
    shown = False
    if interactive():
        shown = (show or toast)(title, message)
    if not shown:
        with contextlib.suppress(OSError):
            leave_note(paths or Paths.from_env(), title, message)
    return shown
