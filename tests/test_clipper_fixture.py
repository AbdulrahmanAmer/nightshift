"""C8 Clipper: the synthetic fixture generator."""
from __future__ import annotations

import json

import pytest

from ns_common import ffmpeg
from ns_studio.clipper.models import Transcript
from tests.fixtures.clipper.make_sample import (
    CAM_H,
    CAM_W,
    DURATION_S,
    HEIGHT,
    WIDTH,
    build_transcript,
    facecam_truth,
    make_sample,
)


def test_transcript_covers_90_seconds() -> None:
    t = Transcript.model_validate(build_transcript())
    assert len(t.segments) == 6
    assert t.duration_s == 90.0
    assert t.words[0].start < 1.0
    assert t.words[-1].end > 85.0
    assert t.words[-1].end < 90.0
    assert {w.seg for w in t.words} == set(range(6))


def test_transcript_is_dense_enough_for_the_filler_filter() -> None:
    t = Transcript.model_validate(build_transcript())
    assert len(t.words) / DURATION_S > 2.2
    for seg in t.segments:
        n = sum(1 for w in t.words if w.seg == seg.i)
        assert n / (seg.end - seg.start) > 2.2


def test_truth_is_a_16_9_rectangle_in_the_top_right_corner() -> None:
    cam = facecam_truth()
    assert cam["w"] * 9 == cam["h"] * 16
    assert cam["x"] + cam["w"] < WIDTH
    assert cam["x"] > WIDTH / 2 and cam["y"] < HEIGHT / 4
    assert (CAM_W, CAM_H) == (cam["w"], cam["h"])


def test_json_only_mode_writes_sidecars(tmp_path) -> None:
    paths = make_sample(tmp_path, with_video=False)
    assert not paths.video.exists()
    assert json.loads(paths.truth.read_text(encoding="utf-8"))["facecam"] == facecam_truth()
    Transcript.model_validate_json(paths.transcript.read_text(encoding="utf-8"))


@pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not available")
def test_sample_video_streams(tmp_path) -> None:
    paths = make_sample(tmp_path)
    info = ffmpeg.probe(paths.video)
    kinds = [s["codec_type"] for s in info["streams"]]
    assert kinds.count("video") == 1 and kinds.count("audio") == 1
    assert ffmpeg.video_size(paths.video) == (WIDTH, HEIGHT)
    assert abs(ffmpeg.duration_s(paths.video) - DURATION_S) < 1.0
