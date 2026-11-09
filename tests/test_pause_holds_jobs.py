"""P47: a pause holds the channel's queued and scheduled story jobs, refuses Retry, and the night stops too.

Before: pausing a channel stopped only NEW work. A story job already queued, or scheduled with Create > Pick a
time, still ran after the pause (the job worker never looked at the channel), the Retry button of a failed
story card and the Assistant's resume reached `create_resume`, which did not look either, and a pause during
the nightly run did not stop that night's remaining jobs.

After: the worker does not start a story job of a paused channel (the job stays `queued` and its message says
why), Retry is refused with the same `channel_paused` answer as a new run, resuming the channel releases the
held jobs, and the night looks at the channel again before each job. Every test ends at the effect: the
runner is called or it is not.
"""

from __future__ import annotations

import contextlib
import json
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ns_common.db import Db
from ns_common.paths import Paths
from ns_dashboard.fakes import seed_channel
from ns_dashboard.handlers import Dashboard
from ns_dashboard.jobs import JobControl
from ns_studio import daily, jobs, review
from ns_studio.channels import crud
from tests.helpers import make_channel

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tests.test_dashboard_api import make_env  # noqa: E402

CH = "after-dark"
OTHER = "tiny-mysteries"


class Clock:
    """A settable local clock (aware datetimes) for the job store."""

    def __init__(self) -> None:
        self.now = datetime.now().astimezone().replace(microsecond=0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


class Runner:
    """Stands in for the studio run: records which channel each started job was for."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, ctl: JobControl) -> dict[str, Any]:
        self.calls.append(str(ctl.params["channel"]))
        return {"output_ids": []}


def setup(tmp_home: Path) -> tuple[TestClient, Any, Runner, Clock]:
    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths, CH)
    seed_channel(state.db, state.paths, OTHER)
    runner = Runner()
    state.worker.handlers["run"] = runner
    clock = Clock()
    state.store.clock = clock
    return client, state, runner, clock


def pause(client: TestClient, slug: str = CH) -> None:
    assert client.post(f"/api/channels/{slug}/pause", json={"note": "warning"}).status_code == 200


def resume(client: TestClient, slug: str = CH) -> None:
    assert client.post(f"/api/channels/{slug}/resume").status_code == 200


def job_of(client: TestClient, job_id: str) -> dict[str, Any]:
    return client.get(f"/api/jobs/{job_id}").json()


def queue_run(client: TestClient, slug: str = CH, **extra: Any) -> dict[str, Any]:
    r = client.post("/api/jobs/run", json={"channel": slug, **extra})
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ queued jobs


def test_a_queued_story_job_of_a_paused_channel_does_not_start_and_says_why(tmp_home: Path):
    client, state, runner, _ = setup(tmp_home)
    job = queue_run(client)
    pause(client)
    assert state.worker.run_next() is False
    assert runner.calls == []
    now = job_of(client, job["job_id"])
    assert now["status"] == "queued" and now["started_at"] is None
    assert now["message"].startswith("Paused: After Dark is paused since ")
    assert now["message"].endswith("; resume it to release this job")
    assert state.store.counts() == {"queued": 1, "running": 0}


def test_resuming_the_channel_releases_the_held_job_and_it_runs(tmp_home: Path):
    client, state, runner, _ = setup(tmp_home)
    job = queue_run(client)
    pause(client)
    assert state.worker.run_next() is False
    resume(client)
    assert job_of(client, job["job_id"])["message"] == "Queued"  # the reason is gone at once
    assert state.worker.run_next() is True
    assert runner.calls == [CH]
    assert job_of(client, job["job_id"])["status"] == "done"


def test_the_pause_shows_on_a_job_at_once_even_while_the_worker_is_busy(tmp_home: Path):
    """No `run_next` between the pause and the read: the Queue must not wait for the worker's turn."""
    client, state, _, _ = setup(tmp_home)
    job = queue_run(client)
    assert job_of(client, job["job_id"])["message"] == "Queued"
    pause(client)
    assert job_of(client, job["job_id"])["message"].startswith("Paused: After Dark is paused since ")
    resume(client)
    assert job_of(client, job["job_id"])["message"] == "Queued"


def test_a_pause_made_by_the_studio_command_holds_the_job_too(tmp_home: Path):
    """`studio pause` runs in another process and writes the profile; the worker reads it, not a message."""
    client, state, runner, _ = setup(tmp_home)
    job = queue_run(client)
    db = Db(state.paths.db_path)
    review.pause(state.paths, db, CH, "Community Guidelines warning")
    assert state.worker.run_next() is False and runner.calls == []
    assert job_of(client, job["job_id"])["message"].startswith("Paused: After Dark is paused since ")
    review.resume(state.paths, db, CH)
    assert state.worker.run_next() is True and runner.calls == [CH]


def test_a_held_job_does_not_block_the_other_channels(tmp_home: Path):
    client, state, runner, _ = setup(tmp_home)
    held = queue_run(client)
    other = queue_run(client, OTHER)
    pause(client)
    assert state.worker.run_next() is True
    assert runner.calls == [OTHER]
    assert job_of(client, other["job_id"])["status"] == "done"
    assert job_of(client, held["job_id"])["status"] == "queued"
    assert state.worker.run_next() is False
    assert state.worker.wait_idle(0.1) is True  # a held job does not keep the engine "busy"


def test_a_held_job_can_still_be_cancelled(tmp_home: Path):
    client, state, runner, _ = setup(tmp_home)
    job = queue_run(client)
    pause(client)
    state.worker.run_next()
    assert client.post(f"/api/jobs/{job['job_id']}/cancel").json()["status"] == "cancelled"
    resume(client)
    assert state.worker.run_next() is False and runner.calls == []


def test_the_clipper_is_not_held_by_a_pause(tmp_home: Path):
    """The Clipper request is accepted for a paused channel (documented in OPERATOR.md), so it is not held."""
    client, state, _, _ = setup(tmp_home)
    got: list[str] = []
    state.worker.handlers["clip"] = lambda ctl: got.append(str(ctl.params["channel"])) or {"output_ids": []}
    body = {"channel": CH, "url": "https://example.com/talk.mp4", "rights_confirmed": True}
    job = client.post("/api/jobs/clip", json=body).json()
    pause(client)
    assert state.worker.run_next() is True and got == [CH]
    assert job_of(client, job["job_id"])["status"] == "done"


# ------------------------------------------------------------------ scheduled jobs


def test_a_scheduled_story_job_of_a_paused_channel_does_not_start_when_its_time_comes(tmp_home: Path):
    client, state, runner, clock = setup(tmp_home)
    when = (clock.now + timedelta(hours=2)).replace(tzinfo=None).isoformat(timespec="minutes")
    job = queue_run(client, schedule_at=when)
    assert job["message"].startswith("Scheduled for ")
    pause(client)
    assert job_of(client, job["job_id"])["message"].startswith("Paused: After Dark is paused since ")
    clock.advance(hours=3)  # the time has come and gone
    assert state.worker.run_next() is False and runner.calls == []
    assert job_of(client, job["job_id"])["status"] == "queued"
    resume(client)
    assert state.worker.run_next() is True and runner.calls == [CH]
    assert job_of(client, job["job_id"])["status"] == "done"


def test_resuming_before_the_time_gives_the_scheduled_text_back_and_it_still_waits(tmp_home: Path):
    client, state, runner, clock = setup(tmp_home)
    when = (clock.now + timedelta(hours=5)).replace(tzinfo=None).isoformat(timespec="minutes")
    job = queue_run(client, schedule_at=when)
    scheduled_text = job["message"]
    pause(client)
    resume(client)
    assert job_of(client, job["job_id"])["message"] == scheduled_text
    assert state.worker.run_next() is False and runner.calls == []
    clock.advance(hours=6)
    assert state.worker.run_next() is True and runner.calls == [CH]


# ------------------------------------------------------------------ old queue files


def test_an_old_queue_row_without_any_new_field_loads_and_is_held_then_released(tmp_home: Path):
    """A row written by an earlier version: params stored flat (no `params`/`result` wrapper), no message."""
    client, state, runner, _ = setup(tmp_home)
    flat = json.dumps({"channel": CH, "format": "shorts_only", "count": 1})
    state.db.execute(
        "INSERT INTO dashboard_jobs(job_id, kind, channel, status, progress, message, created_at,"
        " params_json) VALUES ('run-old-1', 'run', ?, 'queued', 0, NULL, '2026-11-01T00:00:00+00:00', ?)",
        (CH, flat),
    )
    pause(client)
    assert state.worker.run_next() is False and runner.calls == []
    assert job_of(client, "run-old-1")["message"].startswith("Paused: ")
    resume(client)
    assert state.worker.run_next() is True and runner.calls == [CH]
    assert job_of(client, "run-old-1")["status"] == "done"


def test_a_job_with_no_channel_row_or_a_missing_profile_is_not_held(tmp_home: Path):
    client, state, runner, _ = setup(tmp_home)
    rec = state.store.create("run", "gone-channel", {"channel": "gone-channel", "format": "shorts_only"})
    assert state.worker.run_next() is True and runner.calls == ["gone-channel"]
    assert state.store.get(rec.job_id).status == "done"  # type: ignore[union-attr]


# ------------------------------------------------------------------ Retry and the Assistant's resume


def failed_run(state: Any, studio_id: str = "after-dark-20261104-aaaa") -> str:
    rec = state.store.create("run", CH, {"channel": CH, "format": "shorts_only", "count": 1})
    state.store.mark_running(rec.job_id)
    state.store.finish(
        rec.job_id,
        "failed",
        "voice failed",
        {"studio_job_ids": [studio_id], "error": {"code": "NS-E310", "hint": None, "stage": "voice"}},
    )
    folder = state.paths.job_dir(studio_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "job.json").write_text("{}", encoding="utf-8")
    return rec.job_id


def test_retry_of_a_failed_story_job_of_a_paused_channel_is_refused_like_a_new_run(tmp_home: Path):
    client, state, runner, _ = setup(tmp_home)
    failed = failed_run(state)
    pause(client)
    refused_run = client.post("/api/jobs/run", json={"channel": CH})
    assert refused_run.status_code == 409 and refused_run.json()["error"]["code"] == "channel_paused"
    r = client.post(f"/api/jobs/{failed}/resume")
    assert r.status_code == 409
    assert r.json()["error"] == refused_run.json()["error"]  # the same code and the same words
    assert r.json()["error"]["message"] == "'After Dark' is paused. Resume it first."
    # nothing was queued, so nothing can run
    assert state.store.counts() == {"queued": 0, "running": 0}
    assert state.worker.run_next() is False and runner.calls == []
    # the Assistant's gate refuses the same request before it shows a confirm card
    dash = Dashboard(state)
    with pytest.raises(Exception) as ei:
        dash.jobs.check_resume(failed)
    assert getattr(ei.value, "code", None) == "channel_paused"
    # after the channel is resumed the same Retry is accepted and the job runs
    resume(client)
    r2 = client.post(f"/api/jobs/{failed}/resume")
    assert r2.status_code == 201
    new_run = r2.json()
    assert state.worker.run_next() is True and runner.calls == [CH]
    assert job_of(client, new_run["job_id"])["status"] == "done"


def test_retry_of_a_job_whose_channel_profile_is_gone_keeps_its_old_answer(tmp_home: Path):
    """The pause check must not turn into a new error for a channel that was deleted: that is not a pause."""
    client, state, _, _ = setup(tmp_home)
    failed = failed_run(state)
    state.paths.channel_file(CH).unlink()
    r = client.post(f"/api/jobs/{failed}/resume")
    assert r.status_code == 201


# ------------------------------------------------------------------ the nightly run


NOW = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)


def night(tmp_home: Path, pause_after: int | None, slugs: tuple[str, ...] = (CH,)):
    """Run the real nightly planner, two Shorts owed per channel; pause the first after `pause_after` jobs."""
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    created = (NOW - timedelta(weeks=9)).isoformat(timespec="seconds")
    for slug in slugs:
        crud.add_channel(
            paths,
            db,
            make_channel(
                slug=slug,
                display_name=slug,
                niche=f"original fiction about {slug}",
                created_at=created,
                format="shorts_only",
                shorts_per_day_max=2,
            ),
        )
    env = jobs.open_env(light=True)
    env.services.footage_key = True
    ran: list[str] = []

    def run_fn(ctx: Any) -> list[str]:
        ran.append(ctx.channel.slug)
        if pause_after is not None and len(ran) == pause_after:
            review.pause(paths, db, slugs[0], "warning arrived during the night")
        return []

    report = daily.run_daily(
        env, now=NOW, rng=random.Random(1), run_fn=run_fn, awake=lambda: contextlib.nullcontext()
    )
    return report, ran, paths, db


def test_the_night_without_a_pause_makes_both_jobs(tmp_home: Path):
    """The control: the same night, nobody pauses, two jobs are made (so the next test means something)."""
    report, ran, _, _ = night(tmp_home, None)
    assert len(ran) == 2 and len(report["jobs"]) == 2 and not report.get("skipped")


def test_a_channel_paused_during_the_night_gets_no_further_job_that_night(tmp_home: Path):
    report, ran, paths, db = night(tmp_home, pause_after=1)
    assert len(ran) == 1, "the second job must not start after the pause"
    assert len(report["jobs"]) == 1 and report["failures"] == []
    held = report["skipped"]
    assert [(s["channel"], s["jobs"]) for s in held] == [(CH, 1)]
    assert "paused" in held[0]["reason"]
    assert crud.get_channel(paths, CH).status == "paused"


def test_only_the_paused_channel_stops_the_others_carry_on(tmp_home: Path):
    report, ran, _, _ = night(tmp_home, pause_after=1, slugs=(CH, "tiny-mysteries"))
    channels = [j["channel"] for j in report["jobs"]]
    assert channels.count(CH) == 1 and channels.count("tiny-mysteries") == 2
    assert [s["channel"] for s in report["skipped"]] == [CH]


# ------------------------------------------------------------------ break-it round (independent review)


class PausingStage:
    """A stage that pauses the channel while the first video of a run job is made (a warning arrives)."""

    name = "p47_pause_stage"
    tenant = None
    inputs = ()
    outputs = ("p47.json",)
    made = 0
    pause_at = 1

    def run(self, ctx: Any) -> None:
        type(self).made += 1
        if type(self).made == type(self).pause_at:
            paths = Paths.from_env()
            review.pause(paths, Db(paths.db_path), "demo", "warning arrived while the first video was made")
        ctx.write_json("p47.json", {"n": type(self).made})


@pytest.fixture
def pausing_stage(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_studio.orchestrator import FORMAT_STAGES
    from ns_studio.stages import register

    PausingStage.made = 0
    PausingStage.pause_at = 1
    register(PausingStage())
    monkeypatch.setitem(FORMAT_STAGES, "shorts_only", ["p47_pause_stage"])
    monkeypatch.setattr(jobs, "STAGE_MODULES", ())
    monkeypatch.setattr("ns_studio.policy.gate.available", lambda: False)


def run_three(seen: list[str]) -> list[str]:
    from ns_dashboard import studio_bridge
    from ns_studio.context import FAKEABLE

    env = jobs.open_env(FAKEABLE)
    jobs.ensure_demo_channel(env)
    return studio_bridge.run_story_jobs(
        "demo", "shorts_only", 3, lambda *_a: None, fake=tuple(FAKEABLE), seen=seen
    )


def test_a_run_job_with_a_count_makes_no_further_video_after_the_pause(pausing_stage):
    """A dashboard run job with count 3 is one worker job that makes 3 videos in a loop: a pause after the
    first one must stop the loop (the worker's hold only gates the start of a queued job)."""
    seen: list[str] = []
    run_three(seen)
    assert PausingStage.made == 1, "videos 2 and 3 must not start after the pause"
    assert len(seen) == 1
    assert crud.get_channel(Paths.from_env(), "demo").status == "paused"


def test_a_run_job_with_a_count_makes_all_videos_when_nobody_pauses(pausing_stage):
    """The control for the test above: with no pause the same run job makes all 3 videos."""
    PausingStage.pause_at = 99
    seen: list[str] = []
    run_three(seen)
    assert PausingStage.made == 3 and len(seen) == 3


