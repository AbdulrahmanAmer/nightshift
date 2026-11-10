"""FN-5: one PowerShell quoting function (`ns_common.psquote.ps_literal`), used by every script builder.

PowerShell ends a single-quoted string at U+0027 and also at U+2018, U+2019, U+201A and U+201B. A Windows
user named O<U+2019>Brien has that character in the Desktop, `%LOCALAPPDATA%` and `%TEMP%`; seven helpers
doubled only the plain apostrophe, so every generated script broke (or ran text of its own).

These tests run real Windows PowerShell 5.1. The scripts a test runs only echo values back, or call stand-in
functions and classes that echo their arguments: nothing here reaches the Task Scheduler, the registry, the
user's environment or the Desktop (the guards of `ns_common.testguard` refuse those under pytest, and this
file does not touch them). Scripts that WOULD change the PC are parsed, never run.
"""

# ruff: noqa: E501  (PowerShell text and long expectations)
from __future__ import annotations

import ast
import base64
import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from installer.stage2 import winenv
from installer.stage2.runner import RunResult
from installer.stage2.steps import s09_shortcuts_scheduler as s09
from installer.stage2.web import bootstrap, uninstall
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor import homecheck, notify, pwsh, schedule, update_job, updater_copy

try:
    from ns_common.psquote import ps_literal as _new_literal
except ImportError:         # the base (before FN-5): the helpers only doubled the plain apostrophe
    _new_literal = None

ROOT = Path(__file__).resolve().parents[1]
needs_powershell = pytest.mark.skipif(pwsh.find_powershell() is None, reason="powershell not on PATH")

# the five characters PowerShell reads as a single quote
Q = ["'", "\u2018", "\u2019", "\u201a", "\u201b"]
HOME_S = "D:\\NS\\a\u2019b\u2018c\u201ad\u201be'f"
DESK = "C:\\Users\\O\u2019Brien\\Desktop"
LOCAL = "C:\\Users\\O\u2019Brien\\AppData\\Local\\Nightshift"
TASK = "Nightshift \u2019Daily"


def literal(value: str) -> str:
    """`ps_literal`; on the base, the old helper's result (so a test fails at an assertion, not an import)."""
    if _new_literal is not None:
        return _new_literal(value)
    return "'" + value.replace("'", "''") + "'"


# ----------------------------------------------------------------------------------------- the function
def test_the_literal_doubles_each_of_the_five_characters_and_refuses_a_nul() -> None:
    assert _new_literal is not None, "ns_common.psquote.ps_literal does not exist"
    for ch in Q:
        assert _new_literal(f"a{ch}b") == f"'a{ch}{ch}b'"
    assert _new_literal("") == "''" and _new_literal("plain C:\\x y") == "'plain C:\\x y'"
    assert _new_literal("\u2019'") == "'\u2019\u2019'''"
    with pytest.raises(ValueError):
        _new_literal("a\x00b")
    with pytest.raises(TypeError):
        _new_literal(Path("x"))  # type: ignore[arg-type]


def _values() -> list[str]:
    out: list[str] = []
    for ch in Q:
        out += [ch, f"{ch}abc", f"abc{ch}", f"a{ch}{ch}b", f"x{ch}$(Get-Date){ch}y"]
    out += [
        "\u2019'", "".join(Q), "a`b`", "a;b;", "a\nb", "a\r\nb", "D:\\NS\\a b\\c d", "$env:USERNAME",
        "say \"hi\"", "", "Jos\u00e9 \u00d1and\u00fa \u0627\u0644\u0639\u0631\u0628\u064a\u0629", "$(Get-Date)",
        "C:\\Users\\O\u2019Brien", HOME_S, "-NoProfile -File 'x'", "a  b ",
    ]
    return out


ECHO = "[Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes([string]{var}))"


def _echo_back(values: Sequence[str]) -> list[str | None]:
    """Each value placed in `$x = <literal>` in real PowerShell, and what `$x` holds: base64 of its UTF-8
    bytes, so no code page is in between. One process; if the script does not parse, one per value."""
    def one_script(vals: Sequence[str]) -> str:
        lines = ["$ErrorActionPreference = 'Stop'"]
        for i, v in enumerate(vals):
            lines += [f"$x{i} = {literal(v)}", "Write-Output ('V:' + " + ECHO.format(var=f"$x{i}") + ")"]
        return "\n".join(lines)

    def decode(text: str) -> list[str]:
        return [base64.b64decode(ln.strip()[2:]).decode("utf-8") for ln in text.splitlines() if ln.startswith("V:")]

    r = pwsh.run_powershell(one_script(values), timeout_s=120)
    if r.returncode == 0 and len(decode(r.stdout)) == len(values):
        return list(decode(r.stdout))
    out: list[str | None] = []
    for v in values:                      # which values broke the script
        one = pwsh.run_powershell(one_script([v]), timeout_s=60)
        got = decode(one.stdout) if one.returncode == 0 else []
        out.append(got[0] if len(got) == 1 else None)
    return out


@needs_powershell
def test_a_literal_round_trips_every_quote_character() -> None:
    values = _values()
    got = _echo_back(values)
    wrong = [(v, g) for v, g in zip(values, got, strict=True) if g != v]
    assert not wrong, f"{len(wrong)} of {len(values)} values changed or broke the script: {wrong[:6]}"


@needs_powershell
def test_the_value_an_owner_types_in_the_setup_runner_arrives_unchanged() -> None:
    """`winenv.eval_ps_text` is how setup reads a value back; the literal goes through the real runner."""
    from installer.stage2.runner import SubprocessRunner

    for v in (HOME_S, "O\u2019Brien", "a'b", "plain"):
        assert winenv.eval_ps_text(SubprocessRunner(), literal(v), tag="ps:test") == v


# ------------------------------------------------------------------- every builder's script is parsed
class Recorder:
    """A runner that starts nothing and keeps the script of each PowerShell call (`-EncodedCommand`)."""

    def __init__(self) -> None:
        self.scripts: list[str] = []

    def run(self, argv: Sequence[str], **kw: Any) -> RunResult:
        self.scripts.append(base64.b64decode(argv[-1]).decode("utf-16-le"))
        return RunResult(0, "")

    def only(self) -> str:
        assert len(self.scripts) == 1
        return self.scripts[0]


def _recorded(call: Any) -> str:
    rec = Recorder()
    call(rec)
    return rec.only()


def _elevated_script(monkeypatch: pytest.MonkeyPatch, exe: Path, args: Sequence[str]) -> str:
    seen: list[list[str]] = []

    def fake(argv: Sequence[str], **kw: Any) -> bootstrap.CmdResult:
        seen.append(list(argv))
        return bootstrap.CmdResult(0, "", "")

    monkeypatch.setattr(bootstrap, "real_cmd", fake)
    bootstrap.run_elevated_exe(exe, args)
    assert len(seen) == 1 and seen[0][-2] == "-Command"
    return seen[0][-1]


def _no_install_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setattr(homecheck, "_program_home", lambda: Path(HOME_S))
    with pytest.raises(NsError) as info:
        homecheck.require_install({"LOCALAPPDATA": str(tmp_path / "local")})
    m = re.search(r"run (\$env:NIGHTSHIFT_HOME = .*) in this window, or use the", info.value.detail)
    assert m, info.value.detail
    return m.group(1)


def _builders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[str, list[str]]]:
    """name -> (script, the values it must hold as string constants)."""
    home, lnk = Path(HOME_S), Path(DESK + "\\Nightshift.lnk")
    icon, app = Path(HOME_S + "\\app\\i.ico"), Path(HOME_S + "\\app")
    paths = Paths(home)
    args = f'-File "{HOME_S}\\x.ps1" -Name O\u2019Brien'
    other = HOME_S + "\\other\\i.ico"
    status, log = home / "run" / "update_state.json", home / "logs" / "update-1.log"
    exe, exe_args = Path(HOME_S + "\\vc_redist.x64.exe"), ["/install", "/quiet", "a\u2019b '/norestart'"]
    targets = [str(home / n) for n in ("bin", "run", "app", "app.new", "app.prev", "app.prev-config.zip")]
    return {
        "winenv.set_user_env": (
            _recorded(lambda r: winenv.set_user_env(r, "NIGHTSHIFT_HOME", HOME_S)),
            ["NIGHTSHIFT_HOME", HOME_S, "User"]),
        "winenv.get_user_env": (
            _recorded(lambda r: winenv.get_user_env(r, "NS\u2019VAR")), ["NS\u2019VAR", "User"]),
        "s09.home_var_now": (
            _recorded(lambda r: s09.home_var_now(SimpleNamespace(runner=r, paths=paths))),  # type: ignore[arg-type]
            [winenv.HOME_VAR, "User"]),
        "winenv.create_shortcut": (
            _recorded(lambda r: winenv.create_shortcut(r, lnk, "powershell.exe", args, home, "It\u2019s Nightshift",
                                                       icon)),
            [str(lnk), "powershell.exe", args, HOME_S, "It\u2019s Nightshift", str(icon), str(icon) + ",0"]),
        "winenv.create_shortcut (no icon)": (
            _recorded(lambda r: winenv.create_shortcut(r, lnk, "powershell.exe", args, home, "d")),
            [str(lnk), args, HOME_S]),
        "winenv.update_shortcut": (
            _recorded(lambda r: winenv.update_shortcut(r, lnk, args, workdir=home, icon=icon)),
            [str(lnk), args, HOME_S, str(icon)]),
        "winenv.register_aumid_script (keep_other)": (
            winenv.register_aumid_script("Night\u2019shift", "Night \u2018shift", icon, keep_other=True,
                                         also_mine=[other]),
            [winenv.AUMID_KEY_PARENT, "Night\u2019shift", "Night \u2018shift", str(icon), other]),
        "winenv.register_aumid_script": (
            winenv.register_aumid_script("Night\u2019shift", "Night \u2018shift", None),
            [winenv.AUMID_KEY_PARENT, "Night\u2019shift", "Night \u2018shift"]),
        "winenv.unregister_aumid": (
            _recorded(lambda r: winenv.unregister_aumid(r, "Night\u2019shift")),
            [winenv.AUMID_KEY_PARENT, "Night\u2019shift"]),
        "winenv.set_shortcut_aumid_script": (
            winenv.set_shortcut_aumid_script(lnk, "Night\u2019shift"), [str(lnk), "Night\u2019shift"]),
        "winenv.get_shortcut_aumid_script": (winenv.get_shortcut_aumid_script(lnk), [str(lnk)]),
        "uninstall.deferred_script": (
            uninstall.deferred_script(home, [1234], Path(LOCAL)),
            [*targets, str(Path(LOCAL) / "browser"), str(Path(LOCAL) / "webview2"), HOME_S, LOCAL,
             uninstall.HOST_MUTEX, uninstall.LEFTOVERS_FILE]),
        "updater_copy.cleanup_script": (
            updater_copy.cleanup_script(Path(HOME_S + "\\updater\\copy-1"), 4321),
            [HOME_S + "\\updater\\copy-1"]),
        "schedule.build_install_script": (
            schedule.build_install_script(paths, "03:00", app, TASK),
            [TASK, schedule.action_arguments(paths), HOME_S, "03:00"]),
        "schedule.build_install_script (disabled)": (
            schedule.build_install_script(paths, "03:00", app, TASK, disabled=True),
            [TASK, schedule.action_arguments(paths), HOME_S]),
        "schedule.build_action_script": (
            schedule.build_action_script(paths, TASK), [TASK, schedule.action_arguments(paths), HOME_S]),
        "schedule.build_remove_script": (schedule.build_remove_script(TASK), [TASK]),
        "schedule.build_show_script": (schedule.build_show_script(TASK), [TASK]),
        "schedule.build_read_script": (schedule.build_read_script(TASK), [TASK]),
        "bootstrap.run_elevated_exe": (_elevated_script(monkeypatch, exe, exe_args), [str(exe), *exe_args]),
        "update_job.follower_script": (
            update_job.follower_script(status, log, 99, wait=False),
            [str(status), str(log), update_job.WINDOW_TITLE]),
        "homecheck.require_install (the command it prints)": (
            _no_install_command(tmp_path, monkeypatch), [HOME_S]),
        "notify.TOAST_SCRIPT": (notify.TOAST_SCRIPT, [notify.AUMID]),
    }


PARSE = r"""
$ErrorActionPreference = 'Stop'
$items = [IO.File]::ReadAllText($env:NS_PARSE_IN, [Text.Encoding]::UTF8) | ConvertFrom-Json
$all = [ordered]@{}
foreach ($it in $items.PSObject.Properties) {
    $errs = $null; $tokens = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseInput([string]$it.Value, [ref]$tokens, [ref]$errs)
    $consts = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true) |
        ForEach-Object { [ordered]@{ v = [string]$_.Value; t = [string]$_.StringConstantType } })
    $all[$it.Name] = [ordered]@{ errors = @($errs | ForEach-Object { [string]$_.Message }); constants = $consts }
}
[IO.File]::WriteAllText($env:NS_PARSE_OUT, (ConvertTo-Json -InputObject $all -Depth 6 -Compress),
    (New-Object Text.UTF8Encoding $false))
"""


@pytest.fixture(scope="module")
def parsed(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, Any]]:
    """Every builder's script, parsed (never run) by Windows PowerShell's own parser in ONE process."""
    tmp = tmp_path_factory.mktemp("psparse")
    with pytest.MonkeyPatch.context() as mp:
        built = _builders(tmp, mp)
    (tmp / "in.json").write_text(json.dumps({k: v[0] for k, v in built.items()}), encoding="utf-8")
    r = pwsh.run_powershell(PARSE, env_extra={"NS_PARSE_IN": str(tmp / "in.json"),
                                              "NS_PARSE_OUT": str(tmp / "out.json")}, timeout_s=180)
    assert r.returncode == 0, r.stdout + r.stderr
    got = json.loads((tmp / "out.json").read_text(encoding="utf-8"))
    return {k: {"errors": got[k]["errors"], "constants": got[k]["constants"], "expected": built[k][1]}
            for k in built}


NAMES = [
    "winenv.set_user_env", "winenv.get_user_env", "s09.home_var_now", "winenv.create_shortcut",
    "winenv.create_shortcut (no icon)", "winenv.update_shortcut", "winenv.register_aumid_script (keep_other)",
    "winenv.register_aumid_script", "winenv.unregister_aumid", "winenv.set_shortcut_aumid_script",
    "winenv.get_shortcut_aumid_script", "uninstall.deferred_script", "updater_copy.cleanup_script",
    "schedule.build_install_script", "schedule.build_install_script (disabled)", "schedule.build_action_script",
    "schedule.build_remove_script", "schedule.build_show_script", "schedule.build_read_script",
    "bootstrap.run_elevated_exe", "update_job.follower_script",
    "homecheck.require_install (the command it prints)", "notify.TOAST_SCRIPT",
]


@needs_powershell
@pytest.mark.parametrize("name", NAMES)
def test_every_script_builder_keeps_a_path_with_quotes_intact(
    name: str, parsed: dict[str, dict[str, Any]]
) -> None:
    got = parsed[name]
    assert got["errors"] == [], got["errors"]
    constants = [c["v"] for c in got["constants"] if "Here" not in c["t"]]
    for value in got["expected"]:
        assert value in constants, f"{value!r} is not one of the strings PowerShell reads: {constants}"
    # a string that holds a quote character is a value as given (a path under one of the folders, or the
    # action text that names one), never a piece of one
    derived = (HOME_S, DESK, LOCAL)
    pieces = [c for c in constants if any(q in c for q in Q) and c not in got["expected"]
              and not c.startswith(derived) and HOME_S not in c
              and c not in ("& '", "'")]     # (the pieces of Format-NsCommands' own code, in the follower)
    assert pieces == [], pieces


def test_the_builders_listed_here_are_all_of_them(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert sorted(_builders(tmp_path, monkeypatch)) == sorted(NAMES)


# ----------------------------------------------------- the same builders, run with stand-ins that echo back
LOG_FN = "function Add-Log($k, $v) { Write-Host ('LOG:' + $k + '=' + " + ECHO.format(var="$v") + ") }\n"
SCHEDULER_STAND_INS = LOG_FN + r"""
function New-ScheduledTaskAction { param($Execute, $Argument, $WorkingDirectory)
    Add-Log 'Argument' $Argument; Add-Log 'WorkingDirectory' $WorkingDirectory; 'action' }
function New-ScheduledTaskTrigger { param([switch]$Daily, $At) 'trigger' }
function New-ScheduledTaskSettingsSet { param([switch]$WakeToRun, [switch]$StartWhenAvailable,
    [switch]$AllowStartIfOnBatteries, [switch]$DontStopIfGoingOnBatteries, $ExecutionTimeLimit,
    [switch]$Disable) 'settings' }
function New-TimeSpan { param($Hours) 'span' }
function New-ScheduledTaskPrincipal { param($UserId, $LogonType, $RunLevel) 'principal' }
function Register-ScheduledTask { param($TaskName, $Action, $Trigger, $Settings, $Principal, $Description,
    [switch]$Force) Add-Log 'TaskName' $TaskName }
function Set-ScheduledTask { param($TaskName, $Action) Add-Log 'TaskName' $TaskName }
function Unregister-ScheduledTask { param($TaskName, $Confirm) Add-Log 'TaskName' $TaskName }
function Get-ScheduledTask { [CmdletBinding()] param($TaskName) Add-Log 'TaskName' $TaskName; $null }
function Get-ScheduledTaskInfo { param($TaskName) Add-Log 'TaskName' $TaskName }
"""


def _logged(script: str) -> dict[str, list[str]]:
    r = pwsh.run_powershell(script, timeout_s=120)
    assert r.returncode == 0, r.stdout + r.stderr
    out: dict[str, list[str]] = {}
    for line in r.stdout.splitlines():
        if line.startswith("LOG:"):
            key, _, b64 = line[4:].partition("=")
            out.setdefault(key, []).append(base64.b64decode(b64.strip()).decode("utf-8"))
    return out


@needs_powershell
def test_the_task_scripts_hand_the_scheduler_the_exact_name_action_and_folder(tmp_path: Path) -> None:
    paths = Paths(Path(HOME_S))
    app = Path(HOME_S + "\\app")
    for script in (
        schedule.build_install_script(paths, "03:00", app, TASK),
        schedule.build_install_script(paths, "03:00", app, TASK, disabled=True),
        schedule.build_action_script(paths, TASK),
    ):
        seen = _logged(SCHEDULER_STAND_INS + script)
        assert set(seen["TaskName"]) == {TASK}
        assert set(seen["Argument"]) == {schedule.action_arguments(paths)}
        assert set(seen["WorkingDirectory"]) == {HOME_S}
    for script in (schedule.build_remove_script(TASK), schedule.build_show_script(TASK),
                   schedule.build_read_script(TASK)):
        assert set(_logged(SCHEDULER_STAND_INS + script)["TaskName"]) == {TASK}


SHORTCUT_STAND_INS = r"""
Add-Type -TypeDefinition @'
using System;
using System.Text;
public static class Echo {
    public static void Say(string name, params string[] values) {
        Console.WriteLine("LOG:" + name + "=" + Convert.ToBase64String(
            Encoding.UTF8.GetBytes(string.Join("\u001f", values))));
    }
}
public static class NsLink {
    public static void Create(string a, string b, string c, string d, string e, string f) {
        Echo.Say("Create", a, b, c, d, e, f); }
    public static void Update(string a, string b, string c, string d) { Echo.Say("Update", a, b, c, d); }
}
public static class NsAumid {
    public static void Set(string a, string b) { Echo.Say("Set", a, b); }
    public static string Get(string a) { Echo.Say("Get", a); return ""; }
}
'@
"""
PRODUCT_ADD_TYPE = re.compile(r"Add-Type -TypeDefinition @'\r?\n.*?\r?\n'@", re.S)


@needs_powershell
def test_the_shortcut_scripts_hand_the_shell_the_exact_paths_and_text() -> None:
    home, lnk = Path(HOME_S), Path(DESK + "\\Nightshift.lnk")
    icon = Path(HOME_S + "\\app\\i.ico")
    args = f'-File "{HOME_S}\\x.ps1" -Name O\u2019Brien'
    desc = "It\u2019s \u2018Nightshift\u2019 \u201a\u201b '"
    rec = Recorder()
    winenv.create_shortcut(rec, lnk, "powershell.exe", args, home, desc, icon)
    winenv.update_shortcut(rec, lnk, args, workdir=home, icon=icon)
    scripts = [*rec.scripts, winenv.set_shortcut_aumid_script(lnk, "Night\u2019shift"),
               winenv.get_shortcut_aumid_script(lnk)]
    assert all(PRODUCT_ADD_TYPE.search(s) for s in scripts)
    body = "\n".join(PRODUCT_ADD_TYPE.sub("", s) for s in scripts)
    r = pwsh.run_powershell(SHORTCUT_STAND_INS + body, timeout_s=180)
    assert r.returncode == 0, r.stdout + r.stderr
    seen = {}
    for line in r.stdout.splitlines():
        if line.startswith("LOG:"):
            key, _, b64 = line[4:].partition("=")
            seen[key] = base64.b64decode(b64.strip()).decode("utf-8").split("\x1f")
    assert seen["Create"] == [str(lnk), "powershell.exe", args, HOME_S, desc, str(icon)]
    assert seen["Update"] == [str(lnk), args, HOME_S, str(icon)]
    assert seen["Set"] == [str(lnk), "Night\u2019shift"] and seen["Get"] == [str(lnk)]


# ------------------------------------------- the two scripts that delete things, run on a scratch folder
@needs_powershell
def test_the_cleanup_scripts_find_a_folder_whose_name_holds_every_quote_character(tmp_path: Path) -> None:
    name = "a\u2019b\u2018c\u201ad\u201be'f"
    gone = tmp_path / name / "copy-1"
    (gone / "Lib").mkdir(parents=True)
    (gone / "Lib" / "x.txt").write_text("x", encoding="utf-8")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    r = pwsh.run_powershell(updater_copy.cleanup_script(gone, dead.pid), timeout_s=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not gone.exists() and (tmp_path / name).is_dir()      # the one folder it was told, and no other

    home = tmp_path / ("ns" + name)
    local = tmp_path / ("local" + name) / "Nightshift"
    for folder in (home / "bin", home / "run", local / "browser"):
        folder.mkdir(parents=True)
        (folder / "f.txt").write_text("x", encoding="utf-8")
    keep = tmp_path / "beside.txt"
    keep.write_text("stays", encoding="utf-8")
    tmp = tmp_path / "temp"
    tmp.mkdir()
    script = uninstall.deferred_script(home, [], local)
    r = pwsh.run_powershell(script, env_extra={"TEMP": str(tmp), "TMP": str(tmp)}, timeout_s=180)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not home.exists() and not local.exists()
    assert keep.read_text(encoding="utf-8") == "stays" and list(tmp.iterdir()) == []   # nothing left over


@needs_powershell
def test_the_progress_window_script_recognises_its_own_log_whatever_its_name(tmp_path: Path) -> None:
    """The window believes a state only when it names ITS log (`$state.log -eq $logFile`): a log path that
    reached PowerShell changed would read as 'ended without reporting a result'."""
    home = tmp_path / ("ns" + "a\u2019b\u2018c\u201ad\u201be'f")
    (home / "run").mkdir(parents=True)
    (home / "logs").mkdir()
    log, status = home / "logs" / "update-1.log", home / "run" / "update_state.json"
    log.write_text("one line\n", encoding="utf-8")
    state = {"state": "done", "message": "all good", "log": str(log), "pid": 1}
    status.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8-sig")  # (PowerShell 5.1 reads UTF-8 by its BOM)
    r = pwsh.run_powershell(update_job.follower_script(status, log, 1, wait=False), timeout_s=120)
    assert r.returncode == 0 and "Result: all good" in r.stdout, r.stdout + r.stderr


# ------------------------------------------------------------------------------ voices stay out of scripts
def test_a_voice_name_never_enters_a_powershell_script(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from ns_tts import sapi

    voice = "Zira \u2019\u2018\u201a\u201b' test"
    seen: dict[str, Any] = {}

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[bytes]:
        seen["cmd"] = cmd
        seen["script"] = base64.b64decode(cmd[cmd.index("-EncodedCommand") + 1]).decode("utf-16-le")
        job = Path(kw["env"]["NS_SAPI_JOB"])
        seen["job"] = json.loads(job.read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(cmd, 1, b"", b"stand-in: nothing is spoken")

    monkeypatch.setattr(sapi.subprocess, "run", fake_run)
    backend = sapi.PowerShellSapi(tmp_dir=tmp_path, platform="win32")
    monkeypatch.setattr(backend, "_powershell", lambda: "powershell.exe")
    with pytest.raises(NsError):
        backend.speak(voice, ["one line"], rate=0, sr=24000, timeout_s=5)
    assert seen["script"] == sapi._SCRIPT
    assert seen["job"]["voice"] == voice
    assert not any(ch in seen["script"] or ch in " ".join(seen["cmd"]) for ch in Q[1:])
    assert "Zira" not in seen["script"] and "Zira" not in " ".join(seen["cmd"])


# ------------------------------------------------------------------------------------- the one-function guard
SKIP_DIRS = {"tests", "node_modules", ".git", "venv", ".venv", "static", "__pycache__", "installs", "runs"}
HAND_MADE_NAMES = {"ps_quote", "_q", "_quote", "_ps", "_powershell_quote", "_ps_quote", "quote_ps", "ps_escape"}
ONE_PLACE = "ns_common/psquote.py"
#: (file, text of the f-string) pairs that put a name between two quote characters in a SENTENCE for a person,
#: not in PowerShell text. Anything else the scan finds is a new hand-made literal and fails the test.
SENTENCES: tuple[tuple[str, str], ...] = (
    ("dev/ci_install_ffmpeg.py", "::warning::ffmpeg reports"),         # a GitHub Actions annotation
    ("dev/prove.py", "is held by"),                                     # a line the proof runner prints
    ("ns_studio/voice_setup.py", "the voice setup asked for"),          # a note for the owner
    ("ns_studio/voice_setup.py", "setup asked for a voice"),            # a note for the owner
    ("ns_supervisor/update_job.py", "runs in its own window"),          # what `ns update` prints
)
#: `'%s'`, `'{}'`, `'{0}'` and `'{name}'` in a plain string that is later formatted: the other hand-made shape.
FORMATTED = re.compile(r"'(?:%s|%\(\w+\)s|\{\w*\})'")
#: What PowerShell text looks like in the literal parts of an f-string: a variable, a cmdlet, a static call, or
#: a quote beside a comma or a parenthesis (the fragments of a call that a long line was split into). A SENTENCE
#: that puts a name between apostrophes for a person ("Channel 'x' exists") has none of these.
PS_SHAPE = re.compile(
    r"\$\w|\[\w[\w.]*\]::|\b(?:Get|Set|New|Remove|Register|Unregister|Test|Add|Start|Wait|Write)-[A-Z]\w+"
    r"|['‘’‚‛][,)]|\(['‘’‚‛]"
)


def _literal_text(node: ast.JoinedStr) -> str:
    return "".join(p.value for p in node.values if isinstance(p, ast.Constant) and isinstance(p.value, str))


def _product_files() -> list[Path]:
    return sorted(
        p for p in ROOT.rglob("*.py")
        if not (set(p.relative_to(ROOT).parts[:-1]) & SKIP_DIRS)
    )


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def _is_quote_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in Q


def _hand_made_quoting(text: str) -> list[tuple[int, str, str]]:
    """(line, what, source) of every hand-made way to quote a value for PowerShell in `text`."""
    tree = ast.parse(text)
    found: list[tuple[int, str, str]] = []

    def add(node: ast.AST, what: str) -> None:
        found.append((getattr(node, "lineno", 0), what, ast.get_source_segment(text, node) or ""))

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in HAND_MADE_NAMES:
            add(node, f"defines {node.name}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            consts = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if node.func.attr in ("replace", "sub") and any(
                len(c) == 2 and c[0] == c[1] and c[0] in Q for c in consts
            ):
                add(node, f"doubles a quote with .{node.func.attr}()")
            if node.func.attr == "format" and isinstance(node.func.value, ast.Constant) \
                    and isinstance(node.func.value.value, str) and FORMATTED.search(node.func.value.value):
                add(node, "puts a value between quote characters with .format()")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = node.left
            if isinstance(left, ast.BinOp) and isinstance(left.op, ast.Add) and _is_quote_const(left.left) \
                    and _is_quote_const(node.right):
                add(node, "builds '...' by adding quote characters")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod) and isinstance(node.left, ast.Constant) \
                and isinstance(node.left.value, str) and FORMATTED.search(node.left.value):
            add(node, "puts a value between quote characters with %")
        elif isinstance(node, ast.JoinedStr) and PS_SHAPE.search(_literal_text(node)):
            parts = node.values
            for i in range(1, len(parts) - 1):
                before, mid, after = parts[i - 1], parts[i], parts[i + 1]
                if (isinstance(mid, ast.FormattedValue) and isinstance(before, ast.Constant)
                        and isinstance(after, ast.Constant) and isinstance(before.value, str)
                        and isinstance(after.value, str) and before.value[-1:] in Q and after.value[:1] in Q):
                    add(node, "puts a value between two quote characters in an f-string")
                    break
    return found


def test_there_is_one_quoting_function() -> None:
    offenders: list[str] = []
    for path in _product_files():
        rel = _rel(path)
        if rel == ONE_PLACE:
            continue
        for line, what, source in _hand_made_quoting(path.read_text(encoding="utf-8")):
            if any(rel == f and s in source for f, s in SENTENCES):
                continue
            offenders.append(f"{rel}:{line} {what}: {source[:80]!r}")
    assert offenders == [], "\n".join(offenders)


def test_the_guard_finds_each_hand_made_shape() -> None:
    """The scan is not vacuous: every shape the seven helpers (and their cousins) used is found."""
    shapes = {
        "def ps_quote(v):\n    return v\n": 1,
        "x = v.replace(\"'\", \"''\")\n": 1,
        "x = re.sub(\"\\u2019\", \"\\u2019\\u2019\", v)\n": 1,
        "x = \"'\" + v + \"'\"\n": 1,
        "x = f\"$a = '{v}'\"\n": 1,
        "x = \"$a = '{}'\".format(v)\n": 1,
        "x = \"$a = '%s'\" % v\n": 1,
        "x = f\"[X]::Y('{p(a)}', '{p(b)}')\"\n": 1,
        "x = literal(v)\n": 0,
        "x = f\"it is {v}\"\n": 0,
    }
    for source, count in shapes.items():
        assert len(_hand_made_quoting(source)) == count, source


def test_the_builders_import_the_one_function() -> None:
    for rel in ("installer/stage2/winenv.py", "installer/stage2/web/uninstall.py", "installer/stage2/web/bootstrap.py",
                "ns_supervisor/schedule.py",
                "ns_supervisor/updater_copy.py", "ns_supervisor/homecheck.py", "ns_supervisor/update_job.py",
                "ns_supervisor/notify.py"):
        assert "from ns_common.psquote import ps_literal" in (ROOT / rel).read_text(encoding="utf-8"), rel



# --------------------------------------------------------------------------- the one .ps1 that builds a command
DEV_SCRIPT = ROOT / "dev" / "run_services_dev.ps1"


def test_no_powershell_script_builds_a_command_string_with_hand_made_quotes() -> None:
    offenders = []
    for path in sorted(ROOT.rglob("*.ps1")):
        if set(path.relative_to(ROOT).parts[:-1]) & SKIP_DIRS:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "-Command" in line and re.search(r"'\$|\$[\w(]+'", line):
                offenders.append(f"{_rel(path)}:{n}: {line.strip()[:100]}")
            if re.search(r"\.Replace\(\"'\"|-c?replace \"'\"", line):
                offenders.append(f"{_rel(path)}:{n}: doubles a quote by hand: {line.strip()[:100]}")
    assert offenders == [], "\n".join(offenders)


@needs_powershell
def test_the_dev_services_script_quotes_through_one_function_that_round_trips(tmp_path: Path) -> None:
    text = DEV_SCRIPT.read_text(encoding="ascii")
    assert "ConvertTo-NsPsLiteral" in text
    extract = (
        "$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:NS_DEV_SCRIPT, [ref]$null, [ref]$null)\n"
        "$fn = $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] "
        "-and $n.Name -eq 'ConvertTo-NsPsLiteral' }, $true)\n"
        "if (@($fn).Count -ne 1) { throw 'the script must define ConvertTo-NsPsLiteral once' }\n"
        "Invoke-Expression $fn[0].Extent.Text\n"
    )
    values = _values()
    lines = ["$ErrorActionPreference = 'Stop'", extract]
    for i, v in enumerate(values):
        lines += [f"$src{i} = {_ps_text(v)}", f"$lit{i} = ConvertTo-NsPsLiteral $src{i}",
                  f"$back{i} = Invoke-Expression $lit{i}",
                  "Write-Output ('V:' + " + ECHO.format(var=f"$back{i}") + ")"]
    r = pwsh.run_powershell("\n".join(lines), env_extra={"NS_DEV_SCRIPT": str(DEV_SCRIPT)}, timeout_s=180)
    assert r.returncode == 0, r.stdout + r.stderr
    got = [base64.b64decode(ln.strip()[2:]).decode("utf-8") for ln in r.stdout.splitlines() if ln.startswith("V:")]
    assert got == values


def _ps_text(value: str) -> str:
    """A value as a PowerShell expression WITHOUT the function under test (base64, decoded in PowerShell), so
    the test of the .ps1 function does not lean on the Python one."""
    b64 = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{b64}'))"


# --------------------------------------------- the commands shown to the owner (FN-3's texts), in both languages
HOME_D = "D:\\NS\\a$b\u2019c\u2018d\u201ae\u201bf'g"      # a `$` makes the form `& '<path>'`


@needs_powershell
def test_a_command_shown_to_the_owner_names_the_path_exactly(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ns_common import commandline

    monkeypatch.setenv("NS_TEST_USER_PATH", commandline.TEST_UNKNOWN)
    wanted = {name: f"{HOME_D}\\venvs\\{venv}\\Scripts\\{exe}" for name, venv, exe in commandline.PROGRAMS}
    shown = {name: commandline.command_form(path) for name, path in wanted.items()}
    assert all(form.startswith("& '") for form in shown.values()), shown
    script = commandline.ps_formatter() + "\n" + "\n".join(
        [
            "$ErrorActionPreference = 'Stop'",
            "$o = Format-NsCommands $env:NS_T $env:NS_H",
            "$errs = $null; $tokens = $null",
            "$ast = [System.Management.Automation.Language.Parser]::ParseInput($o, [ref]$tokens, [ref]$errs)",
            "if ($errs.Count -gt 0) { throw ($errs | ForEach-Object { $_.Message }) }",
            "$c = @($ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst]"
            " -and $n.StringConstantType -eq 'SingleQuoted' }, $true) | ForEach-Object { $_.Value })",
            "foreach ($v in $c) { Write-Output ('V:' + " + ECHO.format(var="$v") + ") }",
        ]
    )
    r = pwsh.run_powershell(script, env_extra={"NS_T": "ns doctor; studio status", "NS_H": HOME_D}, timeout_s=120)
    assert r.returncode == 0, r.stdout + r.stderr
    got = [base64.b64decode(ln[2:]).decode("utf-8") for ln in r.stdout.splitlines() if ln.startswith("V:")]
    assert got == [wanted["ns"], wanted["studio"]]
    # and the Python form of the same command parses to the same path
    with PowerShellParse(tmp_path) as parse:
        for name, form in shown.items():
            errors, constants = parse(form + " doctor")
            assert errors == [] and constants == [wanted[name]], (name, errors, constants)


class PowerShellParse:
    """Parse short scripts with PowerShell's own parser (one process each; the text goes in a file)."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp

    def __enter__(self) -> PowerShellParse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __call__(self, script: str) -> tuple[list[str], list[str]]:
        (self.tmp / "p.json").write_text(json.dumps({"x": script}), encoding="utf-8")
        r = pwsh.run_powershell(PARSE, env_extra={"NS_PARSE_IN": str(self.tmp / "p.json"),
                                                  "NS_PARSE_OUT": str(self.tmp / "p.out")}, timeout_s=120)
        assert r.returncode == 0, r.stdout + r.stderr
        got = json.loads((self.tmp / "p.out").read_text(encoding="utf-8"))["x"]
        return list(got["errors"]), [c["v"] for c in got["constants"] if c["t"] == "SingleQuoted"]
