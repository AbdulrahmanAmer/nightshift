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

Standard library plus `ns_common.atomic`, so the installer's bare Python can use it (`appcopy.install_app`).
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
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
    """A copy could not be made, read or applied. `missing` is True when there is simply no copy."""

    def __init__(self, message: str, *, missing: bool = False) -> None:
        super().__init__(message)
        self.missing = missing


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


def capture(config: Path) -> dict[str, bytes]:
    """The bytes of every tracked file. `SnapshotError` naming the file when one cannot be read."""
    out: dict[str, bytes] = {}
    for name in tracked_names(config):
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
    return SnapshotError(f"the saved copy is damaged: {reason}")


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
    data = build(files, version=version, fingerprint=fingerprint, taken_by=taken_by, now=now)
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
