"""The program folder: copy the extracted bundle into ``<home>\\app``, or update the install already there.

Standard library only (the setup server runs on the bare Python). Nightshift must not depend on the place the
zip was extracted to: every venv is an editable install of the program folder, and the shortcuts, the nightly
task, the launchers and ``ns update`` all point at it. Setup therefore copies the extracted folder to
``<home>\\app`` and records that path as ``app_path``; the extracted folder can be deleted afterwards.

* Fresh install: the tree is copied to ``<home>\\app.new`` and renamed to ``<home>\\app`` once it is complete
  (the marker file is written last), so a crash never leaves a half program folder that looks finished.
* Re-run from a newer zip: the same swap ``ns update`` does. The old folder becomes ``<home>\\app.prev`` (the
  rollback target of ``ns update --rollback``), and ``.git``, ``config`` and ``answers.json`` of the old tree
  are carried into the new one.
* Never copied: ``venvs`` and ``models`` (somebody extracted into the home), every ``bin`` folder except the
  bundle's ``bin\\webview2``, caches and ``node_modules``, and ``answers.json`` (the owner's input next to
  ``INSTALL.cmd``; it may hold API keys that setup moves to the keyring, so it is not duplicated).
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ns_common.avblock import is_blocked_error

APP_DIR = "app"
MARKER = ".ns-app.done"
CARRY_OVER = (".git", "config", "answers.json")
# answers.json is the owner's input next to INSTALL.cmd and may hold API keys: it is not duplicated
SKIP_TOP = frozenset({"venvs", "models", "answers.json"})
KEEP_IN_BIN = frozenset({"webview2"})
SKIP_ANYWHERE = frozenset(
    {"__pycache__", "node_modules", ".venv", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
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

    def ignore(directory: str, names: list[str]) -> set[str]:
        here = _norm(directory)
        skipped = {n for n in names if n in SKIP_ANYWHERE}
        if here == root:
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
) -> CopyResult:
    """Put a complete copy of ``source`` at ``<home>\\app`` (a fresh copy, or the update swap over it).

    ``stop_engine`` is called before an existing program folder is renamed. Raises ``AppCopyError``:
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
    if is_inside(dest, source) or is_inside(source, dest):
        raise AppCopyError("NS-E115", f"{source} and {dest} overlap; extract the zip to a separate folder")
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
            shutil.rmtree(prev, ignore_errors=True)
            _rename(dest, prev, sleep)
            try:
                _rename(new, dest, sleep)
            except OSError:
                _rename(prev, dest, sleep)                 # put the old folder back
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
