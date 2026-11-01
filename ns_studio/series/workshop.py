"""Series Workshop operations (component C11; spec 23.3).

One method per Assistant tool. Every method reads and writes the story folder (`store.py`); nothing needs
the chat history. LLM calls go through the C5 prompt helpers against a scratch `JobContext` whose files
live under `data/workshop/<slug>/`, never inside the story folder.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import yaml
from pydantic import ValidationError

import ns_studio.series.schemas as _schemas  # noqa: F401  (registers the workshop JSON schemas)
from ns_common.config import load_channel, load_runtime
from ns_common.db import Db, new_job_id
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.models import Channel, Combo, Runtime
from ns_common.paths import Paths
from ns_common.textstats import split_sentences, word_count
from ns_studio.context import FAKEABLE, JobContext, Services, build_services
from ns_studio.errors import StudioError
from ns_studio.media.formats import design_formats, ensure_allowed, formats_allowed
from ns_studio.media.presets import PRESETS_DIR
from ns_studio.series.importer import build_artifacts
from ns_studio.series.policy import is_standalone, text_gate
from ns_studio.series.store import (
    DEFAULT_MINUTES,
    MAX_MINUTES,
    MIN_MINUTES,
    ConceptDoc,
    DesignDoc,
    PartEntry,
    PartState,
    PartStatus,
    ScriptChapter,
    ScriptDoc,
    SeriesDoc,
    ShortDoc,
    StatusDoc,
    StoryDir,
    WorkshopError,
    anchor_of,
    concept_text,
    list_slugs,
    normalize_text,
    stories_dir,
)
from ns_studio.text.budget import PACING_WPM, chapter_budget, distribute_words, short_body_words
from ns_studio.text.models_local import ChapterDraft
from ns_studio.text.prompts import call_json, call_text, parse_model
from ns_studio.text.stages_master import (
    DEFAULT_CLOSINGS,
    HOOK_CONSTRAINTS,
    pairwise_continuity,
    remap_index,
    resolve_moments,
    sentence_index_map,
    span_text,
)
from ns_studio.text.stages_shorts import critic_numbers
from ns_studio.text.validators import (
    Violation,
    check_banned,
    check_length,
    check_reading_level,
    check_sentence_length,
    load_banned,
)

CHAPTER_WORDS = 350  # target words per chapter: 20 minutes at 155 wpm gives 9 chapters (spec 23.3)
MIN_CHAPTERS = 5
MAX_CHAPTERS = 12
MAX_PARTS = 40
HARD_FLOOR = 0.75  # a part below this share of its word target is rejected (NS-E303)
SOFT_BAND = 0.2  # outside +-20 % of the target is only reported in notes.md
DEFAULT_FORMATS = ("short_9x16", "long_16x9", "square_1x1")
FEEDBACK = "Owner feedback"
UNCHANGED = re.compile(r"unchanged\.?", re.I)


@dataclass(frozen=True)
class ProduceRequest:
    """What `produce` hands to the enqueue callback for one approved part."""

    job_id: str
    channel: str
    fmt: str
    plan: str
    story_slug: str
    part_n: int
    parts_total: int
    series_title: str
    series_slug: str
    title: str
    short_titles: list[str]
    script: str
    shorts: list[str]
    concept: str
    design: dict[str, Any]
    end_card: str
    publish_not_before_days: int


EnqueueFn = Callable[[ProduceRequest], object]


def slugify(text: str, fallback: str = "story") -> str:
    """Lowercase dash slug of the first five words of `text` (at most 40 characters)."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    return "-".join(words[:5])[:40].strip("-") or fallback


def chapter_count(target_words: int) -> int:
    """Chapters for a part: about 350 words each, never fewer than 5 or more than 12."""
    return max(MIN_CHAPTERS, min(MAX_CHAPTERS, round(target_words / CHAPTER_WORDS)))


def available_formats() -> list[str]:
    """Format ids a design can pick: the short, long and square presets (else the three shipped ids).

    Per-clip layouts (`clip_*`) are chosen by the clipper, not by a series design.
    """
    try:
        found = design_formats()
    except StudioError:
        found = []
    return found or list(DEFAULT_FORMATS)


def _preset_keys(name: str) -> list[str]:
    data = yaml.safe_load((PRESETS_DIR / name).read_text(encoding="utf-8")) or {}
    return sorted(str(k) for k in data)


def design_options(channel: Channel) -> dict[str, list[str]]:
    """Allowed values per design field; the channel's `formats_allowed` narrows `format` when it is set."""
    formats = available_formats()
    allowed = getattr(channel, "formats_allowed", None)
    if allowed:
        formats = [f for f in formats if f in allowed]
    return {
        "format": formats,
        "visual_treatment": _preset_keys("visual_treatments.yaml"),
        "caption_style": _preset_keys("caption_styles.yaml"),
    }


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _concept_from(
    item: dict[str, Any], letter: str, *, revision: int = 0, feedback: list[str] | None = None
) -> ConceptDoc:
    return ConceptDoc(
        letter=letter,
        title=str(item["title"]).strip(),
        premise=str(item["premise"]).strip(),
        cast=[str(c) for c in item.get("cast", [])],
        setting=str(item.get("setting", "")),
        tone=str(item.get("tone", "")),
        beats=[str(b) for b in item.get("beats", [])],
        why_it_works=str(item.get("why_it_works", "")),
        series_potential=str(item.get("series_potential", "")),
        audience=str(item.get("audience", "")),
        risks=[str(r) for r in item.get("risks", [])],
        feedback=feedback or [],
        revision=revision,
    )


def _violations(violations: list[Violation | None]) -> list[Violation]:
    return [v for v in violations if v is not None]


def _fail_hard(n: int, text: str) -> None:
    """NS-E303 when the chapter has a banned phrase or a sentence over 35 words."""
    found = _violations([check_banned(text, load_banned()), check_sentence_length(text)])
    if found:
        raise NsError("NS-E303", f"chapter {n}: " + "; ".join(f"{v.rule}: {v.detail}" for v in found))


def _bar_met(avg: float, low: float, bar: float) -> bool:
    return avg >= bar and low >= 6


class Workshop:
    """The Series Workshop over one Nightshift home: services in, story folders out."""

    def __init__(
        self,
        paths: Paths,
        services: Services,
        runtime: Runtime,
        db: Db,
        log: NsLogger,
        enqueue: EnqueueFn | None = None,
    ) -> None:
        self.paths = paths
        self.services = services
        self.runtime = runtime
        self.db = db
        self.log = log
        self.enqueue = enqueue

    # --- plumbing ---------------------------------------------------------------------------------

    def channel(self, ref: Channel | str) -> Channel:
        """The channel model: given as is, or loaded from `config/channels/<slug>.yaml`."""
        if isinstance(ref, Channel):
            return ref
        try:
            return load_channel(self.paths, ref)
        except FileNotFoundError as e:
            raise WorkshopError(f"unknown channel {ref!r}; create it with `studio channel add`") from e

    def _open(self, slug: str) -> tuple[StoryDir, StatusDoc, Channel]:
        sd = StoryDir.open(self.paths, slug)
        status = sd.read_status()
        return sd, status, self.channel(status.channel)

    def _ctx(self, channel: Channel, slug: str, tag: str) -> JobContext:
        """Scratch `JobContext` for prompt calls: neutral combo, job dir under `data/workshop/`."""
        pools = channel.pools

        def first(options: list[str], default: str) -> str:
            return options[0] if options else default

        combo = Combo(
            combo_id=f"workshop-{slug}",
            hook=first(pools.hooks, "cold_open"),
            structure=first(pools.structures, "twist"),
            pov_tone=first(pools.pov_tones, "first_person_intimate"),
            pacing=first(pools.pacings, "standard"),
            caption_style=first(pools.caption_styles, "clean_white"),
            visual_treatment=first(pools.visual_treatments, "dark_slowzoom"),
            title_formula=first(pools.title_formulas, "question"),
        )
        job_dir = self.paths.data / "workshop" / slug / tag
        job_dir.mkdir(parents=True, exist_ok=True)
        return JobContext(
            job_id=f"{slug}-{tag}",
            channel=channel,
            combo=combo,
            fmt="master_first",
            paths=self.paths,
            job_dir=job_dir,
            db=self.db,
            runtime=self.runtime,
            services=self.services,
            log=self.log,
            prompt_versions={},
        )

    @contextmanager
    def _llm(self, slug: str) -> Iterator[None]:
        """Hold the `llama` tenant while prompts run."""
        owner = f"workshop:{slug}"
        self.services.acquirer.acquire("llama", owner, model_alias="story")
        try:
            yield
        finally:
            self.services.acquirer.release("llama", owner)

    def _set_part(
        self,
        sd: StoryDir,
        status: StatusDoc,
        n: int,
        new: PartStatus,
        *,
        job_id: str | None = None,
        words: int | None = None,
    ) -> None:
        """Set part `n` to `new` in `status.yaml` and `series.md`, and re-derive the story status."""
        st = status.parts.get(n, PartState())
        status.parts[n] = PartState.model_validate(
            {
                "status": new,
                "job_id": job_id if job_id is not None else st.job_id,
                "words": words if words is not None else st.words,
                "updated": _today(),
            }
        )
        status.recompute()
        sd.write_status(status)
        series = sd.read_series()
        series.entry(n).status = new
        sd.write_series(series)

    def _numbers(self, which: int | str, series: SeriesDoc) -> list[int]:
        total = len(series.parts)
        if isinstance(which, str) and which.lower() == "all":
            return list(range(1, total + 1))
        try:
            n = int(which)
        except ValueError as e:
            raise WorkshopError(f"part must be a number 1-{total} or 'all', not {which!r}") from e
        series.entry(n)
        return [n]

    # --- steps 1 and 2: start, concepts, regenerate, choose ---------------------------------------

    def start(self, channel: Channel | str, premise: str | None = None, *, slug: str | None = None) -> str:
        """Create the story folder with a `concept.md` skeleton and `status.yaml`; returns the slug.

        A given premise goes through the policy text gate first (NS-E601/E602), so an unsafe idea creates
        nothing. The slug comes from the premise unless `slug` is given; clashes get `-2`, `-3`, ...
        """
        ch = self.channel(channel)
        premise = (premise or "").strip()
        if premise:
            with self._llm(slug or "new"):
                text_gate(self._ctx(ch, slug or "new", "start"), premise, what="premise")
        base = slug or (slugify(premise) if premise else f"{ch.slug}-story")
        final, i = base, 2
        while (stories_dir(self.paths) / final).exists():
            final, i = f"{base}-{i}", i + 1
        sd = StoryDir.open(self.paths, final, must_exist=False)
        sd.root.mkdir(parents=True, exist_ok=True)
        sd.write_concept(ConceptDoc(title="Untitled", premise=premise))
        sd.write_status(StatusDoc(slug=final, channel=ch.slug, created=_today()))
        self.log.info("workshop_start", slug=final, channel=ch.slug)
        return final

    def concepts(self, slug: str, n: int = 3) -> list[str]:
        """Write `n` concept drafts `concept-A.md` ...; returns their letters.

        Every draft goes through the policy text gate before anything is written; the first rejection
        raises (NS-E601/E602/E603) and leaves the folder untouched.
        """
        if not 1 <= n <= 6:
            raise WorkshopError("n must be between 1 and 6")
        sd, _status, ch = self._open(slug)
        start = sd.read_concept()
        old = [sd.read_concept(sd.draft(x)).title for x in sd.draft_letters()]
        with self._llm(slug):
            ctx = self._ctx(ch, slug, "concepts")
            out = call_json(ctx, "workshop_concepts", n=n, premise=start.premise, avoid_titles=old)
            items = list(out["concepts"])[:n]
            if not items:
                raise NsError("NS-E302", "workshop_concepts returned no concepts")
            docs = [_concept_from(it, chr(ord("A") + i)) for i, it in enumerate(items)]
            for d in docs:
                text_gate(ctx, concept_text(d), what=f"concept {d.letter}", titles=[d.title])
        for letter in sd.draft_letters():
            sd.draft(letter).unlink()
        for d in docs:
            sd.write_concept(d, sd.draft(d.letter or "A"))
        self.log.info("workshop_concepts", slug=slug, letters=[d.letter for d in docs])
        return [d.letter or "A" for d in docs]

    def regenerate(self, slug: str, letter: str, feedback: str) -> str:
        """Rewrite one draft with the owner's notes; other drafts stay. Returns the letter."""
        letter = letter.strip().upper()
        if not feedback.strip():
            raise WorkshopError("feedback is empty; say what should change")
        sd, _status, ch = self._open(slug)
        path = sd.draft(letter)
        if not path.exists():
            raise WorkshopError(f"story {slug!r} has no draft {letter}; drafts: {sd.draft_letters()}")
        cur = sd.read_concept(path)
        with self._llm(slug):
            ctx = self._ctx(ch, slug, "regenerate")
            out = call_json(
                ctx, "workshop_regenerate", concept=cur.model_dump(), feedback=feedback.strip(), letter=letter
            )
            items = list(out["concepts"])
            if not items:
                raise NsError("NS-E302", "workshop_regenerate returned no concept")
            doc = _concept_from(
                items[0], letter, revision=cur.revision + 1, feedback=[*cur.feedback, feedback.strip()]
            )
            text_gate(ctx, concept_text(doc), what=f"concept {letter}", titles=[doc.title])
        sd.write_concept(doc, path)
        return letter

    def choose(self, slug: str, letter: str) -> str:
        """Promote draft `letter` to `concept.md` (gated again: the owner may have edited it)."""
        letter = letter.strip().upper()
        sd, status, ch = self._open(slug)
        path = sd.draft(letter)
        if not path.exists():
            raise WorkshopError(f"story {slug!r} has no draft {letter}; drafts: {sd.draft_letters()}")
        doc = sd.read_concept(path)
        with self._llm(slug):
            text_gate(self._ctx(ch, slug, "choose"), concept_text(doc), what=f"concept {letter}",
                      titles=[doc.title])
        sd.write_concept(doc)
        status.chosen = letter
        sd.write_status(status)
        return doc.title

    # --- step 3: the part map -------------------------------------------------------------------

    def map_parts(self, slug: str, parts: int, minutes_per_part: int = DEFAULT_MINUTES) -> SeriesDoc:
        """Write `series.md`: `parts` rows with hook, cliff, reveals and runtime (12-25 minutes each).

        The model also checks that each part stands alone for a new viewer (one-line recap allowed) and
        that the arc pays off; open problems are listed in `warnings`. Raises WorkshopError when the
        numbers are out of range or parts were already written.
        """
        if not MIN_MINUTES <= minutes_per_part <= MAX_MINUTES:
            raise WorkshopError(
                f"minutes_per_part must be {MIN_MINUTES}-{MAX_MINUTES}, got {minutes_per_part}"
            )
        if not 1 <= parts <= MAX_PARTS:
            raise WorkshopError(f"parts must be 1-{MAX_PARTS}, got {parts}")
        sd, status, ch = self._open(slug)
        concept = sd.read_concept()
        if not concept.premise.strip() or concept.title == "Untitled":
            raise WorkshopError("choose a concept first (`studio series choose`)")
        if any(p.status != "planned" for p in status.parts.values()):
            raise WorkshopError("parts are already written; edit series.md by hand instead of re-mapping")
        with self._llm(slug):
            ctx = self._ctx(ch, slug, "map")
            out = call_json(ctx, "workshop_map", concept=concept.model_dump(), parts=parts,
                            minutes=minutes_per_part)
            raw: list[dict[str, Any]] = list(out["parts"])
            if len(raw) != parts:
                raise NsError("NS-E302", f"workshop_map returned {len(raw)} parts, expected {parts}")
            entries: list[PartEntry] = []
            warnings: list[str] = []
            for i, p in enumerate(raw, start=1):
                minutes = int(p.get("minutes") or minutes_per_part)
                if not MIN_MINUTES <= minutes <= MAX_MINUTES:
                    minutes = minutes_per_part
                entries.append(
                    PartEntry(
                        n=i,
                        title=str(p["title"]).strip(),
                        hook=str(p["hook"]).strip(),
                        cliff=str(p["cliff"]).strip(),
                        reveals=str(p.get("reveals", "")).strip(),
                        minutes=minutes,
                        recap=str(p.get("recap", "")).strip() if i > 1 else "",
                    )
                )
                if p.get("standalone") is False:
                    warnings.append(f"Part {i} may not stand alone for a new viewer; sharpen its recap")
            warnings += [str(x) for x in out.get("issues", [])]
            doc = SeriesDoc(
                title=str(out.get("series_title") or concept.title).strip(),
                channel=ch.slug,
                minutes_per_part=minutes_per_part,
                arc_payoff=str(out.get("arc_payoff", "")).strip(),
                warnings=warnings,
                parts=entries,
            )
            blob = "\n".join(
                [doc.title, doc.arc_payoff, *[f"{e.title}. {e.hook} {e.cliff} {e.reveals}" for e in entries]]
            )
            text_gate(ctx, blob, what="series map", titles=[doc.title, *[e.title for e in entries]])
        sd.write_series(doc)
        status.parts = {e.n: PartState(updated=_today()) for e in entries}
        status.recompute()
        sd.write_status(status)
        return doc

    # --- step 4: write and revise a part ---------------------------------------------------------

    def _select_shorts(self, ctx: JobContext, story: Any, wpm: int) -> list[dict[str, Any]]:
        """M7 for a part: moments, five hooks and a judge each, a closing line; distinct hooks."""
        buckets = sorted(ctx.channel.length_buckets)
        cmin, cmax = ctx.channel.shorts_per_master
        raw = call_json(
            ctx,
            "shorts_select",
            count_min=cmin,
            count_max=cmax,
            buckets=[
                {
                    "seconds": b,
                    "body_words": short_body_words(b, wpm),
                    "min_words": round(short_body_words(b, wpm) * 0.8),
                    "max_words": round(short_body_words(b, wpm) * 1.2),
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
            raw["moments"], story, buckets=buckets, wpm=wpm, count_min=cmin, count_max=cmax
        )
        used: set[str] = set()
        out: list[dict[str, Any]] = []
        for k, m in enumerate(moments, start=1):
            text = span_text(story, m["chapter"], m["sentence_start"], m["sentence_end"])
            chapter = next(c for c in story.chapters if c.n == m["chapter"])
            answer = call_json(
                ctx, "short_hook", fixture=f"short_hook_{k}", span_text=text,
                why_standalone=m["why_standalone"], chapter_title=chapter.title,
            )
            hooks = [str(h).strip() for h in answer["hooks"] if 0 < len(str(h).split()) <= 12]
            hooks = hooks or [" ".join(split_sentences(text)[0].split()[:12])]
            judged = call_json(
                ctx, "judge",
                purpose="Pick the hook most likely to stop a scroll while staying true to the moment.",
                constraints=HOOK_CONSTRAINTS.format(max=12, style=ctx.combo.hook),
                options=hooks,
            )
            first = hooks[max(0, min(int(judged["index"]), len(hooks) - 1))]
            ordered = [first, *[h for h in hooks if h != first]]
            chosen = next((h for h in ordered if h.lower() not in used), ordered[0])
            used.add(chosen.lower())
            closing = str(answer["closing_line"]).strip()
            if not closing or len(closing.split()) > 15:
                closing = DEFAULT_CLOSINGS[(k - 1) % len(DEFAULT_CLOSINGS)]
            if not is_standalone(ctx, story=chapter.text, moment=text):
                self.log.warn("short_not_standalone", slug=ctx.job_id, k=k)
                continue
            out.append({**m, "text": text, "hook": chosen, "hooks": hooks[:5], "closing": closing})
        if len(out) < cmin:
            raise NsError("NS-E607", f"only {len(out)} stand-alone Shorts found, need {cmin}")
        return out

    def write_part(self, slug: str, n: int) -> dict[str, Any]:
        """Write `full/script.md`, `shorts/sNN.md` and `notes.md` of part `n` through the text stages.

        Steps: chapter outline at the part's runtime budget, chapter drafts with the C5 `chapter_draft`
        prompt, critic/revise (at most 2 revisions each), continuity pass, hard validators, the policy gate
        on the script (NS-E601/E602/E604), then Shorts selection (M7/M8 pieces). Nothing is written when a
        step fails. Returns a summary dict.
        """
        sd, status, ch = self._open(slug)
        series = sd.read_series()
        entry = series.entry(n)
        if status.parts.get(n, PartState()).status in ("producing", "published"):
            raise WorkshopError(f"part {n} is already in production; it cannot be rewritten")
        concept = sd.read_concept()
        with self._llm(slug):
            ctx = self._ctx(ch, slug, f"part-{n:02d}")
            wpm = PACING_WPM.get(ctx.combo.pacing, 155)
            target = entry.minutes * wpm
            plan = self._outline(ctx, concept, series, entry, target, wpm)
            drafted = self._draft(ctx, concept, series, entry, plan, wpm)
            self._continuity(ctx, drafted)
            total = sum(word_count(c["text"]) for c in drafted)
            warnings = self._check_part(drafted, target, wpm, total)
            script = ScriptDoc(
                part=n,
                title=entry.title,
                critic_avg=sum(c["avg"] for c in drafted) / len(drafted),
                chapters=[
                    ScriptChapter(
                        n=c["n"], title=c["title"], text=c["text"], summary=c["summary"],
                        beats=c["beats"], cliff=c["cliff"],
                    )
                    for c in drafted
                ],
            )
            full_text = "\n\n".join(c.text for c in script.chapters)
            text_gate(ctx, full_text, what=f"part {n} script", titles=[entry.title], title=entry.title,
                      hook=entry.hook)
            story = build_artifacts(script, [], wpm=wpm, combo_id=ctx.combo.combo_id).story
            picked = self._select_shorts(ctx, story, wpm)
        shorts = self._short_docs(script, picked)
        sd.write_script(n, script)
        sd.clear_shorts(n)
        for s in shorts:
            sd.write_short(n, s)
        self._write_notes(sd, n, drafted, warnings, shorts)
        self._set_part(sd, status, n, "written", words=total)
        self.log.info("workshop_write_part", slug=slug, part=n, words=total, shorts=len(shorts))
        return {
            "slug": slug,
            "part": n,
            "script": str(sd.script(n)),
            "words": total,
            "chapters": len(script.chapters),
            "shorts": [str(sd.short_file(n, s.k)) for s in shorts],
            "warnings": warnings,
        }

    def _outline(
        self, ctx: JobContext, concept: ConceptDoc, series: SeriesDoc, entry: PartEntry, target: int, wpm: int
    ) -> list[dict[str, Any]]:
        k = chapter_count(target)
        out = call_json(
            ctx,
            "workshop_part_outline",
            concept=concept.model_dump(),
            series={"title": series.title, "parts": [p.model_dump() for p in series.parts]},
            part=entry.model_dump(),
            previous=[p.model_dump() for p in series.parts if p.n < entry.n],
            budget={"minutes": entry.minutes, "words": target, "wpm": wpm, "chapters": k,
                    "chapter_words": target // k},
        )
        raw: list[dict[str, Any]] = list(out["chapters"])
        if not MIN_CHAPTERS <= len(raw) <= MAX_CHAPTERS:
            raise NsError("NS-E302", f"workshop_part_outline returned {len(raw)} chapters, expected 5-12")
        budgets = distribute_words(target, len(raw))
        return [
            {
                "n": i,
                "title": str(c["title"]).strip(),
                "beats": [str(b) for b in c["beats"]],
                "cliff": str(c["cliff"]).strip(),
                "word_budget": budgets[i - 1],
            }
            for i, c in enumerate(raw, start=1)
        ]

    def _draft(
        self,
        ctx: JobContext,
        concept: ConceptDoc,
        series: SeriesDoc,
        entry: PartEntry,
        plan: list[dict[str, Any]],
        wpm: int,
    ) -> list[dict[str, Any]]:
        """Draft, critique and revise chapter by chapter (C5 prompts `chapter_draft`, `critic`, `revise`)."""
        idea = {
            "premise": (
                f"{concept.premise} This is part {entry.n} of {len(series.parts)} of the series "
                f'"{series.title}". {entry.hook}'
            ),
            "twist": entry.reveals,
        }
        summaries: list[dict[str, Any]] = [
            {"n": f"Part {p.n}", "title": p.title, "summary": f"{p.reveals} It ended on: {p.cliff}"}
            for p in series.parts
            if p.n < entry.n
        ]
        bar = ctx.channel.critic_bar
        previous = ""
        drafted: list[dict[str, Any]] = []
        for c in plan:
            raw = call_json(
                ctx,
                "chapter_draft",
                fixture=f"chapter_draft_{c['n']}",
                idea=idea,
                plan={"chapters": plan},
                chapter=c,
                summaries=summaries,
                last_sentences=split_sentences(previous)[-2:] if previous else [],
                budget=chapter_budget(c["word_budget"], wpm),
            )
            draft = parse_model(ChapterDraft, raw, f"chapter_draft {c['n']}")
            text = normalize_text(draft.text)
            revisions, avg, low = 0, 0.0, 0.0
            critique: dict[str, Any] = {}
            for attempt in range(3):
                critique = call_json(
                    ctx,
                    "critic",
                    story=text,
                    banned=load_banned(),
                    scope=f"chapter {c['n']} of {len(plan)} of part {entry.n} of a longer series",
                )
                avg, low = critic_numbers(critique)
                if _bar_met(avg, low, bar) or attempt == 2:
                    break
                text = normalize_text(
                    call_text(ctx, "revise", fixture="workshop_revise_fix", story=text,
                              fixes=critique["fixes"]).strip()
                )
                revisions += 1
            summaries.append({"n": c["n"], "title": c["title"], "summary": draft.summary.strip()})
            previous = text
            drafted.append(
                {
                    **c,
                    "text": text,
                    "summary": draft.summary.strip(),
                    "avg": avg,
                    "low": low,
                    "revisions": revisions,
                    "below_bar": not _bar_met(avg, low, bar),
                    "critique": critique,
                }
            )
        return drafted

    def _continuity(self, ctx: JobContext, drafted: list[dict[str, Any]]) -> None:
        """Whole-part (or pairwise) continuity pass; each affected chapter gets one `revise`."""
        pairwise = pairwise_continuity(ctx)
        groups = [drafted[i : i + 2] for i in range(len(drafted) - 1)] if pairwise else [drafted]
        fixes: dict[int, list[str]] = {}
        issues: list[dict[str, Any]] = []
        by_n = {c["n"]: c for c in drafted}
        for group in groups:
            scope = f"chapters {group[0]['n']} and {group[-1]['n']}" if pairwise else "the whole part"
            out = call_json(
                ctx, "continuity", scope=scope,
                chapters=[{"n": c["n"], "title": c["title"], "text": c["text"]} for c in group],
            )
            for issue in out["issues"]:
                n = int(issue["chapter"])
                if n in by_n:
                    fixes.setdefault(n, []).append(str(issue["fix"]))
                    issues.append({"chapter": n, "problem": issue["problem"], "fix": issue["fix"]})
        for n, chapter_fixes in sorted(fixes.items()):
            by_n[n]["text"] = normalize_text(
                call_text(ctx, "revise", fixture="workshop_revise_fix", story=by_n[n]["text"],
                          fixes=chapter_fixes).strip()
            )
        for c in drafted:
            c["continuity"] = [i for i in issues if i["chapter"] == c["n"]]

    def _check_part(
        self, drafted: list[dict[str, Any]], target: int, wpm: int, total: int
    ) -> list[str]:
        """Hard validators raise NS-E303; soft ones (reading level, lengths) are returned as notes."""
        warnings: list[str] = []
        for c in drafted:
            _fail_hard(c["n"], c["text"])
            soft = _violations(
                [
                    check_length(c["text"], chapter_budget(c["word_budget"], wpm)),
                    check_reading_level(c["text"]),
                ],
            )
            warnings += [f"chapter {c['n']}: {v.rule}: {v.detail}" for v in soft]
            if c["below_bar"]:
                warnings.append(f"chapter {c['n']}: critic below the bar after 2 revisions ({c['avg']:.1f})")
        if total < target * HARD_FLOOR:
            raise NsError("NS-E303", f"part has {total} words, the target is {target}")
        if not target * (1 - SOFT_BAND) <= total <= target * (1 + SOFT_BAND):
            warnings.append(f"part length {total} words is outside +-20 % of the target {target}")
        return warnings

    def _short_docs(self, script: ScriptDoc, picked: list[dict[str, Any]]) -> list[ShortDoc]:
        """Shorts files from the picked moments: chapter-local spans become global sentence numbers."""
        flat = script.sentences()
        offsets: dict[int, int] = {}
        for pos in range(len(flat) - 1, -1, -1):
            offsets[flat[pos][0]] = pos
        docs: list[ShortDoc] = []
        for k, m in enumerate(picked, start=1):
            a = offsets[m["chapter"]] + m["sentence_start"]
            b = offsets[m["chapter"]] + m["sentence_end"]
            docs.append(
                ShortDoc(
                    k=k,
                    hook=m["hook"],
                    span=[a + 1, b + 1],
                    closing_line=m["closing"],
                    length_bucket=m["length_bucket"],
                    chapter=m["chapter"],
                    why_standalone=m["why_standalone"],
                    hooks=m["hooks"],
                    start_text=anchor_of(flat[a][2]),
                    text=m["text"],
                )
            )
        return docs

    def _write_notes(
        self,
        sd: StoryDir,
        n: int,
        drafted: list[dict[str, Any]],
        warnings: list[str],
        shorts: list[ShortDoc],
    ) -> None:
        old = sd.read_notes(n)
        scores = []
        for c in drafted:
            detail = ", ".join(f"{k} {v}" for k, v in c["critique"].get("scores", {}).items())
            scores.append(f"- Chapter {c['n']}: avg {c['avg']:.1f}, low {c['low']:.0f}, "
                          f"{c['revisions']} revision(s) ({detail})")
        cont = [f"- Chapter {i['chapter']}: {i['problem']} Fix: {i['fix']}"
                for c in drafted for i in c.get("continuity", [])]
        sections: dict[str, str] = {
            "Critic scores": "\n".join(scores),
            "Continuity": "\n".join(cont) or "- no contradictions found",
            "Validator warnings": "\n".join(f"- {w}" for w in warnings) or "- none",
            "Shorts": "\n".join(f"- s{s.k:02d} ({s.length_bucket}s): {s.hook}" for s in shorts),
            FEEDBACK: old.get(FEEDBACK, ""),
        }
        for key, text in old.items():
            sections.setdefault(key, text)
        sd.write_notes(n, sections)

    def revise_part(self, slug: str, n: int, feedback: str) -> dict[str, Any]:
        """Apply the owner's notes to part `n`'s script and nothing else; returns the changed chapters.

        Shorts keep their hooks; their spans follow the sentences that survive verbatim. The part goes back
        to `written` and needs approving again. The notes are appended to `notes.md`.
        """
        feedback = feedback.strip()
        if not feedback:
            raise WorkshopError("feedback is empty; say what should change")
        sd, status, ch = self._open(slug)
        if status.parts.get(n, PartState()).status in ("producing", "published"):
            raise WorkshopError(f"part {n} is already in production; it cannot be revised")
        script = sd.read_script(n)
        old_flat = [t for _, _, t in script.sentences()]
        changed: list[int] = []
        with self._llm(slug):
            ctx = self._ctx(ch, slug, f"part-{n:02d}-revise")
            chapters: list[ScriptChapter] = []
            for c in script.chapters:
                answer = call_text(
                    ctx, "workshop_revise", fixture="workshop_revise",
                    chapter={"n": c.n, "title": c.title, "text": c.text}, feedback=feedback,
                ).strip()
                new_text = normalize_text(answer) if answer and not UNCHANGED.fullmatch(answer) else c.text
                if new_text != c.text:
                    _fail_hard(c.n, new_text)
                    changed.append(c.n)
                chapters.append(c.model_copy(update={"text": new_text}))
            if changed:
                script = script.model_copy(update={"chapters": chapters})
                text_gate(ctx, "\n\n".join(c.text for c in chapters), what=f"part {n} script",
                          titles=[script.title])
        shorts = self._remap_shorts(sd, n, script, old_flat) if changed else sd.read_shorts(n)
        if changed:
            sd.write_script(n, script)
            sd.clear_shorts(n)
            for s in shorts:
                sd.write_short(n, s)
            self._set_part(sd, status, n, "written", words=script.words())
        notes = sd.read_notes(n)
        line = f"- {_today()}: {feedback} (chapters changed: {', '.join(map(str, changed)) or 'none'})"
        notes[FEEDBACK] = "\n".join(x for x in (notes.get(FEEDBACK, ""), line) if x)
        sd.write_notes(n, notes)
        return {"slug": slug, "part": n, "changed_chapters": changed}

    def _remap_shorts(self, sd: StoryDir, n: int, script: ScriptDoc, old_flat: list[str]) -> list[ShortDoc]:
        """Move every Short's span onto the same sentences in the revised script (kept in one chapter)."""
        flat = script.sentences()
        new_texts = [t for _, _, t in flat]
        mapping = sentence_index_map(old_flat, new_texts)
        out: list[ShortDoc] = []
        for s in sd.read_shorts(n):
            a = remap_index(s.span[0] - 1, mapping, len(new_texts))
            b = max(a, remap_index(s.span[1] - 1, mapping, len(new_texts)))
            while b > a and flat[b][0] != flat[a][0]:
                b -= 1
            out.append(
                s.model_copy(
                    update={
                        "span": [a + 1, b + 1],
                        "chapter": flat[a][0],
                        "start_text": anchor_of(new_texts[a]),
                        "text": " ".join(new_texts[a : b + 1]),
                    }
                )
            )
        return out

    # --- steps 5 and 6: approve, design, produce -------------------------------------------------

    def _pick(
        self, status: StatusDoc, series: SeriesDoc, which: int | str, allowed: tuple[str, ...], action: str
    ) -> list[int]:
        """Part numbers for `which` whose status is in `allowed`; WorkshopError names the first misfit."""

        def state(n: int) -> str:
            return status.parts.get(n, PartState()).status

        nums = self._numbers(which, series)
        needs = " or ".join(allowed)
        if isinstance(which, str) and which.lower() == "all":
            nums = [n for n in nums if state(n) in allowed]
            if not nums:
                raise WorkshopError(f"no part can {action} yet; a part needs status {needs}")
            return nums
        bad = [n for n in nums if state(n) not in allowed]
        if bad:
            raise WorkshopError(f"part {bad[0]} cannot {action}: status is {state(bad[0])}, needs {needs}")
        return nums

    def approve(self, slug: str, which: int | str = "all") -> StatusDoc:
        """Approve part `which` (or every written part with 'all') after re-checking the files.

        The script goes through the policy gate again (the owner may have edited it) and the Shorts files
        are validated against it. Returns the new `status.yaml` content.
        """
        sd, status, ch = self._open(slug)
        series = sd.read_series()
        nums = self._pick(status, series, which, ("written", "approved"), "approve")
        with self._llm(slug):
            ctx = self._ctx(ch, slug, "approve")
            wpm = PACING_WPM.get(ctx.combo.pacing, 155)
            for n in nums:
                script = sd.read_script(n)
                build_artifacts(script, sd.read_shorts(n), wpm=wpm, combo_id=ctx.combo.combo_id)
                text_gate(ctx, "\n\n".join(c.text for c in script.chapters), what=f"part {n} script",
                          titles=[script.title])
        for n in nums:
            self._set_part(sd, status, n, "approved", words=sd.read_script(n).words())
        return sd.read_status()

    def design(self, slug: str, **fields: Any) -> DesignDoc:
        """Merge `fields` into `design.yaml` after validating them; returns the new design.

        Fields: format, visual_treatment, caption_style, voice (speaker name or a Voice mapping),
        thumbnail_style, publish_cadence_days. `format` must be in the channel's `formats_allowed` (all
        formats when the channel has no such list); styles must exist in the preset files.
        """
        sd, _status, ch = self._open(slug)
        data = (sd.read_design() or DesignDoc()).model_dump()
        for key, value in fields.items():
            if value is None:
                continue
            if key == "voice":
                data["voice"] = {**data["voice"], **({"speaker": value} if isinstance(value, str) else value)}
            elif key in DesignDoc.model_fields:
                data[key] = value
            else:
                known = list(DesignDoc.model_fields)
                raise WorkshopError(f"unknown design field {key!r}; choose from {known}")
        try:
            doc = DesignDoc.model_validate(data)
        except ValidationError as e:
            raise WorkshopError(f"invalid design: {str(e)[:300]}") from e
        try:
            ensure_allowed(doc.format, formats_allowed(ch), channel=ch.slug)
        except StudioError as e:
            raise WorkshopError(f"{e.code}: {e.message} {e.detail}") from e
        options = design_options(ch)
        for key, allowed in options.items():
            if getattr(doc, key) not in allowed:
                raise WorkshopError(f"{key} {getattr(doc, key)!r} is not allowed; choose from {allowed}")
        sd.write_design(doc)
        return doc

    def design_choices(self, slug: str) -> dict[str, list[str]]:
        """The allowed values per design field for the story's channel."""
        _sd, _status, ch = self._open(slug)
        return design_options(ch)

    def produce(
        self, slug: str, which: int | str = "all", *, enqueue: EnqueueFn | None = None
    ) -> list[dict[str, Any]]:
        """Enqueue one master-first job per approved part, using the script files as the story source.

        Each job directory gets `script_source.json` (read by stage `script_import`) and
        `series_context.json` (`{series_slug, series_title, part_n}` for the outbox paths), then the
        `enqueue` callback is called with a `ProduceRequest`. Parts are spaced by the design's
        `publish_cadence_days`. All files are validated before the first job is enqueued.
        """
        sd, status, ch = self._open(slug)
        series = sd.read_series()
        design = sd.read_design()
        if design is None:
            raise WorkshopError("no design yet; run `studio series design` first")
        fn = enqueue or self.enqueue or make_db_enqueue(self.db)
        nums = self._pick(status, series, which, ("approved",), "produce")
        wpm = PACING_WPM.get(ch.pools.pacings[0] if ch.pools.pacings else "standard", 155)
        for n in nums:
            build_artifacts(sd.read_script(n), sd.read_shorts(n), wpm=wpm, combo_id=f"workshop-{slug}")
        series_slug = slugify(series.title)
        results: list[dict[str, Any]] = []
        for idx, n in enumerate(nums):
            shorts = sd.read_shorts(n)
            title = f"{series.title} - Part {n:02d}"
            nxt = next((p for p in series.parts if p.n == n + 1), None)
            req = ProduceRequest(
                job_id=new_job_id(ch.slug),
                channel=ch.slug,
                fmt="master_first",
                plan="master_from_script",
                story_slug=slug,
                part_n=n,
                parts_total=len(series.parts),
                series_title=series.title,
                series_slug=series_slug,
                title=title,
                short_titles=[f"{title} - Short {s.k:02d}" for s in shorts],
                script=str(sd.script(n)),
                shorts=[str(sd.short_file(n, s.k)) for s in shorts],
                concept=str(sd.concept),
                design=design.model_dump(),
                end_card=f"Next: Part {nxt.n}, {nxt.title}." if nxt else "That was the final part.",
                publish_not_before_days=idx * design.publish_cadence_days,
            )
            self._prepare_job(req)
            fn(req)
            self._set_part(sd, status, n, "producing", job_id=req.job_id)
            results.append({"part": n, "job_id": req.job_id, "title": title})
        return results

    def _prepare_job(self, req: ProduceRequest) -> None:
        """Create the job directory with `script_source.json` and `series_context.json`."""
        d = self.paths.job_dir(req.job_id)
        d.mkdir(parents=True, exist_ok=True)
        source = {"kind": "script_import", **asdict(req)}
        (d / "script_source.json").write_text(json.dumps(source, indent=2, ensure_ascii=False) + "\n",
                                              encoding="utf-8")
        context = {"series_slug": req.series_slug, "series_title": req.series_title, "part_n": req.part_n}
        (d / "series_context.json").write_text(json.dumps(context, indent=2) + "\n", encoding="utf-8")

    def mark_published(self, slug: str, n: int) -> StatusDoc:
        """Record that part `n` was published (called by the learning loop or by hand)."""
        sd, status, _ch = self._open(slug)
        sd.read_series().entry(n)
        self._set_part(sd, status, n, "published")
        return sd.read_status()

    # --- read-only views -------------------------------------------------------------------------

    def status(self, slug: str) -> dict[str, Any]:
        """Story status, part table, drafts, design flag and the suggested next step."""
        sd = StoryDir.open(self.paths, slug)
        st = sd.read_status()
        series = sd.read_series() if sd.series.exists() else None
        parts = []
        if series:
            for e in series.parts:
                ps = st.parts.get(e.n, PartState())
                parts.append(
                    {
                        "n": e.n, "title": e.title, "minutes": e.minutes, "status": ps.status,
                        "job_id": ps.job_id, "words": ps.words, "shorts": len(sd.read_shorts(e.n)),
                    }
                )
        return {
            "slug": slug,
            "channel": st.channel,
            "status": st.status,
            "chosen": st.chosen,
            "title": series.title if series else sd.read_concept().title,
            "drafts": sd.draft_letters(),
            "parts": parts,
            "has_design": sd.design.exists(),
            "next_step": self._next_step(sd, st, series),
        }

    @staticmethod
    def _next_step(sd: StoryDir, st: StatusDoc, series: SeriesDoc | None) -> str:
        if st.chosen is None and not (series and series.parts):
            return "concepts" if not sd.draft_letters() else "choose"
        if series is None:
            return "map"
        states = [st.parts.get(p.n, PartState()).status for p in series.parts]
        if "planned" in states:
            return f"write part {states.index('planned') + 1}"
        if "written" in states:
            return f"approve part {states.index('written') + 1}"
        if not sd.design.exists():
            return "design"
        if "approved" in states:
            return "produce"
        return "wait for production" if "producing" in states else "done"

    def list_stories(self) -> list[dict[str, Any]]:
        """Every workshop story: slug, channel, status, title and number of parts."""
        out: list[dict[str, Any]] = []
        for slug in list_slugs(self.paths):
            sd = StoryDir.open(self.paths, slug)
            if not sd.status.exists():
                continue
            st = sd.read_status()
            title = sd.read_series().title if sd.series.exists() else sd.read_concept().title
            out.append({"slug": slug, "channel": st.channel, "status": st.status, "title": title,
                        "parts": len(st.parts)})
        return out


def make_db_enqueue(db: Db) -> EnqueueFn:
    """Default enqueue: a `queued` row in `dashboard_jobs` (kind `series_part`) carrying the request."""

    def enqueue(req: ProduceRequest) -> str:
        db.execute(
            "insert or ignore into dashboard_jobs"
            "(job_id, kind, channel, status, message, created_at, params_json) values (?,?,?,?,?,?,?)",
            (
                req.job_id,
                "series_part",
                req.channel,
                "queued",
                req.title,
                datetime.now(UTC).isoformat(),
                json.dumps(asdict(req)),
            ),
        )
        return req.job_id

    return enqueue


def default_workshop(fake: Collection[str] = (), *, enqueue: EnqueueFn | None = None) -> Workshop:
    """A `Workshop` over `NIGHTSHIFT_HOME`; names in `fake` (subset of FAKEABLE) use in-process fakes."""
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    fakes = frozenset(fake) & FAKEABLE
    runtime = (
        Runtime.default("cpu-dev") if fakes and not paths.runtime_file.exists() else load_runtime(paths)
    )
    services = build_services(runtime, fake=fakes, paths=paths)
    return Workshop(paths, services, runtime, db, NsLogger("studio", paths.logs, echo=False), enqueue)


__all__ = [
    "EnqueueFn",
    "ProduceRequest",
    "Workshop",
    "available_formats",
    "chapter_count",
    "default_workshop",
    "design_options",
    "make_db_enqueue",
    "slugify",
]
