"""D-28: re-registering the 'Nightshift Daily' task must not undo what the owner did in Task Scheduler.

`ns update` (Settings, CLI), the Settings run-time change, `ns schedule install` and the setup step all
look at the task first. The task in Windows is the truth for its state (enabled or not) and its run time: an
enabled task keeps its time, and one the owner disabled is left exactly as it is and read as "Automatic runs
off" (no `schedule.json`; tests/test_nightly_optional.py has the switch itself). `ns schedule install` with no
task registers an enabled one as before. The Windows Task Scheduler is a fake here: no real task is created
or changed.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import timedelta, timezone
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
            # D-28 round 2: a kept-off task is registered disabled in the one call; registering it enabled and
            # disabling it afterwards leaves a window in which it can fire (and stays on if the disable fails)
            assert "Disable-ScheduledTask" not in script
            disabled = re.search(r"-Disable(?![-\w])", script) is not None
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
        (("Ready", "03:00"), ("Ready", "03:00"), "registered"),
        (("Ready", "05:45"), ("Ready", "05:45"), "registered"),  # the owner moved it in Task Scheduler
    ],
    ids=["ready-0300", "ready-moved-0545"],
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


@pytest.mark.parametrize(
    ("start", "local_hours", "clock"),
    [
        ("2026-11-09T04:15:00", 2, "04:15"),  # no zone: the clock time is local already
        ("2026-11-09T01:00:00Z", 2, "03:00"),  # synchronized across zones: one moment, this PC's clock
        ("2026-11-09T04:15:00+02:00", 2, "04:15"),  # the offset is this PC's own
        ("2026-11-09T04:15:00-05:00", 1, "10:15"),  # another zone's wall time, converted
        ("2026-11-09T23:30:00Z", 2, "01:30"),  # across midnight
    ],
)
def test_a_start_boundary_with_a_zone_is_turned_into_this_pcs_clock_time(
    start: str, local_hours: int, clock: str
):
    """D-28 break-it round: reading only the clock part shifted a synchronized trigger by its offset."""
    local = timezone(timedelta(hours=local_hours))
    got = schedule.parse_task_state(f"state: Disabled\nstart: {start}\n", local)
    assert got == schedule.TaskState(enabled=False, run_time=clock)


def test_nothing_is_touched_without_a_schedule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    paths = Paths(tmp_path / "ns")
    fake = install_fake(monkeypatch, ("Ready", "03:00"))  # a task nobody registered through the product
    assert cli._reregister(paths, tmp_path) == [] and fake.scripts == []


def test_a_task_the_owner_switched_back_on_is_registered_enabled_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The task in Windows is the truth: schedule.json said disabled, Task Scheduler says Ready."""
    paths = Paths(tmp_path / "ns")
    write_run_time(paths, "04:15", "switched_off")
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
    assert fake.after == ("Disabled", "03:00") and fake.registered == 0  # left exactly as it is
    assert read_schedule_mode(paths) == "none" and read_run_time(paths) is None  # no run is coming


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
    assert fake.after == ("Ready", "03:00") and "Register-ScheduledTask" in fake.scripts[0]  # no read first


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
    assert fake.after == ("Disabled", "04:15") and fake.registered == 0  # the task is left as it is
    assert not schedule_file(paths).exists()  # off: no record
    kept = json.loads((paths.config / "dashboard.json").read_text(encoding="utf-8"))
    assert kept["run_time"] == "06:00"  # the time waits for --enable or the Settings switch


def test_the_settings_page_run_time_change_keeps_a_task_the_owner_switched_off(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_dashboard.adapters import RealScheduleAdapter

    paths = Paths.from_env()
    fake = install_fake(monkeypatch, ("Disabled", "04:15"))
    result = RealScheduleAdapter(paths).apply("05:00")
    assert result["applied"] is True and "Automatic runs are off" in result["note"]
    assert fake.after == ("Disabled", "04:15") and read_schedule_mode(paths) == "none"
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


# ---------------------------------------------------------- break-it round: dev install, then a real one
def test_installing_for_real_over_a_dev_install_registers_an_enabled_task(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """docs/DEV_MODE.md: the dev task is Disabled and schedule.json says `disabled`: not the owner's choice.

    setup step s09 runs exactly `ns schedule install --time T` for a real install; the task must come out
    enabled and recorded `registered`, and the output must not claim it was left switched off.
    """
    paths = Paths.from_env()
    write_run_time(paths, "03:00", "disabled")  # what the dev install's s09 left
    fake = install_fake(monkeypatch, ("Disabled", "03:00"))
    res = CliRunner().invoke(cli.app, ["schedule", "install", "--time", "03:00"])
    assert res.exit_code == 0, res.output
    assert fake.after == ("Ready", "03:00") and read_schedule_mode(paths) == "registered"
    assert schedule.KEPT_OFF_MARK not in res.output and read_run_time(paths) == "03:00"


def test_an_update_of_a_real_install_whose_owner_ran_schedule_install_disabled_leaves_the_task_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """D-28 round 2: `ns schedule install --disabled` on a real install is the owner pausing the nightly run.

    On main the update did not look at a `disabled` record. The task is Disabled, schedule.json says
    `disabled`, no runtime.json (a real install): after the update it is still Disabled at the same time and
    nothing was registered, so no unattended run starts at 03:00. (Setup step s09 runs `ns schedule install
    --time T`, which switches a dev install's task on: the test above.)
    """
    paths = Paths(tmp_path / "ns")
    write_run_time(paths, "03:00", "disabled")
    fake = install_fake(monkeypatch, ("Disabled", "03:00"))
    env = update.UpdateEnv(
        stop=lambda: None,
        sync=lambda a: None,
        verify=lambda a: True,
        reregister=lambda a: cli._reregister(paths, a),
        settle=lambda: None,
    )
    update.update_app(make_zip(tmp_path), make_app(tmp_path), env, paths=paths)
    assert fake.after == ("Disabled", "03:00") and fake.registered == 0 and fake.scripts == []
    assert schedule_json(paths) == {"run_time": "03:00", "task_name": "Nightshift Daily", "mode": "disabled"}


def test_a_disabled_record_is_left_alone_by_an_update_whatever_state_the_task_is_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The record says the owner chose `disabled`: even a task someone switched on or moved is not touched."""
    paths = Paths(tmp_path / "ns")
    write_run_time(paths, "04:15", "disabled")
    fake = install_fake(monkeypatch, ("Ready", "05:00"))
    assert cli._reregister(paths, tmp_path) == [] and fake.scripts == []
    assert fake.after == ("Ready", "05:00") and read_schedule_mode(paths) == "disabled"


def test_a_dev_install_still_registers_its_task_disabled_and_the_update_leaves_it_alone(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = Paths.from_env()
    save_runtime(paths, Runtime.default("cpu-dev").model_copy(update={"dev_mode": True}))
    write_run_time(paths, "03:00", "disabled")
    fake = install_fake(monkeypatch, None)
    res = CliRunner().invoke(cli.app, ["schedule", "install", "--time", "03:00"])
    assert res.exit_code == 0, res.output
    assert fake.after == ("Disabled", "03:00") and read_schedule_mode(paths) == "disabled"
    before = len(fake.scripts)
    assert cli._reregister(paths, tmp_home) == [] and len(fake.scripts) == before


# ------------------------------------------------------------------ break-it round: one way to switch it on
def test_ns_schedule_install_enable_switches_a_task_on_that_was_switched_off(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = Paths.from_env()
    write_run_time(paths, "04:15", "switched_off")
    fake = install_fake(monkeypatch, ("Disabled", "04:15"))
    runner = CliRunner()
    plain = runner.invoke(cli.app, ["schedule", "install"])
    assert plain.exit_code == 0 and fake.after == ("Disabled", "04:15")
    assert "--enable" in plain.output  # the note says how to switch it on
    res = runner.invoke(cli.app, ["schedule", "install", "--enable"])
    assert res.exit_code == 0, res.output
    assert fake.after == ("Ready", "04:15") and read_schedule_mode(paths) == "registered"
    assert schedule.KEPT_OFF_MARK not in res.output and read_run_time(paths) == "04:15"


def test_enable_and_disabled_together_are_refused_and_change_nothing(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    fake = install_fake(monkeypatch, ("Ready", "03:00"))
    res = CliRunner().invoke(cli.app, ["schedule", "install", "--enable", "--disabled"])
    assert res.exit_code != 0 and fake.scripts == [] and "cannot be used together" in res.output


def test_enable_does_not_switch_a_dev_installs_task_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    paths = Paths(tmp_path / "ns")
    save_runtime(paths, Runtime.default("cpu-dev").model_copy(update={"dev_mode": True}))
    fake = install_fake(monkeypatch, ("Disabled", "03:00"))
    schedule.install(paths, None, tmp_path, keep_state=True, enable=True)
    assert fake.after == ("Disabled", "03:00") and read_schedule_mode(paths) == "disabled"


# -------------------------------------------- break-it round: what the dashboard says about a task kept off
def _off_task_dashboard(tmp_home: Path, monkeypatch: pytest.MonkeyPatch, task: tuple[str, str]):
    from ns_dashboard.adapters import RealScheduleAdapter
    from ns_dashboard.fakes import seed_channel
    from tests.test_dashboard_handlers import make

    paths = Paths.from_env()
    write_run_time(paths, "04:15", "switched_off")
    fake = install_fake(monkeypatch, task)
    monkeypatch.setattr("ns_dashboard.state.SYNC_INLINE", True)  # the look runs in the call
    dash, state = make(tmp_home)
    state.adapters.schedule = RealScheduleAdapter(state.paths)  # the adapter that looks at the (fake) task
    seed_channel(state.db, state.paths, "after-dark")  # an active channel: a night run would be due
    state.paths.config.mkdir(parents=True, exist_ok=True)
    (state.paths.config / "dashboard.json").write_text('{"run_time": "03:00"}', encoding="utf-8")
    return dash, state, fake


def test_a_task_kept_off_shows_the_tasks_own_time_no_coming_run_and_no_windows_refused_claim(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """A task disabled in Task Scheduler is Automatic runs off; its own time is the one kept."""
    dash, state, _ = _off_task_dashboard(tmp_home, monkeypatch, ("Disabled", "04:15"))
    settings = dash.settings.get_settings()
    assert settings["schedule_registered"] is False  # off, said as a state and not as a refusal
    assert settings["schedule_time"] == "04:15"  # the task's time, not dashboard.json's 03:00
    assert settings["next_run"] is None  # switched off: no run is coming
    status = dash.engine.status()
    assert status["schedule_registered"] is False and status["schedule_time"] == "04:15"
    assert status["next_run"] is None and status["schedule_mode"] == "none"


def test_a_dev_installs_disabled_task_still_reads_as_no_task_on_the_page(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    from tests.test_dashboard_handlers import make

    write_run_time(Paths.from_env(), "04:15", "disabled")
    dash, _ = make(tmp_home)
    settings = dash.settings.get_settings()
    assert settings["schedule_registered"] is False and settings["schedule_time"] == "03:00"


def test_the_page_follows_the_owner_switching_the_task_back_on_in_task_scheduler(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """The window between 'switched on in Windows' and the next re-registration (D-28 break-it round)."""
    dash, state, fake = _off_task_dashboard(tmp_home, monkeypatch, ("Ready", "05:00"))
    settings = dash.settings.get_settings()
    assert read_schedule_mode(state.paths) == "registered"  # schedule.json caught up with Windows
    assert settings["schedule_time"] == "05:00" and settings["next_run"] is not None
    assert fake.registered == 0  # it only read the task, never replaced it


def test_the_record_follows_a_time_moved_while_the_task_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = Paths(tmp_path / "ns")
    write_run_time(paths, "04:15", "switched_off")
    install_fake(monkeypatch, ("Disabled", "06:00"))
    assert schedule.sync_record(paths) is True  # the older `switched_off` record becomes "no record"
    assert read_schedule_mode(paths) == "none" and not schedule_file(paths).exists()
    kept = json.loads((paths.config / "dashboard.json").read_text(encoding="utf-8"))
    assert kept["run_time"] == "06:00"  # the task's own time is the one kept
    assert schedule.sync_record(paths) is False  # nothing more to learn


def test_the_record_is_left_alone_when_the_task_cannot_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    paths = Paths(tmp_path / "ns")
    write_run_time(paths, "04:15", "switched_off")
    monkeypatch.setattr(pwsh, "run_powershell", lambda s, **kw: done(code=1, err="Access is denied"))
    assert schedule.sync_record(paths) is False and read_schedule_mode(paths) == "switched_off"
    write_run_time(paths, "03:00", "registered")
    assert schedule.sync_record(paths) is False and read_run_time(paths) == "03:00"


# ------------------------------------------------- D-28 round 2: a read error is not "there is no task"
def test_a_scheduler_error_other_than_not_found_is_refused_and_nothing_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`Get-ScheduledTask -ErrorAction SilentlyContinue` hid access denied: read as 'no task', the task was
    replaced enabled over the owner's. The read script now reports the error and the install refuses."""
    paths = Paths(tmp_path / "ns")
    write_run_time(paths, "03:00", "registered")
    seen: list[str] = []

    def denied(script: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        seen.append(script)
        return done("error: PermissionDenied: Access is denied.\n")

    monkeypatch.setattr(pwsh, "run_powershell", denied)
    notes = cli._reregister(paths, tmp_path)
    assert len(notes) == 1 and notes[0].startswith("scheduled task NOT re-registered")
    assert "Access is denied" in notes[0]
    assert len(seen) == 1 and "Register-ScheduledTask" not in seen[0]
    assert read_run_time(paths) == "03:00" and read_schedule_mode(paths) == "registered"


def test_ns_schedule_install_refuses_with_the_error_code_when_the_task_cannot_be_read(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    fake = FakeTaskScheduler(("Disabled", "04:15"))

    def denied_read(script: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        if "Register-ScheduledTask" in script:
            return fake(script, **kw)
        return done("error: PermissionDenied: Access is denied.\n")

    monkeypatch.setattr(pwsh, "run_powershell", denied_read)
    res = CliRunner().invoke(cli.app, ["schedule", "install"])
    assert res.exit_code != 0 and "NS-E204" in res.output and "Access is denied" in res.output
    assert fake.registered == 0 and fake.after == ("Disabled", "04:15")


def test_the_settings_page_run_time_change_is_refused_when_the_task_cannot_be_read(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_dashboard.adapters import RealScheduleAdapter

    monkeypatch.setattr(
        pwsh, "run_powershell", lambda s, **kw: done("error: PermissionDenied: Access is denied.\n")
    )
    result = RealScheduleAdapter(Paths.from_env()).apply("05:00")
    assert result["applied"] is False and "Access is denied" in result["note"]


def test_no_such_task_is_still_read_as_no_task(monkeypatch: pytest.MonkeyPatch):
    install_fake(monkeypatch, None)
    assert schedule.read_task() is None


def test_the_read_script_tells_not_found_from_other_errors():
    script = schedule.build_read_script()
    assert "-ErrorVariable" in script and "ObjectNotFound" in script and "'error: '" in script


needs_powershell = pytest.mark.skipif(pwsh.find_powershell() is None, reason="powershell not on PATH")

# A Get-ScheduledTask stand-in that fails (or succeeds) the way the real cmdlet does: a non-terminating error
# in a given category, which the read script collects with -ErrorVariable.
GET_TASK_STUB = """
function Get-ScheduledTask {
    [CmdletBinding()] param($TaskName)
    switch ($env:NS_GET) {
        'denied' { Write-Error -Message 'Access is denied.' -Category PermissionDenied }
        'missing' { Write-Error -Message 'No task found.' -Category ObjectNotFound }
        'found' { [pscustomobject]@{ State = 'Disabled'
            Triggers = @([pscustomobject]@{ StartBoundary = '2026-11-01T04:15:00' }) } }
    }
}
"""


def run_read_script(**env: str) -> str:
    r = pwsh.run_powershell(GET_TASK_STUB + schedule.build_read_script(), env_extra=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip()


@needs_powershell
def test_the_read_script_run_in_powershell_reports_an_access_error_as_an_error_not_as_no_task():
    out = run_read_script(NS_GET="denied")
    assert out.startswith("error: PermissionDenied: ") and "Access is denied" in out
    with pytest.raises(schedule.ScheduleError, match="could not be read"):
        schedule.parse_task_state(out)


@needs_powershell
def test_the_read_script_run_in_powershell_reports_a_missing_task_as_not_registered():
    out = run_read_script(NS_GET="missing")
    assert out == "not registered" and schedule.parse_task_state(out) is None


@needs_powershell
def test_the_read_script_run_in_powershell_reads_state_and_time_of_a_task_that_exists():
    state = schedule.parse_task_state(run_read_script(NS_GET="found"))
    assert state == schedule.TaskState(enabled=False, run_time="04:15")


# ------------------------------------------ D-28 round 2: a kept-off task is registered disabled in one step
def test_an_enabled_task_is_registered_without_the_disable_switch(tmp_path: Path):
    paths = Paths(tmp_path / "ns")
    on = schedule.build_install_script(paths, "03:00", tmp_path)
    off = schedule.build_install_script(paths, "03:00", tmp_path, disabled=True)
    assert not re.search(r"-Disable(?![-\w])", on)
    assert re.search(r"-Disable(?![-\w])", off) and "Disable-ScheduledTask" not in off
