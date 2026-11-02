"""Sequential, tenant-grouped job runner (component C7)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from itertools import groupby

from ns_common.errors import NsError
from ns_studio.context import JobContext
from ns_studio.stages import REGISTRY, Stage, Tenant, needs_embed, run_stage

# Canonical stage order per format. Stage modules only `register()` their stages; the order lives here so
# it never depends on import order. Names not registered yet are reported by `plan_job` (NS-E306).
SHORTS_ONLY_STAGES: tuple[str, ...] = (
    "ideas",
    "plan",
    "draft",
    "critic_revise",
    "split",
    "hooks",
    "seo",
    "visual_plan",
    "voice",
    "captions",
    "clips",
    "render",
    "qc",
    "outbox",
)
MASTER_FIRST_STAGES: tuple[str, ...] = (
    "ideas",
    "long_plan",
    "chapters_draft",
    "chapters_critic",
    "continuity",
    "seo_long",
    "shorts_select",
    "visual_plan_long",
    "seo_shorts",
    "voice",
    "duration_check",
    "captions",
    "shorts_timing",
    "clips",
    "render_master",
    "thumbnails",
    "shorts_derive",
    "qc",
    "outbox",
)
# Series parts (C11, CR-C11-3): `script_import` replaces ideas..continuity and shorts_select; chosen
# by `run_job` when a master_first job directory holds `script_source.json`.
MASTER_FROM_SCRIPT_STAGES: tuple[str, ...] = (
    "script_import",
    "visual_plan_long",
    "seo_long",
    "seo_shorts",
    "voice",
    "duration_check",
    "captions",
    "shorts_timing",
    "clips",
    "render_master",
    "thumbnails",
    "shorts_derive",
    "qc",
    "outbox",
)
FORMAT_STAGES: dict[str, list[str]] = {
    "shorts_only": list(SHORTS_ONLY_STAGES),
    "master_first": list(MASTER_FIRST_STAGES),
    "master_from_script": list(MASTER_FROM_SCRIPT_STAGES),
    "series_compilation": [],
    "clip": [],
}


def _tenant_of(stage: Stage) -> Tenant:
    """GPU tenant of `stage` (grouping key)."""
    return stage.tenant


def plan_job(fmt: str) -> list[Stage]:
    """Resolve the ordered stages of `fmt`; NS-E306 for unknown or unimplemented formats."""
    names = FORMAT_STAGES.get(fmt)
    if not names:
        raise NsError("NS-E306", f"format {fmt!r} has no stages registered")
    missing = [n for n in names if n not in REGISTRY]
    if missing:
        raise NsError("NS-E306", f"stages not registered: {missing}")
    return [REGISTRY[n] for n in names]


@contextmanager
def embed_lease(ctx: JobContext, stage: Stage, owner: str) -> Iterator[None]:
    """Hold the `embed` service while `stage` runs when the stage embeds text (gap 4).

    `embed` is a CPU service outside the GPU lock: the supervisor starts it on acquire and unloads it after
    10 idle minutes, so nothing has to start it by hand. Without a lease idea dedupe and the text policy
    check called :8081 while nobody had started it.
    """
    if not needs_embed(stage):
        yield
        return
    ctx.services.acquirer.acquire("embed", owner)
    try:
        yield
    finally:
        ctx.services.acquirer.release("embed", owner)


def run_job(ctx: JobContext, *, resume: bool = True, dry_run: bool = False) -> list[str]:
    """Run every stage of `ctx.fmt` in order and return the names that were visited.

    Consecutive stages with the same GPU tenant share one acquisition; `dry_run` only lists the plan.
    """
    fmt = ctx.fmt
    if fmt == "master_first" and ctx.file("script_source.json").exists():
        fmt = "master_from_script"
    stages = plan_job(fmt)
    if dry_run:
        return [s.name for s in stages]
    owner = f"studio:{ctx.job_id}"
    ran: list[str] = []
    for tenant, group in groupby(stages, key=_tenant_of):
        group_list = list(group)
        if tenant is not None:
            ctx.services.acquirer.acquire(tenant, owner, model_alias="story" if tenant == "llama" else None)
        try:
            for st in group_list:
                with embed_lease(ctx, st, owner):
                    run_stage(ctx, st, force=not resume)
                ran.append(st.name)
        finally:
            if tenant == "whisper":  # free the weights before the lock goes to the next tenant (llama)
                ctx.services.transcriber.unload()
            if tenant is not None:
                ctx.services.acquirer.release(tenant, owner)
    return ran
