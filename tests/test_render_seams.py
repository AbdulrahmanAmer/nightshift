"""H-5b (F-H237): every join of a video rendered in parts is measured with the real FFmpeg.

A long render is cut into parts of at most `max_inputs` shots (`render_in_parts`,
`render_segment_in_parts`) and the parts are copied together through the concat demuxer (`join_parts`);
the master then concatenates its chapters the same way (`RenderMasterStage.run`).
`tests/test_render_constant_command.py` shows that the part render looks like the one-command render; this
file measures the seams themselves, on the file a person plays:

- every frame, once and in order: each shot is a solid colour of its own, so a decoded frame names its
  shot, and the picture must read shot 0 for its frames, then shot 1 for its, and so on, across every join
  (a repeated frame or a dropped one moves the sequence);
- an even clock: the presentation times start at 0 and step by 1 / fps, so no frame is held and no time
  goes back;
- the sound: in all three routes the sound is encoded ONCE (the Short's WAV in the join command, the
  chapters' parts are `-an`, the master encodes one assembled WAV), so the sound has no seam by design and
  these checks cannot see a join today. They protect against someone later cutting the sound per part: the
  audio packets must run on without a gap, overlap or repeat (independent of the encoder), and a least-squares
  fit of the 440 Hz tone over 20 ms either side of each join must leave a small residual (no click).

The checkers are plain functions (`shot_defects`, `clock_defects`, `click_defects`) that return what is
wrong, so the last test can feed them a hand-made bad join and see them speak. These tests need FFmpeg:
CI has it, and a laptop runs them only while holding the laptop lock.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from ns_common import ffmpeg as ff
from ns_common.models import VideoCfg
from ns_studio.media import longform
from ns_studio.media.longform import RenderMasterStage, render_segment
from ns_studio.media.master import ChapterTiming
from ns_studio.media.presets import chapter_card, visual_treatment
from ns_studio.media.render import render_video
from ns_studio.media.shotplan import Shot, frame_edges

needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")
FPS = 30
SR = 48000
SHORT_SIZE = (180, 320)  # a small vertical frame: the seams do not depend on the picture size
CARD_COLOUR = (0x2A, 0x33, 0x50)  # chapter_card("midnight").background


# ------------------------------------------------------------------------------------------------ measuring
def palette(count: int) -> list[tuple[int, int, int]]:
    """`count` colours at least 70 levels apart in some channel (and from the card): each names its shot."""
    levels = (30, 100, 170, 240)
    out: list[tuple[int, int, int]] = []
    for i in range(64):
        n = (i * 13) % 64
        rgb = (levels[n // 16], levels[(n // 4) % 4], levels[n % 4])
        if max(abs(a - b) for a, b in zip(rgb, CARD_COLOUR, strict=True)) < 70:
            continue
        out.append(rgb)
    assert len(out) >= count
    return out[:count]


def _stdout(cmd: list[str]) -> bytes:
    proc = subprocess.run(cmd, capture_output=True, check=False)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-400:]
    return proc.stdout


def frame_means(video: Path) -> np.ndarray:
    """The mean colour (r, g, b) of every decoded frame: each frame is scaled to 8x8 rgb24 (area average)."""
    raw = _stdout(
        [
            ff.ffmpeg_exe(),
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(video),
            "-an",
            "-vf",
            "scale=8:8:flags=area,format=rgb24",
            "-f",
            "rawvideo",
            "-",
        ]
    )
    return np.frombuffer(raw, dtype=np.uint8).reshape(-1, 64, 3).astype(np.float64).mean(axis=1)


NEAR = 25.0  # a frame is a reference colour only within this many levels in every channel
MAX_SHIFT = -2  # what `frame_shots` answers for a frame that is near no reference: black, grey, blended


def frame_shots(video: Path, colours: list[tuple[int, int, int]], *, card: bool = False) -> list[int]:
    """For each decoded frame, the index of the reference colour it is within `NEAR` levels of in every
    channel (-1: the title card colour); `MAX_SHIFT` (-2) for a frame near none, so a black or blended frame
    fails the comparison even when some palette colour happens to be its nearest."""
    means = frame_means(video)
    refs = np.array([*colours, CARD_COLOUR] if card else colours, dtype=np.float64)
    gap = np.abs(means[:, None, :] - refs[None, :, :]).max(axis=2)
    near = np.argmin(gap, axis=1)
    out: list[int] = []
    for i, n in enumerate(near):
        if gap[i, n] >= NEAR:
            out.append(MAX_SHIFT)
        else:
            out.append(-1 if card and int(n) == len(colours) else int(n))
    return out


def frame_times(video: Path) -> list[float]:
    """The presentation time of every video frame, in the order the decoder shows them (ffprobe)."""
    out = _stdout(
        [
            ff.ffprobe_exe(),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "frame=pts_time",
            "-of",
            "csv=p=0",
            str(video),
        ]
    ).decode("utf-8")
    return [float(line.strip().rstrip(",")) for line in out.splitlines() if line.strip()]


def pcm(video: Path) -> np.ndarray:
    """The audio of `video` decoded to mono float32 at 48 kHz."""
    raw = _stdout(
        [
            ff.ffmpeg_exe(),
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(video),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SR),
            "-f",
            "f32le",
            "-",
        ]
    )
    return np.frombuffer(raw, dtype="<f4").astype(np.float64)


def runs(seq: list[int]) -> list[tuple[int, int]]:
    """`(value, length)` of each run of equal values."""
    out: list[tuple[int, int]] = []
    for v in seq:
        if out and out[-1][0] == v:
            out[-1] = (v, out[-1][1] + 1)
        else:
            out.append((v, 1))
    return out


def shot_defects(seen: list[int], expected: list[int]) -> list[str]:
    """What differs between the shots the frames show and the shots they should show, join by join."""
    if seen == expected:
        return []
    bad: list[str] = []
    if len(seen) != len(expected):
        bad.append(f"{len(seen)} frames, expected {len(expected)}")
    first = next((i for i, (h, w) in enumerate(zip(seen, expected, strict=False)) if h != w), None)
    if first is not None:
        bad.append(f"first difference at frame {first}: shot {expected[first]} expected, got {seen[first]}")
    return bad


def clock_defects(times: list[float], count: int, fps: int = FPS, tol: float = 0.001) -> list[str]:
    """Frames that do not sit on an even clock: wrong count, not starting at 0, a step off 1 / fps."""
    bad: list[str] = []
    if len(times) != count:
        bad.append(f"{len(times)} frames, expected {count}")
    if not times:
        return bad
    if abs(times[0]) > tol:
        bad.append(f"first frame at {times[0]:.4f} s")
    steps = np.diff(np.array(times))
    off = np.flatnonzero(np.abs(steps - 1 / fps) > tol)
    for i in off[:3]:
        bad.append(f"frame {int(i) + 1} at {times[int(i) + 1]:.4f} s: step {steps[int(i)] * 1000:.2f} ms")
    if len(off) > 3:
        bad.append(f"and {len(off) - 3} more steps off the clock")
    if len(times) == count and abs(times[-1] - (count - 1) / fps) > tol:
        bad.append(f"last frame at {times[-1]:.4f} s, expected {(count - 1) / fps:.4f}")
    return bad


TONE_HZ = 440.0


def click_defects(sound: np.ndarray, at_s: float, hz: float = TONE_HZ) -> list[str]:
    """A click, gap or jump in the continuous tone within 20 ms of `at_s`.

    A least-squares fit of the tone (a sin + b cos + c at `hz`) is taken over the window and what it cannot
    explain is the click: the peak residual must be at most 5 % of the tone's amplitude. A clean tone, even
    after AAC, leaves about 0.002 of it; a 5 ms piece cut out of the tone leaves about 0.74.
    """
    centre = round(at_s * SR)
    lo, hi = max(0, centre - SR // 50), min(len(sound), centre + SR // 50)
    window = sound[lo:hi]
    t = np.arange(len(window)) / SR
    basis = np.stack([np.sin(2 * np.pi * hz * t), np.cos(2 * np.pi * hz * t), np.ones_like(t)], axis=1)
    coef, *_ = np.linalg.lstsq(basis, window, rcond=None)
    amplitude = float(np.hypot(coef[0], coef[1]))
    if amplitude < 1e-3:
        return [f"at {at_s:.3f} s there is no {hz:.0f} Hz tone (amplitude {amplitude:.5f})"]
    residual = float(np.abs(window - basis @ coef).max()) / amplitude
    if residual > 0.05:
        return [f"at {at_s:.3f} s the tone leaves a residual of {residual:.3f} of its amplitude (limit 0.05)"]
    return []


AAC_FRAME = 1024  # samples in one AAC packet


def audio_packet_defects(video: Path, seconds: float) -> list[str]:
    """The audio packets of `video` (ffprobe), which do not depend on the encoder's noise: the stream starts
    at 0 (after at most one priming packet before it), every packet is one AAC frame and starts where the
    last one ended (no gap, no overlap, no repeat), and the last one ends where the sound should."""
    out = _stdout(
        [
            ff.ffprobe_exe(),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "packet=pts,duration",
            "-of",
            "csv=p=0",
            str(video),
        ]
    ).decode("utf-8")
    rows = [
        tuple(int(x) for x in line.strip().rstrip(",").split(","))
        for line in out.splitlines()
        if line.strip()
    ]
    bad: list[str] = []
    if not rows:
        return ["no audio packets"]
    pts = [r[0] for r in rows]
    first_real = next((p for p in pts if p >= 0), None)
    if first_real != 0:
        bad.append(f"the first packet at or after 0 starts at {first_real}")
    if pts[0] not in (0, -AAC_FRAME):
        bad.append(f"the first packet starts at {pts[0]}")
    for k, ((p, d), (q, _)) in enumerate(zip(rows, rows[1:], strict=False)):
        if q - p != d or d != AAC_FRAME:
            bad.append(f"packet {k + 1}: starts at {q}, the one before started at {p} and lasts {d}")
            break
    end = rows[-1][0] + rows[-1][1]
    if abs(end - round(seconds * SR)) > AAC_FRAME:
        bad.append(f"the sound ends at sample {end}, expected {round(seconds * SR)}")
    return bad


# ----------------------------------------------------------------------------------------------- set-up
def solid_shots(
    folder: Path,
    lengths_s: list[float],
    *,
    size: tuple[int, int],
    start_s: float = 0.0,
    uniform: tuple[int, int, int] | None = None,
) -> tuple[list[Shot], list[tuple[int, int, int]]]:
    """One shot per length, each on a 1 s clip of its own solid colour (wide and tall in turn), or all of
    the one `uniform` colour."""
    colours = palette(len(lengths_s)) if uniform is None else [uniform] * len(lengths_s)
    folder.mkdir(parents=True, exist_ok=True)
    shots: list[Shot] = []
    cursor = start_s
    for i, (rgb, length) in enumerate(zip(colours, lengths_s, strict=True)):
        w, h = (160, 90) if i % 2 == 0 else (90, 160)
        if size[0] > size[1]:  # a landscape edit gets wide clips only
            w, h = 160, 90
        clip = ff.make_test_clip(
            folder / f"shot_{i:03d}.mp4",
            width=w,
            height=h,
            seconds=1.0,
            kind="color",
            color="0x{:02X}{:02X}{:02X}".format(*rgb),
        )
        shots.append(Shot(0, cursor, cursor + length, "fake", f"c{i}", str(clip), 0.0, w, h, "u", "l", "kw"))
        cursor += length
    return shots, colours


def expected_shots(edges: list[int], lead: int = 0) -> list[int]:
    """The shot index each frame should show: `lead` card frames (-1), then shot k for its frames."""
    out = [-1] * lead
    for k in range(len(edges) - 1):
        out += [k] * (edges[k + 1] - edges[k])
    return out


UNEVEN_S = (0.2, 0.3, 0.4, 0.25, 0.35, 0.2, 0.3)  # a pattern repeated over the shots


@dataclass
class ShortInParts:
    video: Path
    edges: list[int]
    starts: list[int]  # the first frame of each part
    colours: list[tuple[int, int, int]]
    seconds: float


@pytest.fixture(scope="module")
def short_in_parts(tmp_path_factory: pytest.TempPathFactory) -> ShortInParts:
    """30 shots of uneven length, `max_inputs=12` (parts of 12 + 12 + 6), rendered once for three tests."""
    work = tmp_path_factory.mktemp("seams_short")
    lengths = [UNEVEN_S[i % len(UNEVEN_S)] for i in range(30)]
    shots, colours = solid_shots(work / "clips", lengths, size=SHORT_SIZE)
    edges = frame_edges([s.end for s in shots], FPS)
    seconds = edges[-1] / FPS
    audio = ff.make_tone_wav(work / "audio.wav", seconds, sr=SR, freq=440.0)
    out = render_video(
        shots=shots,
        audio=audio,
        out=work / "out" / "short.mp4",
        size=SHORT_SIZE,
        treatment=visual_treatment("clean_bright"),
        video=VideoCfg(encoder="libx264", preset="veryfast"),
        ass_path=None,
        max_inputs=12,
    )
    ranges = ff.group_ranges(len(shots), 12)
    assert len(ranges) == 3
    return ShortInParts(out, edges, [edges[a] for a, _b in ranges], colours, seconds)


# ------------------------------------------------------------------------------------------------- Shorts
@needs_ffmpeg
def test_every_join_of_a_short_in_parts_plays_each_frame_once(short_in_parts: ShortInParts):
    s = short_in_parts
    seen = frame_shots(s.video, s.colours)
    assert len(seen) == s.edges[-1]
    assert shot_defects(seen, expected_shots(s.edges)) == []
    want = expected_shots(s.edges)
    for start in s.starts[1:]:  # the last frame before each join and the first after it are the right ones
        assert seen[start - 1 : start + 1] == want[start - 1 : start + 1]


@needs_ffmpeg
def test_the_clock_of_a_short_in_parts_is_even_across_every_join(short_in_parts: ShortInParts):
    s = short_in_parts
    times = frame_times(s.video)
    assert clock_defects(times, s.edges[-1]) == []
    assert times[-1] == pytest.approx((s.edges[-1] - 1) / FPS, abs=0.001)


@needs_ffmpeg
def test_the_sound_of_a_short_in_parts_has_no_click_or_gap_at_a_join(short_in_parts: ShortInParts):
    s = short_in_parts
    assert audio_packet_defects(s.video, s.seconds) == []
    sound = pcm(s.video)
    assert abs(len(sound) - round(s.seconds * SR)) <= 1024
    for start in s.starts[1:]:
        assert click_defects(sound, start / FPS) == []


# ----------------------------------------------------------------------------------------------- chapters
def _chapter_ctx(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from tests.helpers import make_combo, make_ctx

    ctx = make_ctx(tmp_home, fmt="master_first", combo=make_combo(caption_style="clean_white"))
    ctx.subdir("captions")
    monkeypatch.setattr(longform, "fit_chapter_contrast", lambda *a, **k: [])
    return ctx


@needs_ffmpeg
def test_every_join_of_a_chapter_in_parts_plays_each_frame_once(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = _chapter_ctx(tmp_home, monkeypatch)
    lengths = [0.4] * 14
    raw, colours = solid_shots(tmp_path / "clips", lengths, size=(1920, 1080))
    t = ChapterTiming(1, "One", 0.0, 1.0, 1.0 + sum(lengths))  # a 1 s title card, then 14 shots of 0.4 s
    shots = [replace(s, start=1.0 + s.start, end=1.0 + s.end) for s in raw]
    ctx.file("captions/long_c1.ass").write_text(
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\n\n[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
        "Style: Default,Arial,40,&H00FFFFFF,&H00000000,&H00000000,0,0,1,0,0,2,10,10,10\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Text\n",
        encoding="utf-8",
    )
    out = render_segment(
        ctx,
        t,
        shots,
        visual_treatment("clean_bright"),
        chapter_card("midnight"),
        tmp_path / "parts" / "seg_1.mp4",
        max_inputs=5,  # 14 shots: 5 + 5 + 4, the card on the first part
    )
    assert not (tmp_path / "parts" / "seg_1.parts").exists()
    total = round(t.end * FPS)
    card_frames = round(t.audio_start * FPS)
    edges = [0]
    for s in shots:
        edges.append(round(s.end * FPS) - card_frames)
    edges[-1] = total - card_frames
    want = expected_shots(edges, lead=card_frames)
    assert len(want) == total
    assert shot_defects(frame_shots(out, colours, card=True), want) == []
    assert clock_defects(frame_times(out), total) == []


# ---------------------------------------------------------------------------------------------- the master
def chapter_text(prefix: str) -> str:
    """Four short sentences of four distinct words each."""
    return " ".join(
        " ".join(f"{prefix}{chr(97 + s)}{chr(97 + i)}" for i in range(3)) + " end." for s in range(4)
    )


@needs_ffmpeg
def test_a_master_in_parts_joins_its_chapters_and_parts_without_a_gap(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio.media import master as M
    from ns_studio.media import qc as Q
    from ns_studio.media import thumbnails as T
    from ns_studio.media.stages_media import MASTER_MEDIA_STAGES
    from ns_studio.orchestrator import FORMAT_STAGES, run_job
    from tests.helpers import make_combo, make_ctx
    from tests.test_master_pipeline import beat, fake_make_thumbnails

    before_render = [s.name for s in MASTER_MEDIA_STAGES][:5]  # voice .. clips: what render_master reads
    assert before_render == ["voice", "duration_check", "captions", "shorts_timing", "clips"]
    monkeypatch.setitem(FORMAT_STAGES, "master_first", before_render)
    monkeypatch.setattr(M, "FLOOR_S", 1.0)
    monkeypatch.setattr(Q, "LONG_RANGE_S", (1.0, 200.0))
    monkeypatch.setattr(T, "make_thumbnails", fake_make_thumbnails)
    monkeypatch.setitem(longform.render_segment.__kwdefaults__, "max_inputs", 3)  # type: ignore[index]
    joined: list[tuple[list[int], Path]] = []
    real_join = longform.join_parts

    def spy(folder: Path, parts: Any, lengths: Any, fps: int, extra: Any, out: Path, **kw: Any) -> None:
        joined.append((list(lengths), out))
        real_join(folder, parts, lengths, fps, extra, out, **kw)

    monkeypatch.setattr(longform, "join_parts", spy)

    ctx = make_ctx(tmp_home, fmt="master_first", combo=make_combo(visual_treatment="clean_bright"))
    ctx.write_json(
        "story.json",
        {
            "working_title": "Seams",
            "chapters": [
                {"n": i, "title": f"Chapter title {i}", "text": chapter_text(chr(120 - i))} for i in (1, 2, 3)
            ],
        },
    )
    ctx.write_json("shorts_plan.json", [])
    ctx.write_json("short_combos.json", {})
    # one beat per sentence, four sentences a chapter: four shots or more, so `max_inputs=3` cuts each chapter
    ctx.write_json("visual_plan.json", [beat(c, k, k, k, False) for c in (1, 2, 3) for k in range(4)])
    assert run_job(ctx) == before_render
    # The chapters' voice becomes one continuous tone each (same length), so a gap or a click is audible to a
    # measurement; the master puts a silent 1.2 s title card between chapters (master.py CARD_S).
    manifest = ctx.read_json("audio/manifest.json")
    for n in (1, 2, 3):
        entry = manifest[f"c{n}"]
        ff.make_tone_wav(Path(entry["path"]), float(entry["seconds"]), sr=24000, freq=440.0)

    RenderMasterStage().run(ctx)

    master = ctx.file("long/master.mp4")
    timeline = longform.timeline_from_manifest(ctx)
    total = round(timeline[-1].end * FPS)
    segment_joins = [lengths for lengths, out in joined if out.name.startswith("seg_")]
    assert segment_joins and all(len(x) >= 2 for x in segment_joins), "no chapter was rendered in parts"
    times = frame_times(master)
    assert clock_defects(times, total) == []
    # where each chapter's title card starts and ends, frame by frame: the picture must keep its place against
    # the sound (two chapters off by +1 and -1 frame would keep the total and the clock)
    card = longform.pick_card(ctx.job_id).background.lstrip("#")
    card_rgb = np.array([int(card[i : i + 2], 16) for i in (0, 2, 4)], dtype=np.float64)
    is_card = np.abs(frame_means(master) - card_rgb).max(axis=1) < NEAR
    for t in timeline:
        first, after = round(t.seg_start * FPS), round(t.audio_start * FPS)
        assert is_card[first:after].all(), (t.n, first, after, is_card[first:after].tolist())
        assert not is_card[after], (t.n, "the card runs on past frame", after)
        if first > 0:
            assert not is_card[first - 1], (t.n, "the card starts before frame", first)
    assert int(is_card.sum()) == sum(round(t.audio_start * FPS) - round(t.seg_start * FPS) for t in timeline)
    # every join: the parts inside each chapter, then the chapters themselves (frame -> seconds)
    joins_s: list[float] = []
    for t, lengths in zip(timeline, segment_joins, strict=True):
        cursor = round(t.seg_start * FPS)
        for n in lengths[:-1]:
            cursor += n
            joins_s.append(cursor / FPS)
    sound = pcm(master)
    assert abs(len(sound) - round(timeline[-1].end * SR)) <= 1024
    assert audio_packet_defects(master, timeline[-1].end) == []
    for at in joins_s:  # a part join inside a chapter's own sound: a continuous tone
        assert click_defects(sound, at) == [], at
    # each chapter's sound starts where its title card ends, and the card before it is silent
    peak = float(np.abs(sound).max())
    for t in timeline:
        onset = next(i for i in range(round(t.seg_start * SR), len(sound)) if abs(sound[i]) > 0.1 * peak)
        assert onset / SR == pytest.approx(t.audio_start, abs=0.002), t
        if t.audio_start > t.seg_start:  # chapters after the first open with a silent title card
            card = sound[round(t.seg_start * SR) + SR // 20 : round(t.audio_start * SR) - SR // 20]
            assert len(card) > 0 and float(np.abs(card).max()) < 0.02 * peak, t


# ------------------------------------------------------------------------------ a caption across a join
CAPTION_ASS = (
    "[Script Info]\nScriptType: v4.00+\nPlayResX: 180\nPlayResY: 320\nWrapStyle: 0\n\n[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
    "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
    "MarginL, MarginR, MarginV, Encoding\n"
    "Style: Default,Arial,60,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,3,0,5,"
    "10,10,10,1\n"
    "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
)
CAPTION_EDGE = 6  # frames a caption shows before a join, and from the join on
CAPTION_TOL = 0.05  # share of the caption's pixels that may differ from one frame to the next
CAPTION_MIN = 50  # fewest pixels a shown caption has


def _ass_time(half_frames: int) -> str:
    """`h:mm:ss.cc` for `half_frames / 2` frames (captions start half a frame early)."""
    cs = round(100 * half_frames / 2 / FPS)
    return f"0:{cs // 6000:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def caption_events(joins: list[int]) -> tuple[str, list[range]]:
    """An ASS body with one caption across each join (frames join - 6 .. join + 5) and the frames of each."""
    lines: list[str] = []
    shown: list[range] = []
    for j in joins:
        a, b = j - CAPTION_EDGE, j + CAPTION_EDGE
        start, end = _ass_time(2 * a - 1), _ass_time(2 * b - 1)
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,SEAM\n")
        shown.append(range(a, b))
    return CAPTION_ASS + "".join(lines), shown


def caption_masks(video: Path, size: tuple[int, int]) -> np.ndarray:
    """For each frame, which pixels differ from the frame's corner colour by more than `NEAR` levels."""
    w, h = size
    raw = _stdout(
        [
            ff.ffmpeg_exe(),
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(video),
            "-an",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "-",
        ]
    )
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, h, w, 3).astype(np.int16)
    return np.abs(frames - frames[:, :1, :1, :]).max(axis=3) > NEAR


def caption_defects(masks: np.ndarray, shown: list[range]) -> list[str]:
    """A caption that blinks (absent in a frame it belongs in, or left in one it does not) or that moves."""
    bad: list[str] = []
    visible = {i for r in shown for i in r}
    for i, m in enumerate(masks):
        pixels = int(m.sum())
        if i in visible and pixels < CAPTION_MIN:
            bad.append(f"frame {i}: {pixels} caption pixels, the caption should be on")
        if i not in visible and pixels > 5:
            bad.append(f"frame {i}: {pixels} caption pixels, the caption should be off")
    for r in shown:
        ref = masks[r.start]
        for i in r:
            moved = int((masks[i] ^ ref).sum()) / max(1, int(ref.sum()))
            if moved > CAPTION_TOL:
                bad.append(f"frame {i}: {moved:.2%} of the caption's pixels differ from frame {r.start}")
    return bad


def caption_render(tmp_path: Path) -> tuple[np.ndarray, list[range]]:
    lengths = [UNEVEN_S[i % len(UNEVEN_S)] for i in range(14)]
    shots, _ = solid_shots(tmp_path / "clips", lengths, size=SHORT_SIZE, uniform=(100, 100, 100))
    edges = frame_edges([s.end for s in shots], FPS)
    joins = [edges[a] for a, _b in ff.group_ranges(len(shots), 5)[1:]]  # parts of 5 + 5 + 4
    body, shown = caption_events(joins)
    ass = tmp_path / "caption.ass"
    ass.write_text(body, encoding="utf-8")
    audio = ff.make_tone_wav(tmp_path / "audio.wav", edges[-1] / FPS, sr=SR)
    out = render_video(
        shots=shots,
        audio=audio,
        out=tmp_path / "out" / "short.mp4",
        size=SHORT_SIZE,
        treatment=visual_treatment("clean_bright"),
        video=VideoCfg(encoder="libx264", preset="veryfast"),
        ass_path=ass,
        max_inputs=5,
    )
    masks = caption_masks(out, SHORT_SIZE)
    assert len(masks) == edges[-1]
    return masks, shown


@needs_ffmpeg
def test_a_caption_across_a_join_does_not_blink_or_move(tmp_path: Path):
    masks, shown = caption_render(tmp_path)
    assert caption_defects(masks, shown) == []


# --------------------------------------------------------------------------------- the checkers can fail
def _hand_join(folder: Path, frame_counts: list[int], declared: list[int], out: Path) -> Path:
    """Solid-colour parts of `frame_counts` frames joined through `concat_list_text`, the way `join_parts`
    does, with `declared` frames written as each part's `duration`."""
    colours = palette(len(frame_counts))
    folder.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    for k, (n, rgb) in enumerate(zip(frame_counts, colours, strict=True)):
        parts.append(
            ff.make_test_clip(
                folder / f"p{k}.mp4",
                width=160,
                height=90,
                seconds=n / FPS - 0.001,
                kind="color",
                color="0x{:02X}{:02X}{:02X}".format(*rgb),
            )
        )
    lst = folder / "parts.txt"
    lst.write_text(ff.concat_list_text(parts, [n / FPS for n in declared]), encoding="utf-8")
    ff.run(["-f", "concat", "-safe", "0", "-i", str(lst), "-c:v", "copy", "-an", str(out)])
    return out


@needs_ffmpeg
def test_the_seam_checks_catch_a_repeated_and_a_dropped_frame(tmp_path: Path):
    want = [0] * 6 + [1] * 6  # two parts of six frames; the checks must see anything else
    colours = palette(2)
    # the join as it should be: both checks are silent
    good = _hand_join(tmp_path / "good", [6, 6], [6, 6], tmp_path / "good.mp4")
    assert frame_shots(good, colours) == want
    assert shot_defects(frame_shots(good, colours), want) == []
    assert clock_defects(frame_times(good), 12) == []
    # one frame repeated at the join (the first part has a seventh frame): the picture check reports it
    repeated = _hand_join(tmp_path / "rep", [7, 6], [7, 6], tmp_path / "rep.mp4")
    bad = shot_defects(frame_shots(repeated, colours), want)
    assert bad and "13 frames, expected 12" in bad[0]
    assert any("first difference at frame 6" in line for line in bad), bad
    # one frame dropped at the join (the first part has only five): the picture check reports it
    dropped = _hand_join(tmp_path / "drop", [5, 6], [5, 6], tmp_path / "drop.mp4")
    bad = shot_defects(frame_shots(dropped, colours), want)
    assert bad and "11 frames, expected 12" in bad[0]
    assert any("first difference at frame 5" in line for line in bad), bad
    # a part listed one frame too long (a held frame, a gap in the clock): the clock check reports it
    held = _hand_join(tmp_path / "held", [6, 6], [7, 6], tmp_path / "held.mp4")
    bad = clock_defects(frame_times(held), 12)
    assert bad and any("step" in line for line in bad), bad
    # a click in a tone: a gap of 5 ms cut out of it is reported at that time, and a clean tone is not
    tone = ff.make_tone_wav(tmp_path / "tone.wav", 1.0, sr=SR)
    clean = pcm(tone)
    assert click_defects(clean, 0.5) == []
    cut = np.concatenate([clean[: SR // 2], clean[SR // 2 + 240 :]])
    assert click_defects(cut, 0.5) != []
    # a caption that blinks off for a frame, or moves two pixels: the caption check reports each
    flat = np.zeros((20, 40, 40), dtype=bool)
    flat[:, 15:25, 10:30] = True
    shown = [range(0, 20)]
    assert caption_defects(flat, shown) == []
    blink = flat.copy()
    blink[9] = False
    assert any(line.startswith("frame 9:") for line in caption_defects(blink, shown))
    moved = flat.copy()
    moved[11] = np.roll(flat[11], 2, axis=1)
    assert any(line.startswith("frame 11:") for line in caption_defects(moved, shown))
    # the audio packets: one encode of a second of tone is clean; a sound that ends short is noticed
    whole = tmp_path / "whole.mp4"
    ff.run(
        [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate={SR}",
            "-t",
            "1.0",
            "-c:a",
            "aac",
            "-ar",
            str(SR),
            str(whole),
        ]
    )
    assert audio_packet_defects(whole, 1.0) == []
    assert audio_packet_defects(whole, 2.0) != []
