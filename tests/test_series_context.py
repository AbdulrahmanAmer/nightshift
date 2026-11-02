"""A series part uses its design: voice, styles, title, end card, cadence and runtime."""

from pathlib import Path

import pytest

from ns_common.models import ChapterTitle, Seo, SeoLong
from ns_studio.media.outbox import publish_after, series_delay_days
from ns_studio.media.qc import LONG_RANGE_S, duration_check, series_max_s
from ns_studio.media.voice import job_speed, job_voice
from ns_studio.text.seo import finalize_seo, finalize_seo_long
from ns_studio.variation.stages import ShortCombosStage, job_series_fixed, series_fixed
from tests.helpers import make_ctx

DESIGN = {
    "format": "long_16x9", "visual_treatment": "cinematic_teal", "caption_style": "boxed_dark",
    "voice": {"mode": "preset", "speaker": "Aiden", "speed": 1.1}, "thumbnail_style": "auto",
    "publish_cadence_days": 7,
}
CONTEXT = {
    "series_slug": "night-bus", "series_title": "The Night Bus", "part_n": 3, "parts_total": 8,
    "title": "The Night Bus - Part 03", "short_titles": [], "end_card": "Next: Part 4, The Driver.",
    "publish_not_before_days": 14, "minutes": 20, "design": DESIGN,
}


def _series_ctx(tmp_home: Path, **kw):
    ctx = make_ctx(tmp_home, fmt="master_first", **kw)
    ctx.write_json("series_context.json", CONTEXT)
    return ctx


def test_ctx_series_is_empty_for_ordinary_jobs(tmp_home: Path):
    assert make_ctx(tmp_home).series == {}
    assert _series_ctx(tmp_home).series["part_n"] == 3


def test_the_designed_voice_replaces_the_channel_voice(tmp_home: Path):
    plain = make_ctx(tmp_home)
    assert job_voice(plain) == plain.channel.voice
    ctx = _series_ctx(tmp_home)
    assert job_voice(ctx).speaker == "Aiden" and job_speed(ctx) == pytest.approx(1.1)
    ctx.write_json("series_context.json", {**CONTEXT, "design": {**DESIGN, "voice": None}})
    assert job_voice(ctx) == ctx.channel.voice  # a design without a voice keeps the channel's


def test_the_design_pins_caption_style_and_treatment(tmp_home: Path):
    ctx = _series_ctx(tmp_home)
    assert series_fixed(ctx.series) == {"caption_style": "boxed_dark", "visual_treatment": "cinematic_teal"}
    assert job_series_fixed(ctx.job_dir) == series_fixed(ctx.series)
    assert series_fixed({}) == {}
    ctx.write_json("shorts_plan.json", [
        {"k": 1, "length_bucket": 50, "combo_id": ctx.combo.combo_id},
        {"k": 2, "length_bucket": 35, "combo_id": ctx.combo.combo_id},
    ])
    ShortCombosStage().run(ctx)
    combos = ctx.read_json("short_combos.json")
    assert {c["caption_style"] for c in combos.values()} == {"boxed_dark"}
    assert {c["visual_treatment"] for c in combos.values()} == {"cinematic_teal"}


def test_master_title_description_and_end_card():
    raw = SeoLong(title="The Driver Who Never Spoke", description="A bus.\nA driver.", tags=["a"] * 10,
                  hashtags=["#a", "#b", "#c"], keywords=[], chapter_titles=[ChapterTitle(n=1, title="One")])
    out = finalize_seo_long(raw, primary_keyword="night bus", fiction_label=True, series=CONTEXT)
    assert out.title.startswith("The Night Bus - Part 03: ") and len(out.title) <= 100
    summary = out.description.split("\n\n")[0].splitlines()
    assert "Part 3 of 8." in summary and "Next: Part 4, The Driver." in summary
    plain = finalize_seo_long(raw, primary_keyword="night bus", fiction_label=True)
    assert "Part" not in plain.title and "Part 3" not in plain.description


def test_short_titles_and_descriptions_carry_the_part():
    raw = Seo(title="He never spoke", description="A hook.", tags=["a"] * 10,
              hashtags=["#Shorts", "#a", "#b"], keywords=[])
    out = finalize_seo(raw, primary_keyword="night bus", fiction_label=True, part_n=3, parts_total=8,
                       master_title="The Night Bus - Part 03: X")
    assert "(Part 3)" in out.title and "Part 3 of 8." in out.description


def test_parts_wait_for_their_slot_in_the_series(tmp_home: Path):
    ctx = _series_ctx(tmp_home)
    assert series_delay_days(ctx) == 14 and series_delay_days(make_ctx(tmp_home)) == 0
    base = publish_after(ctx.channel, ctx.job_id, "long", day_offset=1)
    later = publish_after(ctx.channel, ctx.job_id, "long", day_offset=1 + series_delay_days(ctx))
    assert later[:10] != base[:10] and later[11:] == base[11:]


def test_series_parts_get_their_own_qc_length_bound():
    """Review clipper row 5: a 15 minute part renders to about 16 minutes; master QC failed it at 15:00."""
    default_hi = LONG_RANGE_S[1]
    assert not duration_check(16 * 60, "long", None).passed
    assert duration_check(16 * 60, "long", None, series_max_s(15)).passed
    assert series_max_s(15) == pytest.approx(15 * 60 * 1.15)
    assert series_max_s(12) == default_hi  # never stricter than an ordinary master
    assert series_max_s(25) == pytest.approx(25 * 60 * 1.15)
    assert not duration_check(30 * 60, "long", None, series_max_s(25)).passed
    assert not duration_check(8 * 60, "long", None, series_max_s(15)).passed  # the floor stays

