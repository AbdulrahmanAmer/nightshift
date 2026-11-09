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
  replace with a growing pause for at most `REPLACE_WAIT_S` (15 s: a virus scan, a sync client or a player can
  hold a file that long); readers wrap the open in `retry_busy` / `read_text_retry` / `read_bytes_retry`.
  When the program does not let go in time the write FAILS with `FileInUseError` (`NS-E119`, also a
  `PermissionError`), the old file is left whole and the temp file is removed. The content is never written
  over the old file in place: a reader (or a crash) would meet it torn, and a torn `install_state.json` is
  moved to `.bad`, where a write that fails loudly would have left the file as it was.
* A crash leaves at most one `~ns<pid>-<thread>-<n>.tmp` beside the target (a short name no sibling glob such
  as `v.*` matches); `sweep_stale_tmp` removes the ones older than an hour, and the outbox copy, the
  diagnostics bundle, the key store (`config/`) and the engine's start call it.
* `prepare(temp file)` (`write_text_atomic`, `write_bytes_atomic`, `write_stream_atomic`) runs on the temp
  file while it is still EMPTY, before a byte of the content is in it: the key store locks its ACL there, so
  the content never sits under the folder's ACL, not even in a temp file a crash leaves behind. Opening the
  file for writing afterwards truncates it and keeps its ACL, and the swap (a rename) keeps it too. When
  `prepare` raises, the temp file is removed and the old file is untouched.
"""

from __future__ import annotations

import contextlib
import itertools
import os
import re
import shutil
import stat
import threading
import time
import zipfile
from collections.abc import Callable, Iterable
from functools import partial
from pathlib import Path
from typing import IO, BinaryIO

from ns_common.errors import NsError

BACKUP_SUFFIX = ".bak"
BAD_SUFFIX = ".bad"
TMP_SUFFIX = ".tmp"
#: How long a refused swap is retried before the write fails (seconds, in total).
REPLACE_WAIT_S = 15.0
_PAUSE_FIRST_S = 0.01
_PAUSE_MAX_S = 0.25
READ_TRIES = 6
#: A temporary file nobody finished (a crash, a power cut) older than this is swept by the next writer.
STALE_TMP_S = 3600.0
#: Short on purpose (the path of a long outbox name must not grow) and never `<target>.*`, so a sibling glob
#: such as `v.*` or `source.*` cannot mistake a temporary file for part of the target.
_TMP_NAME = re.compile(r"^~ns\d+-\d+-\d+\.tmp$")
_tmp_counter = itertools.count(1)


def _tmp_for(path: Path) -> Path:
    """A short temporary name in the target's own folder, unique per process, thread and call."""
    return path.with_name(f"~ns{os.getpid()}-{threading.get_ident()}-{next(_tmp_counter)}{TMP_SUFFIX}")


def sweep_stale_tmp(folder: Path, *, older_than_s: float | None = None) -> int:
    """Delete the temporary files of ours that a crash left in `folder`; returns how many were removed.

    Only a file older than `older_than_s` goes. A file still being written is young (a copy gets a fresh
    creation time, not its source's) and, on Windows, open, so it is never taken for a leftover.
    """
    try:
        entries = [e for e in os.scandir(folder) if _TMP_NAME.match(e.name)]
    except OSError:
        return 0
    removed = 0
    cutoff = time.time() - (STALE_TMP_S if older_than_s is None else older_than_s)
    for entry in entries:
        try:
            if entry.stat().st_ctime < cutoff:
                os.unlink(entry.path)
                removed += 1
        except OSError:
            continue
    return removed


def is_tmp_name(name: str) -> bool:
    """True for the name of a temporary file of ours (`~ns<pid>-<thread>-<n>.tmp`): never real content."""
    return _TMP_NAME.match(name) is not None


def _discard(tmp: Path) -> None:
    try:
        tmp.unlink()
    except PermissionError:
        # `copy2` carries a read-only source's READONLY attribute onto the temp file, and Windows will not
        # delete such a file until the attribute is cleared
        with contextlib.suppress(OSError):
            os.chmod(tmp, stat.S_IWRITE)
            tmp.unlink()
    except OSError:
        pass


def _sync(fh: BinaryIO) -> None:
    """Flush `fh` and ask the OS to put it on disk (best effort: a disk that refuses is not an error)."""
    fh.flush()
    with contextlib.suppress(OSError):
        os.fsync(fh.fileno())


def _sync_path(path: Path) -> None:
    with contextlib.suppress(OSError), path.open("r+b") as fh:
        _sync(fh)


class FileInUseError(NsError, PermissionError):
    """`NS-E119`: another program kept `path` open for the whole wait, so the swap was refused every time.

    The old file is untouched. It is a `PermissionError` (an `OSError`) as well as an `NsError`, so callers
    that already catch `OSError` around a write (the updater's progress lines, a restore's roll back, the key
    store) keep catching it, and the ones that do not show the coded message and its hint.
    """

    def __init__(self, path: Path, waited_s: float) -> None:
        NsError.__init__(self, "NS-E119", f"{path}: still open in another program after {waited_s:.0f} s")


def _in_use(path: Path, waited_s: float, cause: PermissionError) -> PermissionError:
    try:
        return FileInUseError(path, waited_s)
    except (OSError, KeyError, ValueError):  # contracts/errors.json unreadable: say it plainly instead
        return cause


def _waiting_cannot_help(dst: Path) -> bool:
    """A folder in the way or a read-only file is refused for good; only an open file lets go by itself."""
    try:
        return dst.is_dir() or (dst.exists() and not os.access(dst, os.W_OK))
    except OSError:
        return False


def replace_atomic(src: Path, dst: Path, *, wait_s: float = REPLACE_WAIT_S) -> None:
    """`os.replace(src, dst)`, retried while Windows refuses it because another program has `dst` open.

    The pause starts at 10 ms and doubles up to 250 ms; after `wait_s` (default `REPLACE_WAIT_S`) in all it
    raises `FileInUseError` (`NS-E119`) and `dst` is as it was. A caller that must not stall (the updater's
    progress lines, the supervisor's pid file) passes a shorter `wait_s`. A folder in the way or a read-only
    `dst` raises the plain `PermissionError` at once: waiting does not change that.
    """
    started = time.monotonic()
    pause = _PAUSE_FIRST_S
    while True:
        try:
            os.replace(src, dst)  # Windows: refused while a program has the target open
            return
        except PermissionError as e:
            if _waiting_cannot_help(dst):
                raise
            waited = time.monotonic() - started
            if waited >= wait_s:
                raise _in_use(dst, waited, e) from e
            time.sleep(min(pause, wait_s - waited))
            pause = min(pause * 2, _PAUSE_MAX_S)


def _publish(tmp: Path, path: Path, wait_s: float = REPLACE_WAIT_S) -> None:
    """Swap the finished temp file in; the temp file is removed whatever happens.

    When the swap stays refused for `wait_s` the write fails (`FileInUseError`) and `path` keeps its
    old content. It is deliberately NOT copied over the old file in place: that is the torn write this module
    exists to prevent, and a crash in the middle of it leaves the file half written.
    """
    try:
        replace_atomic(tmp, path, wait_s=wait_s)
    except BaseException:
        _discard(tmp)
        raise


PrepareFn = Callable[[Path], None]


def _prepare(tmp: Path, prepare: PrepareFn | None) -> None:
    """Create the empty temp file and hand it to `prepare` (an ACL lock) before any content goes in."""
    if prepare is None:
        return
    with tmp.open("xb"):
        pass
    prepare(tmp)


def write_text_atomic(
    path: Path,
    text: str,
    *,
    keep_backup: bool = False,
    newline: str | None = "",
    wait_s: float = REPLACE_WAIT_S,
    prepare: PrepareFn | None = None,
) -> Path:
    """Write `text` to `path` through a temp file and `os.replace`; optionally keep the old file as `.bak`.

    The previous file is copied to the backup only when it exists, so a reader that finds the new file torn
    (which atomic writes prevent, but a disk can still fail) has the last copy that was written before it.
    `newline` is `open`'s: the default writes LF as it is; `None` gives `Path.write_text`'s CRLF on Windows
    for files a person reads in Notepad. `wait_s`: how long a refused swap is retried (`replace_atomic`).
    `prepare(temp file)` runs on the empty temp file before the text goes in (see the module docstring).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path)
    try:
        _prepare(tmp, prepare)
        with tmp.open("w", encoding="utf-8", newline=newline) as fh:
            fh.write(text)
            fh.flush()
            with contextlib.suppress(OSError):
                os.fsync(fh.fileno())
        if keep_backup and path.exists():
            with contextlib.suppress(OSError):
                shutil.copy2(path, path.with_name(path.name + BACKUP_SUFFIX))
        _publish(tmp, path, wait_s)
    except BaseException:
        _discard(tmp)
        raise
    return path


def write_bytes_atomic(path: Path, data: bytes, *, prepare: PrepareFn | None = None) -> Path:
    """`write_bytes` that a reader never sees half done (`prepare`: as in `write_text_atomic`)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path)
    try:
        _prepare(tmp, prepare)
        with tmp.open("wb") as fh:
            fh.write(data)
            _sync(fh)
        _publish(tmp, path)
    except BaseException:
        _discard(tmp)
        raise
    return path


def write_chunks_atomic(
    path: Path, chunks: Iterable[bytes], *, verify: Callable[[Path], None] | None = None
) -> Path:
    """Write the blocks of a download to `path` through a temp file; `path` shows only when all arrived.

    `verify(temp file)` runs on the finished temporary file before it is swapped in (a digest check). When it
    raises, the temp file is deleted and nothing was ever visible under `path`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path)
    try:
        with tmp.open("wb") as fh:
            for block in chunks:
                fh.write(block)
            _sync(fh)
        if verify is not None:
            verify(tmp)
        _publish(tmp, path)
    except BaseException:
        _discard(tmp)
        raise
    return path


def write_stream_atomic(
    path: Path, src: IO[bytes], *, chunk: int = 1024 * 1024, prepare: PrepareFn | None = None
) -> Path:
    """Copy the open binary stream `src` (a zip member, an upload) to `path` through a temp file.

    `prepare`: as in `write_text_atomic`.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path)
    try:
        _prepare(tmp, prepare)
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
    path: Path,
    fill: Callable[[zipfile.ZipFile], None],
    *,
    compression: int = zipfile.ZIP_DEFLATED,
    prepare: PrepareFn | None = None,
) -> Path:
    """Build a zip with `fill(zipfile)` in a temp file beside `path`, then swap it in whole.

    Nothing is visible under the final name until the central directory is written; `fill` raising leaves
    the old `path` (if any) untouched. `prepare`: as in `write_text_atomic` (an ACL lock on the empty temp
    file, before any content goes in; a failure leaves nothing behind).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path)
    try:
        _prepare(tmp, prepare)
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
