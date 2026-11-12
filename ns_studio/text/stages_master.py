"""Master-first text stages: long plan to Shorts SEO, plus `expand_chapter` (component C5; spec 7.0, 7.1)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from difflib import SequenceMatcher
from typing import Any

from ns_common.atomic import write_text_atomic
from ns_common.devmode import is_dev
from ns_common.embed_client import embed_window, to_blob
from ns_common.errors import NsError
from ns_common.models import (
    Plan,
    PlanChapter,
    Sentence,
    Seo,
    SeoLong,
    ShortsPlanItem,
    Story,
    StoryChapter,
    VisualBeat,
)
from ns_common.textstats import split_sentences, word_count
from ns_studio.context import JobContext
from ns_studio.stages import Tenant, register
from ns_studio.text.budget import (
    SHORT_TOLERANCE,
    chapter_budget,
    distribute_words,
    moment_limits,
    short_body_words,
)
from ns_studio.text.models_local import ChapterDraft
from ns_studio.text.prompts import call_json, call_text, parse_model
from ns_studio.text.seo import finalize_seo, finalize_seo_long
from ns_studio.text.stages_shorts import (
    CLOSING_MAX_WORDS,
    HOOK_MAX_WORDS,
    MAX_LENGTH_REPAIRS,
    closing_is_clean,
    critic_numbers,
    in_window,
    job_master_budget,
    length_failure,
    now_iso,
    repair_beats,
    repair_length,
    window_vars,
)
from ns_studio.text.story_profile import profile_of
from ns_studio.text.validators import (
    check_banned,
    check_cliff_variety,
    check_master_length,
    check_reading_level,
    check_sentence_length,
    load_banned,
    raise_if,
    validate_chapter,
)
from ns_studio.variation.short_draw import draw_short_combos, load_short_combos
from ns_studio.voice_wpm import job_wpm

__all__ = ["expand_chapter", "expansion_plan", "resolve_moments"]

MASTER_STAGES = [
    "ideas",
    "long_plan",
    "chapters_draft",
    "chapters_critic",
    "continuity",
    "seo_long",
    "shorts_select",
    "visual_plan_long",
    "seo_shorts",
]
SIZE_TOLERANCE = SHORT_TOLERANCE  # a Short moment fits its bucket when within the QC band (+-15 %)
HOOK_CONSTRAINTS = "Max {max} words. Must make sense without the rest of the story. Style: {style}"
TITLE_CONSTRAINTS ="At most 70 characters. {kw!r} in the first 40 characters. Never claim the story is real."
DEFAULT_CLOSINGS = (
    "The full story is on the channel.",
    "Chapter by chapter, the whole story is on the channel.",
    "To hear what happened next, watch the full story on the channel.",
)


def _wpm(ctx: JobContext) -> int:
    return job_wpm(ctx)  # the voice's measured pace when a job has measured it, else the pacing table


def _kw(ctx: JobContext) -> str:
    return ctx.channel.seed_keywords[0] if ctx.channel.seed_keywords else ctx.channel.niche.split()[0]


def build_chapter(n: int, title: str, text: str, summary: str, *, wpm: int, is_last: bool) -> StoryChapter:
    """A `StoryChapter` with its sentence index, word count, duration and cliff line (not for the last)."""
    text = text.strip()
    sentences = split_sentences(text) or [text]
    words = max(1, word_count(text))
    return StoryChapter(
        n=n,
        title=title,
        text=text,
        summary=summary,
        word_count=words,
        est_seconds=max(0.01, round(words / wpm * 60.0, 2)),
        cliff_line_idx=None if is_last else len(sentences) - 1,
        sentences=[Sentence(idx=i, text=s) for i, s in enumerate(sentences)],
    )


def load_story(ctx: JobContext) -> Story:
    """Read and validate `story.json`."""
    return parse_model(Story, ctx.read_json("story.json"), "story.json")


def _past_openings(ctx: JobContext) -> list[str]:
    if is_dev(ctx.paths):
        return []          # the fake LLM tells the same story every run; dev mode must not fail on that
    rows = ctx.db.query(
        "select opening_line from stories where channel=? and job_id!=? order by created_at desc limit 50",
        (ctx.channel.slug, ctx.job_id),
    )
    return [r["opening_line"] for r in rows]


class LongPlanStage:
    """M2: chapters with mini-arcs, cliffs and word budgets."""

    name: str = "long_plan"
    tenant: Tenant = "llama"
    inputs: tuple[str, ...] = ("idea.json",)
    outputs: tuple[str, ...] = ("plan.json", "plan_meta.json")

    def run(self, ctx: JobContext) -> None:
        """Write `plan.json` (a `Plan`) and `plan_meta.json` (working title, wpm, planning floor)."""
        mb = job_master_budget(ctx)
        out = call_json(ctx, "long_plan", idea=ctx.read_json("idea.json"), budget=mb)
        raw = out["chapters"]
        if not 5 <= len(raw) <= 7:
            raise NsError("NS-E302", f"long_plan returned {len(raw)} chapters, expected 5-7")
        budgets = distribute_words(mb.target_words, len(raw))
        chapters = [
            parse_model(
                PlanChapter,
                {
                    "n": i,
                    "title": c.get("title"),
                    "beats": c.get("beats"),
                    "cliff": c.get("cliff"),
                    "word_budget": budgets[i - 1],
                },
                f"long_plan chapter {i}",
            )
            for i, c in enumerate(raw, start=1)
        ]
        plan = Plan(target_minutes=mb.target_minutes, target_words=mb.target_words, chapters=chapters)
        ctx.write_json("plan.json", plan.model_dump())
        ctx.write_json(
            "plan_meta.json",
            {"working_title": str(out["working_title"]).strip(), "wpm": mb.wpm, "min_words": mb.min_words},
        )


class ChaptersDraftStage:
    """M3: draft chapter by chapter with a running summary; each chapter is saved as it is written."""

    name: str = "chapters_draft"
    tenant: Tenant = "llama"
    inputs: tuple[str, ...] = ("plan.json", "plan_meta.json")
    outputs: tuple[str, ...] = ("story_draft.json",)

    def run(self, ctx: JobContext) -> None:
        """Write `story_draft.json`; chapters already drafted for the same plan are reused (crash resume).

        A saved chapter also carries the generation it was written in (`JobContext.generation`): a
        regeneration restarts this stage with the same plan, and reusing the chapters would rebuild the story
        that just failed. Drafts without the key (an older job folder) are generation 0.
        """
        plan_bytes = ctx.file("plan.json").read_bytes()
        plan_sha = hashlib.sha256(plan_bytes).hexdigest()
        plan = parse_model(Plan, json.loads(plan_bytes), "plan.json")
        title = ctx.read_json("plan_meta.json")["working_title"]
        idea = ctx.read_json("idea.json")
        drafts = ctx.subdir("drafts")
        wpm = _wpm(ctx)
        generation = ctx.generation
        summaries: list[dict[str, Any]] = []
        chapters: list[dict[str, Any]] = []
        previous = ""
        for ch in plan.chapters:
            f = drafts / f"ch{ch.n}.json"
            saved = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
            if saved and saved.get("plan_sha") == plan_sha and saved.get("generation", 0) == generation:
                draft = ChapterDraft.model_validate(saved["draft"])
            else:
                raw = call_json(
                    ctx,
                    "chapter_draft",
                    fixture=f"chapter_draft_{ch.n}",
                    idea=idea,
                    plan=plan.model_dump(),
                    chapter=ch.model_dump(),
                    summaries=summaries,
                    last_sentences=split_sentences(previous)[-2:] if previous else [],
                    budget=chapter_budget(ch.word_budget, wpm, profile_of(ctx)),
                )
                draft = parse_model(ChapterDraft, raw, f"chapter_draft {ch.n}")
                write_text_atomic(
                    f,
                    json.dumps(
                        {"plan_sha": plan_sha, "generation": generation, "draft": draft.model_dump()},
                        ensure_ascii=False,
                    ),
                )
            text = draft.text.strip()
            summaries.append({"n": ch.n, "title": ch.title, "summary": draft.summary.strip()})
            chapters.append({"n": ch.n, "title": ch.title, "text": text, "summary": draft.summary.strip()})
            previous = text
        ctx.write_json("story_draft.json", {"working_title": title, "chapters": chapters, "revisions": 0})


class ChaptersCriticStage:
    """M4a: critique and revise every chapter (at most 2 revisions, 2 length repairs), then validate it."""

    name: str = "chapters_critic"
    tenant: Tenant = "llama"
    uses_embed: bool = True  # calls the embed service; the orchestrator leases it around run()
    inputs: tuple[str, ...] = ("story_draft.json", "plan.json")
    outputs: tuple[str, ...] = ("story_revised.json", "critic.json")

    def run(self, ctx: JobContext) -> None:
        """Write `story_revised.json` and `critic.json`; NS-E304 when a chapter misses the bar twice over.

        Card story-robust S1, S2: every `revise` is told the chapter's window and its length now. A chapter
        that meets the bar but lies outside its window (after a rewrite, or its first draft) gets a length
        repair inside this loop (`repair_length`: lengthen with `expand.md`, trim with `revise.md`), at most
        `MAX_LENGTH_REPAIRS` per chapter, and is scored again: the bar and the window must both hold.
        Repairs spent: NS-E303 with the chapter, the words and the window. The other validator rules
        (banned phrase, repeated opening, reading level, sentence length) raise as before. The stage writes
        its outputs only at the end: a kill in the middle of a repair redoes the stage from the drafts.
        """
        draft = ctx.read_json("story_draft.json")
        plan = parse_model(Plan, ctx.read_json("plan.json"), "plan.json")
        budgets = {c.n: c.word_budget for c in plan.chapters}
        beats_of = {c.n: list(c.beats) for c in plan.chapters}
        bar = ctx.channel.critic_bar
        wpm = _wpm(ctx)
        profile = profile_of(ctx)
        past = _past_openings(ctx)
        total = len(draft["chapters"])
        revised: list[dict[str, Any]] = []
        report: list[dict[str, Any]] = []
        revisions_total = 0
        for ch in draft["chapters"]:
            text: str = ch["text"]
            budget = chapter_budget(budgets.get(ch["n"], plan.target_words // total), wpm, profile)
            revisions = repairs = attempt = 0
            critique: dict[str, Any] = {}
            avg = low = 0.0
            # Bounded: every pass breaks, raises, or spends a revision (at most 2) or a repair (at most 2).
            while True:
                critique = call_json(
                    ctx,
                    "critic",
                    story=text,
                    banned=load_banned(),
                    scope=f"chapter {ch['n']} of {total} of a longer story",
                )
                avg, low = critic_numbers(critique)
                ctx.log.info("critic", chapter=ch["n"], avg=avg, low=low, attempt=attempt, job_id=ctx.job_id)
                attempt += 1
                if avg >= bar and low >= 6:
                    if in_window(text, budget):
                        break
                    if repairs >= MAX_LENGTH_REPAIRS:
                        every = validate_chapter(
                            text, budget, chapter_n=ch["n"], past_openings=past, embed=ctx.services.embed,
                            profile=profile,
                        )  # fmt: skip
                        raise length_failure(text, budget, f"chapter {ch['n']} ", repairs, every)
                    repairs += 1
                    text = repair_length(
                        ctx, text, budget, attempt=repairs, chapter={"n": ch["n"], "title": ch["title"]},
                        beats=beats_of.get(ch["n"], []), log_fields={"chapter": ch["n"]},
                    )  # fmt: skip
                    continue
                if revisions >= 2:
                    detail = f"chapter {ch['n']}: critic avg {avg:.1f}, low {low:.0f}, {revisions} revisions"
                    raise NsError("NS-E304", detail)
                text = call_text(
                    ctx, "revise", story=text, fixes=critique["fixes"], **window_vars(text, budget)
                ).strip()
                revisions += 1
            raise_if(
                validate_chapter(
                    text,
                    budget,
                    chapter_n=ch["n"],
                    past_openings=past,
                    embed=ctx.services.embed,
                    profile=profile,
                )
            )
            revisions_total += revisions
            revised.append({**ch, "text": text})
            report.append(
                {"n": ch["n"], "avg": avg, "low": low, "revisions": revisions, "critique": critique}
            )
        ctx.write_json(
            "critic.json",
            {"critic_avg": sum(r["avg"] for r in report) / len(report), "chapters": report},
        )
        ctx.write_json(
            "story_revised.json",
            {"working_title": draft["working_title"], "chapters": revised, "revisions": revisions_total},
        )


FLOOR_MAX_EXPANSIONS = 3  # +120..200 words each, tried before the 9:00 floor fails the story


def enforce_word_floor(ctx: JobContext, total_words: int) -> None:
    """Spec 7.0: a story below `plan_meta.min_words` (the 9:00 planning floor) is lengthened, or refused.

    The shortest chapter gets `expand_chapter` (+120..200 words, the llama tenant is already held by the
    continuity stage), up to `FLOOR_MAX_EXPANSIONS` times; a story still below the floor raises NS-E303.
    Without `plan_meta.json` (or a `min_words` in it) there is no floor to hold.
    """
    try:
        floor = int(ctx.read_json("plan_meta.json")["min_words"])
    except (OSError, ValueError, KeyError, TypeError):
        return
    expansions = 0
    while total_words < floor:
        if ctx.owner_script or expansions >= FLOOR_MAX_EXPANSIONS:  # the owner's words are never extended
            raise NsError(
                "NS-E303",
                f"story has {total_words} words after {expansions} expansions, 9:00 floor is {floor}",
            )
        chapter, add_words = expansion_plan(load_story(ctx), floor - total_words)
        ctx.log.warn(
            "story_below_floor", "story is below the 9:00 floor: expanding its shortest chapter",
            job_id=ctx.job_id, words=total_words, floor=floor, chapter=chapter, add_words=add_words,
        )
        expand_chapter(ctx, chapter, add_words)
        expansions += 1
        total_words = sum(c.word_count for c in load_story(ctx).chapters)


def pairwise_continuity(ctx: JobContext) -> bool:
    """True when continuity runs on chapter pairs: the `cpu-dev` profile or a context of 8K tokens or less."""
    return ctx.runtime.profile == "cpu-dev" or ctx.runtime.llama.flags.ctx <= 8192


class ContinuityStage:
    """M4b: whole-story (or pairwise) continuity pass, final validation and `story.json`."""

    name: str = "continuity"
    tenant: Tenant = "llama"
    uses_embed: bool = True  # calls the embed service; the orchestrator leases it around run()
    # plan_meta.json (the 9:00 floor `enforce_word_floor` reads) is not an input on purpose: it only changes
    # when the plan stage reruns, which rewrites the chapters and so `story_revised.json` as well. Declared,
    # it would change the stored hash of every older job and `--resume` would redo this LLM stage.
    inputs: tuple[str, ...] = ("story_revised.json", "critic.json")
    outputs: tuple[str, ...] = ("story.json", "continuity.json")

    def run(self, ctx: JobContext) -> None:
        """Fix contradictions with one `revise` per affected chapter, then write `story.json` (a `Story`)."""
        revised = ctx.read_json("story_revised.json")
        chapters: list[dict[str, Any]] = revised["chapters"]
        by_n = {c["n"]: c for c in chapters}
        pairwise = pairwise_continuity(ctx)
        if pairwise:
            groups = [chapters[i : i + 2] for i in range(len(chapters) - 1)]
        else:
            groups = [chapters]
        fixes: dict[int, list[str]] = {}
        issues: list[dict[str, Any]] = []
        for group in groups:
            scope = "the whole story" if not pairwise else f"chapters {group[0]['n']} and {group[-1]['n']}"
            out = call_json(
                ctx,
                "continuity",
                scope=scope,
                chapters=[{"n": c["n"], "title": c["title"], "text": c["text"]} for c in group],
            )
            for issue in out["issues"]:
                n = int(issue["chapter"])
                if n in by_n:
                    fixes.setdefault(n, []).append(str(issue["fix"]))
                    issues.append({"chapter": n, "problem": issue["problem"], "fix": issue["fix"]})
        wpm = _wpm(ctx)
        profile = profile_of(ctx)
        plan = parse_model(Plan, ctx.read_json("plan.json"), "plan.json")
        budgets = {c.n: c.word_budget for c in plan.chapters}
        past = _past_openings(ctx)
        for n, chapter_fixes in sorted(fixes.items()):
            text = call_text(ctx, "revise", story=by_n[n]["text"], fixes=chapter_fixes).strip()
            raise_if(
                validate_chapter(
                    text,
                    chapter_budget(budgets.get(n, 0) or plan.target_words // len(chapters), wpm, profile),
                    chapter_n=n,
                    past_openings=past,
                    embed=ctx.services.embed,
                    profile=profile,
                )
            )
            by_n[n] = {**by_n[n], "text": text}
        last_n = chapters[-1]["n"]
        built = [
            build_chapter(
                c["n"], c["title"], by_n[c["n"]]["text"], c["summary"], wpm=wpm, is_last=c["n"] == last_n
            )
            for c in chapters
        ]
        endings = [c.sentences[-1].text for c in built]
        raise_if([v for v in [check_cliff_variety(endings, ctx.services.embed)] if v is not None])
        story = Story(
            working_title=revised["working_title"],
            chapters=built,
            revisions=int(revised["revisions"]) + len(fixes),
        )
        full_text = "\n\n".join(c.text for c in built)
        total_words = sum(c.word_count for c in built)
        # Chapters may each run 20 % over; together they must still fit the 15:00 QC ceiling.
        raise_if(
            [v for v in [check_master_length([c.text for c in built], wpm, profile=profile)] if v is not None]
        )
        if total_words < plan.target_words * profile.master_short_share:
            ctx.log.warn("story_short", words=total_words, target=plan.target_words, job_id=ctx.job_id)
        vec = ctx.services.embed.embed([embed_window(full_text, ctx.runtime.embed.batch_size)])[0]
        critic_avg = float(ctx.read_json("critic.json")["critic_avg"])
        ctx.db.execute(
            "insert or replace into stories(job_id, channel, format, working_title, text, word_count, "
            "opening_line, embedding, critic_avg, revisions, created_at) values (?,?,?,?,?,?,?,?,?,?,?)",
            (
                ctx.job_id,
                ctx.channel.slug,
                ctx.fmt,
                story.working_title,
                full_text,
                total_words,
                built[0].sentences[0].text,
                to_blob(vec),
                critic_avg,
                story.revisions,
                now_iso(),
            ),
        )
        ctx.write_json("continuity.json", {"pairwise": pairwise, "issues": issues, "revised": sorted(fixes)})
        ctx.write_json("story.json", story.model_dump())
        enforce_word_floor(ctx, total_words)  # may rewrite story.json and the stories row


class SeoLongStage:
    """M5: title best-of-5 with a judge, description, tags, chapter titles."""

    name: str = "seo_long"
    tenant: Tenant = "llama"
    inputs: tuple[str, ...] = ("story.json",)
    outputs: tuple[str, ...] = ("seo_long.json",)

    def run(self, ctx: JobContext) -> None:
        """Write `seo_long.json` (a `SeoLong`, description holds the chapter-list placeholder)."""
        story = load_story(ctx)
        kw = _kw(ctx)
        opening = " ".join(story.chapters[0].text.split()[:150])
        raw = call_json(
            ctx,
            "long_seo",
            primary_keyword=kw,
            working_title=story.working_title,
            chapters=[{"n": c.n, "title": c.title, "summary": c.summary} for c in story.chapters],
            opening=opening,
        )
        titles = [str(t).strip() for t in raw["titles"] if str(t).strip()]
        if not titles:
            raise NsError("NS-E302", "long_seo returned no titles")
        judged = call_json(
            ctx,
            "judge",
            purpose="Pick the title that best matches what the story delivers without overpromising.",
            constraints=TITLE_CONSTRAINTS.format(kw=kw),
            options=titles,
        )
        title = titles[max(0, min(int(judged["index"]), len(titles) - 1))]
        given = {
            int(t["n"]): str(t["title"]).strip()
            for t in raw["chapter_titles"]
            if str(t.get("title", "")).strip()
        }
        chapter_titles = [{"n": c.n, "title": given.get(c.n, c.title)} for c in story.chapters]
        seo = parse_model(
            SeoLong,
            {
                "title": title[:100],
                "description": raw["description"],
                "tags": raw["tags"][:20],
                "hashtags": raw["hashtags"][:5],
                "keywords": raw.get("keywords", []),
                "chapter_titles": chapter_titles,
            },
            "seo_long",
        )
        final = finalize_seo_long(
            seo,
            primary_keyword=kw,
            fiction_label=ctx.channel.fiction_label,
            creator_note=ctx.channel.creator_note,
            series=ctx.series or None,
        )
        ctx.write_json("seo_long.json", final.model_dump())


def _span_words(sentences: Sequence[Sentence], start: int, end: int) -> int:
    return sum(word_count(s.text) for s in sentences[start : end + 1])


def _within(words: int, target: int) -> bool:
    return target * (1 - SIZE_TOLERANCE) <= words <= target * (1 + SIZE_TOLERANCE)


def resolve_moments(
    moments: Sequence[dict[str, Any]],
    story: Story,
    *,
    buckets: Sequence[int],
    wpm: int,
    count_min: int,
    count_max: int,
) -> list[dict[str, Any]]:
    """Turn the model's proposed moments into valid, sized, non-overlapping Short spans.

    Spans are clamped to their chapter; the length bucket is the proposed one when the span's words fit it
    (+-20 % of `short_body_words`), else the nearest bucket; a span that fits no bucket is re-ended on a
    sentence boundary that does, else dropped. Overlaps are dropped (earlier proposals win), at most
    `count_max` are kept, and the result is sorted by chapter and position. Fewer than `count_min` raises
    NS-E303.
    """
    chapters = {c.n: c for c in story.chapters}
    targets = {b: short_body_words(b, wpm) for b in buckets}
    kept: list[dict[str, Any]] = []

    def overlaps(chapter: int, start: int, end: int) -> bool:
        return any(
            k["chapter"] == chapter and start <= k["sentence_end"] and end >= k["sentence_start"]
            for k in kept
        )

    for m in moments:
        ch = chapters.get(int(m.get("chapter", 0)))
        if ch is None or len(kept) >= count_max:
            continue
        sentences = ch.sentences
        last = len(sentences) - 1
        start = max(0, min(int(m["sentence_start"]), last))
        end = max(start, min(int(m["sentence_end"]), last))
        if overlaps(ch.n, start, end):
            continue
        hint = int(m.get("length_bucket") or 0)
        words = _span_words(sentences, start, end)
        order = ([hint] if hint in targets else []) + sorted(targets, key=lambda b: abs(words - targets[b]))
        bucket = next((b for b in order if _within(words, targets[b])), None)
        if bucket is None and order:
            nearest = order[0]
            target = targets[nearest]
            ends = range(start, last + 1)
            best = min(ends, key=lambda e: abs(_span_words(sentences, start, e) - target))
            if _within(_span_words(sentences, start, best), target) and not overlaps(ch.n, start, best):
                end, bucket = best, nearest
        if bucket is None:
            continue
        kept.append(
            {
                "chapter": ch.n,
                "sentence_start": start,
                "sentence_end": end,
                "length_bucket": bucket,
                "why_standalone": str(m.get("why_standalone", "")).strip(),
                "words": _span_words(sentences, start, end),
            }
        )
    if len(kept) < count_min:
        raise NsError("NS-E303", f"shorts selection yielded {len(kept)} usable moments, need {count_min}")
    return sorted(kept, key=lambda k: (k["chapter"], k["sentence_start"]))


def span_text(story: Story, chapter: int, start: int, end: int) -> str:
    """Text of sentences `start..end` of `chapter`."""
    ch = next(c for c in story.chapters if c.n == chapter)
    return " ".join(s.text for s in ch.sentences[start : end + 1])


class ShortsSelectStage:
    """M7: 4-6 stand-alone moments by sentence span, five hooks and a judge per moment, a closing line."""

    name: str = "shorts_select"
    tenant: Tenant = "llama"
    inputs: tuple[str, ...] = ("story.json",)
    outputs: tuple[str, ...] = ("shorts_plan.json",)

    def run(self, ctx: JobContext) -> None:
        """Write `shorts_plan.json` (list of `ShortsPlanItem`, timings left for the voice/caption stages)."""
        story = load_story(ctx)
        wpm = _wpm(ctx)
        buckets = sorted(ctx.channel.length_buckets)
        count_min, count_max = ctx.channel.shorts_per_master
        count_min = profile_of(ctx).moments_min(count_min)
        raw = call_json(
            ctx,
            "shorts_select",
            count_min=count_min,
            count_max=count_max,
            buckets=[
                {
                    "seconds": b,
                    "body_words": short_body_words(b, wpm),
                    "min_words": moment_limits(short_body_words(b, wpm))[0],
                    "max_words": moment_limits(short_body_words(b, wpm))[1],
                }
                for b in buckets
            ],
            chapters=[
                {
                    "n": c.n,
                    "title": c.title,
                    "sentences": [
                        {"idx": s.idx, "text": s.text, "words": word_count(s.text)} for s in c.sentences
                    ],
                }
                for c in story.chapters
            ],
        )
        moments = resolve_moments(
            raw["moments"], story, buckets=buckets, wpm=wpm, count_min=count_min, count_max=count_max
        )
        # Each Short has its own combo (hook style, caption style, visual treatment, title formula): draw them
        # now so the hook prompt and the judge use the Short's hook style, not the master's.
        short_combos = draw_short_combos(
            ctx, [{"k": k, "length_bucket": m["length_bucket"]} for k, m in enumerate(moments, start=1)]
        )
        items: list[dict[str, Any]] = []
        for k, m in enumerate(moments, start=1):
            short_combo = short_combos[str(k)]
            text = span_text(story, m["chapter"], m["sentence_start"], m["sentence_end"])
            chapter_title = next(c.title for c in story.chapters if c.n == m["chapter"])
            out = call_json(
                ctx,
                "short_hook",
                fixture=f"short_hook_{k}",
                span_text=text,
                why_standalone=m["why_standalone"],
                chapter_title=chapter_title,
                combo=short_combo,
            )
            hooks = [str(h).strip() for h in out["hooks"] if 0 < len(str(h).split()) <= HOOK_MAX_WORDS]
            hooks = hooks or [" ".join(split_sentences(text)[0].split()[:HOOK_MAX_WORDS])]
            judged = call_json(
                ctx,
                "judge",
                purpose="Pick the hook most likely to stop a scroll while staying true to the moment.",
                constraints=HOOK_CONSTRAINTS.format(max=HOOK_MAX_WORDS, style=short_combo.hook),
                options=hooks,
            )
            chosen = hooks[max(0, min(int(judged["index"]), len(hooks) - 1))]
            closing = str(out["closing_line"]).strip()
            if not closing or len(closing.split()) > CLOSING_MAX_WORDS or not closing_is_clean(closing):
                closing = DEFAULT_CLOSINGS[(k - 1) % len(DEFAULT_CLOSINGS)]
            item = ShortsPlanItem(
                k=k,
                chapter=m["chapter"],
                sentence_start=m["sentence_start"],
                sentence_end=m["sentence_end"],
                hooks=hooks,
                chosen_hook=chosen,
                closing_line=closing,
                length_bucket=m["length_bucket"],
                combo_id=short_combo.combo_id,
                why_standalone=m["why_standalone"],
            )
            items.append(item.model_dump())
        ctx.write_json("shorts_plan.json", items)


class VisualPlanLongStage:
    """M6: per-beat keywords for every chapter; `need_vertical` only inside selected Shorts."""

    name: str = "visual_plan_long"
    tenant: Tenant = "llama"
    inputs: tuple[str, ...] = ("story.json", "shorts_plan.json", "idea.json")
    outputs: tuple[str, ...] = ("visual_plan.json",)

    def run(self, ctx: JobContext) -> None:
        """Write `visual_plan.json`: beats of up to 3 sentences covering each chapter sentence once."""
        story = load_story(ctx)
        shorts_plan = [
            parse_model(ShortsPlanItem, i, "shorts_plan.json") for i in ctx.read_json("shorts_plan.json")
        ]
        setting = ctx.read_json("idea.json").get("setting", "")
        out: list[dict[str, Any]] = []
        for ch in story.chapters:
            sentences = [s.text for s in ch.sentences]
            raw = call_json(ctx, "visual_plan", sentences=sentences, setting=setting, chapter_n=ch.n)["beats"]
            overrides = {"chapter": ch.n, "part": None, "need_vertical": False}
            beats = [parse_model(VisualBeat, b | overrides, "visual_plan") for b in raw]
            default = VisualBeat(
                chapter=ch.n,
                beat=0,
                sentence_start=0,
                sentence_end=0,
                keywords=["empty room", "dim light", "window"],
                mood="eerie",
                need_vertical=False,
            )
            spans = [(s.sentence_start, s.sentence_end) for s in shorts_plan if s.chapter == ch.n]
            for b in repair_beats(beats, len(sentences), group=3, default=default):
                inside = any(b.sentence_start <= e and b.sentence_end >= s for s, e in spans)
                out.append(b.model_copy(update={"need_vertical": inside}).model_dump())
        ctx.write_json("visual_plan.json", out)


class SeoShortsStage:
    """M8: Shorts SEO per moment (best-of-5 titles, spec 9.2, 'From the full story: <master title>')."""

    name: str = "seo_shorts"
    tenant: Tenant = "llama"
    inputs: tuple[str, ...] = ("shorts_plan.json", "seo_long.json", "story.json", "short_combos.json")
    outputs: tuple[str, ...] = ("seo_shorts.json",)

    def run(self, ctx: JobContext) -> None:
        """Write `seo_shorts.json`: one `Seo` per Short, in `k` order."""
        story = load_story(ctx)
        master_title = ctx.read_json("seo_long.json")["title"]
        kw = _kw(ctx)
        short_combos = load_short_combos(ctx)
        out: list[dict[str, Any]] = []
        for item in ctx.read_json("shorts_plan.json"):
            text = span_text(story, item["chapter"], item["sentence_start"], item["sentence_end"])
            raw = call_json(
                ctx,
                "seo",
                fixture=f"seo_short_{item['k']}",
                schema_name="seo_titles",
                combo=short_combos.get(str(item["k"]), ctx.combo),  # the Short's own title formula
                primary_keyword=kw,
                part_text=text,
                part_n=ctx.series.get("part_n"),
                parts_total=int(ctx.series.get("parts_total", 1)),
                n_titles=5,
            )
            titles = [str(t).strip() for t in raw.get("titles", []) if str(t).strip()]
            title = str(raw["title"])
            if len(titles) > 1:
                judged = call_json(
                    ctx,
                    "judge",
                    purpose="Pick the title that best matches this Short without overpromising.",
                    constraints=TITLE_CONSTRAINTS.format(kw=kw),
                    options=titles,
                )
                title = titles[max(0, min(int(judged["index"]), len(titles) - 1))]
            seo = parse_model(
                Seo,
                {
                    "title": title[:100],
                    "description": raw["description"],
                    "tags": raw["tags"][:20],
                    "hashtags": raw["hashtags"][:5],
                    "keywords": raw.get("keywords", []),
                },
                f"seo short {item['k']}",
            )
            final = finalize_seo(
                seo,
                primary_keyword=kw,
                fiction_label=ctx.channel.fiction_label,
                part_n=ctx.series.get("part_n"),
                parts_total=int(ctx.series.get("parts_total", 1)),
                master_title=master_title,
                creator_note=ctx.channel.creator_note,
            )
            out.append(final.model_dump())
        ctx.write_json("seo_shorts.json", out)


# --- expansion helper used by the duration check (C6 / C7) -------------------------------------------


def expansion_plan(story: Story, deficit_words: int) -> tuple[int, int]:
    """Which chapter to expand and by how many words: the shortest chapter, +120..200 words (spec 7.0)."""
    shortest = min(story.chapters, key=lambda c: (c.word_count, c.n))
    return shortest.n, max(120, min(200, deficit_words))


def sentence_index_map(old: Sequence[str], new: Sequence[str]) -> dict[int, int]:
    """Map old sentence indices to new ones for sentences that survive verbatim (order-preserving)."""
    sm = SequenceMatcher(None, list(old), list(new), autojunk=False)
    return {a + i: b + i for a, b, size in sm.get_matching_blocks() for i in range(size)}


def remap_index(i: int, mapping: dict[int, int], new_len: int) -> int:
    """New index of old sentence `i`: its match, else offset from the nearest matched neighbour."""
    if i in mapping:
        return mapping[i]
    before = [k for k in mapping if k < i]
    after = [k for k in mapping if k > i]
    if before:
        k = max(before)
        j = mapping[k] + (i - k)
    elif after:
        k = min(after)
        j = mapping[k] - (k - i)
    else:
        j = i
    return max(0, min(j, new_len - 1))


def _remap_shorts_plan(ctx: JobContext, n: int, mapping: dict[int, int], new_len: int) -> None:
    if not ctx.file("shorts_plan.json").exists():
        return
    items = ctx.read_json("shorts_plan.json")
    for it in items:
        if it["chapter"] == n:
            it["sentence_start"] = remap_index(it["sentence_start"], mapping, new_len)
            it["sentence_end"] = max(it["sentence_start"], remap_index(it["sentence_end"], mapping, new_len))
    ctx.write_json("shorts_plan.json", items)


def _remap_visual_plan(ctx: JobContext, n: int, mapping: dict[int, int], new_len: int) -> None:
    if not ctx.file("visual_plan.json").exists():
        return
    beats = ctx.read_json("visual_plan.json")
    mine = sorted((b for b in beats if b.get("chapter") == n), key=lambda b: b["sentence_start"])
    for pos, b in enumerate(mine):
        b["sentence_start"] = 0 if pos == 0 else remap_index(b["sentence_start"], mapping, new_len)
    for pos, b in enumerate(mine):
        b["sentence_end"] = (mine[pos + 1]["sentence_start"] - 1) if pos + 1 < len(mine) else new_len - 1
    ctx.write_json("visual_plan.json", beats)


def expand_chapter(ctx: JobContext, chapter_n: int, add_words: int) -> StoryChapter:
    """Lengthen chapter `chapter_n` by about `add_words` words and keep every plan consistent.

    Calls `expand.md` (existing sentences stay verbatim, new ones are inserted), rewrites `story.json`
    and the `stories` row, and remaps sentence indices in `shorts_plan.json` and `visual_plan.json`.
    The caller must hold the `llama` tenant and re-voice the chapter. Raises NS-E303 when the model adds
    less than half of `add_words` or breaks banned-phrase / reading-level / sentence-length rules.
    """
    story = load_story(ctx)
    idx = next((i for i, c in enumerate(story.chapters) if c.n == chapter_n), None)
    if idx is None:
        raise NsError("NS-E303", f"story has no chapter {chapter_n}")
    old = story.chapters[idx]
    beats: list[str] = []
    if ctx.file("plan.json").exists():
        plan = parse_model(Plan, ctx.read_json("plan.json"), "plan.json")
        beats = next((c.beats for c in plan.chapters if c.n == chapter_n), [])
    text = call_text(
        ctx,
        "expand",
        chapter={"n": old.n, "title": old.title},
        beats=beats,
        add_words=add_words,
        chapter_text=old.text,
    ).strip()
    profile = profile_of(ctx)
    added = word_count(text) - old.word_count
    if added < add_words // profile.expand_divisor:
        raise NsError("NS-E303", f"expansion of chapter {chapter_n} added {added} words, wanted {add_words}")
    checks = (
        check_banned(text, load_banned()),
        check_reading_level(text, profile=profile),
        check_sentence_length(text),
    )
    raise_if([v for v in checks if v is not None])
    if ctx.file("policy_text.json").exists():  # the text gate already ran: it never sees these new words
        from ns_studio.policy.text_checks import check_added_text

        check_added_text(ctx, text, old_text=old.text)
    is_last = idx == len(story.chapters) - 1
    new = build_chapter(old.n, old.title, text, old.summary, wpm=_wpm(ctx), is_last=is_last)
    mapping = sentence_index_map([s.text for s in old.sentences], [s.text for s in new.sentences])
    story.chapters[idx] = new
    ctx.write_json("story.json", story.model_dump())
    _remap_shorts_plan(ctx, chapter_n, mapping, len(new.sentences))
    _remap_visual_plan(ctx, chapter_n, mapping, len(new.sentences))
    full_text = "\n\n".join(c.text for c in story.chapters)
    ctx.db.execute(
        "update stories set text=?, word_count=? where job_id=?",
        (full_text, sum(c.word_count for c in story.chapters), ctx.job_id),
    )
    ctx.log.info("chapter_expanded", chapter=chapter_n, added=added, job_id=ctx.job_id)
    return new


for _st in (
    LongPlanStage(),
    ChaptersDraftStage(),
    ChaptersCriticStage(),
    ContinuityStage(),
    SeoLongStage(),
    ShortsSelectStage(),
    VisualPlanLongStage(),
    SeoShortsStage(),
):
    register(_st)
