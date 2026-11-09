"""Text-gate checks (spec section 18.8): run after the visual plan, before any voice is rendered."""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from typing import Any

from ns_common.embed_client import cosine, embed_window, from_blob
from ns_common.errors import NsError
from ns_common.llm_client import FakeLlm
from ns_common.models import PolicyCheck
from ns_common.paths import Paths
from ns_studio.context import JobContext
from ns_studio.policy.base import Check, fail, ok
from ns_studio.policy.judge import JudgeFn, default_judge, record_notes, verdict_failed
from ns_studio.policy.meta import (
    collect_items,
    policy_list,
    short_moments,
    story_text,
    unsafe_categories,
)

Entity = tuple[str, str]
NerFn = Callable[[str], list[Entity]]

SAME_CHANNEL_SIMILARITY = 0.85
OTHER_CHANNEL_SIMILARITY = 0.80
MAX_TAGS = 20
MAX_TAG_CHARS = 500
MAX_HASHTAGS = 5
SPACY_LABELS = frozenset({"PERSON", "ORG", "GPE", "EVENT"})
PLACE_LABELS = frozenset({"GPE", "LOC", "FAC"})  # countries and cities, other places, buildings and roads
_CAP_SEQ = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b")
_NLP_CACHE: dict[str, Any] = {}


# ---------------------------------------------------------------- entity extraction
def _spacy_nlp(paths: Paths, model: str = "en_core_web_md") -> Any | None:
    """Load spaCy once (lazy). Returns None when spaCy or the model is unavailable."""
    if "nlp" in _NLP_CACHE:
        return _NLP_CACHE["nlp"]
    nlp: Any | None = None
    try:
        spacy = importlib.import_module("spacy")
        local = paths.models_spacy / model
        nlp = spacy.load(str(local) if local.exists() else model)
    except (ImportError, OSError):
        nlp = None
    _NLP_CACHE["nlp"] = nlp
    return nlp


def regex_entities(text: str, first_names: frozenset[str]) -> list[Entity]:
    """Fallback NER: capitalised multi-word sequences that start with a known first name are PERSON."""
    out: list[Entity] = []
    for m in _CAP_SEQ.finditer(text):
        words = m.group(0).split()
        if words[0].lower() in first_names:
            out.append((m.group(0), "PERSON"))
    return out


def make_ner(paths: Paths) -> NerFn:
    """spaCy NER when installed, else the regex fallback over the policy allow-list of first names."""
    allow = frozenset(policy_list("real_entities_allow.txt"))

    def ner(text: str) -> list[Entity]:
        nlp = _spacy_nlp(paths)
        if nlp is None:
            return regex_entities(text, allow)
        return [(e.text, e.label_) for e in nlp(text).ents if e.label_ in SPACY_LABELS]

    return ner


def place_entities(paths: Paths, text: str) -> list[Entity]:
    """Places spaCy finds in `text` (GPE, LOC, FAC); empty when spaCy is not installed.

    The disclosure advice (spec 18.7) treats any of them as a possible real setting. Without spaCy the
    classifier's `real_setting` answer is the only signal, and an unclear answer already means "yes".
    """
    nlp = _spacy_nlp(paths)
    if nlp is None:
        return []
    return [(e.text, e.label_) for e in nlp(text).ents if e.label_ in PLACE_LABELS]


def entity_problems(
    entities: list[Entity], text: str, block: tuple[str, ...], allow: tuple[str, ...]
) -> list[str]:
    """Reasons the recognised entities (plus a raw block-list scan of `text`) break spec 18.8."""
    problems: list[str] = []
    lowered = text.lower()
    for phrase in block:
        if re.search(rf"\b{re.escape(phrase)}\b", lowered):
            problems.append(f"blocked name {phrase!r}")
    for ent, label in entities:
        key = ent.lower().strip()
        if key in block:
            note = f"blocked name {key!r}"
            if note not in problems:
                problems.append(f"blocked {label.lower()} {ent!r}")
        elif label == "PERSON" and len(key.split()) > 1 and key not in allow:
            problems.append(f"full person name {ent!r}")
    return problems


# ---------------------------------------------------------------- checks
class UnsafeTopics(Check):
    """LLM classifier against `contracts/policy/unsafe_topics.yaml` (E601)."""

    name = "unsafe_topics"
    gate = "text"

    def __init__(self, judge: JudgeFn | None = None) -> None:
        self.judge = judge or default_judge

    def run(self, ctx: JobContext) -> PolicyCheck:
        """Fail when the judge flags any section 18.3 category; records the intensity note."""
        answer = self.judge(
            ctx,
            "unsafe_topics",
            story=story_text(ctx),
            categories=list(unsafe_categories()),
            audience=ctx.channel.audience,
        )
        notes = record_notes(ctx, answer, merge=False)
        if verdict_failed(answer):
            cats = ", ".join(str(f.get("category", "?")) for f in answer.get("flags", [])) or "unspecified"
            return fail(f"flagged: {cats}; {answer.get('reason', '')}".strip())
        return ok(f"intensity={notes['intensity']}")


def check_added_text(
    ctx: JobContext, text: str, *, judge: JudgeFn | None = None, ner: NerFn | None = None
) -> None:
    """Run the text-gate checks on text that was added after `policy_text` ran; raises its NS-E6xx.

    The duration check lengthens a chapter after the text gate has passed. The new words get the checks that
    read story text: real people and brands (E602), claims that the story is real (E603) and the unsafe-topic
    classifier (E601, which also raises the intensity and setting notes the disclosure advice reads).
    """
    problems = entity_problems(
        (ner or make_ner(ctx.paths))(text),
        text,
        policy_list("real_entities_block.txt"),
        policy_list("real_entities_allow.txt"),
    )
    if problems:
        raise NsError("NS-E602", "real_entities: " + "; ".join(problems), check="real_entities")
    lowered = text.lower()
    claims = [p for p in policy_list("misleading_claims.txt") if re.search(rf"\b{re.escape(p)}\b", lowered)]
    if claims:
        said = ", ".join(repr(c) for c in claims)
        raise NsError("NS-E603", f"misleading_claims: {said}", check="misleading_claims")
    answer = (judge or default_judge)(
        ctx, "unsafe_topics", story=text, categories=list(unsafe_categories()), audience=ctx.channel.audience
    )
    record_notes(ctx, answer, merge=True)
    if verdict_failed(answer):
        cats = ", ".join(str(f.get("category", "?")) for f in answer.get("flags", [])) or "unspecified"
        why = f"unsafe_topics: flagged: {cats}; {answer.get('reason', '')}".strip()
        raise NsError("NS-E601", why, check="unsafe_topics")


class RealEntities(Check):
    """Real people, brands, places or events in the story or metadata (E602)."""

    name = "real_entities"
    gate = "text"

    def __init__(self, ner: NerFn | None = None) -> None:
        self.ner = ner

    def run(self, ctx: JobContext) -> PolicyCheck:
        """Fail on block-list hits or multi-token PERSON entities missing from the allow-list."""
        ner = self.ner or make_ner(ctx.paths)
        text = "\n".join([story_text(ctx), *(t for it in collect_items(ctx) for t in it.texts())])
        problems = entity_problems(
            ner(text),
            text,
            policy_list("real_entities_block.txt"),
            policy_list("real_entities_allow.txt"),
        )
        return fail("; ".join(problems)) if problems else ok("no real entities found")


class MisleadingClaims(Check):
    """Phrases such as 'true story' in titles, descriptions, tags or hooks (E603)."""

    name = "misleading_claims"
    gate = "text"

    def run(self, ctx: JobContext) -> PolicyCheck:
        """Fail when any `misleading_claims.txt` phrase appears in the metadata of any item."""
        phrases = policy_list("misleading_claims.txt")
        hits: list[str] = []
        for item in collect_items(ctx):
            blob = "\n".join(item.texts()).lower()
            for p in phrases:
                if re.search(rf"\b{re.escape(p)}\b", blob):
                    hits.append(f"{item.name}: {p!r}")
        return fail("; ".join(hits)) if hits else ok("no misleading claims")


class HookTitleHonesty(Check):
    """The judge compares hooks and titles with what the story delivers (E604)."""

    name = "hook_title_honesty"
    gate = "text"

    def __init__(self, judge: JudgeFn | None = None) -> None:
        self.judge = judge or default_judge

    def run(self, ctx: JobContext) -> PolicyCheck:
        """Fail when the judge says a hook or title promises something the story does not deliver."""
        items = [{"item": i.name, "title": i.title, "hook": i.hook} for i in collect_items(ctx)]
        if not items:
            return ok("no hooks or titles yet")
        answer = self.judge(ctx, "hook_title_honesty", story=story_text(ctx), items=items)
        if verdict_failed(answer):
            failed = ", ".join(str(x) for x in answer.get("failed_items", [])) or "unspecified"
            return fail(f"dishonest: {failed}; {answer.get('reason', '')}".strip())
        return ok("hooks and titles are honest")


class SelfSimilarity(Check):
    """Embedding similarity to earlier stories: > 0.85 same channel, > 0.80 other channel (E605).

    Only a story whose video was made (the job has an `outputs` row the owner has not rejected) is in the
    comparison set: a job that stopped at the voice stage left its story row behind but produced nothing to
    repeat, and a rejected video was moved out of the outbox so the story may be made again (D-25).
    """

    name = "self_similarity"
    gate = "text"

    def __init__(self, *, skip_when_fake: bool = True) -> None:
        self.skip_when_fake = skip_when_fake
        self._match = ""  # job id of the finished video the last failing run was too close to

    def hint(self, ctx: JobContext) -> str | None:
        """For the owner's own script no idea is drawn: say what the owner can do instead."""
        if not (ctx.owner_script and self._match):
            return None
        return (
            f"This script is too close to the finished video of job {self._match}. "
            "Change the script, or use that video instead of making a second one."
        )

    def run(self, ctx: JobContext) -> PolicyCheck:
        """Compare the story embedding with the finished stories (this job excluded)."""
        self._match = ""
        if self.skip_when_fake and isinstance(ctx.services.llm, FakeLlm):
            return ok("skipped: fake LLM serves one fixture story to every job")
        text = story_text(ctx)
        if not text.strip():
            return ok("no story text")
        vec = ctx.services.embed.embed([embed_window(text, ctx.runtime.embed.batch_size)])[0]
        return self.compare(ctx, vec)

    def compare(self, ctx: JobContext, vec: list[float]) -> PolicyCheck:
        """Compare the embedding `vec` of this job's story with the stories of finished videos."""
        rows = ctx.db.query(
            "select s.job_id, s.channel, s.embedding from stories s where s.job_id != ? "
            "and s.embedding is not null and exists "
            "(select 1 from outputs o where o.job_id = s.job_id and o.status != 'rejected')",
            (ctx.job_id,),
        )
        worst: tuple[float, str] = (0.0, "")
        for r in rows:
            other = from_blob(r["embedding"])
            if len(other) != len(vec):
                continue
            sim = cosine(vec, other)
            limit = SAME_CHANNEL_SIMILARITY if r["channel"] == ctx.channel.slug else OTHER_CHANNEL_SIMILARITY
            if sim > limit and sim > worst[0]:
                worst = (sim, f"{r['job_id']} ({r['channel']}) at {sim:.2f} > {limit:.2f}")
                self._match = str(r["job_id"])
        if not worst[1]:
            return ok(f"compared with {len(rows)} stories")
        if ctx.owner_script:
            return fail(f"this script is too close to the finished video of {worst[1]}")
        return fail(f"too similar to {worst[1]}")


def recheck_ledger(ctx: JobContext, check: SelfSimilarity) -> None:
    """Meet the repeat ledger again when a job is resumed; raises the same NS-E605 the text gate raises.

    `run_stage` skips `policy_text` on a resume (its inputs are unchanged), but the ledger is not one of its
    inputs: another job with the same story may have finished since. Without this, job A (failed at voice)
    and job B (same script, finished meanwhile) would both end as a video of one story (D-25 review). Needs no
    embed service: it compares the embedding the job stored with its story row. A job with no stored
    embedding, or a fake LLM when `check` skips for one, has nothing to compare.
    """
    if check.skip_when_fake and isinstance(ctx.services.llm, FakeLlm):
        return
    own = ctx.db.query(
        "select embedding from stories where job_id=? and embedding is not null", (ctx.job_id,)
    )
    if not own:
        return
    check._match = ""
    res = check.compare(ctx, from_blob(own[0]["embedding"]))
    if res.passed or not check.code:
        return
    hint = check.hint(ctx)
    extra = {"hint": hint} if hint else {}
    raise NsError(check.code, f"{check.name}: {res.detail}", check=check.name, **extra)


class MetadataSpam(Check):
    """Tag count and length limits plus a relevance judgement (E606)."""

    name = "metadata_spam"
    gate = "text"

    def __init__(self, judge: JudgeFn | None = None) -> None:
        self.judge = judge or default_judge

    def run(self, ctx: JobContext) -> PolicyCheck:
        """Deterministic limits first; then ask the judge whether the tags are relevant."""
        items = collect_items(ctx)
        problems: list[str] = []
        for it in items:
            lowered = [t.lower() for t in it.tags]
            if len(it.tags) > MAX_TAGS:
                problems.append(f"{it.name}: {len(it.tags)} tags (max {MAX_TAGS})")
            if sum(len(t) for t in it.tags) > MAX_TAG_CHARS:
                problems.append(f"{it.name}: tags exceed {MAX_TAG_CHARS} characters")
            if len(set(lowered)) != len(lowered):
                problems.append(f"{it.name}: duplicate tags")
            if len(it.hashtags) > MAX_HASHTAGS:
                problems.append(f"{it.name}: {len(it.hashtags)} hashtags (max {MAX_HASHTAGS})")
            if it.name == "long" and any(h.lower() == "#shorts" for h in it.hashtags):
                problems.append("long: #Shorts hashtag on a long-form video")
        if problems:
            return fail("; ".join(problems))
        if not items:
            return ok("no metadata yet")
        answer = self.judge(
            ctx,
            "tag_relevance",
            story=story_text(ctx),
            items=[{"item": i.name, "title": i.title, "tags": i.tags} for i in items],
        )
        if verdict_failed(answer):
            return fail(f"irrelevant tags: {answer.get('reason', '')}".strip())
        return ok("tags within limits and relevant")


class StandaloneShort(Check):
    """Each selected Short moment must make sense without the master (E607)."""

    name = "standalone_short"
    gate = "text"

    def __init__(self, judge: JudgeFn | None = None) -> None:
        self.judge = judge or default_judge

    def run(self, ctx: JobContext) -> PolicyCheck:
        """Ask the judge about every moment in `shorts_plan.json`."""
        moments = short_moments(ctx)
        if not moments:
            return ok("no derived Shorts")
        answer = self.judge(ctx, "standalone_short", story=story_text(ctx), moments=moments)
        if verdict_failed(answer):
            failed = ", ".join(str(x) for x in answer.get("failed_items", [])) or "unspecified"
            return fail(f"not standalone: {failed}; {answer.get('reason', '')}".strip())
        return ok(f"{len(moments)} moments stand alone")


def text_checks(judge: JudgeFn | None = None, ner: NerFn | None = None) -> list[Check]:
    """The ordered text-gate check list."""
    return [
        UnsafeTopics(judge),
        RealEntities(ner),
        MisleadingClaims(),
        HookTitleHonesty(judge),
        SelfSimilarity(),
        MetadataSpam(judge),
        StandaloneShort(judge),
    ]
