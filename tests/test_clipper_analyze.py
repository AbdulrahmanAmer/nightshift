"""C8 Clipper: FFmpeg analysis (scene cuts, silences, loudness, prompt view)."""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from ns_common import ffmpeg
from ns_common.models import Rect
from ns_studio.clipper import analyze as an
from ns_studio.clipper.models import Analysis, LoudPeak, SilenceSpan
from ns_studio.clipper.source import extract_audio
from tests.fixtures.clipper.make_sample import shared_sample

needs_ffmpeg = pytest.mark.skipif(not ffmpeg.available(), reason="ffmpeg not available")


# ---------- parsers ----------
def test_parse_scene_cuts_reads_showinfo_lines_only() -> None:
    log = "\n".join(
        [
            "[Parsed_showinfo_2 @ 000001] n:   0 pts:      0 pts_time:0.0333 pos: 1 fmt:yuv420p",
            "[Parsed_showinfo_2 @ 000001] n:   1 pts:  90000 pts_time:12.5 pos: 2 fmt:yuv420p",
            "[Parsed_showinfo_2 @ 000001] n:   2 pts: 180000 pts_time:20 pos: 3 fmt:yuv420p",
            "frame=  3 fps=0.0 pts_time:99.0",
        ]
    )
    assert an.parse_scene_cuts(log) == [12.5, 20.0]


def test_parse_silences_with_an_open_final_span() -> None:
    log = "\n".join(
        [
            "[silencedetect @ 0x1] silence_start: 2.5",
            "[silencedetect @ 0x1] silence_end: 4 | silence_duration: 1.5",
            "[silencedetect @ 0x1] silence_start: 88.2",
        ]
    )
    spans = an.parse_silences(log, 90.0)
    assert spans == [SilenceSpan(start=2.5, end=4.0), SilenceSpan(start=88.2, end=90.0)]


def test_parse_ebur128_takes_the_summary_values() -> None:
    log = "\n".join(
        [
            "[Parsed_ebur128_1 @ 0x1] t: 0.1 TARGET:-23 LUFS M: -30.0 S: -120.7 I: -70.0 LUFS LRA: 0.0 LU",
            "  Summary:",
            "  Integrated loudness:",
            "    I:         -17.4 LUFS",
            "    Threshold: -27.5 LUFS",
            "  Loudness range:",
            "    LRA:         3.2 LU",
        ]
    )
    assert an.parse_ebur128(log) == (-17.4, 3.2)
    assert an.parse_ebur128("nothing here") == (None, None)


def test_silent_audio_has_no_integrated_loudness() -> None:
    assert an.parse_ebur128("    I:         -70.0 LUFS\n    LRA:         0.0 LU")[0] is None


def write_wav(path: Path, samples: np.ndarray, rate: int = 16000) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes())
    return path


def test_loudness_peaks_find_the_loud_second(tmp_path: Path) -> None:
    rate = 16000
    rng = np.random.default_rng(1)
    x = (rng.standard_normal(rate * 12) * 0.01).astype(np.float32)
    x[rate * 5 : rate * 6] *= 40  # second 5 is much louder
    peaks = an.loudness_peaks(write_wav(tmp_path / "a.wav", x))
    assert [p.t for p in peaks] == [5.0]
    assert peaks[0].db > -30


def test_loudness_peaks_ignore_flat_audio(tmp_path: Path) -> None:
    x = np.full(16000 * 8, 0.1, dtype=np.float32)
    assert an.loudness_peaks(write_wav(tmp_path / "flat.wav", x)) == []


# ---------- prompt view ----------
def test_window_view_filters_to_the_window() -> None:
    view = an.window_view(
        [5.0, 40.0, 100.0],
        [SilenceSpan(start=10, end=12), SilenceSpan(start=30, end=30.5), SilenceSpan(start=200, end=210)],
        [LoudPeak(t=8, db=-12.0), LoudPeak(t=150, db=-10.0)],
        0.0,
        60.0,
    )
    assert view["scene_cuts"] == [5.0, 40.0]
    assert view["scene_cuts_per_min"] == 2.0
    assert view["silences"] == [[10.0, 12.0]]  # the 0.5 s one is too short to matter
    assert view["loud_peaks"] == [[8.0, -12.0]]


def test_prompt_view_carries_facecam_and_faces() -> None:
    a = Analysis(
        width=1280, height=720, aspect=1.778, duration_s=90, fps=30, scene_cuts=[10.0],
        facecam=Rect(x=944, y=16, w=320, h=180), n_stable_faces=0, faces_detected=False,
    )
    v = an.prompt_view(a, 0, 60)
    assert v["facecam"] == {"x": 944, "y": 16, "w": 320, "h": 180}
    assert v["n_stable_faces"] == 0 and v["faces_detected"] is False
    assert v["window"]["scene_cuts"] == [10.0]  # type: ignore[index]


# ---------- real FFmpeg ----------
@needs_ffmpeg
def test_scene_cut_between_two_colours(tmp_path: Path) -> None:
    out = tmp_path / "cut.mp4"
    ffmpeg.run(
        [
            "-f", "lavfi", "-i", "color=c=red:s=320x180:r=30:d=3",
            "-f", "lavfi", "-i", "color=c=blue:s=320x180:r=30:d=3",
            "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(out),
        ]
    )
    cuts = an.scene_cuts(out)
    assert any(abs(c - 3.0) < 0.2 for c in cuts), cuts


@needs_ffmpeg
def test_audio_events_on_the_fixture(tmp_path: Path) -> None:
    wav = extract_audio(shared_sample().video, tmp_path / "audio.wav")
    silences, lufs, lra = an.audio_events(wav, 90.0)
    assert all(0 <= s.start <= s.end <= 90.0 for s in silences)
    assert lufs is not None and -40.0 < lufs < -3.0
    assert lra is not None and lra >= 0.0
    loud = an.media_loudness(wav, lufs, lra)
    assert loud.integrated_lufs == lufs and all(isinstance(p, LoudPeak) for p in loud.peaks)
