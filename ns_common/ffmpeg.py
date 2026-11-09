from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Literal

from ns_common.errors import NsError
from ns_common.logging import NsLogger


def home_bin() -> Path | None:
    """``bin/ffmpeg`` under the install folder: where setup unpacks the pinned build if winget fails."""
    home = os.environ.get("NIGHTSHIFT_HOME")
    return Path(home) / "bin" / "ffmpeg" if home else None


def _find(name: str, env_var: str) -> str | None:
    """The environment override, then PATH, then the build setup placed under the install folder."""
    found = os.environ.get(env_var) or shutil.which(name)
    if found:
        return found
    folder = home_bin()
    if folder is not None:
        for leaf in (f"{name}.exe", name):
            if (folder / leaf).is_file():
                return str(folder / leaf)
    return None


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


@lru_cache(maxsize=8)
def _reads_filter_script_option(exe: str) -> bool:
    """Whether this FFmpeg still has `-filter_complex_script` (removed in FFmpeg 8; `-/filter_complex` is
    the replacement since 7.0)."""
    try:
        help_text = subprocess.run(
            [exe, "-hide_banner", "-h", "full"], capture_output=True, text=True, timeout=60,
            encoding="utf-8", errors="replace",
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "-filter_complex_script" in help_text


def filter_complex_args(
    graph: str, script: Path, *, log: NsLogger | None = None, base: Path | None = None
) -> list[str]:
    """Write `graph` to `script` and return the option pair that makes FFmpeg read it from there.

    A filter graph for a Short with a few dozen clips is tens of thousands of characters, and Windows refuses
    a command line over 32,767 (WinError 206). The graph text is the same as it would be on the command line.
    """
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(graph, encoding="utf-8", newline="")
    if log:
        log.info("ffmpeg_filter_script", f"filter graph in {script}", path=str(script), chars=len(graph))
    option = "-filter_complex_script" if _reads_filter_script_option(ffmpeg_exe()) else "-/filter_complex"
    return [option, relative_arg(script, base)]


def graph_to_script(
    args: list[str], script: Path, *, log: NsLogger | None = None, base: Path | None = None
) -> list[str]:
    """`args` with the value of `-filter_complex` moved into `script` (unchanged when it has none)."""
    if "-filter_complex" not in args:
        return list(args)
    at = args.index("-filter_complex")
    return [*args[:at], *filter_complex_args(args[at + 1], script, log=log, base=base), *args[at + 2 :]]


def common_dir(paths: Iterable[Path | str]) -> Path | None:
    """The deepest folder holding every path (None when they are on different drives)."""
    absolute = [os.path.abspath(p) for p in paths]
    try:
        return Path(os.path.commonpath(absolute)) if absolute else None
    except ValueError:
        return None


def relative_arg(path: Path | str, base: Path | None) -> str:
    """`path` as a command-line argument for a process started in `base`: `./sub/file`, never a bare `-x`.

    Without a `base`, or on another drive, the full path is kept.
    """
    if base is None:
        return str(path)
    try:
        rel = os.path.relpath(path, base)
    except ValueError:
        return str(path)
    rel = rel.replace("\\", "/")
    return rel if rel.startswith("../") else f"./{rel}"


def run(
    args: list[str], *, timeout_s: int = 3600, log: NsLogger | None = None, cwd: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Run FFmpeg; with `cwd` the process starts in that folder, so `args` can name files relative to it."""
    exe = ffmpeg_exe()
    cmd = [os.path.abspath(exe) if cwd is not None else exe, "-hide_banner", "-y", "-nostdin", *args]
    if log:
        log.debug("ffmpeg_run", cmd=" ".join(cmd), cwd=str(cwd) if cwd is not None else None)
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout_s, encoding="utf-8", errors="replace",
        cwd=cwd,
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
