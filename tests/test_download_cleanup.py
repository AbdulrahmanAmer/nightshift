"""D-9 (box F-B5): a cancelled link download leaves no `.part` file behind.

yt-dlp is replaced by a fake process that keeps `source.mp4.part` open for a moment after it is killed, which
is what made `rmtree(ignore_errors=True)` fail silently on Windows. No network and no real yt-dlp.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_dashboard import studio_bridge
from ns_dashboard.adapters import RealClipAdapter
from ns_dashboard.events import EventHub
from ns_dashboard.handlers import build_state
from ns_dashboard.jobs import JobCancelled, JobControl, JobStore
from ns_dashboard.state import open_db
from ns_dashboard.supervisor_link import SupervisorLink
from ns_studio.clipper import download_cleanup as dc
from ns_studio.clipper import source as src

URL = "https://example.com/watch?v=abc123"
HOLD_S = 0.3


class HeldPartPopen:
    """A fake yt-dlp: writes a `.part` file and keeps it "open" until `HOLD_S` after it was killed."""

    instances: list[HeldPartPopen] = []

    def __init__(self, cmd: list[str], **_kw: Any) -> None:
        HeldPartPopen.instances.append(self)
        self.dest = Path(cmd[cmd.index("-o") + 1]).parent
        self.dest.mkdir(parents=True, exist_ok=True)
        (self.dest / "source.mp4.part").write_bytes(b"half")
        self.held = True
        self.killed = False
        self.stdout = iter(
            ["[download]  12.5% of 10.00MiB at  2.00MiB/s ETA 00:04\n", "[download]  50.0% of 10.00MiB\n"]
        )

    def poll(self) -> int | None:
        return -9 if self.killed else None

    def kill(self) -> None:
        self.killed = True
        timer = threading.Timer(HOLD_S, self.release)
        timer.daemon = True
        timer.start()

    def release(self) -> None:
        self.held = False

    def wait(self) -> int:
        return -9 if self.killed else 0


@pytest.fixture
def held_part(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Make the folder removal fail with PermissionError while the fake yt-dlp still holds its file."""
    HeldPartPopen.instances = []
    seen: dict[str, Any] = {"attempts": 0, "killed_at_first_attempt": None}

    def locked_rmtree(folder: Path) -> None:
        seen["attempts"] += 1
        proc = HeldPartPopen.instances[-1] if HeldPartPopen.instances else None
        if seen["killed_at_first_attempt"] is None and proc is not None:
            seen["killed_at_first_attempt"] = proc.killed
        if proc is not None and proc.held:
            raise PermissionError(32, "the file is being used by another process", str(folder))
        import shutil

        shutil.rmtree(folder)

    monkeypatch.setattr(dc, "_rmtree", locked_rmtree)
    monkeypatch.setattr(dc, "_sleep", lambda s: threading.Event().wait(min(s, 0.05)))
    monkeypatch.setattr(src, "subprocess", SimpleNamespace(Popen=HeldPartPopen, PIPE=-1, STDOUT=-2))
    return seen


def fake_exe(home: Path) -> Path:
    exe = home / "bin" / "yt-dlp" / "yt-dlp.exe"
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_bytes(b"MZ")
    return exe


# ---------- the shared helper ----------
def test_remove_folder_retries_with_a_growing_pause_until_the_file_is_released(tmp_path: Path) -> None:
    folder = tmp_path / "dl"
    folder.mkdir()
    sleeps: list[float] = []
    failures = [3]

    def rmtree(path: Path) -> None:
        if failures[0] > 0:
            failures[0] -= 1
            raise PermissionError(32, "in use", str(path))
        path.rmdir()

    assert dc.remove_folder(folder, backoff_s=0.1, sleep=sleeps.append, rmtree=rmtree) is True
    assert sleeps == pytest.approx([0.1, 0.2, 0.3])
    assert not folder.exists()


def test_remove_folder_that_is_already_gone_is_success(tmp_path: Path) -> None:
    assert dc.remove_folder(tmp_path / "nope", sleep=lambda _s: None) is True


def test_remove_folder_logs_the_path_through_nslogger_when_it_still_fails(tmp_path: Path) -> None:
    folder = tmp_path / "dl"
    folder.mkdir()
    log = NsLogger("studio", tmp_path / "logs", echo=False)

    def rmtree(path: Path) -> None:
        raise PermissionError(32, "in use", str(path))

    assert dc.remove_folder(folder, log=log, retries=2, sleep=lambda _s: None, rmtree=rmtree) is False
    (logfile,) = (tmp_path / "logs").glob("studio-*.jsonl")
    rec = json.loads(logfile.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["event"] == "download_cleanup_failed" and rec["level"] == "WARN"
    assert rec["data"]["path"] == str(folder) and rec["data"]["attempts"] == 3
    assert folder.exists()  # nothing raised, the caller carries on


def test_kill_process_tree_kills_the_tree_first_then_the_process_then_reaps_it() -> None:
    order: list[str] = []

    class Proc:
        pid = 4242

        def poll(self) -> int | None:
            return None

        def kill(self) -> None:
            order.append("kill")

        def wait(self) -> int:
            order.append("wait")
            return -9

    dc.kill_process_tree(Proc(), taskkill=lambda pid: order.append(f"tree:{pid}"))
    assert order == ["tree:4242", "kill", "wait"]


def test_the_tree_kill_goes_through_the_supervisors_helper(monkeypatch: pytest.MonkeyPatch) -> None:
    from ns_supervisor import procs

    pids: list[int] = []
    monkeypatch.setattr(procs, "kill_tree", pids.append)
    dc._taskkill(777)
    assert pids == [777]


def test_kill_process_tree_leaves_an_exited_process_alone() -> None:
    order: list[str] = []

    class Gone:
        pid = 1

        def poll(self) -> int | None:
            return 0

        def kill(self) -> None:
            order.append("kill")

        def wait(self) -> int:
            order.append("wait")
            return 0

    dc.kill_process_tree(Gone(), taskkill=lambda pid: order.append("tree"))
    assert order == ["wait"]


@pytest.mark.skipif(sys.platform != "win32", reason="an open file only blocks its removal on Windows")
def test_a_really_open_part_file_blocks_the_removal_until_it_is_closed(tmp_path: Path) -> None:
    folder = tmp_path / "dl"
    folder.mkdir()
    handle = (folder / "source.mp4.part").open("wb")
    threading.Timer(HOLD_S, handle.close).start()
    assert dc.remove_folder(folder, backoff_s=0.1, retries=20) is True
    assert not folder.exists()


# ---------- import_url ----------
def test_a_cancelled_download_removes_the_folder_after_the_part_file_is_released(
    tmp_path: Path, held_part: dict[str, Any]
) -> None:
    exe = fake_exe(tmp_path / "home")
    token = src.CancelToken()

    def progress(percent: float) -> None:
        if percent >= 12:
            token.cancel()  # the dashboard's Cancel button

    dest = tmp_path / "dl"
    with pytest.raises(NsError) as ei:
        src.import_url(URL, dest, progress, exe=exe, cancel_token=token)
    assert ei.value.code == "NS-E409" and "cancelled" in ei.value.detail
    assert not dest.exists(), "the .part file was left behind"
    assert held_part["attempts"] >= 2, "the first removal hit the open file and had to be retried"
    assert held_part["killed_at_first_attempt"] is True  # the process was killed before any removal


def test_a_progress_callback_that_raises_also_removes_the_folder(
    tmp_path: Path, held_part: dict[str, Any]
) -> None:
    exe = fake_exe(tmp_path / "home")

    class Stop(Exception):
        pass

    def progress(_percent: float) -> None:
        raise Stop

    dest = tmp_path / "dl"
    with pytest.raises(Stop):
        src.import_url(URL, dest, progress, exe=exe)
    assert not dest.exists() and held_part["attempts"] >= 2


def test_a_folder_that_cannot_be_removed_is_logged_and_the_cancel_still_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, held_part: dict[str, Any]
) -> None:
    exe = fake_exe(tmp_path / "home")
    monkeypatch.setattr(HeldPartPopen, "release", lambda self: None)  # the handle is never let go
    log = NsLogger("studio", tmp_path / "logs", echo=False)
    token = src.CancelToken()
    token.cancel()
    dest = tmp_path / "dl"
    with pytest.raises(NsError, match="cancelled"):
        src.import_url(URL, dest, exe=exe, cancel_token=token, log=log)
    assert dest.exists()
    (logfile,) = (tmp_path / "logs").glob("studio-*.jsonl")
    assert "download_cleanup_failed" in logfile.read_text(encoding="utf-8")


# ---------- the dashboard side of the same download ----------
def test_fetch_url_removes_the_folder_with_retries_when_the_job_is_cancelled(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths.from_env()
    store = JobStore(open_db(paths))
    ctl = JobControl(store.create("clip", "after-dark", {}), store, EventHub())
    adapter = RealClipAdapter(paths, SupervisorLink("http://127.0.0.1:9", timeout_s=0.2))
    partial = studio_bridge.download_dir(paths, "src-20261101-dddd")
    partial.mkdir(parents=True)
    (partial / "source.mp4.part").write_bytes(b"x" * 100)
    attempts = {"n": 0}

    def locked_twice(folder: Path) -> None:
        attempts["n"] += 1
        if attempts["n"] <= 2:
            raise PermissionError(32, "in use", str(folder))
        import shutil

        shutil.rmtree(folder)

    def cancelled(*_a: Any, **_kw: Any) -> dict[str, Any]:
        raise JobCancelled

    monkeypatch.setattr(adapter, "child", cancelled)
    monkeypatch.setattr(dc, "_rmtree", locked_twice)
    monkeypatch.setattr(dc, "_sleep", lambda _s: None)
    with pytest.raises(JobCancelled):
        adapter.fetch_url(ctl, URL, "src-20261101-dddd")
    assert not partial.exists() and attempts["n"] == 3


# ---------- the start sweep ----------
def age(path: Path, seconds_ago: float, now: float) -> None:
    os.utime(path, (now - seconds_ago, now - seconds_ago))


def test_sweep_removes_only_old_folders_that_no_live_job_owns(tmp_path: Path) -> None:
    root = tmp_path / "_downloads"
    now = 1_000_000.0
    dead = root / "src-dead"
    live = root / "src-live"
    young = root / "src-young"
    busy = root / "src-busy"
    for folder in (dead, live, young, busy):
        folder.mkdir(parents=True)
        (folder / "source.mp4.part").write_bytes(b"x")
    (root / "notes.txt").write_text("not a folder", encoding="utf-8")
    for folder in (dead, live):
        age(folder / "source.mp4.part", 3600, now)
        age(folder, 3600, now)
    age(young / "source.mp4.part", 30, now)
    age(young, 30, now)
    age(busy, 3600, now)  # the folder itself is old, a file in it was written a minute ago
    age(busy / "source.mp4.part", 60, now)
    removed = dc.sweep_stale_downloads(root, {"src-live"}, now=lambda: now)
    assert removed == [dead]
    assert not dead.exists()
    assert live.exists() and young.exists() and busy.exists() and (root / "notes.txt").exists()


def test_sweep_without_a_downloads_folder_does_nothing(tmp_path: Path) -> None:
    assert dc.sweep_stale_downloads(tmp_path / "missing") == []


def test_the_dashboard_sweeps_leftover_downloads_when_the_worker_starts(tmp_home: Path) -> None:
    paths = Paths.from_env()
    state = build_state(paths, fake_services=True)
    try:
        root = dc.downloads_root(paths)
        old, owned = root / "src-old", root / "src-owned"
        for folder in (old, owned):
            folder.mkdir(parents=True)
            (folder / "source.mp4.part").write_bytes(b"x")
            age(folder / "source.mp4.part", 3600, 1_000_000.0)
            age(folder, 3600, 1_000_000.0)
        state.store.create("clip", "after-dark", {"url": URL, "source_id": "src-owned"})  # queued
        assert state.worker.sweep is not None
        state.worker.sweep(state.store.active_ids(), {})
        assert not old.exists() and owned.exists()
    finally:
        state.db.close()
