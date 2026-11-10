# ruff: noqa: E501
"""D-40: INSTALL.cmd's closing `pause` is for a person; a run that said nobody is there does not wait for a key.

The real `INSTALL.cmd` runs under `cmd` in a scratch folder beside a stage 1 that does nothing (`exit`, with the
code the test asks for), with a stdin that stays open and silent: a console that nobody is watching. A run that
waits is still alive after the grace period (and is stopped by the test); a run that does not wait has exited
with stage 1's code.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GRACE_S = 2.5       # after stage 1 has ended: a `pause` is still there, a run that does not wait has long exited
LIMIT_S = 60.0      # the most a slow PC may need to start cmd and PowerShell


def scratch(tmp_path: Path, code: int = 0) -> Path:
    shutil.copyfile(ROOT / "INSTALL.cmd", tmp_path / "INSTALL.cmd")
    (tmp_path / "installer").mkdir()
    (tmp_path / "installer" / "stage1.ps1").write_text(
        f"Set-Content -LiteralPath (Join-Path $PSScriptRoot 'ended.txt') -Value 'x'\r\nexit {code}\r\n",
        encoding="ascii", newline="",
    )
    return tmp_path / "INSTALL.cmd"


def wait_for_a_key(cmd: Path, args: tuple[str, ...], env: dict[str, str] | None = None) -> tuple[bool, int | None]:
    """(whether INSTALL.cmd was still waiting after the grace period, its exit code when it was not)."""
    full = {k: v for k, v in os.environ.items() if k != "NS_NONINTERACTIVE"}
    full.update(env or {})
    proc = subprocess.Popen(
        ["cmd", "/c", str(cmd), *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env=full, cwd=str(cmd.parent),
    )
    ended = cmd.parent / "installer" / "ended.txt"
    try:
        deadline = time.monotonic() + LIMIT_S
        while not ended.exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ended.exists(), "the stand-in for stage 1 did not run"
        try:
            proc.wait(timeout=GRACE_S)
        except subprocess.TimeoutExpired:
            return True, None
        return False, proc.returncode
    finally:
        if proc.poll() is None:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False)
        proc.wait(timeout=LIMIT_S)
        for stream in (proc.stdin, proc.stdout):
            if stream:
                stream.close()


@pytest.mark.parametrize("args,env", [
    ((), None),
    (("--dev",), None),
    (("-Answers", "a.json", "-Ui"), None),
], ids=["double-click", "dev", "answers-and-the-window"])
def test_a_person_at_the_console_still_reads_the_message_before_the_window_closes(tmp_path: Path, args, env):
    waiting, _ = wait_for_a_key(scratch(tmp_path), args, env)
    assert waiting, "INSTALL.cmd must keep its pause for a person"


@pytest.mark.parametrize("args,env", [
    (("-NoUi",), None),
    (("-nOuI", "-Dev"), None),
    (("-Answers", "my answers.json"), None),
    (("-Answers", "a.json", "-NoUi"), None),
    ((), {"NS_NONINTERACTIVE": "1"}),
], ids=["NoUi", "NoUi-any-case", "answers-with-a-space-in-the-path", "answers-and-NoUi", "NS_NONINTERACTIVE"])
def test_a_run_that_says_nobody_is_there_does_not_wait_for_a_key(tmp_path: Path, args, env):
    waiting, code = wait_for_a_key(scratch(tmp_path), args, env)
    assert not waiting, "INSTALL.cmd waits for a key in a run with nobody at the keyboard"
    assert code == 0


def test_the_exit_code_of_stage_one_is_the_exit_code_of_install_cmd(tmp_path: Path):
    for code in (0, 3, 75):
        folder = tmp_path / f"c{code}"
        folder.mkdir()
        waiting, got = wait_for_a_key(scratch(folder, code), ("-NoUi",))
        assert not waiting and got == code
