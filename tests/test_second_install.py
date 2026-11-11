# ruff: noqa: E501, F811
"""second-install-takeover: a second Nightshift on a PC takes nothing of the first without a yes.

Two installs on one PC are two folders under `tmp_path` that share one temporary Desktop (the wizard's own
`FakeRunner` names it), one fake Task Scheduler (`FakeWindows`, part a's fake) and one simulated user
environment. Entry: the wizard's step objects (s01, s09, s10), the window's `answers_view` functions, the
bootstrap step `B02Home`, `ns schedule install` through `CliRunner`, and the real `installer/uninstall.ps1`
run with `-DryRun -DesktopDir <tmp> -StateFile <tmp>` (nothing of this PC is read or changed). The shortcut
files are real shell links by their bytes (`write_lnk`), which `shortcut_owner.read_lnk` and the uninstaller
both parse.
"""

from __future__ import annotations

import json
import os
import string
import subprocess
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from installer.stage2 import reinstall
from installer.stage2 import shortcut_owner as so
from installer.stage2.answers import InstallerAnswers
from installer.stage2.runner import RunResult
from installer.stage2.steps.s09_shortcuts_scheduler import SHORTCUTS, S09ShortcutsScheduler
from installer.stage2.testing import FakeUi, example_answers, make_test_context
from installer.stage2.web import answers_view, bootstrap
from installer.stage2.web import uninstall as web_uninstall
from ns_common import config_compat, new_fields
from ns_common.errors import NsError, describe
from ns_common.paths import Paths
from ns_supervisor import cli, pwsh, schedule
from tests import frozen_config_models_0_1_14 as old
from tests.test_installer_uv_pin import Transport, VersionCmd, make_boot, pin_for
from tests.test_launcher_reach import direct, through_stub, write_lnk
from tests.test_nightly_optional import (
    FakeWindows,
    windows,  # noqa: F401  (the fake Task Scheduler)
)
from tests.test_ns_schedule import needs_powershell
from tests.test_setup_nightly_question import SetupRunner, saved_answers, setup

try:
    from installer.stage2.web import shortcuts_view
except ImportError:  # the base has no such module: the assertions below say what is missing
    shortcuts_view = None  # type: ignore[assignment]

ROOT = Path(__file__).resolve().parents[1]
UNINSTALL = ROOT / "installer" / "uninstall.ps1"
SIX = [(name, script) for name, script, code_only in SHORTCUTS if not code_only]
HOME_VAR = "NIGHTSHIFT_HOME"


# ----------------------------------------------------------------------------------------- two installs
def install_at(folder: Path) -> Path:
    """An existing Nightshift install: a folder with `app/VERSION`, its launchers and its settings."""
    app = folder / "app"
    (app / "launchers").mkdir(parents=True)
    for _, script, _ in SHORTCUTS:
        (app / "launchers" / script).write_text("exit 0\r\n", encoding="ascii")
    (app / "VERSION").write_text("0.1.15\n", encoding="ascii")
    (folder / "config").mkdir()
    (folder / "config" / "runtime.json").write_text("{}", encoding="utf-8")
    return folder


def put_shortcuts(desktop: Path, home: Path | str, form: Any = through_stub) -> dict[str, bytes]:
    """The six Desktop shortcuts of the install at `home`; their bytes."""
    desktop.mkdir(parents=True, exist_ok=True)
    for name, script in SIX:
        write_lnk(desktop / f"{name}.lnk", form(Path(home), script), str(home), "", f"Nightshift: {name}")
    return links(desktop)


def links(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(folder.glob("*.lnk"))}


def opens(lnk: Path) -> str | None:
    """The install folder a shortcut starts; None when the file is not a shortcut Nightshift can read."""
    try:
        arguments = so.read_lnk(lnk).arguments
    except (OSError, ValueError):
        return None
    found = so.parse(arguments)
    if found is None:
        return None
    return os.path.dirname(found[1]) if found[1] else so.opens_home(arguments)


def desktop_opens(desktop: Path) -> set[str | None]:
    return {opens(desktop / f"{name}.lnk") for name, _ in SIX}


def second(
    tmp_path: Path, raw: dict[str, Any] | None = None, *, ui: FakeUi | None = None, dev: bool = False,
    name: str = "b", resolve: bool = False,
) -> Any:
    """One setup process of install B: its home is `tmp_path/<name>/home`, the Desktop is the PC's one
    (`tmp_path/desktop`), and its answers come from a file next to INSTALL.cmd."""
    answers = example_answers(install_code=False) if raw is None else raw
    ctx = make_test_context(
        tmp_path / name, raw_answers=answers, ui=ui or FakeUi(interactive=False),
        runner=SetupRunner(tmp_path / "desktop"), dev_mode=dev, resolve=resolve,
    )
    src = tmp_path / name / "answers.json"
    src.write_text(json.dumps(answers), encoding="utf-8")
    ctx.answers_path = src
    return ctx


def said(text: str, start: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.startswith(start)]
    return lines[-1] if lines else ""


def two(tmp_path: Path) -> tuple[Path, Path, dict[str, bytes]]:
    """Install A with its six shortcuts on the Desktop: (A's folder, the Desktop, the shortcuts' bytes)."""
    a = install_at(tmp_path / "A")
    desktop = tmp_path / "desktop"
    return a, desktop, put_shortcuts(desktop, a)


def done_line(ctx: Any, desktop: Path) -> str:
    """What the window's Done screen says about the Desktop shortcuts (`controller._build_result`)."""
    assert shortcuts_view is not None, "the base has no sentence about whose the Desktop shortcuts are"
    flags = json.loads((ctx.paths.config / "wizard_flags.json").read_text(encoding="utf-8"))
    return shortcuts_view.summary(ctx.paths.home, ctx.app_path, desktop, flags)["line"]


def missing_drive() -> str:
    free = [f"{letter}:" for letter in reversed(string.ascii_uppercase) if not os.path.exists(f"{letter}:\\")]
    if not free:
        pytest.skip("every drive letter is in use on this PC")
    return free[0]


# ----------------------------------------------------------------------------------- setup: the shortcuts
def test_a_second_install_from_an_answers_file_keeps_the_first_installs_shortcuts_and_says_so(tmp_path: Path, windows: FakeWindows):
    a, desktop, before = two(tmp_path)
    ctx = second(tmp_path)
    text = setup(ctx)
    assert links(desktop) == before  # byte for byte: nothing on the Desktop was written
    own = ctx.paths.home / "Shortcuts"
    assert {p.stem for p in own.glob("*.lnk")} == {name for name, _ in SIX}  # this install's full set
    assert all(so.is_stub_form(so.read_lnk(p).arguments, ctx.paths.home) for p in own.glob("*.lnk"))
    line = said(text, "Desktop shortcuts:")
    assert f"kept for the Nightshift at {a} (NS-W119)" in line and str(own) in line
    assert "shortcuts_here" in line and "Make the Desktop shortcuts and the ns and studio commands open this install" in line  # the way out
    assert text.count(line) == 2  # the scheduler step and the summary: once each
    assert ("NS-W119", str(a)) in ctx.warnings and describe("NS-W119").hint
    assert done_line(ctx, desktop) == line
    assert ctx.flag("shortcuts_owner") == f"other:{a}"
    assert shortcuts_view.NAMES == tuple(name for name, _, _ in SHORTCUTS)  # the window's list is the step's


def test_a_shortcut_this_install_wants_and_the_first_has_not_is_not_mixed_in(tmp_path: Path, windows: FakeWindows):
    a, desktop, before = two(tmp_path)
    ctx = second(tmp_path, example_answers(install_code=True))  # B also wants "Nightshift Code"
    setup(ctx)
    assert links(desktop) == before and not (desktop / "Nightshift Code.lnk").exists()
    assert (ctx.paths.home / "Shortcuts" / "Nightshift Code.lnk").is_file()
    assert S09ShortcutsScheduler().check(ctx) is True  # and that is a finished state, not work to do


@pytest.mark.parametrize("route", ["file", "console", "window"])
def test_a_yes_moves_the_shortcuts_and_says_from_where(tmp_path: Path, windows: FakeWindows, route: str):
    a, desktop, _ = two(tmp_path)
    raw = example_answers(install_code=False)
    ui = FakeUi(interactive=False)
    if route == "file":
        assert "shortcuts_here" in InstallerAnswers.model_fields, "the answers have no such key on the base"
        raw["shortcuts_here"] = True
    elif route == "console":
        ui = FakeUi({"Make them open this install instead": "y"}, interactive=True)
    else:
        assert shortcuts_view is not None, "the base window has no switch for the Desktop shortcuts"
        home = tmp_path / "b" / "home"
        now = answers_view.folder_now(home, ROOT, desktop)
        view = answers_view.answers_to_view(raw, nightly_now=now["nightlySeen"], shortcuts_now=now["shortcutsNow"])
        assert view["shortcutsHere"] is False and view["shortcutsNow"]["state"] == "other"  # shown, off
        assert view["shortcutsNow"]["path"] == str(a)
        assert view["shortcutsNow"]["line"] == f"Now they open the Nightshift at {a}"
        draft = {**{k: v for k, v in view.items() if not k.endswith("KeySet")}, "homePath": str(home), "shortcutsHere": True}
        raw = {**answers_view.draft_to_answers(draft), "install_code": False}
        assert raw["shortcuts_here"] is True
    ctx = second(tmp_path, raw, ui=ui)
    text = setup(ctx)
    assert desktop_opens(desktop) == {str(ctx.paths.home)}
    line = said(text, "Desktop shortcuts:")
    assert line == f"Desktop shortcuts: they opened the Nightshift at {a}; now they open this install, as you chose."
    assert ("NS-W119", str(a)) not in ctx.warnings and ctx.flag("shortcuts_owner") == "mine"


def test_the_console_question_defaults_to_no(tmp_path: Path, windows: FakeWindows):
    a, desktop, before = two(tmp_path)
    ui = FakeUi(interactive=True)  # Enter at every question
    ctx = second(tmp_path, ui=ui)
    text = setup(ctx)
    question = f"The Desktop shortcuts open the Nightshift at {a}. Make them open this install instead?"
    assert question in ui.asked
    assert links(desktop) == before and "shortcuts_here" not in saved_answers(ctx.paths)
    assert f"kept for the Nightshift at {a} (NS-W119)" in said(text, "Desktop shortcuts:")
    # no other install on the Desktop: nothing to ask
    alone = FakeUi(interactive=True)
    setup(second(tmp_path / "alone", ui=alone))
    assert not [q for q in alone.asked if "Desktop shortcuts" in q]


def test_shortcuts_of_a_deleted_install_are_taken_without_a_question(tmp_path: Path, windows: FakeWindows):
    gone = tmp_path / "deleted"  # the drive is there, the folder is not
    desktop = tmp_path / "desktop"
    put_shortcuts(desktop, gone, form=direct)
    ui = FakeUi(interactive=True)
    ctx = second(tmp_path, ui=ui)
    text = setup(ctx)
    assert desktop_opens(desktop) == {str(ctx.paths.home)}
    assert not [q for q in ui.asked if "Desktop shortcuts" in q]
    assert said(text, "Desktop shortcuts:") == f"Desktop shortcuts: they open this install. They opened a deleted Nightshift ({gone}) before."
    assert not [w for w in ctx.warnings if w[0] == "NS-W119"]


def test_shortcuts_on_a_drive_that_is_not_there_are_kept_and_said(tmp_path: Path, windows: FakeWindows):
    away = f"{missing_drive()}\\Nightshift"
    desktop = tmp_path / "desktop"
    before = put_shortcuts(desktop, away)
    (desktop / "Nightshift Studio.lnk").write_bytes(b"not a shortcut")  # the one bad file among good ones
    before = links(desktop)
    ctx = second(tmp_path)
    text = setup(ctx)
    assert links(desktop) == before
    line = said(text, "Desktop shortcuts:")
    assert f"kept for the Nightshift at {away}, which cannot be reached now (its drive is not there) (NS-W119)" in line
    assert "Desktop shortcut 'Nightshift Studio' is not one Nightshift made; it was left as it is." in text
    assert (ctx.paths.home / "Shortcuts" / "Nightshift Studio.lnk").is_file()


def test_a_file_of_the_owners_under_a_shortcut_name_is_left_and_the_rest_is_this_installs(tmp_path: Path, windows: FakeWindows):
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    mine = desktop / "Nightshift Outbox.lnk"
    write_lnk(mine, "D:\\videos", "D:\\", "", "my own link to a folder")  # not a Nightshift launcher
    kept = mine.read_bytes()
    ctx = second(tmp_path)
    text = setup(ctx)
    assert mine.read_bytes() == kept
    assert "Desktop shortcut 'Nightshift Outbox' is not one Nightshift made; it was left as it is." in text
    assert opens(desktop / "Nightshift Dashboard.lnk") == str(ctx.paths.home)
    assert said(text, "Desktop shortcuts:").startswith("Desktop shortcuts: they open this install.")
    assert S09ShortcutsScheduler().check(ctx) is True


# -------------------------------------------------------------------------------- setup: the nightly task
def test_a_dev_install_never_touches_another_installs_task(tmp_path: Path, windows: FakeWindows):
    a, desktop, before = two(tmp_path)
    windows.task = {"state": "Ready", "at": "05:30", "logon": "S4U", "home": str(a)}
    task = dict(windows.task)
    ctx = second(tmp_path, dev=True, resolve=True)
    text = setup(ctx, only=("s09", "s10"))
    assert windows.changes() == [] and windows.task == task  # read, never registered
    assert ctx.flag("schedule") == "other"
    line = f"Dev mode: the nightly task belongs to the Nightshift at {a}; a dev install never takes it, so it was left as it is."
    assert text.count(line) == 2  # the scheduler step and the summary
    assert not schedule.schedule_file(ctx.paths).exists()  # nothing recorded for a task that is not this one's
    assert links(desktop) == before  # and a dev install keeps the other install's shortcuts like any other
    # a yes is about the shortcuts: the task still is A's, even a disabled one
    windows.task["state"] = "Disabled"
    again = second(tmp_path, {**example_answers(install_code=False), "shortcuts_here": True}, dev=True, resolve=True)
    setup(again, only=("s09",))
    assert windows.changes() == [] and windows.task["home"] == str(a) and again.flag("schedule") == "other"
    assert desktop_opens(desktop) == {str(again.paths.home)}
    # `ns schedule install` in a dev install says the same thing and exits 0
    (again.paths.config / "runtime.json").write_text(json.dumps({"dev_mode": True}), encoding="utf-8")
    res = CliRunner().invoke(cli.app, ["schedule", "install"], env={HOME_VAR: str(again.paths.home)})
    assert windows.changes() == [] and str(a) in res.output and "a dev install never takes" in res.output


@pytest.mark.parametrize("found", ["none", "gone", "mine", "unknown"])
def test_a_dev_install_over_a_gone_or_absent_task_registers_it_disabled_as_today(tmp_path: Path, windows: FakeWindows, found: str):
    ctx = second(tmp_path, dev=True, resolve=True)
    homes = {"gone": str(tmp_path / "deleted"), "mine": str(ctx.paths.home)}
    if found != "none":
        windows.task = {"state": "Ready", "at": "05:30", "logon": "S4U"}
        if found in homes:
            windows.task["home"] = homes[found]
    text = setup(ctx, only=("s09",))
    assert windows.has == ("Disabled", "03:00") and windows.task["home"] == str(ctx.paths.home)
    assert ctx.flag("schedule") == "installed" and ctx.flag("schedule_mode") == "disabled"
    assert "registered for 03:00 but DISABLED" in text


def test_a_dev_install_changes_nothing_when_the_task_cannot_be_read(tmp_path: Path, windows: FakeWindows):
    windows.hang = True  # Windows does not answer: whose task it is cannot be known
    ctx = second(tmp_path, dev=True, resolve=True)
    setup(ctx, only=("s09",))
    assert set(windows.calls) == {"hang"} and len(windows.calls) == 1  # asked once, nothing registered
    assert ctx.flag("schedule") == "failed" and [w for w in ctx.warnings if w[0] == "NS-W206"]


# ----------------------------------------------------------------------------- Repair, kills, stored state
def test_a_repair_of_the_second_install_asks_nothing_and_changes_nothing(tmp_path: Path, windows: FakeWindows):
    a, desktop, before = two(tmp_path)
    first = second(tmp_path)
    line = said(setup(first), "Desktop shortcuts:")
    ui = FakeUi({"Make them open this install instead": "y"}, interactive=True)
    repair = second(tmp_path, saved_answers(first.paths), ui=ui)
    assert S09ShortcutsScheduler().check(repair) is True  # A owns the Desktop, B's own set is whole
    calls = len(repair.runner.calls)
    text = setup(repair)
    assert links(desktop) == before and not [q for q in ui.asked if "Desktop shortcuts" in q]
    assert not [c for c in repair.runner.calls[calls:] if c.tag.startswith("ps:shortcut")]
    assert said(text, "Desktop shortcuts:") == line  # the summary still says it, once
    assert shortcuts_view is not None
    home = repair.paths.home
    now = answers_view.folder_now(home, ROOT, desktop)
    view = answers_view.answers_to_view(saved_answers(repair.paths), nightly_now=now["nightlySeen"], shortcuts_now=now["shortcutsNow"])
    assert view["shortcutsHere"] is False and view["shortcutsNow"]["line"] == f"Now they open the Nightshift at {a}"
    # this install's own folder lost a shortcut: that is work for a Repair, and still nothing on the Desktop
    (home / "Shortcuts" / "Nightshift Studio.lnk").unlink()
    assert S09ShortcutsScheduler().check(repair) is False
    setup(repair, only=("s09",))
    assert links(desktop) == before and (home / "Shortcuts" / "Nightshift Studio.lnk").is_file()


def test_once_the_first_install_is_gone_a_repair_of_the_second_takes_the_shortcuts(tmp_path: Path, windows: FakeWindows):
    import shutil

    a, desktop, _ = two(tmp_path)
    first = second(tmp_path)
    setup(first)
    shutil.rmtree(a)  # A was deleted by hand: its shortcuts open nothing
    repair = second(tmp_path, saved_answers(first.paths))
    assert S09ShortcutsScheduler().check(repair) is False
    text = setup(repair)
    assert desktop_opens(desktop) == {str(repair.paths.home)}
    assert said(text, "Desktop shortcuts:") == f"Desktop shortcuts: they open this install. They opened a deleted Nightshift ({a}) before."
    assert repair.flag("shortcuts_owner") == "mine" and S09ShortcutsScheduler().check(repair) is True


def test_when_another_install_took_the_desktop_since_the_next_setup_says_so(tmp_path: Path, windows: FakeWindows):
    desktop = tmp_path / "desktop"
    first = second(tmp_path)  # B alone: the Desktop is its own
    assert said(setup(first), "Desktop shortcuts:") == "Desktop shortcuts: they open this install."
    c = install_at(tmp_path / "C")
    taken = put_shortcuts(desktop, c)  # C's setup took them with its owner's yes
    assert shortcuts_view is not None
    line = done_line(first, desktop)  # B's stored sentence would be untrue now: the state as it is
    assert f"kept for the Nightshift at {c} (NS-W119)" in line
    repair = second(tmp_path, saved_answers(first.paths))
    assert S09ShortcutsScheduler().check(repair) is False
    text = setup(repair)
    assert links(desktop) == taken and said(text, "Desktop shortcuts:") == line


def test_shortcuts_here_is_consumed_after_s09_and_never_inherited(tmp_path: Path, windows: FakeWindows):
    assert "shortcuts_here" in InstallerAnswers.model_fields, "the answers have no such key on the base"
    a, desktop, _ = two(tmp_path)
    ctx = second(tmp_path, {**example_answers(install_code=False), "shortcuts_here": True})
    setup(ctx, only=("s01",))
    held = saved_answers(ctx.paths)
    assert held["shortcuts_here"] is True  # kept for s09, also across a restart
    assert S09ShortcutsScheduler().check(ctx) is False  # an answer nobody acted on yet
    setup(ctx, only=("s09",))
    assert "shortcuts_here" not in saved_answers(ctx.paths) and ctx.flag("shortcuts_taking") is None
    assert S09ShortcutsScheduler().check(ctx) is True
    assert "shortcuts_here" not in reinstall.INHERITED and "shortcuts_here" not in reinstall.SIMPLE_KEYS
    later = second(tmp_path, {"run_time": "03:00"})
    reinstall.inherit(later, {**held, "masters_per_week": 3})
    assert later.raw_answers.get("masters_per_week") == 3 and "shortcuts_here" not in later.raw_answers
    assert reinstall.changes(later, held, {"shortcuts_here": True}) == []  # not a "change" either
    # so A's next Repair, months later with the same old file, does not take them back
    back = make_test_context(tmp_path / "A-setup", raw_answers=saved_answers(ctx.paths), runner=SetupRunner(desktop), ui=FakeUi(interactive=False), resolve=True)
    setup(back, only=("s09",))
    assert desktop_opens(desktop) == {str(ctx.paths.home)}


def test_answers_with_shortcuts_here_load_in_0_1_14_after_for_version():
    draft = {"channel": {"displayName": "After Dark Tales", "niche": "horror fiction"}, "homePath": "D:/Nightshift",
             "llmTier": "small", "voice": {"speaker": "Ryan"}, "runTime": "03:00", "shortcutsHere": True}
    data = answers_view.draft_to_answers(draft)
    written = new_fields.plain_disk_dict("answers.json", dict(data))
    assert written.get("shortcuts_here") is True  # in use: written
    off = answers_view.draft_to_answers({**draft, "shortcutsHere": False})
    assert "shortcuts_here" not in new_fields.plain_disk_dict("answers.json", dict(off))  # no is not written
    dropped = config_compat.for_version("answers.json", written, "0.1.14")
    assert "shortcuts_here" in set(dropped) and "shortcuts_here" not in written
    old.InstallerAnswers.model_validate(written)  # 0.1.14's strict model reads what is left
    assert config_compat.for_version("answers.json", {"shortcuts_here": True}, "0.1.15") == []


def test_a_kill_between_the_desktop_and_the_flags_repeats_the_same_choice(tmp_path: Path, windows: FakeWindows, monkeypatch: pytest.MonkeyPatch):
    assert "shortcuts_here" in InstallerAnswers.model_fields, "the answers have no such key on the base"
    a, desktop, _ = two(tmp_path)
    raw = {**example_answers(install_code=False), "shortcuts_here": True}
    ctx = second(tmp_path, raw)
    setup(ctx, only=("s01",))
    real = ctx.set_flag

    def killed(name: str, value: Any) -> None:
        if name == "shortcuts_owner":
            raise KeyboardInterrupt  # the power goes after the Desktop was written, before the flags
        real(name, value)

    monkeypatch.setattr(ctx, "set_flag", killed)
    with pytest.raises(KeyboardInterrupt):
        setup(ctx, only=("s09",))
    assert desktop_opens(desktop) == {str(ctx.paths.home)}
    again = second(tmp_path, saved_answers(ctx.paths))  # a new process over the same home
    assert saved_answers(again.paths).get("shortcuts_here") is True  # the answer is still there
    assert S09ShortcutsScheduler().check(again) is False
    text = setup(again, only=("s09", "s10"))
    line = f"Desktop shortcuts: they opened the Nightshift at {a}; now they open this install, as you chose."
    assert said(text, "Desktop shortcuts:") == line and desktop_opens(desktop) == {str(again.paths.home)}
    assert "shortcuts_here" not in saved_answers(again.paths) and again.flag("shortcuts_taking") is None
    # killed after the flags and before the answer went: the rerun has nothing to do on the Desktop
    third = second(tmp_path, {**saved_answers(again.paths), "shortcuts_here": True})
    before = links(desktop)
    setup(third)
    assert {k: opens(desktop / k) for k in before} == {k: str(third.paths.home) for k in before}
    assert said(third.ui.text, "Desktop shortcuts:") == "Desktop shortcuts: they open this install."


# ----------------------------------------------------------------------------------------- the window
def test_the_window_rereads_both_lines_when_another_folder_is_picked(tmp_path: Path, windows: FakeWindows):
    a, desktop, _ = two(tmp_path)
    schedule.write_run_time(Paths(a), "04:30", "registered")  # Automatic runs are on in A
    (a / "config" / "wizard_flags.json").write_text("{}", encoding="utf-8")
    b = tmp_path / "b" / "home"
    assert shortcuts_view is not None, "the base window reads neither line for a folder picked later"
    raw = example_answers()
    at_a = answers_view.folder_now(a, a / "app", desktop)
    view_a = answers_view.answers_to_view(raw, nightly_now=at_a["nightlySeen"], shortcuts_now=at_a["shortcutsNow"])
    assert view_a["nightlySeen"] == "on@04:30" and view_a["nightly"] is True
    assert view_a["shortcutsNow"] == {"state": "mine", "path": None, "line": None}  # nothing to ask in A
    at_b = answers_view.folder_now(b, ROOT, desktop)  # the owner picks another folder
    view_b = answers_view.answers_to_view(raw, nightly_now=at_b["nightlySeen"], shortcuts_now=at_b["shortcutsNow"])
    assert view_b["nightlySeen"] == "new" and view_b["nightly"] is False  # B's, not the load-time home's
    assert view_b["shortcutsNow"] == {"state": "other", "path": str(a), "line": f"Now they open the Nightshift at {a}"}
    assert view_b["shortcutsHere"] is False
    # the folder check the page runs after every change of the folder carries both, and the page applies them
    page = (ROOT / "installer" / "ui" / "src" / "screens" / "Choices.tsx").read_text(encoding="utf-8")
    types = (ROOT / "installer" / "ui" / "src" / "api" / "types.ts").read_text(encoding="utf-8")
    assert "nightlySeen: r.nightlySeen" in page and "setShortcutsNow(r.shortcutsNow" in page
    assert 'set("shortcutsHere", v)' in page and "shortcutsHere: boolean" in types
    built = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "installer" / "ui" / "static").rglob("*.js"))
    assert "Make the Desktop shortcuts and the ns and studio commands open this install" in built


# ------------------------------------------------------------------------------------ NIGHTSHIFT_HOME
def test_a_second_install_leaves_the_user_variable_with_the_first_until_a_yes(tmp_path: Path, windows: FakeWindows):
    a, desktop, _ = two(tmp_path)
    ctx = second(tmp_path)
    ctx.runner.user_env[HOME_VAR] = str(a)
    ctx.runner._save_user_env()  # the registry outlives a setup process: the next one reads it again
    text = setup(ctx)
    assert ctx.runner.user_env[HOME_VAR] == str(a)
    line = said(text, "NIGHTSHIFT_HOME:")
    # (since FN-3 the variable no longer decides what a plain 'ns' runs: the line says what does not use it)
    assert line.startswith(f"NIGHTSHIFT_HOME: left naming the Nightshift at {a}") and "commands name their own folder" in line
    assert text.count(line) == 2 and ctx.flag("home_var") == f"other:{a}"
    assert S09ShortcutsScheduler().check(ctx) is True
    yes = second(tmp_path, {**saved_answers(ctx.paths), "shortcuts_here": True})
    text = setup(yes)
    assert yes.runner.user_env[HOME_VAR] == str(yes.paths.home)
    took = said(text, "NIGHTSHIFT_HOME:")
    assert took.startswith(f"NIGHTSHIFT_HOME: it named the Nightshift at {a}; now it names this install, as you chose.")
    assert "new terminal" in took  # the way to use it: programs already open keep the old value
    # a variable that names a deleted folder, or nothing, is this install's without a question
    for stale in (str(tmp_path / "deleted"), ""):
        alone = second(tmp_path / f"alone{len(stale)}")
        if stale:
            alone.runner.user_env[HOME_VAR] = stale
        text = setup(alone)
        assert alone.runner.user_env[HOME_VAR] == str(alone.paths.home) and said(text, "NIGHTSHIFT_HOME:") == ""


def test_what_setup_cannot_read_it_does_not_take(tmp_path: Path, windows: FakeWindows):
    a, desktop, before = two(tmp_path)
    ctx = second(tmp_path)
    ctx.runner.user_env[HOME_VAR] = str(a)
    ctx.runner._save_user_env()
    ctx.runner.results["ps:get-env"] = RunResult(1, "", "Access is denied")  # Windows does not say what it holds
    setup(ctx)
    assert not ctx.runner.called("ps:set-env") and ctx.runner.user_env[HOME_VAR] == str(a)  # not set on a guess
    assert ctx.flag("home_var") == "unread" and S09ShortcutsScheduler().check(ctx) is True
    # the Desktop cannot be found: the step stops with NS-E110, as before, and nothing on it was written
    lost = second(tmp_path, name="lost", resolve=True)
    lost.runner.results["ps:desktop"] = RunResult(0, "")
    with pytest.raises(NsError) as stop:
        S09ShortcutsScheduler().run(lost)
    assert stop.value.code == "NS-E110" and links(desktop) == before


def test_the_setup_window_leaves_the_user_variable_with_another_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(HOME_VAR, "placeholder")  # b02 sets it in this process too: undone at teardown
    a = install_at(tmp_path / "A")
    cmd, net = VersionCmd(), Transport()
    boot = make_boot(tmp_path, tmp_path / "unused" / "uv.exe", cmd, net, pin_for(tmp_path))
    boot.set_home_var(HOME_VAR, str(a))
    step = bootstrap.B02Home()
    step.run(boot)
    assert boot.get_home_var(HOME_VAR) == str(a)  # not taken
    step.verify(boot)  # and that is a finished step, not NS-E121
    (boot.home / "config" / "answers.json").write_text("{}", encoding="utf-8")
    assert step.check(boot) is True
    boot.set_home_var(HOME_VAR, str(tmp_path / "deleted"))  # a variable left by a deleted install
    assert step.check(boot) is False
    step.run(boot)
    assert boot.get_home_var(HOME_VAR) == str(boot.home)


@needs_powershell
def test_a_shortcut_runs_its_own_install_whatever_the_user_variable_names(tmp_path: Path):
    a = install_at(tmp_path / "A")
    b = install_at(tmp_path / "B")
    (b / "nightshift-launch.ps1").write_bytes((ROOT / "installer" / "launch.ps1").read_bytes())
    probe = b / "app" / "launchers" / "open-outbox.ps1"
    probe.write_text("[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'seen.txt'), [string]$env:NIGHTSHIFT_HOME)\r\nexit 0\r\n", encoding="ascii")
    exe = pwsh.find_powershell()
    assert exe is not None
    argv = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(b / "nightshift-launch.ps1"), "-Launcher", str(probe), "-DesktopDir", str(tmp_path / "desktop")]
    env = {**os.environ, HOME_VAR: str(a)}  # the PC's variable names the other install
    done = subprocess.run(argv, input="\n", capture_output=True, text=True, timeout=120, env=env)
    assert done.returncode == 0, done.stdout + done.stderr
    assert (probe.parent / "seen.txt").read_text(encoding="utf-8") == str(b)


# ------------------------------------------------------------------------------ the toast registration
def test_setup_leaves_a_toast_registration_another_install_uses(tmp_path: Path, windows: FakeWindows):
    a, desktop, _ = two(tmp_path)
    icon = a / "app" / "nightshift.ico"
    ctx = second(tmp_path)
    ctx.runner.results["ps:aumid-register"] = RunResult(0, f"kept:{icon}\n")  # what the script answers
    setup(ctx, only=("s01", "s09"))
    call = ctx.runner.called("ps:aumid-register")[-1]
    assert call.meta.get("keep_other") == "yes" and ctx.flag("aumid") == "other"
    assert not ctx.runner.called("ps:aumid-shortcut")  # A's dashboard shortcut is not stamped by B
    from installer.stage2 import winenv

    script = winenv.register_aumid_script("Nightshift", "Nightshift", tmp_path / "b.ico", keep_other=True)
    assert "kept:" in script and "Test-Path -LiteralPath $old" in script
    yes = second(tmp_path, {**saved_answers(ctx.paths), "shortcuts_here": True})
    setup(yes, only=("s01", "s09"))
    assert yes.runner.called("ps:aumid-register")[-1].meta.get("keep_other") == "no"


# ------------------------------------------------------------------------------------- the uninstaller
def uninstall_dry(home: Path, desktop: Path, machine: dict[str, Any], *extra: str, home_arg: bool = True, script: Path = UNINSTALL) -> subprocess.CompletedProcess[str]:
    """`uninstall.ps1 -DryRun` against a described PC: the Desktop is `desktop`, and what Windows has (the
    task's action, the user variable, the toast key) comes from a file, so nothing of this PC is read."""
    exe = pwsh.find_powershell()
    assert exe is not None
    state = desktop.parent / "machine.json"
    state.write_text(json.dumps(machine), encoding="utf-8")
    argv = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-DryRun", "-DesktopDir", str(desktop), "-StateFile", str(state), *extra]
    if home_arg:
        argv += ["-HomeDir", str(home)]
    env = {k: v for k, v in os.environ.items() if k != HOME_VAR}
    env["NS_NONINTERACTIVE"] = "1"
    return subprocess.run(argv, capture_output=True, text=True, timeout=300, env=env)


@needs_powershell
def test_uninstalling_the_second_install_leaves_everything_of_the_first(tmp_path: Path):
    a, desktop, before = two(tmp_path)
    b = install_at(tmp_path / "B")
    (b / "Shortcuts").mkdir()
    write_lnk(b / "Shortcuts" / "Nightshift Dashboard.lnk", through_stub(b, "nightshift-dashboard.ps1"), str(b))
    write_lnk(desktop / "Nightshift Code.lnk", through_stub(b, "nightshift-code.ps1"), str(b))  # one that opens B
    write_lnk(desktop / "Nightshift old.lnk", direct(tmp_path / "deleted", "update.ps1"), str(tmp_path))  # a deleted install's
    (desktop / "Nightshift notes.lnk").write_bytes(b"not a shortcut")  # cannot be read: the safe side
    machine = {"task": {"arguments": schedule.action_arguments(Paths(a))}, "userHome": str(a), "toastKey": True}
    proc = uninstall_dry(b, desktop, machine)
    out = proc.stdout
    assert proc.returncode == 0, out + proc.stderr
    assert "remove scheduled task" not in out and f"Left 'Nightshift Daily': it runs the Nightshift at {a}." in out
    for name in before:
        assert f"delete shortcut {desktop / name}" not in out
    assert f"Left 6 Desktop shortcuts: they open the Nightshift at {a}." in out
    assert f"[dry-run] delete shortcut {desktop / 'Nightshift Code.lnk'}" in out  # B's own goes
    assert f"[dry-run] delete shortcut {desktop / 'Nightshift old.lnk'}" in out   # and a deleted install's
    assert f"Left the Desktop shortcut {desktop / 'Nightshift notes.lnk'}: it could not be read" in out
    assert f"[dry-run] delete folder {b / 'Shortcuts'}" in out
    assert "remove toast registration" not in out and f"Left the notification registration: the Nightshift at {a} uses it." in out
    # B has no Python environment here, so Credential Manager is not looked into: left, and the line says so
    assert "remove from Credential Manager" not in out
    assert f"Left Credential Manager as it is (not looked into: this install has no Python environment to look with): any stock-footage keys in it are used by the Nightshift at {a}." in out
    assert "remove user environment variable" not in out and f"Left NIGHTSHIFT_HOME: it names the Nightshift at {a}." in out
    assert links(desktop).keys() >= before.keys() and (b / "Shortcuts").is_dir()  # a dry run changes nothing
    # a task that does not say which Nightshift it runs, and one on a drive that is not there: left, and said
    proc = uninstall_dry(b, desktop, {**machine, "task": {"arguments": "/c studio run --daily"}})
    assert "remove scheduled task" not in proc.stdout and "it does not say which Nightshift it runs; delete it in Task Scheduler if it is not needed" in proc.stdout
    away = f"{missing_drive()}\\Nightshift"
    proc = uninstall_dry(b, desktop, {**machine, "task": {"arguments": schedule.action_arguments(Paths(Path(away)))}})
    assert "remove scheduled task" not in proc.stdout and f"Left 'Nightshift Daily': it runs the Nightshift at {away}" in proc.stdout


@needs_powershell
def test_uninstalling_the_only_install_still_removes_all_of_it(tmp_path: Path):
    b = install_at(tmp_path / "B")
    desktop = tmp_path / "desktop"
    mine = put_shortcuts(desktop, b)
    write_lnk(desktop / "Nightshift Code.lnk", direct(b, "nightshift-code.ps1"), str(b))  # 0.1.14's form
    (b / "Shortcuts").mkdir()
    machine = {"task": {"arguments": schedule.action_arguments(Paths(b))}, "userHome": str(b), "toastKey": True, "credentials": ["Nightshift"]}
    proc = uninstall_dry(b, desktop, machine)
    out = proc.stdout
    assert proc.returncode == 0, out + proc.stderr
    assert "[dry-run] remove scheduled task 'Nightshift Daily'" in out
    for name in [*mine, "Nightshift Code.lnk"]:
        assert f"[dry-run] delete shortcut {desktop / name}" in out
    assert "[dry-run] remove toast registration" in out and "[dry-run] remove from Credential Manager with cmdkey: Nightshift (" in out
    assert "[dry-run] remove user environment variable NIGHTSHIFT_HOME" in out
    assert f"[dry-run] delete folder {b / 'Shortcuts'}" in out
    assert not [ln for ln in out.splitlines() if ln.startswith("Left ")]
    # nothing there at all (a second run): nothing to remove and nothing to say
    empty = tmp_path / "empty-desktop"
    empty.mkdir()
    proc = uninstall_dry(b, empty, {"task": None, "userHome": None, "toastKey": False})
    assert proc.returncode == 0 and "remove scheduled task" not in proc.stdout and "delete shortcut" not in proc.stdout
    assert "remove user environment variable" not in proc.stdout and not [ln for ln in proc.stdout.splitlines() if ln.startswith("Left ")]
    # a task whose Nightshift was deleted by hand goes too
    proc = uninstall_dry(b, empty, {"task": {"arguments": schedule.action_arguments(Paths(tmp_path / "deleted"))}, "userHome": None, "toastKey": False})
    assert "[dry-run] remove scheduled task 'Nightshift Daily'" in proc.stdout


@needs_powershell
def test_the_uninstaller_in_a_program_folder_removes_that_install_not_the_one_the_variable_names(tmp_path: Path):
    a = install_at(tmp_path / "A")
    b = install_at(tmp_path / "B")
    (b / "app" / "installer").mkdir()
    own = b / "app" / "installer" / "uninstall.ps1"
    own.write_bytes(UNINSTALL.read_bytes())
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    machine = {"task": None, "userHome": str(a), "toastKey": False}  # UNINSTALL.cmd passes no folder
    proc = uninstall_dry(b, desktop, machine, home_arg=False, script=own)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"Install folder: {b}" in proc.stdout and f"Install folder: {a}" not in proc.stdout
    assert f"Left NIGHTSHIFT_HOME: it names the Nightshift at {a}." in proc.stdout


@needs_powershell
def test_the_state_file_is_for_a_dry_run_only(tmp_path: Path):
    b = install_at(tmp_path / "B")
    state = tmp_path / "machine.json"
    state.write_text("{}", encoding="utf-8")
    exe = pwsh.find_powershell()
    assert exe is not None
    argv = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(UNINSTALL), "-SkipSystem", "-HomeDir", str(b), "-StateFile", str(state)]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=120, env={**os.environ, "NS_NONINTERACTIVE": "1"})
    assert proc.returncode == 2 and "-StateFile" in proc.stdout and (b / "app" / "VERSION").is_file()  # refused before anything


def powershell(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    exe = pwsh.find_powershell()
    assert exe is not None
    return subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, timeout=120, env=env)


def cut(text: str, start: str, end: str) -> str:
    return text[text.index(start):text.index(end)]


@needs_powershell
def test_the_uninstaller_outside_an_install_follows_the_variable_only_to_an_install_of_this_program(tmp_path: Path):
    """The repository's UNINSTALL.cmd (a dev install's way) beside a real install A that the variable names."""
    a = install_at(tmp_path / "A")
    (a / "config" / "wizard_flags.json").write_text(json.dumps({"shortcuts_app": str(a / "app")}), encoding="utf-8")
    dev = install_at(tmp_path / "dev")
    (dev / "config" / "wizard_flags.json").write_text(json.dumps({"shortcuts_app": str(ROOT)}), encoding="utf-8")
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    proc = uninstall_dry(a, desktop, {"task": None, "userHome": str(a), "toastKey": False}, home_arg=False)
    out = proc.stdout
    assert proc.returncode == 2, out + proc.stderr  # A is not offered: it does not run this program folder
    assert "nothing was changed" in out and f"Install folder: {a}" not in out and "[dry-run]" not in out
    assert f"UNINSTALL.cmd from its own program folder: {a / 'app'}" in out and "-HomeDir <its folder>" in out
    proc = uninstall_dry(dev, desktop, {"task": None, "userHome": str(dev), "toastKey": False}, home_arg=False)
    assert proc.returncode == 0 and f"Install folder: {dev}" in proc.stdout  # the dev install of this repository
    proc = uninstall_dry(a, desktop, {"task": None, "userHome": None, "toastKey": False}, home_arg=False)
    assert proc.returncode == 2 and "nothing was changed" in proc.stdout  # no variable: never a guess


@needs_powershell
def test_install_cmd_in_a_program_folder_repairs_that_install_not_the_one_the_variable_names(tmp_path: Path):
    b = install_at(tmp_path / "B")
    text = (ROOT / "installer" / "stage1.ps1").read_text(encoding="ascii")
    body = cut(text, "function Resolve-NsHome {", "function Get-KitHint")
    assert body.index("-ieq 'app'") < body.index("GetEnvironmentVariable('NIGHTSHIFT_HOME'")  # the place is asked first
    stubs = "\n".join([
        "$FakeProbe = 'described'",  # as in every test: the PC's own variable is not read
        "function Get-Prop { param($o, $n, $d) return $d }",
        "function Select-DefaultHome { return 'Z:\\Elsewhere' }",
        "function Get-DataDir { return 'Z:\\Data' }",
    ])
    inside = powershell(f"{stubs}\n{body}\nWrite-Output (Resolve-NsHome -Drives @() -AnswersObj $null -Forced '' -AppFolder '{b / 'app'}')")
    assert inside.returncode == 0 and inside.stdout.strip() == str(b), inside.stdout + inside.stderr
    outside = powershell(f"{stubs}\n{body}\nWrite-Output (Resolve-NsHome -Drives @() -AnswersObj $null -Forced '' -AppFolder '{tmp_path}')")
    assert outside.stdout.strip() == "Z:\\Elsewhere"  # an extracted zip: as before
    forced = powershell(f"{stubs}\n{body}\nWrite-Output (Resolve-NsHome -Drives @() -AnswersObj $null -Forced 'Q:\\Asked' -AppFolder '{b / 'app'}')")
    assert forced.stdout.strip() == "Q:\\Asked"  # what the owner names still wins


@needs_powershell
def test_a_launcher_inside_an_install_runs_it_and_says_so_when_the_variable_names_another(tmp_path: Path):
    import shutil

    a = install_at(tmp_path / "A")
    b = install_at(tmp_path / "B")
    common = b / "app" / "launchers" / "_common.ps1"
    shutil.copy(ROOT / "launchers" / "_common.ps1", common)
    env = {k: v for k, v in os.environ.items() if k.lower() != "psmodulepath"}
    env[HOME_VAR] = str(a)
    done = powershell(f". '{common}'\nWrite-Output (Get-NsHome)\nWrite-Output (Get-NsHome)\nWrite-Output $env:NIGHTSHIFT_HOME", env)
    lines = [ln for ln in done.stdout.splitlines() if ln.strip()]
    assert done.returncode == 0, done.stdout + done.stderr
    said = f"Nightshift: using the install in {b} (this launcher is in it); NIGHTSHIFT_HOME names {a}, which is another folder."
    assert lines == [said, str(b), str(b), str(b)]  # said once; every child inherits this install's folder


def test_a_program_inside_an_install_uses_it_when_the_variable_is_only_the_pcs_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    import ns_common.paths as paths_mod

    assert hasattr(paths_mod, "home_of_place"), "the base knows its home from the variable only"
    a = install_at(tmp_path / "A")
    b = install_at(tmp_path / "B")
    assert paths_mod.home_of_place(b / "app", "C:/Python/python.exe") == b  # the folder above `app`
    venv_python = str(b / "venvs" / "studio" / "Scripts" / "python.exe")
    assert paths_mod.home_of_place(ROOT, venv_python) == b  # a dev install: the venv's home
    assert paths_mod.home_of_place(ROOT, "C:/Python/python.exe") is None  # a checkout: the variable decides
    assert paths_mod.home_of_place(b / "run" / "updater" / "app", str(b / "run" / "updater" / "supervisor" / "Scripts" / "python.exe")) is None
    assert paths_mod.home_of_place(tmp_path / "new" / "app", "C:/Python/python.exe") is None  # no runtime.json yet
    monkeypatch.setattr(paths_mod, "repo_root", lambda: b / "app")
    monkeypatch.setattr(paths_mod, "_said_place", [])
    monkeypatch.setenv(HOME_VAR, str(a))
    monkeypatch.setenv("NS_TEST_USER_HOME", str(a))  # the PC's one default names A; nothing set it for this process
    monkeypatch.delenv("NS_HOME_EXPLICIT", raising=False)
    assert Paths.from_env().home == b and Paths.from_env().home == b
    err = capsys.readouterr().err
    assert err.count(f"Nightshift: using the install in {b} (this program is in it); NIGHTSHIFT_HOME names {a}") == 1
    monkeypatch.setenv(HOME_VAR, str(b))
    assert Paths.from_env().home == b and capsys.readouterr().err == ""  # they agree: nothing to say
    assert Paths.from_env({HOME_VAR: str(a)}).home == a  # an environment handed in is somebody else's process


@needs_powershell
def test_the_uninstaller_stops_only_processes_inside_the_folder_it_removes():
    text = UNINSTALL.read_text(encoding="ascii")
    fn = cut(text, "function Test-UnderFolder {", "function Test-RunsThisProgram {")
    assert "Test-UnderFolder -Path $exe -Folder $nsHome" in text and "$exe.StartsWith($nsHome" not in text
    asks = [("D:\\Nightshift\\venvs\\s\\python.exe", "D:\\Nightshift", "True"), ("D:\\Nightshift2\\venvs\\s\\python.exe", "D:\\Nightshift", "False"),
            ("d:\\nightshift\\bin\\x.exe", "D:\\Nightshift\\", "True"), ("D:\\Nightshift", "D:\\Nightshift", "False"), ("", "D:\\Nightshift", "False")]
    calls = "\n".join(f"Write-Output (Test-UnderFolder -Path '{p}' -Folder '{f}')" for p, f, _ in asks)
    done = powershell(f"{fn}\n{calls}")
    assert done.stdout.split() == [want for _, _, want in asks], done.stdout + done.stderr


@needs_powershell
def test_uninstalling_the_first_install_says_the_second_is_still_there_and_keeps_what_it_uses(tmp_path: Path, windows: FakeWindows):
    """B holds none of the shared things (its owner never said yes): A's uninstaller learns of it from the PC's
    list of installs, which every setup of this version writes."""
    a, desktop, _ = two(tmp_path)
    ctx = second(tmp_path)
    listing = tmp_path / "local" / "Nightshift" / "installs.txt"
    ctx.installs_list = listing
    setup(ctx)
    b = ctx.paths.home
    (b / "config" / "runtime.json").write_text("{}", encoding="utf-8")
    assert listing.read_text(encoding="utf-8").split("\n")[:1] == [str(b)]
    again = second(tmp_path, saved_answers(ctx.paths))
    again.installs_list = listing
    setup(again)  # a second run adds nothing
    assert listing.read_text(encoding="utf-8").count(str(b)) == 1
    known = [ln for ln in listing.read_text(encoding="utf-8").splitlines() if ln]
    machine = {"task": {"arguments": schedule.action_arguments(Paths(a))}, "userHome": str(a), "toastKey": True, "installs": [str(a), *known, str(tmp_path / "deleted")]}
    out = uninstall_dry(a, desktop, machine).stdout
    assert "[dry-run] remove scheduled task" in out and "[dry-run] remove user environment variable NIGHTSHIFT_HOME" in out
    assert "remove toast registration" not in out and "remove from Credential Manager" not in out  # B uses them
    assert f"Left Credential Manager as it is (not looked into: this install has no Python environment to look with): any stock-footage keys in it are used by the Nightshift at {b}." in out
    assert f"Another Nightshift is still installed at {b}." in out and f"run INSTALL.cmd in {b / 'app'}" in out
    assert str(tmp_path / "deleted") not in out
    # the window shows that sentence too, and still reports the Desktop shortcuts as removed
    app = web_uninstall.UninstallApp(ROOT, a, popen=lambda *_a, **_k: Script([ln for ln in out.splitlines() if ln.startswith(("Left ", "Another "))]), deferred=lambda _h: True, env_var=lambda _n: None)
    app._work(False, False)
    result = app.view()["result"]
    assert any(k["label"].startswith(f"Another Nightshift is still installed at {b}.") for k in result["kept"])
    assert "Desktop shortcuts" in result["removed"] and "The nightly task" in result["removed"]


def test_the_yes_is_offered_when_only_the_variable_is_another_installs(tmp_path: Path, windows: FakeWindows):
    assert shortcuts_view is not None
    a = install_at(tmp_path / "A")
    desktop = tmp_path / "k" / "desktop"
    first = second(tmp_path / "k")  # B alone on the Desktop ...
    first.runner.user_env[HOME_VAR] = str(a)  # ... while the variable names A (QA's D-42 end state)
    first.runner._save_user_env()
    text = setup(first)
    b = first.paths.home
    assert desktop_opens(desktop) == {str(b)} and first.runner.user_env[HOME_VAR] == str(a)
    kept = said(text, "NIGHTSHIFT_HOME:")
    assert "switch 'Make the Desktop shortcuts and the ns and studio commands open this install' on" in kept and '"shortcuts_here": true' in kept
    assert "that yes moves NIGHTSHIFT_HOME and the Desktop shortcuts here" in kept  # exactly what it moves
    # the window: the switch is there for the variable alone, and its line says what on does
    now = answers_view.folder_now(b, ROOT, desktop, str(a))["shortcutsNow"]
    assert now["state"] == "other" and now["path"] == str(a)
    assert f"NIGHTSHIFT_HOME names the Nightshift at {a}" in now["line"] and "on makes it name this install" in now["line"]
    both = answers_view.folder_now(tmp_path / "c" / "home", ROOT, desktop, str(a))["shortcutsNow"]  # a third folder
    assert both["line"] == f"Now they open the Nightshift at {b}, and NIGHTSHIFT_HOME names the Nightshift at {a}; on moves both here"
    assert answers_view.folder_now(b, ROOT, desktop, str(b))["shortcutsNow"]["state"] == "mine"  # nothing to ask
    view = answers_view.answers_to_view({**saved_answers(first.paths), "shortcuts_here": True}, shortcuts_now=now)
    assert view["shortcutsHere"] is True  # and a yes for it reaches the answers
    # the console asks about the variable alone, and a yes moves it
    ui = FakeUi({"Make it name this install instead": "y"}, interactive=True)
    other = second(tmp_path / "y", ui=ui)
    other.runner.user_env[HOME_VAR] = str(a)
    other.runner._save_user_env()
    text = setup(other)
    # (no word on what a plain 'ns' runs: since FN-3 the command files decide that, not this variable)
    assert f"NIGHTSHIFT_HOME names the Nightshift at {a}. Make it name this install instead?" in ui.asked
    assert other.runner.user_env[HOME_VAR] == str(other.paths.home)
    assert said(text, "NIGHTSHIFT_HOME:").startswith(f"NIGHTSHIFT_HOME: it named the Nightshift at {a}; now it names this install, as you chose.")


def test_the_setup_window_opens_on_the_install_its_program_is_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from installer.stage2.web.controller import SetupController

    a = install_at(tmp_path / "A")
    b = install_at(tmp_path / "B")
    monkeypatch.setenv(HOME_VAR, str(a))

    class Window:
        """`_initial_home` reads the program folder, the variable and stage 1's pick: nothing else."""

        fx = type("Fx", (), {"get_home_var": staticmethod(lambda _n: str(a))})()
        pf: dict[str, Any] = {"home": "C:/Elsewhere"}

        def __init__(self, app: Path) -> None:
            self.cfg = type("Cfg", (), {"home": None, "app_path": app, "answers": None})()

        _prefill_raw = SetupController._prefill_raw

    assert SetupController._initial_home(Window(b / "app")) == b  # <B>/app/INSTALL.cmd: B, not the variable's A
    assert SetupController._initial_home(Window(tmp_path / "zip")) == a  # an extracted zip: the variable, as before


class Script:
    """The uninstall program as the window starts it: its lines, then exit 0."""

    def __init__(self, lines: list[str]) -> None:
        self.stdout = iter(line + "\n" for line in lines)

    def wait(self) -> int:
        return 0


def test_the_uninstall_window_says_what_was_left_for_the_other_install(tmp_path: Path):
    a = install_at(tmp_path / "A")
    b = install_at(tmp_path / "B")
    left = [
        f"Left 'Nightshift Daily': it runs the Nightshift at {a}.",
        f"Left 6 Desktop shortcuts: they open the Nightshift at {a}.",
        f"Left NIGHTSHIFT_HOME: it names the Nightshift at {a}.",
    ]
    app = web_uninstall.UninstallApp(ROOT, b, popen=lambda *_a, **_k: Script(["  removing x", *left]), deferred=lambda _h: True, env_var=lambda _n: str(a))
    app._work(False, False)
    result = app.view()["result"]
    labels = [k["label"] for k in result["kept"]]
    assert all(line in labels for line in left)
    assert not {"The nightly task", "Desktop shortcuts", web_uninstall.NOTE_LABEL} & set(result["removed"])
    assert not [label for label in labels if "could not be removed" in label]  # A's variable is not a failure
    # the only install: all three went, as before
    alone = web_uninstall.UninstallApp(ROOT, b, popen=lambda *_a, **_k: Script(["  removed scheduled task"]), deferred=lambda _h: True, env_var=lambda _n: None)
    alone._work(False, False)
    assert {"The nightly task", "Desktop shortcuts", web_uninstall.NOTE_LABEL} <= set(alone.view()["result"]["removed"])
    # what the window promises before the owner presses the button is true for a second install too
    promised = [row["label"] for row in app.view()["always"]]
    assert "The nightly task, when it runs this install" in promised and "Desktop shortcuts that open this install" in promised
