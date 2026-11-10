"""Sequential, tenant-grouped job runner (component C7)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from itertools import groupby
from pathlib import Path

from ns_common.atomic import read_text_retry
from ns_common.errors import NsError
from ns_studio.acquirer import WAIT_CAP_S
from ns_studio.context import JobContext, owner_script_of
from ns_studio.media.job_formats import prepare_job_formats
from ns_studio.nightclock import job_line
from ns_studio.pause import require_not_paused
from ns_studio.stages import REGISTRY, Stage, StageState, Tenant, needs_embed, run_stage
from ns_studio.text.story_profile import note_job

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
# A story the owner wrote (JobRunRequest.script) is narrated as written: the import stage replaces everything
# that writes or rewrites the story. A Short has no `split` and `hooks` (the import writes its one part and
# the first sentence is its hook); a long video keeps `shorts_select`, which cuts Shorts from the chapters.
# Chosen by `plan_format` when the job folder holds the owner's `script_source.json`.
SHORTS_FROM_OWNER_SCRIPT_STAGES: tuple[str, ...] = (
    "script_import_short",
    "seo",
    "visual_plan",
    "voice",
    "captions",
    "clips",
    "render",
    "qc",
    "outbox",
)
MASTER_FROM_OWNER_SCRIPT_STAGES: tuple[str, ...] = (
    "script_import_long",
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
FORMAT_STAGES: dict[str, list[str]] = {
    "shorts_only": list(SHORTS_ONLY_STAGES),
    "master_first": list(MASTER_FIRST_STAGES),
    "master_from_script": list(MASTER_FROM_SCRIPT_STAGES),
    "shorts_from_owner_script": list(SHORTS_FROM_OWNER_SCRIPT_STAGES),
    "master_from_owner_script": list(MASTER_FROM_OWNER_SCRIPT_STAGES),
    "series_compilation": [],
    "clip": [],
}
# The table above is the bare one: the per-Short combo stage (COMBO_FORMATS), the two policy gates
# (GATED_FORMATS) and the Clipper's stages are put in by the modules that own them. A process that runs jobs
# does that when the stage modules load (`jobs.load_stage_modules`); a process that only READS a job's
# progress (`studio status`, the dashboard's recovery and status, the nightly's note) loads no stage
# module, so `plan_names`, the one reader, completes an entry that is still bare before it answers
# (`complete_plan`).
# A job never writes the table: a job whose policy is off leaves the gates out of its own copy (`plan_names`).
POLICY_STAGES: tuple[str, ...] = ("policy_text", "policy_final")  # `policy/gate.py` TEXT_STAGE, FINAL_STAGE
COMBO_FORMATS: tuple[str, ...] = ("master_first", "master_from_script", "master_from_owner_script")
GATED_FORMATS: tuple[str, ...] = (
    "shorts_only",
    "master_first",
    "master_from_script",
    "shorts_from_owner_script",
    "master_from_owner_script",
)
# The plan a job of this format runs when the owner's own script is its story (`plan_format`).
OWNER_SCRIPT_FORMATS: dict[str, str] = {
    "master_first": "master_from_owner_script",
    "shorts_only": "shorts_from_owner_script",
}


# What the table held when this module was written into memory. An entry that still equals it has not been
# completed in this process; any other value was put there by an installer or by a caller and is left alone.
_BARE_PLANS: dict[str, tuple[str, ...]] = {key: tuple(names) for key, names in FORMAT_STAGES.items()}


def complete_plan(key: str) -> None:
    """Complete the table entry `key` when it is still the bare plan of this module; else do nothing.

    Every reader of a plan comes through `plan_names`, which calls this first, so no process and no caller has
    to remember a set-up call before it names a job's stage. The Short combos go in before the gates (the text
    gate of a master-first plan sits after `short_combos`); a clip plan comes from the Clipper, which is
    imported for it. A Clipper that cannot be imported leaves the clip plan empty: such a process could not
    run a clip either, and a reader then names no stage instead of a wrong one.
    """
    current = FORMAT_STAGES.get(key)
    if current is None or tuple(current) != _BARE_PLANS.get(key):
        return
    if key == "clip":
        try:
            from ns_studio.clipper import stages_clip
        except ImportError:
            return
        stages_clip.register_clip_stages()
        return
    if key in COMBO_FORMATS:
        from ns_studio.variation.stages import install_short_combos

        install_short_combos(key)
    if key in GATED_FORMATS:
        from ns_studio.policy import gate  # imported here: gate imports this module

        gate.install_gates(key)


def _tenant_of(stage: Stage) -> Tenant:
    """GPU tenant of `stage` (grouping key)."""
    return stage.tenant


def plan_format(fmt: str, job_dir: Path) -> str:
    """The `FORMAT_STAGES` key a job runs: its format, or the script variant when it has a source script.

    A job folder that holds `script_source.json` skips the stages that write the story: a series part runs
    `master_from_script`, a story the owner wrote (`owner_script_of`) the plan for its format.
    """
    if owner_script_of(job_dir):
        return OWNER_SCRIPT_FORMATS.get(fmt, fmt)
    if fmt == "master_first" and (job_dir / "script_source.json").exists():
        return "master_from_script"
    return fmt


def policy_of(job: object) -> bool:
    """The policy choice a job recorded (`job.json` `policy`), else whether the gates can run now.

    A job folder from before the key (0.1.14), or one whose value is not a bool, reads as "no choice
    recorded": the gates run when they are available (`gate.available`), which is what `studio run` did then.
    """
    value = job.get("policy") if isinstance(job, dict) else None
    if isinstance(value, bool):
        return value
    from ns_studio.policy import gate  # imported here: gate imports this module

    return gate.available()


def job_policy(job_dir: Path) -> bool:
    """`policy_of` the `job.json` in `job_dir`; a missing or unreadable file counts as no choice recorded."""
    try:
        data = json.loads(read_text_retry(job_dir / "job.json"))
    except (OSError, ValueError):
        data = None
    return policy_of(data)


def plan_names(key: str, policy: bool) -> list[str]:
    """The stage names of plan `key` for a job whose policy is `policy`: a new list, the table is not touched.

    With the policy off the gate stages (`POLICY_STAGES`) are left out; nothing else changes. The entry is
    completed first when this process has not done so (`complete_plan`), so the answer is the same in a
    process that runs jobs and in one that only reads their progress.
    """
    complete_plan(key)
    return [n for n in FORMAT_STAGES.get(key) or () if policy or n not in POLICY_STAGES]


def stage_names(fmt: str, job_dir: Path, policy: bool | None = None) -> list[str]:
    """The stage names the job in `job_dir` runs: one plan for the orchestrator, progress and idea check.

    `policy` is the job's choice (`JobContext.policy`); None reads it from the job's `job.json`
    (`job_policy`), for a reader that has no context (the owner records of `ownership`).
    """
    return plan_names(plan_format(fmt, job_dir), job_policy(job_dir) if policy is None else policy)


def plan_job(fmt: str, policy: bool = True) -> list[Stage]:
    """Resolve the ordered stages of plan `fmt` (`plan_names`); NS-E306 for unknown or unimplemented formats.

    `policy=False` leaves the gates out; the default is the plan as the table holds it.
    """
    names = plan_names(fmt, policy)
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


#: Formats a pause does not stop: a Clipper job cuts the owner's own video, accepted for a paused channel.
UNPAUSED_FORMATS = frozenset({"clip"})


def run_job(ctx: JobContext, *, resume: bool = True, dry_run: bool = False) -> list[str]:
    """Run every stage of `ctx.fmt` in order and return the names that were visited.

    Consecutive stages with the same GPU tenant share one acquisition; `dry_run` only lists the plan. The
    gates run only when the job's policy is on (`ctx.policy`).
    """
    stages = plan_job(plan_format(ctx.fmt, ctx.job_dir), ctx.policy)
    if dry_run:
        return [s.name for s in stages]
    note_job(ctx)  # a job that runs with the engine-test switch on stays marked as a test video for good
    prepare_job_formats(ctx)  # which format each video is drawn from, chosen once, before the first stage
    owner = f"studio:{ctx.job_id}"
    ran: list[str] = []

    def not_paused(stage: str) -> None:  # P47: no stage of a paused channel's story job starts, on any route
        if ctx.fmt not in UNPAUSED_FORMATS:
            ch = ctx.channel
            require_not_paused(ctx.paths, ctx.log, ch.slug, ch.display_name, ctx.job_id, stage)

    t0, outcome = ctx.clock.monotonic(), "done"
    try:
        for tenant, group in groupby(stages, key=_tenant_of):
            group_list = list(group)
            if tenant is not None:
                # Before the graphics card is asked for: a job paused between two groups must not make the
                # engine swap tenants and load a model it will not use (the check before every stage, below,
                # stays: the wait for the card can take hours, and a pause may come during it).
                not_paused(group_list[0].name)
                wait = _wait_cap(ctx, [s.name for s in stages], [s.name for s in group_list])
                with _timed(ctx, tenant, group_list[0].name, wait):
                    alias = "story" if tenant == "llama" else None
                    ctx.services.acquirer.acquire(tenant, owner, model_alias=alias, **wait)  # type: ignore[arg-type]
            _run_group(ctx, tenant, group_list, owner, ran, resume, not_paused)
    except BaseException as e:
        outcome = str(getattr(e, "code", None) or type(e).__name__)
        raise
    finally:
        job_line(ctx, "job_end", plan=plan_format(ctx.fmt, ctx.job_dir), fmt=str(ctx.fmt),
                 seconds=round(ctx.clock.monotonic() - t0, 3), outcome=outcome)
    return ran


def _wait_cap(ctx: JobContext, names: list[str], group: list[str]) -> dict[str, int]:
    """The `timeout_s` of the wait for the graphics card before the stages `group` ({} outside the nightly).
    In the nightly it is bounded by what the night can spend on it (`Night.wait_cap`, S7): the time left after
    the estimate of the stages the job still has to run. At or under 0 it is NS-E201 at once, marked
    `out_of_time` (`daily._attempt` then leaves the job at that stage for the next night)."""
    timeout: int | None = None
    if ctx.deadline is not None:
        done = {n for n, e in StageState.load(ctx.job_dir).entries.items() if e.get("status") == "done"}
        rest = [n for n in names[names.index(group[0]):] if n not in done]
        timeout = ctx.deadline.wait_cap(rest, WAIT_CAP_S)
        mine = [n for n in group if n not in done]
        if timeout is not None and timeout <= 0 and mine:
            raise NsError(
                "NS-E201", f"the night has no time left to wait for the graphics card before {mine[0]}",
                out_of_time=True, stage=mine[0],
            )
        if timeout is not None and not mine:  # a group a resume skips: never refused, never a long wait
            timeout = max(1, timeout)
    return {} if timeout is None else {"timeout_s": timeout}


@contextmanager
def _timed(ctx: JobContext, tenant: str, first: str, wait: dict[str, int]) -> Iterator[None]:
    """The `acquire` ledger line of the wait for `tenant`: its seconds, the wait and the load when the
    acquirer tells them apart (`last_timing`), the `timeout_s` it was given and how it ended."""
    t0, outcome = ctx.clock.monotonic(), "done"
    try:
        yield
    except BaseException as e:
        outcome = str(getattr(e, "code", None) or type(e).__name__)
        raise
    finally:
        timing = getattr(ctx.services.acquirer, "last_timing", None)
        wait_s, load_s = timing if isinstance(timing, tuple) and len(timing) == 2 else (None, None)
        job_line(ctx, "acquire", service=tenant, stage=first, seconds=round(ctx.clock.monotonic() - t0, 3),
                 wait_s=wait_s, load_s=load_s, timeout_s=wait.get("timeout_s"), outcome=outcome)


def _run_group(
    ctx: JobContext,
    tenant: Tenant,
    group_list: list[Stage],
    owner: str,
    ran: list[str],
    resume: bool,
    not_paused: Callable[[str], None],
) -> None:
    """Run one tenant group's stages; the tenant is released whatever happens."""
    try:
        for st in group_list:
            if needs_embed(st):
                not_paused(st.name)  # likewise before the embed service is started for this stage
            with embed_lease(ctx, st, owner):
                # The check no route gets past: immediately before EVERY stage, after any wait for the GPU
                # or the embed lease (up to hours); a pause waits for the stage under way only. Raising
                # here releases the tenant in the `finally` below, so a held job keeps no lease. (The
                # night's time check is inside `run_stage`, after it knows the stage really runs.)
                not_paused(st.name)
                run_stage(ctx, st, force=not resume)
            ran.append(st.name)
    finally:
        if tenant == "whisper":  # free the weights before the lock goes to the next tenant (llama)
            ctx.services.transcriber.unload()
        if tenant is not None:
            ctx.services.acquirer.release(tenant, owner)
