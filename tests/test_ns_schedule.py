import base64
import subprocess
from pathlib import Path

import pytest

from ns_common.paths import Paths
from ns_supervisor import notify, pwsh, schedule
from ns_supervisor.schedule_cfg import read_run_time

needs_powershell = pytest.mark.skipif(pwsh.find_powershell() is None, reason="powershell not on PATH")


def done(stdout: str = "", stderr: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=code, stdout=stdout, stderr=stderr)


def test_install_script_has_every_required_setting(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")
    script = schedule.build_install_script(paths, "04:30", tmp_path / "app")
    for token in (
        "-WakeToRun",
        "-StartWhenAvailable",
        "New-TimeSpan -Hours 6",
        "-LogonType S4U",
        "[datetime]'04:30'",
        "Register-ScheduledTask -TaskName 'Nightshift Daily'",
        "run --daily",
        f"NIGHTSHIFT_HOME={paths.home}",
        "-Daily",
    ):
        assert token in script, token


def test_single_quotes_in_paths_are_escaped(tmp_path: Path) -> None:
    script = schedule.build_install_script(Paths(tmp_path / "O'Brien"), "03:00", tmp_path / "it's")
    assert "O''Brien" in script


def test_the_task_does_not_run_in_the_program_folder(tmp_path: Path) -> None:
    """A cwd inside <home>\\app would make every update fail while the 03:00 run (or its children) lives."""
    paths = Paths(tmp_path / "ns")
    script = schedule.build_install_script(paths, "03:00", tmp_path / "ns" / "app")
    wd = script.split("-WorkingDirectory ")[1].split("\n")[0]
    assert wd == f"'{paths.home}'" and str(tmp_path / "ns" / "app") not in wd


def test_install_runs_powershell_and_saves_the_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = Paths(tmp_path / "ns")
    seen: list[str] = []

    def fake(script: str, **kw: object) -> subprocess.CompletedProcess[str]:
        seen.append(script)
        return done("registered\n")

    monkeypatch.setattr(pwsh, "run_powershell", fake)
    msg = schedule.install(paths, "05:15", tmp_path)
    assert "05:15" in msg and "Register-ScheduledTask" in seen[0]
    assert read_run_time(paths) == "05:15"
    # no --time: the saved time is reused
    schedule.install(paths, None, tmp_path)
    assert "[datetime]'05:15'" in seen[1]


def test_default_time_is_three_am(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pwsh, "run_powershell", lambda script, **kw: done("registered"))
    schedule.install(Paths(tmp_path / "ns"), None, tmp_path)
    assert read_run_time(Paths(tmp_path / "ns")) == "03:00"


@pytest.mark.parametrize("bad", ["3:00", "25:00", "noon", "03:60"])
def test_bad_time_is_rejected(bad: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pwsh, "run_powershell", lambda script, **kw: done())
    with pytest.raises(schedule.ScheduleError):
        schedule.install(Paths(tmp_path / "ns"), bad, tmp_path)


def test_powershell_failure_and_absence_raise_schedule_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths(tmp_path / "ns")
    monkeypatch.setattr(pwsh, "run_powershell", lambda script, **kw: done(stderr="Access denied", code=1))
    with pytest.raises(schedule.ScheduleError, match="Access denied"):
        schedule.install(paths, "03:00", tmp_path)
    assert read_run_time(paths) is None

    def missing(script: str, **kw: object) -> subprocess.CompletedProcess[str]:
        raise pwsh.PowerShellMissing("no powershell")

    monkeypatch.setattr(pwsh, "run_powershell", missing)
    with pytest.raises(schedule.ScheduleError, match="no powershell"):
        schedule.show(paths)


def test_remove_and_show(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = Paths(tmp_path / "ns")
    scripts: list[str] = []

    def fake(script: str, **kw: object) -> subprocess.CompletedProcess[str]:
        scripts.append(script)
        return done("state: Ready\nnext run: tomorrow")

    monkeypatch.setattr(pwsh, "run_powershell", fake)
    schedule.install(paths, "02:00", tmp_path)
    out = schedule.show(paths)
    assert "state: Ready" in out and "configured time: 02:00" in out
    schedule.remove(paths)
    assert "Unregister-ScheduledTask -TaskName 'Nightshift Daily' -Confirm:$false" in scripts[-1]
    assert read_run_time(paths) is None


def test_encode_roundtrip() -> None:
    assert base64.b64decode(pwsh.encode("Write-Output 'é'")).decode("utf-16-le") == "Write-Output 'é'"


def test_toast_passes_text_through_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake(script: str, **kw: object) -> subprocess.CompletedProcess[str]:
        seen["script"] = script
        seen["env"] = kw["env_extra"]
        return done()

    monkeypatch.setattr(pwsh, "run_powershell", fake)
    assert notify.toast("Nightshift", "1 master + 5 Shorts ready'; Remove-Item x") is True
    assert seen["env"] == {
        "NS_TOAST_TITLE": "Nightshift",
        "NS_TOAST_MESSAGE": "1 master + 5 Shorts ready'; Remove-Item x",
    }
    assert "Remove-Item" not in str(seen["script"]) and "Windows.UI.Notifications" in str(seen["script"])


def test_toast_is_a_noop_when_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pwsh, "find_powershell", lambda: None)
    assert notify.toast("a", "b") is False

    monkeypatch.setattr(pwsh, "find_powershell", lambda: "powershell")
    monkeypatch.setattr(pwsh, "run_powershell", lambda script, **kw: done(code=1))
    assert notify.toast("a", "b") is False


def _parse_errors(script: str) -> str:
    check = (
        "$errs = $null; $tokens = $null\n"
        "[void][System.Management.Automation.Language.Parser]::ParseInput("
        "$env:NS_SCRIPT_UNDER_TEST, [ref]$tokens, [ref]$errs)\n"
        "if ($errs.Count -gt 0) { $errs | ForEach-Object { Write-Output $_.Message }; exit 1 }\n"
    )
    r = pwsh.run_powershell(check, env_extra={"NS_SCRIPT_UNDER_TEST": script})
    return r.stdout.strip() if r.returncode != 0 else ""


@needs_powershell
def test_generated_scripts_are_valid_powershell(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")
    for script in (
        schedule.build_install_script(paths, "03:00", tmp_path / "app"),
        schedule.build_remove_script(),
        schedule.build_show_script(),
        schedule.build_read_script(),
        notify.TOAST_SCRIPT,
    ):
        assert _parse_errors(script) == ""


# --- review P1: S4U needs elevation; fall back to an interactive logon instead of failing the install ---
def test_the_script_tries_s4u_first_then_interactive(tmp_path: Path) -> None:
    script = schedule.build_install_script(Paths(tmp_path / "ns"), "03:00", tmp_path / "app")
    assert script.index("-LogonType S4U") < script.index("catch") < script.index("-LogonType Interactive")
    assert script.count("Register-ScheduledTask -TaskName 'Nightshift Daily'") == 2
    assert "-WakeToRun" in script and "registered:' + $mode" in script


STUBS = """
$script:calls = @()
function New-ScheduledTaskAction { param($Execute, $Argument, $WorkingDirectory) 'action' }
function New-ScheduledTaskTrigger { param([switch]$Daily, $At) 'trigger' }
function New-ScheduledTaskSettingsSet { param([switch]$WakeToRun, [switch]$StartWhenAvailable,
    [switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries, $ExecutionTimeLimit,
    [switch]$Disable) 'settings' }
function New-TimeSpan { param($Hours) 'span' }
function New-ScheduledTaskPrincipal {
    param($UserId, $LogonType, $RunLevel)
    [pscustomobject]@{ Logon = $LogonType }
}
function Register-ScheduledTask {
    param($TaskName, $Action, $Trigger, $Settings, $Principal, $Description, [switch]$Force)
    $script:calls += $Principal.Logon
    if ($Principal.Logon -eq 'S4U' -and $env:NS_S4U -eq 'deny') { throw 'Access is denied.' }
    if ($env:NS_ALL -eq 'deny') { throw 'Register-ScheduledTask : Access is denied.' }
}
"""


def run_install_script(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    script = STUBS + schedule.build_install_script(Paths(tmp_path / "ns"), "03:00", tmp_path / "app")
    script += "\nWrite-Output ('calls:' + ($script:calls -join ','))"
    return pwsh.run_powershell(script, env_extra=env)


@needs_powershell
def test_access_denied_for_s4u_falls_back_to_an_interactive_task(tmp_path: Path) -> None:
    r = run_install_script(tmp_path, NS_S4U="deny")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "registered:interactive" in r.stdout and "calls:S4U,Interactive" in r.stdout


@needs_powershell
def test_s4u_is_kept_when_windows_allows_it(tmp_path: Path) -> None:
    r = run_install_script(tmp_path, NS_S4U="ok")
    assert "registered:s4u" in r.stdout and "calls:S4U" in r.stdout and "Interactive" not in r.stdout


@needs_powershell
def test_when_both_modes_are_refused_the_error_reaches_the_caller(tmp_path: Path) -> None:
    r = run_install_script(tmp_path, NS_ALL="deny")
    assert r.returncode != 0 and "Access is denied" in (r.stdout + r.stderr)


def test_install_says_when_it_could_only_register_the_interactive_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pwsh, "run_powershell", lambda script, **kw: done("registered:interactive\n"))
    msg = schedule.install(Paths(tmp_path / "ns"), "03:00", tmp_path)
    assert schedule.INTERACTIVE_MARK in msg and "03:00" in msg
    monkeypatch.setattr(pwsh, "run_powershell", lambda script, **kw: done("registered:s4u\n"))
    assert schedule.INTERACTIVE_MARK not in schedule.install(Paths(tmp_path / "ns"), "03:00", tmp_path)
