# ruff: noqa: E501
"""nightly-optional, part a: the nightly run is the owner's choice, and Windows holds it.

"Automatic runs" is on exactly while Windows has an enabled 'Nightshift Daily' task. `config/schedule.json` is
the product's one record of that, written only by `ns_supervisor.schedule` after Windows answered; every page,
the Assistant, the engine and the updater read the record. The Windows Task Scheduler is a fake here
(`FakeWindows` replaces `ns_supervisor.pwsh.run_powershell`): no real task is read, made or removed.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
import threading
import zipfile
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from installer.stage2.steps.s09_shortcuts_scheduler import remember_run_time  # its home on the base
from ns_common import config_snapshot
from ns_common.config import save_runtime
from ns_common.errors import describe
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_common.singleton import FileLock
from ns_dashboard import state as dash_state
from ns_dashboard.adapters import RealScheduleAdapter
from ns_dashboard.assistant import prompt
from ns_dashboard.assistant.registry import ToolContext
from ns_dashboard.fakes import seed_channel
from ns_dashboard.handlers import Dashboard
from ns_supervisor import backup, cli, notify, pwsh, schedule, update, verify
from ns_supervisor.schedule_cfg import read_run_time, read_schedule_mode, schedule_file, write_run_time
from tests.test_dashboard_api import make_env
from tests.test_ns_schedule import _parse_errors, needs_powershell
from tests.test_ns_update import make_app, make_zip

UI = Path(__file__).resolve().parents[1] / "ns_dashboard" / "ui" / "src"


def done(out: str = "", code: int = 0, err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["powershell"], code, out, err)


class FakeWindows:
    """The Task Scheduler behind `pwsh.run_powershell`: at most one task `{state, at, logon}`.

    `refuse_s4u`: Register falls back to the interactive logon. `refuse_all`: Register and Unregister fail
    ("Access is denied"), reads work. `refuse_unregister`: only Unregister fails (the way back from a
    registration is closed). `lose`: Register answers but no task is there afterwards. `hang`: every call
    times out. `gate`: Register waits for the event (a slow step), after setting `entered`.
    """

    def __init__(self, task: tuple[str, str] | tuple[str, str, str] | None = None, **flags: bool) -> None:
        self.task = {"state": task[0], "at": task[1], "logon": (*task, "S4U")[2]} if task else None
        self.refuse_s4u = flags.get("refuse_s4u", False)
        self.refuse_all = flags.get("refuse_all", False)
        self.refuse_unregister = flags.get("refuse_unregister", False)
        self.lose = flags.get("lose", False)
        self.hang = flags.get("hang", False)
        self.calls: list[str] = []
        self.gate: threading.Event | None = None
        self.entered = threading.Event()
        self.read_gate: threading.Event | None = None  # a slow look: the read waits for it
        self.reading = threading.Event()

    def __call__(self, script: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        if self.hang:
            self.calls.append("hang")
            raise subprocess.TimeoutExpired("powershell", kw.get("timeout_s", 60))
        if "Set-ScheduledTask" in script:           # only the action changes (schedule.refresh_action)
            self.calls.append("set")
            if self.refuse_all or self.task is None:
                return done(code=1, err="Access is denied")
            action = re.search(r"-Argument '((?:[^']|'')*)'", script)
            assert action is not None
            self.task["args"] = action.group(1).replace("''", "'")
            if getattr(self, "set_changes_logon", False):   # a Windows that does not keep the principal
                self.task["logon"] = "Interactive"
            return done("action set\n")
        if "Register-ScheduledTask" in script and "Unregister-ScheduledTask" not in script:
            self.calls.append("register")
            if self.gate is not None:
                self.entered.set()
                assert self.gate.wait(10)
            if self.refuse_all:
                return done(code=1, err="Access is denied")
            at = re.search(r"\[datetime\]'(\d\d:\d\d)'", script)
            assert at is not None
            off = re.search(r"-Disable(?![-\w])", script) is not None
            logon = "Interactive" if self.refuse_s4u else "S4U"
            if not self.lose:
                self.task = {"state": "Disabled" if off else "Ready", "at": at.group(1), "logon": logon}
                home = re.search(r'NIGHTSHIFT_HOME=([^"]+)"', script)  # the install the action runs
                if home is not None:
                    self.task["home"] = home.group(1)
                action = re.search(r"-Argument '((?:[^']|'')*)'", script)   # the whole action, as registered
                if action is not None:
                    self.task["args"] = action.group(1).replace("''", "'")
            return done(f"registered:{logon.lower()}\n")
        if "Unregister-ScheduledTask" in script:
            self.calls.append("unregister")
            if self.refuse_all or self.refuse_unregister:
                return done(code=1, err="Access is denied")
            self.task = None
            return done("removed\n")
        self.calls.append("read")
        if self.read_gate is not None:
            self.reading.set()
            assert self.read_gate.wait(10)
        if self.task is None:
            return done("not registered\n")
        t = self.task
        args = f'args: /d /s /c "set "NIGHTSHIFT_HOME={t["home"]}" && "studio" run --daily"\n' if t.get("home") else ""
        if t.get("args"):
            args = f"args: {t['args']}\n"
        return done(f"state: {t['state']}\nstart: 2026-11-09T{t['at']}:00\nlogon: {t['logon']}\n{args}")

    @property
    def has(self) -> tuple[str, str] | None:
        return (self.task["state"], self.task["at"]) if self.task else None

    def changes(self) -> list[str]:
        return [c for c in self.calls if c != "read"]


@pytest.fixture
def windows(monkeypatch: pytest.MonkeyPatch) -> FakeWindows:
    """No task yet; the dashboard's once-a-minute look at Windows runs inside the request, not in a thread."""
    fake = FakeWindows()
    monkeypatch.setattr(pwsh, "run_powershell", fake)
    monkeypatch.setattr(dash_state, "SYNC_INLINE", True, raising=False)
    monkeypatch.setattr(dash_state, "_record_checked", {})
    return fake


def dashboard(tmp_home: Path) -> tuple[Any, Any, Paths]:
    """The real routes over the real schedule adapter, with one active channel (so a run can be coming)."""
    client, state = make_env(tmp_home)
    state.adapters.schedule = RealScheduleAdapter(state.paths)
    seed_channel(state.db, state.paths)
    return client, state, state.paths


def put(client: Any, **body: Any) -> dict[str, Any]:
    r = client.put("/api/settings", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def result(body: dict[str, Any]) -> dict[str, Any]:
    """`schedule_result` of a settings answer: what the page turns into its toast."""
    return body.get("schedule_result") or {}


def look_again(client: Any) -> dict[str, Any]:
    """`GET /api/status` after the minute between two looks at Windows has passed."""
    dash_state._record_checked.clear()
    return client.get("/api/status").json()


def record(paths: Paths) -> dict[str, Any] | None:
    f = schedule_file(paths)
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def saved_time(paths: Paths) -> str | None:
    f = paths.config / "dashboard.json"
    return json.loads(f.read_text(encoding="utf-8")).get("run_time") if f.exists() else None


def w207_notes(paths: Paths) -> list[str]:
    return [n["message"] for n in notify.read_notes(paths) if n["message"].startswith("NS-W207")]


def tonight_plan(state: Any) -> dict[str, Any]:
    dash = Dashboard(state)
    td = dash.registry.get("tonight_plan")
    assert td is not None
    return td.call(ToolContext(dash, {}), {})


# ------------------------------------------------------------------------------------- the switch
def test_switching_on_registers_the_task_and_every_answer_says_on(tmp_home: Path, windows: FakeWindows):
    client, state, paths = dashboard(tmp_home)
    before = client.get("/api/status").json()
    assert before["schedule_registered"] is False and before["next_run"] is None  # a fresh home: nothing runs

    body = put(client, automatic_runs=True, schedule_time="04:30")

    assert windows.has == ("Ready", "04:30")
    assert record(paths) == {"run_time": "04:30", "task_name": "Nightshift Daily", "mode": "registered"}
    assert body["schedule_result"]["applied"] is True and body["schedule_registered"] is True
    assert body["schedule_time"] == "04:30" and body["next_run"][11:16] == "04:30"
    status = client.get("/api/status").json()
    assert status["schedule_registered"] is True and status["schedule_mode"] == "registered"
    assert status["next_run"][11:16] == "04:30" and status["channels"][0]["tonight"].endswith("at 04:30")
    plan = tonight_plan(state)
    assert plan["automatic_runs"]["on"] is True and plan["next_run"][11:16] == "04:30"
    # the same click again: Windows already has it, so nothing is registered a second time
    put(client, automatic_runs=True, schedule_time="04:30")
    assert windows.changes() == ["register"]


def test_switching_off_removes_the_task_and_keeps_the_time(tmp_home: Path, windows: FakeWindows):
    client, state, paths = dashboard(tmp_home)
    put(client, automatic_runs=True, schedule_time="04:30")

    body = put(client, automatic_runs=False)

    assert windows.has is None and record(paths) is None  # nothing half-on is left in Task Scheduler
    assert saved_time(paths) == "04:30" and body["schedule_time"] == "04:30"
    assert body["schedule_registered"] is False and body["next_run"] is None
    status = client.get("/api/status").json()
    assert status["next_run"] is None and status["next_run_in_s"] is None
    assert status["schedule_registered"] is False and status["schedule_mode"] == "none"
    assert "04:30" not in status["channels"][0]["tonight"] and "off" in status["channels"][0]["tonight"]
    plan = tonight_plan(state)
    assert plan["automatic_runs"]["on"] is False and "Settings" in plan["automatic_runs"]["where"]
    assert w207_notes(paths) == []  # the owner did this here: no "switched off outside Nightshift"
    # on again without a time: the kept time is the one that is registered
    put(client, automatic_runs=True)
    assert windows.has == ("Ready", "04:30")


def test_windows_refusing_the_unattended_mode_gives_on_while_signed_in_with_ns_w205(
    tmp_home: Path, windows: FakeWindows
):
    client, _state, paths = dashboard(tmp_home)
    windows.refuse_s4u = True
    body = put(client, automatic_runs=True, schedule_time="02:15")
    assert windows.has == ("Ready", "02:15") and read_schedule_mode(paths) == "interactive_only"
    said = result(body)
    assert said["applied"] is True and said.get("code") == "NS-W205"
    assert describe("NS-W205").message in said["note"] and "elevated PowerShell" in said["note"]
    status = client.get("/api/status").json()
    assert status["schedule_registered"] is True and status["schedule_mode"] == "interactive_only"
    assert status["next_run"][11:16] == "02:15"
    assert "only while you are signed in" in tonight_plan(_state)["automatic_runs"]["state"]


def test_windows_refusing_everything_leaves_it_off_with_ns_e204(tmp_home: Path, windows: FakeWindows):
    client, _state, paths = dashboard(tmp_home)
    windows.refuse_all = True
    body = put(client, automatic_runs=True, schedule_time="02:15")
    said = result(body)
    assert said["applied"] is False and said.get("code") == "NS-E204" and "Access is denied" in said["note"]
    assert "Automatic runs are still off" in said["note"] and describe("NS-E204").hint in said["note"]
    assert windows.has is None and record(paths) is None
    assert body["schedule_registered"] is False and body["next_run"] is None
    assert saved_time(paths) != "02:15"  # a time Windows refused is not remembered as the schedule
    # failed, then retried once Windows allows it
    windows.refuse_all = False
    assert put(client, automatic_runs=True, schedule_time="02:15")["schedule_registered"] is True


def test_a_refused_switch_off_stays_on_and_says_so(tmp_home: Path, windows: FakeWindows):
    client, _state, paths = dashboard(tmp_home)
    put(client, automatic_runs=True, schedule_time="04:30")
    windows.refuse_all = True
    body = put(client, automatic_runs=False)
    assert result(body).get("applied") is False and result(body).get("code") == "NS-E206"
    assert "Automatic runs are still on" in result(body)["note"]
    assert windows.has == ("Ready", "04:30") and read_schedule_mode(paths) == "registered"
    assert body["schedule_registered"] is True and body["next_run"][11:16] == "04:30"


def test_windows_losing_the_task_after_accepting_it_reads_off(tmp_home: Path, windows: FakeWindows):
    client, _state, paths = dashboard(tmp_home)
    windows.lose = True
    body = put(client, automatic_runs=True, schedule_time="04:30")
    assert result(body).get("applied") is False and body["schedule_registered"] is False
    assert record(paths) is None and body["next_run"] is None  # read back from Windows, never from the click


def test_a_time_change_while_off_touches_no_task(tmp_home: Path, windows: FakeWindows):
    client, _state, paths = dashboard(tmp_home)
    body = put(client, schedule_time="05:45")
    assert windows.changes() == [] and windows.has is None and record(paths) is None
    assert body["schedule_time"] == "05:45" and saved_time(paths) == "05:45"
    assert body["schedule_registered"] is False and body["next_run"] is None
    assert "off" in result(body)["note"]  # the page says the time waits for the switch


def test_a_time_change_while_on_moves_the_task_keeping_logon(tmp_home: Path, windows: FakeWindows):
    client, _state, paths = dashboard(tmp_home)
    windows.refuse_s4u = True  # this PC only gets the signed-in mode
    put(client, automatic_runs=True, schedule_time="03:00")
    body = put(client, schedule_time="05:00")
    assert windows.has == ("Ready", "05:00") and windows.task is not None
    assert windows.task["logon"] == "Interactive" and read_schedule_mode(paths) == "interactive_only"
    assert windows.calls[-2:] == ["register", "read"]  # the answer comes from Windows after the change
    assert record(paths) == {"run_time": "05:00", "task_name": "Nightshift Daily", "mode": "interactive_only"}
    assert body["next_run"][11:16] == "05:00"
    # refused: the task and every page keep the old time
    windows.refuse_all = True
    body = put(client, schedule_time="06:00")
    assert result(body).get("applied") is False and body["schedule_time"] == "05:00"
    assert windows.has == ("Ready", "05:00") and read_run_time(paths) == "05:00"


def test_a_dev_install_refuses_the_switch_with_the_reason(tmp_home: Path, windows: FakeWindows):
    client, _state, paths = dashboard(tmp_home)
    save_runtime(paths, Runtime.default("cpu-dev").model_copy(update={"dev_mode": True}))
    r = client.put("/api/settings", json={"automatic_runs": True})
    assert r.status_code == 409 and r.json()["error"]["code"] == "dev_mode"
    assert "DEV_MODE" in r.json()["error"]["hint"] and windows.calls == []
    assert client.get("/api/status").json()["schedule_registered"] is False


# ---------------------------------------------------------------------- Windows changed behind our back
def test_a_task_deleted_in_task_scheduler_reads_off_with_one_ns_w207_note(
    tmp_home: Path, windows: FakeWindows
):
    client, _state, paths = dashboard(tmp_home)
    put(client, automatic_runs=True, schedule_time="04:30")
    windows.task = None  # the owner deleted it in Task Scheduler

    first = look_again(client)
    second = look_again(client)

    for status in (first, second):
        assert status["schedule_registered"] is False and status["next_run"] is None
    assert record(paths) is None and saved_time(paths) == "04:30"  # the time is kept for switching on again
    notes = w207_notes(paths)
    assert len(notes) == 1  # two passes, one note
    assert describe("NS-W207").message in notes[0] and "Settings" in notes[0]
    assert client.get("/api/notes").json()[0]["message"] == notes[0]  # where the owner sees it
    # the way out the note names works
    assert put(client, automatic_runs=True)["schedule_registered"] is True and windows.has == ("Ready", "04:30")


def test_a_task_disabled_by_hand_reads_off_and_is_left_as_it_is(tmp_home: Path, windows: FakeWindows):
    client, _state, paths = dashboard(tmp_home)
    put(client, automatic_runs=True, schedule_time="04:30")
    assert windows.task is not None
    windows.task["state"] = "Disabled"
    assert look_again(client)["schedule_registered"] is False and len(w207_notes(paths)) == 1
    res = CliRunner().invoke(cli.app, ["schedule", "install"])  # setup's rerun, or the owner without --enable
    assert res.exit_code == 0 and schedule.KEPT_OFF_MARK in res.output
    assert windows.has == ("Disabled", "04:30") and windows.changes() == ["register"] and record(paths) is None
    res = CliRunner().invoke(cli.app, ["schedule", "install", "--enable"])
    assert res.exit_code == 0 and windows.has == ("Ready", "04:30") and read_schedule_mode(paths) == "registered"


def test_a_look_at_windows_that_fails_changes_nothing(tmp_home: Path, windows: FakeWindows):
    client, _state, paths = dashboard(tmp_home)
    put(client, automatic_runs=True, schedule_time="04:30")
    windows.hang = True
    status = look_again(client)
    assert status["schedule_registered"] is True and read_run_time(paths) == "04:30" and w207_notes(paths) == []
    look_again(client)
    windows.hang = False
    assert windows.calls.count("hang") == 2  # asked once per look, not in a loop


# ---------------------------------------------------------------------------------- killed halfway
def test_a_registered_task_with_no_record_reads_on_after_a_kill(tmp_home: Path, windows: FakeWindows):
    """The process died between Windows taking the task and the record being written."""
    client, _state, paths = dashboard(tmp_home)
    windows.task = {"state": "Ready", "at": "04:30", "logon": "Interactive"}
    status = look_again(client)
    assert status["schedule_registered"] is True and status["schedule_mode"] == "interactive_only"
    assert status["next_run"][11:16] == "04:30"
    assert record(paths) == {"run_time": "04:30", "task_name": "Nightshift Daily", "mode": "interactive_only"}


def test_dying_between_the_choice_and_the_task_leaves_what_windows_has(
    tmp_home: Path, windows: FakeWindows, monkeypatch: pytest.MonkeyPatch
):
    """Switching off writes the choice first; killed before Windows removed the task, it is still on."""
    client, _state, paths = dashboard(tmp_home)
    put(client, automatic_runs=True, schedule_time="04:30")
    real = windows.__call__

    def die_at_unregister(script: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        if "Unregister-ScheduledTask" in script:
            raise KeyboardInterrupt  # the process is gone exactly here
        return real(script, **kw)

    monkeypatch.setattr(pwsh, "run_powershell", die_at_unregister)
    with pytest.raises(KeyboardInterrupt):
        schedule.remove(paths)
    assert record(paths) is None  # never "on" in the record while the task's end is unknown
    monkeypatch.setattr(pwsh, "run_powershell", windows)
    status = look_again(client)  # the next start: Windows still has the task, so it is on, and no false note
    assert status["schedule_registered"] is True and status["next_run"][11:16] == "04:30"
    assert w207_notes(paths) == []
    assert put(client, automatic_runs=False)["schedule_registered"] is False and windows.has is None


# ------------------------------------------------------------------- data written by other releases
def test_a_switched_off_record_from_rc2_reads_off_and_is_converted(tmp_home: Path, windows: FakeWindows):
    client, _state, paths = dashboard(tmp_home)
    write_run_time(paths, "04:15", "switched_off")  # 0.1.15-rc2 wrote this for a task disabled by hand
    windows.task = {"state": "Disabled", "at": "04:15", "logon": "S4U"}
    status = look_again(client)
    assert status["schedule_registered"] is False and status["next_run"] is None
    assert record(paths) is None and saved_time(paths) == "04:15" and windows.has == ("Disabled", "04:15")
    assert w207_notes(paths) == []  # it was off before: nothing new to tell
    assert client.get("/api/settings").json()["schedule_time"] == "04:15"


def test_a_torn_schedule_json_is_settled_from_the_task(tmp_home: Path, windows: FakeWindows):
    client, _state, paths = dashboard(tmp_home)
    put(client, schedule_time="04:30")  # dashboard.json is a good file beside the torn one
    schedule_file(paths).write_text('{"run_time": "04:', encoding="utf-8")
    assert look_again(client)["schedule_registered"] is False and not schedule_file(paths).exists()
    schedule_file(paths).write_text('{"run_time": "04:', encoding="utf-8")
    windows.task = {"state": "Ready", "at": "01:10", "logon": "S4U"}
    status = look_again(client)
    assert status["schedule_registered"] is True and status["next_run"][11:16] == "01:10"
    assert record(paths) == {"run_time": "01:10", "task_name": "Nightshift Daily", "mode": "registered"}


# ---------------------------------------------------------------------------------------- updates
def test_an_update_leaves_off_off_and_on_on(tmp_home: Path, windows: FakeWindows):
    paths = Paths.from_env()
    paths.ensure()
    # off, nothing recorded: the update does not even look
    assert cli._reregister(paths, tmp_home) == [] and windows.calls == []
    # on: re-registered at the task's own time, twice in a row with the same result
    windows.task = {"state": "Ready", "at": "05:45", "logon": "S4U"}
    write_run_time(paths, "03:00", "registered")
    for _ in range(2):
        notes = cli._reregister(paths, tmp_home)
        assert windows.has == ("Ready", "05:45") and read_run_time(paths) == "05:45"
        assert notes == ["scheduled task re-registered (daily at 05:45)"]
    # the owner disabled it in Task Scheduler: it stays disabled and the record says off (D-28)
    windows.task["state"] = "Disabled"
    windows.calls.clear()
    notes = cli._reregister(paths, tmp_home)
    assert windows.has == ("Disabled", "05:45") and windows.changes() == [] and record(paths) is None
    assert len(notes) == 1 and "off" in notes[0] and "left as it is" in notes[0]
    # the owner deleted it: an update never makes a new one
    write_run_time(paths, "05:45", "registered")
    windows.task = None
    cli._reregister(paths, tmp_home)
    assert windows.has is None and record(paths) is None and windows.changes() == []
    assert cli._reregister(paths, tmp_home) == []


def old_updater_reregisters(paths: Paths, windows: FakeWindows) -> None:
    """What 0.1.14's own updater does after `ns verify --quick` (v0.1.14 `cli._reregister`): it asks
    `read_run_time` and, given a time, registers the task ENABLED without looking at it."""
    when = read_run_time(paths)
    if when is not None:
        windows.task = {"state": "Ready", "at": when, "logon": "S4U"}


def verify_quick(monkeypatch: pytest.MonkeyPatch, passed: bool) -> int:
    class Up:
        def is_up(self) -> bool:
            return True

    class Report:
        def __init__(self) -> None:
            self.passed = passed

    monkeypatch.setattr(cli, "_client", lambda paths: Up())
    monkeypatch.setattr(cli, "ensure_running", lambda *a, **kw: None)
    monkeypatch.setattr(verify, "run_verify", lambda *a, **kw: Report())
    monkeypatch.setattr(verify, "write_report", lambda paths, report: paths.logs / "verify-report.json")
    monkeypatch.setattr(verify, "format_lines", lambda report: [])
    return CliRunner().invoke(cli.app, ["verify", "--quick"]).exit_code


def test_verify_quick_settles_the_record_before_an_older_updater_reregisters(
    tmp_home: Path, windows: FakeWindows, monkeypatch: pytest.MonkeyPatch
):
    paths = Paths.from_env()
    paths.ensure()
    write_run_time(paths, "03:00", "registered")  # 0.1.14 never learned the owner disabled the task
    windows.task = {"state": "Disabled", "at": "03:00", "logon": "S4U"}

    assert verify_quick(monkeypatch, passed=True) == 0

    assert read_run_time(paths) is None  # the one thing 0.1.14's `_reregister` asks
    old_updater_reregisters(paths, windows)
    assert windows.has == ("Disabled", "03:00")  # so it is not switched on again
    assert saved_time(paths) == "03:00"
    # an enabled task the owner moved keeps running, at the owner's time
    windows.task = {"state": "Ready", "at": "05:45", "logon": "S4U"}
    write_run_time(paths, "03:00", "registered")
    assert verify_quick(monkeypatch, passed=True) == 0
    old_updater_reregisters(paths, windows)
    assert windows.has == ("Ready", "05:45")
    # PowerShell hangs: the check's own answer decides the exit code, both ways, and nothing is changed
    windows.hang = True
    assert verify_quick(monkeypatch, passed=True) == 0 and verify_quick(monkeypatch, passed=False) == 1
    assert read_run_time(paths) == "05:45" and "hang" in windows.calls


def test_a_rollback_keeps_schedule_json_as_windows_has_it(tmp_path: Path):
    assert "schedule.json" in config_snapshot.LEFT_ALONE and "schedule.json" not in config_snapshot.FILES
    assert len(config_snapshot.LEFT_ALONE["schedule.json"]) > 20
    config = tmp_path / "config"
    config.mkdir()
    (config / "schedule.json").write_text('{"run_time": "04:30", "mode": "registered"}', encoding="utf-8")
    (config / "dashboard.json").write_text('{"run_time": "04:30"}', encoding="utf-8")
    assert config_snapshot.tracked_names(config) == ["dashboard.json"]  # a new copy does not hold it
    # a copy 0.1.15-rc2 made does hold one (then: on at 03:00). It still reads, and is not put back
    old = {"schedule.json": b'{"run_time": "03:00", "mode": "registered"}', "dashboard.json": b"{}"}
    listed = {n: {"sha256": hashlib.sha256(d).hexdigest(), "size": len(d)} for n, d in old.items()}
    manifest = {"format": config_snapshot.FORMAT, "program_version": "0.1.14", "program_fingerprint": "f", "files": listed}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in old.items():
            z.writestr(config_snapshot.MEMBER_ROOT + name, data)
        z.writestr(config_snapshot.MANIFEST, json.dumps(manifest))
    snap = config_snapshot.parse(buf.getvalue())
    assert "schedule.json" not in snap.files and snap.files["dashboard.json"] == b"{}"
    (config / "schedule.json").unlink()  # the owner switched Automatic runs off after the update
    live = config_snapshot.capture(config)
    config_snapshot.restore(config, snap.files, live=live, remove_extra=True)
    assert not (config / "schedule.json").exists()  # a rollback does not say "on" for a task that is gone
    assert (config / "dashboard.json").read_bytes() == b"{}"


# ------------------------------------------------------------------------------------ every route
def test_every_route_reads_the_one_record(tmp_home: Path, windows: FakeWindows):
    from ns_supervisor.core import Supervisor

    client, state, paths = dashboard(tmp_home)
    dash = Dashboard(state)

    def answers() -> dict[str, Any]:
        dash_state._record_checked[str(paths.home)] = 1e18  # no look at Windows: only the record speaks
        status, settings = client.get("/api/status").json(), client.get("/api/settings").json()
        nxt = dash.engine.next_run()
        return {
            "status": (status["schedule_registered"], status["schedule_mode"], status["next_run"]),
            "settings": (settings["schedule_registered"], settings["schedule_mode"], settings["next_run"]),
            "engine": nxt.isoformat(timespec="minutes") if nxt else None,
            "plan": tonight_plan(state).get("automatic_runs", {}).get("on"),
            "assistant": prompt.live_state(dash),
            "supervisor": read_run_time(paths),  # `Supervisor.run_due_soon` and 0.1.14's updater
        }

    assert Supervisor.run_due_soon.__code__.co_names.count("read_run_time") == 1
    off = answers()
    assert off["status"] == off["settings"] == (False, "none", None)
    assert (off["engine"], off["plan"], off["supervisor"]) == (None, False, None)
    assert "automatic runs: off" in off["assistant"] and "next nightly run" not in off["assistant"]
    write_run_time(paths, "04:30", "interactive_only")
    on = answers()
    assert on["status"][:2] == on["settings"][:2] == (True, "interactive_only")
    assert on["status"][2] == on["settings"][2] == on["engine"] and on["engine"][11:16] == "04:30"
    assert on["plan"] is True and on["supervisor"] == "04:30" and "next nightly run" in on["assistant"]
    assert windows.calls == []
    res = CliRunner().invoke(cli.app, ["schedule", "remove"])  # the console route writes the same record
    assert res.exit_code == 0 and answers() == off and saved_time(paths) == "04:30"


def test_the_pages_say_which_state_it_is_in():
    settings = (UI / "pages" / "Settings.tsx").read_text(encoding="utf-8")
    assert 'title="Automatic runs"' in settings and "automatic_runs: v" in settings
    assert "No nightly task" not in settings  # off is a choice, not a failure
    assert "DEV_MODE" in settings  # the greyed switch of a dev install says why
    shell = (UI / "shell" / "Shell.tsx").read_text(encoding="utf-8")
    assert "Automatic runs off" in shell and "No nightly task" not in shell
    queue = (UI / "pages" / "Queue.tsx").read_text(encoding="utf-8")
    assert "Switch them on in Settings" in queue and "caption={scheduled ?" in queue
    create = (UI / "pages" / "Create.tsx").read_text(encoding="utf-8")
    assert '|| "03:00"' not in create and "Automatic runs are off" in create


# ----------------------------------------------------------------------------- Windows differences
@needs_powershell
def test_the_remove_and_read_scripts_parse_under_powershell_5_1(tmp_path: Path):
    read = schedule.build_read_script()
    assert "Principal.LogonType" in read  # the signed-in-only mode is read from Windows, not remembered
    for script in (read, schedule.build_remove_script(), schedule.build_install_script(Paths(tmp_path), "04:30", tmp_path)):
        assert _parse_errors(script) == ""
    got = schedule.parse_task_state("state: Ready\nstart: 2026-11-09T04:30:00\nlogon: Interactive\n")
    assert got is not None and got.enabled and got.run_time == "04:30" and got.interactive is True


# ------------------------------------------------------------------------------- two at the same time
def test_two_changes_at_once_run_one_after_the_other(
    tmp_home: Path, windows: FakeWindows, monkeypatch: pytest.MonkeyPatch
):
    client, _state, paths = dashboard(tmp_home)
    windows.gate = threading.Event()  # the first dashboard's Register is slow
    first: list[dict[str, Any]] = []
    t = threading.Thread(target=lambda: first.append(put(client, automatic_runs=True, schedule_time="04:30")))
    t.start()
    assert windows.entered.wait(10)
    # the second dashboard switches off meanwhile; it waits (here 0.3 s instead of 10 s), then is refused
    monkeypatch.setattr(schedule, "LOCK_WAIT_S", 0.3, raising=False)
    busy = put(client, automatic_runs=False)
    assert windows.changes() == ["register"]  # no Unregister ran inside the Register
    assert result(busy).get("applied") is False and result(busy).get("code") == "NS-E206"
    assert "another change" in result(busy)["note"].lower()
    # Windows was never asked for this switch-off: the note does not blame it or send the owner to Task Scheduler
    assert "Windows did not" not in result(busy)["note"] and "Task Scheduler" not in result(busy)["note"]
    assert "try again in a few seconds" in result(busy)["note"] and "shows Automatic runs" in result(busy)["note"]
    windows.gate.set()
    t.join(10)
    assert first and first[0]["schedule_registered"] is True and windows.has == ("Ready", "04:30")
    # once the first is done the second goes through, and the record follows the last one
    assert put(client, automatic_runs=False)["schedule_registered"] is False
    assert windows.has is None and record(paths) is None and windows.changes() == ["register", "unregister"]


# ------------------------------------------------------------------------- review round 1 (ad3fe8b)
def test_try_again_on_the_signed_in_only_row_asks_windows_again(tmp_home: Path, windows: FakeWindows):
    """The NS-W205 row's button sends the same state and time; that must still reach Windows."""
    client, _state, paths = dashboard(tmp_home)
    windows.refuse_s4u = True
    put(client, automatic_runs=True, schedule_time="04:30")
    again = put(client, automatic_runs=True, schedule_time="04:30")  # Windows refuses the unattended mode again
    assert windows.changes() == ["register", "register"] and result(again).get("code") == "NS-W205"
    assert describe("NS-W205").hint in result(again)["note"] and again["schedule_mode"] == "interactive_only"
    windows.refuse_s4u = False  # an elevated dashboard, or the policy changed
    ok = put(client, automatic_runs=True, schedule_time="04:30")
    assert windows.changes() == ["register"] * 3 and windows.task is not None and windows.task["logon"] == "S4U"
    assert result(ok)["applied"] is True and "code" not in result(ok) and ok["schedule_mode"] == "registered"
    assert read_schedule_mode(paths) == "registered"
    put(client, automatic_runs=True, schedule_time="04:30")  # unattended now: nothing more to ask
    assert windows.changes() == ["register"] * 3
    page = (UI / "pages" / "Settings.tsx").read_text(encoding="utf-8")
    assert "Windows accepted it" in page and "if it refuses again" in page


@pytest.mark.parametrize(
    ("logon", "mode"),
    [("Password", "registered"), ("InteractiveOrPassword", "registered"), ("S4U", "registered"),
     ("Interactive", "interactive_only"), ("Group", "interactive_only")],
)
def test_the_tasks_own_logon_type_decides_signed_in_only(
    tmp_home: Path, windows: FakeWindows, logon: str, mode: str
):
    """A task the owner set to "run whether user is logged on or not" with a saved password runs signed out."""
    client, _state, paths = dashboard(tmp_home)
    windows.task = {"state": "Ready", "at": "04:30", "logon": logon}
    status = look_again(client)
    assert status["schedule_registered"] is True and status["schedule_mode"] == mode
    assert read_schedule_mode(paths) == mode


def test_a_settings_save_and_the_look_at_windows_keep_each_others_change(
    tmp_home: Path, windows: FakeWindows, monkeypatch: pytest.MonkeyPatch
):
    """Both rewrite dashboard.json: the save its `notifications`, the look the kept run time."""
    client, _state, paths = dashboard(tmp_home)
    put(client, schedule_time="03:00")
    real = dash_state.write_text_atomic
    inside, go = threading.Event(), threading.Event()

    def slow_save(path: Path, text: str, **kw: Any) -> Any:
        inside.set()  # the save has read the file and is about to write it back
        assert go.wait(10)
        return real(path, text, **kw)

    monkeypatch.setattr(dash_state, "write_text_atomic", slow_save)
    save = threading.Thread(target=lambda: dash_state.save_dashboard_settings(paths, {"notifications": False}))
    save.start()
    assert inside.wait(10)
    look = threading.Thread(target=lambda: remember_run_time(paths, "06:00"))  # what a look does for a lost task
    look.start()
    look.join(0.5)
    assert look.is_alive()  # it waits for the save instead of writing between its read and its write
    go.set()
    save.join(10)
    look.join(10)
    kept = json.loads((paths.config / "dashboard.json").read_text(encoding="utf-8"))
    assert kept == {"run_time": "06:00", "notifications": False}  # neither change was lost


def test_a_restored_backup_never_brings_back_an_older_record(tmp_home: Path, windows: FakeWindows):
    paths = Paths.from_env()
    paths.ensure()
    write_run_time(paths, "04:30", "registered")  # the backup is made while Automatic runs are on
    zip_path = backup.make_backup(paths, tmp_home.parent / "backup.zip")
    schedule.remove(paths)  # then the owner switches them off, here
    lines = backup.restore_backup(paths, zip_path, include_machine=True)
    assert any(ln.startswith("skipped config/schedule.json (the record of this PC") for ln in lines)
    assert record(paths) is None and schedule.sync_record(paths) is False
    assert w207_notes(paths) == []  # no "switched off outside Nightshift" for a switch-off made in Nightshift


def test_the_first_page_of_a_dashboard_does_not_wait_for_the_look_at_windows(
    tmp_home: Path, windows: FakeWindows, monkeypatch: pytest.MonkeyPatch
):
    client, _state, paths = dashboard(tmp_home)
    monkeypatch.setattr(dash_state, "SYNC_INLINE", False, raising=False)  # as the product runs
    write_run_time(paths, "04:30", "registered")  # the last known state; the task was deleted meanwhile
    windows.read_gate = threading.Event()  # PowerShell is slow
    first = client.get("/api/status").json()  # answered while the look still waits
    assert windows.reading.wait(10) and not windows.read_gate.is_set()
    assert first["schedule_registered"] is True and first["next_run"][11:16] == "04:30"
    assert client.get("/api/settings").json()["schedule_registered"] is True and windows.calls == ["read"]
    windows.read_gate.set()
    for t in threading.enumerate():
        if t.name == "schedule-sync":
            t.join(10)
    after = client.get("/api/status").json()  # the next poll shows what the look found
    assert after["schedule_registered"] is False and after["next_run"] is None and len(w207_notes(paths)) == 1


# ------------------------------------------------------------------------- review round 2 (6795816)
# Every lock and file the item added, when it cannot be created, opened or written. A folder sitting where the
# file should be is a file that really cannot be opened (PermissionError on Windows), by any process.
def unopenable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)  # the lock file of an earlier change
    path.mkdir()
    return path


@pytest.mark.parametrize("how", ["the lock file cannot be opened", "run is not a folder"])
def test_the_switch_answers_ns_e204_when_the_schedule_lock_cannot_be_opened(
    tmp_home: Path, windows: FakeWindows, how: str
):
    client, _state, paths = dashboard(tmp_home)
    put(client, automatic_runs=True, schedule_time="04:30")
    run = paths.home / "run"
    if how == "run is not a folder":  # also what a read-only or lost `run` folder gives: nothing can be made in it
        shutil.rmtree(run)
        run.write_text("not a folder", encoding="utf-8")
    else:
        unopenable(run / "schedule.lock")

    off = put(client, automatic_runs=False)
    moved = put(client, schedule_time="05:00")

    for body, code in ((off, "NS-E206"), (moved, "NS-E204")):  # the code follows what was asked: off, or the time
        said = result(body)
        assert said.get("applied") is False and said.get("code") == code and f"Not saved ({code})" in said["note"]
        assert "schedule.lock could not be opened" in said["note"] and "nothing was changed" in said["note"]
        assert "shows Automatic runs on" in said["note"] and "elevated" not in said["note"]  # true words only
        assert body["schedule_registered"] is True and body["schedule_time"] == "04:30"
    assert windows.changes() == ["register"] and windows.has == ("Ready", "04:30")  # Windows was never asked
    # the look at Windows cannot take the lock either: it changes nothing and no page fails
    windows.task = None
    assert look_again(client)["schedule_registered"] is True and read_run_time(paths) == "04:30"
    windows.task = {"state": "Ready", "at": "04:30", "logon": "S4U"}
    # the way out the message names: the folder can be written again, then the same click works
    if run.is_file():
        run.unlink()
    else:
        (run / "schedule.lock").rmdir()
    assert put(client, automatic_runs=False)["schedule_registered"] is False and windows.has is None


def test_an_update_stands_when_the_schedule_lock_cannot_be_opened(tmp_path: Path, windows: FakeWindows):
    paths = Paths(tmp_path / "ns")
    write_run_time(paths, "03:00", "registered")
    windows.task = {"state": "Ready", "at": "03:00", "logon": "S4U"}
    unopenable(paths.home / "run" / "schedule.lock")
    env = update.UpdateEnv(
        stop=lambda: None, sync=lambda a: None, verify=lambda a: True,
        reregister=lambda a: cli._reregister(paths, a), settle=lambda: None,
    )
    res = update.update_app(make_zip(tmp_path), make_app(tmp_path), env, paths=paths)  # no rollback
    assert (res.old_version, res.new_version) == ("0.1.0", "0.2.0")
    said = [n for n in res.notes if n.startswith("scheduled task NOT re-registered")]
    assert len(said) == 1 and "schedule.lock could not be opened" in said[0] and "nothing was changed" in said[0]
    # the owner's choice is as it was: the task was not even looked at
    assert windows.calls == [] and windows.has == ("Ready", "03:00") and read_run_time(paths) == "03:00"


def test_a_settings_save_goes_through_when_its_lock_file_cannot_be_opened(tmp_home: Path, windows: FakeWindows):
    client, _state, paths = dashboard(tmp_home)
    unopenable(paths.home / "run" / "dashboard-settings.lock")
    body = put(client, notifications=False, schedule_time="05:15")
    assert body["notifications"] is False and body["schedule_time"] == "05:15" and saved_time(paths) == "05:15"
    remember_run_time(paths, "06:00")  # the other writer, the same way
    kept = json.loads((paths.config / "dashboard.json").read_text(encoding="utf-8"))
    assert kept == {"run_time": "06:00", "notifications": False}
    warned = [
        ln for f in paths.logs.glob("schedule*.jsonl") for ln in f.read_text(encoding="utf-8").splitlines()
        if "preferences_lock_unavailable" in ln
    ]
    assert len(warned) == 1  # said once in the log, not once per save


def test_a_record_that_cannot_be_written_is_ns_e204_and_never_reads_on(tmp_home: Path, windows: FakeWindows):
    client, _state, paths = dashboard(tmp_home)
    unopenable(schedule_file(paths))  # config/schedule.json cannot be replaced
    body = put(client, automatic_runs=True, schedule_time="04:30")
    said = result(body)
    assert said.get("applied") is False and said.get("code") == "NS-E204"
    assert "could not write config/schedule.json" in said["note"] and "so the change was undone" in said["note"]
    assert "shows Automatic runs off" in said["note"] and "elevated" not in said["note"]
    assert windows.has is None  # part b: the task Windows took is removed again, so no page is untrue
    assert body["schedule_registered"] is False and body["next_run"] is None
    # a task that is on without a record (the undo was refused, or a kill): the look changes nothing while the
    # file cannot be written, and no page fails
    windows.task = {"state": "Ready", "at": "04:30", "logon": "S4U"}
    assert look_again(client)["schedule_registered"] is False
    schedule_file(paths).rmdir()
    status = look_again(client)  # writable again: the page follows Windows
    assert status["schedule_registered"] is True and status["next_run"][11:16] == "04:30"


def test_a_lock_that_fails_on_release_does_not_undo_a_finished_change(
    tmp_home: Path, windows: FakeWindows, monkeypatch: pytest.MonkeyPatch
):
    client, _state, paths = dashboard(tmp_home)
    real = FileLock.release

    def release(self: FileLock) -> None:
        real(self)
        raise OSError(5, "the lock file went away")

    monkeypatch.setattr(FileLock, "release", release)
    body = put(client, automatic_runs=True, schedule_time="04:30")
    assert result(body)["applied"] is True and body["schedule_registered"] is True
    assert put(client, notifications=False)["notifications"] is False and read_run_time(paths) == "04:30"


def test_the_settings_switch_takes_the_fresh_state_when_the_status_brings_it():
    """The look at Windows answers after the page loaded; Settings must not keep the older switch."""
    page = (UI / "pages" / "Settings.tsx").read_text(encoding="utf-8")
    assert "status.schedule_registered !== s.schedule_registered" in page
    assert "[status?.schedule_registered, status?.schedule_mode, status?.schedule_time]" in page
    assert "schedule_registered: status.schedule_registered" in page


# ------------------------------------------------------- FN-4: a failed switch-off, and whose task was left
def test_a_failed_switch_off_tells_how_to_switch_off(tmp_home: Path, windows: FakeWindows):
    """NS-E204's way out is "run ns schedule install": the opposite of what an owner who switched OFF needs."""
    client, state, paths = dashboard(tmp_home)
    put(client, automatic_runs=True, schedule_time="04:30")
    windows.refuse_unregister = True
    body = put(client, automatic_runs=False)
    said = result(body)
    assert said.get("applied") is False and said.get("code") == "NS-E206"
    note = said["note"]
    assert note.startswith("Not saved: Windows did not switch off the nightly task (Access is denied). Automatic runs are still on. NS-E206: ")
    assert describe("NS-E206").hint in note and "Task Scheduler" in note and "Disable" in note
    assert "ns schedule install" not in note and "elevated PowerShell" not in note
    assert body["schedule_registered"] is True and windows.has == ("Ready", "04:30")  # the page says what Windows has
    # failed, then retried: once Windows lets go, the same click works and nothing is left over
    windows.refuse_unregister = False
    assert put(client, automatic_runs=False)["schedule_registered"] is False and windows.has is None
    assert record(paths) is None and state is not None


def test_a_failed_switch_on_still_names_ns_schedule_install(tmp_home: Path, windows: FakeWindows):
    client, _state, _paths = dashboard(tmp_home)
    windows.refuse_all = True
    said = result(put(client, automatic_runs=True, schedule_time="04:30"))
    assert said.get("code") == "NS-E204" and describe("NS-E204").hint in said["note"]
    assert "Windows did not change the nightly task (Access is denied)" in said["note"]
    windows.refuse_all = False
    put(client, automatic_runs=True, schedule_time="04:30")
    windows.refuse_all = True
    moved = result(put(client, schedule_time="05:00"))   # a time change that Windows refuses is a registration too
    assert moved.get("code") == "NS-E204" and "Settings > Schedule > Automatic runs" in moved["note"]
    # since FN-3 a plain `ns` works in any new terminal: the same form as NS-W205 and NS-W206
    assert "run 'ns schedule install' from an elevated PowerShell" in moved["note"]


def test_ns_schedule_remove_that_windows_refuses_is_ns_e206(tmp_home: Path, windows: FakeWindows):
    paths = Paths.from_env()
    paths.ensure()
    windows.task = {"state": "Ready", "at": "04:30", "logon": "S4U"}
    write_run_time(paths, "04:30", "registered")
    windows.refuse_unregister = True
    res = CliRunner().invoke(cli.app, ["schedule", "remove"])
    assert res.exit_code == 1 and "NS-E206" in res.output and "NS-E204" not in res.output, res.output
    assert "schedule remove: Access is denied" in res.output and "choose Disable" in res.output
    assert "ns schedule install" not in res.output
    assert windows.has == ("Ready", "04:30") and read_schedule_mode(paths) == "registered"  # the record follows Windows
    windows.refuse_unregister, windows.hang = False, True
    res = CliRunner().invoke(cli.app, ["schedule", "show"])
    assert res.exit_code == 1 and "NS-E206" in res.output and "schedule show: PowerShell timed out" in res.output
    windows.hang, windows.refuse_all = False, True
    res = CliRunner().invoke(cli.app, ["schedule", "install", "--time", "05:00"])
    assert res.exit_code == 1 and "NS-E204" in res.output and "NS-E206" not in res.output   # registering: unchanged


def _missing_drive() -> str:
    import string

    free = [f"{letter}:" for letter in reversed(string.ascii_uppercase) if not Path(f"{letter}:/").exists()]
    if not free:
        pytest.skip("every drive letter is in use on this PC")
    return free[0]


@pytest.mark.parametrize("row", ["none", "disabled", "other", "unreachable", "gone"])
def test_reregister_says_whose_task_it_left(tmp_path: Path, windows: FakeWindows, row: str):
    """An update of an install whose record says on (written by the previous release) finds the task not on
    here: each reason has its own sentence, and nothing is changed in any of them."""
    paths = Paths(tmp_path / "ns")
    paths.ensure()
    write_run_time(paths, "03:00", "registered")
    other = tmp_path / "other-ns"
    other.mkdir()
    places = {"other": str(other), "gone": str(tmp_path / "deleted")}
    if row == "unreachable":
        places[row] = str(Path(f"{_missing_drive()}/Nightshift"))
    there = places.get(row, "")
    if row == "disabled":
        windows.task = {"state": "Disabled", "at": "05:45", "logon": "S4U"}
    elif row != "none":
        windows.task = {"state": "Ready", "at": "05:45", "logon": "S4U", "home": there}
    expected = {
        "none": (
            "Automatic runs are off: Windows no longer has the nightly task 'Nightshift Daily' (it was removed "
            "outside this Nightshift), so it was not registered again. To get the nightly run back, switch "
            "Automatic runs on in Settings."
        ),
        "disabled": (
            "Automatic runs are off: the nightly task 'Nightshift Daily' is disabled in Task Scheduler and was "
            "left as it is. To get the nightly run back, switch Automatic runs on in Settings."
        ),
        "other": (
            f"Automatic runs are off here: the nightly task runs the Nightshift at {there}, so it was left as it "
            "is. Switch Automatic runs on in this Nightshift's Settings to move it back."
        ),
        "unreachable": (
            f"Automatic runs are off here: the nightly task runs the Nightshift at {there}, whose drive is not "
            "connected now, so it was left as it is. Switch Automatic runs on in this Nightshift's Settings to "
            "move it back."
        ),
        "gone": (
            f"Automatic runs are off: the nightly task runs a Nightshift folder that no longer exists ({there}), "
            "so it was left as it is. Switch Automatic runs on in Settings to take it."
        ),
    }[row]
    before = windows.has
    env = update.UpdateEnv(
        stop=lambda: None, sync=lambda a: None, verify=lambda a: True,
        reregister=lambda a: cli._reregister(paths, a), settle=lambda: None,
    )
    res = update.update_app(make_zip(tmp_path), make_app(tmp_path), env, paths=paths)
    assert expected in res.notes, res.notes
    assert windows.has == before and windows.changes() == [] and record(paths) is None  # left exactly as it is
    # the second run (the next update): the record says off now, so Windows is not asked and nothing is said
    windows.calls.clear()
    assert cli._reregister(paths, tmp_path) == [] and windows.calls == []
