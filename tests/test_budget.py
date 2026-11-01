import pytest

from ns_studio.text.budget import PACING_WPM, short_budget


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
