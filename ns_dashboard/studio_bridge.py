"""The calls from the dashboard into `ns_studio`, written against the real entry points (component C9).

Every function imports its collaborator when it is called, so the dashboard starts and every other page works
even when a studio dependency is broken. `tests/test_dashboard_wiring.py` resolves each `ns_studio` name that
this module imports, so a call to a function that does not exist fails CI instead of the owner's first video.
"""

from __future__ import annotations

import dataclasses
import random
import shutil
from collections.abc import Callable, Collection, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ns_common.acquire import Acquirer
from ns_common.db import Db
from ns_common.logging import Level, NsLogger
from ns_common.paths import Paths

if TYPE_CHECKING:
    from ns_studio.context import JobContext
    from ns_studio.jobs import Env
    from ns_studio.series.workshop import Workshop

#: `report(stage_name, stage_index, stage_total, job_index, job_total)`; may raise to cancel the job.
Report = Callable[[str, int, int, int, int], None]

DOWNLOADS = "_downloads"

#: Services to fake when the workshop is only read or its status files are edited (no model is called).
READ_ONLY = ("llm", "tts", "whisper", "embed", "clips")
FAKE_ALL = READ_ONLY


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
    resume_job_id: str | None = None,
    script: str | None = None,
    script_title: str | None = None,
) -> list[str]:
    """`studio run --channel <channel> --format <fmt> --count <count>`: returns the new output ids.

    Runs `ns_studio.policy.regen.run_job_guarded` (the orchestrator plus the regenerate-once policy) on
    each job. `seen` collects the studio job ids as they are created. With `resume_job_id` no job is created:
    that earlier job (the one that failed) is opened again and runs from its failed stage (`studio run
    --resume`), keeping the story, the voice and everything else it had finished. With `script` (the owner's
    story text, `studio run --script`) one job is made from that text instead of one the model writes
    (`jobs.make_script_job`).
    """
    from ns_studio import jobs
    from ns_studio.channels.crud import get_channel
    from ns_studio.orchestrator import plan_format, plan_job
    from ns_studio.policy.regen import run_job_guarded

    env = open_env(fake, acquirer, paths=paths)
    if resume_job_id:
        ctx = jobs.load_job(env, resume_job_id)
        jobs.prepare_plan(ctx.fmt, None)
        names = [s.name for s in plan_job(plan_format(ctx.fmt, ctx.job_dir))]
        if seen is not None:
            seen.append(ctx.job_id)
        run_job_guarded(watched(ctx, names, 0, 1, report), resume=True)
        return output_ids(env.db, ctx.job_id)
    ch = get_channel(env.paths, channel)
    jobs.prepare_plan(fmt, None)
    names = [s.name for s in plan_job(fmt)]
    n = max(1, int(count or 1))
    if script is not None:
        n = 1
    rng = random.Random()
    out: list[str] = []
    for i in range(n):
        if script is not None:
            ctx = jobs.make_script_job(env, ch, fmt, script, script_title, rng=rng)
            names = [s.name for s in plan_job(plan_format(ctx.fmt, ctx.job_dir))]
        else:
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
    paths: Paths,
    url: str,
    source_id: str,
    on_progress: Callable[[float], None],
    cancel_token: Any = None,
) -> Path:
    """Download `url` with the pinned yt-dlp (`ns_studio.clipper.source.import_url`); returns the video file.

    `on_progress(percent)` gets 0-100 and may raise to stop the job; `cancel_token` (a
    `ns_studio.clipper.source.CancelToken`) lets another thread kill yt-dlp. Failures are NS-E409.
    """
    from ns_studio.clipper import source

    target = download_dir(paths, source_id)
    if cancel_token is None:
        return source.import_url(url, target, on_progress)
    return source.import_url(url, target, on_progress, cancel_token=cancel_token)


def clip_params_of(source_path: Path, channel_slug: str, params: dict[str, Any]) -> Any:
    """`ClipParams` for a dashboard clip job: the options the owner picked in Create, caption look included.

    `caption_style` is the Caption look chosen in Create. `ClipParams` takes it once the Clipper has the
    field (it overrides the style the selector would pick); until then it is left out instead of failing.
    """
    from ns_studio.clipper.models import ClipParams

    kw: dict[str, Any] = {
        "source": str(source_path),
        "channel": channel_slug,
        "template": str(params.get("template") or "auto"),
        "max_clips": int(params.get("max_clips") or 6),
        "min_s": float(params.get("min_s") or 20),
        "max_s": float(params.get("max_s") or 60),
        "rights_confirmed": bool(params.get("rights_confirmed")),
    }
    style = params.get("caption_style")
    if style and "caption_style" in {f.name for f in dataclasses.fields(ClipParams)}:
        kw["caption_style"] = str(style)
    return ClipParams(**kw)


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
    from ns_studio.clipper.stages_clip import create_clip_job
    from ns_studio.orchestrator import run_job

    home = paths or Paths.from_env()
    channel = get_channel(home, str(params["channel"]))
    clip_params = clip_params_of(source_path, channel.slug, params)
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
    style = changes.get("caption_style")
    if style and "caption_style" in {f.name for f in dataclasses.fields(ClipParams)}:
        new = dataclasses.replace(new, caption_style=str(style))
    new.validate()
    ctx = with_acquirer(
        resume_clip_job(home, studio_job_id, get_channel(home, old.channel), fake=frozenset(fake)), acquirer
    )
    new.save(ctx.job_dir)
    run_job(watched(ctx, clip_stage_names(), 0, 1, report))
    return output_ids(ctx.db, ctx.job_id)


# ---------------------------------------------------------------- Series Workshop


@contextmanager
def workshop_session(
    *, acquirer: Acquirer | None = None, fake: Collection[str] = ()
) -> Iterator[Workshop]:
    """A `Workshop` over `NIGHTSHIFT_HOME` (`ns_studio.series.workshop.default_workshop`), closed afterwards.

    Production rows go to `dashboard_jobs` as kind `series_part`; the supervisor is the GPU owner when given.
    """
    from ns_studio.series.workshop import default_workshop

    ws = default_workshop(frozenset(fake))
    if acquirer is not None:
        ws.services = replace(ws.services, acquirer=acquirer)
    try:
        yield ws
    finally:
        ws.db.close()


def list_series() -> list[dict[str, Any]]:
    """Every workshop story with its parts and the next step, read from the story folders (no model)."""
    from ns_studio.series.store import WorkshopError

    out: list[dict[str, Any]] = []
    with workshop_session(fake=READ_ONLY) as ws:
        for story in ws.list_stories():
            try:
                out.append({**story, **ws.status(story["slug"])})
            except WorkshopError:
                out.append({**story, "parts": [], "next_step": "unreadable"})
    return out


def release_part(params: dict[str, Any]) -> None:
    """Put a part whose production failed or was cancelled back to `approved`, so it can be produced again."""
    from ns_studio.series.store import StoryDir, WorkshopError

    try:
        with workshop_session(fake=READ_ONLY) as ws:
            sd = StoryDir.open(ws.paths, str(params["story_slug"]))
            status = sd.read_status()
            n = int(params["part_n"])
            state = status.parts.get(n)
            mine = state is not None and state.job_id in (None, params.get("job_id"))
            if state is not None and state.status == "producing" and mine:
                ws._set_part(sd, status, n, "approved")
    except (WorkshopError, KeyError, ValueError):
        return


def finish_part(params: dict[str, Any]) -> None:
    """A part whose job finished goes from `producing` to `produced` (its video is in the Queue).

    Without this a finished part stayed `producing` with a `done` job behind it, and the start sweep put it
    back to `approved`, so "Produce next part" made the same video twice.
    """
    from ns_studio.series.store import StoryDir, WorkshopError

    try:
        with workshop_session(fake=READ_ONLY) as ws:
            sd = StoryDir.open(ws.paths, str(params["story_slug"]))
            status = sd.read_status()
            n = int(params["part_n"])
            state = status.parts.get(n)
            mine = state is not None and state.job_id in (None, params.get("job_id"))
            if state is not None and state.status == "producing" and mine:
                ws._set_part(sd, status, n, "produced")
    except (WorkshopError, KeyError, ValueError):
        return


def run_series_part(
    params: dict[str, Any],
    report: Report,
    *,
    acquirer: Acquirer | None = None,
    fake: Collection[str] = (),
    paths: Paths | None = None,
) -> list[str]:
    """Produce one approved series part: run the studio job that `Workshop.produce` prepared.

    `params` is the `ProduceRequest` stored in the `series_part` row (`job_id`, `channel`, ...); the job
    directory already holds `script_source.json`, so `run_job` switches to the `master_from_script` plan.
    A resumed job (its `job.json` exists) continues where `stage_state.json` says.
    """
    from ns_studio import jobs
    from ns_studio.channels.crud import get_channel
    from ns_studio.context import JobContext
    from ns_studio.orchestrator import plan_job
    from ns_studio.policy.regen import run_job_guarded
    from ns_studio.variation.draw import draw_combo, record_combo
    from ns_studio.variation.stages import job_series_fixed

    env = open_env(fake, acquirer, paths=paths)
    job_id = str(params["job_id"])
    channel = get_channel(env.paths, str(params["channel"]))
    jobs.prepare_plan("master_first", None)
    job_dir = env.paths.job_dir(job_id)
    if (job_dir / "job.json").exists():
        ctx = jobs.load_job(env, job_id)
    else:
        now = datetime.now(UTC)
        # the series design pins the caption style and visual treatment of every part
        combo = draw_combo(
            env.db, channel, rng=random.Random(), now=now, fixed=job_series_fixed(job_dir),
            on_repeat=lambda why: env.log.warn("variation_repeated", why, code="NS-W325", job_id=job_id),
        )
        record_combo(env.db, channel.slug, job_id, "long", combo, now)
        job_dir.mkdir(parents=True, exist_ok=True)
        ctx = JobContext(
            job_id=job_id,
            channel=channel,
            combo=combo,
            fmt="master_first",
            paths=env.paths,
            job_dir=job_dir,
            db=env.db,
            runtime=env.runtime,
            services=env.services,
            log=env.log,
        )
        ctx.write_json(
            "job.json",
            {
                "job_id": job_id,
                "channel": channel.slug,
                "fmt": "master_first",
                "combo": combo.model_dump(mode="json"),
                "created_at": now.isoformat(timespec="seconds"),
            },
        )
    names = [s.name for s in plan_job("master_from_script")]
    run_job_guarded(watched(ctx, names, 0, 1, report))
    return output_ids(env.db, job_id)


def queue_next_part(slug: str) -> list[dict[str, Any]]:
    """Queue production of the lowest approved part of story `slug` (`Workshop.produce`); no model is called.

    Returns `[{part, job_id, title}]`. Raises `WorkshopError` when the story is unknown, has no design yet
    or has no approved part.
    """
    from ns_studio.series.store import WorkshopError

    with workshop_session(fake=READ_ONLY) as ws:
        parts = ws.status(slug)["parts"]
        nxt = next((int(p["n"]) for p in parts if p["status"] == "approved"), None)
        if nxt is None:
            raise WorkshopError(f"story {slug!r} has no approved part waiting; approve a written part first")
        return ws.produce(slug, nxt)


def sweep_parts(
    active_job_ids: Collection[str], job_statuses: Mapping[str, str] | None = None
) -> int:
    """Settle every part that says `producing` but has no queued or running dashboard job behind it.

    Covers what the per-job hooks cannot: a row that was removed, a restore of the database, a part whose
    job was cancelled while the dashboard was down. With `job_statuses` (job id -> status of every
    `series_part` job) a part is put back to `approved` only when its job is missing, `failed` or
    `cancelled`; one whose job is `done` is finished work and becomes `produced`. Without it every part
    without an active job is put back (the old rule). Returns how many parts were changed.
    """
    from ns_studio.series.store import StoryDir, WorkshopError

    freed = 0
    active = set(active_job_ids)
    try:
        with workshop_session(fake=READ_ONLY) as ws:
            for story in ws.list_stories():
                try:
                    sd = StoryDir.open(ws.paths, str(story["slug"]))
                    status = sd.read_status()
                    for n, state in sorted(status.parts.items()):
                        alive = state.job_id is not None and state.job_id in active
                        if state.status != "producing" or alive:
                            continue
                        done = (
                            job_statuses is not None
                            and state.job_id is not None
                            and job_statuses.get(state.job_id) == "done"
                        )
                        try:
                            ws._set_part(sd, status, n, "produced" if done else "approved")
                        except WorkshopError:  # series.md has no such part: status.yaml is saved already
                            pass
                        freed += 1
                except (WorkshopError, KeyError, ValueError):
                    continue
    except (WorkshopError, OSError):
        return freed
    return freed


# ---------------------------------------------------------------- the voice setup asked for


def _voice_post(base_url: str, route: str, body: dict[str, Any]) -> dict[str, Any]:
    """`ns_studio.voice_setup.http_post`, with an absent model reported as the code the Library page knows.

    The setup voice of a "design" choice needs the VoiceDesign talker, which is not part of the base download:
    `NS-E314` makes the page offer "Get voice design model" before it tries again.
    """
    from ns_common.errors import NsError
    from ns_studio import voice_setup

    try:
        return voice_setup.http_post(base_url, route, body)
    except NsError as e:
        if "is not downloaded" in e.detail and route.endswith("/design"):
            raise NsError("NS-E314") from e
        raise


def apply_pending_voice(
    *, acquirer: Acquirer | None = None, fake: Collection[str] = (), paths: Paths | None = None
) -> str | None:
    """Create the voice that setup queued in `config/pending_voice.json` (`voice_setup.apply_pending`).

    Returns its name, or None when nothing was queued or the voice exists already. A failure leaves the
    request queued and propagates (`NS-E314` when the VoiceDesign model is missing).
    """
    from ns_studio import voice_setup

    env = open_env(fake, acquirer, paths=paths)
    try:
        return voice_setup.apply_pending(env, post=None if "tts" in env.fake else _voice_post)
    finally:
        env.db.close()
