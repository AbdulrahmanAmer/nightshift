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

ALL_DIRS = ["venvs", "bin", "run", "models", "outbox", "rejected", "data", "imports", "cache"]
ALL_DIRS += ["config", "logs", "stories", "state", "tmp"]


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
    assert _present(home) == set(ALL_DIRS) - {"venvs", "bin", "run", "state", "tmp"}
    assert "Kept in" in proc.stdout and "models" in proc.stdout and "stories" in proc.stdout


def test_second_run_is_idempotent(tmp_path: Path):
    home = _make_home(tmp_path)
    assert _run(home, "-SkipSystem").returncode == 0
    again = _run(home, "-SkipSystem")
    assert again.returncode == 0, again.stdout + again.stderr
    assert _present(home) == set(ALL_DIRS) - {"venvs", "bin", "run", "state", "tmp"}


def test_delete_models_only(tmp_path: Path):
    home = _make_home(tmp_path)
    assert _run(home, "-SkipSystem", "-DeleteModels").returncode == 0
    assert _present(home) == set(ALL_DIRS) - {"venvs", "bin", "run", "models", "state", "tmp"}


def test_delete_data_only(tmp_path: Path):
    home = _make_home(tmp_path)
    assert _run(home, "-SkipSystem", "-DeleteData").returncode == 0
    gone = {"venvs", "bin", "run", "outbox", "rejected", "data", "imports", "cache"}
    gone |= {"stories", "state", "tmp"}
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
    """The setup window reads run/apphost-setup.json when the script ends; the deferred script removes run."""
    home = _make_home(tmp_path)
    updater = _with_updater_copy(home)
    proc = _run(home, "-SkipSystem", "-KeepBin")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not updater.exists()                                   # the 150-300 MB are gone at once
    assert (home / "run" / "apphost-setup.json").is_file() and (home / "bin").exists()
    assert "bin and run are removed when the uninstall window closes" in proc.stdout


def _with_program_folders(home: Path) -> list[Path]:
    folders = [home / "app", home / "app.new", home / "app.prev"]
    for f in folders:
        (f / "installer").mkdir(parents=True)
        (f / "VERSION").write_text("0.1.5", encoding="utf-8")
    return folders


def test_the_console_uninstall_removes_the_program_folder_and_the_update_swap_folders(tmp_path: Path):
    home = _make_home(tmp_path)
    folders = _with_program_folders(home)
    proc = _run(home, "-SkipSystem")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not any(f.exists() for f in folders)
    assert _present(home) == set(ALL_DIRS) - {"venvs", "bin", "run", "state", "tmp"}


def test_the_dry_run_lists_the_program_folders(tmp_path: Path):
    home = _make_home(tmp_path)
    folders = _with_program_folders(home)
    proc = _run(home, "-SkipSystem", "-DryRun")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert all(f.exists() for f in folders)
    assert all(f"[dry-run] delete folder {f}" in proc.stdout for f in folders)


def test_with_keepbin_the_program_folder_stays_for_the_windows_deferred_cleanup(tmp_path: Path):
    # the setup window runs from <home>\app, so it can only be removed after the window has gone
    home = _make_home(tmp_path)
    folders = _with_program_folders(home)
    proc = _run(home, "-SkipSystem", "-KeepBin")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert all(f.exists() for f in folders)


def _path_probe(tmp_path: Path, home: Path, start: str, dry: bool = False) -> list[str]:
    """Run Remove-UserPathEntry (cut out of uninstall.ps1) against a throw-away key below HKCU."""
    probe = tmp_path / "path-probe.ps1"
    ffmpeg = home / "bin" / "ffmpeg"
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "Set-StrictMode -Version Latest",
        f"$script:IsDry = ${'true' if dry else 'false'}",
        "function Show-Action { param([string]$Text) Write-Host \"[dry-run] $Text\" }",
        f"$text = Get-Content -LiteralPath '{UNINSTALL}' -Raw",
        "$start = $text.IndexOf('function Remove-UserPathEntry')",
        "$end = $text.IndexOf('# ---- where is the install')",
        "Invoke-Expression $text.Substring($start, $end - $start)",
        r"$sub = 'Software\NightshiftTest\' + [guid]::NewGuid().ToString('N')",
        "$null = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($sub)",
        "try {",
        "    $k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($sub, $true)",
        f"    $k.SetValue('Path', '{start}', [Microsoft.Win32.RegistryValueKind]::ExpandString)",
        "    $k.Close()",
        f"    $r = Remove-UserPathEntry -Directory '{ffmpeg}' -SubKey $sub",
        "    $k = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($sub)",
        "    $opt = [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames",
        "    'result=' + $r",
        "    'path=' + $k.GetValue('Path', $null, $opt)",
        "    'kind=' + $k.GetValueKind('Path')",
        "    $k.Close()",
        "} finally {",
        "    [Microsoft.Win32.Registry]::CurrentUser.DeleteSubKeyTree($sub, $false)",
        "}",
    ]
    probe.write_text("\n".join(lines) + "\n", encoding="utf-8")
    proc = subprocess.run([str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(probe)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout.splitlines()


def test_uninstall_takes_the_ffmpeg_fallback_out_of_the_user_path(tmp_path: Path):
    home = tmp_path / "ns-home"
    ffmpeg = home / "bin" / "ffmpeg"
    start = rf"C:\Tools;%SystemRoot%\system32;{ffmpeg};C:\Other"
    out = _path_probe(tmp_path, home, start)
    assert "result=True" in out
    assert r"path=C:\Tools;%SystemRoot%\system32;C:\Other" in out     # %SystemRoot% is not expanded
    assert "kind=ExpandString" in out                                  # and the registry kind is kept


def test_uninstall_leaves_a_user_path_alone_that_has_no_such_entry(tmp_path: Path):
    home = tmp_path / "ns-home"
    start = rf"C:\Tools;{home}\bin\ffmpeg-other;C:\Other"
    out = _path_probe(tmp_path, home, start)
    assert "result=False" in out and f"path={start}" in out


def test_the_entry_matches_without_regard_to_case_or_a_trailing_backslash(tmp_path: Path):
    home = tmp_path / "ns-home"
    entry = str(home / "bin" / "ffmpeg").upper() + chr(92)
    out = _path_probe(tmp_path, home, rf"C:\Tools;{entry}")
    assert "result=True" in out and r"path=C:\Tools" in out


def test_the_dry_run_names_the_path_entry_and_changes_nothing(tmp_path: Path):
    home = tmp_path / "ns-home"
    start = rf"C:\Tools;{home}\bin\ffmpeg"
    out = _path_probe(tmp_path, home, start, dry=True)
    assert rf"[dry-run] remove {home}\bin\ffmpeg from the user PATH" in out
    assert f"path={start}" in out


def test_delete_data_also_removes_stories_and_the_internal_folders(tmp_path: Path):
    """The first dev-mode uninstall left stories, state and tmp under the install folder."""
    home = _make_home(tmp_path)
    proc = _run(home, "-SkipSystem", "-DeleteData")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _present(home) == {"models", "config", "logs"}
