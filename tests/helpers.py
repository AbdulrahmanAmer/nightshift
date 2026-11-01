"""Shared test helpers for the studio core (component C7)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ns_common.db import Db, new_job_id
from ns_common.logging import NsLogger
from ns_common.models import Channel, Combo, Runtime
from ns_common.paths import Paths
from ns_studio.context import FAKEABLE, JobContext, build_services


def make_channel(**kw: Any) -> Channel:
    """A valid channel; keyword arguments override fields."""
    base: dict[str, Any] = {
        "slug": "after-dark",
        "display_name": "After Dark",
        "niche": "original short horror fiction set in everyday places",
        "language": "en",
        "seed_keywords": ["horror story", "scary story"],
        "voice_notes": "Plain, intimate first person.",
        "format": "shorts_only",
    }
    return Channel(**(base | kw))


def make_combo(**kw: Any) -> Combo:
    """A valid combo; keyword arguments override fields."""
    base: dict[str, Any] = {
        "combo_id": "c-test",
        "hook": "cold_open",
        "structure": "twist",
        "pov_tone": "first_person_intimate",
        "pacing": "standard",
        "caption_style": "punch_yellow",
        "visual_treatment": "dark_slowzoom",
        "title_formula": "question",
        "length_bucket": 50,
    }
    return Combo(**(base | kw))


def make_ctx(
    home: Path,
    *,
    fmt: str = "shorts_only",
    channel: Channel | None = None,
    combo: Combo | None = None,
    fake: set[str] | None = None,
) -> JobContext:
    """Build a JobContext on a temp home with fakes, a migrated DB and a created job directory."""
    del home  # NIGHTSHIFT_HOME is already set by the tmp_home fixture
    paths = Paths.from_env()
    paths.ensure()
    channel = channel or make_channel()
    runtime = Runtime.default("cpu-dev")
    services = build_services(runtime, fake=set(FAKEABLE) if fake is None else fake, paths=paths)
    db = Db(paths.db_path)
    db.migrate()
    db.execute(
        "insert or ignore into channels(slug, display_name, created_at) values (?,?,?)",
        (channel.slug, channel.display_name, "2026-11-01T00:00:00"),
    )
    job_id = new_job_id(channel.slug)
    job_dir = paths.job_dir(job_id)
    job_dir.mkdir(parents=True, exist_ok=True)
    return JobContext(
        job_id=job_id,
        channel=channel,
        combo=combo or make_combo(),
        fmt=fmt,  # type: ignore[arg-type]
        paths=paths,
        job_dir=job_dir,
        db=db,
        runtime=runtime,
        services=services,
        log=NsLogger("studio", paths.logs, echo=False),
        prompt_versions={},
    )
