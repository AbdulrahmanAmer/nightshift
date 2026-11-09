"""Shape rules for the install folder, shared by the Choices validation and the disk check.

Standard library only. The same rules are mirrored in the window (``installer/ui/src/format.ts``,
``homePathProblem``) for instant feedback; the server's answer stays authoritative. A path may hold spaces
but only printable ASCII characters: the model programs (llama.cpp, qwentts.cpp, CTranslate2) open their
files through the ANSI code page and cannot open a folder named in Arabic or with accented letters. The
folder is never created here: setup creates it when Install starts.
"""

from __future__ import annotations

import ntpath
import os
import re
from collections.abc import Callable, Mapping

from ns_common.pathrules import NON_ASCII_PROBLEM, ascii_problem  # noqa: F401 - re-exported for callers

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
    if (problem := ascii_problem(p)) is not None:
        return problem
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
    n = ntpath.normcase(ntpath.normpath(str(path)))
    return n + chr(92) if len(n) == 2 and n[1] == ":" else n      # "d:" is the root "d:\"


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


def _is_drive_root(path: str) -> bool:
    """True for a drive root such as d: (normalised): it holds every other folder of its drive."""
    drive, rest = ntpath.splitdrive(path)
    return bool(drive) and rest in ("", "\\")


def layout_problem(
    home: str, app_path: str | os.PathLike[str] | None, env: Mapping[str, str] | None = None
) -> str | None:
    """Why ``home`` cannot be the install folder given where the program was extracted; None when it is fine.

    * the extracted folder itself, or a folder inside it: setup copies the program into ``<home>\\app``, which
      cannot sit inside the folder it is copied from, and the extracted folder is meant to be deleted later.
      A zip extracted straight into a drive root is the exception: the root holds every folder of the drive,
      so only the root itself is refused and the other folders of that drive are fine;
    * a folder that has the extracted folder inside it (D-21: ``D:\\NightShift`` around
      ``D:\\NightShift\\runs\\dev-0.1.12``): setup would build its folders around the zip's files, and
      deleting the zip afterwards would be deleting inside the install folder. ``<home>\\app`` is the
      installed copy and is fine;
    * Downloads and OneDrive folders: Windows cleans or syncs them, and sync locks break the update swap.
    """
    h = _norm(home.strip())
    if app_path:
        a = _norm(app_path)
        if h == a:
            return ("That is the folder you extracted Nightshift to. Pick another folder: setup copies the "
                    "program into an \"app\" folder inside the install folder, so the extracted folder can "
                    "be deleted afterwards")
        if _inside(h, a) and not _is_drive_root(a):
            return "That folder is inside the folder you extracted Nightshift to. Pick a folder outside it"
        if _inside(a, h) and a != _norm(h.rstrip("\\") + "\\app"):        # <home>\app is the installed copy
            return ("That folder has the folder you extracted Nightshift to inside it. Pick a folder "
                    f"outside it, such as {EXAMPLE}")
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


# ---------------------------------------------------------------------------------------------------------
# what is already in the folder (D-21, D-22): one rule for Browse, a typed path and the default suggestion
# ---------------------------------------------------------------------------------------------------------
# Marker files as (folder, file). A running or finished install has the first group. An install that was
# uninstalled "keeping my data" has only what the uninstaller leaves: it deletes config\install_state.json
# and the program folder, and keeps the settings and the studio database. stage1.ps1 (Get-FolderKind) picks
# the default folder before any Python exists and repeats these lists; a test keeps the two together.
HOME_MARKERS = (("config", "install_state.json"), ("app", "VERSION"))
KEPT_MARKERS = (("config", "answers.json"), ("config", "runtime.json"), ("data", "studio.db"))

FOLDER_NEW = "new"              # does not exist (or cannot be read): used as given
FOLDER_EMPTY = "empty"
FOLDER_HOME = "home"            # a Nightshift install
FOLDER_KEPT = "kept"            # an uninstalled Nightshift home that kept its data
FOLDER_FOREIGN = "foreign"      # something else, with files in it


def _join(path: str, *names: str) -> str:
    return path.rstrip("\\") + "\\" + "\\".join(names)


def _has_file(path: str, folder: str, name: str, listdir: Callable[[str], list[str]]) -> bool:
    try:
        return name.lower() in {n.lower() for n in listdir(_join(path, folder))}
    except OSError:
        return False


def folder_kind(path: str, *, listdir: Callable[[str], list[str]] = os.listdir) -> str:
    """What ``path`` is: new, empty, a Nightshift home, a kept-data home or a foreign folder."""
    p = path.strip()
    if len(p) == 2 and p[1] == ":":
        p += "\\"
    try:
        names = listdir(p)
    except OSError:
        return FOLDER_NEW
    if not names:
        return FOLDER_EMPTY
    if any(_has_file(p, f, n, listdir) for f, n in HOME_MARKERS):
        return FOLDER_HOME
    if any(_has_file(p, f, n, listdir) for f, n in KEPT_MARKERS):
        return FOLDER_KEPT
    return FOLDER_FOREIGN


def suggest_new_folder(path: str, exists: Callable[[str], bool] = os.path.exists) -> str:
    """A folder name next to ``path`` that is not taken yet: D:\\Nightshift gives D:\\Nightshift-2."""
    base = path.strip().rstrip("\\")
    if len(base) <= 2:
        return f"{base[:2]}\\Nightshift"
    for n in range(2, 100):
        if not exists(f"{base}-{n}"):
            return f"{base}-{n}"
    return f"{base}-new"


def foreign_warning(path: str, exists: Callable[[str], bool] = os.path.exists) -> str:
    """The sentence the window shows for a folder that holds other files (D-21)."""
    where = path.strip()
    if len(where) > 3:
        where = where.rstrip("\\")
    return (f"{where} already has other files in it. Pick an empty folder or a new one, such as "
            f"{suggest_new_folder(path, exists)}")


def install_folder_for(picked: str, *, listdir: Callable[[str], list[str]] = os.listdir) -> str:
    """The install folder to use for a folder picked with Browse.

    A drive root (D:) or a foreign folder gets a ``Nightshift`` folder inside it: setup, update and uninstall
    own everything in the install folder and must not touch someone's documents. An empty folder (also one
    made with the dialog's "Make New Folder"), a Nightshift home and a kept-data home (D-22: the folder an
    uninstall left with the owner's videos and settings, so a reinstall gets them back) are used as picked.
    A typed path is not changed; it gets the same ``folder_kind`` and a warning.
    """
    path = picked.strip()
    if len(path) == 2 and path[1] == ":":
        path += "\\"
    if not DRIVE_PATH_RE.match(path):
        return path                                      # not a path on a drive: the validation says why
    if len(path) == 3:
        return path + "Nightshift"
    path = path.rstrip("\\")
    if folder_kind(path, listdir=listdir) == FOLDER_FOREIGN:
        return path + "\\Nightshift"
    return path
