r"""Card version-mismatch-writes: a program never changes an install of ANOTHER version by accident.

The accident: a PowerShell window whose NIGHTSHIFT_HOME still names install A (say 0.1.14), in which the
`ns` or `studio` of install B (a newer or older version) is typed. `Paths.from_env` follows a variable that
was set in the window (second-install-takeover), so B's program would rewrite A's `runtime.json` with B's
fields, which A's own program then refuses at its next start.

ONE function decides "may this program change this home now": `decide(home)`. Every settings writer reaches
it through `first_start.guard(path)` (`refuse_write`), and the commands that change Windows or start work
call `require_own(paths, ...)` before their first side effect. Reads are never refused (`notice` gives a
reader the words to show).

Invariant: a process changes an install's settings, its nightly task or its work only when (a) the program
is in no install (setup from the bundle, the update worker's copy under `run`, a checkout), or (b) the
install is the program's own folder (so an update, a rollback and a Repair, which swap the program UNDER
its own home, are never refused, whatever `app\VERSION` says in the moments in between), or (c) the home
was not reached through this process's NIGHTSHIFT_HOME (code named it), or (d) a starter vouched for it
(`NS_HOME_EXPLICIT`: `ns update --home`, setup's test render, `dev/prove.py`), or (e) the versions are equal.

Nothing is remembered between calls: every call reads the truth again (`app\VERSION` of the other install
may change while this process lives: its own update), so no answer can go stale. A call costs one `is_file`
and, only when the folders differ, one small read.

Standard library plus `ns_common.paths` (the setup window's bare Python reaches this through `first_start`);
`ns_common.errors` is imported only when a refusal is raised.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import ns_common.commandline as commandline
import ns_common.paths as nspaths
from ns_common import __version__

#: The refusal's code (`contracts/errors.json`). The card planned NS-E127; item model-file-damaged took it.
CODE = "NS-E128"


@dataclass(frozen=True)
class Verdict:
    foreign: bool               # True: this program must not change `target`
    why: str                    # one of the WHY_* words: which rule decided
    target: Path                # the home asked about
    place: Path | None          # the install this program is in (None: in no install)
    here: str                   # this program's version
    there: str | None           # the target's `app\VERSION` (None: missing, empty or unreadable)


WHY_NO_PLACE, WHY_OWN, WHY_NOT_BY_VARIABLE, WHY_VOUCHED, WHY_DEFAULT, WHY_SAME_VERSION, WHY_OTHER_VERSION = (
    "no-place", "own-install", "not-by-variable", "vouched", "default", "same-version", "other-version",
)


def version_of(home: Path) -> str | None:
    r"""`<home>\app\VERSION` as written (a BOM and blanks removed). None when it is missing, empty or cannot
    be read: an install whose version nobody can tell counts as another version (never guessed equal)."""
    try:
        text = (Path(home) / "app" / "VERSION").read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        return None
    return text or None


def decide(home: Path) -> Verdict:
    """May this program change the install at `home` now? The one decision (see the module text)."""
    target = Path(home)
    place = nspaths.home_of_place()
    if place is None:
        return Verdict(False, WHY_NO_PLACE, target, None, __version__, None)
    if nspaths._same_folder(place, target):
        return Verdict(False, WHY_OWN, target, place, __version__, None)
    raw = os.environ.get(nspaths.HOME_ENV)
    if not raw or not nspaths._same_folder(raw, target):
        return Verdict(False, WHY_NOT_BY_VARIABLE, target, place, __version__, None)
    kind = nspaths.home_variable_kind(raw, os.environ)
    if kind == nspaths.VOUCHED:
        return Verdict(False, WHY_VOUCHED, target, place, __version__, None)
    there = version_of(target)
    if kind == nspaths.DEFAULT:
        # `Paths.from_env` gives way to the program's own install for the PC's default, so only code that
        # named this folder itself gets here: not the window's accident.
        return Verdict(False, WHY_DEFAULT, target, place, __version__, there)
    if there is not None and there == __version__:
        return Verdict(False, WHY_SAME_VERSION, target, place, __version__, there)
    return Verdict(True, WHY_OTHER_VERSION, target, place, __version__, there)


def sentence(v: Verdict, command: str = "") -> str:
    r"""What the owner reads on a refusal: both installs, both versions, that nothing changed, the way out for
    each install. `command` is what was typed without its folder ("ns tune", "studio pause"); with none (a
    settings writer refused) the other install's `ns` is named.

    Each command is given by full path, in the form a terminal takes (`commandline.by_full_path`: the shim
    `<home>\cmd\<program>.cmd` of FN-3 when the install has one, else the program in its own venv), because
    a plain `ns` in this window is exactly what reached the wrong install."""
    words = command.split()
    program = words[0] if words and words[0] in ("ns", "studio") else "ns"
    asked = " ".join(words) if words and words[0] == program else program
    there = f"version {v.there}" if v.there else (
        f"its version could not be read: {v.target / 'app' / 'VERSION'} is missing, empty or cannot be read"
    )
    other = commandline.by_full_path(v.target, asked)
    own = commandline.by_full_path(v.place, program) if v.place is not None else program
    return (
        f"NIGHTSHIFT_HOME in this window names the Nightshift at {v.target} ({there}), but this program is "
        f"Nightshift {v.here} from {v.place}. Nothing was changed. To change {v.target}, use its own "
        f"command: {other}. To work on {v.place}, open a new PowerShell window and run {own} there."
    )


def notice(home: Path) -> str:
    """For a READING command: "(Nightshift 0.1.14 there, 0.1.16 here)" when `home` is another install of
    another version reached through the window's variable, else "". Never refuses anything."""
    v = decide(home)
    if not v.foreign:
        return ""
    return f"(Nightshift {v.there or 'of an unreadable version'} there, {v.here} here)"


def require_own(home: Path, command: str = "") -> None:
    """Call at the top of a command that changes settings, the nightly task or starts work, before its first
    side effect. Raises NsError NS-E128 with `sentence` when `home` is foreign (`decide`); else nothing."""
    v = decide(Path(home))
    if v.foreign:
        from ns_common.errors import NsError

        raise NsError(
            CODE, sentence(v, command),
            target=str(v.target), place=str(v.place), here=v.here, there=v.there or "",
        )


def refuse_write(config: Path) -> None:
    """For `first_start.guard`: the settings folder `config` (`<home>\\config`) is about to be changed."""
    require_own(Path(config).parent)
