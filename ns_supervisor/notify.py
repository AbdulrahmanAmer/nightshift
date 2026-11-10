"""C2 Windows toast through PowerShell `Windows.UI.Notifications` (spec J10). No extra package.

Title and message travel in environment variables, never inside the script text, so they cannot
break out of the PowerShell source. Everything is best effort: unavailable means no-op.
"""

from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict

from ns_common.atomic import (
    read_bytes_retry,
    read_text_retry,
    retry_busy,
    write_bytes_atomic,
    write_text_atomic,
)
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
#   - the file is changed only here (`leave_note`, `dismiss`, and the mending of a damaged file) and in the
#     launch script's one function `Add-NsMorningNote`; tests/test_morning_notes_owner.py fails on any other;
#   - each change is one read-modify-write under ONE lock: byte 0 of `run/morning_note.lock`, locked by the
#     operating system (`ns_common.singleton.FileLock`: `LockFile` through `msvcrt.locking`; the launch
#     script takes the same byte with .NET `FileStream.Lock(0, 1)`). The lock belongs to an open handle, so
#     a holder that is killed loses it at once: there is no stale lock to detect and no pid to check, and the
#     file `morning_note.lock` left on disk is not the lock;
#   - a writer waits for the lock for `NOTES_WAIT_S` at most and then raises `NotesBusy` (an `OSError`, which
#     every caller already treats as "the note could not be left"): no note may hold up a night or a start;
#   - a reader takes no lock (the file is swapped whole, so it reads the old or the new one) and so is never
#     kept from the notes by a writer that hangs;
#   - a note leaves the file only when the owner dismissed that note, by id. Nothing is dropped to make room;
#   - the file's format is what 0.1.14 wrote and reads (`{"notes": [{"at", "title", "message"}]}`, UTF-8
#     without a byte order mark). A note's id is derived from those three when it is read and never stored,
#     so notes of 0.1.14, of the launch script and of this module all have one, and a rollback reads the file.
NOTE_FILE = "morning_note.json"
NOTE_LOCK = "morning_note.lock"
#: How long a writer waits for another writer (seconds). A read-modify-write of this file takes milliseconds.
NOTES_WAIT_S = 5.0
#: How long a reader that found a damaged file waits to mend it (the next reader tries again).
NOTES_MEND_WAIT_S = 1.0
#: The most notes that wait. Reaching it takes a writer that repeats itself for months with nobody looking;
#: then new notes are refused (`NotesFull`) and the file says so, rather than old ones being dropped unread.
NOTES_FULL = 200
_RETRY_S = 0.05


class Note(TypedDict):
    """One waiting note as readers get it. `id` names it for `dismiss` (see `note_id`)."""

    id: str
    at: str
    title: str
    message: str


class NotesBusy(OSError):
    """The notes are held by another writer for longer than the wait, or their lock cannot be opened.
    Nothing was written."""


class NotesFull(OSError):
    """`NOTES_FULL` notes wait unread: the new one was not added (the file says so once)."""


def note_path(paths: Paths) -> Path:
    """`run/morning_note.json`: what a toast could not show; the dashboard displays and dismisses them."""
    return paths.home / "run" / NOTE_FILE


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


def full_message(paths: Paths) -> str:
    """What the owner reads when `NOTES_FULL` notes wait (left once, as the note after the last)."""
    return (
        f"Nightshift keeps {NOTES_FULL} notes and they are all waiting here, so it takes no new note until "
        f"these are read: later messages are only in the log files in {paths.logs}. Press Mark all read to "
        "make room."
    )


def _as_note(raw: dict[str, Any]) -> Note:
    at, title, message = (str(raw.get(key) or "") for key in ("at", "title", "message"))
    return {"id": note_id(at, title, message), "at": at, "title": title, "message": message}


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
    write_text_atomic(path, json.dumps({"notes": entries}, indent=2) + "\n")


@contextlib.contextmanager
def _locked(paths: Paths, wait_s: float | None) -> Iterator[None]:
    """Hold the notes lock for the block; `NotesBusy` when it is not free within `wait_s`."""
    wait = NOTES_WAIT_S if wait_s is None else wait_s
    lock = FileLock(paths.home / "run" / NOTE_LOCK)
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        try:
            lock.acquire()
            break
        except AlreadyRunning:
            if time.monotonic() >= deadline:
                raise NotesBusy(
                    f"the morning notes are held by another program (waited {wait:g} s for {lock.path})"
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
        with contextlib.suppress(Exception):  # the note is the telling; the log line is for diagnostics
            log = NsLogger("supervisor", paths.logs, echo=False)
            log.warn("morning_note_damaged", said, kept=str(kept))
    return entries


def read_notes(paths: Paths) -> list[Note]:
    """The waiting notes, oldest first, each with its `id`. Takes no lock and never raises.

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


def leave_note(
    paths: Paths,
    title: str,
    message: str,
    *,
    once: Callable[[list[Note]], bool] | None = None,
    now: datetime | None = None,
    wait_s: float | None = None,
) -> bool:
    """Queue a message for the next time somebody opens the dashboard. True when it was added.

    `once`: called with the waiting notes, under the lock, between the read and the write; when it answers
    True (this was said already and still waits) nothing is added and the answer is False. A caller that
    read the notes itself and then called this would decide on a file another writer is about to change.

    Raises `NotesBusy` when another writer held the notes for `wait_s` (default `NOTES_WAIT_S`), `NotesFull`
    when `NOTES_FULL` notes wait, any other `OSError` when the file cannot be read or written. In each case
    the notes that waited are still there.
    """
    with _locked(paths, wait_s):
        entries = _waiting(paths)
        if once is not None and once([_as_note(n) for n in entries]):
            return False
        if len(entries) >= NOTES_FULL:
            raise NotesFull(f"{NOTES_FULL} notes wait unread in {note_path(paths)}: this one was not added")
        at = (now or datetime.now()).isoformat(timespec="seconds")
        entries.append({"at": at, "title": title, "message": message})
        if len(entries) >= NOTES_FULL:
            entries.append({"at": at, "title": "Nightshift", "message": full_message(paths)})
        _store(note_path(paths), entries)
    return True


def dismiss(paths: Paths, ids: Iterable[str], *, wait_s: float | None = None) -> int:
    """The owner saw the notes with these ids: remove exactly those. Returns how many went.

    An id that is not there any more (another tab dismissed it) is ignored; a note that arrived after the
    page was loaded has an id the page never sent, and stays. The file is removed with its last note.
    Raises `NotesBusy` or another `OSError` like `leave_note`, with nothing removed.
    """
    gone = set(ids)
    path = note_path(paths)
    with _locked(paths, wait_s):
        entries = _waiting(paths)
        left = [n for n in entries if _as_note(n)["id"] not in gone]
        removed = len(entries) - len(left)
        if removed and left:
            _store(path, left)
        elif removed:
            retry_busy(lambda: path.unlink(missing_ok=True))
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
    """
    shown = False
    if interactive():
        shown = (show or toast)(title, message)
    if not shown:
        try:
            leave_note(paths or Paths.from_env(), title, message)
        except OSError:
            pass
    return shown
