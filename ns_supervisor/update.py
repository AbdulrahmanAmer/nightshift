"""C2 `ns update <zip> | --rollback` (spec J8, ENGINEERING 10): verified, swappable, reversible.

Order: verify VERSION + `.sha256` sidecar (`NS-E701`, nothing touched) -> stop the engine -> extract to
`<app>.new` -> carry over `.git`, `config`, `answers.json` and untracked data -> copy the settings files
(`config_snapshot`, F-10a; `NS-E702` and nothing changed when that cannot be done) -> swap with
`<app>.prev` -> the venvs are filled again (`resync_venvs`) -> `ns verify --quick` -> re-register the
scheduled task. Any failure after the swap restores `<app>.prev` and the settings files, and raises `NS-E702`.

The venvs get the packages the new program's `uv.lock` pins (P29a, `installer.stage2.web.deplock`). A package
whose lock is missing or was not made for its `pyproject.toml` is refused with `NS-E124` before the swap
(nothing changed); `ns update --rollback` refuses the same way before it writes its record. A program from
before the lock (0.1.14, the 0.1.15 candidates up to rc3) has no lock and is filled its own way.

`--rollback` swaps `<app>` and `<app>.prev` and puts the settings copy back with the program: the older
version gets the files it wrote, not the ones a newer version saved with fields the older one rejects.
Without a usable copy (an update made by an updater older than 0.1.15 whose first start could not take one,
a copy missing or damaged) it takes out of the settings files every setting the older version does not know
(`ns_common.config_compat`, F-10c: the settings added since 0.1.14) and says which; a file it could not change
is named, as one the older version may refuse. The settings are made ready BEFORE the older program is put in
place, after a record of the rollback is written, so an interrupted rollback never leaves the program in
`<app>` with settings it cannot load (`ns_common.rollback_intent`).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

import psutil
import yaml

# (imported here, never inside a function: after the swap `<app>` holds another release's files, and an
# import made then would load that release's module into this updater)
from installer.stage2.web import deplock
from ns_common import __version__, config_compat, config_snapshot, first_start, rollback_intent
from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.devmode import is_dev
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.models import Channel, InstallState, Runtime
from ns_common.paths import Paths
from ns_common.uv import find_uv, home_uv, uv_env
from ns_common.venv_extras import DEV_VENV_EXTRAS, NO_DEPS, VENV_EXTRAS, extras_suffix
from ns_supervisor import update_job
from ns_supervisor.childenv import child_env, window_flags

PRESERVE = (".git", "config", "answers.json")
RELEASE_NOTES_MAX = 20000       # keeps the dashboard's state file small whatever a changelog holds
RENAME_TRIES = 20
RENAME_PAUSE_S = 1.0
BUSY_HINT = (
    "another program still has the Nightshift folder open (a Dashboard, Studio or terminal window?). "
    "Close those windows and run the update again; your previous version is still in place."
)
RESTORE_HINT = (
    "the previous version could not be put back because another program still has the Nightshift "
    "folder open. "
    "Close every Nightshift window, then run `ns update --rollback` (the previous version is in the folder "
    "ending in .prev)."
)
#: Written into a complete program folder (appcopy.MARKER): INSTALL.cmd trusts <home>\app only with it.
APP_MARKER = ".ns-app.done"
UPDATE_VENVS = ("studio", "supervisor", "tts")      # extras: ns_common.venv_extras (shared with setup)
#: Console scripts the app installs into every venv (`[project.scripts]`). A running copy cannot be replaced.
ENTRY_EXES = ("ns.exe", "studio.exe")
OLD_SUFFIX = ".old"
NO_GIT_HINT = (
    "Git is not installed, and `ns update --git` needs it. Install Git for this "
    "(https://git-scm.com/download/win), or update without it: `ns update --github`, or the Update "
    "shortcut with a Nightshift-Update zip."
)
NOTHING_CHANGED = "Nothing was changed: the program and your settings are as they were."
#: A rollback refused over the previous program's lock: said after any note about an earlier, interrupted one.
ROLLBACK_NOT_STARTED = (
    "ns update --rollback did not start: the program, your settings and the Python environments are as they "
    "were. To go back to that version, run INSTALL.cmd from its zip and choose the same folder."
)
#: How a result names a program whose VERSION file is missing or empty (`config_snapshot.program_version`
#: answers `-`): never the version of the code that happens to run the rollback.
UNKNOWN_VERSION = "unknown version"
#: The way out when there is no program folder: no Nightshift command can start then (every venv is an
#: editable install of it), so this is only read by someone running the code from elsewhere.
NO_APP_HINT = (
    "Run INSTALL.cmd from the Nightshift zip (choose the same folder): it puts the program folder back."
)
SETTINGS_REFUSED = (
    f"{NOTHING_CHANGED} Close programs that use the "
    "Nightshift folder, make room on the disk if it is full, and run the update again."
)
GIT_NO_COPY_NOTE = "no settings copy kept for ns update --rollback (a git install has no program backup)"
SETTINGS_BACKUP_HINT = (
    "If Nightshift does not start, restore a backup of your settings: ns restore <backup.zip> --all."
)


#: D-39. After the engine was stopped, how long an update waits for every program that runs from this
#: install's Python environments to end before it refuses (NS-E706): an engine process still exiting ends
#: within seconds; a Studio window, a terminal command or a Python prompt does not, and is named.
HOLDER_WAIT_S = 60.0
#: A venv whose refresh failed on a file in use is tried SYNC_TRIES times, SYNC_PAUSE_S apart (the holder
#: check ran before, so this is a program started since); then the update stops and says what is left.
SYNC_TRIES = 3
SYNC_PAUSE_S = 10.0
HELD_SIGNS = ("os error 32", "os error 5", "being used by another process", "access is denied")
WAITING_FOR_PROGRAMS = (
    "Nightshift is stopped; waiting until no program runs from its Python environments any more (a Studio "
    "window, a terminal command, a Python prompt)."
)
#: Every way out below is typed into the program that is in `<app>` at that moment: all venvs are editable
#: installs of `<app>`, so after a rollback `ns` is the OLDER version's code. From 0.1.14 to 0.1.15-rc3 a
#: terminal `ns update` without `--detach` is D-39 itself (it holds its own ns.exe, fails in the re-sync and
#: says "rolled back"), so the commands named are the `--detach` forms, which work in every version that has
#: the flag (`takes_detach` reads the program on disk); a program without it gets INSTALL.cmd.
DETACH_RESULT = (
    "It returns at once; the result is the last line of the log it names (and the entry with that log in "
    "run/update_state.json)."
)
SWAP_STOPPED = (
    "The update to {new} stopped while it was swapping the program folders. If Nightshift does not open "
    "because the program folder is missing, run INSTALL.cmd from the Nightshift {old} zip (or a newer one) "
    "and choose this folder."
)
SYNC_STOPPED = (
    "The update to {new} stopped after {new}'s program was put in place, while it was refreshing or "
    "checking the Python environments. To fill every environment: "
)
RESTORE_STOPPED = (
    "The update to {new} failed, and its process stopped while it was putting {old} back. Open Nightshift "
    "from a Desktop shortcut; if it does not open, run INSTALL.cmd from the Nightshift {old} zip (or a newer "
    "one) and choose this folder."
)
ROLLBACK_PREPARE_STOPPED = (
    "The rollback stopped before the program was changed. Open Nightshift: it puts your settings back."
)
ROLLBACK_SWAP_STOPPED = (
    "The rollback stopped while it was swapping the program folders. Open Nightshift from a Desktop "
    "shortcut: the program in place starts with settings it can read, and a version from 0.1.15 on finishes "
    "or undoes the rollback at its start. If the shortcut says the program folder is missing, run "
    "INSTALL.cmd from the Nightshift zip (this version or newer) and choose this folder, then open "
    "Nightshift."
)
ROLLBACK_SYNC_STOPPED = (
    "The rollback to {old} stopped while it was refreshing the Python environments: {old}'s program is in "
    "place, and the environments may still hold {new}'s packages. To finish it: "
)


@dataclass
class UpdateEnv:
    """Side effects of an update; replaced in tests."""

    stop: Callable[[], None]
    sync: Callable[[Path], None]
    verify: Callable[[Path], bool]
    reregister: Callable[[Path], list[str]]
    untracked: Callable[[Path], list[str]] = field(default=lambda app: git_untracked(app))
    #: Called before a rollback renames folders: waits until the processes `verify` started have exited.
    settle: Callable[[], object] = field(default=lambda: None)
    #: The bound of the wait for programs that run from the Python environments (`wait_no_holders`).
    holder_wait_s: float = HOLDER_WAIT_S


@dataclass
class UpdateResult:
    """What changed."""

    old_version: str
    new_version: str
    notes: list[str] = field(default_factory=list)
    #: The `## [<new version>]` section of the new program's CHANGELOG.md; empty when there is none.
    release_notes: str = ""
    #: A rollback's plain sentence about the settings files (restored, which of them lost later changes, or
    #: why they could not be restored). The CLI puts it in the result line the dashboard shows.
    settings: str = ""


def release_notes(app: Path, version: str) -> str:
    """The `## [<version>]` section of `<app>/CHANGELOG.md` (heading line included), else an empty string.

    `notes` lists what an update carried over; this is what the owner wants to read afterwards, what changed.
    A missing or unreadable changelog, or one without that version, is not an error: the update worked.
    """
    version = version.strip().removeprefix("v")
    if not version:
        return ""
    try:
        text = (app / "CHANGELOG.md").read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return ""
    lines: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = line[3:].strip().startswith(f"[{version}]")
        if inside:
            lines.append(line.rstrip())
    return "\n".join(lines).strip()[:RELEASE_NOTES_MAX]


def sha256_file(path: Path) -> str:
    """Hex SHA256 of a file."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_sidecar(zip_path: Path) -> str | None:
    """The expected digest from `<zip>.sha256` (`<hex>  <name>` or just `<hex>`), or None."""
    side = zip_path.with_name(zip_path.name + ".sha256")
    try:
        token = side.read_text(encoding="utf-8").split()[0].lower()
    except (OSError, IndexError):
        return None
    return token if len(token) == 64 and all(c in "0123456789abcdef" for c in token) else None


def verify_package(zip_path: Path) -> tuple[str, str]:
    """Check sidecar digest and VERSION; returns (version, member prefix). Raises `NS-E701`."""
    if not zip_path.is_file():
        raise NsError("NS-E701", f"{zip_path} does not exist")
    want = read_sidecar(zip_path)
    if want is None:
        raise NsError("NS-E701", f"missing or unreadable {zip_path.name}.sha256")
    got = sha256_file(zip_path)
    if got != want:
        raise NsError("NS-E701", f"SHA256 mismatch for {zip_path.name}: expected {want[:12]}, got {got[:12]}")
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = [n for n in z.namelist() if not n.endswith("/")]
            prefix = ""
            if "VERSION" not in names:
                tops = {n.split("/", 1)[0] for n in names if "/" in n}
                if len(tops) == 1 and f"{next(iter(tops))}/VERSION" in names:
                    prefix = next(iter(tops)) + "/"
                else:
                    raise NsError("NS-E701", "the zip has no VERSION file at its root")
            version = z.read(prefix + "VERSION").decode("utf-8").strip()
    except zipfile.BadZipFile as e:
        raise NsError("NS-E701", f"{zip_path.name} is not a valid zip: {e}") from e
    if not version:
        raise NsError("NS-E701", "VERSION is empty")
    return version, prefix


def safe_extract(zip_path: Path, dest: Path, prefix: str) -> None:
    """Extract members under `prefix` into `dest`, refusing anything that would land outside it."""
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            name = info.filename
            if not name.startswith(prefix) or name == prefix:
                continue
            rel = PurePosixPath(name[len(prefix) :])
            if rel.is_absolute() or ".." in rel.parts or (rel.parts and ":" in rel.parts[0]):
                raise NsError("NS-E701", f"unsafe path in zip: {name}")
            target = (dest / Path(*rel.parts)).resolve()
            if root != target and root not in target.parents:
                raise NsError("NS-E701", f"unsafe path in zip: {name}")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)


def git_untracked(app: Path) -> list[str]:
    """Top-level entries git does not track (ignored included); empty when git or the repo is absent."""
    git = shutil.which("git")
    if git is None or not (app / ".git").exists():
        return []
    try:
        r = subprocess.run(
            [git, "-C", str(app), "ls-files", "--others", "--directory", "--no-empty-directory"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=child_env(),
            creationflags=window_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    tops = {line.strip().strip("/").split("/")[0] for line in r.stdout.splitlines() if line.strip()}
    return sorted(t for t in tops if t and t != "__pycache__")


def carry_over(old: Path, new: Path, extra: list[str]) -> list[str]:
    """Copy `.git`, `config`, `answers.json` and untracked data from the old tree into the new one."""
    carried: list[str] = []
    for name in dict.fromkeys([*PRESERVE, *extra]):
        src = old / name
        dst = new / name
        if not src.exists() or dst.exists():
            continue
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        carried.append(name)
    return carried


def _sibling(app: Path, suffix: str) -> Path:
    return app.with_name(app.name + suffix)


def app_fingerprint(folder: Path) -> str:
    """Version plus short hashes of manifest.json and pyproject.toml (same text as appcopy.fingerprint and
    `config_snapshot.program_fingerprint`, which the first start compares with the settings copy)."""
    return config_snapshot.program_fingerprint(folder)


def write_app_marker(app: Path, log: NsLogger) -> None:
    """Mark `app` as a finished program folder so a later INSTALL.cmd run swaps it instead of deleting it."""
    try:
        (app / APP_MARKER).write_text(app_fingerprint(app), encoding="utf-8")
    except OSError as e:     # the update itself worked; the next INSTALL.cmd just copies the folder again
        log.warn("app_marker_failed", error=str(e))


def current_version(app: Path) -> str:
    """VERSION file of the app tree, else the package version."""
    try:
        return (app / "VERSION").read_text(encoding="utf-8").strip() or __version__
    except OSError:
        return __version__


def _shown_version(folder: Path) -> str:
    """How a result names the program in `folder`: its VERSION, `UNKNOWN_VERSION` when that is missing or
    empty (never the version of the code that runs)."""
    return UNKNOWN_VERSION if config_snapshot.program_version(folder) == "-" else current_version(folder)


def leave_app(app: Path, paths: Paths) -> None:
    """Move this process out of `app` (to the install home, else the temp dir) before it is renamed.

    Windows refuses to rename a directory that is any process's working directory (WinError 32); the
    shortcuts start `ns.exe` with `<app>` as cwd, so without this every update fails with `NS-E702`.
    """
    try:
        here = Path.cwd().resolve()
    except OSError:
        here = None
    root = app.resolve()
    if here is not None and here != root and root not in here.parents:
        return
    target = paths.home if paths.home.is_dir() else Path(tempfile.gettempdir())
    os.chdir(target)


def rename_with_retry(
    src: Path, dst: Path, *, tries: int | None = None, pause_s: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Rename a folder, retrying while Windows still reports it in use (WinError 32 or 5).

    Processes that were just told to stop (the engine, the dashboard service, a closing window) release the
    folder a moment later, so the first attempt can fail although nothing is wrong.
    """
    tries = RENAME_TRIES if tries is None else tries
    pause_s = RENAME_PAUSE_S if pause_s is None else pause_s
    for attempt in range(1, tries + 1):
        try:
            src.rename(dst)
            return
        except OSError:
            if attempt >= tries:
                raise
            sleep(pause_s)


def _restore(app: Path, prev: Path, log: NsLogger, settle: Callable[[], object] | None = None) -> None:
    """Put `<app>.prev` back in place of a failed new tree.

    The failure that brought us here is usually `ns verify --quick` failing: the engine it started (and its
    llama-server and TTS children) may still be exiting with the new tree open, so wait for them first and
    retry both renames like the forward swap does.
    """
    if settle is not None:
        try:
            settle()
        except Exception as e:  # waiting is best effort; the retrying renames below are the safety net
            log.warn("rollback_settle_failed", error=str(e))
    failed = _sibling(app, ".failed")
    shutil.rmtree(failed, ignore_errors=True)
    if app.exists():
        rename_with_retry(app, failed)
    rename_with_retry(prev, app)
    shutil.rmtree(failed, ignore_errors=True)
    log.warn("update_restored", app=str(app))


# --------------------------------------------------------------------------------------------------------
# the settings copy (F-10a): config_snapshot.py says what is copied and why
# --------------------------------------------------------------------------------------------------------
def _names(names: list[str]) -> str:
    return ", ".join(names) if names else "none"


@dataclass
class _UpdateCopy:
    """The settings side of an update, decided before anything is swapped."""

    files: dict[str, bytes]  # the settings files when the update started (a failed update puts these back)
    wrote: bool  # this update wrote the copy: it goes again when the update does not happen
    had_backup_copy: bool  # the copy it replaced was the one of the program backup it replaces
    notes: list[str]


def _save_settings_copy(app: Path, prev: Path, paths: Paths, log: NsLogger) -> _UpdateCopy:
    """Copy the settings files beside the program backup BEFORE anything is swapped.

    An update that could not be undone completely is not started: `NS-E702`, nothing changed, and a copy that
    is already there is left whole. A copy is written over only together with its own program (the one in
    `<app>.prev`), when it is damaged, or when it is the leaving program's own and no rollback recorded that
    its restore failed. A copy a rollback could not put back stays as the copy for the leaving program. A
    whole copy of any other program is never written over: the update goes on, keeps no copy of its own and
    says whose copy that is and where (`config_snapshot.before_update`).
    """
    zip_path = config_snapshot.path_for(app)
    leaving = app_fingerprint(app)
    backup = app_fingerprint(prev) if prev.is_dir() else None
    try:
        action, slot = config_snapshot.before_update(zip_path, leaving=leaving, backup=backup)
    except config_snapshot.SnapshotError as e:
        log.error("settings_copy_not_replaced", str(e))
        raise NsError("NS-E702", f"{e}. {NOTHING_CHANGED}") from e
    try:
        files = config_snapshot.capture(paths.config)
        if action == config_snapshot.SAVE:
            config_snapshot.save(
                zip_path, files, version=current_version(app), fingerprint=leaving, taken_by="update"
            )
    except config_snapshot.SnapshotError as e:
        log.error("settings_copy_failed", str(e))
        raise NsError(
            "NS-E702",
            f"could not save a copy of your settings files before the update ({e}). {SETTINGS_REFUSED}",
        ) from e
    kept = slot.copy
    if action == config_snapshot.KEEP and kept is not None:
        made = kept.created.replace("T", " ")
        log.warn("settings_copy_kept", copy=str(zip_path), made=made)
        note = (
            f"the settings copy for {current_version(app)} (made {made}) is kept in {zip_path} for "
            "ns update --rollback, not replaced by the settings files as they are now: the last rollback "
            "could not put it back, so those may be a newer version's"
        )
        return _UpdateCopy(files, False, False, [note])
    if action == config_snapshot.FOREIGN and kept is not None:
        note = config_snapshot.foreign_note(zip_path, kept, leaving=leaving, backup=backup)
        log.warn("settings_copy_foreign", copy=str(zip_path), belongs_to=kept.version)
        return _UpdateCopy(files, False, False, [note])
    log.info("settings_copy_saved", files=sorted(files), copy=str(zip_path))
    notes = [f"settings files saved for ns update --rollback: {_names(sorted(files))}"]
    if slot.damaged:
        notes.append(f"the damaged settings copy that was in {zip_path} was replaced ({slot.problem})")
    had_backup = kept is not None and backup is not None and kept.fingerprint == backup
    return _UpdateCopy(files, True, had_backup, notes)


def _put_settings_back(paths: Paths, files: dict[str, bytes], log: NsLogger, copy: Path | None) -> str:
    """After a failed update: the settings files as they were before it (`ns verify` ran the new version,
    which may have saved them again). `copy` is the saved zip of them, if the update saved one (a git update
    does not). Returns the sentence that goes into the error."""
    try:
        live = config_snapshot.capture(paths.config)
        # (a settings file only the failed new version made goes too: the rollback does the same)
        done = config_snapshot.restore(paths.config, files, live=live, remove_extra=True)
    except config_snapshot.SnapshotError as e:
        log.error("settings_restore_failed", str(e))
        where = (
            f" The copy made before the update is {copy} (a zip: copy the files in its config folder over "
            f"{paths.config})."
            if copy is not None
            else ""
        )
        return f" Your settings files could not be put back ({e}).{where}"
    if copy is not None:
        config_snapshot.discard(copy)       # its program backup is gone: the old program is back in place
    log.info(
        "settings_restored_after_failed_update",
        files=done.written,
        changed=done.changed,
        removed=done.removed,
    )
    return " Your settings files were put back as they were before the update."


def _read_settings_copy(
    app: Path, prev: Path
) -> tuple[config_snapshot.Snapshot | None, str, config_snapshot.Slot]:
    """The copy that belongs to the program in `prev`, else (None, why not); and what the zip holds."""
    slot = config_snapshot.look(config_snapshot.path_for(app))
    snap, problem = slot.copy, slot.problem
    if snap is None:
        why = str(problem)
        if problem is not None and problem.missing:
            why = (
                "no copy of them was saved when the version you are leaving was installed "
                "(an update made by an older updater does not save one)"
            )
        return None, why, slot
    if snap.fingerprint != app_fingerprint(prev):
        why = (
            f"the saved copy belongs to {snap.version}, "
            f"not to the program that was kept ({current_version(prev)})"
        )
        return None, why, slot
    return snap, "", slot


def _copy_left_alone(
    slot: config_snapshot.Slot, zip_path: Path, left_fingerprint: str, left_version: str
) -> str:
    """Why a rollback did not replace the copy in `zip_path` with the settings as they are now."""
    snap = slot.copy
    if snap is None:
        return (
            f"the settings copy {zip_path} could not be read and was left as it is, as it may still be "
            f"whole ({slot.problem})"
        )
    made = snap.created.replace("T", " ")
    if snap.fingerprint == left_fingerprint:  # its restore never finished: it is still that program's copy
        return (
            f"the settings copy for {left_version} (made {made}) stays in {zip_path}: ns update --rollback "
            f"again goes back to {left_version} with the settings files it wrote"
        )
    return (
        f"the settings copy in {zip_path} belongs to {snap.version} (made {made}), another program, and was "
        "left as it is: it may be the only copy of those settings files"
    )


def _live_settings(paths: Paths, log: NsLogger) -> tuple[dict[str, bytes] | None, str]:
    try:
        return config_snapshot.capture(paths.config), ""
    except config_snapshot.SnapshotError as e:
        log.warn("settings_read_failed", str(e))
        return None, str(e)


def _unreadable(config: Path, names: Iterable[str]) -> list[str]:
    """The files among `names` that the models of the RUNNING code cannot load (empty: all of them load).

    A sanity check on what was just put back: a rollback must not leave a file that the program doing the
    rollback (and so the one that rolls forward again) cannot read. It never blocks anything; it only reports.
    """
    bad: list[str] = []
    for name in names:
        if name.endswith(".bak"):
            continue
        try:
            text = config.joinpath(*name.split("/")).read_text(encoding="utf-8-sig")
            if name == "runtime.json":
                Runtime.model_validate_json(text)
            elif name == "install_state.json":
                InstallState.model_validate_json(text)
            elif name.startswith("channels/"):
                Channel.model_validate(yaml.safe_load(text))
            elif name.endswith(".json"):
                if not isinstance(json.loads(text), dict):
                    raise ValueError("not an object")
            elif not isinstance(yaml.safe_load(text), dict):
                raise ValueError("not a mapping")
        except Exception:
            bad.append(name)
    return bad


def _readable_by(config: Path, version: str, log: NsLogger) -> tuple[list[str], list[str]]:
    """Take out of every settings file the settings `version` does not know (F-10c, `config_compat`).

    The older version's models are strict: one key it never had makes it refuse the whole file, and
    runtime.json refused means it cannot start. Every file is read back afterwards and checked again. Returns
    (what was taken out, as `file (key, key)`, the files that still hold such a setting: they could not be
    written, or did not read back clean). A file that does not parse is left as it is.
    """
    dropped: list[str] = []
    failed: list[str] = []
    for name in config_snapshot.tracked_names(config):
        if config_compat.model_for(name) is None:
            continue
        path = config.joinpath(*name.split("/"))
        is_yaml = name.endswith(".yaml")
        try:
            text = read_text_retry(path, "utf-8-sig")
            data = yaml.safe_load(text) if is_yaml else json.loads(text)
        except (OSError, ValueError, yaml.YAMLError):
            continue
        keys = config_compat.for_version(name, data, version)
        if not keys:
            continue
        out = (
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
            if is_yaml
            else json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        )
        try:
            write_text_atomic(path, out)
            back = read_text_retry(path, "utf-8-sig")
            again = yaml.safe_load(back) if is_yaml else json.loads(back)
        except (OSError, ValueError, yaml.YAMLError) as e:
            log.error("settings_unknown_to_older_not_removed", str(e), file=name, keys=keys)
            failed.append(name)
            continue
        if config_compat.for_version(name, again, version):  # the check: what is on disk now
            log.error("settings_unknown_to_older_not_removed", "not clean on disk", file=name, keys=keys)
            failed.append(name)
            continue
        dropped.append(f"{name} ({', '.join(keys)})")
    if dropped:
        log.info("settings_unknown_to_older_removed", version=version, removed=dropped)
    return dropped, failed


def _for_older_sentence(
    version: str,
    dropped: list[str],
    failed: list[str],
    *,
    restored: bool,
    readable_as: str | None = None,
    prev_name: str = "app.prev",
) -> str:
    """What the rollback did to make the files readable by `version` (empty when it had nothing to do).
    `readable_as` is what the older program's VERSION file holds, when that is not the name `version`: one
    that is no version number (missing, empty, any other text) is always said, with what follows from it."""
    parts: list[str] = []
    known = bool(config_compat.version_key(version if readable_as is None else readable_as))
    if not known:
        found = "" if dropped or failed else " None of your settings files holds one."
        parts.append(
            f"Because the version of the program in {prev_name} could not be read, it was treated as older "
            f"than every release: every setting added since {config_compat.BASE_VERSION} is taken out for "
            f"it.{found}"
        )
    if dropped:
        where = "The copy held settings" if restored else "They held settings"
        parts.append(
            f"{where} that {version} does not know; they were removed (what you set in them is lost): "
            f"{'; '.join(dropped)}."
        )
    if failed:
        parts.append(
            f"These hold settings {version} does not know and could not be changed, so {version} may refuse "
            f"them: {_names(failed)}."
        )
    if (
        known
        and (dropped or not restored)
        and config_compat.version_key(version) < config_compat.version_key(config_compat.BASE_VERSION)
    ):
        parts.append(f"(Only the settings added since {config_compat.BASE_VERSION} are checked.)")
    return " ".join(parts)


@dataclass
class _Prepared:
    """What a rollback did to the settings BEFORE the swap, and what is left for after it."""

    sentence: str
    notes: list[str]
    after: rollback_intent.AfterSwap
    damaged_slot: bool = False  # (no copy put back) the zip was damaged: the forward copy replaces it


def _not_restored_sentence(
    why: str, version: str, dropped: list[str], failed: list[str], *, no_copy: bool
) -> str:
    """The first sentence when no copy was put back. With no copy at all and nothing for `version` to
    refuse, nothing failed: it says so instead of "could not be restored"."""
    if no_copy and not dropped and not failed:
        return (
            f"There was no copy of your settings files to put back ({why}). They stay as they are, and none "
            f"of them holds a setting {version} does not know."
        )
    no = f"Your settings files could not be restored: {why}. They stay as they are now"
    if dropped:
        return no + ", except for the settings below."
    if failed:  # (named by `_for_older_sentence`)
        return no + "."
    return no + f" (none of them holds a setting {version} does not know)."


def _prepare_settings(
    paths: Paths,
    copy: config_snapshot.Snapshot | None,
    why_not: str,
    live: dict[str, bytes] | None,
    live_error: str,
    *,
    slot: config_snapshot.Slot,
    zip_path: Path,
    restored_version: str,
    left_version: str,
    left_fingerprint: str,
    can_go_forward: bool,
    log: NsLogger,
    readable_as: str | None = None,
    prev_name: str = "app.prev",
) -> _Prepared:
    """BEFORE a rollback puts the older program in place: make the settings files ones it loads. The older
    program may have no rollback code (0.1.14 has none), so what it finds when it starts must already be
    right; the newer program, still in `<app>`, reads both shapes. Puts `copy` back, else (or when that
    fails) takes out what `restored_version` does not know (`_readable_by`, which also checks the result).
    Returns the sentence for the result line, detail lines, and what is left for after the swap (the copy
    for going forward, files to set aside, a failed restore to record). Never raises (but BaseException).

    Without a `copy` to put back, the zip (`slot`) is replaced after the swap only when nothing in it can be
    used (there is none, or it is damaged). A complete copy of another program stays: when it belongs to the
    program that goes to `<app>.prev` (its last restore failed), the live files are not that program's, and
    saving them under its name would make the next rollback "restore" a newer version's files.

    `restored_version` is the name the sentences use; `readable_as` is what the older program's VERSION file
    holds (`config_snapshot.program_version`: `-` when it is missing or empty) and decides what is taken
    out: a version that cannot be read is older than every release (`config_compat.for_version`)."""
    notes: list[str] = []
    after = rollback_intent.AfterSwap()
    readable_as = restored_version if readable_as is None else readable_as

    def for_older(dropped: list[str], failed: list[str], *, restored: bool) -> str:
        return _for_older_sentence(
            restored_version, dropped, failed, restored=restored, readable_as=readable_as, prev_name=prev_name
        )

    if copy is None or live is None:
        own = slot.copy is not None and slot.copy.fingerprint == left_fingerprint
        if copy is None and (slot.free or (own and not config_snapshot.is_unrestored(zip_path))):
            # no copy there, a damaged one, or the leaving program's own copy that no failed restore holds
            # (the live files are that program's): nothing in it to lose
            after.publish_forward = can_go_forward
        elif copy is None:  # a whole copy of another program, or one that could not be read: never replaced
            notes.append(_copy_left_alone(slot, zip_path, left_fingerprint, left_version))
        else:  # the live files could not be read: the copy is the restored program's and goes unused
            after.mark_unrestored = True
        why = why_not if copy is None else live_error
        log.warn("settings_not_restored", why)
        dropped, failed = _readable_by(paths.config, readable_as, log)
        no_copy = copy is None and slot.problem is not None and slot.problem.missing
        no = _not_restored_sentence(why, restored_version, dropped, failed, no_copy=no_copy)
        older = for_older(dropped, failed, restored=False)
        hint = "" if no_copy and not dropped and not failed else SETTINGS_BACKUP_HINT
        sentence = " ".join(p for p in (no, older, hint) if p)
        return _Prepared(sentence, notes, after, damaged_slot=copy is None and slot.damaged)
    try:
        done = config_snapshot.restore(paths.config, copy.files, live=live, remove_extra=False)
    except config_snapshot.SnapshotError as e:
        log.error("settings_restore_failed", str(e))
        after.mark_unrestored = True
        dropped, failed = _readable_by(paths.config, readable_as, log)
        no = _not_restored_sentence(str(e), restored_version, dropped, failed, no_copy=False)
        left = (
            f"The copy made for {restored_version} is still in {zip_path} "
            f"(a zip: the files in its config folder are what {restored_version} wrote)."
        )
        older = for_older(dropped, failed, restored=False)
        return _Prepared(" ".join(p for p in (no, older, left, SETTINGS_BACKUP_HINT) if p), notes, after)
    # files the older version never had leave only after the swap, once they are saved for going forward
    after.publish_forward = can_go_forward
    after.keep_only = sorted(copy.files)
    log.info("settings_restored", files=done.written, changed=done.changed)
    back = "back as " + (
        f"{restored_version} had them when you went back"
        if copy.taken_by == "rollback"
        else f"{restored_version} left them"
    )
    sentence = (
        f"Your settings files are {back} (copy made {copy.created.replace('T', ' ')}): "
        f"{_names(done.written)}."
    )
    if not done.written:
        sentence = (
            f"No settings files were saved with {restored_version} (there were none when the copy was "
            "made), so none were restored."
        )
    elif done.changed:
        sentence += (
            " These were different from the copy and went back to it (the update itself saved some of them "
            f"again; anything you changed in them is lost): {_names(done.changed)}."
        )
    else:
        sentence += " None of them was different from the copy."
    # A copy taken late (a first start whose earlier try failed) can hold a setting the older version lacks;
    # a file the copy does not hold (one the newer version made) may too: it stays until it is set aside.
    dropped, failed = _readable_by(paths.config, readable_as, log)
    older = for_older(dropped, failed, restored=True)
    if older:
        sentence += " " + older
    bad = _unreadable(paths.config, done.written)
    if bad:
        log.warn("settings_unreadable_after_restore", files=bad)
        notes.append(f"warning: this version cannot read {_names(bad)} as they are now")
    return _Prepared(sentence, notes, after)


def _after_swap_notes(
    prepared: _Prepared,
    finished: rollback_intent.Finished,
    *,
    zip_path: Path,
    restored_version: str,
    left_version: str,
    log: NsLogger,
) -> list[str]:
    """The detail lines for what `rollback_intent.finish` did once the older program was in place."""
    notes = list(finished.problems)
    for problem in finished.problems:
        log.warn("settings_copy_failed", problem)
    if finished.stayed:
        notes.append(f"could not set aside (still there): {_names(finished.stayed)}")
    if finished.removed:
        log.info("settings_set_aside", files=finished.removed)
        notes.append(f"set aside, because {restored_version} never had them: {_names(finished.removed)}")
    if finished.kept_forward and prepared.damaged_slot:
        notes.append(
            f"the damaged settings copy in {zip_path} was replaced: ns update --rollback again goes "
            f"forward to {left_version} with the settings as they were just before this rollback"
        )
    elif finished.kept_forward:
        notes.append(
            f"ns update --rollback again goes forward to {left_version} with the settings as they were "
            "just before this rollback"
        )
    return notes


def _swap_programs(app: Path, prev: Path, tmp: Path) -> None:
    """The three renames that swap `<app>` and `<app>.prev` through `<app>.swap`.

    `OSError`: the folders were put back as they were when that could be done (the caller looks at `<app>`
    to know). Any other interruption (Ctrl+C, the process told to end): the folders are left in a state the
    next start understands (`_settle_folders`) and the interruption goes on.

    Nothing is ever deleted to make room: `rollback_app` has settled a leftover `<app>.swap`
    (`rollback_intent.settle_swap`) or refused to start, so the name is free; if something takes it after
    that, this fails before the first rename and the folders are as they were."""
    if os.path.lexists(tmp):
        raise FileExistsError(f"{tmp} is in the way")
    try:
        rename_with_retry(app, tmp)
        try:
            rename_with_retry(prev, app)
        except OSError:
            rename_with_retry(tmp, app)
            raise
        try:
            rename_with_retry(tmp, prev)
        except OSError:
            rename_with_retry(app, prev)
            rename_with_retry(tmp, app)
            raise
    except OSError:
        raise
    except BaseException:
        _settle_folders(app, prev, tmp)
        raise


def _settle_folders(app: Path, prev: Path, tmp: Path) -> None:
    """After an interruption in the middle of the swap: a program in `<app>` whenever a rename can do it.
    Without `<app>` the leaving program goes back (the next start of it undoes the settings); with the older
    program already in `<app>` the swap is finished (its settings are ready). Best effort, one try each."""
    try:
        if not app.exists() and tmp.is_dir():
            os.rename(tmp, app)
        elif app.is_dir() and tmp.is_dir() and not prev.exists():
            os.rename(tmp, prev)
    except OSError:
        pass


def _settle_swap(app: Path, log: NsLogger, *, for_update: bool = False) -> str:
    """Give a leftover `<app>.swap` its place (`rollback_intent.settle_swap`; the caller holds the copy's
    lock) and log what was said. Returns the sentence (empty: there was none)."""
    said = rollback_intent.settle_swap(app, for_update=for_update)
    if said:
        log.warn(
            first_start.SWAP_EVENT,
            said,
            folder=str(rollback_intent.swap_for(app)),
            settled=not rollback_intent.swap_waits(app),
        )
    return said


def _left_notes(app: Path, said: Iterable[str]) -> list[str]:
    """One line for every program folder an earlier interrupted rollback left beside `app` that `said` does
    not name yet: the owner is told at every update and rollback until the folder is gone."""
    told = " ".join(said)
    return [
        rollback_intent.left_sentence(f) for f in rollback_intent.left_folders(app) if str(f) not in told
    ]


def _recover_rollback(app: Path, paths: Paths, log: NsLogger) -> list[str]:
    """Before an update looks at the settings, under the copy's lock: a folder a rollback left in the middle
    of its renames gets its place (`rollback_intent.settle_swap`), then an interrupted rollback is finished
    or undone (`rollback_intent.recover`). Returns what was done and what is left beside the program, one
    sentence each (empty: nothing)."""
    notes: list[str] = []
    if rollback_intent.path_for(app).exists() or rollback_intent.swap_waits(app):
        with config_snapshot.locked(config_snapshot.path_for(app)) as held:
            if not held:
                raise NsError(
                    "NS-E702",
                    f"another Nightshift program is using the settings copy. Close Nightshift windows and "
                    f"try again. {NOTHING_CHANGED}",
                )
            settled = _settle_swap(app, log, for_update=True)
            rec = rollback_intent.recover(app, paths.config)
        _refuse_if_pending(rec, log, settled)
        if rec.sentence:
            log.warn("rollback_recovered", rec.sentence)
        notes += [s for s in (settled, rec.sentence) if s]
    return notes + _left_notes(app, notes)


def _refuse_if_pending(rec: rollback_intent.Recovery, log: NsLogger, settled: str = "") -> None:
    """`NS-E702` when an interrupted rollback could not be finished or undone: the settings may be half put
    back, and an update or a new rollback from there would save or overwrite them (its record stays for the
    next try). `settled` is what `_settle_swap` said just before: a folder may have been renamed, and the
    error then says so instead of "nothing was changed"."""
    if rec.pending:
        log.error("rollback_recovery_failed", rec.sentence)
        nothing = f"{settled} Nothing else was changed." if settled else NOTHING_CHANGED
        raise NsError(
            "NS-E702",
            f"{rec.sentence} {nothing} Close every Nightshift window (and anything that has a file "
            "in the config folder open), then try again.",
        )


def _voice_notes(paths: Paths) -> list[str]:
    """The sentences about a designed or cloned voice that did not speak in the verify step just run.

    That voice is a warning, never a reason to roll back (`verify.talker_warning`); the same sentence is
    already waiting as a morning note (`verify.leave_voice_notes`); here it is part of the update's result.
    """
    try:
        from ns_supervisor import verify

        return verify.read_talker_warnings(paths)
    except Exception:  # a note must never turn a good update into a rollback
        return []


# --------------------------------------------------------------------------------------------------------
# D-39: nothing rewrites a Python environment a program runs from, and every failure says what is in place
# --------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Holder:
    """A running program that keeps files of a Python environment of this install in use."""

    pid: int
    program: str
    started: str

    def __str__(self) -> str:
        return f"{self.program} (process {self.pid}, started {self.started})"


def holders(paths: Paths, venv: str | None = None) -> list[Holder]:
    """The programs on this PC that run from `<home>\\venvs` (or from one venv of it).

    A program counts when its image is in there (a venv's `python.exe`, `ns.exe`, `studio.exe`) or when a
    path in there is on its command line. The second is how the process that really holds the file is
    found: a console command runs as `<the base interpreter> <venv>\\Scripts\\ns.exe ...`, an image outside
    the venvs, and keeps that `ns.exe` open without delete sharing for as long as it lives (pydantic's
    plugin lookup opens every `sys.path` entry as a zip through `importlib.metadata`; found on QA's route,
    D-39). The update worker itself is not exempt: its copy lives under `run\\updater`, and one that had to
    fall back to the venv's own interpreter must not rewrite that venv. A process that cannot be read
    (another user's) is not ours and is skipped."""
    root = os.path.normcase(os.path.abspath(paths.venvs / venv if venv else paths.venvs)) + os.sep
    found: list[Holder] = []
    for proc in psutil.process_iter(["pid", "exe", "cmdline", "create_time"]):
        exe = str(proc.info.get("exe") or "")
        args = [str(a) for a in proc.info.get("cmdline") or []]
        inside = [t for t in (exe, *args) if t and os.path.normcase(os.path.abspath(t)).startswith(root)]
        if not inside:
            continue
        image = Path(exe or (args[0] if args else "a program")).name
        script = Path(inside[0]).name
        try:
            started = datetime.fromtimestamp(float(proc.info.get("create_time") or 0)).strftime("%H:%M:%S")
        except (OSError, ValueError, OverflowError):
            started = "at an unknown time"
        program = image if script.lower() == image.lower() else f"{image} running {script}"
        found.append(Holder(int(proc.info["pid"]), program, started))
    return sorted(found, key=lambda h: h.pid)


def wait_no_holders(
    paths: Paths,
    wait_s: float = HOLDER_WAIT_S,
    *,
    earlier: Iterable[str] = (),
    find: Callable[[], list[Holder]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Return once no program runs from this install's Python environments; `NS-E706`, naming each one,
    when some still do after `wait_s`. Called after the engine was stopped and before anything on disk is
    changed, so the refusal is true as written: nothing was changed (`earlier`: what an interrupted
    rollback's recovery already said in this run)."""
    look = find or (lambda: holders(paths))
    try:
        own = {os.getpid(), *(p.pid for p in psutil.Process().parents())}
    except psutil.Error:
        own = {os.getpid()}
    deadline = clock() + wait_s
    while True:
        found = look()
        if not found:
            return
        if any(h.pid in own for h in found):    # waiting cannot help: it is this process (or its starter)
            raise NsError(
                "NS-E706",
                " ".join([
                    *earlier,
                    "The update's own process runs from Nightshift's Python environments, so it cannot "
                    "replace their files: its private copy of the supervisor environment could not be made "
                    f"({'; '.join(str(h) for h in found if h.pid in own)}). Nothing was changed. Free disk "
                    "space on the drive of the Nightshift folder, close other Nightshift windows and run "
                    "the update again.",
                ]),
                holders=[h.pid for h in found],
            )
        if clock() >= deadline:
            break
        sleep(0.25)
    raise NsError(
        "NS-E706",
        " ".join([
            *earlier,
            "These programs still run from Nightshift's Python environments, and Windows would not let the "
            f"update replace their files: {'; '.join(str(h) for h in found)}. Nothing was changed. Close "
            "them and run the update again.",
        ]),
        holders=[h.pid for h in found],
    )


def installed(paths: Paths) -> dict[str, list[str]]:
    """venv -> the versions of the `nightshift` package installed in it (one, unless a sync broke off)."""
    out: dict[str, list[str]] = {}
    for venv in UPDATE_VENVS:
        folder = paths.venvs / venv
        if not (folder / "Scripts" / "python.exe").exists():
            continue
        out[venv] = []
        for info in sorted((folder / "Lib" / "site-packages").glob("nightshift-*.dist-info")):
            version = info.name[len("nightshift-"):-len(".dist-info")]
            try:
                for line in (info / "METADATA").read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.lower().startswith("version:"):
                        version = line.split(":", 1)[1].strip()
                        break
            except OSError:
                pass
            out[venv].append(version)
    return out


def where_now(paths: Paths, app: Path) -> str:
    """One sentence, read from the disk at this moment: which program is in `app`, and what each Python
    environment has installed. It closes every NS-E702 of an update or rollback and describes a worker
    that died without a word (`update_job.where`), so no message has to claim an outcome from memory."""
    if not app.is_dir():
        return f"In place now: no program folder at {app}."
    here = _shown_version(app)
    have = installed(paths)
    odd: list[str] = []
    for venv, versions in have.items():
        if not versions:
            odd.append(f"the Python environment '{venv}' has no Nightshift package installed")
        elif [deplock.version_key(v) for v in versions] != [deplock.version_key(here)]:
            odd.append(f"the Python environment '{venv}' has Nightshift {' and '.join(versions)} installed")
    if odd:
        return f"In place now: the program of Nightshift {here}, but {'; '.join(odd)}."
    return f"In place now: Nightshift {here}" + (", program and Python environments." if have else ".")


def _sentence(text: str) -> str:
    """`text` ending as a sentence does (a cause quoted in front of what happened next)."""
    text = text.rstrip()
    return text if text.endswith((".", "!", "?", ")")) else text + "."


def _closed(run: Callable[[], UpdateResult], paths: Paths, app: Path) -> UpdateResult:
    """Run an update or rollback; an NS-E702 that does not end with what is in place yet gets `where_now`
    (the one place: whatever raised it, and wherever, the owner reads the state of the disk)."""
    try:
        return run()
    except NsError as e:
        if e.code != "NS-E702" or e.data.get("where"):
            raise
        where = where_now(paths, app.absolute())
        said = f"{_sentence(e.detail)} {where}" if e.detail.strip() else where
        raise NsError(e.code, said, **{**e.data, "where": where}) from (e.__cause__ or e)


def _unfinished(
    lead: str, cause: Exception, *, target: str, other: str, way_out: str, paths: Paths
) -> NsError:
    """The NS-E702 of a re-sync that failed with `target`'s program already in place: which environments
    are left, uv's own last line, that Nightshift was not started on them, and the way out. `venvs_left`
    in its data keeps the worker from starting Nightshift (`update_job.run_worker`)."""
    data = cause.data if isinstance(cause, NsError) else {}
    names = [str(v) for v in data.get("left") or []] or list(installed(paths))
    tail = " ".join(str(data.get("tail") or (cause.detail if isinstance(cause, NsError) else cause)).split())
    using = [str(h) for h in data.get("holders") or []]
    one = len(names) == 1
    which = "the Python environment" if one else "the Python environments"
    quoted = " " + ", ".join(f"'{n}'" for n in names) if names else ""
    closing = (
        f"{which}{quoted} could not be refreshed for {target} ({tail.rstrip('.')}); "
        f"{'it' if one else 'they'} may still hold {other}'s packages. {update_job.NOT_STARTED} To finish "
        f"it: close {', '.join(using) if using else 'every Nightshift window, terminal and Python prompt'}, "
        f"then {way_out}"
    )
    return NsError("NS-E702", f"{lead}{closing}", where=closing, venvs_left=names or ["unknown"])


def takes_detach(program: Path) -> bool:
    """True when the `ns` of the program in this folder knows `ns update --detach` (0.1.14 and later): read
    from its own source, because a way out is typed into that program, not into the one that runs now."""
    try:
        text = (program / "ns_supervisor" / "cli.py").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return '"--detach"' in text


def _setup_from(first: str, second: str) -> str:
    """`run INSTALL.cmd from the Nightshift <first> zip ...`, for when no `ns` command can be used, and the
    `<second>` zip as the next thing to try. Setup's environment step fills the environments again only for
    a version other than the one it last ran for (`S04Venvs.check`: the flag `venvs_version` against the
    zip's version); which one that was is setup's own record (no `ns_*` module reads its flags file), and
    of two different versions at least one is another one."""
    return (
        f"run INSTALL.cmd from the Nightshift {first} zip and choose this folder; if Nightshift still does "
        f"not start after that, do the same with the Nightshift {second} zip (setup fills the Python "
        "environments again only for a version other than the one it last filled them for)"
    )


def rollback_way_out(paths: Paths, app: Path, here: str, other: str) -> str:
    """How to finish when `here`'s program is in `app` on environments that were not refreshed: go to
    `other` and come back, each run refreshing every environment. The first command is typed into `here`'s
    program and the second into `other`'s (`<app>.prev`), so both must have `--detach`."""
    setup = _setup_from(other, here)
    if not (takes_detach(app) and takes_detach(_sibling(app, ".prev"))):
        return f"{setup}."
    return (
        f"run 'ns update --rollback --detach'. {DETACH_RESULT} When it says 'Now on {other}', run the same "
        f"command again: that run comes back to {here} and fills every environment. If 'ns' does not "
        f"start, {setup}."
    )


def update_way_out(paths: Paths, app: Path, here: str, other: str, command: str = "ns update <zip>") -> str:
    """How to fill the environments when `here`'s program is in `app` after an update to or from `other`
    broke off: run the update again, by a route the program in place has."""
    setup = _setup_from(other, here)
    if not takes_detach(app):
        return f"{setup}."
    return (
        f"run the update again with the 'Nightshift - Update' shortcut or '{command} --detach'. "
        f"{DETACH_RESULT} If 'ns' does not start, {setup}."
    )


def rollback_refusal(app: Path) -> str | None:
    """Read-only: why `ns update --rollback` has nothing to go back to; None when it may start (a previous
    version, or a record or folder of an interrupted rollback that it would finish)."""
    app = app.absolute()
    prev = _sibling(app, ".prev")
    if prev.is_dir() or rollback_intent.path_for(app).exists() or rollback_intent.swap_waits(app):
        return None
    return f"no previous version at {prev}. Nothing was changed."


def update_app(zip_path: Path, app: Path, env: UpdateEnv, *, paths: Paths) -> UpdateResult:
    """Apply a release zip to `app`. `NS-E701` before any change; `NS-E706` when a program still runs from
    the Python environments (nothing changed); `NS-E702` after, ending with what is in place."""
    return _closed(lambda: _update_app(zip_path, app, env, paths=paths), paths, app)


def _update_app(zip_path: Path, app: Path, env: UpdateEnv, *, paths: Paths) -> UpdateResult:
    zip_path, app = zip_path.resolve(), app.absolute()
    log = NsLogger("update", paths.logs)
    first_start.settled(paths.config)  # this process makes the settings copy itself
    leave_app(app, paths)
    earlier = _recover_rollback(app, paths, log)  # an interrupted rollback goes first (rollback_intent)
    if not app.is_dir():
        # (a leftover app.swap that could not be put back is named in `earlier`. Going on would delete the
        # program backup to make room and then fail: the last program folder would be gone.)
        why = f"there is no program folder at {app}: the update did not start. {NO_APP_HINT}"
        raise NsError("NS-E702", " ".join([*earlier, why]))
    new_version, prefix = verify_package(zip_path)
    old_version = current_version(app)
    new, prev = _sibling(app, ".new"), _sibling(app, ".prev")
    replaced = current_version(prev) if prev.is_dir() else ""
    log.info("update_start", old=old_version, new=new_version, zip=str(zip_path))
    env.stop()
    update_job.step(paths, WAITING_FOR_PROGRAMS, update_job.unchanged_sentence(old_version))
    wait_no_holders(paths, env.holder_wait_s, earlier=earlier)
    names = {"old": old_version, "new": new_version}
    saved: _UpdateCopy | None = None

    def drop_copy() -> None:  # only a copy this update wrote goes with it; a kept one stays with its program
        if saved is not None and saved.wrote:
            config_snapshot.discard(config_snapshot.path_for(app))

    try:
        shutil.rmtree(new, ignore_errors=True)
        new.mkdir(parents=True)
        safe_extract(zip_path, new, prefix)
        stale = deplock.refusal(new)  # the last refusal that needs the package's files: before the swap
        if stale is not None:
            raise NsError("NS-E124", f"{stale} The update did not start. {NOTHING_CHANGED}")
        notes = list(earlier)
        notes += [f"kept {n}" for n in carry_over(app, new, env.untracked(app))]
        write_app_marker(new, log)
        saved = _save_settings_copy(app, prev, paths, log)  # last step before <app>.prev is replaced
        update_job.step(paths, f"Putting {new_version}'s program in place.", SWAP_STOPPED.format(**names))
        shutil.rmtree(prev, ignore_errors=True)
        rename_with_retry(app, prev)
    except NsError:
        shutil.rmtree(new, ignore_errors=True)
        drop_copy()
        raise
    except OSError as e:
        shutil.rmtree(new, ignore_errors=True)
        drop_copy()  # <app>.prev is gone, so the copy this update made for it belongs to nothing
        raise NsError("NS-E702", f"could not stage the update: {e}. {BUSY_HINT}") from e
    try:
        rename_with_retry(new, app)
    except OSError as e:
        try:
            rename_with_retry(prev, app)
        except OSError as e2:
            raise NsError("NS-E702", f"could not swap directories: {e}; {RESTORE_HINT}") from e2
        drop_copy()
        raise NsError("NS-E702", f"could not swap directories: {e}") from e
    update_job.step(
        paths, "Refreshing the Python environments.",
        SYNC_STOPPED.format(**names) + update_way_out(paths, app, new_version, old_version),
    )
    at = "the refresh of the Python environments"
    try:
        env.sync(app)
        at = "the check of the new version"
        if not env.verify(app):
            raise NsError("NS-E702", "ns verify --quick failed on the new version")
        at = "the re-registration of the nightly task"
        notes += env.reregister(app)
        notes += _voice_notes(paths)
    except Exception as e:  # any step failing after the swap puts the previous version back
        log.error("update_failed", str(e))
        detail = e.detail if isinstance(e, NsError) else f"{type(e).__name__}: {e}"
        update_job.step(paths, f"Putting {old_version} back.", RESTORE_STOPPED.format(**names))
        try:
            _restore(app, prev, log, env.settle)
        except OSError as e3:   # keep the cause of the failure: it is what the owner has to report
            raise NsError("NS-E702", f"{detail}. {RESTORE_HINT} ({e3})") from e
        put_back = ""
        if saved is not None:  # (a copy this update did not write is not its to discard: None keeps it)
            copy_path = config_snapshot.path_for(app) if saved.wrote else None
            put_back = _put_settings_back(paths, saved.files, log, copy_path)
        failed = f"The update to {new_version} failed at {at}: {_sentence(detail)}{put_back} "
        try:
            env.sync(app)
        except Exception as e2:  # the program is back, an environment is not: said, and nothing is started
            log.error("rollback_sync_failed", str(e2))
            raise _unfinished(
                f"{failed}{old_version}'s program is back, but ", e2, target=old_version, other=new_version,
                way_out=update_way_out(paths, app, old_version, new_version), paths=paths,
            ) from e
        back = f"{old_version}'s program is in place again, with its Python environments."
        raise NsError("NS-E702", failed + back, where=back) from e
    had_copy = False
    if saved is not None:
        notes += saved.notes
        had_copy = saved.had_backup_copy
    if replaced:
        notes.append(
            f"the earlier program backup ({replaced})"
            + (" and its settings copy were" if had_copy else " was")
            + f" replaced: ns update --rollback now goes back to {old_version} only"
        )
    log.info("update_done", old=old_version, new=new_version)
    return UpdateResult(old_version, new_version, notes, release_notes(app, new_version))


def rollback_app(app: Path, env: UpdateEnv, *, paths: Paths) -> UpdateResult:
    """`_rollback_app`, with every NS-E702 ending in what is in place (`_closed`)."""
    return _closed(lambda: _rollback_app(app, env, paths=paths), paths, app)


def _rollback_app(app: Path, env: UpdateEnv, *, paths: Paths) -> UpdateResult:
    """Swap `<app>` and `<app>.prev` (so a second rollback rolls forward), put the settings copy back with the
    program (F-10a: the older version gets the files it wrote) and re-sync the venvs.

    What the owner changed in the settings between the update and the rollback is lost, and the result says
    which files it was. Without a usable copy (an update made by an older updater, a damaged copy, one that
    belongs to another program) the settings stay, minus what the older version does not know (F-10c), and
    the result says they were not restored; a copy that reads back complete is then left where it is (it may
    be the only copy of another program's settings), and the result says whose it is and where.

    Crash-safe (F-10c, `ns_common.rollback_intent`): the record `<app>.rollback-pending.json` is written
    first; the settings are made readable by the older program BEFORE it is put in place (it may have no
    rollback code at all); then the programs are swapped; then the copy for going forward is kept and the
    record goes. Interrupted anywhere, the program then in `<app>` finds settings it loads, and the next start
    of a program with this code (or this command run again) finishes or undoes the rest.

    D-39: nothing starts while a program runs from the Python environments (`wait_no_holders`, NS-E706), and
    a re-sync that fails after the swap is said as it is: the older program is in place, which environment
    is left, Nightshift not started, the way to finish (`_unfinished`). It never swaps back: that would need
    a second re-sync against the same held file.
    """
    app = app.absolute()
    first_start.settled(paths.config)  # the rollback manages the copy itself (see first_start.settled)
    leave_app(app, paths)
    log = NsLogger("update", paths.logs)
    prev, tmp = _sibling(app, ".prev"), rollback_intent.swap_for(app)
    nothing = rollback_refusal(app)
    if nothing is not None:
        raise NsError("NS-E702", nothing)
    env.stop()
    update_job.step(paths, WAITING_FOR_PROGRAMS, update_job.unchanged_sentence(current_version(app)))
    wait_no_holders(paths, env.holder_wait_s)
    zip_path = config_snapshot.path_for(app)
    with config_snapshot.locked(zip_path) as held:
        if not held:
            raise NsError(
                "NS-E702",
                f"another Nightshift program is using the settings copy ({zip_path}). Close Nightshift "
                f"windows and try again. {NOTHING_CHANGED}",
            )
        notes: list[str] = []
        no_backup = not os.path.lexists(prev)
        settled = _settle_swap(app, log)  # a folder an interrupted rollback left gets its place first
        if settled:
            notes.append(settled)
        earlier = rollback_intent.recover(app, paths.config)  # a rollback that was interrupted, if any
        _refuse_if_pending(earlier, log, settled)
        if earlier.sentence:
            log.warn("rollback_recovered", earlier.sentence)
            notes.append(earlier.sentence)
        if rollback_intent.swap_waits(app):  # (said by `settled`: held open, or not a program folder)
            # the swap needs that name, and nothing is ever deleted to make room for it
            raise NsError("NS-E702", " ".join([*notes, "ns update --rollback did not start."]))
        if not prev.is_dir():
            raise NsError("NS-E702", " ".join([*notes, f"no previous version at {prev}"]))
        if not app.is_dir():  # (the first rename would fail after the settings were already prepared)
            raise NsError("NS-E702", " ".join([*notes, f"no program folder at {app}. {NO_APP_HINT}"]))
        notes += _left_notes(app, notes)
        if earlier.outcome == "finished" or no_backup:
            # This run completed an EARLIER rollback (its last rename, or its step 4): the program that
            # rollback went to is in place. Swapping now would undo it in the same breath, so this run
            # stops here and says so; the owner decides whether to go back. (The interrupted run never
            # synced the venvs for the program now in place: that is done here, still under the lock.)
            here, back = _shown_version(app), _shown_version(prev)
            twice = rollback_way_out(paths, app, here, back)
            update_job.step(
                paths, "Refreshing the Python environments.",
                ROLLBACK_SYNC_STOPPED.format(old=here, new=back) + twice,
            )
            try:
                env.sync(app)
            except Exception as e:
                log.error("rollback_sync_failed", str(e))
                raise _unfinished(
                    f"An interrupted rollback had put {here}'s program in place, but ", e, target=here,
                    other=back, way_out=twice, paths=paths,
                ) from e
            log.info("rollback_finished_only", now=here, backup=back)
            return UpdateResult(
                here,
                here,
                ["finished an interrupted rollback; no program was swapped", *notes],
                settings=(
                    f"An earlier ns update --rollback was interrupted after it had put {here} in place. "
                    f"It was finished now, and nothing else was done: {here} is the program. Run ns update "
                    f"--rollback again if you want to go to {back}."
                ),
            )
        # (a program whose lock is gone could not get its venvs after the swap: refuse before the record)
        stale = deplock.refusal(prev)
        if stale is not None:
            raise NsError(
                "NS-E702",
                " ".join([*notes, f"the previous version cannot be used: {stale}", ROLLBACK_NOT_STARTED]),
            )
        old_version, restored_version = current_version(app), current_version(prev)
        # One rule for a version that cannot be read (VERSION missing, empty or not a number): it is older
        # than every release, so everything added since BASE_VERSION is taken out. (`current_version` would
        # answer the RUNNING version for a missing or empty file, and nothing would be taken out.)
        readable_as = config_snapshot.program_version(prev)
        if readable_as == "-":
            restored_version = UNKNOWN_VERSION
        left_fingerprint = app_fingerprint(app)  # the program that is about to become <app>.prev
        copy, why_not, slot = _read_settings_copy(app, prev)
        live, live_error = _live_settings(paths, log)
        forward: bytes | None = None  # the settings now: kept for undoing, then the copy for going forward
        live_problem = f"they could not be read: {live_error}" if live is None else ""
        if live is not None:
            try:
                forward = config_snapshot.build(
                    live, version=old_version, fingerprint=left_fingerprint, taken_by="rollback"
                )
            except config_snapshot.SnapshotError as e:
                log.warn("settings_copy_failed", str(e))
                notes.append(f"could not keep a copy of the current settings for going forward again: {e}")
                live_problem = f"they were read, but a copy of them could not be made: {e}"
        intent = rollback_intent.Intent(
            old_version,
            left_fingerprint,
            restored_version,
            app_fingerprint(prev),
            forward,
            from_stamp=config_snapshot.install_stamp(app),
            live_problem=live_problem,
        )
        try:
            rollback_intent.write(app, intent)  # FIRST: nothing has changed yet
        except OSError as e:
            log.error("rollback_record_failed", str(e))
            raise NsError(
                "NS-E702", f"could not record the rollback before starting it ({e}). {SETTINGS_REFUSED}"
            ) from e
        update_job.step(
            paths, f"Preparing your settings files for {restored_version}.", ROLLBACK_PREPARE_STOPPED
        )
        prepared = _prepare_settings(
            paths, copy, why_not, live, live_error,
            slot=slot, zip_path=zip_path, restored_version=restored_version, left_version=old_version,
            left_fingerprint=left_fingerprint, can_go_forward=forward is not None, log=log,
            readable_as=readable_as, prev_name=prev.name,
        )
        intent.stage, intent.after = rollback_intent.PREPARED, prepared.after
        try:
            intent.prepared_digest = rollback_intent.settings_digest(paths.config)
        except config_snapshot.SnapshotError as e:  # (only a sentence of a later undo uses it)
            log.warn("settings_read_failed", str(e))
        write_app_marker(prev, log)  # (it becomes <app>: a later INSTALL.cmd trusts it)
        intent.to_stamp = config_snapshot.install_stamp(prev)  # (the marker just written: this install)
        try:
            rollback_intent.write(app, intent)
        except OSError as e:
            log.error("rollback_record_failed", str(e))
            raise NsError(
                "NS-E702",
                f"could not record the rollback ({e}). {_undo_settings(app, paths, intent, log)}",
            ) from e
        update_job.step(paths, f"Putting {restored_version}'s program in place.", ROLLBACK_SWAP_STOPPED)
        try:
            _swap_programs(app, prev, tmp)
        except OSError as e:
            if app.is_dir() and app_fingerprint(app) == left_fingerprint:  # the folders are as they were
                raise NsError(
                    "NS-E702",
                    f"could not swap directories: {e}. {BUSY_HINT} {_undo_settings(app, paths, intent, log)}",
                ) from e
            raise NsError(
                "NS-E702",
                f"could not swap directories: {e}; {RESTORE_HINT} The record "
                f"{rollback_intent.path_for(app)} says what to do.",
            ) from e
        try:
            finished = rollback_intent.finish(app, paths.config, intent)
        except OSError as e:  # the record stays: the next start of a program with this code deletes it
            log.warn("rollback_record_left", str(e))
            left = rollback_intent.path_for(app)
            finished = rollback_intent.Finished(problems=[f"could not delete {left}"])
        notes += prepared.notes
        notes += _after_swap_notes(
            prepared,
            finished,
            zip_path=zip_path,
            restored_version=restored_version,
            left_version=old_version,
            log=log,
        )
    # (the result line: a program whose VERSION is missing or empty is not named after the running code)
    now = _shown_version(app)
    twice = rollback_way_out(paths, app, now, old_version)
    update_job.step(
        paths, "Refreshing the Python environments.",
        ROLLBACK_SYNC_STOPPED.format(old=now, new=old_version) + twice,
    )
    try:
        env.sync(app)
    except Exception as e:  # the older program and its settings are in place: finish forward, never swap back
        log.error("rollback_sync_failed", str(e))
        raise _unfinished(
            f"The rollback to {now} is not finished. {prepared.sentence} {now}'s program is in place, but ",
            e, target=now, other=old_version, way_out=twice, paths=paths,
        ) from e
    log.info("rollback_done", old=old_version, restored=now)
    return UpdateResult(old_version, now, ["rolled back", *notes], settings=prepared.sentence)


def _undo_settings(app: Path, paths: Paths, intent: rollback_intent.Intent, log: NsLogger) -> str:
    """The rollback stops before the older program is in place: the settings go back to the ones in the
    record. Returns the sentence for the error."""
    try:
        said = rollback_intent.undo(app, paths.config, intent)
    except (config_snapshot.SnapshotError, OSError) as e:
        log.error("settings_restore_failed", str(e))
        return (
            f"Your settings files could not be put back as they were ({e}); the next start of Nightshift "
            "tries again."
        )
    log.info("rollback_undone", said)
    if intent.live is None:
        return (
            "The program was not changed. Your settings files were not saved before the rollback "
            f"({intent.live_problem}), so they stay as they are now."
        )
    return "Nothing else was changed: the program and your settings files are as they were."


def free_entry_points(venv: Path) -> list[tuple[Path, Path]]:
    """Move `ns.exe` and `studio.exe` of `venv` out of the way so uv can write fresh ones.

    Windows refuses to delete or overwrite a running program, but it does allow renaming one. A terminal that
    is following `ns update` (or any Studio window) still runs the old `Scripts\\ns.exe`; without this, uv
    fails with "Access is denied" while replacing it. Returns (original, moved-to) pairs.
    """
    scripts = venv / "Scripts"
    moved: list[tuple[Path, Path]] = []
    for name in ENTRY_EXES:
        exe = scripts / name
        if not exe.is_file():
            continue
        away = scripts / (name + OLD_SUFFIX)
        try:
            away.unlink(missing_ok=True)          # left by the last update; fails while that one still runs
        except OSError:
            away = scripts / f"{name}{OLD_SUFFIX}-{os.getpid()}"
        try:
            os.replace(exe, away)
        except OSError:
            continue                              # not renamable either: uv will report it
        moved.append((exe, away))
    return moved


def restore_entry_points(moved: list[tuple[Path, Path]]) -> None:
    """Put moved launchers back where uv did not write a new one (the install failed half way)."""
    for exe, away in moved:
        if not exe.exists() and away.exists():
            try:
                os.replace(away, exe)
            except OSError:
                pass


def discard_entry_points(moved: list[tuple[Path, Path]]) -> None:
    """Delete the moved launchers; one that is still running stays until the next update."""
    for _exe, away in moved:
        try:
            away.unlink(missing_ok=True)
        except OSError:
            pass


def finish_entry_points(moved: list[tuple[Path, Path]]) -> None:
    """After a successful install: a launcher uv did not write again (nothing to reinstall) comes back, the
    others lose their moved-aside copy."""
    restore_entry_points(moved)
    discard_entry_points(moved)


def resync_venvs(
    app: Path,
    paths: Paths,
    *,
    uv: str | None = None,
    run: Callable[[list[str]], tuple[int, str]] | None = None,
    free: Callable[[Path], list[tuple[Path, Path]]] | None = None,
    tries: int = SYNC_TRIES,
    pause_s: float = SYNC_PAUSE_S,
    sleep: Callable[[float], None] = time.sleep,
    say: Callable[[str], None] | None = None,
) -> list[str]:
    """Fill every venv that exists with the packages of the program in `app`; `NS-E702` on failure.

    A venv whose install fails on a file in use (uv's text says so, or a program runs from that venv:
    `holders`) is tried `tries` times, `pause_s` apart, each wait told through `say` with the programs
    named; any other failure is not retried. The NS-E702 carries `left` (that venv and the ones after it),
    `done`, `tail` (uv's last lines) and `holders` in its data, for the caller's sentence.

    By the rule of the program in `app` (`deplock.follows_lock`), which need not be this updater's own:

    - it has a lock (every release from the first locked one): each venv's extras are exported from `uv.lock`
      with hashes (`logs/pins-<venv>.txt`), installed with `--require-hashes`, and the program is added with
      `--no-deps`. The lock is checked and every venv's set is exported BEFORE the first venv is touched, so
      a missing or stale lock changes no environment;
    - it is older (0.1.14, the 0.1.15 candidates up to rc3): `uv pip install -e <app>[extras]`, as that
      release installed itself.

    The `tts` venv is outside the lock in both cases (its packages are the manifest's pins): only the program
    is installed again, `--no-deps`. A failure in one venv stops there: venvs filled before it stay filled,
    and running the command again fills every venv again (uv skips what is already in place).
    """
    runner = run or (lambda cmd: _run(cmd, uv_env(paths.home)))
    free_up = free or free_entry_points
    exe = uv if uv is not None else find_uv(paths)
    dev = is_dev(paths)         # a dev install re-syncs the dev extras (no speech or GPU wheels)
    venvs = [v for v in UPDATE_VENVS if (paths.venvs / v / "Scripts" / "python.exe").exists()]
    if not venvs:
        return []
    if exe is None:
        raise NsError(
            "NS-E702", f"uv was not found at {home_uv(paths.home)} or on PATH; cannot re-sync the venvs"
        )
    locked = deplock.follows_lock(app)
    pins: dict[str, Path] = {}
    if locked:
        stale = deplock.problem(app)
        if stale is not None:
            raise NsError("NS-E702", f"{stale} {deplock.NO_VENV_CHANGED}")
        table = DEV_VENV_EXTRAS if dev else VENV_EXTRAS
        paths.logs.mkdir(parents=True, exist_ok=True)
        for venv in venvs:
            if venv in NO_DEPS or venv not in table:
                continue
            out = deplock.pins_file(paths.logs, venv)
            rc, tail = runner([exe, *deplock.export_args(app, table[venv], out)])
            if rc != 0:
                raise NsError(
                    "NS-E702",
                    f"uv could not read the locked packages of venv {venv} from {app / deplock.LOCK_NAME}: "
                    f"{tail}. {deplock.NO_VENV_CHANGED}",
                )
            pins[venv] = out
    done: list[str] = []
    for venv in venvs:
        py = paths.venvs / venv / "Scripts" / "python.exe"
        if venv in pins:
            cmds = [[exe, *deplock.install_args(py, pins[venv])], [exe, *deplock.app_args(py, app)]]
        elif locked:                # (tts, or a dev install's venv without a row: the program only)
            cmds = [[exe, *deplock.app_args(py, app)]]
        else:
            cmd = [exe, "pip", "install", "-e", f"{app}{extras_suffix(venv, dev=dev)}", "--python", str(py)]
            cmds = [[*cmd, "--no-deps"] if venv in NO_DEPS else cmd]
        for attempt in range(1, max(1, tries) + 1):
            moved = free_up(paths.venvs / venv)
            failed: str | None = None
            for cmd in cmds:
                rc, tail = runner(cmd)
                if rc != 0:
                    restore_entry_points(moved)
                    failed = tail
                    break
            if failed is None:
                finish_entry_points(moved)
                break
            using = [str(h) for h in holders(paths, venv)]
            in_use = bool(using) or any(sign in failed.lower() for sign in HELD_SIGNS)
            if attempt >= tries or not in_use:
                raise NsError(
                    "NS-E702", f"uv pip install failed for venv {venv}: {failed}",
                    left=venvs[venvs.index(venv):], done=list(done), tail=failed, holders=using,
                )
            who = "; ".join(using) or "a file in it is held open"
            line = (
                f"The Python environment '{venv}' is in use ({who}): trying again in {pause_s:g} s "
                f"(try {attempt} of {tries})."
            )
            NsLogger("update", paths.logs).warn("sync_retry", line, venv=venv, attempt=attempt)
            if say is not None:
                say(line)
            sleep(pause_s)
        done.append(venv)
    return done


def _run(cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=1800, check=False, env=child_env(env),
            creationflags=window_flags(),
        )
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)
    return r.returncode, " | ".join((r.stderr or r.stdout).strip().splitlines()[-3:])


def verify_quick(app: Path, python: str | None = None) -> bool:
    """`ns verify --quick` with the new code (a fresh interpreter in the app directory).

    `python` is the supervisor venv's interpreter, which the re-sync just brought up to date. The default,
    this process's own interpreter, is wrong in the update worker: that runs from a copy of the venv made
    before the re-sync and would not see a dependency the new version added.
    """
    r = subprocess.run(
        [python or sys.executable, "-m", "ns_supervisor.cli", "verify", "--quick"],
        cwd=str(app),
        check=False,
        env=child_env(),
        creationflags=window_flags(),   # a terminal: 0, its lines are seen; the update worker: no window
    )
    return r.returncode == 0


# --------------------------------------------------------------------------------------------------------
# a cloned-repo install: `ns update --git [--channel stable|main]`
# --------------------------------------------------------------------------------------------------------
CHANNELS = ("stable", "main")
GitRun = Callable[[list[str]], tuple[int, str]]


def _git_run(app: Path) -> GitRun:
    exe = shutil.which("git")

    def run(args: list[str]) -> tuple[int, str]:
        if exe is None:
            return 127, "git was not found on PATH"
        try:
            r = subprocess.run(
                [exe, "-C", str(app), *args], capture_output=True, text=True, timeout=900, check=False,
                env=child_env(), creationflags=window_flags(),
            )
        except (OSError, subprocess.SubprocessError) as e:
            return 1, str(e)
        return r.returncode, (r.stdout.strip() or r.stderr.strip())

    return run


def git_update(
    app: Path,
    env: UpdateEnv,
    *,
    paths: Paths,
    channel: str = "stable",
    latest_tag: Callable[[], str] | None = None,
    git: GitRun | None = None,
) -> UpdateResult:
    """`_git_update`, with every NS-E702 ending in what is in place (`_closed`)."""
    return _closed(
        lambda: _git_update(app, env, paths=paths, channel=channel, latest_tag=latest_tag, git=git),
        paths,
        app,
    )


def _git_update(
    app: Path,
    env: UpdateEnv,
    *,
    paths: Paths,
    channel: str = "stable",
    latest_tag: Callable[[], str] | None = None,
    git: GitRun | None = None,
) -> UpdateResult:
    """Update a cloned-repo install: fetch, move to the newest release tag (`stable`) or `origin/main`
    (`main`), re-sync the venvs, verify, and go back to the previous commit if any step fails (`NS-E702`).

    Refuses when the working tree has local changes to tracked files (nothing is touched then).
    """
    if channel not in CHANNELS:
        raise NsError("NS-E702", f"unknown channel {channel!r}; choose stable or main")
    app = app.absolute()
    if not (app / ".git").exists():
        raise NsError("NS-E702", f"{app} is not a git clone; use --github or an update zip instead")
    if git is None and shutil.which("git") is None:
        raise NsError("NS-E702", NO_GIT_HINT)
    run = git or _git_run(app)
    log = NsLogger("update", paths.logs)
    first_start.settled(paths.config)  # a failed git update puts the settings back itself
    old_version = current_version(app)
    code, head = run(["rev-parse", "HEAD"])
    if code != 0:
        raise NsError("NS-E702", f"git rev-parse failed: {head}")
    code, dirty = run(["status", "--porcelain", "--untracked-files=no"])
    if code != 0 or dirty:
        raise NsError("NS-E702", "this copy has local changes to tracked files; commit or discard them first")
    code, out = run(["fetch", "--tags", "--prune", "origin"])
    if code != 0:
        raise NsError("NS-E105", f"git fetch failed: {out}")
    if channel == "stable":
        tag = (latest_tag or (lambda: _latest_release_tag()))()
        target = ["checkout", "--detach", tag]
    else:
        target = ["checkout", "--detach", "origin/main"]
    leave_app(app, paths)
    log.info("git_update_start", channel=channel, old=old_version, head=head[:12])
    env.stop()
    wait_no_holders(paths, env.holder_wait_s)
    try:        # a failed update puts these back (a git install has no program backup to pair a copy with)
        before = config_snapshot.capture(paths.config)
    except config_snapshot.SnapshotError as e:
        log.error("settings_copy_failed", str(e))
        raise NsError(
            "NS-E702", f"could not read your settings files before the update ({e}). {SETTINGS_REFUSED}"
        ) from e
    code, out = run(target)
    if code != 0:
        raise NsError("NS-E702", f"git {' '.join(target)} failed: {out}")
    update_job.step(
        paths, "Refreshing the Python environments.",
        "The update stopped after the newer program was checked out, while it was refreshing or checking "
        "the Python environments. To fill every environment, run 'ns update --git --detach'. "
        + DETACH_RESULT,
    )
    at = "the refresh of the Python environments"
    try:
        env.sync(app)
        at = "the check of the new version"
        if not env.verify(app):
            raise NsError("NS-E702", "ns verify --quick failed on the new version")
        at = "the re-registration of the nightly task"
        notes = env.reregister(app) + _voice_notes(paths)
    except Exception as e:  # any step failing after the move goes back to the previous commit
        log.error("git_update_failed", str(e))
        run(["checkout", "--detach", head])
        put_back = _put_settings_back(paths, before, log, None)
        detail = e.detail if isinstance(e, NsError) else f"{type(e).__name__}: {e}"
        failed = f"The update failed at {at}: {_sentence(detail)}{put_back} "
        try:
            env.sync(app)
        except Exception as e2:  # the commit is back, an environment is not: said, and nothing is started
            log.error("rollback_sync_failed", str(e2))
            raise _unfinished(
                f"{failed}The previous commit ({old_version}) is checked out again, but ", e2,
                target=old_version, other="the newer version",
                way_out=f"run 'ns update --git --detach' again. {DETACH_RESULT}", paths=paths,
            ) from e
        back = f"The previous commit ({old_version}) is checked out again, with its Python environments."
        raise NsError("NS-E702", failed + back, where=back) from e
    new_version = current_version(app)
    log.info("git_update_done", old=old_version, new=new_version)
    return UpdateResult(
        old_version,
        new_version,
        [f"channel {channel}", *notes, GIT_NO_COPY_NOTE],
        release_notes(app, new_version),
    )


def _latest_release_tag() -> str:
    from ns_supervisor import releases

    return releases.fetch_latest().tag
