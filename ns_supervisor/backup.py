"""C2 `ns backup` and `ns restore` (spec J10): settings, voices, studio.db, exemplars. No models or videos."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

from ns_common import __version__
from ns_common.atomic import (
    PrepareFn,
    is_tmp_name,
    read_bytes_retry,
    write_bytes_atomic,
    write_stream_atomic,
    write_zip_atomic,
)
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_common.secret_file import FILE_NAME as SECRETS_FILE
from ns_common.secret_file import (
    NOTICE_FILE,
    UNPROTECTED_CODE,
    SecretFileLockError,
    restrict_to_user,
    volume_keeps_permissions,
)
from ns_supervisor.doctor import desktop_dir

MANIFEST = "backup-manifest.json"
SECRETS_ARC = f"config/{SECRETS_FILE}"
#: why a restore leaves the keys file alone / a backup leaves it out, on a drive without permissions
NO_PERMISSIONS = "this drive has no file permissions (FAT or exFAT): Windows could not keep the keys private"
BLOCK_SUFFIXES = frozenset({".gguf", ".safetensors", ".onnx", ".mp4", ".mkv", ".mov", ".webm", ".avi"})
MAX_FILE_BYTES = 200 * 1024 * 1024
TREES = ("config", "models/tts/voices", "exemplars", "prompt_overrides")
RESTORE_ALLOWED = (*(t + "/" for t in TREES), "data/studio.db")
#: `keys_unprotected.json` says the one-time NS-W116 was given for THIS home's drive: restored onto another
#: home (a FAT stick) it would hide the warning that home still has to give, so it is machine specific too.
MACHINE_FILES = frozenset(
    {
        "config/runtime.json",
        "config/install_state.json",
        "config/schedule.json",
        f"config/{NOTICE_FILE}",
    }
)


def snapshot_db(db: Path, dest: Path) -> None:
    """Consistent copy of a (possibly WAL-mode, in-use) SQLite database."""
    src = sqlite3.connect(f"{db.as_uri()}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _wanted(f: Path) -> bool:
    # a `~ns<pid>-<thread>-<n>.tmp` is a write a crash cut off (or one in flight), never content to restore
    if is_tmp_name(f.name):
        return False
    return f.is_file() and f.suffix.lower() not in BLOCK_SUFFIXES and f.stat().st_size <= MAX_FILE_BYTES


def _lock_for(path: Path) -> PrepareFn | None:
    """The lock for `config/secrets.dpapi` (`restrict_to_user`), None for every other file.

    The file is machine-scope DPAPI with a fixed entropy, so its ACL is all that keeps another user's programs
    from decrypting it. A file the restore creates (the restored one, the copy kept under
    `data\\backup\\pre-restore-*`, and the one a roll back puts back) would take the folder's ACL, so each is
    written through a temp file that is locked while still empty, then checked again under its final name. A
    lock that fails raises `NS-E120` (a `PermissionError`): the restore leaves that one file as it was, says
    so in its result and restores the rest (`_restore_keys`). A key copy is never left under a broad ACL.
    """
    return restrict_to_user if path.name == SECRETS_FILE else None


def _write_member(z: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path) -> None:
    lock = _lock_for(dest)
    with z.open(info) as src:
        write_stream_atomic(dest, src, prepare=lock)  # a restored file is whole, or still the old one
    if lock is not None:
        lock(dest)


def _copy(src: Path, dst: Path) -> None:
    """`shutil.copy2`, except for the secrets file: its copy is locked before the blob goes in."""
    lock = _lock_for(dst)
    if lock is None:
        shutil.copy2(src, dst)
        return
    # not copy2: CopyFile2 makes the new file under the folder's ACL with the blob already in it
    write_bytes_atomic(dst, read_bytes_retry(src), prepare=lock)
    lock(dst)


def make_backup(
    paths: Paths, out: Path | None = None, *, now: datetime | None = None, notes: list[str] | None = None
) -> Path:
    """Write the backup zip (default: Desktop) and return its path.

    `notes` gets a line for what was left out on purpose: the keys file, when the zip goes to a drive without
    permissions (a FAT or exFAT USB stick), where nothing could keep it private (`NS-W116`). The keys are in
    Credential Manager and the home's own file; Settings > Keys writes both again after a restore elsewhere.
    """
    when = now or datetime.now()
    target = out or (desktop_dir() / f"nightshift-backup-{when:%Y-%m-%d}.zip")
    target.parent.mkdir(parents=True, exist_ok=True)
    entries: list[str] = []
    carries_keys = (paths.config / SECRETS_FILE).is_file()
    keys_out = carries_keys and not volume_keeps_permissions(target.parent)  # asked once for this backup

    def fill(z: zipfile.ZipFile) -> None:
        for tree in TREES:
            root = paths.home / Path(*tree.split("/"))
            if not root.is_dir():
                continue
            for f in sorted(root.rglob("*")):
                if _wanted(f):
                    arc = f.relative_to(paths.home).as_posix()
                    if keys_out and arc == SECRETS_ARC:
                        continue
                    z.write(f, arc)
                    entries.append(arc)
        if paths.db_path.is_file():
            with tempfile.TemporaryDirectory(prefix="ns-backup-") as tmp:
                snap = Path(tmp) / "studio.db"
                snapshot_db(paths.db_path, snap)
                z.write(snap, "data/studio.db")
            entries.append("data/studio.db")
        info = {"created": when.isoformat(timespec="seconds"), "version": __version__, "entries": entries}
        z.writestr(MANIFEST, json.dumps(info, indent=2) + "\n")

    # The zip carries config/secrets.dpapi (machine-scope DPAPI, fixed entropy: any account on this PC
    # decrypts it), so the zip is locked like the file: while still empty, then once more under its final
    # name. A folder on a drive with permissions that cannot take the lock (icacls refused, a share) fails
    # the backup with NS-E120 and writes nothing. On a drive without permissions the keys stay out instead.
    lock = restrict_to_user if carries_keys and not keys_out else None
    write_zip_atomic(target, fill, prepare=lock)  # the Desktop never shows a half-written backup
    if lock is not None and SECRETS_FILE in {Path(e).name for e in entries}:
        try:
            lock(target)
        except OSError:
            target.unlink(missing_ok=True)  # never leave the keys in a zip whose lock did not hold
            raise
    if keys_out and notes is not None:
        notes.append(
            f"left out {SECRETS_ARC} ({UNPROTECTED_CODE}: {NO_PERMISSIONS} in the zip; the keys stay in "
            "Credential Manager and in this PC's own copy)"
        )
    return target


def restore_backup(
    paths: Paths, zip_path: Path, *, include_machine: bool = False, now: datetime | None = None
) -> list[str]:
    """Restore a backup into NIGHTSHIFT_HOME. Overwritten files are first copied to
    `data\\backup\\pre-restore-<time>\\`. Machine-specific config (runtime, install_state, schedule) is
    skipped unless `include_machine`. Raises `NS-E703` for a file that is not a Nightshift backup.

    The keys file (`config/secrets.dpapi`) is the one file a restore may leave out and carry on: on a drive
    without permissions (`NS-W116`) and when its lock fails (`NS-E120`) the old one stays as it was and the
    result says `skipped config/secrets.dpapi (<code>: ...)`. Any other failure puts everything back."""
    when = now or datetime.now()
    try:
        z = zipfile.ZipFile(zip_path)
    except (OSError, zipfile.BadZipFile) as e:
        raise NsError("NS-E703", f"{zip_path} is not a readable zip: {e}") from e
    lines: list[str] = []
    with z:
        if MANIFEST not in z.namelist():
            raise NsError("NS-E703", f"{zip_path.name} has no {MANIFEST}; not a Nightshift backup")
        root = paths.home.resolve()
        saved = paths.backup_dir / f"pre-restore-{when:%Y%m%d-%H%M%S}"
        touched: list[tuple[Path, Path | None]] = []
        keys_private: bool | None = None  # asked once, when the zip turns out to hold the keys file
        for info in z.infolist():
            name = info.filename
            if info.is_dir() or name == MANIFEST:
                continue
            rel = PurePosixPath(name)
            if rel.is_absolute() or ".." in rel.parts:
                raise NsError("NS-E703", f"unsafe path in backup: {name}")
            if not (name in RESTORE_ALLOWED or name.startswith(RESTORE_ALLOWED)):
                lines.append(f"ignored {name}")
                continue
            if name in MACHINE_FILES and not include_machine:
                lines.append(f"skipped {name} (machine specific)")
                continue
            dest = (paths.home / Path(*rel.parts)).resolve()
            if root not in dest.parents:
                raise NsError("NS-E703", f"unsafe path in backup: {name}")
            if name == SECRETS_ARC:
                if keys_private is None:
                    keys_private = volume_keeps_permissions(paths.config)
                try:
                    line, done = _restore_keys(z, info, dest, saved / Path(*rel.parts), keys_private)
                except OSError as e:  # not the lock (a full disk, NS-E119): roll back as for any file
                    undone = _roll_back(touched)
                    why = f"restoring {name} failed ({e}); {undone} file(s) put back from {saved.name}"
                    raise NsError("NS-E703", why) from e
                if done is not None:
                    touched.append(done)
                lines.append(line)
                continue
            keep: Path | None = None
            try:
                if dest.exists():
                    keep = saved / Path(*rel.parts)
                    keep.parent.mkdir(parents=True, exist_ok=True)
                    _copy(dest, keep)
                touched.append((dest, keep))
                if name == "data/studio.db":
                    for suffix in ("-wal", "-shm"):  # stale WAL files would corrupt the restored database
                        Path(str(dest) + suffix).unlink(missing_ok=True)
                dest.parent.mkdir(parents=True, exist_ok=True)
                _write_member(z, info, dest)
            except OSError as e:  # disk full, a locked database, no permission: put everything back
                undone = _roll_back(touched)
                raise NsError(
                    "NS-E703", f"restoring {name} failed ({e}); {undone} file(s) put back from {saved.name}"
                ) from e
            lines.append(f"restored {name}")
    return lines


def _restore_keys(
    z: zipfile.ZipFile, info: zipfile.ZipInfo, dest: Path, keep_at: Path, private: bool
) -> tuple[str, tuple[Path, Path | None] | None]:
    """Restore `config/secrets.dpapi` alone: `(result line, (dest, kept copy) or None when skipped)`.

    A lock that fails (`SecretFileLockError`, NS-E120) never stops the restore: the old keys file stays as it
    was, the copy of it this made is removed (nothing was overwritten) and the line says `skipped` with the
    code. On a drive without permissions (`private` False) nothing is tried. Any other `OSError` goes out.
    """
    name = info.filename
    if not private:
        line = f"skipped {name} ({UNPROTECTED_CODE}: {NO_PERMISSIONS}; the keys file was left as it was)"
        return line, None
    keep = keep_at if dest.exists() else None
    stage = "copying the old one"
    try:
        if keep is not None:
            keep.parent.mkdir(parents=True, exist_ok=True)
            _copy(dest, keep)  # locked while empty, checked under its name
        stage = "writing the restored one"
        dest.parent.mkdir(parents=True, exist_ok=True)
        lock = _lock_for(dest)
        with z.open(info) as src:
            write_stream_atomic(dest, src, prepare=lock)  # a lock that fails here leaves `dest` as it was
        stage = "checking the restored one"
        if lock is not None:
            lock(dest)
    except SecretFileLockError as e:
        why = f"{e.code}: {e.why}, while {stage}"
        if stage == "checking the restored one":  # the new file is in: put the old one back, or take it out
            try:
                if keep is not None:
                    write_bytes_atomic(dest, read_bytes_retry(keep), prepare=_lock_for(dest))
                else:
                    dest.unlink()
            except OSError as again:  # the restored file stays; the copy of the old one is kept
                old = f"the old one is kept in {keep}" if keep is not None else "there was no old one"
                return (
                    f"restored {name}, but its lock could not be checked ({why}); putting the old state back "
                    f"failed too ({again}), {old}"
                ), (dest, keep)
        if keep is not None:
            keep.unlink(missing_ok=True)  # nothing was overwritten: no copy to keep
        return f"skipped {name} ({why}; the keys file was left as it was)", None
    return f"restored {name}", (dest, keep)


def _roll_back(touched: list[tuple[Path, Path | None]]) -> int:
    """Undo a half-done restore: copy each saved original back, delete files that did not exist before.

    A file that cannot be put back (the secrets file whose lock fails, too) is not counted: the NS-E703 says
    how many were, and the saved original stays under `data\\backup\\pre-restore-*`.
    """
    count = 0
    for dest, keep in reversed(touched):
        try:
            if keep is not None and keep.is_file():
                _copy(keep, dest)
            elif keep is None:
                dest.unlink(missing_ok=True)
            count += 1
        except OSError:
            continue
    return count
