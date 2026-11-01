import json
import threading
from pathlib import Path

import pytest

from ns_common.paths import Paths
from ns_dashboard.events import EventHub, iter_sse, sse_frame
from ns_dashboard.state import (
    DEFAULT_SETTINGS,
    load_dashboard_settings,
    open_db,
    save_dashboard_settings,
)


def test_hub_fans_out_to_every_subscriber():
    hub = EventHub()
    a, b = hub.subscribe(), hub.subscribe()
    hub.publish("job", {"n": 1})
    assert a.get(0.1) == {"kind": "job", "data": {"n": 1}}
    assert b.get(0.1) == {"kind": "job", "data": {"n": 1}}
    assert a.get(0.01) is None
    a.close()
    assert hub.subscriber_count == 1


def test_slow_consumer_drops_oldest():
    hub = EventHub()
    s = hub.subscribe()
    for i in range(600):
        hub.publish("x", {"i": i})
    first = s.get(0.1)
    assert first is not None and first["data"]["i"] == 100


def test_sse_frame_format():
    f = sse_frame("status", {"a": 1})
    assert f == 'data: {"kind": "status", "payload": {"a": 1}}\n\n'


def test_iter_sse_initial_status_event_then_status_and_limit():
    hub = EventHub()
    sub = hub.subscribe()
    hub.publish("dashboard_job", {"job_id": "j1"})
    frames = list(iter_sse(sub, lambda: {"state": "idle"}, heartbeat_s=0.01, limit=3))
    kinds = [json.loads(f.split("data: ", 1)[1])["kind"] for f in frames]
    assert kinds == ["status", "job", "status"]
    assert json.loads(frames[1].split("data: ", 1)[1])["payload"] == {"job_id": "j1"}
    assert len(frames) == 3
    assert hub.subscriber_count == 0


def test_iter_sse_heartbeat_when_idle():
    hub = EventHub()
    frames = list(iter_sse(hub.subscribe(), lambda: {"s": 1}, heartbeat_s=0.01, limit=3))
    assert [json.loads(f.split("data: ", 1)[1])["kind"] for f in frames] == ["status"] * 3


def test_iter_sse_stops_on_event():
    hub = EventHub()
    stop = threading.Event()
    stop.set()
    frames = list(iter_sse(hub.subscribe(), lambda: {}, heartbeat_s=0.01, stop=stop))
    assert len(frames) == 1


def test_locked_db_tx_rolls_back(tmp_home: Path):
    db = open_db(Paths.from_env())
    db.execute("INSERT INTO channels(slug, display_name, created_at) VALUES ('a','A','t')")
    with pytest.raises(RuntimeError), db.tx():
        db.execute("INSERT INTO channels(slug, display_name, created_at) VALUES ('b','B','t')")
        raise RuntimeError("boom")
    assert [r["slug"] for r in db.query("SELECT slug FROM channels")] == ["a"]
    with db.tx():
        db.execute("INSERT INTO channels(slug, display_name, created_at) VALUES ('c','C','t')")
    assert db.one("SELECT COUNT(*) AS n FROM channels")["n"] == 2
    assert db.one("SELECT name FROM sqlite_master WHERE name='dashboard_jobs'") is not None
    db.close()


def test_settings_defaults_and_round_trip(tmp_home: Path):
    paths = Paths.from_env()
    assert load_dashboard_settings(paths) == DEFAULT_SETTINGS
    saved = save_dashboard_settings(paths, {"run_time": "04:30", "bogus": 1})
    assert saved["run_time"] == "04:30" and "bogus" not in saved
    assert load_dashboard_settings(paths)["run_time"] == "04:30"
    (paths.config / "dashboard.json").write_text("{not json", encoding="utf-8")
    assert load_dashboard_settings(paths) == DEFAULT_SETTINGS
