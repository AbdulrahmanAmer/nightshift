"""H-2 and P46b: every path in a filter graph or a concat list is absolute; the command stays bounded.

FFmpeg starts in the folder the job files share (PR 119), so a path written INSIDE the graph (the ASS captions
file, the title card, the fonts folder) or in a concat list must not depend on the folder the Nightshift
process was started in. With a relative `NIGHTSHIFT_HOME` it used to resolve against the wrong folder and
the render failed. These tests run on a fake FFmpeg runner: they check the text FFmpeg would be given.
"""

from __future__ import annotations

import math
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from ns_common import ffmpeg as ff
from ns_common.models import VideoCfg
from ns_studio.clipper import render as clipper_render
from ns_studio.media import longform
from ns_studio.media.longform import concat_list_text, render_segment
from ns_studio.media.master import ChapterTiming
from ns_studio.media.presets import FONTS_DIR, chapter_card, load_visual_treatments, visual_treatment
from ns_studio.media.render import VERTICAL, escape_filter_path, render_video
from ns_studio.media.shotplan import Shot, plan_slots

CMD_LIMIT = 24_000  # the Head's bound; Windows refuses 32,767 (WinError 206)
MASTER_S = 14 * 60 + 30.0
HOME_CHARS = 200


def _cmd_chars(cmd: list[str]) -> int:
    """What Windows counts: the command line string CreateProcess receives."""
    return len(subprocess.list2cmdline(cmd))


def _filter_text(path: Path) -> str:
    """How a path must appear inside a filter graph: absolute, forward slashes, the drive colon escaped."""
    return "'" + str(path).replace("\\", "/").replace(":", "\\:") + "'"


class FakeFfmpeg:
    """Stands in for `ff.run`: records the command and the folder it would start in, writes nothing."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], Path | None]] = []

    def run(self, args: list[str], *, cwd: Path | None = None, **_kw: Any) -> None:
        self.calls.append((list(args), cwd))


@pytest.fixture
def fake_ff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeFfmpeg:
    fake = FakeFfmpeg()
    monkeypatch.setattr(ff, "run", fake.run)
    monkeypatch.setattr(ff, "ffmpeg_exe", lambda: str(tmp_path / "bin" / "ffmpeg.exe"))
    monkeypatch.setattr(ff, "_reads_filter_script_option", lambda _exe: True)
    return fake


def _shot(path: Path, start: float, end: float) -> Shot:
    return Shot(0, start, end, "fake", path.stem, str(path), 0.0, 1920, 1080, "u", "l", "kw")


def _graph_of(args: list[str], start_dir: Path) -> str:
    """The filter graph FFmpeg would read: the script named after `-filter_complex_script`, from the folder
    FFmpeg starts in."""
    option = "-filter_complex_script" if "-filter_complex_script" in args else "-/filter_complex"
    return (start_dir / args[args.index(option) + 1]).read_text(encoding="utf-8")


# ---------------------------------------------------------------- H-2: relative home
def test_short_graph_paths_are_absolute_with_a_relative_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_ff: FakeFfmpeg
):
    work = tmp_path / "work"
    (work / "nshome" / "fonts").mkdir(parents=True)
    monkeypatch.chdir(work)  # the process folder is NOT the folder FFmpeg will start in
    here = Path.cwd()
    home = Path("nshome")
    ass = home / "jobs" / "j1" / "captions" / "s1.ass"
    clips = home / "cache" / "clips" / "pexels"
    shots = [_shot(clips / f"{i}.mp4", i * 2.0, i * 2.0 + 2.0) for i in range(3)]
    render_video(
        shots=shots,
        audio=home / "jobs" / "j1" / "audio.wav",
        out=home / "jobs" / "j1" / "short.mp4",
        size=VERTICAL,
        treatment=visual_treatment("cinematic_teal"),
        video=VideoCfg(encoder="libx264", preset="veryfast"),
        ass_path=ass,
        fonts_dir=home / "fonts",
    )
    ((args, cwd),) = fake_ff.calls
    assert cwd is not None and cwd.is_absolute() and cwd == here / "nshome"
    graph = _graph_of(args, cwd)
    assert f"ass=filename={_filter_text(here / 'nshome' / 'jobs' / 'j1' / 'captions' / 's1.ass')}" in graph
    assert f"fontsdir={_filter_text(here / 'nshome' / 'fonts')}" in graph
    assert "filename='nshome" not in graph and "fontsdir='nshome" not in graph
    # the files named on the command line resolve from the folder FFmpeg starts in
    inputs = [args[i + 1] for i, a in enumerate(args) if a == "-i"]
    assert [(cwd / n).resolve() for n in inputs[:3]] == [
        (here / "nshome" / "cache" / "clips" / "pexels" / f"{i}.mp4").resolve() for i in range(3)
    ]


def test_chapter_graph_paths_are_absolute_with_a_relative_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_ff: FakeFfmpeg
):
    from tests.helpers import make_combo, make_ctx

    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setenv("NIGHTSHIFT_HOME", "nshome")  # relative, as `set NIGHTSHIFT_HOME=.` would leave it
    ctx = make_ctx(Path("nshome"), fmt="master_first", combo=make_combo(caption_style="clean_white"))
    assert not ctx.paths.home.is_absolute()
    monkeypatch.setattr(longform, "fit_chapter_contrast", lambda *a, **k: [])
    ctx.subdir("captions")
    shots = [_shot(ctx.paths.cache_clips / "pexels" / f"{i}.mp4", 3.0 + i * 6, 9.0 + i * 6) for i in range(2)]
    t = ChapterTiming(1, "One", 0.0, 3.0, 15.0)  # a 3 s title card, then 12 s of shots
    render_segment(
        ctx,
        t,
        shots,
        visual_treatment("cinematic_teal"),
        chapter_card("midnight"),
        ctx.subdir("long") / "seg_1.mp4",
    )
    ((args, cwd),) = fake_ff.calls
    assert cwd is not None and cwd.is_absolute()
    here = Path.cwd()
    graph = _graph_of(args, cwd)
    captions = here / ctx.job_dir / "captions"  # `ctx.job_dir` is relative: nshome/data/jobs/<id>
    assert f"ass=filename={_filter_text(captions / 'long_c1.ass')}" in graph  # the chapter captions
    assert f"ass=filename={_filter_text(captions / 'card_c1.ass')}" in graph  # the title card
    assert "filename='nshome" not in graph
    if FONTS_DIR.is_dir():  # the repo ships a fonts folder; its path is absolute whatever the working folder
        assert f"fontsdir={_filter_text(Path(os.path.abspath(FONTS_DIR)))}" in graph


def test_escape_filter_path_makes_a_relative_path_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    relative = escape_filter_path(Path("home") / "it's" / "cap.ass")
    assert relative == escape_filter_path(tmp_path / "home" / "it's" / "cap.ass")  # escaping is unchanged
    assert not relative.startswith("'home") and relative.endswith("cap.ass'")


def test_clipper_filter_path_is_absolute_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    assert (
        clipper_render.filter_path(Path("nshome") / "c1.ass")
        == _filter_text(tmp_path / "nshome" / "c1.ass")[1:-1]
    )


def test_concat_list_names_absolute_files_with_a_relative_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The concat demuxer reads a relative name against the LIST's folder: a relative one is wrong twice."""
    monkeypatch.chdir(tmp_path)
    text = concat_list_text([Path("nshome/long/seg_1.mp4"), Path("nshome/long/it's/seg_2.mp4")])
    first = (tmp_path / "nshome" / "long" / "seg_1.mp4").as_posix()
    second = (tmp_path / "nshome" / "long" / "it's" / "seg_2.mp4").as_posix()
    assert text.splitlines() == [f"file '{first}'", "file '" + second.replace("'", "'\\''") + "'"]


# ---------------------------------------------------------------- P46b: the largest real case
def _home_of(tmp_path: Path, chars: int) -> Path:
    pad = chars - len(str(tmp_path)) - 1
    assert pad >= 8, "the test folder is already deeper than the home it should simulate"
    home = tmp_path / ("h" * pad)
    assert len(str(home)) == chars
    return home


def _shortest_long_clip_s() -> float:
    """The shortest clip the planner cuts for a master: the low end of the narrowest `long_cut_range_s`."""
    return min(t.long_cut_range_s[0] for t in load_visual_treatments().values())


def test_a_master_at_the_shortest_clip_length_has_174_clips():
    lo = _shortest_long_clip_s()
    slots = plan_slots([(0, 0.0, MASTER_S)], (lo, lo))
    assert lo == 5.0 and len(slots) == math.floor(MASTER_S / lo) == 174


def test_the_longest_master_render_command_stays_bounded_under_a_200_character_home(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_ff: FakeFfmpeg
):
    from tests.helpers import make_combo, make_ctx

    ctx = make_ctx(tmp_home, fmt="master_first", combo=make_combo(caption_style="clean_white"))
    lo = _shortest_long_clip_s()
    count = len(plan_slots([(0, 0.0, MASTER_S)], (lo, lo)))
    home = _home_of(tmp_path, HOME_CHARS)
    exe = home / "bin" / "ffmpeg" / "ffmpeg.exe"
    monkeypatch.setattr(ff, "ffmpeg_exe", lambda: str(exe))
    monkeypatch.setattr(longform, "fit_chapter_contrast", lambda *a, **k: [])
    clips = home / "cache" / "clips" / "pexels"
    shots = [_shot(clips / f"{1_000_000 + i}.mp4", i * lo, (i + 1) * lo) for i in range(count)]
    job = home / "jobs" / "job_20261109_aaaa"

    def started(args: list[str]) -> list[str]:
        return [os.path.abspath(str(exe)), "-hide_banner", "-y", "-nostdin", *args]  # what `ff.run` starts

    # the whole master as ONE chapter is the largest chapter the render can make: 174 clips (H-5: they are
    # rendered in parts of at most ff.MAX_CLIP_INPUTS, so no command carries them all)
    t = ChapterTiming(1, "One", 0.0, 0.0, MASTER_S)
    render_segment(
        ctx,
        t,
        shots,
        visual_treatment("cinematic_teal"),
        chapter_card("midnight"),
        job / "long" / "seg_1.mp4",
    )
    calls = list(fake_ff.calls)
    assert all(cwd is not None for _args, cwd in calls)
    assert sum(args.count("-ss") for args, _cwd in calls) == count == 174
    assert max(args.count("-ss") for args, _cwd in calls) == ff.MAX_CLIP_INPUTS
    chars = max(_cmd_chars(started(args)) for args, _cwd in calls)
    assert chars < CMD_LIMIT, f"{count} clips under a {HOME_CHARS}-character home: {chars} characters"
    # the same clips through the Short renderer stay bounded as well
    fake_ff.calls.clear()
    render_video(
        shots=shots,
        audio=job / "long" / "audio.wav",
        out=job / "long" / "master.mp4",
        size=(1920, 1080),
        treatment=visual_treatment("cinematic_teal"),
        video=VideoCfg(encoder="libx264", preset="veryfast"),
        ass_path=None,
    )
    assert max(_cmd_chars(started(args)) for args, _cwd in fake_ff.calls) < CMD_LIMIT
