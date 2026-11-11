r"""F-10c: a rollback that is interrupted at any point leaves settings the program in `<app>` can load.

Why: `ns update --rollback` changes two things that cannot change together, the settings files and the program
folder (three renames: `<app>` -> `<app>.swap`, `<app>.prev` -> `<app>`, `<app>.swap` -> `<app>.prev`). The
older program it puts back may have no rollback code at all (0.1.14 has none), so whatever it finds when it
starts must already be settings it can load: the settings are made readable by it BEFORE its program is put in
place, and the newer program, which can read both shapes, finishes or undoes the rest.

The order (`ns_supervisor.update.rollback_app`):

1. This record, `<app>.rollback-pending.json`, is written FIRST: which program leaves and which comes back
   (fingerprints, and the install stamp of each: fingerprint plus the time its completion marker was
   written, `config_snapshot.install_stamp`), and the settings as they are now (a `config_snapshot` zip,
   the one that becomes the copy for going forward). Stage `preparing`. Nothing has changed yet.
2. The settings the older program wrote are put back (or, without a usable copy, what it does not know is
   taken out) and checked (`update._readable_by`). The record is written again: stage `prepared`, with a
   digest of the settings as they are now and what is left to do after the swap.
3. The programs are swapped.
4. What is left is done (the copy for going forward is published, files the older program never had are set
   aside, a restore that failed is recorded) and the record is deleted.

Interrupted, the next start of a program that has this code (`first_start`, which every settings writer and
the engine's start call, and `ns update` / `ns update --rollback` themselves) calls `recover`:

* the same install of the newer program is in `<app>` (the swap did not happen, or was put back): the record
  is first written again as stage `undoing` (so an undo that is itself interrupted is resumed, never taken for
  something else), the settings go back to the ones in the record and the rollback is undone;
  `ns update --rollback` can then be run again.
* the same install of the older program is in `<app>` (the swap went through; an older version with this code
  is running): the last rename is finished if `<app>.swap` is still there, and step 4 is done.
* anything else is a stale record and is deleted, the settings left as they are: a program put in place again
  since (an updater, INSTALL.cmd or an older version's rollback write the marker again, so even the same build
  has another install stamp) has run on the settings the rollback prepared, which the program then in
  `<app>` reads. One thing only the record knows is carried over first: a restore that failed
  (`after.mark_unrestored`). When the copy in `<app>.prev-config.zip` is still the one of the program the
  rollback went back to, it is marked (`config_snapshot.mark_unrestored`) BEFORE the record goes, so the next
  update keeps it instead of saving a newer version's settings over it under the older program's name. While
  the mark cannot be written the record stays (outcome "failed") and the next start tries again.

The setup window (INSTALL.cmd) started from a zip does not undo: it finishes a rollback whose older program is
in place, and refuses to replace the program while the newer one waits for its undo (`appcopy.install_app`).
Started from the installed program folder itself (a repair run from `<home>\app`), `install_app` replaces
nothing and does not look at the record: that window is a start of the program in place, and its first
settings write (`first_start.guard`) finishes or undoes like any other start. `ns update` and
`ns update --rollback` refuse while a record could not be finished or undone (`Recovery.pending`).

The folder `<app>.swap` (`settle_swap`, card 206-carried). Only the first rename makes it and only the third
takes it away, so one that is there when nothing is running was left by an interruption, with or without a
record (0.1.14's rollback writes none, and a stale record is deleted). Every place that calls `recover`
(`first_start`, `ns update`, `ns update --rollback`, and the setup window started from a zip,
`appcopy.install_app`) calls `settle_swap` first, under the same lock, and it does one rename or none:

* no `<app>`: `<app>.swap` is the program that was leaving; it goes back to `<app>` (and `recover` then
  undoes the settings when a record says so). In a real install only the setup window can do this: every
  venv is an editable install of `<app>`, so without that folder no command, Dashboard or `ns doctor`
  starts, and the Desktop shortcuts (through `<home>\nightshift-launch.ps1`) say to run INSTALL.cmd from
  the zip. While `<app>` is missing `recover` never calls a record stale: it keeps it and says so;
* `<app>` and no `<app>.prev`: it becomes `<app>.prev`, the rollback target it was about to become;
* `<app>` and `<app>.prev` both there: it is a third program folder nothing looks for. It is renamed to
  `<app>.swap-left-<yyyymmdd-hhmm>` and never deleted (it may be the only copy of a program); `ns doctor`
  and the next update or rollback name it until the owner deletes it;
* it has no completion marker (not a program folder Nightshift finished), or Windows refuses the rename (a
  window or a scanner has it open): nothing is renamed, the sentence says what to do (with a program in
  `<app>`: start Nightshift again; without one: run INSTALL.cmd from the zip again, or rename by hand), and
  `ns update --rollback` does not start while the name is taken. Nothing in it is ever deleted.

A run of `ns update --rollback` that finishes an EARLIER rollback (its last rename through `settle_swap`, or
its step 4 through `recover`) stops there and says so (`update.rollback_app`): swapping again in the same
run would undo the rollback the owner had asked for.

The settings are loadable by whichever program is in `<app>` at every point: before the swap the newer program
is there and reads both shapes; from the second rename on the older program is there and its settings were
made readable in step 2. Only a hard stop (power loss) between the first and the second rename leaves no
program in `<app>`; the record's `help` says what to rename by hand, and the settings are then already the
older program's.

Standard library plus `ns_common.config_snapshot` and `ns_common.atomic`: the setup window's bare Python
imports `first_start`, which calls this.
"""

from __future__ import annotations

import base64
import glob
import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import ns_common.config_snapshot as config_snapshot
from ns_common.atomic import read_text_retry, write_bytes_atomic, write_text_atomic

SUFFIX = ".rollback-pending.json"
SWAP_SUFFIX = ".swap"
#: `<app>.swap-left-<yyyymmdd-hhmm>`: a third program folder `settle_swap` set aside (never deleted).
LEFT_INFIX = ".swap-left-"
FORMAT = 1
PREPARING = "preparing"
PREPARED = "prepared"
UNDOING = "undoing"


def path_for(app: Path) -> Path:
    """`<app>.rollback-pending.json`, beside the program folder."""
    return app.with_name(app.name + SUFFIX)


def swap_for(app: Path) -> Path:
    """`<app>.swap`, where the leaving program waits between the first and the third rename."""
    return app.with_name(app.name + SWAP_SUFFIX)


def prev_for(app: Path) -> Path:
    """`<app>.prev`, the program backup."""
    return app.with_name(app.name + ".prev")


#: `<app>.rollback-settings-<yyyymmdd-hhmm>.zip`: the settings a stale record held (`recover`).
SETTINGS_INFIX = ".rollback-settings-"


def saved_settings(app: Path) -> list[Path]:
    """The zips `recover` saved beside `app` before it removed a stale record (Nightshift never deletes
    them; the uninstall does)."""
    try:
        return sorted(p for p in app.parent.glob(f"{app.name}{SETTINGS_INFIX}*.zip") if p.is_file())
    except OSError:
        return []


def saved_settings_sentence(file: Path, config: Path) -> str:
    return (
        f"The settings saved before the interrupted rollback are in {file}. To use them, stop Nightshift "
        f"and unzip it into {config}."
    )


def swap_waits(app: Path) -> bool:
    """True when something is at `<app>.swap` (a folder, or anything else that takes the name)."""
    return os.path.lexists(swap_for(app))


def left_folders(app: Path) -> list[Path]:
    """The folders `settle_swap` set aside beside `app` (`<app>.swap-left-<stamp>`), by name."""
    try:
        found = app.parent.glob(glob.escape(app.name + LEFT_INFIX) + "*")
        return sorted(p for p in found if p.is_dir())
    except OSError:
        return []


def left_sentence(folder: Path) -> str:
    """What the owner is told about a folder `settle_swap` set aside, for as long as it is there."""
    return (
        f"{folder} is a program folder left by an interrupted rollback; Nightshift does not use it. Delete "
        "it when you no longer need it."
    )


def _finished(folder: Path) -> bool:
    """`folder` is a program folder an updater, INSTALL.cmd or a rollback finished: it holds a completion
    marker that is not empty (what `appcopy.installed_fingerprint` asks of `<app>`)."""
    try:
        return bool((folder / config_snapshot.APP_MARKER).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False


def _then(app: Path) -> str:
    """What the owner does after freeing `<app>.swap`, true in the state the folders are in. With a program
    in `<app>`, Nightshift starts and asks again. Without one nothing of Nightshift can start (every command,
    the Dashboard and `ns doctor` run from that folder): only INSTALL.cmd from a zip, which settles the folder
    itself (`appcopy.install_app`), or a rename by hand helps."""
    if app.is_dir():
        return "then start Nightshift again: it asks again at its next start"
    return (
        f"then run INSTALL.cmd from the Nightshift zip again (it puts the folder back), or rename "
        f"{swap_for(app).name} to {app.name} yourself. Nightshift cannot start while there is no folder "
        f"{app.name}"
    )


def _unfinished_sentence(app: Path) -> str:
    swap = swap_for(app)
    what = (
        f"{swap} takes the name ns update --rollback uses while it swaps the programs, and it is not a "
        f"program folder Nightshift finished (there is no {config_snapshot.APP_MARKER} in it), so it was "
        "left as it is. "
    )
    if app.is_dir():
        return what + "Move it somewhere else; ns update --rollback does not start while it is there."
    return what + (
        f"Move it somewhere else (or, if it is your Nightshift program, rename it to {app.name}), then run "
        "INSTALL.cmd from the Nightshift zip again."
    )


def swap_note(app: Path) -> str:
    """For `ns doctor`, which looks after the start has settled what it could: what a `<app>.swap` that is
    still there is, and the way out (empty: there is none). Changes nothing."""
    if not swap_waits(app):
        return ""
    swap = swap_for(app)
    if not swap.is_dir() or not _finished(swap):
        return _unfinished_sentence(app)
    return (
        f"{swap} is a program folder an interrupted rollback left, and it has not been renamed. Nightshift "
        "renames it when it starts; if it is still here after a start, a program or an Explorer window has "
        "it open: close that window (and every Nightshift window), then start Nightshift again. Nothing in "
        "it was deleted."
    )


def _left_name(app: Path, now: datetime) -> Path:
    base = app.with_name(f"{app.name}{LEFT_INFIX}{now:%Y%m%d-%H%M}")
    target, n = base, 1
    while os.path.lexists(target):  # two in one minute: -2, -3
        n += 1
        target = base.with_name(f"{base.name}-{n}")
    return target


def settle_swap(app: Path, *, now: datetime | None = None, for_update: bool = False) -> str:
    """Give a leftover `<app>.swap` a place something looks at (see the module text): one rename or none,
    never a deletion. The caller holds the copy's lock (`config_snapshot.locked`) and calls this BEFORE
    `recover`. Returns the sentence for the owner (empty: there is no `<app>.swap`); when the folder is
    still there afterwards (`swap_waits`), the sentence says why and what to do, and is true whether or not
    a program is in `<app>` (`_then`). `for_update`: the caller replaces `<app>.prev` next (an update), so
    the sentence promises nothing about it. Never raises.

    Interrupted before the rename nothing has changed and the next call does the same; after it the folder
    is gone from `<app>.swap` and the next call returns "". A second call never renames anything again."""
    if not swap_waits(app):
        return ""
    swap, prev = swap_for(app), prev_for(app)
    if not swap.is_dir() or not _finished(swap):
        return _unfinished_sentence(app)
    if not os.path.lexists(app):
        target = app
        said = f"The program folder was put back from {swap.name} (an interrupted rollback)."
    elif not os.path.lexists(prev):
        target = prev
        version = config_snapshot.program_version(swap)
        to = "it" if version == "-" else version
        said = f"{swap.name} was renamed to {prev.name}: ns update --rollback goes to {to} again."
        if for_update:  # (the update replaces the program backup next: no promise about it)
            said = f"{swap.name} was renamed to {prev.name} (an interrupted rollback left it)."
    else:
        target = _left_name(app, now or datetime.now())
        said = f"{swap.name} was renamed to {target.name}. {left_sentence(target)}"
    try:
        os.rename(swap, target)
    except OSError as e:
        return (
            f"{swap} is a program folder an interrupted rollback left, and it could not be renamed to "
            f"{target.name} ({e}). If a program or an Explorer window has it open, close it (and every "
            f"Nightshift window), {_then(app)}. Nothing was changed, and nothing in the folder was deleted."
        )
    return said


@dataclass
class AfterSwap:
    """What a rollback still has to do once the older program is in `<app>`."""

    publish_forward: bool = False  # the settings from before the rollback become the copy for going forward
    #: with the forward copy kept: every settings file not in this list is set aside (the older program never
    #: had it; the forward copy keeps it). None: nothing is set aside.
    keep_only: list[str] | None = None
    mark_unrestored: bool = False  # the copy could not be put back (`config_snapshot.mark_unrestored`)

    def to_json(self) -> dict[str, Any]:
        return {
            "publish_forward": self.publish_forward,
            "keep_only": self.keep_only,
            "mark_unrestored": self.mark_unrestored,
        }

    @classmethod
    def from_json(cls, data: Any) -> AfterSwap:
        if not isinstance(data, dict):
            return cls()
        keep = data.get("keep_only")
        return cls(
            bool(data.get("publish_forward")),
            [str(n) for n in keep] if isinstance(keep, list) else None,
            bool(data.get("mark_unrestored")),
        )


@dataclass
class Intent:
    """The record of a rollback that has started."""

    from_version: str  # the program leaving `<app>` (the newer one)
    from_fingerprint: str
    to_version: str  # the program coming back from `<app>.prev`
    to_fingerprint: str
    #: the settings as they were before the rollback: a zip made by `config_snapshot` of them (version and
    #: fingerprint of the leaving program, taken_by "rollback"), or None (`live_problem` says why)
    live: bytes | None
    stage: str = PREPARING
    prepared_digest: str = ""  # of the settings once prepared; empty when they could not be read then
    after: AfterSwap = field(default_factory=AfterSwap)
    created: str = ""
    from_stamp: str = ""  # `config_snapshot.install_stamp` of the leaving program
    to_stamp: str = ""  # of the program coming back, once its marker is written (stage prepared)
    live_problem: str = ""  # why `live` is None

    def to_json(self, app: Path) -> str:
        data: dict[str, Any] = {
            "format": FORMAT,
            "stage": self.stage,
            "from": {
                "version": self.from_version,
                "fingerprint": self.from_fingerprint,
                "stamp": self.from_stamp,
            },
            "to": {"version": self.to_version, "fingerprint": self.to_fingerprint, "stamp": self.to_stamp},
            "live_problem": self.live_problem,
            "created": self.created or datetime.now().isoformat(timespec="seconds"),
            "prepared_digest": self.prepared_digest,
            "after_swap": self.after.to_json(),
            "live_zip_base64": base64.b64encode(self.live).decode("ascii") if self.live is not None else None,
            "help": (
                f"`ns update --rollback` from {self.from_version} to {self.to_version} started and has not "
                f"finished. The next start of Nightshift finishes or undoes it. If there is no folder "
                f"{app.name} and there is a folder {app.name}{SWAP_SUFFIX}, rename {app.name}{SWAP_SUFFIX} "
                f"back to {app.name} and start Nightshift (then run ns update --rollback again)."
            ),
        }
        return json.dumps(data, indent=2) + "\n"


def write(app: Path, intent: Intent) -> None:
    """Write the record (atomically). `OSError` when it cannot be written: the caller must not go on."""
    if not intent.created:
        intent.created = datetime.now().isoformat(timespec="seconds")
    write_text_atomic(path_for(app), intent.to_json(app))


def read(app: Path) -> Intent | None:
    """The record beside `app`, None when there is none. `ValueError` when one is there and cannot be used."""
    try:
        text = read_text_retry(path_for(app))
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ValueError(f"{path_for(app).name} cannot be read: {e}") from e
    try:
        data = json.loads(text)
        if data["format"] != FORMAT:
            raise ValueError(f"format {data['format']!r}")
        blob = data.get("live_zip_base64")
        return Intent(
            str(data["from"]["version"]),
            str(data["from"]["fingerprint"]),
            str(data["to"]["version"]),
            str(data["to"]["fingerprint"]),
            base64.b64decode(blob) if blob else None,
            str(data.get("stage", PREPARING)),
            str(data.get("prepared_digest", "")),
            AfterSwap.from_json(data.get("after_swap")),
            str(data.get("created", "")),
            str(data["from"].get("stamp", "")),
            str(data["to"].get("stamp", "")),
            str(data.get("live_problem", "")),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        raise ValueError(f"{path_for(app).name} is damaged: {e}") from e


def clear(app: Path) -> None:
    """Delete the record (the rollback is complete, undone, or the record is stale)."""
    path_for(app).unlink(missing_ok=True)


def settings_digest(config: Path) -> str:
    """A digest of every settings file a copy holds (names and bytes). `SnapshotError` when one cannot be
    read."""
    h = hashlib.sha256()
    for name, data in sorted(config_snapshot.capture(config).items()):
        h.update(name.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()


@dataclass
class Finished:
    """What `finish` did after the swap."""

    kept_forward: bool = False
    removed: list[str] = field(default_factory=list)  # set aside
    stayed: list[str] = field(default_factory=list)  # should have been set aside and could not
    problems: list[str] = field(default_factory=list)


def finish(app: Path, config: Path, intent: Intent) -> Finished:
    """Step 4, with the older program in `<app>`: publish the forward copy, set aside what the older program
    never had, record a failed restore, and delete the record. Never raises for a copy problem (it is
    reported); `OSError` only when the record itself cannot be deleted."""
    done = Finished()
    zip_path = config_snapshot.path_for(app)
    live: dict[str, bytes] = {}
    if intent.live is not None:
        try:
            live = config_snapshot.parse(intent.live).files
        except config_snapshot.SnapshotError as e:
            done.problems.append(f"the settings saved in {path_for(app).name} cannot be used: {e}")
    if intent.after.publish_forward and intent.live is not None and not done.problems:
        try:
            config_snapshot.publish(zip_path, intent.live)
            done.kept_forward = True
        except config_snapshot.SnapshotError as e:
            done.problems.append(
                f"could not keep a copy of the current settings for going forward again: {e}"
            )
    if done.kept_forward and intent.after.keep_only is not None:
        keep = {n: b"" for n in intent.after.keep_only}
        done.removed, done.stayed = config_snapshot.set_aside(config, keep, live=live)
    if intent.after.mark_unrestored:
        try:
            config_snapshot.mark_unrestored(zip_path)
        except config_snapshot.SnapshotError as e:
            done.problems.append(str(e))
    clear(app)
    return done


def undo(app: Path, config: Path, intent: Intent) -> str:
    """Put the settings back as they were before the rollback (the newer program is in `<app>`) and delete
    the record. The record is first written again as stage `undoing`, before any settings file changes, so
    an undo that is interrupted is resumed by the next start. Returns the sentence; `SnapshotError` or
    `OSError` (the record stays, the next start tries again) when they cannot be put back."""
    back = f"A rollback to {intent.to_version} was interrupted before {intent.to_version} was put back"
    if intent.live is None:
        clear(app)
        why = intent.live_problem or "they could not be read"
        return (
            f"{back}. The settings as they were before it were not saved at the time ({why}), so they stay "
            f"as they are now ({intent.from_version} reads them). Run ns update --rollback again to go back."
        )
    try:
        files = config_snapshot.parse(intent.live).files
    except config_snapshot.SnapshotError as e:  # (written atomically; this is rot, not a crash)
        clear(app)
        return (
            f"{back}, and the settings saved before it cannot be used ({e}): they stay as they are now. Run "
            "ns update --rollback again to go back."
        )
    if intent.stage != UNDOING:
        intent.stage = UNDOING
        write(app, intent)  # before anything changes: an interrupted undo is resumed, not misread
    changed = ""
    if intent.prepared_digest:
        try:
            if settings_digest(config) != intent.prepared_digest:
                changed = " (settings saved after the rollback was interrupted were replaced by them)"
        except config_snapshot.SnapshotError:
            pass
    config_snapshot.restore(config, files, live=config_snapshot.capture(config), remove_extra=True)
    clear(app)
    return (
        f"{back}: {intent.from_version} is still the program, and your settings files are back as they were "
        f"before the rollback{changed}. Run ns update --rollback again to go back."
    )


@dataclass
class Recovery:
    """What `recover` did: `sentence` for the log or the result line, and `outcome`."""

    sentence: str = ""
    #: "" (no record), "undone", "finished", "stale", "failed" (the record stays: settings may be half put
    #: back), "needs-undo" (asked not to undo, and the newer program waits for its undo)
    outcome: str = ""

    @property
    def pending(self) -> bool:
        """The record is still there and the settings are not in a finished state: do not go on."""
        return self.outcome in ("failed", "needs-undo")


def _same_install(stamp_now: str, recorded: str) -> bool:
    return not recorded or stamp_now == recorded  # (a record without a stamp: the fingerprint decides)


def recover(app: Path, config: Path, *, may_undo: bool = True) -> Recovery:
    """Finish or undo a rollback that `<app>.rollback-pending.json` says was interrupted (see the module
    text). The caller holds the copy's lock (`config_snapshot.locked`). With `may_undo` False nothing is
    undone: the outcome is then "needs-undo" when that is what the record needs. Never raises; a step that
    fails leaves the record for the next start (outcome "failed") and says so."""
    try:
        intent = read(app)
    except ValueError as e:
        return _cleared(app, f"A record of an interrupted rollback could not be used and was removed ({e}).")
    if intent is None:
        return Recovery()
    if not app.is_dir():
        # No program in place (a power loss between the first two renames, `<app>.swap` not put back): this
        # is not "another install", and the record is the only place the settings from before the rollback
        # are kept. Never stale here: it stays until a program is in `<app>` again.
        return Recovery(
            f"A rollback from {intent.from_version} to {intent.to_version} was interrupted and there is no "
            f"program folder at {app}. Its record was kept (it holds the settings files from before the "
            "rollback). Run INSTALL.cmd from the Nightshift zip: it puts the program folder back.",
            "failed",
        )
    running = config_snapshot.program_fingerprint(app)
    stamp = config_snapshot.install_stamp(app)
    prev, swap = prev_for(app), swap_for(app)
    if (
        running == intent.to_fingerprint
        and intent.stage == PREPARED
        and _same_install(stamp, intent.to_stamp)
    ):
        # the older program is in place: the swap went through, its last rename perhaps not
        if (
            not prev.exists()
            and swap.is_dir()
            and config_snapshot.program_fingerprint(swap) == intent.from_fingerprint
        ):
            try:
                swap.rename(prev)
            except OSError as e:
                return Recovery(
                    f"A rollback to {intent.to_version} stopped before it finished: {swap} could not be "
                    f"renamed to {prev.name} ({e}); the next start tries again.",
                    "failed",
                )
        if config_snapshot.program_fingerprint(prev) == intent.from_fingerprint:
            try:
                done = finish(app, config, intent)
            except OSError as e:
                return Recovery(f"A rollback to {intent.to_version} could not be finished: {e}.", "failed")
            tail = f" ({'; '.join(done.problems)})" if done.problems else ""
            return Recovery(
                f"A rollback to {intent.to_version} stopped after the programs were swapped; it was finished "
                f"now{tail}.",
                "finished",
            )
    elif running == intent.from_fingerprint and _same_install(stamp, intent.from_stamp):
        if not may_undo:
            return Recovery(
                f"A rollback from {intent.from_version} to {intent.to_version} was interrupted before "
                f"{intent.to_version} was put back, and the settings are not put back as they were yet.",
                "needs-undo",
            )
        try:
            return Recovery(undo(app, config, intent), "undone")
        except (config_snapshot.SnapshotError, OSError) as e:
            return Recovery(
                f"A rollback to {intent.to_version} was interrupted, and the settings files could not be "
                f"put back as they were before it ({e}); the next start tries again.",
                "failed",
            )
    stale = (
        f"A record of a rollback from {intent.from_version} to {intent.to_version} (started "
        f"{intent.created.replace('T', ' ')}) does not belong to the programs installed now (one was put in "
        "place again since)"
    )
    marked = ""
    if intent.after.mark_unrestored:
        # Only this record knows that the rollback's restore failed. The copy of the program it went back to
        # must carry that before the record goes, or the next update saves the settings as they are now (a
        # newer version's) over it under that program's name. Marking twice is the same as once, so a stop
        # between the mark and the deletion is repaired by the next start doing both again.
        zip_path = config_snapshot.path_for(app)
        slot = config_snapshot.look(zip_path)
        try:
            if slot.copy is None and not slot.free:  # it cannot be read now: it may be that very copy
                raise config_snapshot.SnapshotError(f"{zip_path.name} could not be read ({slot.problem})")
            if slot.copy is not None and slot.copy.fingerprint == intent.to_fingerprint:
                config_snapshot.mark_unrestored(zip_path)
                marked = (
                    f" That rollback could not put the settings copy for {intent.to_version} back, and the "
                    f"copy ({zip_path.name}) is marked so: the next update keeps it instead of saving the "
                    "settings files as they are now over it."
                )
        except config_snapshot.SnapshotError as e:
            return Recovery(
                f"{stale}, and it says the settings copy for {intent.to_version} was never put back, which "
                f"could not be recorded beside the copy ({e}). The record stays and the next start tries "
                f"again. If this does not go away: make room on the disk, close what has {zip_path.name} "
                f"open, or move {zip_path.name} out of {zip_path.parent} (a rollback then has no settings "
                "copy to put back and keeps the settings files as they are).",
                "failed",
            )
    saved = ""
    if intent.live:
        # The record is the only place that holds the settings from before the rollback (a rollback that
        # lost power, `app.swap` moved away by hand, a fresh program folder from INSTALL.cmd). They go into a
        # file of their own BEFORE the record is removed; a start that could not remove the record afterwards
        # finds the same bytes saved and does not save them again.
        try:
            have = [f for f in saved_settings(app) if f.read_bytes() == intent.live]
            name = f"{app.name}{SETTINGS_INFIX}{datetime.now():%Y%m%d-%H%M}.zip"
            file = have[0] if have else app.with_name(name)
            if not have:
                write_bytes_atomic(file, intent.live)
        except OSError as e:
            return Recovery(
                f"{stale}, and the settings files it holds from before that rollback could not be saved "
                f"beside the program ({e}). The record stays and the next start tries again.",
                "failed",
            )
        saved = " " + saved_settings_sentence(file, config)
    return _cleared(
        app, f"{stale}: it was removed, and the settings files were left as they are.{marked}{saved}"
    )


def _cleared(app: Path, sentence: str) -> Recovery:
    try:
        clear(app)
    except OSError as e:
        return Recovery(f"{sentence[:-1]}, but it could not be deleted ({e}).", "failed")
    return Recovery(sentence, "stale")
