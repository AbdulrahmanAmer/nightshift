"""D-8 (F-B4): a full disk is reported as NS-E104 naming the folder, never as an FFmpeg or a 500 error."""

from __future__ import annotations

import errno
import json
import subprocess
from pathlib import Path

import pytest

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_studio.media import diskfull
from ns_studio.media.diskfull import (
    classify_ffmpeg_error,
    disk_full_error,
    is_disk_full,
    run_ffmpeg,
)

GB = 1 << 30

# the lines FFmpeg prints when the output volume fills up (libx264 + mp4 muxer, Windows)
STDERR_NO_SPACE = "\n".join(
    [
        "frame=  412 fps= 61 q=28.0 size=   10240kB time=00:00:13.73 bitrate=6108.8kbits/s speed=2.04x",
        "[out#0/mp4 @ 000001c2f8a3c500] Error writing trailer: No space left on device",
        "Error closing file C:\\nightshift\\outbox\\x.mp4: No space left on device",
        "[libx264 @ 000001c2f8a4d400] kb/s:4400.68",
        "Conversion failed!",
    ]
)
STDERR_WRITE_OUTPUT = "av_interleaved_write_frame(): No space left on device\nError writing output file"
STDERR_BAD_FILTER = (
    "[AVFilterGraph @ 0000] No such filter: 'nosuchfilter'\nError initializing filters\nConversion failed!"
)
STDERR_QA_TAIL = (
    "[libx264 @ 000001c2f8a4d400] coded y,uvDC,uvAC intra: 38.9% 21.2% 2.4%\n"
    "[libx264 @ 000001c2f8a4d400] kb/s:4400.68\nConversion failed!"
)
STDERR_BAD_ARG = "[mp4 @ 0000] Invalid argument\nConversion failed!"


def _e401(tail: str) -> NsError:
    return NsError("NS-E401", tail, cmd=["ffmpeg"])


def test_is_disk_full_knows_enospc_and_winerror_112():
    assert is_disk_full(OSError(errno.ENOSPC, "No space left on device"))
    assert is_disk_full(OSError(0, "There is not enough space on the disk", None, 112))
    assert not is_disk_full(OSError(errno.EACCES, "denied"))
    assert not is_disk_full(RuntimeError("x"))


def test_disk_full_error_is_ns_e104_and_names_the_folder_and_the_free_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(diskfull, "_free_bytes", lambda folder: 40 * 1024 * 1024)
    err = disk_full_error(tmp_path / "outbox" / "day")
    assert err.code == "NS-E104"
    assert str(tmp_path / "outbox" / "day") in err.detail and "40 MB free" in err.detail
    assert err.data["folder"] == str(tmp_path / "outbox" / "day")


def test_ffmpeg_stderr_no_space_left_is_ns_e104_with_the_folder(tmp_path: Path):
    out = classify_ffmpeg_error(_e401(STDERR_NO_SPACE), tmp_path / "render")
    assert out.code == "NS-E104" and str(tmp_path / "render") in out.detail


def test_ffmpeg_error_writing_output_is_ns_e104(tmp_path: Path):
    assert classify_ffmpeg_error(_e401(STDERR_WRITE_OUTPUT), tmp_path).code == "NS-E104"
    assert classify_ffmpeg_error(_e401("Error writing trailer of x.mp4"), tmp_path).code == "NS-E104"


def test_other_ffmpeg_failures_stay_ns_e401(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(diskfull, "_free_bytes", lambda folder: 500 * GB)
    for tail in (STDERR_BAD_FILTER, STDERR_BAD_ARG, STDERR_QA_TAIL, "Unknown encoder 'h264_nvenc'", ""):
        err = _e401(tail)
        assert classify_ffmpeg_error(err, tmp_path) is err
        assert err.code == "NS-E401"


def test_invalid_argument_on_a_nearly_full_volume_is_a_full_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Windows FFmpeg sometimes reports a failed write on a full volume only as "Invalid argument"
    monkeypatch.setattr(diskfull, "_free_bytes", lambda folder: 3 * 1024 * 1024)
    assert classify_ffmpeg_error(_e401(STDERR_BAD_ARG), tmp_path).code == "NS-E104"
    # the tail QA saw on a 40 MB volume: encoder statistics, then "Conversion failed!", nothing else
    assert classify_ffmpeg_error(_e401(STDERR_QA_TAIL), tmp_path).code == "NS-E104"
    # ... but a filter error on a nearly full volume is still the encoder's problem
    assert classify_ffmpeg_error(_e401(STDERR_BAD_FILTER), tmp_path).code == "NS-E401"


def test_non_e401_errors_pass_through(tmp_path: Path):
    err = NsError("NS-E503", "x")
    assert classify_ffmpeg_error(err, tmp_path) is err


def _fake_run(tail: str):
    def run(args, *, log=None):
        raise _e401(tail)

    return run


def test_run_ffmpeg_maps_the_failure_and_logs_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(ff, "run", _fake_run(STDERR_NO_SPACE))
    log = NsLogger("studio", tmp_path / "logs", echo=False)
    with pytest.raises(NsError) as ei:
        run_ffmpeg(["-i", "x"], folder=tmp_path / "out", log=log)
    assert ei.value.code == "NS-E104" and str(tmp_path / "out") in ei.value.detail
    events = [
        json.loads(line)
        for f in (tmp_path / "logs").glob("*.jsonl")
        for line in f.read_text(encoding="utf-8").splitlines()
    ]
    full = [e for e in events if e["event"] == "disk_full"]
    assert len(full) == 1 and full[0]["data"]["folder"] == str(tmp_path / "out")


def test_run_ffmpeg_leaves_other_failures_as_ns_e401(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(diskfull, "_free_bytes", lambda folder: 500 * GB)
    monkeypatch.setattr(ff, "run", _fake_run(STDERR_BAD_FILTER))
    with pytest.raises(NsError) as ei:
        run_ffmpeg(["-i", "x"], folder=tmp_path)
    assert ei.value.code == "NS-E401"


def test_run_ffmpeg_passes_the_result_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    done = subprocess.CompletedProcess(["ffmpeg"], 0, "", "ok")
    monkeypatch.setattr(ff, "run", lambda args, *, log=None: done)
    assert run_ffmpeg(["-i", "x"], folder=tmp_path) is done


def test_render_does_not_retry_with_libx264_when_the_disk_is_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The NVENC fallback is for encoder trouble; on a full disk it would only fill the disk again."""
    from ns_common.models import VideoCfg
    from ns_studio.media import render as render_mod
    from ns_studio.media.presets import load_visual_treatments
    from ns_studio.media.shotplan import Shot

    calls: list[list[str]] = []

    def full(args, *, timeout_s=3600, log=None):
        calls.append(args)
        raise _e401(STDERR_NO_SPACE)

    monkeypatch.setattr(ff, "run", full)
    shot = Shot(0, 0.0, 2.0, "fake", "c", "c.mp4", 0.0, 1920, 1080, "p", "fake", "k")
    video = VideoCfg(encoder="h264_nvenc")
    with pytest.raises(NsError) as ei:
        render_mod.render_video(
            shots=[shot],
            audio=tmp_path / "a.wav",
            out=tmp_path / "out" / "v.mp4",
            size=(1080, 1920),
            treatment=next(iter(load_visual_treatments().values())),
            video=video,
            ass_path=None,
        )
    assert ei.value.code == "NS-E104" and len(calls) == 1
