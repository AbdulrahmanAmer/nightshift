# ruff: noqa: E501, F811
"""nightly-optional, part b: setup asks once whether Nightshift runs by itself every night (off by default),
and setup, Repair and a reinstall over an install never change a choice the owner already made.

Entry: the wizard's own step objects (s01, s09, s10) over `make_test_context`, the window's `answers_view`
functions, the Settings switch through the dashboard route and `ns schedule` through `CliRunner`. The Windows
Task Scheduler is part a's fake (`FakeWindows`, imported, not copied): `SetupRunner` hands the two things
setup asks Windows (`ns schedule install ...` and its own look at the task) to it, so no real task is read,
made or removed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from installer.stage2 import reinstall
from installer.stage2.runner import RunResult
from installer.stage2.simulate import Call, FakeRunner
from installer.stage2.steps.s01_questions import S01Questions
from installer.stage2.steps.s09_shortcuts_scheduler import S09ShortcutsScheduler
from installer.stage2.steps.s10_summary import S10Summary
from installer.stage2.testing import FakeUi, example_answers, make_test_context
from installer.stage2.web import answers_view
from ns_common import config_compat, new_fields
from ns_common.errors import describe
from ns_common.paths import Paths
from ns_supervisor import cli, notify, pwsh, schedule
from ns_supervisor.schedule_cfg import read_schedule_mode, schedule_file, write_run_time
from tests import frozen_config_models_0_1_14 as old
from tests.test_nightly_optional import (
    FakeWindows,
    dashboard,
    put,
    record,
    result,
    saved_time,
    unopenable,
    verify_quick,
    w207_notes,
    windows,  # noqa: F401  (part a's fixture: the fake Task Scheduler)
)

ROOT = Path(__file__).resolve().parents[1]


class SetupRunner(FakeRunner):
    """`FakeRunner`, except that what setup asks Windows about the task reaches the fake Task Scheduler:
    `ns schedule ...` runs the real command in this process, and setup's own look runs the read script."""

    def _default(self, call: Call) -> RunResult:
        if call.tag.startswith("ns schedule "):
            args = call.argv[call.argv.index("schedule"):]
            res = CliRunner().invoke(cli.app, args, env={"NIGHTSHIFT_HOME": call.env["NIGHTSHIFT_HOME"]})
            return RunResult(res.exit_code, res.output)
        if call.tag == "ps:task-read":
            done = pwsh.run_powershell(schedule.build_read_script())
            return RunResult(done.returncode, done.stdout, done.stderr)
        return super()._default(call)


def context(tmp_path: Path, raw: dict[str, Any], *, file: bool = False, ui: FakeUi | None = None, app: Path | None = None) -> Any:
    """One setup process over the home `tmp_path/home`. `file`: the answers come from a file outside the home
    (next to INSTALL.cmd, or `--answers`); otherwise they are the ones in `config/answers.json`."""
    ctx = make_test_context(
        tmp_path, raw_answers=raw, ui=ui or FakeUi(interactive=False), runner=SetupRunner(tmp_path / "desktop"),
        app_path=app,
    )
    if file:
        src = tmp_path / "answers.json"
        src.write_text(json.dumps(raw), encoding="utf-8")
        ctx.answers_path = src
    return ctx


def moved_app(tmp_path: Path, name: str) -> Path:
    """Another program folder (INSTALL.cmd run from a newer zip): s09 has work to do again."""
    app = tmp_path / name
    (app / "launchers").mkdir(parents=True, exist_ok=True)
    (app / "launchers" / "nightshift-dashboard.ps1").write_text("# launcher\n", encoding="utf-8")
    return app


def setup(ctx: Any, *, only: tuple[str, ...] = ("s01", "s09", "s10")) -> str:
    """Run s01, s09 and s10 the way the wizard does (check, clear the step's flags, run); what was said."""
    for step in (S01Questions(), S09ShortcutsScheduler(), S10Summary()):
        if step.id[:3] not in only:
            continue
        if not step.check(ctx):
            ctx.clear_flags(*step.clears_on_run)
            step.run(ctx)
        ctx.state.mark(step.id, "done")
    return ctx.ui.text


def summary(text: str) -> str:
    """The line the summary says about Automatic runs."""
    lines = [ln for ln in text.splitlines() if ln.startswith("Automatic runs:")]
    return lines[-1] if lines else ""


def saved_answers(paths: Paths) -> dict[str, Any]:
    return json.loads((paths.config / "answers.json").read_text(encoding="utf-8"))


def settings(ctx: Any, *, on: bool | None = None, run_time: str | None = None) -> None:
    """The owner's change in Settings > Schedule (the switch or the time)."""
    schedule.set_automatic(ctx.paths, ctx.app_path, on=on, run_time=run_time)


def shown(raw: dict[str, Any], home: Path) -> dict[str, Any]:
    """What the setup window shows for `raw` over `home` (`controller` builds its view the same way)."""
    try:
        from installer.stage2.web import nightly_view
    except ImportError:  # the base has no such look: its window shows no switch
        return answers_view.answers_to_view(raw)
    return answers_view.answers_to_view(raw, nightly_now=nightly_view.current_choice(home))


def window_answers(ctx: Any, view: dict[str, Any], **changed: Any) -> dict[str, Any]:
    """The owner changes `changed` in the window and presses Install: the answers the window writes."""
    draft = {k: v for k, v in view.items() if k not in ("pexelsKeySet", "pixabayKeySet")}
    draft.update(pexelsKey=None, pixabayKey=None, **changed)
    draft["homePath"] = str(ctx.paths.home)
    return answers_view.draft_to_answers(draft)


def window_writes(ctx: Any, data: dict[str, Any]) -> Any:
    """`controller._write_answers`, then the wizard the window starts (a new process over the same home)."""
    target = ctx.paths.config / "answers.json"
    answers_view.keep_earlier_answers(target)
    answers_view.write_answers(target, data)
    return context(ctx.paths.home.parent, answers_view.read_answers(target), app=moved_app(ctx.paths.home.parent, "app-window"))


# ------------------------------------------------------------------------------------ a first install
def test_an_unattended_first_install_without_the_answer_registers_nothing(tmp_path: Path, windows: FakeWindows):
    ctx = context(tmp_path, example_answers(run_time="04:30"), file=True)

    said = setup(ctx)

    assert windows.has is None and windows.changes() == []  # nothing was registered, nothing removed
    assert record(ctx.paths) is None and saved_time(ctx.paths) == "04:30"  # the time waits for the switch
    assert summary(said).startswith("Automatic runs: off") and "Settings" in summary(said)
    assert "every night at" not in said and ctx.warnings == []  # off is a choice, not a failure
    assert ctx.flag("schedule") == "installed" and ctx.flag("schedule_mode") == "off"
    assert S09ShortcutsScheduler().check(ctx) is True  # the decision is recorded: a rerun has nothing to do


def test_a_first_install_that_says_yes_registers_it_at_the_asked_time(tmp_path: Path, windows: FakeWindows):
    ctx = context(tmp_path, example_answers(run_time="04:30", nightly=True), file=True)

    said = setup(ctx)

    assert windows.has == ("Ready", "04:30") and windows.changes() == ["register"]
    assert record(ctx.paths) == {"run_time": "04:30", "task_name": "Nightshift Daily", "mode": "registered"}
    assert summary(said).startswith("Automatic runs: on at 04:30")
    assert (ctx.flag("schedule"), ctx.flag("schedule_mode")) == ("installed", "s4u")
    # only while signed in: said with its code, and still on
    windows.task, windows.refuse_s4u = None, True
    other = context(tmp_path / "w205", example_answers(nightly=True), file=True)
    said = setup(other)
    assert windows.has == ("Ready", "03:00") and any(code == "NS-W205" for code, _ in other.warnings)
    assert "only while you are signed in" in summary(said) and read_schedule_mode(other.paths) == "interactive_only"


def test_a_first_install_windows_refuses_stays_off_with_ns_w206(tmp_path: Path, windows: FakeWindows):
    windows.refuse_all = True
    ctx = context(tmp_path, example_answers(run_time="04:30", nightly=True), file=True)
    said = setup(ctx)
    assert windows.has is None and record(ctx.paths) is None and saved_time(ctx.paths) == "04:30"
    assert any(code == "NS-W206" and "Access is denied" in detail for code, detail in ctx.warnings)
    assert summary(said).startswith("Automatic runs: off") and "NS-W206" in summary(said)
    assert ctx.flag("schedule") == "failed"


def test_the_console_asks_once_on_a_first_install_only(tmp_path: Path, windows: FakeWindows):
    question = "Run Nightshift by itself every night? You can turn this on later in Settings."
    ui = FakeUi({"every night": "y"}, interactive=True)
    ctx = context(tmp_path, example_answers(run_time="04:30"), ui=ui)
    setup(ctx)
    assert question in ui.asked and windows.has == ("Ready", "04:30")
    # Enter means no (a second PC): asked, nothing registered
    windows.task = None
    quiet = FakeUi(interactive=True)
    other = context(tmp_path / "enter", example_answers(), ui=quiet)
    setup(other)
    assert question in quiet.asked and windows.has is None and "nightly" not in saved_answers(other.paths)
    # a Repair is not asked again, and keeps what the owner has
    again = FakeUi({"every night": "n"}, interactive=True)
    repair = context(tmp_path, example_answers(run_time="04:30"), ui=again, app=moved_app(tmp_path, "app2"))
    windows.task = {"state": "Ready", "at": "04:30", "logon": "S4U"}
    setup(repair)
    assert question not in again.asked and windows.has == ("Ready", "04:30")


# ------------------------------------------------------------------------ a Repair and a reinstall
def test_a_repair_with_saved_answers_keeps_off_off_and_on_on(tmp_path: Path, windows: FakeWindows):
    first = context(tmp_path, example_answers(run_time="03:00"))
    setup(first)  # the first install: no answer, so off
    settings(first, on=True, run_time="05:00")  # the owner switches Automatic runs on in Settings
    windows.calls.clear()

    repair = context(tmp_path, saved_answers(first.paths), app=moved_app(tmp_path, "app2"))
    said = setup(repair)

    assert windows.has == ("Ready", "05:00")  # on, at the owner's time, not the answers' 03:00
    assert windows.changes() == ["register"]  # registered again, so the task starts the program that is here now
    assert summary(said) == "Automatic runs: on at 05:00, kept as set in Settings."
    assert record(repair.paths) == {"run_time": "05:00", "task_name": "Nightshift Daily", "mode": "registered"}

    settings(repair, on=False)  # then off in Settings
    windows.calls.clear()
    second = context(tmp_path, saved_answers(first.paths), app=moved_app(tmp_path, "app3"))
    said = setup(second)

    assert windows.has is None and windows.changes() == [] and record(second.paths) is None
    assert summary(said) == "Automatic runs: off, kept as set in Settings (switch them on in Settings > Schedule)."
    # a task the owner deleted by hand in Task Scheduler is not made again either
    settings(second, on=True)
    windows.task = None
    third = context(tmp_path, saved_answers(first.paths), app=moved_app(tmp_path, "app4"))
    assert summary(setup(third)).startswith("Automatic runs: off, kept") and windows.has is None


def test_a_stale_answers_file_does_not_turn_settings_back(tmp_path: Path, windows: FakeWindows):
    raw = example_answers(run_time="04:30", nightly=True)
    first = context(tmp_path, raw, file=True)
    setup(first)
    assert windows.has == ("Ready", "04:30")
    settings(first, on=False)  # months later the owner switches it off in Settings

    again = context(tmp_path, raw, file=True, app=moved_app(tmp_path, "app2"))  # INSTALL.cmd, the same file
    said = setup(again)

    assert windows.has is None and record(again.paths) is None
    assert summary(said) == "Automatic runs: off, kept as set in Settings (switch them on in Settings > Schedule)."
    # the same file with another choice changed in it runs s01 again: its old "nightly" still changes nothing
    edited = context(tmp_path, {**raw, "masters_per_week": 2}, file=True, app=moved_app(tmp_path, "app3"))
    setup(edited)
    assert windows.has is None and "nightly" not in saved_answers(edited.paths)


def test_the_window_answer_applies_only_to_the_state_it_showed(tmp_path: Path, windows: FakeWindows):
    first = context(tmp_path, example_answers(run_time="03:00"))
    setup(first)  # installed, Automatic runs off
    view = shown(saved_answers(first.paths), first.paths.home)
    assert (view.get("nightly"), view.get("nightlySeen")) == (False, "off")  # the window shows the switch off
    data = window_answers(first, view, nightly=True)  # the owner switches it on in the window ...
    settings(first, on=True, run_time="04:30")  # ... and meanwhile switches it on in Settings, at another time

    wizard = window_writes(first, data)
    said = setup(wizard)

    assert windows.has == ("Ready", "04:30") and record(wizard.paths)["run_time"] == "04:30"
    line = summary(said)
    assert line.startswith("Automatic runs: left on at 04:30.") and "setup changed nothing" in line
    assert "showed them off" in line and "Settings > Schedule" in line  # why, and where to change them
    assert "nightly_seen" not in saved_answers(wizard.paths)

    # the same answer made against the state Windows still has is applied
    view = shown(saved_answers(wizard.paths), wizard.paths.home)
    assert (view["nightly"], view["nightlySeen"]) == (True, "on@04:30")
    off = window_writes(wizard, window_answers(wizard, view, nightly=False))
    said = setup(off)
    assert windows.has is None and record(off.paths) is None
    assert summary(said) == "Automatic runs: off, as you chose in setup."
    assert saved_time(off.paths) == "04:30"  # the time is kept for switching on again


def test_a_changed_time_in_new_answers_moves_the_task_only_if_settings_did_not(tmp_path: Path, windows: FakeWindows):
    first = context(tmp_path, example_answers(run_time="03:00"))
    setup(first)
    settings(first, on=True)  # on, at the answers' own time
    assert windows.has == ("Ready", "03:00")

    moved = context(tmp_path, {"run_time": "05:30"}, file=True)  # new answers name another time (D-14)
    said = setup(moved)
    assert windows.has == ("Ready", "05:30") and saved_answers(moved.paths)["run_time"] == "05:30"
    assert summary(said) == "Automatic runs: on at 05:30, kept as set in Settings."

    settings(moved, run_time="04:00")  # the owner moves the time in Settings
    later = context(tmp_path, {"run_time": "06:00"}, file=True)
    said = setup(later)
    assert windows.has == ("Ready", "04:00")  # Settings wins over the file
    assert "The nightly time stays 04:00 as set in Settings." in said
    assert summary(said).startswith("Automatic runs: on at 04:00, kept as set in Settings.")


def test_nightly_and_nightly_seen_are_consumed_after_s09_and_never_inherited(tmp_path: Path, windows: FakeWindows):
    ctx = context(tmp_path, example_answers(nightly=True, nightly_seen="new"), file=True)
    setup(ctx, only=("s01",))
    held = saved_answers(ctx.paths)
    assert (held["nightly"], held["nightly_seen"]) == (True, "new")  # kept for s09, also across a restart
    assert S09ShortcutsScheduler().check(ctx) is False  # an answer nobody acted on yet is work to do
    setup(ctx, only=("s09",))
    assert windows.has == ("Ready", "03:00")
    assert not {"nightly", "nightly_seen"} & set(saved_answers(ctx.paths))  # used once, then gone
    assert S09ShortcutsScheduler().check(ctx) is True
    # and a later file that leaves them out inherits none from the kept answers
    for key in ("nightly", "nightly_seen"):
        assert key not in reinstall.INHERITED and key not in reinstall.SIMPLE_KEYS
    later = context(tmp_path, {"run_time": "03:00"}, file=True)
    reinstall.inherit(later, {**held, "masters_per_week": 3})
    assert later.raw_answers.get("masters_per_week") == 3 and "nightly" not in later.raw_answers
    assert reinstall.changes(later, held, {"nightly": False, "nightly_seen": "off"}) == []  # not a "change" either


def test_a_resumed_first_install_still_applies_the_answer(tmp_path: Path, windows: FakeWindows):
    raw = example_answers(run_time="04:30", nightly=True)
    setup(context(tmp_path, raw, file=True), only=("s01",))  # ... and the PC is switched off here
    assert windows.has is None

    resumed = context(tmp_path, raw, file=True)
    assert S01Questions().check(resumed) is True  # the questions are answered: s01 does not run again
    said = setup(resumed)
    assert windows.has == ("Ready", "04:30") and summary(said).startswith("Automatic runs: on at 04:30")

    # killed inside s09, after Windows took the task and before setup wrote anything down: the rerun gives the
    # same answer to the same state, and no second script runs
    windows.task = None
    killed = context(tmp_path / "k", raw, file=True)
    setup(killed, only=("s01",))
    real = killed.set_flag

    def die(name: str, value: Any) -> None:
        if name in ("schedule", "first_install"):
            raise KeyboardInterrupt  # the process is gone exactly here, before setup wrote its decision down
        real(name, value)

    killed.set_flag = die  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt):
        setup(killed, only=("s09",))
    killed.set_flag = real  # type: ignore[method-assign]
    assert windows.has == ("Ready", "04:30") and saved_answers(killed.paths).get("nightly") is True
    windows.calls.clear()
    rerun = context(tmp_path / "k", raw, file=True)
    said = setup(rerun)
    assert windows.has == ("Ready", "04:30") and windows.changes() == []
    assert summary(said).startswith("Automatic runs: on at 04:30") and "nightly" not in saved_answers(rerun.paths)


# ------------------------------------------------- Windows took the task, the record cannot be written
def test_a_registered_task_whose_record_cannot_be_written_is_undone_with_ns_e204(tmp_home: Path, tmp_path: Path, windows: FakeWindows):
    # the Settings switch
    client, _state, paths = dashboard(tmp_home)
    unopenable(schedule_file(paths))  # config/schedule.json cannot be replaced
    body = put(client, automatic_runs=True, schedule_time="04:30")
    said = result(body)
    assert windows.has is None and windows.changes() == ["register", "unregister"]  # Windows is as it was
    assert said.get("applied") is False and said.get("code") == "NS-E204"
    assert "so the change was undone: Automatic runs are still off" in said["note"]
    assert "schedule.json" in said["note"] and body["schedule_registered"] is False
    # `ns schedule install`
    windows.calls.clear()
    res = CliRunner().invoke(cli.app, ["schedule", "install", "--time", "04:30"])
    assert res.exit_code == 1 and "NS-E204" in res.output and "so the change was undone" in res.output
    assert windows.has is None
    # failed, then retried once the folder can be written: the same click works
    schedule_file(paths).rmdir()
    assert put(client, automatic_runs=True, schedule_time="04:30")["schedule_registered"] is True
    # setup (s09), on another home
    windows.task = None
    ctx = context(tmp_path / "s09", example_answers(run_time="04:30", nightly=True), file=True)
    setup(ctx, only=("s01",))
    unopenable(schedule_file(ctx.paths))
    said_setup = setup(ctx)
    assert windows.has is None and not any(code == "NS-W208" for code, _ in ctx.warnings)
    line = summary(said_setup)
    assert line.startswith("Automatic runs: off.") and "so the change was undone" in line and "Settings" in line


def test_when_the_undo_fails_too_every_route_says_windows_runs_it_with_ns_w208(tmp_home: Path, tmp_path: Path, windows: FakeWindows):
    w208 = describe("NS-W208")
    windows.refuse_unregister = True  # Windows keeps the task it just took
    # the Settings switch
    client, _state, paths = dashboard(tmp_home)
    unopenable(schedule_file(paths))
    body = put(client, automatic_runs=True, schedule_time="04:30")
    said = result(body)
    assert windows.has == ("Ready", "04:30")
    assert said.get("applied") is False and said.get("code") == "NS-W208"
    assert w208.message in said["note"] and "04:30" in said["note"] and "Task Scheduler" in said["note"]
    assert body["schedule_registered"] is False  # the page still cannot say on: the note says why that is untrue
    notes = [n["message"] for n in notify.read_notes(paths) if n["message"].startswith("NS-W208")]
    assert len(notes) == 1 and w208.message in notes[0] and "04:30" in notes[0]
    # `ns schedule install`: the same words, and no second morning note
    res = CliRunner().invoke(cli.app, ["schedule", "install", "--time", "04:30", "--enable"])
    assert res.exit_code == 1 and "NS-W208" in res.output and w208.message in res.output
    assert len([n for n in notify.read_notes(paths) if n["message"].startswith("NS-W208")]) == 1
    # the way out the hint names: config can be written again, and the pages follow Windows
    schedule_file(paths).rmdir()
    assert schedule.sync_record(paths) is True and read_schedule_mode(paths) == "registered"
    # setup (s09), on another home
    windows.task = None
    ctx = context(tmp_path / "s09", example_answers(run_time="04:30", nightly=True), file=True)
    setup(ctx, only=("s01",))
    unopenable(schedule_file(ctx.paths))
    said_setup = setup(ctx)
    assert windows.has == ("Ready", "04:30")
    assert any(code == "NS-W208" for code, _ in ctx.warnings)
    line = summary(said_setup)
    assert "NS-W208" in line and "Windows runs the nightly task at 04:30" in line and "Task Scheduler" in line


# ----------------------------------------------------------------- data written by the previous release
def test_a_0_1_14_uninstall_left_record_is_settled_quietly_before_s08(tmp_path: Path, windows: FakeWindows, monkeypatch: pytest.MonkeyPatch):
    ctx = context(tmp_path, example_answers(run_time="03:00"), file=True)
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(ctx.paths.home))
    write_run_time(ctx.paths, "03:00", "registered")  # 0.1.14's uninstall removed the task and kept this file

    setup(ctx, only=("s01",))

    assert record(ctx.paths) is None and saved_time(ctx.paths) == "03:00"  # gone before anything reads it
    assert w207_notes(ctx.paths) == []  # nobody "switched it off outside Nightshift"
    assert verify_quick(monkeypatch, passed=True) == 0 and w207_notes(ctx.paths) == []  # s08's own look
    said = setup(ctx, only=("s09", "s10"))
    assert windows.has is None and summary(said).startswith("Automatic runs: off")  # asked again: no answer, off
    # the same file over an enabled task is an install that is on: kept, not a first install
    other = context(tmp_path / "on", example_answers(run_time="03:00"), file=True)
    write_run_time(other.paths, "03:00", "registered")
    windows.task = {"state": "Ready", "at": "03:00", "logon": "S4U"}
    assert summary(setup(other)) == "Automatic runs: on at 03:00, kept as set in Settings."
    # Windows does not answer: nothing is changed and the summary says it was not checked
    windows.calls.clear()
    windows.hang = True
    blind = context(tmp_path / "on", example_answers(run_time="03:00"), file=True, app=moved_app(tmp_path, "app2"))
    line = summary(setup(blind))
    windows.hang = False
    assert line.startswith("Automatic runs: not checked (Windows did not answer:") and "Settings shows them" in line
    assert windows.has == ("Ready", "03:00") and set(windows.calls) == {"hang"}  # asked, nothing changed


def test_answers_with_nightly_load_in_0_1_14_after_for_version():
    data = answers_view.draft_to_answers(
        {"channel": {"displayName": "After Dark Tales", "niche": "horror fiction"}, "homePath": "D:/Nightshift",
         "llmTier": "small", "voice": {"speaker": "Ryan"}, "runTime": "03:00", "nightly": True, "nightlySeen": "on@03:00"}
    )
    written = new_fields.plain_disk_dict("answers.json", dict(data))
    assert (written.get("nightly"), written.get("nightly_seen")) == (True, "on@03:00")  # in use: written
    unused = new_fields.plain_disk_dict("answers.json", {**data, "nightly": None, "nightly_seen": None})
    assert "nightly" not in unused and "nightly_seen" not in unused  # at their default: left out
    dropped = config_compat.for_version("answers.json", written, "0.1.14")
    assert {"nightly", "nightly_seen"} <= set(dropped) and "nightly" not in written
    old.InstallerAnswers.model_validate(written)  # 0.1.14's strict model reads what is left
    assert config_compat.for_version("answers.json", {"nightly": True, "nightly_seen": "off"}, "0.1.15") == []


# --------------------------------------------------------------------------- the routes around setup
def test_the_window_shows_the_switch_and_what_windows_has_now(tmp_path: Path, windows: FakeWindows):
    from installer.stage2.web import nightly_view

    home = tmp_path / "home"
    assert nightly_view.current_choice(home) == "new"  # nothing installed there: no "Now" line
    fresh = answers_view.answers_to_view({}, nightly_now="new")
    assert (fresh["nightly"], fresh["nightlySeen"]) == (False, "new")  # off by default
    # a prefilled answers file may ask for it on a new home only
    asked = {"nightly": True}
    assert answers_view.answers_to_view(asked, nightly_now="new")["nightly"] is True
    assert answers_view.answers_to_view(asked, nightly_now="off")["nightly"] is False
    ctx = context(tmp_path, example_answers())
    setup(ctx)
    assert nightly_view.current_choice(home) == "off"
    settings(ctx, on=True, run_time="04:30")
    assert nightly_view.current_choice(home) == "on@04:30"
    unopenable(tmp_path / "torn" / "config" / "schedule.json")
    assert nightly_view.current_choice(tmp_path / "torn") == "off"  # an unreadable record never reads on
    # a draft from an older page (no switch) changes nothing on an install and asks for nothing on a new one
    plain = window_answers(ctx, {k: v for k, v in shown(saved_answers(ctx.paths), home).items() if not k.startswith("nightly")})
    assert plain["nightly"] is None and plain["nightly_seen"] is None
    page = (ROOT / "installer" / "ui" / "src" / "screens" / "Choices.tsx").read_text(encoding="utf-8")
    copy = (ROOT / "installer" / "ui" / "src" / "copy.ts").read_text(encoding="utf-8")
    assert "Run Nightshift by itself every night" in copy and "You can turn this on later in Settings" in copy
    assert "<Switch" in page and 'set("nightly", v)' in page and "nightlySeen: a.nightlySeen" in page


def test_uninstall_removes_the_record_with_the_task():
    """Keep-my-data leaves `config`: the record of the task must not outlive the task the uninstall removed."""
    script = (ROOT / "installer" / "uninstall.ps1").read_text(encoding="utf-8")
    block = script[script.index("Unregister-ScheduledTask"):script.index("AppUserModelId")]
    assert "schedule.json" in block and "Remove-Item" in block
