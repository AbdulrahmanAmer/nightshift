"""The program folder: copy the extracted bundle into ``<home>\\app``, or update the install already there.

Standard library only (the setup server runs on the bare Python). Nightshift must not depend on the place the
zip was extracted to: every venv is an editable install of the program folder, and the shortcuts, the nightly
task, the launchers and ``ns update`` all point at it. Setup therefore copies the extracted folder to
``<home>\\app`` and records that path as ``app_path``; the extracted folder can be deleted afterwards.

* Fresh install: the tree is copied to ``<home>\\app.new`` and renamed to ``<home>\\app`` once it is complete
  (the marker file is written last), so a crash never leaves a half program folder that looks finished.
* Re-run from a newer zip: the same swap ``ns update`` does. The old folder becomes ``<home>\\app.prev`` (the
  rollback target of ``ns update --rollback``), and ``.git``, ``config`` and ``answers.json`` of the old tree
  are carried into the new one. Before that, the settings files of ``<home>\\config`` are copied to
  ``<home>\\app.prev-config.zip`` (``ns_common.config_snapshot``, standard library only) so the
  rollback can put them back with the program; when that copy cannot be made nothing is swapped
  (``NS-E115``). A rollback that was interrupted (``ns_common.rollback_intent``) is finished first when its
  older program is in place, and the program is not replaced while the newer one waits for its undo.
* A folder ``<home>\\app.swap`` left by an interrupted rollback is given its place first
  (``_settle_leftover_swap``): with no ``<home>\\app`` it IS the program, and this setup run is the only code
  that can put it back (nothing else of Nightshift starts without ``app``). While it cannot be put back
  nothing is copied (``NS-E115``, with the way out).
* Run from the installed program folder itself (INSTALL.cmd started in ``<home>\\app``, a repair): nothing
  is copied, swapped or saved, and the record of an interrupted rollback is not looked at here. That run is
  a start of the program in place: the window's first settings write (``answers_view.write_answers`` and
  every other writer call ``first_start.guard``) finishes or undoes the rollback like any other start.
* Never copied: ``venvs`` and ``models`` (somebody extracted into the home), every ``bin`` folder except the
  bundle's ``bin\\webview2``, caches and ``node_modules``, and ``answers.json`` (the owner's input next to
  ``INSTALL.cmd``; it may hold API keys that setup moves to the keyring, so it is not duplicated).
"""

from __future__ import annotations

import hashlib
import ntpath
import os
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import ns_common.config_snapshot as config_snapshot
import ns_common.rollback_intent as rollback_intent
from ns_common.avblock import is_blocked_error

APP_DIR = "app"
MARKER = ".ns-app.done"
CARRY_OVER = (".git", "config", "answers.json")
# answers.json is the owner's input next to INSTALL.cmd and may hold API keys: it is not duplicated
# ``files`` is the folder of pinned programs beside INSTALL.cmd (D-17): setup reads it where it is
SKIP_TOP = frozenset({"venvs", "models", "answers.json", "files"})
KEEP_IN_BIN = frozenset({"webview2"})
SKIP_ANYWHERE = frozenset(
    {"__pycache__", "node_modules", ".venv", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
)
# A zip extracted straight into a drive root has the whole drive as its "folder": then only the bundle's own
# top-level entries are program files (a test keeps this list covering the repository's tracked entries).
ROOT_ENTRIES = frozenset(
    {
        "build_spec.md", "build_spec_v1.md", "changelog.md", "claude.md", "demo.cmd", "install.cmd",
        "operator.md", "readme.md", "uninstall.cmd", "version", "contracts", "dev", "docs", "installer",
        "launchers", "manifest.json", "ns_common", "ns_dashboard", "ns_studio", "ns_supervisor", "ns_tts",
        "pyproject.toml", "uv.lock", "tools", "tests", "bin", "answers.example.json", ".editorconfig",
        ".gitattributes", ".gitignore", "proofs",
    }
)
RENAME_TRIES = 20
RENAME_PAUSE_S = 1.0


class AppCopyError(Exception):
    """The program folder could not be put in place; ``code`` is an errors.json code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass
class CopyResult:
    """What ``install_app`` did."""

    action: str                                   # copied | updated | in-place
    old_version: str | None = None
    new_version: str | None = None
    kept: list[str] = field(default_factory=list)


def read_version(folder: Path) -> str | None:
    """The ``VERSION`` file of a program folder (None when it has none)."""
    try:
        return (folder / "VERSION").read_text(encoding="utf-8-sig").strip() or None
    except OSError:
        return None


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except OSError:
        return "-"


def fingerprint(folder: Path) -> str:
    """Version plus short hashes of ``manifest.json`` and ``pyproject.toml``: what a venv refresh needs."""
    manifest, project = _digest(folder / "manifest.json"), _digest(folder / "pyproject.toml")
    return f"{read_version(folder) or '-'}|{manifest}|{project}"


def app_dir(home: Path) -> Path:
    """``<home>\\app``."""
    return Path(home) / APP_DIR


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def same_folder(a: Path, b: Path) -> bool:
    """True when both paths name the same folder (Windows spelling rules: case and slashes do not matter)."""
    try:
        if a.exists() and b.exists():
            return a.samefile(b)
    except OSError:
        pass
    return _norm(a) == _norm(b)


def is_drive_root(path: Path | str) -> bool:
    """True for a drive root such as D: (with or without the backslash): it holds the whole drive."""
    drive, rest = ntpath.splitdrive(str(path).strip())
    return bool(drive) and rest in ("", "\\", "/")


def is_inside(child: Path, parent: Path) -> bool:
    """True when ``child`` is ``parent`` or lies below it (case-insensitive, no symlink resolution)."""
    c, p = _norm(child).split(os.sep), _norm(parent).split(os.sep)
    return len(c) >= len(p) and c[: len(p)] == p


def installed_fingerprint(home: Path) -> str | None:
    """The fingerprint recorded when ``<home>\\app`` was completed (None: no complete copy there)."""
    try:
        return (app_dir(home) / MARKER).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def installed_version(home: Path) -> str | None:
    """Version of the program folder installed under ``home`` (None when there is no complete copy)."""
    return read_version(app_dir(home)) if installed_fingerprint(home) else None


def is_current(source: Path, home: Path) -> bool:
    """True when ``<home>\\app`` is a complete copy of ``source`` (or ``source`` is that folder)."""
    if same_folder(source, app_dir(home)):
        return True
    return installed_fingerprint(home) == fingerprint(source)


def _ignore(source: Path) -> Callable[[str, list[str]], set[str]]:
    root = _norm(source)
    root_is_drive = is_drive_root(source)

    def ignore(directory: str, names: list[str]) -> set[str]:
        here = _norm(directory)
        skipped = {n for n in names if n in SKIP_ANYWHERE}
        if here == root:
            if root_is_drive:
                skipped |= {n for n in names if n.lower() not in ROOT_ENTRIES}
            skipped |= {n for n in names if n.lower() in SKIP_TOP}
        elif os.path.basename(here) == "bin" and os.path.dirname(here) == root:
            skipped |= {n for n in names if n.lower() not in KEEP_IN_BIN}
        return skipped

    return ignore


def copy_tree(source: Path, target: Path) -> None:
    """Copy the program folder to ``target`` (which must not exist), leaving out what ``SKIP_*`` lists."""
    shutil.copytree(source, target, ignore=_ignore(source))


def _rename(src: Path, dst: Path, sleep: Callable[[float], None]) -> None:
    for attempt in range(1, RENAME_TRIES + 1):
        try:
            src.rename(dst)
            return
        except OSError:
            if attempt >= RENAME_TRIES:
                raise
            sleep(RENAME_PAUSE_S)


def _carry(old: Path, new: Path) -> list[str]:
    kept: list[str] = []
    for name in CARRY_OVER:
        src, dst = old / name, new / name
        if not src.exists() or dst.exists():
            continue
        if src.is_dir():
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        kept.append(name)
    return kept


def _save_settings_copy(
    home: Path,
    dest: Path,
    old_version: str,
    before: Mapping[str, bytes] | None = None,
    say: Callable[[str], None] = lambda _t: None,
) -> bool:
    """Copy the settings files beside the program backup before the old program becomes ``app.prev``.

    ``before`` is what the setup window read of ``config_snapshot.SETUP_TOUCHES`` before it rewrote them on
    opening (it records the new version in ``install_state.json`` at once); those files go into the copy as
    they were then, every other file as it is now. ``AppCopyError`` (``NS-E115``) when the copy cannot be
    made: the swap is not started, so the program is not replaced (the setup window may already have
    rewritten ``SETUP_TOUCHES`` files; a retry in that window reuses what it read first). A copy that is
    already there (the one for the program in ``app.prev``) stays whole.

    The copy there is written over only together with its program (the one in ``app.prev``), when it is
    damaged, or when it is the old program's own and no rollback recorded that its restore failed. One a
    rollback could not put back stays as the old program's copy (returns False, nothing was written: the
    settings files now may be a newer version's). A whole copy of any other program is never written over: it
    is left where it is and said so, and no copy is kept for this program backup (returns False). Only a zip
    that cannot be read raises ``NS-E115`` (``config_snapshot.before_update``).
    """
    zip_path = config_snapshot.path_for(dest)
    prev = dest.with_name(dest.name + ".prev")
    leaving = fingerprint(dest)
    backup = fingerprint(prev) if prev.is_dir() else None
    try:
        action, slot = config_snapshot.before_update(zip_path, leaving=leaving, backup=backup)
    except config_snapshot.SnapshotError as e:
        raise AppCopyError(
            "NS-E115", f"{e}. The program was not replaced; run INSTALL.cmd again once the copy can be read."
        ) from e
    kept = slot.copy
    if action == config_snapshot.KEEP and kept is not None:
        say(        # the plain words: the window renders what it shows (FN-3), its log keeps them
            f"The settings copy for {old_version} (made {kept.created.replace('T', ' ')}) is kept in "
            f"{zip_path} for ns update --rollback: the last rollback could not put it back, so the settings "
            "files now may be a newer version's"
        )
        return False
    if action == config_snapshot.FOREIGN and kept is not None:
        say(config_snapshot.foreign_note(zip_path, kept, leaving=leaving, backup=backup))
        return False
    try:
        files = config_snapshot.capture(home / "config")
        if before is not None:
            for name in config_snapshot.SETUP_TOUCHES:
                files.pop(name, None)
                if name in before:
                    files[name] = before[name]
        config_snapshot.save(zip_path, files, version=old_version, fingerprint=leaving, taken_by="installer")
    except config_snapshot.SnapshotError as e:
        raise AppCopyError(
            "NS-E115",
            f"could not save a copy of your settings files before replacing the program ({e}). The program "
            "was not replaced. Close programs that use the Nightshift folder, make room on the disk if it is "
            "full, and run INSTALL.cmd again.",
        ) from e
    if slot.damaged:
        say(f"A damaged settings copy in {zip_path} was replaced ({slot.problem})")
    return True


def _finish_interrupted_rollback(
    home: Path, dest: Path, say: Callable[[str], None], settled: str = ""
) -> None:
    """Before the settings copy is saved: a rollback that was interrupted (`<app>.rollback-pending.json`,
    `ns_common.rollback_intent`) is finished when the older program is already in place. When the newer
    program is still in place, its settings are half way to the older version's and the record holds the
    ones from before: the window does not undo it (it has already rewritten some settings files on opening)
    and refuses, ``NS-E115``, so the copy is never made of those settings under the newer version's name."""
    if not rollback_intent.path_for(dest).exists():
        return
    with config_snapshot.locked(config_snapshot.path_for(dest)) as held:
        if not held:
            raise AppCopyError(
                "NS-E115",
                "another Nightshift program is using the settings copy. Close every Nightshift window and "
                "run INSTALL.cmd again. The program was not replaced.",
            )
        rec = rollback_intent.recover(dest, Path(home) / "config", may_undo=False)
    if rec.pending:
        back = f"{settled} Nightshift starts again now. " if settled else ""
        raise AppCopyError(
            "NS-E115",
            f"{back}{rec.sentence} The program was not replaced. Start Nightshift once (it puts your "
            "settings back as they were before the rollback) or run ns update --rollback again, then run "
            "INSTALL.cmd again.",
        )
    if rec.sentence:
        say(rec.sentence)


def _settle_leftover_swap(source: Path, dest: Path, say: Callable[[str], None]) -> str:
    """Before anything is decided about ``dest``: give an ``app.swap`` a rollback left its place
    (``rollback_intent.settle_swap``, under the copy's lock). Returns what was said (empty: nothing).

    This matters most with no ``app`` at all (a power loss between the first two renames of a rollback):
    every venv is an editable install of ``app``, so no Nightshift command, Dashboard or ``ns doctor`` can
    start, and this setup run from a zip is the only code of ours that can. A fresh copy made beside
    ``app.swap`` would leave the owner's program set aside and the rollback's record called stale at the
    first start (the settings from before the rollback lost): so the folder goes back first, and while it
    cannot (held open, not a program folder, or this very setup runs from it) nothing is copied: ``NS-E115``
    with the way out."""
    swap = rollback_intent.swap_for(dest)
    if not rollback_intent.swap_waits(dest):
        return ""
    if same_folder(source, swap):
        raise AppCopyError(
            "NS-E115",
            f"this setup was started from {swap}, the program folder an interrupted rollback left. Nothing "
            f"was changed. Close this window, rename {swap.name} to {dest.name} in {dest.parent}, then start "
            f"Nightshift from its shortcut (or run INSTALL.cmd from {dest}).",
        )
    with config_snapshot.locked(config_snapshot.path_for(dest)) as held:
        if not held:
            raise AppCopyError(
                "NS-E115",
                "another Nightshift program is using the settings copy. Close every Nightshift window and "
                "run INSTALL.cmd again. The program was not replaced.",
            )
        said = rollback_intent.settle_swap(dest, for_update=True)
    if said:
        say(said)
    if rollback_intent.swap_waits(dest) and not dest.exists():
        raise AppCopyError("NS-E115", f"{said} No program was copied.")
    return said


def _error(e: OSError, what: str) -> AppCopyError:
    if is_blocked_error(e):
        return AppCopyError("NS-E113", f"{what}: Windows Security blocked a file ({e})")
    if getattr(e, "winerror", None) in (39, 112) or e.errno == 28:
        return AppCopyError("NS-E104", f"{what}: the disk is full ({e})")
    return AppCopyError("NS-E115", f"{what}: {e}")


def install_app(
    source: Path,
    home: Path,
    *,
    stop_engine: Callable[[], None] | None = None,
    say: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    settings_before: Mapping[str, bytes] | None = None,
) -> CopyResult:
    """Put a complete copy of ``source`` at ``<home>\\app`` (a fresh copy, or the update swap over it).

    When ``source`` IS ``<home>\\app`` (setup run in place) nothing is copied or swapped and the result is
    ``in-place``: only a missing completion marker is written. No settings copy is made and an interrupted
    rollback is neither finished nor refused here (``_finish_interrupted_rollback`` runs on the update path
    only); the first settings write of that setup run does it (``first_start.guard``).

    ``stop_engine`` is called before an existing program folder is renamed. ``settings_before`` is what the
    caller read of ``config_snapshot.SETUP_TOUCHES`` before it changed them (see ``_save_settings_copy``).
    Raises ``AppCopyError``:
    ``NS-E115`` (cannot copy: the folders overlap, a file is locked), ``NS-E104`` (disk full), ``NS-E113``
    (antivirus).
    """
    say = say or (lambda _t: None)
    source, dest = Path(source), app_dir(home)
    new, prev = dest.with_name(dest.name + ".new"), dest.with_name(dest.name + ".prev")
    if same_folder(source, dest):
        if installed_fingerprint(home) is None:
            (dest / MARKER).write_text(fingerprint(dest), encoding="utf-8")
        return CopyResult("in-place", read_version(dest), read_version(dest))
    # (a drive root contains every folder of its drive, the home included; only its bundle entries are copied)
    if (is_inside(dest, source) and not is_drive_root(source)) or is_inside(source, dest):
        raise AppCopyError("NS-E115", f"{source} and {dest} overlap; extract the zip to a separate folder")
    missing = not dest.exists()
    settled = _settle_leftover_swap(source, dest, say)  # first: it can put the program folder back
    if not (missing and dest.exists()):
        settled = ""  # (only "the program folder is back" changes what a refusal below has to say)
    new_version = read_version(source)
    old_version = installed_version(home)
    try:
        shutil.rmtree(new, ignore_errors=True)
        say(f"Copying the program files to {dest}")
        copy_tree(source, new)
        (new / MARKER).write_text(fingerprint(source), encoding="utf-8")
        if dest.exists() and old_version is not None:
            say(f"Updating the Nightshift in {dest} from {old_version} to {new_version}")
            if stop_engine is not None:
                stop_engine()
            kept = _carry(dest, new)
            try:
                _finish_interrupted_rollback(Path(home), dest, say, settled)
                wrote_copy = _save_settings_copy(Path(home), dest, old_version, settings_before, say)
            except AppCopyError:
                shutil.rmtree(new, ignore_errors=True)
                raise
            try:  # from here the copy is replaced for good: it must never outlive its program backup
                shutil.rmtree(prev, ignore_errors=True)
                _rename(dest, prev, sleep)
                try:
                    _rename(new, dest, sleep)
                except OSError:
                    _rename(prev, dest, sleep)  # put the old folder back
                    raise
            except OSError:
                if wrote_copy:  # no program backup: no copy (a kept one stays with the program it belongs to)
                    config_snapshot.discard(config_snapshot.path_for(dest))
                raise
            return CopyResult("updated", old_version, new_version, kept)
        if dest.exists():
            say("Removing an unfinished program folder from an earlier try")
            shutil.rmtree(dest)
        _rename(new, dest, sleep)
    except OSError as e:
        shutil.rmtree(new, ignore_errors=True)
        raise _error(e, f"copying the program folder to {dest}") from e
    return CopyResult("copied", None, new_version)
