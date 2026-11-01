import json

import pytest

from ns_common.embed_client import FakeEmbed
from ns_common.errors import NsError
from ns_studio.text import validators as V
from ns_studio.text.budget import chapter_budget, short_budget

STORY = ("The doorbell camera pings at 3:12 every night. " * 12).strip()  # 108 words


def test_short_budget_math():
    b = short_budget(50, "standard")
    assert b.wpm == 155
    assert b.words == 129 - 12
    assert b.min_words == round(b.words * 0.8)
    assert b.max_words == round(b.words * 1.2)
    two = short_budget(75, "brisk", parts=2)
    assert two.words_per_part == two.words // 2


def test_length_check():
    b = short_budget(50, "standard")
    assert V.check_length("one two three", b) is not None
    assert V.check_length(" ".join(["word"] * b.words), b) is None
    assert V.check_length(" ".join(["word"] * (b.max_words + 1)), b) is not None


def test_banned_and_sentence_length():
    hit = V.check_banned("It was a dark and stormy night, truly.", V.load_banned())
    assert hit is not None and hit.rule == "banned_phrase"
    assert V.check_banned("A calm evening.", V.load_banned()) is None
    long = " ".join(["word"] * 40) + "."
    v = V.check_sentence_length(long)
    assert v is not None and v.rule == "sentence_length"
    assert V.check_sentence_length("Short one. Another short one.") is None


def test_reading_level_bounds():
    hard = (
        "The epistemological ramifications of the aforementioned investigation "
        "remained fundamentally indeterminate throughout."
    )
    v = V.check_reading_level(hard)
    assert v is not None and v.rule == "reading_level"
    easy = "The dog ran home. The cat sat on the mat. We went to bed early."
    assert V.check_reading_level(easy, lo=-5.0) is None


def test_opening_similarity():
    e = FakeEmbed()
    same = "The doorbell camera pings at 3:12 every night."
    v = V.check_opening_similarity(same, [same], e)
    assert v is not None and v.rule == "opening_similarity"
    other = V.check_opening_similarity("A new unrelated first line here.", ["Something else entirely."], e)
    assert other is None
    assert V.check_opening_similarity(same, [], e) is None


def test_raise_if():
    with pytest.raises(NsError) as ei:
        V.raise_if([V.Violation("x", "y")])
    assert ei.value.code == "NS-E303"
    assert "x: y" in str(ei.value)
    V.raise_if([])


def test_fixture_story_passes_every_validator():
    from ns_studio.text.prompts import FIXTURES

    b = short_budget(50, "standard")
    for name in ("story_draft.txt", "revise.txt"):
        text = (FIXTURES / name).read_text(encoding="utf-8")
        assert V.validate_story(text, b, past_openings=[], embed=FakeEmbed()) == [], name


def test_validate_story_collects_all_violations():
    b = short_budget(50, "standard")
    v = V.validate_story("In this video.", b, past_openings=[], embed=FakeEmbed())
    assert {x.rule for x in v} == {"length", "banned_phrase", "reading_level"}


def _chapter_text(words: int) -> str:
    body = "The grey truck rolled slowly through the quiet village and stopped beside the library. "
    return (body * (words // 14 + 1)).strip()


def test_validate_chapter_checks_budget_and_skips_opening_after_chapter_one():
    e = FakeEmbed()
    cb = chapter_budget(310, 155)
    ok = _chapter_text(310)
    assert V.validate_chapter(ok, cb, chapter_n=2, past_openings=[ok.split(".")[0] + "."], embed=e) == []
    rules = {v.rule for v in V.validate_chapter(ok, cb, chapter_n=1, past_openings=[ok], embed=e)}
    assert rules == {"opening_similarity"}
    short = {v.rule for v in V.validate_chapter("Short text.", cb, chapter_n=2, past_openings=[], embed=e)}
    assert "length" in short


def test_fixture_chapters_pass_validate_chapter():
    from ns_studio.text.prompts import FIXTURES

    cb = chapter_budget(310, 155)
    for n in range(1, 6):
        data = json.loads((FIXTURES / f"chapter_draft_{n}.json").read_text(encoding="utf-8"))
        assert V.validate_chapter(data["text"], cb, chapter_n=n, past_openings=[], embed=FakeEmbed()) == [], n


def test_cliff_variety():
    e = FakeEmbed()
    same = "Then the front door opened behind me."
    v = V.check_cliff_variety([same, "A different ending line entirely.", same, same], e)
    assert v is not None and v.rule == "cliff_repeat" and "chapters 3 and 4" in v.detail
    assert V.check_cliff_variety([same, "Footprints led to the door.", "The date said tomorrow."], e) is None
    assert V.check_cliff_variety([same], e) is None
