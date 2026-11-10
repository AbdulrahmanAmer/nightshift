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

import installer.stage2.shortcut_owner as so

#: The canonical Desktop names (`s09_shortcuts_scheduler.SHORTCUTS`; a test keeps the two equal).
NAMES = (
    "Nightshift Dashboard", "Nightshift Studio", "Nightshift Outbox", "Nightshift - Stop All",
    "Nightshift - Update", "Nightshift - Diagnostics", "Nightshift Code",
)
#: This install's own full set, in its home: made by setup, removed by the uninstaller.
FOLDER = "Shortcuts"
SWITCH = "Make the Desktop shortcuts open this install"
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
        f"run setup again and switch '{SWITCH}' on, or put \"shortcuts_here\": true in the answers file."
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


def question(paths: list[str]) -> str:
    """The console's question (default No)."""
    return (
        f"The Desktop shortcuts open the Nightshift at {_folders(paths)}. Make them open this install "
        "instead?"
    )


def var_kept_line(path: str) -> str:
    return (
        f"NIGHTSHIFT_HOME: left naming the Nightshift at {path}; 'ns' and 'studio' typed in a terminal run "
        "that one. This install's shortcuts and its nightly task name their own folder and do not use it; "
        "the yes that moves the Desktop shortcuts moves it too."
    )


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


def for_window(home: Path, app: Path | None, desktop: Path | None) -> dict[str, Any]:
    """What the Choices screen shows for the folder picked: ``{"state", "path", "line"}``. `other` and
    `unreachable` show the switch (off) with the line; `mine` is everything else: the shortcuts are, or
    will be, this install's without a question."""
    seen = look(desktop, home, app)
    if seen["state"] not in KEPT:
        return {"state": so.MINE, "path": None, "line": None}
    where = _folders(seen["paths"])
    return {"state": seen["state"], "path": where, "line": f"Now they open the Nightshift at {where}"}


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
