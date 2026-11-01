from __future__ import annotations

import re

_ABBREV = {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "etc", "a.m", "p.m", "e.g", "i.e"}
_SENT_END = re.compile(r"([.!?]+[\"'”’)]*)(\s+|$)")
_WORD = re.compile(r"[A-Za-z0-9']+")
_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven",
         "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_SYLLABLE_EXCEPTIONS = {"quiet": 2, "science": 2, "poem": 2, "being": 2}


def split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text.strip())
    out: list[str] = []
    start = 0
    for m in _SENT_END.finditer(text):
        end = m.end(1)
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
    return len(_WORD.findall(text))


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
    text = text.lower().replace("’", "'")
    text = re.sub(r"(?<=\w)\.(?=\w\.)", "", text)   # a.m. -> am.
    text = re.sub(r"\b(\d{1,2})\b", lambda m: _num_words(int(m.group(1))), text)
    text = text.replace("'", "")
    return re.findall(r"[a-z0-9]+", text)
