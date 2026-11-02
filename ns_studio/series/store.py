"""Story folder model of the Series Workshop (component C11; spec 23.2).

Layout under `<NIGHTSHIFT_HOME>/stories/<slug>/`: `concept.md`, `concept-A..C.md`, `series.md`,
`design.yaml`, `status.yaml`, `parts/part-NN/{full/script.md, shorts/sNN.md, notes.md}`. Everything is plain
UTF-8 text with LF line endings so the owner can edit it by hand.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, ValidationError

from ns_common.errors import NsError
from ns_common.models import SLUG_RE, Strict, Voice
from ns_common.paths import Paths
from ns_common.textstats import split_sentences, word_count

MIN_MINUTES = 12
MAX_MINUTES = 25
DEFAULT_MINUTES = 15
BREAK = "[BREAK]"
HOOK_MAX_WORDS = 12
CLOSING_MAX_WORDS = 15

StoryStatus = Literal["drafting", "approved", "producing", "published"]
PartStatus = Literal["planned", "written", "approved", "producing", "published"]


class WorkshopError(ValueError):
    """A workshop usage error: unknown story, wrong status, value out of range (see CR-C11-1)."""


# --- markdown with YAML front-matter -------------------------------------------------------------

_FM = re.compile(r"\A---\n(.*?)\n---[ \t]*(?:\n|\Z)(.*)\Z", re.S)
_COMMENT = re.compile(r"<!--.*?-->", re.S)


def split_front_matter(text: str) -> tuple[dict[str, Any], str]:
    """Split `---` YAML front-matter from the body; files without front-matter give `({}, text)`."""
    text = text.replace("\r\n", "\n").lstrip("﻿")
    m = _FM.match(text)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        raise WorkshopError(f"front-matter is not valid YAML: {e}") from e
    if not isinstance(meta, dict):
        raise WorkshopError("front-matter must be a YAML mapping")
    return meta, m.group(2)


def join_front_matter(meta: dict[str, Any], body: str) -> str:
    """Inverse of `split_front_matter`: YAML block (flow style for short lists) plus the body."""
    head = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, default_flow_style=None, width=100)
    return f"---\n{head.rstrip()}\n---\n{body.strip(chr(10))}\n"


def write_text(path: Path, text: str) -> None:
    """Write `text` as UTF-8 with LF endings through a temp file (no half-written files)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
    tmp.replace(path)


def read_text(path: Path) -> str:
    """Read a UTF-8 file with LF endings."""
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


def read_md(path: Path) -> tuple[dict[str, Any], str]:
    """Read a markdown file into `(front-matter, body)`."""
    return split_front_matter(read_text(path))


def write_md(path: Path, meta: dict[str, Any], body: str) -> None:
    """Write a markdown file with front-matter."""
    write_text(path, join_front_matter(meta, body))


def parse_sections(body: str) -> dict[str, str]:
    """Split a body on `## Heading` lines into `{heading: text}` (document order, text stripped)."""
    out: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in body.split("\n"):
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if current is not None:
                out[current] = "\n".join(buf).strip()
            current, buf = m.group(1), []
        elif current is not None:
            buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf).strip()
    return out


def render_sections(title: str, sections: dict[str, str]) -> str:
    """Body with a `# title` line followed by `## heading` sections (empty ones are kept)."""
    parts = [f"# {title}"]
    for head, text in sections.items():
        parts.append(f"## {head}\n\n{text.strip()}".rstrip())
    return "\n\n".join(parts)


def bullets(text: str) -> list[str]:
    """Items of a `- item` / `1. item` list (other lines are ignored)."""
    out: list[str] = []
    for line in text.split("\n"):
        m = re.match(r"^\s*(?:[-*]|\d+[.)])\s+(.*\S)\s*$", line)
        if m:
            out.append(m.group(1))
    return out


def as_bullets(items: list[str], *, numbered: bool = False) -> str:
    """Render `items` as a markdown list."""
    return "\n".join(f"{i}. {t}" if numbered else f"- {t}" for i, t in enumerate(items, start=1))


def _key(heading: str) -> str:
    return re.sub(r"\W+", "_", heading.lower()).strip("_")


# --- concept --------------------------------------------------------------------------------------


class ConceptDoc(Strict):
    """A concept (`concept.md`) or a draft (`concept-A.md`)."""

    letter: str | None = None
    title: str
    premise: str = ""
    cast: list[str] = []
    setting: str = ""
    tone: str = ""
    beats: list[str] = []
    why_it_works: str = ""
    series_potential: str = ""
    audience: str = ""
    risks: list[str] = []
    feedback: list[str] = []
    revision: int = Field(default=0, ge=0)


def render_concept(doc: ConceptDoc) -> str:
    """Markdown of a concept: small front-matter, then one section per field."""
    sections = {
        "Premise": doc.premise,
        "Cast": as_bullets(doc.cast),
        "Setting": doc.setting,
        "Tone": doc.tone,
        "Beats": as_bullets(doc.beats, numbered=True),
        "Why it works": doc.why_it_works,
        "Series potential": doc.series_potential,
        "Audience": doc.audience,
        "Risks": as_bullets(doc.risks),
        "Feedback": as_bullets(doc.feedback),
    }
    meta: dict[str, Any] = {"title": doc.title, "letter": doc.letter, "revision": doc.revision}
    return join_front_matter(meta, render_sections(doc.title, sections))


def parse_concept(text: str) -> ConceptDoc:
    """Parse `concept*.md`; headings are matched case-insensitively and missing ones stay empty."""
    meta, body = split_front_matter(text)
    sec = {_key(k): v for k, v in parse_sections(body).items()}
    title = str(meta.get("title") or "")
    if not title:
        m = re.search(r"^#\s+(.+?)\s*$", body, re.M)
        title = m.group(1) if m else "Untitled"
    return ConceptDoc(
        letter=meta.get("letter"),
        title=title,
        premise=sec.get("premise", ""),
        cast=bullets(sec.get("cast", "")),
        setting=sec.get("setting", ""),
        tone=sec.get("tone", ""),
        beats=bullets(sec.get("beats", "")),
        why_it_works=sec.get("why_it_works", ""),
        series_potential=sec.get("series_potential", ""),
        audience=sec.get("audience", ""),
        risks=bullets(sec.get("risks", "")),
        feedback=bullets(sec.get("feedback", "")),
        revision=int(meta.get("revision") or 0),
    )


def concept_text(doc: ConceptDoc) -> str:
    """Plain text of a concept for the policy gate and prompts (title, premise, cast, beats, ...)."""
    return "\n".join(
        [
            doc.title,
            doc.premise,
            "Cast: " + "; ".join(doc.cast),
            f"Setting: {doc.setting}",
            "Beats: " + " ".join(doc.beats),
            doc.why_it_works,
            doc.series_potential,
        ]
    )


# --- series map, status, design -------------------------------------------------------------------


class PartEntry(Strict):
    """One row of the part map (`series.md` front-matter)."""

    n: int = Field(ge=1)
    title: str
    hook: str
    cliff: str
    reveals: str = ""
    minutes: int = Field(ge=MIN_MINUTES, le=MAX_MINUTES)
    recap: str = ""
    status: PartStatus = "planned"


class SeriesDoc(Strict):
    """`series.md`: title, runtime default and the part map; the body is a generated overview."""

    title: str
    channel: str
    minutes_per_part: int = Field(default=DEFAULT_MINUTES, ge=MIN_MINUTES, le=MAX_MINUTES)
    arc_payoff: str = ""
    warnings: list[str] = []
    parts: list[PartEntry] = Field(min_length=1)

    def entry(self, n: int) -> PartEntry:
        """The map row of part `n`; WorkshopError when the series has no such part."""
        for p in self.parts:
            if p.n == n:
                return p
        raise WorkshopError(f"series has no part {n} (parts: 1-{len(self.parts)})")


def render_series(doc: SeriesDoc) -> str:
    """`series.md` text: front-matter is the source of truth, the body is a readable overview."""
    meta: dict[str, Any] = {
        "title": doc.title,
        "channel": doc.channel,
        "parts_total": len(doc.parts),
        "minutes_per_part": doc.minutes_per_part,
        "arc_payoff": doc.arc_payoff,
        "warnings": doc.warnings,
        "parts": [p.model_dump() for p in doc.parts],
    }
    lines = [f"# {doc.title}", "", "Generated overview. Edit the front-matter above; it is the part map.", ""]
    for p in doc.parts:
        lines += [
            f"## Part {p.n}: {p.title} ({p.minutes} min, {p.status})",
            f"- Hook: {p.hook}",
            f"- Cliff: {p.cliff}",
            f"- Reveals: {p.reveals}",
            "",
        ]
    if doc.arc_payoff:
        lines += ["## Series payoff", doc.arc_payoff, ""]
    if doc.warnings:
        lines += ["## Needs attention", *[f"- {w}" for w in doc.warnings], ""]
    return join_front_matter(meta, "\n".join(lines))


def parse_series(text: str) -> SeriesDoc:
    """Parse `series.md` (front-matter only)."""
    meta, _ = split_front_matter(text)
    meta.pop("parts_total", None)
    try:
        return SeriesDoc.model_validate(meta)
    except ValidationError as e:
        raise WorkshopError(f"series.md is invalid: {str(e)[:300]}") from e


class PartState(Strict):
    """Status of one part in `status.yaml`."""

    status: PartStatus = "planned"
    job_id: str | None = None
    words: int = Field(default=0, ge=0)
    updated: str | None = None


class StatusDoc(Strict):
    """`status.yaml`: story status plus per-part status and job ids."""

    slug: str
    channel: str
    status: StoryStatus = "drafting"
    chosen: str | None = None
    created: str = ""
    parts: dict[int, PartState] = Field(default_factory=dict)

    def recompute(self) -> None:
        """Derive the story status from the parts (published > producing > approved > drafting)."""
        states = [p.status for p in self.parts.values()]
        if not states:
            return
        if all(s == "published" for s in states):
            self.status = "published"
        elif any(s in ("producing", "published") for s in states):
            self.status = "producing"
        elif all(s == "approved" for s in states):
            self.status = "approved"
        else:
            self.status = "drafting"


class DesignDoc(Strict):
    """`design.yaml`: how every part of the series looks and when it is published."""

    format: str = "long_16x9"
    visual_treatment: str = "dark_slowzoom"
    caption_style: str = "clean_white"
    voice: Voice | None = None  # None keeps the channel's own voice
    thumbnail_style: str = "auto"
    publish_cadence_days: int = Field(default=7, ge=1, le=30)


# --- script and Shorts ----------------------------------------------------------------------------


class ScriptChapter(Strict):
    """One chapter of a part script."""

    n: int = Field(ge=1)
    title: str
    text: str
    summary: str = ""
    beats: list[str] = []
    cliff: str = ""


class ScriptDoc(Strict):
    """`full/script.md`: chapters; outline facts live in the front-matter."""

    part: int = Field(ge=1)
    title: str
    critic_avg: float | None = None
    chapters: list[ScriptChapter] = Field(min_length=1)

    def words(self) -> int:
        """Total spoken words."""
        return sum(word_count(c.text) for c in self.chapters)

    def sentences(self) -> list[tuple[int, int, str]]:
        """Flat sentence list as `(chapter n, index within chapter (0-based), text)`.

        The list position plus one is the global sentence number used in `<!-- sK -->` comments.
        """
        out: list[tuple[int, int, str]] = []
        for c in self.chapters:
            out += [(c.n, i, s) for i, s in enumerate(split_sentences(c.text))]
        return out


def _paragraphs(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]


_TERMINAL = ".!?\"'”’)"


def normalize_text(text: str) -> str:
    """Paragraphs separated by blank lines, each ending in terminal punctuation."""
    out: list[str] = []
    for p in _paragraphs(text):
        out.append(p if p[-1] in _TERMINAL else p + ".")
    return "\n\n".join(out)


def _render_chapter_body(text: str, start_no: int) -> tuple[str, int]:
    pars = _paragraphs(text)
    whole = split_sentences(text)
    per = [split_sentences(p) for p in pars]
    if sum(len(x) for x in per) != len(whole):
        per = [whole]  # sentence boundaries cross paragraphs: keep the numbering, drop the layout
    no = start_no
    blocks: list[str] = []
    for sents in per:
        blocks.append(" ".join(f"<!-- s{no + i} -->{s}" for i, s in enumerate(sents)))
        no += len(sents)
    return "\n\n".join(blocks), no


def render_script(doc: ScriptDoc) -> str:
    """`script.md` text: `## N. Title` headings, numbered sentences, `[BREAK]` at every chapter cliff."""
    meta: dict[str, Any] = {
        "part": doc.part,
        "title": doc.title,
        "words": doc.words(),
        "critic_avg": None if doc.critic_avg is None else round(doc.critic_avg, 2),
        "chapters": [
            {"n": c.n, "title": c.title, "summary": c.summary, "beats": c.beats, "cliff": c.cliff}
            for c in doc.chapters
        ],
    }
    blocks: list[str] = []
    no = 1
    for i, ch in enumerate(doc.chapters):
        body, no = _render_chapter_body(ch.text, no)
        block = f"## {ch.n}. {ch.title}\n\n{body}"
        if i < len(doc.chapters) - 1:
            block += f"\n\n{BREAK}"
        blocks.append(block)
    return join_front_matter(meta, "\n\n".join(blocks))


_HEAD = re.compile(r"^##[ \t]+(?:Chapter[ \t]+)?(\d+)[ \t]*[.:)\-][ \t]*(.*?)[ \t]*$", re.M)


def parse_script(text: str) -> ScriptDoc:
    """Parse `script.md`; comments and `[BREAK]` lines are dropped, chapters must be numbered 1..k.

    Raises NS-E303 when no chapter heading is found or the numbering is not 1..k.
    """
    meta, body = split_front_matter(text)
    heads = list(_HEAD.finditer(body))
    if not heads:
        raise NsError("NS-E303", "script.md has no '## N. Title' chapter headings")
    extra: dict[int, dict[str, Any]] = {}
    for c in meta.get("chapters") or []:
        if isinstance(c, dict) and "n" in c:
            extra[int(c["n"])] = c
    chapters: list[ScriptChapter] = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
        raw = _COMMENT.sub("", body[h.end() : end])
        raw = "\n".join(ln for ln in raw.split("\n") if ln.strip() != BREAK)
        n = int(h.group(1))
        if n != i + 1:
            raise NsError("NS-E303", f"script.md chapters must be numbered 1..k, found {n} at {i + 1}")
        body_text = normalize_text(raw)
        if not body_text:
            raise NsError("NS-E303", f"script.md chapter {n} is empty")
        sentences = split_sentences(body_text)
        e = extra.get(n, {})
        chapters.append(
            ScriptChapter(
                n=n,
                title=h.group(2) or f"Chapter {n}",
                text=body_text,
                summary=str(e.get("summary") or " ".join(sentences[:2])),
                beats=[str(b) for b in e.get("beats") or []] or [sentences[0]],
                cliff=str(e.get("cliff") or sentences[-1]),
            )
        )
    critic = meta.get("critic_avg")
    return ScriptDoc(
        part=int(meta.get("part") or 1),
        title=str(meta.get("title") or "Untitled"),
        critic_avg=None if critic is None else float(critic),
        chapters=chapters,
    )


class ShortDoc(Strict):
    """`shorts/sNN.md`: one stand-alone moment of a part."""

    k: int = Field(ge=1)
    hook: str
    span: list[int] = Field(min_length=2, max_length=2)
    closing_line: str
    length_bucket: int = Field(ge=1)
    chapter: int | None = None
    why_standalone: str = ""
    hooks: list[str] = []
    start_text: str = ""
    text: str = ""


def anchor_of(sentence: str) -> str:
    """First eight words of a sentence (the anchor that keeps a Short attached to its moment)."""
    return " ".join(sentence.split()[:8])


def render_short(doc: ShortDoc) -> str:
    """Markdown of a Short: front-matter plus a readable body (hook, moment text, closing line)."""
    meta: dict[str, Any] = {
        "k": doc.k,
        "hook": doc.hook,
        "span": doc.span,
        "chapter": doc.chapter,
        "closing_line": doc.closing_line,
        "length_bucket": doc.length_bucket,
        "why_standalone": doc.why_standalone,
        "hooks": doc.hooks,
        "start_text": doc.start_text,
    }
    body = f"Hook: {doc.hook}\n\n{doc.text}\n\nClosing: {doc.closing_line}"
    return join_front_matter(meta, body)


def parse_short(text: str) -> ShortDoc:
    """Parse a `sNN.md` file; the body is informational and ignored."""
    meta, body = split_front_matter(text)
    meta = {k: v for k, v in meta.items() if v is not None}
    try:
        doc = ShortDoc.model_validate(meta)
    except ValidationError as e:
        raise NsError("NS-E303", f"short file is invalid: {str(e)[:300]}") from e
    return doc.model_copy(update={"text": doc.text or body.strip()})


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.lower()).strip()


def resolve_span(doc: ShortDoc, sentences: list[str]) -> tuple[int, int]:
    """0-based inclusive `(first, last)` indexes of the Short's span in the flat sentence list.

    The numbers in `doc.span` are tried first (and must agree with `start_text` when it is set); when the
    owner edited the script, the span is moved to the nearest sentence that starts with `start_text`,
    keeping its length. A span that is out of range and cannot be relocated raises NS-E303.
    """
    n = len(sentences)
    a, b = doc.span[0] - 1, doc.span[1] - 1
    anchor = _norm(doc.start_text)

    def matches(i: int) -> bool:
        return not anchor or _norm(sentences[i]).startswith(anchor)

    if 0 <= a <= b < n and matches(a):
        return a, b
    hits = [i for i in range(n) if anchor and matches(i)]
    if hits:
        i = min(hits, key=lambda h: abs(h - a))
        return i, min(n - 1, i + max(0, b - a))
    if 0 <= a <= b < n:
        return a, b  # the start sentence itself was edited; trust the numbers
    raise NsError("NS-E303", f"short s{doc.k:02d}: span {doc.span} no longer fits the script; fix `span`")


# --- the folder -----------------------------------------------------------------------------------


def stories_dir(paths: Paths) -> Path:
    """`<NIGHTSHIFT_HOME>/stories`."""
    return paths.home / "stories"


def list_slugs(paths: Paths) -> list[str]:
    """Slugs of every story folder, sorted."""
    base = stories_dir(paths)
    return sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []


@dataclass(frozen=True)
class StoryDir:
    """Paths and typed readers/writers of one story folder."""

    root: Path

    @classmethod
    def open(cls, paths: Paths, slug: str, *, must_exist: bool = True) -> StoryDir:
        """The folder of `slug`; WorkshopError for a malformed slug or (by default) a missing folder."""
        if not re.match(SLUG_RE, slug):
            raise WorkshopError(f"{slug!r} is not a valid story slug (lowercase letters, digits, dashes)")
        sd = cls(stories_dir(paths) / slug)
        if must_exist and not sd.root.is_dir():
            raise WorkshopError(f"no story {slug!r}; run `studio series list`")
        return sd

    @property
    def slug(self) -> str:
        """Folder name."""
        return self.root.name

    @property
    def concept(self) -> Path:
        """`concept.md`."""
        return self.root / "concept.md"

    def draft(self, letter: str) -> Path:
        """`concept-<LETTER>.md`."""
        return self.root / f"concept-{letter.upper()}.md"

    def draft_letters(self) -> list[str]:
        """Letters of the existing drafts, sorted."""
        found = (re.fullmatch(r"concept-([A-Z])\.md", p.name) for p in self.root.glob("concept-*.md"))
        return sorted(m.group(1) for m in found if m)

    @property
    def series(self) -> Path:
        """`series.md`."""
        return self.root / "series.md"

    @property
    def design(self) -> Path:
        """`design.yaml`."""
        return self.root / "design.yaml"

    @property
    def status(self) -> Path:
        """`status.yaml`."""
        return self.root / "status.yaml"

    def part_dir(self, n: int) -> Path:
        """`parts/part-NN`."""
        return self.root / "parts" / f"part-{n:02d}"

    def script(self, n: int) -> Path:
        """`parts/part-NN/full/script.md`."""
        return self.part_dir(n) / "full" / "script.md"

    def shorts_dir(self, n: int) -> Path:
        """`parts/part-NN/shorts`."""
        return self.part_dir(n) / "shorts"

    def short_file(self, n: int, k: int) -> Path:
        """`parts/part-NN/shorts/sKK.md`."""
        return self.shorts_dir(n) / f"s{k:02d}.md"

    def notes(self, n: int) -> Path:
        """`parts/part-NN/notes.md`."""
        return self.part_dir(n) / "notes.md"

    # typed access
    def read_concept(self, path: Path | None = None) -> ConceptDoc:
        """Parse `concept.md` (or the given draft file)."""
        return parse_concept(read_text(path or self.concept))

    def write_concept(self, doc: ConceptDoc, path: Path | None = None) -> None:
        """Write `concept.md` (or the given draft file)."""
        write_text(path or self.concept, render_concept(doc))

    def read_series(self) -> SeriesDoc:
        """Parse `series.md`; WorkshopError when the map was not written yet."""
        if not self.series.exists():
            raise WorkshopError(f"story {self.slug!r} has no series.md yet; run `studio series map`")
        return parse_series(read_text(self.series))

    def write_series(self, doc: SeriesDoc) -> None:
        """Write `series.md`."""
        write_text(self.series, render_series(doc))

    def read_status(self) -> StatusDoc:
        """Parse `status.yaml`."""
        if not self.status.exists():
            raise WorkshopError(f"story {self.slug!r} has no status.yaml")
        data = yaml.safe_load(read_text(self.status)) or {}
        try:
            return StatusDoc.model_validate(data)
        except ValidationError as e:
            raise WorkshopError(f"status.yaml is invalid: {str(e)[:300]}") from e

    def write_status(self, doc: StatusDoc) -> None:
        """Write `status.yaml`."""
        text = yaml.safe_dump(doc.model_dump(), sort_keys=False, allow_unicode=True)
        write_text(self.status, text)

    def read_design(self) -> DesignDoc | None:
        """Parse `design.yaml`; None when the owner has not designed the series yet."""
        if not self.design.exists():
            return None
        data = yaml.safe_load(read_text(self.design)) or {}
        try:
            return DesignDoc.model_validate(data)
        except ValidationError as e:
            raise WorkshopError(f"design.yaml is invalid: {str(e)[:300]}") from e

    def write_design(self, doc: DesignDoc) -> None:
        """Write `design.yaml`."""
        write_text(self.design, yaml.safe_dump(doc.model_dump(), sort_keys=False, allow_unicode=True))

    def read_script(self, n: int) -> ScriptDoc:
        """Parse `parts/part-NN/full/script.md`; WorkshopError when it was not written yet."""
        if not self.script(n).exists():
            raise WorkshopError(f"part {n} has no script yet; run `studio series write {self.slug} {n}`")
        return parse_script(read_text(self.script(n)))

    def write_script(self, n: int, doc: ScriptDoc) -> None:
        """Write the script of part `n`."""
        write_text(self.script(n), render_script(doc))

    def read_shorts(self, n: int) -> list[ShortDoc]:
        """Every `sNN.md` of part `n`, ordered by file name."""
        d = self.shorts_dir(n)
        return [parse_short(read_text(f)) for f in sorted(d.glob("s*.md"))] if d.is_dir() else []

    def clear_shorts(self, n: int) -> None:
        """Delete the Shorts files of part `n` (they are rewritten as a set)."""
        d = self.shorts_dir(n)
        if d.is_dir():
            for f in d.glob("s*.md"):
                f.unlink()

    def write_short(self, n: int, doc: ShortDoc) -> None:
        """Write one Short file."""
        write_text(self.short_file(n, doc.k), render_short(doc))

    def read_notes(self, n: int) -> dict[str, str]:
        """Sections of `notes.md` (empty dict when absent)."""
        if not self.notes(n).exists():
            return {}
        return parse_sections(read_md(self.notes(n))[1])

    def write_notes(self, n: int, sections: dict[str, str]) -> None:
        """Write `notes.md` from `{heading: text}`."""
        write_md(self.notes(n), {"part": n}, render_sections(f"Part {n} notes", sections))
