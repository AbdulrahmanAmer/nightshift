# ruff: noqa: E501
"""Scheduled dashboard jobs (CR-C3-UI-5): `schedule_at` is stored, honoured by the worker and listed."""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_dashboard.errors import ApiError
from ns_dashboard.events import EventHub
from ns_dashboard.fakes import seed_channel
from ns_dashboard.jobs import JobStore, JobWorker
from ns_dashboard.state import open_db
from ns_dashboard.svc_jobs import parse_schedule_at

pytest.importorskip("fastapi")
from tests.test_dashboard_api import make_env  # noqa: E402

URL = "https://example.com/talk.mp4"


class Clock:
    """A settable local clock (aware datetimes)."""

    def __init__(self) -> None:
        self.now = datetime.now().astimezone().replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


def build(tmp_home: Path) -> tuple[JobStore, JobWorker, Clock]:
    paths = Paths.from_env()
    clock = Clock()
    store = JobStore(open_db(paths), clock=clock)
    worker = JobWorker(store, {"verify": lambda ctl: {"ok": True}}, EventHub(), NsLogger("dashboard", paths.logs))
    return store, worker, clock


def at(clock: Clock, **delta: float) -> dict[str, Any]:
    return {"schedule_at": (clock.now + timedelta(**delta)).isoformat(timespec="seconds")}


# ------------------------------------------------------------------ store and worker


def test_a_scheduled_job_waits_until_its_time(tmp_home: Path):
    store, worker, clock = build(tmp_home)
    job = store.create("verify", None, at(clock, hours=1))
    assert worker.run_next() is False and store.counts() == {"queued": 1, "running": 0}
    assert [r.job_id for r in store.scheduled()] == [job.job_id]
    clock.advance(minutes=59)
    assert worker.run_next() is False
    clock.advance(minutes=1)  # exactly at the time: due
    assert worker.run_next() is True
    done = store.get(job.job_id)
    assert done is not None and done.status == "done" and store.scheduled() == []
    assert done.to_dict()["schedule_at"] == job.schedule_at


def test_a_later_job_is_not_blocked_by_an_earlier_scheduled_one(tmp_home: Path):
    store, worker, clock = build(tmp_home)
    later = store.create("verify", None, at(clock, hours=2))
    now_job = store.create("verify", None, {})
    assert worker.run_next() is True
    assert store.get(now_job.job_id).status == "done"  # type: ignore[union-attr]
    assert store.get(later.job_id).status == "queued"  # type: ignore[union-attr]
    assert worker.run_next() is False


def test_due_jobs_keep_insertion_order(tmp_home: Path):
    store, worker, clock = build(tmp_home)
    a = store.create("verify", None, at(clock, minutes=30))
    b = store.create("verify", None, {})
    c = store.create("verify", None, at(clock, minutes=10))
    assert worker.run_next() and store.get(b.job_id).status == "done"  # type: ignore[union-attr]
    clock.advance(hours=1)
    assert worker.run_next() and store.get(a.job_id).status == "done"  # type: ignore[union-attr]
    assert store.get(c.job_id).status == "queued"  # type: ignore[union-attr]
    assert worker.run_next() and store.get(c.job_id).status == "done"  # type: ignore[union-attr]


def test_scheduled_listing_is_soonest_first_and_idle_ignores_it(tmp_home: Path):
    store, worker, clock = build(tmp_home)
    far = store.create("verify", None, at(clock, hours=5))
    near = store.create("verify", None, at(clock, hours=1))
    assert [r.job_id for r in store.scheduled()] == [near.job_id, far.job_id]
    assert worker.wait_idle(0.1) is True  # nothing can run yet, so the worker is idle


def test_a_scheduled_job_can_be_cancelled_before_it_starts(tmp_home: Path):
    store, worker, clock = build(tmp_home)
    job = store.create("verify", None, at(clock, hours=1))
    assert worker.cancel(job.job_id) == "cancelled"
    assert store.scheduled() == [] and worker.run_next() is False


def test_unreadable_or_missing_time_means_run_now(tmp_home: Path):
    store, worker, _ = build(tmp_home)
    bad = store.create("verify", None, {"schedule_at": "not a time"})
    none = store.create("verify", None, {"schedule_at": None})
    assert bad.schedule_at == "not a time" and bad.not_before() is None
    assert none.schedule_at is None and none.to_dict()["schedule_at"] is None
    assert worker.run_next() and worker.run_next() and not worker.run_next()


# ------------------------------------------------------------------ parsing


def test_parse_schedule_at():
    now = datetime(2026, 11, 3, 1, 0).astimezone()
    assert parse_schedule_at(None, now) is None and parse_schedule_at("", now) is None
    naive = parse_schedule_at("2026-11-03T02:30", now)  # no zone: the PC's local time
    assert naive is not None and naive.replace(tzinfo=None) == datetime(2026, 11, 3, 2, 30) and naive.utcoffset() == now.utcoffset()
    zoned = parse_schedule_at("2026-11-03T02:30:00Z", now)
    assert zoned is not None and zoned.utcoffset() is not None and zoned.timestamp() == datetime.fromisoformat("2026-11-03T02:30:00+00:00").timestamp()
    assert parse_schedule_at("2026-11-03T00:59", now) is None  # already passed: now
    assert parse_schedule_at(now.isoformat(), now) is None
    for bad in ("tomorrow", "2026-13-40T00:00", 5, ["x"]):
        with pytest.raises(ApiError) as ei:
            parse_schedule_at(bad, now)
        assert ei.value.status == 422
    with pytest.raises(ApiError) as ei2:
        parse_schedule_at((now + timedelta(days=40)).isoformat(), now)
    assert ei2.value.status == 422


# ------------------------------------------------------------------ routes


def test_run_request_with_schedule_at_is_queued_with_its_time(tmp_home: Path):
    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths)
    clock = Clock()
    state.store.clock = clock
    when = (clock.now + timedelta(hours=3)).replace(tzinfo=None).isoformat(timespec="minutes")
    r = client.post("/api/jobs/run", json={"channel": "after-dark", "schedule_at": when})
    assert r.status_code == 201
    job = r.json()
    assert job["status"] == "queued" and job["message"].startswith("Scheduled for ")
    assert datetime.fromisoformat(job["schedule_at"]) == datetime.fromisoformat(when).astimezone()
    assert job["params"]["schedule_at"] == job["schedule_at"] and job["params"]["channel"] == "after-dark"
    # visible in the queue listing and the status object, and not started
    listed = client.get("/api/jobs", params={"status": "queued"}).json()
    assert [j["job_id"] for j in listed] == [job["job_id"]] and listed[0]["schedule_at"] == job["schedule_at"]
    assert state.worker.run_next() is False
    status = client.get("/api/status").json()
    assert status["queue"]["queued"] == 1 and status["queue"]["scheduled"] == 1
    # the time comes
    clock.advance(hours=3, minutes=1)
    assert client.get("/api/status").json()["queue"]["scheduled"] == 0
    assert state.worker.run_next() is True
    done = client.get(f"/api/jobs/{job['job_id']}").json()
    assert done["status"] == "done" and done["schedule_at"] == job["schedule_at"]


@pytest.mark.parametrize("body_extra", [{}, {"schedule_at": None}, {"schedule_at": ""}], ids=["absent", "null", "empty"])
def test_no_schedule_means_run_now(tmp_home: Path, body_extra: dict[str, Any]):
    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths)
    job = client.post("/api/jobs/run", json={"channel": "after-dark", **body_extra}).json()
    assert job["schedule_at"] is None and job["message"] == "Queued" and "schedule_at" not in job["params"]
    assert state.worker.run_next() is True


def test_a_time_in_the_past_starts_now(tmp_home: Path):
    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths)
    past = (datetime.now() - timedelta(hours=1)).isoformat(timespec="minutes")
    job = client.post("/api/jobs/run", json={"channel": "after-dark", "schedule_at": past}).json()
    assert job["schedule_at"] is None and state.worker.run_next() is True


@pytest.mark.parametrize("bad", ["tomorrow morning", 12, "2020-99-99"])
def test_bad_schedule_at_is_422(tmp_home: Path, bad: Any):
    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths)
    r = client.post("/api/jobs/run", json={"channel": "after-dark", "schedule_at": bad})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_options"
    assert state.store.counts()["queued"] == 0


def test_more_than_31_days_ahead_is_422(tmp_home: Path):
    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths)
    far = (datetime.now() + timedelta(days=40)).isoformat(timespec="minutes")
    r = client.post("/api/jobs/run", json={"channel": "after-dark", "schedule_at": far})
    assert r.status_code == 422 and "31 days" in r.json()["error"]["message"]


def test_clip_jobs_can_be_scheduled_too(tmp_home: Path):
    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths)
    when = (datetime.now() + timedelta(hours=2)).isoformat(timespec="minutes")
    body = {"channel": "after-dark", "url": URL, "rights_confirmed": True, "schedule_at": when}
    job = client.post("/api/jobs/clip", json=body).json()
    assert job["kind"] == "clip" and job["schedule_at"] is not None and state.worker.run_next() is False


def test_cancelling_a_scheduled_job_over_http(tmp_home: Path):
    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths)
    when = (datetime.now() + timedelta(hours=2)).isoformat(timespec="minutes")
    job = client.post("/api/jobs/run", json={"channel": "after-dark", "schedule_at": when}).json()
    assert client.post(f"/api/jobs/{job['job_id']}/cancel").json()["status"] == "cancelled"
    assert client.get("/api/status").json()["queue"]["scheduled"] == 0


def test_assistant_tool_takes_schedule_at_and_the_card_says_when(tmp_home: Path):
    from ns_dashboard.assistant.registry import render_confirm
    from ns_dashboard.assistant.tools import build_registry

    spec = build_registry().get("start_story_job").spec  # type: ignore[union-attr]
    assert "schedule_at" in spec.args_schema["properties"] and "schedule_at" not in spec.args_schema["required"]
    assert "start: now" in render_confirm(spec, {"channel": "x", "format": "shorts_only"})
    assert "start: 2026-11-03T02:00" in render_confirm(spec, {"channel": "x", "format": "shorts_only", "schedule_at": "2026-11-03T02:00"})


# ------------------------------------------------------------------ supervisor side


def put_job(paths: Paths, job_id: str, schedule_at: str | None, status: str = "queued") -> None:
    paths.ensure()
    db = open_db(paths)
    db.close()
    con = sqlite3.connect(paths.db_path)
    params = {"params": {"channel": "x", **({"schedule_at": schedule_at} if schedule_at else {})}, "result": None}
    con.execute(
        "INSERT INTO dashboard_jobs(job_id, kind, channel, status, progress, message, created_at, params_json)"
        " VALUES (?, 'run', 'x', ?, 0, 'Queued', '2026-11-02T00:00:00', ?)",
        (job_id, status, json.dumps(params)),
    )
    con.commit()
    con.close()


def test_supervisor_counts_overdue_scheduled_jobs(tmp_home: Path):
    from ns_supervisor.scheduled import overdue_scheduled_jobs

    paths = Paths.from_env()
    assert overdue_scheduled_jobs(paths) == 0  # no database yet
    now = datetime.now().astimezone()
    put_job(paths, "a", (now - timedelta(minutes=5)).isoformat(timespec="seconds"))
    put_job(paths, "b", (now + timedelta(hours=5)).isoformat(timespec="seconds"))
    put_job(paths, "c", None)
    put_job(paths, "d", (now - timedelta(hours=1)).isoformat(timespec="seconds"), status="done")
    put_job(paths, "e", "garbage schedule_at")
    assert overdue_scheduled_jobs(paths, now) == 1
    assert overdue_scheduled_jobs(paths, now + timedelta(hours=6)) == 2
