"""The calls from the dashboard into `ns_studio`, written against the real entry points (component C9).

Every function imports its collaborator when it is called, so the dashboard starts and every other page works
even when a studio dependency is broken. `tests/test_dashboard_wiring.py` resolves each `ns_studio` name that
this module imports, so a call to a function that does not exist fails CI instead of the owner's first video.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Collection
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from ns_common.acquire import Acquirer
from ns_common.logging import Level, NsLogger
from ns_common.paths import Paths

if TYPE_CHECKING:
    from ns_studio.context import JobContext
    from ns_studio.jobs import Env

#: `report(stage_name, stage_index, stage_total, job_index, job_total)`; may raise to cancel the job.
Report = Callable[[str, int, int, int, int], None]


class StageWatchLogger(NsLogger):
    """An `NsLogger` that also tells `on_stage(name)` when the stage runner starts or skips a stage.

    The stage runner (`ns_studio.stages.run_stage`) logs `stage_start` or `stage_skip` (`stage=<name>`) before
    each stage, so watching the log is how progress and cancel checks reach a running job without changing the
    orchestrator. An exception raised by `on_stage` stops the job before that stage runs.
    """

    def __init__(self, base: NsLogger, on_stage: Callable[[str], None]) -> None:
        super().__init__(base.component, base.logs_dir, echo=base.echo, clock=base.clock)
        self._on_stage = on_stage

    def log(self, level: Level, event: str, msg: str = "", **data: Any) -> None:
        """Write the record, then report the stage when it is a stage-start event."""
        super().log(level, event, msg, **data)
        stage = data.get("stage")
        if event in ("stage_start", "stage_skip") and isinstance(stage, str):
            self._on_stage(stage)


def open_env(
    fake: Collection[str] = (), acquirer: Acquirer | None = None, *, paths: Paths | None = None
) -> Env:
    """The studio environment (`ns_studio.jobs.open_env`) with the supervisor as GPU owner when given."""
    from ns_studio import jobs

    jobs.load_stage_modules()
    env = jobs.open_env(frozenset(fake))
    if paths is not None and env.paths.home != paths.home:  # both must use the same NIGHTSHIFT_HOME
        raise RuntimeError(f"the studio opened {env.paths.home}, the dashboard {paths.home}")
    if acquirer is not None:
        env = replace(env, services=replace(env.services, acquirer=acquirer))
    return env


def watched(ctx: JobContext, names: list[str], job_index: int, job_total: int, report: Report) -> JobContext:
    """`ctx` whose log reports each stage of `names` to `report`."""

    def on_stage(stage: str) -> None:
        idx = names.index(stage) if stage in names else 0
        report(stage, idx, len(names), job_index, job_total)

    return replace(ctx, log=StageWatchLogger(ctx.log, on_stage))


def output_ids(env: Env, job_id: str) -> list[str]:
    """Ids of the `outputs` rows that the job `job_id` inserted, oldest first."""
    sql = "select output_id from outputs where job_id = ? order by created_at, output_id"
    rows = env.db.query(sql, (job_id,))
    return [str(r["output_id"]) for r in rows]


def run_story_jobs(
    channel: str,
    fmt: str,
    count: int | None,
    report: Report,
    *,
    acquirer: Acquirer | None = None,
    fake: Collection[str] = (),
    paths: Paths | None = None,
    seen: list[str] | None = None,
) -> list[str]:
    """`studio run --channel <channel> --format <fmt> --count <count>`: returns the new output ids.

    Runs `ns_studio.policy.regen.run_job_guarded` (the orchestrator plus the regenerate-once policy) on
    each job. `seen` collects the studio job ids as they are created.
    """
    from ns_studio import jobs
    from ns_studio.channels.crud import get_channel
    from ns_studio.orchestrator import plan_job
    from ns_studio.policy.regen import run_job_guarded

    env = open_env(fake, acquirer, paths=paths)
    ch = get_channel(env.paths, channel)
    jobs.prepare_plan(fmt, None)
    names = [s.name for s in plan_job(fmt)]
    n = max(1, int(count or 1))
    rng = random.Random()
    out: list[str] = []
    for i in range(n):
        ctx = jobs.make_job(env, ch, fmt, rng=rng)
        if seen is not None:
            seen.append(ctx.job_id)
        run_job_guarded(watched(ctx, names, i, n, report))
        out.extend(output_ids(env, ctx.job_id))
    return out
