"""The window that follows an update shows a short result, not the machine-readable line with the whole
release notes; the notes keep their own text (no `ns` in a changelog sentence becomes a path)."""

import json
import subprocess
from pathlib import Path

import pytest

from ns_supervisor import pwsh, update_job

ROOT = Path(__file__).resolve().parents[1]
needs_powershell = pytest.mark.skipif(pwsh.find_powershell() is None, reason="powershell not on PATH")

NOTES = "Changed: `ns verify` now names the check that failed (see `ns doctor`). " * 300
RESULT = json.dumps({"updated": True, "old": "0.1.15-rc5", "new": "0.1.15-rc8", "release_notes": NOTES})
SENTENCE = "Now on 0.1.15-rc8 (was 0.1.15-rc5)."


def make_run(tmp_path: Path) -> tuple[Path, Path, Path]:
    home = tmp_path / "home"
    (home / "cmd").mkdir(parents=True)
    (home / "cmd" / "ns.cmd").write_text("@echo off\r\n", encoding="ascii")
    log = tmp_path / "update-20261112.log"
    log.write_text(
        f"Downloading Nightshift-0.1.15-rc8.zip ...\n{SENTENCE}\n  voice warning: run ns doctor\n{RESULT}\n",
        encoding="utf-8",
    )
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"state": "done", "log": str(log), "pid": 1, "message": SENTENCE}),
                      encoding="utf-8")
    return home, log, status


def test_the_result_line_is_recognised_and_a_sentence_is_not() -> None:
    assert update_job.is_result_json(RESULT)
    assert update_job.is_result_json("  " + RESULT + "  ")
    assert not update_job.is_result_json(SENTENCE)
    assert not update_job.is_result_json("{braces in a sentence} are not the result")
    assert not update_job.is_result_json("")


def test_the_python_follower_echoes_every_line_but_the_result_line(tmp_path: Path) -> None:
    _, log, _ = make_run(tmp_path)
    echoed: list[str] = []
    update_job._echo_new(log, 0, echoed.append)
    assert echoed == ["Downloading Nightshift-0.1.15-rc8.zip ...", SENTENCE, "  voice warning: run ns doctor"]


@needs_powershell
def test_the_progress_window_shows_a_short_result_with_the_notes_untouched(tmp_path: Path) -> None:
    home, log, status = make_run(tmp_path)
    script = update_job.follower_script(status, log, 1, wait=False, home=home)
    assert script.isascii()
    f = tmp_path / "follow.ps1"
    f.write_text("﻿" + script, encoding="utf-8")       # a BOM: PowerShell 5.1 reads a file as UTF-8 with it
    exe = pwsh.find_powershell()
    assert exe is not None
    r = subprocess.run([exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(f)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(r.stdout) < 1000, len(r.stdout)                         # the notes alone are 20 KB
    assert "release_notes" not in r.stdout and "Changed:" not in r.stdout
    assert "Result: " + SENTENCE in r.stdout
    assert f"run {home / 'cmd' / 'ns.cmd'} doctor" in r.stdout    # a progress line names commands in full


def test_the_update_shortcut_skips_the_result_line_and_ends_with_a_result_sentence() -> None:
    text = (ROOT / "launchers" / "update.ps1").read_text(encoding="ascii")
    show = text[text.index("function Show-UpdateLine") :][:700]
    assert "StartsWith('{\"')" in show and "EndsWith('}')" in show and "return" in show
    assert show.index("StartsWith") < show.index("Format-NsCommands")
    assert text.count("Show-UpdateLine ([string]$lines[$i])") == 2          # the loop and the last lines
    assert text.index("Result: ") > text.index("$final.state -eq 'failed'")
    assert text.index("Result: ") < text.index("Update finished. Press Enter to close")
    assert "-Encoding UTF8" in text                                       # the log is UTF-8, without a BOM
