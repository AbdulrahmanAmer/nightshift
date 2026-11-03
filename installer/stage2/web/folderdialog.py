"""The "Browse" button of the setup window: the native folder dialog, opened by the server.

The window is a browser page and cannot show a folder dialog that returns a real path, so
``POST /api/pick-folder`` makes the server run ``installer/pick-folder.ps1`` (``powershell.exe -STA``, a
topmost owner window so the dialog is in front of the setup window). Standard library only; every outside
effect is injectable.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from installer.stage2.web.pathcheck import DRIVE_PATH_RE

SCRIPT = Path(__file__).resolve().parents[2] / "pick-folder.ps1"
TIMEOUT_S = 900.0                       # a person is choosing; the dialog ends when they do
FLAGS = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW for the powershell.exe host


class FolderDialogError(RuntimeError):
    """The dialog could not be shown (no PowerShell, the script failed). The window says "type the folder"."""


def argv_for(script: Path, title: str, start: str, out: Path) -> list[str]:
    """The powershell.exe command line (single-threaded apartment: the folder dialog needs it)."""
    return ["powershell.exe", "-NoProfile", "-STA", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File",
            str(script), "-Out", str(out), "-Title", title, "-Start", start]


def pick_folder(
    title: str,
    start: str = "",
    *,
    script: Path = SCRIPT,
    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run,
    timeout_s: float = TIMEOUT_S,
    tmp_dir: Path | None = None,
) -> str | None:
    """The folder the person chose, or None when they cancelled. Raises ``FolderDialogError`` on failure."""
    if not script.is_file():
        raise FolderDialogError(f"{script.name} is missing")
    fd, name = tempfile.mkstemp(prefix="ns-pick-", suffix=".txt", dir=str(tmp_dir) if tmp_dir else None)
    os.close(fd)
    out = Path(name)
    try:
        try:
            proc = run(argv_for(script, title, start, out), capture_output=True, timeout=timeout_s,
                       creationflags=FLAGS, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            raise FolderDialogError(f"{type(e).__name__}: {e}") from e
        if proc.returncode != 0:
            tail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()[-200:]
            raise FolderDialogError(f"the folder window exited with {proc.returncode} {tail}".strip())
        text = out.read_bytes().decode("utf-8-sig", errors="replace").strip()
        return text or None
    finally:
        out.unlink(missing_ok=True)


def install_folder_for(picked: str, *, listdir: Callable[[str], list[str]] = os.listdir) -> str:
    """The install folder to use for a picked folder.

    A drive root (D:) or a folder that already holds other files gets a ``Nightshift`` folder inside it:
    setup, update and uninstall own everything in the install folder and must not touch someone's documents.
    An empty folder (also one made with the dialog's "Make New Folder") and an existing Nightshift install
    (it has config/install_state.json) are used as picked.
    """
    path = picked.strip()
    if len(path) == 2 and path[1] == ":":
        path += "\\"
    if not DRIVE_PATH_RE.match(path):
        return path                                      # not a path on a drive: the validation says why
    if len(path) == 3:
        return path + "Nightshift"
    path = path.rstrip("\\")
    try:
        names = listdir(path)
    except OSError:
        return path                                      # not readable or not there yet: used as given
    if not names or _is_nightshift_home(path, listdir):
        return path
    return path + "\\Nightshift"


def _is_nightshift_home(path: str, listdir: Callable[[str], list[str]]) -> bool:
    try:
        return "install_state.json" in {n.lower() for n in listdir(path + "\\config")}
    except OSError:
        return False
