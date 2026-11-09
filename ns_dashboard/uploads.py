"""Resumable chunked uploads into `imports/clipper/<source_id>/` (component C9, spec 20.5)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
from dataclasses import asdict, dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from ns_common.atomic import read_text_retry, write_text_atomic
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
SIDECAR_SUFFIX = ".transcript.json"   # the Clipper's dev-mode transcript next to a video (clipper/source.py)


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


def tmp_dir(paths: Paths) -> Path:
    """The `tmp` folder in the home: scratch space for large files, on the same drive as the home."""
    return paths.home / "tmp"


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
        return d, json.loads(read_text_retry(f))

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
        write_text_atomic(d / META, json.dumps(meta, indent=2))
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
            write_text_atomic(d / META, json.dumps(meta, indent=2))
            return self._info(d, meta)


def adopt_upload(paths: Paths, source_path: Path) -> Path:
    """Move a finished upload to the place the Clipper import would copy it to, and drop the upload folder.

    `imports/clipper/src-YYYYMMDD-xxxx/source.mp4` becomes `imports/clipper/src-<sha256[:10]>/source.mp4`, the
    folder `import_source` fills. It finds the file already there and skips its copy, so a 25 GB stream is
    stored once and written once. Anything that is not an upload of ours (a path on another drive, a
    download) is returned unchanged.
    """
    folder = source_path.parent
    meta_file = folder / META
    if folder.parent != clipper_dir(paths) or not meta_file.is_file():
        return source_path
    try:
        meta = json.loads(read_text_retry(meta_file))
    except (OSError, ValueError):
        return source_path
    digest = meta.get("sha256") if isinstance(meta, dict) else None
    if not isinstance(digest, str) or len(digest) < 10 or not source_path.is_file():
        return source_path
    target = clipper_dir(paths) / f"src-{digest[:10]}" / source_path.name.lower()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():  # the same video was imported before: the copy in the upload folder is the duplicate
        source_path.unlink()
    else:
        os.replace(source_path, target)
    # dev mode (docs/DEV_MODE.md): a transcript sidecar dropped beside the upload travels along, so the fake
    # transcriber finds it; it used to be deleted with the upload folder and every clip job stopped at NS-E406
    sidecar = folder / f"{source_path.stem}{SIDECAR_SUFFIX}"
    if sidecar.is_file():
        os.replace(sidecar, target.with_name(f"{target.stem}{SIDECAR_SUFFIX}"))

    shutil.rmtree(folder, ignore_errors=True)
    return target
