"""C3: crash-safe writes (standard library only, so the setup server can use it).

A file written in place is truncated if the process or the PC dies mid-write, and anything that opens it
meanwhile (the dashboard's poll, the setup window, an antivirus scan, a partner double-clicking a zip) reads a
torn file. Every writer here puts the new content in a temporary file BESIDE the target (same folder, so the
swap never crosses a volume), flushes it to disk and swaps it in with `os.replace`: the target is always
either the old complete file or the new complete one, never a part of either.

* `write_text_atomic`, `write_bytes_atomic`, `copy_atomic`, `write_stream_atomic`: a whole file at once.
* `write_zip_atomic`: a zip built by a callback; `append_to_zip_atomic`: members added to a finished zip.
  (`ZipFile(path, "a")` overwrites the old central directory first, so a reader in that millisecond gets
  `BadZipFile`, and a crash there loses the zip for good; this builds the new zip beside it instead.)
* Windows refuses `os.replace` while a reader has the target open (Python's `open` does not share delete),
  and refuses a reader's open for a few milliseconds while a replace is in flight. The writer retries the
  replace (`REPLACE_TRIES`); readers wrap the open in `retry_busy` / `read_text_retry` / `read_bytes_retry`.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import threading
import time
import zipfile
from collections.abc import Callable, Iterable
from functools import partial
from pathlib import Path
from typing import IO, BinaryIO

BACKUP_SUFFIX = ".bak"
BAD_SUFFIX = ".bad"
TMP_SUFFIX = ".tmp"
REPLACE_TRIES = 12
READ_TRIES = 6


def _tmp_for(path: Path) -> Path:
    """A temporary name in the target's own folder, unique per process and thread."""
    return path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}{TMP_SUFFIX}")


def _discard(tmp: Path) -> None:
    with contextlib.suppress(OSError):
        tmp.unlink()


def _sync(fh: BinaryIO) -> None:
    """Flush `fh` and ask the OS to put it on disk (best effort: a disk that refuses is not an error)."""
    fh.flush()
    with contextlib.suppress(OSError):
        os.fsync(fh.fileno())


def _sync_path(path: Path) -> None:
    with contextlib.suppress(OSError), path.open("r+b") as fh:
        _sync(fh)


def replace_atomic(src: Path, dst: Path) -> None:
    """`os.replace(src, dst)`, retried while Windows refuses it because a reader has `dst` open."""
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(src, dst)  # Windows: briefly refused while a reader has the target open
            return
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                raise
            time.sleep(min(0.01 * 2**attempt, 0.1))


def _publish(tmp: Path, path: Path) -> None:
    """Swap the finished temp file in; a temp file that could not be swapped in is removed."""
    try:
        replace_atomic(tmp, path)
    except BaseException:
        _discard(tmp)
        raise


def write_text_atomic(path: Path, text: str, *, keep_backup: bool = False, newline: str | None = "") -> Path:
    """Write `text` to `path` through a temp file and `os.replace`; optionally keep the old file as `.bak`.

    The previous file is copied to the backup only when it exists, so a reader that finds the new file torn
    (which atomic writes prevent, but a disk can still fail) has the last copy that was written before it.
    `newline` is `open`'s: the default writes LF as it is; `None` gives `Path.write_text`'s CRLF on Windows
    for files a person reads in Notepad.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path)
    try:
        with tmp.open("w", encoding="utf-8", newline=newline) as fh:
            fh.write(text)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        if keep_backup and path.exists():
            with contextlib.suppress(OSError):
                shutil.copy2(path, path.with_name(path.name + BACKUP_SUFFIX))
        _publish(tmp, path)
    except BaseException:
        _discard(tmp)
        raise
    return path


def write_bytes_atomic(path: Path, data: bytes) -> Path:
    """`write_bytes` that a reader never sees half done."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path)
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            _sync(fh)
        _publish(tmp, path)
    except BaseException:
        _discard(tmp)
        raise
    return path


def write_stream_atomic(path: Path, src: IO[bytes], *, chunk: int = 1024 * 1024) -> Path:
    """Copy the open binary stream `src` (a zip member, an upload) to `path` through a temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path)
    try:
        with tmp.open("wb") as fh:
            shutil.copyfileobj(src, fh, chunk)
            _sync(fh)
        _publish(tmp, path)
    except BaseException:
        _discard(tmp)
        raise
    return path


def copy_atomic(src: Path, dst: Path) -> Path:
    """`shutil.copy2(src, dst)` where `dst` appears whole (a video in the outbox, a kept copy of a file)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(dst)
    try:
        shutil.copy2(src, tmp)
        _sync_path(tmp)
        _publish(tmp, dst)
    except BaseException:
        _discard(tmp)
        raise
    return dst


def write_zip_atomic(
    path: Path, fill: Callable[[zipfile.ZipFile], None], *, compression: int = zipfile.ZIP_DEFLATED
) -> Path:
    """Build a zip with `fill(zipfile)` in a temp file beside `path`, then swap it in whole.

    Nothing is visible under the final name until the central directory is written; `fill` raising leaves
    the old `path` (if any) untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path)
    try:
        with tmp.open("wb") as fh:
            with zipfile.ZipFile(fh, "w", compression) as zf:
                fill(zf)
            _sync(fh)
        _publish(tmp, path)
    except BaseException:
        _discard(tmp)
        raise
    return path


def append_to_zip_atomic(
    path: Path,
    members: Iterable[tuple[str, Path | bytes]],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
) -> list[str]:
    """Add `(name in the zip, file or bytes)` members to the finished zip `path`; returns the names added.

    A member the zip already has is left alone. The work happens on a copy beside `path` that replaces it
    when it is complete, so a reader sees the zip as it was or the zip with the new members, and a crash
    leaves the old one. Raises `FileNotFoundError` / `zipfile.BadZipFile` when `path` is not a readable zip
    (nothing is written then); returns `[]` without touching the file when there is nothing to add.
    """

    def names_of() -> set[str]:
        with zipfile.ZipFile(path) as zf:
            return set(zf.namelist())

    have = retry_busy(names_of)
    wanted = [(arc, item) for arc, item in members if arc not in have]
    if not wanted:
        return []
    tmp = _tmp_for(path)
    added: list[str] = []
    try:
        retry_busy(lambda: shutil.copyfile(path, tmp))
        with zipfile.ZipFile(tmp, "a", compression) as zf:
            for arc, item in wanted:
                if isinstance(item, bytes):
                    zf.writestr(arc, item)
                else:
                    retry_busy(partial(zf.write, item, arc))
                added.append(arc)
        _sync_path(tmp)
        _publish(tmp, path)
    except BaseException:
        _discard(tmp)
        raise
    return added


def retry_busy[T](fn: Callable[[], T], *, tries: int = READ_TRIES) -> T:
    """Run `fn` (an open or a read), retrying a few milliseconds while Windows refuses it (PermissionError).

    A reader that opens a file at the instant `os.replace` swaps it gets `PermissionError` for a moment even
    though the file is there: without the retry the page shows "idle" or the caller thinks the file is gone.
    A file that really is unreadable (permissions, a folder) still raises after the last try.
    """
    for attempt in range(tries):
        try:
            return fn()
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(min(0.005 * 2**attempt, 0.05))
    raise AssertionError("unreachable")


def read_text_retry(path: Path, encoding: str = "utf-8") -> str:
    """`path.read_text(encoding=...)` that rides out a replace in flight."""
    return retry_busy(lambda: path.read_text(encoding=encoding))


def read_bytes_retry(path: Path) -> bytes:
    """`path.read_bytes()` that rides out a replace in flight."""
    return retry_busy(path.read_bytes)


def quarantine(path: Path) -> Path | None:
    """Move an unreadable file aside as `<name>.bad` (replacing an older one); None if that failed."""
    target = path.with_name(path.name + BAD_SUFFIX)
    try:
        os.replace(path, target)
    except OSError:
        return None
    return target
