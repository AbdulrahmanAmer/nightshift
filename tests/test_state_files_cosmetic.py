"""C2/C1: update_state.json is written with retries and read with a dead-pid check; schedule.json only
exists for a registered task; the deferred uninstall waits for a dashboard window.
"""

from __future__ import annotations

import ctypes
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from installer.stage2.web import uninstall
from ns_common import atomic
from ns_common.paths import Paths
from ns_supervisor import update_job

win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


# ----------------------------------------------------------------------- update_state.json: writing
def test_the_state_file_is_replaced_with_retries_while_a_reader_has_it_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths(tmp_path / "home")
    real = atomic.os.replace
    refused = {"n": 3}

    def busy(src: Any, dst: Any) -> None:
        if refused["n"] > 0:
            refused["n"] -= 1
            raise PermissionError(5, "Access is denied")      # the dashboard's 3-second poll has it open
        real(src, dst)

    monkeypatch.setattr(atomic.os, "replace", busy)
    monkeypatch.setattr(atomic.time, "sleep", lambda s: None)
    update_job.write_status(paths, "running", "Updating.", pid=4242)
    assert refused["n"] == 0
    assert update_job.read_status(paths)["pid"] == 4242
    assert not list(update_job.status_file(paths).parent.glob("*.tmp"))


def test_a_state_file_that_stays_locked_does_not_kill_the_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths(tmp_path / "home")
    calls: list[str] = []

    def locked(p: Paths, state: str, message: str = "", **extra: Any) -> None:
        calls.append(state)
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(update_job, "write_status", locked)
    monkeypatch.setattr(update_job, "FINAL_PAUSE_S", 0)
    ran: list[int] = []

    def body() -> update_job.Outcome:
        ran.append(1)
        return update_job.Outcome("Now on 0.2.0 (was 0.1.0).", "0.1.0", "0.2.0")

    log = paths.logs / "u.log"
    assert update_job.run_worker(paths, body, log=log) == 0     # the first write used to be outside the try
    assert ran == [1]
    assert calls.count("running") == 2 and calls.count("done") == update_job.FINAL_TRIES
    assert "could not write update_state.json" in log.read_text(encoding="utf-8")


# ----------------------------------------------------------------------- update_state.json: reading
def running(pid: int | None, updated: datetime) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "state": "running", "old": "0.1.0", "new": "", "message": "Updating.",
        "updated_at": updated.isoformat(timespec="seconds"),
    }
    if pid is not None:
        raw["pid"] = pid
    return raw


def test_a_running_state_whose_worker_is_gone_reads_as_failed() -> None:
    now = datetime(2026, 11, 2, 12, 0, 0)
    gone = update_job.effective_status(running(999, now), alive=lambda pid: False)
    assert gone["state"] == "failed" and gone["exit_code"] == 1
    assert "stopped before it finished" in gone["message"] and gone["old"] == "0.1.0"
    assert update_job.effective_status(running(999, now), alive=lambda pid: True)["state"] == "running"


def test_a_state_written_before_the_worker_started_is_believed_only_for_a_while() -> None:
    now = datetime(2026, 11, 2, 12, 0, 0)
    fresh = running(None, now - timedelta(seconds=20))
    stale = running(None, now - timedelta(seconds=update_job.START_GRACE_S + 30))
    assert update_job.effective_status(fresh, now=now)["state"] == "running"
    assert update_job.effective_status(stale, now=now)["state"] == "failed"
    nothing = {"state": "running", "message": "Installing"}         # no pid, no time: nothing to judge by
    assert update_job.effective_status(nothing, now=now) == nothing


def test_finished_states_are_left_alone() -> None:
    for state in ("done", "failed"):
        raw = {"state": state, "pid": 1, "message": "x"}
        assert update_job.effective_status(raw, alive=lambda pid: False) == raw


def test_worker_alive_tells_a_live_python_from_a_finished_process() -> None:
    import os

    assert update_job.worker_alive(os.getpid()) is True
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert update_job.worker_alive(child.pid) is False


def test_the_dashboard_reader_reports_a_dead_updater_as_failed(tmp_path: Path) -> None:
    from ns_dashboard.updater import read_update_state

    paths = Paths(tmp_path / "home")
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    f = update_job.status_file(paths)
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps(running(child.pid, datetime.now())), encoding="utf-8")
    out = read_update_state(paths)
    assert out["state"] == "failed" and "stopped before it finished" in str(out["message"])


# ----------------------------------------------------------------------- the deferred uninstall delete
def test_the_deferred_script_waits_for_the_dashboard_window_and_lists_what_remains(tmp_path: Path) -> None:
    home, local = tmp_path / "ns", tmp_path / "Local" / "Nightshift"
    script = uninstall.deferred_script(home, [5], local)
    assert uninstall.HOST_MUTEX == "Global\\Nightshift.AppHost.dashboard"
    assert f"'{uninstall.HOST_MUTEX}'" in script and "TryOpenExisting" in script
    assert f"AddMinutes({uninstall.HOST_WAIT_MIN})" in script and uninstall.HOST_WAIT_MIN == 10
    for leaf in (home / "bin", local / "browser", local / "webview2"):
        assert f"'{leaf}'" in script
    assert uninstall.LEFTOVERS_FILE in script and script.rstrip().endswith("exit 0")
    assert "browser" not in uninstall.deferred_script(home, [5])           # only with a local root


def test_the_host_mutex_is_the_one_the_window_host_takes() -> None:
    host = Path(__file__).resolve().parents[1] / "launchers" / "nightshift-host.ps1"
    text = host.read_text(encoding="ascii")
    assert 'Global\\Nightshift.AppHost.$Role' in text and uninstall.HOST_MUTEX.endswith(".dashboard")


def test_the_result_names_the_file_that_lists_leftovers(tmp_path: Path) -> None:
    home = tmp_path / "ns"
    (home / "bin").mkdir(parents=True)
    app = uninstall.UninstallApp(Path("D:/ns-app"), home, deferred=lambda h: True, env_var=lambda n: None)
    removed, _kept = app._outcome(home, False, False, failed=False)
    (line,) = [r for r in removed if r.startswith("Programs and Python environments (the last files go")]
    assert "every Nightshift window is closed" in line and uninstall.LEFTOVERS_FILE in line


def test_the_uninstall_script_leaves_the_browser_files_to_the_deferred_delete() -> None:
    ps1 = Path(__file__).resolve().parents[1] / "installer" / "uninstall.ps1"
    text = ps1.read_text(encoding="ascii")
    assert "if ($KeepBin) { $leaves = @('bootstrap') }" in text


@win_only
def test_the_deferred_script_is_valid_powershell(tmp_path: Path) -> None:
    import shutil

    exe = shutil.which("powershell")
    if exe is None:
        pytest.skip("needs Windows PowerShell")
    script = tmp_path / "deferred.ps1"
    text = uninstall.deferred_script(tmp_path / "ns", [1, 2], tmp_path / "Local")
    script.write_text(text, encoding="utf-8")
    probe = (
        "$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script}', [ref]$null, [ref]$e); exit $e.Count"
    )
    assert subprocess.run([exe, "-NoProfile", "-Command", probe], check=False).returncode == 0


@win_only
def test_a_dashboard_window_delays_the_deferred_delete_until_it_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    exe = shutil.which("powershell")
    if exe is None:
        pytest.skip("needs Windows PowerShell")
    monkeypatch.setattr(uninstall, "HOST_WAIT_MIN", 0.12)               # 7 seconds instead of 10 minutes
    home = tmp_path / "ns"
    (home / "bin").mkdir(parents=True)
    script = uninstall.deferred_script(home, [])

    def run() -> float:
        start = time.monotonic()
        done = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script], check=False)
        assert done.returncode == 0
        return time.monotonic() - start

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, uninstall.HOST_MUTEX)
    assert handle
    try:
        with_window = run()
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
    assert with_window >= 6                                              # it polled until the limit
    assert (home / "bin").exists() is False                              # then removed what it could
    (home / "bin").mkdir(parents=True)             # the script removed the empty home too
    assert run() < with_window                                           # no window: no waiting


# ----------------------------------------------------------------------- schedule.json
def test_a_refused_scheduler_leaves_no_schedule_file_but_keeps_the_chosen_time(tmp_path: Path) -> None:
    from installer.stage2.steps.s09_shortcuts_scheduler import remember_run_time
    from ns_supervisor.schedule_cfg import read_run_time, schedule_file

    paths = Paths(tmp_path / "ns")
    paths.ensure()
    (paths.config / "dashboard.json").write_text(
        json.dumps({"run_time": "03:00", "notifications": False}), encoding="utf-8"
    )
    remember_run_time(paths, "02:15")
    saved = json.loads((paths.config / "dashboard.json").read_text(encoding="utf-8"))
    assert saved == {"run_time": "02:15", "notifications": False}          # other settings kept
    assert not schedule_file(paths).exists() and read_run_time(paths) is None
    fresh = Paths(tmp_path / "fresh")
    remember_run_time(fresh, "04:00")
    assert json.loads((fresh.config / "dashboard.json").read_text(encoding="utf-8")) == {"run_time": "04:00"}
