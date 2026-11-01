from pathlib import Path

import pytest

from ns_common import ffmpeg as ff

pytestmark = pytest.mark.skipif(not ff.available(), reason="ffmpeg not on PATH")


def test_make_test_clip_and_probe(tmp_path: Path):
    clip = ff.make_test_clip(tmp_path / "c.mp4", width=320, height=180, seconds=1.0)
    assert clip.exists()
    assert ff.video_size(clip) == (320, 180)
    assert 0.9 <= ff.duration_s(clip) <= 1.1
    assert ff.has_faststart(clip)


def test_make_tone_wav(tmp_path: Path):
    wav = ff.make_tone_wav(tmp_path / "t.wav", seconds=0.5)
    info = ff.probe(wav)
    assert info["streams"][0]["codec_type"] == "audio"
    assert int(info["streams"][0]["sample_rate"]) == 24000


def test_run_raises_nserror_on_failure(tmp_path: Path):
    from ns_common.errors import NsError

    with pytest.raises(NsError) as ei:
        ff.run(["-i", str(tmp_path / "missing.mp4"), str(tmp_path / "o.mp4")])
    assert ei.value.code == "NS-E401"
