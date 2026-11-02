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
