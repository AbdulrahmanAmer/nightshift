"""C3: crash-safe writes for small state files (standard library only, so the setup server can use it).

A file written in place is truncated if the process or the PC dies mid-write; the next start then fails to
parse it. `write_text_atomic` writes a temp file beside the target, flushes it to disk and swaps it in with
`os.replace`, so the file is always either the old content or the new one.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import threading
import time
from pathlib import Path

BACKUP_SUFFIX = ".bak"
BAD_SUFFIX = ".bad"
REPLACE_TRIES = 8


def write_text_atomic(path: Path, text: str, *, keep_backup: bool = False) -> Path:
    """Write `text` to `path` through a temp file and `os.replace`; optionally keep the old file as `.bak`.

    The previous file is copied to the backup only when it exists, so a reader that finds the new file torn
    (which atomic writes prevent, but a disk can still fail) has the last copy that was written before it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)
        fh.flush()
        with contextlib.suppress(OSError):
            os.fsync(fh.fileno())
    if keep_backup and path.exists():
        with contextlib.suppress(OSError):
            shutil.copy2(path, path.with_name(path.name + BACKUP_SUFFIX))
    for attempt in range(REPLACE_TRIES):
        try:
            os.replace(tmp, path)       # Windows: briefly refused while a reader has the target open
            return path
        except PermissionError:
            if attempt == REPLACE_TRIES - 1:
                with contextlib.suppress(OSError):
                    tmp.unlink()
                raise
            time.sleep(0.05)
    return path


def quarantine(path: Path) -> Path | None:
    """Move an unreadable file aside as `<name>.bad` (replacing an older one); None if that failed."""
    target = path.with_name(path.name + BAD_SUFFIX)
    try:
        os.replace(path, target)
    except OSError:
        return None
    return target
