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
    assert "O''Brien" in script and "it''s" in script


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
        notify.TOAST_SCRIPT,
    ):
        assert _parse_errors(script) == ""
