"""D-6: approving a rejected video moves its files back to the outbox (the mirror of the reject move)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ns_common.errors import NsError
from ns_dashboard.fakes import seed_channel, seed_output
from tests.test_dashboard_handlers import make


def placeholder(tmp_home: Path) -> Path:
    """A fake video file, so the tests never need FFmpeg."""
    f = tmp_home / "fixtures" / "fake.mp4"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
    return f


def files_under(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def seed(tmp_home: Path, **kw):
    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)
    oid = seed_output(state.db, state.paths, video=placeholder(tmp_home), **kw)
    return dash, state, oid


def row_path(state, oid: str) -> Path:
    row = state.db.one("SELECT path FROM outputs WHERE output_id = ?", (oid,))
    assert row is not None
    return Path(row["path"])


def test_reject_then_approve_puts_the_files_back_under_outbox(tmp_home: Path):
    dash, state, oid = seed(tmp_home, kind="short", k=1, title="The Doorbell Camera")
    original = row_path(state, oid)
    before = sorted(p.relative_to(state.paths.outbox) for p in files_under(state.paths.outbox))
    assert len(before) >= 2  # video plus its sheet

    dash.outputs.reject(oid, "too slow")
    rejected_path = row_path(state, oid)
    assert state.paths.rejected in rejected_path.parents and not original.exists()

    item = dash.outputs.approve(oid)

    assert item["status"] == "approved"
    new_path = row_path(state, oid)
    assert new_path == original and new_path.is_file()
    assert state.paths.outbox in new_path.parents
    after = sorted(p.relative_to(state.paths.outbox) for p in files_under(state.paths.outbox))
    assert after == before  # the video and its sheet files came back
    assert files_under(state.paths.rejected) == []  # nothing is left under rejected
    assert dash.outputs.detail(oid)["folder"] == str(original.parent)
    assert dash.outputs.detail(oid)["file_exists"] is True


def test_approve_after_reject_for_a_long_video_brings_its_thumbnails_back(tmp_home: Path):
    dash, state, oid = seed(tmp_home, kind="long", title="A Long Night")
    original = row_path(state, oid)
    for n in (1, 2, 3):
        (original.parent / f"thumb_{n}.jpg").write_bytes(b"jpg")

    dash.outputs.reject(oid, "bad hook")
    assert not (original.parent / "thumb_1.jpg").exists()
    dash.outputs.approve(oid)

    assert row_path(state, oid) == original
    assert [original.parent / f"thumb_{n}.jpg" for n in (1, 2, 3)] == [
        p for p in (original.parent / f"thumb_{n}.jpg" for n in (1, 2, 3)) if p.is_file()
    ]
    assert files_under(state.paths.rejected) == []


def test_approve_is_idempotent(tmp_home: Path):
    dash, state, oid = seed(tmp_home, kind="short", k=2)
    original = row_path(state, oid)
    dash.outputs.reject(oid, "meh")
    dash.outputs.approve(oid)
    again = dash.outputs.approve(oid)
    assert again["status"] == "approved" and row_path(state, oid) == original and original.is_file()


def test_approving_a_pending_video_touches_no_files(tmp_home: Path):
    dash, state, oid = seed(tmp_home, kind="short", k=3)
    original = row_path(state, oid)
    before = files_under(state.paths.outbox)
    assert dash.outputs.approve(oid)["status"] == "approved"
    assert row_path(state, oid) == original and files_under(state.paths.outbox) == before


def test_approve_picks_up_where_an_interrupted_move_stopped(tmp_home: Path):
    """The video was already moved back (the row update was lost): approve finishes the rest."""
    dash, state, oid = seed(tmp_home, kind="short", k=4)
    original = row_path(state, oid)
    dash.outputs.reject(oid, "meh")
    rejected_video = row_path(state, oid)
    original.parent.mkdir(parents=True, exist_ok=True)
    rejected_video.replace(original)  # the video is back, the sheet files are still in rejected
    assert files_under(state.paths.rejected)

    dash.outputs.approve(oid)

    assert row_path(state, oid) == original and original.is_file()
    assert files_under(state.paths.rejected) == []
    assert original.with_suffix(".json").is_file()


def test_approve_with_the_rejected_files_gone_is_a_plain_error_and_changes_nothing(tmp_home: Path):
    dash, state, oid = seed(tmp_home, kind="short", k=5)
    dash.outputs.reject(oid, "meh")
    rejected_video = row_path(state, oid)
    for p in files_under(rejected_video.parent):
        p.unlink()

    with pytest.raises(NsError) as ei:
        dash.outputs.approve(oid)

    assert ei.value.code == "NS-E326"
    assert "rejected" in str(ei.value) and rejected_video.name in str(ei.value)
    row = state.db.one("SELECT status, path FROM outputs WHERE output_id = ?", (oid,))
    assert row is not None and row["status"] == "rejected" and Path(row["path"]) == rejected_video


def test_approve_does_not_overwrite_a_different_file_in_the_outbox(tmp_home: Path):
    dash, state, oid = seed(tmp_home, kind="short", k=6)
    original = row_path(state, oid)
    dash.outputs.reject(oid, "meh")
    rejected_video = row_path(state, oid)
    original.parent.mkdir(parents=True, exist_ok=True)
    original.write_bytes(b"someone else's file")

    with pytest.raises(NsError) as ei:
        dash.outputs.approve(oid)

    assert ei.value.code == "NS-E326"
    assert original.read_bytes() == b"someone else's file" and rejected_video.is_file()
    row = state.db.one("SELECT status FROM outputs WHERE output_id = ?", (oid,))
    assert row is not None and row["status"] == "rejected"


def test_approve_over_http_moves_the_files_back(tmp_home: Path):
    pytest.importorskip("fastapi")
    from tests.test_dashboard_api import make_env

    client, state = make_env(tmp_home)
    oid = seed_output(state.db, state.paths, video=placeholder(tmp_home), title="Over Http")
    original = row_path(state, oid)
    assert client.post(f"/api/outputs/{oid}/reject", json={"reason": "no"}).json()["status"] == "rejected"
    r = client.post(f"/api/outputs/{oid}/approve")
    assert r.status_code == 200 and r.json()["status"] == "approved"
    assert row_path(state, oid) == original and original.is_file()
    assert files_under(state.paths.rejected) == []


def test_approve_does_not_overwrite_a_different_file_of_the_same_size(tmp_home: Path):
    dash, state, oid = seed(tmp_home, kind="short", k=7)
    original = row_path(state, oid)
    dash.outputs.reject(oid, "meh")
    rejected_video = row_path(state, oid)
    size = rejected_video.stat().st_size
    original.parent.mkdir(parents=True, exist_ok=True)
    other = b"\x01" * size
    original.write_bytes(other)  # same size, different bytes

    with pytest.raises(NsError) as ei:
        dash.outputs.approve(oid)

    assert ei.value.code == "NS-E326"
    assert original.read_bytes() == other and rejected_video.is_file()
    row = state.db.one("SELECT status, path FROM outputs WHERE output_id = ?", (oid,))
    assert row is not None and row["status"] == "rejected" and Path(row["path"]) == rejected_video


def test_two_approves_at_once_both_end_approved_with_the_files_back(tmp_home: Path):
    import threading

    dash, state, oid = seed(tmp_home, kind="short", k=8)
    original = row_path(state, oid)
    dash.outputs.reject(oid, "meh")
    results: list[object] = []

    def go() -> None:
        try:
            results.append(dash.outputs.approve(oid)["status"])
        except Exception as e:  # noqa: BLE001 - the test reports whatever came out
            results.append(e)

    threads = [threading.Thread(target=go) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)

    assert results == ["approved"] * 4
    assert row_path(state, oid) == original and original.is_file()
    assert files_under(state.paths.rejected) == []
