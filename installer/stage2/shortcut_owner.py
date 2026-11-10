"""Whose Desktop shortcut is it, and what does it start (card launcher-reach). Standard library only: the
setup window's bare Python reads shortcuts with this too.

A Nightshift shortcut runs ``powershell.exe`` with one of two argument forms:

- direct (0.1.14 and older): ``-File "<home>\\app\\launchers\\<name>.ps1"``;
- through the launch script: ``-File "<home>\\nightshift-launch.ps1" -Launcher
  "<...>\\launchers\\<name>.ps1"``.

The launch script lives beside the program folder, so it can still say what to do when the program folder is
missing. ``.lnk`` files are read here from their bytes (MS-SHLLINK): the strings are UTF-16, so a home with
any characters is read exactly, with no PowerShell and no code page in between.
"""

from __future__ import annotations

import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from ns_common.atomic import copy_atomic

#: The script in the program folder, the name of its copy in the install home, and the copy's format: a
#: copy in the home is replaced only by one whose number is equal or higher (a rollback to an older program
#: never puts an older script under shortcuts and a task that pass it newer switches).
STUB_SOURCE = Path("installer") / "launch.ps1"
STUB_NAME = "nightshift-launch.ps1"
STUB_SCHEMA = 3
_SCHEMA_LINE = re.compile(rb"^# STUB_SCHEMA: (\d+)\s*$", re.MULTILINE)

MINE, GONE, OTHER, UNREACHABLE, FOREIGN = "mine", "gone", "other", "unreachable", "foreign"

_HEAD = "-NoProfile -ExecutionPolicy Bypass -File"
_FORM = re.compile(r'-File\s+"([^"]+)"(?:\s+-Launcher\s+"([^"]+)")?\s*$', re.IGNORECASE)
_CLSID = bytes.fromhex("0114020000000000c000000000000046")


@dataclass(frozen=True)
class Lnk:
    """The strings of a shortcut ("" when it has none)."""

    arguments: str = ""
    working_dir: str = ""
    icon: str = ""
    description: str = ""


def read_lnk(path: Path) -> Lnk:
    """The StringData of the shell link at `path`. `ValueError` when it is not one; `OSError` when it
    cannot be read."""
    data = Path(path).read_bytes()
    if len(data) < 76 or data[:4] != b"\x4c\x00\x00\x00" or data[4:20] != _CLSID:
        raise ValueError(f"{Path(path).name} is not a shortcut")
    flags = struct.unpack_from("<I", data, 20)[0]
    at = 76
    try:
        if flags & 0x01:  # HasLinkTargetIDList
            at += 2 + struct.unpack_from("<H", data, at)[0]
        if flags & 0x02:  # HasLinkInfo
            at += struct.unpack_from("<I", data, at)[0]
        found: dict[int, str] = {}
        for bit in (0x04, 0x08, 0x10, 0x20, 0x40):  # name, relative path, working dir, arguments, icon
            if not flags & bit:
                continue
            count = struct.unpack_from("<H", data, at)[0]
            size = count * 2 if flags & 0x80 else count
            raw = data[at + 2 : at + 2 + size]
            if len(raw) != size:
                raise ValueError("cut short")
            ansi = "mbcs" if os.name == "nt" else "latin-1"
            found[bit] = raw.decode("utf-16-le" if flags & 0x80 else ansi, "replace")
            at += 2 + size
    except (struct.error, ValueError) as e:
        raise ValueError(f"{Path(path).name} is damaged: {e}") from e
    return Lnk(found.get(0x20, ""), found.get(0x10, ""), found.get(0x40, ""), found.get(0x04, ""))


def arguments_for(launcher: Path | str, stub: Path | str | None) -> str:
    """What a shortcut passes to powershell.exe: the launcher through the launch script, or directly."""
    return f'{_HEAD} "{stub}" -Launcher "{launcher}"' if stub is not None else f'{_HEAD} "{launcher}"'


def parse(arguments: str) -> tuple[str, str | None] | None:
    """(launcher, launch script or None) when the arguments are one of the two forms, else None."""
    m = _FORM.search(arguments or "")
    if m is None:
        return None
    first, second = m.group(1), m.group(2)
    launcher, stub = (second, first) if second else (first, None)
    folder = os.path.basename(os.path.dirname(launcher)).lower()
    if not launcher.lower().endswith(".ps1") or folder != "launchers":
        return None
    return launcher, stub


def _same(a: str | Path, b: str | Path) -> bool:
    try:
        if os.path.samefile(a, b):
            return True
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def folder_state(folder: str | Path, home: str | Path) -> str:
    """`mine`: `folder` is the install `home`. `other`: another folder that exists. `unreachable`: its
    drive or share is not there now (unplugged, not mapped: it may come back). `gone`: the drive is there
    and the folder is not."""
    if _same(folder, home):
        return MINE
    if os.path.isdir(folder):
        return OTHER
    drive = os.path.splitdrive(os.path.abspath(folder))[0]
    return GONE if drive and os.path.exists(drive + os.sep) else UNREACHABLE


def opens_home(arguments: str) -> str | None:
    r"""The install folder a shortcut starts (`<home>\app\launchers\<name>.ps1`); None when its arguments
    are not of that form."""
    found = parse(arguments)
    if found is None:
        return None
    program = os.path.dirname(os.path.dirname(found[0]))
    return os.path.dirname(program) if os.path.basename(program).lower() == "app" else None


def owner(arguments: str, home: Path, app: Path | None = None) -> str:
    """Whose shortcut: `mine` (it starts a launcher of the install `home`, or of its program folder `app`),
    `other`, `gone`, `unreachable` (see `folder_state`), or `foreign` (not a Nightshift launcher)."""
    found = parse(arguments)
    if found is None:
        return FOREIGN
    if app is not None and _same(os.path.dirname(os.path.dirname(found[0])), app):
        return MINE
    opened = opens_home(arguments)
    return FOREIGN if opened is None else folder_state(opened, home)


def is_stub_form(arguments: str, home: Path) -> bool:
    """True when the shortcut starts its launcher through the launch script of `home`."""
    found = parse(arguments)
    return found is not None and found[1] is not None and _same(found[1], Path(home) / STUB_NAME)


def stub_schema(path: Path) -> int:
    """The format number of a launch script: 0 when the file is not there, 1 for one without the line (the
    first version of the script had none)."""
    try:
        m = _SCHEMA_LINE.search(Path(path).read_bytes())
    except OSError:
        return 0
    return int(m.group(1)) if m else 1


def install_stub(app: Path, home: Path) -> Path | None:
    """Make `<home>/nightshift-launch.ps1` this program's launch script, unless the one there is of a higher
    format. The copy in the home, or None when there is none (this program has no such script and none was
    copied before). `OSError` when the copy cannot be written."""
    source, target = Path(app) / STUB_SOURCE, Path(home) / STUB_NAME
    have, mine = stub_schema(target), stub_schema(source)
    if mine and mine >= have and (not have or source.read_bytes() != target.read_bytes()):
        copy_atomic(source, target)
    return target if target.is_file() else None
