# ruff: noqa: E501
"""Settings actions that reach other components: update (gap 9), diagnostics (10), run time (13), backup and
restore (17), engine buttons (27), log components (28) and the stored UI preferences (30)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ns_common.paths import Paths
from ns_dashboard import updater
from ns_dashboard.fakes import FakeUpdateAdapter
from tests.test_dashboard_api import err, make_env

pytest.importorskip("fastapi")


# ---------------------------------------------------------------- gap 9: ns update --check / --github


def runner_returning(code: int, out: str = "", error: str = ""):
    calls: list[list[str]] = []

    def run(cmd: list[str], timeout_s: int) -> tuple[int, str, str]:
        calls.append(cmd)
        return code, out, error

    return run, calls


def test_update_check_calls_ns_update_check_and_reads_the_answer(tmp_home: Path):
    run, calls = runner_returning(0, '{"update_available": true, "current": "0.1.0", "latest": "0.2.0"}\n')
    res = updater.RealUpdateAdapter(Paths.from_env(), run).check()
    assert calls[0][-2:] == ["update", "--check"]
    assert res["state"] == "available" and res["up_to_date"] is False and res["latest"] == "0.2.0"
    run2, calls2 = runner_returning(0, "Nightshift 0.2.0 is up to date.\n")
    same = updater.RealUpdateAdapter(Paths.from_env(), run2).check()
    assert calls2[0][-2:] == ["update", "--check"] and same["state"] == "up_to_date" and same["up_to_date"] is True


def test_missing_update_command_is_a_clear_state_not_an_error(tmp_home: Path):
    # what `ns update --check` does today: the command only takes a zip or --rollback
    run, calls = runner_returning(2, "", "give either a zip file or --rollback")
    adapter = updater.RealUpdateAdapter(Paths.from_env(), run)
    res = adapter.check()
    assert res["state"] == "unavailable" and "Update shortcut" in res["message"] and res["up_to_date"] is False
    applied = adapter.apply()
    assert calls[-1][-2:] == ["update", "--github"] and applied["state"] == "unavailable"
    gone = updater.RealUpdateAdapter(Paths.from_env(), runner_returning(127, "", "[WinError 2]")[0]).check()
    assert gone["state"] == "unavailable"
    failed = updater.RealUpdateAdapter(Paths.from_env(), runner_returning(1, "", "network down")[0]).check()
    assert failed["state"] == "error" and failed["message"] == "network down"


def test_update_routes_report_the_unavailable_state(tmp_home: Path):
    client, state = make_env(tmp_home)
    assert isinstance(state.adapters.update, FakeUpdateAdapter)
    assert client.get("/api/update/check").json()["state"] == "available"
    state.adapters.update.missing = True
    chk = client.get("/api/update/check").json()
    assert chk["state"] == "unavailable" and chk["message"]
    r = client.post("/api/update/apply")
    assert r.status_code == 501 and err(r)["code"] == "update_unavailable"


# ---------------------------------------------------------------- gap 10: diagnostics -> doctor.build_bundle


def test_diagnostics_bundle_is_built_by_doctor_and_downloadable(tmp_home: Path):
    import zipfile

    from ns_dashboard.adapters import RealDoctorAdapter

    client, state = make_env(tmp_home)
    paths = Paths.from_env()
    state.adapters.doctor = RealDoctorAdapter(
        paths, out_dir=tmp_home / "out", options={"freeze": lambda _p, _v: None, "smi": lambda: None}
    )
    assert client.get("/api/diagnostics/latest").status_code == 404  # nothing made yet
    made = client.post("/api/diagnostics")
    assert made.status_code == 200, made.text
    body = made.json()
    assert body["name"].startswith("nightshift-diagnostics-") and body["download_url"] == "/api/diagnostics/latest"
    assert Path(body["path"]).parent == tmp_home / "out"
    got = client.get(body["download_url"])
    assert got.status_code == 200 and got.content[:2] == b"PK"
    assert body["name"] in got.headers["content-disposition"]
    with zipfile.ZipFile(body["path"]) as z:
        assert "bundle-info.json" in z.namelist() and "VERSION" in z.namelist()


# ---------------------------------------------------------------- gap 11: verify job -> run_verify(paths, rt, ctl, ...)


def test_verify_job_runs_the_real_checks_with_the_real_signature(tmp_home: Path):
    from ns_common.models import Runtime
    from ns_dashboard.adapters import RealVerifyAdapter
    from ns_dashboard.events import EventHub
    from ns_dashboard.jobs import JobControl, JobStore
    from ns_dashboard.state import open_db
    from ns_supervisor.testing import StubTable, SyncSupervisor
    from tests.test_ns_verify import good_deps

    paths = Paths.from_env()
    paths.ensure()
    sync = SyncSupervisor(paths, Runtime.default("gpu"), resolver=StubTable())
    try:
        store = JobStore(open_db(paths))
        ctl = JobControl(store.create("verify", None, {"quick": True}), store, EventHub())
        out = RealVerifyAdapter(paths, controller=sync, deps=good_deps()).verify(ctl, True)
    finally:
        sync.close()
    names = {c["name"] for c in out["checks"]}
    assert out["passed"] is True and out["quick"] is True
    assert {"supervisor.status", "llama.start", "tts.start", "nvenc.probe"} <= names
    assert "llama.tok_s" not in names  # quick
    assert Path(out["report"]) == paths.logs / "verify-report.json" and Path(out["report"]).exists()


# ---------------------------------------------------------------- gap 12: Results CSV import -> learn.importer


CSV = (
    "Content,Video title,Views,Average percentage viewed (%)\n"
    "Total,,110,50\n"
    "abc12345678,A title,100,62.5\n"
    "zzz12345678,Not ours,10,30\n"
)


def test_csv_import_matches_rows_and_reports_in_plain_words(tmp_home: Path):
    from ns_common.db import Db
    from ns_dashboard.adapters import RealLearnAdapter
    from ns_studio.channels.crud import add_channel
    from tests.helpers import add_output, make_channel

    client, state = make_env(tmp_home)
    paths = Paths.from_env()
    db = Db(paths.db_path)
    add_channel(paths, db, make_channel())
    oid = add_output(db, "after-dark-20261020-aaaa", "s1", title="A title")
    state.adapters.learn = RealLearnAdapter(paths)
    r = client.post("/api/learn/import", files={"file": ("export.csv", CSV.encode(), "text/csv")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["matched"] == 1 and body["unmatched"] == 1 and body["unmatched_rows"] == ["Not ours"]
    assert any("1 row(s) matched" in line for line in body["summary"])
    row = db.one("select views, avg_pct_viewed from analytics where output_id = ?", (oid,))
    assert row is not None and row["views"] == 100 and row["avg_pct_viewed"] == 62.5
    summary = client.get("/api/learn/summary")
    assert summary.status_code == 200 and "pools" in summary.json()


def test_csv_with_no_known_columns_is_a_coded_error(tmp_home: Path):
    from ns_dashboard.adapters import RealLearnAdapter

    client, state = make_env(tmp_home)
    state.adapters.learn = RealLearnAdapter(Paths.from_env())
    r = client.post("/api/learn/import", files={"file": ("export.csv", b"a,b\n1,2\n", "text/csv")})
    assert r.status_code == 500 and "NS-E323" in err(r)["message"]


# ---------------------------------------------------------------- gap 13: run time -> schedule.install


def test_changing_the_run_time_reregisters_the_windows_task(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_dashboard.adapters import RealScheduleAdapter
    from ns_supervisor import schedule
    from ns_supervisor.schedule_cfg import read_run_time

    scripts: list[str] = []
    monkeypatch.setattr(schedule, "_run", lambda script: scripts.append(script) or "registered")
    client, state = make_env(tmp_home)
    state.adapters.schedule = RealScheduleAdapter(Paths.from_env())
    r = client.put("/api/settings", json={"schedule_time": "04:30"})
    assert r.status_code == 200, r.text
    assert r.json()["schedule_result"]["applied"] is True
    assert len(scripts) == 1 and "Register-ScheduledTask" in scripts[0] and "'04:30'" in scripts[0]
    assert read_run_time(Paths.from_env()) == "04:30"  # the supervisor's own copy of the time moved too


def test_a_refused_task_update_keeps_the_time_and_says_so(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_dashboard.adapters import RealScheduleAdapter
    from ns_supervisor import schedule

    def refuse(_script: str) -> str:
        raise schedule.ScheduleError("Access is denied")

    monkeypatch.setattr(schedule, "_run", refuse)
    client, state = make_env(tmp_home)
    state.adapters.schedule = RealScheduleAdapter(Paths.from_env())
    body = client.put("/api/settings", json={"schedule_time": "05:00"}).json()
    assert body["schedule_time"] == "05:00"
    assert body["schedule_result"]["applied"] is False and "Access is denied" in body["schedule_result"]["note"]


# ---------------------------------------------------------------- gap 17: export, import, uninstall -> ns backup / restore


def test_backup_adapter_runs_ns_backup_and_ns_restore(tmp_home: Path):
    from ns_dashboard.errors import ApiError

    paths = Paths.from_env()
    run, calls = runner_returning(0, "Backup written to C:/Users/me/Desktop/nightshift-backup-2026-11-02.zip\n")
    adapter = updater.RealBackupAdapter(paths, run)
    assert adapter.export() == Path("C:/Users/me/Desktop/nightshift-backup-2026-11-02.zip")
    assert calls[0][-1:] == ["backup"]
    run2, calls2 = runner_returning(0, "restored settings\nrestored voices\n")
    lines = updater.RealBackupAdapter(paths, run2).restore(tmp_home / "b.zip")
    assert calls2[0][-2:] == ["restore", str(tmp_home / "b.zip")] and lines == ["restored settings", "restored voices"]
    with pytest.raises(ApiError) as bad:
        updater.RealBackupAdapter(paths, runner_returning(1, "", "not a backup")[0]).restore(tmp_home / "b.zip")
    assert bad.value.code == "restore_failed" and bad.value.hint == "not a backup"
    with pytest.raises(ApiError) as nothing:
        updater.RealBackupAdapter(paths, runner_returning(1, "", "boom")[0]).export()
    assert nothing.value.code == "backup_failed"
    assert updater.RealBackupAdapter(paths).uninstall_path().name == "UNINSTALL.cmd"


def test_settings_export_import_uninstall_routes(tmp_home: Path):
    from ns_dashboard.fakes import FakeBackupAdapter

    client, state = make_env(tmp_home)
    assert isinstance(state.adapters.backup, FakeBackupAdapter)
    assert client.get("/api/settings/export/latest").status_code == 404
    made = client.post("/api/settings/export").json()
    assert made["name"].startswith("nightshift-backup-") and made["download_url"] == "/api/settings/export/latest"
    assert client.get(made["download_url"]).content[:2] == b"PK"
    r = client.post("/api/settings/import", files={"file": ("nightshift-backup-2026-11-02.zip", b"PK\x03\x04data", "application/zip")})
    assert r.status_code == 200 and r.json()["restored"] is True
    saved = state.adapters.backup.restored[0]
    assert saved.read_bytes() == b"PK\x03\x04data" and saved.parent == tmp_home / "imports" / "restore"
    wrong = client.post("/api/settings/import", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert wrong.status_code == 400 and err(wrong)["code"] == "bad_file_type"
    empty = client.post("/api/settings/import", files={"file": ("empty.zip", b"", "application/zip")})
    assert empty.status_code == 400
    state.adapters.backup.fail_restore = True
    refused = client.post("/api/settings/import", files={"file": ("b.zip", b"PK", "application/zip")})
    assert refused.status_code == 422 and err(refused)["code"] == "restore_failed"
    info = client.get("/api/settings/uninstall").json()
    assert info["path"].endswith("UNINSTALL.cmd") and info["exists"] is False


# ---------------------------------------------------------------- gap 18: clip re-run with another template


def test_clip_rerun_route_resumes_the_clip_job_with_the_new_template(tmp_home: Path):
    from ns_dashboard.fakes import FakeClipAdapter, seed_channel
    from tests.test_dashboard_api import clip_file

    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths)
    video = clip_file(tmp_home)
    first = client.post(
        "/api/jobs/clip",
        json={"path": str(video), "channel": "after-dark", "template": "auto", "max_clips": 2, "rights_confirmed": True},
    ).json()["job_id"]
    assert client.post("/api/jobs/clip/rerun", json={"job_id": first, "template": "talking_head"}).status_code == 409  # not finished
    state.worker.run_next()
    done = state.store.get(first)
    assert done is not None and done.status == "done" and done.result is not None
    studio_job = done.result["studio_job_id"]
    r = client.post("/api/jobs/clip/rerun", json={"job_id": first, "template": "talking_head"})
    assert r.status_code == 201, r.text
    state.worker.run_next()
    again = state.store.get(r.json()["job_id"])
    assert again is not None and again.status == "done"
    assert again.params["studio_job_id"] == studio_job and again.params["rerun_of"] == first
    assert again.result is not None and again.result["output_ids"] == done.result["output_ids"]
    assert isinstance(state.adapters.clip, FakeClipAdapter)
    assert state.adapters.clip.reruns[0]["studio_job_id"] == studio_job
    assert state.adapters.clip.reruns[0]["template"] == "talking_head"


def test_clip_rerun_route_rejects_bad_requests(tmp_home: Path):
    client, state = make_env(tmp_home)
    assert client.post("/api/jobs/clip/rerun", json={"job_id": "nope", "template": "auto"}).status_code == 404
    job = state.store.create("run", "after-dark", {})
    assert client.post("/api/jobs/clip/rerun", json={"job_id": job.job_id, "template": "auto"}).status_code == 404
    clip = state.store.create("clip", "after-dark", {})
    state.store.finish(clip.job_id, "done", "Done", {"output_ids": []})
    bad = client.post("/api/jobs/clip/rerun", json={"job_id": clip.job_id, "template": "nope"})
    assert bad.status_code == 422
    old = client.post("/api/jobs/clip/rerun", json={"job_id": clip.job_id, "template": "auto"})
    assert old.status_code == 409 and err(old)["code"] == "cannot_rerun"


# ---------------------------------------------------------------- gap 23: Library > Formats edit form -> PUT


def test_format_edit_form_saves_a_patch_over_http(tmp_home: Path):
    client, _state = make_env(tmp_home)
    listed = {f["id"]: f for f in client.get("/api/library/formats").json()}
    assert listed["short_9x16"]["source"] == "shipped" and listed["short_9x16"]["definition"]["duration"]["max_s"] == 90
    patch = {"changes": {"name": "Short, calmer", "duration": {"min_s": 25, "max_s": 80}, "captions": {"anchor_y_pct": 65}}}
    r = client.put("/api/library/formats/short_9x16", json=patch)
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["source"] == "edited" and saved["name"] == "Short, calmer"
    assert saved["definition"]["duration"]["min_s"] == 25 and saved["definition"]["captions"]["anchor_y_pct"] == 65
    assert saved["definition"]["captions"]["styles_allowed"]  # fields the form did not touch are kept
    assert (Paths.from_env().config / "formats" / "short_9x16.yaml").exists()
    bad = client.put("/api/library/formats/short_9x16", json={"changes": {"duration": {"min_s": 99, "max_s": 30}}})
    assert bad.status_code == 422 and err(bad)["code"] == "invalid_format" and err(bad)["details"]
    dup = client.post("/api/library/formats", json={"duplicate_of": "square_1x1"})
    assert dup.status_code == 201 and dup.json()["id"] == "square_1x1_copy"


# ---------------------------------------------------------------- gap 26: GET /api/runs feeds the "Last night" card


def test_runs_route_gives_the_last_night_card_its_numbers(tmp_home: Path):
    import json

    client, state = make_env(tmp_home)
    report = {"jobs": [{"job_id": "after-dark-20261102-a1f2", "channel": "after-dark", "stages": 19}],
              "failures": [{"job_id": "tiny-20261102-77c3", "channel": "tiny", "code": "NS-E203", "error": "no GPU"}]}
    state.db.execute(
        "insert into runs(run_id, started_at, finished_at, mode, summary_json) values (?,?,?,?,?)",
        ("daily-20261102-a1f2", "2026-11-02T01:00:00+00:00", "2026-11-02T03:52:00+00:00", "daily", json.dumps(report)),
    )
    rows = client.get("/api/runs", params={"limit": 1}).json()
    assert len(rows) == 1 and rows[0]["mode"] == "daily" and rows[0]["duration_s"] == 10320.0
    assert rows[0]["summary"]["jobs"][0]["channel"] == "after-dark" and rows[0]["summary"]["failures"][0]["code"] == "NS-E203"


# ---------------------------------------------------------------- gap 27: Settings > Engine buttons -> existing routes


def test_engine_buttons_reach_the_supervisor_and_the_verify_job(tmp_home: Path):
    from ns_dashboard.fakes import FakeSupervisor
    from ns_dashboard.supervisor_link import SupervisorLink

    with FakeSupervisor() as sup:
        client, state = make_env(tmp_home, link=SupervisorLink(sup.url))
        assert client.post("/api/engine/warmup", json={"service": "llama"}).json() == {"state": "starting"}
        stopped = client.post("/api/engine/stop_all")
        assert stopped.status_code == 200 and stopped.json()["ok"] is True
        queued = client.post("/api/jobs/verify", json={"quick": True})
        assert queued.status_code == 201 and queued.json()["kind"] == "verify"
        assert state.worker.run_next()
        job = client.get("/api/jobs", params={"kind": "verify", "limit": 1}).json()[0]
        assert job["status"] == "done" and job["result"]["checks"]
        assert client.post("/api/engine/warmup", json={"service": "banana"}).status_code == 422


# ---------------------------------------------------------------- gap 28: the log viewer filters by component


def test_log_components_feed_the_viewer_filter(tmp_home: Path):
    from ns_common.logging import NsLogger

    client, state = make_env(tmp_home)
    NsLogger("studio", state.paths.logs).info("studio_probe", "from studio")
    NsLogger("supervisor", state.paths.logs).info("supervisor_probe", "from supervisor")
    comps = client.get("/api/logs/components").json()
    assert {"studio", "supervisor"} <= set(comps)
    only = client.get("/api/logs/tail", params={"component": "studio", "n": 50}).json()
    assert only and {line["component"] for line in only} == {"studio"}
    assert any(line["event"] == "studio_probe" for line in only)
    assert client.get("/api/logs/tail", params={"component": "supervisor"}).json()[-1]["event"] == "supervisor_probe"


# ---------------------------------------------------------------- gap 30: owner_name and the theme from ui_prefs.json


def write_prefs(home: Path, text: str) -> None:
    f = home / "config" / "ui_prefs.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")


def test_settings_carry_the_setup_windows_name_and_theme(tmp_home: Path):
    client, _state = make_env(tmp_home)
    first = client.get("/api/settings").json()
    assert first["owner_name"] is None and first["ui_theme"] is None  # no file yet
    write_prefs(tmp_home, '{"owner_name": "  Sam   Rivers ", "theme": "soft"}')
    got = client.get("/api/settings").json()
    assert got["owner_name"] == "Sam Rivers" and got["ui_theme"] == "soft"
    write_prefs(tmp_home, '{"owner_name": "", "theme": "neon"}')
    bad = client.get("/api/settings").json()
    assert bad["owner_name"] is None and bad["ui_theme"] is None
    write_prefs(tmp_home, "{not json")
    assert client.get("/api/settings").json()["owner_name"] is None
    write_prefs(tmp_home, '{"owner_name": "' + "x" * 100 + '", "theme": "dark"}')
    long = client.get("/api/settings").json()
    assert len(long["owner_name"]) == 40 and long["ui_theme"] == "dark"
