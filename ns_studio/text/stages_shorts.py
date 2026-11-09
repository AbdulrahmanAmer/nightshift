"""Shorts-only text stages: ideas to visual plan (component C5; spec 7.3, 16.2, 16.3, 9.2)."""

from __future__ import annotations

import json
import random
import re
from datetime import UTC, datetime
from typing import Any

from ns_common.devmode import is_dev
from ns_common.embed_client import cosine, embed_window, from_blob, to_blob
from ns_common.errors import NsError
from ns_common.models import Idea, Part, Seo, VisualBeat
from ns_common.pacing import PAUSE_PER_WORD_S, SHORT_QC_MARGIN
from ns_common.textstats import est_seconds, split_sentences, word_count
from ns_studio.context import JobContext
from ns_studio.policy.meta import policy_list
from ns_studio.stages import Tenant, register
from ns_studio.text.budget import (
    CLOSING_MAX_WORDS,
    HOOK_MAX_WORDS,
    SHORT_CLOSING_WORDS,
    SHORT_MIN_CLOSING_WORDS,
    SHORT_MIN_HOOK_WORDS,
    Budget,
    MasterBudget,
    master_as_budget,
    master_budget,
    short_budget,
)
from ns_studio.text.prompts import RUBRIC_KEYS, call_json, call_text, parse_model
from ns_studio.text.seo import finalize_seo
from ns_studio.text.story_profile import profile_of
from ns_studio.text.validators import check_banned, load_banned, raise_if, validate_story
from ns_studio.voice_wpm import job_wpm

__all__ = ["draw_banked_idea", "finalize_seo", "ideas_budget", "job_master_budget", "now_iso"]

IDEA_SIMILARITY_LIMIT = 0.85
CLOSINGS_FILE = "closings.json"
DEFAULT_SHORT_CLOSINGS = (
    "Follow for the next story.",
    "More stories like this are on the channel.",
    "If this stayed with you, the channel has more.",
)
NO_CHAPTER_POTENTIAL = {"", "n/a", "na", "none", "-"}


def job_master_budget(ctx: JobContext) -> MasterBudget:
    """Master budget of this job; the target minutes are drawn from `long_minutes` seeded by the job id."""
    return master_budget(
        ctx.channel.long_minutes,
        ctx.combo.pacing,
        rng=random.Random(ctx.job_id),
        wpm=job_wpm(ctx),
        profile=profile_of(ctx),
    )


def ideas_budget(ctx: JobContext) -> Budget:
    """Word budget of the story: the master (master-first) or one part of `combo.length_bucket` (50 s)."""
    if ctx.fmt == "master_first":
        return master_as_budget(job_master_budget(ctx))
    return short_budget(
        ctx.combo.length_bucket or 50,
        ctx.combo.pacing,
        parts=1,
        hook_words=HOOK_MAX_WORDS,
        closing_words=SHORT_CLOSING_WORDS,
        min_hook_words=SHORT_MIN_HOOK_WORDS,
        min_closing_words=SHORT_MIN_CLOSING_WORDS,
        wpm=job_wpm(ctx),
        profile=profile_of(ctx),
    )


def has_chapter_potential(idea: dict[str, Any]) -> bool:
    """True when the idea says how it stretches to chapters (its `why_chapters` is not empty or "n/a")."""
    return str(idea.get("why_chapters") or "").strip().lower().rstrip(".") not in NO_CHAPTER_POTENTIAL


def pick_idea(ctx: JobContext, survivors: list[tuple[dict[str, Any], Any]]) -> tuple[dict[str, Any], Any]:
    """The survivor the job continues with (spec 7.1 M1).

    A master-first job drops the survivors without chapter potential, then lets a judge call choose among
    the rest (no call for a single one). When none has potential the survivors stay as they were and a
    warning says so: a story with no stretch is a weaker master, not a reason to lose the night's job.
    A Shorts job takes the first survivor.
    """
    if ctx.fmt != "master_first":
        return survivors[0]
    staying = [s for s in survivors if has_chapter_potential(s[0])]
    if not staying:
        ctx.log.warn(
            "idea_no_chapter_potential",
            "no idea says how it stretches to chapters",
            job_id=ctx.job_id,
            ideas=len(survivors),
        )
        staying = survivors
    if len(staying) == 1:
        return staying[0]
    options = [
        f"{i['premise']} Twist: {i['twist']} Stretches to chapters: {i.get('why_chapters') or 'n/a'}"
        for i, _v in staying
    ]
    j = call_json(
        ctx,
        "judge",
        purpose="Pick the idea that can be told as five to seven escalating chapters of a long video "
        "and still keeps its twist.",
        constraints="The idea must carry a long story without padding. No overpromising.",
        options=options,
    )
    return staying[max(0, min(int(j["index"]), len(staying) - 1))]


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
        from ns_studio.policy import idea_check

        banked = draw_banked_idea(ctx)
        if banked is not None:
            idea_check.require_safe_idea(ctx, banked)
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
        flagged: list[str] = []
        chosen = None  # the pick (a master: with chapter potential) the unsafe-topic classifier accepts
        remaining = list(survivors)
        while remaining:
            cand = pick_idea(ctx, remaining)
            why = idea_check.idea_problem(ctx, cand[0])
            if why is None:
                chosen = cand
                break
            flagged.append(why)
            ctx.log.warn("idea_unsafe", job_id=ctx.job_id, premise=cand[0]["premise"], detail=why)
            remaining = [s for s in remaining if s is not cand]
        if chosen is None:
            raise NsError("NS-E601", f"all {len(survivors)} ideas were flagged as unsafe: {flagged[0]}")
        idea, vec = chosen
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
        raise_if(
            validate_story(
                text, ideas_budget(ctx), past_openings=past, embed=ctx.services.embed, profile=profile_of(ctx)
            )
        )
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
        ctx.file(CLOSINGS_FILE).unlink(missing_ok=True)  # ... and so are the closing lines
        ctx.write_json("parts.json", parts)


def cut_words(text: str, limit: int) -> str:
    """`text` cut to its first `limit` words (trailing commas and dashes dropped); unchanged when shorter."""
    words = text.split()
    if len(words) <= limit:
        return text.strip()
    return " ".join(words[:limit]).rstrip(",;:- ")


def default_closing(part_n: int, parts_total: int) -> str:
    """The closing line used when the model gives none or one over `CLOSING_MAX_WORDS` words."""
    if part_n < parts_total:
        return f"Part {part_n + 1} is up next."
    return DEFAULT_SHORT_CLOSINGS[(part_n - 1) % len(DEFAULT_SHORT_CLOSINGS)]


def closing_is_clean(text: str) -> bool:
    """False when the closing line holds a banned phrase (`contracts/policy/banned_phrases.txt`) or a claim
    that the story is real (`misleading_claims.txt`, the E603 list). The line is spoken, captioned and
    published, and the policy gate reads only the metadata, so the stage that takes it checks it."""
    if check_banned(text, load_banned()):
        return False
    low = text.lower()
    return not any(re.search(rf"\b{re.escape(p)}\b", low) for p in policy_list("misleading_claims.txt"))


def closing_line(raw: Any, part_n: int, parts_total: int) -> str:
    """The model's closing line when it is 1-15 words and clean, else the default for this part."""
    text = str(raw or "").strip()
    if 0 < len(text.split()) <= CLOSING_MAX_WORDS and closing_is_clean(text):
        return text
    return default_closing(part_n, parts_total)


def fit_closing(ctx: JobContext, part: dict[str, Any], hook: str, closing: str, parts_total: int) -> str:
    """`closing`, or a shorter line when it would carry the Short past QC's duration band.

    The budget reserves `SHORT_CLOSING_WORDS` for the closing line, but the model may write up to
    `CLOSING_MAX_WORDS`, and a body at the top of its band plus a long hook plus a long closing line lands
    over the bucket by more than the 15 % QC allows (NS-W406, the Short is dropped). The hooks stage knows
    the body and the hook, so it estimates the seconds the model's line leaves and takes the first of the
    model's line, the part's default and the other defaults that stays within the budget's own +12 % margin;
    when none does, the shortest default.
    """
    from ns_studio.media.captions import CLOSE_GAP_S, HOOK_GAP_S
    from ns_studio.media.voice import hook_opens_body

    per_word_s = 60.0 / job_wpm(ctx) + PAUSE_PER_WORD_S
    bucket_s = float(ctx.combo.length_bucket or 50)
    voiced_hook = 0 if hook_opens_body(hook, part["text"]) else word_count(hook)
    spent_s = (word_count(part["text"]) + voiced_hook) * per_word_s + HOOK_GAP_S + CLOSE_GAP_S
    room = int((bucket_s * (1 + SHORT_QC_MARGIN) - spent_s) / per_word_s)
    candidates = [closing, default_closing(int(part["n"]), parts_total), *DEFAULT_SHORT_CLOSINGS]
    for line in candidates:
        if word_count(line) <= room:
            return line
    return min(candidates[1:], key=word_count)


class HooksStage:
    """Shorts 6: five hooks per part, a judge picks one, plus a closing line per part (<= 15 words).

    All hook candidates go to `hooks_all.json`; the closing lines to `closings.json` (`{"1": "...", ...}`),
    which the voice stage speaks as `p<n>_close` and the render appends to the part.

    For a Short made from the owner's own script the stage adds only the closing line: the text and the hook
    (its first sentence) stay as the import wrote them and `hooks_all.json` holds that one hook.
    """

    name: str = "hooks"
    tenant: Tenant = "llama"
    inputs: tuple[str, ...] = ("parts.json",)
    # `closings.json` is written here but is not a declared output: a shorts_only job from before closing
    # lines would otherwise count as missing an output and `--resume` would pick new hooks and redo the
    # voice and captions. Such a job keeps the hooks it has and gets no closing line.
    outputs: tuple[str, ...] = ("parts.json", "hooks_all.json")

    def run(self, ctx: JobContext) -> None:
        """Set `hook` on every part and write its closing line; skip when `hooks_all.json` exists.

        `closings.json` is written before `hooks_all.json`, so a job made by this code has both when it skips.
        A job from before closing lines has only the hooks: it keeps them and gets no closing line, rather
        than picking new hooks (and redoing the voice and captions) on `--resume`.
        """
        if ctx.file("hooks_all.json").exists():
            return
        parts: list[dict[str, Any]] = ctx.read_json("parts.json")
        all_hooks: list[list[str]] = []
        closings: dict[str, str] = {}
        owner = ctx.owner_script
        for i, p in enumerate(parts):
            nxt = parts[i + 1]["text"] if i + 1 < len(parts) else None
            out = call_json(
                ctx, "hook", part_text=p["text"], next_part_text=nxt, part_n=p["n"], parts_total=len(parts)
            )
            wanted = closing_line(out.get("closing_line"), p["n"], len(parts))
            if owner:
                # The owner's story is narrated as written, with its first sentence as the hook: only the
                # closing line is added after it (spec 7.0, D16). The model's hook candidates are not used.
                first = cut_words(split_sentences(p["text"])[0], HOOK_MAX_WORDS)
                hook = str(p.get("hook") or "").strip() or first
                closings[str(p["n"])] = fit_closing(ctx, p, hook, wanted, len(parts))
                all_hooks.append([hook])
                continue
            hooks = [str(h).strip() for h in out["hooks"] if 0 < len(str(h).split()) <= HOOK_MAX_WORDS]
            hooks = hooks or [cut_words(split_sentences(p["text"])[0], HOOK_MAX_WORDS)]
            j = call_json(
                ctx,
                "judge",
                purpose="Pick the hook most likely to stop a scroll while staying strictly true to the part.",
                constraints="Max 12 words. No overpromising. Style: " + ctx.combo.hook,
                options=hooks,
            )
            idx = max(0, min(int(j["index"]), len(hooks) - 1))
            p["hook"] = hooks[idx]
            closings[str(p["n"])] = fit_closing(ctx, p, p["hook"], wanted, len(parts))
            all_hooks.append(hooks)
        ctx.write_json(CLOSINGS_FILE, closings)
        ctx.write_json("hooks_all.json", all_hooks)
        if not owner:
            ctx.write_json("parts.json", parts)  # an owner's parts.json stays as the import wrote it


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
