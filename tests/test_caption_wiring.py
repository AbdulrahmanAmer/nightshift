"""Spec 17.2 wiring: the stages pass safe zones, avoid regions, emphasis, speakers, profanity and contrast."""

from __future__ import annotations

from pathlib import Path

import pytest

from ns_common import ffmpeg as ff
from ns_common.tts_client import FakeTts, wav_seconds
from ns_studio.clipper import layout as lay
from ns_studio.clipper import stages_clip as sc
from ns_studio.clipper.models import ClipItem, ClipLayout, Rect, Transcript
from ns_studio.media.caption_plan import (
    LANDSCAPE_SAFE_ZONES,
    AssOptions,
    channel_setting,
    emphasis_from_beats,
    emphasis_from_list,
    mask_profanity_setting,
    parse_emphasis,
    safe_zones_for,
    speaker_plan,
)
from ns_studio.media.captions import Box, CaptionsStage, caption_band, caption_placement
from ns_studio.media.presets import caption_style
from ns_studio.media.render import VERTICAL, backdrop_luma, fit_caption_contrast, sample_times, shot_at
from ns_studio.media.shotplan import Shot
from ns_studio.media.transcribe import Word
from tests.helpers import make_combo, make_ctx

needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")
SCRIPT = "The doorbell camera pings at 3:12 every night. I stopped checking the clips a week ago."


def _style_line(ass: str) -> list[str]:
    return next(ln for ln in ass.splitlines() if ln.startswith("Style: Default,")).split(",")


def _words(n: int = 8) -> list[Word]:
    return [Word(f"w{i}", i * 0.4, i * 0.4 + 0.3, 0) for i in range(n)]


# ---------- pure helpers ----------
def test_safe_zones_per_variant() -> None:
    assert safe_zones_for("vertical") == (220, 320)
    assert safe_zones_for("landscape") == LANDSCAPE_SAFE_ZONES


def test_parse_emphasis_accepts_mapping_and_lists_and_skips_junk() -> None:
    assert parse_emphasis({"0": "knife", 2: "door", "x": "bad", "3": ""}) == {0: "knife", 2: "door"}
    assert parse_emphasis([{"sentence": 1, "word": "blood"}, [4, "run"], {"word": "x"}, 7]) == {1: "blood", 4: "run"}
    assert parse_emphasis(None) == {} and parse_emphasis("nope") == {}


def test_emphasis_from_beats_filters_by_chapter_and_rebases_for_a_span() -> None:
    beats = [
        {"chapter": 1, "emphasis": {"0": "alpha", "3": "delta"}},
        {"chapter": 2, "emphasis": {"1": "beta", "2": "gamma", "5": "late"}},
        {"chapter": 2, "emphasis": {"1": "dupe"}},
        {"chapter": 2},
    ]
    assert emphasis_from_beats(beats, chapter=1) == {0: "alpha", 3: "delta"}
    assert emphasis_from_beats(beats, chapter=2) == {1: "beta", 2: "gamma", 5: "late"}
    # a Short covering sentences 1-3 of chapter 2: sentence 1 becomes 0, sentence 5 is outside the span
    assert emphasis_from_beats(beats, chapter=2, shift=1, last=3) == {0: "beta", 1: "gamma"}
    assert emphasis_from_beats([{"part": 2, "emphasis": {"0": "x"}}], part=2) == {0: "x"}
    assert emphasis_from_beats([{"part": 2, "emphasis": {"0": "x"}}], part=1) == {}


def test_emphasis_from_list_is_one_word_per_sentence_in_order() -> None:
    assert emphasis_from_list(["first", "", " third "]) == {0: "first", 2: "third"}
    assert emphasis_from_list(None) == {}


def test_speaker_plan_two_speakers_split_groups_and_map_colours() -> None:
    words = _words(8)
    mapping, breaks = speaker_plan(words, [0, 0, 0, 0, 1, 1, 1, 1], "clean_white")
    assert breaks == [4]
    assert set(mapping.values()) == {0, 1}
    assert mapping[0] == 0 and mapping[max(mapping)] == 1
    # None continues the previous speaker (and the first speaker backfills leading gaps)
    mapping2, breaks2 = speaker_plan(words, [None, 0, None, 0, None, 1, None, 1], "clean_white")
    assert breaks2 == [5] and set(mapping2.values()) == {0, 1}


def test_speaker_plan_single_speaker_is_off() -> None:
    assert speaker_plan(_words(4), [1, 1, None, 1], "clean_white") == ({}, [])
    assert speaker_plan(_words(4), [None] * 4, "clean_white") == ({}, [])
    with pytest.raises(ValueError):
        speaker_plan(_words(4), [0], "clean_white")


def test_ass_options_round_trip_and_build_matches_build_ass() -> None:
    opts = AssOptions(
        variant="vertical", safe_zones=(220, 320), avoid_regions=(Box(0, 1000, 1080, 500),),
        emphasis={0: "w1"}, speakers={0: 1}, speaker_breaks=(4,), mask_profanity=True, anchor_y_pct=40.0,
    )
    assert AssOptions.from_json(opts.to_json()) == opts
    assert AssOptions.from_json(None) == AssOptions() and AssOptions.from_json({}) == AssOptions()
    ass = opts.build(_words(), "clean_white")
    assert "fscx115" in ass
    plain = AssOptions().build(_words(), "clean_white")
    assert "fscx115" not in plain


def test_anchor_places_text_centre_and_free_band_search_still_applies() -> None:
    geo = caption_style("clean_white").vertical
    margin, block = caption_placement(geo, "vertical", anchor_y_pct=50.0)
    assert margin == round(1920 * 0.5 - 0.6 * geo.font_size) and block == int(geo.font_size * 1.2 * 2)
    cam = Box(0, 0, 1080, 800)
    moved, _ = caption_placement(geo, "vertical", avoid_regions=[cam], safe_zones=(220, 320), anchor_y_pct=20.0)
    band = caption_band("clean_white", "vertical", avoid_regions=[cam], safe_zones=(220, 320), anchor_y_pct=20.0)
    assert band.y >= 800 and band.y + band.h <= 1600 and 1920 - moved == band.y + band.h
    assert band.x == geo.margin_lr and band.w == 1080 - 2 * geo.margin_lr


def test_sample_times_and_shot_at() -> None:
    assert sample_times(8.0) == [2.0, 4.0, 6.0]
    shots = [
        Shot(0, 0.0, 3.0, "s", "a", "a.mp4", 1.0, 640, 360, "", "", "k"),
        Shot(1, 3.0, 8.0, "s", "b", "b.mp4", 10.0, 640, 360, "", "", "k"),
    ]
    first, t1 = shot_at(shots, 2.0)
    assert first.clip_id == "a" and t1 == pytest.approx(3.0)
    second, t2 = shot_at(shots, 4.0)
    assert second.clip_id == "b" and t2 == pytest.approx(11.0)
    assert shot_at(shots, 99.0)[0].clip_id == "b"


def test_channel_setting_reads_the_model_then_the_yaml_then_the_default(tmp_home: Path) -> None:
    ctx = make_ctx(tmp_home)
    assert channel_setting(ctx.paths, ctx.channel, "captions.mask_profanity", False) is False
    assert mask_profanity_setting(ctx.paths, ctx.channel) is False
    assert channel_setting(ctx.paths, ctx.channel, "slug") == "after-dark"
    f = ctx.paths.channel_file(ctx.channel.slug)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("slug: after-dark\ncaptions:\n  mask_profanity: true\n", encoding="utf-8")
    assert mask_profanity_setting(ctx.paths, ctx.channel) is True
    f.write_text(": : not yaml [", encoding="utf-8")
    assert mask_profanity_setting(ctx.paths, ctx.channel) is False


# ---------- shorts_only captions stage ----------
def _prepare_audio(ctx, text: str) -> None:
    ctx.write_json("parts.json", [{"n": 1, "text": text, "hook": "", "word_count": len(text.split()),
                                   "est_seconds": 5.0, "cliffhanger": False}])
    audio = ctx.subdir("audio")
    wav = audio / "p1.wav"
    FakeTts().speak(text, "Ryan", 1.0, wav)
    wav.with_suffix(".txt").write_text(text, encoding="utf-8")
    ctx.write_json("audio/manifest.json", {"p1": {"path": str(wav), "seconds": round(wav_seconds(wav), 3)}})


def test_shorts_captions_pass_safe_zones_emphasis_and_mask(tmp_home: Path) -> None:
    ctx = make_ctx(tmp_home, combo=make_combo(caption_style="clean_white"))
    _prepare_audio(ctx, "The doorbell camera pings at night. Then the damn light goes out.")
    ctx.write_json("visual_plan.json", [
        {"part": 1, "beat": 0, "sentence_start": 0, "sentence_end": 1, "keywords": ["door"], "mood": "dark",
         "emphasis": {"0": "doorbell", "1": "light"}},
    ])
    f = ctx.paths.channel_file(ctx.channel.slug)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("slug: after-dark\ncaptions:\n  mask_profanity: true\n", encoding="utf-8")
    CaptionsStage().run(ctx)
    ass = ctx.file("captions/s1.ass").read_text(encoding="utf-8")
    assert "fscx115" in ass  # emphasised key words
    assert "d***" in ass and "damn" not in ass  # masked
    tl = ctx.read_json("captions/s1.json")
    assert tl["ass_opts"]["safe_zones"] == [220, 320]
    assert tl["ass_opts"]["emphasis"] == {"0": "doorbell", "1": "light"}
    assert tl["ass_opts"]["mask_profanity"] is True and tl["ass_opts"]["avoid_regions"] == []
    # the same options rebuild the same file (the renderer relies on this)
    words = [Word.from_json(w) for w in tl["words"]]
    assert AssOptions.from_json(tl["ass_opts"]).build(words, tl["style"]).count("fscx115") == ass.count("fscx115")


def test_shorts_captions_without_plan_or_channel_options_stay_plain(tmp_home: Path) -> None:
    ctx = make_ctx(tmp_home, combo=make_combo(caption_style="clean_white"))
    _prepare_audio(ctx, SCRIPT)
    CaptionsStage().run(ctx)
    ass = ctx.file("captions/s1.ass").read_text(encoding="utf-8")
    assert "fscx115" not in ass
    tl = ctx.read_json("captions/s1.json")
    assert tl["ass_opts"]["emphasis"] == {} and tl["ass_opts"]["mask_profanity"] is False


# ---------- clip captions stage (synthetic job files, no video needed) ----------
CAM = Rect(x=944, y=16, w=320, h=180)


def _clip_job(tmp_home: Path, *, mask: bool = False):
    ctx = make_ctx(tmp_home, fmt="clip", combo=make_combo(caption_style="clean_white"))
    words = []
    segs = []
    texts = ["Hello there damn friend.", "This is truly great."]
    t = 0.0
    for i, text in enumerate(texts):
        start = t
        for tok in text.split():
            words.append({"w": tok, "start": round(t, 3), "end": round(t + 0.4, 3), "seg": i})
            t += 0.45
        segs.append({"i": i, "start": start, "end": t, "text": text, "speaker": i})
        t += 1.2
    tr = Transcript.model_validate({"source": "v.mp4", "duration_s": 20.0, "segments": segs, "words": words})
    ctx.write_json("transcript.json", tr.model_dump(mode="json"))
    ctx.write_json("source.json", {"path": str(ctx.job_dir / "missing.mp4"), "width": 1280, "height": 720,
                                   "duration_s": 20.0, "fps": 30.0, "source_id": "src-test"})
    clip = ClipItem(k=1, start_s=0.0, end_s=8.0, title="t", score=0.9, emphasis=["friend", "great"])
    ctx.write_json("clip_plan.json", [clip.model_dump(mode="json")])
    base = ClipLayout(template="gaming_facecam", facecam=CAM)
    crops = lay.plan_layout("gaming_facecam", 1280, 720, base, t_start=0.0, t_end=8.0)
    out = ClipLayout(template="gaming_facecam", facecam=CAM, crops=crops, per_clip={"1": "gaming_facecam"})
    ctx.write_json("clip_layout.json", out.model_dump(mode="json"))
    if mask:
        f = ctx.paths.channel_file(ctx.channel.slug)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("slug: after-dark\ncaptions:\n  mask_profanity: true\n", encoding="utf-8")
    return ctx


def test_clip_captions_stay_off_the_facecam_and_out_of_the_ui_zones(tmp_home: Path) -> None:
    ctx = _clip_job(tmp_home)
    sc.CaptionsStage().run(ctx)
    rep = ctx.read_json("captions/report.json")["c1"]
    assert rep["avoid_regions"] >= 1 and rep["speakers"] == 2
    assert rep["backdrop_luma"] == [] and rep["boxed"] is False  # the source file does not exist: best effort
    ass = ctx.file("captions/c1.ass").read_text(encoding="utf-8")
    margin_v = int(_style_line(ass)[-2])
    block_h = int(caption_style("clean_white").vertical.font_size * 1.2 * 2)
    cam_h = lay.canvas_panels(lay.load_templates()["gaming_facecam"], ClipLayout.model_validate(
        ctx.read_json("clip_layout.json")).crops[0])["cam"].h
    assert 1920 - margin_v - block_h >= cam_h  # below the camera panel
    assert 1920 - margin_v <= 1920 - 320  # above the bottom UI zone
    assert "fscx115" in ass  # "friend" and "great" emphasised
    assert "damn" in ass  # no masking unless the channel asks for it


def test_clip_captions_mask_profanity_when_the_channel_asks(tmp_home: Path) -> None:
    ctx = _clip_job(tmp_home, mask=True)
    sc.CaptionsStage().run(ctx)
    ass = ctx.file("captions/c1.ass").read_text(encoding="utf-8")
    assert "d***" in ass and "damn" not in ass


def test_clip_avoid_boxes_cover_the_camera_panel_and_mapped_faces() -> None:
    from ns_studio.clipper.models import FaceFrame

    tpl = lay.load_templates()["podcast_stacked"]
    left, right = Rect(x=150, y=200, w=100, h=100), Rect(x=850, y=200, w=100, h=100)
    layout = ClipLayout(
        template="podcast_stacked", faces=[FaceFrame(t=float(i), boxes=[left, right]) for i in range(6)]
    )
    crop = lay.plan_layout("podcast_stacked", 1280, 720, layout, t_start=0, t_end=5)[0]
    boxes = lay.avoid_boxes(tpl, crop, layout, 0.0, 5.0)
    assert len(boxes) == 2
    assert boxes[0].y < 960 <= boxes[1].y + boxes[1].h and boxes[1].y >= 960 - 1
    gaming = lay.load_templates()["gaming_facecam"]
    g_layout = ClipLayout(template="gaming_facecam", facecam=CAM)
    g_crop = lay.plan_layout("gaming_facecam", 1280, 720, g_layout, t_start=0, t_end=5)[0]
    cam_box = lay.avoid_boxes(gaming, g_crop, g_layout, 0.0, 5.0)[0]
    assert (cam_box.x, cam_box.y, cam_box.w) == (0, 0, 1080)


def test_band_in_source_maps_back_into_the_frame() -> None:
    tpl = lay.load_templates()["center_crop"]
    layout = ClipLayout(template="center_crop")
    crop = lay.plan_layout("center_crop", 1280, 720, layout, t_start=0, t_end=5)[0]
    boxes = lay.band_in_source(tpl, crop, Box(152, 1000, 776, 200), 1280, 720)
    assert len(boxes) == 1
    b = boxes[0]
    assert 0 <= b.x and b.x + b.w <= 1280 and 0 <= b.y and b.y + b.h <= 720 and b.w % 2 == 0 and b.h % 2 == 0
    assert lay.band_in_source(tpl, crop, Box(0, 0, 1080, 100), 1280, 720) == []  # above the foreground panel


# ---------- contrast sampling (real FFmpeg) ----------
def _flat_clip(path: Path, colour: str) -> Shot:
    ff.run(["-f", "lavfi", "-i", f"color=c={colour}:s=640x360:d=3:r=30", "-pix_fmt", "yuv420p", str(path)])
    return Shot(0, 0.0, 3.0, "s", "c", str(path), 0.0, 640, 360, "", "", "k")


@needs_ffmpeg
def test_backdrop_luma_and_boxed_fallback_on_a_bright_backdrop(tmp_path: Path) -> None:
    shot = _flat_clip(tmp_path / "white.mp4", "white")
    band = caption_band("clean_white", "vertical", safe_zones=(220, 320))
    lumas = backdrop_luma([shot], band, VERTICAL, 3.0)
    assert len(lumas) == 3 and all(y > 200 for y in lumas)
    opts = AssOptions(variant="vertical", safe_zones=(220, 320))
    words = _words()
    ass = tmp_path / "s1.ass"
    ass.write_text(opts.build(words, "clean_white"), encoding="utf-8")
    assert _style_line(ass.read_text(encoding="utf-8"))[15] == "1"
    got = fit_caption_contrast(
        words=words, style="clean_white", opts=opts, shots=[shot], total_s=3.0, size=VERTICAL, ass_path=ass
    )
    assert got == lumas
    assert _style_line(ass.read_text(encoding="utf-8"))[15] == "3"  # opaque box: white text on white is unreadable


@needs_ffmpeg
def test_dark_backdrop_keeps_the_style(tmp_path: Path) -> None:
    shot = _flat_clip(tmp_path / "black.mp4", "black")
    opts = AssOptions(variant="vertical", safe_zones=(220, 320))
    words = _words()
    ass = tmp_path / "s1.ass"
    original = opts.build(words, "clean_white")
    ass.write_text(original, encoding="utf-8")
    got = fit_caption_contrast(
        words=words, style="clean_white", opts=opts, shots=[shot], total_s=3.0, size=VERTICAL, ass_path=ass
    )
    assert got and all(y < 40 for y in got)
    assert ass.read_text(encoding="utf-8") == original


def test_missing_clip_file_never_fails_the_contrast_check(tmp_path: Path) -> None:
    shot = Shot(0, 0.0, 3.0, "s", "c", str(tmp_path / "nope.mp4"), 0.0, 640, 360, "", "", "k")
    ass = tmp_path / "s1.ass"
    ass.write_text("x", encoding="utf-8")
    got = fit_caption_contrast(
        words=_words(), style="clean_white", opts=AssOptions(), shots=[shot], total_s=3.0, size=VERTICAL,
        ass_path=ass,
    )
    assert got == [] and ass.read_text(encoding="utf-8") == "x"


# ---------- master captions ----------
@needs_ffmpeg
def test_master_captions_use_chapter_emphasis_and_keep_landscape_geometry(tmp_home: Path) -> None:
    from ns_studio.media.master import run_captions_master, run_voice_master
    from tests.test_master import write_shorts_plan, write_story

    ctx = make_ctx(tmp_home, fmt="master_first")
    write_story(ctx)
    write_shorts_plan(ctx)
    ctx.write_json("visual_plan.json", [
        {"chapter": 2, "beat": 0, "sentence_start": 0, "sentence_end": 0, "keywords": ["x"], "mood": "m",
         "emphasis": {"0": "wab"}},
    ])
    run_voice_master(ctx)
    run_captions_master(ctx)
    ch1 = ctx.file("captions/long_c1.ass").read_text(encoding="utf-8")
    ch2 = ctx.file("captions/long_c2.ass").read_text(encoding="utf-8")
    assert "fscx115" in ch2 and "fscx115" not in ch1
    assert "PlayResX: 1920" in ch2
    assert "fscx115" not in ctx.file("captions/long.ass").read_text(encoding="utf-8")
