from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from ns_common.errors import NsError
from ns_common.logging import NsLogger


def _find(name: str, env_var: str) -> str | None:
    return os.environ.get(env_var) or shutil.which(name)


def available() -> bool:
    return _find("ffmpeg", "NS_FFMPEG") is not None and _find("ffprobe", "NS_FFPROBE") is not None


def ffmpeg_exe() -> str:
    exe = _find("ffmpeg", "NS_FFMPEG")
    if not exe:
        raise NsError("NS-E401", "ffmpeg not found on PATH (set NS_FFMPEG)")
    return exe


def ffprobe_exe() -> str:
    exe = _find("ffprobe", "NS_FFPROBE")
    if not exe:
        raise NsError("NS-E401", "ffprobe not found on PATH (set NS_FFPROBE)")
    return exe


def run(
    args: list[str], *, timeout_s: int = 3600, log: NsLogger | None = None
) -> subprocess.CompletedProcess[str]:
    cmd = [ffmpeg_exe(), "-hide_banner", "-y", "-nostdin", *args]
    if log:
        log.debug("ffmpeg_run", cmd=" ".join(cmd))
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout_s, encoding="utf-8", errors="replace"
    )
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
        raise NsError("NS-E401", tail, cmd=cmd)
    return proc


def probe(path: Path) -> dict:
    cmd = [ffprobe_exe(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise NsError("NS-E401", proc.stderr.strip()[-400:], path=str(path))
    return json.loads(proc.stdout)


def duration_s(path: Path) -> float:
    return float(probe(path)["format"]["duration"])


def video_size(path: Path) -> tuple[int, int]:
    for s in probe(path)["streams"]:
        if s["codec_type"] == "video":
            return int(s["width"]), int(s["height"])
    raise NsError("NS-E401", "no video stream", path=str(path))


def has_faststart(path: Path) -> bool:
    with path.open("rb") as fh:
        head = fh.read(4 * 1024 * 1024)
    return head.find(b"moov") != -1 and (head.find(b"mdat") == -1 or head.find(b"moov") < head.find(b"mdat"))


def make_test_clip(
    out: Path, *, width: int, height: int, seconds: float,
    kind: Literal["testsrc2", "color"] = "testsrc2", color: str = "0x202020", fps: int = 30,
) -> Path:
    if kind == "testsrc2":
        src = f"testsrc2=size={width}x{height}:rate={fps}"
    else:
        src = f"color=c={color}:size={width}x{height}:rate={fps}"
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["-f", "lavfi", "-i", src, "-t", f"{seconds:.3f}", "-c:v", "libx264", "-preset", "ultrafast",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(out)])
    return out


def make_silence(out: Path, seconds: float, sr: int = 24000) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["-f", "lavfi", "-i", f"anullsrc=r={sr}:cl=mono", "-t", f"{seconds:.3f}",
         "-c:a", "pcm_s16le", str(out)])
    return out


def make_tone_wav(out: Path, seconds: float, sr: int = 24000, freq: float = 440.0) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    run(["-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate={sr}", "-t", f"{seconds:.3f}",
         "-ac", "1", "-c:a", "pcm_s16le", str(out)])
    return out
