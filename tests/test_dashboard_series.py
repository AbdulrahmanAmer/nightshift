# ruff: noqa: E501
"""Gap 8: Library > Series lists the workshop stories and queues the next approved part."""

from __future__ import annotations

from pathlib import Path

import pytest

from ns_common.paths import Paths
from ns_dashboard import studio_bridge
from ns_dashboard.adapters import RealSeriesAdapter
from ns_dashboard.errors import ApiError
from ns_dashboard.fakes import FakeSeriesAdapter, seed_channel
from ns_dashboard.supervisor_link import SupervisorLink
from tests.test_dashboard_api import err, make_env

pytest.importorskip("fastapi")

STORY = {
    "slug": "the-lift", "channel": "after-dark", "status": "producing", "title": "The Lift", "next_step": "produce",
    "has_design": True, "chosen": "A", "drafts": [],
    "parts": [
        {"n": 1, "title": "Floor 13", "minutes": 15, "status": "published", "job_id": "j1", "words": 2300, "shorts": 3},
        {"n": 2, "title": "The Intercom", "minutes": 15, "status": "approved", "job_id": None, "words": 2250, "shorts": 3},
        {"n": 3, "title": "The Service Stair", "minutes": 15, "status": "written", "job_id": None, "words": 2200, "shorts": 3},
    ],
}


def test_series_list_comes_from_the_adapter(tmp_home: Path):
    client, state = make_env(tmp_home)
    assert client.get("/api/library/series").json() == []
    assert isinstance(state.adapters.series, FakeSeriesAdapter)
    state.adapters.series.stories = [STORY]
    rows = client.get("/api/library/series").json()
    assert [r["slug"] for r in rows] == ["the-lift"] and rows[0]["parts"][1]["status"] == "approved"


def test_produce_next_part_queues_a_series_part_job(tmp_home: Path):
    client, state = make_env(tmp_home)
    seed_channel(state.db, Paths.from_env(), "after-dark")
    assert isinstance(state.adapters.series, FakeSeriesAdapter)
    state.adapters.series.stories = [STORY]
    r = client.post("/api/library/series/the-lift/produce")
    assert r.status_code == 201
    queued = r.json()["queued"]
    assert [q["part"] for q in queued] == [2]
    job = state.store.get(queued[0]["job_id"])
    assert job is not None and job.kind == "series_part" and job.status == "queued"
    assert state.store.next_queued() is not None
    # the worker runs it through the fake adapter and files a long video
    assert state.worker.run_next()
    done = state.store.get(queued[0]["job_id"])
    assert done is not None and done.status == "done" and done.result and len(done.result["output_ids"]) == 1
    again = client.post("/api/library/series/the-lift/produce")
    assert again.status_code == 409 and err(again)["code"] == "cannot_produce"
    assert client.post("/api/library/series/nope/produce").status_code == 404


def test_real_listing_reads_the_story_folders(tmp_home: Path):
    client, state = make_env(tmp_home)
    seed_channel(state.db, Paths.from_env(), "after-dark")
    assert studio_bridge.list_series() == []
    with studio_bridge.workshop_session(fake=studio_bridge.READ_ONLY) as ws:
        slug = ws.start("after-dark")
    rows = studio_bridge.list_series()
    assert [r["slug"] for r in rows] == [slug]
    assert rows[0]["channel"] == "after-dark" and rows[0]["parts"] == [] and rows[0]["next_step"]
    assert client.get("/api/library/series").json() == []  # the fake adapter, not the folders


def test_real_adapter_turns_workshop_errors_into_answers(tmp_home: Path):
    paths = Paths.from_env()
    adapter = RealSeriesAdapter(paths, SupervisorLink("http://127.0.0.1:9", timeout_s=0.2))
    with pytest.raises(ApiError) as missing:
        adapter.queue_next("nope")
    assert missing.value.status == 404
    _client, state = make_env(tmp_home)
    seed_channel(state.db, paths, "after-dark")
    with studio_bridge.workshop_session(fake=studio_bridge.READ_ONLY) as ws:
        slug = ws.start("after-dark")
    with pytest.raises(ApiError) as nothing:
        adapter.queue_next(slug)
    assert nothing.value.status == 409 and nothing.value.code == "cannot_produce"
