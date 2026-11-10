"""H-5 (P46): the FFmpeg command of a render is the same size for 16 clips and for 1,000.

PR 135 made every path in a graph absolute and found the rest: the clip count has no upper bound (at least one
slot per beat), and each clip still put `-ss S -t T -i FILE` (about 60 characters) on the command line. A long
master on a deep home could still cross Windows' 32,767 characters (NS-E327). The decision: no planner cap,
the command must not grow with the clip count. A render with more than `MAX_CLIP_INPUTS` clips is cut into
parts of at most that many; each part is one bounded command that writes a video file, and the parts are
joined through a concat-demuxer list file (`file` and `duration` lines) in the job's render folder.

The first half of this file runs on a fake FFmpeg runner (it checks the text FFmpeg would be given). The
second half renders for real on synthetic clips and compares the part render with the one-command render:
frame count, duration, the QC report and the pictures (PSNR). Those tests need FFmpeg; CI has it, and a
laptop runs them only while holding the laptop lock.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.models import VideoCfg
from ns_studio.media import audio as audio_mod
from ns_studio.media import longform
from ns_studio.media.audio import FINAL_LUFS, FINAL_TP, assemble_audio, loudnorm
from ns_studio.media.captions import build_ass
from ns_studio.media.longform import render_segment
from ns_studio.media.master import ChapterTiming
from ns_studio.media.presets import chapter_card, visual_treatment
from ns_studio.media.qc import run_qc
from ns_studio.media.render import VERTICAL, build_graph, post_chain, render_video, shot_chain
from ns_studio.media.shotplan import Shot, frame_edges
from ns_studio.media.transcribe import Word

needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")
ARGV_LIMIT = 4000  # the Head's bound for one command under a 200-character home
HOME_CHARS = 200
FPS = 30
COUNTS = (16, 200, 1000)


def _cmd_chars(cmd: list[str]) -> int:
    """What Windows counts: the command line string CreateProcess receives."""
    return len(subprocess.list2cmdline(cmd))


class FakeFfmpeg:
    """Stands in for `ff.run`: records every command with the folder it starts in, and the files it reads.

    `scripts` holds the text of each `-filter_complex_script` file at the moment of the call, and `lists` the
    text of each concat list; the render folder is cleaned up afterwards, so the test cannot read them later.
    """

    def __init__(self, fail: Callable[[list[str]], bool] | None = None, write_outputs: bool = False) -> None:
        self.calls: list[tuple[list[str], Path | None]] = []
        self.scripts: list[str] = []
        self.lists: list[str] = []
        self.fail = fail
        self.write_outputs = write_outputs

    def run(self, args: list[str], *, cwd: Path | None = None, **_kw: Any) -> None:
        self.calls.append((list(args), cwd))
        here = cwd if cwd is not None else Path.cwd()
        for option in ("-filter_complex_script", "-/filter_complex"):
            if option in args:
                self.scripts.append((here / args[args.index(option) + 1]).read_text(encoding="utf-8"))
        if "concat" in args:
            name = args[args.index("-i") + 1]
            self.lists.append((here / name).read_text(encoding="utf-8"))
        if self.fail is not None and self.fail(args):
            raise NsError("NS-E401", "the encoder refused", cmd=args)
        if self.write_outputs:
            out = here / args[-1]
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"x")


@pytest.fixture
def fake_ff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeFfmpeg:
    fake = FakeFfmpeg()
    monkeypatch.setattr(ff, "run", fake.run)
    monkeypatch.setattr(ff, "ffmpeg_exe", lambda: str(tmp_path / "bin" / "ffmpeg.exe"))
    monkeypatch.setattr(ff, "_reads_filter_script_option", lambda _exe: True)
    return fake


def _home_of(tmp_path: Path, chars: int) -> Path:
    pad = chars - len(str(tmp_path)) - 1
    assert pad >= 8, "the test folder is already deeper than the home it should simulate"
    home = tmp_path / ("h" * pad)
    assert len(str(home)) == chars
    return home


def _shot(path: Path | str, start: float, end: float, w: int = 1920, h: int = 1080) -> Shot:
    return Shot(0, start, end, "fake", Path(path).stem, str(path), 0.0, w, h, "u", "l", "kw")


def _fake_shots(home: Path, count: int, each_s: float = 2.0) -> list[Shot]:
    clips = home / "cache" / "clips" / "pexels"
    return [_shot(clips / f"{1_000_000 + i}.mp4", i * each_s, (i + 1) * each_s) for i in range(count)]


def _started(args: list[str], exe: Path) -> list[str]:
    """The command `ff.run` starts for `args` when it is given a `cwd`."""
    return [os.path.abspath(str(exe)), "-hide_banner", "-y", "-nostdin", *args]


def _render_short(
    shots: list[Shot], home: Path, *, max_inputs: int, encoder: str = "libx264", ass: Path | None = None
) -> Path:
    job = home / "jobs" / "job_20261109_aaaa" / "shorts" / "s1"
    return render_video(
        shots=shots,
        audio=job / "audio.wav",
        out=job / "short.mp4",
        size=VERTICAL,
        treatment=visual_treatment("cinematic_teal"),
        video=VideoCfg(encoder=encoder, preset="veryfast"),
        ass_path=ass,
        max_inputs=max_inputs,
    )


# ------------------------------------------------------------------ the helpers
def test_group_ranges_cover_the_items_in_order():
    assert ff.group_ranges(0, 12) == []
    assert ff.group_ranges(5, 12) == [(0, 5)]
    assert ff.group_ranges(12, 12) == [(0, 12)]
    assert ff.group_ranges(25, 12) == [(0, 12), (12, 24), (24, 25)]
    with pytest.raises(ValueError):
        ff.group_ranges(3, 0)


def test_concat_list_names_absolute_files_and_their_durations(tmp_path: Path):
    a, b = tmp_path / "p1.mp4", tmp_path / "it's" / "p2.mp4"
    text = ff.concat_list_text([a, b], [12.4666667, 3.0])
    assert text.splitlines() == [
        f"file '{a.as_posix()}'",
        "duration 12.466667",
        "file '" + b.as_posix().replace("'", "'\\''") + "'",
        "duration 3.000000",
    ]
    assert ff.concat_list_text([a]) == f"file '{a.as_posix()}'\n"  # without durations: file lines only
    with pytest.raises(ValueError):
        ff.concat_list_text([a, b], [1.0])


def test_the_defaults_keep_a_command_far_under_the_windows_limit():
    # 12 clips of about 60 characters each plus a script, an output and an executable path
    assert 1 <= ff.MAX_CLIP_INPUTS <= 16
    assert ff.MAX_AUDIO_INPUTS >= 2 * 7 - 1  # a master of MAX_CHAPTERS chapters assembles in one command


def test_post_chain_shifts_the_clock_only_around_the_captions(tmp_path: Path):
    t = visual_treatment("cinematic_teal")
    ass = tmp_path / "cap.ass"
    plain = post_chain(t, ass, None)
    assert post_chain(t, ass, None, ass_offset_s=0.0) == plain  # the first part is the whole-video chain
    shifted = post_chain(t, ass, None, ass_offset_s=12.4666667)
    shift = "round(12.466667/TB)"
    assert f"setpts=PTS+{shift},ass=filename=" in shifted
    assert shifted.endswith(f",setpts=PTS-{shift},format=yuv420p")
    assert shifted.replace(f"setpts=PTS+{shift},", "").replace(f",setpts=PTS-{shift}", "") == plain
    assert post_chain(t, None, None, ass_offset_s=5.0) == post_chain(t, None, None)  # no captions, no shift


def test_shot_chain_takes_the_zoom_phase_from_the_shot_not_the_input_slot():
    z = visual_treatment("handheld_grit").zoom
    shot = _shot("a.mp4", 0.0, 2.0)
    same = shot_chain(7, shot, 1080, 1920, FPS, 60, z)
    moved = shot_chain(2, shot, 1080, 1920, FPS, 60, z, phase=7)
    assert "[7:v]" in same and "[2:v]" in moved and "[v2]" in moved
    phase = lambda g: re.findall(r"sin\(on/9\+([0-9.]+)\)", g)  # noqa: E731
    assert phase(same) == phase(moved) == [str(round(7 * 1.7, 1))]


# ------------------------------------------------------------------ the command size
def test_the_short_command_is_the_same_size_for_16_200_and_1000_clips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_ff: FakeFfmpeg
):
    home = _home_of(tmp_path, HOME_CHARS)
    exe = home / "bin" / "ffmpeg" / "ffmpeg.exe"
    monkeypatch.setattr(ff, "ffmpeg_exe", lambda: str(exe))
    longest: dict[int, int] = {}
    commands: dict[int, int] = {}
    for count in COUNTS:
        fake_ff.calls.clear()
        _render_short(_fake_shots(home, count), home, max_inputs=ff.MAX_CLIP_INPUTS)
        sizes = [_cmd_chars(_started(args, exe)) for args, _cwd in fake_ff.calls]
        longest[count], commands[count] = max(sizes), len(fake_ff.calls)
        inputs = [a.count("-i") for a, _ in fake_ff.calls]
        assert max(inputs) <= ff.MAX_CLIP_INPUTS + 1, inputs  # the join also names its list and the audio
    print("Short, longest command by clip count:", longest, "commands:", commands)  # noqa: T201
    assert all(size < ARGV_LIMIT for size in longest.values()), longest
    assert max(longest.values()) - min(longest.values()) <= 4, longest  # the same number, within the digits
    assert commands == {n: math.ceil(n / ff.MAX_CLIP_INPUTS) + 1 for n in COUNTS}  # parts, then the join


def test_the_command_of_the_old_render_grew_with_the_clip_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_ff: FakeFfmpeg
):
    """The 'before' column: one command for all the clips (`max_inputs` large) grows ~60 characters a clip."""
    home = _home_of(tmp_path, HOME_CHARS)
    exe = home / "bin" / "ffmpeg" / "ffmpeg.exe"
    monkeypatch.setattr(ff, "ffmpeg_exe", lambda: str(exe))
    sizes: dict[int, int] = {}
    for count in COUNTS:
        fake_ff.calls.clear()
        _render_short(_fake_shots(home, count), home, max_inputs=10**6)
        ((args, _cwd),) = fake_ff.calls
        sizes[count] = _cmd_chars(_started(args, exe))
    print("Short, one command for every clip:", sizes)  # noqa: T201
    assert sizes[200] - sizes[16] > 8_000 and sizes[1000] > 50_000 > ARGV_LIMIT


def test_the_chapter_command_is_the_same_size_for_16_200_and_1000_clips(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_ff: FakeFfmpeg
):
    from tests.helpers import make_combo, make_ctx

    ctx = make_ctx(tmp_home, fmt="master_first", combo=make_combo(caption_style="clean_white"))
    ctx.subdir("captions")
    monkeypatch.setattr(longform, "fit_chapter_contrast", lambda *a, **k: [])
    home = _home_of(tmp_path, HOME_CHARS)
    exe = home / "bin" / "ffmpeg" / "ffmpeg.exe"
    monkeypatch.setattr(ff, "ffmpeg_exe", lambda: str(exe))
    longest: dict[int, int] = {}
    for count in COUNTS:
        fake_ff.calls.clear()
        shots = [_shot(s.path, 3.0 + s.start * 3, 3.0 + s.end * 3) for s in _fake_shots(home, count)]
        t = ChapterTiming(1, "One", 0.0, 3.0, 3.0 + count * 6.0)  # a 3 s title card, then count * 6 s
        render_segment(
            ctx,
            t,
            shots,
            visual_treatment("cinematic_teal"),
            chapter_card("midnight"),
            home / "jobs" / "job_20261109_aaaa" / "long" / "seg_1.mp4",
        )
        sizes = [_cmd_chars(_started(args, exe)) for args, _cwd in fake_ff.calls]
        longest[count] = max(sizes)
    print("Chapter, longest command by clip count:", longest)  # noqa: T201
    assert all(size < ARGV_LIMIT for size in longest.values()), longest
    assert max(longest.values()) - min(longest.values()) <= 4, longest


# ------------------------------------------------------------------ what the parts contain
def test_every_part_is_its_own_bounded_command_and_the_join_names_them_in_order(
    tmp_path: Path, fake_ff: FakeFfmpeg
):
    home = _home_of(tmp_path, HOME_CHARS)
    shots = _fake_shots(home, 30)
    (job := home / "jobs" / "job_20261109_aaaa" / "shorts" / "s1").mkdir(parents=True)
    ass = job / "s1.ass"
    ass.write_text("x", encoding="utf-8")
    _render_short(shots, home, max_inputs=12, ass=ass)
    parts, join = fake_ff.calls[:-1], fake_ff.calls[-1][0]
    assert [a.count("-ss") for a, _ in parts] == [12, 12, 6]
    assert all("-an" in a and "[vout]" in a and "-filter_complex_script" in a for a, _ in parts)
    assert join[:5] == ["-f", "concat", "-safe", "0", "-i"] and "-c:v" in join
    assert join[join.index("-c:v") + 1] == "copy" and "-map" in join and "1:a" in join
    listing = fake_ff.lists[0].splitlines()
    assert [ln.split("/")[-1] for ln in listing[0::2]] == ["part0000.mp4'", "part0001.mp4'", "part0002.mp4'"]
    edges = frame_edges([s.end for s in shots], FPS)
    want = [(edges[b] - edges[a]) / FPS for a, b in ff.group_ranges(30, 12)]
    got = [float(ln.split()[1]) for ln in listing[1::2]]
    assert got == pytest.approx(want, abs=1e-6) and sum(got) == pytest.approx(edges[-1] / FPS, abs=1e-5)


def test_a_long_render_logs_how_it_was_cut(tmp_home: Path, tmp_path: Path, fake_ff: FakeFfmpeg):
    log = NsLogger("test", tmp_home / "logs")
    home = _home_of(tmp_path, HOME_CHARS)
    job = home / "jobs" / "job_20261109_aaaa" / "shorts" / "s1"
    render_video(
        shots=_fake_shots(home, 30),
        audio=job / "audio.wav",
        out=job / "short.mp4",
        size=VERTICAL,
        treatment=visual_treatment("cinematic_teal"),
        video=VideoCfg(encoder="libx264", preset="veryfast"),
        ass_path=None,
        log=log,
        max_inputs=12,
    )
    records = [
        json.loads(x) for f in (tmp_home / "logs").glob("*.jsonl") for x in f.read_text("utf-8").splitlines()
    ]
    (rec,) = [r for r in records if r["event"] == "render_parts"]
    assert (rec["data"]["clips"], rec["data"]["parts"], rec["data"]["max_inputs"]) == (30, 3, 12)


def test_part_graphs_are_the_slices_of_the_whole_graph(tmp_path: Path, fake_ff: FakeFfmpeg):
    """Each part draws its shots exactly as the one graph does and shows the right captions."""
    home = _home_of(tmp_path, HOME_CHARS)
    shots = _fake_shots(home, 30)
    sizes = [(1080, 1920) if i % 3 else (1920, 1080) for i in range(len(shots))]
    shots = [_shot(s.path, s.start, s.end, *wh) for s, wh in zip(shots, sizes, strict=True)]
    (job := home / "jobs" / "job_20261109_aaaa" / "shorts" / "s1").mkdir(parents=True)
    ass = job / "s1.ass"
    ass.write_text("x", encoding="utf-8")
    treatment = visual_treatment("handheld_grit")
    out = job / "short.mp4"
    render_video(
        shots=shots,
        audio=job / "audio.wav",
        out=out,
        size=VERTICAL,
        treatment=treatment,
        video=VideoCfg(encoder="libx264", preset="veryfast"),
        ass_path=ass,
        max_inputs=12,
    )
    edges = frame_edges([s.end for s in shots], FPS)
    whole = build_graph(shots, edges, VERTICAL, FPS, treatment, ass, None)
    graphs = fake_ff.scripts
    assert len(graphs) == 3
    phase = lambda g: re.findall(r"sin\(on/9\+([0-9.]+)\)", g)  # noqa: E731
    blur = lambda g: g.count("boxblur")  # noqa: E731
    assert sum((phase(g) for g in graphs), []) == phase(whole)  # the handheld drift continues, never restarts
    assert sum(blur(g) for g in graphs) == blur(whole)
    for k, (a, b) in enumerate(ff.group_ranges(30, 12)):
        g = graphs[k]
        assert f"concat=n={b - a}:v=1:a=0[cat]" in g and g.endswith("format=yuv420p[vout]")
        if k == 0:
            assert "setpts=PTS+round" not in g
        else:  # the captions file speaks in whole-video time; this part's clock starts at its first frame
            assert f"setpts=PTS+round({edges[a] / FPS:.6f}/TB),ass=" in g
            assert f",setpts=PTS-round({edges[a] / FPS:.6f}/TB),format=yuv420p" in g


def test_parts_name_absolute_files_in_graphs_and_lists_with_a_relative_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_ff: FakeFfmpeg
):
    """H-2 for the parts: a relative `NIGHTSHIFT_HOME` must not reach the text inside a graph or a list."""
    work = tmp_path / "work"
    (work / "nshome" / "fonts").mkdir(parents=True)
    monkeypatch.chdir(work)
    here = Path.cwd()
    home = Path("nshome")
    ass = home / "jobs" / "j1" / "captions" / "s1.ass"
    shots = _fake_shots(home, 30)
    render_video(
        shots=shots,
        audio=home / "jobs" / "j1" / "audio.wav",
        out=home / "jobs" / "j1" / "short.mp4",
        size=VERTICAL,
        treatment=visual_treatment("cinematic_teal"),
        video=VideoCfg(encoder="libx264", preset="veryfast"),
        ass_path=ass,
        fonts_dir=home / "fonts",
        max_inputs=12,
    )
    assert all(cwd is not None and cwd.is_absolute() for _args, cwd in fake_ff.calls)
    wanted = (
        "'" + (here / "nshome" / "jobs" / "j1" / "captions" / "s1.ass").as_posix().replace(":", "\\:") + "'"
    )
    assert len(fake_ff.scripts) == 3
    for graph in fake_ff.scripts:
        assert f"ass=filename={wanted}" in graph and "filename='nshome" not in graph
    (listing,) = fake_ff.lists
    names = [ln[len("file '") : -1] for ln in listing.splitlines() if ln.startswith("file ")]
    assert len(names) == 3 and all(n.startswith((here / "nshome").as_posix()) for n in names)


def test_a_chapter_with_a_card_continues_the_clock_after_the_card(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_ff: FakeFfmpeg
):
    from tests.helpers import make_combo, make_ctx

    ctx = make_ctx(tmp_home, fmt="master_first", combo=make_combo(caption_style="clean_white"))
    ctx.subdir("captions")
    monkeypatch.setattr(longform, "fit_chapter_contrast", lambda *a, **k: [])
    home = _home_of(tmp_path, HOME_CHARS)
    shots = [_shot(s.path, 3.0 + s.start * 3, 3.0 + s.end * 3) for s in _fake_shots(home, 30)]
    t = ChapterTiming(1, "One", 0.0, 3.0, 3.0 + 30 * 6.0)
    render_segment(
        ctx,
        t,
        shots,
        visual_treatment("handheld_grit"),
        chapter_card("midnight"),
        home / "long" / "seg_1.mp4",
    )
    graphs = fake_ff.scripts
    assert len(graphs) == 3
    first, second, third = graphs
    assert first.startswith("[0:v]format=yuv420p,ass=filename=") and "[vc][vs]concat=n=2:v=1:a=0[cc]" in first
    assert "setpts=PTS+round" not in first
    for g, start in ((second, 3.0 + 12 * 6.0), (third, 3.0 + 24 * 6.0)):  # the card, then the shots before
        assert "[vc]" not in g and "[0:v]" in g
        assert f"setpts=PTS+round({start:.6f}/TB),ass=" in g
    # the phase of the handheld drift is the one the whole chapter would give (shot k sits on input k + 1)
    phases = [re.findall(r"sin\(on/9\+([0-9.]+)\)", g) for g in graphs]
    assert [p for ps in phases for p in ps] == [str(round((k + 1) * 1.7, 1)) for k in range(30)]
    join = fake_ff.calls[-1][0]
    assert join[:5] == ["-f", "concat", "-safe", "0", "-i"] and "copy" in join and "-an" in join


def test_a_failed_nvenc_part_renders_every_part_again_with_libx264(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Parts of two encoders cannot be joined by copying, so the fall-back redoes all of them."""
    fake = FakeFfmpeg(fail=lambda args: "h264_nvenc" in args and args[-1].endswith("part0001.mp4"))
    monkeypatch.setattr(ff, "run", fake.run)
    monkeypatch.setattr(ff, "ffmpeg_exe", lambda: str(tmp_path / "bin" / "ffmpeg.exe"))
    monkeypatch.setattr(ff, "_reads_filter_script_option", lambda _exe: True)
    home = _home_of(tmp_path, HOME_CHARS)
    _render_short(_fake_shots(home, 30), home, max_inputs=12, encoder="h264_nvenc")
    encoders = [a[a.index("-c:v") + 1] for a, _ in fake.calls if "-an" in a]
    assert encoders == ["h264_nvenc", "h264_nvenc", "libx264", "libx264", "libx264"]
    assert fake.calls[-1][0][fake.calls[-1][0].index("-c:v") + 1] == "copy"


def test_a_full_disk_is_not_retried_with_another_encoder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    def run(args: list[str], *, cwd: Path | None = None, **_kw: Any) -> None:
        calls.append(args)
        raise NsError("NS-E401", "No space left on device")

    monkeypatch.setattr(ff, "run", run)
    monkeypatch.setattr(ff, "ffmpeg_exe", lambda: str(tmp_path / "bin" / "ffmpeg.exe"))
    monkeypatch.setattr(ff, "_reads_filter_script_option", lambda _exe: True)
    home = _home_of(tmp_path, HOME_CHARS)
    with pytest.raises(NsError) as ei:
        _render_short(_fake_shots(home, 30), home, max_inputs=12, encoder="h264_nvenc")
    assert ei.value.code == "NS-E104" and len(calls) == 1


def test_the_parts_are_removed_after_a_good_render_and_their_videos_after_a_bad_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = _home_of(tmp_path, HOME_CHARS)
    folder = home / "jobs" / "job_20261109_aaaa" / "shorts" / "s1" / "short.parts"
    for fail_at in (None, 3):
        seen = {"n": 0}

        def fail(_args: list[str], seen: dict[str, int] = seen, fail_at: int | None = fail_at) -> bool:
            seen["n"] += 1
            return seen["n"] == fail_at

        fake = FakeFfmpeg(fail=fail, write_outputs=True)
        monkeypatch.setattr(ff, "run", fake.run)
        monkeypatch.setattr(ff, "ffmpeg_exe", lambda: str(tmp_path / "bin" / "ffmpeg.exe"))
        monkeypatch.setattr(ff, "_reads_filter_script_option", lambda _exe: True)
        shots = _fake_shots(home, 30)
        if fail_at is None:
            _render_short(shots, home, max_inputs=12)
            assert not folder.exists()  # nothing of the parts is left next to short.mp4
        else:
            with pytest.raises(NsError):
                _render_short(shots, home, max_inputs=12)
            assert not list(folder.glob("*.mp4"))  # the big files go; the graphs stay for the failure report
            assert (
                sorted(p.name for p in folder.glob("*.filtergraph.txt"))
                == [
                    "part0000.filtergraph.txt",
                    "part0001.filtergraph.txt",
                    "part0002.filtergraph.txt",
                ][:fail_at]
            )


def test_audio_assembled_from_many_files_is_cut_into_bounded_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake = FakeFfmpeg(write_outputs=True)
    monkeypatch.setattr(ff, "run", fake.run)
    home = _home_of(tmp_path, HOME_CHARS)
    segments: list[Path | float] = [
        home / "audio" / f"p{i}.wav" if i % 2 == 0 else 0.5 for i in range(1, 100)
    ]
    out = assemble_audio(segments, home / "audio" / "raw.wav", max_inputs=24)
    assert out == home / "audio" / "raw.wav"
    parts, join = fake.calls[:-1], fake.calls[-1][0]
    assert len(parts) == math.ceil(99 / 24) and all(a.count("-i") <= 24 for a, _ in parts)
    assert max(_cmd_chars(a) for a, _ in fake.calls) < 12_000  # 24 absolute 200-character paths at most
    assert join[:5] == ["-f", "concat", "-safe", "0", "-i"] and join[-1].endswith("raw.wav")
    assert audio_mod.ff is ff and not (home / "audio" / "raw.parts").exists()


def test_a_short_list_of_audio_is_still_one_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    fake = FakeFfmpeg(write_outputs=True)
    monkeypatch.setattr(ff, "run", fake.run)
    assemble_audio([tmp_path / "hook.wav", 0.4, tmp_path / "body.wav"], tmp_path / "out" / "raw.wav")
    assert len(fake.calls) == 1 and "-filter_complex" in fake.calls[0][0]


# ------------------------------------------------------------------ real FFmpeg: the same video, both ways
def _clip_folder(home: Path) -> Path:
    folder = home / "jobs" / "job_20261109_real" / "clips"
    folder.mkdir(parents=True)
    return folder


def _real_shots(
    home: Path, count: int, each_s: float, *, wide: bool = False, mixed: bool = False
) -> list[Shot]:
    """Shots on synthetic 1 s clips; `mixed` alternates wide and tall (vertical edits blur the wide)."""
    folder = _clip_folder(home)
    sizes = {"wide": (160, 90), "tall": (90, 160)}
    bases = {
        kind: ff.make_test_clip(folder / f"base_{kind}.mp4", width=w, height=h, seconds=1.0)
        for kind, (w, h) in sizes.items()
    }
    shots: list[Shot] = []
    for i in range(count):
        kind = ("wide" if i % 2 == 0 else "tall") if mixed else ("wide" if wide else "tall")
        path = Path(shutil.copy(bases[kind], folder / f"stock_{i:04d}.mp4"))
        w, h = sizes[kind]
        shots.append(
            Shot(0, i * each_s, (i + 1) * each_s, "fake", f"c{i}", str(path), 0.0, w, h, "u", "l", "kw")
        )
    return shots


def _captions(path: Path, seconds: float) -> Path:
    """A caption every 0.4 s over the whole timeline: a part with the wrong clock shows the wrong word."""
    words = [Word(f"w{i}", i * 0.4, i * 0.4 + 0.3) for i in range(int(seconds / 0.4))]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_ass(words, "punch_yellow"), "utf-8")
    return path


def _frames(video: Path) -> int:
    stream = next(s for s in ff.probe(video)["streams"] if s["codec_type"] == "video")
    return int(stream["nb_frames"])


def _psnr_min(a: Path, b: Path, work: Path) -> float:
    """The lowest per-frame PSNR (dB) between two videos; `inf` when every frame is identical."""
    work.mkdir(parents=True, exist_ok=True)
    (work / "a.mp4").write_bytes(a.read_bytes())
    (work / "b.mp4").write_bytes(b.read_bytes())
    ff.run(["-i", "a.mp4", "-i", "b.mp4", "-lavfi", "psnr=stats_file=psnr.log", "-f", "null", "-"], cwd=work)
    values = re.findall(r"psnr_avg:(\S+)", (work / "psnr.log").read_text(encoding="utf-8"))
    assert values, "psnr wrote no per-frame lines"
    return min(float(v) for v in values)


def _qc_report(video: Path) -> dict[str, tuple[bool, str | float | None]]:
    result = run_qc("s1", video, "short", drift_ms=0.0, first_caption_s=0.0)
    return {name: (c.passed, c.value) for name, c in result.checks.items()}


def _same_qc(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """The same checks, each with the same verdict and value (a number within 0.05, e.g. the duration)."""
    if a.keys() != b.keys():
        return False
    for name in a:
        (pa, va), (pb, vb) = a[name], b[name]
        if pa != pb:
            return False
        if isinstance(va, float) and isinstance(vb, float):
            if abs(va - vb) > 0.05:
                return False
        elif va != vb:
            return False
    return True


def _short_audio(folder: Path, seconds: float) -> Path:
    raw = ff.make_tone_wav(folder / "audio_raw.wav", seconds, sr=48000)
    final = raw.with_name("audio.wav")
    loudnorm(raw, final, i=FINAL_LUFS, tp=FINAL_TP)
    return final


@needs_ffmpeg
def test_a_16_clip_short_is_the_same_video_in_one_command_and_in_parts(tmp_path: Path):
    each_s = 0.4
    shots = _real_shots(tmp_path / "home", 16, each_s, mixed=True)
    total_s = 16 * each_s
    folder = tmp_path / "home" / "jobs" / "job_20261109_real" / "shorts" / "s1"
    audio = _short_audio(folder, total_s)
    ass = _captions(tmp_path / "home" / "jobs" / "job_20261109_real" / "captions" / "s1.ass", total_s)
    treatment = visual_treatment("clean_bright")  # no grain: the noise filter's sequence restarts in a part
    video = VideoCfg(encoder="libx264", preset="veryfast")
    one = render_video(
        shots=shots,
        audio=audio,
        out=folder / "one" / "short.mp4",
        size=VERTICAL,
        treatment=treatment,
        video=video,
        ass_path=ass,
        max_inputs=16,
    )
    parts = render_video(
        shots=shots,
        audio=audio,
        out=folder / "parts" / "short.mp4",
        size=VERTICAL,
        treatment=treatment,
        video=video,
        ass_path=ass,
        max_inputs=5,  # 16 clips: 5 + 5 + 5 + 1
    )
    edges = frame_edges([s.end for s in shots], FPS)
    assert _frames(one) == _frames(parts) == edges[-1]
    assert ff.duration_s(parts) == pytest.approx(ff.duration_s(one), abs=0.02)
    assert ff.video_size(parts) == ff.video_size(one) == VERTICAL
    qc_one, qc_parts = _qc_report(one), _qc_report(parts)
    assert _same_qc(qc_one, qc_parts), (qc_one, qc_parts)
    assert all(passed for passed, _ in qc_parts.values())
    # the pictures, caption text included, are the same frame for frame (two lossy encodes of one picture)
    assert _psnr_min(one, parts, tmp_path / "psnr") >= 30.0
    assert not (folder / "parts" / "short.parts").exists()


@needs_ffmpeg
def test_a_200_clip_short_renders_in_parts_and_passes_qc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    count, each_s = 200, 0.2
    shots = _real_shots(tmp_path / "home", count, each_s, mixed=True)
    total_s = count * each_s
    folder = tmp_path / "home" / "jobs" / "job_20261109_real" / "shorts" / "s1"
    audio = _short_audio(folder, total_s)
    ass = _captions(tmp_path / "home" / "jobs" / "job_20261109_real" / "captions" / "s1.ass", total_s)
    seen: list[list[str]] = []
    real = subprocess.run

    def spy(cmd: Any, *a: Any, **kw: Any) -> Any:
        if "-nostdin" in cmd:
            seen.append(list(cmd))
        return real(cmd, *a, **kw)

    with monkeypatch.context() as m:  # QC below runs its own FFmpeg commands; only the render is counted
        m.setattr(ff.subprocess, "run", spy)
        out = render_video(
            shots=shots,
            audio=audio,
            out=folder / "short.mp4",
            size=VERTICAL,
            treatment=visual_treatment("cinematic_teal"),
            video=VideoCfg(encoder="libx264", preset="ultrafast"),
            ass_path=ass,
        )
    assert len(seen) == math.ceil(count / ff.MAX_CLIP_INPUTS) + 1
    assert max(_cmd_chars(c) for c in seen) < ARGV_LIMIT
    edges = frame_edges([s.end for s in shots], FPS)
    assert _frames(out) == edges[-1] == round(total_s * FPS)
    assert ff.duration_s(out) == pytest.approx(total_s, abs=0.1)
    result = run_qc("s1", out, "short", drift_ms=0.0, first_caption_s=0.0)
    assert result.passed, result.reason


@needs_ffmpeg
def test_a_chapter_with_a_card_is_the_same_video_in_one_command_and_in_parts(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from tests.helpers import make_combo, make_ctx

    ctx = make_ctx(tmp_home, fmt="master_first", combo=make_combo(caption_style="clean_white"))
    ctx.subdir("captions")
    monkeypatch.setattr(longform, "fit_chapter_contrast", lambda *a, **k: [])
    shots_raw = _real_shots(tmp_path / "home", 14, 0.4, wide=True)
    t = ChapterTiming(1, "One", 0.0, 1.0, 1.0 + 14 * 0.4)  # a 1 s title card, then 14 shots of 0.4 s
    shots = [_shot(s.path, 1.0 + s.start, 1.0 + s.end, 160, 90) for s in shots_raw]
    ass = ctx.file("captions/long_c1.ass")
    _captions(ass, t.end)
    card = chapter_card("midnight")
    treatment = visual_treatment("clean_bright")
    one = render_segment(ctx, t, shots, treatment, card, tmp_path / "one" / "seg_1.mp4", max_inputs=14)
    parts = render_segment(ctx, t, shots, treatment, card, tmp_path / "parts" / "seg_1.mp4", max_inputs=5)
    want = round(t.end * FPS)
    assert _frames(one) == _frames(parts) == want
    assert ff.video_size(parts) == ff.video_size(one) == (1920, 1080)
    assert ff.duration_s(parts) == pytest.approx(ff.duration_s(one), abs=0.02)
    assert _psnr_min(one, parts, tmp_path / "psnr") >= 30.0
    assert not (tmp_path / "parts" / "seg_1.parts").exists()


def _wav_frames(path: Path) -> bytes:
    with wave.open(str(path), "rb") as w:
        return w.readframes(w.getnframes())


@needs_ffmpeg
def test_audio_assembled_in_parts_is_sample_for_sample_the_audio_of_one_command(tmp_path: Path):
    tones = [
        ff.make_tone_wav(tmp_path / f"t{i}.wav", 0.3 + 0.05 * i, sr=24000, freq=300.0 + 40 * i)
        for i in range(9)
    ]
    segments: list[Path | float] = []
    for i, tone in enumerate(tones):
        segments += [tone, 0.25 + 0.01 * i]
    one = assemble_audio(segments, tmp_path / "one.wav", max_inputs=100)
    parts = assemble_audio(segments, tmp_path / "parts.wav", max_inputs=4)
    assert _wav_frames(one) == _wav_frames(parts)
    assert not (tmp_path / "parts.parts").exists()
