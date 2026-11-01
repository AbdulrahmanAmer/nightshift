"""C8 Clipper: source import and audio extraction (spec 19.2 C1, C2)."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ns_common import ffmpeg
from ns_common.db import Db
from ns_common.errors import NsError
from ns_common.paths import Paths

MAX_DURATION_S = 4 * 3600.0
SIDECAR_SUFFIX = ".transcript.json"


@dataclass
class SourceInfo:
    """Facts about an imported source video (`source.json`)."""

    source_id: str
    path: str
    original_name: str
    title: str
    duration_s: float
    fps: float
    width: int
    height: int
    audio_streams: int
    sha256: str
    rights_confirmed: bool
    imported_at: str
    sidecar: str | None = None


def clipper_dir(paths: Paths, source_id: str) -> Path:
    """`<home>/imports/clipper/<source_id>` (created by `import_source`)."""
    return paths.home / "imports" / "clipper" / source_id


def sha256_file(path: Path) -> str:
    """SHA-256 of a file, read in 1 MiB blocks."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _fps(rate: str) -> float:
    try:
        num, _, den = rate.partition("/")
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def probe_source(video: Path) -> dict[str, Any]:
    """ffprobe facts; raises NS-E405 for unreadable files, no video or audio stream, or more than 4 h."""
    if not video.is_file():
        raise NsError("NS-E405", f"file not found: {video}")
    try:
        info = ffmpeg.probe(video)
    except NsError as e:
        raise NsError("NS-E405", f"cannot read {video.name}: {e.detail}") from e
    streams: list[dict[str, Any]] = info.get("streams", [])
    vids = [s for s in streams if s.get("codec_type") == "video"]
    auds = [s for s in streams if s.get("codec_type") == "audio"]
    if not vids:
        raise NsError("NS-E405", "no video stream", path=str(video))
    if not auds:
        raise NsError("NS-E405", "no audio stream", path=str(video))
    try:
        duration = float(info["format"]["duration"])
    except (KeyError, ValueError) as e:
        raise NsError("NS-E405", "unknown duration", path=str(video)) from e
    if duration <= 0:
        raise NsError("NS-E405", "empty video", path=str(video))
    if duration > MAX_DURATION_S:
        raise NsError("NS-E405", f"{duration / 3600:.1f} h is longer than 4 h", path=str(video))
    v = vids[0]
    return {
        "duration_s": duration,
        "fps": _fps(str(v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1")),
        "width": int(v["width"]),
        "height": int(v["height"]),
        "audio_streams": len(auds),
    }


def import_source(
    paths: Paths,
    db: Db | None,
    video: Path,
    *,
    channel: str | None = None,
    rights_confirmed: bool = False,
    link: bool = False,
    template: str | None = None,
) -> SourceInfo:
    """Copy (or hard-link) `video` into `imports/clipper/<source_id>/source.<ext>` and record it.

    The source id is derived from the content hash, so importing the same file twice is a no-op. A
    `<name>.transcript.json` next to the video travels along as `source.transcript.json`. Writes
    `source.json` and (when `db` is given) the `sources` row.
    """
    facts = probe_source(video)
    digest = sha256_file(video)
    source_id = f"src-{digest[:10]}"
    dest_dir = clipper_dir(paths, source_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"source{video.suffix.lower()}"
    if not dest.exists():
        if link:
            try:
                os.link(video, dest)
            except OSError:
                shutil.copy2(video, dest)
        else:
            shutil.copy2(video, dest)
    sidecar_src = video.parent / f"{video.stem}{SIDECAR_SUFFIX}"
    sidecar: str | None = None
    if sidecar_src.is_file():
        sidecar_dest = dest_dir / f"source{SIDECAR_SUFFIX}"
        shutil.copy2(sidecar_src, sidecar_dest)
        sidecar = str(sidecar_dest)
    info = SourceInfo(
        source_id=source_id,
        path=str(dest),
        original_name=video.name,
        title=video.stem,
        duration_s=facts["duration_s"],
        fps=facts["fps"],
        width=facts["width"],
        height=facts["height"],
        audio_streams=facts["audio_streams"],
        sha256=digest,
        rights_confirmed=rights_confirmed,
        imported_at=datetime.now(UTC).isoformat(timespec="seconds"),
        sidecar=sidecar,
    )
    (dest_dir / "source.json").write_text(json.dumps(asdict(info), indent=2) + "\n", encoding="utf-8")
    if db is not None:
        db.execute(
            "INSERT INTO sources(source_id, channel, path, duration_s, sha256, imported_at, template, status)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(source_id) DO UPDATE SET channel=excluded.channel, path=excluded.path,"
            " duration_s=excluded.duration_s, template=excluded.template",
            (source_id, channel, info.path, info.duration_s, digest, info.imported_at, template, "imported"),
        )
    return info


def set_source_status(db: Db, source_id: str, status: str, template: str | None = None) -> None:
    """Update the `sources` row after a stage (status such as `transcribed`, `done`)."""
    if template is None:
        db.execute("UPDATE sources SET status=? WHERE source_id=?", (status, source_id))
    else:
        db.execute("UPDATE sources SET status=?, template=? WHERE source_id=?", (status, template, source_id))


def extract_audio(video: Path, out_wav: Path) -> Path:
    """16 kHz mono PCM WAV of the first audio stream."""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(
        ["-i", str(video), "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out_wav)]
    )
    return out_wav
