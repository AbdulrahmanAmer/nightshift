r"""F-10a: a copy of the settings files an update may rewrite, kept beside the program backup.

Why: `Runtime`, `InstallState`, `Channel` and the other config models are strict (`extra="forbid"`), and
`save_runtime` writes every field the running version knows. The first time a newer version re-saves
`config\runtime.json` with a field the older version does not have, `ns update --rollback` used to leave an
install whose older program cannot load its own config: the rollback swapped the program folders and nothing
else. So an update copies the files below BEFORE it applies, and the rollback puts them back with the program.

Where: `<app>.prev-config.zip`, next to `<app>.prev` (the program backup). One zip, written whole and swapped
in with `os.replace` (`ns_common.atomic`), so a crash while it is written never shows a part of it, and never
replaces a good copy with a bad one: the new zip is built in memory, read back and checked before it is
published. The zip holds the files and `snapshot.json` (the version and fingerprint of the program it belongs
to, and a SHA256 for every file). A copy is applied only when it is complete, every checksum matches, and the
program in `<app>.prev` is the one it was taken for; otherwise the rollback leaves the settings alone and
says so.

Only the latest pair is kept: the copy belongs to the program in `<app>.prev`, and a second update replaces
both. A rollback swaps the pair like it swaps the programs: the settings as they are just before the rollback
become the copy for the program that goes to `<app>.prev`, so a second `ns update --rollback` (going forward)
gets back exactly what the newer version had, including what the owner changed after the update.

A copy that reads back complete is written over only together with its own program (an update replacing
`<app>.prev`, a rollback that has just put it back). One that belongs to another program may be the only copy
of that program's settings: after a restore that failed, the zip still holds what the older program wrote
while the live files are the newer version's, and saving those over it under the older program's name would
make the next rollback "restore" the wrong files. So such a copy is left where it is (`before_update`,
`Slot.free`); only a missing or damaged one may be replaced, and the result says so.

Standard library plus `ns_common.atomic`, so the installer's bare Python can use it (`appcopy.install_app`).
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from ns_common.atomic import read_bytes_retry, sweep_stale_tmp, write_bytes_atomic

SUFFIX = ".prev-config.zip"
MANIFEST = "snapshot.json"
MEMBER_ROOT = "config/"
FORMAT = 1

#: Files of `<home>\config` the copy holds, with why. Each is a file the new version (or the update itself)
#: rewrites in place and that the older version reads with a strict model or in a fixed shape.
FILES: dict[str, str] = {
    "runtime.json": (
        "Runtime is strict and save_runtime writes every field the running version knows; the Settings page, "
        "`ns tune`, `ns models get` and setup all re-save it, so an older version meets keys it never saw."
    ),
    "install_state.json": (
        "InstallState is strict; setup rewrites it after every step, and a repair or resume after an update "
        "runs the new setup code over it."
    ),
    "install_state.json.bak": (
        "The copy load_install_state falls back to when install_state.json is torn: it must match the file "
        "it backs up."
    ),
    "answers.json": (
        "The saved setup choices (the strict Answers form); a setup re-run, repair or reinstall rewrites it."
    ),
    "wizard_flags.json": "Setup's own flags, saved beside the install state by the same re-runs.",
    "schedule.json": (
        "The update itself rewrites it: its re-register step runs `ns schedule install`, which saves the run "
        "time and mode again."
    ),
    "dashboard.json": "The Settings page saves it (notifications and the like); a newer one may add keys.",
}

#: Folders of `<home>\config` whose `*.yaml` files the copy holds, with why.
TREES: dict[str, str] = {
    "channels": (
        "Channel is strict; the channel editor, the dashboard and setup save every channel with the full "
        "current model, so a field added in a newer version reaches every channel file it saves."
    ),
    "formats": (
        "Format is strict and Library > Formats saves the owner's edited formats with the full current model."
    ),
}

#: The files INSTALL.cmd's setup window rewrites on its own, before it copies the new program (it records the
#: new version and the update steps in install_state.json as soon as it opens; a mode switch drops
#: wizard_flags.json and runtime.json). The window takes these as they were when it opened and hands them to
#: `appcopy.install_app`, so the copy holds what the older program wrote, not what the window made of it.
SETUP_TOUCHES: tuple[str, ...] = (
    "install_state.json",
    "install_state.json.bak",
    "wizard_flags.json",
    "runtime.json",
)

#: Files under `<home>\config` the code names and the copy does NOT hold, with why (a test keeps this list and
#: FILES covering every name the code mentions, so a file added in a later release has to be decided here).
LEFT_ALONE: dict[str, str] = {
    "secrets.dpapi": (
        "The owner's API keys, encrypted to this Windows user: never duplicated into a second file, and a "
        "key added after the update must survive a rollback."
    ),
    "opencode.json": "Written for the OpenCode tool by setup; no strict Nightshift model reads it.",
    "supervisor.json": "One number (health_timeout_s), read with a default when it is missing or odd.",
    "tts_speed.json": "A measurement of this PC, read with a default when it is unreadable.",
    "update_check.json": "A cache of the last update check, read as a plain dict.",
    "ui_prefs.json": "Owner name and theme from the setup window, read as a plain dict (extra keys skipped).",
    "answers.kept.json": "The old answers a setup window keeps until the new choices are applied.",
    "pending_voice.json": "A voice setup queued for the first start; deleted once it is made.",
}


class SnapshotError(Exception):
    """A copy could not be made, read or applied. `missing` is True when there is simply no copy, `damaged`
    when there is one that does not read back complete (nothing in it can be used)."""

    def __init__(self, message: str, *, missing: bool = False, damaged: bool = False) -> None:
        super().__init__(message)
        self.missing = missing
        self.damaged = damaged


@dataclass(frozen=True)
class Snapshot:
    """A copy that read back complete: `files` by relative name (`runtime.json`, `channels/demo.yaml`)."""

    files: dict[str, bytes]
    version: str  # the program the copy belongs to (the one in `<app>.prev`)
    fingerprint: str  # that program's version|manifest|pyproject fingerprint (what pairs the two)
    taken_by: str  # update | rollback | installer
    created: str


@dataclass
class Restored:
    """What `restore` did, by file name."""

    written: list[str]
    changed: list[str]  # written, and the file there now was different (what changed since is gone)
    removed: list[str]  # there now, not in the copy: taken out (the caller keeps them elsewhere)


def path_for(app: Path) -> Path:
    """`<app>.prev-config.zip`."""
    return app.with_name(app.name + SUFFIX)


def valid_name(name: str) -> bool:
    """True for a name a copy may hold: a listed file or `<tree>/<file>.yaml` (nothing that leaves config)."""
    if not name or "\\" in name or ":" in name or name.startswith("/"):
        return False
    parts = PurePosixPath(name).parts
    if not parts or any(p in ("", ".", "..") for p in parts):
        return False
    if len(parts) == 1:
        return name in FILES
    return len(parts) == 2 and parts[0] in TREES and parts[1].lower().endswith(".yaml")


def _path(config: Path, name: str) -> Path:
    return config.joinpath(*name.split("/"))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tracked_names(config: Path) -> list[str]:
    """The files that exist now under `config` and belong in a copy: FILES first, then each tree's yaml."""
    names = [n for n in FILES if _path(config, n).is_file()]
    for tree in TREES:
        folder = config / tree
        if folder.is_dir():
            yamls = (f"{tree}/{p.name}" for p in folder.glob("*.yaml") if p.is_file())
            names += sorted(n for n in yamls if valid_name(n))
    return names


def capture(config: Path, only: Iterable[str] | None = None) -> dict[str, bytes]:
    """The bytes of every tracked file (with `only`: of those among them). `SnapshotError` naming the file
    when one cannot be read."""
    out: dict[str, bytes] = {}
    wanted = None if only is None else set(only)
    for name in tracked_names(config):
        if wanted is not None and name not in wanted:
            continue
        try:
            out[name] = read_bytes_retry(_path(config, name))
        except OSError as e:
            raise SnapshotError(f"could not read {name}: {e}") from e
    return out


def build(
    files: dict[str, bytes], *, version: str, fingerprint: str, taken_by: str, now: datetime | None = None
) -> bytes:
    """The zip for `files`, read back and checked before it is returned: only a good one can be published."""
    manifest = {
        "format": FORMAT,
        "created": (now or datetime.now()).isoformat(timespec="seconds"),
        "taken_by": taken_by,
        "program_version": version,
        "program_fingerprint": fingerprint,
        "files": {n: {"sha256": _sha(d), "size": len(d)} for n, d in files.items()},
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(MEMBER_ROOT + name, data)
        z.writestr(MANIFEST, json.dumps(manifest, indent=2) + "\n")
    data = buf.getvalue()
    if parse(data).files != files:
        raise SnapshotError("the copy did not read back as it was written")
    return data


def _damaged(reason: str) -> SnapshotError:
    return SnapshotError(f"the saved copy is damaged: {reason}", damaged=True)


def parse(data: bytes) -> Snapshot:
    """Read a copy. `SnapshotError` (\"damaged\") unless it is complete and every checksum matches."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            bad = z.testzip()
            if bad is not None:
                raise _damaged(f"{bad} does not read back")
            manifest = json.loads(z.read(MANIFEST).decode("utf-8"))
            if manifest["format"] != FORMAT:
                raise _damaged(f"format {manifest['format']!r} is not one this version reads")
            listed: dict[str, dict[str, object]] = manifest["files"]
            members = {n for n in z.namelist() if n.startswith(MEMBER_ROOT)}
            if members != {MEMBER_ROOT + n for n in listed}:
                raise _damaged("its file list does not match the zip")
            files: dict[str, bytes] = {}
            for name, meta in listed.items():
                if not valid_name(name):
                    raise _damaged(f"{name!r} is not a settings file name")
                content = z.read(MEMBER_ROOT + name)
                if len(content) != meta["size"] or _sha(content) != meta["sha256"]:
                    raise _damaged(f"{name} does not match its checksum")
                files[name] = content
            return Snapshot(
                files,
                str(manifest["program_version"]),
                str(manifest["program_fingerprint"]),
                str(manifest.get("taken_by", "")),
                str(manifest.get("created", "")),
            )
    except SnapshotError:
        raise
    except Exception as e:  # a zip of any kind of rot: bad zip, bad json, missing key, bad deflate stream
        raise _damaged(f"{type(e).__name__}: {e}") from e


def save(
    path: Path,
    files: dict[str, bytes],
    *,
    version: str,
    fingerprint: str,
    taken_by: str,
    now: datetime | None = None,
) -> Snapshot:
    """Publish a copy at `path` (atomically); the copy there stays whole unless this one is complete."""
    return publish(path, build(files, version=version, fingerprint=fingerprint, taken_by=taken_by, now=now))


def publish(path: Path, data: bytes) -> Snapshot:
    """Write a zip made by `build` to `path` (atomically). Splitting `build` from `publish` lets a caller
    check that a copy can be made, do something else, and only then replace the copy that is there."""
    try:
        sweep_stale_tmp(path.parent)
        write_bytes_atomic(path, data)
    except OSError as e:
        raise SnapshotError(f"could not write {path.name}: {e}") from e
    return parse(data)


def read(path: Path) -> Snapshot:
    """The copy at `path`. `SnapshotError`: `missing=True` when there is none, else damaged or unreadable."""
    try:
        data = read_bytes_retry(path)
    except FileNotFoundError as e:
        raise SnapshotError(f"there is no copy at {path.name}", missing=True) from e
    except OSError as e:
        raise SnapshotError(f"{path.name} cannot be read: {e}") from e
    return parse(data)


@dataclass(frozen=True)
class Slot:
    """What is at `<app>.prev-config.zip` now: `copy` when it reads back complete, else `problem` says why."""

    copy: Snapshot | None
    problem: SnapshotError | None = None

    @property
    def damaged(self) -> bool:
        return self.problem is not None and self.problem.damaged

    @property
    def free(self) -> bool:
        """Nothing that could be needed is there: no zip, or a damaged one (which may be replaced; the caller
        says so). A complete copy may be the only one of its settings, and a zip that could not be read at
        all may still be whole: neither is ever written over unless it goes with its own program."""
        return self.problem is not None and (self.problem.missing or self.problem.damaged)


def look(path: Path) -> Slot:
    """The copy at `path`, or why there is none. Never raises."""
    try:
        return Slot(read(path))
    except SnapshotError as e:
        return Slot(None, e)


def version_of(fingerprint: str) -> str:
    """The version part of a program fingerprint (`<version>|<manifest hash>|<pyproject hash>`)."""
    return fingerprint.split("|", 1)[0]


def before_update(path: Path, *, leaving: str, backup: str | None) -> tuple[bool, Slot]:
    """Decide what an update does with the copy at `path` before the program whose fingerprint is `leaving`
    becomes the program backup, in place of the backup whose fingerprint is `backup` (None: there is none).

    Returns (save, slot). `save` True: the caller saves a new copy, because there is none, or a damaged one
    (`slot.damaged`; the caller says it was replaced), or the copy of `backup`, which is replaced together
    with its program (`slot.copy` is set). `save` False: the copy already belongs to `leaving` (a rollback or
    a failed update could not put it back, so the settings files now may be a newer version's): it stays as
    the copy for `leaving`, and the settings as they are now must not be saved over it under its name.

    `SnapshotError` for a complete copy that belongs to neither program, or a zip that could not be read: it
    is never written over (it may be the only copy of those settings files), so the update cannot keep a copy
    for its program backup and must not start. The message says whose copy it is, where, and what to do.
    """
    slot = look(path)
    snap = slot.copy
    if snap is None:
        if slot.free:
            return True, slot
        raise SnapshotError(
            f"the settings copy {path} could not be read ({slot.problem}), so it was left as it is: it may "
            "still be whole. Close programs that use the Nightshift folder and run the update again"
        )
    if snap.fingerprint == leaving:
        return False, slot
    if backup is not None and snap.fingerprint == backup:
        return True, slot
    others = f"this program ({version_of(leaving)})"
    if backup is not None:
        others += f" or the program backup ({version_of(backup)})"
    raise SnapshotError(
        f"the settings copy {path} belongs to {snap.version} (made {snap.created.replace('T', ' ')}), not to "
        f"{others}. It was left as it is, because it may be the only copy of those settings files: move it "
        f"out of {path.parent} (or delete it if you do not need it) and run the update again"
    )


def discard(path: Path) -> None:
    """Delete the copy at `path` (best effort: a copy that stays is checked against its program anyway)."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def restore(config: Path, files: dict[str, bytes], *, live: dict[str, bytes], remove_extra: bool) -> Restored:
    """Write every file of `files` into `config`, each atomically and read back; `live` is `capture(config)`.

    With `remove_extra`, a tracked file that exists now and is not in `files` is deleted (the older version
    never had it; the caller has saved it). One file that cannot be written puts every file already written
    or deleted back to its `live` bytes and raises `SnapshotError`: a restore is all or nothing.
    """
    written: list[str] = []
    removed: list[str] = []
    current = ""
    try:
        for current, data in files.items():
            dest = _path(config, current)
            write_bytes_atomic(dest, data)
            if _sha(read_bytes_retry(dest)) != _sha(data):
                raise OSError("it did not read back as written")
            written.append(current)
        if remove_extra:
            for current in live:
                if current not in files:
                    _path(config, current).unlink(missing_ok=True)
                    removed.append(current)
    except OSError as e:
        _undo(config, live, written, removed)
        raise SnapshotError(f"could not restore {current}: {e}") from e
    return Restored(written, [n for n in written if live.get(n) != files[n]], removed)


def set_aside(
    config: Path, files: dict[str, bytes], *, live: dict[str, bytes]
) -> tuple[list[str], list[str]]:
    """Delete every tracked file that exists now (`live`) and is not in `files`: (deleted, could not delete).

    Only for the caller that has kept those files elsewhere. A file that stays is reported, never fatal.
    """
    removed: list[str] = []
    stayed: list[str] = []
    for name in live:
        if name in files:
            continue
        try:
            _path(config, name).unlink(missing_ok=True)
            removed.append(name)
        except OSError:
            stayed.append(name)
    return removed, stayed


def _undo(config: Path, live: dict[str, bytes], written: list[str], removed: list[str]) -> None:
    for name in [*written, *removed]:
        dest = _path(config, name)
        try:
            if name in live:
                write_bytes_atomic(dest, live[name])
            else:
                dest.unlink(missing_ok=True)
        except OSError:
            continue
