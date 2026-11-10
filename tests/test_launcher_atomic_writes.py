"""F-8: the PowerShell launchers write their state files through one temp-and-swap helper, and read them back
with a retry, so a reader never meets a torn file or the brief refusal Windows gives during a swap.

Everything here runs under Windows PowerShell (`powershell.exe`, 5.1 on a real machine and on the CI image),
the same host the shortcuts use. Nothing starts the engine, the dashboard or a window: the launcher functions
are cut out of the scripts with the PowerShell parser and run against a throwaway home.
"""

from __future__ import annotations

import ctypes
import itertools
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHERS = ROOT / "launchers"
COMMON = LAUNCHERS / "_common.ps1"
HOST = LAUNCHERS / "nightshift-host.ps1"
POWERSHELL = shutil.which("powershell")

needs_powershell = pytest.mark.skipif(
    sys.platform != "win32" or POWERSHELL is None, reason="needs Windows PowerShell"
)

_counter = itertools.count()
PRELUDE = "Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.lower() != "psmodulepath"}
    env.pop("NIGHTSHIFT_HOME", None)
    env.update(extra or {})
    return env


def start_ps(tmp: Path, body: str, env: dict[str, str] | None = None) -> subprocess.Popen[str]:
    """Start `body` as a .ps1 file (-File: the way the shortcuts run the launchers)."""
    assert POWERSHELL is not None
    script = tmp / f"t{next(_counter)}.ps1"
    script.write_text(PRELUDE + body, encoding="ascii")
    return subprocess.Popen(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_env(env),
    )


def finish(proc: subprocess.Popen[str], timeout: float = 120) -> str:
    out, err = proc.communicate(timeout=timeout)
    assert proc.returncode == 0, out + err
    return out


def run_ps(tmp: Path, body: str, env: dict[str, str] | None = None) -> str:
    return finish(start_ps(tmp, body, env))


# --- a file held shut, the way a reader or a sync client holds it ------------------------------------------
_k32: Any = ctypes.WinDLL("kernel32", use_last_error=True) if sys.platform == "win32" else None
if _k32 is not None:
    _k32.CreateFileW.restype = ctypes.c_void_p
    _k32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]  # fmt: skip
    _k32.CloseHandle.argtypes = [ctypes.c_void_p]


class Hold:
    """Open `path` with no sharing at all: every other open of it is refused until `release()`."""

    def __init__(self, path: Path) -> None:
        handle = _k32.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
        assert handle not in (None, ctypes.c_void_p(-1).value), ctypes.get_last_error()
        self.handle: int | None = handle

    def release(self) -> None:
        if self.handle is not None:
            _k32.CloseHandle(self.handle)
            self.handle = None


def hold_until_the_script_is_in_the_call(path: Path, marker: Path, hold_s: float) -> None:
    """Keep `path` shut from the moment the script says it is about to call, for `hold_s` more seconds."""
    deadline = time.time() + 90
    while not marker.exists() and time.time() < deadline:
        time.sleep(0.02)
    time.sleep(hold_s)


def inode(path: Path) -> int:
    return path.stat().st_ino


def leftovers(folder: Path, expected: set[str]) -> set[str]:
    return {p.name for p in folder.rglob("*") if p.is_file()} - expected


def cut_functions(names: list[str], script: Path = HOST) -> str:
    """PowerShell that defines `names` from `script` (default nightshift-host.ps1) in the caller's scope;
    nothing else in the script runs."""
    quoted = ", ".join(f"'{n}'" for n in names)
    return (
        "$errs = $null; $tokens = $null\n"
        "$lang = [System.Management.Automation.Language.Parser]\n"
        f"$ast = $lang::ParseFile('{script}', [ref]$tokens, [ref]$errs)\n"
        "$kind = [System.Management.Automation.Language.FunctionDefinitionAst]\n"
        f"$found = $ast.FindAll({{ param($n) $n -is $kind -and $n.Name -in @({quoted}) }}, $true)\n"
        "$src = ($found | ForEach-Object { $_.Extent.Text }) -join [Environment]::NewLine\n"
        f"if (@($found).Count -ne {len(names)}) {{ throw 'missing function in the host script' }}\n"
        ". ([scriptblock]::Create($src))\n"
    )


# ----------------------------------------------------------------------------------------------------------
# the helper itself
# ----------------------------------------------------------------------------------------------------------
@needs_powershell
def test_the_helper_writes_exactly_the_bytes_it_is_asked_to(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "replace.txt").write_bytes(b"an older and much longer content than the new one")
    body = f"""
. '{COMMON}'
$d = '{out}'
$e = [string][char]0xE9
Write-NsFileAtomic -Path "$d\\utf8.txt" -Text ('caf' + $e)
$withBom = New-Object System.Text.UTF8Encoding $true
Write-NsFileAtomic -Path "$d\\bom.txt" -Text ('caf' + $e) -Encoding $withBom
Write-NsFileAtomic -Path "$d\\ascii.txt" -Text ("1.2.3`r`n") -Encoding ([System.Text.Encoding]::ASCII)
Write-NsFileAtomic -Path "$d\\empty.txt" -Text ''
Write-NsFileAtomic -Path "$d\\lf.txt" -Text ("a`nb`r`nc")
Write-NsFileAtomic -Path "$d\\raw.bin" -Bytes ([byte[]](0, 255, 10, 13, 0, 7))
Write-NsFileAtomic -Path "$d\\nothing.bin" -Bytes ([byte[]]@())
Write-NsFileAtomic -Path "$d\\replace.txt" -Text 'short'
Write-NsFileAtomic -Path "$d\\sub\\deeper\\made.txt" -Text 'new folder'
"""
    run_ps(tmp_path, body)
    assert (out / "utf8.txt").read_bytes() == b"caf\xc3\xa9"  # UTF-8 without a BOM
    assert (out / "bom.txt").read_bytes() == b"\xef\xbb\xbfcaf\xc3\xa9"  # a BOM only when asked
    assert (out / "ascii.txt").read_bytes() == b"1.2.3\r\n"
    assert (out / "empty.txt").read_bytes() == b""
    assert (out / "lf.txt").read_bytes() == b"a\nb\r\nc"  # line endings are never touched
    assert (out / "raw.bin").read_bytes() == bytes([0, 255, 10, 13, 0, 7])
    assert (out / "nothing.bin").read_bytes() == b""
    assert (out / "replace.txt").read_bytes() == b"short"  # replaced whole, no old tail
    assert (out / "sub" / "deeper" / "made.txt").read_bytes() == b"new folder"
    expected = {"utf8.txt", "bom.txt", "ascii.txt", "empty.txt", "lf.txt", "raw.bin", "nothing.bin",
                "replace.txt", "made.txt"}  # fmt: skip
    assert leftovers(out, expected) == set()  # no temp file after success


@needs_powershell
def test_the_helper_swaps_a_new_file_in_instead_of_writing_over_the_old_one(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"{}")
    before = inode(target)
    run_ps(tmp_path, f". '{COMMON}'\nWrite-NsFileAtomic -Path '{target}' -Text '{{\"a\":1}}'\n")
    assert target.read_bytes() == b'{"a":1}'
    assert inode(target) != before  # a different file now has the name: no reader saw it half written


@needs_powershell
def test_a_failed_write_leaves_the_old_file_whole_and_no_temp_file(tmp_path: Path) -> None:
    held = tmp_path / "held.json"
    held.write_bytes(b"OLD")
    folder = tmp_path / "isadir"
    folder.mkdir()
    (folder / "inside.txt").write_bytes(b"x")
    result = tmp_path / "result.txt"
    body = f"""
. '{COMMON}'
$r = @()
# the helper must exist and work first: without this a missing helper would make every call "throw"
Write-NsFileAtomic -Path '{tmp_path}\\ok.json' -Text 'WORKS'
$fs =[IO.File]::Open('{held}', [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
try {{
    try {{ Write-NsFileAtomic -Path '{held}' -Text 'NEW' -Tries 3; $r += 'held:no error' }}
    catch {{ $r += 'held:threw' }}
}}
finally {{ $fs.Dispose() }}
try {{ Write-NsFileAtomic -Path '{folder}' -Text 'NEW' -Tries 3; $r += 'dir:no error' }}
catch {{ $r += 'dir:threw' }}
[IO.File]::WriteAllText('{result}', ($r -join ','))
"""
    run_ps(tmp_path, body)
    assert result.read_text() == "held:threw,dir:threw"
    assert (tmp_path / "ok.json").read_bytes() == b"WORKS"
    assert held.read_bytes() == b"OLD"
    assert (folder / "inside.txt").read_bytes() == b"x"
    assert leftovers(tmp_path, {"held.json", "inside.txt", "result.txt", "ok.json"}) == {
        p.name for p in tmp_path.glob("t*.ps1")
    }  # not one ~ns*.tmp left


# a C# compiler that is blocked (security software, policy): the shadowing `Add-Type` below throws the way the
# real one does, and the helper must still swap the file in whole instead of failing the launcher's write
BLOCKED_COMPILER = "function Add-Type { throw 'compilation blocked by policy' }\n"


@needs_powershell
def test_the_helper_still_writes_when_the_swap_helper_cannot_be_compiled(tmp_path: Path) -> None:
    replaced = tmp_path / "replaced.json"
    replaced.write_bytes(b"an older and much longer content than the new one")
    before = inode(replaced)
    held = tmp_path / "held.json"
    held.write_bytes(b"OLD")
    result = tmp_path / "result.txt"
    body = (
        BLOCKED_COMPILER
        + f"""
. '{COMMON}'
Write-NsFileAtomic -Path '{replaced}' -Text 'short'
Write-NsFileAtomic -Path '{tmp_path}\\fresh\\new.json' -Text 'brand new'
$fs = [IO.File]::Open('{held}', [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
try {{
    try {{ Write-NsFileAtomic -Path '{held}' -Text 'NEW' -Tries 3; $r = 'no error' }}
    catch {{ $r = 'threw' }}
}}
finally {{ $fs.Dispose() }}
[IO.File]::WriteAllText('{result}', $r)
"""
    )
    run_ps(tmp_path, body)
    assert (
        replaced.read_bytes() == b"short" and inode(replaced) != before
    )  # swapped in whole, not written over
    assert (tmp_path / "fresh" / "new.json").read_bytes() == b"brand new"
    assert result.read_text() == "threw" and held.read_bytes() == b"OLD"  # a real failure is still a failure
    assert leftovers(tmp_path, {"replaced.json", "new.json", "held.json", "result.txt"}) == {
        p.name for p in tmp_path.glob("t*.ps1")
    }


@needs_powershell
def test_the_host_still_writes_its_run_file_when_the_swap_helper_cannot_be_compiled(tmp_path: Path) -> None:
    run_file = tmp_path / "run" / "apphost-dashboard.json"
    run_file.parent.mkdir()
    body = (
        BLOCKED_COMPILER + f". '{COMMON}'\n{cut_functions(['Write-RunFile'])}"
        f"$runFile = '{run_file}'\nWrite-RunFile -Handle ([IntPtr]4660) -Ready\nWrite-Output $PID\n"
    )
    pid = int(run_ps(tmp_path, body).strip())
    assert json.loads(run_file.read_bytes()) == {"hwnd": 4660, "pid": pid, "ready": True}


@needs_powershell
def test_a_reader_that_holds_the_file_for_a_moment_does_not_make_the_write_fail(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b"OLD")
    marker = tmp_path / "go"
    proc = start_ps(
        tmp_path,
        # the first call compiles the swap helper; the hold starts after it
        f". '{COMMON}'\nWrite-NsFileAtomic -Path '{tmp_path}\\warm.txt' -Text 'x'\n"
        f"[IO.File]::WriteAllText('{marker}', 'go')\nWrite-NsFileAtomic -Path '{target}' -Text 'NEW'\n",
    )
    hold = Hold(target)
    try:
        hold_until_the_script_is_in_the_call(target, marker, 0.4)
    finally:
        hold.release()
    finish(proc)
    assert target.read_bytes() == b"NEW"
    assert leftovers(tmp_path, {"state.json", "go", "warm.txt"}) == {p.name for p in tmp_path.glob("t*.ps1")}


# ----------------------------------------------------------------------------------------------------------
# a reader hammering while the file is swapped 40 times never sees a torn file
# ----------------------------------------------------------------------------------------------------------
PS_READER = """
. '{common}'
[IO.File]::WriteAllText($env:T_READY, 'ready')
$reads = 0; $torn = 0; $stuck = 0
while (-not (Test-Path -LiteralPath $env:T_STOP)) {{
    try {{
        $t = Read-NsTextRetry -Path $env:T_FILE
        $reads++
        if ($null -eq $t) {{ $torn++ }}
        else {{
            $o = $t | ConvertFrom-Json
            if ($o.pad.Length -ne $o.len) {{ $torn++ }}
        }}
    }}
    catch {{ $stuck++ }}
}}
[IO.File]::WriteAllText($env:T_OUT, "$reads $torn $stuck")
"""

PS_WRITER = """
. '{common}'
for ($i = 0; $i -lt 40; $i++) {{
    $len = 1000 + (($i * 937) % 19000)
    $obj = [ordered]@{{ n = $i; len = $len; pad = ('x' * $len) }}
    Write-NsFileAtomic -Path $env:T_FILE -Text ($obj | ConvertTo-Json -Compress)
    Start-Sleep -Milliseconds 15
}}
"""


def payload(n: int) -> bytes:
    length = 1000 + (n * 937) % 19000
    return json.dumps({"n": n, "len": length, "pad": "x" * length}, separators=(",", ":")).encode()


@needs_powershell
def test_readers_never_see_a_torn_file_while_the_helper_swaps_it_forty_times(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(payload(0))
    ready, stop, out = tmp_path / "reader-ready", tmp_path / "stop", tmp_path / "reader-out"
    env = {"T_FILE": str(target), "T_READY": str(ready), "T_STOP": str(stop), "T_OUT": str(out)}
    ps_reader = start_ps(tmp_path, PS_READER.format(common=COMMON), env)
    stats = SimpleNamespace(reads=0, torn=0, stuck=0)
    done = threading.Event()

    def python_reader() -> None:
        while not done.is_set():
            data = None
            # far more patient than a product reader (ns_common.atomic.retry_busy: 6 tries, about 125 ms in
            # all): this test counts torn files, not how long a refusal can last
            for _ in range(250):
                try:
                    data = target.read_bytes()
                    break
                except PermissionError:
                    time.sleep(0.002)
                except FileNotFoundError:
                    break
            stats.reads += 1
            if data is None:
                stats.stuck += 1
                continue
            try:
                doc = json.loads(data)
                if len(doc["pad"]) != doc["len"]:
                    stats.torn += 1
            except ValueError:
                stats.torn += 1

    thread = threading.Thread(target=python_reader, daemon=True)
    try:
        deadline = time.time() + 90
        while not ready.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert ready.exists(), "the PowerShell reader did not start"
        thread.start()
        finish(start_ps(tmp_path, PS_WRITER.format(common=COMMON), env))
    finally:
        done.set()
        stop.write_text("stop")
        thread.join(timeout=30)
    finish(ps_reader)
    reads, torn, stuck = (int(x) for x in out.read_text().split())
    assert target.read_bytes() == payload(39)  # the last swap won, whole
    assert stats.reads >= 20 and stats.torn == 0 and stats.stuck == 0, vars(stats)
    assert reads >= 20 and torn == 0 and stuck == 0, (reads, torn, stuck)
    assert leftovers(tmp_path, {"state.json", "reader-ready", "stop", "reader-out"}) == {
        p.name for p in tmp_path.glob("t*.ps1")
    }


# ----------------------------------------------------------------------------------------------------------
# the PowerShell reader
# ----------------------------------------------------------------------------------------------------------
@needs_powershell
def test_the_reader_decodes_bom_and_plain_utf8_and_says_null_for_a_missing_file(tmp_path: Path) -> None:
    (tmp_path / "plain.txt").write_bytes("café".encode())
    (tmp_path / "bom.txt").write_bytes(b"\xef\xbb\xbf" + "café".encode())
    (tmp_path / "empty.txt").write_bytes(b"")
    body = f"""
. '{COMMON}'
$d = '{tmp_path}'
$o = @()
foreach ($n in @('plain.txt', 'bom.txt', 'empty.txt')) {{
    $t = Read-NsTextRetry -Path "$d\\$n"
    $o += ($n + '=' + ((($t.ToCharArray() | ForEach-Object {{ [int]$_ }}) -join ',')))
}}
$o += ('missing=' + ($null -eq (Read-NsTextRetry -Path "$d\\nope.txt")))
$o += ('nofolder=' + ($null -eq (Read-NsTextRetry -Path "$d\\nofolder\\nope.txt")))
[IO.File]::WriteAllText("$d\\result.txt", ($o -join ';'))
"""
    run_ps(tmp_path, body)
    assert (tmp_path / "result.txt").read_text() == (
        "plain.txt=99,97,102,233;bom.txt=99,97,102,233;empty.txt=;missing=True;nofolder=True"
    )


@needs_powershell
def test_the_reader_rides_out_a_refused_open_and_gives_up_on_a_file_that_stays_shut(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    target.write_bytes(b'{"a":1}')
    marker = tmp_path / "go"
    result = tmp_path / "result.txt"
    proc = start_ps(
        tmp_path,
        f". '{COMMON}'\n[IO.File]::WriteAllText('{marker}', 'go')\n"
        f"[IO.File]::WriteAllText('{result}', (Read-NsTextRetry -Path '{target}'))\n",
    )
    hold = Hold(target)
    try:
        hold_until_the_script_is_in_the_call(target, marker, 0.3)
    finally:
        hold.release()
    finish(proc)
    assert result.read_text() == '{"a":1}'
    # a file that stays shut for the whole retry time is an error for the caller, never "no file"
    hold = Hold(target)
    try:
        read = f"Read-NsTextRetry -Path '{target}' -Tries 3"
        body = f". '{COMMON}'\ntry {{ {read}; 'value' }} catch {{ 'threw' }}\n"
        assert run_ps(tmp_path, body).strip() == "threw"
    finally:
        hold.release()


@needs_powershell
def test_the_supervisor_port_comes_from_runtime_json_even_while_it_is_being_swapped(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    runtime = config / "runtime.json"
    runtime.write_text('{"supervisor_port": 8123}', encoding="utf-8")
    marker = tmp_path / "go"
    proc = start_ps(
        tmp_path,
        f". '{COMMON}'\n[IO.File]::WriteAllText('{marker}', 'go')\nGet-NsSupervisorPort\n",
        {"NIGHTSHIFT_HOME": str(tmp_path)},
    )
    hold = Hold(runtime)
    try:
        hold_until_the_script_is_in_the_call(runtime, marker, 0.3)
    finally:
        hold.release()
    assert finish(proc).strip() == "8123"  # not the 8099 default a refused read used to fall back to


# ----------------------------------------------------------------------------------------------------------
# the launchers' own writers and readers, run for real against a throwaway home
# ----------------------------------------------------------------------------------------------------------
@needs_powershell
def test_the_host_writes_its_run_file_through_the_helper(tmp_path: Path) -> None:
    run_file = tmp_path / "run" / "apphost-dashboard.json"
    run_file.parent.mkdir()
    run_file.write_bytes(b'{"hwnd":1,"pid":1,"ready":false}')
    before = inode(run_file)
    body = (
        f". '{COMMON}'\n{cut_functions(['Write-RunFile'])}"
        f"$runFile = '{run_file}'\nWrite-RunFile -Handle ([IntPtr]4660) -Ready\nWrite-Output $PID\n"
    )
    pid = int(run_ps(tmp_path, body).strip())
    raw = run_file.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")  # no BOM, as before
    assert json.loads(raw) == {"hwnd": 4660, "pid": pid, "ready": True}
    assert raw == f'{{"hwnd":4660,"pid":{pid},"ready":true}}'.encode()  # compact, no trailing newline
    assert inode(run_file) != before and leftovers(run_file.parent, {"apphost-dashboard.json"}) == set()


@needs_powershell
def test_the_host_writes_window_json_through_the_helper(tmp_path: Path) -> None:
    state_file = tmp_path / "config" / "window.json"
    state_file.parent.mkdir()
    state_file.write_bytes(b'{"left":0,"top":0,"width":1,"height":1,"maximized":true}')
    before = inode(state_file)
    body = (
        f". '{COMMON}'\nAdd-Type -AssemblyName PresentationFramework, WindowsBase\n"
        f"{cut_functions(['Save-WindowState'])}"
        f"$stateFile = '{state_file}'\n"
        "$rect = New-Object System.Windows.Rect 10.4, 20, 1100, 700\n"
        "$script:win = [pscustomobject]@{ RestoreBounds = $rect; "
        "WindowState = [System.Windows.WindowState]::Normal }\n"
        "Save-WindowState\n"
    )
    run_ps(tmp_path, body)
    assert state_file.read_bytes() == b'{"left":10,"top":20,"width":1100,"height":700,"maximized":false}'
    assert inode(state_file) != before and leftovers(state_file.parent, {"window.json"}) == set()


@needs_powershell
def test_the_host_reads_window_json_even_while_it_is_being_swapped(tmp_path: Path) -> None:
    state_file = tmp_path / "window.json"
    state_file.write_bytes(b'{"left":5,"top":6,"width":1200,"height":800,"maximized":false}')
    marker = tmp_path / "go"
    proc = start_ps(
        tmp_path,
        f". '{COMMON}'\n{cut_functions(['Read-WindowState'])}$stateFile = '{state_file}'\n"
        f"[IO.File]::WriteAllText('{marker}', 'go')\n"
        "$s = Read-WindowState\nif ($null -eq $s) { 'none' } else { $s.width }\n",
    )
    hold = Hold(state_file)
    try:
        hold_until_the_script_is_in_the_call(state_file, marker, 0.3)
    finally:
        hold.release()
    assert finish(proc).strip() == "1200"  # a refused read used to mean "no saved window, use the default"


@needs_powershell
def test_the_host_writes_the_cache_stamp_through_the_helper_with_the_same_bytes_as_before(
    tmp_path: Path,
) -> None:
    app, profile = tmp_path / "app", tmp_path / "profile"
    app.mkdir()
    profile.mkdir()
    (app / "VERSION").write_bytes(b"1.9.0\n")
    stamp = profile / "ns-cache-version.txt"
    stamp.write_bytes(b"1.8.0\r\n")
    before = inode(stamp)
    body = (
        f". '{COMMON}'\n{cut_functions(['Reset-StaleHttpCache'])}"
        f"Reset-StaleHttpCache -ProfileDir '{profile}' -AppDir '{app}'\n"
    )
    assert run_ps(tmp_path, body).strip() == "True"
    assert stamp.read_bytes() == b"1.9.0\r\n"  # what Set-Content -Encoding ascii wrote: text + CRLF
    assert inode(stamp) != before and leftovers(profile, {"ns-cache-version.txt"}) == set()


OPENCODE = {"provider": {"local": {"options": {"baseURL": "http://127.0.0.1:8080/v1", "apiKey": "x"}}}}


@needs_powershell
def test_the_opencode_port_sync_swaps_the_file_and_keeps_it_whole_when_it_cannot(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "runtime.json").write_text("{}", encoding="utf-8")
    target = config / "opencode.json"
    target.write_text(json.dumps(OPENCODE), encoding="utf-8")
    before = inode(target)
    env = {"NIGHTSHIFT_HOME": str(tmp_path)}
    run_ps(tmp_path, f". '{COMMON}'\nSync-NsOpenCodePort -BaseUrl 'http://127.0.0.1:9191/v1'\n", env)
    raw = target.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf") and raw.endswith(b"\n")
    assert json.loads(raw)["provider"]["local"]["options"]["baseURL"] == "http://127.0.0.1:9191/v1"
    assert inode(target) != before and leftovers(config, {"runtime.json", "opencode.json"}) == set()
    # the file cannot be swapped (a process keeps it shut): the owner's file is untouched, the launcher
    # goes on with a note, and no temp file stays
    good = target.read_bytes()
    hold = Hold(target)
    try:
        out = run_ps(
            tmp_path, f". '{COMMON}'\nSync-NsOpenCodePort -BaseUrl 'http://127.0.0.1:7777/v1'\n", env
        )
    finally:
        hold.release()
    assert "Could not update the port" in out
    assert target.read_bytes() == good and leftovers(config, {"runtime.json", "opencode.json"}) == set()


@needs_powershell
def test_a_patch_switch_that_could_not_be_removed_is_said_so_and_never_called_removed(tmp_path: Path) -> None:
    """Block B proof, defect 1: the sync takes "patch": false back (it removes OpenCode's edit and write
    tools). The file can be read but not replaced (another program has it open): the file stays whole, nothing
    says "Removed", and the owner is told that Nightshift Code still cannot change a file and what to do."""
    config = tmp_path / "config"
    config.mkdir()
    (config / "runtime.json").write_text("{}", encoding="utf-8")
    target = config / "opencode.json"
    cfg = {**OPENCODE, "tools": {"todowrite": False, "todoread": False, "webfetch": False, "task": False,
                                 "patch": False}}
    target.write_text(json.dumps(cfg), encoding="utf-8")
    before = target.read_bytes()
    # opened for reading, sharing reads only: the sync can read it, the swap is refused
    handle = _k32.CreateFileW(str(target), 0x80000000, 0x1, None, 3, 0x80, None)
    assert handle not in (None, ctypes.c_void_p(-1).value), ctypes.get_last_error()
    env = {"NIGHTSHIFT_HOME": str(tmp_path)}
    try:
        out = run_ps(tmp_path, f". '{COMMON}'\nSync-NsOpenCodePort\n", env)
    finally:
        _k32.CloseHandle(handle)
    assert target.read_bytes() == before and leftovers(config, {"runtime.json", "opencode.json"}) == set()
    assert "Could not update the port" in out and "Removed" not in out
    assert '"patch": false is still in that file' in out and "cannot change a file" in out
    assert "start Nightshift Code again" in out
    # the file is free again: the next start removes the switch and says so
    out = run_ps(tmp_path, f". '{COMMON}'\nSync-NsOpenCodePort\n", env)
    assert 'Removed "patch": false' in out and "patch" not in json.loads(target.read_bytes())["tools"]


@needs_powershell
def test_stop_all_still_closes_a_host_whose_run_file_is_being_swapped(tmp_path: Path) -> None:
    """The `nothing to stop` family: a refused read of apphost-*.json was taken for a stale file, so the file
    was deleted and the window host kept running."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run_file = run_dir / "apphost-dashboard.json"
    warm = tmp_path / "warm"
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        # the first call compiles the window helper; only then is the run file written and shut
        # the script waits for the `shut` marker (written once the file exists AND is held) before its second
        # call, so there is no fixed sleep for a loaded runner to overrun
        shut = tmp_path / "shut"
        proc = start_ps(
            tmp_path,
            f". '{COMMON}'\nClose-NsHostWindows -GraceSec 1\n[IO.File]::WriteAllText('{warm}', 'go')\n"
            "$until = (Get-Date).AddSeconds(90)\n"
            f"while (-not (Test-Path -LiteralPath '{shut}') -and (Get-Date) -lt $until)"
            " { Start-Sleep -Milliseconds 20 }\n"
            "Close-NsHostWindows -GraceSec 1\n",
            {"NIGHTSHIFT_HOME": str(tmp_path)},
        )
        hold_until_the_script_is_in_the_call(run_file, warm, 0)
        run_file.write_text(json.dumps({"hwnd": 0, "pid": sleeper.pid, "ready": True}), encoding="utf-8")
        hold = Hold(run_file)
        shut.write_text("shut")
        time.sleep(0.7)
        hold.release()
        out = finish(proc)
        assert "Closed the Nightshift window (dashboard)" in out, out
        assert sleeper.wait(timeout=30) is not None  # the host process was stopped
        assert not run_file.exists()
    finally:
        if sleeper.poll() is None:
            sleeper.kill()
            sleeper.wait(timeout=30)


@needs_powershell
def test_stop_all_leaves_a_run_file_it_cannot_read_alone(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    run_file = run_dir / "apphost-setup.json"
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        run_file.write_text(json.dumps({"hwnd": 0, "pid": sleeper.pid}), encoding="utf-8")
        hold = Hold(run_file)
        try:
            out = run_ps(
                tmp_path,
                f". '{COMMON}'\nClose-NsHostWindows -GraceSec 1\n",
                {"NIGHTSHIFT_HOME": str(tmp_path)},
            )
        finally:
            hold.release()
        assert "Could not read" in out and "Closed the Nightshift window" not in out
        assert run_file.exists() and sleeper.poll() is None  # neither deleted nor taken for stale
    finally:
        sleeper.kill()
        sleeper.wait(timeout=30)


@needs_powershell
def test_stop_all_still_treats_a_garbage_or_empty_run_file_as_stale(tmp_path: Path) -> None:
    """The other half of the contract: a file that is readable but is not a window record is removed."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    garbage = run_dir / "apphost-dashboard.json"
    empty = run_dir / "apphost-setup.json"
    dead_pid = run_dir / "apphost-other.json"
    garbage.write_bytes(b"not json at all {")
    empty.write_bytes(b"")
    dead_pid.write_text(json.dumps({"hwnd": 0, "pid": 2147483000}), encoding="utf-8")  # no such process
    out = run_ps(
        tmp_path, f". '{COMMON}'\nClose-NsHostWindows -GraceSec 1\n", {"NIGHTSHIFT_HOME": str(tmp_path)}
    )
    assert not garbage.exists() and not empty.exists() and not dead_pid.exists()
    assert "Closed the Nightshift window" not in out and "Could not read" not in out


# ----------------------------------------------------------------------------------------------------------
# a guard for the next writer someone adds
# ----------------------------------------------------------------------------------------------------------
INSTALLER = ROOT / "installer"
FORBIDDEN_WRITES = ("WriteAllText", "WriteAllBytes", "Set-Content", "Out-File", "Add-Content",
                    "AppendAllText",
                    "[IO.File]::Create", "[System.IO.File]::Create", "Tee-Object")  # fmt: skip


def test_no_launcher_writes_a_file_except_through_the_helper() -> None:
    for script in sorted(LAUNCHERS.glob("*.ps1")):
        text = script.read_text(encoding="ascii")
        for needle in FORBIDDEN_WRITES:
            assert needle not in text, (script.name, needle)
    assert ". (Join-Path $PSScriptRoot '_common.ps1')" in HOST.read_text(encoding="ascii")


def test_no_installer_script_writes_a_file_another_process_reads_in_place() -> None:
    """stage1.ps1 (the hand-over files the setup server reads) and pick-folder.ps1 (the dialog's result file)
    write through their own `Write-NsFileSwap`; the other installer scripts write nothing."""
    for script in sorted(INSTALLER.glob("*.ps1")):
        text = script.read_text(encoding="utf-8")
        for needle in FORBIDDEN_WRITES:
            assert needle not in text, (script.name, needle)
    stage1 = (INSTALLER / "stage1.ps1").read_text(encoding="utf-8")
    assert "Write-NsFileSwap -Path $Out " in stage1 and "Write-NsFileSwap -Path $preflightFile " in stage1
    picker = (INSTALLER / "pick-folder.ps1").read_text(encoding="utf-8")
    assert "Write-NsFileSwap -Path $Out -Text $chosen" in picker


@needs_powershell
@pytest.mark.parametrize("script", ["stage1.ps1", "pick-folder.ps1"])
def test_the_installer_swap_writes_whole_files_and_leaves_the_old_one_on_failure(
    tmp_path: Path, script: str
) -> None:
    out = tmp_path / "out"
    out.mkdir()
    replaced = out / "replaced.json"
    replaced.write_bytes(b"an older and much longer content than the new one")
    before = inode(replaced)
    held = out / "held.json"
    held.write_bytes(b"OLD")
    result = tmp_path / "result.txt"
    body = f"""
{cut_functions(["Write-NsFileSwap"], INSTALLER / script)}
$e = [string][char]0xE9
Write-NsFileSwap -Path '{replaced}' -Text ('caf' + $e)
Write-NsFileSwap -Path '{out}\\empty.txt' -Text ''
Write-NsFileSwap -Path '{out}\\new.json' -Text '{{"a":1}}'
$fs = [IO.File]::Open('{held}', [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
try {{
    try {{ Write-NsFileSwap -Path '{held}' -Text 'NEW'; $r = 'no error' }}
    catch {{ $r = 'threw' }}
}}
finally {{ $fs.Dispose() }}
[IO.File]::WriteAllText('{result}', $r)
"""
    run_ps(tmp_path, body)
    assert replaced.read_bytes() == b"caf\xc3\xa9" and inode(replaced) != before  # no BOM, swapped in whole
    assert (out / "empty.txt").read_bytes() == b"" and (out / "new.json").read_bytes() == b'{"a":1}'
    assert result.read_text() == "threw" and held.read_bytes() == b"OLD"
    assert leftovers(out, {"replaced.json", "empty.txt", "new.json", "held.json"}) == set()


# `Start-Sleep` is shadowed so the test counts the pauses the swap takes between refused attempts and, on the
# chosen pause, lets go of the file: "refused twice, then allowed" is exact, not a matter of timing.
PS_COUNT_SLEEPS = """
$global:slept = 0
$global:releaseAt = {release_at}
function Start-Sleep {{
    param([int]$Milliseconds)
    $global:slept++
    if ($global:slept -eq $global:releaseAt -and $global:holder) {{
        $global:holder.Dispose(); $global:holder = $null
    }}
}}
"""


@needs_powershell
@pytest.mark.parametrize("script", ["stage1.ps1", "pick-folder.ps1"])
def test_the_installer_swap_rides_out_a_target_that_is_refused_twice_then_allowed(
    tmp_path: Path, script: str
) -> None:
    """A second setup run, an antivirus scan or the setup server reading the hand-over file keeps it open for
    a moment; the swap is retried (bounded) instead of failing the whole preflight."""
    held = tmp_path / "held.json"
    held.write_bytes(b"OLD")
    result = tmp_path / "result.txt"
    body = f"""
{cut_functions(["Write-NsFileSwap"], INSTALLER / script)}
{PS_COUNT_SLEEPS.format(release_at=2)}
$global:holder = [IO.File]::Open('{held}', [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
try {{ Write-NsFileSwap -Path '{held}' -Text 'NEW'; $r = 'written' }}
catch {{ $r = 'threw: ' + $_.Exception.Message }}
finally {{ if ($global:holder) {{ $global:holder.Dispose() }} }}
[IO.File]::WriteAllText('{result}', $r + '|' + $global:slept)
"""
    run_ps(tmp_path, body)
    assert result.read_text() == "written|2"  # refused on try 1 and 2, swapped in on try 3
    assert held.read_bytes() == b"NEW"
    assert leftovers(tmp_path, {"held.json", "result.txt"}) == {p.name for p in tmp_path.glob("t*.ps1")}


@needs_powershell
@pytest.mark.parametrize("script", ["stage1.ps1", "pick-folder.ps1"])
def test_the_installer_swap_gives_up_on_a_target_that_stays_shut_and_leaves_the_old_file(
    tmp_path: Path, script: str
) -> None:
    held = tmp_path / "held.json"
    held.write_bytes(b"OLD")
    result = tmp_path / "result.txt"
    body = f"""
{cut_functions(["Write-NsFileSwap"], INSTALLER / script)}
{PS_COUNT_SLEEPS.format(release_at=-1)}
$global:holder = [IO.File]::Open('{held}', [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
try {{ Write-NsFileSwap -Path '{held}' -Text 'NEW' -Tries 4; $r = 'written' }}
catch {{ $r = 'threw ' + $_.Exception.GetType().Name }}
finally {{ $global:holder.Dispose() }}
[IO.File]::WriteAllText('{result}', $r + '|' + $global:slept)
"""
    run_ps(tmp_path, body)
    assert result.read_text().endswith("|3")  # four tries, a pause between each, then the error
    assert result.read_text().startswith("threw ") and "Binding" not in result.read_text()
    assert held.read_bytes() == b"OLD"
    assert leftovers(tmp_path, {"held.json", "result.txt"}) == {p.name for p in tmp_path.glob("t*.ps1")}


# ----------------------------------------------------------------------------------------------------------
# the Python readers of the files the launchers write
# ----------------------------------------------------------------------------------------------------------
@pytest.mark.skipif(sys.platform != "win32", reason="Windows file sharing")
def test_the_engine_still_sees_the_dashboard_window_while_its_run_file_is_swapped(tmp_path: Path) -> None:
    from ns_common.paths import Paths
    from ns_supervisor import presence

    run_file = tmp_path / "run" / "apphost-dashboard.json"
    run_file.parent.mkdir()
    run_file.write_bytes(b'\xef\xbb\xbf{"hwnd":1,"pid":4242,"ready":true}')
    hold = Hold(run_file)
    threading.Timer(0.1, hold.release).start()
    assert presence.host_window_open(Paths(tmp_path), image=lambda pid: "powershell.exe") is True
    assert (
        presence.host_window_open(Paths(tmp_path / "elsewhere"), image=lambda pid: "powershell.exe") is False
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows file sharing")
def test_the_uninstaller_still_finds_the_setup_host_while_its_run_file_is_swapped(tmp_path: Path) -> None:
    from installer.stage2.web import uninstall

    run_file = tmp_path / "run" / "apphost-setup.json"
    run_file.parent.mkdir()
    run_file.write_bytes(b'{"hwnd":1,"pid":4242}')
    hold = Hold(run_file)
    threading.Timer(0.1, hold.release).start()
    assert uninstall.host_pids(tmp_path) == [os.getpid(), 4242]


def test_both_run_file_readers_use_the_shared_retry_reader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One retry rule for every product reader (`ns_common.atomic.retry_busy`), not a private copy each."""
    from installer.stage2.web import uninstall
    from ns_common import atomic
    from ns_common.paths import Paths
    from ns_supervisor import presence

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "apphost-dashboard.json").write_bytes(b'\xef\xbb\xbf{"hwnd":1,"pid":4242}')
    (run_dir / "apphost-setup.json").write_bytes(b'\xef\xbb\xbf{"hwnd":1,"pid":4343}')
    seen: list[str] = []
    real = atomic.retry_busy

    def recording(fn, **kwargs):  # type: ignore[no-untyped-def]
        seen.append("retry_busy")
        return real(fn, **kwargs)

    monkeypatch.setattr(atomic, "retry_busy", recording)
    assert presence.host_window_open(Paths(tmp_path), image=lambda pid: "powershell.exe") is True
    assert uninstall.host_pids(tmp_path) == [os.getpid(), 4343]  # a BOM does not hide the pid
    assert seen == ["retry_busy", "retry_busy"]
    assert not hasattr(presence, "_read_run_file") and not hasattr(uninstall, "_read_run_file")
