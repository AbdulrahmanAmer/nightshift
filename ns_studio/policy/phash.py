"""Perceptual hashing with FFmpeg and numpy only (component C7).

The spec asks for a pHash of 10 sampled frames. `imagehash` and Pillow are not needed: FFmpeg
decodes 32x32 grayscale frames and a 2-D DCT done with numpy yields the standard 64-bit pHash.
"""

from __future__ import annotations

import tempfile
from functools import lru_cache
from pathlib import Path

import numpy as np

from ns_common import ffmpeg
from ns_common.errors import NsError

SIZE = 32
SAMPLES = 10


@lru_cache(maxsize=1)
def _dct_matrix() -> np.ndarray:
    n = np.arange(SIZE)
    k = n.reshape(-1, 1)
    m = np.cos(np.pi * (2 * n + 1) * k / (2 * SIZE)) * np.sqrt(2.0 / SIZE)
    m[0, :] = np.sqrt(1.0 / SIZE)
    return m


def hash_frame(gray: np.ndarray) -> int:
    """64-bit pHash of one 32x32 grayscale frame (top-left 8x8 DCT block against its median)."""
    x = gray.astype(np.float64).reshape(SIZE, SIZE)
    c = _dct_matrix()
    d = c @ x @ c.T
    block = d[:8, :8].flatten()
    median = float(np.median(block[1:]))
    bits = block > median
    value = 0
    for b in bits:
        value = (value << 1) | int(b)
    return value


def frame_hashes(video: Path, samples: int = SAMPLES) -> list[int]:
    """pHashes of `samples` frames spread evenly through `video` (mid-points of equal slices)."""
    duration = ffmpeg.duration_s(video)
    if duration <= 0:
        raise NsError("NS-E401", f"{video} has no duration")
    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "frames.raw"
        ffmpeg.run(
            [
                "-ss",
                f"{duration / (2 * samples):.3f}",
                "-i",
                str(video),
                "-vf",
                f"fps={samples}/{duration:.4f},scale={SIZE}:{SIZE}:flags=area,format=gray",
                "-frames:v",
                str(samples),
                "-f",
                "rawvideo",
                str(raw),
            ]
        )
        data = raw.read_bytes()
    frame = SIZE * SIZE
    count = len(data) // frame
    if count == 0:
        raise NsError("NS-E401", f"no frames decoded from {video}")
    arr = np.frombuffer(data[: count * frame], dtype=np.uint8).reshape(count, frame)
    return [hash_frame(arr[i]) for i in range(count)]


def distance(a: list[int], b: list[int]) -> float:
    """Mean Hamming distance (bits out of 64) over frame-aligned hashes."""
    n = min(len(a), len(b))
    if n == 0:
        return 64.0
    return sum((a[i] ^ b[i]).bit_count() for i in range(n)) / n


def encode(hashes: list[int]) -> str:
    """Comma-separated 16-digit hex form stored in `outputs.phash` and the index file."""
    return ",".join(f"{h:016x}" for h in hashes)


def decode(text: str) -> list[int]:
    """Inverse of `encode`; an empty or malformed string gives an empty list."""
    try:
        return [int(p, 16) for p in text.split(",") if p]
    except ValueError:
        return []
