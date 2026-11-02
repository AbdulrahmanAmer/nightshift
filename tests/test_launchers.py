from pathlib import Path

import pytest

from ns_supervisor import pwsh

LAUNCHERS = Path(__file__).resolve().parents[1] / "launchers"
EXPECTED = {
    "_common.ps1",
    "nightshift-dashboard.ps1",
    "nightshift-host.ps1",
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
    text = raw.decode("ascii")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if script.name == "nightshift-host.ps1":
        # A script with a param() block must start with it; the two guard lines follow the block.
        start = lines.index("Set-StrictMode -Version Latest")
        assert lines[start + 1] == "$ErrorActionPreference = 'Stop'"
        assert "[CmdletBinding()]" in lines[:start] and "param(" in lines[:start]
    else:
        assert lines[0] == "Set-StrictMode -Version Latest"
        assert lines[1] == "$ErrorActionPreference = 'Stop'"
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
    assert text.index("Wait-NsSupervisor") < text.index("--app=$url")
    assert "msedge.exe" in text and "chrome.exe" in text
    common = read("_common.ps1")
    assert "-WindowStyle $style" in common and "'Minimized'" in common and "ns_supervisor.app" in common
    assert "/status" in common


def test_dashboard_launcher_tries_the_webview2_host_before_the_browser_fallback() -> None:
    text = read("nightshift-dashboard.ps1")
    assert "nightshift-host.ps1" in text and "-Url $url" in text
    assert text.index("Wait-NsSupervisor") < text.index("nightshift-host.ps1") < text.index("--app=$url")
    assert "-ne 3" in text                      # exit code 3 = WebView2 unavailable: fall through


def test_host_script_follows_the_app_host_protocol() -> None:
    text = read("nightshift-host.ps1")
    for needle in (
        "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}", "WOW6432Node", "HKCU:", "bin\\webview2", "state\\webview2",
        "config\\window.json", "run\\apphost.json", "Global\\Nightshift.AppHost", "#15131d",
        "CaptionHeight = 0", "ResizeBorderThickness = New-Object System.Windows.Thickness 6",
        "GlassFrameThickness = New-Object System.Windows.Thickness 0", "UseAeroCaptionButtons = $false",
        "WindowStyle]::None", "ResizeMode]::CanResize", "$MinWidth = 1024", "$MinHeight = 640", "$CloseGraceMs = 1500",
        "'minimize'", "'toggle-maximize'", "'drag-start'", "'close'", "'open-external'", "'close-ack'", "'ready'",
        "close-requested", "window-state", "maximized =", "WebMessageReceived", "exit 3",
    ):
        assert needle in text, needle
    assert "$uri.Scheme -in @('http', 'https')" in text      # open-external is http(s) only


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
