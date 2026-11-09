"""A full disk is reported as a full disk (NS-E104), whoever noticed it first (defect D-8, box F-B4).

Two sources, one answer: a Python write that raised `OSError` with errno 28 (`ENOSPC`) or WinError 112
(`ERROR_DISK_FULL`), and an FFmpeg run that died because its output volume filled up. FFmpeg's exit is a
plain non-zero code that `ns_common.ffmpeg.run` turns into NS-E401 with the tail of stderr, so `run_ffmpeg`
reads that tail and, only on clear signs of a full disk, raises NS-E104 naming the folder instead. Every
other FFmpeg failure stays NS-E401.
"""

from __future__ import annotations

import errno
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.logging import NsLogger

if TYPE_CHECKING:
    import subprocess  # noqa: TID251  (annotation only; the run itself is `ns_common.ffmpeg.run`)

WINERROR_DISK_FULL = 112
# what FFmpeg says when the volume under the output fills up (conservative: no bare "Conversion failed")
FULL_DISK_SIGNS = ("No space left on device", "Error writing trailer", "Error writing output")
# Windows builds sometimes report a failed write on a full volume only as "Invalid argument", or as the
# encoder's closing statistics followed by "Conversion failed!" (the tail QA saw); either counts as a full
# disk only when the volume really is nearly full
NEARLY_FULL_BYTES = 64 * 1024 * 1024


def is_disk_full(exc: BaseException) -> bool:
    """True for an `OSError` meaning the volume is full (errno 28 or WinError 112)."""
    if not isinstance(exc, OSError):
        return False
    return exc.errno == errno.ENOSPC or getattr(exc, "winerror", None) == WINERROR_DISK_FULL


def _free_bytes(folder: Path) -> int | None:
    """Free bytes on the volume that holds `folder` (or its nearest existing parent); None when unknown."""
    probe = folder
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return None


def _free_text(free: int | None) -> str:
    if free is None:
        return ""
    if free >= 1 << 30:
        return f"{free / (1 << 30):.1f} GB free"
    return f"{free / (1 << 20):.0f} MB free"


def disk_full_error(folder: Path, log: NsLogger | None = None) -> NsError:
    """NS-E104 naming the folder that is full and how much room its volume has left."""
    free = _free_bytes(folder)
    detail = f"the disk is full: no space left in {folder}"
    if free is not None:
        detail += f" ({_free_text(free)})"
    if log:
        log.error("disk_full", code="NS-E104", folder=str(folder), free_bytes=free)
    return NsError("NS-E104", detail, folder=str(folder), free_bytes=free)


def classify_ffmpeg_error(err: NsError, folder: Path, log: NsLogger | None = None) -> NsError:
    """NS-E104 when an FFmpeg NS-E401 says the output volume is full, else `err` itself."""
    if err.code != "NS-E401":
        return err
    text = err.detail
    if any(sign in text for sign in FULL_DISK_SIGNS):
        return disk_full_error(folder, log)
    after_encoding = "kb/s:" in text and "Conversion failed" in text
    if "Invalid argument" in text or after_encoding:
        free = _free_bytes(folder)
        if free is not None and free < NEARLY_FULL_BYTES:
            return disk_full_error(folder, log)
    return err


def run_ffmpeg(
    args: list[str], *, folder: Path, log: NsLogger | None = None
) -> subprocess.CompletedProcess[str]:
    """`ns_common.ffmpeg.run`, but a full output volume (`folder` is where FFmpeg writes) is NS-E104."""
    try:
        return ff.run(args, log=log)
    except NsError as e:
        mapped = classify_ffmpeg_error(e, folder, log)
        if mapped is e:
            raise
        raise mapped from e
