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

Who makes the copy: `ns update` (`taken_by` update), INSTALL.cmd's setup window (installer), a rollback (the
copy for going forward again), and since F-10c the first start of a version that an OLDER updater installed
(first-start, `ns_common.first_start`): 0.1.14's `ns update` takes no copy, so the new version takes it
itself before it changes any settings file. The first start follows the same rules as the updater below.

A copy that reads back complete is written over only together with its own program (an update replacing
`<app>.prev`, a rollback that has just put it back), with one more case: a copy that belongs to the program
leaving `<app>` is replaced too, UNLESS a rollback recorded that its restore never finished (a marker beside
the zip, `mark_unrestored`: the live files are then a newer version's, and saving them over it under the
older program's name would make the next rollback "restore" the wrong files). A copy of any other program may
be the only copy of that program's settings, so an update leaves it where it is, goes on without keeping a
copy for its own program backup, and says whose copy it is and where (`before_update`); only a missing or
damaged one may be replaced, and the result says so. A fingerprint alone proves nothing about a restore: a
normal rollback, or an older updater that ignores the zip, also leaves a copy that carries the fingerprint of
the program now in `<app>`.

Standard library plus `ns_common.atomic` and `ns_common.singleton`, so the installer's bare Python can use it
(`appcopy.install_app`).
It lives in `ns_common` (F-10c; it was `ns_supervisor/config_snapshot.py` in F-10a) because every process
that writes a settings file needs it, and `ns_common` cannot import `ns_supervisor`.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import time
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from ns_common.atomic import read_bytes_retry, sweep_stale_tmp, write_bytes_atomic
from ns_common.singleton import AlreadyRunning, FileLock

SUFFIX = ".prev-config.zip"
UNRESTORED_SUFFIX = ".unrestored"  # `<app>.prev-config.zip.unrestored`: see mark_unrestored
LOCK_SUFFIX = ".lock"  # `<app>.prev-config.zip.lock`: see locked
#: How long a process waits for another one that holds the copy's lock (a first start saving the copy takes
#: well under a second; a rollback holds it while it renames the program folders, with retries).
LOCK_WAIT_S = 60.0
LOCK_POLL_S = 0.05
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
    ".ns-first-start.json": (
        "Where the first start records that it could not save the copy when the program folder cannot be "
        "written (`first_start.write_state`); not a setting."
    ),
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
    taken_by: str  # update | rollback | installer | first-start
    created: str
    #: first-start copies only: `first_start.install_stamp` of the program that made it (which install of the
    #: newer program saw the settings as the older one left them); empty for every other copy.
    for_program: str = ""


@dataclass
class Restored:
    """What `restore` did, by file name."""

    written: list[str]
    changed: list[str]  # written, and the file there now was different (what changed since is gone)
    removed: list[str]  # there now, not in the copy: taken out (the caller keeps them elsewhere)


def path_for(app: Path) -> Path:
    """`<app>.prev-config.zip`."""
    return app.with_name(app.name + SUFFIX)


def _digest16(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "-"


def program_version(folder: Path) -> str:
    """The `VERSION` file of a program folder, `-` when it has none."""
    try:
        return (folder / "VERSION").read_text(encoding="utf-8-sig").strip() or "-"
    except OSError:
        return "-"


def program_fingerprint(folder: Path) -> str:
    """Version plus short hashes of manifest.json and pyproject.toml: what pairs a copy with its program.

    The same text as `ns_supervisor.update.app_fingerprint` and `installer.stage2.web.appcopy.fingerprint`
    (a test keeps the three equal).
    """
    manifest, project = _digest16(folder / "manifest.json"), _digest16(folder / "pyproject.toml")
    return f"{program_version(folder)}|{manifest}|{project}"


#: Written into a finished program folder by every updater, INSTALL.cmd and the rollback (update.APP_MARKER).
APP_MARKER = ".ns-app.done"


def install_stamp(program: Path) -> str:
    """Which install of `program` this is: its fingerprint and when its completion marker was written.

    Every updater, INSTALL.cmd and every rollback write the marker again, so a program folder put in place
    again (a second try of a failed update, a roll forward by an older version's rollback, the same build
    installed again) has a new stamp although its fingerprint is the same.
    """
    try:
        written = str((program / APP_MARKER).stat().st_mtime_ns)
    except OSError:
        written = "0"
    return f"{program_fingerprint(program)}@{written}"


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
    files: dict[str, bytes],
    *,
    version: str,
    fingerprint: str,
    taken_by: str,
    now: datetime | None = None,
    for_program: str = "",
) -> bytes:
    """The zip for `files`, read back and checked before it is returned: only a good one can be published."""
    manifest: dict[str, object] = {
        "format": FORMAT,
        "created": (now or datetime.now()).isoformat(timespec="seconds"),
        "taken_by": taken_by,
        "program_version": version,
        "program_fingerprint": fingerprint,
        "files": {n: {"sha256": _sha(d), "size": len(d)} for n, d in files.items()},
    }
    if for_program:
        manifest["for_program"] = for_program
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
                str(manifest.get("for_program", "")),
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
    _clear_marker(path)  # a new zip is a new copy: whatever was said about the old one does not apply
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


#: What `before_update` decides.
SAVE = "save"  # the caller saves a new copy
KEEP = "keep"  # the copy of the program leaving `<app>` stays: its restore never finished
FOREIGN = "foreign"  # a whole copy of another program stays: no copy is kept for this update


def before_update(path: Path, *, leaving: str, backup: str | None) -> tuple[str, Slot]:
    """Decide what an update does with the copy at `path` before the program whose fingerprint is `leaving`
    becomes the program backup, in place of the backup whose fingerprint is `backup` (None: there is none).

    Returns (action, slot):

    * `SAVE`: the caller saves a new copy. There is none, or a damaged one (`slot.damaged`; the caller says it
      was replaced), or the copy is the one of `backup` (replaced together with its program), or it belongs
      to `leaving` and nothing says its restore failed (a normal rollback, or an older updater that ignores
      the zip, leaves such a copy; the settings files now are `leaving`'s own, so they replace it).
    * `KEEP`: the copy belongs to `leaving` and a rollback recorded that it could not be put back
      (`mark_unrestored`), so the settings files now may be a newer version's: the copy stays as the copy for
      `leaving`, and those files must not be saved over it under its name.
    * `FOREIGN`: a whole copy of a program that is neither: it may be the only copy of those settings files,
      so it is left where it is (`slot.copy` says whose it is). The update goes on without a copy of its own.

    `SnapshotError` for a zip that could not be read (a program holds it open; it may still be whole): it is
    not written over, and the update does not start until it can be read.
    """
    slot = look(path)
    snap = slot.copy
    if snap is None:
        if slot.free:
            return SAVE, slot
        raise SnapshotError(
            f"the settings copy {path} could not be read ({slot.problem}), so it was left as it is: it may "
            "still be whole. Close programs that use the Nightshift folder, then try again"
        )
    if snap.fingerprint == leaving:
        return (KEEP if is_unrestored(path) else SAVE), slot
    if backup is not None and snap.fingerprint == backup:
        return SAVE, slot
    return FOREIGN, slot


def foreign_note(path: Path, snap: Snapshot, *, leaving: str, backup: str | None) -> str:
    """The sentence for an update that left the copy of another program where it is."""
    others = f"this program ({version_of(leaving)})"
    if backup is not None:
        others += f" or the program backup ({version_of(backup)})"
    return (
        f"the settings copy {path} belongs to {snap.version} (made {snap.created.replace('T', ' ')}), not to "
        f"{others}, and was left as it is, because it may be the only copy of those settings files. No "
        f"settings copy is kept for this update, so ns update --rollback will not restore settings files. To "
        f"have one kept again, move {path.name} out of {path.parent} (or delete it if you do not need it) "
        "before the next update"
    )


def _marker(path: Path) -> Path:
    return path.with_name(path.name + UNRESTORED_SUFFIX)


def _clear_marker(path: Path) -> None:
    try:
        _marker(path).unlink(missing_ok=True)
    except OSError:
        pass


def mark_unrestored(path: Path) -> None:
    """Record that the copy now at `path` could not be put back (a rollback's restore raised): the settings
    files are a newer version's while the program in `<app>` is the one the copy belongs to. The marker holds
    the SHA256 of the zip, so it applies to this copy only. `SnapshotError` when it cannot be written."""
    try:
        digest = _sha(read_bytes_retry(path))
        write_bytes_atomic(_marker(path), json.dumps({"zip_sha256": digest}).encode("utf-8"))
    except OSError as e:
        raise SnapshotError(f"could not record that {path.name} was not put back: {e}") from e


def is_unrestored(path: Path) -> bool:
    """True when `mark_unrestored` was called for the copy that is at `path` now (never raises)."""
    try:
        marked = json.loads(read_bytes_retry(_marker(path)).decode("utf-8"))["zip_sha256"]
        return bool(marked == _sha(read_bytes_retry(path)))
    except Exception:  # no marker, a torn one, an unreadable zip: nothing says the restore failed
        return False


@contextlib.contextmanager
def locked(path: Path, *, wait_s: float | None = None) -> Iterator[bool]:
    """Hold `<path>.lock` (`path` is the copy's zip) across processes for the block: yields True when it is
    held, False when another process kept it for `wait_s` seconds (the caller then does not touch the copy).

    Two processes of a newer version can start at once (the engine and a CLI): without it, one could look at
    the copy's place before the other published its first-start copy and then capture the settings after the
    other had already changed them, publishing them as the older version's copy. Whoever holds the lock looks
    at the copy again inside it. A rollback holds it from its record to its end (`rollback_intent`).
    """
    lock = FileLock(path.with_name(path.name + LOCK_SUFFIX))
    deadline = time.monotonic() + (LOCK_WAIT_S if wait_s is None else wait_s)
    while True:
        try:
            lock.acquire()
            break
        except AlreadyRunning:
            if time.monotonic() >= deadline:
                yield False
                return
            time.sleep(LOCK_POLL_S)
        except OSError:  # the home cannot hold a lock file (read-only): nobody else can write the copy either
            yield True
            return
    try:
        yield True
    finally:
        lock.release()


def discard(path: Path) -> None:
    """Delete the copy at `path` (best effort: a copy that stays is checked against its program anyway)."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    _clear_marker(path)


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
