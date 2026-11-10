"""The ``files`` folder beside INSTALL.cmd: pinned programs taken from disk when a site is down (D-17).

Made beforehand by ``tools/make-files-kit.ps1`` on any PC with internet. One rule, kept here for every Python
caller (the bootstrap, the downloader, step s04): a file is taken only when its bytes have the SHA256 the
manifest pins. ``copy_checked`` hashes the very bytes it writes, to ``<dest>.copy``, and renames after the
check, so nothing unchecked ever has the destination's name. A missing or unreadable folder is no folder.
Standard library only (the setup server imports this before any venv exists).
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import unquote, urlsplit

FILES_DIR = "files"
CHUNK = 1 << 20
#: The way out a failed download of a pinned program names.
KIT_HINT = (
    "Setup can take this file from a files folder instead: on a PC with internet run "
    "tools\\make-files-kit.ps1 (from this bundle), copy its folder files next to INSTALL.cmd, "
    "and run INSTALL.cmd again."
)
ProgressFn = Callable[[int, int | None], None]


def beside(app_path: Path | str) -> Path | None:
    """``<app_path>/files`` when it is a folder that can be seen, else None."""
    folder = Path(app_path) / FILES_DIR
    try:
        return folder if folder.is_dir() else None
    except OSError:
        return None


def url_leaf(url: str) -> str:
    """The file name a pin's URL ends with: the name the kit saves the file under."""
    return Path(unquote(urlsplit(url).path)).name


def is_pin(sha256: str | None) -> bool:
    """True for a real 64-digit SHA256 (not None, not ``TBD-release``)."""
    return sha256 is not None and len(sha256) == 64 and all(c in "0123456789abcdefABCDEF" for c in sha256)


def find(root: Path, name: str, max_depth: int = 4) -> list[Path]:
    """Files called ``name`` in ``root`` (first) and up to ``max_depth`` folders below it."""
    direct = root / name
    found = [direct] if direct.is_file() else []
    base = len(root.parts)
    for current, dirs, files in os.walk(root):
        if len(Path(current).parts) - base >= max_depth:
            dirs[:] = []
        if name in files:
            p = Path(current) / name
            if p not in found:
                found.append(p)
    return found


def label(root: Path, src: Path) -> str:
    """How a file of the folder is named to the owner: ``files\\python\\x.tar.gz``."""
    try:
        rel = src.relative_to(root)
    except ValueError:
        rel = Path(src.name)
    return "\\".join((root.name, *rel.parts))


def copy_checked(src: Path, dest: Path, sha256: str | None, progress: ProgressFn | None = None) -> str | None:
    """Copy ``src`` to ``dest`` when its SHA256 is ``sha256``; the digest when copied, None when it differs.

    ``sha256=None`` (a file with no pin, which only a models folder may give) copies on the name alone. The
    copy goes to ``<dest>.copy`` (``<dest>.part`` belongs to a download in progress) and that file is removed
    on a mismatch, an error, Ctrl+C or Stop, so a half or wrong copy never counts for anything.
    """
    part = dest.with_name(dest.name + ".copy")
    digest = hashlib.sha256()
    done = 0
    try:
        total = src.stat().st_size
        dest.parent.mkdir(parents=True, exist_ok=True)
        with src.open("rb") as fin, part.open("wb") as fout:
            while True:
                block = fin.read(CHUNK)
                if not block:
                    break
                digest.update(block)
                fout.write(block)
                done += len(block)
                if progress:
                    progress(done, total)
        if sha256 is None or digest.hexdigest() == sha256.lower():
            os.replace(part, dest)
            return digest.hexdigest()
    except BaseException:
        _remove(part)
        raise
    _remove(part)
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass        # the copy is never read under this name; the next copy overwrites it


def take(
    root: Path | None, names: Sequence[str], sha256: str | None, dest: Path, say: Callable[[str], None], *,
    progress: ProgressFn | None = None, then: str = "downloading it",
    fatal: Callable[[OSError], bool] = lambda _e: False,
) -> Path | None:
    """Put the pinned file at ``dest`` from the folder ``root``; the file it came from, or None.

    Looks for each of ``names``. Nothing is taken without a real pin. A file whose bytes differ, or that
    cannot be read, is skipped and said once (``files\\x.zip does not match its pin; downloading it``); an
    ``OSError`` for which ``fatal`` is true (a full disk) is raised instead.
    """
    if root is None or not is_pin(sha256):
        return None
    try:
        if not root.is_dir():
            return None
        found = [p for name in dict.fromkeys(names) if name for p in find(root, name)]
    except OSError:
        return None
    for src in dict.fromkeys(found):
        try:
            if copy_checked(src, dest, sha256, progress) is not None:
                return src
        except OSError as e:
            if fatal(e):
                raise
            say(f"{label(root, src)} could not be copied ({e}); {then}")
            continue
        say(f"{label(root, src)} does not match its pin; {then}")
    return None


def verified(root: Path | None, name: str, sha256: str | None, say: Callable[[str], None]) -> Path | None:
    """A file called ``name`` in the folder whose SHA256 is the pin, left where it is; None without one.

    For a caller that hands the path to a program which checks the same SHA256 again (uv with ``--hash``).
    """
    if root is None or not is_pin(sha256):
        return None
    assert sha256 is not None
    try:
        found = find(root, name) if root.is_dir() else []
    except OSError:
        return None
    for src in found:
        try:
            digest = _sha256(src)
        except OSError as e:
            say(f"{label(root, src)} could not be read ({e}); downloading it")
            continue
        if digest == sha256.lower():
            return src
        say(f"{label(root, src)} does not match its pin; downloading it")
    return None
