"""C2 child environment: the one place that decides what a process the product starts inherits.

Imports nothing from the product so every module that starts a child (`procs`, `pwsh`, `verify`, `doctor`,
`gpu`, `presence`, `control`, `update`, `client`) can use it without an import cycle.
"""

from __future__ import annotations

import os
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
