from __future__ import annotations

import re
import unicodedata

_ABBREV = {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "etc", "a.m", "p.m", "e.g", "i.e"}
# A sentence ends at . ! ? the Arabic question mark, the Devanagari danda (U+0964, U+0965) or an ellipsis; the
# CJK full stops end one with no space after them.
_SENT_END = re.compile(
    r"([.!?؟।॥…]+[\"'”’)]*)(\s+|$)"
    r"|([。！？]+[」』）\"'”’)]*)"
)
# Kana and CJK ideographs are written without spaces: each character counts as one word.
CJK_RANGES = r"぀-ヿ㐀-䶿一-鿿"
# Thai, Lao, Myanmar and Khmer have no spaces either, but a character is far less than a word: about four base
# letters (with their vowel and tone marks) count as one.
NOSPACE_RANGES = r"฀-໿က-႟ក-៿"
NOSPACE_CHUNK = 4


def _mark_ranges() -> str:
    """Character-class body of the combining marks (Mn, Mc, Me) of the basic plane.

    `\\w` leaves them out, so Hindi fell apart at every vowel sign and NFD 'cafe + accent' counted twice.
    """
    out: list[str] = []
    start = prev = -2
    for cp in range(0x300, 0x10000):
        if unicodedata.category(chr(cp)) in ("Mn", "Mc", "Me"):
            if cp != prev + 1:
                if start >= 0:
                    out.append(f"\\u{start:04x}-\\u{prev:04x}")
                start = cp
            prev = cp
    out.append(f"\\u{start:04x}-\\u{prev:04x}")
    return "".join(out)


MARKS = _mark_ranges()
_LETTER = rf"[^\W_{CJK_RANGES}{NOSPACE_RANGES}]"
_LETTER_OR_MARK = rf"(?:{_LETTER}|[{MARKS}])"
# A word: a run of letters or digits in any script with its combining marks (an apostrophe may sit inside it),
# one CJK character, or up to four base letters of a script written without spaces.
WORD_PATTERN = (
    rf"[{CJK_RANGES}]"
    rf"|(?:[{NOSPACE_RANGES}][{MARKS}]*){{1,{NOSPACE_CHUNK}}}"
    rf"|{_LETTER}(?:{_LETTER_OR_MARK}|['’])*"
)
_WORD = re.compile(WORD_PATTERN)
# Alignment tokens: every CJK character on its own (a recogniser returns Japanese/Chinese as multi-character
# pieces; the script is shown character by character), other scripts as runs of letters, digits and marks.
_NORM_TOKEN = re.compile(rf"[{CJK_RANGES}]|(?:[^\W_{CJK_RANGES}]|[{MARKS}])+")
_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
         "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_SYLLABLE_EXCEPTIONS = {"quiet": 2, "science": 2, "poem": 2, "being": 2}


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text.strip())
    out: list[str] = []
    start = 0
    for m in _SENT_END.finditer(text):
        end = m.end(1) if m.group(1) else m.end(3)
        candidate = text[start:end].strip()
        prev_word = re.findall(r"[A-Za-z.]+", candidate[-8:])
        tail = (prev_word[-1].rstrip(".").lower() if prev_word else "")
        if tail in _ABBREV and m.group(1) == ".":
            continue
        if candidate:
            out.append(candidate)
        start = m.end()
    rest = text[start:].strip()
    if rest:
        out.append(rest)
    return out


def word_count(text: str) -> int:
    return len(_WORD.findall(unicodedata.normalize("NFC", text)))


def est_seconds(text: str, wpm: float) -> float:
    return round(word_count(text) / wpm * 60.0, 2)


def syllables(word: str) -> int:
    w = word.lower().strip("'")
    if not w:
        return 0
    if w in _SYLLABLE_EXCEPTIONS:
        return _SYLLABLE_EXCEPTIONS[w]
    if len(w) <= 3:
        return 1
    w = re.sub(r"([^laeiouy])e(ment)$", r"\1\2", w)   # base-ment: silent e before -ment
    w = re.sub(r"(?:[^laeiouy]es|ed|[^laeiouy]e)$", "", w)
    w = re.sub(r"^y", "", w)
    groups = re.findall(r"[aeiouy]+", w)
    return max(1, len(groups))


def fk_grade(text: str) -> float:
    sentences = split_sentences(text) or [text]
    words = _WORD.findall(text)
    if not words:
        return 0.0
    syl = sum(syllables(w) for w in words)
    grade = 0.39 * (len(words) / len(sentences)) + 11.8 * (syl / len(words)) - 15.59
    return round(grade, 2)


def _num_words(n: int) -> str:
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        return _TENS[tens] + ("" if ones == 0 else " " + _ONES[ones])
    return str(n)


def normalize_tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFC", text).casefold().replace("\u2019", "'")
    text = re.sub(r"(?<=\w)\.(?=\w\.)", "", text)   # a.m. -> am.
    text = re.sub(r"\b(\d{1,2})\b", lambda m: _num_words(int(m.group(1))), text)
    text = text.replace("'", "")
    return _NORM_TOKEN.findall(text)


# A long video must reach 9:00 of narration. The duration check can add about 400 words by itself (two
# expansions of 120 to 200 words); a script shorter than this would still end in NS-E305 after the voice
# was already rendered, so both the dashboard and `studio run --script` refuse it up front.
SCRIPT_LONG_MIN_WORDS = 1100


def script_problem(text: str, fmt: str) -> str | None:
    """Plain-words reason the owner's script cannot become a video of `fmt`, or None when it can."""
    words = word_count(text)
    if fmt == "master_first" and words < SCRIPT_LONG_MIN_WORDS:
        return (
            f"A long video needs a script of at least {SCRIPT_LONG_MIN_WORDS} words (about 8 minutes of "
            f"narration); this one has {words}. Write a little more, or make a Short from it instead."
        )
    return None
