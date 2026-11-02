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


def test_words_are_counted_in_any_script():
    """Review row 13: an Arabic or Russian channel had word_count 0 and failed NS-E303 on every story."""
    assert word_count("Это очень длинная история.") == 4
    assert word_count("كان الباب مفتوحا. من هناك؟") == 5
    assert word_count("canción más fácil") == 3  # accented Latin is not split at the accent
    assert word_count("don't stop") == 2 and word_count("the dog's bone") == 3
    assert word_count("夜中にドアが開いた。") == 9  # Japanese: one word per character
    assert word_count("3:12 every night") == 4


def test_sentences_split_on_arabic_and_cjk_stops():
    text = "كان الباب مفتوحا. من هناك؟ لا أحد."
    assert split_sentences(text) == ["كان الباب مفتوحا.", "من هناك؟", "لا أحد."]
    assert split_sentences("ドアが開いた。誰もいない。") == ["ドアが開いた。", "誰もいない。"]


def test_normalize_tokens_folds_case_in_any_script():
    assert normalize_tokens("Это ОЧЕНЬ Длинная") == ["это", "очень", "длинная"]
    assert normalize_tokens("Straße") == ["strasse"]
    assert normalize_tokens("It’s 3 PM") == ["its", "three", "pm"]


def test_combining_marks_stay_inside_their_word():
    """Re-review row 11: the regex word class leaves out Mn/Mc: Hindi counted 2-3x, NFD 'cafe' twice."""
    assert word_count("किताब पढ़ो") == 2  # Devanagari with vowel signs and a nukta
    assert word_count("café au lait") == 3  # decomposed e + acute accent
    assert word_count("café au lait") == 3  # the same text, precomposed
    assert word_count("வணக்கம் உலகம்") == 2  # Tamil
    assert normalize_tokens("किताब पढ़ो") == ["किताब", "पढ़ो"]
    assert normalize_tokens("café") == normalize_tokens("café") == ["café"]
    assert word_count("كَانَ البَابُ مَفْتُوحًا") == 3  # Arabic with harakat


def test_danda_and_ellipsis_end_a_sentence():
    first, second = "यह पहला वाक्य है।", "यह दूसरा वाक्य है।"
    assert split_sentences(f"{first} {second}") == [first, second]
    assert split_sentences("पहली पंक्ति॥ दूसरी पंक्ति॥") == ["पहली पंक्ति॥", "दूसरी पंक्ति॥"]
    assert split_sentences("Wait… what? Yes.") == ["Wait…", "what?", "Yes."]
    long_hindi = " ".join(["यह एक छोटा वाक्य है।"] * 12)
    assert len(split_sentences(long_hindi)) == 12  # was one 60-word 'sentence': NS-E303 on every story


def test_scripts_without_spaces_are_not_counted_per_fragment():
    """Thai gave 6 fragments for two words; about four base letters now make one counted word."""
    assert word_count("สวัสดีครับ ผมชื่อ") == 3
    assert 1 <= word_count("สวัสดีครับ") <= 3
