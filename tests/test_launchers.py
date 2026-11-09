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
    if script.name == "nightshift-dashboard.ps1":
        # the only launcher that starts another PowerShell (the hidden window host): the child needs the same
        # policy bypass the shortcut gave this one, and nothing else changes the policy
        assert text.count("ExecutionPolicy Bypass") == 1 and "Set-ExecutionPolicy" not in text
    else:
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


def test_get_ns_home_falls_back_to_the_launchers_own_install_folder(tmp_path: Path) -> None:
    """NIGHTSHIFT_HOME unset or stale: the home is two levels above the launchers folder (first dev run)."""
    import os
    import shutil
    import subprocess

    from ns_supervisor import pwsh

    exe = pwsh.find_powershell()
    if exe is None:
        pytest.skip("powershell not available")
    home = tmp_path / "Night Home"
    launchers = home / "app" / "launchers"
    launchers.mkdir(parents=True)
    (home / "config").mkdir()
    (home / "config" / "runtime.json").write_text("{}", encoding="utf-8")
    shutil.copy(LAUNCHERS / "_common.ps1", launchers / "_common.ps1")
    stale = tmp_path / "stale"
    stale.mkdir()
    common = launchers / "_common.ps1"
    script = f". '{common}'\nWrite-Output (Get-NsHome)\nWrite-Output $env:NIGHTSHIFT_HOME\n"
    env = {k: v for k, v in os.environ.items() if k.lower() != "psmodulepath"}
    for value in (None, str(stale)):                   # unset, and set to a folder that is not an install
        env.pop("NIGHTSHIFT_HOME", None)
        if value is not None:
            env["NIGHTSHIFT_HOME"] = value
        out = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                             text=True, timeout=60, env=env, check=False)
        assert out.returncode == 0, out.stdout + out.stderr
        lines = out.stdout.strip().splitlines()
        assert lines == [str(home), str(home)], (value, out.stdout, out.stderr)   # resolved, and exported
    env["NIGHTSHIFT_HOME"] = str(home)                 # a good variable still wins
    out = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                         text=True, timeout=60, env=env, check=False)
    assert out.stdout.strip().splitlines() == [str(home), str(home)]


def test_dashboard_launcher_opens_port_8100_after_waiting_for_the_engine() -> None:
    text = read("nightshift-dashboard.ps1")
    assert "http://127.0.0.1:8100" in text
    assert text.index("Wait-NsSupervisor") < text.index("Start-Process $url")
    assert text.index("Wait-NsSupervisor") < text.index("--app=$url")
    assert "msedge.exe" in text and "chrome.exe" in text
    common = read("_common.ps1")
    assert "-WindowStyle $style" in common and "'Minimized'" in common and "ns_supervisor.app" in common
    liveness = common[common.index("function Test-NsSupervisor") : common.index("function Get-NsStatus")]
    assert "/health" in liveness and "/status" not in liveness    # the cheap liveness route has a caller


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
    # the baseURL is refreshed from runtime.json after the model is up and before OpenCode starts
    assert text.index("/acquire") < text.index("Sync-NsOpenCodePort") < text.index("OPENCODE_CONFIG")
    # the acquire result (the running llama-server's address) is kept and handed over, not thrown away
    assert "$ep = Invoke-NsPost -Path '/acquire'" in text
    assert "Sync-NsOpenCodePort -BaseUrl" in text and "'/v1'" in text


OPENCODE_JSON = {
    "$schema": "https://opencode.ai/config.json",
    "enabled_providers": ["local"],
    "provider": {"local": {
        "npm": "@ai-sdk/openai-compatible",
        "name": "Nightshift Local Qwen",
        "options": {"baseURL": "http://127.0.0.1:8080/v1", "apiKey": "local"},
        "models": {"code": {"name": "Qwen3.6 35B-A3B (local)"}},
    }},
    "model": "local/code",
    "small_model": "local/code",
    "autoupdate": False,
    "share": "disabled",
}


def run_sync(
    tmp_path: Path, runtime: str | None, opencode: str | None, base_url: str | None = None
) -> tuple[str, Path]:
    """Run Sync-NsOpenCodePort against a throwaway home; returns the helper's output and opencode.json."""
    import os
    import subprocess

    exe = pwsh.find_powershell()
    if exe is None:
        pytest.skip("powershell not available")
    home = tmp_path / "Night Home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "runtime.json").write_text(runtime if runtime is not None else "{}", encoding="utf-8")
    target = home / "config" / "opencode.json"
    if opencode is not None:
        target.write_text(opencode, encoding="utf-8")
    call = "Sync-NsOpenCodePort" if base_url is None else f"Sync-NsOpenCodePort -BaseUrl '{base_url}'"
    script = f". '{LAUNCHERS / '_common.ps1'}'\n{call}\n"
    env = {k: v for k, v in os.environ.items() if k.lower() != "psmodulepath"}
    env["NIGHTSHIFT_HOME"] = str(home)
    out = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                         text=True, timeout=60, env=env, check=False)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout, target


@needs_powershell
def test_sync_opencode_port_rewrites_only_the_base_url(tmp_path: Path) -> None:
    runtime = json.dumps({"llama": {"host": "127.0.0.1", "port": 9191}})
    _out, target = run_sync(tmp_path, runtime, json.dumps(OPENCODE_JSON, indent=2) + "\n")
    raw = target.read_bytes()
    assert raw[:3] != bytes([0xEF, 0xBB, 0xBF])                       # no BOM
    got = json.loads(raw.decode("utf-8"))
    expected = json.loads(json.dumps(OPENCODE_JSON))
    expected["provider"]["local"]["options"]["baseURL"] = "http://127.0.0.1:9191/v1"
    assert got == expected  # nothing else changed (arrays stay arrays)


@needs_powershell
def test_sync_opencode_port_prefers_the_address_the_engine_reported(tmp_path: Path) -> None:
    """/acquire returns the running llama-server's address; it wins over runtime.json, which may be stale."""
    runtime = json.dumps({"llama": {"host": "127.0.0.1", "port": 9191}})
    text = json.dumps(OPENCODE_JSON, indent=2) + "\n"
    _out, target = run_sync(tmp_path / "a", runtime, text, base_url="http://127.0.0.1:7777/v1")
    got = json.loads(target.read_text(encoding="utf-8"))
    assert got["provider"]["local"]["options"]["baseURL"] == "http://127.0.0.1:7777/v1"
    # an empty or non-http value (e.g. "inproc") falls back to runtime.json
    for bad in ("", "inproc"):
        _out, target = run_sync(tmp_path / f"b{bad}", runtime, text, base_url=bad)
        got = json.loads(target.read_text(encoding="utf-8"))
        assert got["provider"]["local"]["options"]["baseURL"] == "http://127.0.0.1:9191/v1"


@needs_powershell
def test_sync_opencode_port_keeps_non_ascii_text_intact(tmp_path: Path) -> None:
    """Windows PowerShell 5.1 reads a BOM-less file as ANSI unless told UTF-8; edited text must survive."""
    edited = json.loads(json.dumps(OPENCODE_JSON))
    edited["provider"]["local"]["name"] = "Nightshift Local Qwen — café مرحبا"
    runtime = json.dumps({"llama": {"host": "127.0.0.1", "port": 9191}})
    text = json.dumps(edited, indent=2, ensure_ascii=False) + "\n"
    assert "café" in text                                          # really raw UTF-8 on disk
    _out, target = run_sync(tmp_path, runtime, text)
    got = json.loads(target.read_bytes().decode("utf-8"))
    edited["provider"]["local"]["options"]["baseURL"] = "http://127.0.0.1:9191/v1"
    assert got == edited


@needs_powershell
def test_sync_opencode_port_leaves_a_matching_or_absent_file_alone(tmp_path: Path) -> None:
    same = json.dumps(OPENCODE_JSON)
    _out, target = run_sync(tmp_path / "a", json.dumps({"llama": {"port": 8080}}), same)
    assert target.read_text(encoding="utf-8") == same                 # already right: not rewritten
    _out, target = run_sync(tmp_path / "b", json.dumps({"llama": {"port": 9191}}), None)
    assert not target.exists()  # no Nightshift Code config: nothing made
    _out, target = run_sync(tmp_path / "c", "{}", same)  # no llama block: default 8080 stays
    assert target.read_text(encoding="utf-8") == same


@needs_powershell
def test_sync_opencode_port_never_stops_the_launcher(tmp_path: Path) -> None:
    out, target = run_sync(tmp_path, json.dumps({"llama": {"port": 9191}}), "{ not json")
    assert target.read_text(encoding="utf-8") == "{ not json"
    assert "opencode.json" in out                                      # a plain note, no exception


@needs_powershell
def test_start_ns_supervisor_does_not_pass_the_setup_windows_no_host_switch_to_the_engine(
    tmp_path: Path,
) -> None:
    """D-3 / F-D4: the engine started by a launcher keeps NIGHTSHIFT_HOME but not NS_DASHBOARD_NO_HOST."""
    import os
    import time

    exe = pwsh.find_powershell()
    assert exe is not None
    home = tmp_path / "home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "runtime.json").write_text("{}", encoding="utf-8")
    out = tmp_path / "child-env.txt"
    fake = tmp_path / "fake-engine.cmd"
    fake.write_text(
        f'@echo off\r\necho NO_HOST=[%NS_DASHBOARD_NO_HOST%]> "{out}.tmp"\r\n'
        f'echo HOME=[%NIGHTSHIFT_HOME%]>> "{out}.tmp"\r\nmove /y "{out}.tmp" "{out}" > nul\r\n',
        encoding="ascii",
    )
    script = (
        f". '{LAUNCHERS / '_common.ps1'}'\n"
        f"function Get-NsVenvExe {{ param([string]$Venv, [string]$Name) return '{fake}' }}\n"
        "Start-NsSupervisor -Headless\n"
        "Write-Output ('LAUNCHER=[' + $env:NS_DASHBOARD_NO_HOST + ']')\n"
    )
    env = {k: v for k, v in os.environ.items() if k.lower() != "psmodulepath"}
    env.update({"NIGHTSHIFT_HOME": str(home), "NS_DASHBOARD_NO_HOST": "1"})
    r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                       text=True, timeout=60, env=env, check=False)
    assert r.returncode == 0, r.stdout + r.stderr
    # the launcher itself still sees the switch afterwards (the dashboard launcher reads it for the host)
    assert "LAUNCHER=[1]" in r.stdout
    deadline = time.monotonic() + 30
    while not out.exists() and time.monotonic() < deadline:
        time.sleep(0.2)
    assert out.exists(), "the fake engine never ran"
    lines = out.read_text(encoding="ascii").split()
    assert lines == ["NO_HOST=[]", f"HOME=[{home}]"]


def test_start_ns_supervisor_hides_the_switch_for_the_start_and_restores_it() -> None:
    body = read("_common.ps1")
    fn = body[body.index("function Start-NsSupervisor") : body.index("function Wait-NsHostReady")]
    assert fn.index("Remove-Item Env:NS_DASHBOARD_NO_HOST") < fn.index("Start-Process")
    assert fn.index("Start-Process") < fn.index("finally") < fn.rindex("$env:NS_DASHBOARD_NO_HOST = $noHost")


def test_update_launcher_filters_release_zips_and_calls_ns_update() -> None:
    text = read("update.ps1")
    assert "Nightshift-*.zip" in text and "update $dlg.FileName" in text and "--detach" in text


def test_other_launchers_call_the_right_tools() -> None:
    assert " stop" in read("stop-all.ps1")
    assert "doctor --bundle" in read("diagnostics.ps1")
    assert "outbox" in read("open-outbox.ps1")
    assert "studio.exe" in read("nightshift-studio.ps1")


def test_stop_all_closes_the_window_hosts_and_the_host_closes_itself_when_the_server_is_gone() -> None:
    """stop-all left the WebView2 window on a dead page (153 MB) in the 0.1.10 dev teardown test."""
    assert "Close-NsHostWindows" in read("stop-all.ps1")
    common = read("_common.ps1")
    fn = common[common.index("function Close-NsHostWindows"):]
    fn = fn[: fn.index("\nfunction ")]
    assert "apphost-*.json" in fn and "PostMessage" in fn and "0x0010" in fn and "Stop-Process" in fn
    host = read("nightshift-host.ps1")
    assert "function Test-ServerAlive" in host and "$ServerMissLimit = 3" in host
    after_ready = host.split("$script:ready = $true")[1]
    armed = after_ready.split("serverTimer.Start()")[0]
    assert "serverTimer" in after_ready and "$script:confirmedClose = $true" in armed


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
    assert "Set-NsHomeLocation" in text
    assert text.index("Set-NsHomeLocation") < text.index("& $py -m ns_supervisor.cli update")
    common = read("_common.ps1")
    assert "[Environment]::CurrentDirectory = $nsHome" in common   # the Win32 cwd, not just PowerShell's


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


# --- the window host is its own hidden process; the launcher does not keep a console open behind it ---
def test_dashboard_launcher_starts_the_host_as_its_own_hidden_process_and_ends() -> None:
    text = read("nightshift-dashboard.ps1")
    assert "& $hostScript" not in text            # the window no longer lives in the launcher's process
    start = text.index("Start-Process -FilePath 'powershell.exe'")
    call = text[start : text.index("\n", start)]
    assert "-WindowStyle Hidden" in call and "-PassThru" in call and "-WorkingDirectory (Get-NsHome)" in call
    args_line = text[text.index("$hostArguments ="):][:200]
    assert "-STA" in args_line and '-File `"$hostScript`"' in args_line and "-Url $url" in args_line
    assert 'AppPath `"$appPath`"' in args_line         # quoted: the program folder may contain spaces
    assert "GetConsoleWindow" not in text and "ShowWindow" not in text
    common = read("_common.ps1")
    wait = common[common.index("function Wait-NsHostReady") : common.index("function Wait-NsSupervisor")]
    assert "$Process.HasExited" in wait and "'ready'" in wait and "apphost-" in wait
    host = read("nightshift-host.ps1")
    assert "[switch]$Ready" in host and "-Ready" in host.split("add_CoreWebView2InitializationCompleted")[1]


def test_dashboard_launcher_only_trusts_a_clean_exit_from_the_host() -> None:
    text = read("nightshift-dashboard.ps1")
    assert "if ($hostCode -eq 0) { return }" in text
    assert "if ($hostCode -ne 3) { return }" not in text
    assert "$hostCode = Wait-NsHostReady -Process $hostProcess" in text
    # the fallback chain after a failed host: Edge or Chrome app window, then the default browser
    ok_return = text.index("if ($hostCode -eq 0) { return }")
    assert ok_return < text.index("--app=$url") < text.index("Start-Process $url")


def test_the_browser_app_window_never_shows_the_first_run_pages() -> None:
    text = read("nightshift-dashboard.ps1")
    fallback = text[text.index('@("--app=$url"') :][:140]
    assert "'--no-first-run'" in fallback and "'--no-default-browser-check'" in fallback


@needs_powershell
def test_wait_for_the_host_returns_its_exit_code_or_ready(tmp_path: Path) -> None:
    env = {"NIGHTSHIFT_HOME": str(tmp_path)}
    # a host that already exited with 3 (WebView2 unavailable): the launcher falls back to the browser
    gone = run_common(
        "$p = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c exit 3' -WindowStyle Hidden -PassThru; "
        "Wait-NsHostReady -Process $p -TimeoutSec 20", env)
    assert gone.stdout.strip() == "3", gone.stdout + gone.stderr
    # a host that is running and whose state file says ready: 0 at once (the launcher ends)
    run = tmp_path / "run"
    run.mkdir()
    ready = run_common(
        "$p = Start-Process -FilePath 'ping.exe' -ArgumentList '-n 30 127.0.0.1' "
        "-WindowStyle Hidden -PassThru; "
        "try { "
        "$j = @{ hwnd = 1; pid = $p.Id; ready = $true } | ConvertTo-Json -Compress; "
        "[IO.File]::WriteAllText((Join-Path $env:NIGHTSHIFT_HOME 'run\\apphost-dashboard.json'), $j); "
        "Wait-NsHostReady -Process $p -TimeoutSec 20 } finally { Stop-Process -Id $p.Id -Force }", env)
    assert ready.stdout.strip() == "0", ready.stdout + ready.stderr


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


def test_the_liveness_probe_asks_a_route_the_engine_really_has() -> None:
    """Test-NsSupervisor must call /health; the engine serves it (and not a made-up path)."""
    import re

    probe = re.search(r"'(/\w+)'\) -TimeoutSec 3", read("_common.ps1"))
    assert probe is not None
    app_src = (Path(__file__).resolve().parents[1] / "ns_supervisor" / "app.py").read_text(encoding="utf-8")
    assert f'@app.get("{probe.group(1)}")' in app_src


# --- an engine started by the 03:00 task lives in session 0 (no desktop) ---
def test_dashboard_launcher_restarts_an_idle_session_0_engine_and_never_interrupts_a_run() -> None:
    text = read("nightshift-dashboard.ps1")
    first = text.index("Get-NsEngineSessionAction")
    assert text.index("Test-NsSupervisor") < first < text.index("Start-NsSupervisor")
    block = text[first : text.index("if (-not (Test-NsSupervisor))")]
    assert "'restart'" in block and "Stop-NsEngine" in block and "'busy'" in block
    assert block.index("Stop-NsEngine") < block.index("'busy'")        # only the restart branch stops it
    assert "Get-NsStatus" in block and "Get-NsOwnSessionId" in block


@needs_powershell
@pytest.mark.parametrize(
    ("status", "mine", "expected"),
    [
        ('{"session":{"id":0,"interactive":false},"busy":false}', 2, "restart"),
        ('{"session":{"id":0,"interactive":false},"busy":true}', 2, "busy"),
        ('{"session":{"id":2,"interactive":true},"busy":false}', 2, "ok"),
        ('{"session":{"id":0,"interactive":false},"busy":false}', 0, "ok"),    # the launcher is in session 0
        ('{"session":{"id":0,"interactive":false}}', 2, "restart"),
        ('{"gpu_owner":null,"services":{}}', 2, "ok"),                          # an older engine says nothing
        ('{"session":{"id":null,"interactive":true},"busy":true}', 2, "ok"),
        ("null", 2, "ok"),
    ],
)
def test_engine_session_action(status: str, mine: int, expected: str, tmp_path: Path) -> None:
    snippet = f"$s = ConvertFrom-Json '{status}'\nGet-NsEngineSessionAction -Status $s -MySession {mine}"
    r = run_common(snippet, {"NIGHTSHIFT_HOME": str(tmp_path)})
    assert r.returncode == 0 and r.stdout.strip() == expected, r.stdout + r.stderr


@needs_powershell
def test_get_nsstatus_reads_the_engine_and_is_null_when_it_does_not_answer(
    engine: tuple[FakeEngine, dict[str, str]],
) -> None:
    eng, env = engine
    up = run_common("(Get-NsStatus).state", env)
    assert up.stdout.strip() == "running", up.stdout + up.stderr
    eng.close()
    down = run_common("if ($null -eq (Get-NsStatus)) { 'null' } else { 'object' }", env)
    assert down.stdout.strip() == "null", down.stdout + down.stderr


@needs_powershell
def test_stop_nsengine_posts_the_shutdown_and_waits_for_the_engine_to_go(
    engine: tuple[FakeEngine, dict[str, str]],
) -> None:
    eng, env = engine
    r = run_common("Stop-NsEngine -TimeoutSec 2", env)           # the fake keeps answering: it does not stop
    assert r.stdout.strip() == "False", r.stdout + r.stderr
    assert eng.posts == [("/shutdown", {"grace_s": 10})]


def test_the_engine_is_started_from_the_install_home_without_stage_ones_python_path() -> None:
    common = read("_common.ps1")
    body = common[common.index("function Start-NsSupervisor") : common.index("function Wait-NsHostReady")]
    assert "-WorkingDirectory (Get-NsHome)" in body and "Get-NsAppPath" not in body
    start = body.index("Start-Process")
    assert body.index("Remove-Item Env:PYTHONPATH") < start
    assert body.index("Remove-Item Env:NIGHTSHIFT_BOOTSTRAP_UV") < start
