"""C8 Clipper: source import and audio extraction."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from ns_common import ffmpeg
from ns_common.db import Db
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_studio.clipper import source as src
from tests.fixtures.clipper.make_sample import shared_sample

needs_ffmpeg = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not available")


def env(tmp_home: Path) -> tuple[Paths, Db]:
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    return paths, db


def copy_sample(tmp_path: Path, with_sidecar: bool = True) -> Path:
    s = shared_sample()
    dst = tmp_path / "vod"
    dst.mkdir()
    shutil.copy2(s.video, dst / "sample.mp4")
    if with_sidecar:
        shutil.copy2(s.transcript, dst / "sample.transcript.json")
    return dst / "sample.mp4"


@needs_ffmpeg
def test_import_copies_probes_and_records(tmp_home: Path, tmp_path: Path) -> None:
    paths, db = env(tmp_home)
    video = copy_sample(tmp_path)
    info = src.import_source(paths, db, video, channel="after-dark", rights_confirmed=True)
    dest = Path(info.path)
    assert dest == src.clipper_dir(paths, info.source_id) / "source.mp4"
    assert dest.stat().st_size == video.stat().st_size
    assert info.source_id.startswith("src-") and len(info.source_id) == 14
    assert (info.width, info.height, info.audio_streams) == (1280, 720, 1)
    assert 89 < info.duration_s < 91 and 29 < info.fps < 31
    assert info.sha256 == src.sha256_file(video)
    assert info.sidecar is not None and Path(info.sidecar).name == "source.transcript.json"
    on_disk: dict[str, Any] = json.loads((dest.parent / "source.json").read_text(encoding="utf-8"))
    assert on_disk["rights_confirmed"] is True
    row = db.one("SELECT * FROM sources WHERE source_id=?", (info.source_id,))
    assert row is not None and row["channel"] == "after-dark" and row["status"] == "imported"
    assert row["sha256"] == info.sha256


@needs_ffmpeg
def test_import_twice_is_a_noop(tmp_home: Path, tmp_path: Path) -> None:
    paths, db = env(tmp_home)
    video = copy_sample(tmp_path, with_sidecar=False)
    a = src.import_source(paths, db, video, channel="after-dark")
    b = src.import_source(paths, db, video, channel="after-dark")
    assert a.source_id == b.source_id and a.sidecar is None
    assert len(db.query("SELECT 1 FROM sources")) == 1


@needs_ffmpeg
def test_video_without_audio_is_e405(tmp_home: Path, tmp_path: Path) -> None:
    paths, db = env(tmp_home)
    silent = ffmpeg.make_test_clip(tmp_path / "silent.mp4", width=320, height=180, seconds=2)
    with pytest.raises(NsError) as ei:
        src.import_source(paths, db, silent)
    assert ei.value.code == "NS-E405" and "audio" in ei.value.detail


def test_missing_file_is_e405(tmp_home: Path, tmp_path: Path) -> None:
    paths, db = env(tmp_home)
    with pytest.raises(NsError) as ei:
        src.import_source(paths, db, tmp_path / "nope.mp4")
    assert ei.value.code == "NS-E405"


@needs_ffmpeg
def test_unreadable_file_is_e405(tmp_home: Path, tmp_path: Path) -> None:
    paths, db = env(tmp_home)
    junk = tmp_path / "junk.mp4"
    junk.write_text("not a video", encoding="utf-8")
    with pytest.raises(NsError) as ei:
        src.import_source(paths, db, junk)
    assert ei.value.code == "NS-E405"


def test_longer_than_four_hours_is_e405(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "long.mp4"
    f.write_bytes(b"x")
    fake: dict[str, Any] = {
        "format": {"duration": str(4 * 3600 + 5)},
        "streams": [
            {"codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "30/1"},
            {"codec_type": "audio"},
        ],
    }
    monkeypatch.setattr(ffmpeg, "probe", lambda p: fake)
    with pytest.raises(NsError) as ei:
        src.probe_source(f)
    assert ei.value.code == "NS-E405" and "4 h" in ei.value.detail


def test_exactly_four_hours_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "ok.mp4"
    f.write_bytes(b"x")
    fake: dict[str, Any] = {
        "format": {"duration": str(4 * 3600)},
        "streams": [
            {"codec_type": "video", "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001"},
            {"codec_type": "audio"},
        ],
    }
    monkeypatch.setattr(ffmpeg, "probe", lambda p: fake)
    facts = src.probe_source(f)
    assert facts["duration_s"] == 4 * 3600 and facts["fps"] == pytest.approx(29.97, abs=0.01)


def test_set_source_status(tmp_home: Path) -> None:
    paths, db = env(tmp_home)
    db.execute(
        "INSERT INTO sources(source_id, path, imported_at) VALUES ('src-1','p','2026-11-01T00:00:00')"
    )
    src.set_source_status(db, "src-1", "done", "gaming_facecam")
    row = db.one("SELECT status, template FROM sources WHERE source_id='src-1'")
    assert row is not None and (row["status"], row["template"]) == ("done", "gaming_facecam")


@needs_ffmpeg
def test_extract_audio_is_16k_mono(tmp_path: Path) -> None:
    wav = src.extract_audio(shared_sample().video, tmp_path / "a" / "audio.wav")
    streams = ffmpeg.probe(wav)["streams"]
    assert streams[0]["codec_name"] == "pcm_s16le"
    assert (int(streams[0]["sample_rate"]), int(streams[0]["channels"])) == (16000, 1)
    assert 89 < ffmpeg.duration_s(wav) < 91


def test_stream_rotation_reads_the_display_matrix_and_the_old_tag() -> None:
    """Review clipper row 8: a phone video coded 1920x1080 but shown portrait was analysed sideways."""
    assert src.stream_rotation({}) == 0
    def matrix(rotation: int) -> dict:
        return {"side_data_list": [{"side_data_type": "Display Matrix", "rotation": rotation}]}

    assert src.stream_rotation(matrix(-90)) == 90
    assert src.stream_rotation(matrix(90)) == 270
    assert src.stream_rotation({"side_data_list": [{"rotation": 180}]}) == 180
    assert src.stream_rotation({"tags": {"rotate": "270"}}) == 270
    assert src.stream_rotation({"side_data_list": [{"side_data_type": "Mastering display metadata"}]}) == 0


@needs_ffmpeg
def test_probe_source_reports_the_shown_size_of_a_rotated_phone_video(tmp_path: Path) -> None:
    raw = tmp_path / "raw.mp4"
    rot = tmp_path / "phone.mp4"
    ffmpeg.run(["-f", "lavfi", "-i", "testsrc2=s=640x360:r=10:d=1", "-f", "lavfi", "-i", "sine=d=1",
                "-shortest", "-pix_fmt", "yuv420p", str(raw)])
    ffmpeg.run(["-display_rotation:v", "90", "-i", str(raw), "-c", "copy", str(rot)])
    facts = src.probe_source(rot)
    stream = next(s for s in ffmpeg.probe(rot)["streams"] if s["codec_type"] == "video")
    if src.stream_rotation(stream) == 0:
        pytest.skip("this FFmpeg did not write a display matrix")
    assert (facts["width"], facts["height"]) == (360, 640) and facts["rotation"] in (90, 270)


@needs_ffmpeg
def test_import_from_the_hash_folder_keeps_the_sidecar(tmp_home: Path, tmp_path: Path) -> None:
    """An upload is adopted into imports/clipper/src-<hash>/ with its dev sidecar before the Clipper imports
    it; the second import found the sidecar beside the video and copied it onto itself (WinError 32)."""
    paths, db = env(tmp_home)
    first = src.import_source(paths, db, copy_sample(tmp_path), channel="c", rights_confirmed=True)
    assert first.sidecar and Path(first.sidecar).is_file()
    again = src.import_source(paths, db, Path(first.path), channel="c", rights_confirmed=True)
    assert again.source_id == first.source_id and again.sidecar == first.sidecar
    assert Path(again.sidecar).is_file()
