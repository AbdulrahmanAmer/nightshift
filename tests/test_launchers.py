import json
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
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
        "config\\window.json", "apphost-", "-Role", "Global\\Nightshift.AppHost", "#15131d",
        "CaptionHeight = 0", "ResizeBorderThickness = New-Object System.Windows.Thickness 6",
        "GlassFrameThickness = New-Object System.Windows.Thickness 0", "UseAeroCaptionButtons = $false",
        "WindowStyle]::None", "ResizeMode]::CanResize", "$MinWidth = 1024", "$MinHeight = 640",
        "$CloseGraceMs = 1500",
        "'minimize'", "'toggle-maximize'", "'drag-start'", "'close'", "'open-external'",
        "'close-ack'", "'ready'",
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


def test_host_script_sets_the_window_icon_from_the_shipped_brand_ico() -> None:
    text = read("nightshift-host.ps1")
    assert "ns_dashboard\\static\\brand\\nightshift.ico" in text
    assert "$script:win.Icon" in text and "Test-Path -LiteralPath $iconFile" in text    # skipped when missing
    ico = Path(__file__).resolve().parents[1] / "ns_dashboard" / "static" / "brand" / "nightshift.ico"
    assert ico.is_file() and ico.read_bytes()[:4] == b"\x00\x00\x01\x00"                 # a real .ico


def test_bundle_script_requires_the_brand_icon() -> None:
    bundle = (Path(__file__).resolve().parents[1] / "dev" / "make_bundle.ps1").read_text(encoding="utf-8")
    assert "ns_dashboard/static/brand/nightshift.ico" in bundle


def test_update_launcher_steps_out_of_the_app_folder_before_calling_ns_update() -> None:
    text = read("update.ps1")
    assert "Set-Location -LiteralPath (Get-NsHome)" in text
    assert text.index("Set-Location") < text.index("& $ns update")


# --- dashboard warmup ---
class FakeEngine:
    """Stands in for the supervisor API; records every POST."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, object]] = []
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self) -> None:
                body = b'{"state": "running"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                self._reply()

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                engine.posts.append((self.path, json.loads(raw or b"null")))
                self._reply()

            def log_message(self, *args: object) -> None:
                return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[tuple[FakeEngine, dict[str, str]]]:
    eng = FakeEngine()
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "runtime.json").write_text(
        json.dumps({"supervisor_port": eng.port}), encoding="utf-8"
    )
    yield eng, {"NIGHTSHIFT_HOME": str(tmp_path)}
    eng.close()


def run_common(snippet: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    script = f". '{LAUNCHERS / '_common.ps1'}'\n{snippet}\n"
    return pwsh.run_powershell(script, env_extra=env, timeout_s=90)


@needs_powershell
def test_dashboard_warmup_posts_to_a_running_headless_engine(
    engine: tuple[FakeEngine, dict[str, str]],
) -> None:
    eng, env = engine
    snippet = "if (Test-NsSupervisor) { if (Start-NsDashboardService -TimeoutSec 20) { 'warmed' } }"
    r = run_common(snippet, env)
    assert r.returncode == 0 and "warmed" in r.stdout, r.stdout + r.stderr
    assert eng.posts == [("/warmup", {"service": "dashboard"})]


@needs_powershell
def test_wait_for_the_dashboard_port_is_true_when_served_and_false_when_not(
    engine: tuple[FakeEngine, dict[str, str]],
) -> None:
    eng, env = engine
    up = run_common(f"Wait-NsPort -Url 'http://127.0.0.1:{eng.port}/' -TimeoutSec 10", env)
    assert up.stdout.strip() == "True", up.stdout + up.stderr
    eng.close()
    down = run_common(f"Wait-NsPort -Url 'http://127.0.0.1:{eng.port}/' -TimeoutSec 2", env)
    assert down.stdout.strip() == "False", down.stdout + down.stderr


def test_dashboard_launcher_always_asks_the_engine_for_the_dashboard_and_never_opens_a_dead_page() -> None:
    text = read("nightshift-dashboard.ps1")
    ask = text.index("Start-NsDashboardService")
    wait, host = text.index("Wait-NsPort"), text.index("nightshift-host.ps1")
    assert text.index("Test-NsSupervisor") < ask < wait < host
    # the warmup is not inside the "engine was not running" branch
    assert text.index("Start-NsSupervisor") < text.index("Wait-NsSupervisor") < ask
    branch_end = text.index("\n}\n", text.index("if (-not (Test-NsSupervisor))"))
    assert ask > branch_end
    assert "if (-not (Wait-NsPort" in text and "Exit-NsWithPause" in text[text.index("Wait-NsPort") :][:300]


def test_open_dashboard_action_runs_the_launcher_that_does_the_warmup() -> None:
    from installer.stage2.web import controller

    src = Path(controller.__file__).read_text(encoding="utf-8")
    body = src[src.index("def _act_open_dashboard") :][:600]
    assert "nightshift-dashboard.ps1" in body        # one implementation of the warmup: the launcher
    assert "Start-NsDashboardService" in read("nightshift-dashboard.ps1")


# --- the console the launcher hid is always restored ---
AST_PROBE = """
$errs = $null; $tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:NS_PS1, [ref]$tokens, [ref]$errs)
$kind = [System.Management.Automation.Language.TryStatementAst]
$tries = $ast.FindAll({ param($n) $n -is $kind }, $true)
$hit = @($tries | Where-Object {
    $null -ne $_.Finally -and
    $_.Finally.Extent.Text.Contains('ShowWindow($consoleHandle, 5)') -and
    $_.Body.Extent.Text.Contains('& $hostScript')
})
Write-Output ('FINALLY_AROUND_HOST=' + $hit.Count)
"""


@needs_powershell
def test_dashboard_launcher_restores_its_console_in_a_finally_around_the_host_call() -> None:
    r = pwsh.run_powershell(AST_PROBE, env_extra={"NS_PS1": str(LAUNCHERS / "nightshift-dashboard.ps1")})
    assert r.returncode == 0 and "FINALLY_AROUND_HOST=1" in r.stdout, r.stdout + r.stderr


def test_dashboard_launcher_only_trusts_a_clean_exit_from_the_host() -> None:
    text = read("nightshift-dashboard.ps1")
    assert "if ($hostCode -eq 0) { return }" in text
    assert "if ($hostCode -ne 3) { return }" not in text
    # nothing between the host call and the restore can return early
    host_call = text.index("& $hostScript")
    finally_at = text.index("finally", host_call)
    assert "return" not in text[host_call:finally_at]
    assert text.index("ShowWindow($consoleHandle, 5)") > finally_at
    assert text.index("if ($hostCode -eq 0) { return }") > text.index("ShowWindow($consoleHandle, 5)")


# --- host roles: the setup window and the dashboard window are separate single instances ---
BS = chr(92)

ROLE_PROBE = """
$errs = $null; $tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:NS_PS1, [ref]$tokens, [ref]$errs)
$fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
    $n.Name -eq 'Get-HostHome' }, $true)
. ([scriptblock]::Create($fn.Extent.Text))
$assign = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.AssignmentStatementAst] -and
    $n.Left.Extent.Text -in @('$MutexName', '$runFile') }, $true)
$names = @{}
foreach ($role in @('setup', 'dashboard')) {
    $Role = $role
    foreach ($a in $assign) { . ([scriptblock]::Create($a.Extent.Text)) }
    $names[$role] = @($MutexName, $runFile)
}
$param = $ast.ParamBlock.Parameters | Where-Object { $_.Name.VariablePath.UserPath -eq 'Role' }
Write-Output ('DEFAULT=' + $param.DefaultValue.Extent.Text)
foreach ($k in @('setup', 'dashboard')) {
    Write-Output ('ROLE ' + $k + ' ' + $names[$k][0] + ' ' + $names[$k][1])
}
$c1 = $false; $c2 = $false; $c3 = $true
$m1 = New-Object System.Threading.Mutex($true, $names['setup'][0], [ref]$c1)
$m2 = New-Object System.Threading.Mutex($true, $names['dashboard'][0], [ref]$c2)
$m3 = New-Object System.Threading.Mutex($true, $names['setup'][0], [ref]$c3)
Write-Output ('CREATED ' + $c1 + ' ' + $c2 + ' ' + $c3)
$m1.Dispose(); $m2.Dispose(); $m3.Dispose()
"""


@needs_powershell
def test_host_roles_get_their_own_mutex_and_state_file(tmp_path: Path) -> None:
    env = {"NS_PS1": str(LAUNCHERS / "nightshift-host.ps1"), "NIGHTSHIFT_HOME": str(tmp_path)}
    r = pwsh.run_powershell(ROLE_PROBE, env_extra=env)
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "DEFAULT='dashboard'" in out                      # the dashboard launcher passes no -Role
    assert f"ROLE setup Global{BS}Nightshift.AppHost.setup {tmp_path}{BS}run{BS}apphost-setup.json" in out
    dash_mutex = f"Global{BS}Nightshift.AppHost.dashboard"
    assert f"ROLE dashboard {dash_mutex} {tmp_path}{BS}run{BS}apphost-dashboard.json" in out
    # two roles can both hold their mutex at once; the same role cannot be created twice
    assert "CREATED True True False" in out


def test_host_validates_the_role_and_the_launchers_pass_the_right_one() -> None:
    host = read("nightshift-host.ps1")
    assert "[ValidateSet('setup', 'dashboard')][string]$Role = 'dashboard'" in host
    assert f"{BS}apphost.json" not in host
    assert "-Role" not in read("nightshift-dashboard.ps1")     # default: dashboard
    from installer.stage2.web import launch

    argv = launch.host_argv(Path("C:/app"), "http://127.0.0.1:1/")
    assert argv[argv.index("-Role") + 1] == "setup"
