# ruff: noqa: E501
import pytest

from ns_common.models import Seo
from ns_studio.text.models_local import ChapterTitle, SeoLong
from ns_studio.text.seo import (
    CHAPTER_LIST_PLACEHOLDER,
    fill_chapter_list,
    finalize_seo,
    finalize_seo_long,
    format_timestamp,
    normalize_hashtags,
    normalize_tags,
    render_chapter_list,
)


def _long(**kw) -> SeoLong:
    base = {
        "title": "Horror Story: The Doorbell Camera",
        "description": "Line one.\nLine two.",
        "tags": ["horror story", "scary story"],
        "hashtags": ["#horrorstory", "#scarystories", "#creepy"],
        "keywords": ["k"],
        "chapter_titles": [ChapterTitle(n=1, title="One"), ChapterTitle(n=2, title="Two")],
    }
    return SeoLong(**(base | kw))


def test_long_title_has_no_part_and_keeps_keyword_early():
    out = finalize_seo_long(
        _long(title="The Doorbell Camera (Part 2) of my night"), primary_keyword="horror story", fiction_label=True
    )
    assert "part" not in out.title.lower()
    assert out.title.lower().startswith("horror story")
    assert len(out.title) <= 100
    out2 = finalize_seo_long(_long(title="Horror Story [Part 1]: Door"), primary_keyword="horror story", fiction_label=True)
    assert out2.title == "Horror Story: Door"


def test_long_description_layout_and_placeholder():
    out = finalize_seo_long(
        _long(description="One.\nTwo.\nThree.\nFour.\n00:00 Intro\n#hash\nOriginal fiction."),
        primary_keyword="horror story",
        fiction_label=True,
        creator_note="Thanks for listening.",
    )
    assert out.description == (
        f"One.\nTwo.\nThree.\n\n{CHAPTER_LIST_PLACEHOLDER}\n\nOriginal fiction.\nThanks for listening."
    )
    bare = finalize_seo_long(_long(description=""), primary_keyword="horror story", fiction_label=False)
    assert bare.description == f"An original horror story narrated in chapters.\n\n{CHAPTER_LIST_PLACEHOLDER}"


def test_long_hashtags_never_include_shorts_and_are_3_to_5():
    out = finalize_seo_long(
        _long(hashtags=["#Shorts", "#a", "#b", "#c", "#d", "#e", "#f"]), primary_keyword="horror story", fiction_label=True
    )
    assert "#Shorts" not in out.hashtags and len(out.hashtags) == 5
    few = finalize_seo_long(_long(hashtags=["#Shorts"]), primary_keyword="horror story", fiction_label=True)
    assert 3 <= len(few.hashtags) <= 5 and "#Shorts" not in few.hashtags


def test_long_tags_rules():
    out = finalize_seo_long(_long(tags=["x"]), primary_keyword="horror story", fiction_label=True)
    assert 10 <= len(out.tags) <= 20 and out.tags[0] == "horror story"
    many = [f"{'t' * 40}{i:02d}" for i in range(20)]
    capped = normalize_tags(many, "horror story")
    assert len(capped) <= 20 and sum(len(t) for t in capped) + len(capped) - 1 <= 500 or len(capped) == 10


def test_long_keeps_chapter_titles_and_keywords():
    out = finalize_seo_long(_long(), primary_keyword="horror story", fiction_label=True)
    assert [c.title for c in out.chapter_titles] == ["One", "Two"] and out.keywords == ["k"]


def test_format_timestamp_and_chapter_list():
    assert format_timestamp(0) == "00:00" and format_timestamp(75.9) == "01:15" and format_timestamp(3725) == "1:02:05"
    lines = render_chapter_list(
        [{"title": "One", "start_s": 4.2}, {"title": "Two ", "start_s": 130}, {"title": "Three", "start_s": 301}]
    )
    assert lines.splitlines() == ["00:00 One", "02:10 Two", "05:01 Three"]


def test_fill_chapter_list_replaces_placeholder_or_appends():
    chapters = [{"title": "One", "start_s": 0}, {"title": "Two", "start_s": 61}]
    desc = f"Summary.\n\n{CHAPTER_LIST_PLACEHOLDER}\n\nOriginal fiction."
    assert fill_chapter_list(desc, chapters) == "Summary.\n\n00:00 One\n01:01 Two\n\nOriginal fiction."
    assert fill_chapter_list("Summary.", chapters) == "Summary.\n\n00:00 One\n01:01 Two"


def test_normalize_hashtags_modes():
    assert normalize_hashtags(["#Shorts", "#a b", "A"], "scary story", shorts=True)[:3] == ["#Shorts", "#ab", "#A"]
    assert "#Shorts" not in normalize_hashtags(["#shorts", "x"], "scary story", shorts=False)
    assert normalize_hashtags([], "scary story", shorts=False)[0] == "#ScaryStory"


def test_finalize_seo_extra_lines_and_order():
    seo = Seo(title="Scary Story: X", description="Desc.", tags=["a"], hashtags=[], keywords=[])
    out = finalize_seo(
        seo,
        primary_keyword="scary story",
        fiction_label=True,
        part_n=None,
        parts_total=1,
        master_title="Master",
        extra_lines=["Clip from: talk.mp4"],
    )
    assert out.description.splitlines() == ["Desc.", "Clip from: talk.mp4", "From the full story: Master", "Original fiction."]


def test_seo_long_model_requires_chapter_titles():
    with pytest.raises(ValueError):
        SeoLong(title="t", description="d", tags=["a"], hashtags=[], chapter_titles=[])
