"""C1: installer/uninstall.ps1 against a temporary home folder (system integration skipped)."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UNINSTALL = ROOT / "installer" / "uninstall.ps1"
PS = shutil.which("powershell") or shutil.which("pwsh")
pytestmark = pytest.mark.skipif(PS is None, reason="PowerShell not available")

ALL_DIRS = ["venvs", "bin", "models", "outbox", "rejected", "data", "imports", "cache", "config", "logs"]


def _make_home(tmp_path: Path) -> Path:
    home = tmp_path / "ns-home"
    for d in ALL_DIRS:
        (home / d).mkdir(parents=True)
        (home / d / "keep.txt").write_text(d, encoding="utf-8")
    return home


def _run(home: Path, *extra: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "NIGHTSHIFT_HOME"}
    env["NS_NONINTERACTIVE"] = "1"
    cmd = [str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(UNINSTALL), "-HomeDir", str(home)]
    cmd.extend(extra)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)


def _present(home: Path) -> set[str]:
    return {d for d in ALL_DIRS if (home / d).exists()}


def test_dry_run_deletes_nothing_and_lists_actions(tmp_path: Path):
    home = _make_home(tmp_path)
    proc = _run(home, "-DryRun", "-DeleteModels", "-DeleteData")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _present(home) == set(ALL_DIRS)
    assert "[dry-run] delete folder" in proc.stdout and "venvs" in proc.stdout
    assert "[dry-run] remove user environment variable NIGHTSHIFT_HOME" in proc.stdout
    assert "models" in proc.stdout and "outbox" in proc.stdout


def test_default_removes_venvs_and_bin_only(tmp_path: Path):
    home = _make_home(tmp_path)
    proc = _run(home, "-SkipSystem")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _present(home) == set(ALL_DIRS) - {"venvs", "bin"}
    assert "Kept in" in proc.stdout and "models" in proc.stdout


def test_second_run_is_idempotent(tmp_path: Path):
    home = _make_home(tmp_path)
    assert _run(home, "-SkipSystem").returncode == 0
    again = _run(home, "-SkipSystem")
    assert again.returncode == 0, again.stdout + again.stderr
    assert _present(home) == set(ALL_DIRS) - {"venvs", "bin"}


def test_delete_models_only(tmp_path: Path):
    home = _make_home(tmp_path)
    assert _run(home, "-SkipSystem", "-DeleteModels").returncode == 0
    assert _present(home) == set(ALL_DIRS) - {"venvs", "bin", "models"}


def test_delete_data_only(tmp_path: Path):
    home = _make_home(tmp_path)
    assert _run(home, "-SkipSystem", "-DeleteData").returncode == 0
    gone = {"venvs", "bin", "outbox", "rejected", "data", "imports", "cache"}
    assert _present(home) == set(ALL_DIRS) - gone


def test_missing_home_is_not_an_error(tmp_path: Path):
    proc = _run(tmp_path / "does-not-exist", "-SkipSystem")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Nothing was kept" in proc.stdout
