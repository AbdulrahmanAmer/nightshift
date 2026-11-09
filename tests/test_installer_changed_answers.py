"""C1: a changed answers file next to INSTALL.cmd is not ignored (defect D-14).

``find_answers`` looks at the explicit path, then next to INSTALL.cmd, then in ``config``. s01 used to call
itself done whenever a valid ``config/answers.json`` from an earlier run existed, so after an uninstall that
keeps data a new answers.json (another voice, tier, run time) was ignored with no message. s01 now keeps a
note of the file the saved answers came from (``config/answers.source.json``) and runs again when that file
changed. What the answers change further down is checked where it is used: s09 makes the nightly task for the
run time the answers ask for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from installer.stage2.simulate import FakeRunner
from installer.stage2.state import load_flags, save_flags
from installer.stage2.steps.s01_questions import S01Questions
from installer.stage2.steps.s09_shortcuts_scheduler import S09ShortcutsScheduler
from installer.stage2.testing import FakeUi, example_answers, make_test_context
from installer.stage2.wizard import main
from tests.test_install_done_means_done import uninstall_like_0_1_12
from tests.test_wizard_e2e import args, started_steps, write_answers
from tests.test_wizard_steps_late import launcher_app


@pytest.fixture(autouse=True)
def _no_real_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NIGHTSHIFT_HOME", raising=False)


def provided(tmp_path: Path, **over):
    return example_answers(home_path=str(tmp_path / "home"), **over)


def ctx_from_file(tmp_path: Path, raw: dict, name: str = "answers.json"):
    """A context whose answers came from a file next to INSTALL.cmd, as ``build_context`` makes it."""
    src = tmp_path / "zip" / name
    src.parent.mkdir(exist_ok=True)
    src.write_text(json.dumps(raw), encoding="utf-8")
    ctx = make_test_context(tmp_path, raw_answers=raw, ui=FakeUi(interactive=False))
    ctx.answers_path = src
    return ctx, src


def test_s01_runs_again_with_a_changed_answers_file_next_to_install_cmd(tmp_path: Path):
    raw = provided(tmp_path)
    ctx, src = ctx_from_file(tmp_path, raw)
    step = S01Questions()
    assert step.check(ctx) is False
    step.run(ctx)
    assert (ctx.paths.config / "answers.source.json").is_file()
    same, _ = ctx_from_file(tmp_path, raw)
    assert step.check(same) is True and same.answers is not None  # nothing changed: nothing to ask again

    designed = provided(tmp_path, voice={"mode": "design", "description": "warm, slow", "name": "QaNarrator"})
    changed, _ = ctx_from_file(tmp_path, designed)
    assert step.check(changed) is False  # the file wins over the saved copy
    step.run(changed)
    assert "differ from the ones your earlier setup saved" in changed.ui.text
    saved = json.loads((changed.paths.config / "answers.json").read_text(encoding="utf-8"))
    assert saved["voice"]["mode"] == "design" and saved["voice"]["name"] == "QaNarrator"
    final, _ = ctx_from_file(tmp_path, designed)
    assert step.check(final) is True
    assert final.answers is not None and final.answers.voice.name == "QaNarrator"


def test_s01_answers_saved_without_a_record_of_their_source_do_not_beat_a_file_next_to_install_cmd(
    tmp_path: Path,
):
    """A saved copy from before the record existed, or typed into the questions: the file's choices win."""
    ctx, _ = ctx_from_file(tmp_path, provided(tmp_path))
    S01Questions().run(ctx)
    (ctx.paths.config / "answers.source.json").unlink()
    again, _ = ctx_from_file(tmp_path, provided(tmp_path))
    assert S01Questions().check(again) is False


def test_s01_ignores_the_api_keys_in_the_file_and_the_saved_copy_itself(tmp_path: Path):
    ctx, src = ctx_from_file(tmp_path, provided(tmp_path, pexels_api_key="typed-key"))
    S01Questions().run(ctx)
    blanked = provided(tmp_path, pexels_api_key="")  # s07 blanks the keys in the file
    src.write_text(json.dumps(blanked), encoding="utf-8")
    again = make_test_context(tmp_path, raw_answers=blanked, ui=FakeUi(interactive=False))
    again.answers_path = src
    assert S01Questions().check(again) is True
    inside = make_test_context(tmp_path, ui=FakeUi(interactive=False))
    inside.answers_path = inside.paths.config / "answers.json"  # find_answers found the saved copy
    assert S01Questions().check(inside) is True


def test_a_changed_answers_file_wins_after_an_uninstall_that_keeps_the_data(tmp_path: Path):
    answers = write_answers(tmp_path)
    home = tmp_path / "home"
    assert main(args(tmp_path, answers)) == 0
    first = json.loads((home / "config" / "answers.json").read_text(encoding="utf-8"))
    assert first["voice"]["mode"] == "preset"
    uninstall_like_0_1_12(home)

    data = json.loads(answers.read_text(encoding="utf-8"))
    data["voice"] = {"mode": "design", "description": "a calm night-shift narrator", "name": "QaNarrator"}
    data["run_time"] = "04:15"
    answers.write_text(json.dumps(data), encoding="utf-8")
    before = len(started_steps(home))
    assert main(args(tmp_path, answers)) == 0
    assert "s01_questions" in started_steps(home)[before:]
    saved = json.loads((home / "config" / "answers.json").read_text(encoding="utf-8"))
    assert saved["voice"]["mode"] == "design" and saved["voice"]["name"] == "QaNarrator"
    assert saved["run_time"] == "04:15"


# ---------------------------------------------------------------------------------------------- the run time
def test_s09_runs_again_when_the_answers_ask_for_another_run_time(tmp_path: Path):
    app = launcher_app(tmp_path, ["nightshift-dashboard.ps1"])
    runner = FakeRunner(tmp_path / "desktop")
    ctx = make_test_context(
        tmp_path, raw_answers=example_answers(run_time="03:00"), resolve=True, app_path=app, runner=runner
    )
    step = S09ShortcutsScheduler()
    step.run(ctx)
    assert step.check(ctx) is True and ctx.flag("schedule_time") == "03:00"
    other = make_test_context(
        tmp_path, raw_answers=example_answers(run_time="04:15"), resolve=True, app_path=app, runner=runner
    )
    assert step.check(other) is False  # the task was made for 03:00
    step.run(other)
    assert other.flag("schedule_time") == "04:15" and step.check(other) is True
    assert runner.called("ns schedule install")[-1].argv[-2:] == ["--time", "04:15"]


def test_s09_state_from_before_the_run_time_was_recorded_stays_done(tmp_path: Path):
    app = launcher_app(tmp_path, ["nightshift-dashboard.ps1"])
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, app_path=app)
    step = S09ShortcutsScheduler()
    step.run(ctx)
    flags = load_flags(ctx.paths)
    del flags["schedule_time"]
    save_flags(ctx.paths, flags)
    assert step.check(ctx) is True
