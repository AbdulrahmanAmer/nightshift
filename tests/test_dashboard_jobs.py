from pathlib import Path
from typing import Any

import pytest

from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_dashboard.events import EventHub
from ns_dashboard.jobs import JobControl, JobStore, JobWorker, make_gpu_gate
from ns_dashboard.state import open_db

STAGES = ["import", "transcribe", "select", "render"]


def build(tmp_home: Path, handlers: dict[str, Any], **kw: Any):
    paths = Paths.from_env()
    db = open_db(paths)
    store = JobStore(db)
    hub = EventHub()
    worker = JobWorker(store, handlers, hub, NsLogger("dashboard", paths.logs), **kw)
    return store, worker, hub, paths


def stage_handler(seen: list[str]):
    def handler(ctl: JobControl) -> dict[str, Any]:
        for name in ctl.stages(STAGES):
            seen.append(name)
        return {"output_ids": ["x/1"]}

    return handler


def test_fifo_and_done_result(tmp_home: Path):
    seen: list[str] = []
    store, worker, _hub, _ = build(tmp_home, {"clip": stage_handler(seen), "verify": lambda ctl: {"n": 2}})
    a = store.create("clip", "after-dark", {"path": "a.mp4"})
    b = store.create("verify", None, {})
    c = store.create("clip", "after-dark", {"path": "c.mp4"})
    assert worker.run_next() and worker.run_next() and worker.run_next()
    assert not worker.run_next()
    ra, rb, rc = (store.get(j.job_id) for j in (a, b, c))
    assert ra is not None and rb is not None and rc is not None
    assert (ra.status, rb.status, rc.status) == ("done", "done", "done")
    assert ra.result == {"output_ids": ["x/1"]} and rb.result == {"n": 2}
    assert ra.progress == 1.0 and ra.params == {"path": "a.mp4"}
    assert ra.started_at and ra.finished_at
    assert seen == STAGES * 2
    assert ra.finished_at <= rb.started_at <= rb.finished_at  # strictly sequential, in insertion order


def test_progress_rows_and_events_per_stage(tmp_home: Path):
    snapshots: list[tuple[float, str]] = []

    def handler(ctl: JobControl) -> None:
        for _ in ctl.stages(STAGES):
            rec = store.get(ctl.job_id)
            assert rec is not None
            snapshots.append((rec.progress, rec.message))

    store, worker, hub, _ = build(tmp_home, {"clip": handler})
    sub = hub.subscribe()
    job = store.create("clip", "c", {})
    worker.run_next()
    assert snapshots == [
        (0.0, "import (1/4)"),
        (0.25, "transcribe (2/4)"),
        (0.5, "select (3/4)"),
        (0.75, "render (4/4)"),
    ]
    events = []
    while (e := sub.get(0.01)) is not None:
        events.append(e)
    assert {e["kind"] for e in events} == {"dashboard_job"}
    assert events[-1]["data"]["status"] == "done" and events[-1]["data"]["job_id"] == job.job_id


def test_cancel_queued_is_immediate(tmp_home: Path):
    store, worker, _hub, _ = build(tmp_home, {"clip": stage_handler([])})
    job = store.create("clip", "c", {})
    assert worker.cancel(job.job_id) == "cancelled"
    assert not worker.run_next()
    rec = store.get(job.job_id)
    assert rec is not None and rec.status == "cancelled" and rec.finished_at


def test_cancel_running_stops_between_stages(tmp_home: Path):
    seen: list[str] = []
    holder: dict[str, JobControl] = {}

    def handler(ctl: JobControl) -> None:
        holder["ctl"] = ctl
        for name in ctl.stages(STAGES):
            seen.append(name)
            if name == "transcribe":
                assert worker.cancel(ctl.job_id) == "cancelling"
                assert ctl.cancelled()
                rec = store.get(ctl.job_id)
                assert rec is not None and rec.cancel_requested

    store, worker, _hub, _ = build(tmp_home, {"clip": handler})
    job = store.create("clip", "c", {})
    worker.run_next()
    assert seen == ["import", "transcribe"]
    rec = store.get(job.job_id)
    assert rec is not None and rec.status == "cancelled" and not rec.cancel_requested
    assert worker.cancel(job.job_id) == "cancelled"  # terminal: reports its status


def test_cancel_unknown_job(tmp_home: Path):
    _store, worker, _hub, _ = build(tmp_home, {})
    with pytest.raises(LookupError):
        worker.cancel("nope-20260101-abcd")


def test_cancel_running_helper(tmp_home: Path):
    def handler(ctl: JobControl) -> None:
        assert worker.cancel_running() == ctl.job_id
        ctl.check_cancel()

    store, worker, _hub, _ = build(tmp_home, {"clip": handler})
    job = store.create("clip", None, {})
    assert worker.cancel_running() is None
    worker.run_next()
    rec = store.get(job.job_id)
    assert rec is not None and rec.status == "cancelled"


def test_failure_paths_record_message(tmp_home: Path):
    def boom(ctl: JobControl) -> None:
        raise ValueError("kaput")

    def coded(ctl: JobControl) -> None:
        raise NsError("NS-E201", "held by opencode")

    store, worker, _hub, _ = build(tmp_home, {"clip": boom, "run": coded})
    a = store.create("clip", None, {})
    b = store.create("run", None, {})
    c = store.create("verify", None, {})  # no handler registered
    for _ in range(3):
        worker.run_next()
    ra, rb, rc = (store.get(j.job_id) for j in (a, b, c))
    assert ra is not None and rb is not None and rc is not None
    assert ra.status == "failed" and "ValueError: kaput" in ra.message
    assert rb.status == "failed" and "NS-E201" in rb.message
    assert rb.result is not None and rb.result["error"]["code"] == "NS-E201"
    assert rc.status == "failed" and "no handler" in rc.message


def test_recover_interrupted_on_start(tmp_home: Path):
    store, worker, _hub, _ = build(tmp_home, {})
    job = store.create("clip", None, {})
    store.mark_running(job.job_id)
    worker.start()
    worker.stop()
    rec = store.get(job.job_id)
    assert rec is not None and rec.status == "failed" and "restart" in rec.message


def test_thread_mode_drains_queue(tmp_home: Path):
    seen: list[str] = []
    store, worker, _hub, _ = build(tmp_home, {"clip": stage_handler(seen)})
    worker.start()
    try:
        jobs = [store.create("clip", "c", {}) for _ in range(2)]
        for j in jobs:
            worker.enqueue(j.job_id)
        assert worker.wait_idle(20)
    finally:
        worker.stop()
    assert all((r := store.get(j.job_id)) is not None and r.status == "done" for j in jobs)
    assert store.counts() == {"queued": 0, "running": 0}


def test_gpu_gate_waits_then_proceeds(tmp_home: Path):
    owners = ["opencode", "opencode", None]
    sleeps: list[float] = []
    messages: list[str] = []

    def status() -> dict[str, Any]:
        return {"gpu_owner": owners.pop(0) if owners else None}

    def sleeper(s: float) -> None:
        sleeps.append(s)
        rec = store.running()
        assert rec is not None
        messages.append(rec.message)

    gate = make_gpu_gate(status, sleeper=sleeper, poll_s=2.0)
    store, worker, _hub, _ = build(tmp_home, {"clip": lambda ctl: {"ok": True}}, gpu_gate=gate)
    job = store.create("clip", None, {})
    worker.run_next()
    assert sleeps == [2.0, 2.0]
    assert messages == ["Waiting for GPU (held by opencode)"] * 2
    rec = store.get(job.job_id)
    assert rec is not None and rec.status == "done"


def test_gpu_gate_gives_up_with_ns_e201(tmp_home: Path):
    gate = make_gpu_gate(lambda: {"gpu_owner": "opencode"}, sleeper=lambda s: None, poll_s=10, max_wait_s=30)
    store, worker, _hub, _ = build(tmp_home, {"run": lambda ctl: None}, gpu_gate=gate)
    job = store.create("run", "c", {})
    worker.run_next()
    rec = store.get(job.job_id)
    assert rec is not None and rec.status == "failed" and "NS-E201" in rec.message


def test_gpu_gate_ignores_missing_supervisor_and_non_gpu_kinds(tmp_home: Path):
    calls: list[str] = []

    def gate(ctl: JobControl) -> None:
        calls.append(ctl.kind)

    handlers = {"learn": lambda ctl: None, "clip": lambda ctl: None}
    store, worker, _hub, _ = build(tmp_home, handlers, gpu_gate=gate, inline_kinds=frozenset())
    store.create("learn", None, {})
    store.create("clip", None, {})
    worker.run_next()
    worker.run_next()
    assert calls == ["clip"]
    g = make_gpu_gate(lambda: None)
    ctl_store, _w, _h, _ = build(tmp_home, {})
    rec = ctl_store.create("clip", None, {})
    g(JobControl(rec, ctl_store, EventHub()))  # returns immediately: no supervisor, nothing to wait for


def test_params_json_layout_and_list_filters(tmp_home: Path):
    store, _worker, _hub, _ = build(tmp_home, {})
    a = store.create("clip", "x", {"k": 1})
    store.create("learn", None, {})
    raw = store.db.one("SELECT params_json FROM dashboard_jobs WHERE job_id = ?", (a.job_id,))
    assert raw is not None and raw["params_json"] == '{"params": {"k": 1}, "result": null}'
    assert [j.kind for j in store.list_jobs()] == ["learn", "clip"]
    assert [j.job_id for j in store.list_jobs(kind="clip")] == [a.job_id]
    assert store.list_jobs(status="done") == []
    with pytest.raises(ValueError):
        store.create("bogus", None, {})
    # a bare params object written by another component is still readable
    sql = "UPDATE dashboard_jobs SET params_json = ? WHERE job_id = ?"
    store.db.execute(sql, ('{"path": "z"}', a.job_id))
    got = store.get(a.job_id)
    assert got is not None and got.params == {"path": "z"} and got.result is None


def test_to_dict_uses_percent_and_stage(tmp_home: Path):
    store, worker, _hub, _ = build(tmp_home, {"clip": stage_handler([])})
    job = store.create("clip", "c", {})
    d0 = job.to_dict()
    assert d0["progress"] == 0 and d0["stage"] is None and d0["status"] == "queued"
    worker.run_next()
    done = store.get(job.job_id)
    assert done is not None
    d = done.to_dict()
    assert d["progress"] == 100.0 and d["status"] == "done" and d["result"] == {"output_ids": ["x/1"]}
    store.update_progress(job.job_id, 0.5, "select (3/6)")
    mid = store.get(job.job_id)
    assert mid is not None and mid.to_dict()["progress"] == 50.0 and mid.to_dict()["stage"] == "select"


def test_run_inline_executes_a_specific_job(tmp_home: Path):
    store, worker, _hub, _ = build(tmp_home, {"learn": lambda ctl: {"matched": 3}, "clip": lambda ctl: None})
    first = store.create("clip", None, {})
    learn = store.create("learn", None, {})
    rec = worker.run_inline(learn.job_id)
    assert rec.status == "done" and rec.result == {"matched": 3}
    first_rec = store.get(first.job_id)
    assert first_rec is not None and first_rec.status == "queued"  # the FIFO job was not touched
    assert worker.running_job_id is None
    assert worker.run_inline(learn.job_id).status == "done"  # already finished: returned as is
    with pytest.raises(LookupError):
        worker.run_inline("nope-20260101-abcd")


def test_worker_leaves_inline_kinds_to_run_inline(tmp_home: Path):
    store, worker, _hub, _ = build(tmp_home, {"learn": lambda ctl: {"ok": 1}, "clip": lambda ctl: None})
    learn = store.create("learn", None, {})
    assert worker.run_next() is False  # the only queued job is a learn import
    clip = store.create("clip", None, {})
    assert worker.run_next() is True
    clip_rec, learn_rec = store.get(clip.job_id), store.get(learn.job_id)
    assert clip_rec is not None and clip_rec.status == "done"
    assert learn_rec is not None and learn_rec.status == "queued"


def test_cancel_that_loses_the_race_with_the_worker_still_cancels_the_running_job(tmp_home: Path):
    """Review row 14: the job turns `running` between the read and `cancel_queued`."""
    store, worker, _hub, _ = build(tmp_home, {"clip": stage_handler([])})
    job = store.create("clip", "c", {})
    real = store.cancel_queued

    def racing(job_id: str) -> bool:
        store.mark_running(job_id)  # the worker wins
        return real(job_id)  # now a no-op: the row is no longer queued

    store.cancel_queued = racing  # type: ignore[method-assign]
    assert worker.cancel(job.job_id) == "cancelling"
    assert store.is_cancel_requested(job.job_id)
    rec = store.get(job.job_id)
    assert rec is not None and rec.cancel_requested
