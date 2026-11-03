"""Shape rules for the install folder, shared by the Choices validation and the disk check.

Standard library only. The same rules are mirrored in the window (``installer/ui/src/format.ts``,
``homePathProblem``) for instant feedback; the server's answer stays authoritative. A path may hold spaces and
non-ASCII letters. The folder is never created here: setup creates it when Install starts.
"""

from __future__ import annotations

import ntpath
import os
import re
from collections.abc import Mapping

MAX_PATH_LEN = 120
DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:\\")
ILLEGAL_CHARS = '<>:"|?*%'
RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)
EXAMPLE = "D:\\Nightshift"


def home_problem(path: str, max_len: int = MAX_PATH_LEN) -> str | None:
    """Why ``path`` cannot be the install folder, in plain words; None when its shape is fine."""
    p = (path or "").strip()
    if not p:
        return f"Type a folder such as {EXAMPLE}, or press Browse"
    if p.startswith(("\\\\", "//")):
        return "Network folders are not supported. Pick a folder on a drive of this PC"
    if not DRIVE_PATH_RE.match(p):
        return f"Use a full path such as {EXAMPLE}"
    rest = p[3:]
    if any(c in ILLEGAL_CHARS or ord(c) < 32 for c in rest):
        return 'That path has a character Windows does not allow: < > : " | ? * %'
    if len(p) > max_len:
        return f"Use a path of {max_len} characters or fewer"
    parts = rest.split("\\")
    if parts and parts[-1] == "":
        parts = parts[:-1]                          # one trailing backslash is fine
    for name in parts:
        if name == "":
            return "Use single backslashes in the path"
        if name in (".", ".."):
            return "Use a plain folder path without . or .."
        if name != name.rstrip(" .") or name.split(".")[0].upper() in RESERVED_NAMES:
            return f'Windows does not allow the folder name "{name}"'
    return None


# ---------------------------------------------------------------------------------------------------------
# where the install folder may be, compared with the folder the zip was extracted to
# ---------------------------------------------------------------------------------------------------------
def _norm(path: str | os.PathLike[str]) -> str:
    return ntpath.normcase(ntpath.normpath(str(path)))


def _inside(child: str, parent: str) -> bool:
    """True when the normalised ``child`` is ``parent`` or below it."""
    return child == parent or child.startswith(parent.rstrip("\\") + "\\")


def known_downloads_folder() -> str | None:
    """The Downloads folder of this user as the shell has it (it can be moved), or None when unknown."""
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _ = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")
        return os.path.expandvars(str(value))
    except (ImportError, OSError):
        return None


def sync_roots(env: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """(name, normalised path) of the folders an install must not live in: Downloads and the OneDrive roots.

    Without ``env`` the real environment and the shell's Downloads folder are used; tests pass a mapping.
    """
    real = env is None
    e: Mapping[str, str] = os.environ if env is None else env
    roots: list[tuple[str, str]] = []
    profile = e.get("USERPROFILE")
    if profile:
        roots.append(("Downloads", _norm(ntpath.join(profile, "Downloads"))))
    if real:
        moved = known_downloads_folder()
        if moved:
            roots.append(("Downloads", _norm(moved)))
    for name in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        if e.get(name):
            roots.append(("OneDrive", _norm(e[name])))
    return roots


def layout_problem(
    home: str, app_path: str | os.PathLike[str] | None, env: Mapping[str, str] | None = None
) -> str | None:
    """Why ``home`` cannot be the install folder given where the program was extracted; None when it is fine.

    * the extracted folder itself, or a folder inside it: setup copies the program into ``<home>\\app``, which
      cannot sit inside the folder it is copied from, and the extracted folder is meant to be deleted later;
    * Downloads and OneDrive folders: Windows cleans or syncs them, and sync locks break the update swap.
    """
    h = _norm(home.strip())
    if app_path:
        a = _norm(app_path)
        if h == a:
            return ("That is the folder you extracted Nightshift to. Pick another folder: setup copies the "
                    "program into an \"app\" folder inside the install folder, so the extracted folder can "
                    "be deleted afterwards")
        if _inside(h, a):
            return "That folder is inside the folder you extracted Nightshift to. Pick a folder outside it"
    why = "Windows cleans or syncs it and Nightshift's files get locked"
    for label, root in sync_roots(env):
        if _inside(h, root):
            return f"Do not install into {label} folders: {why}. Pick a folder such as {EXAMPLE}"
    parts = [p.lower() for p in ntpath.splitdrive(h)[1].split("\\") if p]
    if any(p == "onedrive" or p.startswith("onedrive - ") for p in parts):
        return f"Do not install into OneDrive folders: {why}. Pick a folder such as {EXAMPLE}"
    if len(parts) >= 3 and parts[0] == "users" and parts[2] == "downloads":
        return f"Do not install into the Downloads folder: {why}. Pick a folder such as {EXAMPLE}"
    return None
