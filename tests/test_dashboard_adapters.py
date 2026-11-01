import json
import sys
import threading
import types
from pathlib import Path
from typing import Any

import pytest

from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_dashboard.adapters import (
    RealClipAdapter,
    RealDoctorAdapter,
    RealKeyringAdapter,
    RealLearnAdapter,
    RealRunAdapter,
    RealScheduleAdapter,
    RealVerifyAdapter,
    lazy,
    real_adapters,
    scan_voice_library,
    stage_reporter,
)
from ns_dashboard.errors import AdapterUnavailable, ApiError
from ns_dashboard.events import EventHub
from ns_dashboard.fakes import (
    FakeClipAdapter,
    FakeLearnAdapter,
    FakeSupervisor,
    FakeTtsAdapter,
    fake_adapters,
    seed_output,
)
from ns_dashboard.jobs import JobControl, JobStore, JobWorker
from ns_dashboard.state import open_db
from ns_dashboard.supervisor_link import EventRelay, HttpAcquirer, SupervisorLink, make_acquirer


def make_worker(handlers: dict[str, Any]):
    paths = Paths.from_env()
    db = open_db(paths)
    store = JobStore(db)
    worker = JobWorker(store, handlers, EventHub(), NsLogger("dashboard", paths.logs))
    return paths, db, store, worker


def test_lazy_names_the_missing_symbol(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "ns_studio.clipper", None)
    with pytest.raises(AdapterUnavailable, match="ns_studio.clipper.run_clip_job"):
        lazy("ns_studio.clipper", "run_clip_job")
    with pytest.raises(AdapterUnavailable, match="no_such_attr"):
        lazy("ns_dashboard.errors", "no_such_attr")
    assert lazy("ns_dashboard.errors", "ApiError") is ApiError


def test_real_adapters_report_unavailable_collaborators(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    for mod in ("ns_studio.clipper", "ns_studio.learn", "ns_supervisor.verify", "ns_supervisor.doctor",
                "ns_supervisor.scheduler", "keyring"):
        monkeypatch.setitem(sys.modules, mod, None)
    monkeypatch.delattr("ns_studio.orchestrator.run_job_with_progress", raising=False)
    paths, _db, store, worker = make_worker({})
    link = SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    rec = store.create("clip", "c", {})
    ctl = JobControl(rec, store, EventHub())
    with pytest.raises(AdapterUnavailable):
        RealRunAdapter(paths, link).run(ctl, "c", "shorts_only", None)
    with pytest.raises(AdapterUnavailable):
        RealClipAdapter(paths, link).clip(ctl, tmp_home / "x.mp4", "src-1", {})
    with pytest.raises(AdapterUnavailable):
        RealLearnAdapter(paths).import_csv(ctl, tmp_home / "a.csv")
    with pytest.raises(AdapterUnavailable):
        RealLearnAdapter(paths).summary()
    with pytest.raises(AdapterUnavailable):
        RealVerifyAdapter(paths).verify(ctl, True)
    with pytest.raises(AdapterUnavailable):
        RealDoctorAdapter(paths).bundle()
    with pytest.raises(AdapterUnavailable):
        RealKeyringAdapter().get("pexels_api_key")
    out = RealScheduleAdapter(paths).apply("04:00")
    assert out["applied"] is False and "Saved" in out["note"]
    assert worker.run_next() is False
    assert real_adapters(paths, link, NsLogger("dashboard", paths.logs)).folder is not None


def test_real_adapters_pass_progress_cancel_and_params(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "ns_supervisor.client", None)  # force the HTTP acquirer
    seen: dict[str, Any] = {}

    def run_clip_job(**kw: Any) -> list[str]:
        seen.update(kw)
        for i, name in enumerate(["import", "select"]):
            kw["on_stage"](name, i, 2)
        return ["job/c1"]

    monkeypatch.setitem(sys.modules, "ns_studio.clipper", types.SimpleNamespace(run_clip_job=run_clip_job))
    paths, _db, store, worker = make_worker({})
    link = SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    adapter = RealClipAdapter(paths, link)
    worker.handlers["clip"] = lambda ctl: {"output_ids": adapter.clip(ctl, Path("a.mp4"), "src-1", {"x": 1})}
    job = store.create("clip", "c", {})
    worker.run_next()
    rec = store.get(job.job_id)
    assert rec is not None and rec.status == "done" and rec.result == {"output_ids": ["job/c1"]}
    assert seen["source_id"] == "src-1" and seen["params"] == {"x": 1}
    assert isinstance(seen["acquirer"], HttpAcquirer)

    # cancel requested before the second stage unwinds through the collaborator
    def cancelling(**kw: Any) -> list[str]:
        kw["on_stage"]("import", 0, 2)
        worker.cancel_running()
        kw["on_stage"]("select", 1, 2)
        return []

    monkeypatch.setitem(sys.modules, "ns_studio.clipper", types.SimpleNamespace(run_clip_job=cancelling))
    job2 = store.create("clip", "c", {})
    worker.run_next()
    rec2 = store.get(job2.job_id)
    assert rec2 is not None and rec2.status == "cancelled"


def test_stage_reporter_formats_progress(tmp_home: Path):
    _paths, _db, store, _worker = make_worker({})
    ctl = JobControl(store.create("run", "c", {}), store, EventHub())
    stage_reporter(ctl)("render", 3, 4)
    rec = store.get(ctl.job_id)
    assert rec is not None and rec.progress == 0.75 and rec.message == "render (4/4)"


def test_scan_voice_library(tmp_home: Path):
    paths = Paths.from_env()
    d = paths.models_tts_voices / "narrator"
    d.mkdir(parents=True)
    (d / "voice.json").write_text(json.dumps({"mode": "design"}), encoding="utf-8")
    bad = paths.models_tts_voices / "broken"
    bad.mkdir()
    (bad / "voice.json").write_text("{nope", encoding="utf-8")
    names = {v["name"]: v for v in scan_voice_library(paths)}
    assert names["Ryan"]["mode"] == "preset"
    assert names["narrator"] == {"name": "narrator", "mode": "design", "source": "library"}
    assert names["broken"]["mode"] == "clone"


def test_supervisor_link_round_trip():
    with FakeSupervisor() as sup:
        sup.state = {"gpu_owner": "studio:j1", "services": {"llama": {"state": "ready"}}}
        link = SupervisorLink(sup.url)
        assert link.status() == sup.state
        assert link.warmup("llama") == {"state": "starting"}
        assert link.shutdown(30) == {"ok": True}
        assert link.stop_all() == {"ok": True}
        posts = [(p, b) for m, p, b in sup.calls if m == "POST"]
        assert ("/warmup", {"service": "llama"}) in posts
        assert ("/shutdown", {"grace_s": 30}) in posts


def test_supervisor_link_offline():
    link = SupervisorLink("http://127.0.0.1:9", timeout_s=0.3)
    assert link.status() is None
    with pytest.raises(ApiError) as ei:
        link.warmup("llama")
    assert ei.value.status == 503 and ei.value.code == "engine_offline"


def test_events_stream_and_relay():
    with FakeSupervisor() as sup:
        sup.events = [
            {"ts": 1, "kind": "service", "data": {"name": "llama", "state": "starting"}},
            {"ts": 2, "kind": "job", "data": {"job_id": "j", "stage": "voice"}},
            {"ts": 3, "kind": "odd", "data": {}},
        ]
        link = SupervisorLink(sup.url)
        got = list(link.events(threading.Event()))
        assert [e["ts"] for e in got] == [1, 2, 3]
        hub = EventHub()
        sub = hub.subscribe()
        relay = EventRelay(link, hub, NsLogger("dashboard", Paths.from_env().logs), retry_s=0.01)
        assert relay.relay_once() == 3
        kinds = []
        while (e := sub.get(0.01)) is not None:
            kinds.append(e["kind"])
        assert kinds == ["service", "job", "service"]


def test_http_acquirer_acquire_release():
    with FakeSupervisor() as sup:
        acq = HttpAcquirer(sup.url)
        ep = acq.acquire("llama", "studio:j1", model_alias="story")
        assert ep.base_url == "http://127.0.0.1:9" and ep.model_alias == "story"
        acq.release("llama", "studio:j1")
        posts = {p: b for m, p, b in sup.calls if m == "POST"}
        assert posts["/acquire"]["owner"] == "studio:j1" and posts["/acquire"]["wait"] is True
        assert posts["/release"] == {"service": "llama", "owner": "studio:j1"}


def test_make_acquirer_falls_back_to_http(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "ns_supervisor.client", None)
    assert isinstance(make_acquirer("http://127.0.0.1:8099"), HttpAcquirer)


def test_fake_clip_adapter_writes_files_and_rows(tmp_home: Path):
    paths, db, store, worker = make_worker({})
    fake = FakeClipAdapter(db, paths)
    src = tmp_home / "in" / "talk.mp4"
    src.parent.mkdir()
    src.write_bytes(b"source-bytes")
    worker.handlers["clip"] = lambda ctl: {
        "output_ids": fake.clip(ctl, src, "src-20261101-aaaa", {"channel": "after-dark", "max_clips": 5})
    }
    job = store.create("clip", "after-dark", {})
    worker.run_next()
    rec = store.get(job.job_id)
    assert rec is not None and rec.status == "done" and rec.result is not None
    ids = rec.result["output_ids"]
    assert len(ids) == 2
    rows = db.query("SELECT * FROM outputs WHERE kind = 'clip' ORDER BY output_id")
    assert [r["output_id"] for r in rows] == sorted(ids)
    assert all(r["status"] == "pending" and r["source_id"] == "src-20261101-aaaa" for r in rows)
    video = Path(rows[0]["path"])
    assert video.read_bytes() == b"source-bytes" and video.with_suffix(".json").exists()
    assert db.one("SELECT status FROM sources WHERE source_id = 'src-20261101-aaaa'") is not None


def test_fake_learn_and_tts_and_seed(tmp_home: Path):
    paths, db, store, worker = make_worker({})
    csv = tmp_home / "a.csv"
    csv.write_text("title,views\nA,1\nB,2\n", encoding="utf-8")
    learn = FakeLearnAdapter()
    worker.handlers["learn"] = lambda ctl: learn.import_csv(ctl, csv)
    job = store.create("learn", None, {})
    worker.run_next()
    rec = store.get(job.job_id)
    assert rec is not None and rec.result is not None and rec.result["rows"] == 2
    tts = FakeTtsAdapter(paths)
    voices, live = tts.voices()
    assert live and any(v["name"] == "Narrator" for v in voices)
    assert tts.preview("Ryan", "hello there", 1.0)[:4] == b"RIFF"
    oid = seed_output(db, paths, kind="long", status="approved")
    assert oid.endswith("/long")
    row = db.one("SELECT * FROM outputs WHERE output_id = ?", (oid,))
    assert row is not None and Path(row["path"]).exists() and row["k"] is None
    assert fake_adapters(db, paths).keyring.get("pexels_api_key") is None
