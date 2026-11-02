import io
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from ns_common.config import load_channel, load_runtime, save_runtime
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_dashboard.errors import AdapterUnavailable, ApiError
from ns_dashboard.fakes import FakeSupervisor, seed_channel, seed_output
from ns_dashboard.handlers import Dashboard, build_state
from ns_dashboard.state import AppState
from ns_dashboard.supervisor_link import SupervisorLink
from ns_dashboard.svc_jobs import learn_response
from ns_dashboard.uploads import UploadManager

NOON = datetime(2026, 11, 1, 12, 0)


def make(tmp_home: Path, link: SupervisorLink | None = None) -> tuple[Dashboard, AppState]:
    paths = Paths.from_env()
    state = build_state(
        paths, fake_services=True, link=link or SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    )
    return Dashboard(state, clock=lambda: NOON), state


def code_of(fn: Any, *args: Any, **kw: Any) -> str:
    with pytest.raises(ApiError) as ei:
        fn(*args, **kw)
    return ei.value.code


# ------------------------------------------------------------------ status / engine


def test_status_when_engine_is_off(tmp_home: Path):
    dash, _ = make(tmp_home)
    st = dash.engine.status()
    assert st["engine"]["state"] == "idle" and st["engine"]["reachable"] is False
    assert st["engine"]["detail"] == "Engine not running" and st["engine"]["elapsed_s"] is None
    assert st["outbox"] == {"pending": 0, "approved": 0, "rejected": 0, "uploaded": 0}
    assert st["channels"] == [] and st["next_run"] is None and st["services"] == {}
    assert set(st["disk"]) == {"free_gb", "total_gb", "home"} and st["disk"]["home"] == str(tmp_home)
    assert st["last_error"] is None and st["job"] is None


def test_status_channels_schedule_and_outbox(tmp_home: Path):
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths, "after-dark", masters_per_week=2)
    seed_channel(state.db, state.paths, "paused-one", status="paused")
    seed_output(state.db, state.paths, channel="after-dark")
    seed_output(state.db, state.paths, channel="after-dark", status="approved", k=2)
    st = dash.engine.status()
    assert st["next_run"] == "2026-11-02T03:00" and st["next_run_in_s"] == 15 * 3600
    assert st["outbox"]["pending"] == 1 and st["outbox"]["approved"] == 1
    cards = {c["slug"]: c for c in st["channels"]}
    assert set(cards["after-dark"]) >= {
        "slug", "display_name", "status", "niche", "voice", "masters_per_week", "publish_window",
    }
    assert cards["after-dark"]["masters_per_week"] == 2 and cards["after-dark"]["pending"] == 1
    assert cards["paused-one"]["tonight"].startswith("Paused")
    dash.settings.put_settings({"schedule_time": "13:30"})
    assert dash.engine.status()["next_run"] == "2026-11-01T13:30"


def test_engine_state_machine_against_fake_supervisor(tmp_home: Path):
    with FakeSupervisor() as sup:
        dash, state = make(tmp_home, SupervisorLink(sup.url))
        clock = {"t": 100.0}
        dash.engine.monotonic = lambda: clock["t"]

        sup.state = {"gpu_owner": None, "services": {}}
        e = dash.engine.engine()
        assert (e["state"], e["detail"], e["reachable"]) == ("idle", "Services idle", True)

        sup.state = {"status": "booting"}
        assert dash.engine.engine()["state"] == "starting"

        sup.state = {"gpu_owner": None, "services": {"llama": {"state": "starting"}}}
        assert dash.engine.engine()["state"] == "loading"
        clock["t"] = 107.0
        e = dash.engine.engine()
        assert e["state"] == "loading" and e["elapsed_s"] == 7 and "language model" in e["detail"]

        sup.state = {"gpu_owner": "studio:j1", "services": {"llama": {"state": "ready"}}}
        e = dash.engine.engine()
        assert (e["state"], e["vram_owner"], e["elapsed_s"]) == ("ready", "studio:j1", None)

        rec = state.store.create("clip", "c", {})
        state.store.mark_running(rec.job_id)
        state.store.update_progress(rec.job_id, 0.4, "select (4/9)")
        e = dash.engine.engine()
        assert e["state"] == "working" and e["detail"] == "select (4/9)" and e["progress"] == 40.0

        state.draining = True
        assert dash.engine.engine()["detail"] == "Shutting down..."
        assert dash.engine.status()["services"]["llama"]["state"] == "ready"


def test_last_error_clears_after_a_success(tmp_home: Path):
    dash, state = make(tmp_home)
    a = state.store.create("verify", None, {})
    state.store.mark_running(a.job_id)
    state.store.finish(a.job_id, "failed", "RuntimeError: kaput")
    err = dash.engine.status()["last_error"]
    assert err is not None and err["message"] == "RuntimeError: kaput"
    b = state.store.create("verify", None, {})
    state.store.mark_running(b.job_id)
    state.store.finish(b.job_id, "done", "Done", {})
    assert dash.engine.status()["last_error"] is None


def test_engine_commands(tmp_home: Path):
    with FakeSupervisor() as sup:
        dash, state = make(tmp_home, SupervisorLink(sup.url))
        assert dash.engine.warmup(None) == {"state": "starting"}
        assert code_of(dash.engine.warmup, "bogus") == "unknown_service"
        seed_channel(state.db, state.paths)
        job = dash.jobs.create_run({"channel": "after-dark", "format": "shorts_only"})
        state.store.mark_running(job["job_id"])
        out = dash.engine.shutdown(10)
        assert out == {"ok": True, "cancelled_job": job["job_id"]}
        assert state.draining and state.store.is_cancel_requested(job["job_id"])
        assert ("/shutdown", {"grace_s": 10}) in [(p, b) for m, p, b in sup.calls if m == "POST"]
        run = {"channel": "after-dark", "format": "shorts_only"}
        assert code_of(dash.jobs.create_run, run) == "shutting_down"
        assert dash.engine.stop_all()["ok"] is True


def test_shutdown_with_engine_offline_keeps_accepting(tmp_home: Path):
    dash, state = make(tmp_home)
    assert code_of(dash.engine.shutdown, 30) == "engine_offline"
    assert state.draining is False
    assert code_of(dash.engine.shutdown, 999) == "invalid_options"


def test_events_stream_frames(tmp_home: Path):
    dash, state = make(tmp_home)
    stream = dash.engine.events(limit=3)  # subscribes immediately
    state.hub.publish("dashboard_job", {"job_id": "j1", "progress": 50})
    frames = list(stream)
    kinds = [json.loads(f.split("data: ", 1)[1]) for f in frames]
    assert [k["kind"] for k in kinds] == ["status", "job", "status"]
    assert "engine" in kinds[0]["payload"] and kinds[1]["payload"]["job_id"] == "j1"


def test_runs_rows(tmp_home: Path):
    dash, state = make(tmp_home)
    state.db.execute(
        "INSERT INTO runs(run_id, started_at, finished_at, mode, summary_json) VALUES (?, ?, ?, ?, ?)",
        ("r1", "2026-11-01T03:00:00+00:00", "2026-11-01T03:30:00+00:00", "daily",
         '{"jobs": 2, "rejected": 0}'),
    )
    state.db.execute(
        "INSERT INTO runs(run_id, started_at, mode) VALUES ('r2', '2026-11-02T03:00:00+00:00', 'manual')"
    )
    runs = dash.engine.runs()
    assert [r["run_id"] for r in runs] == ["r2", "r1"]
    assert runs[1]["duration_s"] == 1800.0 and runs[1]["summary"] == {"jobs": 2, "rejected": 0}
    assert runs[0]["duration_s"] is None and runs[0]["summary"] == {}
    assert dash.engine.status()["last_run"]["run_id"] == "r2"


# ------------------------------------------------------------------ outputs


def test_outputs_list_filters_and_detail(tmp_home: Path):
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)  # stories.channel references channels(slug)
    a = seed_output(state.db, state.paths, kind="short", k=1, job_id="after-dark-20261101-aaaa")
    b = seed_output(state.db, state.paths, kind="clip", k=1, status="approved", source_id=None)
    items = dash.outputs.list_outputs()
    assert isinstance(items, list) and {i["output_id"] for i in items} == {a, b}
    assert [i["output_id"] for i in dash.outputs.list_outputs(status="pending")] == [a]
    assert [i["output_id"] for i in dash.outputs.list_outputs(kind="clip")] == [b]
    assert dash.outputs.list_outputs(channel="nobody") == []
    assert code_of(dash.outputs.list_outputs, status="weird") == "bad_filter"
    first = next(i for i in items if i["output_id"] == a)
    assert first["media_url"] == f"/api/media/{a}" and first["thumb_url"] == f"/api/media/{a}/thumb.jpg"
    assert {"output_id", "kind", "k", "title", "channel", "duration_s", "created_at", "status"} <= set(first)

    job_dir = state.paths.job_dir("after-dark-20261101-aaaa")
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "policy_final.json").write_text(
        json.dumps(
            {
                "stage": "final",
                "passed": True,
                "checks": {
                    "misleading_claims": {"passed": False, "detail": "says true story"},
                    "metadata_spam": {"passed": True, "detail": ""},
                },
                "advice": {"ai_disclosure": "yes", "made_for_kids": False, "age_restrict_suggested": True},
            }
        ),
        encoding="utf-8",
    )
    state.db.execute(
        "INSERT INTO stories(job_id, channel, format, text, word_count, opening_line, created_at)"
        " VALUES ('after-dark-20261101-aaaa', 'after-dark', 'shorts_only', 'Once upon a night.', 4, 'Once',"
        " 't')"
    )
    d = dash.outputs.detail(a)
    assert d["description"].startswith("Seeded short 1") and d["tags"] == ["story", "fiction"]
    assert d["hashtags"] == ["#Shorts"] and d["sheet_text"].startswith("Seeded short 1")
    assert d["ai_disclosure"] == "yes" and d["made_for_kids"] is False and d["age_restrict_suggested"] is True
    assert d["policy_notes"] == ["Problem: misleading claims - says true story", "OK: metadata spam"]
    assert d["story_excerpt"] == "Once upon a night."
    assert d["publish_after"] is None and d["chapters"] is None and d["upload"] is None
    assert d["file_exists"] is True and d["folder"].endswith("shorts")
    assert code_of(dash.outputs.detail, "nope/s9") == "not_found"


def test_clip_detail_has_transcript_excerpt(tmp_home: Path):
    dash, state = make(tmp_home)
    src = state.paths.home / "imports" / "clipper" / "src-20261101-aaaa"
    src.mkdir(parents=True)
    segs = [{"start": 0, "end": 5, "text": "hello there"}, {"start": 5, "end": 9, "text": "general kenobi"}]
    (src / "transcript.json").write_text(json.dumps({"segments": segs}), encoding="utf-8")
    state.db.execute(
        "INSERT INTO sources(source_id, path, imported_at) VALUES ('src-20261101-aaaa', 'x', 't')"
    )
    oid = seed_output(state.db, state.paths, kind="clip", source_id="src-20261101-aaaa")
    assert dash.outputs.detail(oid)["transcript_excerpt"] == "hello there general kenobi"


def test_review_actions(tmp_home: Path):
    dash, state = make(tmp_home)
    oid = seed_output(state.db, state.paths)
    assert dash.outputs.approve(oid)["status"] == "approved"
    assert code_of(dash.outputs.reject, oid, "  ") == "reason_required"
    rej = dash.outputs.reject(oid, "Hook is boring")
    assert rej["status"] == "rejected" and rej["review_note"] == "Hook is boring"
    assert code_of(dash.outputs.mark_uploaded, oid, "https://youtu.be/x") == "rejected"
    assert dash.outputs.approve(oid)["status"] == "approved"
    assert code_of(dash.outputs.mark_uploaded, oid, "not a url") == "bad_url"
    up = dash.outputs.mark_uploaded(oid, " https://youtu.be/abc123 ")
    assert up["status"] == "uploaded"
    row = state.db.one("SELECT video_url FROM uploads WHERE output_id = ?", (oid,))
    assert row is not None and row["video_url"] == "https://youtu.be/abc123"
    assert dash.outputs.detail(oid)["upload"]["video_url"] == "https://youtu.be/abc123"
    assert code_of(dash.outputs.approve, oid) == "already_uploaded"
    assert code_of(dash.outputs.reject, oid, "late") == "already_uploaded"
    assert code_of(dash.outputs.approve, "no/such") == "not_found"
    assert dash.outputs.counts()["uploaded"] == 1


def test_open_folder_uses_the_adapter(tmp_home: Path):
    dash, state = make(tmp_home)
    oid = seed_output(state.db, state.paths)
    out = dash.outputs.open_folder(oid)
    assert out["ok"] is True
    assert state.adapters.folder.opened == [Path(out["folder"])]  # type: ignore[attr-defined]
    gone = seed_output(state.db, state.paths, k=2, with_files=False)
    gone_path = str(tmp_home / "gone" / "x.mp4")
    state.db.execute("UPDATE outputs SET path = ? WHERE output_id = ?", (gone_path, gone))
    assert code_of(dash.outputs.open_folder, gone) == "folder_missing"


def test_media_path_rules(tmp_home: Path, tmp_path: Path):
    dash, state = make(tmp_home)
    ok_id = seed_output(state.db, state.paths)
    assert dash.outputs.media_path(ok_id).is_file()
    outside = tmp_path / "elsewhere.mp4"
    outside.write_bytes(b"x")
    evil = seed_output(state.db, state.paths, k=2, with_files=False)
    state.db.execute("UPDATE outputs SET path = ? WHERE output_id = ?", (str(outside), evil))
    assert code_of(dash.outputs.media_path, evil) == "forbidden"
    missing = seed_output(state.db, state.paths, k=3, with_files=False)
    assert code_of(dash.outputs.media_path, missing) == "file_missing"


# ------------------------------------------------------------------ channels


def profile(**kw: Any) -> dict[str, Any]:
    return {"slug": "night-owl", "display_name": "Night Owl", "niche": "cosy mysteries", **kw}


def test_channel_lifecycle(tmp_home: Path):
    dash, state = make(tmp_home)
    created = dash.channels.create(profile())
    assert created["profile"]["slug"] == "night-owl" and created["profile"]["created_at"]
    assert isinstance(created["warnings"], list) and all(isinstance(w, str) for w in created["warnings"])
    assert created["warning_fields"][0]["field"].startswith("pools.")
    assert state.db.one("SELECT status FROM channels WHERE slug = 'night-owl'")["status"] == "active"
    assert code_of(dash.channels.create, profile()) == "channel_exists"

    with pytest.raises(ApiError) as ei:
        dash.channels.create(profile(slug="Bad Slug", critic_bar=99))
    body = ei.value.to_body()["error"]
    assert ei.value.status == 422 and body["code"] == "invalid_profile"
    assert {d["field"] for d in body["details"]} >= {"slug", "critic_bar"}

    got = dash.channels.get("night-owl")
    assert got["profile"]["niche"] == "cosy mysteries"
    upd = dash.channels.update("night-owl", profile(niche="cosy crime"))
    assert upd["profile"]["niche"] == "cosy crime"
    assert upd["profile"]["created_at"] == created["profile"]["created_at"]
    assert code_of(dash.channels.update, "night-owl", profile(slug="other")) == "invalid_profile"
    assert code_of(dash.channels.update, "ghost", profile(slug="ghost")) == "channel_not_found"
    assert code_of(dash.channels.get, "../etc") == "not_found"

    paused = dash.channels.pause("night-owl", "YouTube warning")
    assert paused["profile"]["status"] == "paused"
    assert load_channel(state.paths, "night-owl").status == "paused"
    assert state.db.one("SELECT status FROM channels WHERE slug = 'night-owl'")["status"] == "paused"
    inc = state.db.one("SELECT kind, note FROM incidents WHERE channel = 'night-owl'")
    assert inc is not None and (inc["kind"], inc["note"]) == ("pause", "YouTube warning")
    run = {"channel": "night-owl", "format": "shorts_only"}
    assert code_of(dash.jobs.create_run, run) == "channel_paused"
    assert dash.channels.resume("night-owl")["profile"]["status"] == "active"
    assert [r["kind"] for r in state.db.query("SELECT kind FROM incidents ORDER BY incident_id")] == [
        "pause",
        "resume",
    ]
    cards = dash.channels.cards()
    assert cards[0]["slug"] == "night-owl" and cards[0]["pending"] == 0


def test_channel_validate_shape(tmp_home: Path):
    dash, _ = make(tmp_home)
    draft = {"slug": "draft", "display_name": "D", "niche": "n", "critic_bar": 1}
    bad = dash.channels.validate("draft", draft)
    assert bad["ok"] is False and bad["errors"] == ["Must be at least 5.0."]
    assert bad["error_fields"][0]["field"] == "critic_bar"
    good = dash.channels.validate("draft", profile(slug="draft"))
    assert good["ok"] is True and good["errors"] == [] and all(isinstance(w, str) for w in good["warnings"])
    dash.channels.create(profile())
    saved = dash.channels.validate("night-owl", None)
    assert saved["ok"] is True
    twin = dash.channels.validate("twin", profile(slug="twin", display_name="Twin"))
    assert any("Night Owl" in w for w in twin["warnings"])


# ------------------------------------------------------------------ jobs


def test_run_job_end_to_end_with_fakes(tmp_home: Path):
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)
    assert code_of(dash.jobs.create_run, {"channel": "nope", "format": "shorts_only"}) == "channel_not_found"
    assert code_of(dash.jobs.create_run, {"channel": "after-dark", "format": "weekly"}) == "invalid_options"
    job = dash.jobs.create_run({"channel": "after-dark", "format": "shorts_only", "count": 2})
    assert job["status"] == "queued" and job["kind"] == "run" and job["progress"] == 0
    assert state.worker.run_next() is True
    done = dash.jobs.get(job["job_id"])
    assert done["status"] == "done" and done["progress"] == 100.0
    ids = done["result"]["output_ids"]
    assert len(ids) == 2
    pending = dash.outputs.list_outputs(status="pending", kind="short")
    assert {p["output_id"] for p in pending} == set(ids)
    assert [j["job_id"] for j in dash.jobs.list_jobs(None, "run", 10)] == [job["job_id"]]
    assert dash.jobs.list_jobs("failed", None, 10) == []
    assert code_of(dash.jobs.get, "nope-20260101-abcd") == "not_found"


def test_clip_job_requires_rights_and_valid_options(tmp_home: Path):
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)
    video = tmp_home / "in.mp4"
    video.write_bytes(b"video")
    base = {"path": str(video), "channel": "after-dark", "rights_confirmed": True}
    assert code_of(dash.jobs.create_clip, {**base, "rights_confirmed": False}) == "rights_required"
    assert code_of(dash.jobs.create_clip, {**base, "rights_confirmed": "true"}) == "rights_required"
    assert code_of(dash.jobs.create_clip, {**base, "template": "sparkles"}) == "invalid_options"
    assert code_of(dash.jobs.create_clip, {**base, "max_clips": 99}) == "invalid_options"
    assert code_of(dash.jobs.create_clip, {**base, "min_s": 50, "max_s": 20}) == "invalid_options"
    assert code_of(dash.jobs.create_clip, {**base, "max_clips": "many"}) == "invalid_options"
    assert code_of(dash.jobs.create_clip, {**base, "channel": "nope"}) == "channel_not_found"
    assert code_of(dash.jobs.create_clip, {**base, "path": str(tmp_home / "missing.mp4")}) == "bad_path"
    notes = tmp_home / "notes.txt"
    notes.write_text("x", encoding="utf-8")
    assert code_of(dash.jobs.create_clip, {**base, "path": str(notes)}) == "bad_path"
    assert code_of(dash.jobs.create_clip, {k: v for k, v in base.items() if k != "path"}) == "no_source"
    assert code_of(dash.jobs.create_clip, {**base, "upload_id": "src-20261101-ffff"}) == "upload_incomplete"
    ghost = {**base, "path": None, "source_id": "src-20261101-ffff"}
    assert code_of(dash.jobs.create_clip, ghost) == "not_found"
    assert state.store.counts() == {"queued": 0, "running": 0}


def test_clip_job_from_local_path_reaches_the_review_queue(tmp_home: Path):
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)
    video = tmp_home / "talk.mp4"
    video.write_bytes(b"video-bytes")
    job = dash.jobs.create_clip(
        {"path": str(video), "channel": "after-dark", "template": "gaming_facecam", "rights_confirmed": True,
         "max_clips": 2}
    )
    p = job["params"]
    assert p["template"] == "gaming_facecam" and p["rights_confirmed"] is True and p["rerun"] is False
    assert p["source_id"].startswith("src-") and p["source_path"] == str(video)
    unseen = state.db.one("SELECT 1 FROM sources WHERE source_id = ?", (p["source_id"],))
    assert unseen is None  # the Clipper import creates the row
    # the fake Clipper import creates the row with the owner's confirmation (CR-C9-2 column)
    state.worker.run_next()
    assert dash.jobs.get(job["job_id"])["status"] == "done"
    row_sql = "SELECT rights_confirmed, title FROM sources WHERE source_id = ?"
    src_row = state.db.one(row_sql, (p["source_id"],))
    assert src_row is not None and src_row["rights_confirmed"] == 1 and src_row["title"] == "talk"
    clips = dash.outputs.list_outputs(status="pending", kind="clip")
    assert len(clips) == 2 and all(c["source_id"] == p["source_id"] for c in clips)
    call = state.adapters.clip.calls[0]  # type: ignore[attr-defined]
    assert call["template"] == "gaming_facecam" and call["channel"] == "after-dark" and call["rerun"] is False
    assert dash.outputs.approve(clips[0]["output_id"])["status"] == "approved"

    again = dash.jobs.create_clip(
        {"source_id": p["source_id"], "channel": "after-dark", "template": "center_crop",
         "rights_confirmed": True}
    )
    assert again["params"]["rerun"] is True and again["params"]["source_path"] == str(video)


def test_clip_job_from_chunked_upload_and_multipart_stream(tmp_home: Path):
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)
    mgr: UploadManager = dash.jobs.uploads
    info = mgr.start("vod.mkv", 8)
    mgr.append(info.upload_id, 0, b"abcd")
    assert code_of(dash.jobs.create_clip, {"upload_id": info.upload_id, "channel": "after-dark",
                                           "rights_confirmed": True}) == "upload_incomplete"
    mgr.append(info.upload_id, 4, b"efgh")
    mgr.complete(info.upload_id)
    req = {"upload_id": info.upload_id, "channel": "after-dark", "rights_confirmed": True}
    job = dash.jobs.create_clip(req)
    assert job["params"]["source_id"] == info.source_id
    assert job["params"]["source_path"].endswith("source.mkv")

    streamed = dash.jobs.import_stream("podcast.MP4", io.BytesIO(b"x" * 100))
    assert streamed.status == "complete" and streamed.size == 100
    job2 = dash.jobs.create_clip(
        {"channel": "after-dark", "rights_confirmed": True, "max_clips": "3", "template": "auto"}, streamed
    )
    assert job2["params"]["source_id"] == streamed.source_id and job2["params"]["max_clips"] == 3


def test_a_finished_voice_recording_is_not_a_clip_source(tmp_home: Path):
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)
    info = dash.jobs.uploads.start("sample.wav", 4)
    dash.jobs.uploads.append(info.upload_id, 0, b"RIFF")
    dash.jobs.uploads.complete(info.upload_id)
    req = {"upload_id": info.upload_id, "channel": "after-dark", "rights_confirmed": True}
    assert code_of(dash.jobs.create_clip, req) == "bad_file_type"


def test_cancel_job(tmp_home: Path):
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)
    a = dash.jobs.create_run({"channel": "after-dark", "format": "shorts_only"})
    assert dash.jobs.cancel(a["job_id"])["status"] == "cancelled"
    state.worker.run_next()
    assert code_of(dash.jobs.cancel, a["job_id"]) == "already_finished"
    assert code_of(dash.jobs.cancel, "nope-20260101-abcd") == "not_found"


def test_verify_job(tmp_home: Path):
    dash, state = make(tmp_home)
    job = dash.jobs.create_verify({"quick": True})
    assert job["kind"] == "verify" and job["params"] == {"quick": True}
    state.worker.run_next()
    assert dash.jobs.get(job["job_id"])["result"]["quick"] is True


# ------------------------------------------------------------------ learn


def test_learn_import_is_synchronous_and_shaped(tmp_home: Path):
    dash, state = make(tmp_home)
    out = dash.jobs.import_learn("Content export.csv", io.BytesIO(b"title,views\nA,10\nB,20\n"))
    assert out["matched"] == 2 and out["unmatched"] == 0 and out["changes"] == []
    assert isinstance(out["summary"], list) and all(isinstance(s, str) for s in out["summary"])
    saved = list(state.paths.imports_analytics.glob("*Content_export.csv"))
    assert len(saved) == 1
    job = dash.jobs.get(out["job_id"])
    assert job["kind"] == "learn" and job["status"] == "done"
    assert code_of(dash.jobs.import_learn, "x.txt", io.BytesIO(b"a")) == "bad_file_type"
    assert code_of(dash.jobs.import_learn, "x.csv", io.BytesIO(b"")) == "empty_file"
    assert state.worker.run_next() is False  # learn jobs never go through the FIFO thread


def test_learn_response_normalisation():
    r = learn_response(
        {
            "matched": ["a", "b", "c"],
            "unmatched": ["row 9"],
            "weight_changes": [
                {"pool": "hooks", "option": "cold_open", "delta": 0.4},
                {"pool": "pacings", "option": "measured", "change": -0.2},
                "junk",
            ],
        },
        "learn-20261101-aaaa",
    )
    assert r["matched"] == 3 and r["unmatched"] == 1 and r["unmatched_rows"] == ["row 9"]
    assert r["changes"] == [
        {"pool": "hooks", "option": "cold_open", "delta": 0.4},
        {"pool": "pacings", "option": "measured", "delta": -0.2},
    ]
    assert r["job_id"] == "learn-20261101-aaaa"
    assert any("cold_open" in s and "up" in s for s in r["summary"])
    custom = learn_response({"summary": ["All good."], "matched": 1})
    assert custom["summary"] == ["All good."] and custom["unmatched"] == 0


def test_learn_summary_from_database(tmp_home: Path):
    dash, state = make(tmp_home)
    def unavailable() -> dict[str, Any]:
        raise AdapterUnavailable("no C7")

    state.adapters.learn.summary = unavailable  # type: ignore[attr-defined]
    empty = dash.settings.learn_summary()
    assert empty["pools"] == {} and "Nothing learned yet" in empty["summary"][0]
    for opt, w in (("cold_open", 1.4), ("question", 1.1), ("warning", 0.9), ("confession", 0.6), ("x", 0.4)):
        state.db.execute(
            "INSERT INTO pool_weights(channel, pool, option, weight, updated_at)"
            " VALUES ('c', 'hooks', ?, ?, 't')",
            (opt, w),
        )
    state.db.execute(
        "INSERT INTO combos(combo_id, channel, job_id, item, hook, created_at)"
        " VALUES ('k1','c','j','long','cold_open','t')"
    )
    oid = seed_output(state.db, state.paths, status="approved")
    s = dash.settings.learn_summary()
    hooks = s["pools"]["hooks"]
    assert [t["option"] for t in hooks["top"]] == ["cold_open", "question", "warning"]
    assert hooks["top"][0]["n"] == 1
    assert [b["option"] for b in hooks["bottom"]] == ["x", "confession", "warning"]
    assert s["unmatched_uploads"] == [oid] and s["last_import"] is None
    assert s["summary"][0].startswith("Best hooks: cold_open")


# ------------------------------------------------------------------ settings


def test_settings_defaults_and_schedule(tmp_home: Path):
    dash, state = make(tmp_home)
    s = dash.settings.get_settings()
    assert s["schedule_time"] == "03:00" and s["home"] == str(tmp_home) and s["profile"] is None
    assert s["flags"] == {} and s["notifications"] is True
    assert s["keys"] == {"pexels": False, "pixabay": False}
    out = dash.settings.put_settings({"schedule_time": "04:15", "notifications": False})
    assert out["schedule_time"] == "04:15" and out["notifications"] is False
    assert out["schedule_result"]["applied"] is True
    assert state.adapters.schedule.applied == ["04:15"]  # type: ignore[attr-defined]
    assert code_of(dash.settings.put_settings, {"schedule_time": "25:99"}) == "bad_time"
    assert code_of(dash.settings.put_settings, {"notifications": "yes"}) == "invalid_options"


def test_runtime_flags_locked_until_unlocked(tmp_home: Path):
    dash, state = make(tmp_home)
    save_runtime(state.paths, Runtime.default("cpu-dev"))
    s = dash.settings.get_settings()
    assert s["profile"] == "cpu-dev" and s["flags"]["video"]["encoder"] == "libx264"
    assert "profile" not in s["flags"] and "supervisor_port" not in s["flags"]
    change = {"flags": {"video": {"encoder": "libx264", "preset": "fast", "fps": 30}}}
    assert code_of(dash.settings.put_settings, change) == "runtime_locked"
    assert load_runtime(state.paths).video.preset == "ultrafast"
    out = dash.settings.put_settings({**change, "unlock": True})
    assert out["flags"]["video"]["preset"] == "fast" and load_runtime(state.paths).video.preset == "fast"
    bad = {"flags": {"video": {"encoder": "divx"}}, "unlock": True}
    assert code_of(dash.settings.put_settings, bad) == "invalid_runtime"
    assert dash.settings.put_settings({"profile": "gpu"}, unlock=True)["profile"] == "gpu"


def test_runtime_edit_without_runtime_file(tmp_home: Path):
    dash, _ = make(tmp_home)
    assert code_of(dash.settings.put_settings, {"flags": {}, "unlock": True}) == "no_runtime"


def test_api_keys_are_stored_but_never_echoed_or_logged(tmp_home: Path):
    dash, state = make(tmp_home)
    secret = "pexels-SECRET-0123456789"
    out = dash.settings.set_key("pexels", f"  {secret}  ")
    assert out == {"name": "pexels_api_key", "set": True} and secret not in json.dumps(out)
    assert state.adapters.keyring.get("pexels_api_key") == secret
    assert dash.settings.get_settings()["keys"] == {"pexels": True, "pixabay": False}
    assert secret not in json.dumps(dash.settings.get_settings())
    assert dash.settings.set_key("pixabay_api_key", "k")["name"] == "pixabay_api_key"
    assert code_of(dash.settings.set_key, "youtube", "k") == "unknown_key"
    assert code_of(dash.settings.set_key, "pexels", "   ") == "bad_key"
    logged = "".join(f.read_text(encoding="utf-8") for f in state.paths.logs.glob("*.jsonl"))
    assert "api_key_set" in logged and secret not in logged


def test_diagnostics_bundle(tmp_home: Path):
    dash, _ = make(tmp_home)
    assert code_of(dash.settings.latest_bundle) == "not_found"
    out = dash.settings.diagnostics()
    assert out["name"].startswith("nightshift-diagnostics-") and out["name"].endswith(".zip")
    assert Path(out["path"]).is_file() and out["size"] > 0
    assert out["download_url"] == "/api/diagnostics/latest"
    assert dash.settings.latest_bundle() == Path(out["path"])


def test_templates_voices_and_preview_cache(tmp_home: Path):
    dash, state = make(tmp_home)
    tpl = dash.settings.templates()
    assert isinstance(tpl, list) and tpl[0]["id"] == "auto" and {"id", "label", "description"} <= set(tpl[0])
    voices = dash.settings.voices()
    assert isinstance(voices, list) and {"name": "Ryan", "mode": "preset", "source": "builtin"} in voices
    wav = dash.settings.voice_preview({"voice": "Ryan"})
    assert wav[:4] == b"RIFF"
    assert dash.settings.voice_preview({"voice": "Ryan"}) == wav
    assert len(state.adapters.tts.previews) == 1  # type: ignore[attr-defined]
    dash.settings.voice_preview({"voice": "Ryan", "text": "Different words", "speed": 1.2})
    assert len(state.adapters.tts.previews) == 2  # type: ignore[attr-defined]
    assert code_of(dash.settings.voice_preview, {}) == "no_voice"
    assert code_of(dash.settings.voice_preview, {"voice": "Ryan", "speed": 5}) == "invalid_options"
    assert code_of(dash.settings.template_preview, "nope") == "not_found"


def test_logs_tail_is_a_bare_list(tmp_home: Path):
    dash, state = make(tmp_home)
    state.log.info("hello_event", "hi")
    state.log.error("bad_event", "oops")
    lines = dash.settings.tail(None, None, 200, None)
    assert isinstance(lines, list) and {"hello_event", "bad_event"} <= {r["event"] for r in lines}
    assert {"ts", "level", "component", "event", "msg"} <= set(lines[0])
    assert [r["event"] for r in dash.settings.tail("dashboard", "ERROR", 10, None)][-1] == "bad_event"
    assert "dashboard" in dash.settings.log_components()
