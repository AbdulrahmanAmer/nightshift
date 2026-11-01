"""Range streaming of output videos and cached thumbnails (component C9, spec 20.5)."""

from __future__ import annotations

import hashlib
import os
import re
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ns_common import ffmpeg
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths

CONTENT_TYPES = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}
_RANGE_RE = re.compile(r"\s*bytes\s*=\s*(\d*)\s*-\s*(\d*)\s*", re.IGNORECASE)
_thumb_lock = threading.Lock()


class RangeError(Exception):
    """The requested range cannot be satisfied (HTTP 416)."""

    def __init__(self, size: int) -> None:
        super().__init__(f"range not satisfiable for {size} bytes")
        self.size = size


def parse_range(header: str | None, size: int) -> tuple[int, int] | None:
    """Parse a single `Range: bytes=a-b` header into inclusive (start, end).

    Returns None when the whole file should be served (no header, malformed, multi-range, or empty
    file) and raises RangeError when the range starts past the end of the file.
    """
    if not header or size <= 0:
        return None
    m = _RANGE_RE.fullmatch(header)
    if not m:
        return None
    first, last = m.group(1), m.group(2)
    if first == "" and last == "":
        return None
    if first == "":
        count = int(last)
        if count == 0:
            raise RangeError(size)
        return max(0, size - count), size - 1
    start = int(first)
    if start >= size:
        raise RangeError(size)
    end = int(last) if last else size - 1
    if end < start:
        return None
    return start, min(end, size - 1)


@dataclass
class RangeResult:
    """Everything the HTTP layer needs to answer a (possibly ranged) media request."""

    status: int
    headers: dict[str, str]
    body: Iterator[bytes]
    length: int = 0
    extra: dict[str, int] = field(default_factory=dict)


def _read(path: Path, start: int, length: int, chunk: int) -> Iterator[bytes]:
    remaining = length
    with path.open("rb") as fh:
        fh.seek(start)
        while remaining > 0:
            block = fh.read(min(chunk, remaining))
            if not block:
                break
            remaining -= len(block)
            yield block


def open_range(path: Path, header: str | None, *, chunk: int = 1 << 20) -> RangeResult:
    """Prepare a 200, 206 or 416 response for `path` honouring the `Range` header."""
    size = path.stat().st_size
    ctype = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
    base = {"Accept-Ranges": "bytes", "Content-Type": ctype}
    try:
        rng = parse_range(header, size)
    except RangeError:
        return RangeResult(416, {**base, "Content-Range": f"bytes */{size}", "Content-Length": "0"}, iter(()))
    if rng is None:
        headers = {**base, "Content-Length": str(size)}
        return RangeResult(200, headers, _read(path, 0, size, chunk), size)
    start, end = rng
    length = end - start + 1
    headers = {**base, "Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(length)}
    return RangeResult(206, headers, _read(path, start, length, chunk), length, {"start": start, "end": end})


def thumbs_dir(paths: Paths) -> Path:
    """Where generated thumbnails are cached."""
    return paths.home / "cache" / "dashboard" / "thumbs"


def thumbnail(paths: Paths, output_id: str, video: Path, log: NsLogger | None = None) -> Path:
    """First-frame JPEG of `video`, generated once with FFmpeg and cached (rebuilt if the video changed)."""
    key = hashlib.sha1(output_id.encode("utf-8")).hexdigest()[:16]
    out = thumbs_dir(paths) / f"{key}.jpg"
    with _thumb_lock:
        if out.exists() and out.stat().st_mtime >= video.stat().st_mtime:
            return out
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(f"{key}.tmp.jpg")
        vf = ["-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4", str(tmp)]
        try:
            ffmpeg.run(["-ss", "0.5", "-i", str(video), *vf], log=log, timeout_s=60)
            if not tmp.exists() or tmp.stat().st_size == 0:
                raise NsError("NS-E401", "no frame at 0.5 s")
        except NsError:
            tmp.unlink(missing_ok=True)
            ffmpeg.run(["-i", str(video), *vf], log=log, timeout_s=60)
        os.replace(tmp, out)
    return out
