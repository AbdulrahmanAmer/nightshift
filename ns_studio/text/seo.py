"""SEO finalizers that enforce the metadata rules in code (component C5; spec 9.1, 9.2, 18.4)."""

from __future__ import annotations

import re
from collections.abc import Sequence

from ns_common.models import Seo

TAG_FILLERS = (
    "short story",
    "fiction",
    "narrated story",
    "story time",
    "original story",
    "audio story",
    "storytelling",
    "suspense",
    "bedtime story",
    "creepy story",
)
MAX_TAG_CHARS = 500


def normalize_tags(tags: Sequence[str], primary_keyword: str) -> list[str]:
    """Dedupe tags (case-insensitive), pad to 10 with generic fiction tags, cap at 20 and 500 characters."""
    seen: set[str] = set()
    out: list[str] = []
    for t in [primary_keyword, *tags]:
        t = t.strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    for filler in TAG_FILLERS:
        if len(out) >= 10:
            break
        if filler.lower() not in seen:
            seen.add(filler.lower())
            out.append(filler)
    out = out[:20]
    while len(out) > 10 and sum(len(t) for t in out) + len(out) - 1 > MAX_TAG_CHARS:
        out.pop()
    return out


def normalize_hashtags(
    hashtags: Sequence[str], primary_keyword: str, *, shorts: bool, limit: int = 5
) -> list[str]:
    """Clean hashtags: # prefix, no spaces, deduped, 3 to `limit`; #Shorts leads when `shorts`."""
    cleaned: list[str] = []
    for h in hashtags:
        word = re.sub(r"\s+", "", h.strip().lstrip("#"))
        if word and ("#" + word).lower() not in {c.lower() for c in cleaned} and word.lower() != "shorts":
            cleaned.append("#" + word)
    keyword_tag = "#" + re.sub(r"[^A-Za-z0-9]", "", primary_keyword.title())
    for extra in (keyword_tag, "#ShortStory", "#NarratedStory", "#OriginalFiction"):
        if len(cleaned) >= 3:
            break
        if len(extra) > 1 and extra.lower() not in {c.lower() for c in cleaned}:
            cleaned.append(extra)
    if shorts:
        return ["#Shorts", *cleaned][:limit]
    return cleaned[:limit]


def fit_title(title: str, primary_keyword: str, *, max_chars: int = 100) -> str:
    """Put the primary keyword in the first 40 characters (prefix it if absent) and cap at `max_chars`."""
    title = title.strip()
    if primary_keyword.lower() not in title[:40].lower():
        title = f"{primary_keyword.title()}: {title}"
    return title[:max_chars].rstrip()


def finalize_seo(
    seo: Seo,
    *,
    primary_keyword: str,
    fiction_label: bool,
    part_n: int | None,
    parts_total: int,
    master_title: str | None = None,
    extra_lines: Sequence[str] = (),
) -> Seo:
    """Apply the Shorts metadata rules of spec 9.2 to an LLM-written `Seo`.

    Title: keyword within the first 40 characters, "Part n" when `part_n`, at most 100 characters.
    Description: the model's lines, "Part n of N." for series, `extra_lines`, "From the full story: <master
    title>" when `master_title` is given, and "Original fiction." last when `fiction_label`.
    Hashtags: `#Shorts` first, at most 5. Tags: 10-20 and at most 500 characters.
    """
    title = fit_title(seo.title, primary_keyword, max_chars=1000)
    if part_n and f"part {part_n}" not in title.lower():
        title = f"{title} (Part {part_n})"
    title = title[:100].rstrip()
    lines = [seo.description.strip()]
    if part_n:
        lines.append(f"Part {part_n} of {parts_total}.")
    lines.extend(line.strip() for line in extra_lines)
    if master_title:
        lines.append(f"From the full story: {master_title.strip()}")
    if fiction_label:
        lines.append("Original fiction.")
    return Seo(
        title=title,
        description="\n".join(line for line in lines if line),
        tags=normalize_tags(seo.tags, primary_keyword),
        hashtags=normalize_hashtags(seo.hashtags, primary_keyword, shorts=True),
        keywords=seo.keywords,
    )
