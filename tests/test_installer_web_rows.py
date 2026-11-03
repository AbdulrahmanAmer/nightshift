"""C1: download rows: speed, time left and resume state (pure helpers)."""

from pathlib import Path

import pytest

from installer.stage2 import downloader
from installer.stage2.web import rows


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_speed_is_an_average_over_windows_and_time_left_waits_for_twenty_seconds_of_data():
    clock = Clock()
    meter = rows.SpeedMeter(clock)
    assert meter.update("a", 0) is None                    # the first sample only starts the window
    clock.t += 1.0
    assert meter.update("a", 5_000_000) is None            # the first window is 2 s long
    clock.t += 1.0
    assert meter.update("a", 2_000_000) == 1_000_000       # 2 MB in 2 s
    clock.t += 3.0
    assert meter.update("a", 4_000_000) == 1_000_000       # later windows are 5 s: too early, same value
    clock.t += 2.0
    speed = meter.update("a", 12_000_000)                  # 10 MB in 5 s = 2 MB/s, averaged 30/70 with 1 MB/s
    assert abs(speed - 1_300_000) < 1
    assert meter.speed("a") == speed and meter.has_data() is False      # only 7 s of data so far
    clock.t += 4.0
    assert meter.has_data() is False                       # 11 s: the hero still says "estimating"
    clock.t += 8.0
    assert meter.has_data() is False                       # 19 s
    clock.t += 1.0
    assert meter.has_data() is True                        # 20 s: the estimate may show
    assert rows.MIN_DATA_S == 20.0
    meter.drop("a")
    assert meter.speed("a") is None
    meter.reset()
    assert meter.has_data() is False


def test_time_left_is_rounded_up_to_the_whole_minute():
    assert rows.eta_seconds(600e6, 1e6) == 600
    assert rows.eta_seconds(100e6, 1e6) == 120
    assert rows.eta_seconds(1, 1e6) == 60
    assert rows.eta_seconds(0, 1e6) == 0
    assert rows.eta_seconds(5, None) is None and rows.eta_seconds(5, 0) is None


def test_part_files_and_verified_markers_show_where_a_download_stands(tmp_path: Path):
    row = rows.make_row({"key": "m.gguf", "label": "Story model", "file": "m.gguf", "group": "models",
                         "via": "download", "bytes": 5000})
    assert rows.part_state(tmp_path, row) == (0, False)
    (tmp_path / "models" / "llm").mkdir(parents=True)
    (tmp_path / "models" / "llm" / "m.gguf.part").write_bytes(b"x" * 1234)
    assert rows.part_state(tmp_path, row) == (1234, False)
    (tmp_path / "models" / "llm" / "m.gguf").write_bytes(b"y" * 5000)
    (tmp_path / "models" / "llm" / "m.gguf.ok").write_text("{}", encoding="utf-8")
    assert rows.part_state(tmp_path, row) == (1234, False)  # a marker that vouches for nothing is ignored
    downloader.write_marker(tmp_path / "models" / "llm" / "m.gguf", None)
    assert rows.part_state(tmp_path, row) == (5000, True)
    (tmp_path / "models" / "llm" / "m.gguf").write_bytes(b"y" * 4999)           # the file changed size
    assert rows.part_state(tmp_path, row) == (1234, False)
    (tmp_path / "models" / "llm" / "m.gguf").unlink()                             # ... or was deleted
    assert rows.part_state(tmp_path, row) == (1234, False)
    winget = rows.make_row({"key": "git", "label": "Git", "file": None, "group": "tools", "via": "winget",
                            "bytes": None})
    assert rows.part_state(tmp_path, winget) == (0, False)
    assert rows.format_gb(2 * 1024 ** 3) == "2.0 GB"


def test_the_overall_bar_sums_the_rows():
    meter = rows.SpeedMeter(Clock())
    items = [
        {"bytesTotal": 100, "bytesDone": 100, "status": "done", "speedBps": None},
        {"bytesTotal": 300, "bytesDone": 50, "status": "active", "speedBps": 10.0},
        {"bytesTotal": None, "bytesDone": 0, "status": "queued", "speedBps": None},
    ]
    total = rows.overall(items, meter)
    assert total["bytesTotal"] == 400 and total["bytesDone"] == 150
    assert total["filesTotal"] == 3 and total["filesDone"] == 1
    assert total["speedBps"] == 10.0 and total["etaS"] is None          # not enough data yet


def test_the_whole_download_speed_is_a_trailing_window_over_all_files():
    clock = Clock()
    meter = rows.SpeedMeter(clock)
    # the window needs 5 s of samples before it answers
    assert meter.total_rate(0) is None
    clock.t += 2
    assert meter.total_rate(2_000_000) is None
    clock.t += 4
    rate = meter.total_rate(6_000_000)
    assert rate == pytest.approx(1_000_000)          # 6 MB over 6 s
    # a restarted file drops the total: the window starts again
    clock.t += 1
    assert meter.total_rate(500_000) is None
    # the window keeps only the last 30 s
    for _ in range(40):
        clock.t += 1
        last = meter.total_rate(500_000 + int((clock.t - 107) * 2_000_000))
    assert last == pytest.approx(2_000_000, rel=0.05)
    meter.reset()
    assert meter.total_rate(0) is None


def test_the_overall_time_left_uses_the_whole_download_speed_once_measured():
    clock = Clock()
    meter = rows.SpeedMeter(clock)
    items = [
        {"bytesTotal": 100_000_000, "bytesDone": 0, "status": "active", "speedBps": 10.0},
    ]
    rows.overall(items, meter)                       # first sample, per-file speed stands in
    assert rows.overall(items, meter)["speedBps"] == 10.0
    clock.t += 30
    items[0]["bytesDone"] = 30_000_000
    total = rows.overall(items, meter)               # 30 MB over 30 s beats the stale per-file figure
    assert total["speedBps"] == pytest.approx(1_000_000)
    assert total["etaS"] == 120                       # 70 MB left at 1 MB/s = 70 s, rounded up to minutes


def test_the_time_left_rises_by_at_most_a_fifth_between_polls():
    meter = rows.SpeedMeter(Clock())
    assert meter.smooth_eta(60, 100) == 60
    assert meter.smooth_eta(600, 200) == 72              # a ten times longer estimate climbs 20 % at a time
    assert meter.smooth_eta(600, 300) == 86
    assert meter.smooth_eta(30, 400) == 30               # falling is free
    assert meter.smooth_eta(None, 500) is None           # unknown stays unknown, and the last figure is kept
    assert meter.smooth_eta(60, 600) == 36
    assert meter.smooth_eta(600, 100) == 600             # the bytes done fell: a file restarted, start again
    meter.reset()
    assert meter.smooth_eta(600, 0) == 600


def test_the_overall_time_left_never_jumps_up_by_more_than_a_fifth_between_polls():
    clock = Clock()
    meter = rows.SpeedMeter(clock)
    item = {"bytesTotal": 900_000_000, "bytesDone": 0, "status": "active", "speedBps": 1.0}
    shown: list[int] = []
    # fast, then a long slow stretch (a stalled CDN), then fast again, one poll a second
    for rate in ([30e6] * 12 + [0.2e6] * 25 + [30e6] * 10) * 2:
        clock.t += 1
        item["bytesDone"] = min(item["bytesTotal"], item["bytesDone"] + int(rate))
        eta = rows.overall([item], meter)["etaS"]
        if eta:
            shown.append(eta)
    assert len(shown) > 30
    assert max(b / a for a, b in zip(shown, shown[1:], strict=False)) <= 1.2 + 1e-9
    # without the smoothing the minute rounding alone would have gone 60 -> 120 in one poll
    assert any(b > a for a, b in zip(shown, shown[1:], strict=False))
