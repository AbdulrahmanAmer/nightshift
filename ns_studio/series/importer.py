"""Turn the script files of one part into the artifacts of C5's master stages (component C11).

Pure functions shared by the `script_import` stage, `approve` and `produce`: the script and Shorts files are
the only input, the output has the shapes of `story.json`, `plan.json`, `plan_meta.json`, `idea.json` and
`shorts_plan.json` (spec 15.8, 7.1 M1-M4, M7).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ns_common.errors import NsError
from ns_common.models import Idea, Plan, PlanChapter, ShortsPlanItem, Story
from ns_common.textstats import split_sentences, word_count
from ns_studio.series.store import (
    _HEAD,
    CLOSING_MAX_WORDS,
    HOOK_MAX_WORDS,
    ConceptDoc,
    ScriptDoc,
    ShortDoc,
    join_front_matter,
    resolve_span,
    split_front_matter,
)
from ns_studio.text.stages_master import build_chapter


@dataclass
class PartArtifacts:
    """Everything `script_import` writes for one part."""

    story: Story
    plan: dict[str, Any]
    plan_meta: dict[str, Any]
    idea: dict[str, Any]
    shorts_plan: list[dict[str, Any]]


def validate_short(doc: ShortDoc) -> None:
    """Hook <= 12 words, closing line <= 15 words and a non-empty, ordered span (NS-E303 otherwise)."""
    name = f"s{doc.k:02d}"
    if not 0 < len(doc.hook.split()) <= HOOK_MAX_WORDS:
        n_words = len(doc.hook.split())
        raise NsError("NS-E303", f"{name}: hook must be 1-{HOOK_MAX_WORDS} words, has {n_words}")
    if not 0 < len(doc.closing_line.split()) <= CLOSING_MAX_WORDS:
        raise NsError("NS-E303", f"{name}: closing line must be 1-{CLOSING_MAX_WORDS} words")
    if doc.span[0] < 1 or doc.span[1] < doc.span[0]:
        raise NsError("NS-E303", f"{name}: span {doc.span} must be ascending sentence numbers from 1")


def _idea(concept: ConceptDoc | None, script: ScriptDoc) -> dict[str, Any]:
    title = concept.title if concept else script.title
    setting = (concept.setting if concept else "") or "an everyday place"
    words = re.findall(r"[a-z]+", f"{setting} {title}".lower())
    keywords = [w for w in dict.fromkeys(words) if len(w) > 3][:6] or ["night"]
    beats = concept.beats if concept else []
    return Idea(
        premise=(concept.premise if concept and concept.premise else script.chapters[0].summary) or title,
        twist=beats[3] if len(beats) > 3 else script.chapters[-1].cliff,
        setting=setting,
        protagonist=concept.cast[0] if concept and concept.cast else "the narrator",
        why_chapters=concept.series_potential if concept else "",
        keywords=keywords,
    ).model_dump()


def build_artifacts(
    script: ScriptDoc,
    shorts: list[ShortDoc],
    *,
    wpm: int,
    combo_id: str,
    concept: ConceptDoc | None = None,
) -> PartArtifacts:
    """Validate the files of one part and build the master artifacts from them.

    Raises NS-E303 for a Short with a bad hook, closing line or span, a span that crosses a chapter, two
    Shorts with the same hook or the same number.
    """
    last = script.chapters[-1].n
    chapters = [
        build_chapter(c.n, c.title, c.text, c.summary or c.title, wpm=wpm, is_last=c.n == last)
        for c in script.chapters
    ]
    story = Story(working_title=script.title, chapters=chapters, revisions=0)

    flat = script.sentences()
    texts = [t for _, _, t in flat]
    items: list[dict[str, Any]] = []
    seen_k: set[int] = set()
    seen_hooks: set[str] = set()
    for doc in sorted(shorts, key=lambda s: s.k):
        validate_short(doc)
        if doc.k in seen_k or doc.hook.strip().lower() in seen_hooks:
            raise NsError("NS-E303", f"s{doc.k:02d}: duplicate Short number or hook")
        seen_k.add(doc.k)
        seen_hooks.add(doc.hook.strip().lower())
        a, b = resolve_span(doc, texts)
        if flat[a][0] != flat[b][0]:
            raise NsError("NS-E303", f"s{doc.k:02d}: span {doc.span} crosses a chapter break")
        hooks = [doc.hook, *[h for h in doc.hooks if h != doc.hook]][:5]
        item = ShortsPlanItem(
            k=doc.k,
            chapter=flat[a][0],
            sentence_start=flat[a][1],
            sentence_end=flat[b][1],
            hooks=hooks,
            chosen_hook=doc.hook,
            closing_line=doc.closing_line,
            length_bucket=doc.length_bucket,
            combo_id=combo_id,
            why_standalone=doc.why_standalone or "Stands alone as written in the script file.",
        )
        items.append(item.model_dump())

    total = sum(c.word_count for c in chapters)
    plan_chapters = [
        PlanChapter(n=c.n, title=c.title, beats=c.beats or [c.summary or c.title], cliff=c.cliff or c.title,
                    word_budget=max(1, word_count(c.text)))
        for c in script.chapters
    ]
    plan: dict[str, Any] = {
        "target_minutes": max(9, round(total / wpm)),
        "target_words": max(1, total),
        "chapters": [c.model_dump() for c in plan_chapters],
    }
    if 5 <= len(plan_chapters) <= 7:  # `Plan` only models master-sized stories (see CR-C11-3)
        plan = Plan.model_validate(plan).model_dump()
    plan_meta = {"working_title": script.title, "wpm": wpm, "min_words": 9 * wpm}
    return PartArtifacts(
        story=story, plan=plan, plan_meta=plan_meta, idea=_idea(concept, script), shorts_plan=items
    )


# --- a script typed or pasted as plain text (JobRunRequest.script) ---------------------------------

PLAIN_CHAPTER_WORDS = 700  # a long video from plain text gets about one chapter per this many words
PLAIN_MAX_CHAPTERS = 7
TITLE_MAX_CHARS = 60


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


def script_markdown(text: str, title: str | None, *, chaptered: bool) -> str:
    """The `script.md` for a story the owner wrote as plain text, so `script_import` reads it like a part.

    A text that already has `## 1. Title` chapter headings (and optional front-matter) is kept as it is. Plain
    text becomes one chapter for a Short; for a long video it is cut into chapters of about
    `PLAIN_CHAPTER_WORDS` words at paragraph ends (at sentence ends when it has too few paragraphs), because
    the long video's chapter cards, cliff lines and Shorts selection work per chapter. The title is `title`,
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
    n = 1
    if chaptered:
        n = max(1, min(PLAIN_MAX_CHAPTERS, round(word_count(body) / PLAIN_CHAPTER_WORDS)))
    units = paragraphs if len(paragraphs) >= n else split_sentences(body)
    groups = _balanced(units, min(n, len(units)))
    blocks = [
        f"## {i}. {named if len(groups) == 1 else f'Part {i}'}\n\n" + "\n\n".join(g)
        for i, g in enumerate(groups, start=1)
    ]
    return join_front_matter({"part": 1, "title": named}, "\n\n".join(blocks))
