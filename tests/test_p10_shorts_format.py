# ruff: noqa: E501
"""Package P10a: story Shorts and Shorts made from a full video draw from their format.

The acceptance is at the effect: duplicate a preset through the Library's own function, change something
measurable (frame size, caption anchor, safe margins), choose it on a channel, make the Short on fakes and read
the file (ffprobe) and its caption file (ASS). Tests that need FFmpeg are marked `needs_ffmpeg`; they skip where
FFmpeg is off the PATH and run in CI.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from ns_common import ffmpeg as ff
from ns_common.models import Format, VideoCfg
from ns_common.paths import Paths
from ns_common.tts_client import FakeTts, wav_seconds
from ns_dashboard.formats import create_format
from ns_studio.media import formats as F
from ns_studio.media import qc as Q
from ns_studio.media import render as R
from ns_studio.media import thumbnails as T
from ns_studio.media.captions import CaptionsStage
from ns_studio.media.layout import SourceSpec, build_regions_graph
from ns_studio.media.presets import caption_style, visual_treatment
from ns_studio.media.shotplan import Shot, frame_edges
from ns_studio.media.stages_media import MASTER_MEDIA_STAGES, SHORTS_MEDIA_STAGES
from ns_studio.orchestrator import FORMAT_STAGES, run_job
from ns_studio.text import stages_shorts  # noqa: F401
from tests.helpers import make_channel, make_combo, make_ctx
from tests.test_master_pipeline import MASTER_TITLE, beat, chapter_text, fake_make_thumbnails
from tests.test_media_pipeline import TEXT

needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")


# ---------- helpers ----------
def duplicate(paths: Paths, source: str, **changes: Any) -> str:
    """Duplicate a preset the way Library > Formats does and return the copy's id."""
    return str(create_format(paths, {"duplicate_of": source, "changes": changes})["id"])


SMALL: dict[str, Any] = {
    "name": "Small Short",
    "canvas": {"w": 720, "h": 1280},
    "layout": {"regions": [{"name": "main", "x": 0, "y": 0, "w": 720, "h": 1280, "source": "full"}]},
    "captions": {"anchor_y_pct": 50},
    "safe_margins": {"top": 150, "bottom": 200},
}


def style_fields(ass: str) -> list[str]:
    return next(ln for ln in ass.splitlines() if ln.startswith("Style: Default,")).split(",")


def play_res(ass: str) -> tuple[int, int]:
    x = re.search(r"PlayResX: (\d+)", ass)
    y = re.search(r"PlayResY: (\d+)", ass)
    assert x and y
    return int(x.group(1)), int(y.group(1))


def prepare_audio(ctx: Any, text: str) -> None:
    ctx.write_json(
        "parts.json",
        [
            {
                "n": 1,
                "text": text,
                "hook": "",
                "word_count": len(text.split()),
                "est_seconds": 5.0,
                "cliffhanger": False,
            }
        ],
    )
    audio = ctx.subdir("audio")
    wav = audio / "p1.wav"
    FakeTts().speak(text, "Ryan", 1.0, wav)
    wav.with_suffix(".txt").write_text(text, encoding="utf-8")
    ctx.write_json("audio/manifest.json", {"p1": {"path": str(wav), "seconds": round(wav_seconds(wav), 3)}})


def caption_job(tmp_home: Path, formats_allowed: list[str], style: str = "punch_yellow") -> Any:
    ctx = make_ctx(
        tmp_home, combo=make_combo(caption_style=style), channel=make_channel(formats_allowed=formats_allowed)
    )
    prepare_audio(ctx, "The doorbell camera pings at night. Then the light goes out.")
    return ctx


def anchored_margin(style: str, h: int, width: int, anchor: float) -> int:
    """MarginV for a caption centred at `anchor` % of the height, from the style's own sizes scaled by width."""
    size = round(caption_style(style).vertical.font_size * width / 1080)
    return max(20, round(h * (1 - anchor / 100.0) - 0.6 * size))


# ---------- the picture graph (pure) ----------
def shots() -> list[Shot]:
    sizes = [(1080, 1920), (1920, 1080), (1080, 1920)]
    out, start = [], 0.0
    for i, ((w, h), end) in enumerate(zip(sizes, (2.0, 4.5, 7.0), strict=True)):
        out.append(Shot(0, start, end, "pexels", f"i{i}", f"c{i}.mp4", 0.0, w, h, "u", "l", "k"))
        start = end
    return out


def test_the_shipped_short_layout_is_plain_and_draws_todays_graph() -> None:
    fmt = F.get_format("short_9x16")
    assert R.is_plain(fmt) and R.footage_size(fmt, (1, 1)) == (1080, 1920)
    s = shots()
    edges = frame_edges([x.end for x in s], 30)
    t = visual_treatment("dark_slowzoom")
    assert R.build_graph(s, edges, R.VERTICAL, 30, t, None, None, fmt) == R.build_graph(
        s, edges, R.VERTICAL, 30, t, None, None
    )


def test_a_smaller_frame_is_drawn_at_that_size() -> None:
    raw = yaml.safe_load((F.FORMATS_DIR / "short_9x16.yaml").read_text(encoding="utf-8"))
    raw["canvas"] = {"w": 720, "h": 1280}
    raw["layout"]["regions"][0].update(w=720, h=1280)
    fmt = Format.model_validate(raw)
    s = shots()
    edges = frame_edges([x.end for x in s], 30)
    graph = R.build_graph(s, edges, (720, 1280), 30, visual_treatment("dark_slowzoom"), None, None, fmt)
    assert "s=720x1280" in graph and "1080x1920" not in graph and "scale=720:1280" in graph
    assert "overlay=" not in graph.replace("overlay=(W-w)/2:(H-h)/2", "")  # only the wide-clip blur fill


def test_regions_graph_places_the_footage_region_on_the_canvas() -> None:
    raw = yaml.safe_load((F.FORMATS_DIR / "short_9x16.yaml").read_text(encoding="utf-8"))
    raw["layout"]["regions"] = [
        {"name": "bg", "x": 0, "y": 0, "w": 1080, "h": 1920, "source": "blur_fill"},
        {"name": "fg", "x": 40, "y": 300, "w": 1000, "h": 1320, "source": "full"},
    ]
    fmt = Format.model_validate(raw)
    assert not R.is_plain(fmt) and R.footage_size(fmt, (1, 1)) == (1000, 1320)
    s = shots()
    edges = frame_edges([x.end for x in s], 30)
    graph = R.build_graph(s, edges, R.VERTICAL, 30, visual_treatment("dark_slowzoom"), None, None, fmt)
    assert "[cat]split=2[cat_0][cat_1]" in graph  # one picture feeds the fill and the footage region
    assert "overlay=40:300:shortest=1[comp]" in graph
    assert "color=c=black" not in graph  # the first region covers the canvas
    assert graph.endswith("format=yuv420p[vout]") and "[comp]" in graph
    assert "s=1000x1320" in graph  # shots are drawn at the footage region's size
    # a first region that does not cover the canvas starts from black, for as long as the video
    raw["layout"]["regions"] = [{"name": "fg", "x": 40, "y": 300, "w": 1000, "h": 1320, "source": "full"}]
    inset = Format.model_validate(raw)
    graph2 = R.build_graph(s, edges, R.VERTICAL, 30, visual_treatment("dark_slowzoom"), None, None, inset)
    total = edges[-1] / 30
    assert f"color=c=black:s=1080x1920:r=30:d={total:.3f}[base]" in graph2


def test_the_black_base_runs_at_the_rate_of_the_shots_not_the_models_default() -> None:
    """Review nit: a canvas without `fps` in its file is 30 in the model, the shots follow Settings."""
    raw = yaml.safe_load((F.FORMATS_DIR / "short_9x16.yaml").read_text(encoding="utf-8"))
    raw["layout"]["regions"] = [{"name": "fg", "x": 40, "y": 300, "w": 1000, "h": 1320, "source": "full"}]
    assert "fps" not in raw["canvas"]
    inset = Format.model_validate(raw)
    s = shots()
    edges = frame_edges([x.end for x in s], 24)
    graph = R.build_graph(s, edges, R.VERTICAL, 24, visual_treatment("dark_slowzoom"), None, None, inset)
    total = edges[-1] / 24
    assert f"color=c=black:s=1080x1920:r=24:d={total:.3f}[base]" in graph and ":r=30:" not in graph
    # the layout engine alone (the clipper's call) still uses the format's own rate
    assert any(
        ":r=30" in f for f in build_regions_graph(inset, {"full": SourceSpec(1080, 1920, label="cat")})
    )
    assert any(
        ":r=24" in f
        for f in build_regions_graph(inset, {"full": SourceSpec(1080, 1920, label="cat")}, fps=24)
    )


def test_the_video_takes_the_frame_rate_and_preset_the_file_writes() -> None:
    video = VideoCfg(encoder="libx264", preset="medium", fps=30)
    plain = F.get_format("short_9x16")
    assert R.video_for_format(video, plain) == video
    raw = yaml.safe_load((F.FORMATS_DIR / "short_9x16.yaml").read_text(encoding="utf-8"))
    raw["canvas"]["fps"] = 24
    raw["encoder"] = {"preset": "slow"}
    mine = R.video_for_format(video, Format.model_validate(raw))
    assert (mine.fps, mine.preset) == (24, "slow")
    # a graph at 24 fps says so, and libx264 never gets an NVENC preset name
    raw["encoder"] = {"preset": "p5"}
    nv = R.video_for_format(video, Format.model_validate(raw))
    assert (
        "-preset" in R.encoder_args(nv)
        and R.encoder_args(nv)[R.encoder_args(nv).index("-preset") + 1] == "veryfast"
    )


# ---------- the captions stage on fakes (no FFmpeg) ----------
def test_a_duplicated_short_format_shapes_the_captions_file(tmp_home: Path) -> None:
    paths = Paths.from_env()
    copy_id = duplicate(paths, "short_9x16", **SMALL)
    ctx = caption_job(tmp_home, [copy_id])
    CaptionsStage().run(ctx)
    ass = ctx.file("captions/s1.ass").read_text(encoding="utf-8")
    assert play_res(ass) == (720, 1280)
    fields = style_fields(ass)
    assert int(fields[-2]) == anchored_margin("punch_yellow", 1280, 720, 50)
    assert int(fields[2]) == round(92 * 720 / 1080)
    tl = ctx.read_json("captions/s1.json")
    assert tl["ass_opts"]["canvas"] == [720, 1280] and tl["ass_opts"]["anchor_y_pct"] == 50.0
    assert tl["ass_opts"]["safe_zones"] == [150, 200]
    assert ctx.read_json("formats.json")["short"]["id"] == copy_id


def test_the_shipped_short_still_gives_todays_captions_file(tmp_home: Path) -> None:
    ctx = caption_job(tmp_home, [])
    CaptionsStage().run(ctx)
    ass = ctx.file("captions/s1.ass").read_text(encoding="utf-8")
    assert play_res(ass) == (1080, 1920)
    fields = style_fields(ass)
    assert int(fields[-2]) == round(1920 * 30 / 100) and int(fields[2]) == 92  # punch_yellow's own place
    opts = ctx.read_json("captions/s1.json")["ass_opts"]
    assert opts["safe_zones"] == [220, 320] and opts["anchor_y_pct"] is None
    assert opts["canvas"] is None and opts["max_words"] is None


def test_an_edited_shipped_short_preset_changes_the_captions_file(tmp_home: Path) -> None:
    paths = Paths.from_env()
    paths.formats_dir.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_load((F.FORMATS_DIR / "short_9x16.yaml").read_text(encoding="utf-8"))
    raw["safe_margins"]["bottom"] = 800  # the style's own place is now under the safe margin
    (paths.formats_dir / "short_9x16.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    ctx = caption_job(tmp_home, [])
    CaptionsStage().run(ctx)
    fields = style_fields(ctx.file("captions/s1.ass").read_text(encoding="utf-8"))
    assert int(fields[-2]) == 812 and int(fields[-2]) != round(1920 * 30 / 100)
    assert ctx.read_json("formats.json")["short"]["source"] == "edited"


def test_a_format_that_limits_the_caption_styles_swaps_the_style(tmp_home: Path) -> None:
    paths = Paths.from_env()
    copy_id = duplicate(paths, "short_9x16", captions={"styles_allowed": ["clean_white"]})
    ctx = caption_job(tmp_home, [copy_id], style="punch_yellow")
    CaptionsStage().run(ctx)
    assert ctx.read_json("captions/s1.json")["style"] == "clean_white"
    log = "\n".join(p.read_text(encoding="utf-8") for p in ctx.paths.logs.glob("*.jsonl"))
    assert "caption_style_not_in_format" in log


# ---------- the whole Short on fakes (FFmpeg: CI) ----------
def short_job(tmp_home: Path, monkeypatch: pytest.MonkeyPatch, formats_allowed: list[str]) -> Any:
    names = [s.name for s in SHORTS_MEDIA_STAGES]
    monkeypatch.setitem(FORMAT_STAGES, "media_only", names)
    ctx = make_ctx(
        tmp_home,
        fmt="media_only",
        combo=make_combo(length_bucket=23),
        channel=make_channel(formats_allowed=formats_allowed),
    )
    ctx.write_json(
        "parts.json",
        [
            {
                "n": 1,
                "text": TEXT,
                "hook": "My doorbell camera records someone every night.",
                "word_count": len(TEXT.split()),
                "est_seconds": 19.0,
                "cliffhanger": False,
            }
        ],
    )
    ctx.write_json(
        "visual_plan.json",
        [
            {
                "part": 1,
                "beat": 0,
                "sentence_start": 0,
                "sentence_end": 2,
                "keywords": ["doorbell", "dark porch"],
                "mood": "eerie",
                "need_vertical": True,
            },
            {
                "part": 1,
                "beat": 1,
                "sentence_start": 3,
                "sentence_end": 4,
                "keywords": ["open door", "night street"],
                "mood": "horror",
                "need_vertical": True,
            },
        ],
    )
    ctx.write_json(
        "seo.json",
        [
            {
                "title": "Scary Story: The Doorbell Camera",
                "description": "A camera records someone. Original fiction.",
                "tags": [f"tag{i}" for i in range(10)],
                "keywords": [],
                "hashtags": ["#Shorts", "#horror", "#scarystory"],
            }
        ],
    )
    assert run_job(ctx) == names
    return ctx


@needs_ffmpeg
def test_a_duplicated_short_format_shapes_the_story_short(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths.from_env()
    copy_id = duplicate(paths, "short_9x16", **SMALL)
    ctx = short_job(tmp_home, monkeypatch, [copy_id])
    report = ctx.read_json("render/report.json")["s1"]
    video = Path(report["path"])
    assert ff.video_size(video) == (720, 1280)
    assert report["format"] == copy_id and report["canvas"] == [720, 1280]
    assert report["format_why"] == "first allowed of that kind"
    ass = ctx.file("captions/s1.ass").read_text(encoding="utf-8")
    assert play_res(ass) == (720, 1280)
    assert int(style_fields(ass)[-2]) == anchored_margin("punch_yellow", 1280, 720, 50)
    qc = ctx.read_json("qc/s1.json")
    assert qc["passed"], qc
    assert qc["checks"]["aspect"]["limit"] == "720x1280" and qc["checks"]["aspect"]["passed"]
    out = Path(ctx.read_json("outbox/manifest.json")["s1"]["mp4"])
    assert ff.video_size(out) == (720, 1280)


@needs_ffmpeg
def test_an_edited_shipped_short_preset_changes_the_short(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths.from_env()
    paths.formats_dir.mkdir(parents=True, exist_ok=True)
    raw = yaml.safe_load((F.FORMATS_DIR / "short_9x16.yaml").read_text(encoding="utf-8"))
    raw["safe_margins"]["bottom"] = 800
    (paths.formats_dir / "short_9x16.yaml").write_text(yaml.safe_dump(raw), encoding="utf-8")
    ctx = short_job(tmp_home, monkeypatch, [])
    ass = ctx.file("captions/s1.ass").read_text(encoding="utf-8")
    assert int(style_fields(ass)[-2]) == 812
    assert ff.video_size(Path(ctx.read_json("render/report.json")["s1"]["path"])) == (1080, 1920)
    assert ctx.read_json("render/report.json")["s1"]["format_why"].startswith("default")


@needs_ffmpeg
def test_the_shipped_short_still_renders_1080x1920_with_todays_ass(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = short_job(tmp_home, monkeypatch, [])
    report = ctx.read_json("render/report.json")["s1"]
    assert ff.video_size(Path(report["path"])) == (1080, 1920) and report["canvas"] == [1080, 1920]
    ass = ctx.file("captions/s1.ass").read_text(encoding="utf-8")
    assert play_res(ass) == (1080, 1920)
    fields = style_fields(ass)
    assert int(fields[-2]) == 576 and int(fields[2]) == 92  # punch_yellow, today
    graph = ctx.file("render/s1.filtergraph.txt") if ctx.file("render/s1.filtergraph.txt").exists() else None
    script = next(ctx.file("shorts/s1").glob("*.filtergraph.txt"), graph)
    assert script is not None
    text = script.read_text(encoding="utf-8")
    assert "color=c=black" not in text and "[comp]" not in text and "s=1080x1920" in text
    assert ctx.read_json("qc/s1.json")["checks"]["aspect"]["limit"] == "1080x1920"


@needs_ffmpeg
def test_a_derived_short_uses_the_channels_short_format(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ns_studio.media import master as M

    paths = Paths.from_env()
    copy_id = duplicate(paths, "short_9x16", **SMALL)
    names = [s.name for s in MASTER_MEDIA_STAGES]
    monkeypatch.setitem(FORMAT_STAGES, "master_first", names)
    monkeypatch.setattr(M, "FLOOR_S", 20.0)
    monkeypatch.setattr(Q, "LONG_RANGE_S", (20.0, 200.0))
    monkeypatch.setattr(T, "make_thumbnails", fake_make_thumbnails)
    ctx = make_ctx(
        tmp_home,
        fmt="master_first",
        combo=make_combo(visual_treatment="cinematic_teal"),
        channel=make_channel(formats_allowed=[copy_id, "long_16x9"]),
    )
    ctx.write_json(
        "story.json",
        {
            "working_title": MASTER_TITLE,
            "chapters": [
                {"n": i, "title": f"Chapter title {i}", "text": chapter_text(chr(120 - i))} for i in (1, 2, 3)
            ],
        },
    )
    ctx.write_json(
        "shorts_plan.json",
        [
            {
                "k": 1,
                "chapter": 1,
                "sentence_start": 0,
                "sentence_end": 1,
                "hooks": ["Listen to this now."],
                "chosen_hook": "Listen to this opening line now.",
                "closing_line": "The full story is on the channel.",
                "combo_id": "c-test",
                "why_standalone": "x",
            },
        ],
    )
    ctx.write_json(
        "short_combos.json",
        {
            "1": make_combo(
                combo_id="c-s1",
                caption_style="clean_white",
                visual_treatment="handheld_grit",
                length_bucket=None,
            ).model_dump(mode="json")
        },
    )
    ctx.write_json(
        "visual_plan.json",
        [
            beat(1, 0, 0, 1, True),
            beat(1, 1, 2, 2, False),
            beat(2, 0, 0, 2, False),
            beat(3, 0, 0, 2, False),
        ],
    )
    ctx.write_json(
        "seo_long.json",
        {
            "title": MASTER_TITLE,
            "description": "A drive home goes wrong.\n\n[[CHAPTERS]]\n\nOriginal fiction.",
            "tags": [f"tag{i}" for i in range(10)],
            "hashtags": ["#horror", "#story", "#shorts"],
            "keywords": [],
            "chapter_titles": [{"n": i, "title": f"Chapter title {i}"} for i in (1, 2, 3)],
        },
    )
    ctx.write_json(
        "seo_shorts.json",
        [
            {
                "title": "Short 1: The Long Night",
                "description": "A hook.\nOriginal fiction.",
                "tags": [f"tag{i}" for i in range(10)],
                "hashtags": ["#Shorts", "#horror", "#story"],
                "keywords": [],
            }
        ],
    )
    assert run_job(ctx) == names
    assert ff.video_size(ctx.file("long/master.mp4")) == (
        1920,
        1080,
    )  # the master is unchanged (package P10b)
    entry = ctx.read_json("render/report.json")["s1"]
    assert entry["format"] == copy_id and entry["format_why"] == "first allowed of that kind"
    short = Path(entry["path"])
    assert ff.video_size(short) == (720, 1280)
    ass = ctx.file("captions/s1.ass").read_text(encoding="utf-8")
    assert play_res(ass) == (720, 1280)
    assert int(style_fields(ass)[-2]) == anchored_margin("clean_white", 1280, 720, 50)
    qc = ctx.read_json("qc/report.json")
    assert qc["s1"]["passed"] and qc["long"]["passed"], qc


@needs_ffmpeg
@pytest.mark.parametrize("inset_only", [False, True], ids=["blurred_fill_and_footage", "footage_on_black"])
def test_a_composed_short_format_renders_at_its_canvas_for_as_long_as_the_audio(
    tmp_path: Path, inset_only: bool
) -> None:
    """Regions drawn by the layout engine through `render_video`: the size is the canvas, the length the audio's."""
    src = []
    for i, (w, h) in enumerate(((1080, 1920), (1920, 1080))):
        clip = tmp_path / f"c{i}.mp4"
        ff.run(
            [
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={w}x{h}:rate=30:duration=4",
                "-pix_fmt",
                "yuv420p",
                str(clip),
            ]
        )
        src.append(clip)
    audio = tmp_path / "a.wav"
    ff.run(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono", "-t", "5", "-c:a", "pcm_s16le", str(audio)])
    raw = yaml.safe_load((F.FORMATS_DIR / "short_9x16.yaml").read_text(encoding="utf-8"))
    footage = {"name": "fg", "x": 40, "y": 300, "w": 1000, "h": 1320, "source": "full"}
    raw["layout"]["regions"] = (
        [footage]
        if inset_only
        else [{"name": "bg", "x": 0, "y": 0, "w": 1080, "h": 1920, "source": "blur_fill"}, footage]
    )
    fmt = Format.model_validate(raw)
    shots_ = [
        Shot(0, 0.0, 2.5, "t", "a", str(src[0]), 0.0, 1080, 1920, "u", "l", "k"),
        Shot(0, 2.5, 5.0, "t", "b", str(src[1]), 0.0, 1920, 1080, "u", "l", "k"),
    ]
    out = R.render_video(
        shots=shots_,
        audio=audio,
        out=tmp_path / "o.mp4",
        size=(1080, 1920),
        treatment=visual_treatment("clean_bright"),
        video=VideoCfg(encoder="libx264", preset="veryfast", fps=30),
        ass_path=None,
        fmt=fmt,
    )
    assert ff.video_size(out) == (1080, 1920)
    assert abs(ff.duration_s(out) - 5.0) < 0.25


@needs_ffmpeg
def test_a_clip_job_logs_a_hand_broken_clip_layout_instead_of_ignoring_it(
    tmp_home: Path, tmp_path: Path
) -> None:
    """The clip stages say `format_override_broken` when the owner's copy of the layout in use does not load."""
    import json

    from tests.test_clipper_stages import make_job, run_through

    paths = Paths.from_env()
    paths.formats_dir.mkdir(parents=True, exist_ok=True)
    (paths.formats_dir / "clip_gaming_facecam.yaml").write_text("id: clip_gaming_facecam\n", encoding="utf-8")
    ctx = make_job(tmp_path)
    run_through(ctx, "clip_captions")
    rows = [
        json.loads(ln)
        for f in paths.logs.glob("*.jsonl")
        for ln in f.read_text(encoding="utf-8").splitlines()
    ]
    broken = [r for r in rows if r["event"] == "format_override_broken"]
    assert broken and broken[0]["level"] == "WARN" and broken[0]["data"]["format_id"] == "clip_gaming_facecam"
    assert ctx.file("captions/c1.ass").exists()  # and the job went on with the shipped layout
