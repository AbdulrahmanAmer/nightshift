"""Sequential, tenant-grouped job runner (component C7)."""

from __future__ import annotations

from itertools import groupby

from ns_common.errors import NsError
from ns_studio.context import JobContext
from ns_studio.stages import REGISTRY, Stage, Tenant, run_stage

# Filled by stage modules at import time (text/stages_shorts.py, media/*.py append their names in order).
FORMAT_STAGES: dict[str, list[str]] = {"shorts_only": [], "master_first": [], "series_compilation": []}


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


def run_job(ctx: JobContext, *, resume: bool = True, dry_run: bool = False) -> list[str]:
    """Run every stage of `ctx.fmt` in order and return the names that were visited.

    Consecutive stages with the same GPU tenant share one acquisition; `dry_run` only lists the plan.
    """
    stages = plan_job(ctx.fmt)
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
                run_stage(ctx, st, force=not resume)
                ran.append(st.name)
        finally:
            if tenant is not None:
                ctx.services.acquirer.release(tenant, owner)
    return ran
