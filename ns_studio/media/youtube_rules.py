"""What YouTube Studio accepts in an upload sheet, and the one function that makes a sheet fit it.

Every limit is a `Rule` with the help page it comes from and the day that page was read. When YouTube
changes a rule, change the `Rule` here (and `RULES_VERSION`, so the dashboard's start sweep rewrites the
sheets still waiting to be posted); nothing else in Nightshift holds the number.

`fit` takes a sheet's title, description, tags and hashtags and returns them repaired, with one plain line
per change and a warning per problem that has no safe repair. `disclosure` gives the altered or synthetic
content answer for the voice the video was narrated with, with its reason. Nothing here touches a file.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ns_studio.context import JobContext

CHECKED = "2026-11-11"
API_VIDEOS = "https://developers.google.com/youtube/v3/docs/videos"
HELP_SETTINGS = "https://support.google.com/youtube/answer/57404"
HELP_HASHTAGS = "https://support.google.com/youtube/answer/6390658"
HELP_CHAPTERS = "https://support.google.com/youtube/answer/9884579"
HELP_SHORTS = "https://support.google.com/youtube/answer/15424877"
HELP_DISCLOSURE = "https://support.google.com/youtube/answer/14328491"
HELP_KIDS = "https://support.google.com/youtube/answer/9528076"


@dataclass(frozen=True)
class Rule:
    """One YouTube rule: its value, the page that states it, the day the page was read and its words."""

    id: str
    what: str
    value: float
    source: str
    checked: str
    quote: str


NO_ANGLE_BRACKETS = Rule(
    "R1", "no < or > in the title, the description or a tag", 0, API_VIDEOS, CHECKED,
    "The property value has a maximum length of 100 characters and may contain all valid UTF-8 characters "
    "except < and >.",
)
TITLE_MAX_CHARS = Rule(
    # Counted in UTF-16 units (`title_units`), the safe side: the page says "characters" without saying how an
    # emoji counts; a browser's string length counts an emoji outside the BMP as 2. A title within 100 units
    # is within 100 code points too. The GPU kit's paste check E1 records how Studio counts; change this then.
    "R2", "title of 1 to 100 characters (counted in UTF-16 units), not only spaces", 100, API_VIDEOS, CHECKED,
    "The property value has a maximum length of 100 characters (snippet.title); Studio: 'Video titles have a "
    "character limit of 100 characters and cannot include invalid characters.' (" + HELP_SETTINGS + ")",
)
DESCRIPTION_MAX_BYTES = Rule(
    "R3", "description of at most 5,000 bytes, its hashtag line included", 5000, API_VIDEOS, CHECKED,
    "The property value has a maximum length of 5000 bytes and may contain all valid UTF-8 characters except "
    "< and >. (snippet.description; Studio's page says 5,000 characters, which bytes never exceed)",
)
TAGS_MAX_CHARS = Rule(
    "R4", "tags of at most 500 characters in all, commas counted, a tag with a space counted with two quotes",
    500, API_VIDEOS, CHECKED,
    "The property value has a maximum length of 500 characters. ... commas between items in the list count "
    "toward the limit. If a tag contains a space, the API server handles the tag value as though it were "
    "wrapped in quotation marks, and the quotation marks count toward the character limit.",
)
HASHTAGS_IGNORED_ABOVE = Rule(
    "R5", "hashtags are one word each after a #; more than 60 make YouTube ignore them all", 60,
    HELP_HASHTAGS, CHECKED,
    "No spaces: Hashtags don't contain any spaces. If you want to have two words in a hashtag, you can join "
    "them together (#TwoWords, #twowords). ... If a video or playlist has more than 60 hashtags, we'll "
    "ignore each hashtag on that content.",
)
CHAPTERS_MIN_COUNT = Rule(
    "R6", "chapters: at least three, the first at 00:00, ascending", 3, HELP_CHAPTERS, CHECKED,
    "Make sure that the first timestamp you list starts with 00:00. Your video should have at least three "
    "timestamps listed in ascending order.",
)
CHAPTER_MIN_SECONDS = Rule(
    "R6", "chapters: each at least 10 seconds", 10, HELP_CHAPTERS, CHECKED,
    "The minimum length for video chapters is 10 seconds.",
)
SHORT_MAX_SECONDS = Rule(
    "R7", "a Short is square or vertical and at most 3 minutes", 180, HELP_SHORTS, CHECKED,
    "Any videos uploaded on or after this date with a square or vertical aspect ratio up to three minutes "
    "in length",
)
DISCLOSURE = Rule(
    "R8", "altered or synthetic content: Yes for realistic content that makes a real person appear to say "
    "what they did not, alters a real event or place, or shows a realistic scene that did not occur",
    0, HELP_DISCLOSURE, CHECKED,
    # Verbatim, as the page has them (its curly apostrophes kept): the introduction of the list of what must
    # be disclosed and its first item; the heading of the list of what need not be, and its item on voices.
    "Creators must disclose GenAI content that: | Makes a real person appear to say or do something they "
    "didn’t do. | Examples of content creators don’t have to disclose | Cloning one’s own voice to "
    "create voice overs or dubs",
)
MADE_FOR_KIDS = Rule(
    "R9", "made for kids is answered for every video", 0, HELP_KIDS, CHECKED,
    "Regardless of your location, we require you to tell us whether or not your videos are made for kids",
)
RULES: tuple[Rule, ...] = (
    NO_ANGLE_BRACKETS, TITLE_MAX_CHARS, DESCRIPTION_MAX_BYTES, TAGS_MAX_CHARS, HASHTAGS_IGNORED_ABOVE,
    CHAPTERS_MIN_COUNT, CHAPTER_MIN_SECONDS, SHORT_MAX_SECONDS, DISCLOSURE, MADE_FOR_KIDS,
)
#: Raised whenever a rule above changes: the dashboard's start sweep then rewrites the waiting sheets once.
RULES_VERSION = 1


@dataclass(frozen=True)
class Credit:
    """What a stock footage site asks of a video made with its API footage: its page, the day it was read, its
    words, and whether a credit is REQUIRED (then it would be a Rule of the sheet's fields, not an offer)."""

    site: str
    source: str
    checked: str
    quote: str
    required: bool


#: Read 2026-11-11 (card stock-truth). Neither site requires a credit in the published video: Pixabay "kindly
#: request[s]" one where search results are displayed; Pexels' guidelines ask for a link "when possible" and
#: its licence says no credit is needed. So the sheet OFFERS one line the owner can paste or leave out.
CREDITS: dict[str, Credit] = {
    "pixabay": Credit(
        "Pixabay", "https://pixabay.com/api/docs/", "2026-11-11",
        "If you make use of the API, show your users where the images and videos are from, whenever search "
        "results are displayed. That's the one thing we kindly request in return for free API usage.",
        False,
    ),
    "pexels": Credit(
        "Pexels", "https://www.pexels.com/api/documentation/", "2026-11-11",
        "Whenever you are doing an API request make sure to show a prominent link to Pexels. ... Always "
        "credit our photographers when possible | License (https://www.pexels.com/license/ and terms of "
        "service): You do not need to credit Pexels or the contributor of Content when you use it",
        False,
    ),
}
#: Every credit line of a sheet starts with this (`bare_sheet` leaves it out: it is an offer, not a field).
CREDIT_HEAD = "OPTIONAL CREDIT"
CREDIT_PREFIX = f"{CREDIT_HEAD} (not required; paste it at the end of the description or leave it out): "


def credit_text(sources: Sequence[str]) -> str | None:
    """The credit itself for the stock sites a video's clips came from (`Metadata.clips[].source`), or None
    when none did (fake footage, the clipper's own footage)."""
    sites = [CREDITS[s].site for s in dict.fromkeys(sources) if s in CREDITS]
    return f"Stock footage from {' and '.join(sites)}." if sites else None


def credit_line(sources: Sequence[str], description: str, hashtags: Sequence[str]) -> str | None:
    """The sheet's credit line, counted against R3 like every fixed line: the description as the sheet gives
    it, its hashtag line and the credit pasted after them (a blank line between) must stay within
    `DESCRIPTION_MAX_BYTES`. When they would not, the credit is NOT offered: the line says it was left out and
    why, so the owner is never handed a paste YouTube refuses. None when no stock site supplied a clip."""
    text = credit_text(sources)
    if text is None:
        return None
    used = description_bytes(description.rstrip()) + hashtag_line_bytes(hashtags)
    limit = int(DESCRIPTION_MAX_BYTES.value)
    if used + description_bytes("\n\n" + text) <= limit:
        return CREDIT_PREFIX + text
    return (f"{CREDIT_HEAD} left out: the description and its hashtags already use {used:,} of YouTube's "
            f"{limit:,} bytes, so the credit ({text}) would not fit.")
#: Nightshift's own choice, well inside R5: at most five hashtags (YouTube shows three by the title).
HASHTAGS_KEPT = 5

CHANGED_PREFIX = "Changed for YouTube: "
WARNING_PREFIX = "WARNING FOR YOUTUBE: "
_BRACKETS = re.compile(r"[<>]")
_CHAPTER_LINE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?\s")
_FIXED_LINE = re.compile(r"^(Original fiction\.|From the full story: |Clip from: |Part \d+ of \d+\.)")
_SENTENCE_END = ".!?…。！？"  # . ! ? ... and the CJK full stop, exclamation and question marks


@dataclass
class Fitted:
    """A sheet's fields as YouTube accepts them, with what was changed and what could not be repaired."""

    title: str
    description: str
    tags: list[str]
    hashtags: list[str]
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def description_bytes(text: str) -> int:
    """Bytes of `text` as pasted from a Windows sheet: UTF-8, each line break counted as CR LF (2 bytes).

    The `.txt` sheet is written with Windows line ends; counting them as two bytes keeps a description that
    is copied from the sheet within R3 as well as one copied from the Queue."""
    return len(text.encode("utf-8")) + text.count("\n")


def tags_length(tags: Sequence[str]) -> int:
    """R4 as YouTube counts it: `["a b", "c"]` is `"a b",c`, 7 characters."""
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + max(0, len(tags) - 1)


def _no_brackets(text: str) -> tuple[str, bool]:
    if not _BRACKETS.search(text):
        return text, False
    lines = [re.sub(r"[ \t]{2,}", " ", _BRACKETS.sub("", ln)) if _BRACKETS.search(ln) else ln
             for ln in text.split("\n")]
    return "\n".join(lines), True


def title_units(text: str) -> int:
    """Length in UTF-16 code units: an emoji outside the Basic Multilingual Plane counts 2 (see R2)."""
    return len(text.encode("utf-16-le")) // 2


def _cut_title(title: str) -> str:
    """At most `TITLE_MAX_CHARS` UTF-16 units, cut before a word where one ends inside the limit (never
    inside a surrogate pair); a title that would be left empty is cut at the last whole character."""
    limit = int(TITLE_MAX_CHARS.value)
    if title_units(title) <= limit:
        return title
    n = len(title)
    while title_units(title[:n]) > limit:
        n -= 1
    head = title[: n + 1]  # one character past the limit: a space there ends the last whole word
    cut = head.rsplit(" ", 1)[0] if " " in head[1:] else title[:n]
    cut = cut[:n].rstrip(" ,;:-–—")
    return cut or title[:n].rstrip() or title[:n]


def fit_title(title: str, fallback: str) -> tuple[str, list[str]]:
    """R1 and R2: brackets removed, spaces collapsed, 1 to 100 characters; empty becomes `fallback`."""
    changes: list[str] = []
    clean, had = _no_brackets(title)
    if had:
        changes.append("removed < and > from the title")
    clean = re.sub(r"\s+", " ", clean).strip()
    if not clean:
        clean = re.sub(r"\s+", " ", _BRACKETS.sub("", fallback)).strip() or "Untitled story"
        changes.append(f"the title was empty, so the working title {clean[:60]!r} is used")
    cut = _cut_title(clean)
    if cut != clean:
        changes.append(f"shortened the title to {int(TITLE_MAX_CHARS.value)} characters")
    return cut, changes


def _cut_text(text: str, room: int) -> str:
    """The longest start of `text` within `room` bytes: at a sentence end, else a word end, else at a
    character."""
    if description_bytes(text) <= room:
        return text
    if room <= 0:
        return ""
    ends = [i + 1 for i, c in enumerate(text) if c in _SENTENCE_END and (i + 1 == len(text) or
            text[i + 1].isspace() or c in "。！？")]
    ends += [i for i, c in enumerate(text) if c == "\n"]
    for marks in (ends, [i for i, c in enumerate(text) if c.isspace()]):
        fits = [i for i in marks if text[:i].strip() and description_bytes(text[:i].rstrip()) <= room]
        if fits:
            return text[: max(fits)].rstrip()
    lo, hi = 0, len(text)  # no sentence or word end fits (a text without spaces): the most whole characters
    while lo < hi:
        mid = (lo + hi + 1) // 2
        lo, hi = (mid, hi) if description_bytes(text[:mid]) <= room else (lo, mid - 1)
    return text[:lo].rstrip()


def fit_description(description: str, budget: int) -> tuple[str, list[str], list[str]]:
    """R1 and R3. Only the summary (the lines before the first chapter line or fixed line) is shortened.

    The chapter list, the series line, "From the full story", "Clip from" and "Original fiction." stay. When
    those alone pass `budget`, the summary goes and the end is cut at a line, with a warning."""
    changes: list[str] = []
    warnings: list[str] = []
    text, had = _no_brackets(description)
    if had:
        changes.append("removed < and > from the description")
    if description_bytes(text) <= budget:
        return text, changes, warnings
    lines = text.split("\n")
    fixed = (i for i, ln in enumerate(lines) if _CHAPTER_LINE.match(ln) or _FIXED_LINE.match(ln))
    at = next(fixed, len(lines))
    head, tail = "\n".join(lines[:at]).rstrip(), "\n".join(lines[at:]).lstrip("\n")
    joint = "\n\n" if head and tail and lines[at - 1 : at] == [""] else "\n"
    room = budget - description_bytes(tail) - (description_bytes(joint) if tail else 0)
    short = _cut_text(head, room) if room > 0 else ""
    out = joint.join(part for part in (short, tail) if part)
    if description_bytes(out) > budget:  # the fixed lines alone are too long: keep whole lines from the top
        keep: list[str] = []
        for ln in out.split("\n"):
            if description_bytes("\n".join([*keep, ln])) > budget:
                break
            keep.append(ln)
        out = "\n".join(keep).rstrip()
        warnings.append(
            "the description was too long for YouTube even without its summary, so its end was cut at "
            f"{int(DESCRIPTION_MAX_BYTES.value):,} bytes: read it before posting"
        )
    changes.append(f"shortened the description to {int(DESCRIPTION_MAX_BYTES.value):,} bytes")
    return out, changes, warnings


def fit_tags(tags: Sequence[str]) -> tuple[list[str], list[str]]:
    """R1 and R4: no `<`, `>` or `,` in a tag, no empty tag, and the last tags dropped to fit 500."""
    changes: list[str] = []
    out: list[str] = []
    brackets = commas = empty = 0
    for raw in tags:
        t = str(raw)
        brackets += bool(_BRACKETS.search(t))
        commas += "," in t
        t = re.sub(r"\s+", " ", _BRACKETS.sub("", t).replace(",", " ")).strip()
        if t:
            out.append(t)
        else:
            empty += 1
    if brackets:
        changes.append("removed < and > from the tags")
    if commas:
        changes.append("removed commas from inside tags")
    if empty:
        changes.append(f"dropped {empty} empty tag{'s' if empty > 1 else ''}")
    dropped = 0
    while out and tags_length(out) > TAGS_MAX_CHARS.value:
        out.pop()
        dropped += 1
    if dropped:
        changes.append(
            f"dropped the last {dropped} tag{'s' if dropped > 1 else ''} to keep the tags within "
            f"{int(TAGS_MAX_CHARS.value)} characters"
        )
    return out, changes


def hashtag_char(c: str) -> bool:
    """A character a hashtag word keeps: a letter or digit of any script, `_`, or a combining mark.

    R5's page says only "No spaces: Hashtags don't contain any spaces." and that two words are joined
    together (#TwoWords). A hyphen or other punctuation ends the hashtag YouTube links (`#true-crime` links
    `#true` only), so Nightshift joins across them as across spaces: the safe side, not a quoted rule."""
    return c.isalnum() or c == "_" or unicodedata.category(c).startswith("M")


def fit_hashtags(hashtags: Sequence[str]) -> tuple[list[str], list[str]]:
    """R1 and R5: each one word after one `#`, no repeats, at most `HASHTAGS_KEPT`."""
    changes: set[str] = set()
    out: list[str] = []
    for raw in hashtags:
        h = str(raw).strip()
        if _BRACKETS.search(h):
            changes.add("removed < and > from the hashtags")
        body = _BRACKETS.sub("", h).lstrip("#")
        word = "".join(c for c in body if hashtag_char(c))
        if re.search(r"\s", body):
            changes.add("joined the words of a hashtag (a hashtag has no spaces)")
        if any(not hashtag_char(c) and not c.isspace() and c not in "<>" for c in body):
            changes.add("joined a hashtag across a hyphen or other mark (YouTube ends a hashtag there)")
        if not h.startswith("#") and word:
            changes.add("added # to a hashtag")
        if not word:
            continue
        if ("#" + word).lower() in {o.lower() for o in out}:
            changes.add("dropped a repeated hashtag")
            continue
        out.append("#" + word)
    if len(out) > HASHTAGS_KEPT:
        out = out[:HASHTAGS_KEPT]
        changes.add(f"kept the first {HASHTAGS_KEPT} hashtags")
    return out, sorted(changes)


def hashtag_line_bytes(hashtags: Sequence[str]) -> int:
    """What the hashtag line adds to the pasted description: a blank line and the line itself."""
    return description_bytes("\n\n" + " ".join(hashtags)) if hashtags else 0


def fit(
    *, title: str, description: str, tags: Sequence[str], hashtags: Sequence[str], fallback_title: str = "",
    short_seconds: float | None = None,
) -> Fitted:
    """The sheet's fields as YouTube accepts them (R1 to R5, R7), with a line per change and per warning.

    `short_seconds` is the length of a Short or a clip (a vertical video): over R7 it is a warning, since
    no text change can repair it. The hashtag line counts toward the description's bytes because the sheet
    and the Queue both put it at the end of the description."""
    t, changes = fit_title(title, fallback_title)
    h, h_changes = fit_hashtags(hashtags)
    room = int(DESCRIPTION_MAX_BYTES.value) - hashtag_line_bytes(h)
    d, d_changes, warnings = fit_description(description, room)
    g, g_changes = fit_tags(tags)
    if short_seconds is not None and short_seconds > SHORT_MAX_SECONDS.value:
        m, s = divmod(int(round(short_seconds)), 60)
        warnings.append(
            f"this vertical video is {m}:{s:02d} long; YouTube makes a Short only of a square or vertical "
            "video up to 3 minutes, so it will be posted as a regular video"
        )
    return Fitted(t, d, g, h, [*changes, *d_changes, *g_changes, *h_changes], warnings)


def chapter_problem(chapters: Sequence[dict[str, Any]], duration_s: float | None) -> str | None:
    """Why the chapter list breaks R6 (YouTube would then show no chapters), or None when it is valid."""
    n = int(CHAPTERS_MIN_COUNT.value)
    if len(chapters) < n:
        return f"YouTube needs at least {n} chapters and this video has {len(chapters)}"
    starts = [float(c.get("start_s", 0.0)) for c in chapters]
    if starts[0] != 0.0 or str(chapters[0].get("timestamp", "00:00")) not in ("00:00", "0:00", "0:00:00"):
        return "the first chapter does not start at 00:00"
    ends = [*starts[1:], float(duration_s) if duration_s else float("inf")]
    if any(b <= a for a, b in zip(starts, starts[1:], strict=False)):
        return "the chapter times are not in ascending order"
    pairs = enumerate(zip(starts, ends, strict=True))
    short = [i + 1 for i, (a, b) in pairs if b - a < CHAPTER_MIN_SECONDS.value]
    if short:
        return f"chapter {short[0]} is shorter than {int(CHAPTER_MIN_SECONDS.value)} seconds"
    return None


def drop_chapter_lines(description: str) -> str:
    """The description without its `MM:SS Title` lines (blank lines they leave are folded)."""
    kept = [ln for ln in description.split("\n") if not _CHAPTER_LINE.match(ln)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip("\n")


# ------------------------------------------------------------------------------------------- disclosure
REASON_CLONE = "the narration is a voice cloned from a person's recording"
NOTE_CLONE = (
    "YouTube lists \"Cloning one’s own voice to create voice overs or dubs\" under \"Examples of content "
    "creators don’t have to disclose\". If this is your own voice and nothing else in the video could pass "
    "as a real person, place or event, No is allowed."
)
REASON_REAL = (
    "the story could pass as a real event or place (a real place, a claim that it is true, or a setting "
    "the check could not rule out)"
)
REASON_SYNTHETIC = (
    "the narrator is a synthetic voice that imitates no real person, and the story was not found to pass "
    "as real"
)
NOTE_SYNTHETIC = (
    "YouTube asks for Yes when realistic content could be taken for a real person, place or event; a "
    "narrator voice that imitates nobody is not on its list. The call is yours if you think viewers would "
    "take this story as true."
)
REASON_UNKNOWN = (
    "Nightshift did not record which voice narrated this video (it was made before Nightshift kept that "
    "record), so the answer is yours"
)
NOTE_UNKNOWN = (
    "YouTube: \"Creators must disclose GenAI content that: Makes a real person appear to say or do something "
    "they didn’t do.\" Under \"Examples of content creators don’t have to disclose\" it lists \"Cloning "
    "one’s own voice to create voice overs or dubs\". Answer Yes if the narration is a voice cloned from "
    "someone other than you."
)
REASON_CLIP = "the clip is your own footage; Nightshift only cut it, framed it and added captions"
REASON_CLIP_YES = "the metadata check asked for it"
UNKNOWN = "unknown"  # the voice mode of a job whose narrating voice was never recorded


@dataclass(frozen=True)
class Disclosure:
    """The altered or synthetic content answer, why, and what is left to the owner's judgement.

    `answer` is "yes", "no" or "unknown" (the owner decides; the sheet says so); `stored` is what the `.json`
    keeps, always "yes" or "no" (the policy gates' answer when the sheet leaves it to the owner)."""

    answer: str
    reason: str
    note: str = ""
    stored: str = ""

    def __post_init__(self) -> None:
        if not self.stored:
            object.__setattr__(self, "stored", "yes" if self.answer == "yes" else "no")


def disclosure(answer: str, voice_mode: str | None, *, kind: str = "short") -> Disclosure:
    """R8 for the cases Nightshift makes. `answer` is the policy gates' answer without the voice.

    A cloned voice is always Yes (whose voice it is is not recorded; a Yes only adds YouTube's label), with
    YouTube's own-voice example said in the note. A preset or designed voice imitates no real person and
    leaves the gates' answer as it is. A job whose narrating voice was never recorded (`UNKNOWN`, made before
    the voice record) is Yes when the gates say Yes, else left to the owner with the rule quoted: never the
    channel's current voice, which may not be the one heard. A clip is the owner's own footage."""
    if kind == "clip":
        return Disclosure(answer, REASON_CLIP_YES if answer == "yes" else REASON_CLIP)
    if voice_mode == "clone":
        return Disclosure("yes", REASON_CLONE, NOTE_CLONE)
    if answer == "yes":
        return Disclosure("yes", REASON_REAL)
    if voice_mode == UNKNOWN:
        return Disclosure(UNKNOWN, REASON_UNKNOWN, NOTE_UNKNOWN, stored="no")
    return Disclosure("no", REASON_SYNTHETIC, NOTE_SYNTHETIC)


VOICE_RECORD = "audio/voice.json"


def job_voice_mode(ctx: JobContext) -> str:
    """The voice mode the job narrated with (see `voice_mode_of`), or `UNKNOWN`."""
    return voice_mode_of(ctx.job_dir)


def voice_mode_of(job_dir: Path) -> str:
    """The voice the job narrated with: `audio/voice.json` (written by the voice stage), else the series
    design's voice (a part is narrated by it), else `UNKNOWN`. The channel's current voice is never used:
    the owner may have changed it after the video was narrated."""
    for name, pick in ((VOICE_RECORD, lambda d: d.get("mode")),
                       ("series_context.json",
                        lambda d: ((d.get("design") or {}).get("voice") or {}).get("mode"))):
        try:
            data = json.loads((job_dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        mode = pick(data) if isinstance(data, dict) else None
        if mode in ("preset", "design", "clone"):
            return str(mode)
    return UNKNOWN


# ------------------------------------------------------------------------------------------- the check
def problems(*, title: str, description: str, tags: Sequence[str], hashtags: Sequence[str]) -> list[str]:
    """What in these fields YouTube would refuse or ignore (R1 to R5); empty when the sheet holds."""
    out: list[str] = []
    fields = [title, description, *tags, *hashtags]
    if any(_BRACKETS.search(f) for f in fields):
        out.append("R1: < or > in a field")
    bad_title = not title.strip() or title != title.strip() or "\n" in title
    if bad_title or title_units(title) > TITLE_MAX_CHARS.value:
        out.append(f"R2: title of {title_units(title)} units, or empty, or not trimmed")
    if description_bytes(description) + hashtag_line_bytes(hashtags) > DESCRIPTION_MAX_BYTES.value:
        out.append("R3: description over 5,000 bytes with its hashtag line")
    if tags_length(tags) > TAGS_MAX_CHARS.value or any(not t.strip() or "," in t for t in tags):
        out.append("R4: tags over 500, or an empty tag, or a comma in a tag")
    bodies = [h[1:] for h in hashtags]
    if (len(hashtags) > HASHTAGS_KEPT or any(not h.startswith("#") or not h[1:] for h in hashtags)
            or any(not hashtag_char(c) for b in bodies for c in b)
            or len({b.lower() for b in bodies}) != len(bodies)):
        out.append("R5: a hashtag that is not one word after one #, a repeat, or more than five")
    return out


# ------------------------------------------------------------------------------------- the sheet's notes
KEPT_PREFIX = "YOUR EDITS KEPT: "


def kept_line(name: str) -> str:
    """The line that names the copy of a hand-edited sheet the start sweep kept."""
    return (f"{KEPT_PREFIX}this sheet was rewritten for YouTube's rules; the copy you edited is kept next "
            f"to it as {name}")


@dataclass
class Notes:
    """The lines a sheet carries for the owner: warnings, changes, the copy of their edits it kept, and the
    disclosure's reason and note (`decide`: the answer is left to the owner)."""

    warnings: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    reason: str = ""
    note: str = ""
    decide: bool = False
    kept: str = ""

    def merged(self, other: Notes) -> Notes:
        """These notes plus `other`'s warnings and changes (no repeats); `other`'s disclosure when it has
        one."""
        theirs = bool(other.reason)
        return Notes(
            list(dict.fromkeys([*self.warnings, *other.warnings])),
            list(dict.fromkeys([*self.changes, *other.changes])),
            other.reason if theirs else self.reason, other.note if theirs else self.note,
            other.decide if theirs else self.decide, other.kept or self.kept,
        )

    def owner_lines(self) -> list[str]:
        """The warnings, the kept copy and the changes, as the sheet's head and the Queue show them."""
        lines = [f"{WARNING_PREFIX}{w}" for w in self.warnings]
        if self.kept:
            lines.append(kept_line(self.kept))
        if self.changes:
            lines.append(CHANGED_PREFIX + "; ".join(self.changes))
        return lines

    def head_lines(self) -> list[str]:
        """The lines at the top of the sheet (empty when there is nothing to say)."""
        lines = self.owner_lines()
        return [*lines, ""] if lines else []


_DISCLOSURE_LINE = re.compile(r"^  Altered or synthetic content: (Yes|No|You decide)(?: \((.*)\))?$")
_KEPT = re.compile(r"^" + re.escape(KEPT_PREFIX) + r".* as (.+)$")


def read_notes(sheet_text: str | None) -> Notes:
    """The notes an earlier `.txt` sheet carries (rewriting a sheet keeps what it already told the owner)."""
    notes = Notes()
    lines = (sheet_text or "").replace("\r\n", "\n").split("\n")
    for i, ln in enumerate(lines):
        if ln.startswith(WARNING_PREFIX):
            notes.warnings.append(ln[len(WARNING_PREFIX):])
        elif ln.startswith(CHANGED_PREFIX):
            notes.changes.extend(c for c in ln[len(CHANGED_PREFIX):].split("; ") if c)
        elif m := _KEPT.match(ln):
            notes.kept = m.group(1)
        elif m := _DISCLOSURE_LINE.match(ln):
            notes.decide = m.group(1) == "You decide"
            notes.reason = m.group(2) or ""
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            notes.note = nxt.strip() if nxt.startswith("    ") else ""
    return notes


def disclosure_lines(answer: str, notes: Notes) -> list[str]:
    """The sheet's altered-content line, with its reason, and the note below it."""
    reason = f" ({notes.reason})" if notes.reason else ""
    label = "You decide" if notes.decide else answer.capitalize()
    note = [f"    {notes.note}"] if notes.note else []
    return [f"  Altered or synthetic content: {label}{reason}", *note]


def notes_for(told: Disclosure, warnings: Sequence[str] = (), changes: Sequence[str] = ()) -> Notes:
    """The notes of a sheet whose disclosure is `told`."""
    return Notes(list(warnings), list(changes), told.reason, told.note, told.answer == UNKNOWN)


def bare_sheet(text: str) -> str:
    """A sheet's text without what Nightshift adds for the owner (the head notes, the disclosure answer, its
    reason and note), line ends as LF and blank runs folded: two sheets with the same bare text hold the same
    fields, so a `.txt` whose bare text differs from what its `.json` renders was edited by hand."""
    out: list[str] = []
    skip_note = False
    for ln in text.replace("\r\n", "\n").split("\n"):
        if skip_note:
            skip_note = False
            if ln.startswith("    "):
                continue
        if ln.startswith((WARNING_PREFIX, CHANGED_PREFIX, KEPT_PREFIX, CREDIT_HEAD)):
            continue  # the credit is an offer, not a field: a sheet written without it (rc4) is not "edited"
        if _DISCLOSURE_LINE.match(ln):
            out.append("  Altered or synthetic content:")
            skip_note = True
            continue
        out.append(ln.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n")
