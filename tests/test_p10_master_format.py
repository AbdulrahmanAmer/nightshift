# ruff: noqa: E501
"""Package P10b: full videos (masters) draw their picture, captions and encoder from their format.

The acceptance is at the effect. A duplicate of the shipped long preset with another frame size, caption
margin, frame rate and encoder preset is chosen on a channel (`formats_allowed`), the chapter render runs
against a recording stand-in for FFmpeg and the text it would hand to FFmpeg (arguments, filter graph, title
card, caption files) is read. The same text from the shipped presets is pinned by `tests/test_p10b_golden.py`.
The tests that really render and ffprobe the file are `needs_ffmpeg` and run in CI.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from ns_common import ffmpeg as ff
from ns_common.paths import Paths
from ns_common.tts_client import FakeTts, wav_seconds
from ns_studio.media import formats as F
from ns_studio.media import job_formats as JF
from ns_studio.media import longform as L
from ns_studio.media import master as M
from ns_studio.media.caption_plan import AssOptions
from ns_studio.media.master import ChapterTiming
from ns_studio.media.presets import caption_style, chapter_card, visual_treatment
from ns_studio.media.shotplan import Shot
from tests.golden.p10b.make_golden import Recorder
from tests.helpers import make_channel, make_combo, make_ctx
from tests.test_master import write_story
from tests.test_p10_formats_core import put

needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")

HD = {
    "canvas": {"w": 1280, "h": 720, "fps": 24},
    "layout": {
        "seam_captions": False,
        "regions": [{"name": "main", "x": 0, "y": 0, "w": 1280, "h": 720, "source": "full"}],
    },
    "safe_margins": {"top": 54, "bottom": 200},
    "encoder": {"preset": "slow"},
}
#: a picture inset over a blurred copy of itself: the first region covers the canvas, the second is smaller
INSET = {
    "canvas": {"w": 1280, "h": 720},
    "layout": {
        "seam_captions": False,
        "regions": [
            {"name": "back", "x": 0, "y": 0, "w": 1280, "h": 720, "source": "blur_fill"},
            {"name": "main", "x": 160, "y": 0, "w": 960, "h": 720, "source": "full"},
        ],
    },
}


def master_job(
    tmp_home: Path,
    formats_allowed: list[str],
    series: dict[str, Any] | None = None,
    style: str = "clean_white",
) -> Any:
    """A master-first job with a three-chapter story and its chapter audio (fakes, no FFmpeg)."""
    ctx = make_ctx(
        tmp_home,
        fmt="master_first",
        channel=make_channel(formats_allowed=formats_allowed),
        combo=make_combo(caption_style=style),
    )
    texts = write_story(ctx)
    manifest: dict[str, Any] = {}
    audio = ctx.subdir("audio")
    for i, text in enumerate(texts, start=1):
        wav = audio / f"c{i}.wav"
        FakeTts().speak(text, "Ryan", 1.0, wav)
        wav.with_suffix(".txt").write_text(text, encoding="utf-8")
        manifest[f"c{i}"] = {"path": str(wav), "seconds": round(wav_seconds(wav), 3)}
    ctx.write_json("audio/manifest.json", manifest)
    if series is not None:
        ctx.write_json("series_context.json", series)
    return ctx


def style_fields(ass: str) -> list[str]:
    return next(ln for ln in ass.splitlines() if ln.startswith("Style: Default,")).split(",")


def play_res(ass: str) -> tuple[int, int]:
    x, y = re.search(r"PlayResX: (\d+)", ass), re.search(r"PlayResY: (\d+)", ass)
    assert x and y
    return int(x.group(1)), int(y.group(1))


def chapter_shots(ctx: Any, first: float, size: tuple[int, int] = (1920, 1080)) -> list[Shot]:
    ends = [2.4, 4.7, 7.5, 9.6, 12.0]
    out, start = [], first
    for i, end in enumerate(ends):
        path = ctx.paths.cache_clips / "pexels" / f"{1000 + i}.mp4"
        out.append(
            Shot(
                0, start, first + end, "pexels", f"id{i}", str(path), 0.5 * i, size[0], size[1], "u", "l", "k"
            )
        )
        start = first + end
    return out


def render_chapter(
    ctx: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, card: bool = True
) -> tuple[Recorder, str]:
    """Render chapter 1 against the recording stand-in; returns it and the card's caption file text."""
    rec = Recorder()
    monkeypatch.setattr(ff, "run", rec.run)
    monkeypatch.setattr(ff, "ffmpeg_exe", lambda: str(tmp_path / "bin" / "ffmpeg.exe"))
    monkeypatch.setattr(ff, "_reads_filter_script_option", lambda _exe: True)
    monkeypatch.setattr(L, "fit_chapter_contrast", lambda *a, **k: [])
    ctx.subdir("captions")
    start = 3.0 if card else 0.0
    t = ChapterTiming(1, "One", 0.0, start, start + 12.0)
    L.render_segment(
        ctx,
        t,
        chapter_shots(ctx, start),
        visual_treatment("cinematic_teal"),
        chapter_card("midnight"),
        ctx.subdir("long") / "seg_1.mp4",
    )
    card_ass = ctx.file("captions/card_c1.ass")
    return rec, card_ass.read_text(encoding="utf-8") if card_ass.exists() else ""


# ---------- the title card ----------
def test_card_ass_takes_the_canvas_and_is_unchanged_at_1920x1080() -> None:
    card = chapter_card("midnight")
    today = L.card_ass("The Night Shift Begins", 2, card, 3.0)
    assert L.card_ass("The Night Shift Begins", 2, card, 3.0, canvas=(1920, 1080)) == today
    assert play_res(today) == (1920, 1080)
    small = L.card_ass("The Night Shift Begins", 2, card, 3.0, canvas=(1280, 720))
    assert play_res(small) == (1280, 720)
    title = next(ln for ln in small.splitlines() if ln.startswith("Style: Title,")).split(",")
    assert int(title[2]) == round(card.font_size * 1280 / 1920)  # font size follows the frame
    assert title[-4:-1] == ["80", "80", "0"]  # MarginL, MarginR (120 * 2/3) and MarginV
    number = next(ln for ln in small.splitlines() if ln.startswith("Dialogue:") and "CHAPTER" in ln)
    assert ",0,0,113,," in number  # the number sits 170 * 2/3 px above the middle
    square = L.card_ass("The Night Shift Begins", 2, card, 3.0, canvas=(1080, 1080))
    assert play_res(square) == (1080, 1080)


def test_card_source_follows_the_canvas_and_frame_rate() -> None:
    card = chapter_card("midnight")
    assert L.card_source(card, (1920, 1080), 30, 90) == [
        "-f",
        "lavfi",
        "-t",
        "3.50",
        "-i",
        "color=c=0x2A3350:s=1920x1080:r=30",
    ]
    assert L.card_source(card, (1080, 1080), 24, 48)[-1] == "color=c=0x2A3350:s=1080x1080:r=24"


# ---------- the picture graph ----------
def test_segment_graph_composes_regions_and_keeps_the_card_full_canvas(tmp_home: Path) -> None:
    paths = Paths.from_env()
    put(paths, "long_inset", base="long_16x9", **INSET)
    fmt = F.get_format("long_inset")
    shots = chapter_shots(make_ctx(tmp_home, fmt="master_first"), 3.0)
    edges = [0, 72, 141, 225, 288, 360]
    plain = L.build_segment_graph(shots, edges, 90, 30, visual_treatment("cinematic_teal"), None, None, None)
    graph = L.build_segment_graph(
        shots, edges, 90, 30, visual_treatment("cinematic_teal"), None, None, None, fmt=fmt
    )
    # the shots are drawn at the size of the picture region, then laid over the blurred copy
    assert "scale=960:720:force_original_aspect_ratio=increase,crop=960:720" in graph
    assert "boxblur" in graph and "overlay=160:0" in graph and "[comp]" in graph
    # the card is one chain over the whole frame: the same text with or without regions
    card_chain = plain.split(";", 1)[0]
    assert graph.split(";", 1)[0] == card_chain and "overlay" not in card_chain
    # colour and grain still never touch the card
    assert "[comp]eq=" in graph.replace("[comp]", "[comp]") and "[vc][vs]concat=n=2" in graph


def test_a_plain_long_format_draws_todays_segment_graph(tmp_home: Path) -> None:
    fmt = F.get_format("long_16x9")
    shots = chapter_shots(make_ctx(tmp_home, fmt="master_first"), 3.0)
    edges = [0, 72, 141, 225, 288, 360]
    t = visual_treatment("handheld_grit")
    assert L.build_segment_graph(shots, edges, 90, 30, t, None, None, None, fmt=fmt) == L.build_segment_graph(
        shots, edges, 90, 30, t, None, None, None
    )


# ---------- captions ----------
def test_master_captions_options_come_from_the_long_format(tmp_home: Path) -> None:
    ctx = master_job(tmp_home, [])
    today = AssOptions(variant="landscape", safe_zones=(54, 54), mask_profanity=False)
    assert M.long_options(ctx) == today
    put(Paths.from_env(), "long_hd", base="long_16x9", **HD)
    ctx = master_job(tmp_home, ["long_hd"])
    opts = M.long_options(ctx)
    assert (opts.variant, opts.canvas) == ("landscape", (1280, 720))
    assert opts.safe_zones == (36, 133)  # 54 and 200 px at 1920x1080, as pixels of 1280x720


def test_the_master_captions_stage_writes_the_formats_geometry(tmp_home: Path) -> None:
    put(Paths.from_env(), "long_hd", base="long_16x9", **HD)
    ctx = master_job(tmp_home, ["long_hd"])
    M.run_captions_master(ctx)
    for name in ("captions/long_c1.ass", "captions/long.ass"):
        ass = ctx.file(name).read_text(encoding="utf-8")
        assert play_res(ass) == (1280, 720), name
        fields = style_fields(ass)
        assert int(fields[-3]) == round(caption_style("clean_white").landscape.margin_lr * 2 / 3)
    assert ctx.read_json("formats.json")["long"]["id"] == "long_hd"


def test_the_shipped_long_format_still_gives_todays_master_captions(tmp_home: Path) -> None:
    ctx = master_job(tmp_home, [])
    M.run_captions_master(ctx)
    ass = ctx.file("captions/long_c1.ass").read_text(encoding="utf-8")
    geo = caption_style("clean_white").landscape
    fields = style_fields(ass)
    assert play_res(ass) == (1920, 1080)
    assert (int(fields[2]), int(fields[-3])) == (geo.font_size, geo.margin_lr)
    assert ctx.read_json("formats.json")["long"]["id"] == "long_16x9"


def test_a_format_that_limits_the_caption_styles_swaps_a_master_style(tmp_home: Path) -> None:
    put(Paths.from_env(), "long_min", base="long_16x9", captions={"styles_allowed": ["minimal_lower"]})
    ctx = master_job(tmp_home, ["long_min"], style="clean_white")
    M.run_captions_master(ctx)
    assert ctx.read_json("captions/long.json")["style"] == "minimal_lower"


def test_the_contrast_check_samples_the_formats_frame(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(Paths.from_env(), "long_hd", base="long_16x9", **HD)
    ctx = master_job(tmp_home, ["long_hd"])
    seen: dict[str, Any] = {}

    def fake_luma(shots: Any, band: Any, size: Any, total_s: float, **_k: Any) -> list[float]:
        seen.update(size=size, band=band)
        return []

    monkeypatch.setattr(L, "backdrop_luma", fake_luma)
    t = ChapterTiming(1, "One", 0.0, 3.0, 15.0)
    L.fit_chapter_contrast(ctx, t, chapter_shots(ctx, 3.0))
    assert seen["size"] == (1280, 720)
    band = seen["band"]
    assert band.x + band.w <= 1280 and band.y + band.h <= 720


# ---------- the clips a master is cut from ----------
def run_clips(ctx: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Run the master clips branch with the clip search replaced by a recorder."""
    seen: dict[str, Any] = {"wanted": set(), "lengths": []}

    def fake_assign(*, slots: Any, wanted: str, **_k: Any) -> list[Shot]:
        seen["wanted"].add(wanted)
        seen["lengths"] += [round(s.end - s.start, 3) for s in slots]
        return []

    monkeypatch.setattr(L, "assign_clips", fake_assign)
    M.run_captions_master(ctx)  # writes the aligned words the clip planner reads
    ctx.write_json(
        "visual_plan.json",
        [
            {
                "chapter": n,
                "beat": 0,
                "sentence_start": 0,
                "sentence_end": 2,
                "keywords": ["night road"],
                "mood": "eerie",
                "need_vertical": False,
            }
            for n in (1, 2, 3)
        ],
    )
    L.run_clips_master(ctx)
    return seen


def test_master_clips_are_cut_to_the_formats_shot_range(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(Paths.from_env(), "long_fast", base="long_16x9", shots={"min_s": 2, "max_s": 3})
    seen = run_clips(master_job(tmp_home, ["long_fast"]), monkeypatch)
    assert seen["lengths"] and max(seen["lengths"]) <= 3.0 + 1e-6 and seen["wanted"] == {"landscape"}


def test_the_shipped_long_format_keeps_todays_shot_lengths(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = master_job(tmp_home, [])
    seen = run_clips(ctx, monkeypatch)
    lo, hi = visual_treatment(ctx.combo.visual_treatment).long_cut_range_s
    assert seen["lengths"] and all(lo - 1e-6 <= n <= hi + 1e-6 for n in seen["lengths"][:-1])
    assert min(seen["lengths"]) >= 0


def test_a_portrait_full_video_format_looks_for_portrait_footage(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = {
        "seam_captions": False,
        "regions": [{"name": "main", "x": 0, "y": 0, "w": 1080, "h": 1920, "source": "full"}],
    }
    put(Paths.from_env(), "long_tall", base="long_16x9", canvas={"w": 1080, "h": 1920}, layout=layout)
    seen = run_clips(master_job(tmp_home, ["long_tall"]), monkeypatch)
    assert seen["wanted"] == {"portrait"}


# ---------- the chapter render (recording stand-in for FFmpeg) ----------
def test_the_chapter_render_of_a_duplicated_long_format_takes_its_frame_rate_encoder_and_size(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(Paths.from_env(), "long_hd", base="long_16x9", **HD)
    ctx = master_job(tmp_home, ["long_hd"])
    rec, card_ass = render_chapter(ctx, monkeypatch, tmp_path)
    ((args, graph),) = rec.calls
    assert "color=c=0x2A3350:s=1280x720:r=24" in args  # the title card's colour source
    assert graph.count("scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720") == 5
    assert "1920" not in graph and "fps=24" in graph and "fps=30" not in graph
    assert args[args.index("-r") + 1] == "24" and args[args.index("-preset") + 1] == "slow"
    assert play_res(card_ass) == (1280, 720)
    assert ctx.read_json("formats.json")["long"]["id"] == "long_hd"


def test_a_square_format_draws_a_square_full_video(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = master_job(tmp_home, ["square_1x1"])
    rec, card_ass = render_chapter(ctx, monkeypatch, tmp_path)
    ((args, graph),) = rec.calls
    assert "color=c=0x2A3350:s=1080x1080:r=30" in args
    assert graph.count("scale=1080:1080:force_original_aspect_ratio=increase,crop=1080:1080") == 5
    assert "1920" not in graph
    assert play_res(card_ass) == (1080, 1080)
    assert JF.job_format(ctx, "long").kind == "square"
    assert ctx.read_json("formats.json")["long"]["why"] == "first allowed square format"


def test_a_composed_long_format_places_the_regions_on_the_canvas(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(Paths.from_env(), "long_inset", base="long_16x9", **INSET)
    ctx = master_job(tmp_home, ["long_inset"])
    rec, _ = render_chapter(ctx, monkeypatch, tmp_path)
    ((_args, graph),) = rec.calls
    assert "overlay=160:0" in graph and "scale=960:720" in graph and "[comp]" in graph


def test_the_chapter_render_without_a_card_has_no_card_source(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    put(Paths.from_env(), "long_hd", base="long_16x9", **HD)
    ctx = master_job(tmp_home, ["long_hd"])
    rec, card_ass = render_chapter(ctx, monkeypatch, tmp_path, card=False)
    ((args, graph),) = rec.calls
    assert not any(a.startswith("color=") for a in args) and card_ass == ""
    assert "[vc]" not in graph and "scale=1280:720" in graph


# ---------- jobs that started under P10a ----------
def p10a_record(ctx: Any) -> None:
    """The `formats.json` P10a wrote for a master-first job: a `short` entry and no `long` one."""
    doc = JF.prepare_job_formats(ctx)
    assert doc is not None
    ctx.file("formats.json").unlink()
    ctx.write_json("formats.json", {"version": 1, "short": doc["short"]})


def test_a_job_started_under_p10a_gets_a_long_entry_when_it_resumes(tmp_home: Path) -> None:
    put(Paths.from_env(), "long_hd", base="long_16x9", **HD)
    ctx = master_job(tmp_home, ["long_hd"])
    p10a_record(ctx)
    assert "long" not in json.loads(ctx.file("formats.json").read_text(encoding="utf-8"))
    fmt = JF.job_format(ctx, "long")  # used to stop with "has no long format"
    assert fmt.id == "long_hd"
    doc = json.loads(ctx.file("formats.json").read_text(encoding="utf-8"))
    assert (
        doc["long"]["id"] == "long_hd"
        and "started before full videos read their format" in doc["long"]["note"]
    )
    assert doc["short"]["id"] == "short_9x16"  # what the job already chose is not chosen again


def test_a_p10a_job_with_finished_stages_keeps_the_shipped_long_format(tmp_home: Path) -> None:
    put(Paths.from_env(), "long_hd", base="long_16x9", **HD)  # made after the job's captions were written
    ctx = master_job(tmp_home, ["long_hd"])
    p10a_record(ctx)
    ctx.write_json("stage_state.json", {"voice": {"status": "done", "finished_at": "x", "inputs_hash": "y"}})
    fmt = JF.job_format(ctx, "long")
    assert fmt.canvas.w == 1920 and fmt.id == "long_16x9"
    entry = json.loads(ctx.file("formats.json").read_text(encoding="utf-8"))["long"]
    assert entry["legacy"] is True and entry["source"] == "shipped"


def test_a_job_with_no_record_at_all_picks_both_roles_once(tmp_home: Path) -> None:
    put(Paths.from_env(), "long_hd", base="long_16x9", **HD)
    ctx = master_job(tmp_home, ["long_hd"])
    doc = JF.prepare_job_formats(ctx)
    assert doc is not None and set(doc) == {"version", "short", "long"}
    assert doc["long"]["why"] == "first allowed of that kind" and doc["short"]["id"] == "short_9x16"


# ---------- the whole master on fakes (FFmpeg: CI) ----------
def master_pipeline(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, allowed: list[str], series: dict[str, Any] | None = None
) -> Any:
    from ns_studio.media import qc as Q
    from ns_studio.media import thumbnails as T
    from ns_studio.media.stages_media import MASTER_MEDIA_STAGES
    from ns_studio.orchestrator import FORMAT_STAGES, run_job
    from tests.test_master_pipeline import MASTER_TITLE, beat, chapter_text, fake_make_thumbnails

    names = [s.name for s in MASTER_MEDIA_STAGES]
    monkeypatch.setitem(FORMAT_STAGES, "master_first", names)
    monkeypatch.setattr(M, "FLOOR_S", 20.0)
    monkeypatch.setattr(Q, "LONG_RANGE_S", (20.0, 200.0))
    monkeypatch.setattr(T, "make_thumbnails", fake_make_thumbnails)
    ctx = make_ctx(
        tmp_home,
        fmt="master_first",
        combo=make_combo(visual_treatment="cinematic_teal"),
        channel=make_channel(formats_allowed=allowed),
    )
    if series is not None:
        ctx.write_json("series_context.json", series)
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
            {
                "k": 2,
                "chapter": 2,
                "sentence_start": 1,
                "sentence_end": 2,
                "hooks": ["Nobody believed me."],
                "chosen_hook": "Nobody believed me at first.",
                "closing_line": "Watch the whole thing on the channel.",
                "combo_id": "c-test",
                "why_standalone": "x",
            },
        ],
    )
    short_combos = {
        "1": make_combo(
            combo_id="c-s1", caption_style="clean_white", visual_treatment="handheld_grit", length_bucket=None
        ),
        "2": make_combo(
            combo_id="c-s2", caption_style="boxed_dark", visual_treatment="clean_bright", length_bucket=None
        ),
    }
    ctx.write_json("short_combos.json", {k: c.model_dump(mode="json") for k, c in short_combos.items()})
    ctx.write_json(
        "visual_plan.json",
        [
            beat(1, 0, 0, 1, True),
            beat(1, 1, 2, 2, False),
            beat(2, 0, 0, 0, False),
            beat(2, 1, 1, 2, True),
            beat(3, 0, 0, 2, False),
        ],
    )
    ctx.write_json(
        "seo_long.json",
        {
            "title": MASTER_TITLE,
            "description": "A drive home goes wrong.\nNothing is as it seems.\n\n[[CHAPTERS]]\n\nOriginal fiction.",
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
                "title": f"Short {k}: The Long Night",
                "description": "A hook.\nOriginal fiction.",
                "tags": [f"tag{i}" for i in range(10)],
                "hashtags": ["#Shorts", "#horror", "#story"],
                "keywords": [],
            }
            for k in (1, 2)
        ],
    )
    assert run_job(ctx) == names
    return ctx


@needs_ffmpeg
def test_a_duplicated_long_format_shapes_the_master(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    put(Paths.from_env(), "long_hd", base="long_16x9", **HD)
    ctx = master_pipeline(tmp_home, monkeypatch, ["long_hd", "short_9x16"])
    info = ctx.read_json("render/long.json")
    master = Path(info["path"])
    assert ff.video_size(master) == (1280, 720)
    assert info["format"] == "long_hd" and info["canvas"] == [1280, 720]
    assert play_res(ctx.file("captions/long_c1.ass").read_text(encoding="utf-8")) == (1280, 720)
    assert play_res(ctx.file("captions/card_c2.ass").read_text(encoding="utf-8")) == (
        1280,
        720,
    )  # a title card
    qc = ctx.read_json("qc/long.json")
    assert qc["checks"]["aspect"]["limit"] == "1280x720" and qc["checks"]["aspect"]["passed"], qc
    assert ff.video_size(Path(ctx.read_json("render/report.json")["s1"]["path"])) == (
        1080,
        1920,
    )  # the Shorts stay 9:16
    video_stream = next(st for st in ff.probe(master)["streams"] if st.get("codec_type") == "video")
    assert video_stream["r_frame_rate"] == "24/1"


@needs_ffmpeg
def test_the_shipped_long_format_still_gives_todays_master(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = master_pipeline(tmp_home, monkeypatch, [])
    info = ctx.read_json("render/long.json")
    assert ff.video_size(Path(info["path"])) == (1920, 1080) and info["canvas"] == [1920, 1080]
    ass = ctx.file("captions/long_c1.ass").read_text(encoding="utf-8")
    assert play_res(ass) == (1920, 1080)
    qc = ctx.read_json("qc/long.json")
    assert qc["checks"]["aspect"]["limit"] == "1920x1080" and qc["passed"]


@needs_ffmpeg
def test_a_square_master_renders_1080x1080(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = master_pipeline(tmp_home, monkeypatch, ["square_1x1", "short_9x16"])
    info = ctx.read_json("render/long.json")
    assert ff.video_size(Path(info["path"])) == (1080, 1080)
    qc = ctx.read_json("qc/long.json")
    assert qc["checks"]["aspect"]["limit"] == "1080x1080" and qc["checks"]["aspect"]["passed"], qc
    assert ff.video_size(Path(ctx.read_json("render/report.json")["s1"]["path"])) == (1080, 1920)
