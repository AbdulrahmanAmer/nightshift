"""C2: after a detached update the worker starts Nightshift again and says `done` only once it answers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor import relaunch, update_job
from ns_supervisor.procs import UNDER_ENGINE_ENV


class FakeClient:
    """`/status` answers once `up` is True; `start()` is what brings it up (the fake engine launch)."""

    def __init__(self, up: bool = True) -> None:
        self.up = up
        self.warmed: list[str] = []
        self.events: list[str] = []

    def is_up(self) -> bool:
        return self.up

    def warmup(self, service: str) -> str:
        self.warmed.append(service)
        return "ready"


def make_relaunch(
    tmp_path: Path, client: FakeClient, *, window: bool, engine_comes_up: bool = True
) -> tuple[relaunch.Relaunch, list[str]]:
    calls: list[str] = []

    def start_engine() -> None:
        calls.append("engine")
        client.up = engine_comes_up

    def start_window() -> None:
        calls.append("window")
        client.up = engine_comes_up

    r = relaunch.Relaunch(
        paths=Paths(tmp_path / "home"), client=client,  # type: ignore[arg-type]
        window_open=lambda: window, start_engine=start_engine, start_window=start_window, wait_s=0.3,
    )
    return r, calls


def stopping_body(client: FakeClient, result: update_job.Outcome | Exception) -> Any:
    """An update body: the engine goes down (what `env.stop()` does), then it succeeds or raises."""

    def body() -> update_job.Outcome:
        client.up = False
        if isinstance(result, Exception):
            raise result
        return result

    return body


OK = update_job.Outcome("Now on 0.2.0 (was 0.1.0).", "0.1.0", "0.2.0")


def test_the_engine_is_started_again_and_done_is_written_only_after_it_answers(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    client = FakeClient(up=True)
    r, calls = make_relaunch(tmp_path, client, window=False)
    seen: list[str] = []

    def start_engine() -> None:
        seen.append(str(update_job.read_status(paths)["state"]))     # what the page reads while we start it
        calls.append("engine")
        client.up = True

    r.start_engine = start_engine
    code = update_job.run_worker(paths, stopping_body(client, OK), relaunch=r, old_version="0.1.0")
    st = update_job.read_status(paths)
    assert code == 0 and calls == ["engine"] and seen == ["running"]
    assert st["state"] == "done" and st["new"] == "0.2.0"
    assert client.warmed == ["dashboard"]                  # a headless engine would not start it by itself


def test_a_dashboard_window_that_was_open_comes_back_through_the_dashboard_launcher(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    client = FakeClient(up=True)
    r, calls = make_relaunch(tmp_path, client, window=True)
    assert update_job.run_worker(paths, stopping_body(client, OK), relaunch=r) == 0
    assert calls == ["window"]                             # the launcher starts the engine itself


def test_nothing_is_started_when_nothing_was_running(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    client = FakeClient(up=False)
    r, calls = make_relaunch(tmp_path, client, window=False)
    assert update_job.run_worker(paths, lambda: OK, relaunch=r) == 0
    assert calls == [] and update_job.read_status(paths)["state"] == "done"


def test_an_update_that_changed_nothing_leaves_the_running_engine_alone(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    client = FakeClient(up=True)
    r, calls = make_relaunch(tmp_path, client, window=True)
    same = update_job.Outcome("Already on the newest version (0.1.0).", "0.1.0", "0.1.0", updated=False)
    assert update_job.run_worker(paths, lambda: same, relaunch=r) == 0
    assert calls == []


def test_an_engine_that_never_answers_makes_the_state_failed_not_done(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    client = FakeClient(up=True)
    r, calls = make_relaunch(tmp_path, client, window=False, engine_comes_up=False)
    code = update_job.run_worker(paths, stopping_body(client, OK), relaunch=r, log=paths.logs / "u.log")
    st = update_job.read_status(paths)
    assert code == 1 and calls == ["engine"]
    assert st["state"] == "failed" and st["restart_failed"] is True and st["updated"] is True
    assert "Now on 0.2.0" in st["message"] and "Dashboard shortcut" in st["message"]


def test_a_rolled_back_update_brings_the_old_version_up_and_then_reports_the_failure(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    client = FakeClient(up=True)
    r, calls = make_relaunch(tmp_path, client, window=False)
    boom = NsError("NS-E702", "ns verify --quick failed on the new version")
    code = update_job.run_worker(paths, stopping_body(client, boom), relaunch=r, log=paths.logs / "u.log")
    st = update_job.read_status(paths)
    assert code == 1 and calls == ["engine"]
    assert st["state"] == "failed" and "NS-E702" in st["message"] and "restart_failed" not in st


def test_a_crashing_launch_does_not_kill_the_worker(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    client = FakeClient(up=True)
    r, _ = make_relaunch(tmp_path, client, window=False)

    def explode() -> None:
        raise OSError("no such file")

    r.start_engine = explode
    assert update_job.run_worker(paths, stopping_body(client, OK), relaunch=r, log=paths.logs / "u.log") == 1
    assert update_job.read_status(paths)["state"] == "failed"


# ----------------------------------------------------------------------------- the launcher process
def test_the_dashboard_launcher_runs_detached_from_the_home_without_the_worker_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, app = tmp_path / "home", tmp_path / "app"
    home.mkdir()
    (app / "launchers").mkdir(parents=True)
    script = app / "launchers" / "nightshift-dashboard.ps1"
    script.write_text("# launcher", encoding="ascii")
    monkeypatch.setenv(UNDER_ENGINE_ENV, "1")
    monkeypatch.setenv("NS_DASHBOARD_NO_HOST", "1")
    monkeypatch.setattr(relaunch.pwsh, "find_powershell", lambda: "powershell.exe")
    seen: list[tuple[list[str], dict[str, Any]]] = []
    relaunch.start_dashboard_launcher(Paths(home), app, popen=lambda argv, **kw: seen.append((argv, kw)))
    argv, kw = seen[0]
    assert argv[0] == "powershell.exe" and argv[-2:] == ["-File", str(script)]
    assert kw["cwd"] == str(home) and kw["stdin"] == subprocess.DEVNULL
    assert UNDER_ENGINE_ENV not in kw["env"] and "NS_DASHBOARD_NO_HOST" not in kw["env"]


def test_a_missing_launcher_is_ns_e202(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(relaunch.pwsh, "find_powershell", lambda: "powershell.exe")
    with pytest.raises(NsError) as ei:
        relaunch.start_dashboard_launcher(Paths(tmp_path), tmp_path / "nowhere", popen=lambda *a, **k: None)
    assert ei.value.code == "NS-E202"


def test_the_engine_is_started_without_the_worker_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ns_supervisor import client as client_mod

    seen: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kw: Any) -> None:
        seen.update(kw, argv=argv)

    monkeypatch.setenv(UNDER_ENGINE_ENV, "1")
    monkeypatch.setattr(client_mod.subprocess, "Popen", fake_popen)
    client_mod.spawn_supervisor(Paths(tmp_path / "home"), headless=False)
    assert "--headless" not in seen["argv"]
    assert UNDER_ENGINE_ENV not in seen["env"]
