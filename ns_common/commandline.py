"""Which program a plain `ns` or `studio` typed in a new terminal runs, and every text that names one (FN-3).

Standard library only (the setup window's bare Python reads it). Setup puts `<home>\\cmd` (`ns.cmd`,
`studio.cmd`) on the user PATH, but a plain `ns` reaches THIS install's file only when nothing comes before
it: another Nightshift install's command folder, another program's `ns` or `studio` (the NativeScript CLI's
`ns.cmd` in npm's folder, JetBrains Toolbox's or Android Studio's `studio`), or no entry at all (refused, a
dev install). A message that tells the owner to type `ns update --rollback` would then run something else,
for example roll ANOTHER install back. So:

* `reach(home)`: what a new window finds for both names, searched as Windows does: the user folder first
  (cmd.exe looks in its current directory, where a new window starts, before the PATH), then the machine
  PATH, then the user PATH, each folder with every PATHEXT ending in order;
* `typed(home, words)`: the command as the owner should type it: the plain words when they reach this
  install, else this install's own command file by its full path (its venv program when there is none);
* `render(text, home)`: every command a text names, given the same way. The sinks call it: the hints and
  messages of `contracts/errors.json` (`ns_common.errors.describe`), an error's detail (`NsError`), the
  morning notes and toasts (`ns_supervisor.notify`). A text whose commands reach this install is returned
  unchanged, and so is any text when the PATH or the home is not known (nothing is changed on a guess).

A test run never reads the registry: `NS_TEST_USER_PATH` and `NS_TEST_MACHINE_PATH` stand for the two
PATH values (`NS_TEST_USER_PATH` unset: not known) and `NS_TEST_PROFILE` for the user folder.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

import ns_common.testguard as testguard


def runtime_is_dev(runtime_file: Path) -> bool:
    """`"dev_mode": true` in `runtime.json` (`ns_common.devmode.runtime_is_dev`'s rule; read here without
    importing it: `ns_common.errors` imports this module, and devmode's imports reach back to it)."""
    try:
        data = json.loads(runtime_file.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("dev_mode") is True

FOLDER = "cmd"
TEST_USER_PATH_ENV = "NS_TEST_USER_PATH"
TEST_MACHINE_PATH_ENV = "NS_TEST_MACHINE_PATH"
TEST_PROFILE_ENV = "NS_TEST_PROFILE"
HOME_ENV = "NIGHTSHIFT_HOME"
#: (command name, venv, program in its Scripts folder)
PROGRAMS = (("ns", "supervisor", "ns.exe"), ("studio", "studio", "studio.exe"))
#: The commands of each program and the subcommands of its groups (`tests/test_commands_on_path.py` keeps
#: them equal to the CLIs), so a mention is recognised with exactly its own words.
COMMANDS: dict[str, dict[str, tuple[str, ...]]] = {
    "ns": {
        "stop": (), "doctor": (), "verify": (), "tune": (), "update": (), "backup": (), "restore": (),
        "notify": (), "schedule": ("install", "remove", "show"), "models": ("get",),
    },
    "studio": {
        "ask": (), "run": (), "mark-uploaded": (), "pause": (), "resume": (), "status": (), "dashboard": (),
        "learn": (), "calibrate": (), "channel": ("add", "edit", "list", "validate"),
        "voice": ("apply-pending", "clone", "design", "list", "preview"), "clip": (),
        "series": (
            "approve", "choose", "concepts", "design", "list", "map", "produce", "regenerate", "revise",
            "start", "status", "write",
        ),
        "review": ("approve", "reject", "show"),
    },
}
_SET_LINE = re.compile(r'^set "NIGHTSHIFT_HOME=([^"\r\n]+)"\r?$', re.MULTILINE | re.IGNORECASE)
#: `ns <command> [<subcommand>] [--option [<value>] | -x | <placeholder>]...`, not inside a path or a word
_MENTION = re.compile(
    r"(?<![\w.\\/:%-])(ns|studio) ([a-z][a-z-]*)((?: [a-z][a-z-]*)?)((?: (?:--?[a-z][a-z-]*|<[^<>]+>))*)"
)


def same_folder(a: str | Path, b: str | Path) -> bool:
    """The same folder (case, a trailing backslash, 8.3 names, subst and junctions of folders that exist)."""
    try:
        if os.path.samefile(a, b):
            return True
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def split(value: str) -> list[str]:
    """The entries of a PATH value, empty ones included (so a join gives back every byte)."""
    return value.split(";") if value else []


def expanded(entry: str) -> str:
    return os.path.expandvars(entry.strip().strip('"').strip())


def named_home(shim: Path) -> str | None:
    """The folder a command file names in its ``set "NIGHTSHIFT_HOME=..."`` line; None when it is not one."""
    try:
        text = shim.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    m = _SET_LINE.search(text)
    return m.group(1).strip() if m else None


def registry_user_path() -> tuple[str, str] | None:
    """(the user PATH as stored, unexpanded; its kind) from HKCU, read only; ("", "none") when there is none;
    None when it is not known (Windows did not say; a test run that set no stand-in)."""
    if testguard.under_test():
        value = os.environ.get(TEST_USER_PATH_ENV)
        return None if value is None else (value, "REG_EXPAND_SZ")
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
    """The machine PATH, expanded ("" when it cannot be read)."""
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


def user_folder() -> str:
    """Where a new terminal starts (cmd.exe looks there before the PATH)."""
    if testguard.under_test():
        return os.environ.get(TEST_PROFILE_ENV, "")
    return os.environ.get("USERPROFILE", "")


def first_found(name: str, directories: list[str], pathext: str | None = None) -> Path | None:
    """The file a new terminal runs for the plain command `name`: the first folder, in order, that holds
    `name` with one of the PATHEXT endings."""
    endings = [e for e in (pathext or os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(";") if e]
    for directory in directories:
        d = expanded(directory)
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


@dataclass(frozen=True)
class Reach:
    """What a plain `ns` and `studio` run in a new terminal, seen from the install `home`. `ok`: True when
    both are this install's command files; False when not (`why` says what instead); None when not known."""

    ok: bool | None
    why: str = ""
    other: str | None = None        # another Nightshift install whose command files come first


def reach(
    home: Path | str, user_value: str | None = None, machine_value: str | None = None,
    profile: str | None = None,
) -> Reach:
    """`Reach` for `home`. `user_value` / `machine_value`: the PATH values to judge (default: as Windows has
    them now, read only); `profile`: the folder a new terminal starts in."""
    folder = Path(home) / FOLDER
    if runtime_is_dev(Path(home) / "config" / "runtime.json"):
        return Reach(False, "a dev install does not put them on your PATH")
    if user_value is None:
        found = registry_user_path()
        if found is None:
            return Reach(None)
        user_value = found[0]
    machine = registry_machine_path() if machine_value is None else machine_value
    start = user_folder() if profile is None else profile
    directories = [start, *split(machine), *split(user_value)]
    for name, _, _ in PROGRAMS:
        hit = first_found(name, directories)
        if hit is not None and same_folder(hit.parent, folder) and (folder / f"{name}.cmd").is_file():
            continue
        if hit is None:
            return Reach(False, "they are not on your PATH")
        other = named_home(hit.parent / "ns.cmd") if hit.parent.name.lower() == FOLDER else None
        if other is not None:
            return Reach(False, f"'ns' and 'studio' open the Nightshift at {other}", other)
        at_start = bool(start) and same_folder(hit.parent, start)
        where = " (the folder a new Command Prompt starts in)" if at_start else ""
        return Reach(False, f"'{name}' runs {hit}{where} first")
    return Reach(True)


#: (home, until, Reach): one answer per process for a short while (a message names several commands).
_cached: list[tuple[str, float, Reach]] = []
CACHE_S = 30.0


def reach_now(home: Path | str) -> Reach:
    """`reach(home)` as Windows has the PATH now, kept for `CACHE_S` (never under a test run)."""
    key = os.path.normcase(str(home))
    now = time.monotonic()
    if not testguard.under_test():
        for k, until, found in _cached:
            if k == key and until > now:
                return found
    found = reach(home)
    _cached[:] = [(key, now + CACHE_S, found)]
    return found


def own_file(home: Path | str, tool: str) -> Path:
    """This install's own command file for `tool`; its venv program when the file is not there."""
    shim = Path(home) / FOLDER / f"{tool}.cmd"
    if shim.is_file():
        return shim
    venv, exe = next((v, e) for n, v, e in PROGRAMS if n == tool)
    return Path(home) / "venvs" / venv / "Scripts" / exe


def by_full_path(home: Path | str, words: str) -> str:
    """`words` (`ns update --rollback`) with this install's own program in place of the plain name. A path
    with a space or a character PowerShell reads is given in PowerShell's form (`& "<path>" ...`)."""
    tool, _, rest = words.partition(" ")
    path = str(own_file(home, tool))
    head = path if re.fullmatch(r"[A-Za-z0-9:\\._-]+", path) else f'& "{path}"'
    return f"{head} {rest}".rstrip()


def typed(home: Path | str | None, words: str) -> str:
    """The command `words` as the owner should type it in a new terminal to reach the install `home`."""
    if not home:
        return words
    found = reach_now(home)
    return by_full_path(home, words) if found.ok is False else words


def _mention(m: re.Match[str]) -> str | None:
    tool, command, sub, rest = m.group(1), m.group(2), m.group(3).strip(), m.group(4)
    subs = COMMANDS[tool].get(command)
    if subs is None:
        return None
    if sub and sub not in subs:
        return f"{tool} {command}"          # a word of the sentence follows ("run ns update again")
    return f"{tool} {command}" + (f" {sub}" if sub else "") + rest


def render(text: str, home: Path | str | None = None) -> str:
    """`text` with every `ns ...` and `studio ...` it names given as `typed` gives it (unchanged when they
    reach `home`, or nothing is known). `home` defaults to this process's NIGHTSHIFT_HOME."""
    where = home or os.environ.get(HOME_ENV)
    if not text or not where or ("ns " not in text and "studio " not in text):
        return text
    found = reach_now(where)
    if found.ok is not False:
        return text

    def swap(m: re.Match[str]) -> str:
        words = _mention(m)
        if words is None:
            return m.group(0)
        tail = m.group(0)[len(words):]
        return by_full_path(where, words) + tail

    return _MENTION.sub(swap, text)
