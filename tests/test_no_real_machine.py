# ruff: noqa: E501
"""No test can reach the machine's real Task Scheduler (or the real keyring), whatever it does with its fakes.

On 2026-11-10 a test called `monkeypatch.undo()`; that also removed the fake Task Scheduler, and the rest of
the test registered the laptop's real 'Nightshift Daily' task. The refusal therefore lives in the product, at
the lowest points that start the real thing (`ns_supervisor/testguard.py`). These tests install NO fake and
call the real routes: each must raise before anything is started, and the task Windows holds must be the
same before and after (read here with a plain `Get-ScheduledTask`, reading only).
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from installer.stage2 import winenv
from installer.stage2.runner import SubprocessRunner
from installer.stage2.secrets import KeyringStore
from ns_common.paths import Paths
from ns_supervisor import pwsh, schedule, testguard
from ns_supervisor.schedule_cfg import write_run_time
from ns_supervisor.testguard import RealMachineUnderTest

ROOT = Path(__file__).resolve().parents[1]
needs_powershell = pytest.mark.skipif(pwsh.find_powershell() is None, reason="needs PowerShell (Windows)")

READ_ONLY = "; ".join([
    "$t = Get-ScheduledTask -TaskName 'Nightshift Daily' -ErrorAction SilentlyContinue",
    "if ($null -eq $t) { 'none' } else { 'state=' + $t.State; 'args=' + (@($t.Actions) | Select-Object -First 1).Arguments; "
    "'start=' + (@($t.Triggers) | Select-Object -First 1).StartBoundary; 'logon=' + $t.Principal.LogonType }",
])


def powershell(command: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """The test's own PowerShell, outside the product's runners."""
    exe = pwsh.find_powershell()
    assert exe is not None
    return subprocess.run(
        [exe, "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, env=env,
        stdin=subprocess.DEVNULL, timeout=120,
    )


def real_task() -> str:
    """What Windows holds for 'Nightshift Daily' right now. The test's own look: a read, nothing else."""
    done = powershell(READ_ONLY)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


@pytest.fixture
def home(tmp_path: Path) -> Paths:
    paths = Paths(tmp_path / "home")
    paths.ensure()
    return paths


def test_the_session_is_marked_for_every_child_process():
    assert os.environ.get(testguard.ENV) == "1"  # tests/conftest.py, at import: no fixture can undo it
    assert testguard.under_test() is True


def test_in_this_process_no_environment_change_lifts_the_refusal(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(testguard.ENV)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert testguard.under_test() is True  # pytest is imported: a test's environment cannot change that
    with pytest.raises(RealMachineUnderTest, match="real Windows Task Scheduler"):
        pwsh.run_powershell(schedule.build_read_script())


@needs_powershell
def test_every_real_route_to_the_task_scheduler_raises_and_changes_nothing(home: Paths, tmp_path: Path):
    """No fake is installed here: `pwsh.run_powershell` and the installer's runner are the real ones."""
    assert pwsh.run_powershell.__module__ == "ns_supervisor.pwsh"
    before = real_task()

    routes = {
        "read": lambda: schedule.read_task(),
        "the switch on": lambda: schedule.set_automatic(home, tmp_path, on=True, run_time="04:30"),
        "the switch off": lambda: schedule.set_automatic(home, tmp_path, on=False),
        "ns schedule install": lambda: schedule.install(home, "04:30", tmp_path, keep_state=True),
        "install, task unread": lambda: schedule.install(home, "04:30", tmp_path),
        "install --disabled": lambda: schedule.install(home, "04:30", tmp_path, disabled=True),
        "ns schedule remove": lambda: schedule.remove(home),
        "ns schedule show": lambda: schedule.show(home),
        "setup": lambda: schedule.setup_apply(home, tmp_path, first=True, nightly=True, seen=None, run_time="04:30"),
        "the look": lambda: schedule.sync_record(home),
        "register script": lambda: pwsh.run_powershell(schedule.build_install_script(home, "04:30", tmp_path)),
        "remove script": lambda: pwsh.run_powershell(schedule.build_remove_script()),
        "schtasks": lambda: pwsh.run_powershell("schtasks /Delete /TN 'Nightshift Daily' /F"),
        "setup's own look": lambda: winenv.run_ps(SubprocessRunner(), schedule.build_read_script(), tag="ps:task-read"),
        "the runner, schtasks.exe": lambda: SubprocessRunner().run(["schtasks.exe", "/Query", "/TN", "Nightshift Daily"]),
        "the runner, a command": lambda: SubprocessRunner().run(
            ["powershell.exe", "-NoProfile", "-Command", "Unregister-ScheduledTask -TaskName 'Nightshift Daily'"]
        ),
    }
    for name, call in routes.items():
        with pytest.raises(RealMachineUnderTest, match="refused under pytest"):
            call()
        assert not (home.config / "schedule.json").exists(), name  # and nothing was written down as done
    write_run_time(home, "04:30", "registered")  # a record that says on: an update has work to do
    with pytest.raises(RealMachineUnderTest, match="refused under pytest"):
        schedule.reregister(home, tmp_path)
    assert real_task() == before

    # a child process a test starts: it has no pytest inside; the session's variable is what it inherits
    env = {k: v for k, v in os.environ.items() if k != "PYTEST_CURRENT_TEST"}
    env.update(NIGHTSHIFT_HOME=str(home.home), PYTHONPATH=str(ROOT), PYTHONUTF8="1")
    for args in (
        ["schedule", "install", "--time", "04:30"],
        ["schedule", "remove"],
        ["schedule", "install", "--time", "04:30", "--setup", "first", "--nightly", "yes"],
    ):
        child = subprocess.run(
            [sys.executable, "-m", "ns_supervisor.cli", *args], capture_output=True, text=True, env=env,
            encoding="utf-8", errors="replace", timeout=120,
        )
        assert child.returncode != 0 and "RealMachineUnderTest" in child.stderr, (args, child.stdout, child.stderr)
    assert real_task() == before


@needs_powershell
def test_the_uninstaller_refuses_a_test_run_before_it_changes_anything():
    """uninstall.ps1 removes the task, the shortcuts, the registration and the stored keys. It is never run
    whole here (were the refusal ever lost, this test would uninstall the machine it runs on): the refusal
    must stand before the first statement that changes something, and its own lines must exit 3."""
    script = (ROOT / "installer" / "uninstall.ps1").read_text(encoding="utf-8")
    start = script.index("if (($env:NS_UNDER_TEST -or $env:PYTEST_CURRENT_TEST)")
    end = script.index("}", script.index("exit 3", start)) + 1
    guard = script[start:end]
    assert "-not $SkipSystem -and -not $DryRun" in guard
    top = script[script.index("$ErrorActionPreference = 'Stop'"):start]  # what runs before the refusal
    for changing in ("Unregister-ScheduledTask", "Remove-Item", "delete_password", "SetEnvironmentVariable", "& $"):
        assert changing not in top, changing
    assert "Unregister-ScheduledTask" in script[end:]

    def run(switches: str, env: dict[str, str]) -> int:
        return powershell(switches + "; " + guard + "; exit 0", env).returncode

    marked = dict(os.environ)
    bare = {k: v for k, v in os.environ.items() if k not in (testguard.ENV, "PYTEST_CURRENT_TEST")}
    assert run("$SkipSystem = $false; $DryRun = $false", marked) == 3  # refused
    assert run("$SkipSystem = $true; $DryRun = $false", marked) == 0  # what the tests may call
    assert run("$SkipSystem = $false; $DryRun = $true", marked) == 0
    assert run("$SkipSystem = $false; $DryRun = $false", bare) == 0  # an owner's real uninstall is not refused


def test_a_script_that_only_carries_the_words_as_data_still_runs():
    """The parse tests hand the script over in an environment variable: nothing in the command names it."""
    testguard.refuse_task_scheduler("[void][System.Management.Automation.Language.Parser]::ParseInput($env:X)")
    testguard.refuse_task_scheduler(["uv.exe", "--version"])
    with pytest.raises(RealMachineUnderTest):
        testguard.refuse_task_scheduler(["powershell.exe", "-EncodedCommand", pwsh.encode("Get-ScheduledTask")])
    # a script whose every ScheduledTask command is its own stand-in function runs (the tests of the generated
    # scripts do this under a real PowerShell); one real command beside the stand-ins is refused again
    stub = "function Get-ScheduledTask { param($TaskName) $null }; "
    testguard.refuse_task_scheduler(stub + schedule.build_read_script())
    leaks = ("Unregister-ScheduledTask -TaskName x", "schtasks /Query", "ScheduledTasks" + chr(92) + "Get-ScheduledTask",
             "Import-Module ScheduledTasks -Force")
    for leak in leaks:
        with pytest.raises(RealMachineUnderTest):
            testguard.refuse_task_scheduler(stub + schedule.build_read_script() + "; " + leak)


def test_the_real_keyring_package_is_refused_and_a_stand_in_is_not(monkeypatch: pytest.MonkeyPatch):
    kept: dict[str, str] = {}
    stand_in = types.SimpleNamespace(
        get_password=lambda service, name: kept.get(name),
        set_password=lambda service, name, value: kept.__setitem__(name, value),
    )
    monkeypatch.setitem(sys.modules, "keyring", stand_in)
    KeyringStore().set("pexels_api_key", "k")
    assert KeyringStore().get("pexels_api_key") == "k"
    installed = types.ModuleType("keyring")
    installed.__file__ = str(Path(sys.prefix) / "Lib" / "site-packages" / "keyring" / "__init__.py")
    monkeypatch.setitem(sys.modules, "keyring", installed)  # what the real package looks like
    for call in (lambda: KeyringStore().get("x"), lambda: KeyringStore().set("x", "y"), lambda: KeyringStore().delete("x")):
        with pytest.raises(RealMachineUnderTest, match="Credential Manager"):
            call()
