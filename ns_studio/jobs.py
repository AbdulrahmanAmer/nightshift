"""Job creation, resume and service wiring shared by the CLI and the nightly run (component C7)."""

from __future__ import annotations

import importlib
import json
import random
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from ns_common.config import load_runtime
from ns_common.db import Db, new_job_id
from ns_common.logging import NsLogger
from ns_common.models import Channel, Combo, Runtime
from ns_common.paths import Paths
from ns_studio.channels.crud import add_channel, all_channels, get_channel
from ns_studio.context import FAKEABLE, JobContext, Services, build_services
from ns_studio.errors import StudioError
from ns_studio.variation.draw import draw_combo, record_combo

# Imported lazily by `load_stage_modules`; each registers its stages and appends them to FORMAT_STAGES.
STAGE_MODULES: tuple[str, ...] = ("ns_studio.text.stages_shorts", "ns_studio.media.stages_media")
KEYRING_SERVICE = "Nightshift"
Fmt = Literal["master_first", "shorts_only", "series_compilation"]
FORMATS: tuple[str, ...] = ("master_first", "shorts_only", "series_compilation")


def load_stage_modules(modules: Collection[str] | None = None) -> list[str]:
    """Import the C5/C6 stage modules that exist; returns the names imported.

    A module that is missing (not yet built) is skipped; an import error inside an existing
    module propagates.
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
    return loaded


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


def load_keys() -> dict[str, str]:
    """Pexels and Pixabay keys from the Windows keyring (service 'Nightshift'); {} when unavailable."""
    try:
        keyring = importlib.import_module("keyring")
    except ImportError:
        return {}
    keyring_error = importlib.import_module("keyring.errors").KeyringError
    keys: dict[str, str] = {}
    for name in ("pexels", "pixabay"):
        try:
            value = keyring.get_password(KEYRING_SERVICE, name)
        except keyring_error:
            value = None  # a broken backend must not stop a text-only run
        if value:
            keys[name] = str(value)
    return keys


@dataclass
class Env:
    """Opened paths, database, runtime config, services and logger for one CLI invocation."""

    paths: Paths
    db: Db
    runtime: Runtime
    services: Services
    log: NsLogger
    fake: frozenset[str]


def open_env(fake: frozenset[str] = frozenset()) -> Env:
    """Open the studio environment from `NIGHTSHIFT_HOME`; real services are built but not contacted."""
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    if paths.runtime_file.exists() or not fake:
        runtime = load_runtime(paths)
    else:
        runtime = Runtime.default("cpu-dev")
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
        raise StudioError("NS-E309", "no active channel configured; run 'studio channel add'")
    return active


def make_job(
    env: Env,
    channel: Channel,
    fmt: str,
    *,
    now: datetime | None = None,
    rng: random.Random | None = None,
    fixed: dict[str, str] | None = None,
) -> JobContext:
    """Draw a combo, record it in the ledger, create the job directory and return its context."""
    now = now or datetime.now(UTC)
    rng = rng or random.Random()
    combo = draw_combo(env.db, channel, rng=rng, now=now, fixed=fixed)
    job_id = new_job_id(channel.slug, now)
    item = "long" if fmt == "master_first" else "s1"
    record_combo(env.db, channel.slug, job_id, item, combo, now)
    job_dir = env.paths.job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
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
    )
    ctx.write_json(
        "job.json",
        {
            "job_id": job_id,
            "channel": channel.slug,
            "fmt": fmt,
            "combo": combo.model_dump(mode="json"),
            "created_at": now.isoformat(timespec="seconds"),
        },
    )
    env.log.info("job_created", job_id=job_id, channel=channel.slug, fmt=fmt, combo_id=combo.combo_id)
    return ctx


def load_job(env: Env, job_id: str) -> JobContext:
    """Rebuild the context of an existing job from its `job.json` (used by `--resume`)."""
    job_dir = env.paths.job_dir(job_id)
    f = job_dir / "job.json"
    if not f.exists():
        raise StudioError("NS-E313", f"job {job_id} has no job.json under {job_dir}")
    data = json.loads(f.read_text(encoding="utf-8"))
    return JobContext(
        job_id=job_id,
        channel=get_channel(env.paths, data["channel"]),
        combo=Combo.model_validate(data["combo"]),
        fmt=cast(Fmt, data["fmt"]),
        paths=env.paths,
        job_dir=job_dir,
        db=env.db,
        runtime=env.runtime,
        services=env.services,
        log=env.log,
    )
