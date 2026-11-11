import os
from pathlib import Path

import pytest

from ns_common import ffmpeg
from ns_common.paths import Paths
from ns_dashboard.media import RangeError, open_range, parse_range, thumbnail, thumbs_dir

needs_ffmpeg = pytest.mark.skipif(not ffmpeg.available(), reason="FFmpeg not installed")


@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        (None, 100, None),
        ("", 100, None),
        ("bytes=0-9", 100, (0, 9)),
        ("bytes=10-", 100, (10, 99)),
        ("bytes=-5", 100, (95, 99)),
        ("bytes=-500", 100, (0, 99)),
        ("bytes=90-500", 100, (90, 99)),
        ("bytes=0-0", 100, (0, 0)),
        ("BYTES = 5 - 6", 100, (5, 6)),
        ("bytes=5-2", 100, None),  # inverted: ignore the header
        ("bytes=0-1,5-6", 100, None),  # multi-range unsupported: serve everything
        ("items=0-1", 100, None),
        ("bytes=abc", 100, None),
        ("bytes=-", 100, None),
        ("bytes=0-9", 0, None),
    ],
)
def test_parse_range_table(header: str | None, size: int, expected):
    assert parse_range(header, size) == expected


@pytest.mark.parametrize("header", ["bytes=100-", "bytes=999999-", "bytes=-0"])
def test_parse_range_unsatisfiable(header: str):
    with pytest.raises(RangeError) as ei:
        parse_range(header, 100)
    assert ei.value.size == 100


def make_file(tmp_path: Path, n: int = 5000) -> tuple[Path, bytes]:
    data = bytes(i % 251 for i in range(n))
    f = tmp_path / "v.mp4"
    f.write_bytes(data)
    return f, data


def test_open_range_full_partial_and_416(tmp_path: Path):
    f, data = make_file(tmp_path)
    full = open_range(f, None, chunk=700)
    assert full.status == 200 and full.headers["Content-Length"] == "5000"
    assert full.headers["Accept-Ranges"] == "bytes" and full.headers["Content-Type"] == "video/mp4"
    assert b"".join(full.body) == data

    part = open_range(f, "bytes=1000-1999", chunk=300)
    assert part.status == 206
    assert part.headers["Content-Range"] == "bytes 1000-1999/5000"
    assert part.headers["Content-Length"] == "1000"
    assert b"".join(part.body) == data[1000:2000]

    tail = open_range(f, "bytes=-10")
    assert tail.status == 206 and b"".join(tail.body) == data[-10:]
    assert tail.headers["Content-Range"] == "bytes 4990-4999/5000"

    bad = open_range(f, "bytes=9999-")
    assert bad.status == 416 and bad.headers["Content-Range"] == "bytes */5000"
    assert b"".join(bad.body) == b""


def test_open_range_unknown_type(tmp_path: Path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"abc")
    assert open_range(f, None).headers["Content-Type"] == "application/octet-stream"


@needs_ffmpeg
def test_thumbnail_generated_once_and_cached(tmp_home: Path):
    paths = Paths.from_env()
    video = ffmpeg.make_test_clip(tmp_home / "v.mp4", width=320, height=180, seconds=1.0)
    out = thumbnail(paths, "job-20261101-abcd/s1", video)
    assert out.parent == thumbs_dir(paths) and out.suffix == ".jpg"
    assert out.read_bytes()[:3] == b"\xff\xd8\xff"
    first = out.stat().st_mtime_ns
    again = thumbnail(paths, "job-20261101-abcd/s1", video)
    assert again == out and again.stat().st_mtime_ns == first  # served from cache
    assert not list(thumbs_dir(paths).glob("*.tmp.jpg"))
    other = thumbnail(paths, "job-20261101-abcd/s2", video)
    assert other != out


@needs_ffmpeg
def test_thumbnail_of_very_short_clip_falls_back_to_first_frame(tmp_home: Path):
    paths = Paths.from_env()
    video = ffmpeg.make_test_clip(tmp_home / "tiny.mp4", width=160, height=90, seconds=0.2)
    out = thumbnail(paths, "j-20261101-aaaa/c1", video)
    assert out.exists() and out.stat().st_size > 100


# ---------------------------------------------------------------------------------------------- D-57
def test_a_video_being_played_can_be_moved_away_between_two_blocks(tmp_path: Path):
    """A browser takes the first blocks of a video, stops reading and keeps the connection while the page
    shows the player. The answer held the file open across that pause, so on Windows Reject could not move
    the video (WinError 32). No handle is open between two blocks now: the move works, and the answer ends."""
    f, data = make_file(tmp_path)
    body = open_range(f, None, chunk=1000).body
    assert next(body) == data[:1000]  # the player has its first block and waits
    moved = tmp_path / "rejected.mp4"
    os.replace(f, moved)  # what Reject does; Windows refuses it while an ordinary handle is open on the file
    assert moved.read_bytes() == data and not f.exists()
    assert list(body) == [], "the answer ends where the file went away"


def test_blocks_are_read_from_the_answers_own_offset(tmp_path: Path):
    """Each block is a fresh open and a seek: the bytes and their order are what one open handle gave."""
    f, data = make_file(tmp_path)
    blocks = list(open_range(f, "bytes=1000-3999", chunk=700).body)
    assert [len(b) for b in blocks] == [700, 700, 700, 700, 200] and b"".join(blocks) == data[1000:4000]
    # a file that became shorter while it was being sent ends the answer at its end (no endless loop)
    body = open_range(f, None, chunk=2000).body
    assert next(body) == data[:2000]
    f.write_bytes(data[:2500])
    assert b"".join(body) == data[2000:2500]
