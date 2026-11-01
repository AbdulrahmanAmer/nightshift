from ns_common.textstats import (
    est_seconds,
    fk_grade,
    normalize_tokens,
    split_sentences,
    syllables,
    word_count,
)


def test_split_sentences_basic():
    s = split_sentences("I heard it again. Did you? \"Run,\" she said!  Nothing happened...")
    assert s == ["I heard it again.", "Did you?", "\"Run,\" she said!", "Nothing happened..."]


def test_split_handles_abbreviations_and_newlines():
    s = split_sentences("Mr. Hale left at 3 p.m. on Monday.\n\nThe door was open.")
    assert s == ["Mr. Hale left at 3 p.m. on Monday.", "The door was open."]


def test_word_count_and_est_seconds():
    t = "one two three four five six seven eight nine ten"
    assert word_count(t) == 10
    assert est_seconds(t, 150) == 4.0


def test_syllables_and_grade():
    assert syllables("the") == 1 and syllables("quiet") == 2 and syllables("basement") == 2
    simple = "The dog ran. The cat sat. It was dark. I was cold."
    assert fk_grade(simple) < 4
    hard = (
        "The epistemological ramifications of the aforementioned investigation "
        "remained fundamentally indeterminate."
    )
    assert fk_grade(hard) > 12


def test_normalize_tokens():
    assert normalize_tokens("It's 3 A.M., isn't it?") == ["its", "three", "am", "isnt", "it"]
