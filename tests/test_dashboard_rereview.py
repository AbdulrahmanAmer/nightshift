# ruff: noqa: E501
"""Dashboard re-review fixes (rereview-dashboard.md in NightShift-refs), one section per row."""

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
        assert r.json() == {
            "ok": True, "released": True, "owner": "studio:after-dark-20261102-aaaa",
            "stopped_job": None, "foreign": False,
        }
        owner = "studio:after-dark-20261102-aaaa"
        assert ("POST", "/release_owner", {"owner": owner, "unless_alive": True}) in sup.calls
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
    (queued,) = series.queue_next("the-lift")  # the fake inserts the `series_part` row itself, like the workshop
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


# ---------------------------------------------------------------- rows 5, 6, 13, 14 (re-review): the pages


SRC = Path(__file__).resolve().parents[1] / "ns_dashboard" / "ui" / "src"


def page(name: str) -> str:
    return (SRC / "pages" / name).read_text(encoding="utf-8")


def test_create_has_an_automatic_caption_look_that_sends_nothing():
    src = page("Create.tsx")
    assert 'useState("punch_yellow")' not in src
    assert 'const [caption, setCaption] = useState("")' in src
    assert src.count("caption_style: caption || null") == 2  # upload and link
    assert ">Automatic</ChoiceChip>" in src


@pytest.mark.parametrize("given", [None, ""])
def test_a_clip_job_without_a_caption_style_stores_none(tmp_home: Path, given: Any):
    from ns_dashboard.fakes import seed_channel
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths)
    video = tmp_home / "talk.mp4"
    video.write_bytes(b"x" * 64)
    r = client.post("/api/jobs/clip", json={"channel": "after-dark", "path": str(video), "rights_confirmed": True,
                                             "caption_style": given})
    assert r.status_code == 201 and r.json()["params"]["caption_style"] is None


def test_the_thumbnail_picker_reads_the_digit_of_the_file_name():
    src = page("Queue.tsx")
    assert "/\d/.exec(t)" in src and "/d/.exec" not in src


def test_no_page_has_a_regex_that_matches_a_bare_letter_where_a_class_was_meant():
    """`/d/` for `/\d/` and `s` for `\s` after a lookbehind went unnoticed twice (Queue thumbnails, Assistant labels)."""
    import re

    bad = re.compile(r"\]\)[dsw]/|\.(?:exec|test|match|split)\(?/[dsw]/|/[dsw]/[gimsuy]*\.(?:exec|test)\(")
    hits = []
    for f in sorted(SRC.rglob("*.ts*")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if bad.search(line):
                hits.append(f"{f.name}:{n}")
    assert hits == []


def test_the_assistant_words_a_tool_by_its_first_sentence_only():
    assert r"t.split(/(?<=[.!?])\s/)" in page("Assistant.tsx")


# ---------------------------------------------------------------- row 8: voice errors keep their code and the right model


def voice_maker_for(status: int, body: dict[str, Any]):
    import httpx

    from ns_common.acquire import Endpoint
    from ns_dashboard.adapters import RealVoiceMaker

    class Acq:
        def acquire(self, service: str, owner: str, **_kw: Any) -> Endpoint:
            return Endpoint("http://tts.test")

        def release(self, service: str, owner: str) -> None:
            return None

    def handler(_req: Any) -> httpx.Response:
        return httpx.Response(status, json=body)

    return RealVoiceMaker(
        Paths.from_env(), SupervisorLink("http://127.0.0.1:9", timeout_s=0.2), acquirer=Acq(),  # type: ignore[arg-type]
        http=httpx.Client(transport=httpx.MockTransport(handler)), transcribe=lambda _p: "these are the words",
    )


@pytest.mark.parametrize("code", ["NS-E311", "NS-E315", "NS-E312", "NS-E310"])
def test_the_service_code_reaches_the_page(tmp_home: Path, code: str):
    from ns_common.errors import NsError

    maker = voice_maker_for(500, {"error": code, "detail": "the codec said no"})
    with pytest.raises(NsError) as err:
        maker.clone("Mine", tmp_home / "me.wav", "Hello there.")
    assert err.value.code == code and "the codec said no" in err.value.detail


def test_an_unknown_error_code_is_an_engine_failure(tmp_home: Path):
    from ns_common.errors import NsError

    maker = voice_maker_for(500, {"error": "NS-E999", "detail": "?"})
    with pytest.raises(NsError) as err:
        maker.design("Deep", "a voice", "Hi.")
    assert err.value.code == "NS-E310"


def test_a_missing_base_model_does_not_offer_the_voice_design_download(tmp_home: Path):
    from ns_dashboard.errors import ApiError

    clone = voice_maker_for(409, {"missing_model": "qwen-talker-1.7b-base-Q8_0.gguf"})
    with pytest.raises(ApiError) as e1:
        clone.clone("Mine", tmp_home / "me.wav", "Hello there.")
    assert e1.value.status == 409 and e1.value.code == "base_model_missing" and "qwen-talker-1.7b-base" in e1.value.message
    design = voice_maker_for(409, {"missing_model": "qwen-talker-1.7b-base-Q8_0.gguf", "error": "NS-E314"})
    with pytest.raises(ApiError) as e2:  # the design route also needs the Base talker: that is the file to fix
        design.design("Deep", "a voice", "Hi.")
    assert e2.value.code == "base_model_missing"
    only_design = voice_maker_for(409, {"missing_model": "qwen-talker-1.7b-voicedesign-Q8_0.gguf", "error": "NS-E314"})
    with pytest.raises(ApiError) as e3:
        only_design.design("Deep", "a voice", "Hi.")
    assert e3.value.code == "model_missing"


def test_the_library_titles_the_new_voice_states():
    src = page("Library.tsx")
    for code in ("NS-E311", "NS-E315", "base_model_missing"):
        assert f'"{code}":' in src


# ---------------------------------------------------------------- row 9: a stale "running" update state


def write_update_state(paths: Paths, **fields: Any) -> None:
    f = paths.home / "run" / "update_state.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"state": "running", "old": "0.1.0", "new": None, "message": "Updating.", **fields}), encoding="utf-8")


def test_a_running_update_whose_process_is_gone_is_shown_as_failed(tmp_home: Path):
    from datetime import datetime

    from ns_dashboard import updater

    paths = Paths.from_env()
    write_update_state(paths, pid=4242, updated_at=datetime.now().isoformat())
    now = updater.read_update_state(paths, alive=lambda _pid: False)
    assert now["state"] == "failed" and now["stale"] is True and "stopped" in now["message"]
    alive = updater.read_update_state(paths, alive=lambda _pid: True)
    assert alive["state"] == "running" and "stale" not in alive and alive["message"] == "Updating."


def test_without_a_pid_a_state_the_launcher_wrote_is_believed_only_for_a_short_while(tmp_home: Path):
    from datetime import datetime, timedelta

    from ns_dashboard import updater

    paths = Paths.from_env()
    now = datetime(2026, 11, 2, 12, 0, 0)
    write_update_state(paths, updated_at=(now - timedelta(seconds=30)).isoformat())
    assert updater.read_update_state(paths, now=lambda: now)["state"] == "running"
    write_update_state(paths, updated_at=(now - timedelta(minutes=10)).isoformat())
    assert updater.read_update_state(paths, now=lambda: now)["state"] == "failed"
    write_update_state(paths, state="done", new="0.1.1", updated_at=(now - timedelta(days=3)).isoformat())
    assert updater.read_update_state(paths, alive=lambda _p: False, now=lambda: now)["state"] == "done"  # only `running` goes stale


def test_the_age_fallback_when_the_supervisor_package_is_missing(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    import sys
    from datetime import datetime, timedelta

    from ns_dashboard import updater

    monkeypatch.setitem(sys.modules, "ns_supervisor.update_job", None)  # `import` now raises ImportError
    paths = Paths.from_env()
    now = datetime(2026, 11, 2, 12, 0, 0)
    write_update_state(paths, updated_at=(now - timedelta(minutes=5)).isoformat())
    assert updater.read_update_state(paths, now=lambda: now)["state"] == "running"
    write_update_state(paths, updated_at=(now - timedelta(minutes=31)).isoformat())
    out = updater.read_update_state(paths, now=lambda: now)
    assert out["state"] == "failed" and out["stale"] is True


# ---------------------------------------------------------------- rows 2, 13, 15 (re-review): the page after an update, demo mode, schedule


def test_the_settings_page_follows_an_update_to_the_new_version_and_gives_up_after_ten_minutes():
    src = page("Settings.tsx")
    assert "const UPDATE_WAIT_MS = 10 * 60_000" in src and "Date.now() - began > UPDATE_WAIT_MS" in src
    assert 'api.get<{ version?: string }>("/status")' in src and "now.version !== u.new" in src  # the NEW version must answer
    assert "location.reload()" in src  # and the page loads its scripts
    assert "from the shortcut on your desktop" in src  # the plain message when it never answers
    assert "n > 1200" not in src  # the silent one-hour poll is gone


def test_demo_mode_starts_only_when_there_is_no_backend():
    src = (SRC / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "if (this.answered || !noBackend(e)) throw e;" in src
    assert "e instanceof TypeError" in src and "e.status === 404" in src  # nothing listens; the static demo server says 404
    assert 'throw new Error(String(res.status))' not in src  # a 500/503 while the service restarts is no reason for sample data


def test_a_time_windows_refused_is_not_saved_and_the_page_says_so(tmp_home: Path):
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)

    class Refuse:
        def apply(self, run_time: str) -> dict[str, Any]:
            return {"applied": False, "note": "Not saved: Windows refused to change the nightly task (access denied)"}

    before = client.get("/api/settings").json()["schedule_time"]
    state.adapters.schedule = Refuse()  # type: ignore[assignment]
    r = client.put("/api/settings", json={"schedule_time": "02:00", "notifications": False})
    body = r.json()
    assert r.status_code == 200 and body["schedule_result"]["applied"] is False
    assert body["schedule_time"] == before  # the field does not move
    assert not (state.paths.config / "dashboard.json").exists() or json.loads((state.paths.config / "dashboard.json").read_text(encoding="utf-8")).get("run_time", before) == before
    assert body["notifications"] is False  # the other fields were saved


def test_a_time_the_task_accepts_is_saved(tmp_home: Path):
    from tests.test_dashboard_api import make_env

    client, _state = make_env(tmp_home)
    body = client.put("/api/settings", json={"schedule_time": "02:00"}).json()
    assert body["schedule_result"]["applied"] is True and body["schedule_time"] == "02:00"


def test_the_refusal_note_says_not_saved():
    src = (Path(__file__).resolve().parents[1] / "ns_dashboard" / "adapters.py").read_text(encoding="utf-8")
    assert "Saved, but Windows did not update the nightly task" not in src and "Not saved: Windows refused" in src


# ---------------------------------------------------------------- row 11: never two workers


def worker_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == "ns-dashboard-worker" and t.is_alive()]


def test_stop_that_times_out_keeps_the_thread_and_start_resumes_it(tmp_home: Path):
    from ns_common.logging import NsLogger
    from ns_dashboard.jobs import JobWorker

    paths = Paths.from_env()
    store = JobStore(open_db(paths))
    worker = JobWorker(store, {"verify": lambda ctl: {}}, EventHub(), NsLogger("dashboard", paths.logs))
    gate, entered = threading.Event(), threading.Event()

    def slow_answer() -> None:  # the Assistant answering a saved request
        entered.set()
        gate.wait(20)

    worker.on_idle = slow_answer
    store.create("verify", None, {})
    worker.start()
    try:
        assert entered.wait(10)
        assert worker.stop(0.2) is False  # still inside the answer
        first = worker._thread
        worker.start()
        assert worker._thread is first and len(worker_threads()) == 1  # resumed, not doubled
        gate.set()
        job = store.create("verify", None, {})
        worker.enqueue(job.job_id)
        end = time.monotonic() + 10
        while time.monotonic() < end and (store.get(job.job_id) or job).status != "done":
            time.sleep(0.05)
        assert store.get(job.job_id).status == "done"  # type: ignore[union-attr]  # the resumed thread works
        assert worker.stop(5) is True and worker_threads() == []
        worker.start()  # a clean restart after a clean stop starts exactly one
        assert len(worker_threads()) == 1
    finally:
        gate.set()
        worker.stop(5)


def test_import_settings_refuses_while_an_answer_runs_and_never_adds_a_worker(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    import io
    import zipfile

    from ns_dashboard import svc_settings
    from ns_dashboard.errors import ApiError
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    dash = client.app.state.dashboard  # type: ignore[attr-defined]
    gate, entered = threading.Event(), threading.Event()
    state.worker.on_idle = lambda: (entered.set(), gate.wait(20))[0]
    state.store.create("verify", None, {})
    monkeypatch.setattr(svc_settings, "WORKER_STOP_WAIT_S", 0.2)
    restored: list[Any] = []
    state.adapters.backup.restore = lambda z: restored.append(z) or ["ok"]  # type: ignore[method-assign]
    state.worker.start()
    try:
        assert entered.wait(10)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("config/x.json", "{}")
        buf.seek(0)
        with pytest.raises(ApiError) as err:
            dash.settings.import_settings("nightshift-backup.zip", buf)
        assert err.value.status == 409 and err.value.code == "assistant_busy"
        assert restored == [] and len(worker_threads()) == 1
        gate.set()
        time.sleep(0.5)
        buf.seek(0)
        out = dash.settings.import_settings("nightshift-backup.zip", buf)  # now it goes through
        assert out["restored"] is True and len(restored) == 1 and len(worker_threads()) == 1
    finally:
        gate.set()
        state.worker.stop(5)


# ---------------------------------------------------------------- row 10: a clip source is stored once, failed downloads are cleaned


def finished_upload(paths: Paths, payload: bytes = b"v" * 5000) -> Path:
    from ns_dashboard.uploads import UploadManager

    mgr = UploadManager(paths)
    info = mgr.start("stream.mp4", len(payload))
    mgr.append(info.upload_id, 0, payload)
    done = mgr.complete(info.upload_id)
    assert done.path
    return Path(done.path)


def test_an_upload_is_moved_to_the_import_folder_not_copied(tmp_home: Path):
    import hashlib

    from ns_dashboard.uploads import adopt_upload, clipper_dir

    paths = Paths.from_env()
    upload = finished_upload(paths)
    moved = adopt_upload(paths, upload)
    digest = hashlib.sha256(b"v" * 5000).hexdigest()
    assert moved == clipper_dir(paths) / f"src-{digest[:10]}" / "source.mp4" and moved.read_bytes() == b"v" * 5000
    assert not upload.parent.exists()  # no second copy left behind
    assert [p.name for p in clipper_dir(paths).iterdir()] == [f"src-{digest[:10]}"]
    again = finished_upload(paths)  # the same video uploaded again
    assert adopt_upload(paths, again) == moved and not again.parent.exists()


def test_a_path_that_is_not_an_upload_is_left_alone(tmp_home: Path, tmp_path: Path):
    from ns_dashboard.uploads import adopt_upload

    mine = tmp_path / "my-stream.mp4"
    mine.write_bytes(b"x")
    assert adopt_upload(Paths.from_env(), mine) == mine and mine.exists()


FINISH = r'''
import json, os, sys
mode = os.environ.get("CHILD_MODE", "ok")
with open(os.environ["CHILD_ARGS_OUT"], "w", encoding="utf-8") as fh:
    json.dump(json.load(open(sys.argv[sys.argv.index("--args") + 1], encoding="utf-8")), fh)
if mode == "ok":
    print("NSJOB " + json.dumps({"ev": "job", "job_id": "clip-1"}), flush=True)
    print("NSJOB " + json.dumps({"ev": "result", "output_ids": ["clip-1/c1"], "job_ids": ["clip-1"]}), flush=True)
else:
    sys.exit(3)
'''


def test_the_clip_adapter_hands_the_moved_upload_to_the_studio(tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_dashboard.adapters import RealClipAdapter

    script = tmp_path / "finish.py"
    script.write_text(FINISH, encoding="utf-8")
    monkeypatch.setenv("CHILD_ARGS_OUT", str(tmp_path / "args.json"))
    paths = Paths.from_env()
    upload = finished_upload(paths)
    _store, ctl = make_ctl(paths, "clip")
    adapter = RealClipAdapter(paths, RecordingLink(), runner=runner_for(script, tmp_path))
    ids = adapter.clip(ctl, upload, "src-20261102-aaaa", {"template": "auto"})
    assert ids == ["clip-1/c1"]
    sent = json.loads((tmp_path / "args.json").read_text(encoding="utf-8"))["source_path"]
    assert "src-20261102" not in sent and Path(sent).is_file() and not upload.parent.exists()


def test_a_failed_link_download_job_leaves_nothing_behind(tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_dashboard import studio_bridge
    from ns_dashboard.adapters import RealClipAdapter

    script = tmp_path / "finish.py"
    script.write_text(FINISH, encoding="utf-8")
    monkeypatch.setenv("CHILD_ARGS_OUT", str(tmp_path / "args.json"))
    monkeypatch.setenv("CHILD_MODE", "crash")
    paths = Paths.from_env()
    folder = studio_bridge.download_dir(paths, "src-20261102-bbbb")
    folder.mkdir(parents=True)
    video = folder / "video.mp4"
    video.write_bytes(b"d" * 100)
    _store, ctl = make_ctl(paths, "clip")
    adapter = RealClipAdapter(paths, RecordingLink(), runner=runner_for(script, tmp_path))
    with pytest.raises(RuntimeError):
        adapter.clip(ctl, video, "src-20261102-bbbb", {"template": "auto"})
    assert not folder.exists()


# ---------------------------------------------------------------- keys reach Credential Manager AND the DPAPI file


def xor(data: bytes) -> bytes:
    return bytes(b ^ 0x5A for b in data)  # stands in for DPAPI on any OS


class MemoryKeyring:
    def __init__(self, broken: bool = False) -> None:
        self.values: dict[tuple[str, str], str] = {}
        self.broken = broken

    def set_password(self, service: str, name: str, value: str) -> None:
        if self.broken:
            raise OSError("no access to Credential Manager in this session")
        self.values[(service, name)] = value

    def get_password(self, service: str, name: str) -> str | None:
        if self.broken:
            raise OSError("no access")
        return self.values.get((service, name))


def test_a_key_saved_in_settings_is_written_to_both_stores(tmp_home: Path):
    from ns_common.secret_file import load_secret
    from ns_common.secrets_names import KEYRING_SERVICE
    from ns_dashboard.adapters import RealKeyringAdapter

    paths = Paths.from_env()
    keyring = MemoryKeyring()
    adapter = RealKeyringAdapter(paths, keyring_module=keyring, protect=xor, unprotect=xor)
    adapter.set("pexels_api_key", "KEY-NEW")
    assert keyring.values[(KEYRING_SERVICE, "pexels_api_key")] == "KEY-NEW"  # Credential Manager
    assert load_secret(paths, "pexels_api_key", unprotect=xor) == "KEY-NEW"  # and the file the nightly task reads
    adapter.set("pexels_api_key", "KEY-NEWER")  # a change reaches both, not just the first
    assert keyring.values[(KEYRING_SERVICE, "pexels_api_key")] == "KEY-NEWER"
    assert load_secret(paths, "pexels_api_key", unprotect=xor) == "KEY-NEWER"


def test_one_working_store_is_enough_and_both_failing_is_an_error(tmp_home: Path):
    from ns_common.secret_file import load_secret
    from ns_dashboard.adapters import RealKeyringAdapter
    from ns_dashboard.errors import AdapterUnavailable

    paths = Paths.from_env()
    broken = RealKeyringAdapter(paths, keyring_module=MemoryKeyring(broken=True), protect=xor, unprotect=xor)
    broken.set("pixabay_api_key", "KEY-2")  # the file took it
    assert load_secret(paths, "pixabay_api_key", unprotect=xor) == "KEY-2"
    assert broken.get("pixabay_api_key") == "KEY-2"  # read back through the file when the keyring cannot answer

    def refuse(_data: bytes) -> bytes:
        raise OSError("disk full")

    both = RealKeyringAdapter(paths, keyring_module=MemoryKeyring(broken=True), protect=refuse, unprotect=xor)
    with pytest.raises(AdapterUnavailable):
        both.set("pixabay_api_key", "KEY-3")


def test_the_settings_route_saves_through_the_adapter_and_never_returns_the_value(tmp_home: Path):
    from ns_dashboard.adapters import RealKeyringAdapter
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    keyring = MemoryKeyring()
    state.adapters.keyring = RealKeyringAdapter(Paths.from_env(), keyring_module=keyring, protect=xor, unprotect=xor)  # type: ignore[assignment]
    r = client.post("/api/settings/keys", json={"name": "pexels", "value": "KEY-ROUTE"})
    assert r.status_code == 200 and "KEY-ROUTE" not in r.text
    assert "KEY-ROUTE" in keyring.values.values()
    assert (Paths.from_env().config / "secrets.dpapi").is_file()


# ---------------------------------------------------------------- the voice setup asked for, made from Library > Voices


def queue_setup_voice(paths: Paths, **kw: Any) -> None:
    f = paths.config / "pending_voice.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"channel": "after-dark", "mode": "design", "name": "Hollow", "description": "a slow, low voice", **kw}), encoding="utf-8")


def test_the_pending_setup_voice_is_listed_and_created_by_one_button(tmp_home: Path):
    from tests.test_api_ui_contract import check_payload
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    assert client.get("/api/voices/pending").json() == {"pending": None}
    queue_setup_voice(state.paths)
    shown = client.get("/api/voices/pending").json()
    assert shown == {"pending": {"name": "Hollow", "mode": "design", "description": "a slow, low voice", "channel": "after-dark"}}
    assert check_payload("PendingVoice", shown, False, "GET /voices/pending") == []
    made = client.post("/api/voices/apply-pending")  # queues the apply_voice job (see tests/test_pending_voice_job.py)
    assert made.status_code == 200 and made.json()["created"] is None and made.json()["job_id"]
    assert check_payload("VoiceApplied", made.json(), False, "POST /voices/apply-pending") == []
    assert state.worker.run_next() is True
    job = state.store.get(made.json()["job_id"])
    assert job is not None and job.status == "done" and job.result == {"created": "Hollow", "name": "Hollow"}
    assert (state.paths.models_tts_voices / "Hollow" / "voice.json").is_file()
    assert not (state.paths.config / "pending_voice.json").exists()
    assert client.post("/api/voices/apply-pending").json() == {"created": None, "pending": None, "job_id": None}


def test_a_missing_design_model_ends_the_job_with_ns_e314_and_the_request_stays_queued(tmp_home: Path):
    from ns_common.errors import NsError
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    queue_setup_voice(state.paths)
    state.adapters.voice_maker.fail_with = NsError("NS-E314")  # type: ignore[attr-defined]
    job_id = client.post("/api/voices/apply-pending").json()["job_id"]
    state.worker.run_next()
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "failed" and job["result"]["error"]["code"] == "NS-E314"
    assert client.get("/api/voices/pending").json()["pending"]["name"] == "Hollow"
    src = page("Library.tsx")
    assert "Pending voice from setup" in src and "Create it now" in src and '"/voices/apply-pending"' in src
    assert 'e.code === "NS-E314"' in src  # the same failure card offers "Get voice design model"
    assert 'code === "NS-E314"' in src and "failOfJob" in src  # and so does the card of a failed job


def test_a_voice_that_exists_is_not_pending(tmp_home: Path):
    from ns_dashboard.adapters import read_pending_voice

    paths = Paths.from_env()
    queue_setup_voice(paths)
    assert read_pending_voice(paths) is not None
    (paths.models_tts_voices / "Hollow").mkdir(parents=True)
    (paths.models_tts_voices / "Hollow" / "voice.json").write_text("{}", encoding="utf-8")
    assert read_pending_voice(paths) is None
    (paths.config / "pending_voice.json").write_text("not json", encoding="utf-8")
    assert read_pending_voice(paths) is None


def test_the_studio_side_makes_the_voice_with_the_fake_engine(tmp_home: Path):
    from ns_dashboard import studio_bridge

    paths = Paths.from_env()
    paths.ensure()
    assert studio_bridge.apply_pending_voice(fake=("tts",), paths=paths) is None
    queue_setup_voice(paths)
    assert studio_bridge.apply_pending_voice(fake=("tts",), paths=paths) == "Hollow"
    assert (paths.models_tts_voices / "Hollow" / "voice.json").is_file() and not (paths.config / "pending_voice.json").exists()


def test_a_missing_voicedesign_model_from_the_service_becomes_ns_e314(monkeypatch: pytest.MonkeyPatch):
    import httpx

    from ns_common.errors import NsError
    from ns_dashboard import studio_bridge

    def answer(status: int, body: dict[str, Any]):
        return lambda *_a, **_kw: httpx.Response(status, json=body, request=httpx.Request("POST", "http://tts.test"))

    monkeypatch.setattr(httpx, "post", answer(409, {"missing_model": "qwen-talker-1.7b-voicedesign-Q8_0.gguf"}))
    with pytest.raises(NsError) as design:
        studio_bridge._voice_post("http://tts.test", "/v1/voices/design", {"name": "x"})
    assert design.value.code == "NS-E314"
    with pytest.raises(NsError) as clone:  # a missing Base model is not fixed by the design download
        studio_bridge._voice_post("http://tts.test", "/v1/voices/clone", {"name": "x"})
    assert clone.value.code == "NS-E310"


def test_the_real_adapter_runs_apply_voice_in_the_child(tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_dashboard.adapters import RealVoiceMaker

    script = tmp_path / "voice.py"
    script.write_text(
        'import json, os, sys\n'
        'json.dump({"argv": sys.argv[1:], "args": json.load(open(sys.argv[sys.argv.index("--args") + 1]))}, open(os.environ["CHILD_ARGS_OUT"], "w"))\n'
        'print("NSJOB " + json.dumps({"ev": "result", "name": "Hollow"}), flush=True)\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CHILD_ARGS_OUT", str(tmp_path / "out.json"))
    paths = Paths.from_env()
    maker = RealVoiceMaker(paths, SupervisorLink("http://127.0.0.1:8099", timeout_s=0.2), runner=runner_for(script, tmp_path))
    assert maker.apply_pending() == "Hollow"
    seen = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert seen["argv"][2] == "apply_voice" and seen["args"] == {"supervisor_url": "http://127.0.0.1:8099"}
    assert maker.pending() is None
    queue_setup_voice(paths, mode="clone")
    assert maker.pending() == {"name": "Hollow", "mode": "clone", "description": "Cloned from your recording", "channel": "after-dark"}


# ---------------------------------------------------------------- no nightly task (NS-W206)


def test_status_and_settings_say_when_windows_has_no_nightly_task(tmp_home: Path):
    from ns_supervisor.schedule_cfg import write_run_time
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    assert client.get("/api/status").json()["schedule_registered"] is False
    assert client.get("/api/settings").json()["schedule_registered"] is False
    write_run_time(state.paths, "02:30")  # `ns schedule` registered the task
    assert client.get("/api/status").json()["schedule_registered"] is True
    body = client.get("/api/settings").json()
    assert body["schedule_registered"] is True and body["schedule_time"] == "02:30"


def test_try_again_registers_the_task_through_the_schedule_adapter(tmp_home: Path):
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    state.adapters.schedule.applied.clear()  # type: ignore[attr-defined]
    r = client.put("/api/settings", json={"schedule_time": "03:00"})
    assert r.json()["schedule_result"]["applied"] is True
    assert state.adapters.schedule.applied == ["03:00"]  # type: ignore[attr-defined]


def test_the_pages_show_the_no_task_message_and_a_try_again_button():
    lib = (SRC / "lib" / "format.ts").read_text(encoding="utf-8")
    assert 'export const NO_TASK = "No nightly task; videos are made only while Nightshift is open"' in lib
    shell = (SRC / "shell" / "Shell.tsx").read_text(encoding="utf-8")
    assert 'status?.schedule_registered === false' in shell and "No nightly task" in shell and "NO_TASK" in shell
    settings = page("Settings.tsx")
    assert "s.schedule_registered === false" in settings and ">Try again</Button>" in settings
    assert "put({ schedule_time: s.schedule_time }" in settings
