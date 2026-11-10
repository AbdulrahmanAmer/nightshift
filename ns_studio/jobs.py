"""Job creation, resume and service wiring shared by the CLI and the nightly run (component C7)."""

from __future__ import annotations

import importlib
import json
import random
import sqlite3
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, cast

import ns_common.testguard as testguard
from ns_common.config import load_runtime
from ns_common.db import Db, claim_dir, claim_job_id
from ns_common.logging import NsLogger
from ns_common.models import Channel, Combo, Runtime
from ns_common.paths import Paths
from ns_common.runtime_read import read_runtime
from ns_common.secrets_names import KEYRING_SERVICE, SHORT_NAMES
from ns_studio import ownership
from ns_studio.channels.crud import (
    add_channel,
    all_channels,
    get_channel,
    pool_name_problems,
    sync_channel_row,
)
from ns_studio.context import FAKEABLE, JobContext, Services, build_services
from ns_studio.errors import StudioError
from ns_studio.variation.draw import draw_combo, record_combo

# Imported lazily by `load_stage_modules`; each registers its stages (the order of a plan is FORMAT_STAGES).
STAGE_MODULES: tuple[str, ...] = (
    "ns_studio.text.stages_shorts",
    "ns_studio.text.stages_master",
    "ns_studio.media.stages_media",
    "ns_studio.series.stages",
)
# Fixtures served by the fake LLM are written for a 50 s Short at the "standard" pace; validators stay real.
FAKE_LLM_COMBO: dict[str, str] = {"length_bucket": "50", "pacing": "standard"}
Fmt = Literal["master_first", "shorts_only", "series_compilation", "clip", "master_from_script"]
FORMATS: tuple[str, ...] = ("master_first", "shorts_only", "series_compilation")


def load_stage_modules(modules: Collection[str] | None = None) -> list[str]:
    """Import the C5/C6 stage modules that exist, then complete the stage table; returns the names imported.

    A module that is missing (not yet built) is skipped; an import error inside an existing
    module propagates. After the imports `install_plan_extras` puts the Short combos and the policy gates into
    the table, the same for the whole process; a job with its policy off leaves the gates out of its own plan.
    """
    loaded: list[str] = []
    for name in STAGE_MODULES if modules is None else modules:
        try:
            importlib.import_module(name)
        except ModuleNotFoundError as e:
            if e.name is not None and (e.name == name or name.startswith(e.name + ".")):
                continue
            raise
        loaded.append(name)
    install_plan_extras()
    return loaded


def install_plan_extras() -> None:
    """Put the per-Short combo stage and the two policy gates into every plan they belong to.

    `install_short_combos` and `install_gates` are idempotent and write an entry only when it lacks the stage,
    so the table is written the first time the stage modules load and never again for a job. Clip and series
    compilation plans get neither (the clip plan is the Clipper's own, set when it is imported).
    """
    from ns_studio.orchestrator import COMBO_FORMATS, GATED_FORMATS
    from ns_studio.policy import gate
    from ns_studio.variation.stages import install_short_combos

    for fmt in COMBO_FORMATS:  # first: the text gate of a master-first plan goes after `short_combos`
        install_short_combos(fmt)
    for fmt in GATED_FORMATS:
        gate.install_gates(fmt)


def parse_fake(value: str | None) -> frozenset[str]:
    """Parse `--fake-services[=list]`: None -> none, '' / 'all' -> everything, 'llm,tts' -> subset."""
    if value is None:
        return frozenset()
    text = value.strip().lower()
    if text in ("", "all", "true"):
        return FAKEABLE
    names = {p.strip() for p in text.split(",") if p.strip()}
    unknown = names - FAKEABLE
    if unknown:
        known = sorted(FAKEABLE)
        raise ValueError(f"unknown fake service(s): {', '.join(sorted(unknown))}; choose from {known}")
    return frozenset(names)


def _warn(event: str, msg: str) -> None:
    """Log a key-lookup problem; logging must never be the reason the lookup fails."""
    try:
        NsLogger("studio", Paths.from_env().logs).warn(event, msg)
    except Exception:  # noqa: BLE001 - a diagnostic line is not worth a crash at start-up
        pass


def load_keys() -> dict[str, str]:
    """Pexels and Pixabay keys: Windows keyring (service 'Nightshift'), else `config/secrets.dpapi`.

    Every keyring call is wrapped in `except Exception`: in the S4U nightly session the backend raises a
    non-`KeyringError` type (Windows CredRead error 1312, "no such logon session"), and that must not stop
    `open_env` before the DPAPI file is tried. When both sources are empty the key stays missing and the clip
    source raises the coded `NS-E501` at its first use, so text-only runs never see it.
    """
    keyring: Any = None
    try:
        keyring = importlib.import_module("keyring")
    except ImportError:
        pass
    except Exception as e:  # noqa: BLE001 - a broken backend import is treated as "no keyring"
        _warn("keyring_import_failed", f"{type(e).__name__}: {e}")
    if testguard.real_keyring(keyring):  # a test run never asks the real Credential Manager: "no keyring"
        keyring = None
    keys: dict[str, str] = {}
    for name, short in SHORT_NAMES.items():  # the names setup and Settings store, not the short ones
        value: str | None = None
        if keyring is not None:
            try:
                value = keyring.get_password(KEYRING_SERVICE, name)
            except Exception as e:  # noqa: BLE001
                _warn("keyring_failed", f"{name}: {type(e).__name__}: {e}")
        if not value:
            value = _dpapi_secret(name)  # the nightly task may not reach Credential Manager (S4U session)
        if value:
            keys[short] = str(value)
    return keys


def _dpapi_secret(name: str) -> str | None:
    """The key from `config/secrets.dpapi` (`ns_common.secret_file`), or None when absent or unreadable."""
    paths = Paths.from_env()  # outside the try: a test run with no home of its own is refused, not hidden
    try:
        from ns_common.secret_file import load_secret  # imported here: it loads ctypes

        return load_secret(paths, name)
    except Exception as e:  # noqa: BLE001 - OSError, ValueError, ctypes failures: all mean "no key here"
        _warn("secret_file_failed", f"{name}: {type(e).__name__}: {e}")
        return None


@dataclass
class Env:
    """Opened paths, database, runtime config, services and logger for one CLI invocation."""

    paths: Paths
    db: Db
    runtime: Runtime
    services: Services
    log: NsLogger
    fake: frozenset[str]
    #: `--policy/--no-policy` of this invocation for the jobs it makes (None: on when the gates can run).
    #: Only `studio run --daily` sets it: the nightly makes its jobs in `daily.py`.
    policy: bool | None = None


#: What `studio status` says when it found no database, or an empty file, and so made a new empty one.
NEW_DB_NOTE = (
    "note: {db} did not exist or was empty, so a new, empty one was made. A new install starts this way; if "
    "this install made videos before, their records are in its last backup (ns restore <the backup zip>)."
)


def db_is_new(paths: Paths) -> bool:
    """True when the database file is not there or holds zero bytes (opening it makes a new, empty one)."""
    try:
        return not paths.db_path.is_file() or paths.db_path.stat().st_size == 0
    except OSError:
        return False


def open_env(fake: frozenset[str] = frozenset(), *, light: bool = False) -> Env:
    """Open the studio environment from `NIGHTSHIFT_HOME`; real services are built but not contacted.

    `light=True` is for commands that never call a service (channel, review, status, learn): a missing
    `runtime.json` is tolerated and every service is a fake. So is a dev install (`runtime.json` says
    `dev_mode`, docs/DEV_MODE.md). A runtime.json that is there but cannot be used is NS-E125 naming it
    (`ns_common.runtime_read`, the reading setup and `ns doctor` share), and a database that cannot be opened
    is NS-E327 naming it: a line and exit 1 through the CLI's `guarded`, never a traceback.
    """
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    try:
        db.migrate()
    except (sqlite3.Error, OSError) as e:   # a folder of that name, a file another program holds, a bad file
        db.close()
        raise StudioError(
            "NS-E327", f"{paths.db_path} could not be opened ({type(e).__name__}: {e}); if another program "
            "holds it, close that program and run this again",
        ) from e
    if light:
        fake = FAKEABLE
    if light and paths.runtime_file.exists():
        runtime = read_runtime(paths)   # NS-E125 names the file and the way out, as `ns doctor` does
    elif paths.runtime_file.exists() or not fake:
        runtime = load_runtime(paths)
    else:
        runtime = Runtime.default("cpu-dev")
    if runtime.dev_mode:        # a dev install (docs/DEV_MODE.md) has no model: every service is its fake
        fake = FAKEABLE
    keys = {} if "clips" in fake else load_keys()
    services = build_services(runtime, fake=fake, paths=paths, keys=keys)
    return Env(paths, db, runtime, services, NsLogger("studio", paths.logs), fake)


def ensure_demo_channel(env: Env) -> Channel:
    """Create the 'demo' shorts-only channel used when fake services run on an empty install."""
    demo = Channel(
        slug="demo",
        display_name="Demo",
        niche="original short horror fiction set in everyday places",
        seed_keywords=["horror story", "scary story"],
        voice_notes="Plain, intimate first person.",
        format="shorts_only",
    )
    add_channel(env.paths, env.db, demo)
    env.log.info("demo_channel_created", slug=demo.slug)
    return get_channel(env.paths, demo.slug)


def resolve_channels(env: Env, slug: str | None) -> list[Channel]:
    """The channels a run applies to: the named one, else every active one (demo when faking)."""
    if slug:
        return [get_channel(env.paths, slug)]
    active = [c for c in all_channels(env.paths) if c.status == "active"]
    if not active and env.fake:
        active = [ensure_demo_channel(env)]
    if not active:
        raise StudioError("NS-E322", "no active channel configured; run 'studio channel add'")
    return active


def make_job(
    env: Env,
    channel: Channel,
    fmt: str,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
    fixed: dict[str, str] | None = None,
    record: bool = True,
    origin: str = "manual",
    run_id: str | None = None,
    policy: bool | None = None,
) -> JobContext:
    """Draw a combo, record it in the ledger unless `record` is False, create the job dir and context.

    `origin` says who made the job: `daily` (the nightly run, `run_id` its run row: after a kill the next
    nightly resumes it) or `manual` (`studio run`, the dashboard: offered in the Queue with Retry). The owner
    file is written before the combos row (`ownership`), so a job the ledger knows is never unowned.

    `policy` is the job's choice of the policy gates (None: `env.policy`, and when that is None too, on when
    the gates are available). It is resolved here, once, and kept in `job.json`: a resume runs the same plan.
    """
    choice = prepare_plan(fmt, env.policy if policy is None else policy)
    now = now or datetime.now(UTC)
    rng = rng or random.Random()
    # The setup channel exists only as YAML; `ideas`, `stories`, `combos` and `outputs` reference its row.
    sync_channel_row(env.db, channel, now)
    unknown = pool_name_problems(channel.pools)
    if unknown:
        raise StudioError("NS-E320", "; ".join(unknown))
    if "llm" in env.fake:
        fixed = {**FAKE_LLM_COMBO, **(fixed or {})}  # the fixtures are written for this budget
    combo = draw_combo(
        env.db, channel, rng=rng, now=now, fixed=fixed,
        on_repeat=lambda why: env.log.warn("variation_repeated", why, code="NS-W325", channel=channel.slug),
    )
    # The job takes its id by making its folder: an id that already names a job is drawn again, so a new job
    # can never write into another job's folder or over its `job.json` (D-ids). The folder comes first; the
    # owner file and the combos row are written for an id that is ours.
    job_id = claim_job_id(channel.slug, claim_dir(env.paths.job_dir), now)
    item = "long" if fmt == "master_first" else "s1"
    if record:
        ownership.announce_job(
            env.paths, job_id, origin=origin, channel=channel.slug, fmt=fmt, run_id=run_id, created=now
        )
        record_combo(env.db, channel.slug, job_id, item, combo, now)
    job_dir = env.paths.job_dir(job_id)
    ctx = JobContext(
        job_id=job_id,
        channel=channel,
        combo=combo,
        fmt=cast(Fmt, fmt),
        paths=env.paths,
        job_dir=job_dir,
        db=env.db,
        runtime=env.runtime,
        services=env.services,
        log=env.log,
        policy=choice,
    )
    ctx.write_json(
        "job.json",
        {
            "job_id": job_id,
            "channel": channel.slug,
            "fmt": fmt,
            "combo": combo.model_dump(mode="json"),
            "created_at": now.isoformat(timespec="seconds"),
            "policy": choice,
        },
    )
    env.log.info(
        "job_created", job_id=job_id, channel=channel.slug, fmt=fmt, combo_id=combo.combo_id, policy=choice
    )
    return ctx


def make_script_job(
    env: Env,
    channel: Channel,
    fmt: str,
    script: str,
    title: str | None = None,
    *,
    rng: random.Random | None = None,
    policy: bool | None = None,
) -> JobContext:
    """`make_job` for a story the owner wrote: the text becomes the job's `script.md`, not the model's idea.

    The text goes in the job folder as `script.md` (the file a series part's `script_source.json` points at)
    and `script_source.json` names it, marked `owner_script`, so `run_job` runs the owner-script plan
    (`plan_format`) and every stage after the import is unchanged. `plan_script` checks everything that would
    fail only after the voice or the render, before a job exists (NS-E303): banned phrases, sentence length,
    chapters, and the video length. It also picks the pace from the channel's own pool and, for a Short, the
    length bucket, so the finished video lands inside the QC band.
    """
    from ns_studio.series.plain import ScriptRefused, plan_script
    from ns_studio.series.store import write_text

    if fmt not in ("shorts_only", "master_first"):
        raise StudioError("NS-E306", f"a script becomes a Short or a long video, not {fmt!r}")
    try:
        plan = plan_script(script, fmt, list(channel.pools.pacings), title, channel.voice.speed)
    except ScriptRefused as e:
        raise StudioError("NS-E303", str(e)) from e
    fixed = {"pacing": plan.pacing}
    if plan.length_bucket is not None:
        fixed["length_bucket"] = str(plan.length_bucket)
    ctx = make_job(env, channel, fmt, rng=rng, fixed=fixed, policy=policy)
    write_text(ctx.file("script.md"), plan.markdown)
    ctx.write_json(
        "script_source.json",
        {"kind": "script_import", "owner_script": True, "script": "script.md", "shorts": [],
         "title": plan.doc.title},
    )
    env.log.info(
        "script_job_created", job_id=ctx.job_id, words=plan.doc.words(), chapters=len(plan.doc.chapters),
        pacing=plan.pacing, seconds=round(plan.seconds),
    )
    return ctx


def load_job(env: Env, job_id: str, *, policy: bool | None = None) -> JobContext:
    """Rebuild the context of an existing job from its `job.json` (used by `--resume`).

    The job keeps the policy it was made with (`job.json` `policy`; a folder from before the key runs the
    gates when they are available, `orchestrator.policy_of`). An explicit `policy` (`--resume --policy` or
    `--no-policy`) wins for this run; `record_policy` keeps it for the next one. Nothing is written here.
    """
    from ns_studio.orchestrator import policy_of

    job_dir = env.paths.job_dir(job_id)
    f = job_dir / "job.json"
    if not f.exists():
        raise StudioError("NS-E326", f"job {job_id} has no job.json under {job_dir}")
    data = json.loads(f.read_text(encoding="utf-8"))
    channel = get_channel(env.paths, data["channel"])
    sync_channel_row(env.db, channel)
    return JobContext(
        job_id=job_id,
        channel=channel,
        combo=Combo.model_validate(data["combo"]),
        fmt=cast(Fmt, data["fmt"]),
        paths=env.paths,
        job_dir=job_dir,
        db=env.db,
        runtime=env.runtime,
        services=env.services,
        log=env.log,
        policy=policy_of(data) if policy is None else policy,
    )


def record_policy(ctx: JobContext, policy: bool | None) -> None:
    """Keep an explicit `--resume --policy/--no-policy` in the job's `job.json`; None (no flag) writes none.

    Called after the last refusal of a resume and before the job runs, so a refused resume changes nothing and
    a resume that is killed or fails keeps the choice for the next one.
    """
    if policy is None:
        return
    data = ctx.read_json("job.json")
    if isinstance(data, dict) and data.get("policy") is not policy:
        ctx.write_json("job.json", {**data, "policy": policy})
        ctx.log.info("job_policy_changed", job_id=ctx.job_id, policy=policy)


def prepare_plan(fmt: str, policy: bool | None) -> bool:
    """The policy a new job of `fmt` runs with: `policy`, or with None, whether the gates can run.

    Writes nothing: the gates and the Short combos are in the stage table since the stage modules loaded
    (`load_stage_modules`), and a job whose policy is off leaves the gates out of its own plan
    (`orchestrator.plan_names`). `fmt` does not change the answer; a plan without gate stages (a clip) just
    has nothing to leave out.
    """
    from ns_studio.policy import gate

    return gate.available() if policy is None else bool(policy)
