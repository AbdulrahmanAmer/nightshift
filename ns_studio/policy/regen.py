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
from ns_studio.orchestrator import plan_job, run_job
from ns_studio.policy.base import POLICY_CODES
from ns_studio.stages import StageState

ATTEMPTS_FILE = "policy_attempts.json"
MAX_REGENERATIONS = 1

# Candidate restart stages per code, earliest sensible first; the first one present in the plan wins.
REGEN_FROM: dict[str, tuple[str, ...]] = {
    "NS-E601": ("ideas",),
    "NS-E602": ("draft", "ideas"),
    "NS-E603": ("seo", "long_seo", "ideas"),
    "NS-E604": ("hooks", "seo", "ideas"),
    "NS-E605": ("ideas",),
    "NS-E606": ("seo", "long_seo", "ideas"),
    "NS-E607": ("shorts_select", "shorts_plan", "ideas"),
    "NS-E611": ("clips", "visual_plan", "ideas"),
    "NS-E612": ("expand", "draft", "plan", "ideas"),
    "NS-E613": ("clips", "visual_plan", "ideas"),
    "NS-E614": ("voice", "render", "ideas"),
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


def invalidate_from(ctx: JobContext, names: list[str], stage: str) -> None:
    """Forget `stage` and every later stage so the next `run_job` executes them again."""
    state = StageState.load(ctx.job_dir)
    for n in names[names.index(stage) :]:
        state.entries.pop(n, None)
    state.save(ctx.job_dir)


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
    """`run_job` with regenerate-once handling of NS-E601 to NS-E614 policy failures."""
    try:
        return run_job(ctx, resume=resume)
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
        names = [s.name for s in plan_job(ctx.fmt)]
        start = earliest_stage(e.code, names)
        ctx.log.warn("policy_regenerate", code=e.code, restart_at=start, job_id=ctx.job_id)
        invalidate_from(ctx, names, start)
    return run_job(ctx, resume=True)
