"""Pre-tag (final-dashboard row 1): a series part whose job finished stays finished.

The start sweep used to put every `producing` part without a queued or running job back to `approved`; a
part whose job was `done` was one of them, so "Produce next part" made it a second time.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_dashboard import studio_bridge
from ns_dashboard.errors import ApiError
from ns_dashboard.events import EventHub
from ns_dashboard.fakes import FakeSeriesAdapter, seed_channel
from ns_dashboard.jobs import JobRecord, JobStore, JobWorker
from ns_dashboard.state import open_db
from ns_studio.series.store import PartState, StoryDir


def seed_story(paths: Paths, parts: dict[int, PartState]) -> str:
    seed_channel(open_db(paths), paths, "after-dark")
    with studio_bridge.workshop_session(fake=studio_bridge.READ_ONLY) as ws:
        slug = ws.start("after-dark")
        sd = StoryDir.open(ws.paths, slug)
        status = sd.read_status()
        status.parts.update(parts)
        sd.write_status(status)
    return slug


def states(slug: str) -> dict[int, str]:
    with studio_bridge.workshop_session(fake=studio_bridge.READ_ONLY) as ws:
        return {n: p.status for n, p in StoryDir.open(ws.paths, slug).read_status().parts.items()}


def test_the_real_sweep_resets_a_part_only_when_its_job_is_missing_failed_or_cancelled(tmp_home: Path):
    paths = Paths.from_env()
    slug = seed_story(
        paths,
        {
            1: PartState(status="producing", job_id="done-job"),
            2: PartState(status="producing", job_id="failed-job"),
            3: PartState(status="producing", job_id="cancelled-job"),
            4: PartState(status="producing", job_id="gone-job"),
            5: PartState(status="producing", job_id="live-job"),
            6: PartState(status="producing", job_id=None),
        },
    )
    statuses = {"done-job": "done", "failed-job": "failed", "cancelled-job": "cancelled"}
    statuses["live-job"] = "running"
    assert studio_bridge.sweep_parts({"live-job"}, statuses) == 5
    assert states(slug) == {
        1: "produced",  # finished work is kept (and no longer says "producing")
        2: "approved", 3: "approved", 4: "approved", 6: "approved",
        5: "producing",  # a job still runs behind it
    }  # fmt: skip
    assert studio_bridge.sweep_parts({"live-job"}, statuses) == 0  # settled


def test_a_part_that_already_says_produced_is_left_alone(tmp_home: Path):
    paths = Paths.from_env()
    slug = seed_story(paths, {1: PartState(status="produced", job_id="done-job")})
    assert studio_bridge.sweep_parts(set(), {"done-job": "done"}) == 0
    assert states(slug) == {1: "produced"}


def test_finish_part_moves_only_the_part_of_that_job(tmp_home: Path):
    paths = Paths.from_env()
    slug = seed_story(
        paths,
        {1: PartState(status="producing", job_id="job-a"), 2: PartState(status="producing", job_id="job-b")},
    )
    studio_bridge.finish_part({"story_slug": slug, "part_n": 1, "job_id": "job-a"})
    studio_bridge.finish_part({"story_slug": slug, "part_n": 2, "job_id": "somebody-else"})  # not its job
    studio_bridge.finish_part({"story_slug": "no-such-story", "part_n": 1, "job_id": "job-a"})  # harmless
    assert states(slug) == {1: "produced", 2: "producing"}


def worker_with_hooks(paths: Paths):
    store = JobStore(open_db(paths))
    series = FakeSeriesAdapter(store.db, paths)
    series.stories = [
        {"slug": "the-lift", "channel": "after-dark", "title": "The Lift",
         "parts": [{"n": 1, "title": "Floor 13", "status": "approved", "job_id": None}]}
    ]  # fmt: skip

    def release(rec: JobRecord) -> None:
        series.release({**rec.params, "job_id": rec.job_id})

    def finish(rec: JobRecord) -> None:
        series.finish({**rec.params, "job_id": rec.job_id})

    def make() -> JobWorker:
        return JobWorker(
            store, {"series_part": lambda ctl: {"output_ids": []}}, EventHub(),
            NsLogger("dashboard", paths.logs), on_recover={"series_part": release},
            on_end={"series_part": release},
            on_done={"series_part": finish}, sweep=series.sweep,
        )  # fmt: skip

    return store, series, make


def test_a_finished_part_is_not_queued_again_after_the_dashboard_restarts(tmp_home: Path):
    paths = Paths.from_env()
    paths.ensure()
    store, series, make = worker_with_hooks(paths)
    worker = make()
    (queued,) = series.queue_next("the-lift")
    assert worker.run_next() is True
    (rec,) = store.list_jobs(kind="series_part")
    assert rec.status == "done" and rec.job_id == queued["job_id"]
    assert series.stories[0]["parts"][0]["status"] == "produced"  # the job end hook moved it

    again = make()  # Quit and reopen, the 03:00 engine start, or Import settings
    again.start()
    again.stop()
    assert series.stories[0]["parts"][0]["status"] == "produced"  # the sweep did not reset it
    with pytest.raises(ApiError) as ei:
        series.queue_next("the-lift")  # "Produce next part" has nothing to queue a second time
    assert ei.value.status == 409


def test_a_part_left_producing_by_the_old_version_is_settled_by_the_sweep(tmp_home: Path):
    paths = Paths.from_env()
    paths.ensure()
    store, series, make = worker_with_hooks(paths)
    worker = make()
    (queued,) = series.queue_next("the-lift")
    store.db.execute("UPDATE dashboard_jobs SET status = 'done' WHERE job_id = ?", (queued["job_id"],))
    assert series.stories[0]["parts"][0]["status"] == "producing"  # what 0.1.0 left behind
    worker.start()
    worker.stop()
    assert series.stories[0]["parts"][0]["status"] == "produced"
