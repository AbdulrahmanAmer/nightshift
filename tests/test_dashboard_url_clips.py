# ruff: noqa: E501
import importlib.util
from pathlib import Path
from typing import Any

import pytest

from ns_common.paths import Paths
from ns_dashboard.adapters import RealClipAdapter
from ns_dashboard.assistant.llm import ScriptedToolLlm, StaticProvider, call, say
from ns_dashboard.errors import AdapterUnavailable, ApiError
from ns_dashboard.fakes import seed_channel
from ns_dashboard.handlers import Dashboard, build_state
from ns_dashboard.jobs import JobControl, _stage_of
from ns_dashboard.supervisor_link import SupervisorLink
from ns_dashboard.svc_jobs import clean_url
from tests.helpers import InProcessRunner

URL = "https://videos.example.com/watch?v=abc123&t=5"


def make(tmp_home: Path, llm: ScriptedToolLlm | None = None):
    state = build_state(
        Paths.from_env(), fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    )
    seed_channel(state.db, state.paths, "after-dark")
    return Dashboard(state, llm_provider=StaticProvider(llm or ScriptedToolLlm([]))), state


def code_of(fn: Any, *args: Any, **kw: Any) -> str:
    with pytest.raises(ApiError) as ei:
        fn(*args, **kw)
    return ei.value.code


def body(**kw: Any) -> dict[str, Any]:
    return {"channel": "after-dark", "rights_confirmed": True, **kw}


def test_clean_url_rules():
    assert clean_url(None) is None and clean_url("") is None
    assert clean_url("  http://a.example/x?y=1#z ") == "http://a.example/x?y=1#z"
    assert clean_url("https://a.example:8080/v") == "https://a.example:8080/v"
    for bad in ("ftp://a.example/x", "javascript:alert(1)", "https://user:pw@a.example/x", "https://user@a.example/x",
                "https://a b.example/x", "//a.example/x", "a.example/x", "https://", "https://a.example/" + "x" * 2100):
        assert code_of(clean_url, bad) == "bad_url", bad


def test_stage_of_download_messages():
    assert _stage_of("Downloading 42%") == "download"
    assert _stage_of("select (3/9)") == "select"
    assert _stage_of("Waiting for GPU (held by x)") is None


def test_url_job_downloads_then_clips_and_reports_progress(tmp_home: Path):
    dash, state = make(tmp_home)
    sub = state.hub.subscribe()
    job = dash.jobs.create_clip(body(url=URL, template="auto", max_clips=2))
    p = job["params"]
    assert p["url"] == URL and p["source_path"] is None and p["rerun"] is False and p["rights_confirmed"] is True
    assert p["source_id"].startswith("src-")
    state.worker.run_next()
    done = dash.jobs.get(job["job_id"])
    assert done["status"] == "done" and done["progress"] == 100.0

    seen = []
    while (e := sub.get(0.01)) is not None:
        if e["kind"] == "dashboard_job" and e["data"]["job_id"] == job["job_id"]:
            seen.append((e["data"]["stage"], e["data"]["message"], e["data"]["progress"]))
    downloads = [s for s in seen if s[0] == "download"]
    assert [m for _s, m, _p in downloads][:5] == [f"Downloading {n}%" for n in (0, 25, 50, 75, 100)]
    assert [pr for _s, _m, pr in downloads][:5] == [0.0, 25.0, 50.0, 75.0, 100.0]
    assert any(s[0] not in (None, "download") for s in seen)  # the clip stages follow the download

    clip_adapter = state.adapters.clip
    assert clip_adapter.urls == [URL]  # type: ignore[attr-defined]
    call_info = clip_adapter.calls[0]  # type: ignore[attr-defined]
    assert call_info["source"].endswith("source.mp4") and p["source_id"] in call_info["source"]
    assert Path(call_info["source"]).is_file()
    clips = dash.outputs.list_outputs(status="pending", kind="clip")
    assert len(clips) == 2 and all(c["source_id"] == p["source_id"] for c in clips)
    assert done["result"]["source_id"] == p["source_id"]


def test_url_jobs_keep_the_rights_gate_and_exclusive_sources(tmp_home: Path):
    dash, state = make(tmp_home)
    assert code_of(dash.jobs.create_clip, body(url=URL, rights_confirmed=False)) == "rights_required"
    assert code_of(dash.jobs.create_clip, {"channel": "after-dark", "url": URL}) == "rights_required"
    assert code_of(dash.jobs.create_clip, body(url=URL, rights_confirmed="true")) == "rights_required"
    assert code_of(dash.jobs.create_clip, body(url="ftp://x.example/v")) == "bad_url"
    assert code_of(dash.jobs.create_clip, body(url=URL, path=str(tmp_home / "a.mp4"))) == "invalid_options"
    assert code_of(dash.jobs.create_clip, body(url=URL, source_id="src-20261101-aaaa")) == "invalid_options"
    assert code_of(dash.jobs.create_clip, body(url=URL, channel="ghost")) == "channel_not_found"
    assert code_of(dash.jobs.create_clip, body()) == "no_source"
    assert state.store.counts() == {"queued": 0, "running": 0}
    state.draining = True
    assert code_of(dash.jobs.create_clip, body(url=URL)) == "shutting_down"


def test_url_import_unavailable_fails_the_job_with_a_plain_message(tmp_home: Path):
    dash, state = make(tmp_home)
    state.adapters.clip.fail_url_with = AdapterUnavailable("URL import not available yet")  # type: ignore[attr-defined]
    job = dash.jobs.create_clip(body(url=URL))
    state.worker.run_next()
    done = dash.jobs.get(job["job_id"])
    assert done["status"] == "failed" and done["message"] == "URL import not available yet"
    assert done["result"]["error"]["code"] == "unavailable"
    assert dash.outputs.list_outputs(kind="clip") == []


def test_cancel_during_download(tmp_home: Path):
    dash, state = make(tmp_home)
    started: list[str] = []

    def fetch(ctl: JobControl, url: str, source_id: str) -> Path:
        ctl.download(0)
        started.append(url)
        state.worker.cancel_running()
        ctl.download(50)  # raises JobCancelled
        raise AssertionError("not reached")

    state.adapters.clip.fetch_url = fetch  # type: ignore[attr-defined]
    job = dash.jobs.create_clip(body(url=URL))
    state.worker.run_next()
    assert started == [URL] and dash.jobs.get(job["job_id"])["status"] == "cancelled"


def test_download_percent_is_clamped(tmp_home: Path):
    dash, state = make(tmp_home)
    rec = state.store.create("clip", "after-dark", {})
    ctl = JobControl(rec, state.store, state.hub)
    ctl.download(150)
    ctl.download(-5)
    got = state.store.get(rec.job_id)
    assert got is not None and got.message == "Downloading 0%" and got.progress == 0.0
    ctl.download(150)
    got = state.store.get(rec.job_id)
    assert got is not None and got.message == "Downloading 100%" and got.progress == 1.0


# ------------------------------------------------------------------ the real adapter


def test_real_adapter_calls_import_url_with_progress(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_studio.clipper import source

    seen: dict[str, Any] = {}

    def import_url(
        url: str, dest_dir: Path, progress_cb: Any = None, *, exe: Path | None = None, cancel_token: Any = None
    ) -> Path:
        seen.update(url=url, dest=dest_dir)
        for pct in (10, 60):
            progress_cb(pct)
        return dest_dir / "source.mp4"

    monkeypatch.setattr(source, "import_url", import_url)
    paths = Paths.from_env()
    state = build_state(paths, fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2))
    adapter = RealClipAdapter(
        paths, SupervisorLink("http://127.0.0.1:9", timeout_s=0.2), runner=InProcessRunner()
    )
    rec = state.store.create("clip", "after-dark", {})
    ctl = JobControl(rec, state.store, state.hub)
    out = adapter.fetch_url(ctl, URL, "src-20261101-aaaa")
    assert out.name == "source.mp4" and seen["url"] == URL
    assert seen["dest"] == paths.home / "imports" / "clipper" / "_downloads" / "src-20261101-aaaa"
    got = state.store.get(rec.job_id)
    assert got is not None and got.message == "Downloading 60%" and got.progress == 0.6


# ------------------------------------------------------------------ the Assistant tool


def test_assistant_can_start_a_clip_job_from_a_link(tmp_home: Path):
    llm = ScriptedToolLlm([
        call("start_clip_job", {"url": URL, "channel": "after-dark", "max_clips": 4}),
        say("Please confirm."),
    ])
    dash, state = make(tmp_home, llm)
    events = list(dash.assistant.chat("cut clips from this link " + URL))
    card = next(e["payload"] for e in events if e["kind"] == "confirm")
    assert URL in card["text"] and "up to 4 clips" in card["text"] and "you confirm you own this video" in card["text"]
    assert state.store.counts() == {"queued": 0, "running": 0}
    out = dash.assistant.confirm(card["action_id"])
    assert out["status"] == "done"
    rec = state.store.list_jobs()[0]
    assert rec.kind == "clip" and rec.params["url"] == URL and rec.params["rights_confirmed"] is True


def test_start_clip_job_needs_a_source_before_the_card_is_shown(tmp_home: Path):
    llm = ScriptedToolLlm([call("start_clip_job", {"channel": "after-dark"}), say("I need a link or a file.")])
    dash, state = make(tmp_home, llm)
    events = list(dash.assistant.chat("make clips"))
    assert not [e for e in events if e["kind"] == "confirm"]
    bad = next(e["payload"] for e in events if e["kind"] == "tool_result")
    assert bad["ok"] is False and "web link" in bad["error"]
    spec = dash.registry.get("start_clip_job").spec  # type: ignore[union-attr]
    assert "url" in spec.args_schema["properties"] and spec.args_schema["required"] == ["channel"]
    assert dash.registry.validate("start_clip_job", {"channel": "x", "url": URL}) is None
    assert dash.registry.validate("start_clip_job", {"channel": "x", "path": "C:\\v.mp4"}) is None


# ------------------------------------------------------------------ HTTP


def client_for(tmp_home: Path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_dashboard.server import create_app

    state = build_state(
        Paths.from_env(), fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    )
    seed_channel(state.db, state.paths, "after-dark")
    app = create_app(state=state, start_worker=False, static_dir=tmp_home / "static-none")
    return TestClient(app, base_url="http://127.0.0.1:8100"), state


def test_json_url_over_http(tmp_home: Path):
    client, state = client_for(tmp_home)
    r = client.post("/api/jobs/clip", json=body(url=URL, template="auto"))
    assert r.status_code == 201 and r.json()["params"]["url"] == URL
    no = client.post("/api/jobs/clip", json={"channel": "after-dark", "url": URL})
    assert no.status_code == 400 and no.json()["error"]["code"] == "rights_required"
    bad = client.post("/api/jobs/clip", json=body(url="file:///etc/passwd"))
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "bad_url"
    state.worker.run_next()
    job = client.get("/api/jobs").json()[0]
    assert job["status"] == "done" and len(job["result"]["output_ids"]) == 2


HAS_MULTIPART = bool(importlib.util.find_spec("python_multipart") or importlib.util.find_spec("multipart"))


@pytest.mark.skipif(not HAS_MULTIPART, reason="python-multipart not installed")
def test_form_url_over_http(tmp_home: Path):
    client, _state = client_for(tmp_home)
    form = {"channel": "after-dark", "url": URL, "template": "auto", "max_clips": "3", "rights_confirmed": "true"}
    # multipart needs at least one file part for httpx to choose that encoding; an empty filename is ignored
    r = client.post("/api/jobs/clip", data=form, files={"note": (None, "x")})
    assert r.status_code == 201, r.text
    assert r.json()["params"]["url"] == URL and r.json()["params"]["max_clips"] == 3
    refused = client.post("/api/jobs/clip", data={**form, "rights_confirmed": "false"}, files={"note": (None, "x")})
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "rights_required"


@pytest.mark.skipif(not HAS_MULTIPART, reason="python-multipart not installed")
def test_urlencoded_form_url_over_http(tmp_home: Path):
    client, _state = client_for(tmp_home)
    form = {"channel": "after-dark", "url": URL, "rights_confirmed": "on"}
    r = client.post("/api/jobs/clip", data=form)
    assert r.status_code == 201, r.text
    assert r.json()["params"]["url"] == URL
    assert client.post("/api/jobs/clip", data={"channel": "after-dark", "url": URL}).status_code == 400
