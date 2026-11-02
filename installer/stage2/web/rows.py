"""Download rows: building them, measuring speed and time left (docs/design/installer-ui.md 2.4)."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

SPEED_WINDOW_S = 5.0        # speed is measured over windows this long (the first one is shorter)
FIRST_WINDOW_S = 2.0
EMA_ALPHA = 0.3
MIN_DATA_S = 20.0           # no time estimate before this much data (the hero says "estimating" until then)
LOW_DISK_GB = 2.0
# where the wizard writes finished files, relative to the install folder (the .part and .ok files sit there)
DEST_DIRS = (
    "models/llm", "models/embed", "models/tts", "models/cv", "bin/_downloads", "bin/yt-dlp",
)


class SpeedMeter:
    """Exponential moving average of bytes per second per file, over 5 s windows."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last: dict[str, tuple[float, int]] = {}
        self._ema: dict[str, float] = {}
        self._first: float | None = None

    def update(self, key: str, done: int) -> float | None:
        """Feed the bytes done so far; returns the file's current average speed (None until measured)."""
        now = self._clock()
        if self._first is None:
            self._first = now
        last = self._last.get(key)
        if last is None:
            self._last[key] = (now, done)
            return None
        dt = now - last[0]
        window = SPEED_WINDOW_S if key in self._ema else FIRST_WINDOW_S
        if dt >= window:
            inst = max(0.0, (done - last[1]) / dt)
            prev = self._ema.get(key)
            self._ema[key] = inst if prev is None else EMA_ALPHA * inst + (1 - EMA_ALPHA) * prev
            self._last[key] = (now, done)
        return self._ema.get(key)

    def speed(self, key: str) -> float | None:
        """The last average for a file."""
        return self._ema.get(key)

    def drop(self, key: str) -> None:
        """Forget a file (it finished)."""
        self._last.pop(key, None)
        self._ema.pop(key, None)

    def reset(self) -> None:
        """Forget everything (pause, stop, restart)."""
        self._last.clear()
        self._ema.clear()
        self._first = None

    def has_data(self) -> bool:
        """True after 20 s of measurements."""
        return self._first is not None and self._clock() - self._first >= MIN_DATA_S


def eta_seconds(remaining: float, speed: float | None) -> int | None:
    """Seconds left rounded up to the whole minute; None when the speed is unknown."""
    if not speed or speed <= 0:
        return None
    if remaining <= 0:
        return 0
    return int(math.ceil(remaining / speed / 60.0) * 60)


def make_row(item: dict[str, Any]) -> dict[str, Any]:
    """A DownloadRow from a plan row."""
    via = item.get("via", "download")
    return {
        "key": item["key"], "label": item["label"], "file": item.get("file"), "group": item["group"],
        "via": via, "bytesTotal": item.get("bytes"), "bytesDone": 0, "status": "queued", "speedBps": None,
        "etaS": None, "note": None, "error": None,
    }


def format_gb(n: float) -> str:
    """Size for notes: 2.1 GB."""
    return f"{n / 1024 ** 3:.1f} GB"


def part_state(home: Path, row: dict[str, Any]) -> tuple[int, bool]:
    """(bytes already in the .part file, whether a verified .ok marker exists) for a download row."""
    name = row.get("file")
    if not name or row["via"] not in ("download", "copy"):
        return 0, False
    for rel in DEST_DIRS:
        base = home / rel
        if (base / f"{name}.ok").is_file() and (base / name).is_file():
            return (base / name).stat().st_size, True
        part = base / f"{name}.part"
        if part.is_file():
            return part.stat().st_size, False
    return 0, False


def overall(rows: list[dict[str, Any]], meter: SpeedMeter) -> dict[str, Any]:
    """The overall bar: bytes, files, speed and time left."""
    total = sum(r["bytesTotal"] or 0 for r in rows)
    done = sum(min(r["bytesDone"], r["bytesTotal"]) if r["bytesTotal"] else r["bytesDone"] for r in rows)
    active = [r for r in rows if r["status"] == "active" and r["speedBps"]]
    speed = sum(r["speedBps"] for r in active) if active else None
    eta = eta_seconds(max(0, total - done), speed) if meter.has_data() and speed else None
    return {
        "bytesTotal": total, "bytesDone": done, "filesTotal": len(rows),
        "filesDone": sum(1 for r in rows if r["status"] in ("done", "skipped")),
        "speedBps": speed, "etaS": eta,
    }
