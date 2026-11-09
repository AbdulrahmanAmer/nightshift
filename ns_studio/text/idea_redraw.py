"""Idea redraw after a critic failure (spec 16.2, FEATURE_STATUS F-H57 / F-H99).

A story that misses the critic's bar after two revisions is discarded and a new idea is drawn; a job tries at
most `MAX_IDEAS` ideas, then ends with NS-E304. The failed premises are kept in `ideas_failed.json` in the job
folder, so the count survives `--resume` and a restart. `policy.regen.run_job_guarded` records a failure and
restarts the plan at `ideas`; `IdeasStage` then asks `next_unused_idea` before it calls the model again.
"""

from __future__ import annotations

from typing import Any

from ns_common.embed_client import cosine, from_blob, to_blob
from ns_common.models import Idea
from ns_studio.context import JobContext
from ns_studio.text.prompts import parse_model

FAILED_FILE = "ideas_failed.json"
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
    """Add the premise of `idea.json` to the failed list (once) and return how many ideas have failed."""
    failed = failed_premises(ctx)
    try:
        premise = str(ctx.read_json("idea.json")["premise"])
    except (OSError, ValueError, KeyError, TypeError):
        premise = ""
    if premise and premise not in failed:
        failed.append(premise)
        ctx.write_json(FAILED_FILE, failed)
    return len(failed)


def next_unused_idea(ctx: JobContext) -> dict[str, Any] | None:
    """The next premise of `ideas_all.json` that did not fail and is no repeat of history, or None.

    Only used after a critic failure. The idea is written to `idea.json` and recorded in the `ideas` table
    like a freshly drawn one. `ideas_all.json` stays as it is, so the next redraw sees the rest of the
    batch. None means the batch is used up (or the job had a banked idea): the stage draws as it always does.
    """
    from ns_studio.text.stages_shorts import IDEA_SIMILARITY_LIMIT, now_iso

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
    for idea, vec in zip(candidates, vecs, strict=True):
        if past and max(cosine(vec, p) for p in past) >= IDEA_SIMILARITY_LIMIT:
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
