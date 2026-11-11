r"""D-35: `ns` and `studio` refuse, with NS-E123, when they find no install and were not told where one is.

`Paths.from_env` falls back to `%LOCALAPPDATA%\Nightshift` when NIGHTSHIFT_HOME is unset. In a console that
never got the variable (an old window, a PC where setup has not run) that folder holds no install, and a
command used to go on there: `ns models get voicedesign` planned the GPU model into it and started a 2 GB
download on C:, `studio run` made job folders in it. This check runs once, when a subcommand is invoked,
before any command opens or writes anything.

The one rule (the invariant): no `ns` or `studio` subcommand reads or writes a home that was not NAMED
(NIGHTSHIFT_HOME set and not empty, or `ns update --home <folder>`, which is how the update worker is
started) or FOUND to hold an install (the default folder has `config\runtime.json` or
`config\install_state.json`: every setup writes one of them).

What it does not decide: a NIGHTSHIFT_HOME that names an empty or missing folder is the owner's own statement
(setup itself runs `ns` before the install is complete), so it is never refused. A folder that cannot be
looked at (access denied on `config`) is not "no install": the check cannot tell, says nothing and lets the
command go on as it always did; nothing is deleted or rewritten here, whatever the answer.

Standard library, `typer` and `ns_common.errors` / `ns_common.paths` only: it runs before anything else.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import typer
from typer.core import TyperGroup

from ns_common.errors import NsError
from ns_common.psquote import ps_literal

#: Under `<home>\config`: either file means "an install is here" (setup writes `install_state.json` first).
MARKERS = ("runtime.json", "install_state.json")
#: Commands that name their home with `--home <folder>` (`ns update`'s worker is started that way).
HOME_OPTION_COMMANDS = frozenset({"update"})
HOME_OPTION = "--home"
CODE = "NS-E123"
_META = "nightshift.homecheck.invoked"


def _is_file(path: Path) -> bool | None:
    """True / False for a file that is / is not there; None when that cannot be told (access denied...)."""
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None


def holds_install(home: Path) -> bool | None:
    """True when `home` holds an install (a marker file), False when it surely does not, None when unknown."""
    seen = [_is_file(home / "config" / name) for name in MARKERS]
    if any(s is True for s in seen):
        return True
    return None if any(s is None for s in seen) else False


def default_home(env: Mapping[str, str]) -> Path:
    """Where `Paths.from_env` falls back to when NIGHTSHIFT_HOME is unset: `%LOCALAPPDATA%/Nightshift`.

    The same two lines as `Paths.from_env` (a test holds them equal): this check must not go through `Paths`,
    which a command reads only after the check.
    """
    local = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(local) / "Nightshift"


def _program_home() -> Path | None:
    """The install this program belongs to (`<home>` when it runs from `<home>/app`), or None (a checkout)."""
    from ns_common import first_start

    home = Path(first_start.program_dir()).parent
    return home if holds_install(home) is True else None


def require_install(env: Mapping[str, str] | None = None, *, named_by_option: bool = False) -> None:
    """Raise NS-E123 when no install was named or found (see the module text); else return, touching nothing.

    `env` is the environment to read (default: this process's). `named_by_option` is True when the command
    line itself names the home (`ns update --home`).
    """
    e = os.environ if env is None else env
    if named_by_option or e.get("NIGHTSHIFT_HOME"):
        return
    default = default_home(e)
    if holds_install(default) is not False:  # an install, or a folder that cannot be looked at: go on
        return
    detail = f"Looked in {default}; NIGHTSHIFT_HOME is not set in this window."
    own = _program_home()
    if own is not None:
        detail += (
            f" This program belongs to the install in {own}: run "
            f"$env:NIGHTSHIFT_HOME = {ps_literal(str(own))} in this window, "
            "or use the Nightshift shortcuts."
        )
    raise NsError(CODE, detail)


def _names_home(rest: list[str]) -> bool:
    """True when `rest` (the words after the command's name) carry `--home <non-empty folder>`."""
    for i, word in enumerate(rest):
        if word == "--":
            return False
        if word.startswith(HOME_OPTION + "="):
            return bool(word[len(HOME_OPTION) + 1:])
        if word == HOME_OPTION:
            return i + 1 < len(rest) and bool(rest[i + 1])
    return False


def _only_asks_for_help(command: Any, rest: list[str]) -> bool:
    """`--help` (before any `--`), or a command group named alone: help or a usage error, nothing runs."""
    for word in rest:
        if word == "--":
            break
        if word == "--help":
            return True
    is_group = hasattr(command, "commands")     # duck-typed: typer 0.27 has its own click copy
    return is_group and not getattr(command, "invoke_without_command", False) and not rest


def remember(ctx: Any, name: str | None, command: Any, rest: list[str]) -> None:
    """Keep the subcommand and its words for `check_root` (called from `resolve_command` of a root group)."""
    ctx.meta[_META] = (name, command, list(rest))


class InstallGuardGroup(TyperGroup):
    """The root group of `ns` and `studio`: remembers which subcommand and arguments follow, for `check_root`.

    Click runs the root callback BEFORE the subcommand parses its own arguments (so `--help` of a subcommand
    would reach the callback), and clears the arguments first; `resolve_command` is the one place where both
    the command and its remaining words are known. `ns_studio` cannot import this module at the top (it
    reaches `ns_supervisor` lazily only), so its own group calls `remember` from the same place.
    """

    def resolve_command(self, ctx: Any, args: list[str]) -> Any:
        """Resolve as usual and keep (name, command, rest) in the context for the root callback."""
        name, command, rest = super().resolve_command(ctx, args)
        remember(ctx, name, command, rest)
        return name, command, rest


def _takes_value(group: Any, word: str) -> bool:
    """True when `word` is an option of `group` that takes the next word as its value (`--channel x`)."""
    if "=" in word:
        return False
    for param in getattr(group, "params", ()):
        names = [*getattr(param, "opts", ()), *getattr(param, "secondary_opts", ())]
        if word in names:
            return not (getattr(param, "is_flag", False) or getattr(param, "count", False))
    return False


def command_path(program: str, name: str | None, command: Any, rest: list[str]) -> tuple[str, list[str]]:
    """The command's words ("studio channel add") and the words that follow them, from what `remember` kept:
    the subcommand of each group is the group's first word that is not an option or an option's value."""
    words, args = [program, name or ""], list(rest)
    while command is not None and hasattr(command, "commands"):    # duck-typed, as `_only_asks_for_help`
        i, sub = 0, None
        while i < len(args):
            word = args[i]
            if word == "--":
                break
            if word.startswith("-"):
                i += 2 if _takes_value(command, word) else 1
                continue
            sub = word
            break
        if sub is None or sub not in command.commands:
            break
        words.append(sub)
        command, args = command.commands[sub], args[i + 1:]
    return " ".join(w for w in words if w), args


def check_home(ctx: typer.Context, program: str) -> None:
    """Call in the root callback after `check_root` (card version-mismatch-writes): NS-E128 when the window's
    NIGHTSHIFT_HOME names an install of another version and the command writes (`homeguard.enter`, the one
    table). Before any command runs; nothing for the bare root, `--help`, a group named alone, or a command
    that names its home on its own command line (`ns update --home`: its starter vouched for it)."""
    if ctx.invoked_subcommand is None:
        return
    name, command, rest = ctx.meta.get(_META, (ctx.invoked_subcommand, None, []))
    if _only_asks_for_help(command, rest):
        return
    if name in HOME_OPTION_COMMANDS and _names_home(rest):
        return
    from ns_common import homeguard

    homeguard.enter(*command_path(program, name, command, rest))


def check_root(ctx: typer.Context, env: Mapping[str, str] | None = None) -> None:
    """Call first in the root callback: raises NS-E123 when a subcommand would run with no install.

    Nothing happens for the bare root command, for `--help` anywhere, or for a group named without a command.
    """
    if ctx.invoked_subcommand is None:
        return
    name, command, rest = ctx.meta.get(_META, (ctx.invoked_subcommand, None, []))
    if _only_asks_for_help(command, rest):
        return
    require_install(env, named_by_option=name in HOME_OPTION_COMMANDS and _names_home(rest))
