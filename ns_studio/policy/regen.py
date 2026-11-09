"""Regenerate-once policy for gate failures (spec section 18.8, component C7).

A failure moves the item to `rejected\\` with the reason, regenerates once from the earliest
affected stage, and logs the code. A second failure ends the job.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from ns_common.errors import NsError
from ns_studio.context import JobContext
from ns_studio.orchestrator import plan_format, plan_job, run_job
from ns_studio.policy.base import POLICY_CODES
from ns_studio.stages import StageState

ATTEMPTS_FILE = "policy_attempts.json"
MAX_REGENERATIONS = 1

# Candidate restart stages per code, earliest sensible first; the first one present in the plan wins. Each
# tuple names the stages of every plan: shorts_only (`draft`, `hooks`, `seo`), master_first (`chapters_draft`,
# `shorts_select`, `seo_long`, `seo_shorts`), master_from_script (`seo_long`, `seo_shorts`) and
# shorts_from_script (`hooks`, `seo`). A name that is in no plan sends the job back to the first stage of its
# plan: an hour of LLM work for one disliked tag, or for a script job the cheap `script_import` (the text is
# the owner's, so it is never rewritten).
REGEN_FROM: dict[str, tuple[str, ...]] = {
    "NS-E601": ("ideas",),
    "NS-E602": ("draft", "chapters_draft", "ideas"),
    "NS-E603": ("seo", "seo_long", "ideas"),
    "NS-E604": ("hooks", "shorts_select", "seo", "seo_long", "ideas"),
    "NS-E605": ("ideas",),
    "NS-E606": ("seo", "seo_long", "ideas"),
    "NS-E607": ("shorts_select", "shorts_plan", "ideas"),
    "NS-E611": ("clips", "visual_plan", "ideas"),
    "NS-E612": ("expand", "chapters_draft", "draft", "plan", "long_plan", "ideas"),
    "NS-E613": ("clips", "visual_plan", "ideas"),
    "NS-E614": ("voice", "render", "ideas"),
}
# Metadata failures are fixed by rewriting the metadata alone: only these stages are redone (everything after
# them is gated on its input hashes and follows when the SEO files change), never the chapters.
REGEN_ONLY: dict[str, tuple[str, ...]] = {
    "NS-E603": ("seo", "seo_long", "seo_shorts"),
    "NS-E606": ("seo", "seo_long", "seo_shorts"),
}
_SMALL_FILES = (
    "idea.json",
    "story.json",
    "parts.json",
    "seo.json",
    "seo_long.json",
    "seo_shorts.json",
    "shorts_plan.json",
    "policy_text.json",
    "policy_final.json",
    "policy_notes.json",
)
_MEDIA_DIRS = ("long", "shorts")


def earliest_stage(code: str, names: list[str]) -> str:
    """The stage to restart from for `code`, falling back to the first stage of the plan."""
    for cand in REGEN_FROM.get(code, ()):
        if cand in names:
            return cand
    return names[0]


#: Files a stage keeps as its own memo and returns early on; the retry would reproduce them unchanged.
STAGE_MEMOS: dict[str, tuple[str, ...]] = {
    "hooks": ("hooks_all.json",),  # HooksStage skips when this exists
    "voice": ("audio/manifest.json",),  # synthesize() reuses every WAV with an unchanged signature
}


def invalidate_from(ctx: JobContext, names: list[str], stage: str) -> None:
    """Forget `stage` and every later stage, and their memo files, so the next `run_job` really redoes them.

    The retry also differs from the first pass: prompts run hotter (`text.prompts.attempt_temperature`),
    TTS gets another seed and clip draws another seed with the rejected clips blocked.
    """
    state = StageState.load(ctx.job_dir)
    for n in names[names.index(stage) :]:
        state.entries.pop(n, None)
        for memo in STAGE_MEMOS.get(n, ()):
            ctx.file(memo).unlink(missing_ok=True)
    state.save(ctx.job_dir)


def invalidate_only(ctx: JobContext, names: list[str], only: tuple[str, ...]) -> list[str]:
    """Forget just the stages of `names` that are in `only` (and their memos); returns those stages."""
    state = StageState.load(ctx.job_dir)
    hit = [n for n in names if n in only]
    for n in hit:
        state.entries.pop(n, None)
        for memo in STAGE_MEMOS.get(n, ()):
            ctx.file(memo).unlink(missing_ok=True)
    state.save(ctx.job_dir)
    return hit


def _attempts(ctx: JobContext) -> dict:
    f = ctx.file(ATTEMPTS_FILE)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else {"regenerations": 0, "history": []}


def reject_artifacts(ctx: JobContext, err: NsError, attempt: int) -> Path:
    """Move rendered media and copy the text artifacts to `rejected\\<slug>\\<date>\\<job>\\attempt<N>`."""
    now = datetime.now(UTC)
    dest = ctx.paths.rejected_dir(ctx.channel.slug, f"{now:%Y-%m-%d}", ctx.job_id) / f"attempt{attempt}"
    dest.mkdir(parents=True, exist_ok=True)
    reason = {
        "code": err.code,
        "message": err.definition.message,
        "detail": err.detail,
        "ts": now.isoformat(),
    }
    (dest / "reason.json").write_text(json.dumps(reason, indent=2) + "\n", encoding="utf-8")
    for name in _SMALL_FILES:
        if ctx.file(name).exists():
            shutil.copy2(ctx.file(name), dest / name)
    for d in _MEDIA_DIRS:
        src = ctx.file(d)
        if src.is_dir():
            shutil.move(str(src), str(dest / d))
    return dest


def run_job_guarded(ctx: JobContext, *, resume: bool = True) -> list[str]:
    """`run_job` with regenerate-once handling of NS-E601 to NS-E614 policy failures.

    The voice setup queued (`config/pending_voice.json`) is created first, so every path that runs a job
    (`studio run`, the dashboard's Run now, a series part) has it before the voice stage asks for it.
    """
    from ns_studio.channels.crud import design_name_problems, pool_name_problems, sync_channel_row
    from ns_studio.errors import StudioError
    from ns_studio.voice_setup import apply_pending_voices

    # A series part is created by the dashboard without `make_job`: give its channel the display name of its
    # profile (a YAML-only channel was left with its slug) and refuse unknown preset names up front (NS-E320).
    sync_channel_row(ctx.db, ctx.channel)
    if ctx.series:
        problems = [*pool_name_problems(ctx.channel.pools), *design_name_problems(ctx.series)]
        if problems:
            raise StudioError("NS-E320", "; ".join(problems))
    apply_pending_voices(ctx)
    first = True
    while True:
        try:
            return run_job(ctx, resume=resume if first else True)
        except NsError as e:
            if e.code not in POLICY_CODES:
                raise
            att = _attempts(ctx)
            n = int(att["regenerations"])
            dest = reject_artifacts(ctx, e, n + 1)
            att["history"].append({"code": e.code, "detail": e.detail, "rejected_to": str(dest)})
            if n >= MAX_REGENERATIONS:
                ctx.write_json(ATTEMPTS_FILE, att)
                ctx.log.error("policy_fail_final", code=e.code, job_id=ctx.job_id, detail=e.detail)
                raise
            att["regenerations"] = n + 1
            ctx.write_json(ATTEMPTS_FILE, att)
            names = [s.name for s in plan_job(plan_format(ctx.fmt, ctx.job_dir))]
            start = earliest_stage(e.code, names)
            ctx.log.warn("policy_regenerate", code=e.code, restart_at=start, job_id=ctx.job_id)
            only = REGEN_ONLY.get(e.code)
            if not (only and invalidate_only(ctx, names, only)):
                invalidate_from(ctx, names, start)
            first = False
