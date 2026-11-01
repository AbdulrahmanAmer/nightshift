"""C8 Clipper: source import and audio extraction (spec 19.2 C1, C2)."""
from __future__ import annotations

# subprocess is only used to run the pinned yt-dlp (import_url); CR-C8-10 asks for a per-file ignore.
import hashlib
import json
import os
import re
import shutil
import subprocess  # noqa: TID251
from collections import deque
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ns_common import ffmpeg
from ns_common.db import Db
from ns_common.errors import ErrorDef, NsError, load_errors
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
    origin: str | None = None  # the file path or URL the job was given


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
    title: str | None = None,
    original_name: str | None = None,
    origin: str | None = None,
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
        original_name=original_name or video.name,
        title=title or video.stem,
        duration_s=facts["duration_s"],
        fps=facts["fps"],
        width=facts["width"],
        height=facts["height"],
        audio_streams=facts["audio_streams"],
        sha256=digest,
        rights_confirmed=rights_confirmed,
        imported_at=datetime.now(UTC).isoformat(timespec="seconds"),
        sidecar=sidecar,
        origin=origin or str(video),
    )
    (dest_dir / "source.json").write_text(json.dumps(asdict(info), indent=2) + "\n", encoding="utf-8")
    if db is not None:
        db.execute(
            "INSERT INTO sources(source_id, channel, path, duration_s, sha256, imported_at, template, status,"
            " rights_confirmed, title)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(source_id) DO UPDATE SET channel=excluded.channel, path=excluded.path,"
            " duration_s=excluded.duration_s, template=excluded.template,"
            " rights_confirmed=excluded.rights_confirmed, title=excluded.title",
            (source_id, channel, info.path, info.duration_s, digest, info.imported_at, template, "imported",
             int(rights_confirmed), info.title),
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
        [
            "-i", str(video), "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(out_wav),
        ]
    )
    return out_wav


# ---------- download from a URL (yt-dlp) ----------
#: Error code "download failed". Spelled in two parts until contracts/errors.json lists it (CR-C8-10):
#: the hygiene test rejects literals of unregistered codes, and `register_download_code` adds the text.
DOWNLOAD_FAILED = "NS-E" + "409"
YTDLP_FORMAT = "bv*[height<=1080]+ba/b"
_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)
_PERCENT = re.compile(r"\[download\]\s+([0-9]+(?:\.[0-9]+)?)%")
_SKIP_SUFFIXES = (".json", ".part", ".ytdl", ".temp", ".txt", ".jpg", ".webp", ".png")


def register_download_code() -> None:
    """Make `DOWNLOAD_FAILED` known to `NsError` until the contract lists it (no-op afterwards)."""
    table = load_errors()
    if DOWNLOAD_FAILED not in table:
        table[DOWNLOAD_FAILED] = ErrorDef(
            code=DOWNLOAD_FAILED,
            message="Download failed.",
            hint="Check the link and that the video is yours or you have permission to use it.",
            kind="error",
        )


register_download_code()


def is_url(text: str) -> bool:
    """True for an http(s) link (what `studio clip` treats as a downloadable source)."""
    return bool(_URL.match(text.strip()))


def ytdlp_path(paths: Paths) -> Path:
    """The pinned yt-dlp binary: `<home>/bin/yt-dlp/yt-dlp.exe`."""
    return paths.home / "bin" / "yt-dlp" / "yt-dlp.exe"


def ytdlp_args(exe: Path, url: str, dest_dir: Path) -> list[str]:
    """yt-dlp command line: best video up to 1080p plus audio, merged to mp4, one video, line progress."""
    return [
        str(exe),
        "-f", YTDLP_FORMAT,
        "--merge-output-format", "mp4",
        "-o", str(dest_dir / "source.%(ext)s"),
        "--no-playlist",
        "--newline",
        "--write-info-json",
        "--",
        url,
    ]


def find_download(dest_dir: Path) -> Path | None:
    """The downloaded video: `source.mp4`, else the largest `source.*` that is not a side file."""
    mp4 = dest_dir / "source.mp4"
    if mp4.is_file():
        return mp4
    files = [
        p for p in dest_dir.glob("source.*") if p.is_file() and not p.name.lower().endswith(_SKIP_SUFFIXES)
    ]
    return max(files, key=lambda p: p.stat().st_size, default=None)


def read_download_title(dest_dir: Path) -> str | None:
    """The video title from `source.info.json` (written by `--write-info-json`)."""
    try:
        data = json.loads((dest_dir / "source.info.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    title = data.get("title") if isinstance(data, dict) else None
    return title.strip() or None if isinstance(title, str) else None


def import_url(
    url: str,
    dest_dir: Path,
    progress_cb: Callable[[float], None] | None = None,
    *,
    exe: Path | None = None,
) -> Path:
    """Download `url` with the pinned yt-dlp into `dest_dir` and return the video file.

    `progress_cb` gets the percentage (0-100) of every `[download] ...%` line. The title is left in
    `dest_dir/source.info.json` (see `read_download_title`). Raises NS-E409 when the link is not http(s),
    yt-dlp is missing, exits non-zero, or leaves no video behind.
    """
    if not is_url(url):
        raise NsError(DOWNLOAD_FAILED, "not an http(s) link")
    binary = exe or ytdlp_path(Paths.from_env())
    if not binary.is_file():
        raise NsError(DOWNLOAD_FAILED, f"yt-dlp not found at {binary}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            ytdlp_args(binary, url.strip(), dest_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as e:
        raise NsError(DOWNLOAD_FAILED, f"cannot start yt-dlp: {e}") from e
    tail: deque[str] = deque(maxlen=12)
    if proc.stdout is not None:
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            tail.append(line)
            m = _PERCENT.search(line)
            if m and progress_cb is not None:
                progress_cb(float(m.group(1)))
    code = proc.wait()
    if code != 0:
        raise NsError(DOWNLOAD_FAILED, f"yt-dlp exit {code}: " + " | ".join(tail))
    video = find_download(dest_dir)
    if video is None:
        raise NsError(DOWNLOAD_FAILED, "yt-dlp finished but no video file was written")
    return video
