"""The calls from the dashboard into `ns_studio`, written against the real entry points (component C9).

Every function imports its collaborator when it is called, so the dashboard starts and every other page works
even when a studio dependency is broken. `tests/test_dashboard_wiring.py` resolves each `ns_studio` name that
this module imports, so a call to a function that does not exist fails CI instead of the owner's first video.
"""

from __future__ import annotations

import dataclasses
import random
import shutil
from collections.abc import Callable, Collection
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ns_common.acquire import Acquirer
from ns_common.db import Db
from ns_common.logging import Level, NsLogger
from ns_common.paths import Paths

if TYPE_CHECKING:
    from ns_studio.context import JobContext
    from ns_studio.jobs import Env

#: `report(stage_name, stage_index, stage_total, job_index, job_total)`; may raise to cancel the job.
Report = Callable[[str, int, int, int, int], None]

DOWNLOADS = "_downloads"


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


def output_ids(db: Db, job_id: str) -> list[str]:
    """Ids of the `outputs` rows that the job `job_id` inserted, oldest first."""
    sql = "select output_id from outputs where job_id = ? order by created_at, output_id"
    rows = db.query(sql, (job_id,))
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
        out.extend(output_ids(env.db, ctx.job_id))
    return out


def with_acquirer(ctx: JobContext, acquirer: Acquirer | None) -> JobContext:
    """`ctx` whose services use `acquirer` (the supervisor) instead of the one the studio built."""
    if acquirer is None:
        return ctx
    return replace(ctx, services=replace(ctx.services, acquirer=acquirer))


def clip_stage_names() -> list[str]:
    """The stage names of a clip job (`FORMAT_STAGES["clip"]`, registered when the Clipper is imported)."""
    from ns_studio.clipper import stages_clip  # noqa: F401  (importing registers the clip stages)
    from ns_studio.orchestrator import FORMAT_STAGES

    return list(FORMAT_STAGES["clip"])


def download_dir(paths: Paths, source_id: str) -> Path:
    """Where a linked video is downloaded before the Clipper imports it (removed after the import)."""
    return paths.home / "imports" / "clipper" / DOWNLOADS / source_id


def download_source(
    paths: Paths, url: str, source_id: str, on_progress: Callable[[float], None]
) -> Path:
    """Download `url` with the pinned yt-dlp (`ns_studio.clipper.source.import_url`); returns the video file.

    `on_progress(percent)` gets 0-100 and may raise to stop the job. Failures are NS-E409.
    """
    from ns_studio.clipper import source

    return source.import_url(url, download_dir(paths, source_id), on_progress)


def run_clip(
    source_path: Path,
    params: dict[str, Any],
    report: Report,
    *,
    acquirer: Acquirer | None = None,
    fake: Collection[str] = (),
    paths: Paths | None = None,
    seen: list[str] | None = None,
) -> list[str]:
    """`studio clip <source_path> --channel ...`: create the clip job and run its stages; returns output ids.

    `params` carries `channel`, `template`, `max_clips`, `min_s`, `max_s` and `rights_confirmed` as the
    dashboard stores them. A video that was downloaded for the job is deleted once it has been imported.
    """
    from ns_studio.channels.crud import get_channel
    from ns_studio.clipper.models import ClipParams
    from ns_studio.clipper.stages_clip import create_clip_job
    from ns_studio.orchestrator import run_job

    home = paths or Paths.from_env()
    channel = get_channel(home, str(params["channel"]))
    clip_params = ClipParams(
        source=str(source_path),
        channel=channel.slug,
        template=str(params.get("template") or "auto"),
        max_clips=int(params.get("max_clips") or 6),
        min_s=float(params.get("min_s") or 20),
        max_s=float(params.get("max_s") or 60),
        rights_confirmed=bool(params.get("rights_confirmed")),
    )
    clip_params.validate()
    ctx = with_acquirer(create_clip_job(home, channel, clip_params, fake=frozenset(fake)), acquirer)
    if seen is not None:
        seen.append(ctx.job_id)
    run_job(watched(ctx, clip_stage_names(), 0, 1, report))
    if source_path.parent.parent.name == DOWNLOADS:
        shutil.rmtree(source_path.parent, ignore_errors=True)
    return output_ids(ctx.db, ctx.job_id)


def rerun_clip(
    studio_job_id: str,
    changes: dict[str, Any],
    report: Report,
    *,
    acquirer: Acquirer | None = None,
    fake: Collection[str] = (),
    paths: Paths | None = None,
) -> list[str]:
    """Run the later stages of an earlier clip job again with other options (`studio clip --reselect`).

    The import, audio, analysis and transcript are kept; selection, layout, captions, render, QC and
    the outbox run again. `changes` may hold `template`, `max_clips`, `min_s` and `max_s`.
    """
    from ns_studio.channels.crud import get_channel
    from ns_studio.clipper.models import ClipParams
    from ns_studio.clipper.stages_clip import resume_clip_job
    from ns_studio.orchestrator import run_job

    home = paths or Paths.from_env()
    old = ClipParams.load(home.job_dir(studio_job_id))
    new = dataclasses.replace(
        old,
        template=str(changes.get("template") or old.template),
        max_clips=int(changes.get("max_clips") or old.max_clips),
        min_s=float(changes.get("min_s") or old.min_s),
        max_s=float(changes.get("max_s") or old.max_s),
    )
    new.validate()
    ctx = with_acquirer(
        resume_clip_job(home, studio_job_id, get_channel(home, old.channel), fake=frozenset(fake)), acquirer
    )
    new.save(ctx.job_dir)
    run_job(watched(ctx, clip_stage_names(), 0, 1, report))
    return output_ids(ctx.db, ctx.job_id)
