# ruff: noqa: E501
"""Card launcher-reach: the launch script reaches every install, every start and the nightly task.

Nothing here touches this PC: the Desktop is a folder under `tmp_path` handed to `launch_reach.run`, the
shortcuts are written by a stand-in runner (`Shell`) or, for the one real round trip, by PowerShell into
`tmp_path`, and the Task Scheduler is `FakeWindows`. The stub's own tests are in tests/test_launchers.py.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime
import importlib
import json
import os
import stat
import struct
import subprocess
import sys
import threading
import types
from pathlib import Path
from typing import Any

import httpx
import pytest

from installer.stage2 import winenv
from installer.stage2.runner import RunResult, SubprocessRunner
from installer.stage2.steps.s09_shortcuts_scheduler import SHORTCUTS
from installer.stage2.web import nightly_view
from ns_common import first_start, rollback_intent, testguard
from ns_common.logging import NsLogger
from ns_common.paths import Paths
from ns_supervisor import doctor, notify, pwsh, schedule
from ns_supervisor.client import SupervisorClient
from ns_supervisor.schedule_cfg import remembered_run_time, write_run_time
from ns_supervisor.testguard import RealMachineUnderTest
from tests.test_nightly_optional import FakeWindows
from tests.test_ns_schedule import needs_powershell


def _module(name: str) -> Any:
    """A module this card adds; None on the base, so a test fails at its assertion and not at an import."""
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


reach: Any = _module("ns_supervisor.launch_reach")
so: Any = _module("installer.stage2.shortcut_owner")
NEW = "this card's module is not there"
ROOT = Path(__file__).resolve().parents[1]
HARD = "Jo's Nightshift (2) \u0644\u064a\u0644\u0629"
SIX = [(name, script) for name, script, code in SHORTCUTS if not code]
HEAD = "-NoProfile -ExecutionPolicy Bypass -File"


def direct(home: Path, script: str) -> str:
    """What 0.1.14 wrote into a shortcut."""
    return f'{HEAD} "{home}\\app\\launchers\\{script}"'


def through_stub(home: Path, script: str) -> str:
    return f'{HEAD} "{home}\\nightshift-launch.ps1" -Launcher "{home}\\app\\launchers\\{script}"'


def write_lnk(path: Path, arguments: str, workdir: str, icon: str = "", name: str = "") -> None:
    """A minimal shell link (MS-SHLLINK): the header and Unicode StringData, no target list."""
    flags = 0x80 | (0x04 if name else 0) | 0x10 | 0x20 | (0x40 if icon else 0)
    head = struct.pack("<I16sI", 0x4C, bytes.fromhex("0114020000000000c000000000000046"), flags) + bytes(52)
    body = b"".join(
        struct.pack("<H", len(text)) + text.encode("utf-16-le") for text in (name, workdir, arguments, icon) if text
    )
    path.write_bytes(head + body + bytes(4))


class Shell:
    """The runner handed to the reach: it changes a shortcut as PowerShell would, and names the Desktop."""

    def __init__(self, desktop: Path | None = None) -> None:
        self.desktop, self.calls = desktop, []  # type: ignore[var-annotated]
        self.user_path = ""                     # the user PATH this stand-in Windows holds (card FN-3)

    def run(self, argv: Any, *, tag: str = "", meta: Any = None, **kw: Any) -> RunResult:
        self.calls.append((tag, dict(meta or {})))
        if tag == "ps:userpath-read":
            return RunResult(0, winenv.user_path_output(self.user_path))
        if tag == "ps:userpath-write":
            self.user_path = meta["new"]
            return RunResult(0, "written\n")
        if tag == "ps:launch-cleanup":       # the launch script itself, for real (it is under a test run)
            done = subprocess.run(list(argv), capture_output=True, text=True, timeout=120)
            return RunResult(done.returncode, done.stdout, done.stderr)
        if tag == "ps:desktop":
            return RunResult(0, __import__("base64").b64encode(str(self.desktop or "").encode()).decode())
        if tag == "ps:shortcut-update":
            lnk = Path(meta["lnk"])
            if not os.access(lnk, os.W_OK):
                return RunResult(1, "", "Access to the path is denied.")
            old = so.read_lnk(lnk)
            write_lnk(lnk, meta["arguments"], meta.get("workdir") or old.working_dir, meta.get("icon") or old.icon, old.description)
        return RunResult(0)

    def updates(self) -> list[str]:
        return [Path(m["lnk"]).name for tag, m in self.calls if tag == "ps:shortcut-update"]


@dataclasses.dataclass
class Inst:
    home: Path
    paths: Paths
    app: Path
    desktop: Path
    shell: Shell
    windows: FakeWindows
    icon: Path

    def run(self, **kw: Any) -> dict[str, Any]:
        return reach.run(self.paths, self.app, **{"desktop": self.desktop, "runner": self.shell, **kw})

    def args(self, name: str) -> str:
        return so.read_lnk(self.desktop / f"{name}.lnk").arguments

    def new_stamp(self) -> None:
        """The program folder was put in place again (an update): a new completion marker."""
        done = self.app / ".ns-app.done"
        later = done.stat().st_mtime_ns + 5_000_000_000
        os.utime(done, ns=(later, later))


@pytest.fixture
def inst(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Inst:
    """An install as 0.1.14's updater leaves it under this version: no stub in the home, six direct
    shortcuts, and the nightly task enabled at 05:30 with 0.1.14's action."""
    home = tmp_path / HARD
    paths = Paths(home)
    paths.ensure()
    app = home / "app"
    for _, script in SIX:
        (app / "launchers").mkdir(parents=True, exist_ok=True)
        (app / "launchers" / script).write_text("exit 0\r\n", encoding="ascii")
    (app / "installer").mkdir()
    (app / "installer" / "launch.ps1").write_bytes((ROOT / "installer" / "launch.ps1").read_bytes())
    (app / "VERSION").write_text("0.1.15\n", encoding="ascii")
    (app / ".ns-app.done").write_text("done", encoding="ascii")
    icon = app / "ns_dashboard" / "static" / "brand" / "nightshift.ico"
    icon.parent.mkdir(parents=True)
    icon.write_bytes(b"ico")
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    for name, script in SIX:
        write_lnk(desktop / f"{name}.lnk", direct(home, script), str(home), str(icon), f"Nightshift: {name}")
    windows = FakeWindows(("Ready", "05:30"))
    assert windows.task is not None
    windows.task["home"] = str(home)
    write_run_time(paths, "05:30", "registered")
    monkeypatch.setattr(pwsh, "run_powershell", windows)
    return Inst(home, paths, app, desktop, Shell(), windows, icon)


def shortcut_notes(paths: Paths) -> list[Any]:
    """The waiting notes, apart from FN-3's one note about the commands (this install's folder name has
    Arabic letters, which a command file cannot hold: its own test is in tests/test_commands_on_path.py)."""
    return [n for n in notify.read_notes(paths) if n["title"] != "Nightshift commands"]


def launch_row(paths: Paths) -> tuple[bool, str]:
    """The "launch script" row of `ns doctor`."""

    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no engine")

    client = SupervisorClient("http://sup", client=httpx.Client(transport=httpx.MockTransport(refuse)))
    rows = {name: (ok, detail) for name, ok, detail in doctor.diagnose(paths, client)}
    assert "launch script" in rows, sorted(rows)
    return rows["launch script"]


# ------------------------------------------------------------------------------------ the reach itself
def test_an_install_updated_by_0_1_14_gets_the_stub_its_shortcuts_and_the_task_at_the_first_start(inst: Inst):
    assert reach is not None and so is not None, NEW
    stub = inst.home / "nightshift-launch.ps1"
    assert not stub.exists()
    marker = inst.run()
    assert stub.read_bytes() == (inst.app / "installer" / "launch.ps1").read_bytes()
    for name, script in SIX:
        lnk = so.read_lnk(inst.desktop / f"{name}.lnk")
        assert lnk.arguments == through_stub(inst.home, script) and so.is_stub_form(lnk.arguments, inst.home)
        assert (lnk.working_dir, lnk.icon, lnk.description) == (str(inst.home), str(inst.icon), f"Nightshift: {name}")
    assert sorted(p.name for p in inst.desktop.iterdir()) == sorted(f"{name}.lnk" for name, _ in SIX)
    task = inst.windows.task
    assert task is not None and (task["state"], task["at"], task["logon"]) == ("Ready", "05:30", "S4U")
    assert task["args"] == schedule.action_arguments(inst.paths) and schedule.is_stub_action(task["args"])
    assert inst.windows.changes() == ["set"]                  # only the action was set: nothing registered again
    assert marker["stamp"] == first_start.install_stamp(inst.app) and marker["failed"] == []
    file = reach.marker_file(inst.paths)
    assert json.loads(file.read_text(encoding="utf-8")) == marker
    # a second start: no PowerShell, nothing written
    seen = (len(inst.shell.calls), list(inst.windows.calls), file.read_bytes(), file.stat().st_mtime_ns)
    inst.run()
    assert (len(inst.shell.calls), list(inst.windows.calls), file.read_bytes(), file.stat().st_mtime_ns) == seen
    # killed before the marker was written: every step again, and none finds anything left to change
    file.unlink()
    updates, changes = inst.shell.updates(), inst.windows.changes()
    assert inst.run()["failed"] == [] and file.exists()
    assert (inst.shell.updates(), inst.windows.changes()) == (updates, changes)


def test_shortcuts_the_owner_deleted_stay_deleted(inst: Inst):
    assert reach is not None, NEW
    for name in ("Nightshift Outbox", "Nightshift - Update"):
        (inst.desktop / f"{name}.lnk").unlink()
    inst.run()
    assert sorted(p.name for p in inst.desktop.iterdir()) == sorted(
        f"{name}.lnk" for name, _ in SIX if name not in ("Nightshift Outbox", "Nightshift - Update")
    )
    assert all(so.is_stub_form(so.read_lnk(p).arguments, inst.home) for p in inst.desktop.iterdir())
    # nothing on the Desktop at all: nothing to do, and that is not a failure
    for p in inst.desktop.iterdir():
        p.unlink()
    inst.new_stamp()
    calls = len(inst.shell.calls)
    assert inst.run()["failed"] == [] and len(inst.shell.calls) == calls and list(inst.desktop.iterdir()) == []


def test_a_renamed_shortcut_of_this_install_is_rewritten_under_its_own_name(inst: Inst):
    assert reach is not None, NEW
    (inst.desktop / "Nightshift Dashboard.lnk").rename(inst.desktop / "Night work.lnk")
    inst.run()
    assert inst.args("Night work") == through_stub(inst.home, "nightshift-dashboard.ps1")
    assert not (inst.desktop / "Nightshift Dashboard.lnk").exists()


def test_a_shortcut_of_another_existing_install_is_never_touched(inst: Inst, tmp_path: Path):
    assert reach is not None, NEW
    other = tmp_path / "Other Nightshift"
    (other / "app" / "launchers").mkdir(parents=True)
    write_lnk(inst.desktop / "Other direct.lnk", direct(other, "nightshift-dashboard.ps1"), str(other))
    write_lnk(inst.desktop / "Other stub.lnk", through_stub(other, "update.ps1"), str(other))
    write_lnk(inst.desktop / "Editor.lnk", '-File "C:\\tools\\edit.ps1"', "C:\\tools")
    (inst.desktop / "Torn.lnk").write_bytes(b"not a shortcut")        # the one bad file among good ones
    kept = {p.name: p.read_bytes() for p in inst.desktop.iterdir() if p.stem in ("Other direct", "Other stub", "Editor", "Torn")}
    marker = inst.run()
    assert {name: (inst.desktop / name).read_bytes() for name in kept} == kept
    assert marker["failed"] == [] and marker["shortcuts"]["Other direct.lnk"] == "other"
    assert marker["shortcuts"]["Editor.lnk"] == marker["shortcuts"]["Torn.lnk"] == "foreign"
    assert all(inst.args(name) == through_stub(inst.home, script) for name, script in SIX)
    assert shortcut_notes(inst.paths) == []


def test_a_shortcut_of_a_deleted_install_is_never_taken(inst: Inst, tmp_path: Path):
    """The Desktop holds shortcuts of three homes: this one, another that exists, one that was deleted.
    Only this home's change; nothing is taken over and nothing is said."""
    assert reach is not None, NEW
    other, gone = tmp_path / "Other Nightshift", tmp_path / "Deleted Nightshift"
    (other / "app" / "launchers").mkdir(parents=True)
    theirs = {}
    for home, tag in ((other, "other"), (gone, "gone")):
        for name, script in SIX:
            lnk = inst.desktop / f"{name} ({tag}).lnk"
            write_lnk(lnk, direct(home, script), str(home), str(home / "n.ico"), f"Nightshift: {name}")
            theirs[lnk.name] = lnk.read_bytes()
    marker = inst.run()
    assert {name: (inst.desktop / name).read_bytes() for name in theirs} == theirs
    assert all(inst.args(name) == through_stub(inst.home, script) for name, script in SIX)
    assert sorted(inst.shell.updates()) == sorted(f"{name}.lnk" for name, _ in SIX)
    states = marker["shortcuts"]
    assert {states[f"{name} (other).lnk"] for name, _ in SIX} == {"other"}
    assert {states[f"{name} (gone).lnk"] for name, _ in SIX} == {"gone"}
    assert marker["failed"] == [] and shortcut_notes(inst.paths) == []
    assert sorted(p.name for p in inst.desktop.iterdir()) == sorted([*theirs, *(f"{name}.lnk" for name, _ in SIX)])
    # a shortcut of this home whose launcher this program no longer has is left too
    write_lnk(inst.desktop / "Code.lnk", direct(inst.home, "nightshift-code.ps1"), str(inst.home))
    inst.new_stamp()
    assert inst.run()["shortcuts"]["Code.lnk"] == "left"


def test_a_dev_install_never_changes_the_desktop(inst: Inst):
    assert reach is not None, NEW
    inst.paths.runtime_file.write_text(json.dumps({"dev_mode": True}), encoding="utf-8")
    before = {p.name: p.read_bytes() for p in inst.desktop.iterdir()}
    marker = inst.run()
    assert {p.name: p.read_bytes() for p in inst.desktop.iterdir()} == before and inst.shell.calls == []
    assert marker["failed"] == [] and marker["stub"] == "ok" and marker["desktop"].startswith("dev install")


def test_no_test_reaches_the_real_desktop_through_the_lowest_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """`winenv` refuses under a test run before anything is started, whatever a test did with its fakes. The
    refusal and its list of real folders are the one pair in `ns_common.testguard` (`refuse_real_shortcut`,
    `REAL_FOLDERS`); `winenv` keeps no list of its own."""
    assert hasattr(winenv, "update_shortcut"), NEW
    assert not hasattr(winenv, "real_shell_folders") and not hasattr(winenv, "refuse_real_desktop")
    folders = [Path(f) for f in testguard.REAL_FOLDERS]

    def reached(*a: Any, **k: Any) -> Any:
        raise AssertionError("the real PowerShell was started")

    monkeypatch.setattr("installer.stage2.runner.subprocess.run", reached)
    real = SubprocessRunner()
    with pytest.raises(RealMachineUnderTest, match="Desktop"):
        winenv.desktop_dir(real)
    assert len(folders) >= 2                                  # the Desktop and the common Desktop, at least
    for folder in folders:
        lnk = folder / "Nightshift Dashboard.lnk"
        for call in (
            lambda lnk=lnk: winenv.create_shortcut(real, lnk, "powershell.exe", "-File x", tmp_path, "d"),
            lambda lnk=lnk: winenv.update_shortcut(real, lnk, "-File x"),
            lambda lnk=lnk: winenv.set_shortcut_aumid(real, lnk, "Nightshift"),
        ):
            with pytest.raises(RealMachineUnderTest, match="a shortcut in a real shell folder"):
                call()
    monkeypatch.delenv("NS_UNDER_TEST")                      # no environment change lifts it in this process
    with pytest.raises(RealMachineUnderTest):
        winenv.update_shortcut(real, folders[0] / "x.lnk", "-File x")


def test_a_shortcut_whose_drive_is_not_there_is_left(inst: Inst):
    assert reach is not None, NEW
    letter = next(c for c in "QRSTUVWXYZ" if not os.path.exists(f"{c}:\\"))
    away = Path(f"{letter}:\\Nightshift")
    write_lnk(inst.desktop / "Away.lnk", direct(away, "nightshift-dashboard.ps1"), str(away))
    before = (inst.desktop / "Away.lnk").read_bytes()
    marker = inst.run()
    assert (inst.desktop / "Away.lnk").read_bytes() == before and marker["shortcuts"]["Away.lnk"] == "unreachable"
    assert marker["failed"] == [] and shortcut_notes(inst.paths) == []


def test_the_desktop_folder_is_the_one_windows_names_also_when_onedrive_moved_it(
    inst: Inst, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    assert reach is not None, NEW
    moved = tmp_path / "OneDrive" / "Desktop"
    moved.mkdir(parents=True)
    write_lnk(moved / "Nightshift Studio.lnk", direct(inst.home, "nightshift-studio.ps1"), str(inst.home))
    old = {p.name: p.read_bytes() for p in inst.desktop.iterdir()}
    reach.run(inst.paths, inst.app, runner=Shell(moved))        # no folder handed in: Windows is asked
    assert so.read_lnk(moved / "Nightshift Studio.lnk").arguments == through_stub(inst.home, "nightshift-studio.ps1")
    assert {p.name: p.read_bytes() for p in inst.desktop.iterdir()} == old
    # a test run never asks the real Windows for the real Desktop, whatever it passes
    inst.new_stamp()

    def reached(*a: Any, **k: Any) -> Any:
        raise AssertionError("the real PowerShell was started")

    monkeypatch.setattr("installer.stage2.runner.subprocess.run", reached)
    for runner in (None, SubprocessRunner()):
        with pytest.raises(RealMachineUnderTest, match="Desktop"):
            reach.run(inst.paths, inst.app, runner=runner)


@needs_powershell
def test_a_hard_home_round_trips_through_a_real_shortcut(tmp_path: Path):
    assert so is not None and hasattr(winenv, "update_shortcut"), NEW
    home = tmp_path / HARD
    home.mkdir()
    lnk, runner = tmp_path / f"{HARD}.lnk", SubprocessRunner()
    args = direct(home, "nightshift-dashboard.ps1")
    assert winenv.create_shortcut(runner, lnk, "powershell.exe", args, home, "Nightshift: Dashboard", home / "n.ico")
    assert winenv.set_shortcut_aumid(runner, lnk, "Nightshift")
    read = so.read_lnk(lnk)
    assert (read.arguments, read.working_dir, read.description) == (args, str(home), "Nightshift: Dashboard")
    assert so.owner(read.arguments, home) == "mine" and not so.is_stub_form(read.arguments, home)
    new = through_stub(home, "nightshift-dashboard.ps1")
    assert winenv.update_shortcut(runner, lnk, new).ok
    again = so.read_lnk(lnk)
    assert (again.arguments, again.working_dir, again.description) == (new, str(home), "Nightshift: Dashboard")
    assert again.icon.startswith(str(home / "n.ico")) and so.is_stub_form(again.arguments, home)
    kept = winenv.run_ps(runner, winenv.get_shortcut_aumid_script(lnk), tag="ps:aumid-read")
    assert kept.stdout.strip() == "Nightshift", kept.text    # the toast identity survives the rewrite
    # the change went through a temp file and a rename: nothing is left beside the shortcut ...
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("~")] == []
    # ... and a shortcut that cannot be replaced is whole and unchanged, with the reason
    whole = lnk.read_bytes()
    os.chmod(lnk, stat.S_IREAD)
    try:
        refused = winenv.update_shortcut(runner, lnk, args)
    finally:
        os.chmod(lnk, stat.S_IWRITE | stat.S_IREAD)
    assert not refused.ok and refused.text and lnk.read_bytes() == whole
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith("~")] == []


def test_a_failed_reach_is_retried_at_the_next_start_and_shown_by_doctor(inst: Inst):
    assert reach is not None, NEW
    held = inst.desktop / "Nightshift Outbox.lnk"
    os.chmod(held, stat.S_IREAD)
    try:
        marker = inst.run()
        assert marker["failed"] == ["shortcuts"] and inst.args("Nightshift Outbox") == direct(inst.home, "open-outbox.ps1")
        assert sum(so.is_stub_form(inst.args(name), inst.home) for name, _ in SIX) == 5
        ok, detail = launch_row(inst.paths)
        assert not ok and detail.startswith(
            "1 Desktop shortcut still starts Nightshift directly (Nightshift could not update it: Access to the path is denied.). "
            "It works; only after an interrupted update would it close without a word. "
            "Run INSTALL.cmd and choose Repair to update it."
        )
    finally:
        os.chmod(held, stat.S_IWRITE | stat.S_IREAD)
    done = len(inst.shell.updates())
    assert inst.run()["failed"] == []                       # the same install stamp: only the failure is retried
    assert inst.shell.updates()[done:] == ["Nightshift Outbox.lnk"]
    assert inst.args("Nightshift Outbox") == through_stub(inst.home, "open-outbox.ps1")
    assert launch_row(inst.paths) == (True, "ok")


def test_a_stub_that_cannot_be_written_stops_the_reach_and_is_said(inst: Inst):
    assert reach is not None, NEW
    (inst.home / "nightshift-launch.ps1").mkdir()            # nothing can be put in its place
    marker = inst.run()
    assert marker["failed"] == ["stub"] and inst.shell.updates() == [] and inst.windows.changes() == []
    ok, detail = launch_row(inst.paths)
    assert not ok and "The launch script could not be copied" in detail and "choose Repair" in detail
    (inst.home / "nightshift-launch.ps1").rmdir()
    assert inst.run()["failed"] == [] and len(inst.shell.updates()) == 6


def test_a_task_windows_will_not_change_is_left_and_said_by_doctor(inst: Inst):
    assert reach is not None, NEW
    inst.windows.refuse_all = True
    marker = inst.run()
    assert marker["failed"] == ["task"] and len(inst.shell.updates()) == 6
    assert inst.windows.task is not None and "args" not in inst.windows.task     # as 0.1.14 left it
    ok, detail = launch_row(inst.paths)
    assert not ok and detail.startswith("The nightly task starts Nightshift directly (")
    assert "Access is denied" in detail and detail.split("), ", 1)[1].startswith(
        "so a missing program folder would go unnoticed at night. Run INSTALL.cmd and choose Repair."
    )
    inst.windows.refuse_all = False
    assert inst.run()["failed"] == [] and schedule.is_stub_action(inst.windows.task["args"])


@pytest.mark.parametrize("case", ["other", "disabled", "none", "dev"])
def test_a_task_that_is_not_this_installs_enabled_one_is_not_touched(inst: Inst, tmp_path: Path, case: str):
    assert reach is not None, NEW
    assert inst.windows.task is not None
    if case == "other":
        (tmp_path / "Other").mkdir()
        inst.windows.task["home"] = str(tmp_path / "Other")
    elif case == "disabled":
        inst.windows.task["state"] = "Disabled"
    elif case == "none":
        inst.windows.task = None
    else:
        inst.paths.runtime_file.parent.mkdir(parents=True, exist_ok=True)
        inst.paths.runtime_file.write_text(json.dumps({"dev_mode": True}), encoding="utf-8")
    marker = inst.run()
    assert inst.windows.changes() == [] and marker["failed"] == []
    assert len(inst.shell.updates()) == (0 if case == "dev" else 6)      # a dev install leaves the Desktop


def test_the_engine_start_hands_the_reach_to_a_thread(inst: Inst, monkeypatch: pytest.MonkeyPatch):
    assert reach is not None, NEW
    entered, gate = threading.Event(), threading.Event()

    def slow(paths: Paths, app: Path, **kw: Any) -> dict[str, Any]:
        entered.set()
        assert gate.wait(10)
        raise OSError("the disk went away")

    thread = reach.start(inst.paths, inst.app, NsLogger("engine", inst.paths.logs, echo=False), run=slow)
    assert entered.wait(5) and thread.is_alive() and thread.daemon      # start came back while run still waits
    gate.set()
    thread.join(5)
    assert not thread.is_alive()
    logged = "".join(f.read_text(encoding="utf-8") for f in inst.paths.logs.glob("engine-*.jsonl"))
    assert "launch_reach_failed" in logged and "the disk went away" in logged
    # and the engine's start calls it, right after the start check and before anything else can fail
    from ns_supervisor import app as engine

    class Seen(Exception):
        pass

    started: list[Any] = []

    def stop(paths: Paths) -> Any:
        raise Seen

    monkeypatch.setitem(sys.modules, "uvicorn", types.ModuleType("uvicorn"))
    monkeypatch.setattr(first_start, "program_dir", lambda: inst.app)
    monkeypatch.setattr(reach, "start", lambda paths, app, log=None, **kw: started.append((paths, app)))
    monkeypatch.setattr(engine, "load_runtime", stop)
    with pytest.raises(Seen):
        engine._serve(argparse.Namespace(port=None, headless=True), inst.paths)
    assert started == [(inst.paths, inst.app)]


def test_a_rollback_to_a_version_without_the_reach_and_forward_again_keeps_working(inst: Inst):
    assert reach is not None, NEW
    source = inst.app / "installer" / "launch.ps1"
    text = source.read_bytes()
    source.unlink()                                          # a program folder that has no launch script
    marker = inst.run()
    assert not (inst.home / "nightshift-launch.ps1").exists() and marker["failed"] == []
    assert inst.shell.calls == [] and inst.windows.changes() == []
    assert inst.args("Nightshift Studio") == direct(inst.home, "nightshift-studio.ps1")
    source.write_bytes(text)                                 # forward again: a new stamp runs the reach again
    inst.new_stamp()
    assert inst.run()["failed"] == [] and len(inst.shell.updates()) == 6
    newer = b"# STUB_SCHEMA: 9\r\n# a later version's script\r\n"
    (inst.home / "nightshift-launch.ps1").write_bytes(newer)  # back on this version after a newer one ran here
    inst.new_stamp()
    inst.run()
    assert (inst.home / "nightshift-launch.ps1").read_bytes() == newer
    assert so.stub_schema(source) == so.STUB_SCHEMA == 6      # (6: night-fits' NS_NIGHT_STARTED)


@pytest.mark.parametrize("logon", ["Interactive", "S4U", "Password"])
def test_the_task_keeps_its_logon_type_and_time(inst: Inst, logon: str):
    """Only the action is set. A task that runs only while signed in is not made to run signed out, and one
    the owner gave a password in Task Scheduler is not registered again without it."""
    assert reach is not None, NEW
    assert inst.windows.task is not None
    inst.windows.task.update(logon=logon, at="04:45")
    write_run_time(inst.paths, "04:45", "interactive_only" if logon == "Interactive" else "registered")
    assert inst.run()["task"] == "done"
    task = inst.windows.task
    assert (task["state"], task["at"], task["logon"]) == ("Ready", "04:45", logon)
    assert schedule.is_stub_action(task["args"]) and "register" not in inst.windows.calls


def test_a_task_whose_logon_changed_with_the_action_is_said(inst: Inst):
    assert reach is not None, NEW
    inst.windows.set_changes_logon = True                    # type: ignore[attr-defined]
    marker = inst.run()
    assert marker["failed"] == ["task"]
    assert "the logon type was S4U and is Interactive" in marker["task"]
    ok, detail = launch_row(inst.paths)
    assert not ok and "the logon type was S4U and is Interactive" in detail


# --------------------------------------------------------------- a missed night is cleared by every start
def missed(inst: Inst, first: str, nights: int = 2) -> Path:
    """What the launch script left after nights without the program: its count and its Desktop note."""
    record = inst.home / "run" / "launch-missed.json"
    record.parent.mkdir(exist_ok=True)
    record.write_text(json.dumps({"first": first, "last": first, "nights": nights, "what": "folder"}), encoding="ascii")
    note = inst.desktop / "Nightshift did not run.txt"
    note.write_text(f"Nightshift could not start its nightly run on {first} (2 nights so far ...\n", encoding="utf-8-sig")
    return record


def stub_start(inst: Inst, *args: object) -> subprocess.CompletedProcess[str]:
    """A start through the launch script in the home (a Desktop shortcut, the nightly task)."""
    exe = pwsh.find_powershell()
    assert exe is not None
    argv = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(inst.home / "nightshift-launch.ps1")]
    argv += [str(a) for a in args] + ["-DesktopDir", str(inst.desktop)]
    return subprocess.run(argv, input="\n", capture_output=True, text=True, timeout=120)


def notes_about(inst: Inst) -> list[str]:
    return [n["message"] for n in notify.read_notes(inst.paths) if n["message"].startswith("Nightshift did not run")]


@needs_powershell
def test_an_engine_start_clears_a_missed_night_once_whatever_started_it(inst: Inst):
    """Setup's Open dashboard, `ns up`, the restart after an update: all start the engine without the launch
    script, and the engine's start clears the night, also when the reach itself has nothing left to do."""
    assert reach is not None, NEW
    assert inst.run()["failed"] == []                        # the reach is done for this program folder
    today = datetime.date.today().isoformat()
    record = missed(inst, "2026-11-08")
    marker_bytes = reach.marker_file(inst.paths).read_bytes()
    inst.run()                                               # the next engine start (the gate says: done)
    said = f"Nightshift did not run on 2 nights, from 2026-11-08 to 2026-11-08: its program folder was missing until it was put back on {today}."
    assert notes_about(inst) == [said]
    assert not record.exists() and not (inst.desktop / "Nightshift did not run.txt").exists()
    assert reach.marker_file(inst.paths).read_bytes() == marker_bytes
    calls = len(inst.shell.calls)
    inst.run()                                               # a second engine start: nothing left, no PowerShell
    assert notes_about(inst) == [said] and len(inst.shell.calls) == calls
    # each order of the two kinds of start clears one night once
    record = missed(inst, "2026-11-01")
    assert stub_start(inst, "-Launcher", inst.app / "launchers" / "update.ps1").returncode == 0     # a shortcut
    inst.run()                                               # then the engine it started
    assert len(notes_about(inst)) == 2 and not record.exists()
    record = missed(inst, "2026-10-20")
    inst.run()                                               # the engine first (Open dashboard from setup)
    assert stub_start(inst, "-Launcher", inst.app / "launchers" / "update.ps1").returncode == 0     # then a shortcut
    studio = inst.desktop.parent / "studio.cmd"
    studio.write_bytes(b"@echo off\r\nexit /b 0\r\n")
    (inst.app / "ns_studio").mkdir()
    (inst.app / "ns_studio" / "cli.py").write_text("", encoding="ascii")
    assert stub_start(inst, "-Nightly", "-Studio", studio).returncode == 0                          # then the night
    assert len(notes_about(inst)) == 3 and not record.exists()
    assert not (inst.desktop / "Nightshift did not run.txt").exists()


@needs_powershell
def test_two_starters_at_once_clear_a_missed_night_once(inst: Inst):
    """The step is slowed for real: another starter holds the count while this one arrives."""
    assert reach is not None, NEW
    assert inst.run()["failed"] == []
    record = missed(inst, "2026-11-08")
    import ctypes

    open_existing, read, share_delete = 3, 0x80000000, 4
    create = ctypes.windll.kernel32.CreateFileW
    create.restype = ctypes.c_void_p
    handle = create(str(record), read, share_delete, None, open_existing, 0, None)   # as the script holds it
    assert handle and handle != ctypes.c_void_p(-1).value
    try:
        assert stub_start(inst, "-Cleanup").stdout.strip() == "busy"
        inst.run()
        assert notes_about(inst) == [] and record.exists()    # the holder does it; nobody did it twice
    finally:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
    inst.run()
    assert len(notes_about(inst)) == 1 and not record.exists()


@needs_powershell
def test_a_missed_night_that_cannot_be_noted_keeps_its_count_and_the_reach_goes_on(inst: Inst):
    assert reach is not None, NEW
    record = missed(inst, "2026-11-08")
    blocked = inst.home / "run" / "morning_note.json"
    blocked.mkdir()                                          # the Dashboard's notes cannot be written
    marker = inst.run()
    assert record.exists() and (inst.desktop / "Nightshift did not run.txt").exists()
    assert marker["failed"] == [] and len(inst.shell.updates()) == 6
    blocked.rmdir()
    inst.run()
    assert len(notes_about(inst)) == 1 and not record.exists()


# ------------------------------------------------------------------------------- the task's own home
def test_task_owner_reads_the_home_from_the_new_action_and_from_the_0_1_14_one(inst: Inst):
    make = getattr(schedule, "action_arguments", None)
    assert make is not None, NEW
    studio = schedule.studio_exe(inst.paths)
    assert make(inst.paths) == (
        f'/d /s /c "set "NIGHTSHIFT_HOME={inst.home}" && if exist "{inst.home}\\nightshift-launch.ps1" '
        f'(powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{inst.home}\\nightshift-launch.ps1" '
        f'-Nightly -Studio "{studio}") else ("{studio}" run --daily)"'
    )
    head = "state: Ready\nstart: 2026-11-09T05:30:00\nlogon: S4U\nargs: "
    new = schedule.parse_task_state(head + make(inst.paths) + "\n")
    old = schedule.parse_task_state(head + f'/d /s /c "set "NIGHTSHIFT_HOME={inst.home}" && "{studio}" run --daily"\n')
    assert new is not None and old is not None
    assert schedule.task_owner(inst.paths, new) == schedule.task_owner(inst.paths, old) == "mine"
    assert (new.stub, old.stub) == (True, False)


@needs_powershell
def test_the_task_home_is_read_in_utf8(tmp_path: Path):
    home = tmp_path / HARD
    home.mkdir()
    # A PC whose console writes the OEM code page 437, as most do (this laptop's may be UTF-8 already): the
    # letters of the home do not exist in it.
    stand_in = (
        "[Console]::OutputEncoding = [Text.Encoding]::GetEncoding(437)\n"
        "function Get-ScheduledTask { [CmdletBinding()] param([string]$TaskName)\n"
        "  [pscustomobject]@{ State = 'Ready'; Triggers = @([pscustomobject]@{ StartBoundary = '2026-11-09T05:30:00' });\n"
        "    Principal = [pscustomobject]@{ LogonType = 'S4U' }; Actions = @([pscustomobject]@{ Arguments = $env:NS_TEST_ACTION }) } }\n"
    )
    action = f'/d /s /c "set "NIGHTSHIFT_HOME={home}" && "studio" run --daily"'
    done = pwsh.run_powershell(stand_in + schedule.build_read_script(), env_extra={"NS_TEST_ACTION": action})
    assert done.returncode == 0, done.stderr
    task = schedule.parse_task_state(done.stdout)
    assert task is not None and task.home == str(home), done.stdout
    assert schedule.task_owner(Paths(home), task) == "mine"


def test_a_home_that_came_back_damaged_is_never_read_as_a_deleted_install(tmp_path: Path):
    task = schedule.TaskState(True, "05:30", False, str(tmp_path / "Night \ufffd\ufffd"))
    assert schedule.task_owner(Paths(tmp_path / "home"), task) == "unknown"


def setup_again(inst: Inst) -> tuple[Any, str]:
    res = schedule.setup_apply(inst.paths, inst.app, first=False, nightly=None, seen=None, run_time="03:00")
    return res, nightly_view.outcome_line(dataclasses.asdict(res))


def test_a_repair_over_a_task_whose_folder_is_gone_removes_it_and_says_so(inst: Inst, tmp_path: Path):
    gone = tmp_path / "Deleted Nightshift"
    assert inst.windows.task is not None
    inst.windows.task.update(home=str(gone), at="04:10")
    schedule.schedule_file(inst.paths).unlink()
    inst.windows.refuse_unregister = True                    # Windows refuses first: said, and still there
    res, line = setup_again(inst)
    assert inst.windows.task is not None and res.case == "refused" and res.on is False
    assert line.startswith(f"Automatic runs: off. A nightly task left by a deleted Nightshift ({gone}) could not be removed (")
    assert "Delete 'Nightshift Daily' in Task Scheduler" in line
    inst.windows.refuse_unregister = False
    res, line = setup_again(inst)
    assert inst.windows.task is None and res.on is False and remembered_run_time(inst.paths) == "04:10"
    assert line == (
        f"Automatic runs: off. A nightly task left by a deleted Nightshift ({gone}) was removed: it woke this PC "
        f"for nothing. Switch Automatic runs on in {nightly_view.WHERE} to run this one every night."
    )
    res, line = setup_again(inst)                            # the next Repair has nothing left to say about it
    assert inst.windows.changes().count("unregister") == 2 and "deleted Nightshift" not in line


def test_a_task_whose_drive_is_not_there_is_kept_and_said(inst: Inst):
    letter = next(c for c in "QRSTUVWXYZ" if not os.path.exists(f"{c}:\\"))
    away = f"{letter}:\\Nightshift"
    assert inst.windows.task is not None
    inst.windows.task["home"] = away
    schedule.schedule_file(inst.paths).unlink()
    before = dict(inst.windows.task)
    res, line = setup_again(inst)
    assert inst.windows.task == before and inst.windows.changes() == [] and res.on is False
    assert schedule.task_owner(inst.paths, schedule.TaskState(True, "05:30", False, away)) == "unreachable"
    assert (
        f"The nightly task runs the Nightshift at {away}, which cannot be reached now (its drive is not there); "
        "it was left as it is."
    ) in line


# ----------------------------------------------------------------- the rollback record's saved settings
def test_a_lost_swap_folder_keeps_the_rollbacks_saved_settings_in_a_file(inst: Inst, monkeypatch: pytest.MonkeyPatch):
    saved = b"PK\x05\x06" + bytes(18)                         # the settings from before the rollback, as a zip
    rollback_intent.write(inst.app, rollback_intent.Intent("0.1.16", "f-new", "0.1.15", "f-old", saved, stage="prepared"))
    assert not rollback_intent.swap_for(inst.app).exists()   # app.swap was moved away; setup made a fresh app
    monkeypatch.setattr(first_start, "program_dir", lambda: inst.app)
    with contextlib.suppress(AttributeError):
        first_start.reset()
    first_start.ensure(inst.paths.config)
    kept = sorted(inst.home.glob("app.rollback-settings-*.zip"))
    assert len(kept) == 1 and kept[0].read_bytes() == saved, [p.name for p in inst.home.iterdir()]
    assert not rollback_intent.path_for(inst.app).exists()
    words = (
        f"The settings saved before the interrupted rollback are in {kept[0]}. To use them, stop Nightshift "
        f"and unzip it into {inst.paths.config}."
    )
    logged = "".join(f.read_text(encoding="utf-8") for f in inst.paths.logs.glob("first_start-*.jsonl"))
    assert json.dumps(words, ensure_ascii=False)[1:-1] in logged
    first_start.reset()
    _, ok, detail = doctor.program_folders(inst.paths, inst.app)
    assert ok and words in detail                            # a later look still names the file
