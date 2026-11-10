"""P47, what was left: every route says a paused channel's story job is held by the pause, and none starts it.

Before: a pause met by the studio child (the seconds between the worker's look and the first video) failed the
job as NS-E327 ("unexpected problem"); `studio run --channel <paused>` and `--resume` ran anyway; the Queue's
"Last night" card printed nothing for the report's `paused` key (and `types.ts` did not declare it); the
Assistant said a held job starts "as soon as possible"; an overdue scheduled job of a paused channel started
the dashboard at every engine start; a channel profile held open by another program (OSError) broke the
night's pause check; and a released Series Workshop part lost its title for "Queued".

After: NS-E330 with its hint on the job and in the terminal, nothing made; the card names the held jobs; the
Assistant says "when <channel> is resumed"; the engine start does not count the held job; an unreadable
profile is not a pause (a WARN, the night goes on); the part gets its title back. Each test enters where
the product enters (the studio child, the CLI, the routes, the nightly run, the supervisor's count) and
asserts the effect.
"""

from __future__ import annotations

import contextlib
import json
import random
import re
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ns_common.db import Db
from ns_common.errors import describe
from ns_common.logging import NsLogger
from ns_common.paths import Paths, repo_root
from ns_dashboard.events import EventHub
from ns_dashboard.fakes import seed_channel
from ns_dashboard.jobs import JobControl, JobStore, JobWorker
from ns_dashboard.state import open_db
from ns_studio import jobs, review
from ns_studio.channels import crud
from tests.helpers import make_channel

ROOT = Path(__file__).resolve().parents[1]
CH = "after-dark"
OTHER = "tiny-mysteries"
PAUSED_CODE = "NS-E330"  # the code a paused channel's refusal carries (contracts/errors.json)


def warns(paths: Paths, event: str) -> list[dict[str, Any]]:
    """The WARN records named `event` in every log of the home."""
    out: list[dict[str, Any]] = []
    for f in sorted(paths.logs.glob("*.jsonl")) if paths.logs.is_dir() else []:
        for line in f.read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(ValueError):
                rec = json.loads(line)
                if rec.get("level") == "WARN" and rec.get("event") == event:
                    out.append(rec)
    return out


# ------------------------------------------------------------------ 1. the studio child meets the pause

REAL_CHILD = r'''
import os
import sys

sys.path.insert(0, sys.argv[sys.argv.index("--repo") + 1])
from ns_dashboard import studio_job
from ns_studio import jobs


def made(*_a, **_kw):
    """Any video started: note it and stop (no model runs in this test, on any code)."""
    with open(os.environ["P47_MADE"], "a", encoding="utf-8") as fh:
        fh.write("made\n")
    raise RuntimeError("a story video was started for a paused channel")


jobs.make_job = made
jobs.make_script_job = made
sys.exit(studio_job.main(["run", "--args", sys.argv[sys.argv.index("--args") + 1]]))
'''


def test_a_pause_met_by_the_studio_child_fails_the_job_with_ns_e330(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The worker has looked (no hold here: the pause comes right after); the real `studio_job` child opens
    the studio with fake services and finds the channel paused before the first video."""
    from ns_dashboard.adapters import RealRunAdapter
    from ns_dashboard.studio_runner import StudioRunner
    from ns_dashboard.supervisor_link import SupervisorLink
    from ns_studio.context import FAKEABLE

    env = jobs.open_env(FAKEABLE)
    jobs.ensure_demo_channel(env)
    review.pause(env.paths, env.db, "demo", "warning arrived while the studio process started")
    made = tmp_path / "made.txt"
    monkeypatch.setenv("P47_MADE", str(made))
    script = tmp_path / "real_child.py"
    script.write_text(REAL_CHILD, encoding="utf-8")
    app = tmp_path / "app"
    app.mkdir()
    paths = Paths.from_env()
    runner = StudioRunner(
        paths, python=[sys.executable, str(script), "--repo", str(repo_root())], cwd=app, poll_s=0.05
    )
    adapter = RealRunAdapter(
        paths, SupervisorLink("http://127.0.0.1:9", timeout_s=0.2), fake=tuple(sorted(FAKEABLE)),
        runner=runner,
    )

    def handler(ctl: JobControl) -> dict[str, Any]:
        return {"output_ids": adapter.run(ctl, "demo", "shorts_only", 1)}

    store = JobStore(open_db(paths))
    worker = JobWorker(store, {"run": handler}, EventHub(), NsLogger("dashboard", paths.logs))
    job = store.create("run", "demo", {"channel": "demo", "format": "shorts_only", "count": 1})
    assert worker.run_next()
    rec = store.get(job.job_id)
    assert rec is not None and rec.status == "failed" and rec.result is not None
    assert rec.result["error"]["code"] == PAUSED_CODE, rec.message
    assert rec.result["error"]["hint"] == describe(PAUSED_CODE).hint
    assert PAUSED_CODE in rec.message and "NS-E327" not in rec.message
    assert "none was made" in rec.message
    assert not made.exists(), "no video may be started"
    assert not paths.jobs_dir.exists() or not any(paths.jobs_dir.iterdir()), "no job folder"


# ------------------------------------------------------------------ 3. studio run typed in a terminal

cli_runner = CliRunner()
CALLS: list[str] = []
JOB_CALLS: list[str] = []  # the same calls by job id


class StubA:
    name = "p47f_a"
    tenant = None
    inputs = ()
    outputs = ("a.json",)

    pause_on: str | None = None  # pause this channel while the stage runs (once): the pause lands mid-job

    def run(self, ctx: Any) -> None:
        CALLS.append(f"a:{ctx.channel.slug}")
        JOB_CALLS.append(f"a:{ctx.job_id}")
        if StubA.pause_on == ctx.channel.slug:
            StubA.pause_on = None
            review.pause(ctx.paths, ctx.db, ctx.channel.slug, "a warning arrived while this stage ran")
        ctx.write_json("a.json", {"n": 1})


class StubB:
    name = "p47f_b"
    tenant = None
    inputs = ("a.json",)
    outputs = ("b.json",)
    pause_after: int | None = None  # pause `CH` when this many of its jobs have run this stage

    def run(self, ctx: Any) -> None:
        slug = ctx.channel.slug
        CALLS.append(f"b:{slug}")
        JOB_CALLS.append(f"b:{ctx.job_id}")
        ctx.write_json("b.json", {"n": 2})
        if slug == CH and StubB.pause_after is not None and CALLS.count(f"b:{CH}") == StubB.pause_after:
            review.pause(ctx.paths, ctx.db, slug, "a warning arrived while the run went on")


@pytest.fixture
def two_channels(tmp_home: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Paths, Db]:
    """Two active shorts channels, the stage plan replaced by two quick stubs (nothing renders)."""
    from ns_studio.orchestrator import FORMAT_STAGES
    from ns_studio.stages import REGISTRY

    CALLS.clear()
    JOB_CALLS.clear()
    StubB.pause_after = None
    StubA.pause_on = None
    monkeypatch.setitem(REGISTRY, StubA.name, StubA())
    monkeypatch.setitem(REGISTRY, StubB.name, StubB())
    monkeypatch.setitem(FORMAT_STAGES, "shorts_only", [StubA.name, StubB.name])
    monkeypatch.setattr(jobs, "STAGE_MODULES", ())
    monkeypatch.setattr("ns_studio.policy.gate.available", lambda: False)
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    for slug in (CH, OTHER):
        crud.add_channel(
            paths, db, make_channel(slug=slug, display_name=slug.replace("-", " ").title(),
                                    niche=f"original fiction about {slug}", format="shorts_only"),
        )
    return paths, db


def job_dirs(paths: Paths, slug: str) -> list[Path]:
    return sorted(paths.jobs_dir.glob(f"{slug}-*")) if paths.jobs_dir.is_dir() else []


def studio(*args: str) -> Any:
    from ns_studio.cli import app

    return cli_runner.invoke(app, [*args, "--fake-services"])


def test_studio_run_for_a_paused_channel_refuses_with_ns_e330(two_channels: tuple[Paths, Db]):
    paths, db = two_channels
    review.pause(paths, db, CH, "warning")
    res = studio("run", "--channel", CH, "--format", "shorts_only")
    assert PAUSED_CODE in res.output, res.output
    assert res.exit_code == 1
    assert describe(PAUSED_CODE).hint in res.output and "no job was made" in res.output
    assert CALLS == [] and job_dirs(paths, CH) == [], "no job folder, no stage"
    # control: with no --channel the paused one is skipped as before, the other is made
    res = studio("run", "--format", "shorts_only")
    assert res.exit_code == 0, res.output
    assert CALLS == [f"a:{OTHER}", f"b:{OTHER}"] and job_dirs(paths, CH) == []
    assert len(job_dirs(paths, OTHER)) == 1


def test_studio_run_resume_of_a_paused_channels_job_refuses(two_channels: tuple[Paths, Db]):
    """Killed halfway (its last stage's file is gone), the channel paused, then `--resume`: nothing runs."""
    paths, db = two_channels
    res = studio("run", "--channel", CH, "--format", "shorts_only")
    assert res.exit_code == 0, res.output
    (job_dir,) = job_dirs(paths, CH)
    (job_dir / "b.json").unlink()
    review.pause(paths, db, CH, "warning")
    CALLS.clear()
    res = studio("run", "--resume", job_dir.name)
    assert PAUSED_CODE in res.output, res.output
    assert res.exit_code == 1 and f"job {job_dir.name} was not continued" in res.output
    assert CALLS == [] and not (job_dir / "b.json").exists(), "no stage ran"
    # resumed: the same command continues it
    review.resume(paths, db, CH)
    res = studio("run", "--resume", job_dir.name)
    assert res.exit_code == 0, res.output
    assert CALLS == [f"b:{CH}"] and (job_dir / "b.json").exists()


def test_a_pause_typed_during_a_studio_run_stops_its_next_job(two_channels: tuple[Paths, Db]):
    """`--count 3`: the channel is paused while the first job runs; the second is not started."""
    paths, _db = two_channels
    StubB.pause_after = 1
    res = studio("run", "--channel", CH, "--format", "shorts_only", "--count", "3")
    assert PAUSED_CODE in res.output, res.output
    assert res.exit_code == 1 and "1 of 3 jobs were made" in res.output
    assert CALLS == [f"a:{CH}", f"b:{CH}"] and len(job_dirs(paths, CH)) == 1


def test_a_run_of_every_channel_leaves_out_one_paused_during_it(two_channels: tuple[Paths, Db]):
    paths, _db = two_channels
    StubB.pause_after = 1  # the first job of CH pauses it
    res = studio("run", "--format", "shorts_only", "--count", "2")
    assert res.exit_code == 0, res.output
    assert f"channel {CH} was paused: 1 job(s) not started" in res.output
    assert len(job_dirs(paths, CH)) == 1 and len(job_dirs(paths, OTHER)) == 2


# ------------------------------------------------------------------ 4. the Queue's "Last night" card


def test_the_last_night_card_names_the_jobs_a_pause_held():
    source = (ROOT / "ns_dashboard" / "ui" / "src" / "pages" / "Queue.tsx").read_text(encoding="utf-8")
    assert "r.summary?.paused" in source
    built = (ROOT / "ns_dashboard" / "static" / "app.js").read_text(encoding="utf-8")
    held = re.search(r"not started: \$\{[^`]{1,80}\} is paused", built)
    assert held, "the rebuilt bundle holds the sentence"
    assert "paused." in built and "held, the channel" in built
    assert "its half-made job waits for the resume" in built


# ------------------------------------------------------------------ 5. the Assistant's tonight_plan


def test_tonight_plan_says_a_held_job_waits_for_the_resume(tmp_home: Path):
    from ns_dashboard.assistant.registry import ToolContext
    from tests.assistant_eval import fixture_home

    home = fixture_home.build(tmp_home)
    td = home.dash.registry.get("tonight_plan")
    assert td is not None

    def plan() -> dict[str, dict[str, Any]]:
        out = td.call(ToolContext(home.dash, {}), {})
        return {j["job_id"]: j for j in out["queued_jobs"]}

    assert plan() == {}, "nothing queued: nothing listed"
    store = home.state.store
    held = store.create("run", "wholesome-twist", {"channel": "wholesome-twist", "format": "shorts_only"})
    at = (datetime.now().astimezone() + timedelta(hours=2)).isoformat(timespec="seconds")
    later = store.create("run", "wholesome-twist", {"channel": "wholesome-twist", "schedule_at": at})
    clip = store.create("clip", "wholesome-twist", {"channel": "wholesome-twist"})
    free = store.create("run", "after-dark", {"channel": "after-dark", "format": "shorts_only"})
    assert plan()[held.job_id]["start"] == "as soon as possible", "active: as before"
    home.dash.channels.pause("wholesome-twist", "strike")
    now = plan()
    assert now[held.job_id]["start"] == "when Wholesome Twist is resumed"
    start = now[later.job_id]["start"]
    assert at in start and "when Wholesome Twist is resumed" in start
    assert now[clip.job_id]["start"] == "as soon as possible", "a Clipper job is not held"
    assert now[free.job_id]["start"] == "as soon as possible", "another channel's job is not held"
    home.dash.channels.resume("wholesome-twist")
    assert plan()[held.job_id]["start"] == "as soon as possible"


# ------------------------------------------------------------------ 6. the engine start (supervisor)


def put_job(paths: Paths, job_id: str, kind: str, channel: str, schedule_at: str) -> None:
    """A scheduled job row as 0.1.14 writes it (`JobStore.create` plus `schedule_at` in the params)."""
    params = {"params": {"channel": channel, "schedule_at": schedule_at}, "result": None}
    con = sqlite3.connect(paths.db_path)
    con.execute(
        "INSERT INTO dashboard_jobs(job_id, kind, channel, status, progress, message, created_at,"
        " params_json) VALUES (?, ?, ?, 'queued', 0, 'Scheduled for Mon 02 Nov 03:00',"
        " '2026-11-02T00:00:00', ?)",
        (job_id, kind, channel, json.dumps(params)),
    )
    con.commit()
    con.close()


def test_an_overdue_job_of_a_paused_channel_does_not_wake_the_dashboard(tmp_home: Path):
    from ns_supervisor.scheduled import overdue_scheduled_jobs

    paths = Paths.from_env()
    paths.ensure()
    db = open_db(paths)
    seed_channel(db, paths, CH)
    seed_channel(db, paths, OTHER)
    now = datetime.now().astimezone()
    past = (now - timedelta(minutes=5)).isoformat(timespec="seconds")
    put_job(paths, "held", "run", CH, past)
    assert overdue_scheduled_jobs(paths, now) == 1, "active channel: it wakes the dashboard, as before"
    review.pause(paths, Db(paths.db_path), CH, "warning")
    assert overdue_scheduled_jobs(paths, now) == 0, "paused: the worker would only hold it"
    put_job(paths, "clip", "clip", CH, past)
    put_job(paths, "other", "run", OTHER, past)
    assert overdue_scheduled_jobs(paths, now) == 2, "a Clipper job and another channel's job still count"
    paths.channel_file(CH).write_text("status: [paused\n", encoding="utf-8")  # torn profile
    assert overdue_scheduled_jobs(paths, now) == 3, "an unreadable profile counts as before"
    from ns_dashboard.channel_hold import HELD_KINDS as DASHBOARD_HELD
    from ns_supervisor.scheduled import HELD_KINDS

    assert HELD_KINDS == DASHBOARD_HELD, "the supervisor's copy of the held kinds"


# ------------------------------------------------------------------ 7. a profile that cannot be read

HELD_OPEN = "[WinError 32] The process cannot access the file because it is being used by another process"


def test_a_profile_that_cannot_be_read_is_not_a_pause_and_does_not_stop_the_night(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """Windows: another program holds the profile open (PermissionError, an OSError) while the night looks."""
    from ns_dashboard import studio_bridge
    from ns_studio import daily

    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    now = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)
    created = (now - timedelta(weeks=9)).isoformat(timespec="seconds")
    crud.add_channel(paths, db, make_channel(slug=CH, display_name="After Dark", created_at=created,
                                             format="shorts_only", shorts_per_day_max=2))
    env = jobs.open_env(light=True)
    env.services.footage_key = True
    ran: list[str] = []

    def run_fn(ctx: Any) -> list[str]:
        ran.append(ctx.channel.slug)
        return []

    def held_open(*_a: Any, **_kw: Any) -> Any:
        raise PermissionError(13, HELD_OPEN)

    monkeypatch.setattr(daily, "get_channel", held_open)  # the nightly's look at the profile
    report = daily.run_daily(env, now=now, rng=random.Random(1), run_fn=run_fn, awake=contextlib.nullcontext)
    assert ran == [CH, CH] and report["failures"] == [], "the night goes on"
    assert not report.get("paused")
    seen = warns(paths, "channel_profile_unreadable")
    assert seen, "a WARN says the profile could not be read"
    assert all(w["data"]["channel"] == CH and HELD_OPEN in w["msg"] for w in seen)
    # the studio child's look (`run_story_jobs`, `run_series_part`)
    monkeypatch.setattr(crud, "get_channel", held_open)
    paused_now = getattr(studio_bridge, "paused_now", None)
    assert paused_now is not None, "the studio child reads the pause before each video"
    assert paused_now(env, CH) is False
    assert len(warns(paths, "channel_profile_unreadable")) == len(seen) + 1


# ------------------------------------------------------------------ 8. a Series Workshop part


def test_a_released_series_part_gets_its_title_back(tmp_home: Path):
    """Queued from the Series Workshop (its message is its title), held, released: twice."""
    pytest.importorskip("fastapi")
    from ns_dashboard.fakes import FakeSeriesAdapter
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths, CH)
    series = state.adapters.series
    assert isinstance(series, FakeSeriesAdapter)
    series.stories = [{
        "slug": "the-lift", "channel": CH, "status": "producing", "title": "The Lift", "next_step": "produce",
        "has_design": True, "chosen": "A", "drafts": [],
        "parts": [{"n": 1, "title": "Floor 13", "minutes": 15, "status": "approved", "job_id": None,
                   "words": 2250, "shorts": 3}],
    }]
    made: list[str] = []
    state.worker.handlers["series_part"] = lambda ctl: made.append(ctl.job_id) or {"output_ids": []}
    r = client.post("/api/library/series/the-lift/produce")
    assert r.status_code == 201, r.text
    (queued,) = r.json()["queued"]
    job_id, title = str(queued["job_id"]), str(queued["title"])

    def message() -> str:
        return str(client.get(f"/api/jobs/{job_id}").json()["message"])

    assert message() == title
    for _ in range(2):  # the second pause and resume too
        assert client.post(f"/api/channels/{CH}/pause", json={"note": "warning"}).status_code == 200
        assert state.worker.run_next() is False and made == []
        assert message().startswith("Paused: ")
        assert client.post(f"/api/channels/{CH}/resume").status_code == 200
        assert message() == title, "the part's title, not 'Queued'"
    assert state.worker.run_next() is True and made == [job_id]


# -------------------------------------------------------------- 9. a pause while a job runs (firmware rows)
# The invariant: no stage of a paused channel's story job starts, and nothing is lost or duplicated when it is
# resumed. `ns_studio/pause.py:require_not_paused`, called by `orchestrator.run_job` before every stage, is
# where every route meets it. In the tests below the pause lands INSIDE a stage (the stub pauses the channel
# while it runs), which is the widest gap: the stage under way finishes, the next must not start.


def test_a_pause_during_a_stage_stops_studio_run_before_its_next_stage_and_the_resume_runs_the_rest_once(
    two_channels: tuple[Paths, Db],
):
    paths, db = two_channels
    StubA.pause_on = CH
    res = studio("run", "--channel", CH, "--format", "shorts_only")
    assert PAUSED_CODE in res.output, res.output
    assert res.exit_code == 1
    (job_dir,) = job_dirs(paths, CH)
    assert f"studio run --resume {job_dir.name}" in res.output, "the terminal says how to continue it"
    assert CALLS == [f"a:{CH}"], "the stage under way finished, the next did not start"
    assert (job_dir / "a.json").exists() and not (job_dir / "b.json").exists()
    res = studio("run", "--resume", job_dir.name)  # still paused: refused, nothing runs
    assert res.exit_code == 1 and PAUSED_CODE in res.output and CALLS == [f"a:{CH}"]
    review.resume(paths, db, CH)
    res = studio("run", "--resume", job_dir.name)
    assert res.exit_code == 0, res.output
    assert CALLS == [f"a:{CH}", f"b:{CH}"], "only the stage that did not run, once"
    res = studio("run", "--resume", job_dir.name)  # the second time: nothing runs again
    assert res.exit_code == 0 and CALLS == [f"a:{CH}", f"b:{CH}"]


def test_the_stage_check_counts_a_profile_held_open_as_not_paused(
    two_channels: tuple[Paths, Db], monkeypatch: pytest.MonkeyPatch
):
    import importlib

    assert importlib.util.find_spec("ns_studio.pause") is not None, "the one place routes check"
    pause = importlib.import_module("ns_studio.pause")
    paths, _db = two_channels

    def held_open(*_a: Any, **_kw: Any) -> Any:
        raise PermissionError(13, HELD_OPEN)

    monkeypatch.setattr(pause, "load_channel", held_open)
    res = studio("run", "--channel", CH, "--format", "shorts_only")
    assert res.exit_code == 0, res.output
    assert CALLS == [f"a:{CH}", f"b:{CH}"], "the job goes on"
    seen = warns(paths, "channel_profile_unreadable")
    assert seen and all(w["data"]["check"] == "pause" and w["data"]["channel"] == CH for w in seen)


def test_a_paused_job_gives_its_gpu_tenant_back_and_takes_no_other(
    two_channels: tuple[Paths, Db], monkeypatch: pytest.MonkeyPatch
):
    """Held, not stuck: the tenant of the stage under way is released, the next group's is never taken."""
    from dataclasses import replace

    from ns_studio.orchestrator import FORMAT_STAGES, run_job
    from ns_studio.stages import REGISTRY

    paths, _db = two_channels
    events: list[str] = []

    class Recorder:
        def acquire(self, tenant: str, owner: str, **_kw: Any) -> None:
            events.append(f"acquire {tenant}")

        def release(self, tenant: str, owner: str) -> None:
            events.append(f"release {tenant}")

    class LlamaA(StubA):
        name = "p47f_llama_a"
        tenant = "llama"

    class LlamaB(StubB):
        name = "p47f_llama_b"
        tenant = "llama"

    class Whisper(StubB):
        name = "p47f_whisper"
        tenant = "whisper"
        inputs = ("b.json",)
        outputs = ("w.json",)

    for stage in (LlamaA(), LlamaB(), Whisper()):
        monkeypatch.setitem(REGISTRY, stage.name, stage)
    monkeypatch.setitem(FORMAT_STAGES, "shorts_only", [LlamaA.name, LlamaB.name, Whisper.name])
    env = jobs.open_env(frozenset({"llm", "tts", "whisper", "embed", "clips"}))
    ctx = jobs.make_job(env, crud.get_channel(paths, CH), "shorts_only")
    ctx = replace(ctx, services=replace(ctx.services, acquirer=Recorder()))
    StubA.pause_on = CH
    with pytest.raises(Exception) as ei:
        run_job(ctx)
    assert getattr(ei.value, "code", None) == PAUSED_CODE
    assert events == ["acquire llama", "release llama"], "released at once, whisper never taken"
    assert CALLS == [f"a:{CH}"]


def test_a_clipper_job_of_a_paused_channel_is_not_stopped_between_its_stages(
    two_channels: tuple[Paths, Db], monkeypatch: pytest.MonkeyPatch
):
    from dataclasses import replace

    from ns_studio.orchestrator import FORMAT_STAGES, run_job

    paths, db = two_channels
    monkeypatch.setitem(FORMAT_STAGES, "clip", [StubA.name, StubB.name])
    env = jobs.open_env(frozenset({"llm", "tts", "whisper", "embed", "clips"}))
    story = jobs.make_job(env, crud.get_channel(paths, CH), "shorts_only")
    review.pause(paths, db, CH, "warning")
    with pytest.raises(Exception) as ei:
        run_job(story)  # control: a story job of the paused channel starts no stage at all
    assert getattr(ei.value, "code", None) == PAUSED_CODE and CALLS == []
    run_job(replace(story, fmt="clip"))
    assert CALLS == [f"a:{CH}", f"b:{CH}"], "a Clipper job is accepted for a paused channel"


# --- the dashboard: the real studio child stops between two stages; the job is held, then continues

CHILD_WITH_STAGES = r'''
import os
import sys

sys.path.insert(0, sys.argv[sys.argv.index("--repo") + 1])
import ns_studio.policy.gate as gate
from ns_dashboard import studio_job
from ns_studio import jobs, review
from ns_studio.orchestrator import FORMAT_STAGES
from ns_studio.stages import register


def note(line):
    with open(os.environ["P47_RAN"], "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


class A:
    name, tenant, inputs, outputs = "p47c_a", None, (), ("a.json",)

    def run(self, ctx):
        note(f"a:{ctx.job_id}")
        flag = os.environ["P47_PAUSED_ONCE"]
        if not os.path.exists(flag):  # the warning arrives while this stage runs, once
            open(flag, "w").close()
            review.pause(ctx.paths, ctx.db, ctx.channel.slug, "a warning arrived while the video was made")
        ctx.write_json("a.json", {"n": 1})


class B:
    name, tenant, inputs, outputs = "p47c_b", None, ("a.json",), ("b.json",)

    def run(self, ctx):
        note(f"b:{ctx.job_id}")
        ctx.write_json("b.json", {"n": 2})


register(A())
register(B())
FORMAT_STAGES["shorts_only"] = ["p47c_a", "p47c_b"]
jobs.STAGE_MODULES = ()
gate.available = lambda: False
sys.exit(studio_job.main(["run", "--args", sys.argv[sys.argv.index("--args") + 1]]))
'''


def test_a_dashboard_job_paused_between_two_stages_is_held_and_continues_after_the_resume(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pytest.importorskip("fastapi")
    from types import SimpleNamespace
    from typing import cast

    from ns_dashboard.adapters import RealRunAdapter
    from ns_dashboard.studio_runner import StudioRunner
    from ns_dashboard.supervisor_link import SupervisorLink
    from ns_dashboard.svc_jobs import build_handlers
    from ns_studio.context import FAKEABLE
    from tests.test_dashboard_api import make_env

    ran = tmp_path / "ran.txt"
    monkeypatch.setenv("P47_RAN", str(ran))
    monkeypatch.setenv("P47_PAUSED_ONCE", str(tmp_path / "paused-once"))
    script = tmp_path / "child.py"
    script.write_text(CHILD_WITH_STAGES, encoding="utf-8")
    (tmp_path / "app").mkdir()
    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths, CH)
    runner = StudioRunner(
        state.paths, python=[sys.executable, str(script), "--repo", str(repo_root())], cwd=tmp_path / "app",
        poll_s=0.05,
    )
    adapter = RealRunAdapter(
        state.paths, SupervisorLink("http://127.0.0.1:9", timeout_s=0.2), fake=tuple(sorted(FAKEABLE)),
        runner=runner,
    )
    state.worker.handlers["run"] = build_handlers(cast(Any, SimpleNamespace(run=adapter)))["run"]
    r = client.post("/api/jobs/run", json={"channel": CH, "format": "shorts_only"})
    assert r.status_code == 201, r.text
    job_id = r.json()["job_id"]

    def job() -> dict[str, Any]:
        return client.get(f"/api/jobs/{job_id}").json()

    def lines() -> list[str]:
        return ran.read_text(encoding="utf-8").split() if ran.exists() else []

    assert state.worker.run_next() is True
    first = lines()[0]
    studio_job_id = first.split(":", 1)[1]
    held = job()
    assert held["status"] == "queued", held["message"]
    assert lines() == [first], "the second stage did not start"
    assert held["message"].startswith("Paused: "), "held, with the reason, not failed"
    assert held["params"]["resume_job_id"] == studio_job_id and held["started_at"] is None
    assert state.worker.run_next() is False and lines() == [first], "nothing runs while paused"
    assert client.post(f"/api/channels/{CH}/resume").status_code == 200
    assert job()["message"] == "Queued"
    assert state.worker.run_next() is True
    done = job()
    assert done["status"] == "done", done["message"]
    assert lines() == [f"a:{studio_job_id}", f"b:{studio_job_id}"], "the rest, once: nothing redone or lost"
    assert state.worker.run_next() is False, "the second pass finds nothing to do"


def test_a_series_part_stopped_by_a_pause_keeps_its_job_and_continues(tmp_home: Path):
    pytest.importorskip("fastapi")
    from ns_common.errors import NsError
    from ns_dashboard.fakes import FakeSeriesAdapter
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths, CH)
    series = state.adapters.series
    assert isinstance(series, FakeSeriesAdapter)
    series.stories = [{
        "slug": "the-lift", "channel": CH, "status": "producing", "title": "The Lift", "next_step": "produce",
        "has_design": True, "chosen": "A", "drafts": [],
        "parts": [{"n": 1, "title": "Floor 13", "minutes": 15, "status": "approved", "job_id": None,
                   "words": 2250, "shorts": 3}],
    }]
    calls: list[str] = []

    def part(ctl: JobControl) -> dict[str, Any]:  # stands in for the studio: the pause lands mid-job once
        calls.append(ctl.job_id)
        if len(calls) == 1:
            review.pause(state.paths, Db(state.paths.db_path), CH, "warning")
            raise NsError(PAUSED_CODE, f"job {ctl.job_id} stopped before its stage voice")
        return {"output_ids": []}

    state.worker.handlers["series_part"] = part
    (queued,) = client.post("/api/library/series/the-lift/produce").json()["queued"]
    job_id = str(queued["job_id"])
    assert state.worker.run_next() is True
    now = client.get(f"/api/jobs/{job_id}").json()
    assert now["status"] == "queued" and now["message"].startswith("Paused: ")
    assert series.stories[0]["parts"][0]["status"] == "producing", "the part stays with its job"
    assert state.worker.run_next() is False
    assert client.post(f"/api/channels/{CH}/resume").status_code == 200
    assert client.get(f"/api/jobs/{job_id}").json()["message"] == str(queued["title"])
    assert state.worker.run_next() is True
    assert calls == [job_id, job_id] and client.get(f"/api/jobs/{job_id}").json()["status"] == "done"


@pytest.mark.parametrize("case", ["resumed_before_the_worker_looked", "no_studio_job_yet"])
def test_a_job_stopped_by_a_pause_fails_with_ns_e330_when_it_cannot_be_held(tmp_home: Path, case: str):
    """The resume lands between the studio's stop and the worker's look (the worker sees no pause: holding it
    would start it again at once, a loop), or a run job made no studio job yet: it fails with NS-E330 and its
    hint, once, and the worker goes idle."""
    pytest.importorskip("fastapi")
    from ns_common.errors import NsError
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths, CH)
    calls: list[str] = []

    def run(ctl: JobControl) -> dict[str, Any]:
        calls.append(ctl.job_id)
        if case == "no_studio_job_yet":
            review.pause(state.paths, Db(state.paths.db_path), CH, "warning")
        else:
            ctl.extras["studio_job_ids"] = ["after-dark-20261110-0001"]
        raise NsError(PAUSED_CODE, "stopped")

    state.worker.handlers["run"] = run
    job_id = client.post("/api/jobs/run", json={"channel": CH}).json()["job_id"]
    assert state.worker.run_next() is True
    rec = client.get(f"/api/jobs/{job_id}").json()
    assert rec["status"] == "failed" and rec["result"]["error"]["code"] == PAUSED_CODE
    assert rec["result"]["error"]["hint"] == describe(PAUSED_CODE).hint
    assert state.worker.run_next() is False and calls == [job_id], "no loop"


def test_a_nightly_job_paused_between_two_stages_waits_and_the_first_night_after_the_resume_continues_it(
    two_channels: tuple[Paths, Db],
):
    from ns_common.config import save_channel
    from ns_studio import daily, ownership
    from ns_studio.policy.regen import run_job_guarded

    paths, db = two_channels
    review.pause(paths, db, OTHER, "not part of this test")
    now = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)
    created = (now - timedelta(weeks=9)).isoformat(timespec="seconds")
    ch = crud.get_channel(paths, CH)
    save_channel(paths, ch.model_copy(update={"created_at": created, "shorts_per_day_max": 2}))
    env = jobs.open_env(light=True)
    env.services.footage_key = True
    StubA.pause_on = CH

    def night(at: datetime) -> dict[str, Any]:
        return daily.run_daily(env, now=at, rng=random.Random(1), run_fn=run_job_guarded,
                               awake=contextlib.nullcontext)

    report = night(now)
    (first,) = {c.split(":", 1)[1] for c in JOB_CALLS}
    assert JOB_CALLS == [f"a:{first}"], "the second stage and the second job did not start"
    assert report["failures"] == [], "a pause is not a failure"
    held = {(p["channel"], p.get("job_id"), p["jobs"]) for p in report["paused"]}
    assert (CH, first, 1) in held and (CH, None, 1) in held
    record = ownership.read_record(paths, first) or {}
    assert record.get("state") == "interrupted" and record.get("origin") == "daily"
    assert db.query("select 1 from dashboard_jobs where params_json like ?", (f"%{first}%",)), "a Queue card"
    review.resume(paths, db, CH)
    again = night(now + timedelta(days=1))
    assert JOB_CALLS.count(f"a:{first}") == 1 and JOB_CALLS.count(f"b:{first}") == 1, "continued, not redone"
    assert any(j["job_id"] == first and j.get("resumed") for j in again["jobs"])
