"""Shared test helpers for the studio core (component C7)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ns_common.config import save_channel
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


def yaml_only_channel(paths: Paths, **kw: Any) -> Channel:
    """Write a channel the way installer step s07 does: the YAML profile only, no `channels` table row."""
    ch = make_channel(**kw)
    save_channel(paths, ch)
    return ch


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
    channel_row: bool = True,
) -> JobContext:
    """Build a JobContext on a temp home with fakes, a migrated DB and a created job directory.

    `channel_row=False` leaves the `channels` table empty, like a real install whose only channel was
    written as YAML by the installer (see `yaml_only_channel`).
    """
    del home  # NIGHTSHIFT_HOME is already set by the tmp_home fixture
    paths = Paths.from_env()
    paths.ensure()
    channel = channel or make_channel()
    runtime = Runtime.default("cpu-dev")
    services = build_services(runtime, fake=set(FAKEABLE) if fake is None else fake, paths=paths)
    db = Db(paths.db_path)
    db.migrate()
    if channel_row:
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


def add_output(
    db: Db,
    job_id: str,
    item: str,
    *,
    channel: str = "after-dark",
    title: str = "A title",
    status: str = "pending",
    created_at: str = "2026-10-20T10:00:00+00:00",
    combo: Combo | None = None,
    duration_s: float = 40.0,
    path: str = "",
    note: str | None = None,
) -> str:
    """Insert an `outputs` row (and its combo) and return the output id."""
    kind = "long" if item == "long" else "short"
    cmb = combo or make_combo(combo_id=f"c-{job_id}-{item}")
    db.execute(
        "insert or ignore into channels(slug, display_name, created_at) values (?,?,?)",
        (channel, channel, "2026-10-01T00:00:00"),
    )
    db.execute(
        "insert or replace into combos(combo_id, channel, job_id, item, hook, structure, pov_tone, pacing, "
        "caption_style, visual_treatment, title_formula, length_bucket, created_at) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (cmb.combo_id, channel, job_id, item, cmb.hook, cmb.structure, cmb.pov_tone, cmb.pacing,
         cmb.caption_style, cmb.visual_treatment, cmb.title_formula, cmb.length_bucket, created_at),
    )
    output_id = f"{job_id}/{item}"
    db.execute(
        "insert into outputs(output_id, job_id, channel, kind, k, title, path, duration_s, combo_id, status, "
        "review_note, created_at) values (?,?,?,?,?,?,?,?,?,?,?,?)",
        (output_id, job_id, channel, kind, None if item == "long" else int(item[1:]), title, path, duration_s,
         cmb.combo_id, status, note, created_at),
    )
    return output_id


def add_analytics(
    db: Db,
    output_id: str,
    date: str,
    *,
    views: int = 1000,
    viewed_pct: float | None = 70.0,
    avg_pct_viewed: float | None = 80.0,
    avg_view_duration_s: float | None = 30.0,
    subs_gained: int = 0,
) -> None:
    """Insert an `analytics` row."""
    db.execute(
        "insert or replace into analytics(output_id, date, views, viewed_pct, avg_pct_viewed, "
        "avg_view_duration_s, likes, comments, shares, subs_gained) values (?,?,?,?,?,?,?,?,?,?)",
        (output_id, date, views, viewed_pct, avg_pct_viewed, avg_view_duration_s, 0, 0, 0, subs_gained),
    )


class InProcessRunner:
    """A `StudioRunner` stand-in that runs the child's handler in this process (dashboard adapter tests).

    The studio plans and stages these tests register exist only in this interpreter, so they cannot cross a
    process boundary. Everything else is the real path: `studio_job.HANDLERS[kind]` prints its protocol
    events through `studio_job.emit`, which is captured here and handed to `on_event` exactly as the real
    runner does; `check_cancel` may raise after each event.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def run(
        self,
        kind: str,
        args: dict[str, Any],
        *,
        on_event: Any = lambda _e: None,
        check_cancel: Any = lambda: None,
        on_cancel: Any = None,
    ) -> dict[str, Any]:
        """Run `kind` here; returns the same payload the child's `result` event would carry."""
        from unittest import mock

        from ns_dashboard import studio_job

        self.calls.append((kind, args))

        def capture(ev: str, **data: Any) -> None:
            on_event({"ev": ev, **data})
            check_cancel()

        no_supervisor = {**args, "supervisor_url": None}  # fakes never acquire: no supervisor HTTP
        with mock.patch.object(studio_job, "emit", capture):
            return studio_job.HANDLERS[kind](no_supervisor)
