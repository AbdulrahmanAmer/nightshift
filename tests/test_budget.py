import random

import pytest

from ns_studio.text.budget import (
    PACING_WPM,
    chapter_budget,
    distribute_words,
    master_as_budget,
    master_budget,
    short_body_words,
    short_budget,
)


def test_pacing_table_matches_spec():
    assert PACING_WPM == {"brisk": 175, "standard": 155, "measured": 140}


@pytest.mark.parametrize(
    ("bucket", "pacing", "words"),
    [(50, "standard", 117), (35, "brisk", 102 - 12), (75, "measured", 175 - 12)],
)
def test_short_budget_words(bucket: int, pacing: str, words: int):
    b = short_budget(bucket, pacing)
    assert b.words == words
    assert b.parts == 1 and b.words_per_part == words


def test_short_budget_subtracts_closing_and_floors_at_40():
    assert short_budget(50, "standard", closing_words=15).words == 117 - 15
    assert short_budget(5, "measured").words == 40


def test_short_budget_multi_part():
    b = short_budget(60, "standard", parts=3)
    assert b.parts == 3
    assert b.words == round(60 * 155 / 60) * 3 - 12 * 3
    assert b.words_per_part == b.words // 3


def test_unknown_pacing_is_a_key_error():
    with pytest.raises(KeyError):
        short_budget(50, "frantic")


def test_master_budget_ten_minutes_standard():
    mb = master_budget(10, "standard")
    assert (mb.target_minutes, mb.target_words, mb.min_words, mb.wpm) == (10, 1550, 1395, 155)
    assert mb.chapters == 5 and mb.chapter_budgets == (310,) * 5


@pytest.mark.parametrize(
    ("minutes", "pacing", "chapters"),
    [(9, "measured", 5), (14, "standard", 6), (14, "brisk", 7), (10, "brisk", 5), (12, "standard", 5)],
)
def test_master_budget_chapter_count_is_5_to_7(minutes: int, pacing: str, chapters: int):
    mb = master_budget(minutes, pacing)
    assert mb.chapters == chapters
    assert len(mb.chapter_budgets) == chapters
    assert sum(mb.chapter_budgets) == mb.target_words
    assert mb.min_words == 9 * mb.wpm


def test_distribute_words_sums_exactly_and_spreads_remainder():
    parts = distribute_words(1553, 5)
    assert sum(parts) == 1553 and max(parts) - min(parts) == 1
    assert parts == (311, 311, 311, 310, 310)


def test_master_budget_tuple_midpoint_and_seeded_draw():
    assert master_budget((10, 14), "standard").target_minutes == 12
    assert master_budget((9, 10), "standard").target_minutes == 10
    a = master_budget((10, 14), "standard", rng=random.Random("job-1"))
    b = master_budget((10, 14), "standard", rng=random.Random("job-1"))
    assert a == b and 10 <= a.target_minutes <= 14
    rngs = (random.Random(f"j{i}") for i in range(40))
    drawn = {master_budget((10, 14), "standard", rng=r).target_minutes for r in rngs}
    assert len(drawn) >= 3 and drawn <= {10, 11, 12, 13, 14}


def test_master_budget_below_floor_is_rejected():
    with pytest.raises(ValueError, match="floor"):
        master_budget(8, "standard")


def test_chapter_budget_limits_and_short_body_words():
    cb = chapter_budget(310, 155)
    assert (cb.words, cb.min_words, cb.max_words, cb.parts) == (310, 248, 372, 1)
    assert short_body_words(35, 155) == 66
    assert short_body_words(50, 155) == 105
    assert short_body_words(75, 155) == 170
    assert short_body_words(5, 155) == 20


def test_master_as_budget():
    b = master_as_budget(master_budget(10, "standard"))
    assert (b.words, b.parts, b.words_per_part, b.wpm) == (1550, 5, 310, 155)
