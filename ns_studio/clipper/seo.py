"""C8 Clipper: clip SEO and metadata-only policy checks (spec 19.2 C8, 9.2).

The LLM answer is normalised to the Shorts rules of section 9.2 (title <= 70, keyword early, one or two
honest lines plus "Clip from: <source>", 3 to 5 hashtags including #Shorts, 10 to 20 tags within 500
characters). The policy checks `misleading_claims`, `metadata_spam` and `real_entities` run on the
metadata only: the spoken content is the owner's.

Clips are the owner's real footage, so the fiction rules do not apply: `misleading_claims` uses its own list
(`clip_misleading_claims.txt`: deceptive clickbait, not "caught on camera"), `real_entities` only looks for
links and @handles, and the fixed `Clip from: <source>` line is never checked (the model cannot change it,
so a source called "Interview with Dr. Smith" used to fail every attempt).
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from ns_common.errors import NsError
from ns_common.models import ClipPlanItem, PolicyCheck, PolicyResult, Seo
from ns_common.paths import contracts_dir
from ns_studio.context import JobContext
from ns_studio.media.youtube_rules import TAGS_MAX_CHARS, tags_length
from ns_studio.text.prompts import call_json

TITLE_TARGET = 70
TAGS_MAX = 20
TAGS_MIN = 10
TAGS_CHARS = int(TAGS_MAX_CHARS.value)  # YouTube's R4, counted by `tags_length`
HASHTAGS_MAX = 5
HASHTAGS_MIN = 3
CHECK_CODES = {"misleading_claims": "NS-E603", "metadata_spam": "NS-E606", "real_entities": "NS-E602"}

_HANDLE_OR_URL = re.compile(r"(?:^|\s)@\w+|https?://|www\.")
_WORD = re.compile(r"[A-Za-z']{4,}")


def _policy_lines(name: str) -> list[str]:
    path = contracts_dir() / "policy" / name
    out: list[str] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        ln = ln.strip().lower()
        if ln and not ln.startswith("#"):
            out.append(ln)
    return out


def _find_phrases(phrases: list[str], text: str) -> list[str]:
    low = text.lower()
    return [p for p in phrases if re.search(rf"(?<!\w){re.escape(p)}(?!\w)", low)]


SOURCE_LINE = "Clip from:"


def metadata_texts(seo: Seo) -> list[str]:
    """Every model-written metadata string the checks look at (not the fixed `Clip from:` line)."""
    written: list[str] = []
    for ln in seo.description.splitlines():
        if ln.startswith(SOURCE_LINE):
            break  # the source line and the owner's creator note after it are not the model's words
        written.append(ln)
    own = "\n".join(written)
    return [seo.title, own, " ".join(seo.tags), " ".join(seo.hashtags)]


# ---------- checks ----------
def check_misleading_claims(seo: Seo) -> PolicyCheck:
    """Fails when a `clip_misleading_claims.txt` phrase appears in title, description, tags or hashtags."""
    hits = _find_phrases(_policy_lines("clip_misleading_claims.txt"), "\n".join(metadata_texts(seo)))
    return PolicyCheck(passed=not hits, detail=f"phrases: {', '.join(hits)}" if hits else "")


def check_metadata_spam(seo: Seo) -> PolicyCheck:
    """Tag count and length limits, hashtag count, shouting titles and keyword stuffing."""
    problems: list[str] = []
    if len(seo.tags) > TAGS_MAX:
        problems.append(f"{len(seo.tags)} tags")
    if tags_length(seo.tags) > TAGS_CHARS:
        problems.append("tags longer than 500 characters")
    if len(seo.hashtags) > HASHTAGS_MAX:
        problems.append(f"{len(seo.hashtags)} hashtags")
    letters = [c for c in seo.title if c.isalpha()]
    if len(letters) > 10 and sum(c.isupper() for c in letters) / len(letters) > 0.7:
        problems.append("title is mostly capitals")
    counts = Counter(w.lower() for w in _WORD.findall(seo.description))
    stuffed = [w for w, n in counts.items() if n > 6]
    if stuffed:
        problems.append(f"repeated words: {', '.join(stuffed)}")
    return PolicyCheck(passed=not problems, detail="; ".join(problems))


def check_real_entities(seo: Seo) -> PolicyCheck:
    """Links and @handles in the model-written metadata; the owner's guests and games may be named."""
    text = "\n".join(metadata_texts(seo))
    hits = [m.group(0).strip() for m in _HANDLE_OR_URL.finditer(text)]
    return PolicyCheck(passed=not hits, detail=f"found: {', '.join(hits)}" if hits else "")


def check_metadata(seo: Seo) -> PolicyResult:
    """The three metadata-only checks as a `policy` record with stage "text"."""
    checks = {
        "misleading_claims": check_misleading_claims(seo),
        "metadata_spam": check_metadata_spam(seo),
        "real_entities": check_real_entities(seo),
    }
    return PolicyResult(stage="text", passed=all(c.passed for c in checks.values()), checks=checks)


# ---------- normalisation ----------
def _clean_hashtag(raw: str) -> str:
    body = re.sub(r"[^0-9A-Za-z_]", "", raw.lstrip("#"))
    return f"#{body}" if body else ""


def normalize_hashtags(raw: list[Any], fallback_words: list[str]) -> list[str]:
    """3 to 5 unique hashtags, `#Shorts` always first."""
    out = ["#Shorts"]
    for item in [*raw, *[f"#{w}" for w in fallback_words]]:
        tag = _clean_hashtag(str(item))
        if tag and tag.lower() not in {t.lower() for t in out}:
            out.append(tag)
        if len(out) >= HASHTAGS_MAX:
            break
    for filler in ("#clip", "#video"):
        if len(out) < HASHTAGS_MIN:
            out.append(filler)
    return out[:HASHTAGS_MAX]


def normalize_tags(raw: list[Any], extra: list[str]) -> list[str]:
    """Unique tags, 10 to 20 when enough material exists, at most 500 characters in total."""
    out: list[str] = []
    seen: set[str] = set()
    for item in [*raw, *extra]:
        tag = re.sub(r"\s+", " ", str(item)).strip().lstrip("#")
        if not tag or len(tag) > 30 or tag.lower() in seen:
            continue
        if tags_length([*out, tag]) > TAGS_CHARS or len(out) >= TAGS_MAX:
            break
        out.append(tag)
        seen.add(tag.lower())
        if len(out) >= TAGS_MAX:
            break
    return out


def shorten_title(title: str, limit: int = TITLE_TARGET) -> str:
    """Cut at a word boundary at or below `limit` characters."""
    t = re.sub(r"\s+", " ", title).strip()
    if len(t) <= limit:
        return t
    cut = t[:limit].rsplit(" ", 1)[0] if " " in t[:limit] else t[:limit]
    return cut.rstrip(" ,;:-–—.")


def finalize_seo(
    raw: dict[str, Any], *, clip: ClipPlanItem, source_title: str, seed_keywords: list[str],
    creator_note: str = "",
) -> Seo:
    """Normalise an LLM answer to the section 9.2 Shorts rules and validate it as `Seo`."""
    title = shorten_title(str(raw.get("title") or clip.title)) or shorten_title(clip.title) or "Clip"
    kws = [str(k) for k in raw.get("keywords", []) if isinstance(k, str)] or list(clip.keywords)
    if kws and kws[0].lower() not in title.lower()[:40]:
        prefixed = f"{kws[0].strip().title()}: {title}"
        if len(prefixed) <= TITLE_TARGET:
            title = prefixed
    lines = [ln.strip() for ln in str(raw.get("description") or "").splitlines() if ln.strip()][:2]
    if not lines:
        lines = [clip.why or clip.title]
    description = "\n".join([*lines, "", f"{SOURCE_LINE} {source_title}"])
    if creator_note.strip():
        description += "\n" + creator_note.strip()
    words = re.findall(r"[A-Za-z]{4,}", f"{title} {' '.join(kws)}")
    hashtags = normalize_hashtags(list(raw.get("hashtags", [])), words)
    extras = [*kws, *clip.keywords, *seed_keywords, *[w.lower() for w in words]]
    tags = normalize_tags(list(raw.get("tags", [])), extras)
    return Seo(
        title=title[:100],
        description=description,
        tags=tags or [clip.title[:30]],
        hashtags=hashtags,
        keywords=kws[:8],
    )


def generate_seo(
    ctx: JobContext,
    clip: ClipPlanItem,
    *,
    excerpt: str,
    source_title: str,
    creator_note: str = "",
) -> tuple[Seo, PolicyResult]:
    """Ask `clip_seo` (retry once when a policy check fails) and return the metadata and its check record."""
    last: PolicyResult | None = None
    seo: Seo | None = None
    for _ in range(2):
        raw = call_json(
            ctx,
            "clip_seo",
            source_title=source_title,
            primary_keyword=(clip.keywords[0] if clip.keywords else clip.title.split()[0]),
            clip_title=clip.title,
            clip_text=excerpt,
        )
        seo = finalize_seo(
            raw, clip=clip, source_title=source_title, seed_keywords=ctx.channel.seed_keywords,
            creator_note=creator_note,
        )
        last = check_metadata(seo)
        if last.passed:
            return seo, last
        checks = last.model_dump(mode="json")
        ctx.log.warn("clip_seo_policy_retry", job_id=ctx.job_id, k=clip.k, checks=checks)
    assert last is not None and seo is not None
    failed = next(name for name, c in last.checks.items() if not c.passed)
    raise NsError(CHECK_CODES[failed], f"clip c{clip.k}: {last.checks[failed].detail}")
