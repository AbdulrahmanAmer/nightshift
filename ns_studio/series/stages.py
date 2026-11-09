"""The `script_import` stage (component C11; spec 23.3 step 6).

Replaces M1-M4 for series parts: it reads the owner-approved `script.md` and `shorts/*.md` named in
`script_source.json` and writes `idea.json`, `plan.json`, `plan_meta.json`, `story.json` and
`shorts_plan.json` in the shapes C5's master stages produce, so everything from `seo_long` on runs unchanged.
The files are read when the stage runs, so a hand edit made after `produce` is used; re-running a finished
job needs `force` (the stage hash only covers `script_source.json`).

A story the owner typed or pasted (`JobRunRequest.script`) takes the same road: `jobs.make_script_job` writes
it as `script.md` into the job folder next to a `script_source.json` that names it. Two variants of the stage
write only what their pipeline reads: `script_import_long` (chapters and plan, no Shorts plan: `shorts_select`
cuts the Shorts afterwards) and `script_import_short` (`idea.json`, `story.json` and `parts.json` with one
part holding the text exactly as given). The owner's words are never cut, extended or replaced.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ns_common.embed_client import embed_window, to_blob
from ns_common.errors import NsError
from ns_common.models import Part
from ns_common.pacing import narration_seconds
from ns_common.textstats import split_sentences, word_count
from ns_studio.context import JobContext
from ns_studio.media.voice import job_voice
from ns_studio.series.importer import PartArtifacts, build_artifacts
from ns_studio.series.plain import spoken_wpm
from ns_studio.series.store import ScriptDoc, parse_concept, parse_script, parse_short, read_text
from ns_studio.stages import Tenant, register
from ns_studio.text.budget import PACING_WPM
from ns_studio.text.stages_shorts import now_iso


def _read(path: str, what: str, base: Path | None = None) -> str:
    """Text of a source file; a relative `path` is looked up in the job folder (`base`)."""
    p = Path(path)
    if base is not None and not p.is_absolute():
        p = base / p
    if not p.is_file():
        raise NsError("NS-E303", f"script_import: {what} not found at {p}")
    return read_text(p)


def _build(ctx: JobContext) -> tuple[ScriptDoc, PartArtifacts, int]:
    """Read `script_source.json` and the files it names: the script, its artifacts and the Shorts count."""
    src: dict[str, Any] = ctx.read_json("script_source.json")
    script = parse_script(_read(src["script"], "script", ctx.job_dir))
    shorts = [parse_short(_read(p, "short file")) for p in src.get("shorts", [])]
    concept = parse_concept(_read(src["concept"], "concept")) if src.get("concept") else None
    wpm = PACING_WPM.get(ctx.combo.pacing, 155)
    art = build_artifacts(script, shorts, wpm=wpm, combo_id=ctx.combo.combo_id, concept=concept)
    return script, art, len(shorts)


def _store(ctx: JobContext, script: ScriptDoc, art: PartArtifacts) -> tuple[str, int]:
    """Record the story in the `stories` table (embedding included); returns its text and word count."""
    full_text = "\n\n".join(c.text for c in art.story.chapters)
    total_words = sum(c.word_count for c in art.story.chapters)
    vec = ctx.services.embed.embed([embed_window(full_text, ctx.runtime.embed.batch_size)])[0]
    ctx.db.execute(
        "insert or ignore into channels(slug, display_name, created_at) values (?,?,?)",
        (ctx.channel.slug, ctx.channel.display_name, now_iso()),
    )
    ctx.db.execute(
        "insert or replace into stories(job_id, channel, format, working_title, text, word_count, "
        "opening_line, embedding, critic_avg, revisions, created_at) values (?,?,?,?,?,?,?,?,?,?,?)",
        (
            ctx.job_id,
            ctx.channel.slug,
            ctx.fmt,
            art.story.working_title,
            full_text,
            total_words,
            art.story.chapters[0].sentences[0].text,
            to_blob(vec),
            script.critic_avg,
            0,
            now_iso(),
        ),
    )
    return full_text, total_words


class ScriptImportStage:
    """Series M1-M4 replacement: story, plan, idea and Shorts plan from the script files."""

    name: str = "script_import"
    tenant: Tenant = None
    uses_embed: bool = True  # calls the embed service; the orchestrator leases it around run()
    inputs: tuple[str, ...] = ("script_source.json",)
    outputs: tuple[str, ...] = (
        "idea.json",
        "plan.json",
        "plan_meta.json",
        "story.json",
        "shorts_plan.json",
    )

    def run(self, ctx: JobContext) -> None:
        """Write the master artifacts from `script_source.json`; NS-E303 when a file is missing or invalid."""
        script, art, shorts = _build(ctx)
        _store(ctx, script, art)
        ctx.write_json("idea.json", art.idea)
        ctx.write_json("plan.json", art.plan)
        ctx.write_json("plan_meta.json", art.plan_meta)
        ctx.write_json("shorts_plan.json", art.shorts_plan)
        ctx.write_json("story.json", art.story.model_dump())
        ctx.log.info("script_import", job_id=ctx.job_id, chapters=len(art.story.chapters), shorts=shorts)


class ScriptImportLongStage:
    """A long video from the owner's own script: the master artifacts without a Shorts plan.

    The Shorts are cut from the chapters afterwards (`shorts_select`), as in a master the model wrote.
    """

    name: str = "script_import_long"
    tenant: Tenant = None
    uses_embed: bool = True
    inputs: tuple[str, ...] = ("script_source.json",)
    outputs: tuple[str, ...] = ("idea.json", "plan.json", "plan_meta.json", "story.json")

    def run(self, ctx: JobContext) -> None:
        """Write `idea.json`, `plan.json`, `plan_meta.json` and `story.json` (5 to 7 chapters)."""
        script, art, _shorts = _build(ctx)
        _store(ctx, script, art)
        ctx.write_json("idea.json", art.idea)
        ctx.write_json("plan.json", art.plan)
        ctx.write_json("plan_meta.json", art.plan_meta)
        ctx.write_json("story.json", art.story.model_dump())
        ctx.log.info("script_import", job_id=ctx.job_id, chapters=len(art.story.chapters), owner_script=True)


class ScriptImportShortStage:
    """A Short from the owner's own script: one part holding the text exactly as given.

    `parts.json` is written here, so the model's `split` and `hooks` stages are not in the plan and cannot cut
    or reword the story. The first sentence is the part's hook: the voice stage speaks it once, as part of the
    story, and nothing is invented in front of it.
    """

    name: str = "script_import_short"
    tenant: Tenant = None
    uses_embed: bool = True
    inputs: tuple[str, ...] = ("script_source.json",)
    outputs: tuple[str, ...] = ("idea.json", "story.json", "parts.json")

    def run(self, ctx: JobContext) -> None:
        """Write `idea.json`, `story.json` ({text, word_count, revisions}) and `parts.json` (one part)."""
        script, art, _shorts = _build(ctx)
        text, words = _store(ctx, script, art)
        wpm = spoken_wpm(ctx.combo.pacing, job_voice(ctx).speed)  # what the voice stage will speak at
        part = Part(
            n=1,
            text=text,
            hook=split_sentences(text)[0],
            word_count=max(1, word_count(text)),
            est_seconds=max(0.01, narration_seconds(text, wpm)),
            cliffhanger=False,
        )
        ctx.write_json("idea.json", art.idea)
        ctx.write_json(
            "story.json", {"text": text, "word_count": words, "revisions": 0, "critic_avg": script.critic_avg}
        )
        ctx.write_json("parts.json", [part.model_dump()])
        ctx.log.info("script_import", job_id=ctx.job_id, words=words, owner_script=True)


register(ScriptImportStage())
register(ScriptImportLongStage())
register(ScriptImportShortStage())
