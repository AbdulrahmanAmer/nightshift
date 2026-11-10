"""P47, the state machine of a Create request that a pause stops mid-way (BUILDER_RULES 1b).

States of the request (one `dashboard_jobs` row): queued; running a stage; waiting for the GPU; stopped by the
studio (NS-E330) but not yet seen by the dashboard; put back in line (held "Paused: ...", or plain "Queued"
when the owner resumed first); failed and offered for Retry; cancelled; done.

Invariants, each decided in one place:
- no stage of a paused channel's story job starts: `ns_studio/pause.py:require_not_paused` (every route);
- every video asked for is made exactly once or is visibly cancelled: `ns_dashboard/jobs.py:owed_after_stop`
  (read by the worker's requeue and by Retry) and `JobStore.requeue` (a Cancel always wins);
- nothing a person sees is untrue: `JobWorker._whole` (the card lists every video of the request),
  `cancelled_text` (what a cancelled request had made), `channel_hold.hold_text` (what a held one owes).

Each test below is one row of the table: a transition, met by the resume, a second pause, a restart, Cancel
or Retry. The timing rows block the step next to the gap (the worker's look, Cancel's flag) until the other
side has moved. The studio child is the real `studio_job` process wherever the row is about what it reports.
"""

from __future__ import annotations

import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from ns_common.db import Db
from ns_common.errors import NsError, describe
from ns_common.paths import repo_root
from ns_dashboard import jobs as dashboard_jobs
from ns_dashboard.fakes import seed_channel
from ns_dashboard.jobs import JobControl, JobWorker
from ns_studio import review

pytest.importorskip("fastapi")

CH = "after-dark"
PAUSED_CODE = "NS-E330"
#: read with a default, so that on a base without it the tests fail at their assertions, not at the import
PAUSE_REQUEUE_LIMIT = int(getattr(dashboard_jobs, "PAUSE_REQUEUE_LIMIT", 3))
V1, V2, V3 = (f"{CH}-20261110-000{n}" for n in (1, 2, 3))

CHILD = r'''
import os
import sys

sys.path.insert(0, sys.argv[sys.argv.index("--repo") + 1])
import ns_studio.policy.gate as gate
from ns_dashboard import studio_job
from ns_studio import jobs, review
from ns_studio.orchestrator import FORMAT_STAGES
from ns_studio.stages import register

RAN = os.environ["P47S_RAN"]


def step(ctx, stage):
    """Note the stage; pause the channel inside it when the plan names this run of it ("a:2": the 2nd a)."""
    with open(RAN, "a", encoding="utf-8") as fh:
        fh.write(f"{stage}:{ctx.job_id}\n")
    with open(RAN, encoding="utf-8") as fh:
        nth = sum(1 for line in fh if line.startswith(stage + ":"))
    if f"{stage}:{nth}" in os.environ.get("P47S_PAUSE_AT", "").split(","):
        review.pause(ctx.paths, ctx.db, ctx.channel.slug, "a warning arrived while the video was made")
    ctx.write_json(stage + ".json", {"ok": True})


class A:
    name, tenant, inputs, outputs = "p47s_a", None, (), ("a.json",)

    def run(self, ctx):
        step(ctx, "a")


class B:
    name, tenant, inputs, outputs = "p47s_b", None, ("a.json",), ("b.json",)

    def run(self, ctx):
        step(ctx, "b")


register(A())
register(B())
FORMAT_STAGES["shorts_only"] = ["p47s_a", "p47s_b"]
jobs.STAGE_MODULES = ()
gate.available = lambda: False
sys.exit(studio_job.main(["run", "--args", sys.argv[sys.argv.index("--args") + 1]]))
'''


@pytest.fixture
def dash(tmp_home: Path) -> SimpleNamespace:
    """The dashboard over a fresh home with one active channel; `run_next` is the worker's one pass."""
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths, CH)

    def job(job_id: str) -> dict[str, Any]:
        return client.get(f"/api/jobs/{job_id}").json()

    def ask(count: int = 3) -> str:
        r = client.post("/api/jobs/run", json={"channel": CH, "format": "shorts_only", "count": count})
        assert r.status_code == 201, r.text
        return str(r.json()["job_id"])

    def pause() -> None:
        assert client.post(f"/api/channels/{CH}/pause", json={"note": "warning"}).status_code == 200

    def resume() -> None:
        assert client.post(f"/api/channels/{CH}/resume").status_code == 200

    return SimpleNamespace(client=client, state=state, job=job, ask=ask, pause=pause, resume=resume,
                           run_next=state.worker.run_next)


@pytest.fixture
def real(dash: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """`dash` whose story jobs run in the real studio child (two quick stub stages, fake services)."""
    from ns_dashboard.adapters import RealRunAdapter
    from ns_dashboard.studio_runner import StudioRunner
    from ns_dashboard.supervisor_link import SupervisorLink
    from ns_dashboard.svc_jobs import build_handlers
    from ns_studio.context import FAKEABLE

    ran = tmp_path / "ran.txt"
    monkeypatch.setenv("P47S_RAN", str(ran))
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    (tmp_path / "app").mkdir()
    paths = dash.state.paths
    runner = StudioRunner(
        paths, python=[sys.executable, str(script), "--repo", str(repo_root())], cwd=tmp_path / "app",
        poll_s=0.05,
    )
    adapter = RealRunAdapter(
        paths, SupervisorLink("http://127.0.0.1:9", timeout_s=0.2), fake=tuple(sorted(FAKEABLE)),
        runner=runner,
    )
    dash.state.worker.handlers["run"] = build_handlers(cast(Any, SimpleNamespace(run=adapter)))["run"]
    dash.lines = lambda: ran.read_text(encoding="utf-8").split() if ran.exists() else []
    dash.pause_at = lambda plan: monkeypatch.setenv("P47S_PAUSE_AT", plan)
    return dash


def videos_of(lines: list[str]) -> list[str]:
    return list(dict.fromkeys(c.split(":", 1)[1] for c in lines))


def all_made_once(dash: SimpleNamespace, n: int) -> list[str]:
    """Every video ran each stage exactly once and has its last file; returns them in the order made."""
    calls = dash.lines()
    videos = videos_of(calls)
    assert len(videos) == n, f"{n} videos: {calls}"
    for v in videos:
        assert calls.count(f"a:{v}") == 1 and calls.count(f"b:{v}") == 1, f"none twice: {calls}"
        assert (dash.state.paths.job_dir(v) / "b.json").exists()
    return videos


def error_of(resp: Any) -> dict[str, Any]:
    return dict(resp.json()["error"])


def studio_folder(dash: SimpleNamespace, job_id: str) -> None:
    folder = dash.state.paths.job_dir(job_id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "job.json").write_text("{}", encoding="utf-8")


# ------------------------------------------- stopped, not yet seen by the dashboard: the resume arrives


@pytest.mark.parametrize(("stage", "count", "owed_text"), [
    ("a", 3, "(3 videos still to make, the first one half made)"),  # stopped inside video 1
    ("b", 2, "(2 videos still to make)"),  # stopped between video 1 and 2
])
def test_a_resume_before_the_dashboard_looked_puts_the_request_back_and_every_video_is_made(
    real: SimpleNamespace, stage: str, count: int, owed_text: str
):
    """The gap is held open: the worker's look (the handler's return) waits until `studio resume` has landed.
    The request must not fail with a Retry that covers one video (or none): it goes back in line with all it
    owes. A second pause that finds it there holds it, and the resume after that makes the rest."""
    state = real.state
    inner = state.worker.handlers["run"]
    resumed: list[str] = []

    def stopped_then_resumed(ctl: JobControl) -> dict[str, Any] | None:
        try:
            return inner(ctl)
        except NsError as e:
            if e.code == PAUSED_CODE and not resumed:  # typed in a terminal before the dashboard looked
                review.resume(state.paths, Db(state.paths.db_path), CH)
                resumed.append(ctl.job_id)
            raise

    state.worker.handlers["run"] = stopped_then_resumed
    real.pause_at(f"{stage}:1")
    job_id = real.ask(3)
    assert real.run_next() is True
    back = real.job(job_id)
    assert resumed == [job_id], "the studio stopped it for the pause"
    assert back["status"] == "queued", f"not failed: {back['message']}"
    assert back["message"] == "Queued" and back["started_at"] is None
    (v1,) = videos_of(real.lines())
    assert back["params"]["count"] == count
    assert back["params"]["resume_job_id"] == (v1 if stage == "a" else None)
    assert back["params"]["made_job_ids"] == ([] if stage == "a" else [v1])
    real.pause()  # the pause arrives again while it waits in line
    assert real.run_next() is False
    assert real.job(job_id)["message"].endswith(owed_text)
    real.resume()
    assert real.run_next() is True
    done = real.job(job_id)
    assert done["status"] == "done", done["message"]
    assert done["result"]["studio_job_ids"] == all_made_once(real, 3), "the card lists every video"
    assert real.run_next() is False


def test_a_second_pause_in_the_second_video_still_makes_all_three_and_the_card_lists_them(
    real: SimpleNamespace,
):
    real.pause_at("a:1,a:2")  # inside video 1, then (after the resume) inside video 2
    job_id = real.ask(3)
    assert real.run_next() is True
    held = real.job(job_id)
    assert held["message"].endswith("(3 videos still to make, the first one half made)"), held["message"]
    real.resume()
    assert real.run_next() is True
    v1, v2 = videos_of(real.lines())
    held = real.job(job_id)
    assert held["status"] == "queued" and held["message"].startswith("Paused: ")
    assert held["message"].endswith("(2 videos still to make, the first one half made)")
    assert held["params"]["resume_job_id"] == v2 and held["params"]["count"] == 2
    assert held["params"].get("made_job_ids") == [v1] and held["params"].get("asked") == 3
    assert held["result"]["studio_job_ids"] == [v1], "the waiting card already lists what was made"
    assert real.run_next() is False
    real.resume()
    assert real.run_next() is True
    done = real.job(job_id)
    assert done["status"] == "done", done["message"]
    assert done["result"]["studio_job_ids"] == all_made_once(real, 3)


def test_a_request_stopped_before_it_made_anything_is_held_whole_and_retry_is_refused_while_it_waits(
    dash: SimpleNamespace,
):
    """A stop that names nothing (no studio job yet, no account): the request owes what it was asked."""
    state = dash.state
    calls: list[dict[str, Any]] = []

    def run(ctl: JobControl) -> dict[str, Any]:
        calls.append(dict(ctl.params))
        if len(calls) == 1:
            review.pause(state.paths, Db(state.paths.db_path), CH, "warning")
            raise NsError(PAUSED_CODE, "stopped")
        return {"output_ids": []}

    state.worker.handlers["run"] = run
    job_id = dash.ask(3)
    assert dash.run_next() is True
    held = dash.job(job_id)
    assert held["status"] == "queued", held["message"]
    assert held["message"].startswith("Paused: ") and held["message"].endswith("(3 videos still to make)")
    r = dash.client.post(f"/api/jobs/{job_id}/resume")  # Retry on a job that waits
    assert r.status_code == 409 and error_of(r)["code"] == "not_failed"
    assert dash.run_next() is False and len(calls) == 1
    dash.resume()
    assert dash.job(job_id)["message"] == "Queued"
    assert dash.run_next() is True and dash.job(job_id)["status"] == "done"
    assert calls[1]["count"] == 3 and not calls[1].get("resume_job_id")


def test_a_series_part_resumed_before_the_dashboard_looked_keeps_its_job_and_continues(dash: SimpleNamespace):
    from ns_dashboard.fakes import FakeSeriesAdapter

    state = dash.state
    series = state.adapters.series
    assert isinstance(series, FakeSeriesAdapter)
    series.stories = [{
        "slug": "the-lift", "channel": CH, "status": "producing", "title": "The Lift", "next_step": "produce",
        "has_design": True, "chosen": "A", "drafts": [],
        "parts": [{"n": 1, "title": "Floor 13", "minutes": 15, "status": "approved", "job_id": None,
                   "words": 2250, "shorts": 3}],
    }]
    calls: list[str] = []

    def part(ctl: JobControl) -> dict[str, Any]:  # the studio saw a pause the owner has lifted since
        calls.append(ctl.job_id)
        if len(calls) == 1:
            raise NsError(PAUSED_CODE, f"job {ctl.job_id} stopped before its stage voice")
        return {"output_ids": []}

    state.worker.handlers["series_part"] = part
    (queued,) = dash.client.post("/api/library/series/the-lift/produce").json()["queued"]
    job_id = str(queued["job_id"])
    assert dash.run_next() is True
    back = dash.job(job_id)
    assert back["status"] == "queued" and back["message"] == str(queued["title"]), back["message"]
    assert series.stories[0]["parts"][0]["status"] == "producing", "the part stays with its job"
    assert dash.run_next() is True
    assert calls == [job_id, job_id] and dash.job(job_id)["status"] == "done"


# ------------------------------------------- it cannot go round for ever; failed and offered for Retry


def test_a_pause_only_the_studio_sees_fails_after_three_turns_and_retry_makes_the_owed_videos_once(
    dash: SimpleNamespace,
):
    """The studio reports a pause between video 1 and 2 again and again while the dashboard sees none (they
    disagree; no person does that). Three turns back in line, then NS-E330, once. Retry covers the two videos
    still owed (not "continue a finished job"), and a second Retry cannot make them again."""
    state = dash.state
    calls: list[dict[str, Any]] = []
    ok = False

    def run(ctl: JobControl) -> dict[str, Any]:
        calls.append(dict(ctl.params))
        if ok:
            ctl.extras["studio_job_ids"] = [V2, V3]
            return {"output_ids": [], **ctl.extras}
        if len(calls) == 1:
            ctl.note(studio_job_ids=[V1])  # video 1 was made in the first pass
        raise NsError(PAUSED_CODE, "After Dark was paused after 1 of 3 videos", left=2)

    state.worker.handlers["run"] = run
    job_id = dash.ask(3)
    for turn in range(1, PAUSE_REQUEUE_LIMIT + 1):
        assert dash.run_next() is True
        back = dash.job(job_id)
        assert back["status"] == "queued" and back["message"] == "Queued", back["message"]
        assert back["params"]["pause_requeues"] == turn and back["params"]["count"] == 2
    assert dash.run_next() is True
    failed = dash.job(job_id)
    assert failed["status"] == "failed" and failed["result"]["error"]["code"] == PAUSED_CODE
    assert failed["result"]["error"]["hint"] == describe(PAUSED_CODE).hint
    assert failed["result"]["studio_job_ids"] == [V1] and failed["result"]["stop"] == {"left": 2}
    assert dash.run_next() is False and len(calls) == PAUSE_REQUEUE_LIMIT + 1, "no loop"
    r = dash.client.post(f"/api/jobs/{job_id}/resume")  # Retry
    assert r.status_code == 201, r.text
    retry = r.json()
    assert retry["params"] == {"channel": CH, "format": "shorts_only", "count": 2, "resume_of": job_id}
    again = dash.client.post(f"/api/jobs/{job_id}/resume")  # pressed twice
    assert again.status_code == 409 and error_of(again)["code"] == "already_queued"
    ok = True
    assert dash.run_next() is True and dash.job(retry["job_id"])["status"] == "done"
    assert calls[-1]["count"] == 2 and not calls[-1].get("resume_job_id")
    late = dash.client.post(f"/api/jobs/{job_id}/resume")  # and once more after it finished
    assert late.status_code == 409 and error_of(late)["code"] == "already_continued"
    assert dash.run_next() is False


def test_a_dashboard_restart_keeps_what_a_stopped_request_owes(dash: SimpleNamespace):
    """(1) While the request waits, held: its row carries what it owes, and a new worker continues it with
    exactly that; its finished card lists the video made before the pause too. (2) After the studio stopped
    it and before the dashboard looked (the row still says running): the start closes it as interrupted
    (NS-E328) and Retry covers the video under way and the ones not started, not one video."""
    state = dash.state
    calls: list[dict[str, Any]] = []

    def run(ctl: JobControl) -> dict[str, Any]:
        calls.append(dict(ctl.params))
        if len(calls) == 1:
            ctl.note(studio_job_ids=[V1, V2])
            state.db.execute(
                "INSERT INTO outputs(output_id, job_id, channel, kind, k, title, path, duration_s, status,"
                " created_at) VALUES (?, ?, ?, 'short', 1, 't', 'x.mp4', 1.0, 'pending', ?)",
                (f"{V1}/s1", V1, CH, datetime.now(UTC).isoformat()),
            )
            review.pause(state.paths, Db(state.paths.db_path), CH, "warning")
            raise NsError(PAUSED_CODE, "stopped before its stage voice", left=0, half_made=V2)
        ctl.extras["studio_job_ids"] = [V2]
        return {"output_ids": [f"{V2}/s1"], **ctl.extras}

    state.worker.handlers["run"] = run
    job_id = dash.ask(2)
    assert dash.run_next() is True
    held = dash.job(job_id)
    assert held["status"] == "queued" and held["message"].endswith(
        "(its video is half made and continues from where it stopped)"), held["message"]
    assert (held["result"] or {}).get("output_ids") == [f"{V1}/s1"], "the waiting card lists the video made"
    assert state.store.recover_interrupted() == 0, "a waiting job is not touched by a start"
    assert getattr(dashboard_jobs, "GONE", None) == state.store.requeue(job_id, "put back twice")
    assert dash.job(job_id)["message"] == held["message"], "the second put-back writes nothing"
    fresh = JobWorker(state.store, state.worker.handlers, state.hub, state.log, hold=state.worker.hold)
    assert fresh.run_next() is False, "still paused after the restart"
    dash.resume()
    assert fresh.run_next() is True
    assert calls[1]["resume_job_id"] == V2 and calls[1]["count"] == 1
    done = dash.job(job_id)
    assert done["status"] == "done" and done["result"]["output_ids"] == [f"{V1}/s1", f"{V2}/s1"]
    assert done["result"]["studio_job_ids"] == [V1, V2]
    # (2) three asked, video 1 announced, then the dashboard died before it looked at the stop
    lost = dash.ask(3)
    assert state.store.mark_running(lost)
    state.store.note_result(lost, {"studio_job_ids": [V3]})
    studio_folder(dash, V3)
    assert state.store.recover_interrupted() == 1
    r = dash.client.post(f"/api/jobs/{lost}/resume")
    assert r.status_code == 201, r.text
    assert r.json()["params"] == {
        "channel": CH, "format": "shorts_only", "count": 3, "resume_job_id": V3, "resume_of": lost,
    }


# ------------------------------------------- Cancel


def test_cancel_of_a_held_request_says_what_was_made_and_accounts_for_the_half_made_video(
    real: SimpleNamespace,
):
    real.pause_at("a:2")  # video 1 is made, the pause lands inside video 2
    job_id = real.ask(3)
    assert real.run_next() is True
    v1, v2 = videos_of(real.lines())
    assert real.job(job_id)["message"].endswith("(2 videos still to make, the first one half made)")
    r = real.client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200, r.text
    gone = real.job(job_id)
    assert gone["status"] == "cancelled"
    assert gone["message"] == (
        f"Cancelled while it waited: 1 of 3 videos were made; the half-made one ({v2}) is not finished and "
        "its files stay in its job folder; no further video of this request is made"
    )
    assert gone["result"]["half_made"] == v2 and gone["result"]["studio_job_ids"] == [v1, v2]
    folder = real.state.paths.job_dir(v2)
    assert (folder / "a.json").exists() and not (folder / "b.json").exists(), "left as it was, not removed"
    before = real.lines()
    real.resume()
    assert real.run_next() is False and real.lines() == before, "nothing of a cancelled request runs"


@pytest.mark.parametrize("order", ["cancel_first", "put_back_first"])
def test_cancel_wins_when_it_meets_the_pause_that_puts_the_job_back(dash: SimpleNamespace, order: str):
    """Cancel is pressed on the running job while the studio stops it for a pause. Either Cancel's flag is
    set before the worker puts the job back (the worker must not put it back), or Cancel had read "running"
    and sets its flag only after (the flag must not sit on a waiting job, which would then show "Paused" and
    be cancelled only after the resume). The second order is forced by blocking the flag until the row is
    back in line."""
    state = dash.state
    store = state.store
    answers: list[Any] = []
    calls: list[str] = []
    about_to_flag, put_back = threading.Event(), threading.Event()
    real_flag, real_requeue = store.request_cancel, store.requeue

    def slow_flag(job_id: str) -> None:
        about_to_flag.set()
        put_back.wait(10)
        real_flag(job_id)

    def requeue(*a: Any, **kw: Any) -> str:
        out = real_requeue(*a, **kw)
        put_back.set()
        return out

    def press_cancel(job_id: str) -> None:
        answers.append(dash.client.post(f"/api/jobs/{job_id}/cancel").status_code)

    def run(ctl: JobControl) -> dict[str, Any]:
        calls.append(ctl.job_id)
        ctl.note(studio_job_ids=[V1])
        review.pause(state.paths, Db(state.paths.db_path), CH, "warning")
        if order == "cancel_first":
            press_cancel(ctl.job_id)
        else:
            presser.start()
            assert about_to_flag.wait(10), "Cancel never got to its flag"
        raise NsError(PAUSED_CODE, "stopped before its stage voice", left=2, half_made=V1)

    if order == "put_back_first":
        store.request_cancel = slow_flag  # type: ignore[method-assign]
        store.requeue = requeue  # type: ignore[method-assign]
    state.worker.handlers["run"] = run
    job_id = dash.ask(3)
    presser = threading.Thread(target=press_cancel, args=(job_id,))
    assert dash.run_next() is True
    if order == "put_back_first":
        presser.join(20)
    assert answers == [200]
    gone = dash.job(job_id)
    assert gone["status"] == "cancelled", gone["message"]
    assert gone["message"].startswith("Cancelled"), gone["message"]
    assert not store.is_cancel_requested(job_id), "no flag left behind"
    dash.resume()
    assert dash.run_next() is False and calls == [job_id], "a cancelled request never continues"


# ------------------------------------------- failed for another reason, or killed: Retry covers the request


def test_retry_of_a_request_whose_studio_process_died_covers_the_videos_it_had_not_started(
    dash: SimpleNamespace,
):
    """The studio child was killed inside video 2 of 3 (NS-E327, no account of the stop): Retry continues
    video 2 and makes video 3; pressed again after that, it is refused (it would make video 3 twice)."""
    state = dash.state
    calls: list[dict[str, Any]] = []

    def run(ctl: JobControl) -> dict[str, Any]:
        calls.append(dict(ctl.params))
        if len(calls) == 1:
            ctl.note(studio_job_ids=[V1, V2])
            raise NsError("NS-E327", "The studio process stopped (exit 1).")
        ctl.extras["studio_job_ids"] = [V2, V3]
        return {"output_ids": [], **ctl.extras}

    state.worker.handlers["run"] = run
    job_id = dash.ask(3)
    studio_folder(dash, V2)
    assert dash.run_next() is True and dash.job(job_id)["status"] == "failed"
    r = dash.client.post(f"/api/jobs/{job_id}/resume")
    assert r.status_code == 201, r.text
    assert r.json()["params"]["count"] == 2 and r.json()["params"]["resume_job_id"] == V2
    assert dash.run_next() is True and dash.job(r.json()["job_id"])["status"] == "done"
    again = dash.client.post(f"/api/jobs/{job_id}/resume")
    assert again.status_code == 409 and error_of(again)["code"] == "already_continued"


def test_cancel_of_a_series_part_a_pause_stopped_says_it_had_started_and_gives_the_part_back(
    dash: SimpleNamespace,
):
    from ns_dashboard.fakes import FakeSeriesAdapter

    state = dash.state
    series = state.adapters.series
    assert isinstance(series, FakeSeriesAdapter)
    series.stories = [{
        "slug": "the-lift", "channel": CH, "status": "producing", "title": "The Lift", "next_step": "produce",
        "has_design": True, "chosen": "A", "drafts": [],
        "parts": [{"n": 1, "title": "Floor 13", "minutes": 15, "status": "approved", "job_id": None,
                   "words": 2250, "shorts": 3}],
    }]

    def part(ctl: JobControl) -> dict[str, Any]:
        review.pause(state.paths, Db(state.paths.db_path), CH, "warning")
        raise NsError(PAUSED_CODE, f"job {ctl.job_id} stopped before its stage voice")

    state.worker.handlers["series_part"] = part
    (queued,) = dash.client.post("/api/library/series/the-lift/produce").json()["queued"]
    job_id = str(queued["job_id"])
    assert dash.run_next() is True and dash.job(job_id)["message"].startswith("Paused: ")
    assert dash.client.post(f"/api/jobs/{job_id}/cancel").status_code == 200
    gone = dash.job(job_id)
    assert gone["status"] == "cancelled" and gone["message"].startswith(
        "Cancelled while it waited: 0 of 1 videos were made; the half-made one ("), gone["message"]
    assert gone["result"]["half_made"] == gone["params"]["job_id"]
    assert series.stories[0]["parts"][0]["status"] == "approved", "the part can be produced again"
