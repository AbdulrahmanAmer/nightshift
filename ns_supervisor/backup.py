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
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor.doctor import desktop_dir

MANIFEST = "backup-manifest.json"
BLOCK_SUFFIXES = frozenset({".gguf", ".safetensors", ".onnx", ".mp4", ".mkv", ".mov", ".webm", ".avi"})
MAX_FILE_BYTES = 200 * 1024 * 1024
TREES = ("config", "models/tts/voices", "exemplars", "prompt_overrides")
RESTORE_ALLOWED = (*(t + "/" for t in TREES), "data/studio.db")
MACHINE_FILES = frozenset({"config/runtime.json", "config/install_state.json", "config/schedule.json"})


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
    return f.is_file() and f.suffix.lower() not in BLOCK_SUFFIXES and f.stat().st_size <= MAX_FILE_BYTES


def make_backup(paths: Paths, out: Path | None = None, *, now: datetime | None = None) -> Path:
    """Write the backup zip (default: Desktop) and return its path."""
    when = now or datetime.now()
    target = out or (desktop_dir() / f"nightshift-backup-{when:%Y-%m-%d}.zip")
    target.parent.mkdir(parents=True, exist_ok=True)
    entries: list[str] = []
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        for tree in TREES:
            root = paths.home / Path(*tree.split("/"))
            if not root.is_dir():
                continue
            for f in sorted(root.rglob("*")):
                if _wanted(f):
                    arc = f.relative_to(paths.home).as_posix()
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
    return target


def restore_backup(
    paths: Paths, zip_path: Path, *, include_machine: bool = False, now: datetime | None = None
) -> list[str]:
    """Restore a backup into NIGHTSHIFT_HOME. Overwritten files are first copied to
    `data\\backup\\pre-restore-<time>\\`. Machine-specific config (runtime, install_state, schedule) is
    skipped unless `include_machine`. Raises `NS-E703` for a file that is not a Nightshift backup."""
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
            keep: Path | None = None
            try:
                if dest.exists():
                    keep = saved / Path(*rel.parts)
                    keep.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dest, keep)
                touched.append((dest, keep))
                if name == "data/studio.db":
                    for suffix in ("-wal", "-shm"):  # stale WAL files would corrupt the restored database
                        Path(str(dest) + suffix).unlink(missing_ok=True)
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, dest.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
            except OSError as e:  # disk full, a locked database, no permission: put everything back
                undone = _roll_back(touched)
                raise NsError(
                    "NS-E703", f"restoring {name} failed ({e}); {undone} file(s) put back from {saved.name}"
                ) from e
            lines.append(f"restored {name}")
    return lines


def _roll_back(touched: list[tuple[Path, Path | None]]) -> int:
    """Undo a half-done restore: copy each saved original back, delete files that did not exist before."""
    count = 0
    for dest, keep in reversed(touched):
        try:
            if keep is not None and keep.is_file():
                shutil.copy2(keep, dest)
            elif keep is None:
                dest.unlink(missing_ok=True)
            count += 1
        except OSError:
            continue
    return count
