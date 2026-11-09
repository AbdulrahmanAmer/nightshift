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
  `<app>` reads.

The setup window (INSTALL.cmd) does not undo: it finishes a rollback whose older program is in place, and
refuses to replace the program while the newer one waits for its undo (`appcopy.install_app`). `ns update` and
`ns update --rollback` refuse while a record could not be finished or undone (`Recovery.pending`).

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
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import ns_common.config_snapshot as config_snapshot
from ns_common.atomic import read_text_retry, write_text_atomic

SUFFIX = ".rollback-pending.json"
SWAP_SUFFIX = ".swap"
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
    return _cleared(
        app,
        f"A record of a rollback from {intent.from_version} to {intent.to_version} (started "
        f"{intent.created.replace('T', ' ')}) does not belong to the programs installed now (one was put in "
        "place again since): it was removed, and the settings files were left as they are.",
    )


def _cleared(app: Path, sentence: str) -> Recovery:
    try:
        clear(app)
    except OSError as e:
        return Recovery(f"{sentence[:-1]}, but it could not be deleted ({e}).", "failed")
    return Recovery(sentence, "stale")
