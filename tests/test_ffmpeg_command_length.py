"""P46: an FFmpeg command line does not grow with the install path depth or the clip count.

Windows refuses a command line over 32,767 characters (WinError 206, NS-E327). The Short render used to put
the whole filter graph and one absolute path per stock clip on the command line (35,866 characters on a
deep temp path). The graph now goes into a script file in the render folder and the inputs are named relative
to the folder they share.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ns_common import ffmpeg as ff
from ns_common.logging import NsLogger
from ns_common.models import VideoCfg
from ns_studio.media import longform
from ns_studio.media.audio import FINAL_LUFS, FINAL_TP, loudnorm
from ns_studio.media.captions import build_ass
from ns_studio.media.longform import render_segment
from ns_studio.media.master import ChapterTiming
from ns_studio.media.presets import chapter_card, visual_treatment
from ns_studio.media.qc import run_qc
from ns_studio.media.render import VERTICAL, build_graph, render_video
from ns_studio.media.shotplan import Shot, frame_edges
from ns_studio.media.transcribe import Word

needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")
ARGV_LIMIT = 4000
CLIPS = 16


def _cmd_chars(cmd: list[str]) -> int:
    """What Windows counts: the command line string CreateProcess receives."""
    return len(subprocess.list2cmdline(cmd))


@pytest.fixture
def commands(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Every FFmpeg command line `ns_common.ffmpeg` starts, and the folder it was started in."""
    seen: list[list[str]] = []
    real = subprocess.run

    def spy(cmd: Any, *a: Any, **kw: Any) -> Any:
        if "-nostdin" in cmd:  # a render, not the help text read to pick the script option
            seen.append(list(cmd))
        return real(cmd, *a, **kw)

    monkeypatch.setattr(ff.subprocess, "run", spy)
    return seen


def _deep_home(tmp_path: Path) -> Path:
    """A home whose path is at least 120 characters, as under a long user name or a synced folder."""
    pad = max(8, 120 - len(str(tmp_path)))
    home = tmp_path / ("h" * pad)
    home.mkdir()
    assert len(str(home)) >= 120
    return home


def _shots(home: Path, count: int, each_s: float) -> list[Shot]:
    folder = home / "jobs" / "job_20261109_deep" / "clips"
    folder.mkdir(parents=True)
    first = ff.make_test_clip(folder / "stock_000.mp4", width=160, height=90, seconds=1.0)
    shots: list[Shot] = []
    for i in range(count):
        path = first if i == 0 else Path(shutil.copy(first, folder / f"stock_{i:03d}.mp4"))
        shots.append(
            Shot(0, i * each_s, (i + 1) * each_s, "fake", f"c{i}", str(path), 0.0, 160, 90, "u", "l", "kw")
        )
    return shots


def test_filter_script_replaces_the_graph_on_the_command_line(tmp_path: Path):
    graph = "[0:v]scale=64:64[vout];" * 400
    script = tmp_path / "out" / "short.filtergraph.txt"
    args = ff.filter_complex_args(graph, script)
    assert script.read_text(encoding="utf-8") == graph
    assert args[0] in ("-/filter_complex", "-filter_complex_script")
    assert args[1] == str(script)
    assert len(" ".join(args)) < 200 < len(graph)


def test_filter_script_is_logged(tmp_home: Path, tmp_path: Path):
    log = NsLogger("test", tmp_home / "logs")
    script = tmp_path / "g.txt"
    ff.filter_complex_args("[0:v]null[vout]", script, log=log)
    lines = [
        json.loads(x) for f in (tmp_home / "logs").glob("*.jsonl") for x in f.read_text("utf-8").splitlines()
    ]
    (rec,) = [r for r in lines if r["event"] == "ffmpeg_filter_script"]
    assert rec["data"]["path"] == str(script) and rec["data"]["chars"] == len("[0:v]null[vout]")


def test_move_graph_to_script_swaps_only_the_graph_value(tmp_path: Path):
    args = ["-i", "in.mp4", "-filter_complex", "[0:v]null[vout]", "-map", "[vout]", "out.mp4"]
    moved = ff.graph_to_script(args, tmp_path / "g.txt")
    assert moved[:2] == ["-i", "in.mp4"] and moved[-3:] == ["-map", "[vout]", "out.mp4"]
    assert "[0:v]null[vout]" not in moved
    assert (tmp_path / "g.txt").read_text(encoding="utf-8") == "[0:v]null[vout]"
    assert ff.graph_to_script(["-i", "x"], tmp_path / "none.txt") == ["-i", "x"]
    assert not (tmp_path / "none.txt").exists()


def test_inputs_are_named_relative_to_their_shared_folder(tmp_path: Path):
    a, b = tmp_path / "jobs" / "j1" / "a.mp4", tmp_path / "cache" / "b.mp4"
    base = ff.common_dir([a, b])
    assert base == tmp_path
    assert ff.relative_arg(a, base) == "./jobs/j1/a.mp4" and ff.relative_arg(b, base) == "./cache/b.mp4"


def test_inputs_on_another_drive_keep_their_full_path():
    if os.name != "nt":
        pytest.skip("drive letters")
    assert ff.common_dir([Path("C:/x/a.mp4"), Path("D:/y/b.mp4")]) is None
    assert ff.relative_arg(Path("D:/y/b.mp4"), None) == str(Path("D:/y/b.mp4"))


def test_a_dash_led_file_name_is_not_read_as_an_option(tmp_path: Path):
    assert ff.relative_arg(tmp_path / "-weird.mp4", tmp_path) == "./-weird.mp4"


@needs_ffmpeg
def test_run_starts_ffmpeg_in_the_given_folder(tmp_path: Path):
    (tmp_path / "sub").mkdir()
    ff.make_test_clip(tmp_path / "sub" / "c.mp4", width=64, height=64, seconds=0.3)
    ff.run(["-i", "./sub/c.mp4", "-f", "null", "-"], cwd=tmp_path)


@needs_ffmpeg
def test_filter_script_runs_in_this_ffmpeg(tmp_path: Path):
    args = ff.filter_complex_args("color=c=red:s=64x64:d=0.2[v]", tmp_path / "g.txt")
    ff.run([*args, "-map", "[v]", "-f", "null", "-"])


@needs_ffmpeg
def test_short_render_command_is_short_under_a_deep_home_and_passes_qc(
    tmp_path: Path, commands: list[list[str]]
):
    home = _deep_home(tmp_path)
    shots = _shots(home, CLIPS, 0.4)
    treatment = visual_treatment("cinematic_teal")
    video = VideoCfg(encoder="libx264", preset="ultrafast")
    edges = frame_edges([s.end for s in shots], video.fps)
    ass = home / "jobs" / "job_20261109_deep" / "captions" / "s1.ass"
    ass.parent.mkdir(parents=True)
    ass.write_text(build_ass([Word("quiet", 0.0, 0.3), Word("night", 0.4, 0.8)], "punch_yellow"), "utf-8")
    graph = build_graph(shots, edges, VERTICAL, video.fps, treatment, ass, None)
    assert len(graph) > ARGV_LIMIT  # the part that used to ride on the command line

    out_dir = home / "jobs" / "job_20261109_deep" / "shorts" / "s1"
    raw = ff.make_tone_wav(out_dir / "audio_raw.wav", 6.4, sr=48000)
    audio = raw.with_name("audio.wav")
    loudnorm(raw, audio, i=FINAL_LUFS, tp=FINAL_TP)
    out = out_dir / "short.mp4"
    commands.clear()
    render_video(
        shots=shots, audio=audio, out=out, size=VERTICAL, treatment=treatment, video=video, ass_path=ass,
    )
    (cmd,) = commands
    assert _cmd_chars(cmd) < ARGV_LIMIT, _cmd_chars(cmd)
    script = out.with_name("short.filtergraph.txt")
    assert script.read_text(encoding="utf-8") == graph
    assert graph not in cmd and cmd.count("-i") == CLIPS + 1
    result = run_qc("s1", out, "short", drift_ms=0.0, first_caption_s=0.0)
    assert result.passed, result.reason


@needs_ffmpeg
def test_the_command_does_not_grow_with_the_path_depth(tmp_path: Path, commands: list[list[str]]):
    sizes: list[int] = []
    for name in ("a", "b" * 100):
        home = tmp_path / name
        home.mkdir()
        shots = _shots(home, 3, 0.3)
        raw = ff.make_tone_wav(home / "audio.wav", 0.9, sr=48000)
        commands.clear()
        render_video(
            shots=shots, audio=raw, out=home / "o" / "short.mp4", size=VERTICAL,
            treatment=visual_treatment("clean_bright"), video=VideoCfg(encoder="libx264", preset="ultrafast"),
            ass_path=None,
        )
        sizes.append(_cmd_chars(commands[0]))
    assert sizes[0] == sizes[1]


@needs_ffmpeg
def test_chapter_segment_uses_a_script_and_short_paths(
    tmp_home: Path, tmp_path: Path, commands: list[list[str]], monkeypatch: pytest.MonkeyPatch
):
    from tests.helpers import make_combo, make_ctx

    ctx = make_ctx(tmp_home, fmt="master_first", combo=make_combo(caption_style="clean_white"))
    monkeypatch.setattr(longform, "fit_chapter_contrast", lambda *a, **k: [])
    home = _deep_home(tmp_path)
    shots = _shots(home, 6, 0.5)
    ass = ctx.file("captions/long_c1.ass")
    ass.parent.mkdir(parents=True, exist_ok=True)
    ass.write_text(build_ass([Word("hello", 0.0, 0.3)], "clean_white"), encoding="utf-8")
    t = ChapterTiming(1, "One", 0.0, 0.0, 3.0)
    commands.clear()
    out = render_segment(
        ctx, t, shots, visual_treatment("cinematic_teal"), chapter_card("midnight"),
        home / "long" / "seg_1.mp4",
    )
    (cmd,) = commands
    assert _cmd_chars(cmd) < ARGV_LIMIT, _cmd_chars(cmd)
    assert (home / "long" / "seg_1.filtergraph.txt").is_file() and out.is_file()
    assert ff.video_size(out) == (1920, 1080)


@needs_ffmpeg
def test_clip_render_keeps_its_graph_in_a_script(tmp_path: Path, commands: list[list[str]]):
    from ns_studio.clipper import render as rd
    from tests.test_clipper_render import spec_for

    out = rd.render_clip(spec_for("center_crop", tmp_path / "c1.mp4"))
    assert out.exists()
    render_cmds = [c for c in commands if str(out.name) in c[-1]]
    assert render_cmds and all("-filter_complex" not in c for c in render_cmds)
    assert (tmp_path / "c1.filtergraph.txt").is_file()
