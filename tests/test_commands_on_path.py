# ruff: noqa: E501, F811
"""FN-3: the `ns` and `studio` commands that Nightshift's messages name work in any new terminal.

Each install gets `<home>\\cmd\\ns.cmd` and `studio.cmd`, and setup puts that folder on the user PATH by the
ownership rule of second-install-takeover. Nothing here reads or changes this PC's PATH, registry or
environment: the user PATH is the wizard's simulated one (`FakeRunner`, kept in a file beside the
temporary Desktop, shared by two installs as the real one is), `ns doctor` reads its stand-ins
(`NS_TEST_USER_PATH`, `NS_TEST_MACHINE_PATH`), the uninstaller runs `-DryRun -StateFile`, and its PATH
function and setup's PATH write script run under a real PowerShell 5.1 against stand-ins for the registry
key and the cmdlets. The command files themselves run for real under cmd.exe and powershell.exe in folders
under `tmp_path`, with a stand-in `ns.exe` (a copy of a venv's own `python.exe`, so it runs Python).
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import venv
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import psutil
import pytest
from typer.testing import CliRunner

from installer.stage2 import winenv
from installer.stage2.runner import RunResult, SubprocessRunner
from installer.stage2.simulate import Call, FakeRunner
from installer.stage2.steps.s09_shortcuts_scheduler import S09ShortcutsScheduler
from installer.stage2.testing import FakeUi, example_answers
from installer.stage2.web import answers_view, pathcheck, shortcuts_view
from installer.stage2.web import uninstall as web_uninstall
from ns_common import testguard
from ns_common.errors import describe
from ns_common.paths import Paths
from ns_supervisor import cli, doctor, update
from ns_supervisor.client import SupervisorClient
from ns_supervisor.testguard import RealMachineUnderTest
from tests.test_installer_uninstall import _path_probe
from tests.test_launcher_reach import Inst, inst  # noqa: F401  (the 0.1.14-updated install and its fixture)
from tests.test_nightly_optional import FakeWindows, windows  # noqa: F401  (the fake Task Scheduler)
from tests.test_ns_schedule import needs_powershell
from tests.test_second_install import install_at, missing_drive, said, second, uninstall_dry
from tests.test_setup_nightly_question import saved_answers, setup


def _module(name: str) -> Any:
    """A module this card adds; None on the base, so a test fails at its assertion and not at an import."""
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


commands: Any = _module("installer.stage2.commands")
NEW = "the base has no installer.stage2.commands"
ROOT = Path(__file__).resolve().parents[1]
SWITCH = "Make the Desktop shortcuts and the ns and studio commands open this install"
#: A home with every character a command file has to keep inside quotes; `pathcheck.home_problem` allows all.
HARD = "Jo's & (Co) ^x"
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="runs cmd.exe and powershell.exe")
PS = shutil.which("powershell")


def path_of(ctx: Any) -> str:
    return ctx.runner.user_env.get("Path", "")


def set_path(ctx: Any, value: str) -> None:
    ctx.runner.user_env["Path"] = value
    ctx.runner._save_user_env()


def writes(ctx: Any) -> list[Call]:
    return ctx.runner.called("ps:userpath-write")


def flags(ctx: Any) -> dict[str, Any]:
    return json.loads((ctx.paths.config / "wizard_flags.json").read_text(encoding="utf-8"))


def done_screen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ctx: Any) -> dict[str, Any]:
    """The setup window's Done screen (`controller._build_result`) over the flags this setup left."""
    from tests.test_installer_web_controller import Setup

    s = Setup(tmp_path / "ctl", monkeypatch)
    (s.home / "config").mkdir(parents=True, exist_ok=True)
    shutil.copy(ctx.paths.config / "wizard_flags.json", s.home / "config" / "wizard_flags.json")
    return s.ctl._build_result()


def with_commands(folder: Path) -> Path:
    """An existing install that ran this version's setup: its `cmd\\ns.cmd` names it (written by hand, the
    way setup writes its first lines, so the base reads the same file)."""
    install_at(folder)
    (folder / "cmd").mkdir()
    (folder / "cmd" / "ns.cmd").write_bytes(f'@echo off\r\nsetlocal\r\nset "NIGHTSHIFT_HOME={folder}"\r\n'.encode("ascii"))
    return folder


def mine_line(home: Path) -> str:
    return (
        "Commands: 'ns' and 'studio' work in any PowerShell or Command Prompt window you open from now on, "
        f"and open this install (their folder {home}\\cmd is on your PATH)."
    )


# ------------------------------------------------------------------------------------ setup: a fresh install
def test_setup_writes_both_shims_and_puts_their_folder_on_the_user_path(tmp_path: Path, windows: FakeWindows, monkeypatch: pytest.MonkeyPatch):
    ctx = second(tmp_path)
    before = r"C:\Tools;%USERPROFILE%\bin;;C:\Tools;"     # %VAR%, an empty entry, a duplicate, a trailing ';'
    set_path(ctx, before)
    text = setup(ctx)
    home = ctx.paths.home
    for name in ("ns.cmd", "studio.cmd"):
        assert (home / "cmd" / name).is_file(), f"setup wrote no {name}"
    assert commands is not None, NEW
    for name, venv_name, exe, package in commands.SHIMS:
        data = (home / "cmd" / name).read_bytes()
        assert data == commands.shim_text(home, venv_name, exe, ctx.app_path, package).encode("ascii")
        assert data.count(b"\r\n") == data.count(b"\n")                       # CRLF only
        assert f'set "NIGHTSHIFT_HOME={home}"'.encode() in data
    # appended once, after every byte that was there (the trailing ';' stays after it)
    assert path_of(ctx) == before + f"{home}\\cmd;"
    assert ctx.runner.user_env["Path:kind"] == "ExpandString" and len(writes(ctx)) == 1
    line = mine_line(home)
    assert said(text, "Commands:") == line and text.count(line) == 2            # the scheduler step and the summary
    assert ctx.flag("commands_owner") == "mine" and flags(ctx)["commands_line"] == line
    assert not [w for w in ctx.warnings if w[0] == "NS-W120"]
    assert done_screen(tmp_path, monkeypatch, ctx)["commandsLine"] == line
    # a second run (Repair) adds nothing: the entry is there, the files are this program's
    again = second(tmp_path, saved_answers(ctx.paths))
    setup(again)
    assert writes(again) == [] and path_of(again) == before + f"{home}\\cmd;"


def test_an_empty_path_gets_only_this_entry_and_a_string_path_stays_a_string(tmp_path: Path, windows: FakeWindows):
    assert commands is not None, NEW
    ctx = second(tmp_path)
    setup(ctx)
    assert path_of(ctx) == f"{ctx.paths.home}\\cmd"
    other = second(tmp_path / "s")
    set_path(other, r"C:\Tools")
    other.runner.user_env["Path:kind"] = "String"
    other.runner._save_user_env()
    setup(other)
    assert path_of(other) == rf"C:\Tools;{other.paths.home}\cmd" and other.runner.user_env["Path:kind"] == "String"


# --------------------------------------------------------------------------- the command files, for real
def stand_in_install(home: Path, *, exit_code: str = "0") -> Path:
    """An install folder whose `ns.exe` is a copy of a venv's own python.exe (it runs Python), with the
    program folder the command files look for."""
    (home / "config").mkdir(parents=True)
    (home / "config" / "runtime.json").write_text("{}", encoding="utf-8")
    for package in ("ns_supervisor", "ns_studio"):
        (home / "app" / package).mkdir(parents=True)
        (home / "app" / package / "__init__.py").write_text("", encoding="ascii")
    env_dir = home / "venvs" / "supervisor"
    venv.create(env_dir, with_pip=False)
    shutil.copy(env_dir / "Scripts" / "python.exe", env_dir / "Scripts" / "ns.exe")
    if commands is not None:
        assert commands.write_shims(home, home / "app") is None
    return home / "cmd" / "ns.cmd"


#: The stand-in program: prints the home it was given and its arguments, waits NS_SLEEP s, exits NS_EXIT.
SHOW = (
    "import json, os, sys, time\n"
    "print(os.environ.get('NIGHTSHIFT_HOME'))\n"
    "print(json.dumps(sys.argv[1:]))\n"
    "time.sleep(float(os.environ.get('NS_SLEEP', '0')))\n"
    "sys.exit(int(os.environ.get('NS_EXIT', '0')))\n"
)


def run_cmd(line: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """`line` exactly as typed at a Command Prompt (`cmd.exe /d /s /c "<line>"`)."""
    return subprocess.run(f'cmd.exe /d /s /c "{line}"', capture_output=True, text=True, timeout=120, env=env)


@windows_only
def test_a_shim_runs_its_own_install_whatever_nightshift_home_says(tmp_path: Path):
    home = tmp_path / HARD
    assert pathcheck.home_problem(str(home), 400) is None        # setup accepts this folder
    shim = stand_in_install(home)
    show = tmp_path / "show.py"
    show.write_text(SHOW, encoding="ascii")
    env = {**os.environ, "NIGHTSHIFT_HOME": str(tmp_path / "another install"), "NS_EXIT": "7"}
    # cmd.exe: quoted arguments keep spaces, & | and ^; % and ! are kept as they are
    proc = run_cmd(f'"{shim}" "{show}" "a b" "x&y" 100% hi! "p|q" "c^d"', env)
    out = proc.stdout.splitlines()
    assert out[:2] == [str(home), json.dumps(["a b", "x&y", "100%", "hi!", "p|q", "c^d"])], proc.stdout + proc.stderr
    assert proc.returncode == 7                                        # the program's exit code
    env["NS_EXIT"] = "0"
    assert run_cmd(f'"{shim}" "{show}"', env).returncode == 0
    # Windows PowerShell 5.1: the same, with its own quoting; exit code passed through
    assert PS is not None
    ps = (
        "& $env:NS_SHIM $env:NS_SHOW 'a b' '\"x&y\"' '100%' 'hi!' '\"c^d\"'; exit $LASTEXITCODE"
    )
    env.update(NS_SHIM=str(shim), NS_SHOW=str(show), NS_EXIT="7")
    proc = subprocess.run([PS, "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=120, env=env)
    out = proc.stdout.splitlines()
    assert out[:2] == [str(home), json.dumps(["a b", "x&y", "100%", "hi!", "c^d"])], proc.stdout + proc.stderr
    assert proc.returncode == 7
    # the stated limit (OPERATOR.md, "Commands"): PowerShell 5.1 hands a bare ^ to cmd.exe unquoted, which
    # takes it as its escape character before the command file runs; quoting it, as above, keeps it
    proc = subprocess.run([PS, "-NoProfile", "-NonInteractive", "-Command", "& $env:NS_SHIM $env:NS_SHOW 'c^d'"], capture_output=True, text=True, timeout=120, env=env)
    assert proc.stdout.splitlines()[1] == json.dumps(["cd"])


@windows_only
def test_a_shim_whose_program_is_gone_says_so(tmp_path: Path):
    home = tmp_path / HARD
    shim = stand_in_install(home)
    (home / "venvs" / "supervisor" / "Scripts" / "ns.exe").unlink()
    proc = run_cmd(f'"{shim}" doctor', dict(os.environ))
    assert proc.returncode == 9009
    assert proc.stdout.strip() == (
        f"Nightshift at {home}: its Python environment is missing. Run INSTALL.cmd from the Nightshift zip and "
        "choose this folder (Repair)."
    )
    # the program folder gone (an interrupted update or rollback): the launch script's words, not a traceback
    shutil.copy(home / "venvs" / "supervisor" / "Scripts" / "python.exe", home / "venvs" / "supervisor" / "Scripts" / "ns.exe")
    shutil.rmtree(home / "app")
    proc = run_cmd(f'"{shim}" doctor', dict(os.environ))
    assert proc.returncode == 9009 and "its program folder is missing or incomplete" in proc.stdout
    assert f"Nightshift at {home} cannot start" in proc.stdout and "Run INSTALL.cmd" in proc.stdout


@windows_only
def test_a_shim_rewritten_while_its_program_runs_is_not_read_again(tmp_path: Path):
    """`ns update` run from the command file rewrites that very file (the first start of the new version):
    cmd.exe must not read a line of the new file at the old offset when the program ends."""
    home = tmp_path / "home"
    shim = stand_in_install(home)
    swap = tmp_path / "swap.py"
    swap.write_text(
        "import os, pathlib, sys\n"
        "p = pathlib.Path(os.environ['NS_SHIM'])\n"
        "p.write_bytes(b'@echo off\\r\\n' + b'echo REWRITTEN\\r\\n' * 40)\n"
        "sys.exit(5)\n",
        encoding="ascii",
    )
    proc = run_cmd(f'"{shim}" "{swap}"', {**os.environ, "NS_SHIM": str(shim)})
    assert "REWRITTEN" not in proc.stdout + proc.stderr and proc.returncode == 5


@windows_only
def test_the_shim_is_not_a_holder_of_the_python_environments(tmp_path: Path):
    """D-39a: `ns update` waits while a program runs from `<home>\\venvs`. Through the command file it is the
    same program the launchers start (its `ns.exe`), and the cmd.exe that runs the file is not one."""
    home = tmp_path / "home"
    shim = stand_in_install(home)
    show = tmp_path / "show.py"
    show.write_text(SHOW, encoding="ascii")
    started = subprocess.Popen(f'cmd.exe /d /s /c ""{shim}" "{show}""', env={**os.environ, "NS_SLEEP": "30"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        found: list[Any] = []
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not found:
            found = update.holders(Paths(home))
            time.sleep(0.2)
        programs = [h.program.lower() for h in found]
        assert any(p.startswith("ns.exe") for p in programs), programs
        assert not any(p.startswith("cmd.exe") for p in programs), programs
        assert started.pid not in [h.pid for h in found]
    finally:
        for child in psutil.Process(started.pid).children(recursive=True):
            child.kill()
        started.kill()
        started.wait(30)


# ----------------------------------------------------------------------------------- two installs
def test_another_installs_commands_are_kept_and_said(tmp_path: Path, windows: FakeWindows, monkeypatch: pytest.MonkeyPatch):
    a = with_commands(tmp_path / "A")
    ctx = second(tmp_path)
    before = rf"C:\Tools;{a}\cmd"
    set_path(ctx, before)
    text = setup(ctx)
    b = ctx.paths.home
    assert path_of(ctx) == before and writes(ctx) == []                  # A's commands stay first: no write
    line = said(text, "Commands:")
    assert line.startswith(f"Commands: 'ns' and 'studio' open the Nightshift at {a} (NS-W120).")
    assert f"{b}\\cmd\\ns.cmd and {b}\\cmd\\studio.cmd: type the full path" in line and f"switch '{SWITCH}' on" in line
    assert text.count(line) == 2 and ("NS-W120", str(a)) in ctx.warnings
    assert describe("NS-W120").message == "The 'ns' and 'studio' commands do not open this install."
    assert ctx.flag("commands_owner") == f"other:{a}"
    assert (b / "cmd" / "ns.cmd").is_file()                               # this install's own still work by full path
    assert done_screen(tmp_path, monkeypatch, ctx)["commandsLine"] == line
    # the window offers the one switch for the commands alone, and says what on moves
    now = answers_view.folder_now(b, ROOT, None, None, before)["shortcutsNow"]
    assert now["state"] == "other" and now["path"] == str(a)
    assert now["line"] == f"The Desktop shortcuts are not another install's, but 'ns' and 'studio' open the Nightshift at {a}; on makes them open this install"
    # the console asks too (Enter is no)
    ui = FakeUi({"Make them open this install instead": "n"}, interactive=True)
    asked = second(tmp_path / "c", ui=ui)
    set_path(asked, before)
    setup(asked)
    assert f"'ns' and 'studio' open the Nightshift at {a}. Make them open this install instead?" in ui.asked
    assert path_of(asked) == before


def test_a_yes_moves_the_commands_and_says_from_where(tmp_path: Path, windows: FakeWindows):
    a = with_commands(tmp_path / "A")
    ctx = second(tmp_path, {**example_answers(install_code=False), "shortcuts_here": True})
    set_path(ctx, rf"C:\Tools;{a}\cmd;D:\x")
    text = setup(ctx)
    b = ctx.paths.home
    assert path_of(ctx) == rf"C:\Tools;D:\x;{b}\cmd" and len(writes(ctx)) == 1   # A's out, this one's in: one write
    assert said(text, "Commands:") == (
        f"Commands: 'ns' and 'studio' opened the Nightshift at {a}; now they open this install, as you chose "
        "(in windows you open from now on)."
    )
    assert ctx.flag("commands_owner") == "mine" and "shortcuts_here" not in saved_answers(ctx.paths)
    assert (a / "cmd" / "ns.cmd").is_file()                               # A's files are A's: never touched


def test_a_deleted_installs_entry_is_replaced_without_a_question(tmp_path: Path, windows: FakeWindows):
    gone = tmp_path / "deleted"                                            # its folder is gone, the drive is here
    ctx = second(tmp_path)
    listing = tmp_path / "local" / "installs.txt"
    listing.parent.mkdir()
    listing.write_text(f"{gone}\n", encoding="utf-8")
    ctx.installs_list = listing
    set_path(ctx, rf"{gone}\cmd;C:\Tools")
    text = setup(ctx)
    assert path_of(ctx) == rf"C:\Tools;{ctx.paths.home}\cmd"
    assert said(text, "Commands:") == mine_line(ctx.paths.home) and not [w for w in ctx.warnings if w[0] == "NS-W120"]


def test_an_entry_on_a_drive_that_is_not_there_is_kept_and_said(tmp_path: Path, windows: FakeWindows):
    away = missing_drive() + r"\Nightshift"
    ctx = second(tmp_path)
    listing = tmp_path / "local" / "installs.txt"
    listing.parent.mkdir()
    listing.write_text(f"{away}\n", encoding="utf-8")
    ctx.installs_list = listing
    set_path(ctx, rf"{away}\cmd")
    text = setup(ctx)
    assert path_of(ctx) == rf"{away}\cmd" and writes(ctx) == []
    line = said(text, "Commands:")
    assert line.startswith(f"Commands: 'ns' and 'studio' belong to the Nightshift at {away}, whose drive is not there now (NS-W120).")
    assert ctx.flag("commands_owner") == f"other:{away}"


# ------------------------------------------------------------------------------- failed, killed, Repair
def test_a_path_that_cannot_be_written_does_not_stop_setup(tmp_path: Path, windows: FakeWindows):
    ctx = second(tmp_path)
    ctx.runner.results["ps:userpath-write"] = RunResult(1, "", "Requested registry access is not allowed.")
    text = setup(ctx)                                                      # verify passes: the step is done
    home = ctx.paths.home
    line = said(text, "Commands:")
    assert line == (
        "Commands: 'ns' and 'studio' could not be put on your PATH (Windows refused the change: Requested "
        f"registry access is not allowed.) (NS-W120). Type {home}\\cmd\\ns.cmd and {home}\\cmd\\studio.cmd "
        "instead, or run INSTALL.cmd again and choose Repair."
    )
    assert ("NS-W120", "not on your PATH") in ctx.warnings and path_of(ctx) == ""
    assert (home / "cmd" / "ns.cmd").is_file() and ctx.state.status("s09_shortcuts_scheduler") == "done"
    # retried: the next Repair finds the entry missing, runs the step again and writes it
    again = second(tmp_path, saved_answers(ctx.paths))
    assert S09ShortcutsScheduler().check(again) is False
    assert said(setup(again), "Commands:") == mine_line(home) and path_of(again) == f"{home}\\cmd"
    # Windows does not answer the read: nothing is written on a guess, and it is said
    unread = second(tmp_path / "u")
    unread.runner.results["ps:userpath-read"] = RunResult(1, "", "")
    assert "(Windows did not say what your PATH holds) (NS-W120)" in said(setup(unread), "Commands:")
    assert writes(unread) == []


def test_a_repair_changes_nothing_and_a_kill_repeats_the_same_choice(tmp_path: Path, windows: FakeWindows):
    ctx = second(tmp_path)
    setup(ctx)
    home = ctx.paths.home
    repair = second(tmp_path, saved_answers(ctx.paths))
    assert S09ShortcutsScheduler().check(repair) is True                   # files and entry as left: nothing to do
    (home / "cmd" / "studio.cmd").unlink()
    assert S09ShortcutsScheduler().check(repair) is False                  # a file went: the step runs again
    setup(repair)
    assert (home / "cmd" / "studio.cmd").is_file() and writes(repair) == []
    # a kill between the PATH write and the flags, with a yes over another install's commands
    a = with_commands(tmp_path / "A")
    yes = second(tmp_path / "y", {**example_answers(install_code=False), "shortcuts_here": True})
    set_path(yes, rf"{a}\cmd")

    def write_then_die(call: Call) -> RunResult:
        FakeRunner._default(yes.runner, call)
        raise KeyboardInterrupt

    yes.runner.results["ps:userpath-write"] = write_then_die
    with pytest.raises(KeyboardInterrupt):
        setup(yes, only=("s01", "s09"))
    b = yes.paths.home
    assert path_of(yes) == f"{b}\\cmd" and saved_answers(yes.paths).get("shortcuts_here") is True
    rerun = second(tmp_path / "y", saved_answers(yes.paths))
    text = setup(rerun)
    assert writes(rerun) == [] and path_of(rerun) == f"{b}\\cmd"            # the rerun finds it done
    assert said(text, "Commands:").startswith(f"Commands: 'ns' and 'studio' opened the Nightshift at {a}; now they open this install")
    assert "shortcuts_here" not in saved_answers(rerun.paths) and rerun.flag("commands_taking") is None


def test_a_path_another_program_changes_between_the_read_and_the_write_is_read_again(tmp_path: Path, windows: FakeWindows):
    """The write is refused when the PATH no longer holds what was read (an installer running beside setup):
    read and planned again, once; nothing that program added is lost. Twice changed: said, nothing written."""
    ctx = second(tmp_path)
    set_path(ctx, r"C:\Tools")
    seen: list[str] = []

    def another_program_first(call: Call) -> RunResult:
        seen.append(call.env["NS_PATH_NEW"])
        if len(seen) == 1:
            set_path(ctx, r"C:\Tools;E:\Their\bin")                    # the other program's write lands first
        return FakeRunner._default(ctx.runner, call)

    ctx.runner.results["ps:userpath-write"] = another_program_first
    text = setup(ctx)
    home = ctx.paths.home
    assert path_of(ctx) == rf"C:\Tools;E:\Their\bin;{home}\cmd" and len(seen) == 2
    assert said(text, "Commands:") == mine_line(home)
    busy = second(tmp_path / "busy")
    set_path(busy, r"C:\Tools")
    busy.runner.results["ps:userpath-write"] = RunResult(3, "changed\n")
    line = said(setup(busy), "Commands:")
    assert "(another program changed your PATH at the same moment, twice) (NS-W120)" in line
    assert len(writes(busy)) == 2 and path_of(busy) == r"C:\Tools"


# ---------------------------------------------------------------------------------- the PATH itself
def test_a_path_near_the_limits_is_never_cut(tmp_path: Path, windows: FakeWindows):
    assert commands is not None, NEW
    home = tmp_path / "home"
    entry = f"{home}\\cmd"
    room = commands.MAX_USER_PATH - len(entry) - 1
    exact = "C:\\" + "x" * (room - 3)                                     # with this install's entry: 2047 exactly
    fits = commands.plan(exact, home, False)
    assert fits.new_value == f"{exact};{entry}" and len(fits.new_value) == commands.MAX_USER_PATH
    over = commands.plan(exact + "y", home, False)                         # 2048: refused, nothing cut
    assert over.new_value is None and "it would grow to 2048 characters, past the 2047" in str(over.refusal)
    total = commands.plan(r"C:\Tools", home, False, machine_len=commands.MAX_ENV - 10)
    assert total.new_value is None and "past the 32767 Windows allows" in str(total.refusal)
    # the step: the PATH stays byte for byte, and the owner reads why
    ctx = second(tmp_path)
    long = "C:\\" + "y" * (commands.MAX_USER_PATH - 3)
    set_path(ctx, long)
    line = said(setup(ctx), "Commands:")
    assert path_of(ctx) == long and writes(ctx) == []
    assert "could not be put on your PATH (it would grow to" in line and "(NS-W120)" in line and "Repair" in line


def test_the_path_keeps_every_other_byte_from_setup_to_uninstall(tmp_path: Path):
    """What setup adds, the uninstaller takes out again, and the value is exactly what it was: empty entries,
    a trailing ';', duplicates, an entry that differs only in case or a trailing backslash, %VAR% entries."""
    assert commands is not None, NEW
    home = tmp_path / "ns-home"
    for before in (r"C:\Tools;;%SystemRoot%\system32;C:\Tools;c:\tools\;D:\x;", r"C:\Tools;%X%\y", ""):
        added = commands.plan(before, home, False).new_value
        assert added is not None and f"{home}\\cmd" in commands.split(added)
        out = _path_probe(tmp_path, home, added, directory=home / "cmd")
        assert f"path={before}" in out and "result=True" in out and "kind=ExpandString" in out
    out = _path_probe(tmp_path, home, rf"C:\a;{home}\cmd;C:\b", kind="String", directory=home / "cmd")
    assert r"path=C:\a;C:\b" in out and "kind=String" in out               # REG_SZ stays REG_SZ
    out = _path_probe(tmp_path, home, r"C:\a;D:\other\cmd", directory=home / "cmd")
    assert "result=False" in out and "writes=0" in out                     # another folder's entry: nothing written


@needs_powershell
def test_the_write_script_writes_only_what_was_read_and_keeps_the_kind(tmp_path: Path):
    """Setup's PATH write under Windows PowerShell 5.1, with the registry cmdlets replaced by functions of the
    script itself and its key replaced by a folder: the real registry cannot be reached by this test."""
    assert hasattr(winenv, "set_user_path"), NEW
    body = winenv._WRITE_USER_PATH.replace("HKCU:\\Environment", str(tmp_path / "not-the-registry"))
    assert "HKCU" not in body
    stand_ins = "\n".join([
        "function Get-Item { param([string]$LiteralPath) $o = New-Object psobject; "
        "$o | Add-Member ScriptMethod GetValue { param($n, $d, $x) [string]$env:NS_FAKE_NOW }; $o }",
        "function Set-ItemProperty { param([string]$LiteralPath, [string]$Name, $Value, [string]$Type) "
        "Write-Output ('SET ' + $Name + '|' + $Type + '|' + $Value) }",
        "function Add-Type { param($Namespace, $Name, $MemberDefinition) Write-Output ('TELL ' + $MemberDefinition); throw 'no broadcast in a test' }",
    ])
    script = tmp_path / "write.ps1"
    script.write_text(stand_ins + "\n" + body + "\n", encoding="ascii")

    def run(now: str, old: str, new: str, kind: str) -> subprocess.CompletedProcess[str]:
        env = {**os.environ, "NS_FAKE_NOW": now, "NS_PATH_OLD": old, "NS_PATH_NEW": new, "NS_PATH_KIND": kind}
        return subprocess.run([str(PS), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)], capture_output=True, text=True, timeout=120, env=env)

    done = run(r"C:\a;%X%", r"C:\a;%X%", r"C:\a;%X%;D:\h\cmd", "ExpandString")
    assert done.returncode == 0 and r"SET Path|ExpandString|C:\a;%X%;D:\h\cmd" in done.stdout and "written" in done.stdout
    assert "TELL" in done.stdout and "SendMessageTimeout" in done.stdout   # running programs are told (WM_SETTINGCHANGE)
    assert "SET Path|String|" in run("x", "x", "x;y", "String").stdout
    moved = run(r"C:\a;C:\new", r"C:\a", r"C:\a;D:\h\cmd", "ExpandString")  # another program wrote meanwhile
    assert moved.returncode == 3 and "SET" not in moved.stdout


def test_a_test_run_never_reaches_the_real_path(tmp_path: Path):
    assert hasattr(winenv, "set_user_path"), NEW
    with pytest.raises(RealMachineUnderTest):
        winenv.set_user_path(SubprocessRunner(), "", "none", r"D:\x\cmd")
    seen = FakeRunner(tmp_path / "desktop")
    winenv.set_user_path(seen, "", "none", r"D:\x\cmd")
    with pytest.raises(RealMachineUnderTest):                              # the runner's own guard refuses it too
        testguard.refuse_machine_writes(seen.calls[-1].argv)


# ------------------------------------------------------------------- update, rollback, dev install
@pytest.fixture
def plain_inst(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Inst:
    """launcher-reach's install as 0.1.14's updater leaves it, in a folder setup accepts (plain ASCII)."""
    import tests.test_launcher_reach as reach_tests

    monkeypatch.setattr(reach_tests, "HARD", "Jo's Nightshift (2) & co")
    return request.getfixturevalue("inst")


def test_a_home_a_command_file_cannot_name_is_said_and_never_put_on_the_path(inst: Inst):
    """launcher-reach's own install has Arabic letters in its folder name (only a hand-set NIGHTSHIFT_HOME can
    give one: setup refuses it). No command file is written for it and the PATH is not touched."""
    assert commands is not None, NEW
    marker = inst.run()
    assert str(marker["commands"]).startswith("unfit: this folder's path has characters a command file cannot hold")
    from ns_supervisor import notify

    told = [n["message"] for n in notify.read_notes(inst.paths) if n["title"] == "Nightshift commands"]
    assert len(told) == 1 and "cannot be made for this install" in told[0] and "Start Nightshift from its shortcuts" in told[0]
    assert not (inst.home / "cmd").exists() and not [c for c in inst.shell.calls if c[0].startswith("ps:userpath")]
    assert marker["failed"] == []
    file = inst.paths.home / "run" / "launch-reach.json"
    seen = file.stat().st_mtime_ns
    inst.run()                                                             # settled: a later start writes nothing
    assert file.stat().st_mtime_ns == seen


def test_the_first_start_of_a_new_version_brings_the_commands(plain_inst: Inst):
    """Most installs arrive by update. 0.1.14's updater never runs setup: the engine's first start of the new
    program folder writes the command files and the PATH entry; a later start does nothing; a start of a
    newer program folder (this version's own updater) rewrites a file only when its text changed."""
    from ns_supervisor import launch_reach

    inst = plain_inst

    assert commands is not None, NEW
    inst.shell.user_path = r"C:\Tools;"
    marker = inst.run()
    assert (inst.home / "cmd" / "ns.cmd").is_file() and (inst.home / "cmd" / "studio.cmd").is_file()
    assert inst.shell.user_path == rf"C:\Tools;{inst.home}\cmd;" and marker["commands"] == "mine"
    assert marker["failed"] == []                                          # not one of the launch-script failures
    calls = len(inst.shell.calls)
    inst.run()
    assert len(inst.shell.calls) == calls                                  # settled: no PowerShell at a later start
    shim = inst.home / "cmd" / "ns.cmd"
    stamp = shim.stat().st_mtime_ns
    inst.new_stamp()                                                       # an update put a new program folder
    inst.run()
    assert shim.stat().st_mtime_ns == stamp and inst.shell.user_path == rf"C:\Tools;{inst.home}\cmd;"
    # a marker written by the version before this one (no "commands"): the next start does the step
    data = json.loads(launch_reach.marker_file(inst.paths).read_text(encoding="utf-8"))
    data.pop("commands")
    launch_reach.marker_file(inst.paths).write_text(json.dumps(data), encoding="utf-8")
    shutil.rmtree(inst.home / "cmd")
    assert inst.run()["commands"] == "mine" and shim.is_file()
    # another existing install's commands come first: the reach never takes them (that needs a yes in setup)
    a = with_commands(inst.home.parent / "A")
    inst.shell.user_path = rf"{a}\cmd"
    data = json.loads(launch_reach.marker_file(inst.paths).read_text(encoding="utf-8"))
    data.pop("commands")
    launch_reach.marker_file(inst.paths).write_text(json.dumps(data), encoding="utf-8")
    assert inst.run()["commands"] == f"other:{a}" and inst.shell.user_path == rf"{a}\cmd"


def test_a_rollback_to_0_1_14_keeps_the_commands_working(tmp_path: Path):
    """0.1.14 has the same `venvs\\supervisor\\Scripts\\ns.exe` and `app\\ns_supervisor`: the command files a
    newer version wrote start it unchanged (they name nothing that only a newer version has)."""
    assert commands is not None, NEW
    home = tmp_path / "home"
    text = commands.shim_text(home, "supervisor", "ns.exe", home / "app", "ns_supervisor")
    named = set(re.findall(r'"([^"]+)"', text))
    assert named == {
        f"NIGHTSHIFT_HOME={home}", str(home / "venvs" / "supervisor" / "Scripts" / "ns.exe"),
        str(home / "app" / "ns_supervisor" / "__init__.py"),
    }


def test_a_dev_install_never_writes_the_path(tmp_path: Path, windows: FakeWindows, plain_inst: Inst):
    inst = plain_inst
    ctx = second(tmp_path, dev=True)
    set_path(ctx, r"C:\Tools")
    text = setup(ctx)
    home = ctx.paths.home
    assert (home / "cmd" / "ns.cmd").is_file() and path_of(ctx) == r"C:\Tools" and writes(ctx) == []
    assert said(text, "Commands:") == (
        f"Commands: a dev install does not put 'ns' and 'studio' on your PATH; type {home}\\cmd\\ns.cmd and "
        f"{home}\\cmd\\studio.cmd."
    )
    assert S09ShortcutsScheduler().check(second(tmp_path, saved_answers(ctx.paths), dev=True)) is True
    # the engine's first start of a dev install: the files, never the PATH
    inst.paths.runtime_file.write_text(json.dumps({"dev_mode": True}), encoding="utf-8")
    inst.shell.user_path = r"C:\Tools"
    assert inst.run()["commands"] == "dev" and inst.shell.user_path == r"C:\Tools"
    assert not [c for c in inst.shell.calls if c[0].startswith("ps:userpath")]


# --------------------------------------------------------------------------------------- ns doctor
def test_doctor_says_which_install_the_commands_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    paths = Paths(home)
    paths.ensure()
    for venv_name, exe in (("supervisor", "ns.exe"), ("studio", "studio.exe")):
        (home / "venvs" / venv_name / "Scripts").mkdir(parents=True)
        (home / "venvs" / venv_name / "Scripts" / exe).write_bytes(b"")
    if commands is not None:
        commands.write_shims(home, ROOT)
    row = getattr(doctor, "commands_row", None)
    assert row is not None, "the base doctor has no row 'commands'"

    def seen(user: str, machine: str = "") -> tuple[bool, str]:
        monkeypatch.setenv("NS_TEST_USER_PATH", user)
        monkeypatch.setenv("NS_TEST_MACHINE_PATH", machine)
        _, ok, detail = row(paths)
        return ok, detail

    assert seen(rf"C:\Tools;{home}\cmd") == (True, "'ns' and 'studio' open this install")
    a = with_commands(tmp_path / "A")
    assert seen(rf"{a}\cmd;{home}\cmd") == (True, f"WARN: 'ns' and 'studio' open the Nightshift at {a} (NS-W120).")
    assert seen(r"C:\Tools") == (True, "WARN: not on your PATH (NS-W120): run INSTALL.cmd and choose Repair")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "studio.cmd").write_text("@echo off\r\n", encoding="ascii")
    assert seen(rf"{home}\cmd", str(tools)) == (True, f"WARN: 'studio' runs {tools / 'studio.cmd'} first: rename or remove it from your PATH")
    (home / "venvs" / "studio" / "Scripts" / "studio.exe").unlink()
    assert seen(rf"{home}\cmd") == (False, f"Nightshift at {home}: its Python environment is missing. Run INSTALL.cmd from the Nightshift zip and choose this folder (Repair).")
    # the row is one of `ns doctor`'s, and the terminal prints a WARN row as [WARN] (not a failure)
    (home / "venvs" / "studio" / "Scripts" / "studio.exe").write_bytes(b"")
    monkeypatch.setenv("NS_TEST_USER_PATH", r"C:\Tools")
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(home))
    client = SupervisorClient("http://sup", client=httpx.Client(transport=httpx.MockTransport(refuse)))
    rows = {name: (ok, detail) for name, ok, detail in doctor.diagnose(paths, client)}
    assert rows["commands"] == (True, "WARN: not on your PATH (NS-W120): run INSTALL.cmd and choose Repair")
    monkeypatch.setattr(doctor, "diagnose", lambda p, _client, on_wait=None: [row(p)])
    res = CliRunner().invoke(cli.app, ["doctor"])
    assert "[WARN] commands: not on your PATH (NS-W120): run INSTALL.cmd and choose Repair" in res.output
    assert res.exit_code == 0


def refuse(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("no engine")


# ------------------------------------------------------------------------------------ the uninstaller
@needs_powershell
def test_uninstall_removes_only_its_own_entry_and_folder(tmp_path: Path):
    a = with_commands(tmp_path / "A")
    b = with_commands(tmp_path / "B")
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    machine = {"task": None, "userHome": "", "toastKey": False, "installs": [str(a), str(b)],
               "userPath": rf"C:\Tools;{a}\cmd;{b}\cmd;"}
    proc = uninstall_dry(b, desktop, machine)
    out = proc.stdout
    assert proc.returncode == 0, out + proc.stderr
    assert f"[dry-run] remove {b}\\cmd from the user PATH" in out and f"[dry-run] delete folder {b}\\cmd" in out
    assert f"{a}\\cmd from the user PATH" not in out and f"delete folder {a}\\cmd" not in out
    # the plain commands were B's: the line says how A gets them, and A's own files by their full path
    assert (
        f"The plain 'ns' and 'studio' commands opened this install and are no longer on your PATH. The Nightshift at {a} "
        f"has its own: {a}\\cmd\\ns.cmd and studio.cmd. To make the plain commands open it, run INSTALL.cmd in {a}\\app"
    ) in out
    # the window lists that line among what was left, and its deferred cleanup removes the folder
    app = web_uninstall.UninstallApp(ROOT, b, popen=lambda *_a, **_k: Script(out.splitlines()), deferred=lambda _h: True, env_var=lambda _n: None)
    app._work(False, False)
    assert any(k["label"].startswith("The plain 'ns' and 'studio' commands") for k in app.view()["result"]["kept"])
    assert f"'{b}\\cmd'" in web_uninstall.deferred_script(b, [])


class Script:
    """A finished uninstall process that printed `lines`."""

    def __init__(self, lines: list[str]) -> None:
        self.stdout = iter(line + "\n" for line in lines)

    def wait(self) -> int:
        return 0


# --------------------------------------------------------------------------------- the words of the hints
def test_the_one_switch_names_the_commands_everywhere():
    assert shortcuts_view.SWITCH == SWITCH
    assert f"switch '{SWITCH}' on" in describe("NS-W119").hint and f"switch '{SWITCH}' on" in describe("NS-W120").hint
    copy_ts = (ROOT / "installer" / "ui" / "src" / "copy.ts").read_text(encoding="utf-8")
    assert f'shortcutsHere: "{SWITCH}"' in copy_ts
    operator = (ROOT / "installer" / "stage2" / "web" / "operator_copy.json").read_text(encoding="utf-8")
    assert SWITCH in json.loads(operator)["NS-W119"]["whatToDo"] and "NS-W120" in json.loads(operator)
    done = (ROOT / "installer" / "ui" / "src" / "screens" / "Done.tsx").read_text(encoding="utf-8")
    assert "r.commandsLine" in done


def test_the_elevated_powershell_hints_say_the_same_thing_in_the_same_form():
    """FN-4's review: NS-E204, NS-W205, NS-W206 and the Settings row for NS-W205 give `ns schedule install`
    the same way, now that it works from any new terminal (FN-3)."""
    form = "'ns schedule install' from an elevated PowerShell"
    for code in ("NS-E204", "NS-W205", "NS-W206"):
        assert form in describe(code).hint, code
    assert "a plain ns is not found" not in describe("NS-E204").hint
    settings = (ROOT / "ns_dashboard" / "ui" / "src" / "pages" / "Settings.tsx").read_text(encoding="utf-8")
    # the Settings row takes the command as this install's owner types it (the engine serves it, FN-3 review)
    assert "run '${s.schedule_install_command ?? \"ns schedule install\"}' once from an elevated PowerShell" in settings
    operator = (ROOT / "OPERATOR.md").read_text(encoding="utf-8")
    assert "put it in quotes twice" in operator and "odd number of double quotes" in operator


# ---------------------------------------------------------------------- FN-3 review: what a plain ns reaches
def stand_alone(home: Path) -> Path:
    """An install folder with both command files (and no venv: the files are what a window finds)."""
    (home / "config").mkdir(parents=True)
    (home / "config" / "runtime.json").write_text("{}", encoding="utf-8")
    assert commands is not None, NEW
    assert commands.write_shims(home, home / "app") is None
    return home


def two_installs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A with its command files first on the user PATH; B, this process's install, with its own files but no
    entry (its owner left the switch off): a plain `ns` in a new window runs A's."""
    a = stand_alone(tmp_path / "A")
    b = stand_alone(tmp_path / "B")
    monkeypatch.setenv("NS_TEST_USER_PATH", rf"C:\Tools;{a}\cmd")
    monkeypatch.setenv("NS_TEST_MACHINE_PATH", "")
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(b))
    return a, b


def test_an_update_way_out_names_this_installs_own_command_when_another_install_has_the_plain_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """B's failed update must not tell its owner to type a plain `ns update --rollback --detach`: with A's
    commands first that rolls A back. The rule (FN-3 review of b6b2394): every text is STORED with the plain
    words (what a plain `ns` reaches changes after it is written, and once-only checks compare the words) and
    RENDERED where it is shown: the CLI, the dashboard's answers (notes, job card, error, update state), the
    toast. Stored: the update's way out, an error's hint and detail, a studio error, a morning note, the
    update's state file."""
    commandline = _module("ns_common.commandline")
    assert commandline is not None, "the base has no ns_common.commandline"
    assert hasattr(commandline, "command_form"), "the base has no commandline.command_form"
    from ns_common import errors
    from ns_common.errors import NsError
    from ns_dashboard import updater
    from ns_dashboard.errors import ApiError
    from ns_dashboard.jobs import JobRecord
    from ns_dashboard.svc_engine import EngineService
    from ns_studio.errors import StudioError
    from ns_supervisor import notify, update_job

    a, b = two_installs(tmp_path, monkeypatch)
    ns_form = commandline.command_form(f"{b}\\cmd\\ns.cmd")
    studio_form = commandline.command_form(f"{b}\\cmd\\studio.cmd")
    assert commandline.reach(b).ok is False and commandline.reach(b).other == str(a)
    assert commandline.typed(b, "ns update --rollback --detach") == f"{ns_form} update --rollback --detach"
    # stored: the plain words
    monkeypatch.setattr(update, "takes_detach", lambda _app: True)
    way = update.rollback_way_out(Paths(b), b / "app", "0.1.15", "0.1.14")
    assert "run 'ns update --rollback --detach'" in way
    assert "'ns update <zip> --detach'" in update.update_way_out(Paths(b), b / "app", "0.1.15", "0.1.14")
    assert describe("NS-E202").hint == (     # (model-file-damaged: the hint names the model rows and Repair)
        "Run 'ns doctor': it checks the model files and the logs. If it names a damaged or missing model, run "
        "INSTALL.cmd again and choose Repair."
    )
    err = NsError("NS-E702", "Run ns update --rollback again to go back.")
    assert err.detail == "Run ns update --rollback again to go back." and "Run 'ns doctor'" not in str(err)
    assert StudioError("NS-E324", "run 'studio calibrate' first").detail == "run 'studio calibrate' first"
    paths = Paths(b)
    notify.leave_note(paths, "Nightshift", "continue it with studio run --resume <id>")
    on_disk = json.loads(notify.note_path(paths).read_text(encoding="utf-8"))["notes"]
    assert on_disk == [{"at": on_disk[0]["at"], "title": "Nightshift", "message": "continue it with studio run --resume <id>"}]
    (b / "run").mkdir(parents=True, exist_ok=True)
    update_job.status_file(paths).write_text(json.dumps({"state": "running", "pid": os.getpid(), "log": "x.log"}), encoding="utf-8")
    update_job.step(paths, "Refreshing the Python environments.", way)
    state = json.loads(update_job.status_file(paths).read_text(encoding="utf-8"))
    assert state["if_stopped_here"] == way                                   # the update's state file: plain
    # shown: B's own command files by full path, on every route that shows a text
    assert f"run '{ns_form} update --rollback --detach'" in commandline.render(way, b)
    assert f"Run '{ns_form} doctor'" in errors.describe_shown("NS-E202").hint
    assert f"run {ns_form} update again" in errors.describe_shown("NS-E124").hint   # unquoted words too
    assert f"{studio_form} channel edit" in errors.describe_shown("NS-E320").hint
    shown = notify.shown_notes(paths)
    assert shown[0]["message"] == f"continue it with {studio_form} run --resume <id>"
    assert shown[0]["id"] == notify.read_notes(paths)[0]["id"]            # the bell dismisses the stored note
    assert EngineService.notes(cast(Any, SimpleNamespace(s=SimpleNamespace(paths=paths)))) == shown
    body = ApiError(409, err.code, str(err), hint=err.definition.hint).to_body()["error"]
    assert f"Run {ns_form} update --rollback again" in body["message"]
    card = JobRecord("j1", "run_now", None, "failed", 0.0, err.detail, "t", None, None,
                     result={"error": {"code": "NS-E202", "hint": describe("NS-E202").hint}}).to_dict()
    assert card["message"].startswith(f"Run {ns_form} update --rollback") and f"'{ns_form} doctor'" in card["result"]["error"]["hint"]
    state["message"] = err.detail
    update_job.status_file(paths).write_text(json.dumps(state), encoding="utf-8")
    assert updater.read_update_state(paths, alive=lambda _p: True)["message"] == f"Run {ns_form} update --rollback again to go back."
    echoed: list[str] = []
    monkeypatch.setattr(cli.typer, "echo", lambda text, **_k: echoed.append(text))
    cli.echo(err.detail)
    assert echoed == [f"Run {ns_form} update --rollback again to go back."]
    # B's commands reach B again (its entry first): every text keeps its plain words, the stored note too
    monkeypatch.setenv("NS_TEST_USER_PATH", rf"{b}\cmd;{a}\cmd")
    assert commandline.render(way, b) == way and errors.describe_shown("NS-E202").hint == describe("NS-E202").hint
    assert notify.shown_notes(paths)[0]["message"] == "continue it with studio run --resume <id>"
    assert commandline.typed(b, "ns update --rollback --detach") == "ns update --rollback --detach"


def test_render_gives_the_full_path_when_what_a_plain_command_reaches_is_not_known_and_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Not known (Windows does not say what the user PATH holds): the full path, which is right whatever the
    PATH holds. No home: the plain words. An error while rendering an error never hides it."""
    commandline = _module("ns_common.commandline")
    assert commandline is not None, "the base has no ns_common.commandline"
    assert hasattr(commandline, "TEST_UNKNOWN"), "the base has no stand-in for a PATH Windows does not tell"
    from ns_common import errors

    home = stand_alone(tmp_path / "B")
    form = commandline.command_form(f"{home}\\cmd\\ns.cmd")
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(home))
    monkeypatch.setenv("NS_TEST_USER_PATH", commandline.TEST_UNKNOWN)
    assert commandline.reach(home).ok is None
    assert commandline.render("Run 'ns doctor'.") == f"Run '{form} doctor'."
    assert commandline.typed(home, "ns doctor") == f"{form} doctor"
    assert f"'{form} doctor'" in errors.describe_shown("NS-E202").hint
    monkeypatch.delenv("NIGHTSHIFT_HOME")                                   # no home: nothing else to give
    assert commandline.render("Run 'ns doctor'.") == "Run 'ns doctor'."

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("a bug while rendering")

    monkeypatch.setattr(commandline, "reach_now", boom)                     # the full path, never a raise
    assert commandline.render("Run 'ns doctor'.", home) == f"Run '{form} doctor'."
    assert commandline.typed(home, "ns doctor") == f"{form} doctor"
    monkeypatch.setattr(commandline, "own_file", boom)                      # even that fails: the text
    assert commandline.render("Run 'ns doctor'.", home) == "Run 'ns doctor'."
    assert commandline.typed(home, "ns doctor") == "ns doctor"
    assert commandline.render(cast(Any, None), home) is None


@windows_only
def test_a_full_path_with_a_space_is_given_in_a_form_a_command_prompt_and_powershell_both_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The shown command, typed as it is, runs this install's program in cmd.exe and in Windows PowerShell 5.1
    (a home with a space, an apostrophe, & ( ) ^). The forms it replaced do not: PowerShell's `& "..."` is
    cmd.exe's "& was unexpected", the plain quoted path is PowerShell's "Unexpected token"."""
    import base64

    commandline = _module("ns_common.commandline")
    assert commandline is not None, "the base has no ns_common.commandline"
    assert hasattr(commandline, "command_form"), "the base has no commandline.command_form"
    home = tmp_path / HARD
    shim = stand_in_install(home)
    show = tmp_path / "show.py"
    show.write_text(SHOW, encoding="ascii")
    monkeypatch.setenv("NS_TEST_USER_PATH", "")                             # known: nothing reaches this install
    form = commandline.command_form(str(shim))
    assert form == f'{str(shim)[:2]}"{str(shim)[2:]}"'
    assert commandline.render("run 'ns doctor --bundle' and send it", home) == f"run '{form} doctor --bundle' and send it"
    typed_line = f'{form} "{show}" doctor --bundle'           # the shown command, the stand-in's script first
    want = [str(home), json.dumps(["doctor", "--bundle"])]
    env = {**os.environ, "NS_EXIT": "0", "NIGHTSHIFT_HOME": str(tmp_path / "another install")}

    def powershell(line: str) -> subprocess.CompletedProcess[str]:
        encoded = base64.b64encode(line.encode("utf-16-le")).decode("ascii")     # the line byte for byte
        assert PS is not None
        return subprocess.run([PS, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], capture_output=True, text=True, timeout=120, env=env)

    proc = run_cmd(typed_line, env)
    assert proc.stdout.splitlines()[:2] == want, proc.stdout + proc.stderr
    proc = powershell(typed_line)
    assert proc.stdout.splitlines()[:2] == want, proc.stdout + proc.stderr
    assert run_cmd(f'& "{shim}" "{show}" doctor', env).stdout.splitlines()[:1] != [str(home)]
    assert "Unexpected token" in powershell(f'"{shim}" "{show}" doctor').stderr
    # a $ or a backtick (setup allows both): PowerShell's single-quoted form, its quote doubled
    assert commandline.command_form("D:\\a$b 'c\\cmd\\ns.cmd") == "& 'D:\\a$b ''c\\cmd\\ns.cmd'"
    assert commandline.command_form("D:\\Nightshift\\cmd\\ns.cmd") == "D:\\Nightshift\\cmd\\ns.cmd"


@needs_powershell
def test_the_windows_without_python_show_the_same_full_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The Update window (launchers/update.ps1) and the update's progress window show what the update's state
    and log files hold, with no Python: `Format-NsCommands` gives the full path always, in render's form."""
    import base64

    commandline = _module("ns_common.commandline")
    assert commandline is not None, "the base has no ns_common.commandline"
    assert hasattr(commandline, "ps_formatter"), "the base has no PowerShell formatter"
    from ns_supervisor import update_job

    monkeypatch.setenv("NS_TEST_USER_PATH", commandline.TEST_UNKNOWN)
    text = "Run 'ns update --rollback --detach', then studio run --resume <id>; ns.cmd and a word ns stay"
    script = commandline.ps_formatter() + "\nWrite-Output (Format-NsCommands $env:NS_T $env:NS_H)\n"
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    assert PS is not None
    for home in (stand_alone(tmp_path / HARD), tmp_path / "no files" / "home"):   # command files; the venv program
        env = {**os.environ, "NS_T": text, "NS_H": str(home)}
        proc = subprocess.run([PS, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded], capture_output=True, text=True, timeout=120, env=env)
        assert proc.stdout.rstrip("\r\n") == commandline.render(text, home) != text, proc.stdout + proc.stderr
    common = (ROOT / "launchers" / "_common.ps1").read_text(encoding="utf-8").replace("\r\n", "\n")
    assert commandline.ps_formatter() in common                              # the launchers' copy is this one
    follower = update_job.follower_script(Path("s.json"), Path("u.log"), 1, home=tmp_path)
    assert commandline.ps_formatter() in follower and follower.isascii()
    assert "Format-NsCommands ([string]$state.if_stopped_here) $nsHome" in follower
    shortcut = (ROOT / "launchers" / "update.ps1").read_text(encoding="utf-8")
    for shown in ("Format-NsCommands $Line $nsHome", "Format-NsCommands ([string]$state.if_stopped_here) $nsHome",
                  "Format-NsCommands ([string]$final.message) $nsHome"):
        assert shown in shortcut, shown


def test_the_command_table_is_the_clis_own():
    import click
    import typer.main

    commandline = _module("ns_common.commandline")
    assert commandline is not None, "the base has no ns_common.commandline"
    from ns_studio.cli import app as studio_app
    from ns_supervisor.cli import app as ns_app

    for tool, app in (("ns", ns_app), ("studio", studio_app)):
        group: Any = typer.main.get_command(app)
        ctx = click.Context(group)
        seen: dict[str, tuple[str, ...]] = {}
        for name in group.list_commands(ctx):
            cmd: Any = group.get_command(ctx, name)
            seen[name] = tuple(sorted(cmd.list_commands(click.Context(cmd)))) if hasattr(cmd, "list_commands") else ()
        assert seen == {k: tuple(sorted(v)) for k, v in commandline.COMMANDS[tool].items()}, tool


def test_setup_says_when_another_programs_ns_comes_first(tmp_path: Path, windows: FakeWindows, monkeypatch: pytest.MonkeyPatch):
    """NativeScript's `ns.cmd` in a folder of the machine PATH (searched before the user PATH): setup's entry
    is written, and it says that a plain `ns` runs that program, with this install's files by full path."""
    npm = tmp_path / "npm"
    npm.mkdir()
    (npm / "ns.cmd").write_text("@echo NativeScript\r\n", encoding="ascii")
    monkeypatch.setenv("NS_TEST_MACHINE_PATH", str(npm))
    ctx = second(tmp_path)
    text = setup(ctx)
    home = ctx.paths.home
    line = said(text, "Commands:")
    assert path_of(ctx) == f"{home}\\cmd"                                  # the entry is there all the same
    assert line == (
        f"Commands: 'ns' runs {npm / 'ns.cmd'} first, not this install (NS-W120). This install's are "
        f"{home}\\cmd\\ns.cmd and {home}\\cmd\\studio.cmd: type the full path, or rename or remove that file "
        "from your PATH and open a new window."
    )
    assert ("NS-W120", "another program's command comes first") in ctx.warnings
    assert ctx.flag("commands_owner") == "shadowed"
    # a Command Prompt also looks in the folder it starts in (the user folder) before the PATH
    commandline = _module("ns_common.commandline")
    assert commandline is not None, "the base has no ns_common.commandline"

    monkeypatch.setenv("NS_TEST_MACHINE_PATH", "")
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "studio.bat").write_text("@echo other\r\n", encoding="ascii")
    monkeypatch.setenv("NS_TEST_PROFILE", str(profile))
    assert commandline.reach(home, user_value=f"{home}\\cmd").why == (
        f"'studio' runs {profile / 'studio.bat'} (the folder a new Command Prompt starts in) first"
    )


def test_a_broadcast_that_does_not_finish_after_the_write_is_not_called_a_refusal(tmp_path: Path, windows: FakeWindows):
    ctx = second(tmp_path)

    def written_then_timeout(call: Call) -> RunResult:
        FakeRunner._default(ctx.runner, call)
        return RunResult(124, "", "timed out after 60 s")

    ctx.runner.results["ps:userpath-write"] = written_then_timeout
    line = said(setup(ctx), "Commands:")
    home = ctx.paths.home
    assert line.startswith(mine_line(home)) and "Windows refused" not in line
    assert line.endswith("if a new window does not know 'ns', sign out and in again.")
    assert ctx.flag("commands_owner") == "mine"


def test_an_update_that_brings_the_commands_leaves_one_note_per_outcome(plain_inst: Inst):
    """No setup runs when the commands come by update: the engine's first start leaves the sentence as a
    morning note, once per outcome (a later start with the same outcome adds nothing); `mine` adds none."""
    from ns_supervisor import launch_reach, notify

    inst = plain_inst
    a = with_commands(inst.home.parent / "A")
    inst.shell.user_path = rf"{a}\cmd"
    marker = inst.run()
    assert marker["commands"] == f"other:{a}"

    def notes() -> list[Any]:
        return [n for n in notify.read_notes(inst.paths) if n["title"] == launch_reach.COMMANDS_TITLE]

    said_once = notes()
    assert len(said_once) == 1
    assert said_once[0]["message"].startswith(f"Commands: 'ns' and 'studio' open the Nightshift at {a} (NS-W120).")
    inst.run()
    assert len(notes()) == 1                                               # settled: no second note
    # the outcome changes (the PATH is now too long): a second, different note, once
    data = json.loads(launch_reach.marker_file(inst.paths).read_text(encoding="utf-8"))
    data.pop("commands")
    launch_reach.marker_file(inst.paths).write_text(json.dumps(data), encoding="utf-8")
    inst.shell.user_path = "C:\\" + "y" * 2040
    assert inst.run()["commands"] == "refused"
    inst.run()                                                             # retried, the same sentence: no note
    told = notes()
    assert len(told) == 2 and "could not be put on your PATH (it would grow to" in told[1]["message"]


@windows_only
def test_an_argument_with_an_odd_number_of_quotes_runs_the_program_and_nothing_after_it(tmp_path: Path):
    home = tmp_path / "home"
    shim = stand_in_install(home)
    show = tmp_path / "show.py"
    show.write_text(SHOW, encoding="ascii")
    env = {**os.environ, "NS_EXIT": "7"}
    proc = run_cmd(f'"{shim}" "{show}" "a "b"', env)
    assert proc.returncode == 7 and "Python environment is missing" not in proc.stdout, proc.stdout
    assert PS is not None
    env.update(NS_SHIM=str(shim), NS_SHOW=str(show))
    ps = "& $env:NS_SHIM $env:NS_SHOW 'a \"b'; exit $LASTEXITCODE"
    proc = subprocess.run([PS, "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, text=True, timeout=120, env=env)
    assert proc.returncode == 7 and "Python environment is missing" not in proc.stdout, proc.stdout
    assert proc.stdout.splitlines()[0] == str(home)
