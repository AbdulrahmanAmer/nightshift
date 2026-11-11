r"""Card version-mismatch-writes: a program never changes an install of ANOTHER version by accident.

The accident: a PowerShell window whose NIGHTSHIFT_HOME still names install A (say 0.1.14), in which the
`ns` or `studio` of install B (a newer or older version) is typed. `Paths.from_env` follows a variable that
was set in the window (second-install-takeover), so B's program would rewrite A's `runtime.json` with B's
fields, which A's own program then refuses at its next start.

ONE function decides "may this program change this home now": `decide(home)`. It is asked at the few places
every writer passes (the Head's ruling of 2026-11-12, table in runs/block-e-version-mismatch-writes/
rework-table.md), never per command:
- the root callback of `ns` and of `studio` (`enter`, through `ns_supervisor.homecheck.check_home`), which
  looks the command up in ONE table, `COMMANDS` (reads / writes); a command missing from it counts as writing,
  and tests/test_version_mismatch_writes.py fails for every registered command that is in no row;
- `ns_studio.jobs.open_env`, the series workshop, a clip job and the database openers of `ns_common.db`
  (`may_write`);
- the start of the engine, of the dashboard, of the dashboard's job child and of the voice server
  (`require_own`), before anything is written; the dashboard's writing routes are in `DASHBOARD_WRITES` (the
  same test walks them);
- every settings writer (`first_start.guard` -> `refuse_write`).
Reads are never refused: a reading command in another version's install is marked as reading for its process,
so `open_env` and the database openers open that install without writing (no folders made, the database
read-only, no migration), and its logger writes no line there (`reading`, asked by `NsLogger`). What SQLite
itself makes beside a database in WAL mode that it opens read-only (`studio.db-shm`, an empty `-wal`) is the
one thing a reader can leave; no setting, record or row changes.

What this guard does NOT do (known limits, said here so nobody reads it as more):
- A program from BEFORE this guard (0.1.14, the 0.1.15 candidates up to rc5) has no such code: in the same
  accident it still writes a newer install. What limits the harm is older than this card: the settings
  models are strict (`config_compat`: a field added later stays out of its file until it is used, and an
  older program refuses a whole file that holds a field it does not know, so it cannot load and re-save
  it), and `Db.migrate` only adds. `dashboard.json` is a plain record that an older program saves with only
  the keys it knows (`save_dashboard_settings`); nothing detects that. A "written by" stamp would only tell
  afterwards, and only for programs that stamp: not built.
- A window with NO NIGHTSHIFT_HOME at all, in which a program of install B is started by its full path while
  `%LOCALAPPDATA%\Nightshift` holds another install: `Paths.from_env` uses that folder (its fallback, card
  second-install-takeover, rule 3), and `decide` answers not-by-variable. Not the accident this card is about
  (F-E29: "reached only through a NIGHTSHIFT_HOME set in that window"); the shims and the launchers set the
  variable to their own install, so only a program typed by full path gets there.
- A settings writer refused here in the MIDDLE of a command (only possible when the other install's version
  changed while the command ran: its own update) says "Nothing was changed" about its own write; what the
  command wrote before that moment, while the versions were equal, stays.

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

READS, WRITES = "reads", "writes"
#: (access, options that turn it to the other access). `access_of` reads it; nothing else decides.
Rule = tuple[str, tuple[str, ...]]
_R: Rule = (READS, ())
_W: Rule = (WRITES, ())
_DESIGN_CHOICES = (
    "--format", "--visual-treatment", "--caption-style", "--voice", "--thumbnail-style", "--cadence-days",
)

#: Every command of both apps, by its words: what it does to the home NIGHTSHIFT_HOME names. The one table
#: (rework-table.md says why for each row). A command not here counts as WRITES.
COMMANDS: dict[str, Rule] = {
    "ns stop": _W,                          # ends that install's engine and processes, removes their records
    "ns doctor": _R,                        # --bundle: the zip goes to the Desktop or --out-dir
    "ns verify": _W,                        # starts the engine, writes the report and the model marks
    "ns tune": (WRITES, ("--dry-run",)),
    "ns update": (WRITES, ("--check",)),    # --home is vouched before the table (homecheck.check_home)
    "ns backup": _R,                        # the zip goes to the Desktop or --out; the database read-only
    "ns restore": _W,
    "ns schedule install": _W,
    "ns schedule remove": _W,
    "ns schedule show": _R,
    "ns notify": _W,                        # a morning note when there is no desktop
    "ns models get": _W,
    "studio run": (WRITES, ("--dry-run",)),
    "studio channel add": _W,
    "studio channel edit": _W,
    "studio channel list": _R,
    "studio channel validate": _R,
    "studio voice list": _W,                # starts that install's voice service
    "studio voice preview": _W,
    "studio voice design": _W,
    "studio voice clone": _W,
    "studio voice apply-pending": _W,
    "studio review": _R,                    # the list (the group's own callback)
    "studio review show": _R,
    "studio review approve": _W,
    "studio review reject": _W,
    "studio mark-uploaded": _W,
    "studio pause": _W,
    "studio resume": _W,
    "studio status": _R,
    "studio dashboard": _W,                 # starts that install's engine and dashboard
    "studio learn": _W,
    "studio calibrate": _W,
    "studio clip": _W,
    "studio series start": _W,
    "studio series concepts": _W,
    "studio series regenerate": _W,
    "studio series choose": _W,
    "studio series map": _W,
    "studio series write": _W,
    "studio series revise": _W,
    "studio series approve": _W,
    "studio series design": (READS, _DESIGN_CHOICES),    # no choice given: it shows the choices
    "studio series produce": _W,
    "studio series status": _R,
    "studio series list": _R,
    "studio ask": _W,                       # assistant.db; its tools start jobs
}

#: Every writing route of the dashboard (ns_dashboard/server.py). None runs in another version's install: the
#: dashboard refuses at its start (`require_own` in `server.main` and `handlers.build_state`).
DASHBOARD_WRITES: frozenset[str] = frozenset({
    "POST /api/engine/warmup", "POST /api/engine/shutdown", "POST /api/engine/release_gpu",
    "POST /api/engine/stop_all",
    "POST /api/channels", "PUT /api/channels/{slug}", "POST /api/channels/{slug}/pause",
    "POST /api/channels/{slug}/resume", "POST /api/channels/{slug}/validate",
    "POST /api/outputs/{output_id:path}/approve", "POST /api/outputs/{output_id:path}/reject",
    "POST /api/outputs/{output_id:path}/uploaded", "POST /api/outputs/{output_id:path}/open-folder",
    "POST /api/uploads", "PUT /api/uploads/{upload_id}", "POST /api/uploads/{upload_id}/complete",
    "POST /api/jobs/run", "POST /api/jobs/verify", "POST /api/jobs/clip", "POST /api/jobs/clip/rerun",
    "POST /api/jobs/{job_id}/resume", "POST /api/jobs/{job_id}/cancel", "POST /api/jobs/{job_id}/dismiss",
    "POST /api/notes/clear", "POST /api/learn/import",
    "PUT /api/settings", "POST /api/settings/keys", "POST /api/diagnostics", "POST /api/settings/export",
    "POST /api/settings/import",
    "POST /api/voices/preview", "POST /api/voices/apply-pending", "POST /api/voices/design",
    "POST /api/voices/clone",
    "POST /api/library/ideas", "PUT /api/library/ideas/{idea_id}", "POST /api/library/footage/fetch",
    "DELETE /api/library/footage/{source}/{clip_id}", "POST /api/library/formats",
    "PUT /api/library/formats/{format_id}", "POST /api/library/series/{slug}/produce",
    "POST /api/models/get", "POST /api/update/file", "POST /api/update/apply",
    "POST /api/assistant/chat", "POST /api/assistant/confirm", "POST /api/assistant/dismiss",
    "POST /api/assistant/queue/run",
})


def _given(args: list[str], options: tuple[str, ...]) -> bool:
    for word in args:
        if word == "--":
            return False
        if word.split("=", 1)[0] in options:
            return True
    return False


def access_of(command: str, args: list[str]) -> str:
    """READS or WRITES for `command` (its words, "studio channel add") with the words that follow it."""
    rule = COMMANDS.get(command)
    if rule is None:
        return WRITES                       # fail closed: an unlisted command is treated as one that writes
    access, flip = rule
    if flip and _given(args, flip):
        return READS if access == WRITES else WRITES
    return access


#: The homes (normalised) this process opens as a READER only (`enter` decided it): `may_write` says False.
_reading: set[str] = set()


def _key(home: Path) -> str:
    return os.path.normcase(os.path.abspath(str(home)))

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


def sentence(v: Verdict, command: str = "", *, verb: str = "change") -> str:
    r"""What the owner reads on a refusal: both installs, both versions, that nothing changed, the way out for
    each install. `command` is the command's words without its folder ("ns tune", "studio channel add");
    with none (a settings writer refused) the other install's `ns` is named. `verb`: "see" for a reading
    command that could not read without a change.

    When the other install's version cannot be read (an update or a rollback may have been killed half way),
    the words also name the way forward that never depends on this program: that install's own shortcuts
    finish or undo it, and INSTALL.cmd's Repair puts the program back.

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
    forward = "" if v.there else (
        " If an update or a rollback of that install was interrupted, its own shortcuts finish it; if they "
        "do not start, run INSTALL.cmd from its Nightshift zip and choose Repair."
    )
    return (
        f"NIGHTSHIFT_HOME in this window names the Nightshift at {v.target} ({there}), but this program is "
        f"Nightshift {v.here} from {v.place}. Nothing was changed. To {verb} {v.target}, use its own "
        f"command: {other}. To work on {v.place}, open a new PowerShell window and run {own} there.{forward}"
    )


def notice(home: Path) -> str:
    """For a READING command: "(Nightshift 0.1.14 there, 0.1.16 here)" when `home` is another install of
    another version reached through the window's variable, else "". Never refuses anything."""
    v = decide(home)
    if not v.foreign:
        return ""
    return f"(Nightshift {v.there or 'of an unreadable version'} there, {v.here} here)"


def refusal(v: Verdict, command: str = "", *, detail: str = "", verb: str = "change") -> Exception:
    """NS-E128 for the foreign verdict `v` (`detail` goes before the sentence)."""
    from ns_common.errors import NsError

    return NsError(
        CODE, detail + sentence(v, command, verb=verb),
        target=str(v.target), place=str(v.place), here=v.here, there=v.there or "",
    )


def require_own(home: Path, command: str = "") -> None:
    """At the start of a process that writes (the engine, the dashboard), before its first side effect.
    Raises NS-E128 with `sentence` when `home` is foreign (`decide`); else nothing."""
    v = decide(Path(home))
    if v.foreign:
        raise refusal(v, command)


def enter(command: str, args: list[str]) -> None:
    """The root callback of `ns` and `studio`, before any command runs (`homecheck.check_home`).

    `command` is the command's words ("ns schedule install"), `args` the words after them. Nothing happens
    unless the window's NIGHTSHIFT_HOME names an install of another version (`decide`); then a command that
    writes (`access_of`, the table) is refused with NS-E128, and one that reads goes on, marked as reading for
    this process, so `may_write` lets it open that install without writing. Only a program that is in an
    install and has NIGHTSHIFT_HOME set looks further than two lookups."""
    if not os.environ.get(nspaths.HOME_ENV) or nspaths.home_of_place() is None:
        return
    home = nspaths.Paths.from_env().home
    v = decide(home)
    if not v.foreign:
        return
    if access_of(command, args) == READS:
        _reading.add(_key(v.target))
        return
    raise refusal(v, command)


def may_write(home: Path) -> bool:
    """`jobs.open_env` and the database openers, before anything is made, opened or migrated.

    True: this program may write `home` (not foreign). False: a reading command of this process is looking at
    another version's install (`enter` marked it): open it without writing. Raises NS-E128 for a foreign home
    no command line decided about (a caller that is not a CLI command): fail closed, nothing written."""
    v = decide(Path(home))
    if not v.foreign:
        return True
    if _key(v.target) in _reading:
        return False
    raise refusal(v)


def reading(home: Path) -> bool:
    """True while this process only looks at `home`, an install of another version (`enter` marked it). Asked
    by `ns_common.logging.NsLogger`: a reader adds no line to that install's logs and makes no folder there.
    One truth test in every process that marked nothing."""
    return bool(_reading) and _key(Path(home)) in _reading


def read_refusal(home: Path, what: Path, error: object) -> Exception:
    """NS-E128 for a reading command that could not read `what` of another version's install without changing
    it (a database file that is missing, or of a schema this program does not read as it is)."""
    v = decide(Path(home))
    return refusal(v, detail=f"{what} could not be read without changing it ({error}). ", verb="see")


def forget() -> None:
    """Tests: forget what `enter` marked as reading (a new process knows nothing)."""
    _reading.clear()


def refuse_write(config: Path) -> None:
    """For `first_start.guard`: the settings folder `config` (`<home>\\config`) is about to be changed. Raises
    for a reading command too: a reader never changes settings."""
    require_own(Path(config).parent)
