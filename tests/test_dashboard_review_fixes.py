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
