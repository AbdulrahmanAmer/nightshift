"""Remove a half-downloaded link folder for sure (defect D-9, box F-B5).

On Windows a file that a process still has open cannot be deleted. yt-dlp is a bundled program whose real
worker is a child of the `yt-dlp.exe` we start, and it may run FFmpeg for the merge, so killing only the
process we started left `source.mp4.part` open for a moment and `rmtree(ignore_errors=True)` swallowed the
error. These helpers kill the whole tree, wait for it to exit, remove the folder with a few retries and a
short backoff, and log through `NsLogger` when the folder still cannot be removed.
`sweep_stale_downloads` clears folders that an earlier crash left behind.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Collection
from pathlib import Path
from typing import Any

from ns_common.logging import NsLogger
from ns_common.paths import Paths

#: Folder under `imports/clipper/` that holds one subfolder per linked video being downloaded.
DOWNLOADS = "_downloads"
#: Removal attempts after the first one, and the pause before attempt n (n * BACKOFF_S seconds).
RETRIES = 8
BACKOFF_S = 0.2
#: A leftover folder is only swept when nothing in it was written for this long.
STALE_AFTER_S = 300.0

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def downloads_root(paths: Paths) -> Path:
    """`<home>/imports/clipper/_downloads`."""
    return paths.home / "imports" / "clipper" / DOWNLOADS


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _rmtree(folder: Path) -> None:
    shutil.rmtree(folder)


def _taskkill(pid: int) -> None:
    """Hard kill of `pid` and its descendants (Windows `taskkill /T /F`); a no-op elsewhere."""
    if sys.platform != "win32":
        return
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=20,
            check=False,
            creationflags=_NO_WINDOW,
        )


def kill_process_tree(proc: Any, *, taskkill: Callable[[int], None] = _taskkill) -> None:
    """Kill `proc` with everything it started, then reap it; never raises.

    The tree goes first (yt-dlp's worker and FFmpeg hold the `.part` file), then `proc.kill()` as the
    backstop, then `proc.wait()` so the caller knows the process is gone.
    """
    pid = getattr(proc, "pid", None)
    try:
        running = proc.poll() is None
    except OSError:
        return
    if running:
        if isinstance(pid, int):
            taskkill(pid)
        with contextlib.suppress(OSError):
            proc.kill()
    with contextlib.suppress(OSError):
        proc.wait()


def _logger(log: NsLogger | None) -> NsLogger | None:
    if log is not None:
        return log
    try:
        return NsLogger("studio", Paths.from_env().logs, echo=False)
    except Exception:  # no usable home: the caller still gets the False answer
        return None


def remove_folder(
    folder: Path,
    *,
    log: NsLogger | None = None,
    retries: int = RETRIES,
    backoff_s: float = BACKOFF_S,
    sleep: Callable[[float], None] | None = None,
    rmtree: Callable[[Path], None] | None = None,
) -> bool:
    """Remove `folder` and everything in it; True when it is gone.

    A file that is still open fails the removal on Windows, so it is tried again up to `retries` more times,
    pausing `backoff_s * attempt` seconds before each. When it still fails the path and the error go to the
    log (`download_cleanup_failed`) and the answer is False; nothing is raised.
    """
    remove = rmtree or _rmtree
    pause = sleep or _sleep
    last: OSError | None = None
    for attempt in range(retries + 1):
        try:
            remove(folder)
            return True
        except FileNotFoundError:
            return True
        except OSError as e:
            last = e
            if attempt < retries:
                pause(backoff_s * (attempt + 1))
    sink = _logger(log)
    if sink is not None:
        with contextlib.suppress(Exception):
            sink.warn(
                "download_cleanup_failed",
                f"could not remove {folder}: {last}",
                path=str(folder),
                attempts=retries + 1,
                error=str(last),
            )
    return False


def discard_download(
    folder: Path,
    proc: Any = None,
    *,
    log: NsLogger | None = None,
    kill: Callable[[Any], None] = kill_process_tree,
    **kw: Any,
) -> bool:
    """Stop `proc` (when given) and its tree, wait for it, then remove `folder` with retries."""
    if proc is not None:
        kill(proc)
    return remove_folder(folder, log=log, **kw)


def _newest_write(folder: Path) -> float:
    """The latest modification time of `folder` or anything inside it."""
    newest = 0.0
    with contextlib.suppress(OSError):
        newest = folder.stat().st_mtime
    for root, dirs, files in os.walk(folder):
        for name in (*dirs, *files):
            with contextlib.suppress(OSError):
                newest = max(newest, os.stat(os.path.join(root, name)).st_mtime)
    return newest


def sweep_stale_downloads(
    root: Path,
    live_ids: Collection[str] = (),
    *,
    min_age_s: float = STALE_AFTER_S,
    now: Callable[[], float] = time.time,
    log: NsLogger | None = None,
    **kw: Any,
) -> list[Path]:
    """Remove the folders under `root` (`imports/clipper/_downloads`) that no live job owns.

    A folder is kept when its name is in `live_ids` (the source id of a queued or running job), when
    anything in it was written within `min_age_s`, or when it is not a plain folder. Returns the folders that
    were removed. A folder that cannot be removed is logged by `remove_folder` and left for the next start.
    """
    if not root.is_dir():
        return []
    live = {str(i) for i in live_ids}
    removed: list[Path] = []
    for child in sorted(root.iterdir()):
        if child.is_symlink() or not child.is_dir() or child.name in live:
            continue
        if now() - _newest_write(child) < min_age_s:
            continue
        if remove_folder(child, log=log, **kw):
            removed.append(child)
    return removed
