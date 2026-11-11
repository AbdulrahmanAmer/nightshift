# ruff: noqa: E501
"""Regression tests for the clean-clone review of the dashboard (one section per review row)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ns_dashboard.errors import ApiError
from ns_dashboard.fakes import seed_channel, seed_output
from ns_dashboard.handlers import Dashboard
from tests.test_dashboard_handlers import code_of, make

# ------------------------------------------------------------------ row 20: clip length bounds match ClipParams


@pytest.mark.parametrize(
    ("min_s", "max_s"),
    [(5, 6), (5, 10), (20, 60), (20, 179), (20, 180), (60, 60), (60, 30), (5, 179), (4, 20), (120, 179), (121, 179), (178, 179), (179, 179)],
)
def test_clip_length_bounds_are_the_clipparams_bounds(tmp_home: Path, min_s: int, max_s: int):
    """The API accepted `max_s` 180 and `min_s == max_s`; the job then failed in `ClipParams.validate`."""
    from ns_studio.clipper.models import ClipParams

    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)
    video = tmp_home / "in.mp4"
    video.write_bytes(b"video")
    try:
        ClipParams(source="x", channel="after-dark", min_s=min_s, max_s=max_s, rights_confirmed=True).validate()
        studio_ok = True
    except ValueError:
        studio_ok = False
    body = {"path": str(video), "channel": "after-dark", "rights_confirmed": True, "min_s": min_s, "max_s": max_s}
    try:
        dash.jobs.create_clip(body)
        api_ok = True
    except ApiError as e:
        assert e.code == "invalid_options"
        api_ok = False
    assert api_ok == studio_ok, f"min_s={min_s} max_s={max_s}: API {api_ok}, ClipParams {studio_ok}"


# ------------------------------------------------------------------ row 5: Queue "Approved and posted"


def test_queue_posted_tab_status_list_reason_url_and_path(tmp_home: Path):
    """The tab asked for `status=approved,uploaded` (422 before); rejected rows lacked `reason`, posted `url`."""
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)
    approved = seed_output(state.db, state.paths, kind="short", k=1, title="A")
    posted = seed_output(state.db, state.paths, kind="short", k=2, title="B")
    rejected = seed_output(state.db, state.paths, kind="short", k=3, title="C")
    seed_output(state.db, state.paths, kind="short", k=4, title="D")  # stays pending
    dash.outputs.approve(approved)
    dash.outputs.approve(posted)
    dash.outputs.mark_uploaded(posted, "https://youtu.be/xyz")
    dash.outputs.reject(rejected, "bad audio")
    rows = dash.outputs.list_outputs("approved,uploaded")
    assert {r["output_id"] for r in rows} == {approved, posted}
    by_id = {r["output_id"]: r for r in rows}
    assert by_id[posted]["url"] == "https://youtu.be/xyz" and by_id[approved]["url"] is None
    assert by_id[posted]["path"].endswith(".mp4")
    (rej,) = dash.outputs.list_outputs("rejected")
    assert rej["reason"] == "bad audio" == rej["review_note"]
    assert dash.outputs.detail(posted)["url"] == "https://youtu.be/xyz"
    assert code_of(dash.outputs.list_outputs, "approved,nonsense") == "bad_filter"
    assert len(dash.outputs.list_outputs("all")) == 4 and len(dash.outputs.list_outputs(None)) == 4


# ------------------------------------------------------------------ row 7: import settings closes the database


def test_import_settings_closes_the_database_for_the_restore_and_restarts_the_worker(tmp_home: Path):
    """`ns restore` could not replace `studio.db` while the dashboard's connection held its -wal/-shm files."""
    import io

    from ns_dashboard.fakes import FakeBackupAdapter

    dash, state = make(tmp_home)
    backup = state.adapters.backup
    assert isinstance(backup, FakeBackupAdapter)
    seen: dict[str, Any] = {}

    def during_restore() -> None:
        seen["closed"] = state.db.db._conn is None
        seen["worker_stopped"] = not state.worker.started

    backup.on_restore = during_restore
    state.worker.start()
    try:
        out = dash.settings.import_settings("backup.zip", io.BytesIO(b"PK-not-looked-at"))
        assert out["restored"] is True
        assert seen == {"closed": True, "worker_stopped": True}
        assert state.worker.started  # running again
        assert state.db.one("SELECT COUNT(*) AS n FROM dashboard_jobs")["n"] == 0  # and the database answers
    finally:
        state.worker.stop()


def test_import_settings_restarts_the_worker_even_when_the_restore_fails(tmp_home: Path):
    import io

    from ns_dashboard.fakes import FakeBackupAdapter

    dash, state = make(tmp_home)
    assert isinstance(state.adapters.backup, FakeBackupAdapter)
    state.adapters.backup.fail_restore = True
    state.worker.start()
    try:
        assert code_of(dash.settings.import_settings, "backup.zip", io.BytesIO(b"x")) == "restore_failed"
        assert state.worker.started
        assert state.db.one("SELECT 1 AS one")["one"] == 1
    finally:
        state.worker.stop()


def test_import_settings_is_refused_while_a_job_runs(tmp_home: Path):
    import io

    dash, state = make(tmp_home)
    job = state.store.create("verify", None, {})
    state.store.mark_running(job.job_id)
    assert code_of(dash.settings.import_settings, "backup.zip", io.BytesIO(b"x")) == "job_running"


# ------------------------------------------------------------------ row 13: saved Assistant requests are answered by themselves


def test_saved_assistant_request_is_answered_when_the_job_ends(tmp_home: Path):
    """'I saved your request and will answer when the engine is free' needed the owner to press a button."""
    from ns_common.paths import Paths
    from ns_dashboard.assistant.llm import ScriptedToolLlm, StaticProvider, say
    from ns_dashboard.handlers import build_state
    from ns_dashboard.supervisor_link import SupervisorLink

    state = build_state(
        Paths.from_env(), fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    )
    dash = Dashboard(state, llm_provider=StaticProvider(ScriptedToolLlm([say("It is Tuesday.")])))
    cid = dash.assistant_store.new_conversation()
    dash.assistant_store.add_message(cid, "user", "What day is it?")
    dash.assistant_store.queue_message(cid, "What day is it?", {})
    job = state.store.create("verify", None, {})
    state.worker.enqueue(job.job_id)
    state.worker.start()
    try:
        assert state.worker.wait_idle(20)
        deadline = __import__("time").monotonic() + 20
        while dash.assistant_store.queued() and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.05)
    finally:
        state.worker.stop()
    assert dash.assistant_store.queued() == []
    texts = [m["content"] for m in dash.assistant_store.messages(cid) if m["role"] == "assistant"]
    assert "It is Tuesday." in texts


# ------------------------------------------------------------------ row 11: the Caption look reaches the clip job


def test_caption_look_chosen_in_create_reaches_clipparams(monkeypatch: pytest.MonkeyPatch):
    """`caption_style` was stored in the job row and never read by the studio."""
    import dataclasses

    from ns_dashboard import studio_bridge
    from ns_studio.clipper import models

    @dataclasses.dataclass
    class WithStyle(models.ClipParams):  # what ClipParams looks like once the Clipper has the field
        caption_style: str | None = None

    monkeypatch.setattr(models, "ClipParams", WithStyle)
    p = studio_bridge.clip_params_of(
        Path("a.mp4"), "after-dark",
        {"template": "podcast_single", "max_clips": 3, "min_s": 20, "max_s": 45, "caption_style": "boxed_dark", "rights_confirmed": True},
    )
    assert p.caption_style == "boxed_dark" and p.template == "podcast_single" and p.max_clips == 3
    assert studio_bridge.clip_params_of(Path("a.mp4"), "c", {}).caption_style is None  # type: ignore[attr-defined]


def test_clip_params_without_a_caption_field_still_build(monkeypatch: pytest.MonkeyPatch):
    from ns_dashboard import studio_bridge

    p = studio_bridge.clip_params_of(Path("a.mp4"), "after-dark", {"caption_style": "boxed_dark", "rights_confirmed": True})
    assert p.channel == "after-dark" and p.rights_confirmed


# ------------------------------------------------------------------ row 22: formats say which channels use them


def test_formats_list_the_channels_that_may_use_them(tmp_home: Path):
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths, "after-dark", formats_allowed=["short_9x16"])
    seed_channel(state.db, state.paths, "open-door")  # empty list: every preset
    shipped = dash.library.list_formats()
    assert shipped
    by_id = {f["id"]: f for f in shipped}
    assert all("channels" in f for f in shipped)
    assert by_id["short_9x16"]["channels"] == ["after-dark", "open-door"]
    other = next(f for f in shipped if f["id"] != "short_9x16")
    assert other["channels"] == ["open-door"]


# ------------------------------------------------------------------ row 21: footage rows carry clip_id and a use count


def test_footage_rows_count_their_uses(tmp_home: Path):
    dash, state = make(tmp_home)
    now = "2026-11-02T00:00:00+00:00"
    for cid in ("1", "2"):
        state.db.execute(
            "INSERT INTO clips(source, clip_id, url, license, width, height, duration, local_path, fetched_at, keywords)"
            " VALUES ('pexels', ?, 'https://x', 'Pexels', 1920, 1080, 9.5, 'c.mp4', ?, '[]')", (cid, now),
        )
    for item in ("long", "s1"):
        state.db.execute(
            "INSERT INTO clip_usage(source, clip_id, channel, job_id, item, used_at) VALUES ('pexels', '1', 'c', 'j', ?, ?)",
            (item, now),
        )
    rows = {r["clip_id"]: r for r in dash.library.footage()}
    assert rows["1"]["uses"] == 2 and rows["2"]["uses"] == 0 and rows["2"]["last_used"] is None


# ------------------------------------------------------------------ row 24: jobs carry step and steps


def test_job_dict_has_step_and_steps_from_the_progress_message(tmp_home: Path):
    dash, state = make(tmp_home)
    job = state.store.create("run", "after-dark", {})
    state.store.mark_running(job.job_id)
    state.store.update_progress(job.job_id, 0.25, "voice (2/8)")
    d = state.store.get(job.job_id).to_dict()  # type: ignore[union-attr]
    assert (d["stage"], d["step"], d["steps"]) == ("voice", 2, 8)
    state.store.update_progress(job.job_id, 0.3, "Downloading 30%")
    d = state.store.get(job.job_id).to_dict()  # type: ignore[union-attr]
    assert (d["stage"], d["step"], d["steps"]) == ("download", None, None)
    assert dash.jobs.get(job.job_id)["steps"] is None


# ------------------------------------------------------------------ row 16: uploads are spooled under <home>\tmp


def test_server_spools_uploads_under_the_home_tmp(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """Starlette spooled every multipart upload to %TEMP% on C: before `import_stream` copied it again."""
    import tempfile

    from ns_common.paths import Paths
    from ns_dashboard.server import configure_tmp

    monkeypatch.setattr(tempfile, "tempdir", None)
    stale = tmp_home / "tmp" / "old.part"
    stale.parent.mkdir()
    stale.write_bytes(b"x")
    folder = configure_tmp(Paths.from_env())
    assert folder == tmp_home / "tmp" and tempfile.gettempdir() == str(folder)
    assert not stale.exists()
    with tempfile.SpooledTemporaryFile(max_size=1) as f:  # what Starlette's multipart parser uses
        f.write(b"abc")
        assert Path(f._file.name).parent == folder  # type: ignore[attr-defined]


# ------------------------------------------------------------------ gap 20: the run time is the registered task's


def test_run_time_is_read_from_the_registered_schedule(tmp_home: Path):
    from ns_supervisor.schedule_cfg import write_run_time

    dash, state = make(tmp_home)
    assert dash.settings.get_settings()["schedule_time"] == "03:00"
    write_run_time(state.paths, "04:30")  # `ns schedule install --time 04:30` ran
    assert dash.settings.get_settings()["schedule_time"] == "04:30"
    assert dash.engine.status()["schedule_time"] == "04:30"


# ------------------------------------------------------------------ row 17: Results after a reload


def test_learn_summary_has_what_the_results_page_draws(tmp_home: Path):
    """The page read imported_at, matched, top, bottom, daily, milestone and changes; the API had none of them."""
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)
    short = seed_output(state.db, state.paths, kind="short", k=1, title="Floor 13")
    long_ = seed_output(state.db, state.paths, kind="long", k=1, title="The Lift")
    for oid, day, views, pct in ((short, "2026-11-01", 900, 61.5), (short, "2026-11-02", 100, 58.0), (long_, "2026-11-02", 40, 33.0)):
        state.db.execute(
            "INSERT INTO analytics(output_id, date, views, viewed_pct, avg_view_duration_s, subs_gained) VALUES (?, ?, ?, ?, 120, 2)",
            (oid, day, views, pct),
        )
    job = state.store.create("learn", None, {})
    state.store.mark_running(job.job_id)
    state.store.finish(job.job_id, "done", "Done", {"rows": 2, "matched": 2, "unmatched": ["x"], "changes": [{"pool": "hooks", "option": "question", "delta": 0.2}]})
    s = dash.settings.learn_summary()
    assert s["imported_at"] and s["matched"] == 2 and s["unmatched"] == 1
    assert s["changes"] == [{"pool": "hooks", "option": "question", "delta": 0.2}]
    assert [r["title"] for r in s["top"]] == ["Floor 13", "The Lift"] and s["top"][0]["views"] == 100
    assert s["bottom"] == []
    assert len(s["daily"]) == 14 and s["daily"][-1] == {"date": "2026-11-02", "shorts": 100, "long": 40}
    assert s["daily"][-2]["shorts"] == 900
    assert s["summary"] and set(s["milestone"]) == {"label", "rule", "current", "target", "weeks", "weeks_needed"}
    assert s["milestone"]["weeks_needed"] == 8


def test_learn_summary_without_an_import_is_empty_but_complete(tmp_home: Path):
    dash, _state = make(tmp_home)
    s = dash.settings.learn_summary()
    assert s["imported_at"] is None and s["top"] == [] and s["daily"] == [] and s["milestone"] is None


# ------------------------------------------------------------------ row 3: the update runs detached; the page follows it


def test_update_output_of_ns_update_github_is_understood():
    """`parse_update_output` read 'Now on X (was Y)' as latest=old, current=new, state unknown."""
    from ns_dashboard.updater import classify_update, parse_update_output

    text = classify_update(parse_update_output(0, "downloading...\nNow on 0.2.0 (was 0.1.0)\n", ""))
    assert text["state"] == "updated" and (text["old"], text["new"]) == ("0.1.0", "0.2.0")
    assert "0.2.0" in text["message"] and "0.1.0" in text["message"]
    js = classify_update(parse_update_output(0, 'x\n{"updated": true, "old": "0.1.0", "new": "0.1.1"}\n', ""))
    assert js["state"] == "updated" and js["new"] == "0.1.1"
    started = classify_update(parse_update_output(0, '{"started": true}', ""))
    assert started["state"] == "started"


def test_update_state_file_is_read_from_run_update_state_json(tmp_home: Path):
    import json

    dash, state = make(tmp_home)
    assert dash.library.update_state()["state"] == "idle"
    f = state.paths.home / "run" / "update_state.json"
    f.parent.mkdir(parents=True, exist_ok=True)  # the dashboard's start checked studio.db: run/db-check.json
    f.write_text(json.dumps({"state": "running", "old": "0.1.0", "new": "0.1.1", "message": "Installing"}), encoding="utf-8")
    assert dash.library.update_state() == {"state": "running", "old": "0.1.0", "new": "0.1.1", "message": "Installing", "updated_at": None}
    f.write_text("{not json", encoding="utf-8")
    assert dash.library.update_state()["state"] == "idle"
    f.write_text(json.dumps({"state": "done", "old": "0.1.0", "new": "0.1.1"}), encoding="utf-8")
    client_state = dash.library.update_state()
    assert client_state["state"] == "done" and client_state["new"] == "0.1.1"


# ------------------------------------------------------------------ row 4: channel cards carry what the cards show


def test_channel_card_has_cadence_window_and_caption_looks(tmp_home: Path):
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths, masters_per_week=2, publish_window="19:00-22:00")
    (card,) = dash.channels.cards()
    assert card["masters_per_week"] == 2 and card["publish_window"] == "19:00-22:00"
    assert card["caption_styles"] == ["punch_yellow", "clean_white"] and card["formats_allowed"] == []


def test_channel_put_accepts_the_profile_the_get_returned(tmp_home: Path):
    """The editor started from the card and PUT card keys (`approved`, `last_output_at`): 422 extra_forbidden."""
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths, masters_per_week=3, avoid_topics=["war"])
    profile = dash.channels.get("after-dark")["profile"]
    profile["niche"] = "edited niche about doorbells"
    saved = dash.channels.update("after-dark", profile)["profile"]
    assert saved["masters_per_week"] == 3 and saved["avoid_topics"] == ["war"]
    card = dash.channels.cards()[0]
    with pytest.raises(ApiError) as err:  # the card is not a profile
        dash.channels.update("after-dark", card)
    assert err.value.code == "invalid_profile"


# ------------------------------------------------------------------ gap 23: the three thumbnails of a long video are served


def test_thumbnail_choices_are_served(tmp_home: Path):
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    seed_channel(state.db, state.paths)
    long_ = seed_output(state.db, state.paths, kind="long", k=1)
    folder = Path(state.db.one("SELECT path FROM outputs WHERE output_id = ?", (long_,))["path"]).parent
    (folder / "thumb_1.jpg").write_bytes(b"\xff\xd8jpeg1")
    (folder / "thumb_3.jpg").write_bytes(b"\xff\xd8jpeg3")
    r = client.get(f"/api/thumbs/1/{long_}")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content.endswith(b"jpeg1")
    assert client.get(f"/api/thumbs/2/{long_}").status_code == 404  # not made
    assert client.get(f"/api/thumbs/4/{long_}").status_code == 404
    assert client.get("/api/thumbs/1/nope-20260101-abcd/long").status_code == 404


# ------------------------------------------------------------------ row 8: Settings > Models edits plain values


def test_engine_flags_round_trip_as_dotted_plain_values(tmp_home: Path):
    """Every value was a nested object shown as `[object Object]` and saved back as that text (422)."""
    from ns_common.config import load_runtime, save_runtime
    from ns_common.models import Runtime

    dash, state = make(tmp_home)
    save_runtime(state.paths, Runtime.default("cpu-dev"))
    flags = dash.settings.get_settings()["flags"]
    assert flags and all(isinstance(v, str | int | float | bool) for v in flags.values())
    assert {"llama.flags.ctx", "video.encoder", "tts.port", "whisper.model"} <= set(flags)
    # what the page sends: every value as text, only one changed
    sent = {k: str(v) for k, v in flags.items()}
    sent["llama.flags.ctx"] = "8192"
    sent["video.fps"] = "24"
    out = dash.settings.put_settings({"flags": sent}, unlock=True)
    rt = load_runtime(state.paths)
    assert rt.llama.flags.ctx == 8192 and rt.video.fps == 24
    assert rt.tts.port == flags["tts.port"] and rt.llama.flags.kv_type == flags["llama.flags.kv_type"]
    assert out["flags"]["llama.flags.ctx"] == 8192
    assert code_of(dash.settings.put_settings, {"flags": {"video.encoder": "divx"}}, unlock=True) == "invalid_runtime"


# ------------------------------------------------------------------ row 2: Library > Stories rows


def test_story_rows_have_title_words_status_and_a_nullable_critic(tmp_home: Path):
    """The page called `s.words.toLocaleString()` and `s.critic_avg.toFixed()` on rows that had neither."""
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)
    oid = seed_output(state.db, state.paths, kind="short", k=1, title="Floor 13")
    job = oid.split("/")[0]
    now = "2026-11-02T00:00:00+00:00"
    state.db.execute(
        "INSERT INTO stories(job_id, channel, format, working_title, text, word_count, opening_line, critic_avg, revisions, created_at)"
        " VALUES (?, 'after-dark', 'shorts_only', NULL, 't', 120, 'It began at three.', NULL, 0, ?)", (job, now),
    )
    (row,) = dash.library.stories()
    assert row["title"] == "It began at three." and row["words"] == 120 and row["critic_avg"] is None
    assert row["status"] == "pending" and [o["output_id"] for o in row["outputs"]] == [oid]
    dash.outputs.approve(oid)
    assert dash.library.stories()[0]["status"] == "approved"
    dash.outputs.mark_uploaded(oid, "https://youtu.be/x")
    assert dash.library.stories()[0]["status"] == "posted"


def test_story_status_words():
    from ns_dashboard.svc_library import story_status

    assert story_status([]) == "none" and story_status(["rejected", "rejected"]) == "rejected"
    assert story_status(["rejected", "approved"]) == "approved" and story_status(["uploaded", "pending"]) == "pending"


# ------------------------------------------------------------------ row 23: the Assistant's labels come from the registry


def test_assistant_tools_route_lists_the_registry(tmp_home: Path):
    from tests.test_dashboard_api import make_env

    client, _state = make_env(tmp_home)
    tools = client.get("/api/assistant/tools").json()
    by_name = {t["name"]: t for t in tools}
    # the names the page used to guess (list_outputs, start_run, start_clips, save_ideas, library_ideas) do not exist
    for real in ("list_queue", "start_story_job", "start_clip_job", "add_idea", "list_ideas", "pause_channel"):
        assert real in by_name and by_name[real]["description"]
    assert by_name["start_story_job"]["side_effect"] is True and by_name["list_queue"]["side_effect"] is False
    for guessed in ("list_outputs", "start_run", "start_clips", "save_ideas", "library_ideas"):
        assert guessed not in by_name


# ------------------------------------------------------------------ gap: Settings > Models "Get voice design model" / "Get story model"


def test_model_download_is_unavailable_until_ns_models_exists_then_runs_in_the_background(tmp_home: Path):
    import time

    from ns_dashboard.fakes import FakeUpdateAdapter

    dash, state = make(tmp_home)
    update = state.adapters.update
    assert isinstance(update, FakeUpdateAdapter)
    with pytest.raises(ApiError) as err:
        dash.library.models_get("voicedesign")
    assert err.value.status == 501 and err.value.code == "models_unavailable"
    assert code_of(dash.library.models_get, "nope") == "unknown_model"
    update.models_command = True  # type: ignore[attr-defined]
    assert dash.library.models_get("voicedesign")["state"] == "running"
    deadline = time.monotonic() + 10
    while dash.library.models_state()["voicedesign"]["state"] == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    assert dash.library.models_state()["voicedesign"] == {"state": "done", "message": "voicedesign is ready"}
    assert update.downloaded == ["voicedesign"]  # type: ignore[attr-defined]


# ------------------------------------------------------------------ gap 26: Library > Footage delete


def test_footage_can_be_deleted_with_its_file(tmp_home: Path):
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    video = state.paths.home / "cache" / "clips" / "pexels" / "1.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"x")
    outside = tmp_home.parent / "elsewhere.mp4"
    outside.write_bytes(b"x")
    for cid, path in (("1", video), ("2", outside)):
        state.db.execute(
            "INSERT INTO clips(source, clip_id, url, license, local_path, fetched_at, keywords) VALUES ('pexels', ?, 'u', 'l', ?, 'now', '[]')",
            (cid, str(path)),
        )
    state.db.execute("INSERT INTO clip_usage(source, clip_id, channel, job_id, item, used_at) VALUES ('pexels', '1', 'c', 'j', 'long', 'now')")
    r = client.delete("/api/library/footage/pexels/1")
    assert r.status_code == 200 and r.json()["file_removed"] is True and not video.exists()
    assert state.db.one("SELECT COUNT(*) AS n FROM clip_usage")["n"] == 0
    assert client.delete("/api/library/footage/pexels/1").status_code == 404
    assert client.delete("/api/library/footage/pexels/2").json()["file_removed"] is False and outside.exists()  # not ours to delete


# ------------------------------------------------------------------ gap 31: a clip result says which layout was chosen


def test_clip_result_records_the_chosen_layout_and_a_missing_face_cam(tmp_home: Path):
    import json

    from ns_dashboard.svc_jobs import read_clip_layout

    dash, state = make(tmp_home)
    jd = state.paths.job_dir("clips-20261102-aaaa")
    jd.mkdir(parents=True)
    (jd / "clip_layout.json").write_text(
        json.dumps({"template": "center_crop", "facecam": None, "per_clip": {"1": "center_crop"}, "notes": {"1": "NS-W402: no face cam"}}),
        encoding="utf-8",
    )
    got = read_clip_layout(state.paths, "clips-20261102-aaaa")
    assert got == {"template": "center_crop", "per_clip": {"1": "center_crop"}, "notes": {"1": "NS-W402: no face cam"}, "no_facecam": True}
    assert read_clip_layout(state.paths, "nope") is None and read_clip_layout(None, "x") is None


# ------------------------------------------------------------------ morning notes (a toast that could not show)


def test_morning_notes_are_listed_and_cleared(tmp_home: Path):
    import json

    dash, state = make(tmp_home)
    assert dash.engine.notes() == []
    f = state.paths.home / "run" / "morning_note.json"
    f.parent.mkdir(parents=True, exist_ok=True)  # the dashboard's start checked studio.db: run/db-check.json
    f.write_text(json.dumps({"notes": [{"at": "2026-11-03T03:30:00", "title": "Nightshift", "message": "2 videos are ready"}, "junk"]}), encoding="utf-8")
    # FN-2: an entry that is no note makes the file a damaged one: it is kept aside and the bell says so
    first, said = dash.engine.notes()
    assert {k: first[k] for k in ("at", "title", "message")} == {"at": "2026-11-03T03:30:00", "title": "Nightshift", "message": "2 videos are ready"}
    assert said["message"].startswith("Nightshift could not read its waiting notes") and len(list(f.parent.glob("morning_note.json.bad-*"))) == 1
    assert dash.engine.dismiss_notes({"ids": [first["id"], said["id"]]}) == {"ok": True, "removed": 2, "left": 0}
    assert not f.exists() and dash.engine.notes() == []
    f.write_text("{broken", encoding="utf-8")
    (told,) = dash.engine.notes()
    assert told["message"].startswith("Nightshift could not read its waiting notes")

