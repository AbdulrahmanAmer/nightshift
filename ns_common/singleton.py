"""One owner per lock file: a lock the operating system drops when the owner dies (standard library only).

Used by the unattended setup run (a second double-click on INSTALL.cmd must not start a second wizard on the
same home) and by the engine (a second start must exit without touching the first one's ``run`` files). A
crashed owner leaves no stale lock: the OS releases it with the process, so there is no pid to check.
"""

from __future__ import annotations

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import IO


class AlreadyRunning(Exception):
    """The lock is held by another process; ``owner`` is its pid when the file says so."""

    def __init__(self, path: Path, owner: int | None = None) -> None:
        super().__init__(f"{path} is held by another process" + (f" (pid {owner})" if owner else ""))
        self.path = path
        self.owner = owner


def _try_lock(fh: IO[bytes]) -> None:
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)       # type: ignore[attr-defined]
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh: IO[bytes]) -> None:
    with contextlib.suppress(OSError):
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)   # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def read_owner(path: Path) -> int | None:
    """The pid written in the lock file (None when unreadable). Windows refuses reads of a locked byte."""
    try:
        with open(path, "rb") as fh:
            fh.seek(1)                                          # byte 0 is the locked one
            text = fh.read(32).decode("ascii", errors="ignore").strip()
        return int(text) if text.isdigit() else None
    except OSError:
        return None


class FileLock:
    """An exclusive, non-blocking lock on ``path``. ``acquire`` raises ``AlreadyRunning`` when it is taken."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh: IO[bytes] | None = None

    def acquire(self) -> None:
        """Take the lock and write this process's pid after the locked byte."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")  # noqa: SIM115 - held until release()
        try:
            _try_lock(fh)
        except OSError:
            fh.close()
            raise AlreadyRunning(self.path, read_owner(self.path)) from None
        self._fh = fh
        with contextlib.suppress(OSError):
            fh.seek(1)
            fh.truncate()
            fh.write(f"{os.getpid()}\n".encode("ascii"))
            fh.flush()

    def release(self) -> None:
        """Drop the lock (also done by the OS when the process ends)."""
        fh, self._fh = self._fh, None
        if fh is None:
            return
        _unlock(fh)
        fh.close()

    @property
    def held(self) -> bool:
        """True while this object holds the lock."""
        return self._fh is not None


@contextlib.contextmanager
def single_instance(path: Path) -> Iterator[FileLock]:
    """Hold ``path`` for the length of the block; ``AlreadyRunning`` when another process has it."""
    lock = FileLock(path)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()
