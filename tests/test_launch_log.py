"""G-52: the Dashboard launcher writes each launch step to logs\\launch-<date>.log, and an error nothing
catches ends in that log and a window that stays open, not in a console that closes with the message."""

import os
import re
import subprocess
from pathlib import Path

import pytest

from ns_supervisor import pwsh

LAUNCHERS = Path(__file__).resolve().parents[1] / "launchers"
needs_powershell = pytest.mark.skipif(pwsh.find_powershell() is None, reason="powershell not on PATH")


def make_home(tmp_path: Path) -> Path:
    home = tmp_path / "Night Home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "runtime.json").write_text('{"supervisor_port": 8197}', encoding="ascii")
    return home


def run(tmp_path: Path, home: Path, body: str) -> "subprocess.CompletedProcess[str]":
    """`body` as a launcher script of its own: strict mode, stop on error, `_common.ps1` loaded."""
    script = tmp_path / "probe-launcher.ps1"
    script.write_text(
        "Set-StrictMode -Version Latest\n$ErrorActionPreference = 'Stop'\n"
        f". '{LAUNCHERS / '_common.ps1'}'\n{body}\n",
        encoding="ascii",
    )
    exe = pwsh.find_powershell()
    assert exe is not None
    env = {**os.environ, "NIGHTSHIFT_HOME": str(home)}
    return subprocess.run([exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                          input="\n", capture_output=True, text=True, timeout=120, env=env)


def lines(home: Path) -> list[str]:
    out: list[str] = []
    for f in sorted((home / "logs").glob("launch-*.log")):
        raw = f.read_bytes()
        assert not raw.startswith(b"\xef\xbb\xbf")                   # UTF-8 without a byte order mark
        out += raw.decode("utf-8").splitlines()
    return out


LINE = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} pid=\d+ (INFO|ERROR) probe-launcher\.ps1 (.*)$")


@needs_powershell
def test_a_step_is_one_line_with_time_pid_level_launcher_and_words(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    body = "Write-NsLaunchLog 'engine' \"two`r`nlines\"\nWrite-NsLaunchLog 'page' 'down' 'ERROR'"
    r = run(tmp_path, home, body)
    assert r.returncode == 0, r.stdout + r.stderr
    got = lines(home)
    assert len(got) == 2, got
    first, second = LINE.match(got[0]), LINE.match(got[1])
    assert first and first.group(1) == "INFO" and first.group(2) == "engine two | lines"
    assert second and second.group(1) == "ERROR" and second.group(2) == "page down"
    day = got[0][:10].replace("-", "")
    assert (home / "logs" / f"launch-{day}.log").is_file()


@needs_powershell
def test_an_error_nothing_catches_is_logged_and_keeps_the_window_open(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    trap = (LAUNCHERS / "nightshift-dashboard.ps1").read_text(encoding="ascii")
    block = trap[trap.index("trap {") : trap.index("\n}\n", trap.index("trap {")) + 3]
    r = run(tmp_path, home, block + "\nWrite-NsLaunchLog 'step' 'before'\nthrow 'the step that failed'\n")
    assert r.returncode == 1, r.stdout + r.stderr       # Exit-NsWithPause's code, after its pause
    assert "The Nightshift launcher stopped: the step that failed" in r.stdout
    got = "\n".join(lines(home))
    assert "step before" in got
    assert "ERROR probe-launcher.ps1 error the step that failed" in got
    assert "stopped exit 1: The Nightshift launcher stopped: the step that failed" in got


@needs_powershell
def test_the_wait_for_the_engine_says_what_became_of_the_process_it_started(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    body = (
        "$script:NsEngineProcess = Start-Process -FilePath 'cmd.exe' -ArgumentList '/c exit 5' "
        "-WindowStyle Hidden -PassThru\n"
        "$null = $script:NsEngineProcess.Handle\n"
        "$null = $script:NsEngineProcess.WaitForExit(20000)\n"
        "Write-Host ('result=' + (Wait-NsSupervisor -TimeoutSec 2))\n"
    )
    r = run(tmp_path, home, body)
    assert "result=False" in r.stdout, r.stdout + r.stderr
    assert "no answer after 2 s: the engine process" in "\n".join(lines(home))
    assert "had already ended with exit code 5" in "\n".join(lines(home))


@needs_powershell
def test_with_no_engine_started_by_this_launcher_the_log_says_so(tmp_path: Path) -> None:
    home = make_home(tmp_path)
    r = run(tmp_path, home, "Write-Host ('result=' + (Wait-NsSupervisor -TimeoutSec 2))")
    assert "result=False" in r.stdout, r.stdout + r.stderr
    assert "no engine process was started by this launcher" in "\n".join(lines(home))


def test_the_dashboard_launcher_logs_its_steps_in_order() -> None:
    text = (LAUNCHERS / "nightshift-dashboard.ps1").read_text(encoding="ascii")
    steps = ["trap {", "Write-NsLaunchLog 'launcher'", "Write-NsLaunchLog 'engine' 'not running",
             "Write-NsLaunchLog 'dashboard'", "Write-NsLaunchLog 'window'"]
    at = [text.index(s) for s in steps]
    assert at == sorted(at), at
    assert text.index("trap {") < text.index("Set-NsHomeLocation")      # an error of the first step too
    common = (LAUNCHERS / "_common.ps1").read_text(encoding="ascii")
    for needle in ("Write-NsLaunchLog 'gate'", "Write-NsLaunchLog 'engine' ('process started", "-PassThru",
                   "Write-NsLaunchLog 'stopped'", "Write-NsLaunchLog 'page'"):
        assert needle in common, needle
