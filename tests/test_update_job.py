"""C2: `ns update` survives the engine stop.

The dashboard runs `ns update --github` below the engine's Job Object; the update stops the engine, and the
engine's shutdown killed the dashboard tree, the update included. The update now runs as a detached worker
that breaks away from the job, starts in the install home and waits for its caller to exit.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor import cli, procs, update, update_job
from ns_supervisor.services import ServiceDef
from ns_supervisor.testing import wait_until

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()
win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


class FakePopen:
    """Records the arguments of each start; the first `fail` starts raise PermissionError."""

    def __init__(self, fail: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail

    def __call__(self, argv: list[str], **kw: Any) -> Any:
        self.calls.append({"argv": argv, **kw})
        if len(self.calls) <= self.fail:
            raise PermissionError(5, "Access is denied")

        class Child:
            pid = 4321

        return Child()


def test_the_worker_is_detached_broken_away_from_the_job_and_starts_in_the_home(tmp_path: Path) -> None:
    popen = FakePopen()
    pid = update_job.spawn_detached(["ns", "update"], cwd=tmp_path, popen=popen)
    call = popen.calls[0]
    flags = call["creationflags"]
    assert pid == 4321 and len(popen.calls) == 1
    assert flags & update_job.CREATE_NEW_PROCESS_GROUP and flags & update_job.DETACHED_PROCESS
    assert flags & update_job.CREATE_BREAKAWAY_FROM_JOB
    assert call["cwd"] == str(tmp_path) and call["stdin"] == subprocess.DEVNULL


def test_the_detached_worker_does_not_inherit_the_setup_windows_no_host_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NS_DASHBOARD_NO_HOST", "1")
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path))
    popen = FakePopen()
    update_job.spawn_detached(["ns", "update"], cwd=tmp_path, popen=popen)
    env = popen.calls[0]["env"]
    assert "NS_DASHBOARD_NO_HOST" not in env and env["NIGHTSHIFT_HOME"] == str(tmp_path)


def test_a_job_that_forbids_breakaway_goes_through_wmi(tmp_path: Path) -> None:
    popen = FakePopen(fail=1)
    seen: list[tuple[list[str], Path]] = []

    def cim(argv: list[str], cwd: Path) -> int:
        seen.append((argv, cwd))
        return 777

    assert update_job.spawn_detached(["ns"], cwd=tmp_path, popen=popen, cim=cim) == 777
    assert len(popen.calls) == 1 and seen == [(["ns"], tmp_path)]


def test_without_wmi_the_last_resort_is_a_plain_detached_start(tmp_path: Path) -> None:
    popen = FakePopen(fail=1)
    assert update_job.spawn_detached(["ns"], cwd=tmp_path, popen=popen, cim=lambda a, c: None) == 4321
    assert not popen.calls[1]["creationflags"] & update_job.CREATE_BREAKAWAY_FROM_JOB


def test_nothing_starting_is_ns_e702(tmp_path: Path) -> None:
    with pytest.raises(NsError) as ei:
        update_job.spawn_detached(["ns"], cwd=tmp_path, popen=FakePopen(fail=9), cim=lambda a, c: None)
    assert ei.value.code == "NS-E702"


def test_every_service_is_marked_as_running_below_the_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_popen(argv: list[str], **kw: Any) -> Any:
        seen.update(kw)
        raise OSError("stop here")

    monkeypatch.setattr(procs.subprocess, "Popen", fake_popen)
    with pytest.raises(NsError):
        procs.start(ServiceDef("dashboard", ["x"], "127.0.0.1", 1, False), tmp_path)
    assert seen["env"][procs.UNDER_ENGINE_ENV] == "1"


# ------------------------------------------------------------------------------------------- the CLI
def make_zip(tmp_path: Path) -> Path:
    from tests.test_ns_update import make_zip as mk

    return mk(tmp_path)


def invoke(home: Path, *args: str, env: dict[str, str] | None = None) -> Any:
    return runner.invoke(cli.app, list(args), env={"NIGHTSHIFT_HOME": str(home), **(env or {})})


def forbid_in_process_update(monkeypatch: pytest.MonkeyPatch) -> None:
    def no(*a: Any, **k: Any) -> None:
        pytest.fail("the caller must not update itself")

    monkeypatch.setattr(update, "update_app", no)
    monkeypatch.setattr(update, "rollback_app", no)


def test_detach_starts_a_worker_and_returns_at_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    forbid_in_process_update(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    started: list[tuple[list[str], Path]] = []
    monkeypatch.setattr(update_job, "spawn_detached", lambda argv, cwd: started.append((argv, cwd)) or 99)
    z = make_zip(tmp_path)
    r = invoke(home, "update", str(z), "--detach")
    assert r.exit_code == 0, r.output
    last = json.loads(r.output.strip().splitlines()[-1])
    assert last["started"] is True and Path(last["log"]).parent == Paths(home).logs
    argv, cwd = started[0]
    assert cwd == home                                   # never the app folder
    assert str(z.resolve()) in argv and "--worker" in argv and "--wait-pid" in argv
    assert argv[argv.index("--home") + 1] == str(home)
    st = update_job.read_status(Paths(home))
    assert st["state"] == "running" and update_job.status_file(Paths(home)).name == "update_state.json"


def test_below_the_engine_the_update_detaches_without_being_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    forbid_in_process_update(monkeypatch)
    home = tmp_path / "home"
    home.mkdir()
    started: list[list[str]] = []
    monkeypatch.setattr(update_job, "spawn_detached", lambda argv, cwd: started.append(argv) or 99)
    r = invoke(home, "update", "--github", env={procs.UNDER_ENGINE_ENV: "1"})
    assert r.exit_code == 0 and '"started": true' in r.output
    assert "--github" in started[0] and "--wait-pid" in started[0]


def test_from_a_shortcut_the_caller_waits_and_relays_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    paths = Paths(home)
    argvs: list[list[str]] = []

    def spawn(argv: list[str], cwd: Path) -> int:
        argvs.append(argv)
        log = Path(argv[argv.index("--log") + 1])
        log.write_text("Now on 0.2.0 (was 0.1.0).\n", encoding="utf-8")
        update_job.write_status(paths, "done", "Now on 0.2.0 (was 0.1.0).", exit_code=0)
        return 55

    monkeypatch.setattr(update_job, "spawn_detached", spawn)
    monkeypatch.setattr(update_job, "pid_alive", lambda pid: False)
    r = invoke(home, "update", "--github")
    assert r.exit_code == 0 and "Now on 0.2.0" in r.output
    assert "--wait-pid" not in argvs[0]  # it must not wait for a caller that waits for it

    def failing(argv: list[str], cwd: Path) -> int:
        update_job.write_status(paths, "failed", "NS-E702: x", exit_code=1)
        return 56

    monkeypatch.setattr(update_job, "spawn_detached", failing)
    assert invoke(home, "update", "--github").exit_code == 1


def test_the_worker_logs_reports_and_waits_for_the_caller(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    waited: list[int] = []
    log = paths.logs / "u.log"
    code = update_job.run_worker(
        paths, lambda: update_job.Outcome("Now on 0.2.0 (was 0.1.0).", "0.1.0", "0.2.0"), log=log,
        wait_pid=1234, wait=waited.append, old_version="0.1.0",
    )
    st = update_job.read_status(paths)
    assert code == 0 and waited == [1234]
    assert st["state"] == "done" and st["exit_code"] == 0 and st["log"] == str(log)
    assert (st["old"], st["new"], st["message"]) == ("0.1.0", "0.2.0", "Now on 0.2.0 (was 0.1.0).")
    assert st["updated_at"]                                   # the contract the dashboard reads


def test_a_failing_worker_exits_one_and_says_why(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    log = paths.logs / "u.log"

    def boom() -> update_job.Outcome:
        raise NsError("NS-E702", "could not swap directories")

    code = update_job.run_worker(paths, boom, log=log)
    st = update_job.read_status(paths)
    assert code == 1 and st["state"] == "failed" and "NS-E702" in st["message"]
    assert "could not swap directories" in log.read_text(encoding="utf-8")


def test_an_up_to_date_answer_is_not_a_failure(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    out = update_job.Outcome("Already on the newest version (0.1.0).", "0.1.0", "0.1.0", updated=False)
    assert update_job.run_worker(paths, lambda: out) == 0
    st = update_job.read_status(paths)
    assert st["state"] == "done" and st["updated"] is False


def test_wait_for_parent_polls_until_it_is_gone() -> None:
    answers = iter([True, True, False])
    slept: list[float] = []
    update_job.wait_for_parent(7, alive=lambda pid: next(answers), sleep=slept.append, grace_s=2.0)
    assert slept == [0.5, 0.5, 2.0]


# ------------------------------------------------------------------- the real thing (Windows)
def _listed(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return f" {pid} " in out


@win_only
def test_a_worker_started_below_the_engine_survives_the_job_closing(tmp_path: Path) -> None:
    """The exact failure: the engine's Job Object closes and kills the service tree, the worker lives on."""
    go, pid_file, env_file = tmp_path / "go", tmp_path / "pid.txt", tmp_path / "env.txt"
    sleeper = "import time; time.sleep(120)"
    script = "\n".join([
        "import os, pathlib, sys, time",
        f"go = pathlib.Path({str(go)!r})",
        "while not go.exists():",
        "    time.sleep(0.05)",
        "from ns_supervisor import update_job",
        f"home = pathlib.Path({str(tmp_path)!r})",
        f"pid = update_job.spawn_detached([sys.executable, '-c', {sleeper!r}], cwd=home)",
        f"pathlib.Path({str(env_file)!r}).write_text(os.environ.get('NIGHTSHIFT_UNDER_ENGINE', ''))",
        f"pathlib.Path({str(pid_file)!r}).write_text(str(pid))",
        "time.sleep(120)",
    ])
    svc = ServiceDef(
        "dashboard", [sys.executable, "-c", script], "127.0.0.1", 1, False, env={"PYTHONPATH": str(ROOT)}
    )
    job = procs.JobObject()
    assert job.available
    proc = procs.start(svc, tmp_path / "logs", job=job)
    worker = 0
    try:
        go.write_text("go")
        assert wait_until(pid_file.exists, timeout_s=60)
        worker = int(pid_file.read_text())
        assert env_file.read_text() == "1"
        job.close()                                       # what the engine's shutdown does
        assert wait_until(lambda: not proc.alive(), timeout_s=30), "the service tree should be killed"
        assert _listed(worker), "the detached worker must outlive the job"
    finally:
        job.close()
        if worker:
            subprocess.run(["taskkill", "/PID", str(worker), "/T", "/F"], capture_output=True, check=False)


def test_state_says_running_before_the_engine_is_stopped(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    seen: list[str] = []

    def body() -> update_job.Outcome:
        seen.append(str(update_job.read_status(paths)["state"]))      # what the dashboard would read now
        return update_job.Outcome("Now on 0.2.0 (was 0.1.0).", "0.1.0", "0.2.0")

    update_job.run_worker(paths, body, old_version="0.1.0")
    assert seen == ["running"]
    assert update_job.read_status(paths)["state"] == "done"


def test_a_github_update_ends_with_a_json_result_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_ns_update_github import pin_latest

    home = tmp_path / "home"
    pin_latest(monkeypatch, "99.0.0")
    monkeypatch.setattr(
        cli.releases, "download", lambda rel, dest, client=None: dest / rel.zip_name
    )
    done = update.UpdateResult("0.1.0", "99.0.0", ["scheduled task re-registered"])
    monkeypatch.setattr(update, "update_app", lambda *a, **k: done)
    r = invoke(home, "update", "--github", "--worker")
    lines = r.output.strip().splitlines()
    assert r.exit_code == 0 and "Now on 99.0.0 (was 0.1.0)." in r.output
    assert json.loads(lines[-1]) == {"updated": True, "old": "0.1.0", "new": "99.0.0"}
    assert update_job.read_status(Paths(home))["new"] == "99.0.0"
