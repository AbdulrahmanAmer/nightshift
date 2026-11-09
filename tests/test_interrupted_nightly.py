"""D-31: a nightly that is killed (power cut, crash) is closed, offered, resumed and reported.

Before: the run row stayed open for good, the half-made job was in no list and was never resumed, and for
12 hours `studio run --daily` counted the dead job's combo row as "still running" and said the channel
already had its week's video. After: "in progress" is decided by a live owner (an OS lock the kernel drops
with the process), so the channel is due again at once, the job is closed as interrupted (NS-E328, with the
stage), offered in the Queue with Retry, resumed by the next nightly, and the owner is told (morning note,
Queue, `/api/status`).

The kill tests run a real, tiny fake-services process (`tests/d31_driver.py`: the real planner, run row and
stage runner with three stub stages) and kill it with `Popen.kill()`, which gives it no chance to clean up.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ns_common.db import Db
from ns_common.paths import Paths
from ns_dashboard.fakes import seed_channel
from ns_dashboard.handlers import Dashboard, build_state
from ns_dashboard.supervisor_link import SupervisorLink
from ns_studio import daily, jobs
from ns_studio.channels import cadence, crud
from ns_studio.stages import StageState
from tests import d31_driver as driver
from tests.helpers import make_channel

REPO = Path(__file__).resolve().parents[1]


# ------------------------------------------------------------------ helpers
def _setup(tmp_home: Path, **kw: Any) -> tuple[Paths, Db]:
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    created = (datetime.now(UTC) - timedelta(weeks=11)).isoformat(timespec="seconds")
    crud.add_channel(paths, db, make_channel(**{"format": "master_first", "created_at": created, **kw}))
    return paths, db


def _kill_a_nightly_at(stage: str, tmp_home: Path, tmp_path: Path) -> None:
    """Start the fake nightly in its own process, wait until it is inside `stage`, and kill it."""
    ready = tmp_path / f"ready-{stage}-{time.monotonic_ns()}"
    env = {**os.environ, "NIGHTSHIFT_HOME": str(tmp_home), "D31_HANG_AT": stage, "D31_READY": str(ready)}
    log = tmp_path / f"driver-{stage}-{time.monotonic_ns()}.log"
    with log.open("wb") as out:
        proc = subprocess.Popen(
            [sys.executable, "-m", "tests.d31_driver"],
            cwd=REPO,
            env=env,
            stdout=out,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + 120
            while not ready.exists():
                assert proc.poll() is None, (
                    f"the fake nightly ended before {stage}:\n{log.read_text('utf-8', 'replace')}"
                )
                assert time.monotonic() < deadline, f"the fake nightly never reached {stage}"
                time.sleep(0.1)
            proc.kill()  # no cleanup runs: this is a power cut
            proc.wait(timeout=30)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)


def _long_jobs(db: Db) -> list[str]:
    return [r["job_id"] for r in db.query("select distinct job_id from combos where item='long'")]


def _queue_rows(db: Db) -> list[dict[str, Any]]:
    out = []
    for r in db.query("select * from dashboard_jobs where kind='run' order by created_at, rowid"):
        payload = json.loads(r["params_json"])
        out.append({**dict(r), "params": payload["params"], "result": payload["result"] or {}})
    return out


def _notes(home: Path) -> list[dict[str, str]]:
    f = home / "run" / "morning_note.json"
    return json.loads(f.read_text(encoding="utf-8"))["notes"] if f.exists() else []


def _first_stage_after(fmt: str, *finished: str) -> str:
    """The stage a job of `fmt` stopped at when only `finished` had been completed (the plan as it is now)."""
    from ns_studio import orchestrator

    return next(n for n in orchestrator.FORMAT_STAGES[fmt] if n not in finished)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _runs(db: Db) -> list[dict[str, Any]]:
    return [
        {**dict(r), "summary": json.loads(r["summary_json"] or "{}")}
        for r in db.query("select * from runs order by started_at, rowid")
    ]


# ------------------------------------------------------------------ the kill, end to end
def test_a_killed_nightly_is_due_again_at_once_then_resumed_and_reported(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, db = _setup(tmp_home)
    _kill_a_nightly_at("d31_b", tmp_home, tmp_path)
    (killed,) = _long_jobs(db)
    assert driver.calls(tmp_home) == ["d31_a:start", "d31_a:done", "d31_b:start"]

    # (2) the planner counts finished videos and live work only: the dead job is neither
    due = cadence.plan_daily(paths, db, datetime.now(UTC))
    assert [d.channel.slug for d in due] == ["after-dark"], "the channel must be due again at once"

    # start again: the night closes the dead run, offers the job and RESUMES it (3), then tells the owner (4)
    driver.install(monkeypatch)
    env = jobs.open_env(light=True)
    told: list[str] = []
    report = daily.run_daily(
        env, rng=random.Random(2), notifier=lambda title, message: told.append(message) or True
    )

    assert [j["job_id"] for j in report["jobs"]] == [killed] and report["jobs"][0]["resumed"] is True
    assert report["failures"] == []
    assert _long_jobs(db) == [killed], "no second job and no second combos row"
    assert driver.calls(tmp_home) == [
        "d31_a:start",
        "d31_a:done",
        "d31_b:start",  # the killed night
        "d31_b:start",
        "d31_b:done",
        "d31_c:start",
        "d31_c:done",  # the resume: stage a is not run again
    ]
    (out,) = db.query("select output_id, kind from outputs where job_id=?", (killed,))
    assert out["kind"] == "long"
    assert cadence.plan_daily(paths, db, datetime.now(UTC)) == [], "the finished video counts now"

    # the dead run row is closed as interrupted, with the job and the stage it reached
    dead, fresh = _runs(db)
    assert dead["finished_at"] and dead["summary"]["interrupted"] is True
    assert dead["summary"]["interrupted_jobs"] == [
        {
            "job_id": killed,
            "channel": "after-dark",
            "stage": "d31_b",
            "state": "open",  # as it was when the run was closed; it is served as resumed once it is finished
            "resumed": False,
            "has_card": True,
        }
    ]
    assert dead["summary"]["jobs"] == [], (
        "nothing was made by the dead run: the Queue counts videos from here"
    )
    assert fresh["finished_at"] and not fresh["summary"].get("interrupted")

    # the owner is told: a morning note, the Queue, and the toast at the end of the night
    (note,) = _notes(tmp_home)
    assert "interrupted" in note["message"] and "d31_b" in note["message"] and "after-dark" in note["message"]
    assert "continued it and it finished" in note["message"], "the note says what became of the job"
    assert "continuing it now" not in note["message"]
    (row,) = _queue_rows(db)
    assert row["status"] == "done" and row["channel"] == "after-dark"
    assert row["result"]["studio_job_ids"] == [killed]
    assert (
        row["result"]["interrupted"]["stage"] == "d31_b"
        and row["result"]["interrupted"]["resume"] == "resumed"
    )
    assert "d31_b" in row["message"] and "interrupted" in row["message"].lower()
    assert any("interrupted" in m and "resumed" in m for m in told), told


def test_a_second_kill_and_start_does_not_double_the_video_or_the_combos_rows(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, db = _setup(tmp_home)
    _kill_a_nightly_at("d31_b", tmp_home, tmp_path)
    (killed,) = _long_jobs(db)
    _kill_a_nightly_at("d31_c", tmp_home, tmp_path)  # the restart resumes the same job and is killed again
    assert _long_jobs(db) == [killed], "the restart resumed the job: no new job, no new combos row"
    (row,) = _queue_rows(db)  # the card of the first kill: one card, not one per start
    assert row["status"] == "failed" and row["result"]["error"]["code"] == "NS-E328"
    assert row["result"]["error"]["stage"] == "d31_b"

    # an engine start in between (the dashboard worker calls this): the card follows the job to where it is
    from ns_studio import ownership

    driver.install(monkeypatch)  # the plan this process names stages from (the driver's own is the stubs)
    ownership.recover(paths, db)
    (row,) = _queue_rows(db)
    assert row["status"] == "failed" and row["result"]["error"]["stage"] == "d31_c"
    # the restart that was killed while continuing the job told nobody (its note comes after the attempt);
    # the engine start tells it now, once, with the stage the job is at
    (note,) = _notes(tmp_home)
    assert "d31_c" in note["message"] and "interrupted" in note["message"]

    report = daily.run_daily(jobs.open_env(light=True), rng=random.Random(3))

    assert [j["job_id"] for j in report["jobs"]] == [killed]
    assert _long_jobs(db) == [killed]
    assert db.query("select count(*) n from combos where item='long'")[0]["n"] == 1
    assert [r["job_id"] for r in db.query("select job_id from outputs where kind='long'")] == [killed]
    done = [c for c in driver.calls(tmp_home) if c.endswith(":done")]
    assert done == ["d31_a:done", "d31_b:done", "d31_c:done"], done  # every stage finished exactly once
    first, second, third = _runs(db)
    assert first["summary"]["interrupted"] and second["summary"]["interrupted"]
    assert third["finished_at"] and not third["summary"].get("interrupted")
    (final,) = _queue_rows(db)  # one card for the job through all three nights, not one per kill
    assert final["job_id"] == row["job_id"] and final["status"] == "done"


# ------------------------------------------------------------------ the planner and the reason it gives
def test_nothing_due_names_the_job_that_is_still_being_made(tmp_home: Path) -> None:
    from ns_studio import ownership

    paths, db = _setup(tmp_home)
    env = jobs.open_env(light=True)
    ctx = jobs.make_job(env, crud.get_channel(paths, "after-dark"), "master_first", origin="daily")
    with ownership.hold_job(paths, ctx.job_id):
        why: dict[str, str] = {}
        assert cadence.plan_daily(paths, db, datetime.now(UTC), why=why) == []
        assert ctx.job_id in why["after-dark"] and "being made" in why["after-dark"]
        report = daily.run_daily(env, rng=random.Random(1), run_fn=lambda c: pytest.fail("nothing is due"))
        assert report["jobs"] == [] and report["channels"] == []
        message = daily.nothing_due_message(report)
        assert message.startswith("nothing due tonight") and ctx.job_id in message
        assert "full video or today's Shorts" not in message, "the old sentence was untrue here"
    # the job's owner is gone without a trace of an end (a kill): the channel is due, with the reason gone
    ownership.write_record(paths, ctx.job_id, state="running", pid=2**22)
    assert [d.channel.slug for d in cadence.plan_daily(paths, db, datetime.now(UTC))] == ["after-dark"]


def test_a_killed_short_does_not_use_up_the_days_shorts(tmp_home: Path) -> None:
    from ns_studio import ownership

    paths, db = _setup(tmp_home, format="shorts_only", shorts_per_day_max=3)
    ch = crud.get_channel(paths, "after-dark")
    env = jobs.open_env(light=True)
    now = datetime.now(UTC)
    plan = cadence.CadencePlan("after-dark", "steady", 1, 3)
    assert cadence.jobs_due_today(db, ch, plan, now, paths=paths) == 3
    dead = jobs.make_job(env, ch, "shorts_only", origin="daily")
    ownership.write_record(paths, dead.job_id, state="running", pid=2**22)  # killed: no lock held
    assert cadence.jobs_due_today(db, ch, plan, now, paths=paths) == 3, "a dead job made no Short"
    live = jobs.make_job(env, ch, "shorts_only", origin="daily")
    with ownership.hold_job(paths, live.job_id):
        assert cadence.jobs_due_today(db, ch, plan, now, paths=paths) == 2, "a live job will make one"
    assert cadence.jobs_due_today(db, ch, plan, now, paths=paths) == 2, "a job that finished counts"
    failed = jobs.make_job(env, ch, "shorts_only", origin="daily")
    with pytest.raises(RuntimeError), ownership.hold_job(paths, failed.job_id):
        raise RuntimeError("a clean failure")
    assert cadence.jobs_due_today(db, ch, plan, now, paths=paths) == 2, "a job that failed made no Short"


# ------------------------------------------------------------------ /api/status and the engine start
def _dashboard(tmp_home: Path) -> Dashboard:
    state = build_state(
        Paths.from_env(), fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    )
    seed_channel(state.db, state.paths, "after-dark")
    return Dashboard(state)


def test_status_never_shows_an_unfinished_run_as_the_last_run_without_saying_so(tmp_home: Path) -> None:
    from ns_studio import ownership

    dash = _dashboard(tmp_home)
    paths, db = dash.state.paths, dash.state.db
    started = datetime.now(UTC).isoformat(timespec="seconds")

    def last_run() -> dict[str, Any]:
        return dash.engine.status()["last_run"]

    db.execute("insert into runs(run_id, started_at, mode) values ('daily-live', ?, 'daily')", (started,))
    with ownership.hold_run(paths, "daily-live", origin="daily"):
        assert last_run()["state"] == "running"
    ownership.write_record(paths, "daily-live", state="running", pid=2**22)  # the process is gone
    gone = last_run()
    assert gone["state"] == "interrupted" and gone["finished_at"] is None
    assert "interrupted" in gone["note"].lower()

    old = (datetime.now(UTC) - timedelta(hours=20)).isoformat(
        timespec="seconds"
    )  # written by 0.1.14: no owner
    db.execute("insert into runs(run_id, started_at, mode) values ('daily-old', ?, 'daily')", (old,))
    db.execute("update runs set started_at=? where run_id='daily-live'", ("2000-01-01T00:00:00+00:00",))
    legacy = last_run()
    assert legacy["run_id"] == "daily-old" and legacy["state"] == "interrupted"
    assert "owner unknown" in legacy["note"]

    db.execute("update runs set finished_at=?, summary_json='{}' where run_id='daily-old'", (started,))
    assert last_run()["state"] == "finished"
    assert [r["run_id"] for r in dash.engine.runs(5)][0] == "daily-old"


def test_the_engine_start_closes_a_dead_run_and_offers_the_job_with_retry(tmp_home: Path) -> None:
    from ns_studio import ownership

    dash = _dashboard(tmp_home)
    state = dash.state
    paths = state.paths
    seed_channel(state.db, paths, "night-desk", format="master_first")
    env = jobs.open_env(light=True)
    ctx = jobs.make_job(env, crud.get_channel(paths, "night-desk"), "master_first", origin="daily")
    done = StageState()
    done.mark_done("ideas", "h")
    done.save(ctx.job_dir)
    ownership.write_record(paths, ctx.job_id, state="running", pid=2**22)  # killed: nobody holds the lock
    state.db.execute(
        "insert into runs(run_id, started_at, mode) values ('daily-x', ?, 'daily')",
        (datetime.now(UTC).isoformat(timespec="seconds"),),
    )
    ownership.write_record(
        paths,
        "daily-x",
        kind="run",
        state="running",
        pid=2**22,
        origin="daily",
        jobs=[{"job_id": ctx.job_id, "channel": "night-desk"}],
    )

    state.worker.start()
    state.worker.stop()

    (row,) = [
        r for r in state.store.list_jobs(kind="run") if (r.result or {}).get("studio_job_ids") == [ctx.job_id]
    ]
    assert row.status == "failed" and row.result and row.result["error"]["code"] == "NS-E328"
    stage = _first_stage_after("master_first", "ideas")
    assert row.result["error"]["stage"] == stage and row.failed_stage() == stage
    run = state.db.one("select finished_at, summary_json from runs where run_id='daily-x'")
    assert run is not None and run["finished_at"] and json.loads(run["summary_json"])["interrupted"] is True
    (note,) = _notes(tmp_home)
    assert stage in note["message"] and "Retry" in note["message"]
    state.worker.start()  # a second start finds nothing new: no second card, no second note
    state.worker.stop()
    assert len(_notes(tmp_home)) == 1
    assert (
        len([r for r in state.store.list_jobs(kind="run") if r.result and r.result.get("studio_job_ids")])
        == 1
    )
    dash.jobs.check_resume(row.job_id)  # Retry accepts it: same studio job, from where it stopped
    again = dash.jobs.create_resume(row.job_id)
    assert again["params"]["resume_job_id"] == ctx.job_id


# ------------------------------------------------------------------ rows written by 0.1.0 to 0.1.14
def test_a_database_written_before_this_change_loads_and_old_unfinished_rows_are_closed(
    tmp_home: Path,
) -> None:
    """No pid, no start time, no owner file: the studio.db of 0.1.14 has the same columns as before."""
    from ns_studio import ownership

    paths, db = _setup(tmp_home)
    assert [r["name"] for r in db.query("pragma table_info(runs)")] == [
        "run_id",
        "started_at",
        "finished_at",
        "mode",
        "summary_json",
    ], "no schema change: owner info lives beside the job, not in a new column"
    now = datetime.now(UTC)
    old = now - timedelta(hours=20)
    # the shape QA found: an unfinished run row and a half-made job (combos rows, stage state), nothing else
    db.execute(
        "insert into runs(run_id, started_at, mode) values ('daily-20261109-e3a4', ?, 'daily')",
        (old.isoformat(timespec="seconds"),),
    )
    env = jobs.open_env(light=True)
    ctx = jobs.make_job(env, crud.get_channel(paths, "after-dark"), "master_first", now=old)
    ownership.forget(paths, ctx.job_id)  # 0.1.14 wrote no owner file
    done = StageState()
    done.mark_done("ideas", "h")
    done.mark_done("long_plan", "h")
    done.save(ctx.job_dir)
    ch = crud.get_channel(paths, "after-dark")
    plan = cadence.CadencePlan("after-dark", "steady", 1, 1)

    # an old row with no owner, older than the in-progress bound, counts for nothing; a younger one still can
    assert cadence.jobs_due_today(db, ch, plan, now, paths=paths) == 1
    db.execute(
        "update combos set created_at=? where job_id=?",
        ((now - timedelta(hours=1)).isoformat(timespec="seconds"), ctx.job_id),
    )
    assert cadence.jobs_due_today(db, ch, plan, now, paths=paths) == 0
    db.execute(
        "update combos set created_at=? where job_id=?", (old.isoformat(timespec="seconds"), ctx.job_id)
    )

    out = ownership.recover(paths, db, now=now)
    assert [r["run_id"] for r in out.runs] == ["daily-20261109-e3a4"]
    row = db.one("select finished_at, summary_json from runs where run_id='daily-20261109-e3a4'")
    assert row is not None and row["finished_at"]
    summary = json.loads(row["summary_json"])
    assert summary["interrupted"] is True and summary["owner"] == "unknown"
    (card,) = _queue_rows(db)
    assert card["status"] == "failed" and card["result"]["studio_job_ids"] == [ctx.job_id]
    stage = _first_stage_after("master_first", "ideas", "long_plan")
    assert card["result"]["error"]["code"] == "NS-E328" and card["result"]["error"]["stage"] == stage
    (note,) = _notes(tmp_home)
    assert "owner unknown" in note["message"] and stage in note["message"]
    again = ownership.recover(paths, db, now=now)  # nothing new the second time
    assert not again.runs and not again.jobs and len(_notes(tmp_home)) == 1 and len(_queue_rows(db)) == 1


# ------------------------------------------------------------------ the owner lock itself
def test_hold_is_reentrant_records_how_it_ended_and_refuses_a_second_process(tmp_home: Path) -> None:
    from ns_common.singleton import FileLock
    from ns_studio import ownership
    from ns_studio.errors import StudioError

    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    with ownership.hold_job(paths, "j-1", origin="daily", channel="c", fmt="master_first"):
        assert ownership.liveness(paths, "j-1") == "live"
        with ownership.hold_job(paths, "j-1"):  # run_job_guarded inside the nightly's own hold
            pass
        assert ownership.liveness(paths, "j-1") == "live", "the inner block must not let go"
    assert ownership.liveness(paths, "j-1") == "ended"
    rec = ownership.read_record(paths, "j-1")
    assert (
        rec is not None and rec["state"] == "done" and rec["origin"] == "daily" and rec["pid"] == os.getpid()
    )

    with pytest.raises(RuntimeError), ownership.hold_job(paths, "j-2"):
        raise RuntimeError("a clean failure")
    rec = ownership.read_record(paths, "j-2")
    assert rec is not None and rec["state"] == "failed", "a job that failed cleanly is not an interrupted one"
    assert ownership.recover(paths, db).jobs == []

    other = FileLock(ownership.lock_path(paths, "j-3"))  # another process holds it
    other.acquire()
    try:
        with pytest.raises(StudioError) as ei, ownership.hold_job(paths, "j-3"):
            pass
        assert "already running" in str(ei.value)
    finally:
        other.release()


def test_a_second_start_while_one_is_closing_things_does_nothing_twice(tmp_home: Path) -> None:
    """The engine and the nightly can start together: only one of them closes a dead job, once."""
    from ns_common.singleton import FileLock
    from ns_studio import ownership

    paths, db = _setup(tmp_home)
    ctx = jobs.make_job(
        jobs.open_env(light=True), crud.get_channel(paths, "after-dark"), "master_first", origin="daily"
    )
    ownership.write_record(paths, ctx.job_id, state="running", pid=2**22)  # killed
    guard = FileLock(ownership.owners_dir(paths) / "recover.lock")
    guard.acquire()  # the other process is in the middle of it
    try:
        assert not ownership.recover(paths, db).any
    finally:
        guard.release()
    assert [j.job_id for j in ownership.recover(paths, db).jobs] == [ctx.job_id]
    assert len(_queue_rows(db)) == 1


def test_studio_status_lists_the_job_that_was_interrupted(tmp_home: Path) -> None:
    from typer.testing import CliRunner

    from ns_studio import ownership
    from ns_studio.cli import app

    paths, db = _setup(tmp_home)
    ctx = jobs.make_job(
        jobs.open_env(light=True), crud.get_channel(paths, "after-dark"), "master_first", origin="daily"
    )
    ownership.write_record(paths, ctx.job_id, state="running", pid=2**22)  # killed
    result = CliRunner().invoke(app, ["status", "--channel", "after-dark"])
    assert result.exit_code == 0, result.output
    assert f"interrupted: job {ctx.job_id}" in result.output and "Retry" in result.output


# ------------------------------------------------------------------ the break-it round
def _add_long_output(db: Db, job_id: str) -> None:
    db.execute(
        "insert into outputs(output_id, job_id, channel, kind, title, path, created_at)"
        " values (?,?,?,?,?,?,?)",
        (f"{job_id}/long", job_id, "after-dark", "long", "t", "long.mp4", _now_iso()),
    )


def _dead_daily_job(paths: Paths, db: Db, stage_done: tuple[str, ...] = ("d31_a",), **kw: Any) -> str:
    """A job of the nightly whose process is gone: announced, partly staged, owner record says running."""
    from ns_studio import ownership

    ch = crud.get_channel(paths, "after-dark")
    ctx = jobs.make_job(jobs.open_env(light=True), ch, "master_first", origin="daily", **kw)
    state = StageState()
    for name in stage_done:
        state.mark_done(name, "h")
    state.save(ctx.job_dir)
    ownership.write_record(paths, ctx.job_id, state="running", pid=2**22)
    return ctx.job_id


def test_the_last_night_data_never_counts_an_interrupted_job_as_made(tmp_home: Path) -> None:
    """The Queue's 'Last night' card read summary.jobs as 'videos made' and drew a green 'Finished'."""
    from ns_studio import ownership

    dash = _dashboard(tmp_home)
    state, paths = dash.state, dash.state.paths
    seed_channel(state.db, paths, "night-desk", format="master_first")
    ctx = jobs.make_job(
        jobs.open_env(light=True), crud.get_channel(paths, "night-desk"), "master_first", origin="daily"
    )
    ownership.write_record(paths, ctx.job_id, state="running", pid=2**22)
    state.db.execute(
        "insert into runs(run_id, started_at, mode) values ('daily-x', ?, 'daily')", (_now_iso(),)
    )
    ownership.write_record(
        paths,
        "daily-x",
        kind="run",
        state="running",
        pid=2**22,
        origin="daily",
        jobs=[{"job_id": ctx.job_id, "channel": "night-desk"}],
    )
    state.worker.start()  # the engine start closes the dead run before the next nightly
    state.worker.stop()

    (last,) = dash.engine.runs(1)
    assert last["state"] == "interrupted" and last["interrupted"] is True and last["note"]
    assert last["summary"]["jobs"] == [], "the interrupted job is not a video that was made"
    (unfinished,) = last["summary"]["interrupted_jobs"]
    assert (
        unfinished["job_id"] == ctx.job_id and unfinished["channel"] == "night-desk" and unfinished["stage"]
    )
    # the page that draws it must read the state and the unfinished list, not only summary.jobs
    queue = (REPO / "ns_dashboard" / "ui" / "src" / "pages" / "Queue.tsx").read_text(encoding="utf-8")
    assert 'r.state === "interrupted"' in queue and "interrupted_jobs" in queue and "r.note" in queue


def test_a_database_error_while_offering_the_job_does_not_lose_it_or_stop_the_others(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ns_studio import ownership

    paths, db = _setup(tmp_home)
    first = _dead_daily_job(paths, db)
    second = _dead_daily_job(paths, db)
    db.execute("insert into runs(run_id, started_at, mode) values ('daily-x', ?, 'daily')", (_now_iso(),))
    ownership.write_record(
        paths,
        "daily-x",
        kind="run",
        state="running",
        pid=2**22,
        origin="daily",
        jobs=[{"job_id": first, "channel": "after-dark"}, {"job_id": second, "channel": "after-dark"}],
    )
    real = ownership.offer_job
    refuse = {first}

    def flaky(db_: Any, job_id: str, *a: Any, **kw: Any) -> str | None:
        if job_id in refuse:
            raise sqlite3.OperationalError("database is locked")
        return real(db_, job_id, *a, **kw)

    monkeypatch.setattr(ownership, "offer_job", flaky)
    out = ownership.recover(paths, db)  # must not raise
    assert [j.job_id for j in out.jobs] == [second], "one bad job never stops the others"
    rec = ownership.read_record(paths, first)
    assert rec is not None and rec["state"] == "running", "the refused job is still open for the next call"
    row = db.one("select finished_at from runs where run_id='daily-x'")
    assert row is not None and row["finished_at"] is None, "the run that listed it stays open too"
    assert [r["result"]["studio_job_ids"] for r in _queue_rows(db)] == [[second]]

    refuse.clear()  # the database is back
    again = ownership.recover(paths, db)
    assert [j.job_id for j in again.jobs] == [first]
    assert sorted(r["result"]["studio_job_ids"][0] for r in _queue_rows(db)) == sorted([first, second])
    row = db.one("select finished_at, summary_json from runs where run_id='daily-x'")
    assert row is not None and row["finished_at"]
    assert {j["job_id"] for j in json.loads(row["summary_json"])["interrupted_jobs"]} == {first, second}
    assert ownership.recover(paths, db).any is False


def test_a_long_video_without_its_shorts_is_an_interrupted_job_and_is_continued(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The outbox records the long video first and the Shorts after: a kill between is not a finished job."""
    from ns_studio import ownership

    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    job = _dead_daily_job(paths, db, stage_done=("d31_a", "d31_b"))
    _add_long_output(db, job)
    assert ownership.job_finished(paths, db, job, "master_first") is False

    out = ownership.recover(paths, db, note=False)
    (item,) = out.jobs
    assert item.job_id == job and item.stage == "d31_c" and item.resumable
    (card,) = _queue_rows(db)
    assert card["status"] == "failed" and card["result"]["studio_job_ids"] == [job]

    report = daily.run_daily(jobs.open_env(light=True), rng=random.Random(5))
    assert [j["job_id"] for j in report["jobs"]] == [job] and report["jobs"][0]["resumed"] is True
    assert driver.calls(tmp_home)[-2:] == ["d31_c:start", "d31_c:done"], "the missing stage ran"
    assert ownership.job_finished(paths, db, job, "master_first") is True
    (final,) = _queue_rows(db)
    assert final["status"] == "done"


def test_a_job_with_no_stage_state_is_finished_when_it_has_an_output(tmp_home: Path) -> None:
    from ns_studio import ownership

    paths, db = _setup(tmp_home)
    ctx = jobs.make_job(
        jobs.open_env(light=True), crud.get_channel(paths, "after-dark"), "master_first", origin="daily"
    )
    assert ownership.job_finished(paths, db, ctx.job_id, "master_first") is False
    _add_long_output(db, ctx.job_id)
    assert ownership.job_finished(paths, db, ctx.job_id, "master_first") is True


def test_a_continued_job_that_fails_again_does_not_start_a_second_job_that_night(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    dead = _dead_daily_job(paths, db)
    attempts: list[str] = []

    def always_fails(ctx: Any) -> list[str]:
        attempts.append(ctx.job_id)
        raise RuntimeError("a late stage breaks")

    report = daily.run_daily(jobs.open_env(light=True), rng=random.Random(1), run_fn=always_fails)

    assert attempts == [dead], "one attempt tonight: the continued job, not a new job from ideas"
    assert len(report["failures"]) == 1 and report["channels"] == []
    assert _long_jobs(db) == [dead], "no second combos row"
    assert "stopped again" in report["not_due"]["after-dark"]
    (card,) = _queue_rows(db)
    assert card["status"] == "failed" and card["result"]["interrupted"]["resume"] == "retry"


@pytest.mark.parametrize(
    ("raises", "words"),
    [
        (None, ("continued it and it finished",)),
        (RuntimeError("late stage"), ("stopped again", "NS-E", "Retry")),
        ("busy", ("GPU was busy", "next nightly run")),
    ],
)
def test_the_morning_note_says_what_became_of_the_job_not_what_was_planned(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, raises: Any, words: tuple[str, ...]
) -> None:
    from ns_common.errors import NsError

    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    _dead_daily_job(paths, db)

    def run_fn(ctx: Any) -> list[str]:
        if raises == "busy":
            raise NsError("NS-E201", "held by another program")
        if raises is not None:
            raise raises
        return ["d31_c"]

    daily.run_daily(jobs.open_env(light=True), rng=random.Random(1), run_fn=run_fn)
    (note,) = _notes(tmp_home)
    assert "interrupted" in note["message"]
    for w in words:
        assert w in note["message"], note["message"]
    assert "continuing it now" not in note["message"] and "is continuing" not in note["message"]


def test_a_job_with_no_queue_card_is_not_told_to_press_retry_in_the_queue(tmp_home: Path) -> None:
    """A job known only through `hold` (never announced) has no card; the note names the real way back."""
    from ns_studio import ownership

    paths, db = _setup(tmp_home)
    job = _dead_daily_job(paths, db)
    ownership.forget(paths, job)
    ownership.write_record(  # what hold() alone leaves behind: no announced_at
        paths,
        job,
        kind="job",
        state="running",
        pid=2**22,
        origin="manual",
        channel="after-dark",
        fmt="master_first",
    )
    out = ownership.recover(paths, db)
    (item,) = out.jobs
    assert item.has_card is False and _queue_rows(db) == []
    (note,) = _notes(tmp_home)
    assert f"studio run --resume {job}" in note["message"]
    assert "Retry in the Queue" not in note["message"]


def test_nothing_due_still_names_a_profile_that_was_skipped_because_it_cannot_be_read(tmp_home: Path) -> None:
    paths, _ = _setup(tmp_home, status="paused")  # no active channel at all
    paths.channel_file("broken").write_text("slug: Bad Slug\n", encoding="utf-8")
    report = daily.run_daily(jobs.open_env(light=True), rng=random.Random(1))
    assert report["channels"] == [] and report["not_due"] == {}
    assert [slug for slug, _ in report["unreadable_profiles"]] == ["broken"]
    message = daily.nothing_due_message(report)
    assert "broken" in message and "cannot be read" in message
    assert "no channel is active" not in message, "a skipped profile is not an inactive channel"
    assert daily.nothing_due_message({"not_due": {}}) == "nothing due tonight: no channel is active"


# ------------------------------------------------------------------ review round 1 (PR 168 at 5952129)
def _served(tmp_home: Path) -> Dashboard:
    """The dashboard of this home with no channel of its own (the nightly plans only the `_setup` one)."""
    return Dashboard(
        build_state(
            Paths.from_env(), fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
        )
    )


def _lines(last: dict[str, Any]) -> list[tuple[str, str, bool]]:
    return [(i["job_id"], i["state"], i["resumed"]) for i in last["summary"]["interrupted_jobs"]]


@pytest.mark.parametrize(
    ("case", "state", "made"),
    [
        ("open", "open", False),  # the job cannot be continued by the nightly (its channel is paused)
        ("resumed", "resumed", True),  # continued and finished
        ("failed_again", "failed_again", False),  # continued and stopped again
        ("waiting", "waiting", False),  # the GPU was busy: it waits for the next night
    ],
)
def test_the_last_night_card_data_is_true_for_every_fate_of_the_interrupted_job(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, case: str, state: str, made: bool
) -> None:
    """The card drew every `interrupted_jobs` entry as 'not finished', also the job the night rescued."""
    from ns_common.errors import NsError

    paths, db = _setup(tmp_home, status="paused" if case == "open" else "active")
    driver.install(monkeypatch)
    dead = _dead_daily_job(paths, db)
    dash = _served(tmp_home)

    def run_fn(ctx: Any) -> list[str]:
        if case == "failed_again":
            raise RuntimeError("a late stage breaks")
        if case == "waiting":
            raise NsError("NS-E201", "held by another program")
        return ["d31_c"]

    daily.run_daily(jobs.open_env(light=True), rng=random.Random(1), run_fn=run_fn)
    (last,) = dash.engine.runs(1)  # the summary as the dashboard serves it to the Queue page
    assert _lines(last) == [(dead, state, state == "resumed")], last["summary"]
    assert last["summary"]["interrupted_jobs"][0]["stage"] == "d31_b"
    assert [j["job_id"] for j in last["summary"]["jobs"]] == ([dead] if made else [])
    assert last["state"] == "finished", "the night itself finished; only the job's fate differs"
    failures = last["summary"]["failures"]
    if case == "waiting":  # waiting is not a failure: no red NS-E201 line next to 'waiting for the GPU'
        assert [f for f in failures if f["job_id"] == dead] == [] and failures == []
    if case == "failed_again":
        assert [f["job_id"] for f in failures] == [dead]


def test_the_served_state_follows_the_job_when_it_is_finished_later_by_retry(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead run's summary says 'open' when closed; once Retry has finished the job the card must not."""
    from ns_studio import ownership

    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    job = _dead_daily_job(paths, db, stage_done=("d31_a",))
    db.execute("insert into runs(run_id, started_at, mode) values ('daily-x', ?, 'daily')", (_now_iso(),))
    ownership.write_record(
        paths,
        "daily-x",
        kind="run",
        state="running",
        pid=2**22,
        origin="daily",
        jobs=[{"job_id": job, "channel": "after-dark"}],
    )
    ownership.recover(paths, db)
    dash = _served(tmp_home)
    (last,) = dash.engine.runs(1)
    assert last["state"] == "interrupted" and _lines(last) == [(job, "open", False)]
    state = StageState()
    for name in ("d31_a", "d31_b", "d31_c"):
        state.mark_done(name, "h")
    state.save(paths.job_dir(job))  # Retry ran the job to its end
    (after,) = dash.engine.runs(1)
    assert _lines(after) == [(job, "resumed", True)]


def test_the_queue_page_words_each_state_of_an_interrupted_job() -> None:
    queue = (REPO / "ns_dashboard" / "ui" / "src" / "pages" / "Queue.tsx").read_text(encoding="utf-8")
    for state in ("open", "resumed", "failed_again", "waiting"):
        assert f'"{state}"' in queue, state
    assert "not finished" in queue and "and finished" in queue and "stopped again" in queue
    assert "GPU" in queue


def _manual_card(db: Db, job_id: str, *, created: str) -> None:
    """A dashboard Queue card (failed) that names `job_id`: what a failed Run now leaves behind."""
    payload = json.dumps({"params": {"channel": "after-dark"}, "result": {"studio_job_ids": [job_id]}})
    db.execute(
        "insert into dashboard_jobs(job_id, kind, channel, status, progress, message, created_at, started_at,"
        " finished_at, params_json) values (?, 'run', 'after-dark', 'failed', 0, 'qc failed', ?, ?, ?, ?)",
        (f"run-{job_id}", created, created, created, payload),
    )


def test_a_legacy_recovery_adopts_only_the_jobs_the_killed_nightly_made(tmp_home: Path) -> None:
    """0.1.14 inserts a `runs` row only for the nightly: a manual job made after it must stay."""
    from ns_studio import ownership

    paths, db = _setup(tmp_home)
    env = jobs.open_env(light=True)
    ch = crud.get_channel(paths, "after-dark")
    now = datetime.now(UTC)
    killed_at = now - timedelta(hours=20)
    db.execute(
        "insert into runs(run_id, started_at, mode) values ('daily-old', ?, 'daily')",
        (killed_at.isoformat(timespec="seconds"),),
    )

    def made(when: datetime) -> str:
        ctx = jobs.make_job(env, ch, "master_first", now=when)
        ownership.forget(paths, ctx.job_id)  # 0.1.14 wrote no owner file
        state = StageState()
        state.mark_done("ideas", "h")
        state.save(ctx.job_dir)
        return ctx.job_id

    nightly = made(killed_at)  # the nightly stamps its row and its job with the same `now`
    manual = made(killed_at + timedelta(hours=2))  # a Run now after the killed night, no runs row of its own
    cli_run = made(killed_at + timedelta(hours=3))
    carded = made(killed_at)  # the same second as the night (a Run now pressed as it started) ...
    _manual_card(db, carded, created=killed_at.isoformat(timespec="seconds"))  # ... but it has its own card

    out = ownership.recover(paths, db, now=now, note=False)

    assert [j.job_id for j in out.jobs] == [nightly]
    rec = ownership.read_record(paths, nightly)
    assert rec is not None and rec["origin"] == "daily"
    for other in (manual, cli_run, carded):
        assert ownership.read_record(paths, other) is None, "a job the nightly did not make is left alone"
    ours = [r["result"]["studio_job_ids"] for r in _queue_rows(db) if r["params"].get("source")]
    assert ours == [[nightly]]
    row = db.one("select summary_json from runs where run_id='daily-old'")
    assert row is not None
    assert [i["job_id"] for i in json.loads(row["summary_json"])["interrupted_jobs"]] == [nightly]
    # and the next nightly continues the nightly's job only: no second master, no paid work nobody asked for
    assert [r["id"] for r in ownership.pending_resumes(paths, db, now)] == [nightly]


def test_a_second_start_waits_for_the_one_that_is_closing_things_then_scans_again(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Engine and nightly start together after a power cut: the nightly must not plan before J is closed."""
    import threading

    from ns_studio import ownership

    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    dead = _dead_daily_job(paths, db)
    inside, release = threading.Event(), threading.Event()
    real = ownership._close_job

    def slow_close(*a: Any, **kw: Any) -> Any:
        inside.set()  # the engine is in the middle of closing the job; its record is written last
        release.wait(10)
        return real(*a, **kw)

    monkeypatch.setattr(ownership, "_close_job", slow_close)
    engine = threading.Thread(target=lambda: ownership.recover(paths, Db(paths.db_path)), name="engine")
    engine.start()
    assert inside.wait(10)
    timer = threading.Timer(0.6, release.set)
    timer.start()
    report = daily.run_daily(jobs.open_env(light=True), rng=random.Random(1))  # the nightly starts now
    engine.join(10)
    timer.cancel()

    assert [j["job_id"] for j in report["jobs"]] == [dead] and report["jobs"][0]["resumed"] is True
    assert _long_jobs(db) == [dead], "no second master was started while the first one was being closed"


def test_a_resume_that_meets_a_retry_of_the_same_job_is_not_a_failure(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dashboard's Retry runs the job: the nightly leaves it be, and the card and the toast alone."""
    from ns_common.singleton import FileLock
    from ns_studio import ownership

    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    dead = _dead_daily_job(paths, db)
    retry = FileLock(ownership.lock_path(paths, dead))
    real_load = jobs.load_job

    def retry_takes_it_first(env: Any, job_id: str) -> Any:
        ctx = real_load(env, job_id)
        retry.acquire()  # the Retry of the same job starts between the closing and the attempt
        ownership.write_record(paths, job_id, state="running", pid=os.getpid())  # as `hold` does
        return ctx

    monkeypatch.setattr(jobs, "load_job", retry_takes_it_first)
    told: list[str] = []
    try:
        report = daily.run_daily(
            jobs.open_env(light=True), rng=random.Random(1), notifier=lambda t, m: told.append(m) or True
        )
    finally:
        retry.release()

    assert report["failures"] == [] and report["jobs"] == [], "the retry owns the job: nothing failed"
    assert all("failed" not in m for m in told), told
    (card,) = _queue_rows(db)
    assert "stopped again" not in card["message"] and card["result"]["interrupted"]["resume"] == "pending"
    (note,) = _notes(tmp_home)
    assert "stopped again" not in note["message"] and "being continued" in note["message"]


def test_a_job_with_no_card_is_never_told_to_press_retry(tmp_home: Path) -> None:
    from ns_studio import notifications, ownership
    from ns_studio.cli import interrupted_line

    paths, db = _setup(tmp_home)
    no_card = {
        "job_id": "j-1",
        "channel": "after-dark",
        "stage": "d31_b",
        "state": "open",
        "resumed": False,
        "has_card": False,
    }
    with_card = {**no_card, "job_id": "j-2", "has_card": True}

    def toast(item: dict[str, Any]) -> str:
        text = notifications.nightly_message({"jobs": [], "failures": [], "interrupted_jobs": [item]}, 0, 0)
        assert text is not None
        return text

    assert "Retry in the Queue" not in toast(no_card) and "studio run --resume j-1" in toast(no_card)
    assert "Retry in the Queue" in toast(with_card)
    assert "GPU" in toast({**with_card, "state": "waiting"})
    assert "Retry in the Queue" not in toast({**with_card, "state": "waiting"})
    two = {**no_card, "job_id": "j-3", "stage": "d31_c"}
    text = notifications.nightly_message(
        {"jobs": [], "failures": [], "interrupted_jobs": [no_card, two]}, 0, 0
    )
    assert text is not None
    assert "studio run --resume j-1 (" in text and "studio run --resume j-3 (" in text, text
    assert "j-1, j-3" not in text, "--resume takes one job: each command stands on its own"
    assert "press Retry" not in interrupted_line(no_card) and "--resume j-1" in interrupted_line(no_card)
    assert "press Retry in the Queue" in interrupted_line(with_card)
    assert "None" not in interrupted_line({**with_card, "stage": None})
    assert "finished" in interrupted_line({**with_card, "state": "resumed", "resumed": True})

    # `studio status`: a job known only through `hold` has no card; the night never continues a manual job
    hold_only = _dead_daily_job(paths, db)
    ownership.write_record(paths, hold_only, origin="manual", announced_at=None)
    manual = _dead_daily_job(paths, db)
    ownership.write_record(paths, manual, origin="manual")
    daily_job = _dead_daily_job(paths, db)
    lines = {ln.split("job ")[1].split(" ")[0]: ln for ln in ownership.status_lines(paths, db, "after-dark")}
    assert "Retry" not in lines[hold_only] and f"studio run --resume {hold_only}" in lines[hold_only]
    assert "Retry in the Queue" in lines[manual] and "nightly" not in lines[manual]
    assert "Retry in the Queue" in lines[daily_job] and "next nightly run continues it" in lines[daily_job]


def test_the_run_command_names_the_whole_command_to_continue_a_dead_job(
    tmp_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`studio run` closes dead work first and tells it: the line must be a command that can be typed."""
    from ns_studio import cli, ownership

    paths, db = _setup(tmp_home)
    job = _dead_daily_job(paths, db)
    ownership.write_record(paths, job, origin="manual", announced_at=None)  # known only through `hold`
    cli._close_dead_work(jobs.open_env(light=True))
    out = capsys.readouterr().out
    assert f"studio run --resume {job}" in out, out


def test_a_ctrl_c_inside_a_job_leaves_it_to_be_closed_with_its_card_and_stage(tmp_home: Path) -> None:
    """`hold` wrote `interrupted` itself on BaseException: `_close_job` never ran (no card, note, stage)."""
    from ns_studio import ownership

    paths, db = _setup(tmp_home)
    ctx = jobs.make_job(
        jobs.open_env(light=True), crud.get_channel(paths, "after-dark"), "master_first", origin="daily"
    )
    with pytest.raises(KeyboardInterrupt), ownership.hold_job(paths, ctx.job_id, channel="after-dark"):
        raise KeyboardInterrupt
    assert ownership.liveness(paths, ctx.job_id) == "dead", "nobody holds it any more"
    out = ownership.recover(paths, db)
    (item,) = out.jobs
    assert item.job_id == ctx.job_id and item.card_id is not None and item.stage
    assert len(_queue_rows(db)) == 1 and len(_notes(tmp_home)) == 1


def test_a_lock_that_cannot_be_taken_refuses_the_job_instead_of_running_it_unowned(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ns_studio import ownership
    from ns_studio.errors import StudioError

    paths = Paths.from_env()
    paths.ensure()

    class NoLockFiles:
        def __init__(self, path: Path) -> None:
            self.path = path

        def acquire(self) -> None:
            raise PermissionError("the owners folder is read-only")

        def release(self) -> None:
            pass

    monkeypatch.setattr(ownership, "FileLock", NoLockFiles)
    monkeypatch.setattr(ownership, "OPEN_RETRY_WAITS_S", (0.0,) * 4)  # the tries stay five, the waits do not
    ran: list[str] = []
    with pytest.raises(StudioError) as ei, ownership.hold_job(paths, "j-9"):
        ran.append("ran")
    assert ran == [] and "read-only" in str(ei.value)
    assert isinstance(ei.value, ownership.LockUnavailable)


def test_the_card_lookup_does_not_treat_an_underscore_as_a_wildcard(tmp_home: Path) -> None:
    from ns_studio import ownership

    paths, db = _setup(tmp_home)
    _manual_card(db, "x-20261109-abc1", created=_now_iso())
    assert [r["job_id"] for r in ownership._rows_for(db, "x-20261109-abc1")] == ["run-x-20261109-abc1"]
    assert ownership._rows_for(db, "x-20261109-ab_1") == [], "`_` matched any character"
    assert ownership._rows_for(db, "x-20261109-ab%1") == [], "`%` matched anything"
