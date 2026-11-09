"""C2 `ns update <zip> | --rollback` (spec J8, ENGINEERING 10): verified, swappable, reversible.

Order: verify VERSION + `.sha256` sidecar (`NS-E701`, nothing touched) -> stop the engine -> extract to
`<app>.new` -> carry over `.git`, `config`, `answers.json` and untracked data -> copy the settings files
(`config_snapshot`, F-10a; `NS-E702` and nothing changed when that cannot be done) -> swap with
`<app>.prev` -> `uv pip install -e` per venv -> `ns verify --quick` -> re-register the scheduled task. Any
failure after the swap restores `<app>.prev` and the settings files, and raises `NS-E702`.

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
from pathlib import Path, PurePosixPath

import yaml

from ns_common import __version__, config_compat, config_snapshot, first_start, rollback_intent
from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.devmode import is_dev
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.models import Channel, InstallState, Runtime
from ns_common.paths import Paths
from ns_common.uv import find_uv, home_uv, uv_env
from ns_common.venv_extras import NO_DEPS, extras_suffix
from ns_supervisor.childenv import child_env

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
SETTINGS_REFUSED = (
    f"{NOTHING_CHANGED} Close programs that use the "
    "Nightshift folder, make room on the disk if it is full, and run the update again."
)
GIT_NO_COPY_NOTE = "no settings copy kept for ns update --rollback (a git install has no program backup)"
SETTINGS_BACKUP_HINT = (
    "If Nightshift does not start, restore a backup of your settings: ns restore <backup.zip> --all."
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


def _for_older_sentence(version: str, dropped: list[str], failed: list[str], *, restored: bool) -> str:
    """What the rollback did to make the files readable by `version` (empty when it had nothing to do)."""
    parts: list[str] = []
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
    if (dropped or not restored) and config_compat.version_key(version) < config_compat.version_key(
        config_compat.BASE_VERSION
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
    saving them under its name would make the next rollback "restore" a newer version's files."""
    notes: list[str] = []
    after = rollback_intent.AfterSwap()
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
        dropped, failed = _readable_by(paths.config, restored_version, log)
        no_copy = copy is None and slot.problem is not None and slot.problem.missing
        no = _not_restored_sentence(why, restored_version, dropped, failed, no_copy=no_copy)
        older = _for_older_sentence(restored_version, dropped, failed, restored=False)
        hint = "" if no_copy and not dropped and not failed else SETTINGS_BACKUP_HINT
        sentence = " ".join(p for p in (no, older, hint) if p)
        return _Prepared(sentence, notes, after, damaged_slot=copy is None and slot.damaged)
    try:
        done = config_snapshot.restore(paths.config, copy.files, live=live, remove_extra=False)
    except config_snapshot.SnapshotError as e:
        log.error("settings_restore_failed", str(e))
        after.mark_unrestored = True
        dropped, failed = _readable_by(paths.config, restored_version, log)
        no = _not_restored_sentence(str(e), restored_version, dropped, failed, no_copy=False)
        left = (
            f"The copy made for {restored_version} is still in {zip_path} "
            f"(a zip: the files in its config folder are what {restored_version} wrote)."
        )
        older = _for_older_sentence(restored_version, dropped, failed, restored=False)
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
    dropped, failed = _readable_by(paths.config, restored_version, log)
    older = _for_older_sentence(restored_version, dropped, failed, restored=True)
    if older:
        sentence += " " + older
    if "schedule.json" in done.changed:
        notes.append(
            "the nightly run time in schedule.json is back to the old value, but the Windows task keeps "
            "the time it has now: set the time again in Settings if you changed it after the update"
        )
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
    next start understands (`_settle_folders`) and the interruption goes on."""
    shutil.rmtree(tmp, ignore_errors=True)
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


def _recover_rollback(app: Path, paths: Paths, log: NsLogger) -> str:
    """Finish or undo a rollback that was interrupted (`rollback_intent.recover`, under the copy's lock)
    before an update looks at the settings. Returns what was done (empty: nothing)."""
    if not rollback_intent.path_for(app).exists():
        return ""
    with config_snapshot.locked(config_snapshot.path_for(app)) as held:
        if not held:
            raise NsError(
                "NS-E702",
                f"another Nightshift program is using the settings copy. Close Nightshift windows and try "
                f"again. {NOTHING_CHANGED}",
            )
        said = rollback_intent.recover(app, paths.config)
    if said:
        log.warn("rollback_recovered", said)
    return said


def update_app(zip_path: Path, app: Path, env: UpdateEnv, *, paths: Paths) -> UpdateResult:
    """Apply a release zip to `app`. `NS-E701` before any change, `NS-E702` (rolled back) after."""
    zip_path, app = zip_path.resolve(), app.absolute()
    log = NsLogger("update", paths.logs)
    first_start.settled(paths.config)  # this process makes the settings copy itself
    leave_app(app, paths)
    earlier = _recover_rollback(app, paths, log)  # an interrupted rollback goes first (rollback_intent)
    new_version, prefix = verify_package(zip_path)
    old_version = current_version(app)
    new, prev = _sibling(app, ".new"), _sibling(app, ".prev")
    replaced = current_version(prev) if prev.is_dir() else ""
    log.info("update_start", old=old_version, new=new_version, zip=str(zip_path))
    env.stop()
    saved: _UpdateCopy | None = None

    def drop_copy() -> None:  # only a copy this update wrote goes with it; a kept one stays with its program
        if saved is not None and saved.wrote:
            config_snapshot.discard(config_snapshot.path_for(app))

    try:
        shutil.rmtree(new, ignore_errors=True)
        new.mkdir(parents=True)
        safe_extract(zip_path, new, prefix)
        notes = [earlier] if earlier else []
        notes += [f"kept {n}" for n in carry_over(app, new, env.untracked(app))]
        write_app_marker(new, log)
        saved = _save_settings_copy(app, prev, paths, log)  # last step before <app>.prev is replaced
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
    try:
        env.sync(app)
        if not env.verify(app):
            raise NsError("NS-E702", "ns verify --quick failed on the new version")
        notes += env.reregister(app)
    except Exception as e:  # any step failing after the swap rolls back
        log.error("update_failed", str(e))
        detail = e.detail if isinstance(e, NsError) else f"{type(e).__name__}: {e}"
        try:
            _restore(app, prev, log, env.settle)
        except OSError as e3:   # keep the cause of the failure: it is what the owner has to report
            raise NsError("NS-E702", f"{detail}. {RESTORE_HINT} ({e3})") from e
        put_back = ""
        if saved is not None:  # (a copy this update did not write is not its to discard: None keeps it)
            copy_path = config_snapshot.path_for(app) if saved.wrote else None
            put_back = _put_settings_back(paths, saved.files, log, copy_path)
        try:
            env.sync(app)
        except Exception as e2:  # best effort: report, the old tree is already back
            log.error("rollback_sync_failed", str(e2))
        raise NsError("NS-E702", detail.rstrip(".") + "." + put_back if put_back else detail) from e
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
    """
    app = app.absolute()
    first_start.settled(paths.config)  # the rollback manages the copy itself (see first_start.settled)
    leave_app(app, paths)
    log = NsLogger("update", paths.logs)
    prev = _sibling(app, ".prev")
    if not prev.is_dir() and not rollback_intent.path_for(app).exists():
        raise NsError("NS-E702", f"no previous version at {prev}")
    env.stop()
    zip_path = config_snapshot.path_for(app)
    with config_snapshot.locked(zip_path) as held:
        if not held:
            raise NsError(
                "NS-E702",
                f"another Nightshift program is using the settings copy ({zip_path}). Close Nightshift "
                f"windows and try again. {NOTHING_CHANGED}",
            )
        notes: list[str] = []
        earlier = rollback_intent.recover(app, paths.config)  # a rollback that was interrupted, if any
        if earlier:
            log.warn("rollback_recovered", earlier)
            notes.append(earlier)
        if not prev.is_dir():
            raise NsError("NS-E702", f"no previous version at {prev}")
        old_version, restored_version = current_version(app), current_version(prev)
        left_fingerprint = app_fingerprint(app)  # the program that is about to become <app>.prev
        copy, why_not, slot = _read_settings_copy(app, prev)
        live, live_error = _live_settings(paths, log)
        forward: bytes | None = None  # the settings now: kept for undoing, then the copy for going forward
        if live is not None:
            try:
                forward = config_snapshot.build(
                    live, version=old_version, fingerprint=left_fingerprint, taken_by="rollback"
                )
            except config_snapshot.SnapshotError as e:
                log.warn("settings_copy_failed", str(e))
                notes.append(f"could not keep a copy of the current settings for going forward again: {e}")
        intent = rollback_intent.Intent(
            old_version, left_fingerprint, restored_version, app_fingerprint(prev), forward
        )
        try:
            rollback_intent.write(app, intent)  # FIRST: nothing has changed yet
        except OSError as e:
            log.error("rollback_record_failed", str(e))
            raise NsError(
                "NS-E702", f"could not record the rollback before starting it ({e}). {SETTINGS_REFUSED}"
            ) from e
        prepared = _prepare_settings(
            paths, copy, why_not, live, live_error,
            slot=slot, zip_path=zip_path, restored_version=restored_version, left_version=old_version,
            left_fingerprint=left_fingerprint, can_go_forward=forward is not None, log=log,
        )
        intent.stage, intent.after = rollback_intent.PREPARED, prepared.after
        try:
            intent.prepared_digest = rollback_intent.settings_digest(paths.config)
        except config_snapshot.SnapshotError as e:  # (then an interruption leaves these settings as they are)
            log.warn("settings_read_failed", str(e))
        try:
            rollback_intent.write(app, intent)
        except OSError as e:
            log.error("rollback_record_failed", str(e))
            raise NsError(
                "NS-E702",
                f"could not record the rollback ({e}). {_undo_settings(app, paths, intent, log)}",
            ) from e
        write_app_marker(prev, log)  # (it becomes <app>: a later INSTALL.cmd trusts it)
        tmp = _sibling(app, ".swap")
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
    env.sync(app)
    log.info("rollback_done", old=old_version, restored=current_version(app))
    now = current_version(app)
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
            "The program was not changed. Your settings files could not be read before the rollback, so they "
            "stay as they are now."
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
) -> list[str]:
    """`uv pip install -e <app>[extras]` in every venv that exists; raises `NS-E702` on failure."""
    runner = run or (lambda cmd: _run(cmd, uv_env(paths.home)))
    free_up = free or free_entry_points
    exe = uv if uv is not None else find_uv(paths)
    done: list[str] = []
    dev = is_dev(paths)         # a dev install re-syncs the dev extras (no speech or GPU wheels)
    for venv in UPDATE_VENVS:
        extras, no_deps = extras_suffix(venv, dev=dev), venv in NO_DEPS
        py = paths.venvs / venv / "Scripts" / "python.exe"
        if not py.exists():
            continue
        if exe is None:
            raise NsError(
                "NS-E702", f"uv was not found at {home_uv(paths.home)} or on PATH; cannot re-sync the venvs"
            )
        cmd = [exe, "pip", "install", "-e", f"{app}{extras}", "--python", str(py)]
        if no_deps:
            cmd.append("--no-deps")
        moved = free_up(paths.venvs / venv)
        rc, tail = runner(cmd)
        if rc != 0:
            restore_entry_points(moved)
            raise NsError("NS-E702", f"uv pip install failed for venv {venv}: {tail}")
        finish_entry_points(moved)
        done.append(venv)
    return done


def _run(cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=1800, check=False, env=child_env(env)
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
                env=child_env(),
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
    try:
        env.sync(app)
        if not env.verify(app):
            raise NsError("NS-E702", "ns verify --quick failed on the new version")
        notes = env.reregister(app)
    except Exception as e:  # any step failing after the move goes back to the previous commit
        log.error("git_update_failed", str(e))
        run(["checkout", "--detach", head])
        put_back = _put_settings_back(paths, before, log, None)
        try:
            env.sync(app)
        except Exception as e2:  # best effort: the old tree is back
            log.error("rollback_sync_failed", str(e2))
        detail = e.detail if isinstance(e, NsError) else f"{type(e).__name__}: {e}"
        raise NsError("NS-E702", detail.rstrip(".") + "." + put_back) from e
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
