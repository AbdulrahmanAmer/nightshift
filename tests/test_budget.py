import random

import pytest

from ns_common.pacing import master_fixed_seconds, words_for_seconds
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
    [(50, "standard", 118 - 12), (35, "brisk", 92 - 12), (75, "measured", 161 - 12)],
)
def test_short_budget_words(bucket: int, pacing: str, words: int):
    """The words fill the bucket with the TTS pauses counted (0.035 s per word), not speech alone."""
    b = short_budget(bucket, pacing)
    assert b.words == words == words_for_seconds(bucket, b.wpm) - 12
    assert b.parts == 1 and b.words_per_part == words
    assert words < round(bucket * b.wpm / 60) - 12  # fewer than the speech-only count


def test_short_budget_subtracts_closing_and_floors_at_40():
    assert short_budget(50, "standard", closing_words=15).words == 106 - 15
    assert short_budget(5, "measured").words == 40


def test_short_budget_multi_part():
    b = short_budget(60, "standard", parts=3)
    assert b.parts == 3
    assert b.words == words_for_seconds(60 * 3, 155) - 12 * 3
    assert b.words_per_part == b.words // 3


def test_unknown_pacing_is_a_key_error():
    with pytest.raises(KeyError):
        short_budget(50, "frantic")


def test_master_budget_ten_minutes_standard():
    """1,550 words of speech play for 10:00 plus about 1:10 of pauses and cards: the budget is smaller."""
    mb = master_budget(10, "standard")
    assert (mb.target_minutes, mb.wpm) == (10, 155)
    assert mb.target_words == words_for_seconds(600, 155, fixed_s=master_fixed_seconds(5)) < 1550
    assert mb.min_words == words_for_seconds(540, 155, fixed_s=master_fixed_seconds(5)) < 9 * 155
    assert mb.chapters == 5 and sum(mb.chapter_budgets) == mb.target_words


@pytest.mark.parametrize(
    ("minutes", "pacing"),
    [(9, "measured"), (14, "standard"), (14, "brisk"), (10, "brisk"), (12, "standard"), (14, "measured")],
)
def test_master_budget_chapter_count_is_5_to_7(minutes: int, pacing: str):
    mb = master_budget(minutes, pacing)
    assert 5 <= mb.chapters <= 7
    assert len(mb.chapter_budgets) == mb.chapters
    assert sum(mb.chapter_budgets) == mb.target_words
    floor = words_for_seconds(540, mb.wpm, fixed_s=master_fixed_seconds(5))
    assert mb.min_words == min(mb.target_words, floor)


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
    mb = master_budget(10, "standard")
    b = master_as_budget(mb)
    assert (b.words, b.parts, b.words_per_part, b.wpm) == (mb.target_words, 5, mb.target_words // 5, 155)
