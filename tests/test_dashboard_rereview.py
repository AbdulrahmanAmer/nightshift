# ruff: noqa: E501
"""Dashboard re-review fixes (`D:\NightShift-refs\rereview-dashboard.md`), one section per row."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from ns_common.paths import Paths
from ns_dashboard import studio_job
from ns_dashboard.adapters import RealRunAdapter, studio_owners
from ns_dashboard.events import EventHub
from ns_dashboard.jobs import JobCancelled, JobControl, JobStore
from ns_dashboard.state import open_db
from ns_dashboard.studio_runner import CANCEL_ENV, StudioRunner
from ns_dashboard.supervisor_link import SupervisorLink

# A stand-in for the studio child: announces a job, then either stops politely at the "next stage boundary"
# (when the cancel file appears) or ignores everything like a long stage does.
CHILD = r'''
import json, os, sys, time

def emit(ev, **data):
    print("NSJOB " + json.dumps({"ev": ev, **data}), flush=True)

mode = os.environ.get("CHILD_MODE", "polite")
emit("job", job_id="after-dark-20261102-aaaa")
emit("stage", stage="voice", index=0, total=2, job_index=0, job_total=1)
with open(os.environ["CHILD_UP"], "w", encoding="utf-8") as fh:
    fh.write("up")
flag = os.environ["NS_JOB_CANCEL_FILE"]
end = time.time() + 120
while time.time() < end:
    if mode == "polite" and os.path.exists(flag):
        with open(os.environ["CHILD_MARK"], "w", encoding="utf-8") as fh:
            fh.write("stopped itself")
        emit("cancelled")
        sys.exit(0)
    time.sleep(0.05)
'''


@pytest.fixture
def child(tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    script = tmp_path / "child.py"
    script.write_text(CHILD, encoding="utf-8")
    monkeypatch.setenv("CHILD_UP", str(tmp_path / "up"))
    monkeypatch.setenv("CHILD_MARK", str(tmp_path / "mark"))
    return script


def runner_for(script: Path, tmp_path: Path, **kw: Any) -> StudioRunner:
    app = tmp_path / "app"
    app.mkdir(exist_ok=True)
    kw.setdefault("grace_s", 0.5)
    return StudioRunner(Paths.from_env(), python=[sys.executable, str(script)], cwd=app, poll_s=0.05, **kw)


def cancel_when_up(tmp_path: Path, store: JobStore, job_id: str) -> None:
    def go() -> None:
        end = time.monotonic() + 20
        while not (tmp_path / "up").exists() and time.monotonic() < end:
            time.sleep(0.05)
        store.request_cancel(job_id)

    threading.Thread(target=go, daemon=True).start()


class RecordingLink(SupervisorLink):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:9", timeout_s=0.2)
        self.released: list[str] = []

    def release_owner(self, owner: str) -> bool:
        self.released.append(owner)
        return True


def make_ctl(paths: Paths, kind: str = "run") -> tuple[JobStore, JobControl]:
    store = JobStore(open_db(paths))
    return store, JobControl(store.create(kind, "after-dark", {}), store, EventHub())


# ---------------------------------------------------------------- row 1: cancel releases the GPU lease


def test_cancel_asks_the_child_to_stop_before_it_kills_it(child: Path, tmp_path: Path):
    paths = Paths.from_env()
    store, ctl = make_ctl(paths)
    cancel_when_up(tmp_path, store, ctl.job_id)
    link = RecordingLink()
    adapter = RealRunAdapter(paths, link, runner=runner_for(child, tmp_path, grace_s=15))
    started = time.monotonic()
    with pytest.raises(JobCancelled):
        adapter.run(ctl, "after-dark", "shorts_only", 1)
    assert (tmp_path / "mark").read_text(encoding="utf-8") == "stopped itself"  # its own finally blocks ran
    assert time.monotonic() - started < 14  # it did not sit out the grace period
    assert link.released == ["studio:after-dark-20261102-aaaa"]  # and the lease is released in any case


def test_a_child_stuck_in_a_stage_is_killed_after_the_grace_and_its_lease_is_released(
    child: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("CHILD_MODE", "stubborn")
    paths = Paths.from_env()
    store, ctl = make_ctl(paths)
    cancel_when_up(tmp_path, store, ctl.job_id)
    link = RecordingLink()
    killed: list[int] = []

    def kill(proc: Any) -> None:
        killed.append(proc.pid)
        proc.kill()
        proc.wait(timeout=15)

    adapter = RealRunAdapter(paths, link, runner=runner_for(child, tmp_path, grace_s=0.6, kill=kill))
    with pytest.raises(JobCancelled):
        adapter.run(ctl, "after-dark", "shorts_only", 1)
    assert len(killed) == 1 and not (tmp_path / "mark").exists()
    assert link.released == ["studio:after-dark-20261102-aaaa"]


def test_the_runner_hands_the_child_its_cancel_file_and_removes_it_afterwards(tmp_home: Path, tmp_path: Path):
    runner = StudioRunner(Paths.from_env(), python=[sys.executable], cwd=tmp_path)
    flag = tmp_path / "x.cancel"
    assert runner.environment(flag)[CANCEL_ENV] == str(flag)
    assert CANCEL_ENV not in runner.environment()


def test_the_child_stops_between_stages_when_the_flag_file_exists(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    flag = tmp_path / "go.cancel"
    monkeypatch.setenv(CANCEL_ENV, str(flag))
    studio_job.report("voice", 0, 3, 0, 1)  # no flag: the stage is announced
    assert '"stage": "voice"' in capsys.readouterr().out
    flag.write_text("cancel", encoding="utf-8")
    with pytest.raises(studio_job.CancelRequested):
        studio_job.report("captions", 1, 3, 0, 1)
    assert "captions" not in capsys.readouterr().out


def test_main_ends_with_a_cancelled_event_and_exit_0_when_asked_to_stop(tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    from ns_dashboard import studio_bridge

    flag = tmp_path / "go.cancel"
    flag.write_text("cancel", encoding="utf-8")
    monkeypatch.setenv(CANCEL_ENV, str(flag))

    def run(channel: str, fmt: str, count: Any, report: Any, **_kw: Any) -> list[str]:
        report("voice", 0, 3, 0, 1)
        return ["never"]

    monkeypatch.setattr(studio_bridge, "run_story_jobs", run)
    args = tmp_path / "a.json"
    args.write_text(json.dumps({"channel": "c", "fmt": "shorts_only"}), encoding="utf-8")
    assert studio_job.main(["run", "--args", str(args)]) == 0
    out = capsys.readouterr().out
    assert '"ev": "cancelled"' in out and '"ev": "result"' not in out


def test_the_child_acquires_through_the_studio_acquirer_so_the_lease_is_renewed(tmp_home: Path):
    from ns_studio.acquirer import StudioAcquirer

    assert studio_job._acquirer({}) is None
    acq = studio_job._acquirer({"supervisor_url": "http://127.0.0.1:9"})  # builds nothing that connects
    assert isinstance(acq, StudioAcquirer)


def test_studio_owners_cover_run_clip_rerun_and_series(tmp_home: Path):
    paths = Paths.from_env()
    _store, ctl = make_ctl(paths)
    ctl.extras["studio_job_ids"] = ["a-1", "a-2"]
    ctl.extras["studio_job_id"] = "c-1"
    owners = studio_owners(ctl, {"studio_job_id": "r-1", "params": {"job_id": "s-1"}})
    assert owners == ["studio:a-1", "studio:a-2", "studio:c-1", "studio:r-1", "studio:s-1"]
    assert studio_owners(JobControl(ctl._store.get(ctl.job_id), ctl._store, EventHub()), {}) == []  # type: ignore[arg-type]


def test_release_gpu_frees_only_the_current_holder(tmp_home: Path):
    from ns_dashboard.fakes import FakeSupervisor
    from tests.test_dashboard_api import make_env

    with FakeSupervisor() as sup:
        client, _state = make_env(tmp_home, SupervisorLink(sup.url, timeout_s=2))
        sup.state = {"gpu_owner": "studio:after-dark-20261102-aaaa", "services": {}}
        assert client.get("/api/status").json()["engine"]["vram_owner"] == "studio:after-dark-20261102-aaaa"
        stale = client.post("/api/engine/release_gpu", json={"owner": "studio:other"})
        assert stale.status_code == 409 and stale.json()["error"]["code"] == "not_holding"
        assert client.post("/api/engine/release_gpu", json={}).status_code == 422
        r = client.post("/api/engine/release_gpu", json={"owner": "studio:after-dark-20261102-aaaa"})
        assert r.json() == {"ok": True, "released": True, "owner": "studio:after-dark-20261102-aaaa"}
        assert ("POST", "/release_owner", {"owner": "studio:after-dark-20261102-aaaa"}) in sup.calls
        assert client.get("/api/status").json()["engine"]["vram_owner"] is None


# ---------------------------------------------------------------- rows 4, 7 (re-review 4, 9): jobs that end early


def series_worker(tmp_home: Path, **kw: Any):
    from ns_common.logging import NsLogger
    from ns_dashboard.fakes import FakeSeriesAdapter
    from ns_dashboard.jobs import JobRecord, JobWorker

    paths = Paths.from_env()
    store = JobStore(open_db(paths))
    series = FakeSeriesAdapter(store.db, paths)
    series.stories = [
        {"slug": "the-lift", "channel": "after-dark", "title": "The Lift",
         "parts": [{"n": 2, "title": "The Intercom", "status": "approved", "job_id": None}]}
    ]

    def release(rec: JobRecord) -> None:
        series.release({**rec.params, "job_id": rec.job_id})

    handlers = {"series_part": lambda ctl: {"output_ids": series.produce(ctl, ctl.params)}}
    worker = JobWorker(store, handlers, EventHub(), NsLogger("dashboard", paths.logs),
                       on_recover={"series_part": release}, on_end={"series_part": release},
                       sweep=series.sweep, **kw)
    return store, worker, series


def queue_part(store: JobStore, series: Any) -> str:
    (queued,) = series.queue_next("the-lift")
    store.create("series_part", "after-dark", {"story_slug": "the-lift", "part_n": 2, "job_id": queued["job_id"]})
    return str(queued["job_id"])


def part_status(series: Any) -> str:
    return str(series.stories[0]["parts"][0]["status"])


def test_cancelling_a_queued_series_part_makes_it_approved_again(tmp_home: Path):
    store, worker, series = series_worker(tmp_home)
    queue_part(store, series)
    assert part_status(series) == "producing"
    (rec,) = store.list_jobs(kind="series_part")
    assert worker.cancel(rec.job_id) == "cancelled"
    assert part_status(series) == "approved"


def test_a_gpu_wait_that_times_out_makes_the_part_approved_again(tmp_home: Path):
    from ns_common.errors import NsError

    def gate(_ctl: JobControl) -> None:
        raise NsError("NS-E201", "GPU still held by studio:x after 7200 s")

    store, worker, series = series_worker(tmp_home, gpu_gate=gate)
    queue_part(store, series)
    assert worker.run_next()
    (rec,) = store.list_jobs(kind="series_part")
    assert rec.status == "failed" and part_status(series) == "approved"


def test_the_start_sweep_frees_parts_that_no_job_stands_behind(tmp_home: Path):
    store, worker, series = series_worker(tmp_home)
    series.stories[0]["parts"].append({"n": 3, "title": "Floor 9", "status": "producing", "job_id": "gone-1"})
    series.stories[0]["parts"].append({"n": 4, "title": "Roof", "status": "producing", "job_id": "queued-1"})
    series.stories[0]["parts"][0]["status"] = "written"
    store.db.execute(
        "INSERT INTO dashboard_jobs(job_id, kind, channel, status, progress, message, created_at, params_json)"
        " VALUES ('queued-1', 'series_part', 'after-dark', 'queued', 0, 'Queued', 'now', '{}')"
    )
    worker.start()
    worker.stop()
    states = {p["n"]: p["status"] for p in series.stories[0]["parts"]}
    assert states == {2: "written", 3: "approved", 4: "producing"}  # the queued one keeps its place


def test_the_real_sweep_resets_a_producing_part_in_the_story_folder(tmp_home: Path):
    """Against the real workshop files, not a stand-in."""
    from ns_dashboard import studio_bridge
    from ns_dashboard.fakes import seed_channel
    from ns_studio.series.store import PartState, StoryDir

    paths = Paths.from_env()
    seed_channel(open_db(paths), paths, "after-dark")
    with studio_bridge.workshop_session(fake=studio_bridge.READ_ONLY) as ws:
        slug = ws.start("after-dark")
        sd = StoryDir.open(ws.paths, slug)
        status = sd.read_status()
        status.parts[1] = PartState(status="producing", job_id="dead-job")
        status.parts[2] = PartState(status="producing", job_id="live-job")
        sd.write_status(status)
    assert studio_bridge.sweep_parts({"live-job"}) == 1
    with studio_bridge.workshop_session(fake=studio_bridge.READ_ONLY) as ws:
        again = StoryDir.open(ws.paths, slug).read_status()
        assert again.parts[1].status == "approved" and again.parts[2].status == "producing"
    assert studio_bridge.sweep_parts({"live-job"}) == 0


def test_a_failed_job_keeps_its_stage_and_its_studio_job_ids(tmp_home: Path):
    from ns_common.errors import NsError
    from ns_common.logging import NsLogger
    from ns_dashboard.jobs import JobWorker

    paths = Paths.from_env()
    store = JobStore(open_db(paths))

    def handler(ctl: JobControl) -> dict[str, Any]:
        ctl.extras["studio_job_ids"] = ["after-dark-20261102-aaaa"]
        ctl.progress(0.5, "captions (3/6)")
        raise NsError("NS-E202", "whisper failed to load")

    worker = JobWorker(store, {"run": handler}, EventHub(), NsLogger("dashboard", paths.logs))
    job = store.create("run", "after-dark", {})
    assert worker.run_next()
    d = store.get(job.job_id).to_dict()  # type: ignore[union-attr]
    assert d["status"] == "failed" and d["stage"] == "captions"  # not "working"
    assert d["result"]["error"]["stage"] == "captions" and d["result"]["error"]["code"] == "NS-E202"
    assert d["result"]["studio_job_ids"] == ["after-dark-20261102-aaaa"]


def test_a_cancelled_job_keeps_its_studio_job_ids(tmp_home: Path):
    from ns_common.logging import NsLogger
    from ns_dashboard.jobs import JobWorker

    paths = Paths.from_env()
    store = JobStore(open_db(paths))

    def handler(ctl: JobControl) -> dict[str, Any]:
        ctl.extras["studio_job_ids"] = ["after-dark-20261102-bbbb"]
        raise JobCancelled(ctl.job_id)

    worker = JobWorker(store, {"run": handler}, EventHub(), NsLogger("dashboard", paths.logs))
    job = store.create("run", "after-dark", {})
    worker.run_next()
    rec = store.get(job.job_id)
    assert rec is not None and rec.status == "cancelled" and rec.result == {"studio_job_ids": ["after-dark-20261102-bbbb"]}


# ---------------------------------------------------------------- row 3: a saved request is answered once


class Provider:
    """Base of the test providers: the loop hands the model back after each turn."""

    def release(self) -> None:
        return None


def assistant_env(tmp_home: Path):
    from ns_dashboard.assistant.llm import ScriptedToolLlm, say
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home, llm=ScriptedToolLlm([say("Here is the answer.")] * 5))
    return client, state, client.app.state.dashboard  # type: ignore[attr-defined]


def test_two_callers_answer_a_saved_request_only_once(tmp_home: Path):
    from ns_dashboard.assistant.llm import ScriptedToolLlm, say

    _client, _state, dash = assistant_env(tmp_home)
    store = dash.assistant_store
    cid = store.new_conversation("clips")
    store.queue_message(cid, "make 2 clips from my stream", {})
    answers: list[str] = []
    gate = threading.Event()
    entered = threading.Event()

    class Slow(Provider):
        def acquire(self) -> Any:
            entered.set()
            gate.wait(10)  # the first answer takes its time; the second caller arrives meanwhile
            return ScriptedToolLlm([say("Done once.")])

    dash.assistant.provider = Slow()
    first: list[Any] = []
    t = threading.Thread(target=lambda: first.append(dash.assistant.run_queued()), daemon=True)
    t.start()
    assert entered.wait(10)
    assert dash.assistant.run_queued() == []  # the second caller finds it claimed and does nothing
    gate.set()
    t.join(10)
    assert len(first[0]) == 1 and store.queued() == []
    texts = [m["content"] for m in store.messages(cid) if m["role"] == "assistant"]
    answers.extend(texts)
    assert answers.count("Done once.") == 1


def test_a_request_is_given_back_when_the_engine_is_busy_or_the_answer_fails(tmp_home: Path):
    from ns_dashboard.assistant.llm import EngineBusy

    _client, _state, dash = assistant_env(tmp_home)
    store = dash.assistant_store
    cid = store.new_conversation("x")
    qid = store.queue_message(cid, "hello", {})

    class Busy(Provider):
        def acquire(self) -> Any:
            raise EngineBusy("studio:j1")

    dash.assistant.provider = Busy()
    assert dash.assistant.run_queued() == []
    assert [q["queue_id"] for q in store.queued()] == [qid]  # still saved
    assert store.claim_queued(qid, "someone") is True  # and free to be claimed again
    store.release_queued(qid, "someone")

    class Boom(Provider):
        def acquire(self) -> Any:
            raise RuntimeError("model fell over")

    dash.assistant.provider = Boom()
    with pytest.raises(RuntimeError):
        dash.assistant.run_queued()
    assert store.claim_queued(qid, "someone-else") is True


def test_claims_are_one_statement_and_a_dead_claim_can_be_taken_over(tmp_home: Path):
    from ns_dashboard.assistant.store import AssistantStore

    store = AssistantStore(Paths.from_env())
    cid = store.new_conversation("x")
    qid = store.queue_message(cid, "hello", {})
    assert store.claim_queued(qid, "a") is True
    assert store.claim_queued(qid, "b") is False
    store.release_queued(qid, "b")  # not the claimer: changes nothing
    assert store.claim_queued(qid, "c") is False
    assert store.claim_queued(qid, "c", stale_s=0.0) is True  # a claim older than the limit is taken over
    store.mark_queued_done(qid)
    assert store.claim_queued(qid, "d") is False and store.queued() == []


def test_an_old_assistant_database_gets_the_claim_columns(tmp_home: Path):
    import sqlite3

    from ns_dashboard.assistant.store import AssistantStore

    paths = Paths.from_env()
    paths.assistant_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(paths.assistant_db)
    conn.execute(
        "CREATE TABLE queued_messages (queue_id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id TEXT NOT NULL,"
        " text TEXT NOT NULL, context_json TEXT, status TEXT NOT NULL DEFAULT 'queued', created_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()
    store = AssistantStore(paths)
    cols = {r["name"] for r in store.db.query("PRAGMA table_info(queued_messages)")}
    assert {"claimed_at", "claimed_by"} <= cols
