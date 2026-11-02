"""C1: download rows: speed, time left and resume state (pure helpers)."""

from pathlib import Path

from installer.stage2.web import rows


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def test_speed_is_an_average_over_windows_and_time_left_waits_for_ten_seconds_of_data():
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
    assert meter.has_data() is True
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
    assert rows.part_state(tmp_path, row) == (5000, True)
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
