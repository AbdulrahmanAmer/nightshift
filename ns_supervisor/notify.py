"""C2 Windows toast through PowerShell `Windows.UI.Notifications` (spec J10). No extra package.

Title and message travel in environment variables, never inside the script text, so they cannot
break out of the PowerShell source. Everything is best effort: unavailable means no-op.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ns_common.atomic import write_text_atomic
from ns_common.paths import Paths
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
    """Show a toast; True if PowerShell accepted it. Never raises (no PowerShell, headless: no-op)."""
    try:
        r = pwsh.run_powershell(
            TOAST_SCRIPT, env_extra={"NS_TOAST_TITLE": title, "NS_TOAST_MESSAGE": message}, timeout_s=30
        )
    except (pwsh.PowerShellMissing, OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


# --- the nightly task may run in session 0 (no desktop): no toast can show there, so leave a morning note ---
NOTE_FILE = "morning_note.json"
NOTE_KEEP = 20


def session_is_interactive() -> bool:
    """False in session 0 (services, S4U tasks): there is no desktop to show a toast on. True elsewhere."""
    if sys.platform != "win32":
        return True
    try:
        sid = ctypes.c_ulong()
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        ok = kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))
    except (AttributeError, OSError):
        return True
    return not ok or sid.value != 0


def note_path(paths: Paths) -> Path:
    """`run\morning_note.json`: messages a toast could not show; the dashboard displays and clears them."""
    return paths.home / "run" / NOTE_FILE


def read_notes(paths: Paths) -> list[dict[str, str]]:
    """The waiting notes, oldest first (empty when none or unreadable)."""
    try:
        data = json.loads(note_path(paths).read_text(encoding="utf-8"))
        notes = data["notes"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    return [n for n in notes if isinstance(n, dict)] if isinstance(notes, list) else []


def leave_note(paths: Paths, title: str, message: str, *, now: datetime | None = None) -> None:
    """Queue a message for the next time somebody opens the dashboard (atomic write, newest 20 kept)."""
    notes = read_notes(paths)
    at = (now or datetime.now()).isoformat(timespec="seconds")
    notes.append({"at": at, "title": title, "message": message})
    write_text_atomic(note_path(paths), json.dumps({"notes": notes[-NOTE_KEEP:]}, indent=2) + "\n")


def clear_notes(paths: Paths) -> None:
    """Delete the waiting notes (the dashboard did show them)."""
    note_path(paths).unlink(missing_ok=True)


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
