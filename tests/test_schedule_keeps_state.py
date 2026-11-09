"""D-28: re-registering the 'Nightshift Daily' task must not undo what the owner did in Task Scheduler.

`ns update` (Settings, CLI), the Settings run-time change, `ns schedule install` and the setup step all
re-register the task. The task in Windows is the truth for its state (enabled or not) and its run time; they
are read BEFORE the task is replaced and restored after, and `schedule.json` is rewritten to match. A fresh
install (no task yet) registers an enabled task as before. The Windows Task Scheduler is a fake here: no real
task is created or changed.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from installer.stage2.runner import RunResult
from installer.stage2.simulate import FakeRunner
from installer.stage2.steps import default_steps
from installer.stage2.steps.s09_shortcuts_scheduler import S09ShortcutsScheduler
from installer.stage2.steps.s10_summary import S10Summary
from installer.stage2.testing import FakeUi, example_answers, make_test_context
from ns_common.config import save_runtime
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_supervisor import cli, pwsh, schedule, update
from ns_supervisor.schedule_cfg import read_run_time, read_schedule_mode, schedule_file, write_run_time
from tests.test_ns_update import make_app, make_zip


def done(out: str = "", code: int = 0, err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["powershell"], code, out, err)


class FakeTaskScheduler:
    """The Windows Task Scheduler behind `pwsh.run_powershell`: one task, read and re-registered by script.

    `before` is `(state, "HH:MM")` or None (no task). `start_suffix` is what Windows appends to the trigger's
    StartBoundary (`""` or a UTC offset). With `refuse_s4u` the register script falls back to the interactive
    logon, as it does when Windows refuses "run whether logged on or not".
    """

    def __init__(
        self, before: tuple[str, str] | None, *, start_suffix: str = "", refuse_s4u: bool = False
    ) -> None:
        self.task = {"state": before[0], "at": before[1]} if before else None
        self.start_suffix = start_suffix
        self.refuse_s4u = refuse_s4u
        self.scripts: list[str] = []
        self.registered = 0

    def __call__(self, script: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        self.scripts.append(script)
        if "Register-ScheduledTask" in script:
            at = re.search(r"\[datetime\]'(\d\d:\d\d)'", script)
            assert at is not None
            disabled = "Disable-ScheduledTask" in script
            self.task = {"state": "Disabled" if disabled else "Ready", "at": at.group(1)}
            self.registered += 1
            return done("registered:interactive\n" if self.refuse_s4u else "registered:s4u\n")
        if "Unregister-ScheduledTask" in script:
            self.task = None
            return done("removed")
        if self.task is None:
            return done("not registered\n")
        start = f"2026-11-09T{self.task['at']}:00{self.start_suffix}"
        return done(f"state: {self.task['state']}\nstart: {start}\n")

    @property
    def after(self) -> tuple[str, str] | None:
        return (self.task["state"], self.task["at"]) if self.task else None


def install_fake(
    monkeypatch: pytest.MonkeyPatch, before: tuple[str, str] | None, **kw: Any
) -> FakeTaskScheduler:
    fake = FakeTaskScheduler(before, **kw)
    monkeypatch.setattr(pwsh, "run_powershell", fake)
    return fake


def schedule_json(paths: Paths) -> dict[str, Any]:
    return json.loads(schedule_file(paths).read_text(encoding="utf-8"))


def update_log_lines(paths: Paths) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for f in sorted(paths.logs.glob("update-*.jsonl")):
        out += [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return out


# ------------------------------------------------------------------------------------------ ns update
@pytest.mark.parametrize(
    ("before", "after", "mode"),
    [
        (("Disabled", "04:15"), ("Disabled", "04:15"), "disabled"),  # D-28: the owner switched it off
        (("Ready", "03:00"), ("Ready", "03:00"), "registered"),
        (("Ready", "05:45"), ("Ready", "05:45"), "registered"),  # the owner moved it in Task Scheduler
        (("Disabled", "02:30"), ("Disabled", "02:30"), "disabled"),
    ],
    ids=["disabled-0415", "ready-0300", "ready-moved-0545", "disabled-0230"],
)
def test_ns_update_keeps_the_state_and_time_of_the_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    before: tuple[str, str],
    after: tuple[str, str],
    mode: str,
):
    paths = Paths(tmp_path / "ns")
    write_run_time(paths, "03:00", "registered")  # the product's record never saw the owner's change
    fake = install_fake(monkeypatch, before)
    env = update.UpdateEnv(
        stop=lambda: None,
        sync=lambda a: None,
        verify=lambda a: True,
        reregister=lambda a: cli._reregister(paths, a),
        settle=lambda: None,
    )
    res = update.update_app(make_zip(tmp_path), make_app(tmp_path), env, paths=paths)

    assert fake.registered == 1 and fake.after == after
    assert read_schedule_mode(paths) == mode and schedule_json(paths)["run_time"] == after[1]
    assert any(n.startswith("scheduled task re-registered") for n in res.notes)
    # the update says what was kept, in the notes the owner sees and in the log
    note = next(n for n in res.notes if n.startswith("scheduled task re-registered"))
    assert after[1] in note and ("switched off" in note) == (after[0] == "Disabled")
    kept = [r for r in update_log_lines(paths) if r["event"] == "schedule_state_kept"]
    assert len(kept) == 1
    assert kept[0]["data"]["before_enabled"] is (before[0] != "Disabled")
    assert kept[0]["data"]["before_time"] == before[1] and kept[0]["data"]["after_time"] == after[1]
    assert kept[0]["data"]["after_enabled"] is (after[0] != "Disabled")


def test_a_start_boundary_with_an_offset_still_gives_the_clock_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = Paths(tmp_path / "ns")
    write_run_time(paths, "03:00")
    fake = install_fake(monkeypatch, ("Disabled", "04:15"), start_suffix="+02:00")
    cli._reregister(paths, tmp_path)
    assert fake.after == ("Disabled", "04:15")


def test_no_task_before_registers_an_enabled_task_from_the_saved_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The task was deleted (or never made) but schedule.json has a schedule: as before the fix, enabled."""
    paths = Paths(tmp_path / "ns")
    write_run_time(paths, "03:30", "registered")
    fake = install_fake(monkeypatch, None)
    notes = cli._reregister(paths, tmp_path)
    assert fake.after == ("Ready", "03:30") and read_schedule_mode(paths) == "registered"
    assert notes and notes[0].startswith("scheduled task re-registered") and "switched off" not in notes[0]
    events = [r["event"] for r in update_log_lines(paths)]
    assert "schedule_state_no_task" in events and "schedule_state_kept" not in events


def test_nothing_is_touched_without_a_schedule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    paths = Paths(tmp_path / "ns")
    fake = install_fake(monkeypatch, ("Ready", "03:00"))  # a task nobody registered through the product
    assert cli._reregister(paths, tmp_path) == [] and fake.scripts == []


def test_a_disabled_record_whose_task_is_gone_comes_back_disabled_at_the_recorded_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = Paths(tmp_path / "ns")
    write_run_time(paths, "04:15", "disabled")  # an earlier update kept it off
    fake = install_fake(monkeypatch, None)
    cli._reregister(paths, tmp_path)
    assert fake.after == ("Disabled", "04:15") and read_schedule_mode(paths) == "disabled"
    assert schedule_json(paths)["run_time"] == "04:15"


def test_a_task_the_owner_switched_back_on_is_registered_enabled_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The task in Windows is the truth: schedule.json said disabled, Task Scheduler says Ready."""
    paths = Paths(tmp_path / "ns")
    write_run_time(paths, "04:15", "disabled")
    fake = install_fake(monkeypatch, ("Ready", "04:15"))
    cli._reregister(paths, tmp_path)
    assert fake.after == ("Ready", "04:15") and read_schedule_mode(paths) == "registered"
    assert read_run_time(paths) == "04:15"


def test_interactive_only_mode_is_still_recorded_when_the_task_is_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = Paths(tmp_path / "ns")
    write_run_time(paths, "03:00", "interactive_only")
    fake = install_fake(monkeypatch, ("Ready", "03:00"), refuse_s4u=True)
    cli._reregister(paths, tmp_path)
    assert fake.after == ("Ready", "03:00") and read_schedule_mode(paths) == "interactive_only"


def test_a_disabled_interactive_task_stays_disabled_and_is_not_claimed_to_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = Paths(tmp_path / "ns")
    write_run_time(paths, "03:00", "interactive_only")
    fake = install_fake(monkeypatch, ("Disabled", "03:00"), refuse_s4u=True)
    cli._reregister(paths, tmp_path)
    assert fake.after == ("Disabled", "03:00")
    assert read_schedule_mode(paths) == "disabled" and read_run_time(paths) is None  # no run is coming


def test_a_dev_install_is_left_alone_by_the_update_and_stays_disabled_when_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = Paths(tmp_path / "ns")
    save_runtime(paths, Runtime.default("cpu-dev").model_copy(update={"dev_mode": True}))
    write_run_time(paths, "03:00", "disabled")
    fake = install_fake(monkeypatch, ("Disabled", "03:00"))
    assert cli._reregister(paths, tmp_path) == [] and fake.scripts == []  # as before: not touched
    # anything else that registers it (Settings, ns schedule install) still gets it disabled
    fake2 = install_fake(monkeypatch, ("Ready", "03:00"))
    schedule.install(paths, "05:00", tmp_path, keep_state=True)
    assert fake2.after == ("Disabled", "05:00") and read_schedule_mode(paths) == "disabled"


def test_when_the_task_cannot_be_read_nothing_is_replaced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    paths = Paths(tmp_path / "ns")
    write_run_time(paths, "03:00")
    seen: list[str] = []

    def refuse(script: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(script)
        return done(code=1, err="Access is denied")

    monkeypatch.setattr(pwsh, "run_powershell", refuse)
    notes = cli._reregister(paths, tmp_path)
    assert notes == ["scheduled task NOT re-registered: Access is denied"]
    assert len(seen) == 1 and "Register-ScheduledTask" not in seen[0]  # it never got to replacing it
    assert read_run_time(paths) == "03:00"


# -------------------------------------------------------------------- the other routes that register it
def test_a_fresh_install_registers_an_enabled_task_as_today(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    paths = Paths(tmp_path / "ns")
    fake = install_fake(monkeypatch, None)
    msg = schedule.install(paths, "03:00", tmp_path, keep_state=True)
    assert fake.after == ("Ready", "03:00") and read_schedule_mode(paths) == "registered"
    assert "switched off" not in msg and "DISABLED" not in msg


def test_without_keep_state_install_replaces_the_task_enabled_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = Paths(tmp_path / "ns")
    fake = install_fake(monkeypatch, ("Disabled", "04:15"))
    schedule.install(paths, "03:00", tmp_path)
    assert fake.after == ("Ready", "03:00") and len(fake.scripts) == 1  # no read, no surprise


def test_ns_schedule_install_keeps_a_task_the_owner_switched_off(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = Paths.from_env()
    fake = install_fake(monkeypatch, ("Disabled", "04:15"))
    runner = CliRunner()
    res = runner.invoke(cli.app, ["schedule", "install"])  # no --time: the task's own time
    assert res.exit_code == 0, res.output
    assert fake.after == ("Disabled", "04:15") and schedule.KEPT_OFF_MARK in res.output
    res = runner.invoke(cli.app, ["schedule", "install", "--time", "06:00"])  # the owner asks for a new time
    assert res.exit_code == 0, res.output
    assert fake.after == ("Disabled", "06:00") and schedule_json(paths)["run_time"] == "06:00"


def test_the_settings_page_run_time_change_keeps_a_task_the_owner_switched_off(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_dashboard.adapters import RealScheduleAdapter

    paths = Paths.from_env()
    fake = install_fake(monkeypatch, ("Disabled", "04:15"))
    result = RealScheduleAdapter(paths).apply("05:00")
    assert result["applied"] is True and schedule.KEPT_OFF_MARK in result["note"]
    assert fake.after == ("Disabled", "05:00") and read_schedule_mode(paths) == "disabled"
    fake2 = install_fake(monkeypatch, ("Ready", "04:15"))
    RealScheduleAdapter(paths).apply("05:00")
    assert fake2.after == ("Ready", "05:00") and read_schedule_mode(paths) == "registered"


def test_setup_over_a_task_the_owner_switched_off_says_so_and_does_not_claim_a_nightly_run(tmp_path: Path):
    app = tmp_path / "app"
    (app / "launchers").mkdir(parents=True)
    (app / "launchers" / "nightshift-dashboard.ps1").write_text("# x\n", encoding="utf-8")
    kept = f"registered (Nightshift Daily daily at 04:30; {schedule.KEPT_OFF_NOTE})"
    runner = FakeRunner(
        tmp_path / "desktop", results={"ns schedule install --time 04:30": RunResult(0, kept)}
    )
    ui = FakeUi()
    ctx = make_test_context(
        tmp_path,
        raw_answers=example_answers(run_time="04:30"),
        resolve=True,
        app_path=app,
        runner=runner,
        ui=ui,
    )
    S09ShortcutsScheduler().run(ctx)
    assert (ctx.flag("schedule"), ctx.flag("schedule_mode")) == ("installed", "disabled")
    assert "SWITCHED OFF" in ui.text and "wakes the PC" not in ui.text and ctx.warnings == []


def test_the_setup_summary_names_a_task_left_switched_off_without_calling_it_dev_mode(tmp_path: Path):
    ui = FakeUi()
    ctx = make_test_context(tmp_path, raw_answers=example_answers(run_time="04:30"), resolve=True, ui=ui)
    for step in default_steps():
        if step.id != "s10_summary":
            ctx.state.mark(step.id, "done")
    ctx.set_flag("schedule", "installed")
    ctx.set_flag("schedule_mode", "disabled")
    S10Summary().run(ctx)
    assert "switched off in Task Scheduler" in ui.text and "dev mode" not in ui.text.lower()
    assert "the PC wakes itself" not in ui.text
