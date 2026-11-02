from pathlib import Path

import pytest

from ns_common import ffmpeg as ff
from ns_studio.media.longform import (
    build_segment_graph,
    card_ass,
    chapters_json,
    concat_list_text,
    fit_chapter_contrast,
    fmt_timestamp,
    midroll_suggestions,
    pick_card,
    segment_edges,
)
from ns_studio.media.master import ChapterTiming
from ns_studio.media.outbox import chapter_list, long_description, short_description
from ns_studio.media.presets import chapter_card, load_chapter_cards, visual_treatment
from ns_studio.media.qc import chapters_check, midroll_check
from ns_studio.media.shotplan import Shot


def timeline(starts: list[float], total: float) -> list[ChapterTiming]:
    out = []
    for i, s in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else total
        out.append(ChapterTiming(i + 1, f"Title {i + 1}", s, s + (1.2 if i else 0.0), end))
    return out


def test_fmt_timestamp():
    assert fmt_timestamp(0) == "00:00" and fmt_timestamp(59.9) == "00:59" and fmt_timestamp(125) == "02:05"
    assert fmt_timestamp(3725) == "1:02:05"


def test_midroll_only_at_chapter_breaks_and_not_in_first_or_last_two_minutes():
    tl = timeline([0, 100, 200, 400, 600, 700], 760)
    # breaks: 100 (too early), 200, 400, 600, 700 (within 120 s of the end 760)
    assert midroll_suggestions(tl, 760) == [200.0, 400.0, 600.0]
    assert midroll_suggestions(tl, 760, edge_s=0) == [100.0, 200.0, 400.0, 600.0, 700.0]
    assert midroll_suggestions(timeline([0], 500), 500) == []


def test_chapters_json_timestamps_and_checks():
    tl = timeline([0, 130, 300, 520], 640)
    doc = chapters_json(tl, 640)
    assert [c["timestamp"] for c in doc["chapters"]] == ["00:00", "02:10", "05:00", "08:40"]
    assert doc["midroll_suggestions_s"] == [130.0, 300.0, 520.0]
    assert chapters_check(doc).passed and midroll_check(doc).passed
    bad = dict(doc, chapters=doc["chapters"][:2])
    assert not chapters_check(bad).passed  # fewer than 3
    short = chapters_json(timeline([0, 5, 200], 300), 300)
    assert not chapters_check(short).passed  # first chapter is 5 s
    early = dict(doc, midroll_suggestions_s=[60.0])
    assert not midroll_check(early).passed
    off_break = dict(doc, midroll_suggestions_s=[250.0])
    assert not midroll_check(off_break).passed


def test_chapter_cards_are_three_and_pick_is_deterministic():
    assert len(load_chapter_cards()) == 3
    assert pick_card("after-dark-20261101-ab12") == pick_card("after-dark-20261101-ab12")
    ass = card_ass("The Night Bus", 3, chapter_card("midnight"))
    assert "PlayResX: 1920" in ass and "The Night Bus" in ass and "CHAPTER 3" in ass
    assert "\\fad(250,250)" in ass
    assert "0:00:01.20" in ass  # card is 1.2 s, under the 1.5 s limit
    assert "CHAPTER" not in card_ass("X", 1, chapter_card("slate"))


def shot(i: int, start: float, end: float, w: int = 1920, h: int = 1080) -> Shot:
    return Shot(0, start, end, "fake", f"c{i}", f"clip{i}.mp4", 0.0, w, h, "u", "l", "kw")


def test_segment_edges_end_exactly_at_content_frames():
    shots = [shot(0, 100.0, 106.0), shot(1, 106.0, 112.3), shot(2, 112.3, 118.0)]
    aud = round(100.0 * 30)
    edges = segment_edges(shots, aud, round(118.0 * 30) - aud, 30)
    assert edges == [0, 180, 369, 540]


def test_segment_graph_with_and_without_card(tmp_path: Path):
    t = visual_treatment("dark_slowzoom")
    shots = [shot(0, 1.2, 7.2), shot(1, 7.2, 13.2)]
    g = build_segment_graph(shots, [0, 180, 360], 36, 30, t, tmp_path / "c.ass", tmp_path / "card.ass")
    assert g.startswith("[0:v]format=yuv420p,ass=filename=")
    assert "trim=end_frame=36" in g and "[v1][v2]concat=n=2:v=1:a=0[cat]" in g
    assert "[1:v]" in g and "[2:v]" in g and "[vc][vs]concat=n=2:v=1:a=0[cc]" in g
    assert "scale=1920:1080" in g and "boxblur" not in g and g.endswith("[vout]")
    first = build_segment_graph(shots, [0, 180, 360], 0, 30, t, None, None)
    assert "[vc]" not in first and "concat=n=2:v=1:a=0[cat]" in first and "[0:v]fps=30" in first


def test_concat_list_text():
    text = concat_list_text([Path("C:/a/seg_1.mp4"), Path("C:/a/it's/seg_2.mp4")])
    assert text == "file 'C:/a/seg_1.mp4'\n" + "file 'C:/a/it'" + "\\''s/seg_2.mp4'\n"  # the demuxer's close-escape-reopen rule


def test_descriptions_follow_spec_9_1_and_9_2():
    chapters = chapters_json(timeline([0, 130, 300], 460), 460)["chapters"]
    assert chapter_list(chapters).splitlines()[0] == "00:00 Title 1"
    with_placeholder = "Line one.\nLine two.\n\n[[CHAPTERS]]\n\nOriginal fiction."
    out = long_description(with_placeholder, chapters)
    assert "[[CHAPTERS]]" not in out and "02:10 Title 2" in out and out.endswith("Original fiction.")
    no_placeholder = long_description("Summary.\n\nOriginal fiction.", chapters)
    assert no_placeholder.index("00:00 Title 1") < no_placeholder.index("Original fiction.")
    s = short_description("A hook line.\nOriginal fiction.", "The Full Story")
    assert s.splitlines() == ["A hook line.", "From the full story: The Full Story", "Original fiction."]
    assert short_description(s, "The Full Story") == s
    assert short_description("Only text.", "T").endswith("From the full story: T")


def test_all_chapter_cards_are_at_most_1_5_seconds():
    from ns_studio.media.master import CARD_S

    assert CARD_S <= 1.5


@pytest.mark.parametrize("name", ["midnight", "ember", "slate"])
def test_card_ass_is_valid_for_every_style(name: str):
    ass = card_ass("A Title {with braces}", 2, chapter_card(name))
    assert "{with" not in ass and "(with braces)" in ass
    assert ass.count("Dialogue:") == (2 if chapter_card(name).show_number else 1)


@pytest.mark.skipif(not ff.available(), reason="ffmpeg required")
def test_master_captions_switch_to_the_boxed_style_on_bright_footage(tmp_home: Path):
    """System gap 16: only the Clipper looked at the footage behind the captions."""
    from tests.helpers import make_combo, make_ctx

    ctx = make_ctx(tmp_home, fmt="master_first", combo=make_combo(caption_style="clean_white"))
    clip = tmp_home / "white.mp4"
    ff.run(["-f", "lavfi", "-i", "color=c=white:s=1920x1080:r=25:d=4", "-pix_fmt", "yuv420p", str(clip)])
    ctx.write_json("words/c1.json", [
        {"w": "Hello", "start": 0.0, "end": 0.4, "sentence_idx": 0},
        {"w": "there.", "start": 0.5, "end": 0.9, "sentence_idx": 0},
    ])
    ctx.subdir("captions")
    ass = ctx.file("captions/long_c1.ass")
    ass.write_text("plain", encoding="utf-8")
    t = ChapterTiming(1, "One", 0.0, 0.0, 3.0)
    shot = Shot(0, 0.0, 3.0, "fake", "w", str(clip), 0.0, 1920, 1080, "u", "l", "kw")
    luma = fit_chapter_contrast(ctx, t, [shot])
    assert luma and min(luma) > 200
    assert ass.read_text(encoding="utf-8").startswith("[Script Info]")  # rewritten with the boxed style
    dark = tmp_home / "black.mp4"
    ff.run(["-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=25:d=4", "-pix_fmt", "yuv420p", str(dark)])
    ass.write_text("plain", encoding="utf-8")
    fit_chapter_contrast(ctx, t, [Shot(0, 0.0, 3.0, "fake", "b", str(dark), 0.0, 1920, 1080, "u", "l", "kw")])
    assert ass.read_text(encoding="utf-8") == "plain"  # white text on black keeps its style
