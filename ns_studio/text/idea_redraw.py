"""Idea redraw after a critic failure (spec 16.2, FEATURE_STATUS F-H57 / F-H99).

A story that misses the critic's bar after two revisions is discarded and a new idea is drawn; a job tries at
most `MAX_IDEAS` ideas, then ends with NS-E304. The failed premises are kept in `ideas_failed.json` in the job
folder, so the count survives `--resume` and a restart. `policy.regen.run_job_guarded` records a failure and
restarts the plan at `ideas`; `IdeasStage` then asks `next_unused_idea` before it calls the model again.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from ns_common.embed_client import cosine, from_blob, to_blob
from ns_common.models import Idea
from ns_studio.context import JobContext
from ns_studio.text.prompts import parse_model

FAILED_FILE = "ideas_failed.json"
LAST_FAILED_FILE = "idea_last_failed.json"  # digest of the idea recorded last (a resume records it once)
MAX_IDEAS = 3
#: Stages whose NS-E304 means "the story missed the critic's bar" (the `ideas` stage raises NS-E304 too, for
#: ideas that are all near-duplicates of history; that is no reason to draw another).
CRITIC_STAGES: tuple[str, ...] = ("critic_revise", "chapters_critic")


def failed_premises(ctx: JobContext) -> list[str]:
    """Premises of the ideas of this job whose story failed the critic, oldest first."""
    f = ctx.file(FAILED_FILE)
    if not f.exists():
        return []
    try:
        data = ctx.read_json(FAILED_FILE)
    except ValueError:
        return []
    return [str(p) for p in data] if isinstance(data, list) else []


def record_failed_idea(ctx: JobContext) -> int:
    """Add the idea of `idea.json` to the failed list and return how many ideas have failed.

    Every idea that failed grows the list by one, so `MAX_IDEAS` is a hard bound whatever the idea looks like:
    the entry is the premise, or `<premise> (idea N)` when that premise failed before, or
    `(idea N without a premise)` when `idea.json` has none or cannot be read. (`next_unused_idea` skips the
    premises in the list.) The same idea failing again without a new one drawn in between (a resume of a job
    that already ended) is counted once: `idea_last_failed.json` keeps the digest of the last one recorded.
    """
    failed = failed_premises(ctx)
    try:
        raw = ctx.file("idea.json").read_bytes()
    except OSError:
        raw = b""
    digest = hashlib.sha256(raw).hexdigest()[:16]
    if failed and _last_failed_digest(ctx) == digest:
        return len(failed)
    n = len(failed) + 1
    try:
        premise = str(json.loads(raw)["premise"])
    except (ValueError, KeyError, TypeError):
        premise = ""
    if not premise.strip():
        entry = f"(idea {n} without a premise)"
    elif premise in failed:
        entry = f"{premise} (idea {n})"
    else:
        entry = premise
    failed.append(entry)
    ctx.write_json(FAILED_FILE, failed)
    ctx.write_json(LAST_FAILED_FILE, {"digest": digest})
    return len(failed)


def _last_failed_digest(ctx: JobContext) -> str:
    try:
        return str(ctx.read_json(LAST_FAILED_FILE)["digest"])
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def next_unused_idea(ctx: JobContext) -> dict[str, Any] | None:
    """The next premise of `ideas_all.json` that did not fail and is no repeat of history, or None.

    Only used after a critic failure. The idea passes the same unsafe-topic check as a drawn one, is written
    to `idea.json` and recorded in the `ideas` table like a freshly drawn one. `ideas_all.json` stays as it
    is, so the next redraw sees the rest of the batch. None means the batch is used up (or the job
    had a banked idea): the stage draws as it always does.
    """
    from ns_studio.policy import idea_check
    from ns_studio.text.stages_shorts import IDEA_SIMILARITY_LIMIT, now_iso, pick_idea

    failed = failed_premises(ctx)
    if not failed or not ctx.file("ideas_all.json").exists():
        return None
    try:
        batch = ctx.read_json("ideas_all.json")
        candidates = [parse_model(Idea, i, "idea").model_dump() for i in batch if i["premise"] not in failed]
    except (ValueError, KeyError, TypeError):
        return None
    if not candidates:
        return None
    rows = ctx.db.query(
        "select premise, embedding from ideas where channel=? order by idea_id desc limit 50",
        (ctx.channel.slug,),
    )
    past = [from_blob(r["embedding"]) for r in rows if r["embedding"]]
    vecs = ctx.services.embed.embed([c["premise"] for c in candidates])
    remaining = [
        (idea, vec)
        for idea, vec in zip(candidates, vecs, strict=True)
        if not past or max(cosine(vec, p) for p in past) < IDEA_SIMILARITY_LIMIT
    ]
    while remaining:
        # the same pick as a first draw: a master takes an idea with chapter potential while one is left
        idea, vec = pick_idea(ctx, remaining)
        remaining = [s for s in remaining if s[0] is not idea]
        why = idea_check.idea_problem(ctx, idea)  # the unsafe-topic gate (18.3) sees every idea that is used
        if why is not None:
            ctx.log.warn("idea_unsafe", job_id=ctx.job_id, premise=idea["premise"], detail=why)
            continue
        ctx.db.execute(
            "insert into ideas(channel, job_id, premise, twist, embedding, created_at, used) "
            "values (?,?,?,?,?,?,1)",
            (ctx.channel.slug, ctx.job_id, idea["premise"], idea["twist"], to_blob(vec), now_iso()),
        )
        ctx.write_json("idea.json", idea)
        ctx.log.info("idea_redrawn", job_id=ctx.job_id, tried=len(failed) + 1)
        return idea
    return None
