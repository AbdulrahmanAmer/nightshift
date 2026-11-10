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

from ns_common.config import load_channel, save_channel
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
    # main's disk-room check (D-33) loads the channel and answers not_found: that is the old answer now,
    # and it is not a pause
    assert r.status_code == 404 and r.json()["error"]["code"] != "channel_paused"
    assert state.store.counts() == {"queued": 0, "running": 0}


# ------------------------------------------------------------------ the nightly run


NOW = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)


def night(
    tmp_home: Path,
    pause_after: int | None,
    slugs: tuple[str, ...] = (CH,),
    on_run: Any = None,
):
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
        if on_run is not None:
            on_run(paths, db, ctx)
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
    assert not report.get("paused")


def test_a_channel_paused_during_the_night_gets_no_further_job_that_night(tmp_home: Path):
    report, ran, paths, db = night(tmp_home, pause_after=1)
    assert len(ran) == 1, "the second job must not start after the pause"
    assert len(report["jobs"]) == 1 and report["failures"] == []
    held = report["paused"]
    assert [(s["channel"], s["jobs"]) for s in held] == [(CH, 1)]
    assert "paused" in held[0]["reason"]
    assert not report.get("skipped"), "skipped keeps its meaning: an earlier job stopped"
    assert crud.get_channel(paths, CH).status == "paused"


def test_a_channel_paused_after_the_plan_but_before_its_first_job_makes_nothing_that_night(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """The pause lands between the planner and the first job: the first job is held too."""
    real_plan = daily.cadence.plan_daily

    def plan_then_pause(paths: Paths, db: Db, now: datetime, **kw: Any):
        due = real_plan(paths, db, now, **kw)
        review.pause(paths, db, CH, "warning arrived after the plan was made")
        return due

    monkeypatch.setattr(daily.cadence, "plan_daily", plan_then_pause)
    report, ran, _, _ = night(tmp_home, None)
    assert ran == [], "no job of the paused channel may start"
    assert report["jobs"] == [] and report["failures"] == []
    assert [(s["channel"], s["jobs"]) for s in report["paused"]] == [(CH, 2)]
    assert not report.get("skipped")


def test_only_the_paused_channel_stops_the_others_carry_on(tmp_home: Path):
    report, ran, _, _ = night(tmp_home, pause_after=1, slugs=(CH, "tiny-mysteries"))
    channels = [j["channel"] for j in report["jobs"]]
    assert channels.count(CH) == 1 and channels.count("tiny-mysteries") == 2
    assert [s["channel"] for s in report["paused"]] == [CH]


def test_a_paused_channel_is_not_reported_as_skipped_when_the_gpu_stayed_busy(tmp_home: Path):
    """The GPU stays busy (NS-E201) on the first channel's job; the second channel was paused meanwhile.

    The first channel is `skipped` (an earlier job stopped); the paused one is `paused`, never `skipped`."""
    from ns_common.errors import NsError

    def busy_and_pause(paths: Paths, db: Db, ctx: Any) -> None:
        review.pause(paths, db, "tiny-mysteries", "warning arrived during the night")
        raise NsError("NS-E201", "held by another program")

    report, ran, _, _ = night(tmp_home, None, slugs=(CH, "tiny-mysteries"), on_run=busy_and_pause)
    assert len(ran) == 1, "after the busy GPU nothing else starts"
    skipped = [s["channel"] for s in report.get("skipped", [])]
    held = [s["channel"] for s in report.get("paused", [])]
    assert "tiny-mysteries" in held and "tiny-mysteries" not in skipped
    assert skipped and ran[0] in skipped


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
    from ns_dashboard import studio_bridge

    seen: list[str] = []
    with pytest.raises(studio_bridge.ChannelPaused) as ei:  # the request keeps the 2 videos it still owes
        run_three(seen)
    assert ei.value.data["left"] == 2 and "after 1 of 3 videos" in str(ei.value)
    assert PausingStage.made == 1, "videos 2 and 3 must not start after the pause"
    assert len(seen) == 1
    assert crud.get_channel(Paths.from_env(), "demo").status == "paused"


def test_a_run_job_with_a_count_makes_all_videos_when_nobody_pauses(pausing_stage):
    """The control for the test above: with no pause the same run job makes all 3 videos."""
    PausingStage.pause_at = 99
    seen: list[str] = []
    run_three(seen)
    assert PausingStage.made == 3 and len(seen) == 3


def test_a_stale_hold_never_overwrites_the_message_of_a_job_that_started(tmp_home: Path):
    """The Channels pause (request thread) holds a snapshot of the job; the worker may start the job before
    the message is written. The running job keeps its own message and progress."""
    client, state, _, _ = setup(tmp_home)
    job = queue_run(client)
    stale = state.store.get(job["job_id"])
    assert stale is not None
    state.store.mark_running(job["job_id"])
    state.store.update_progress(job["job_id"], 0.4, "Writing the story")
    state.worker.hold = lambda rec: "Paused: After Dark is paused; resume it to release this job"
    assert state.worker._held(stale) is not None
    now = job_of(client, job["job_id"])
    assert now["status"] == "running"
    assert now["message"] == "Writing the story" and now["progress"] == pytest.approx(40.0)
    # the release path (a snapshot that still reads "Paused: ...") is guarded the same way
    stale.message = "Paused: After Dark is paused; resume it to release this job"
    state.worker.hold = lambda rec: None
    assert state.worker._held(stale) is None
    now = job_of(client, job["job_id"])
    assert now["message"] == "Writing the story" and now["progress"] == pytest.approx(40.0)


def test_a_stale_hold_never_overwrites_the_message_of_a_cancelled_job(tmp_home: Path):
    client, state, _, _ = setup(tmp_home)
    job = queue_run(client)
    stale = state.store.get(job["job_id"])
    assert stale is not None
    cancelled = client.post(f"/api/jobs/{job['job_id']}/cancel").json()
    state.worker.hold = lambda rec: "Paused: After Dark is paused; resume it to release this job"
    state.worker._held(stale)
    now = job_of(client, job["job_id"])
    assert now["status"] == "cancelled" and now["message"] == cancelled["message"]
    assert not now["message"].startswith("Paused")


def test_a_channel_known_to_be_paused_stays_held_when_the_pause_time_cannot_be_read(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """The hold must not fail open: if the incident query raises, the paused channel's job still waits,
    with a reason that has no time in it."""
    from ns_dashboard import channel_hold

    client, state, runner, _ = setup(tmp_home)
    job = queue_run(client)

    def boom(*_a: Any) -> str:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(channel_hold, "paused_since", boom)
    pause(client)
    assert state.worker.run_next() is False and runner.calls == []
    now = job_of(client, job["job_id"])
    assert now["status"] == "queued"
    assert now["message"] == "Paused: After Dark is paused; resume it to release this job"
    monkeypatch.undo()
    resume(client)
    assert state.worker.run_next() is True and runner.calls == [CH]


def test_the_pause_time_is_left_out_when_the_profile_was_paused_by_hand_after_a_resume(tmp_home: Path):
    """pause (incident), resume (incident), then the profile file is edited to `paused` by hand: there is no
    incident for this pause, and the old pause time would be untrue."""
    client, state, runner, _ = setup(tmp_home)
    job = queue_run(client)
    pause(client)
    assert " since " in job_of(client, job["job_id"])["message"]
    resume(client)
    ch = load_channel(state.paths, CH)
    ch.status = "paused"
    save_channel(state.paths, ch)
    assert state.worker.run_next() is False and runner.calls == []
    assert job_of(client, job["job_id"])["message"] == (
        "Paused: After Dark is paused; resume it to release this job"
    )
    # a real pause after that has its own incident and shows its own time again
    resume(client)
    pause(client)
    assert " since " in job_of(client, job["job_id"])["message"]


# ------------------------------------------------------------------ a job that waits at the GPU gate
#
# The worker marks a job `running` before the GPU gate, and the gate can wait for hours (the nightly holds the
# GPU). A pause that lands while the job waits there must still stop it: once the job holds the gate, the
# worker asks the hold again, puts the job back to `queued` and starts nothing.


def gate_that_pauses(client: TestClient, state: Any, *, pause_while_waiting: bool = True) -> list[str]:
    """Install the real gate with a busy GPU for two polls; the pause (if any) lands during the first wait.

    Returns the list of waits, so a test can see that the job really waited at the gate."""
    from ns_dashboard.jobs import make_gpu_gate

    waits: list[str] = []
    polls = {"n": 0}

    def status() -> dict[str, Any]:
        polls["n"] += 1
        return {"gpu_owner": "nightly" if polls["n"] <= 2 else None}

    def sleeper(_s: float) -> None:
        waits.append("waited")
        if pause_while_waiting and len(waits) == 1:
            pause(client)

    state.worker.gpu_gate = make_gpu_gate(status, sleeper=sleeper, poll_s=0.0)
    return waits


def test_a_run_job_with_a_count_that_waited_at_the_gate_is_held_when_the_pause_landed_there(tmp_home: Path):
    client, state, runner, _ = setup(tmp_home)
    job = queue_run(client, count=3)
    waits = gate_that_pauses(client, state)
    state.worker.run_next()
    assert len(waits) == 2, "the job did wait at the gate"
    assert runner.calls == [], "no video may start: the channel was paused while the job waited"
    now = job_of(client, job["job_id"])
    assert now["status"] == "queued" and now["started_at"] is None and now["finished_at"] is None
    assert now["message"].startswith("Paused: After Dark is paused since ")
    assert state.worker.run_next() is False and runner.calls == []  # held, not looping
    # resuming releases it, and it then runs
    resume(client)
    assert job_of(client, job["job_id"])["message"] == "Queued"
    state.worker.gpu_gate = None
    assert state.worker.run_next() is True and runner.calls == [CH]
    assert job_of(client, job["job_id"])["status"] == "done"


def test_a_job_that_waited_at_the_gate_still_runs_when_nobody_paused(tmp_home: Path):
    """The control for the test above: the same wait, no pause, the job runs."""
    client, state, runner, _ = setup(tmp_home)
    job = queue_run(client, count=3)
    waits = gate_that_pauses(client, state, pause_while_waiting=False)
    assert state.worker.run_next() is True
    assert len(waits) == 2 and runner.calls == [CH]
    assert job_of(client, job["job_id"])["status"] == "done"


def test_a_series_part_that_waited_at_the_gate_is_held_and_its_part_is_not_released(tmp_home: Path):
    client, state, _, _ = setup(tmp_home)
    made: list[str] = []
    freed: list[str] = []
    state.worker.handlers["series_part"] = lambda ctl: made.append(ctl.job_id) or {"output_ids": []}
    state.worker.on_end = {"series_part": lambda rec: freed.append(rec.job_id)}
    part = state.store.create("series_part", CH, {"channel": CH, "story_slug": "tale", "part_n": 1})
    waits = gate_that_pauses(client, state)
    state.worker.run_next()
    assert len(waits) == 2 and made == [], "no video for the part after the pause"
    now = job_of(client, part.job_id)
    assert now["status"] == "queued" and now["message"].startswith("Paused: After Dark is paused")
    assert freed == [], "the part stays with its job: it is waiting, not failed"
    resume(client)
    state.worker.gpu_gate = None
    assert state.worker.run_next() is True and made == [part.job_id]
    assert job_of(client, part.job_id)["status"] == "done"


def test_cancel_still_wins_over_the_hold_for_a_job_at_the_gate(tmp_home: Path):
    from ns_dashboard.jobs import make_gpu_gate

    client, state, runner, _ = setup(tmp_home)
    job = queue_run(client)

    def status() -> dict[str, Any]:
        pause(client)
        state.worker.cancel(job["job_id"])  # Cancel pressed on the running job while it waits
        return {"gpu_owner": None}

    state.worker.gpu_gate = make_gpu_gate(status, poll_s=0.0)
    state.worker.run_next()
    assert runner.calls == [] and job_of(client, job["job_id"])["status"] == "cancelled"


def test_a_clip_job_that_waited_at_the_gate_is_not_held(tmp_home: Path):
    client, state, _, _ = setup(tmp_home)
    got: list[str] = []
    state.worker.handlers["clip"] = lambda ctl: got.append(str(ctl.params["channel"])) or {"output_ids": []}
    body = {"channel": CH, "url": "https://example.com/talk.mp4", "rights_confirmed": True}
    job = client.post("/api/jobs/clip", json=body).json()
    gate_that_pauses(client, state)
    state.worker.run_next()
    assert got == [CH] and job_of(client, job["job_id"])["status"] == "done"


# ------------------------------------------------------------------ the studio child: before every video


def test_a_run_job_paused_before_its_first_video_makes_none_and_says_so(pausing_stage):
    """The pause comes in the seconds between the worker's last look and the child's first video: the first
    video is checked too (before, only the second and later videos were)."""
    from ns_dashboard import studio_bridge
    from ns_studio.context import FAKEABLE

    env = jobs.open_env(FAKEABLE)
    jobs.ensure_demo_channel(env)
    review.pause(env.paths, env.db, "demo", "warning arrived before the first video")
    seen: list[str] = []
    with pytest.raises(studio_bridge.ChannelPaused, match="paused"):
        studio_bridge.run_story_jobs(
            "demo", "shorts_only", 3, lambda *_a: None, fake=tuple(FAKEABLE), seen=seen
        )
    assert PausingStage.made == 0, "no stage of any video may run"
    assert seen == []


def test_a_retry_paused_before_it_restarts_makes_no_stage_run(pausing_stage):
    from ns_dashboard import studio_bridge

    PausingStage.pause_at = 99
    seen: list[str] = []
    run_three(seen)  # one run job, three videos, nobody pauses
    assert PausingStage.made == 3
    paths = Paths.from_env()
    review.pause(paths, Db(paths.db_path), "demo", "warning")
    with pytest.raises(studio_bridge.ChannelPaused):
        studio_bridge.run_story_jobs(
            "demo", "shorts_only", None, lambda *_a: None, resume_job_id=seen[0], fake=("llm",)
        )
    assert PausingStage.made == 3


def test_a_series_part_of_a_paused_channel_makes_no_video_and_no_job_folder(tmp_home: Path):
    from ns_dashboard import studio_bridge
    from ns_studio.context import FAKEABLE

    env = jobs.open_env(FAKEABLE)
    jobs.ensure_demo_channel(env)
    review.pause(env.paths, env.db, "demo", "warning")
    params = {"job_id": "series-p47-1", "channel": "demo", "story_slug": "tale", "part_n": 1}
    with pytest.raises(studio_bridge.ChannelPaused):
        studio_bridge.run_series_part(params, lambda *_a: None, fake=tuple(FAKEABLE))
    assert not env.paths.job_dir("series-p47-1").exists()
    assert env.db.query("select 1 from combos where job_id = ?", ("series-p47-1",)) == []


# ------------------------------------------------------------------ the night's resume loop


def test_the_nights_resume_loop_starts_no_further_job_of_a_channel_paused_mid_night(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio import ownership
    from tests import d31_driver as driver
    from tests.test_interrupted_nightly import _dead_daily_job, _setup

    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    first = _dead_daily_job(paths, db)
    second = _dead_daily_job(paths, db)
    attempts: list[str] = []

    def run_fn(ctx: Any) -> list[str]:
        attempts.append(ctx.job_id)
        if len(attempts) == 1:
            review.pause(paths, db, "after-dark", "warning arrived during the night")
        return []

    report = daily.run_daily(jobs.open_env(light=True), rng=random.Random(5), run_fn=run_fn)
    assert len(attempts) == 1, "the second half-made job must not be continued after the pause"
    (stays,) = {first, second} - set(attempts)
    record = next(r for r in ownership.list_records(paths) if r["id"] == stays)
    assert record["state"] == "interrupted", "it stays interrupted: nothing was changed to pass"
    (held,) = report["paused"]
    assert held["channel"] == "after-dark" and held["jobs"] == 1 and "paused" in held["reason"]
    assert not report.get("skipped"), "skipped keeps its meaning: an earlier job stopped"
    # the channel is resumed: the next night continues the job
    review.resume(paths, db, "after-dark")
    attempts.clear()
    again = daily.run_daily(jobs.open_env(light=True), rng=random.Random(5), run_fn=run_fn)
    assert attempts == [stays] and not again.get("paused")


def test_the_morning_note_says_the_job_of_a_paused_channel_waits():
    from ns_studio import ownership

    job = ownership.Interrupted(
        job_id="j1",
        channel="after-dark",
        fmt="master_first",
        stage="d31_b",
        origin="daily",
        run_id=None,
        owner="x",
        resumable=True,
    )
    note = ownership.recovery_message(ownership.Recovery(jobs=[job]), outcomes={"j1": ("paused", None)})
    assert note is not None
    assert "channel was paused during the night" in note and "resume the channel" in note
    assert "It continues at the next nightly run" not in note


def test_the_morning_note_for_a_held_half_made_job_names_the_age_limit_and_the_way_back():
    """The night continues a held job only under RESUME_WITHIN_DAYS old: the note says so (P47 review)."""
    from ns_studio import ownership

    def note_for(has_card: bool) -> str:
        job = ownership.Interrupted(
            job_id="j1",
            channel="after-dark",
            fmt="master_first",
            stage="d31_b",
            origin="daily",
            run_id=None,
            owner="x",
            resumable=True,
            card_id="card1" if has_card else None,
        )
        out = ownership.recovery_message(ownership.Recovery(jobs=[job]), outcomes={"j1": ("paused", None)})
        assert out is not None
        return out

    days = ownership.RESUME_WITHIN_DAYS
    with_card = note_for(True)
    assert f"no more than {days} days before that run" in with_card
    assert "press Retry in the Queue" in with_card
    assert "run studio run --resume j1" in note_for(False)
