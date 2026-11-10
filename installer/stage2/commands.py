"""The `ns` and `studio` commands in any new PowerShell or Command Prompt window (card FN-3).

Standard library only: the setup window's bare Python, the engine's first-start reach and `ns doctor` read
this. Every message Nightshift shows that names a command (`ns doctor --bundle`, `studio resume <slug>`)
works only when a new terminal finds that command. So each install has two small command files in
``<home>\\cmd`` (``ns.cmd`` and ``studio.cmd``), and that folder is put on the user PATH.

A command file belongs to the install whose folder holds it ("a program knows its home from where it is"):
it sets NIGHTSHIFT_HOME to that folder and starts that install's own ``venvs\\...\\Scripts\\<name>.exe``,
which also finds its home from its place (`ns_common.paths.home_of_place`), so the two always agree. Its
last command line runs the program and leaves the file in one line (``& call exit /b``): cmd.exe never reads
the file again after the program ends, so a file rewritten by an update while `ns update` runs from it is
never read half old, half new, and the program's exit code is passed through.

Who owns a user PATH entry (the rule of card second-install-takeover, `shortcut_owner.folder_state`): an entry
is Nightshift's when it is a ``cmd`` folder whose ``ns.cmd`` names a Nightshift folder in its
``set "NIGHTSHIFT_HOME=..."`` line, or the ``cmd`` folder of a Nightshift folder setup recorded on this PC
(``installs.txt``); its owner is that folder: `mine`, `other` (exists), `unreachable` (its drive is not there
now), `gone` (the drive is there, the folder is not). Every other entry is not Nightshift's and is never
touched. The first Nightshift entry decides what a bare ``ns`` reaches. Setup appends this install's entry
when there is none (removing `gone` ones in the same write), leaves the PATH as it is when this install's is
first, and leaves another existing install's (or one whose drive is away) unless the owner said yes in this
run. The PATH is read unexpanded, its kind (REG_EXPAND_SZ or REG_SZ) is kept, every other byte of it stays
(empty entries, a trailing ';', duplicates, ``%VAR%`` entries), and it is never cut: a value that would pass
`MAX_USER_PATH` is refused and said.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import installer.stage2.shortcut_owner as so
import ns_common.testguard as testguard
from ns_common.atomic import write_bytes_atomic

FOLDER = "cmd"
#: (command file, venv, program in its Scripts folder, package the program folder holds)
SHIMS = (
    ("ns.cmd", "supervisor", "ns.exe", "ns_supervisor"),
    ("studio.cmd", "studio", "studio.exe", "ns_studio"),
)
CODE = "NS-W120"
#: The one switch (and the answer `shortcuts_here`) that moves the Desktop shortcuts, NIGHTSHIFT_HOME and the
#: commands to this install. One switch, so the Desktop never opens one install and `ns` another by accident.
SWITCH = "Make the Desktop shortcuts and the ns and studio commands open this install"
#: The longest user PATH this code writes: Windows' own Environment Variables window cannot edit (or save) a
#: value longer than this, so the owner could no longer change their PATH by hand.
MAX_USER_PATH = 2047
#: The longest value an environment variable can hold (the machine PATH and the user PATH together).
MAX_ENV = 32767
#: The exit code of a command file whose program is missing (cmd.exe's own "not recognized").
MISSING_EXIT = 9009
#: Under a test run the registry is never read: these stand for the user and the machine PATH.
TEST_USER_PATH_ENV = "NS_TEST_USER_PATH"
TEST_MACHINE_PATH_ENV = "NS_TEST_MACHINE_PATH"

_SET_LINE = re.compile(r'^set "NIGHTSHIFT_HOME=([^"\r\n]+)"\r?$', re.MULTILINE | re.IGNORECASE)
_CARET = re.compile(r"([\^&|<>()])")


def folder(home: Path | str) -> Path:
    return Path(home) / FOLDER


def entry_text(home: Path | str) -> str:
    """The user PATH entry of the install `home`, exactly as setup writes it."""
    return str(folder(home))


def shim_paths(home: Path | str) -> list[Path]:
    return [folder(home) / name for name, _, _, _ in SHIMS]


# ---------------------------------------------------------------------------------------------- the lines
def mine_line(home: Path | str) -> str:
    """L1: the commands are this install's."""
    return (
        "Commands: 'ns' and 'studio' work in any PowerShell or Command Prompt window you open from now on, "
        f"and open this install (their folder {folder(home)} is on your PATH)."
    )


def _full(home: Path | str) -> str:
    ns, studio = shim_paths(home)
    return f"{ns} and {studio}"


def kept_line(home: Path | str, other: str, away: bool = False) -> str:
    """L2 (NS-W120): another install's commands were left on the PATH."""
    first = (
        f"Commands: 'ns' and 'studio' belong to the Nightshift at {other}, whose drive is not there now "
        f"({CODE})." if away else f"Commands: 'ns' and 'studio' open the Nightshift at {other} ({CODE})."
    )
    return (
        f"{first} This install's are {_full(home)}: type the full path, or run INSTALL.cmd again and switch "
        f"'{SWITCH}' on."
    )


def took_line(other: str) -> str:
    """L3: the owner's yes moved the commands here."""
    return (
        f"Commands: 'ns' and 'studio' opened the Nightshift at {other}; now they open this install, as you "
        "chose (in windows you open from now on)."
    )


def failed_line(home: Path | str, why: str) -> str:
    """L4 (NS-W120): the commands could not be put on the PATH; the files still work by their full path."""
    return (
        f"Commands: 'ns' and 'studio' could not be put on your PATH ({why}) ({CODE}). Type {_full(home)} "
        "instead, or run INSTALL.cmd again and choose Repair."
    )


def missing_line(home: Path | str) -> str:
    """L5: what a command file says when its program is not there."""
    return (
        f"Nightshift at {home}: its Python environment is missing. Run INSTALL.cmd from the Nightshift zip "
        "and choose this folder (Repair)."
    )


def no_program_line(home: Path | str) -> str:
    """What a command file says when the program folder is gone (the launch script's own words)."""
    return (
        f"Nightshift at {home} cannot start: its program folder is missing or incomplete. This happens when "
        "an update or a rollback was interrupted. Run INSTALL.cmd from a Nightshift zip of version 0.1.15 or "
        "later and choose this folder: setup puts the program folder back."
    )


def dev_line(home: Path | str) -> str:
    """A dev install never changes the PATH of the PC it is developed on."""
    return f"Commands: a dev install does not put 'ns' and 'studio' on your PATH; type {_full(home)}."


#: The reasons of L4.
UNREAD = "Windows did not say what your PATH holds"
CHANGED = "another program changed your PATH at the same moment, twice"


def too_long(length: int) -> str:
    return (
        f"it would grow to {length} characters, past the {MAX_USER_PATH} that the Windows window for "
        "environment variables can edit; nothing was changed. Remove PATH entries you no longer use first"
    )


def too_long_total(length: int) -> str:
    return (
        f"together with the PC's own PATH it would be {length} characters, past the {MAX_ENV} Windows "
        "allows; nothing was changed. Remove PATH entries you no longer use first"
    )


# ----------------------------------------------------------------------------------------- the files
def shim_problem(home: Path | str, program: Path | str | None = None) -> str | None:
    """Why a command file cannot name this folder (None when it can). The setup folder rules
    (`pathcheck.home_problem`) already refuse these; a hand-set NIGHTSHIFT_HOME may not."""
    for text in (str(home), str(program or home)):
        if any(not 32 <= ord(c) <= 126 for c in text):
            return "this folder's path has characters a command file cannot hold (only plain ASCII)"
        if '"' in text or "%" in text:
            return "this folder's path holds a \" or a %, which a command file cannot hold"
    return None


def _echo(text: str) -> str:
    return "echo " + _CARET.sub(r"^\1", text)


def shim_text(home: Path | str, venv: str, exe: str, program: Path | str, package: str) -> str:
    """The command file for `venv`'s `exe` of the install `home` whose program folder is `program` (pure
    ASCII, CRLF). Every path is inside double quotes, where cmd.exe takes ``& ( ) ^ '`` and spaces as they
    are; the ``echo`` lines escape them with ``^``. Delayed expansion is switched off, so a ``!`` in a path
    or an argument is kept."""
    target = Path(home) / "venvs" / venv / "Scripts" / exe
    lines = [
        "@echo off",
        "setlocal DisableDelayedExpansion",
        "rem Written by Nightshift setup: the command of the install that holds this folder.",
        f'set "NIGHTSHIFT_HOME={home}"',
        f'if not exist "{target}" goto missing',
        f'if not exist "{Path(program) / package / "__init__.py"}" goto noprogram',
        # one line: the program runs, then the file is left without reading another line of it
        f'"{target}" %* & call exit /b %%ERRORLEVEL%%',
        ":missing",
        _echo(missing_line(home)),
        f"exit /b {MISSING_EXIT}",
        ":noprogram",
        _echo(no_program_line(home)),
        f"exit /b {MISSING_EXIT}",
    ]
    return "\r\n".join(lines) + "\r\n"


def write_shims(home: Path | str, program: Path | str) -> str | None:
    """Write ``<home>/cmd/ns.cmd`` and ``studio.cmd`` (each only when its text differs). None when both are
    in place, else why not (the files that could be written stay)."""
    problem = shim_problem(home, program)
    if problem is not None:
        return problem
    try:
        for (_, venv, exe, package), path in zip(SHIMS, shim_paths(home), strict=True):
            data = shim_text(home, venv, exe, program, package).encode("ascii")
            try:
                if path.read_bytes() == data:
                    continue
            except OSError:
                pass
            write_bytes_atomic(path, data)
    except OSError as e:
        return f"the command files could not be written in {folder(home)}: {e}"
    return None


def shims_current(home: Path | str, program: Path | str) -> bool:
    """True when both command files hold exactly what `write_shims` writes for this program folder."""
    try:
        return all(
            path.read_bytes() == shim_text(home, venv, exe, program, package).encode("ascii")
            for (_, venv, exe, package), path in zip(SHIMS, shim_paths(home), strict=True)
        )
    except (OSError, UnicodeEncodeError):
        return False


def named_home(shim: Path) -> str | None:
    """The folder a command file names in its ``set "NIGHTSHIFT_HOME=..."`` line; None when it is not one."""
    try:
        text = shim.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    m = _SET_LINE.search(text)
    return m.group(1).strip() if m else None


# ------------------------------------------------------------------------------------------ the PATH
def split(value: str) -> list[str]:
    """The entries of a PATH value, empty ones included (so a join gives back every byte)."""
    return value.split(";") if value else []


def _expanded(entry: str) -> str:
    return os.path.expandvars(entry.strip().strip('"').strip())


def entry_owner(entry: str, home: Path | str, known: Iterable[str] = ()) -> tuple[str, str | None]:
    """Whose PATH entry, seen from the install `home`: (`mine` | `other` | `unreachable` | `gone`, the
    Nightshift folder it belongs to), or (`none`, None) when it is not Nightshift's."""
    text = _expanded(entry)
    if not text or os.path.basename(text.rstrip("\\/")).lower() != FOLDER:
        return "none", None
    if so.same_folder(text.rstrip("\\/"), folder(home)):
        return so.MINE, str(home)
    named = named_home(Path(text.rstrip("\\/")) / SHIMS[0][0])
    parent = os.path.dirname(text.rstrip("\\/"))
    if named is None and any(so.same_folder(parent, k) for k in known if k):
        named = parent
    if named is None:
        return "none", None
    state = so.folder_state(named, home)
    return state, named


@dataclass(frozen=True)
class Plan:
    """What setup does with the user PATH, and what it says."""

    state: str                          # mine | other | unreachable | none: the first Nightshift entry
    other: str | None = None            # the other install's folder (kept, or taken with a yes)
    new_value: str | None = None        # the value to write; None: nothing is written
    taking: str | None = None           # the other folder a yes takes the commands from
    refusal: str | None = None          # why nothing was written although it was wanted
    removed: tuple[str, ...] = field(default_factory=tuple)


def plan(
    value: str, home: Path | str, yes: bool, known: Iterable[str] = (), machine_len: int = 0,
) -> Plan:
    """The change for this install, by the ownership rule (module docstring). `machine_len` is the length of
    the machine PATH as programs get it (expanded)."""
    known = list(known)
    parts = split(value)
    owners = [entry_owner(p, home, known) for p in parts]
    first = next(((s, f) for s, f in owners if s in (so.MINE, so.OTHER, so.UNREACHABLE)), ("none", None))
    state, other = first
    if state == so.MINE:
        return Plan(so.MINE)
    if state in (so.OTHER, so.UNREACHABLE) and not yes:
        return Plan(state, other)
    drop = (so.GONE, so.OTHER, so.UNREACHABLE) if yes else (so.GONE,)
    keep = [p for p, (s, _) in zip(parts, owners, strict=True) if s not in drop]
    removed = tuple(p for p, (s, _) in zip(parts, owners, strict=True) if s in drop)
    if not any(s == so.MINE for s, _ in owners):
        entry = entry_text(home)
        if ";" in entry:
            return Plan(state, other, refusal="this folder's path holds a ';', which a PATH entry cannot")
        if not keep:
            keep = [entry]
        elif keep[-1] == "":
            keep[-1:] = [entry, ""]     # "a;b;" becomes "a;b;<entry>;": removing it gives "a;b;" back
        else:
            keep.append(entry)
    new = ";".join(keep)
    taking = other if state in (so.OTHER, so.UNREACHABLE) else None
    if len(new) > MAX_USER_PATH and len(new) > len(value):
        return Plan(state, other, taking=taking, refusal=too_long(len(new)))
    total = machine_len + 1 + len(os.path.expandvars(new))
    if total > MAX_ENV and len(new) > len(value):
        return Plan(state, other, taking=taking, refusal=too_long_total(total))
    return Plan(state, other, new_value=new, taking=taking, removed=removed)


#: The write script's exit code when the PATH changed between the read and the write (nothing written).
CHANGED_EXIT = 3
_TEXT_KINDS = ("ExpandString", "String", "REG_EXPAND_SZ", "REG_SZ", "none")


class PathNow(Protocol):
    @property
    def value(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def machine(self) -> int: ...


class WriteDone(Protocol):
    @property
    def returncode(self) -> int: ...

    @property
    def ok(self) -> bool: ...

    @property
    def text(self) -> str: ...


def put_on_path(
    read: Callable[[], PathNow | None],
    write: Callable[[PathNow, str], WriteDone],
    home: Path | str,
    yes: bool,
    known: Iterable[str] = (),
    *,
    taking: str | None = None,
    note: Callable[[str], None] = lambda _folder: None,
) -> tuple[str, str]:
    """Steps C2 and C3: read the user PATH, write it at most once by `plan`, and say it. (owner, line):
    owner `mine`, `other:<folder>`, `refused`, `unread` or `failed`. `taking`: the folder an earlier run of
    this yes noted before its write (a rerun after a kill finds this install's entry and still says what the
    yes moved); `note` is called with the folder a yes takes from BEFORE the write. A PATH another program
    changed between the read and the write is read and planned again, once."""
    known = list(known)
    for _ in range(2):
        now = read()
        if now is None:
            return "unread", failed_line(home, UNREAD)
        if now.kind not in _TEXT_KINDS:
            return "refused", failed_line(home, f"your PATH is stored as {now.kind}, not as text")
        found = plan(now.value, home, yes, known, now.machine)
        if found.state == so.MINE:
            return so.MINE, took_line(taking) if yes and taking else mine_line(home)
        if found.refusal is not None:
            return "refused", failed_line(home, found.refusal)
        if found.new_value is None:         # another install's, kept without a yes
            return f"other:{found.other}", kept_line(home, str(found.other), found.state == so.UNREACHABLE)
        if found.taking:
            note(found.taking)
        res = write(now, found.new_value)
        if res.returncode == CHANGED_EXIT:
            continue
        if not res.ok:
            lines = [ln.strip() for ln in res.text.splitlines() if ln.strip()]
            why = (lines[-1] if lines else f"exit code {res.returncode}")[:200]
            return "failed", failed_line(home, f"Windows refused the change: {why}")
        took = found.taking or (taking if yes else None)
        return so.MINE, took_line(took) if took else mine_line(home)
    return "failed", failed_line(home, CHANGED)


def others_commands(value: str | None, home: Path | str, known: Iterable[str] = ()) -> str | None:
    """The other Nightshift folder whose commands a bare `ns` reaches now (one that exists, or whose drive is
    away); None when they are this install's or nobody's, or the PATH is not known."""
    if value is None:
        return None
    found = plan(value, home, False, known)
    return found.other if found.state in (so.OTHER, so.UNREACHABLE) else None


def registry_user_path() -> tuple[str, str] | None:
    """(the user PATH as stored, unexpanded; its kind) read from HKCU, read only; ("", "none") when there is
    none; None when Windows did not say. A test run never reads the registry: `NS_TEST_USER_PATH` stands
    for it (unset: no PATH)."""
    if testguard.under_test():
        return os.environ.get(TEST_USER_PATH_ENV, ""), "REG_EXPAND_SZ"
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, kind = winreg.QueryValueEx(key, "Path")
    except FileNotFoundError:
        return "", "none"
    except (ImportError, OSError, ValueError):
        return None
    if not isinstance(value, str):
        return None
    return value, "REG_SZ" if kind == 1 else "REG_EXPAND_SZ"


def registry_machine_path() -> str:
    """The machine PATH, expanded ("" when it cannot be read). A test run reads `NS_TEST_MACHINE_PATH`."""
    if testguard.under_test():
        return os.environ.get(TEST_MACHINE_PATH_ENV, "")
    try:
        import winreg

        key_path = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            value, _ = winreg.QueryValueEx(key, "Path")
    except (ImportError, OSError, ValueError):
        return ""
    return os.path.expandvars(str(value))


def first_found(name: str, directories: Iterable[str], pathext: str | None = None) -> Path | None:
    """The file a new terminal runs for the bare command `name`: the first PATH folder, in order, that holds
    `name` with one of the PATHEXT endings (cmd.exe's and PowerShell's search for an external command)."""
    endings = [e for e in (pathext or os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";") if e]
    for directory in directories:
        d = _expanded(directory)
        if not d:
            continue
        for ending in endings:
            candidate = Path(d) / f"{name}{ending.lower()}"
            try:
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
    return None
