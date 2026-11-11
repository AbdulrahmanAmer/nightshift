"""Whose the Desktop shortcuts are, as setup reads and says it (card second-install-takeover).

Standard library only: the setup window runs on the bare Python. A PC has one Desktop and may have several
Nightshift folders. An install changes a Desktop shortcut only when it is its own, belongs to no folder that
exists, or the owner said yes in this run (`shortcuts_here`); every install also keeps its full set in
``<home>/Shortcuts``, so "kept" never leaves one without a way to start. One place for:

* the reading (`look`): each canonical shortcut's owner by `shortcut_owner.read_owner`, and the state of the
  Desktop as a whole. Another install's shortcut among them makes the whole Desktop that install's: nothing
  is mixed in beside it;
* the sentences (the scheduler step, the console summary and the window's Done screen say the same words);
* what the window shows under its switch for the folder picked (`for_window`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import installer.stage2.commands as commands
import installer.stage2.shortcut_owner as so

#: The canonical Desktop names (`s09_shortcuts_scheduler.SHORTCUTS`; a test keeps the two equal).
NAMES = (
    "Nightshift Dashboard", "Nightshift Studio", "Nightshift Outbox", "Nightshift - Stop All",
    "Nightshift - Update", "Nightshift - Diagnostics", "Nightshift Code",
)
#: This install's own full set, in its home: made by setup, removed by the uninstaller.
FOLDER = "Shortcuts"
#: The one switch: it moves the Desktop shortcuts, NIGHTSHIFT_HOME and the `ns` and `studio` commands (FN-3).
SWITCH = commands.SWITCH
KEPT = (so.OTHER, so.UNREACHABLE)
NONE = "none"


def look(desktop: Path | None, home: Path, app: Path | None, names: Any = None) -> dict[str, Any]:
    """What the Desktop holds under the canonical names, seen from the install `home`.

    ``files``: name -> (owner, the folder it names). ``state``: `other` or `unreachable` when at least one
    belongs to a folder that exists or whose drive is not there now (``paths`` names them, in order);
    else `gone` (one opens a deleted Nightshift, ``gone`` names it), `mine`, or `none`. ``foreign``: the
    names held by a file Nightshift did not make."""
    wanted = [str(n) for n in names] if isinstance(names, (list, tuple)) and names else list(NAMES)
    files = {
        name: so.read_owner(desktop / f"{name}.lnk", home, app) if desktop is not None else (NONE, None)
        for name in wanted
    }
    kept = [(state, path) for state, path in files.values() if state in KEPT and path]
    paths = list(dict.fromkeys(path for _, path in kept))
    gone = list(dict.fromkeys(path for state, path in files.values() if state == so.GONE and path))
    states = {state for state, _ in files.values()}
    if kept:
        state = so.OTHER if any(s == so.OTHER for s, _ in kept) else so.UNREACHABLE
    elif gone:
        state = so.GONE
    else:
        state = so.MINE if so.MINE in states else NONE
    foreign = [name for name, (owner, _) in files.items() if owner == so.FOREIGN]
    return {"state": state, "paths": paths, "gone": gone, "foreign": foreign, "files": files}


def owner_flag(seen: dict[str, Any]) -> str:
    """The wizard flag `shortcuts_owner` for a reading: `other:<folder>` (several: joined with `|`) while
    the Desktop is another install's, else `mine`."""
    return "other:" + "|".join(seen["paths"]) if seen["state"] in KEPT else so.MINE


def _folders(paths: list[str]) -> str:
    return " and ".join(paths)


def kept_line(seen: dict[str, Any], home: Path) -> str:
    """NS-W119: the Desktop was left to the other install; where this one's shortcuts are; the way out."""
    away = ", which cannot be reached now (its drive is not there)" if seen["state"] == so.UNREACHABLE else ""
    return (
        f"Desktop shortcuts: kept for the Nightshift at {_folders(seen['paths'])}{away} (NS-W119). This "
        f"install's shortcuts are in {Path(home) / FOLDER}. To make the Desktop shortcuts open this install, "
        f"run setup again and switch '{SWITCH}' on, or put \"shortcuts_here\": true in the answers file; "
        "that yes also makes NIGHTSHIFT_HOME name this install and moves the 'ns' and 'studio' commands here."
    )


MINE_LINE = "Desktop shortcuts: they open this install."


def gone_line(paths: list[str]) -> str:
    return f"{MINE_LINE} They opened a deleted Nightshift ({_folders(paths)}) before."


def took_line(paths: list[str]) -> str:
    return (
        f"Desktop shortcuts: they opened the Nightshift at {_folders(paths)}; now they open this install, as "
        "you chose."
    )


def foreign_line(name: str) -> str:
    return f"Desktop shortcut '{name}' is not one Nightshift made; it was left as it is."


def question(paths: list[str], var: str | None = None, cmds: str | None = None) -> str:
    """The console's question (default No). It is asked when the Desktop shortcuts (`paths`), the user
    variable NIGHTSHIFT_HOME (`var`: the other folder it names), the `ns` and `studio` commands (`cmds`: the
    other folder whose commands come first on the user PATH) or several of them are another install's, and
    says exactly what a yes moves."""
    if cmds:
        said = [f"the Desktop shortcuts open the Nightshift at {_folders(paths)}"] if paths else []
        said += [f"NIGHTSHIFT_HOME names the Nightshift at {var}"] if var else []
        said.append(f"'ns' and 'studio' open the Nightshift at {cmds}")
        text = said[0] if len(said) == 1 else ", ".join(said[:-1]) + " and " + said[-1]
        one = len(said) == 1
        ask = "Make them open this install instead?" if one else "Make all of them this install's instead?"
        return f"{text[0].upper()}{text[1:]}. {ask}"
    if paths and var:
        return (
            f"The Desktop shortcuts open the Nightshift at {_folders(paths)} and NIGHTSHIFT_HOME names the "
            f"Nightshift at {var}. Make both this install's instead?"
        )
    if var:
        return f"NIGHTSHIFT_HOME names the Nightshift at {var}. Make it name this install instead?"
    return (
        f"The Desktop shortcuts open the Nightshift at {_folders(paths)}. Make them open this install "
        "instead?"
    )


def var_kept_line(path: str) -> str:
    return (
        f"NIGHTSHIFT_HOME: left naming the Nightshift at {path}. This install's shortcuts, its nightly task "
        "and its 'ns' and 'studio' commands name their own folder and do not use it. "
        f"To make it name this install, run setup again and switch '{SWITCH}' on (the setup window "
        "offers it while this variable, the Desktop shortcuts or the commands are another install's), or "
        "put \"shortcuts_here\": true in the answers file: that yes moves NIGHTSHIFT_HOME and the Desktop "
        "shortcuts here, and the commands."
    )


def var_of_another(named: str | None, home: Path) -> str | None:
    """The other Nightshift folder the user variable NIGHTSHIFT_HOME names (one that exists, or whose drive
    is not there now); None when it names this folder, nothing, or a folder that is gone."""
    value = (named or "").strip()
    if not value:
        return None
    return value if so.folder_state(value, home) in KEPT else None


def var_took_line(path: str) -> str:
    return (
        f"NIGHTSHIFT_HOME: it named the Nightshift at {path}; now it names this install, as you chose. "
        "Open a new terminal to use it (programs that are already open keep the old value)."
    )


def dev_task_line(path: str) -> str:
    return (
        f"Dev mode: the nightly task belongs to the Nightshift at {path}; a dev install never takes it, so "
        "it was left as it is."
    )


def for_window(
    home: Path, app: Path | None, desktop: Path | None, var: str | None = None, user_path: str | None = None,
) -> dict[str, Any]:
    """What the Choices screen shows for the folder picked: ``{"state", "path", "line"}``. `other` and
    `unreachable` show the switch (off) with the line; `mine` is everything else: nothing is another
    install's, so there is nothing to ask.

    The one yes moves the Desktop shortcuts, the user variable NIGHTSHIFT_HOME (`var` is its value) and the
    `ns` and `studio` commands (`user_path`: the user PATH, card FN-3), so the switch is offered while ANY of
    them is another install's, and the line says which and what the switch moves."""
    seen = look(desktop, home, app)
    named = var_of_another(var, home)
    cmds = commands.others_commands(user_path, home)
    commands_too = f"; 'ns' and 'studio' open the Nightshift at {cmds}" if cmds else ""
    if seen["state"] in KEPT:
        where = _folders(seen["paths"])
        line = f"Now they open the Nightshift at {where}"
        if named:
            line += f", and NIGHTSHIFT_HOME names the Nightshift at {named}; on moves both here"
        if cmds:
            line += f"{commands_too}; on moves them here too"
        return {"state": seen["state"], "path": where, "line": line}
    if named:
        line = (
            f"The Desktop shortcuts are not another install's, but NIGHTSHIFT_HOME names the Nightshift at "
            f"{named}; on makes it name this install"
        )
        if cmds:
            line += f"{commands_too}; on moves them here too"
        return {"state": so.OTHER, "path": named, "line": line}
    if cmds:
        line = (
            f"The Desktop shortcuts are not another install's, but 'ns' and 'studio' open the Nightshift at "
            f"{cmds}; on makes them open this install"
        )
        return {"state": so.OTHER, "path": cmds, "line": line}
    return {"state": so.MINE, "path": None, "line": None}


def summary(home: Path, app: Path | None, desktop: Path | None, flags: dict[str, Any]) -> dict[str, Any]:
    """What the end of setup says about the Desktop shortcuts: ``{"state", "line"}``.

    The scheduler step leaves its sentence in the flag ``shortcuts_line`` with the owner it found
    (``shortcuts_owner``). A run in which that step had nothing to do still reaches the summary: when the
    Desktop is not that owner's any more (another install took it, or gave it up), the stored sentence would
    be untrue and the state as it is now is said instead."""
    seen = look(desktop, home, app, flags.get("shortcuts_names"))
    line = flags.get("shortcuts_line")
    if not isinstance(line, str) or not line or flags.get("shortcuts_owner") != owner_flag(seen):
        if seen["state"] in KEPT:
            line = kept_line(seen, home)
        elif seen["state"] == so.MINE:
            line = MINE_LINE
        else:
            line = "Desktop shortcuts: none of this install on the Desktop; run setup again to make them."
    return {"state": seen["state"], "line": line}
