"""Download rows: building them, measuring speed and time left (docs/design/installer-ui.md 2.4)."""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ns_common.sizes import size_text

SPEED_WINDOW_S = 5.0        # speed is measured over windows this long (the first one is shorter)
FIRST_WINDOW_S = 2.0
EMA_ALPHA = 0.3
MIN_DATA_S = 20.0           # no time estimate before this much data (the hero says "estimating" until then)
TOTAL_WINDOW_S = 30.0       # the whole download's speed is the trailing average over this long
TOTAL_MIN_SPAN_S = 5.0      # ... and needs at least this much of it before it is used
ETA_MAX_RISE = 1.2          # the time left may grow by at most 20 % from one poll to the next
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
        self._totals: list[tuple[float, int]] = []
        self._eta_last: int | None = None
        self._eta_done = 0

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
        self.forget_totals()

    def has_data(self) -> bool:
        """True after 20 s of measurements."""
        return self._first is not None and self._clock() - self._first >= MIN_DATA_S

    # ---- the whole download ----
    def total_rate(self, done_total: int) -> float | None:
        """Bytes per second over the last ``TOTAL_WINDOW_S`` for all files together.

        Fed with the sum of bytes done across every row on each progress event. Steadier than adding up
        per-file averages when files are small or one file restarts; a drop in the total (a restarted
        file) starts the window again. None until the window holds at least ``TOTAL_MIN_SPAN_S`` of data.
        """
        now = self._clock()
        if self._first is None:
            self._first = now
        samples = self._totals
        if samples and done_total < samples[-1][1]:
            samples.clear()
        samples.append((now, done_total))
        cutoff = now - TOTAL_WINDOW_S
        while len(samples) > 2 and samples[1][0] <= cutoff:
            samples.pop(0)
        t0, d0 = samples[0]
        span = now - t0
        if span < TOTAL_MIN_SPAN_S:
            return None
        return max(0.0, (done_total - d0) / span)

    def forget_totals(self) -> None:
        """Start the whole-download window again (pause, stop, restart)."""
        self._totals = []
        self._eta_last = None
        self._eta_done = 0

    def smooth_eta(self, eta: int | None, done_total: int) -> int | None:
        """Keep the whole download's time left from jumping up between polls.

        The estimate may fall as fast as it likes, but it rises by at most 20 % per poll (a speed dip, a
        retry, a slow file at the end of the queue climb to their value over a few polls instead of in one
        jump). It starts again when the bytes done drop (a file restarted) and after a pause or stop.
        """
        if done_total < self._eta_done:
            self._eta_last = None             # a file restarted: the old figure says nothing any more
        self._eta_done = done_total
        if eta is None:
            return None
        last = self._eta_last
        if last and eta > last * ETA_MAX_RISE:
            eta = int(last * ETA_MAX_RISE)
        self._eta_last = eta
        return eta


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
    return size_text(n)


def marker_matches(dest: Path) -> bool:
    """True when ``<dest>.ok`` exists and still describes ``dest`` (same size and mtime).

    The setup server runs on the bare Python (standard library only), so this repeats the check of
    ``installer.stage2.downloader.is_verified`` without importing it; ``test_downloader`` keeps them equal.
    """
    try:
        st = dest.stat()
        marker = json.loads(dest.with_name(dest.name + ".ok").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return (
        isinstance(marker, dict)
        and marker.get("size") == st.st_size
        and marker.get("mtime_ns") == st.st_mtime_ns
    )


def part_state(home: Path, row: dict[str, Any]) -> tuple[int, bool]:
    """(bytes already in the .part file, whether a verified .ok marker exists) for a download row."""
    name = row.get("file")
    if not name or row["via"] not in ("download", "copy"):
        return 0, False
    for rel in DEST_DIRS:
        base = home / rel
        # a marker counts only while the file it vouches for is still there with the size and mtime it had
        if (base / f"{name}.ok").is_file() and marker_matches(base / name):
            return (base / name).stat().st_size, True
        part = base / f"{name}.part"
        if part.is_file():
            return part.stat().st_size, False
    return 0, False


def overall(rows: list[dict[str, Any]], meter: SpeedMeter) -> dict[str, Any]:
    """The overall bar: bytes, files, speed and time left.

    The speed is the whole download's rate over the last 30 s (``SpeedMeter.total_rate``), which is what the
    time left is divided by; while that window is still filling, the per-file averages add up instead.
    """
    total = sum(r["bytesTotal"] or 0 for r in rows)
    done = sum(min(r["bytesDone"], r["bytesTotal"]) if r["bytesTotal"] else r["bytesDone"] for r in rows)
    active = [r for r in rows if r["status"] == "active" and r["speedBps"]]
    per_file = sum(r["speedBps"] for r in active) if active else None
    speed = (meter.total_rate(done) if active else None) or per_file
    eta = eta_seconds(max(0, total - done), speed) if meter.has_data() and speed else None
    eta = meter.smooth_eta(eta, done)
    return {
        "bytesTotal": total, "bytesDone": done, "filesTotal": len(rows),
        "filesDone": sum(1 for r in rows if r["status"] in ("done", "skipped")),
        "speedBps": speed, "etaS": eta,
    }
