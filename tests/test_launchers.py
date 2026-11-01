from pathlib import Path

import pytest

from ns_supervisor import pwsh

LAUNCHERS = Path(__file__).resolve().parents[1] / "launchers"
EXPECTED = {
    "_common.ps1",
    "nightshift-dashboard.ps1",
    "nightshift-studio.ps1",
    "open-outbox.ps1",
    "stop-all.ps1",
    "update.ps1",
    "diagnostics.ps1",
    "nightshift-code.ps1",
}
SCRIPTS = sorted(LAUNCHERS.glob("*.ps1"))

needs_powershell = pytest.mark.skipif(pwsh.find_powershell() is None, reason="powershell not on PATH")


def test_all_launchers_exist() -> None:
    assert {p.name for p in SCRIPTS} == EXPECTED


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_hygiene(script: Path) -> None:
    raw = script.read_bytes()
    raw.decode("ascii")  # ASCII only (also guards against a BOM)
    lines = [ln.strip() for ln in raw.decode("ascii").splitlines() if ln.strip()]
    assert lines[0] == "Set-StrictMode -Version Latest"
    assert lines[1] == "$ErrorActionPreference = 'Stop'"
    text = raw.decode("ascii")
    assert "Invoke-Expression" not in text and "iex " not in text.lower()
    assert "ExecutionPolicy" not in text and "Bypass" not in text


@needs_powershell
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_script_parses(script: Path) -> None:
    check = (
        "$errs = $null; $tokens = $null\n"
        "[void][System.Management.Automation.Language.Parser]::ParseFile("
        "$env:NS_PS1, [ref]$tokens, [ref]$errs)\n"
        "if ($errs.Count -gt 0) { $errs | ForEach-Object { Write-Output $_.Message }; exit 1 }\n"
    )
    r = pwsh.run_powershell(check, env_extra={"NS_PS1": str(script)})
    assert r.returncode == 0, r.stdout + r.stderr


def read(name: str) -> str:
    return (LAUNCHERS / name).read_text(encoding="ascii")


def test_dashboard_launcher_opens_port_8100_after_waiting_for_the_engine() -> None:
    text = read("nightshift-dashboard.ps1")
    assert "http://127.0.0.1:8100" in text
    assert text.index("Wait-NsSupervisor") < text.index("Start-Process $url")
    common = read("_common.ps1")
    assert "-WindowStyle $style" in common and "'Minimized'" in common and "ns_supervisor.app" in common
    assert "/status" in common


def test_code_launcher_acquires_as_opencode_and_releases_in_finally() -> None:
    text = read("nightshift-code.ps1")
    assert "$owner = 'opencode'" in text and "model_alias = 'code'" in text
    assert "OPENCODE_CONFIG" in text and "opencode.json" in text
    assert text.index("/acquire") < text.index("OPENCODE_CONFIG") < text.rindex("/release")
    assert "finally" in text[text.index("OPENCODE_CONFIG") :]


def test_update_launcher_filters_release_zips_and_calls_ns_update() -> None:
    text = read("update.ps1")
    assert "Nightshift-*.zip" in text and "update $dlg.FileName" in text


def test_other_launchers_call_the_right_tools() -> None:
    assert " stop" in read("stop-all.ps1")
    assert "doctor --bundle" in read("diagnostics.ps1")
    assert "outbox" in read("open-outbox.ps1")
    assert "studio.exe" in read("nightshift-studio.ps1")
