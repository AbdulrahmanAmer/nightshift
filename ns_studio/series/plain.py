"""A story the owner wrote as plain text, checked and turned into a `script.md` (`JobRunRequest.script`).

The pipeline cannot lengthen or shorten an owner's words, so everything that would fail after the voice or
the render is checked here, before a job exists: banned phrases, sentence length, chapter numbering, and the
length of the finished video. A Short must land inside the QC band of its length bucket (+-15 %, at most
179 s) and a long video between the 9:00 planning floor and the 14:30 ceiling (QC fails over 15:00). The
pace (`pacing`) is picked from the channel's own pool so the text fits; `length_bucket` of a Short is set
from the estimate instead of drawn at random. Pure functions, no model and no service: the dashboard calls
them to refuse a script before the owner confirms it, and `ns_studio.jobs.make_script_job` calls them to
build the job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ns_common.errors import NsError
from ns_common.pacing import (
    NOMINAL_WPM,
    PACING_WPM,
    master_fixed_seconds,
    master_seconds,
    narration_seconds,
    pacing_speed,
    words_for_seconds,
)
from ns_common.textstats import split_sentences, word_count
from ns_studio.series.store import (
    _HEAD,
    ScriptDoc,
    join_front_matter,
    parse_script,
    split_front_matter,
)
from ns_studio.text.budget import CHAPTER_WORDS_MID, MASTER_MAX_SECONDS, MAX_CHAPTERS, MIN_CHAPTERS
from ns_studio.text.validators import check_banned, check_master_length, check_sentence_length, load_banned

TITLE_MAX_CHARS = 60
SHORT_MIN_S = 10.0
SHORT_MAX_S = 165.0       # QC allows 179 s; the rest is room for the real voice running a little slower
MASTER_MIN_S = 556.0      # the 9:00 floor of `duration_check` (540 s) plus 3 % for the real voice
MAX_SENTENCE_WORDS = 35


class ScriptRefused(ValueError):
    """The script cannot become a video; the message says why in the owner's words."""


@dataclass(frozen=True)
class ScriptPlan:
    """A script that passed every check, with what the job needs: its `script.md`, pace and length."""

    fmt: str
    markdown: str
    doc: ScriptDoc
    pacing: str
    seconds: float                  # estimated narration, pauses and chapter cards included
    length_bucket: int | None       # a Short's QC bucket (the estimate); None for a long video


def spoken_wpm(pacing: str, voice_speed: float = 1.0) -> float:
    """Words per minute the voice stage really speaks at (TTS speed is `pacing_speed(voice speed, pacing)`).

    The pace alone (`PACING_WPM`) is right only for a voice at speed 1.0; a voice set to 0.85 talks
    15 % slower and a Short sized for the pace would come out longer than its QC band.
    """
    return NOMINAL_WPM * pacing_speed(voice_speed, pacing)


def _balanced(units: list[str], n: int) -> list[list[str]]:
    """Cut `units` into `n` consecutive groups of about the same number of words (none empty)."""
    words = [word_count(u) for u in units]
    total = max(1, sum(words))
    groups: list[list[str]] = [[]]
    seen = 0
    for i, (unit, w) in enumerate(zip(units, words, strict=True)):
        groups_left = n - len(groups)
        must_split = len(units) - i <= groups_left
        past_boundary = seen + w / 2 > total * len(groups) / n
        if groups[-1] and groups_left > 0 and (must_split or past_boundary):
            groups.append([])
        groups[-1].append(unit)
        seen += w
    return groups


def script_markdown(text: str, title: str | None, *, chapters: int = 1) -> str:
    """The `script.md` for a story the owner wrote, so `script_import` reads it like a series part.

    A text that already has `## 1. Title` chapter headings (and optional front-matter) keeps its chapters.
    Plain text is cut into `chapters` groups of about the same size at paragraph ends (at sentence ends when
    it has too few paragraphs); one group is a single chapter named after the title. The title is `title`,
    else the front-matter title, else a leading `# Heading` line, else the first words of the text.
    """
    text = text.replace("\r\n", "\n").strip()
    meta, body = split_front_matter(text)
    body = body.strip()
    lead = re.match(r"#[ \t]+(.+?)[ \t]*\n", body + "\n")
    if lead and not _HEAD.search(body):
        body = body[lead.end() :].strip()
    named = (title or "").strip() or str(meta.get("title") or "").strip() or (lead.group(1) if lead else "")
    if not named:
        named = " ".join(body.split()[:8]).rstrip(".,;:!?") or "Untitled"
    named = named[:TITLE_MAX_CHARS].rstrip()
    if _HEAD.search(body):
        return join_front_matter({**meta, "title": named}, body)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    units = paragraphs if len(paragraphs) >= chapters else split_sentences(body)
    groups = _balanced(units, max(1, min(chapters, len(units))))
    blocks = [
        f"## {i}. {named if len(groups) == 1 else f'Part {i}'}\n\n" + "\n\n".join(g)
        for i, g in enumerate(groups, start=1)
    ]
    return join_front_matter({"part": 1, "title": named}, "\n\n".join(blocks))


def _minutes(seconds: float) -> str:
    m, s = divmod(round(seconds), 60)
    return f"{m}:{s:02d}"


def _parse(text: str, title: str | None, chapters: int) -> tuple[str, ScriptDoc]:
    markdown = script_markdown(text, title, chapters=chapters)
    try:
        return markdown, parse_script(markdown)
    except NsError as e:
        raise ScriptRefused(f"The chapter headings in the script do not work: {e.detail}") from e


def _text_problems(doc: ScriptDoc) -> None:
    full = "\n\n".join(c.text for c in doc.chapters)
    banned = check_banned(full, load_banned())
    if banned:
        raise ScriptRefused(
            f"The script uses a phrase Nightshift does not allow ({banned.detail}). Reword it first."
        )
    long_sentence = check_sentence_length(full, MAX_SENTENCE_WORDS)
    if long_sentence:
        raise ScriptRefused(
            f"A sentence in the script is too long to narrate well ({long_sentence.detail}...). "
            f"Break it into sentences of {MAX_SENTENCE_WORDS} words or fewer."
        )


def _short(text: str, title: str | None, pacings: list[str], speed: float) -> ScriptPlan:
    markdown, doc = _parse(text, title, 1)
    _text_problems(doc)
    full = "\n\n".join(c.text for c in doc.chapters)
    estimates = {p: narration_seconds(full, spoken_wpm(p, speed)) for p in pacings}
    for pacing, seconds in estimates.items():
        if SHORT_MIN_S <= seconds <= SHORT_MAX_S:
            return ScriptPlan("shorts_only", markdown, doc, pacing, seconds, max(1, round(seconds)))
    first = pacings[0]
    seconds = estimates[first]
    top = words_for_seconds(SHORT_MAX_S, spoken_wpm(first, speed))
    if seconds > SHORT_MAX_S:
        raise ScriptRefused(
            f"This script would run about {_minutes(seconds)} as a Short. A Short can be about "
            f"{_minutes(SHORT_MAX_S)} at most (around {top} words at this channel's pace). "
            "Shorten it, or make a long video from it."
        )
    raise ScriptRefused(
        f"This script would run only about {round(seconds)} seconds as a Short; a Short needs at least "
        f"{round(SHORT_MIN_S)} seconds. Add a little more."
    )


def _long(text: str, title: str | None, pacings: list[str], speed: float) -> ScriptPlan:
    words = word_count(text)
    chapters = max(MIN_CHAPTERS, min(MAX_CHAPTERS, round(words / CHAPTER_WORDS_MID)))
    markdown, doc = _parse(text, title, chapters)
    _text_problems(doc)
    texts = [c.text for c in doc.chapters]
    fits: dict[str, float] = {}
    for pacing in pacings:
        seconds = master_seconds(texts, spoken_wpm(pacing, speed))
        fits[pacing] = seconds
        if seconds >= MASTER_MIN_S and check_master_length(texts, spoken_wpm(pacing, speed)) is None:
            if not MIN_CHAPTERS <= len(doc.chapters) <= MAX_CHAPTERS:
                raise ScriptRefused(
                    f"A long video is told in {MIN_CHAPTERS} to {MAX_CHAPTERS} chapters, but the script has "
                    f"{len(doc.chapters)}. Use {MIN_CHAPTERS} to {MAX_CHAPTERS} numbered headings, or leave "
                    "the headings out and Nightshift will cut the text into chapters."
                )
            return ScriptPlan("master_first", markdown, doc, pacing, seconds, None)
    fixed_lo, fixed_hi = master_fixed_seconds(MAX_CHAPTERS), master_fixed_seconds(MIN_CHAPTERS)
    lo = min(words_for_seconds(MASTER_MIN_S, spoken_wpm(p, speed), fixed_s=fixed_lo) for p in pacings)
    hi = max(words_for_seconds(MASTER_MAX_SECONDS, spoken_wpm(p, speed), fixed_s=fixed_hi) for p in pacings)
    seconds = fits[pacings[0]]
    if seconds < MASTER_MIN_S:
        raise ScriptRefused(
            f"This script would run about {_minutes(seconds)} as a long video; a long video needs at least "
            f"9:00. At this channel's pace that is about {lo} words, the script has {words}. "
            "Write a little more, or make a Short from it."
        )
    raise ScriptRefused(
        f"This script would run about {_minutes(seconds)} as a long video; a long video can be about "
        f"{_minutes(MASTER_MAX_SECONDS)} at most (around {hi} words at this channel's pace), the script has "
        f"{words}. Shorten it."
    )


def plan_script(
    text: str, fmt: str, pacings: list[str], title: str | None = None, voice_speed: float = 1.0
) -> ScriptPlan:
    """Check `text` for a Short (`shorts_only`) or a long video (`master_first`) and plan its job.

    `pacings` is the channel's own pool; the first pace at which the text fits the video's length is used.
    `voice_speed` is the speed of the channel's voice (`Voice.speed`): the lengths are estimated at the speed
    the voice stage will really speak at (`spoken_wpm`).
    Raises `ScriptRefused` with a plain-words reason when no pace fits or a text rule fails.
    """
    pool = [p for p in pacings if p in PACING_WPM] or ["standard"]
    if fmt == "shorts_only":
        return _short(text, title, pool, voice_speed)
    if fmt == "master_first":
        return _long(text, title, pool, voice_speed)
    raise ScriptRefused("A script becomes a Short or a long video, not that type.")


def script_problem(
    text: str, fmt: str, pacings: list[str], title: str | None = None, voice_speed: float = 1.0
) -> str | None:
    """The reason `plan_script` refuses `text`, or None when it can become a video of `fmt`."""
    try:
        plan_script(text, fmt, pacings, title, voice_speed)
    except ScriptRefused as e:
        return str(e)
    return None
