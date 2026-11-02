"""C1: stage 1 hands the setup server its arguments so paths with spaces survive (C:\\Users\\John Doe\\...).

`& $py @serverArgs` makes Windows PowerShell 5.1 quote arguments that contain spaces itself; the old code had
already wrapped them in literal double quotes, so `C:\\Users\\John Doe\\...` arrived mangled. These tests run
the real functions of stage1.ps1 and let a real child process echo the argv it received.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from ns_supervisor import pwsh

ROOT = Path(__file__).resolve().parents[1]
STAGE1 = ROOT / "installer" / "stage1.ps1"
PS = shutil.which("powershell") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(PS is None, reason="PowerShell not available")

LOAD = r"""
$errs = $null; $tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:NS_STAGE1, [ref]$tokens, [ref]$errs)
$wanted = @('Get-ServerArgs', 'ConvertTo-ArgumentString')
$kind = [System.Management.Automation.Language.FunctionDefinitionAst]
$defs = $ast.FindAll({ param($n) $n -is $kind -and $n.Name -in $wanted }, $true)
if (@($defs).Count -ne 2) { Write-Output 'MISSING FUNCTIONS'; exit 9 }
foreach ($d in $defs) { . ([scriptblock]::Create($d.Extent.Text)) }
$ps = (Get-Process -Id $PID).Path
"""

SPACED_APP = r"C:\Users\John Doe\Night Shift"
SPACED_PRE = r"C:\Users\John Doe\AppData\Local\Temp\nightshift-preflight.json"
SPACED_ANS = r"D:\My Answers\answers.json"

EXPECTED = [
    "-X", "utf8", "-m", "installer.stage2.web",
    "--app-path", SPACED_APP, "--preflight", SPACED_PRE, "--answers", SPACED_ANS, "--headless",
]  # fmt: skip


def run_ps(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    echo_dir = tmp_path / "echo dir"
    echo_dir.mkdir(exist_ok=True)
    echo = echo_dir / "echo.ps1"
    echo.write_text("ConvertTo-Json -InputObject @($args) -Compress\n", encoding="ascii")
    env = {
        "NS_STAGE1": str(STAGE1),
        "NS_ECHO": str(echo),
        "NS_OUT": str(tmp_path / "out.txt"),
        "NS_APP": SPACED_APP,
        "NS_PRE": SPACED_PRE,
        "NS_ANS": SPACED_ANS,
    }
    return pwsh.run_powershell(LOAD + body, env_extra=env, timeout_s=180)


def argv_of(proc: subprocess.CompletedProcess[str]) -> list[str]:
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip().startswith("[")]
    assert lines, proc.stdout + proc.stderr
    return json.loads(lines[-1])


def test_headless_splat_delivers_paths_with_spaces_unchanged(tmp_path: Path) -> None:
    body = r"""
$a = Get-ServerArgs -App $env:NS_APP -PreflightFile $env:NS_PRE -AnswersPath $env:NS_ANS -Headless $true
& $ps -NoProfile -NonInteractive -File $env:NS_ECHO @a
"""
    assert argv_of(run_ps(tmp_path, body)) == EXPECTED


def test_window_mode_start_process_delivers_paths_with_spaces_unchanged(tmp_path: Path) -> None:
    body = r"""
$a = Get-ServerArgs -App $env:NS_APP -PreflightFile $env:NS_PRE -AnswersPath $env:NS_ANS -Headless $true
$line = '-NoProfile -NonInteractive -File "' + $env:NS_ECHO + '" ' + (ConvertTo-ArgumentString $a)
Start-Process -FilePath $ps -ArgumentList $line -Wait -NoNewWindow -RedirectStandardOutput $env:NS_OUT
Get-Content -LiteralPath $env:NS_OUT
"""
    assert argv_of(run_ps(tmp_path, body)) == EXPECTED


def test_server_args_without_answers_or_headless(tmp_path: Path) -> None:
    body = r"""
$a = Get-ServerArgs -App $env:NS_APP -PreflightFile $env:NS_PRE
& $ps -NoProfile -NonInteractive -File $env:NS_ECHO @a
"""
    assert argv_of(run_ps(tmp_path, body)) == EXPECTED[:8]


def test_argument_string_handles_trailing_backslashes_and_quotes(tmp_path: Path) -> None:
    tricky = ["C:\\dir with space\\", 'say "hi" there', "plain", "back\\slash"]
    body = r"""
$a = @('C:\dir with space\', 'say "hi" there', 'plain', 'back\slash')
$line = '-NoProfile -NonInteractive -File "' + $env:NS_ECHO + '" ' + (ConvertTo-ArgumentString $a)
Start-Process -FilePath $ps -ArgumentList $line -Wait -NoNewWindow -RedirectStandardOutput $env:NS_OUT
Get-Content -LiteralPath $env:NS_OUT
"""
    assert argv_of(run_ps(tmp_path, body)) == tricky


def test_the_script_never_wraps_arguments_in_literal_quotes() -> None:
    text = STAGE1.read_text(encoding="ascii")
    assert "'\"{0}\"' -f" not in text
    assert "& $py @serverArgs" in text and "Get-ServerArgs" in text


def test_dry_run_with_a_spaced_app_path_prints_unquoted_arguments(tmp_path: Path) -> None:
    app = tmp_path / "John Doe" / "Night Shift"
    app.mkdir(parents=True)
    shutil.copy(ROOT / "manifest.json", app / "manifest.json")
    env = {k: v for k, v in os.environ.items() if k != "NIGHTSHIFT_HOME"}
    env["NS_NONINTERACTIVE"] = "1"
    cmd = [str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(STAGE1), "-AppPath", str(app)]
    proc = subprocess.run(
        [*cmd, "-DryRun"], capture_output=True, text=True, timeout=600, cwd=ROOT, env=env
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = next(ln for ln in proc.stdout.splitlines() if "start setup server" in ln)
    assert f"--app-path {app}" in line and '"' not in line
