"""Shorts-only text stages: ideas to visual plan (component C5; spec 7.3, 16.2, 16.3, 9.2)."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from typing import Any

from ns_common.devmode import is_dev
from ns_common.embed_client import cosine, embed_window, from_blob, to_blob
from ns_common.errors import NsError
from ns_common.models import Idea, Part, Seo, VisualBeat
from ns_common.textstats import est_seconds, split_sentences, word_count
from ns_studio.context import JobContext
from ns_studio.stages import Tenant, register
from ns_studio.text.budget import Budget, MasterBudget, master_as_budget, master_budget, short_budget
from ns_studio.text.prompts import RUBRIC_KEYS, call_json, call_text, parse_model
from ns_studio.text.seo import finalize_seo
from ns_studio.text.validators import load_banned, raise_if, validate_story

__all__ = ["draw_banked_idea", "finalize_seo", "ideas_budget", "job_master_budget", "now_iso"]

IDEA_SIMILARITY_LIMIT = 0.85


def job_master_budget(ctx: JobContext) -> MasterBudget:
    """Master budget of this job; the target minutes are drawn from `long_minutes` seeded by the job id."""
    return master_budget(ctx.channel.long_minutes, ctx.combo.pacing, rng=random.Random(ctx.job_id))


def ideas_budget(ctx: JobContext) -> Budget:
    """Word budget of the story: the master (master-first) or one part of `combo.length_bucket` (50 s)."""
    if ctx.fmt == "master_first":
        return master_as_budget(job_master_budget(ctx))
    return short_budget(ctx.combo.length_bucket or 50, ctx.combo.pacing, parts=1)


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string (for DB rows)."""
    return datetime.now(UTC).isoformat()


def critic_numbers(critique: dict[str, Any]) -> tuple[float, float]:
    """Average and minimum of the seven rubric scores of a critic answer."""
    try:
        scores = [float(critique["scores"][k]) for k in RUBRIC_KEYS]
    except (KeyError, TypeError, ValueError) as e:
        raise NsError("NS-E302", f"critic answer lacks rubric scores: {e}") from e
    return sum(scores) / len(scores), min(scores)


def draw_banked_idea(ctx: JobContext) -> dict[str, Any] | None:
    """Claim the oldest owner-approved idea of the channel (idea bank, spec 20.2) or return None.

    The row becomes `status='used'`, `used=1` and carries this job id. Columns the shared schema does not
    have yet (`setting`, `protagonist`, `keywords`, `updated_at`; CR-C9-6) are read and written only when the
    database has them. A row claimed by another run in the meantime counts as not found.
    """
    row = ctx.db.one(
        "select * from ideas where channel=? and status='approved' order by created_at, idea_id limit 1",
        (ctx.channel.slug,),
    )
    if row is None:
        return None
    cols = set(row.keys())

    def text(name: str) -> str:
        return str(row[name] or "").strip() if name in cols else ""

    keywords: list[str] = []
    if text("keywords"):
        try:
            loaded = json.loads(text("keywords"))
        except json.JSONDecodeError:
            loaded = []
        if isinstance(loaded, list):
            keywords = [str(k).strip() for k in loaded if str(k).strip()][:8]
    if not keywords:
        keywords = list(ctx.channel.seed_keywords[:3]) or ["story"]
    idea = parse_model(
        Idea,
        {
            "premise": row["premise"],
            "twist": row["twist"],
            "setting": text("setting"),
            "protagonist": text("protagonist"),
            "keywords": keywords,
        },
        "approved idea",
    ).model_dump()
    blob = row["embedding"] or to_blob(ctx.services.embed.embed([idea["premise"]])[0])
    sets, params = "status='used', used=1, job_id=?, embedding=?", [ctx.job_id, blob]
    if "updated_at" in cols:
        sets, params = sets + ", updated_at=?", [*params, now_iso()]
    claimed = ctx.db.execute(
        f"update ideas set {sets} where idea_id=? and status='approved'", (*params, row["idea_id"])
    )
    if claimed.rowcount != 1:
        return None
    ctx.log.info("idea_from_bank", job_id=ctx.job_id, idea_id=row["idea_id"], channel=ctx.channel.slug)
    return idea


class IdeasStage:
    """M1 / shorts 1: an approved idea from the bank, else 8 premises minus near-duplicates of history."""

    name: str = "ideas"
    tenant: Tenant = "llama"
    uses_embed: bool = True  # calls the embed service; the orchestrator leases it around run()
    inputs: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ("idea.json", "ideas_all.json")

    def run(self, ctx: JobContext) -> None:
        """Use the owner's oldest approved idea if there is one; else draw 8 premises and keep a survivor.

        Writes `ideas_all.json` and the chosen `idea.json`; an invented idea is recorded in the `ideas` table.
        """
        banked = draw_banked_idea(ctx)
        if banked is not None:
            ctx.write_json("ideas_all.json", [banked])
            ctx.write_json("idea.json", banked)
            return
        rows = ctx.db.query(
            "select premise, embedding from ideas where channel=? order by idea_id desc limit 50",
            (ctx.channel.slug,),
        )
        out = call_json(
            ctx, "ideas", n=8, budget=ideas_budget(ctx), avoid_premises=[r["premise"] for r in rows]
        )
        ideas = [parse_model(Idea, i, "idea").model_dump() for i in out["ideas"]]
        ctx.write_json("ideas_all.json", ideas)
        vecs = ctx.services.embed.embed([i["premise"] for i in ideas])
        past = [from_blob(r["embedding"]) for r in rows if r["embedding"]]
        survivors = [
            (i, v)
            for i, v in zip(ideas, vecs, strict=True)
            if not past or max(cosine(v, p) for p in past) < IDEA_SIMILARITY_LIMIT
        ]
        if not survivors:
            raise NsError("NS-E304", f"all {len(ideas)} ideas too similar to history")
        idea, vec = survivors[0]
        ctx.db.execute(
            "insert into ideas(channel, job_id, premise, twist, embedding, created_at, used) "
            "values (?,?,?,?,?,?,1)",
            (ctx.channel.slug, ctx.job_id, idea["premise"], idea["twist"], to_blob(vec), now_iso()),
        )
        ctx.write_json("idea.json", idea)


class PlanStage:
    """Shorts 2: beat sheet from the chosen idea."""

    name: str = "plan"
    tenant: Tenant = "llama"
    inputs: tuple[str, ...] = ("idea.json",)
    outputs: tuple[str, ...] = ("plan.json",)

    def run(self, ctx: JobContext) -> None:
        """Write `plan.json` (beats, part_breaks, ending_line_idea)."""
        plan = call_json(ctx, "story_plan", idea=ctx.read_json("idea.json"), budget=ideas_budget(ctx))
        ctx.write_json("plan.json", plan)


class DraftStage:
    """Shorts 3: full story text from the beat sheet."""

    name: str = "draft"
    tenant: Tenant = "llama"
    inputs: tuple[str, ...] = ("plan.json",)
    outputs: tuple[str, ...] = ("story.json",)

    def run(self, ctx: JobContext) -> None:
        """Write `story.json` = {text, word_count, revisions: 0}."""
        plan = ctx.read_json("plan.json")
        text = call_text(ctx, "story_draft", plan=plan, budget=ideas_budget(ctx)).strip()
        ctx.write_json("story.json", {"text": text, "word_count": word_count(text), "revisions": 0})


class CriticReviseStage:
    """Shorts 4: critic loop (at most 2 revisions), hard validators, `stories` row."""

    name: str = "critic_revise"
    tenant: Tenant = "llama"
    uses_embed: bool = True  # calls the embed service; the orchestrator leases it around run()
    inputs: tuple[str, ...] = ("story.json",)
    outputs: tuple[str, ...] = ("story.json", "critic.json")

    def run(self, ctx: JobContext) -> None:
        """Critique, revise up to twice, validate (NS-E303) and persist; NS-E304 if the bar is not met."""
        story = ctx.read_json("story.json")
        if story.get("critic_avg") is not None and ctx.file("critic.json").exists():
            return  # already critiqued: story.json is its own output
        text: str = story["text"]
        bar = ctx.channel.critic_bar
        revisions = 0
        critique: dict[str, Any] = {}
        for attempt in range(3):
            critique = call_json(ctx, "critic", story=text, banned=load_banned())
            avg, low = critic_numbers(critique)
            ctx.log.info("critic", avg=avg, low=low, attempt=attempt, job_id=ctx.job_id)
            if avg >= bar and low >= 6:
                break
            if attempt == 2:
                raise NsError("NS-E304", f"critic avg {avg:.1f} low {low:.0f} after {revisions} revisions")
            text = call_text(ctx, "revise", story=text, fixes=critique["fixes"]).strip()
            revisions += 1
        past = [
            r["opening_line"]
            for r in ctx.db.query(
                "select opening_line from stories where channel=? and job_id!=? "
                "order by created_at desc limit 50",
                (ctx.channel.slug, ctx.job_id),
            )
        ]
        if is_dev(ctx.paths):
            past = []      # the fake LLM tells the same story every run; dev mode must not fail on that
        raise_if(validate_story(text, ideas_budget(ctx), past_openings=past, embed=ctx.services.embed))
        avg, _ = critic_numbers(critique)
        vec = ctx.services.embed.embed([embed_window(text, ctx.runtime.embed.batch_size)])[0]
        ctx.db.execute(
            "insert or replace into stories(job_id, channel, format, working_title, text, word_count, "
            "opening_line, embedding, critic_avg, revisions, created_at) values (?,?,?,?,?,?,?,?,?,?,?)",
            (
                ctx.job_id,
                ctx.channel.slug,
                ctx.fmt,
                None,
                text,
                word_count(text),
                split_sentences(text)[0],
                to_blob(vec),
                avg,
                revisions,
                now_iso(),
            ),
        )
        ctx.write_json("critic.json", critique)
        ctx.write_json(
            "story.json",
            {"text": text, "word_count": word_count(text), "revisions": revisions, "critic_avg": avg},
        )


class SplitStage:
    """Shorts 5: cut the story into parts; counts and durations are recomputed locally."""

    name: str = "split"
    tenant: Tenant = "llama"
    inputs: tuple[str, ...] = ("story.json",)
    outputs: tuple[str, ...] = ("parts.json",)

    def run(self, ctx: JobContext) -> None:
        """Write `parts.json` (list of `Part`); the last part is never a cliffhanger."""
        budget = ideas_budget(ctx)
        out = call_json(ctx, "split", story=ctx.read_json("story.json")["text"], budget=budget)
        parts: list[dict[str, Any]] = []
        for i, p in enumerate(out["parts"], start=1):
            text = str(p["text"]).strip()
            part = Part(
                n=i,
                text=text,
                hook=p.get("hook") or split_sentences(text)[0],
                word_count=max(1, word_count(text)),
                est_seconds=max(0.01, est_seconds(text, budget.wpm)),
                cliffhanger=bool(p.get("cliffhanger", False)),
            )
            parts.append(part.model_dump())
        parts[-1]["cliffhanger"] = False
        ctx.file("hooks_all.json").unlink(missing_ok=True)  # parts changed: hooks must be redone
        ctx.write_json("parts.json", parts)


class HooksStage:
    """Shorts 6: five hooks per part, a judge picks one; all candidates go to `hooks_all.json`."""

    name: str = "hooks"
    tenant: Tenant = "llama"
    inputs: tuple[str, ...] = ("parts.json",)
    outputs: tuple[str, ...] = ("parts.json", "hooks_all.json")

    def run(self, ctx: JobContext) -> None:
        """Set `hook` on every part; skip when `hooks_all.json` already exists for these parts."""
        if ctx.file("hooks_all.json").exists():
            return
        parts: list[dict[str, Any]] = ctx.read_json("parts.json")
        all_hooks: list[list[str]] = []
        for i, p in enumerate(parts):
            nxt = parts[i + 1]["text"] if i + 1 < len(parts) else None
            raw = call_json(ctx, "hook", part_text=p["text"], next_part_text=nxt)["hooks"]
            hooks = [str(h).strip() for h in raw if 0 < len(str(h).split()) <= 12]
            hooks = hooks or [split_sentences(p["text"])[0]]
            j = call_json(
                ctx,
                "judge",
                purpose="Pick the hook most likely to stop a scroll while staying strictly true to the part.",
                constraints="Max 12 words. No overpromising. Style: " + ctx.combo.hook,
                options=hooks,
            )
            idx = max(0, min(int(j["index"]), len(hooks) - 1))
            p["hook"] = hooks[idx]
            all_hooks.append(hooks)
        ctx.write_json("hooks_all.json", all_hooks)
        ctx.write_json("parts.json", parts)


class SeoStage:
    """Shorts 7: one `Seo` per part, finalized by `finalize_seo` (spec 9.2)."""

    name: str = "seo"
    tenant: Tenant = "llama"
    inputs: tuple[str, ...] = ("parts.json",)
    outputs: tuple[str, ...] = ("seo.json",)

    def run(self, ctx: JobContext) -> None:
        """Write `seo.json` as a list with one entry per part."""
        parts = ctx.read_json("parts.json")
        kw = ctx.channel.seed_keywords[0] if ctx.channel.seed_keywords else ctx.channel.niche.split()[0]
        out: list[dict[str, Any]] = []
        for p in parts:
            part_n = p["n"] if len(parts) > 1 else None
            raw = parse_model(
                Seo,
                call_json(
                    ctx, "seo", primary_keyword=kw, part_text=p["text"], part_n=part_n, parts_total=len(parts)
                ),
                "seo",
            )
            final = finalize_seo(
                raw,
                primary_keyword=kw,
                fiction_label=ctx.channel.fiction_label,
                part_n=part_n,
                parts_total=len(parts),
                creator_note=ctx.channel.creator_note,
            )
            out.append(final.model_dump())
        ctx.write_json("seo.json", out)


def repair_beats(
    raw_beats: list[VisualBeat], sentence_count: int, *, group: int, default: VisualBeat
) -> list[VisualBeat]:
    """Make `raw_beats` cover sentences 0..sentence_count-1 exactly once, in order.

    Out-of-range or overlapping beats are dropped; gaps are filled with beats of up to `group` sentences
    that reuse the previous beat's keywords and mood (`default` when none exists). Beats are renumbered.
    """
    covered: set[int] = set()
    kept: list[VisualBeat] = []
    for b in sorted(raw_beats, key=lambda x: x.sentence_start):
        s, e = max(0, b.sentence_start), min(sentence_count - 1, b.sentence_end)
        if s > e or any(i in covered for i in range(s, e + 1)):
            continue
        kept.append(b.model_copy(update={"sentence_start": s, "sentence_end": e}))
        covered.update(range(s, e + 1))
    missing = [i for i in range(sentence_count) if i not in covered]
    runs: list[list[int]] = []
    for i in missing:
        if runs and runs[-1][-1] == i - 1 and len(runs[-1]) < group:
            runs[-1].append(i)
        else:
            runs.append([i])
    for run in runs:
        before = [b for b in kept if b.sentence_end < run[0]]
        src = max(before, key=lambda b: b.sentence_end) if before else (kept[0] if kept else default)
        kept.append(src.model_copy(update={"sentence_start": run[0], "sentence_end": run[-1]}))
    kept.sort(key=lambda x: x.sentence_start)
    return [b.model_copy(update={"beat": n}) for n, b in enumerate(kept)]


class VisualPlanStage:
    """Shorts 8: keyword beats covering every sentence of every part."""

    name: str = "visual_plan"
    tenant: Tenant = "llama"
    inputs: tuple[str, ...] = ("parts.json", "idea.json")
    outputs: tuple[str, ...] = ("visual_plan.json",)

    def run(self, ctx: JobContext) -> None:
        """Write `visual_plan.json` (list of `VisualBeat`, repaired to cover each sentence once)."""
        setting = ctx.read_json("idea.json").get("setting", "")
        beats_out: list[dict[str, Any]] = []
        for p in ctx.read_json("parts.json"):
            sentences = split_sentences(p["text"])
            raw = call_json(ctx, "visual_plan", sentences=sentences, setting=setting, part_n=p["n"])["beats"]
            beats = [parse_model(VisualBeat, b | {"part": p["n"]}, "visual_plan") for b in raw]
            default = VisualBeat(
                part=p["n"],
                beat=0,
                sentence_start=0,
                sentence_end=0,
                keywords=["empty room", "dim light", "window"],
                mood="eerie",
            )
            fixed = repair_beats(beats, len(sentences), group=3, default=default)
            beats_out += [b.model_dump() for b in fixed]
        ctx.write_json("visual_plan.json", beats_out)


for _st in (
    IdeasStage(),
    PlanStage(),
    DraftStage(),
    CriticReviseStage(),
    SplitStage(),
    HooksStage(),
    SeoStage(),
    VisualPlanStage(),
):
    register(_st)
