from pathlib import Path

import numpy as np
import pytest

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_studio.media.presets import load_thumbnail_layouts
from ns_studio.media.thumbnails import (
    ThumbnailsStage,
    extract_frame,
    make_thumbnails,
    parse_pgm,
    pick_frame_times,
    render_thumbnail,
    sample_times,
    thumb_text,
)
from tests.helpers import make_ctx

needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")


def test_thumb_text_is_at_most_four_uppercase_words():
    assert thumb_text("The Doorbell Camera That Knew My Name") == "DOORBELL CAMERA KNEW NAME"
    assert thumb_text("Scary Story: The Night Bus") == "SCARY STORY NIGHT BUS"
    assert thumb_text("the of a") == "THE OF A"  # only stop words: keep what there is
    assert len(thumb_text("one two three four five six").split()) == 4


def test_three_layouts_are_defined():
    layouts = load_thumbnail_layouts()
    assert len(layouts) == 3
    assert {lay.position for lay in layouts.values()} == {"bottom_left", "center", "top_left"}


def test_parse_pgm_reads_header_and_pixels():
    pix = bytes(range(12))
    img = parse_pgm(b"P5\n# comment\n4 3\n255\n" + pix)
    assert img.shape == (3, 4) and img[2, 3] == 11
    # pixel data that starts with whitespace-like bytes is not eaten by the header parser
    img2 = parse_pgm(b"P5\n2 2\n255\n" + bytes([10, 32, 9, 255]))
    assert img2.tolist() == [[10, 32], [9, 255]]


def test_sample_times_span_the_middle_of_the_video():
    ts = sample_times(100.0, 5)
    assert ts[0] == pytest.approx(8.0) and ts[-1] == pytest.approx(92.0) and len(ts) == 5
    assert sample_times(10.0, 1) == [5.0]


@needs_ffmpeg
def test_pick_frame_times_prefers_high_variance_frames(tmp_path: Path):
    video = tmp_path / "v.mp4"
    ff.run([
        "-f", "lavfi", "-i", "color=c=black:s=320x180:r=30:d=3", "-f", "lavfi", "-i",
        "testsrc2=s=320x180:r=30:d=3", "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(video),
    ])
    times = pick_frame_times(video, tmp_path / "scratch", count=2, samples=12)
    assert len(times) == 2 and all(t > 3.0 for t in times)
    frame = extract_frame(video, times[0], tmp_path / "f.png")
    assert frame.exists() and ff.video_size(frame) == (1280, 720)


def test_render_thumbnail_rejects_more_than_four_words(tmp_path: Path):
    with pytest.raises(NsError) as ei:
        render_thumbnail(tmp_path / "missing.png", "ONE TWO THREE FOUR FIVE",
                         next(iter(load_thumbnail_layouts().values())), tmp_path / "o.jpg")
    assert ei.value.code == "NS-E401"


@needs_ffmpeg
def test_make_thumbnails_with_pillow(tmp_path: Path):
    pytest.importorskip("PIL")
    from PIL import Image

    frames = []
    for i in range(3):
        ff.make_test_clip(tmp_path / f"c{i}.mp4", width=640, height=360, seconds=1.0)
        frames.append(extract_frame(tmp_path / f"c{i}.mp4", 0.5, tmp_path / f"f{i}.png"))
    out = make_thumbnails(frames, "THE NIGHT BUS", tmp_path / "out")
    assert [p.name for p in out] == ["thumb_1.jpg", "thumb_2.jpg", "thumb_3.jpg"]
    images = [Image.open(p) for p in out]
    assert all(im.size == (1280, 720) and im.format == "JPEG" for im in images)
    arrays = [np.asarray(im.convert("L"), dtype=np.int16) for im in images]
    assert not np.array_equal(arrays[0], arrays[1])  # different frames and layouts


@needs_ffmpeg
def test_thumbnails_stage_writes_three_files(tmp_home: Path):
    pytest.importorskip("PIL")

    ctx = make_ctx(tmp_home, fmt="master_first")
    ctx.subdir("long")
    ff.make_test_clip(ctx.file("long/master.mp4"), width=640, height=360, seconds=4.0)
    ctx.write_json("seo_long.json", {"title": "The Doorbell Camera That Knew My Name"})
    ThumbnailsStage().run(ctx)
    for i in (1, 2, 3):
        assert ctx.file(f"long/thumb_{i}.jpg").stat().st_size > 1000
