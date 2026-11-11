"""C2 child environment: the one place that decides what a process the product starts inherits: its
environment (`child_env`) and whether it may open a console window (`window_flags`).

Imports nothing from the product so every module that starts a child (`procs`, `pwsh`, `verify`, `doctor`,
`gpu`, `presence`, `control`, `update`, `client`) can use it without an import cycle.
"""

from __future__ import annotations

import ctypes
import os
import sys
from collections.abc import Iterable, Mapping

#: Variables only the dashboard launcher reads (the setup window sets one to skip the host window). No child
#: the product builds an environment for may inherit them, or a later dashboard would never raise its window.
LAUNCHER_ONLY_ENV = ("NS_DASHBOARD_NO_HOST",)


def scrubbed(env: Mapping[str, str], also: Iterable[str] = ()) -> dict[str, str]:
    """A copy of `env` without `LAUNCHER_ONLY_ENV` and the names in `also` (every child env uses it)."""
    drop = {*LAUNCHER_ONLY_ENV, *also}
    return {k: v for k, v in env.items() if k not in drop}


def child_env(extra: Mapping[str, str] | None = None, *, also: Iterable[str] = ()) -> dict[str, str]:
    """This process's environment for a child: scrubbed, then `extra` on top (`NIGHTSHIFT_HOME` is kept)."""
    return {**scrubbed(os.environ, also=also), **(extra or {})}


#: `CREATE_NO_WINDOW`: the child gets a console of its own that has no window (its pipes still carry output).
CREATE_NO_WINDOW = 0x08000000


def has_console_window() -> bool:
    """True when this process has a console with a window (a terminal, setup's own console, a launcher's
    window, also a hidden one): a child then shares it and opens nothing. False for a process without one:
    the engine (`client.spawn_supervisor` starts it detached), the update worker, and every child that was
    itself started with CREATE_NO_WINDOW. Not Windows: True (there is nothing to hide)."""
    if sys.platform != "win32":
        return True
    try:
        return bool(ctypes.windll.kernel32.GetConsoleWindow())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return True


def window_flags() -> int:
    """The `creationflags` for EVERY console program the product starts and does not show on purpose.

    Windows gives a console program started by a process without a console a NEW console, with a window: a
    black window per child, for the engine about ten of them at a first start (PowerShell for the Desktop
    folder, each shortcut, the nightly task). So such a parent passes CREATE_NO_WINDOW. A parent that has a
    console window passes nothing: the child stays in that console, which is the one the owner is looking
    at (`ns ...` in a terminal, INSTALL.cmd's setup, `ns verify --quick` during `ns update`), and what it
    prints there is still seen.
    """
    return 0 if has_console_window() else CREATE_NO_WINDOW
