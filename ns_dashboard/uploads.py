"""Resumable chunked uploads into `imports/clipper/<source_id>/` (component C9, spec 20.5)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import asdict, dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from ns_common.db import new_job_id
from ns_common.paths import Paths
from ns_dashboard.errors import ApiError

CHUNK = 8 * 1024 * 1024
MAX_CHUNK = 16 * 1024 * 1024
MAX_SIZE = 64 * 1024**3
ALLOWED_EXT = frozenset({".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".mpg", ".mpeg", ".ts"})
# voice-sample recordings for Library -> Clone (the clone route reads any recording by content with FFmpeg)
AUDIO_EXT = frozenset({".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webm"})
UPLOAD_EXT = ALLOWED_EXT | AUDIO_EXT
SOURCE_ID_RE = re.compile(r"^src-\d{8}-[0-9a-f]{4}$")
META = "upload.json"


@dataclass
class UploadInfo:
    """State of one upload; `upload_id` equals the `source_id` used by the Clipper."""

    upload_id: str
    source_id: str
    filename: str
    size: int
    offset: int
    status: str  # uploading | complete
    path: str | None = None
    sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """API shape."""
        return asdict(self)


def clipper_dir(paths: Paths) -> Path:
    """`imports/clipper` under the Nightshift home."""
    return paths.home / "imports" / "clipper"


def source_dir(paths: Paths, source_id: str) -> Path:
    """Directory of one source; rejects ids that are not of the generated form."""
    if not SOURCE_ID_RE.match(source_id):
        raise ApiError(404, "not_found", "Unknown upload or source.")
    return clipper_dir(paths) / source_id


def find_source(paths: Paths, source_id: str) -> Path | None:
    """The finished `source.<ext>` of `source_id`, or None."""
    d = source_dir(paths, source_id)
    if not d.exists():
        return None
    for f in sorted(d.glob("source.*")):
        if f.suffix != ".part":
            return f
    return None


class UploadManager:
    """Creates uploads, appends chunks at the right offset, and finalises with a sha256."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self._lock = threading.Lock()

    def _meta(self, upload_id: str) -> tuple[Path, dict[str, Any]]:
        d = source_dir(self.paths, upload_id)
        f = d / META
        if not f.exists():
            raise ApiError(404, "not_found", "Unknown upload.")
        return d, json.loads(f.read_text(encoding="utf-8"))

    def _info(self, d: Path, meta: dict[str, Any]) -> UploadInfo:
        part = d / f"source{meta['ext']}.part"
        final = d / f"source{meta['ext']}"
        complete = meta.get("status") == "complete"
        offset = meta["size"] if complete else (part.stat().st_size if part.exists() else 0)
        return UploadInfo(
            upload_id=meta["source_id"],
            source_id=meta["source_id"],
            filename=meta["filename"],
            size=meta["size"],
            offset=offset,
            status="complete" if complete else "uploading",
            path=str(final) if complete else None,
            sha256=meta.get("sha256"),
        )

    def start(self, filename: str, size: int) -> UploadInfo:
        """Begin an upload; `filename` only contributes its (allow-listed video or audio) extension."""
        name = PureWindowsPath(filename or "").name
        ext = Path(name).suffix.lower()
        if ext not in UPLOAD_EXT:
            raise ApiError(
                400, "bad_file_type", f"'{ext or name}' is not a supported video or audio file.",
                hint="Videos: mp4, mkv, mov, webm, avi, m4v, mpg or ts. Voice samples: wav, mp3, m4a, flac, "
                "ogg or webm.",
            )
        if not isinstance(size, int) or size <= 0 or size > MAX_SIZE:
            raise ApiError(400, "bad_size", "The file size is missing or too large.")
        source_id = new_job_id("src")
        d = clipper_dir(self.paths) / source_id
        d.mkdir(parents=True, exist_ok=True)
        meta = {"source_id": source_id, "filename": name, "ext": ext, "size": size, "status": "uploading"}
        (d / META).write_text(json.dumps(meta, indent=2), encoding="utf-8")
        (d / f"source{ext}.part").write_bytes(b"")
        return self._info(d, meta)

    def status(self, upload_id: str) -> UploadInfo:
        """Current offset (resume point) of an upload."""
        d, meta = self._meta(upload_id)
        return self._info(d, meta)

    def append(self, upload_id: str, offset: int, data: bytes) -> UploadInfo:
        """Append `data` at `offset`; 409 with the true offset when they disagree."""
        if not data:
            raise ApiError(400, "empty_chunk", "The chunk is empty.")
        if len(data) > MAX_CHUNK:
            raise ApiError(413, "chunk_too_large", "Chunks must be 8 MB or smaller.")
        with self._lock:
            d, meta = self._meta(upload_id)
            if meta.get("status") == "complete":
                raise ApiError(409, "already_complete", "This upload is already complete.")
            part = d / f"source{meta['ext']}.part"
            current = part.stat().st_size if part.exists() else 0
            if offset != current:
                raise ApiError(
                    409, "offset_mismatch", f"Expected offset {current}.", details={"offset": current}
                )
            if current + len(data) > meta["size"]:
                raise ApiError(400, "too_much_data", "The chunk goes past the declared file size.")
            with part.open("ab") as fh:
                fh.write(data)
            return self._info(d, meta)

    def complete(self, upload_id: str) -> UploadInfo:
        """Verify the size, compute sha256, and move the file to `source.<ext>`."""
        with self._lock:
            d, meta = self._meta(upload_id)
            if meta.get("status") == "complete":
                return self._info(d, meta)
            part = d / f"source{meta['ext']}.part"
            have = part.stat().st_size if part.exists() else 0
            if have != meta["size"]:
                raise ApiError(
                    409, "incomplete", f"Only {have} of {meta['size']} bytes arrived.",
                    details={"offset": have},
                )
            h = hashlib.sha256()
            with part.open("rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    h.update(block)
            final = d / f"source{meta['ext']}"
            os.replace(part, final)
            meta["status"] = "complete"
            meta["sha256"] = h.hexdigest()
            (d / META).write_text(json.dumps(meta, indent=2), encoding="utf-8")
            return self._info(d, meta)
