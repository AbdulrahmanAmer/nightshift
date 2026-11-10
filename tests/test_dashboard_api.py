# ruff: noqa: E501
import hashlib
import importlib.util
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from ns_common import ffmpeg
from ns_common.paths import Paths
from ns_dashboard.assistant.llm import ScriptedToolLlm, StaticProvider, call, say
from ns_dashboard.assistant.tools import NO_TOOL, build_registry
from ns_dashboard.errors import SupervisorUnavailable
from ns_dashboard.fakes import FakeSupervisor, seed_channel, seed_output
from ns_dashboard.handlers import build_state
from ns_dashboard.state import AppState
from ns_dashboard.supervisor_link import SupervisorLink

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ns_dashboard.server import create_app  # noqa: E402

HAS_MULTIPART = bool(importlib.util.find_spec("python_multipart") or importlib.util.find_spec("multipart"))
needs_multipart = pytest.mark.skipif(not HAS_MULTIPART, reason="python-multipart not installed")
needs_ffmpeg = pytest.mark.skipif(not ffmpeg.available(), reason="FFmpeg not installed")
BASE = "http://127.0.0.1:8100"


def make_env(
    tmp_home: Path, link: SupervisorLink | None = None, llm: ScriptedToolLlm | None = None
) -> tuple[TestClient, AppState]:
    static = tmp_home / "static"
    static.mkdir(exist_ok=True)
    (static / "index.html").write_text("<!doctype html><title>Nightshift</title>", encoding="utf-8")
    (static / "app.js").write_text("// ui", encoding="utf-8")
    state = build_state(
        Paths.from_env(), fake_services=True, link=link or SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    )
    provider = StaticProvider(llm or ScriptedToolLlm([]))
    app = create_app(state=state, start_worker=False, static_dir=static, heartbeat_s=0.01, llm_provider=provider)
    return TestClient(app, base_url=BASE), state


@pytest.fixture
def env(tmp_home: Path) -> tuple[TestClient, AppState]:
    return make_env(tmp_home)


def err(resp: Any) -> dict[str, Any]:
    body = resp.json()
    assert set(body) == {"error"}
    return body["error"]


# ------------------------------------------------------------------ security / static


def test_non_loopback_host_is_refused(env):
    client, _ = env
    r = client.get("/api/status", headers={"host": "evil.example.com"})
    assert r.status_code == 403 and err(r)["code"] == "NS-E207"
    assert client.get("/", headers={"host": "192.168.1.9:8100"}).status_code == 403
    assert client.get("/api/status", headers={"host": "localhost:8100"}).status_code == 200
    assert client.get("/api/status", headers={"host": "[::1]:8100"}).status_code == 200


def test_foreign_origin_is_refused(env):
    client, _ = env
    r = client.post("/api/engine/stop_all", headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.get("/api/status", headers={"origin": "null"}).status_code == 403
    assert client.get("/api/status", headers={"origin": BASE}).status_code == 200


def test_static_index_and_headers(env):
    client, _ = env
    r = client.get("/")
    assert r.status_code == 200 and "Nightshift" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/app.js").text == "// ui"
    api = client.get("/api/status")
    assert api.headers["cache-control"] == "no-store" and "content-security-policy" not in api.headers


def test_unknown_api_path_is_a_json_404(env):
    client, _ = env
    r = client.get("/api/nothing-here")
    assert r.status_code == 404 and err(r)["code"] == "not_found"


def test_missing_static_folder_does_not_break_the_api(tmp_home: Path):
    link = SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    state = build_state(Paths.from_env(), fake_services=True, link=link)
    app = create_app(state=state, start_worker=False, static_dir=tmp_home / "absent")
    client = TestClient(app, base_url=BASE)
    assert client.get("/api/status").status_code == 200
    assert client.get("/").status_code == 404


def test_status_shape(env):
    client, state = env
    seed_channel(state.db, state.paths)
    seed_output(state.db, state.paths)
    body = client.get("/api/status").json()
    assert set(body["engine"]) >= {"state", "detail", "elapsed_s", "vram_owner"}
    assert body["engine"]["state"] == "idle" and body["outbox"]["pending"] == 1
    assert body["channels"][0]["slug"] == "after-dark"
    assert set(body["disk"]) == {"free_gb", "total_gb", "home"}
    assert isinstance(body["services"], dict) and "next_run" in body


def test_status_passes_the_supervisor_gpu_block_through(tmp_home):
    with FakeSupervisor() as sup:
        client, _ = make_env(tmp_home, SupervisorLink(sup.url, timeout_s=2))
        assert client.get("/api/status").json()["gpu"] is None
        gpu = {"name": "RTX 4080", "load_pct": 41.0, "vram_used_mb": 9000, "vram_total_mb": 16376, "temp_c": 58.0}
        sup.state = {"gpu_owner": None, "services": {}, "gpu": gpu}
        assert client.get("/api/status").json()["gpu"] == gpu
        sup.state = {"gpu_owner": None, "services": {}, "gpu": {"load_pct": 3}}  # no name: malformed
        assert client.get("/api/status").json()["gpu"] is None


def test_route_table_matches_the_contract(env):
    client, _ = env
    paths = {getattr(r, "path", "") for r in client.app.routes}  # type: ignore[attr-defined]
    expected = {
        "/api/status", "/api/events", "/api/engine/warmup", "/api/engine/shutdown", "/api/channels",
        "/api/channels/{slug}", "/api/channels/{slug}/pause", "/api/channels/{slug}/resume",
        "/api/channels/{slug}/validate", "/api/outputs", "/api/outputs/{output_id:path}",
        "/api/outputs/{output_id:path}/approve", "/api/outputs/{output_id:path}/reject",
        "/api/outputs/{output_id:path}/uploaded", "/api/media/{output_id:path}",
        "/api/media/{output_id:path}/thumb.jpg", "/api/jobs/run", "/api/jobs/clip", "/api/jobs",
        "/api/jobs/{job_id}", "/api/jobs/{job_id}/cancel", "/api/runs", "/api/logs/tail",
        "/api/learn/import", "/api/learn/summary", "/api/settings", "/api/settings/keys",
        "/api/diagnostics", "/api/templates", "/api/voices", "/api/voices/preview", "/api/uploads",
    }
    assert expected <= paths


# ------------------------------------------------------------------ outputs and media


def clip_file(tmp_home: Path) -> Path:
    if ffmpeg.available():
        return ffmpeg.make_test_clip(tmp_home / "fixtures" / "c.mp4", width=320, height=180, seconds=2.0)
    f = tmp_home / "fixtures" / "c.mp4"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(bytes(i % 251 for i in range(20000)))
    return f


def test_review_flow_over_http(env, tmp_home: Path):
    client, state = env
    oid = seed_output(state.db, state.paths, video=clip_file(tmp_home), title="The Doorbell Camera")
    listed = client.get("/api/outputs", params={"status": "pending"}).json()
    assert isinstance(listed, list) and listed[0]["output_id"] == oid and listed[0]["kind"] == "short"
    detail = client.get(f"/api/outputs/{oid}").json()
    assert detail["title"] == "The Doorbell Camera" and detail["tags"] and detail["policy_notes"] == []
    assert client.post(f"/api/outputs/{oid}/reject", json={}).status_code == 400
    r = client.post(f"/api/outputs/{oid}/reject", json={"reason": "Too slow"})
    assert r.json()["status"] == "rejected" and r.json()["review_note"] == "Too slow"
    assert client.post(f"/api/outputs/{oid}/approve").json()["status"] == "approved"
    bad = client.post(f"/api/outputs/{oid}/uploaded", json={"url": "nope"})
    assert bad.status_code == 400 and err(bad)["code"] == "bad_url"
    done = client.post(f"/api/outputs/{oid}/uploaded", json={"url": "https://youtu.be/abc"})
    assert done.json()["status"] == "uploaded"
    assert client.post(f"/api/outputs/{oid}/open-folder").json()["ok"] is True
    assert client.get("/api/outputs", params={"status": "uploaded"}).json()[0]["output_id"] == oid
    assert client.get("/api/outputs/nope/s1").status_code == 404
    assert client.get("/api/outputs", params={"status": "bogus"}).status_code == 422


def test_media_range_streaming(env, tmp_home: Path):
    client, state = env
    src = clip_file(tmp_home)
    data = src.read_bytes()
    oid = seed_output(state.db, state.paths, video=src)
    url = f"/api/media/{oid}"

    full = client.get(url)
    assert full.status_code == 200 and full.content == data
    assert full.headers["accept-ranges"] == "bytes" and full.headers["content-type"] == "video/mp4"
    assert int(full.headers["content-length"]) == len(data)

    part = client.get(url, headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and part.content == data[:100]
    assert part.headers["content-range"] == f"bytes 0-99/{len(data)}"
    assert part.headers["content-length"] == "100"

    mid = client.get(url, headers={"Range": "bytes=100-199"})
    assert mid.status_code == 206 and mid.content == data[100:200]
    open_end = client.get(url, headers={"Range": "bytes=150-"})
    assert open_end.status_code == 206 and open_end.content == data[150:]
    suffix = client.get(url, headers={"Range": "bytes=-10"})
    assert suffix.status_code == 206 and suffix.content == data[-10:]

    bad = client.get(url, headers={"Range": f"bytes={len(data) + 50}-"})
    assert bad.status_code == 416 and bad.headers["content-range"] == f"bytes */{len(data)}"
    assert client.get(url, headers={"Range": "bytes=oops"}).status_code == 200

    head = client.head(url)
    assert head.status_code == 200 and head.content == b""
    assert int(head.headers["content-length"]) == len(data)
    assert client.get("/api/media/no/such").status_code == 404


@needs_ffmpeg
def test_thumbnail_is_generated_and_cached(env, tmp_home: Path):
    client, state = env
    oid = seed_output(state.db, state.paths, video=clip_file(tmp_home))
    r = client.get(f"/api/media/{oid}/thumb.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content[:3] == b"\xff\xd8\xff"
    thumbs = list((state.paths.home / "cache" / "dashboard" / "thumbs").glob("*.jpg"))
    assert len(thumbs) == 1
    mtime = thumbs[0].stat().st_mtime_ns
    again = client.get(f"/api/media/{oid}/thumb.jpg")
    assert again.content == r.content and thumbs[0].stat().st_mtime_ns == mtime


# ------------------------------------------------------------------ channels


def test_channels_over_http(env):
    client, _ = env
    body = {"slug": "night-owl", "display_name": "Night Owl", "niche": "cosy mysteries"}
    r = client.post("/api/channels", json=body)
    assert r.status_code == 201 and r.json()["profile"]["slug"] == "night-owl"
    assert client.post("/api/channels", json=body).status_code == 409
    bad = client.post("/api/channels", json={**body, "slug": "Bad Slug"})
    assert bad.status_code == 422 and err(bad)["code"] == "invalid_profile" and err(bad)["details"]
    cards = client.get("/api/channels").json()
    assert isinstance(cards, list) and cards[0]["slug"] == "night-owl"
    assert client.get("/api/channels/night-owl").json()["profile"]["niche"] == "cosy mysteries"
    put = client.put("/api/channels/night-owl", json={**body, "niche": "cosy crime"})
    assert put.json()["profile"]["niche"] == "cosy crime"
    paused = client.post("/api/channels/night-owl/pause", json={"note": "warning"})
    assert paused.json()["profile"]["status"] == "paused"
    assert client.post("/api/channels/night-owl/resume").json()["profile"]["status"] == "active"
    v = client.post("/api/channels/night-owl/validate", json={**body, "critic_bar": 1}).json()
    assert v["ok"] is False and v["errors"] == ["Must be at least 5.0."]
    saved = client.post("/api/channels/night-owl/validate").json()
    assert saved["ok"] is True and isinstance(saved["warnings"], list)
    assert client.get("/api/channels/ghost").status_code == 404
    broken = client.post("/api/channels", content=b"{not json", headers={"content-type": "application/json"})
    assert broken.status_code == 400


OLD_VOICE = {"mode": "design", "speaker": None, "description": "warm", "name": "x" * 57}


def test_channel_routes_apply_the_voice_name_rule_to_names_typed_now(env):
    """D-24: the API (and the assistant tools that call it) refuse a name the voice library would refuse."""
    client, state = env
    body = {"slug": "night-owl", "display_name": "Night Owl", "niche": "cosy mysteries"}
    bad = client.post("/api/channels", json={**body, "voice": {**OLD_VOICE, "name": "Night Narrator"}})
    assert bad.status_code == 422 and err(bad)["code"] == "invalid_profile"
    assert any(d["field"] == "voice.name" and "1 to 40 letters" in d["message"] for d in err(bad)["details"])
    assert not state.paths.channel_file("night-owl").exists()
    ok = client.post("/api/channels", json={**body, "voice": {**OLD_VOICE, "name": "Night-Narrator"}})
    assert ok.status_code == 201


def test_channel_routes_keep_working_on_a_stored_name_the_rule_refuses(env):
    client, state = env
    seed_channel(state.db, state.paths, "old-timer", voice=OLD_VOICE)
    saved = client.get("/api/channels/old-timer").json()["profile"]
    same = client.put("/api/channels/old-timer", json={**saved, "niche": "calmer stories"})
    assert same.status_code == 200 and same.json()["profile"]["voice"]["name"] == "x" * 57
    assert client.post("/api/channels/old-timer/validate").json()["ok"] is True
    typed = client.put("/api/channels/old-timer", json={**saved, "voice": {**OLD_VOICE, "name": "a b"}})
    assert typed.status_code == 422
    assert client.get("/api/channels/old-timer").json()["profile"]["voice"]["name"] == "x" * 57


# ------------------------------------------------------------------ jobs


def test_run_and_clip_jobs_over_http(env, tmp_home: Path):
    client, state = env
    seed_channel(state.db, state.paths)
    r = client.post("/api/jobs/run", json={"channel": "after-dark", "format": "shorts_only"})
    assert r.status_code == 201
    job_id = r.json()["job_id"]
    state.worker.run_next()
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "done" and job["progress"] == 100.0 and len(job["result"]["output_ids"]) == 1
    assert client.post(f"/api/jobs/{job_id}/cancel").status_code == 409

    video = clip_file(tmp_home)
    no_rights = client.post("/api/jobs/clip", json={"path": str(video), "channel": "after-dark"})
    assert no_rights.status_code == 400 and err(no_rights)["code"] == "rights_required"
    ok = client.post(
        "/api/jobs/clip",
        json={"path": str(video), "channel": "after-dark", "template": "auto", "max_clips": 2,
              "min_s": 20, "max_s": 60, "rights_confirmed": True},
    )
    assert ok.status_code == 201
    clip_job = ok.json()["job_id"]
    jobs = client.get("/api/jobs").json()
    assert isinstance(jobs, list) and jobs[0]["job_id"] == clip_job and jobs[0]["status"] == "queued"
    state.worker.run_next()
    final = client.get("/api/jobs", params={"kind": "clip"}).json()[0]
    assert final["status"] == "done" and final["stage"] is None and final["progress"] == 100.0
    clips = client.get("/api/outputs", params={"status": "pending", "kind": "clip"}).json()
    assert {c["output_id"] for c in clips} == set(final["result"]["output_ids"])
    run_body = {"channel": "after-dark", "format": "shorts_only"}
    cancel_target = client.post("/api/jobs/run", json=run_body).json()
    assert client.post(f"/api/jobs/{cancel_target['job_id']}/cancel").json()["status"] == "cancelled"
    assert client.get("/api/jobs/nope-20260101-abcd").status_code == 404
    assert client.post("/api/jobs/verify", json={}).status_code == 201


@needs_multipart
def test_clip_job_multipart_upload(env):
    client, state = env
    seed_channel(state.db, state.paths)
    data = {"channel": "after-dark", "template": "auto", "max_clips": "3", "min_s": "20", "max_s": "60",
            "caption_style": "punch_yellow", "rights_confirmed": "true"}
    files = {"file": ("My VOD.mp4", b"not really a video" * 50, "video/mp4")}
    r = client.post("/api/jobs/clip", data=data, files=files)
    assert r.status_code == 201
    params = r.json()["params"]
    assert params["max_clips"] == 3 and params["rights_confirmed"] is True
    assert Path(params["source_path"]).read_bytes() == b"not really a video" * 50
    refused = client.post("/api/jobs/clip", data={**data, "rights_confirmed": "false"}, files=files)
    assert refused.status_code == 400 and err(refused)["code"] == "rights_required"


@needs_multipart
def test_learn_import_multipart(env):
    client, _ = env
    csv = b"title,views\nA,10\nB,20\n"
    r = client.post("/api/learn/import", files={"file": ("export.csv", csv, "text/csv")})
    assert r.status_code == 200
    body = r.json()
    assert body["matched"] == 2 and body["unmatched"] == 0 and isinstance(body["summary"], list)
    assert body["changes"] == []
    not_csv = client.post("/api/learn/import", files={"file": ("notes.txt", b"x", "text/plain")})
    assert not_csv.status_code == 400
    summary = client.get("/api/learn/summary").json()
    assert "pools" in summary and "unmatched_uploads" in summary


def test_chunked_upload_over_http(env):
    client, state = env
    payload = bytes(range(256)) * 8
    start = client.post("/api/uploads", json={"filename": "talk.mp4", "size": len(payload)})
    assert start.status_code == 201 and start.json()["chunk_size"] == 8 * 1024 * 1024
    up = start.json()["upload_id"]
    first = client.put(f"/api/uploads/{up}", params={"offset": 0}, content=payload[:1000])
    assert first.json()["offset"] == 1000
    clash = client.put(f"/api/uploads/{up}", params={"offset": 0}, content=payload[:1000])
    assert clash.status_code == 409 and err(clash)["details"] == {"offset": 1000}
    assert client.get(f"/api/uploads/{up}").json()["offset"] == 1000
    client.put(f"/api/uploads/{up}", params={"offset": 1000}, content=payload[1000:])
    done = client.post(f"/api/uploads/{up}/complete").json()
    assert done["status"] == "complete" and done["sha256"] == hashlib.sha256(payload).hexdigest()
    assert Path(done["path"]).read_bytes() == payload
    assert client.post("/api/uploads", json={"filename": "evil.exe", "size": 5}).status_code == 400
    assert client.put("/api/uploads/not-an-id", params={"offset": 0}, content=b"x").status_code == 404


# ------------------------------------------------------------------ settings, tools, lists


def test_settings_keys_diagnostics(env):
    client, state = env
    s = client.get("/api/settings").json()
    assert s["schedule_time"] == "03:00" and s["keys"] == {"pexels": False, "pixabay": False}
    put = client.put("/api/settings", json={"schedule_time": "05:00", "notifications": False}).json()
    assert put["schedule_time"] == "05:00" and put["notifications"] is False
    assert client.put("/api/settings", json={"schedule_time": "5pm"}).status_code == 422
    assert client.put("/api/settings", json={"flags": {}}).status_code == 409
    key = client.post("/api/settings/keys", json={"name": "pexels", "value": "TOP-SECRET-VALUE"})
    assert key.json() == {"name": "pexels_api_key", "set": True} and "TOP-SECRET" not in key.text
    assert client.get("/api/settings").json()["keys"]["pexels"] is True
    assert client.get("/api/diagnostics/latest").status_code == 404
    diag = client.post("/api/diagnostics").json()
    assert Path(diag["path"]).is_file()
    zipped = client.get("/api/diagnostics/latest")
    assert zipped.status_code == 200 and zipped.headers["content-type"] == "application/zip"
    assert zipped.content[:2] == b"PK"


def test_templates_voices_logs_runs(env):
    client, state = env
    templates = client.get("/api/templates").json()
    assert isinstance(templates, list) and {"id", "label", "description"} <= set(templates[0])
    voices = client.get("/api/voices").json()
    assert isinstance(voices, list) and voices[0]["name"] == "Ryan"
    wav = client.post("/api/voices/preview", json={"voice": "Ryan", "text": "Hello from Nightshift."})
    assert wav.status_code == 200 and wav.headers["content-type"] == "audio/wav"
    assert wav.content[:4] == b"RIFF"
    assert client.post("/api/voices/preview", json={}).status_code == 400
    state.log.warn("probe_event", "from the test")
    lines = client.get("/api/logs/tail", params={"n": 50, "level": "WARN"}).json()
    assert isinstance(lines, list) and any(r["event"] == "probe_event" for r in lines)
    assert "dashboard" in client.get("/api/logs/components").json()
    assert client.get("/api/runs").json() == []
    assert client.get("/api/templates/nope/preview.png").status_code == 404  # the route is gone


def test_events_stream(env):
    client, state = env
    r = client.get("/api/events", params={"limit": 2})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    frames = [json.loads(chunk[len("data: "):]) for chunk in r.text.strip().split("\n\n")]
    assert [f["kind"] for f in frames] == ["status", "status"]
    assert "engine" in frames[0]["payload"] and "outbox" in frames[0]["payload"]


def test_engine_commands_over_http(tmp_home: Path):
    with FakeSupervisor() as sup:
        sup.state = {"gpu_owner": "studio:j", "services": {"llama": {"state": "ready"}}}
        client, state = make_env(tmp_home, SupervisorLink(sup.url))
        assert client.post("/api/engine/warmup", json={"service": "llama"}).json() == {"state": "starting"}
        st = client.get("/api/status").json()
        assert st["engine"]["state"] == "ready" and st["engine"]["vram_owner"] == "studio:j"
        assert st["services"]["llama"]["state"] == "ready"
        later: list[Any] = []
        client.app.state.dashboard.engine.schedule = lambda _d, fn: later.append(fn)  # type: ignore[attr-defined]
        r = client.post("/api/engine/shutdown", json={"grace_s": 30})
        assert r.status_code == 200 and r.json() == {"ok": True, "cancelled_job": None}
        assert state.draining is True
        assert not any(p == "/shutdown" for _m, p, _b in sup.calls)  # the reply came first
        assert client.get("/api/status").json()["engine"]["detail"] == "Shutting down..."
        assert len(later) == 1
        later[0]()
        assert any(p == "/shutdown" for _m, p, _b in sup.calls)


def test_shutdown_route_answers_before_the_link_shutdown_runs(tmp_home: Path):
    """D-7: the supervisor stops the dashboard when it shuts down, so the reply must be out first."""
    order: list[str] = []

    class OrderedLink(SupervisorLink):
        def status(self) -> dict[str, Any] | None:
            return {"services": {}, "gpu_owner": None}

        def shutdown(self, grace_s: int = 30) -> dict[str, Any]:
            order.append("link.shutdown")
            raise SupervisorUnavailable("connection reset")  # failures never reach the caller

    done = threading.Event()
    link = OrderedLink("http://127.0.0.1:9", timeout_s=0.2)
    client, _ = make_env(tmp_home, link)
    engine = client.app.state.dashboard.engine  # type: ignore[attr-defined]
    gate = threading.Event()  # opened once the test has seen the response

    def background(_delay: float, fn: Any) -> None:
        def run() -> None:
            gate.wait(5.0)
            fn()
            done.set()

        threading.Thread(target=run, daemon=True).start()

    engine.schedule = background
    r = client.post("/api/engine/shutdown", json={"grace_s": 30})
    order.append("response")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert order == ["response"]  # the 200 and its body arrived before the supervisor call
    gate.set()
    assert done.wait(5.0)  # and the engine is still stopped afterwards
    assert order == ["response", "link.shutdown"]


def test_engine_commands_when_engine_is_off(env):
    client, _ = env
    r = client.post("/api/engine/warmup", json={})
    assert r.status_code == 503 and err(r)["code"] == "engine_offline"
    assert client.post("/api/engine/shutdown", json={}).status_code == 503


def test_every_api_path_used_by_app_js_has_a_route(env):
    import re

    js = Path(__file__).resolve().parents[1] / "ns_dashboard" / "static" / "app.js"
    if not js.exists():
        pytest.skip("ns_dashboard/static/app.js not present on this branch")
    client, _ = env
    routes = client.app.routes  # type: ignore[attr-defined]
    prefixes = {getattr(r, "path", "").split("{", 1)[0] for r in routes}
    used = set(re.findall(r"""['"`](/api/[A-Za-z0-9_\-./]*)""", js.read_text(encoding="utf-8")))
    missing = sorted(u for u in used if not any(u.startswith(p) or p.startswith(u) for p in prefixes if p))
    assert not missing, f"app.js calls API paths without a route: {missing}"


# ------------------------------------------------------------------ tool registry hygiene, library, update, assistant


def test_every_route_has_a_tool_descriptor_or_a_reason(env):
    """A new `/api` route without a descriptor in assistant/tools.py (or an entry in NO_TOOL) fails CI."""
    from fastapi.routing import APIRoute

    client, _ = env
    actual = {
        (m, r.path)
        for r in client.app.routes  # type: ignore[attr-defined]
        if isinstance(r, APIRoute)
        for m in r.methods
        if m not in ("HEAD", "OPTIONS")
    }
    declared = set(build_registry().routes()) | set(NO_TOOL)
    assert actual - declared == set(), f"routes without a tool descriptor or NO_TOOL reason: {sorted(actual - declared)}"
    assert declared - actual == set(), f"descriptors for routes that do not exist: {sorted(declared - actual)}"


def test_library_endpoints(env):
    client, state = env
    seed_channel(state.db, state.paths)
    made = client.post("/api/library/ideas", json={"channel": "after-dark", "premise": "A doorbell films tomorrow night."})
    assert made.status_code == 201 and made.json()["status"] == "new"
    idea_id = made.json()["idea_id"]
    assert client.get("/api/library/ideas", params={"channel": "after-dark"}).json()[0]["idea_id"] == idea_id
    put = client.put(f"/api/library/ideas/{idea_id}", json={"status": "approved"})
    assert put.json()["status"] == "approved"
    assert client.put(f"/api/library/ideas/{idea_id}", json={"status": "maybe"}).status_code == 422
    assert client.put("/api/library/ideas/abc", json={"status": "new"}).status_code == 404
    assert client.get("/api/library/ideas", params={"status": "approved"}).json()[0]["idea_id"] == idea_id
    assert client.get("/api/library/stories", params={"q": "nothing"}).json() == []
    assert client.get("/api/library/footage").json() == []
    fmt = {"name": "Square test", "kind": "square", "canvas": {"w": 1080, "h": 1080, "fps": 30},
           "duration": {"min_s": 5, "max_s": 30}, "captions": {},
           "layout": {"regions": [{"name": "m", "x": 0, "y": 0, "w": 1080, "h": 1080, "source": "full"}]}}
    created = client.post("/api/library/formats", json={"id": "square_test", "definition": fmt})
    assert created.status_code == 201 and created.json()["id"] == "square_test"
    assert any(f["id"] == "square_test" for f in client.get("/api/library/formats").json())
    upd = client.put("/api/library/formats/square_test", json={"changes": {"duration": {"max_s": 45}}})
    assert upd.json()["definition"]["duration"]["max_s"] == 45
    bad = client.put("/api/library/formats/square_test", json={"changes": {"kind": "poster"}})
    assert bad.status_code == 422 and err(bad)["code"] == "invalid_format"


def test_update_endpoints(env):
    client, state = env
    chk = client.get("/api/update/check").json()
    assert chk["update_available"] is True and chk["latest"] == "0.2.0"
    assert client.post("/api/update/apply").json()["ok"] is True
    assert state.adapters.update.applied == 1  # type: ignore[attr-defined]


def sse_events(resp: Any) -> list[dict[str, Any]]:
    return [json.loads(chunk[len("data: "):]) for chunk in resp.text.strip().split("\n\n") if chunk.startswith("data: ")]


def test_assistant_chat_confirm_and_history_over_http(tmp_home: Path):
    llm = ScriptedToolLlm([
        call("list_queue", {"status": "pending"}), say("One video is waiting."),
        call("pause_channel", {"slug": "after-dark"}), say("Please confirm."),
    ])
    client, state = make_env(tmp_home, llm=llm)
    seed_channel(state.db, state.paths)
    seed_output(state.db, state.paths)
    r = client.post("/api/assistant/chat", json={"message": "what is waiting?", "context": {"page": "Queue"}})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = sse_events(r)
    assert [e["kind"] for e in events] == ["conversation", "tool_call", "tool_result", "message", "done"]
    cid = events[0]["payload"]["conversation_id"]

    r2 = client.post("/api/assistant/chat", json={"message": "pause after-dark", "conversation_id": cid})
    ev2 = sse_events(r2)
    card = next(e["payload"] for e in ev2 if e["kind"] == "confirm")
    assert card["tool"] == "pause_channel" and "Pause channel after-dark" in card["text"]
    assert client.get("/api/channels/after-dark").json()["profile"]["status"] == "active"  # not yet
    done = client.post("/api/assistant/confirm", json={"action_id": card["action_id"]})
    assert done.status_code == 200 and done.json()["status"] == "done"
    assert client.get("/api/channels/after-dark").json()["profile"]["status"] == "paused"
    again = client.post("/api/assistant/confirm", json={"action_id": card["action_id"]})
    assert again.status_code == 409 and err(again)["code"] == "already_decided"
    assert client.post("/api/assistant/dismiss", json={"action_id": "nope"}).status_code == 404

    hist = client.get("/api/assistant/history", params={"conversation_id": cid}).json()
    assert hist["messages"][0]["role"] == "user" and hist["pending_actions"] == []
    assert client.get("/api/assistant/history", params={"conversation_id": "zzz"}).status_code == 404
    convs = client.get("/api/assistant/conversations").json()
    assert convs[0]["conversation_id"] == cid
    assert client.post("/api/assistant/queue/run").json() == []


def test_assistant_busy_engine_over_http(tmp_home: Path):
    client, state = make_env(tmp_home)
    client.app.state.dashboard.assistant.provider.busy_owner = "studio:x"  # type: ignore[attr-defined]
    events = sse_events(client.post("/api/assistant/chat", json={"message": "hello"}))
    assert [e["kind"] for e in events] == ["conversation", "busy", "done"]
    assert events[1]["payload"]["queued"] is True
