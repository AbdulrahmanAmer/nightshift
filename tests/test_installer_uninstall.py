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

ALL_DIRS = ["venvs", "bin", "run", "models", "outbox", "rejected", "data", "imports", "cache", "config", "logs"]


def _make_home(tmp_path: Path) -> Path:
    home = tmp_path / "ns-home"
    for d in ALL_DIRS:
        (home / d).mkdir(parents=True)
        (home / d / "keep.txt").write_text(d, encoding="utf-8")
    return home


def _run(home: Path, *extra: str) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "NIGHTSHIFT_HOME"}
    env["NS_NONINTERACTIVE"] = "1"
    cmd = [str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(UNINSTALL)]
    cmd += ["-HomeDir", str(home)]
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


def test_default_removes_venvs_bin_and_run_only(tmp_path: Path):
    home = _make_home(tmp_path)
    proc = _run(home, "-SkipSystem")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _present(home) == set(ALL_DIRS) - {"venvs", "bin", "run"}
    assert "Kept in" in proc.stdout and "models" in proc.stdout


def test_second_run_is_idempotent(tmp_path: Path):
    home = _make_home(tmp_path)
    assert _run(home, "-SkipSystem").returncode == 0
    again = _run(home, "-SkipSystem")
    assert again.returncode == 0, again.stdout + again.stderr
    assert _present(home) == set(ALL_DIRS) - {"venvs", "bin", "run"}


def test_delete_models_only(tmp_path: Path):
    home = _make_home(tmp_path)
    assert _run(home, "-SkipSystem", "-DeleteModels").returncode == 0
    assert _present(home) == set(ALL_DIRS) - {"venvs", "bin", "run", "models"}


def test_delete_data_only(tmp_path: Path):
    home = _make_home(tmp_path)
    assert _run(home, "-SkipSystem", "-DeleteData").returncode == 0
    gone = {"venvs", "bin", "run", "outbox", "rejected", "data", "imports", "cache"}
    assert _present(home) == set(ALL_DIRS) - gone


def test_missing_home_is_not_an_error(tmp_path: Path):
    proc = _run(tmp_path / "does-not-exist", "-SkipSystem")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Nothing was kept" in proc.stdout


def _with_updater_copy(home: Path) -> Path:
    copy = home / "run" / "updater" / "venv-20261103-030000" / "Scripts"
    copy.mkdir(parents=True)
    (copy / "python.exe").write_bytes(b"x" * 1000)
    (home / "run" / "apphost-setup.json").write_text('{"pid": 1}', encoding="utf-8")
    return home / "run" / "updater"


def test_uninstall_removes_the_updaters_private_venv_copy_and_says_so(tmp_path: Path):
    home = _make_home(tmp_path)
    updater = _with_updater_copy(home)
    proc = _run(home, "-SkipSystem")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not updater.exists() and not (home / "run").exists()
    assert "removing" in proc.stdout and str(home / "run" / "updater") in proc.stdout  # the report lists it
    assert str(home / "run") in proc.stdout


def test_the_dry_run_lists_the_run_folder(tmp_path: Path):
    home = _make_home(tmp_path)
    updater = _with_updater_copy(home)
    proc = _run(home, "-SkipSystem", "-DryRun")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert updater.exists()
    assert f"[dry-run] delete folder {updater}" in proc.stdout
    assert f"[dry-run] delete folder {home / 'run'}" in proc.stdout


def test_with_keepbin_the_copy_goes_now_and_the_pid_files_wait_for_the_window(tmp_path: Path):
    """The setup window reads run\apphost-setup.json after the script ends; the deferred script removes run."""
    home = _make_home(tmp_path)
    updater = _with_updater_copy(home)
    proc = _run(home, "-SkipSystem", "-KeepBin")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not updater.exists()                                   # the 150-300 MB are gone at once
    assert (home / "run" / "apphost-setup.json").is_file() and (home / "bin").exists()
    assert "bin and run are removed when the uninstall window closes" in proc.stdout
