import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from ns_common.paths import Paths
from ns_supervisor import notify, pwsh, schedule

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
    if script.name == "nightshift-host.ps1" or lines[0].startswith("param("):
        # A script with a param() block must start with it; the two guard lines follow the block.
        start = lines.index("Set-StrictMode -Version Latest")
        assert lines[start + 1] == "$ErrorActionPreference = 'Stop'"
        assert any(ln.startswith("param(") for ln in lines[:start])
        if script.name == "nightshift-host.ps1":
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


OPENCODE_JSON_0114 = {      # what setup of 0.1.14 wrote: no limit, no tools, no agent, the baked model name
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


def current_opencode_json() -> dict:
    """What setup writes now for a runtime with no model file and the default context."""
    from installer.stage2.steps.s07_configs import opencode_config

    return opencode_config("127.0.0.1", 8080, 16384, "")


def run_sync(
    tmp_path: Path, runtime: str | None, opencode: str | None, base_url: str | None = None,
    ctx: int | None = None,
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
        target.write_bytes(opencode.encode("utf-8"))   # as given: no newline translation
    call = "Sync-NsOpenCodePort" if base_url is None else f"Sync-NsOpenCodePort -BaseUrl '{base_url}'"
    if ctx is not None:
        call += f" -Ctx {ctx}"
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
    _out, target = run_sync(tmp_path, runtime, json.dumps(current_opencode_json(), indent=2) + "\n")
    raw = target.read_bytes()
    assert raw[:3] != bytes([0xEF, 0xBB, 0xBF])                       # no BOM
    got = json.loads(raw.decode("utf-8"))
    expected = json.loads(json.dumps(current_opencode_json()))
    expected["provider"]["local"]["options"]["baseURL"] = "http://127.0.0.1:9191/v1"
    assert got == expected  # nothing else changed (arrays stay arrays)


@needs_powershell
def test_sync_opencode_port_prefers_the_address_the_engine_reported(tmp_path: Path) -> None:
    """/acquire returns the running llama-server's address; it wins over runtime.json, which may be stale."""
    runtime = json.dumps({"llama": {"host": "127.0.0.1", "port": 9191}})
    text = json.dumps(current_opencode_json(), indent=2) + "\n"
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
    edited = json.loads(json.dumps(current_opencode_json()))
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
    same = json.dumps(current_opencode_json())
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
def test_code_launcher_brings_an_old_opencode_json_up_to_the_budget(tmp_path: Path) -> None:
    """D-34: a 0.1.14 file (no limit, no tools, no agent) gets the budget keys and the install's context;
    an owner's key stays; a second launch leaves the file byte for byte as it is."""
    old = json.loads(json.dumps(OPENCODE_JSON_0114))
    old["theme"] = "x"
    runtime = json.dumps({"llama": {
        "host": "127.0.0.1", "port": 8080, "model_path": r"D:\Night Home\models\llm\Qwen3-1.7B-Q8_0.gguf",
        "flags": {"ctx": 32768}}})
    _out, target = run_sync(tmp_path / "a", runtime, json.dumps(old, indent=2) + "\n")
    got = json.loads(target.read_text(encoding="utf-8"))
    code = got["provider"]["local"]["models"]["code"]
    assert code.get("limit") == {"context": 32768, "output": 4096}
    assert code.get("name") == "Qwen3-1.7B-Q8_0 (local)"
    off = ("todowrite", "todoread", "webfetch", "task", "skill")
    assert got.get("tools") == {name: False for name in off}    # no switch that removes edit or write
    assert got.get("agent", {}).get("build", {}).get("prompt", "").startswith("You are Nightshift Code")
    assert got["theme"] == "x"                                          # the owner's key stays
    assert got["enabled_providers"] == ["local"] and got["provider"]["local"]["options"]["apiKey"] == "local"
    first = target.read_bytes()
    assert first[:3] != bytes([0xEF, 0xBB, 0xBF]) and first.endswith(b"\n")
    # second launch: nothing to change, so the file is not rewritten
    _out, target2 = run_sync(tmp_path / "b", runtime, first.decode("utf-8"))
    assert target2.read_bytes() == first


@needs_powershell
def test_code_launcher_keeps_what_the_owner_set_and_follows_a_changed_context(tmp_path: Path) -> None:
    """Only missing keys and the values Nightshift owns (baseURL, limit.context, model name) are written."""
    cfg = current_opencode_json()
    cfg["tools"]["webfetch"] = True                                     # the owner turned a tool back on
    cfg["agent"]["build"]["prompt"] = "my own prompt"
    cfg["provider"]["local"]["models"]["code"]["limit"] = {"context": 16384, "output": 2048}
    del cfg["tools"]["task"]                                            # a missing tool switch is added
    runtime = json.dumps({"llama": {"flags": {"ctx": 8192}}})           # `ns tune` changed the context
    _out, target = run_sync(tmp_path, runtime, json.dumps(cfg, indent=2) + "\n")
    got = json.loads(target.read_text(encoding="utf-8"))
    assert got["tools"]["webfetch"] is True and got["tools"]["task"] is False
    assert got["agent"]["build"]["prompt"] == "my own prompt"
    assert got["provider"]["local"]["models"]["code"]["limit"] == {"context": 8192, "output": 2048}


# The build prompt an earlier build of 0.1.15 (block B before its install proof) wrote, with "patch": false.
OPENCODE_PROMPT_EARLIER = """You are Nightshift Code, a coding assistant that runs entirely on this computer with a small local model. Nothing you read or write leaves this machine.

Your context is small, so work in small steps and keep every answer short.

Where you work
- Start in the Projects folder of the person using you. Each folder in it is a project. Do not leave it unless you are asked to.
- Never touch the Nightshift program folders or its settings unless you are asked to.

How you work
- Read before you edit. Open a file, look at the part that matters, and only then change it. Do not guess what a file contains.
- Look at a few lines at a time. Search with grep or glob instead of opening whole files or whole folders.
- Change the least you can. Make one edit, then say what you changed and why in a sentence or two.
- Do not rewrite a file that only needs a small change. Do not add features nobody asked for.
- Run a command only when it helps answer the question. Say what you ran and what it showed.
- If a request is unclear, ask one short question before you start.
- If you do not know something, say so. Do not invent file names, functions or results.

How you answer
- Plain, short sentences. No long introductions and no summaries of what you were just told.
- Show code in small blocks, only the lines that matter.
- When a task is done, say so and name the files you changed.
- If a task is too big for the context you have, say so and offer the first small step instead of starting something you cannot finish."""  # noqa: E501
EARLIER_TOOLS = ("todowrite", "todoread", "webfetch", "task", "patch")
SYNC_RUNTIME = json.dumps({"llama": {"host": "127.0.0.1", "port": 8080, "flags": {"ctx": 16384}}})


def earlier_opencode_json() -> dict:
    """What the earlier build's setup wrote: the current shape, its five switches, its longer prompt."""
    cfg = current_opencode_json()
    cfg["tools"] = {name: False for name in EARLIER_TOOLS}
    cfg["agent"]["build"]["prompt"] = OPENCODE_PROMPT_EARLIER
    return cfg


def test_the_earlier_prompt_fixture_is_the_text_the_sync_knows() -> None:
    import hashlib

    from installer.stage2.steps import s07_configs

    digest = hashlib.sha256(OPENCODE_PROMPT_EARLIER.encode("utf-8")).hexdigest()
    assert digest in getattr(s07_configs, "SUPERSEDED_PROMPT_SHA256", ())


@needs_powershell
def test_code_launcher_brings_the_earlier_builds_file_to_what_setup_writes_now(tmp_path: Path) -> None:
    """Block B proof, defect 1: "patch": false removed OpenCode's edit and write tools. A file the earlier
    build wrote ends exactly as a fresh one (edit and write back, the skill tool off, the shorter prompt), an
    owner's key stays, the owner is told once, and a second start changes nothing and says nothing."""
    old = earlier_opencode_json()
    old["theme"] = "x"
    out, target = run_sync(tmp_path / "a", SYNC_RUNTIME, json.dumps(old, indent=2) + "\n")
    got = json.loads(target.read_text(encoding="utf-8"))
    assert "patch" not in got["tools"]
    expected = current_opencode_json() | {"theme": "x"}
    assert got == expected
    assert 'Removed "patch": false' in out and "edit and write" in out and '"edit": false' in out
    first = target.read_bytes()
    out2, target2 = run_sync(tmp_path / "b", SYNC_RUNTIME, first.decode("utf-8"))
    assert target2.read_bytes() == first and out2.strip() == ""


@needs_powershell
def test_code_launcher_takes_back_its_patch_switch_in_a_hand_edited_file_and_keeps_the_rest(
    tmp_path: Path,
) -> None:
    """The earlier sync added "patch": false to a file the owner had edited: the switch goes, the owner's
    prompt, the tool the owner turned back on and the owner's own keys stay."""
    cfg = earlier_opencode_json()
    cfg["tools"]["webfetch"] = True
    cfg["tools"]["bash"] = False                                      # an owner's switch, not Nightshift's
    cfg["agent"]["build"]["prompt"] = "my own prompt"
    cfg["agent"]["build"]["temperature"] = 0.2
    cfg["theme"] = "x"
    out, target = run_sync(tmp_path, SYNC_RUNTIME, json.dumps(cfg, indent=2) + "\n")
    got = json.loads(target.read_text(encoding="utf-8"))
    assert got["tools"] == {"todowrite": False, "todoread": False, "webfetch": True, "task": False,
                            "bash": False, "skill": False}
    assert got["agent"]["build"] == {"prompt": "my own prompt", "temperature": 0.2} and got["theme"] == "x"
    assert 'Removed "patch": false' in out and "cannot change a file:" not in out


@needs_powershell
@pytest.mark.parametrize(("edit", "named"), [
    ({"tools": {"edit": False}}, "tools.edit"),
    ({"tools": {"write": False}}, "tools.write"),
    ({"permission": {"edit": "deny"}}, "permission.edit"),
    ({"agent_tools": {"patch": False}}, "agent.build.tools.patch"),
], ids=["edit", "write", "permission", "agent"])
def test_code_launcher_leaves_an_owners_read_only_choice_and_says_so_at_every_start(
    tmp_path: Path, edit: dict, named: str,
) -> None:
    """The owner's own way of saying "read only" is a key Nightshift never wrote. Then "patch": false is not
    Nightshift's to remove (editing is off either way), nothing of the owner's is lost, and every start says
    which key keeps Nightshift Code from changing a file and how to bring editing back."""
    cfg = earlier_opencode_json()
    cfg["agent"]["build"]["prompt"] = "my own prompt"
    cfg["tools"].update(edit.get("tools", {}))
    if "permission" in edit:
        cfg["permission"] = edit["permission"]
    if "agent_tools" in edit:
        cfg["agent"]["build"]["tools"] = edit["agent_tools"]
    out, target = run_sync(tmp_path / "a", SYNC_RUNTIME, json.dumps(cfg, indent=2) + "\n")
    got = json.loads(target.read_text(encoding="utf-8"))
    expected = json.loads(json.dumps(cfg))
    expected["tools"]["skill"] = False                                  # the one missing switch is added
    assert got == expected and got["tools"]["patch"] is False
    assert "Removed" not in out
    assert "cannot change a file:" in out and named in out and "start Nightshift Code again" in out
    first = target.read_bytes()
    out2, target2 = run_sync(tmp_path / "b", SYNC_RUNTIME, first.decode("utf-8"))
    assert target2.read_bytes() == first and named in out2             # told again, file untouched


@needs_powershell
def test_code_launcher_brings_a_file_with_empty_blocks_up_instead_of_failing(tmp_path: Path) -> None:
    """A hand-edited file with `"tools": {}` or an empty build agent, and a runtime.json that is `{}`: under
    strict mode Windows PowerShell 5.1 fails on the names of an object that has none, and the whole sync was
    skipped with a note at every start. The blocks are filled like missing ones and nothing is said."""
    cfg = earlier_opencode_json()
    cfg["tools"] = {}
    cfg["agent"] = {"build": {}}
    cfg["provider"]["local"]["models"]["code"] = {}
    out, target = run_sync(tmp_path / "a", "{}", json.dumps(cfg, indent=2) + "\n")
    assert out.strip() == "", out
    assert json.loads(target.read_text(encoding="utf-8")) == current_opencode_json()
    first = target.read_bytes()
    out2, target2 = run_sync(tmp_path / "b", "{}", first.decode("utf-8"))
    assert target2.read_bytes() == first and out2.strip() == ""


@needs_powershell
def test_code_launcher_leaves_a_patch_switch_turned_on_and_a_fresh_file_alone(tmp_path: Path) -> None:
    cfg = current_opencode_json()
    cfg["tools"]["patch"] = True
    text = json.dumps(cfg, indent=2) + "\n"
    out, target = run_sync(tmp_path / "a", SYNC_RUNTIME, text)
    assert target.read_text(encoding="utf-8") == text and out.strip() == ""
    fresh = json.dumps(current_opencode_json(), indent=2) + "\n"
    out, target = run_sync(tmp_path / "b", SYNC_RUNTIME, fresh)
    assert target.read_text(encoding="utf-8") == fresh and out.strip() == ""


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
        self.engine_ctx: int | None = None    # what GET /status says llama runs with (None: not reported)
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def _reply(self) -> None:
                body = b'{"state": "running"}'
                if self.path == "/status" and engine.engine_ctx is not None:
                    llama = {"state": "stopped", "ctx": engine.engine_ctx}
                    body = json.dumps({"services": {"llama": llama}}).encode()
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


def test_code_launcher_sends_its_own_pid_in_the_acquire_body() -> None:
    """F-H47: the supervisor frees the GPU when the console that holds it is gone (closed with X)."""
    text = read("nightshift-code.ps1")
    body = next(ln for ln in text.splitlines() if ln.strip().startswith("$body = @{"))
    assert "owner = $owner" in body and "pid = $PID" in body, body
    assert text.index("$body = @{") < text.index("$ep = Invoke-NsPost -Path '/acquire' -Body $body")
    assert "finally" in text[text.index("OPENCODE_CONFIG") :]
    assert text.rindex("finally") < text.rindex("/release")


@needs_powershell
def test_code_launcher_run_posts_its_pid_with_the_acquire_and_releases_after_opencode_exits(
    engine: tuple[FakeEngine, dict[str, str]], tmp_path: Path
) -> None:
    """F-H47 under powershell.exe: the /acquire body carries the launcher's own $PID; /release follows."""
    import os
    import re
    import shutil

    eng, env = engine
    opencode = tmp_path / "bin" / "opencode" / "opencode.exe"
    opencode.parent.mkdir(parents=True)
    system32 = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32"
    shutil.copy(system32 / "hostname.exe", opencode)  # stands in for OpenCode: prints a line and exits
    env = {**env, "USERPROFILE": str(tmp_path / "profile")}  # the launcher makes %USERPROFILE%\Projects
    script = f"Write-Output ('launcher-pid=' + $PID)\n& '{LAUNCHERS / 'nightshift-code.ps1'}'\n"
    r = pwsh.run_powershell(script, env_extra=env, timeout_s=90)
    assert r.returncode == 0, r.stdout + r.stderr
    found = re.search(r"launcher-pid=(\d+)", r.stdout)
    assert found, r.stdout
    assert [p for p, _ in eng.posts] == ["/acquire", "/release"], eng.posts
    acquire = eng.posts[0][1]
    assert isinstance(acquire, dict) and acquire["owner"] == "opencode" and acquire["model_alias"] == "code"
    assert acquire.get("pid") == int(found.group(1)), acquire  # an integer, the console's own process
    assert eng.posts[1][1] == {"service": "llama", "owner": "opencode"}


def code_launcher_run(
    eng: FakeEngine, env: dict[str, str], home: Path, ctx_tokens: int | None
) -> subprocess.CompletedProcess[str]:
    """Run nightshift-code.ps1 under powershell.exe, OpenCode stood in for by hostname.exe."""
    import os
    import shutil

    runtime: dict[str, object] = {"supervisor_port": eng.port}
    if ctx_tokens is not None:
        runtime["llama"] = {"flags": {"ctx": ctx_tokens}}
    (home / "config" / "runtime.json").write_text(json.dumps(runtime), encoding="utf-8")
    opencode = home / "bin" / "opencode" / "opencode.exe"
    opencode.parent.mkdir(parents=True, exist_ok=True)
    system32 = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32"
    shutil.copy(system32 / "hostname.exe", opencode)
    env = {**env, "USERPROFILE": str(home / "profile")}
    return pwsh.run_powershell(f"& '{LAUNCHERS / 'nightshift-code.ps1'}'\n", env_extra=env, timeout_s=90)


@needs_powershell
def test_code_launcher_refuses_a_context_under_16384_before_taking_the_gpu(
    engine: tuple[FakeEngine, dict[str, str]], tmp_path: Path
) -> None:
    """D-34: OpenCode's first request needs about half of a 16k context; refuse at launch, take nothing."""
    eng, env = engine
    refused = code_launcher_run(eng, env, tmp_path, 8192)
    assert "Nightshift Code needs 16384 tokens of context; this install has 8192" in refused.stdout, (
        refused.stdout + refused.stderr)
    assert refused.returncode != 0
    assert eng.posts == [], eng.posts                                   # no /acquire, nothing to release
    # failed, then retried after `ns tune` raised the context: it acquires as before
    ok = code_launcher_run(eng, env, tmp_path, 16384)
    assert ok.returncode == 0 and "needs 16384" not in ok.stdout, ok.stdout + ok.stderr
    assert [p for p, _ in eng.posts] == ["/acquire", "/release"], eng.posts


@needs_powershell
def test_code_launcher_refuses_on_the_context_the_engine_runs_not_the_one_in_the_file(
    engine: tuple[FakeEngine, dict[str, str]], tmp_path: Path
) -> None:
    """D-34 review: the engine read runtime.json when it started. The owner raised the file to 16384 as the
    refusal said, but the running engine still loads 8192: the launcher must not pass, and must say so."""
    eng, env = engine
    eng.engine_ctx = 8192
    refused = code_launcher_run(eng, env, tmp_path, 16384)       # the file says 16384, the engine runs 8192
    out = refused.stdout
    assert "Nightshift Code needs 16384 tokens of context; the running engine has 8192" in out, out
    assert "config\\runtime.json says 16384" in out and "Stop All" in out and "nightly" in out, out
    assert refused.returncode != 0 and eng.posts == [], (refused.stdout, eng.posts)
    # file and engine both too small: the way out names the file edit AND the restart
    both = code_launcher_run(eng, env, tmp_path, 8192)
    assert "this install has 8192" in both.stdout and "llama.flags.ctx" in both.stdout, both.stdout
    assert "Stop All" in both.stdout and "nightly" in both.stdout and eng.posts == [], both.stdout


@needs_powershell
def test_code_launcher_passes_when_the_engine_runs_16384_and_tells_opencode_that_number(
    engine: tuple[FakeEngine, dict[str, str]], tmp_path: Path
) -> None:
    eng, env = engine
    eng.engine_ctx = 16384
    old = tmp_path / "config" / "opencode.json"
    old.write_text(json.dumps(OPENCODE_JSON_0114), encoding="utf-8")
    ok = code_launcher_run(eng, env, tmp_path, 8192)             # file lower than the engine: engine counts
    assert ok.returncode == 0 and "needs 16384" not in ok.stdout, ok.stdout + ok.stderr
    assert [p for p, _ in eng.posts] == ["/acquire", "/release"], eng.posts
    limit = json.loads(old.read_text(encoding="utf-8"))["provider"]["local"]["models"]["code"]["limit"]
    assert limit == {"context": 16384, "output": 4096}
    eng.posts.clear()
    eng.engine_ctx = 32768                                       # a bigger engine: OpenCode is told 32768
    ok2 = code_launcher_run(eng, env, tmp_path, 16384)
    assert ok2.returncode == 0, ok2.stdout + ok2.stderr
    limit = json.loads(old.read_text(encoding="utf-8"))["provider"]["local"]["models"]["code"]["limit"]
    assert limit["context"] == 32768


@needs_powershell
def test_code_launcher_with_no_engine_running_refuses_on_the_file_and_starts_nothing(
    engine: tuple[FakeEngine, dict[str, str]], tmp_path: Path
) -> None:
    """No engine: the one this launcher would start reads the file, so the file's number counts. No restart
    is asked for (there is nothing to restart) and nothing is started."""
    eng, env = engine
    eng.close()
    refused = code_launcher_run(eng, env, tmp_path, 8192)
    assert "Nightshift Code needs 16384 tokens of context; this install has 8192" in refused.stdout, (
        refused.stdout + refused.stderr)
    assert "llama.flags.ctx" in refused.stdout and "Stop All" not in refused.stdout
    assert "nightly" in refused.stdout and refused.returncode != 0


@needs_powershell
def test_the_old_json_with_a_null_limit_still_gets_the_context(tmp_path: Path) -> None:
    """An owner's `"limit": null` (or tools or agent set to null) counts as missing; the sync goes on."""
    cfg = json.loads(json.dumps(OPENCODE_JSON_0114))
    cfg["provider"]["local"]["models"]["code"]["limit"] = None
    cfg["tools"] = None
    cfg["agent"] = {"build": None}
    _out, target = run_sync(tmp_path, json.dumps({"llama": {"flags": {"ctx": 32768}}}), json.dumps(cfg))
    got = json.loads(target.read_text(encoding="utf-8"))
    assert got["provider"]["local"]["models"]["code"]["limit"] == {"context": 32768, "output": 4096}
    assert got["tools"]["webfetch"] is False
    assert got["agent"]["build"]["prompt"].startswith("You are Nightshift Code")


@needs_powershell
def test_sync_takes_the_context_the_engine_runs_over_the_file(tmp_path: Path) -> None:
    runtime = json.dumps({"llama": {"flags": {"ctx": 16384}}})
    text = json.dumps(OPENCODE_JSON_0114)
    _out, target = run_sync(tmp_path, runtime, text, ctx=32768)
    limit = json.loads(target.read_text(encoding="utf-8"))["provider"]["local"]["models"]["code"]["limit"]
    assert limit["context"] == 32768


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


def host_cache_reset(tmp_path: Path, version: str | None, profile: Path) -> str:
    """Run `Reset-StaleHttpCache` from nightshift-host.ps1 on its own and return what it printed."""
    import re

    text = read("nightshift-host.ps1")
    m = re.search(r"^function Reset-StaleHttpCache \{.*?^\}\n", text, re.S | re.M)
    assert m, "nightshift-host.ps1 has no Reset-StaleHttpCache"
    app = tmp_path / "app"
    app.mkdir(parents=True, exist_ok=True)
    if version is not None:
        (app / "VERSION").write_text(version + "\n", encoding="ascii")
    # the host dot-sources _common.ps1 (Read-NsTextRetry, Write-NsFileAtomic) before it defines this function
    common = LAUNCHERS / "_common.ps1"
    call = f"Write-Output (Reset-StaleHttpCache -ProfileDir '{profile}' -AppDir '{app}')"
    script = f". '{common}'\n{m.group(0)}\n{call}\n"
    r = pwsh.run_powershell(script, timeout_s=90)
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout.strip().splitlines()[-1]


def make_profile(profile: Path) -> dict[str, Path]:
    files = {
        "cache": profile / "EBWebView" / "Default" / "Cache" / "Cache_Data" / "f_000001",
        "code": profile / "EBWebView" / "Default" / "Code Cache" / "js" / "0",
        "storage": profile / "EBWebView" / "Default" / "Local Storage" / "leveldb" / "000003.log",
    }
    for f in files.values():
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    return files


@needs_powershell
def test_the_host_clears_the_http_cache_once_per_app_version_and_keeps_the_profile(tmp_path: Path) -> None:
    """D-29: the profile (state/webview2) outlives an update, so its cache holds the old page and bundle."""
    profile = tmp_path / "state" / "webview2"
    files = make_profile(profile)
    assert host_cache_reset(tmp_path, "0.1.15", profile) == "True"
    assert not files["cache"].parent.parent.exists() and not files["code"].parent.parent.exists()
    assert files["storage"].exists()  # settings and storage are kept
    assert (profile / "ns-cache-version.txt").read_text(encoding="ascii").strip() == "0.1.15"
    files = make_profile(profile)  # the window caches again
    assert host_cache_reset(tmp_path, "0.1.15", profile) == "False"  # same version: left alone
    assert files["cache"].exists()
    assert host_cache_reset(tmp_path, "0.1.16", profile) == "True"  # after the next update
    assert not files["cache"].exists() and files["storage"].exists()


@needs_powershell
def test_the_host_cache_reset_never_stops_the_window_from_starting(tmp_path: Path) -> None:
    profile = tmp_path / "state" / "webview2"
    assert host_cache_reset(tmp_path / "none", None, profile) == "False"  # no VERSION file
    assert host_cache_reset(tmp_path, "0.1.15", profile) == "True"  # no profile yet: stamp only
    assert (profile / "ns-cache-version.txt").exists()


@needs_powershell
@pytest.mark.skipif(sys.platform != "win32", reason="an open file blocks a delete only on Windows")
def test_a_cache_that_is_still_in_use_is_left_alone_and_retried_at_the_next_start(tmp_path: Path) -> None:
    """Another window of the same profile is open: nothing is half-stamped, the next start clears it."""
    profile = tmp_path / "state" / "webview2"
    files = make_profile(profile)
    with files["cache"].open("rb"):
        assert host_cache_reset(tmp_path, "0.1.15", profile) == "False"
        assert not (profile / "ns-cache-version.txt").exists() and files["storage"].exists()
    assert host_cache_reset(tmp_path, "0.1.15", profile) == "True"
    assert not files["cache"].exists()


def test_host_script_does_not_change_the_browser_arguments() -> None:
    """The profile is shared by the dashboard and setup roles and by every version: WebView2 refuses to start
    in a user data folder whose browser process was created with other arguments, so the host sets none."""
    text = read("nightshift-host.ps1")
    assert "AdditionalBrowserArguments" not in text and "--disable-http-cache" not in text
    call = text.index("Reset-StaleHttpCache -ProfileDir")
    assert call < text.index("$script:wv.CreationProperties = $props")


# ---- card 206-carried: what the shortcuts start when the program folder may be missing ----------------
STUB = LAUNCHERS.parent / "installer" / "launch.ps1"


def _run_stub(launcher: Path) -> "subprocess.CompletedProcess[str]":
    exe = pwsh.find_powershell()
    assert exe is not None
    argv = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(STUB), "-Launcher", str(launcher)]
    return subprocess.run(argv, input="\n", capture_output=True, text=True, timeout=120)


@needs_powershell
def test_the_shortcut_script_says_what_to_do_when_the_program_folder_is_missing(tmp_path: Path) -> None:
    """No `app` (an interrupted rollback): the launcher is gone with it. The window stays open and says to
    run INSTALL.cmd from the zip; it needs no Python and nothing from the program folder."""
    home = tmp_path / "Jo's home"
    (home / "app.swap").mkdir(parents=True)
    proc = _run_stub(home / "app" / "launchers" / "nightshift-dashboard.ps1")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "Nightshift cannot start: its program folder is missing or incomplete." in proc.stdout
    assert "Run INSTALL.cmd from a Nightshift zip of version 0.1.15 or later" in proc.stdout
    assert f"The program is waiting in {home / 'app.swap'}" in proc.stdout
    assert proc.stderr.strip() == ""


@needs_powershell
def test_the_shortcut_script_runs_the_launcher_and_passes_its_exit_code(tmp_path: Path) -> None:
    launcher = tmp_path / "Jo's home" / "app" / "launchers" / "x.ps1"
    launcher.parent.mkdir(parents=True)
    body = "Set-Content -LiteralPath (Join-Path $PSScriptRoot 'ran.txt') -Value 'yes'\r\nexit 7\r\n"
    launcher.write_text(body, encoding="ascii")
    proc = _run_stub(launcher)
    assert proc.returncode == 7, proc.stdout + proc.stderr
    assert (launcher.parent / "ran.txt").read_text(encoding="ascii").strip() == "yes"
    assert "cannot start" not in proc.stdout


def test_the_shortcut_script_keeps_the_rules_of_every_ps1() -> None:
    raw = STUB.read_bytes()
    text = raw.decode("ascii")  # ASCII only
    assert "Set-StrictMode -Version Latest" in text and "$ErrorActionPreference = 'Stop'" in text
    # it starts no Python (the words "Python environment" are what it tells the owner; a replayed record runs
    # uv, whose command lines come from the record)
    assert "Invoke-Expression" not in text
    assert not re.search(r"python(w)?\.exe|-m ns_|\bpy\.exe", text.lower())


# ---- card launcher-reach: the nightly task runs through the same script, and a missed night is said ----
# Every run here is a test run (NS_UNDER_TEST is inherited): the script shows no toast, and writes its
# Desktop note only into the folder -DesktopDir names.
HARD = "Jo's Nightshift (2) ليلة"
NOTE = "Nightshift did not run.txt"
TODAY = date.today().isoformat()


def _home(tmp_path: Path, *, app: bool = True) -> Path:
    """An install home with the script beside the program folder, as setup and the first start leave it."""
    home = tmp_path / HARD
    for name in ("config", "run", "logs"):
        (home / name).mkdir(parents=True)
    shutil.copy(STUB, home / "nightshift-launch.ps1")
    if app:
        (home / "app" / "ns_studio").mkdir(parents=True)
        (home / "app" / "ns_studio" / "cli.py").write_text("", encoding="ascii")
    return home


def _studio(tmp_path: Path) -> Path:
    """A stand-in for studio.exe: it notes that it was asked for the nightly run of the right home; exit 5."""
    cmd = tmp_path / "studio.cmd"
    line = 'if "%~1 %~2"=="run --daily" if exist "%NIGHTSHIFT_HOME%\\config" echo seen>"%~dp0seen.txt"'
    cmd.write_bytes(f"@echo off\r\n{line}\r\nexit /b 5\r\n".encode("ascii"))
    return cmd


def _stub(home: Path, *args: object) -> "subprocess.CompletedProcess[str]":
    exe = pwsh.find_powershell()
    assert exe is not None
    script = str(home / "nightshift-launch.ps1")
    argv = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, *(str(a) for a in args)]
    env = {**os.environ, "NIGHTSHIFT_HOME": str(home)}
    return subprocess.run(argv, input="\n", capture_output=True, text=True, timeout=120, env=env)


def _lines(home: Path) -> list[dict]:
    out: list[dict] = []
    for f in sorted((home / "logs").glob("launch-*.jsonl")):
        raw = f.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")       # UTF-8 without a byte order mark, like every log
        out += [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    return out


def _missed(home: Path) -> dict:
    return json.loads((home / "run" / "launch-missed.json").read_text(encoding="utf-8-sig"))


@needs_powershell
def test_the_nightly_switch_runs_studio_and_passes_its_exit_code(tmp_path: Path) -> None:
    home, studio = _home(tmp_path), _studio(tmp_path)
    proc = _stub(home, "-Nightly", "-Studio", studio)
    assert proc.returncode == 5, proc.stdout + proc.stderr
    assert (tmp_path / "seen.txt").exists()              # `run --daily`, with the home in its environment
    assert _lines(home) == [] and not (home / "run" / "launch-missed.json").exists()


@needs_powershell
def test_the_nightly_switch_without_the_program_folder_tells_the_owner_each_night(tmp_path: Path) -> None:
    home, studio, desk = _home(tmp_path, app=False), _studio(tmp_path), tmp_path / "Desk"
    (home / "app.swap").mkdir()
    desk.mkdir()
    proc = _stub(home, "-Nightly", "-Studio", studio, "-DesktopDir", desk)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert not (tmp_path / "seen.txt").exists()
    (line,) = _lines(home)
    assert (line["level"], line["component"]) == ("WARN", "launch")
    assert line["data"] == {
        "code": "NS-W209", "home": str(home), "first": TODAY, "last": TODAY, "nights": 1, "what": "folder",
        "toast": "test-run", "desktop_note": "written",
    }
    assert _missed(home) == {"first": TODAY, "last": TODAY, "nights": 1, "what": "folder"}
    note = (desk / NOTE).read_text(encoding="utf-8-sig")
    assert note.splitlines()[0] == (
        f"Nightshift could not start its nightly run on {TODAY} (1 night so far, the first on {TODAY}). "
        "(NS-W209)"
    )
    assert f"Its program folder {home}\\app is missing or incomplete." in note
    assert f"The program is waiting in {home}\\app.swap and nothing in it was deleted." in note
    assert "2. Run INSTALL.cmd from a Nightshift zip of version 0.1.15 or later and choose the same" in note
    assert f"folder ({home}). Setup puts the program folder back." in note
    assert "this file is removed the next time Nightshift starts" in note
    assert "disable 'Nightshift Daily' in Task Scheduler" in note and "move it back" not in note
    # the next night: the record of the night before is there (also when the run was killed before its note)
    before = (date.today() - timedelta(days=1)).isoformat()
    record = home / "run" / "launch-missed.json"
    night = {"first": before, "last": before, "nights": 1, "what": "folder"}
    record.write_text(json.dumps(night), encoding="ascii")
    (desk / NOTE).unlink()
    assert _stub(home, "-Nightly", "-Studio", studio, "-DesktopDir", desk).returncode == 1
    assert len(_lines(home)) == 2 and _missed(home) == {**night, "last": TODAY, "nights": 2}
    assert (desk / NOTE).read_text(encoding="utf-8-sig").splitlines()[0] == (
        f"Nightshift could not start its nightly run on {TODAY} (2 nights so far, the first on {before}). "
        "(NS-W209)"
    )
    # the task started twice in one night: one more log line, not one more night, still one note
    assert _stub(home, "-Nightly", "-Studio", studio, "-DesktopDir", desk).returncode == 1
    assert len(_lines(home)) == 3 and _missed(home)["nights"] == 2
    assert [p.name for p in desk.iterdir()] == [NOTE]
    # a Desktop that is not there, and a file of that name that is the owner's own: said in the log line
    nowhere = tmp_path / "nowhere"
    assert _stub(home, "-Nightly", "-Studio", studio, "-DesktopDir", nowhere).returncode == 1
    assert _lines(home)[-1]["data"]["desktop_note"].startswith("failed: ")
    (desk / NOTE).write_text("my own notes", encoding="ascii")
    assert _stub(home, "-Nightly", "-Studio", studio, "-DesktopDir", desk).returncode == 1
    assert (desk / NOTE).read_text(encoding="ascii") == "my own notes"
    assert _lines(home)[-1]["data"]["desktop_note"].startswith("failed: ")
    # the program is there and its environment is not: said as that
    (home / "app" / "ns_studio").mkdir(parents=True)
    (home / "app" / "ns_studio" / "cli.py").write_text("", encoding="ascii")
    (desk / NOTE).unlink()
    record.unlink()
    no_exe = home / "venvs" / "studio.exe"
    assert _stub(home, "-Nightly", "-Studio", no_exe, "-DesktopDir", desk).returncode == 1
    assert _missed(home)["what"] == "environment"
    assert "Its program environment is missing or incomplete" in (desk / NOTE).read_text("utf-8-sig")


@needs_powershell
def test_the_first_start_with_the_program_back_leaves_one_morning_note_and_clears_the_rest(
    tmp_path: Path,
) -> None:
    home, desk = _home(tmp_path, app=False), tmp_path / "Desk"
    desk.mkdir()
    assert _stub(home, "-Nightly", "-Studio", _studio(tmp_path), "-DesktopDir", desk).returncode == 1
    launcher = home / "app" / "launchers" / "x.ps1"
    launcher.parent.mkdir(parents=True)                  # setup put the program folder back
    body = "Add-Content -LiteralPath (Join-Path $PSScriptRoot 'ran.txt') -Value 'x'\r\nexit 0\r\n"
    launcher.write_text(body, encoding="ascii")
    # the note cannot be written: the record stays, and the launcher runs all the same
    blocked = home / "run" / "morning_note.json"
    blocked.mkdir()
    proc = _stub(home, "-Launcher", launcher, "-DesktopDir", desk)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (home / "run" / "launch-missed.json").exists() and (desk / NOTE).exists()
    blocked.rmdir()
    assert _stub(home, "-Launcher", launcher, "-DesktopDir", desk).returncode == 0
    said = (
        f"Nightshift did not run on 1 night, from {TODAY} to {TODAY}: its program folder was missing until "
        f"it was put back on {TODAY}."
    )
    assert [n["message"] for n in notify.read_notes(Paths(home))] == [said]
    assert not (desk / NOTE).exists() and not (home / "run" / "launch-missed.json").exists()
    assert _stub(home, "-Launcher", launcher, "-DesktopDir", desk).returncode == 0
    assert len(notify.read_notes(Paths(home))) == 1
    assert (launcher.parent / "ran.txt").read_text(encoding="ascii").split() == ["x", "x", "x"]


@needs_powershell
def test_under_a_test_run_the_stub_shows_no_toast_and_writes_no_real_desktop(tmp_path: Path) -> None:
    home = _home(tmp_path, app=False)
    assert os.environ.get("NS_UNDER_TEST")
    assert _stub(home, "-Nightly", "-Studio", _studio(tmp_path)).returncode == 1
    (line,) = _lines(home)
    assert line["data"]["toast"] == line["data"]["desktop_note"] == "test-run"
    # a dev install never names the real Desktop either: said as that, with or without a test run
    (home / "config" / "runtime.json").write_text(json.dumps({"dev_mode": True}), encoding="utf-8")
    assert _stub(home, "-Nightly", "-Studio", _studio(tmp_path)).returncode == 1
    assert _lines(home)[-1]["data"]["desktop_note"] == "dev-install"
    desk = tmp_path / "Desk"
    desk.mkdir()                                         # the folder a proof hands it is still written
    assert _stub(home, "-Nightly", "-Studio", _studio(tmp_path), "-DesktopDir", desk).returncode == 1
    assert _lines(home)[-1]["data"]["desktop_note"] == "written" and (desk / NOTE).exists()


@needs_powershell
def test_the_task_action_runs_under_cmd_with_a_hard_home(tmp_path: Path) -> None:
    action = getattr(schedule, "action_arguments", None)
    assert action is not None, "the task's action is still built inline"
    home = _home(tmp_path, app=False)
    # one command line, as Task Scheduler starts it (a list would be quoted again by Python)
    proc = subprocess.run("cmd.exe " + action(Paths(home)), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    (line,) = _lines(home)
    assert line["data"]["code"] == "NS-W209" and line["data"]["home"] == str(home)


@needs_powershell
def test_without_the_stub_file_the_task_action_still_starts_studio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    action = getattr(schedule, "action_arguments", None)
    assert action is not None, "the task's action is still built inline"
    home, studio = _home(tmp_path), _studio(tmp_path)
    monkeypatch.setattr(schedule, "studio_exe", lambda paths: str(studio))
    proc = subprocess.run("cmd.exe " + action(Paths(home)), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 5 and (tmp_path / "seen.txt").exists(), proc.stdout + proc.stderr  # by the stub
    (tmp_path / "seen.txt").unlink()
    (home / "nightshift-launch.ps1").unlink()
    proc = subprocess.run("cmd.exe " + action(Paths(home)), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 5 and (tmp_path / "seen.txt").exists(), proc.stdout + proc.stderr  # as 0.1.14


@needs_powershell
def test_the_stub_names_the_zip_version_that_puts_the_program_back(tmp_path: Path) -> None:
    home, desk = _home(tmp_path, app=False), tmp_path / "Desk"
    desk.mkdir()
    proc = _stub(home, "-Launcher", home / "app" / "launchers" / "nightshift-dashboard.ps1")
    assert proc.returncode == 1
    assert "Run INSTALL.cmd from a Nightshift zip of version 0.1.15 or later" in proc.stdout
    assert "move it back" not in proc.stdout
    (home / "app.rollback-pending.json").write_text("{}", encoding="ascii")   # an interrupted rollback, and
    proc = _stub(home, "-Launcher", home / "app" / "launchers" / "nightshift-dashboard.ps1")   # no app.swap
    assert "If you moved the folder app or app.swap somewhere else, move it back to " in proc.stdout
    assert "before you run INSTALL.cmd: otherwise setup starts a fresh program folder." in proc.stdout
    assert _stub(home, "-Nightly", "-Studio", _studio(tmp_path), "-DesktopDir", desk).returncode == 1
    assert (
        f"If you moved the folder app or app.swap somewhere else, move it back to {home}\\app.swap before "
        "you run INSTALL.cmd: otherwise setup starts a fresh program folder."
    ) in (desk / NOTE).read_text(encoding="utf-8-sig")


# ---- card D-39b: nothing starts on an environment an update left unfinished, nor while one runs ----------
# The record and the state file are written here in the shape the update worker writes them
# (installer/stage2/web/venvrepair.py write, ns_supervisor/update_job.py write_status).
MARKER = "0.2.0|aaaaaaaaaaaaaaaa|bbbbbbbbbbbbbbbb"
RUN_EXIT = 0


def _program_home(tmp_path: Path) -> tuple[Path, Path]:
    """An install home on 0.2.0 with two venvs and a launcher stand-in that notes each start."""
    home = _home(tmp_path)
    app = home / "app"
    (app / "VERSION").write_text("0.2.0\n", encoding="ascii")
    (app / ".ns-app.done").write_text(MARKER, encoding="ascii")
    launcher = app / "launchers" / "x.ps1"
    launcher.parent.mkdir(parents=True)
    body = "Add-Content -LiteralPath (Join-Path $PSScriptRoot 'ran.txt') -Value 'x'\r\nexit 0\r\n"
    launcher.write_text(body, encoding="ascii")
    for venv in ("studio", "supervisor"):
        (home / "venvs" / venv / "Scripts").mkdir(parents=True)
        (home / "venvs" / venv / "Scripts" / "python.exe").write_bytes(b"MZ")
    return home, launcher


def _uv(tmp_path: Path, *, fail: bool = False) -> Path:
    """A stand-in for uv.exe: it writes its environment's UV_LINK_MODE and its arguments, one line a call."""
    lines = ["@echo off", 'echo %UV_LINK_MODE% %*>>"%~dp0uv-ran.txt"']
    if fail:
        lines += [
            "echo Resolved 1 package 1>&2",
            "echo error: failed to remove file `ns.exe`: The process cannot access the file because it is "
            "being used by another process. (os error 32) 1>&2",
            "exit /b 1",
        ]
    else:
        lines.append("exit /b 0")
    cmd = tmp_path / "uv.cmd"
    cmd.write_bytes(("\r\n".join(lines) + "\r\n").encode("ascii"))
    return cmd


def _record(home: Path, uv: Path, **over: object) -> Path:
    app = home / "app"

    def argv(venv: str) -> list[str]:
        py = home / "venvs" / venv / "Scripts" / "python.exe"
        return [str(uv), "pip", "install", "-e", f"{app}[{venv}]", "--python", str(py)]

    data = {
        "schema": 1, "app": str(app), "match": "marker", "for_app": MARKER, "version": "0.2.0",
        "by": "update",
        "written": "2026-11-10T03:00:00", "uv": str(uv), "env": {"UV_LINK_MODE": "copy"},
        "commands": [{"venv": v, "argv": argv(v)} for v in ("studio", "supervisor")], **over,
    }
    target = home / "run" / "venv-repair.json"
    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return target


def _ran(tmp_path: Path) -> list[str]:
    f = tmp_path / "uv-ran.txt"
    return f.read_text(encoding="ascii", errors="replace").splitlines() if f.exists() else []


def _starts(launcher: Path) -> int:
    f = launcher.parent / "ran.txt"
    return len(f.read_text(encoding="ascii").split()) if f.exists() else 0


def _words(text: str) -> str:
    return " ".join(text.split())


@needs_powershell
def test_the_stub_finishes_an_interrupted_sync_before_starting_anything(tmp_path: Path) -> None:
    home, launcher = _program_home(tmp_path)
    record = _record(home, _uv(tmp_path))
    proc = _stub(home, "-Launcher", launcher)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    ran = _ran(tmp_path)
    assert len(ran) == 2, f"the launcher started on a half-filled environment (uv ran {ran})"
    assert ran[0].startswith("copy pip install -e ") and "studio" in ran[0] and "supervisor" in ran[1]
    assert (tmp_path / "uv-ran.txt").stat().st_mtime_ns <= (launcher.parent / "ran.txt").stat().st_mtime_ns
    assert _starts(launcher) == 1 and not record.exists()
    said = (f"An interrupted update was finished on {TODAY}: the Python environments 'studio', 'supervisor' "
            "now match Nightshift 0.2.0.")
    assert [n["message"] for n in notify.read_notes(Paths(home))] == [said]
    # the next start has nothing to finish: no uv, no second note
    assert _stub(home, "-Launcher", launcher).returncode == 0
    assert len(_ran(tmp_path)) == 2 and _starts(launcher) == 2 and len(notify.read_notes(Paths(home))) == 1
    # the nightly task finishes it the same way before the night runs
    _record(home, _uv(tmp_path))
    assert _stub(home, "-Nightly", "-Studio", _studio(tmp_path)).returncode == 5
    assert len(_ran(tmp_path)) == 4 and (tmp_path / "seen.txt").exists() and not record.exists()


@needs_powershell
def test_a_failed_repair_is_said_and_nothing_starts(tmp_path: Path) -> None:
    home, launcher = _program_home(tmp_path)
    record = _record(home, _uv(tmp_path, fail=True))
    proc = _stub(home, "-Launcher", launcher)
    assert proc.returncode == 1 and _starts(launcher) == 0, "Nightshift started on a half-filled environment"
    assert _words(
        "Nightshift could not finish an interrupted update (NS-W210): its Python environment 'studio' "
        "does not match the program yet. uv said: error: failed to remove file `ns.exe`: The process "
        "cannot access the "
        "file because it is being used by another process. (os error 32) Close every Nightshift window and "
        "open Nightshift again. If it fails again, run INSTALL.cmd from the Nightshift 0.2.0 zip and choose "
        "this folder (Repair)."
    ) in _words(proc.stdout)
    assert record.exists() and len(_ran(tmp_path)) == 1
    # the nightly run: a missed night with that reason, the record kept for the next start
    desk = tmp_path / "Desk"
    desk.mkdir()
    proc = _stub(home, "-Nightly", "-Studio", _studio(tmp_path), "-DesktopDir", desk)
    assert proc.returncode == 1 and not (tmp_path / "seen.txt").exists() and record.exists()
    assert _missed(home)["what"] == "repair"
    line = _lines(home)[-1]
    assert line["data"]["code"] == "NS-W210" and "(os error 32)" in line["data"]["reason"]
    note = (desk / NOTE).read_text(encoding="utf-8-sig")
    assert "(NS-W210)" in note.splitlines()[0] and "uv said: error: failed to remove file" in note
    # a record that cannot be read is treated as there: nothing starts, and it is said
    record.write_text("{torn", encoding="utf-8")
    proc = _stub(home, "-Launcher", launcher)
    assert proc.returncode == 1 and _starts(launcher) == 0
    said = _words(proc.stdout)      # (the home's non-ASCII letters may not survive the console)
    assert "(NS-W210): the record " in said and "venv-repair.json cannot be read" in said
    assert record.exists()


@needs_powershell
def test_a_stale_record_is_dropped_by_the_stub(tmp_path: Path) -> None:
    home, launcher = _program_home(tmp_path)
    record = _record(home, _uv(tmp_path), for_app="0.1.9|cccc|dddd", version="0.1.9")   # a later INSTALL.cmd
    assert _stub(home, "-Launcher", launcher).returncode == 0
    assert _ran(tmp_path) == [] and not record.exists() and _starts(launcher) == 1
    assert [x["event"] for x in _lines(home)] == ["venv_record_stale"]
    assert notify.read_notes(Paths(home)) == []


def _sleeper(seconds: float) -> "subprocess.Popen[bytes]":
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _running(home: Path, pid: int, **extra: object) -> None:
    state = {"state": "running", "old": "0.2.0", "new": "", "message": "Updating.",
             "updated_at": "2026-11-10T03:00:00", "log": str(home / "logs" / "update-x.log"), "pid": pid,
             **extra}
    (home / "run" / "update_state.json").write_text(json.dumps(state), encoding="utf-8")


@needs_powershell
def test_the_nightly_task_during_an_update_waits_then_runs(tmp_path: Path) -> None:
    home, launcher = _program_home(tmp_path)
    studio, desk = _studio(tmp_path), tmp_path / "Desk"
    desk.mkdir()
    procs: list[subprocess.Popen] = []
    try:
        # an update of an older release (it takes no lock) ends inside the bound: the night runs after it
        procs.append(_sleeper(4))
        _running(home, procs[-1].pid)
        proc = _stub(home, "-Nightly", "-Studio", studio, "-UpdateWaitSeconds", 60)
        assert proc.returncode == 5, proc.stdout + proc.stderr
        assert procs[-1].poll() is not None and (tmp_path / "seen.txt").exists()
        (tmp_path / "seen.txt").unlink()
        # one that is still running at the bound: a missed night, said as that
        procs.append(_sleeper(120))
        _running(home, procs[-1].pid)
        proc = _stub(home, "-Nightly", "-Studio", studio, "-UpdateWaitSeconds", 2, "-DesktopDir", desk)
        assert proc.returncode == 1 and not (tmp_path / "seen.txt").exists()
        assert _missed(home)["what"] == "update" and _lines(home)[-1]["data"]["code"] == "NS-W210"
        assert "was still running after" in (desk / NOTE).read_text(encoding="utf-8-sig")
        # a shortcut meanwhile starts nothing and says why
        proc = _stub(home, "-Launcher", launcher)
        assert proc.returncode == 1 and _starts(launcher) == 0
        assert "An update, rollback or repair of Nightshift is running (progress: " in _words(proc.stdout)
        assert "Nothing was started." in proc.stdout
        procs[-1].kill()
        procs[-1].wait(timeout=30)
        # this release's update holds run\update.lock (its state may say anything): the same
        (home / "run" / "update_state.json").unlink()
        code = ("import sys, time; from pathlib import Path; from ns_common.singleton import FileLock; "
                "lock = FileLock(Path(sys.argv[1])); lock.acquire(); print('held', flush=True); "
                "time.sleep(120)")
        holder = subprocess.Popen([sys.executable, "-c", code, str(home / "run" / "update.lock")],
                                  cwd=str(LAUNCHERS.parent), stdout=subprocess.PIPE, text=True)
        procs.append(holder)
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        proc = _stub(home, "-Launcher", launcher)
        assert proc.returncode == 1 and _starts(launcher) == 0 and "is running" in proc.stdout
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=30)
    assert _stub(home, "-Launcher", launcher).returncode == 0 and _starts(launcher) == 1


@needs_powershell
def test_the_nightly_stub_hands_the_task_start_to_the_night(tmp_path: Path) -> None:
    """Card night-fits: `NS_NIGHT_STARTED` is the stub's own start, set before D-39b's wait for an update, so
    the night counts that wait against Windows' limit for the task; a value left in the environment is
    replaced. A shortcut's start sets nothing."""
    from datetime import UTC, datetime

    home, launcher = _program_home(tmp_path)
    studio = tmp_path / "studio.cmd"   # a stand-in that writes the variable it was given
    line = 'if "%~1 %~2"=="run --daily" echo %NS_NIGHT_STARTED%>"%~dp0started.txt"'
    studio.write_bytes(f"@echo off\r\n{line}\r\nexit /b 5\r\n".encode("ascii"))
    sleeper = _sleeper(8)                # an update of an older release that ends inside the bound
    try:
        _running(home, sleeper.pid)
        called = datetime.now(UTC).replace(microsecond=0)
        env_before = os.environ.get("NS_NIGHT_STARTED")
        os.environ["NS_NIGHT_STARTED"] = "2001-01-01T00:00:00Z"      # an old value inherited from somewhere
        try:
            proc = _stub(home, "-Nightly", "-Studio", studio, "-UpdateWaitSeconds", 60)
        finally:
            if env_before is None:
                os.environ.pop("NS_NIGHT_STARTED", None)
            else:
                os.environ["NS_NIGHT_STARTED"] = env_before
        ended = datetime.now(UTC)
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
        sleeper.wait(timeout=30)
    assert proc.returncode == 5, proc.stdout + proc.stderr
    seen = tmp_path / "started.txt"
    raw = seen.read_text(encoding="ascii").strip() if seen.exists() else ""
    assert raw.startswith("20") and raw != "2001-01-01T00:00:00Z", f"no start for the night: {raw!r}"
    started = datetime.fromisoformat(raw)
    assert started.tzinfo is not None and started.utcoffset() == timedelta(0), raw
    assert (ended - called).total_seconds() >= 7, "the stub waited for the update first"
    assert called - timedelta(seconds=2) <= started <= called + timedelta(seconds=5), (called, raw, ended)


@needs_powershell
def test_a_dead_update_is_told_once_at_the_next_start(tmp_path: Path) -> None:
    home, launcher = _program_home(tmp_path)
    gone = _sleeper(0)
    gone.wait(timeout=30)
    left = "The update process stopped before it changed anything. 0.2.0 is in place."
    _running(home, gone.pid, if_stopped_here=left)
    assert _stub(home, "-Launcher", launcher).returncode == 0 and _starts(launcher) == 1
    state = json.loads((home / "run" / "update_state.json").read_text(encoding="utf-8"))
    assert state["state"] == "failed" and state["message"] == left and state["pid"] == gone.pid
    notes = notify.read_notes(Paths(home))
    assert [(n["title"], n["message"]) for n in notes] == [
        ("Nightshift", f"An update or rollback of Nightshift was interrupted. {left}")]
    assert _stub(home, "-Launcher", launcher).returncode == 0 and len(notify.read_notes(Paths(home))) == 1


@needs_powershell
def test_an_environment_an_older_updater_left_is_said_once_and_nightshift_starts(tmp_path: Path) -> None:
    home, launcher = _program_home(tmp_path)
    info = home / "venvs" / "supervisor" / "Lib" / "site-packages" / "nightshift-0.1.14.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Metadata-Version: 2.1\nName: nightshift\nVersion: 0.1.14\n", "utf-8")
    said = ("The Python environment 'supervisor' holds Nightshift 0.1.14, but the program is 0.2.0 (an "
            "update by an older version was interrupted). Run INSTALL.cmd from the Nightshift 0.2.0 zip and "
            "choose this folder (Repair).")
    proc = _stub(home, "-Launcher", launcher)
    assert proc.returncode == 0 and _starts(launcher) == 1 and said in _words(proc.stdout)
    assert [n["message"] for n in notify.read_notes(Paths(home))] == [said]
    assert _stub(home, "-Launcher", launcher).returncode == 0 and len(notify.read_notes(Paths(home))) == 1

@needs_powershell
def test_after_a_failed_repair_the_diagnostics_and_outbox_shortcuts_still_open(tmp_path: Path) -> None:
    """The way out may ask for the diagnostics zip: those two shortcuts open after a FAILED replay (they start
    no engine), the others say so on their refusal; while an update runs, all of them wait."""
    home, launcher = _program_home(tmp_path)
    _record(home, _uv(tmp_path, fail=True))
    body = "Add-Content -LiteralPath (Join-Path $PSScriptRoot 'ran.txt') -Value 'x'\r\nexit 0\r\n"
    for name in ("diagnostics.ps1", "open-outbox.ps1"):
        (launcher.parent / name).write_text(body, encoding="ascii")
    proc = _stub(home, "-Launcher", launcher)
    assert proc.returncode == 1 and _starts(launcher) == 0
    assert "The Diagnostics shortcut still opens" in _words(proc.stdout)
    for name in ("diagnostics.ps1", "open-outbox.ps1"):
        proc = _stub(home, "-Launcher", launcher.parent / name)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "(NS-W210)" in proc.stdout              # the reason is still said
    assert _starts(launcher) == 2                        # (both stand-ins write the same ran.txt)
    # while an update runs (an older release's worker: a live python pid), even these wait
    sleeper = _sleeper(120)
    try:
        _running(home, sleeper.pid)
        proc = _stub(home, "-Launcher", launcher.parent / "diagnostics.ps1")
        assert proc.returncode == 1 and _starts(launcher) == 2 and "is running" in proc.stdout
    finally:
        sleeper.kill()
        sleeper.wait(timeout=30)

# ---- D-39b review @ e2d2bb0: the record's last step in the launch script, and the Diagnostics shortcut ----
@needs_powershell
@pytest.mark.skipif(sys.platform != "win32", reason="Windows file sharing")
def test_a_replayed_record_held_open_at_its_delete_is_marked_finished_and_nightshift_starts(
    tmp_path: Path,
) -> None:
    from tests.test_venv_repair import _hold, _stop, venvrepair

    vr = venvrepair()           # (None before D-39b: the assertions below then fail, not the import)

    home, launcher = _program_home(tmp_path)
    record = _record(home, _uv(tmp_path), id="0123456789abcdef")
    done = home / "run" / "venv-repair.done"
    holder = _hold(record, 120)            # a reader (Python's open: no delete sharing) for the whole start
    try:
        proc = _stub(home, "-Launcher", launcher, "-ClearWaitSeconds", 1)
        assert proc.returncode == 0 and _starts(launcher) == 1, proc.stdout + proc.stderr
        assert record.exists()
        assert json.loads(done.read_text(encoding="utf-8-sig"))["id"] == "0123456789abcdef"
        assert vr is not None and vr.read(home) is None and vr.start_refusal(home, home / "app") is None
        assert "venv_record_marked_finished" in [x["event"] for x in _lines(home)]
        # the next start: finished, so no replay; the two files go once nothing holds them
        proc = _stub(home, "-Launcher", launcher, "-ClearWaitSeconds", 1)
        assert proc.returncode == 0 and _starts(launcher) == 2 and len(_ran(tmp_path)) == 2
    finally:
        _stop([holder])
    assert _stub(home, "-Launcher", launcher).returncode == 0 and _starts(launcher) == 3
    assert not record.exists() and not done.exists() and len(_ran(tmp_path)) == 2
    # a record with no id cannot be marked: said, and nothing starts (every Python start would refuse it)
    record = _record(home, _uv(tmp_path))
    holder = _hold(record, 120)
    try:
        proc = _stub(home, "-Launcher", launcher, "-ClearWaitSeconds", 1)
    finally:
        _stop([holder])
    said = _words(proc.stdout)
    assert proc.returncode == 1 and _starts(launcher) == 3
    assert "(NS-W210): its Python environments 'studio', 'supervisor' were refreshed, but the record" in said
    assert "could not be removed" in said and "(Repair)" in said


def _diagnostics_home(tmp_path: Path, doctor: str | None) -> tuple[Path, Path, Path]:
    """A home whose replay fails (so the stub lets only Diagnostics and Outbox through), with the real
    Diagnostics launcher and an `ns.exe` stand-in: a copy of this venv's python.exe launcher that runs the
    file `doctor` from the working folder (`doctor` None: no ns.exe at all)."""
    home, launcher = _program_home(tmp_path)
    _record(home, _uv(tmp_path, fail=True))
    for name in ("diagnostics.ps1", "_common.ps1"):
        shutil.copy(LAUNCHERS / name, launcher.parent / name)
    (home / "config" / "runtime.json").write_text("{}", encoding="ascii")
    (home / "config" / "install_state.json").write_text('{"version": "0.2.0"}', encoding="ascii")
    (home / "config" / "answers.json").write_text('{"pexels_api_key": "SECRET"}', encoding="ascii")
    (home / "run" / "update_state.json").write_text('{"state": "failed"}', encoding="ascii")
    (home / "logs" / "update-x.log").write_bytes(b"the update log\n")
    work = tmp_path / "work"
    work.mkdir()
    if doctor is not None:
        sup = home / "venvs" / "supervisor"
        shutil.copy(Path(sys.executable), sup / "Scripts" / "ns.exe")
        shutil.copy(Path(sys.prefix) / "pyvenv.cfg", sup / "pyvenv.cfg")
        (work / "doctor").write_text(doctor, encoding="ascii")
    return home, launcher.parent / "diagnostics.ps1", work


def _stub_in(work: Path, home: Path, *args: object) -> "subprocess.CompletedProcess[str]":
    exe = pwsh.find_powershell()
    assert exe is not None
    argv = [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(home / "nightshift-launch.ps1"),
            *(str(a) for a in args)]
    return subprocess.run(argv, input="\n", capture_output=True, text=True, timeout=180, cwd=str(work),
                          env={**os.environ, "NIGHTSHIFT_HOME": str(home)})


DIES = "import sys\nsys.stderr.write(\"ModuleNotFoundError: No module named 'pydantic'\n\")\nsys.exit(1)\n"
WORKS = (
    "import sys, zipfile\nfrom pathlib import Path\n"
    "out = Path(sys.argv[sys.argv.index('--out-dir') + 1]) / 'nightshift-diagnostics-2026-11-10-0300.zip'\n"
    "with zipfile.ZipFile(out, 'w') as z:\n    z.writestr('VERSION', '0.2.0')\n"
    "print(f'Diagnostics written to {out}')\n"
)


@needs_powershell
@pytest.mark.skipif(sys.platform != "win32", reason="a venv launcher as the ns.exe stand-in")
def test_the_diagnostics_shortcut_says_which_zip_it_made(tmp_path: Path) -> None:
    import zipfile

    # the supervisor environment is half-filled: ns.exe dies at import. A smaller zip, made without Python
    home, diag, work = _diagnostics_home(tmp_path / "dies", DIES)
    desk = tmp_path / "dies" / "Desk"
    desk.mkdir()
    proc = _stub_in(work, home, "-Launcher", diag, "-DesktopDir", desk)
    said = _words(proc.stdout)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "(NS-W210)" in said and "The diagnostics zip is on your Desktop" not in said
    # the command it names is shown by this install's full path (FN-3: PowerShell windows always give it, in
    # `commandline.command_form`'s form; the home's Arabic letters may reach the captured stdout as '?')
    told = re.search(
        r'could not be made \([A-Za-z]:"\\[^"]*\\venvs\\supervisor\\Scripts\\ns\.exe" '
        r"doctor --bundle ended with exit code 1",
        said,
    )
    assert told is not None, said
    (small,) = list(desk.glob("nightshift-diagnostics-small-*.zip"))
    assert f"A smaller diagnostics zip was made without Python instead: {small}" in said
    with zipfile.ZipFile(small) as z:
        names = {n.replace("\\", "/") for n in z.namelist()}
        assert {"logs/update-x.log", "install_state.json", "VERSION", "run/venv-repair.json",
                "run/update_state.json", "bundle-notes.txt"} <= names, names
        assert not any("answers" in n or "secrets" in n for n in names)
        assert z.read([n for n in z.namelist() if n.endswith("update-x.log")][0]) == b"the update log\n"
    assert not list((home / "run").glob("diagnostics-*")), "the staging folder was left"
    # ns doctor --bundle works: its zip, named as it is
    home, diag, work = _diagnostics_home(tmp_path / "works", WORKS)
    desk = tmp_path / "works" / "Desk"
    desk.mkdir()
    proc = _stub_in(work, home, "-Launcher", diag, "-DesktopDir", desk)
    said = _words(proc.stdout)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    full = desk / "nightshift-diagnostics-2026-11-10-0300.zip"
    assert f"The diagnostics zip is on your Desktop: {full}" in said
    assert not list(desk.glob("nightshift-diagnostics-small-*.zip"))
    # neither can be made (the Desktop folder is gone): said plainly, with what to send instead
    home, diag, work = _diagnostics_home(tmp_path / "none", DIES)
    gone = tmp_path / "none" / "Desk"
    proc = _stub_in(work, home, "-Launcher", diag, "-DesktopDir", gone)
    said = _words(proc.stdout)
    assert proc.returncode == 1 and not gone.exists()
    assert "No diagnostics zip was made: the smaller zip without Python failed too" in said
    assert "Instead, send the folder " in said and "venv-repair.json" in said
    assert "zip is on your Desktop" not in said and "was made without Python instead" not in said
