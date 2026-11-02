"""The `script_import` stage (component C11; spec 23.3 step 6).

Replaces M1-M4 for series parts: it reads the owner-approved `script.md` and `shorts/*.md` named in
`script_source.json` and writes `idea.json`, `plan.json`, `plan_meta.json`, `story.json` and
`shorts_plan.json` in the shapes C5's master stages produce, so everything from `seo_long` on runs unchanged.
The files are read when the stage runs, so a hand edit made after `produce` is used; re-running a finished
job needs `force` (the stage hash only covers `script_source.json`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ns_common.embed_client import to_blob
from ns_common.errors import NsError
from ns_studio.context import JobContext
from ns_studio.series.importer import build_artifacts
from ns_studio.series.store import parse_concept, parse_script, parse_short, read_text
from ns_studio.stages import Tenant, register
from ns_studio.text.budget import PACING_WPM
from ns_studio.text.stages_shorts import now_iso

EMBED_CHARS = 1500


def _read(path: str, what: str) -> str:
    p = Path(path)
    if not p.is_file():
        raise NsError("NS-E303", f"script_import: {what} not found at {p}")
    return read_text(p)


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
        src: dict[str, Any] = ctx.read_json("script_source.json")
        script = parse_script(_read(src["script"], "script"))
        shorts = [parse_short(_read(p, "short file")) for p in src.get("shorts", [])]
        concept = parse_concept(_read(src["concept"], "concept")) if src.get("concept") else None
        wpm = PACING_WPM.get(ctx.combo.pacing, 155)
        art = build_artifacts(script, shorts, wpm=wpm, combo_id=ctx.combo.combo_id, concept=concept)
        full_text = "\n\n".join(c.text for c in art.story.chapters)
        total_words = sum(c.word_count for c in art.story.chapters)
        vec = ctx.services.embed.embed([full_text[:EMBED_CHARS]])[0]
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
        ctx.write_json("idea.json", art.idea)
        ctx.write_json("plan.json", art.plan)
        ctx.write_json("plan_meta.json", art.plan_meta)
        ctx.write_json("shorts_plan.json", art.shorts_plan)
        ctx.write_json("story.json", art.story.model_dump())
        ctx.log.info("script_import", job_id=ctx.job_id, chapters=len(art.story.chapters), shorts=len(shorts))


register(ScriptImportStage())
