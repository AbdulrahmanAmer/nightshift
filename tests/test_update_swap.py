"""C1/C2: nothing may hold the app folder as its current directory while `ns update` swaps it.

The regression: `Set-Location` alone does not move the powershell.exe process out of the folder the shortcut
started it in, so `app.rename(prev)` failed with WinError 32 on every Update-shortcut run. These tests start a
real powershell.exe with the app folder as its working directory.
"""

import base64
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from installer.stage2.simulate import FakeRunner
from installer.stage2.steps.s09_shortcuts_scheduler import S09ShortcutsScheduler
from installer.stage2.testing import example_answers, make_test_context
from ns_common.errors import NsError
from ns_supervisor import update

LAUNCHERS = Path(__file__).resolve().parents[1] / "launchers"
PS = shutil.which("powershell")
real_ps = pytest.mark.skipif(sys.platform != "win32" or PS is None, reason="needs Windows PowerShell")


def _start(app: Path, home: Path, body: str) -> subprocess.Popen[str]:
    script = app.parent / "probe.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f". '{LAUNCHERS / '_common.ps1'}'\n"
        f"{body}\n"
        "[Console]::Out.WriteLine('ready'); [Console]::Out.Flush()\n"
        "[void][Console]::In.ReadLine()\n",
        encoding="utf-8",
    )
    env = {**os.environ, "NIGHTSHIFT_HOME": str(home)}
    return subprocess.Popen(
        [str(PS), "-NoProfile", "-File", str(script)], cwd=str(app), env=env, stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def _wait_ready(proc: subprocess.Popen[str]) -> None:
    assert proc.stdout is not None
    line = proc.stdout.readline()
    assert line.strip() == "ready", line + (proc.stderr.read() if proc.stderr else "")


def _finish(proc: subprocess.Popen[str]) -> None:
    assert proc.stdin is not None
    proc.stdin.write("\n")
    proc.stdin.flush()
    proc.wait(timeout=30)


@real_ps
def test_set_location_alone_leaves_the_folder_locked_but_the_helper_frees_it(tmp_path: Path) -> None:
    app, home = tmp_path / "app", tmp_path / "home"
    app.mkdir()
    home.mkdir()
    # control: what update.ps1 used to do
    old = _start(app, home, "Set-Location -LiteralPath (Get-NsHome)")
    try:
        _wait_ready(old)
        with pytest.raises(OSError):
            app.rename(tmp_path / "app.prev")
    finally:
        _finish(old)
    # the fix: Set-NsHomeLocation also moves the Win32 current directory
    new = _start(app, home, "Set-NsHomeLocation")
    try:
        _wait_ready(new)
        app.rename(tmp_path / "app.prev")
    finally:
        _finish(new)
    assert (tmp_path / "app.prev").is_dir()


def test_launchers_that_stay_open_step_out_of_the_app_folder() -> None:
    for name in ("update.ps1", "nightshift-dashboard.ps1", "nightshift-studio.ps1"):
        assert "Set-NsHomeLocation" in (LAUNCHERS / name).read_text(encoding="ascii"), name


def _decoded_scripts(runner: FakeRunner) -> list[str]:
    out = []
    for call in runner.calls:
        if call.argv and call.argv[0] == "powershell.exe" and "-EncodedCommand" in call.argv:
            encoded = call.argv[call.argv.index("-EncodedCommand") + 1]
            out.append(base64.b64decode(encoded).decode("utf-16-le"))
    return out


def test_every_shortcut_starts_in_the_install_home_not_the_app_folder(tmp_path: Path) -> None:
    app = tmp_path / "app"
    (app / "launchers").mkdir(parents=True)
    for n in ("nightshift-dashboard.ps1", "update.ps1", "open-outbox.ps1"):
        (app / "launchers" / n).write_text("# launcher", encoding="utf-8")
    runner = FakeRunner(tmp_path / "desktop")
    ctx = make_test_context(
        tmp_path, raw_answers=example_answers(), resolve=True, app_path=app, runner=runner
    )
    S09ShortcutsScheduler().run(ctx)
    shortcut_scripts = [s for s in _decoded_scripts(runner) if "CreateShortcut" in s]
    assert len(shortcut_scripts) == 3
    for s in shortcut_scripts:
        assert f"$s.WorkingDirectory = '{ctx.paths.home}'" in s
        assert str(app) not in s.split("WorkingDirectory")[1].split("\n")[0]


def test_rename_retries_while_windows_still_reports_the_folder_in_use(tmp_path: Path) -> None:
    src, dst = tmp_path / "a", tmp_path / "b"
    src.mkdir()
    sleeps: list[float] = []
    fails = {"n": 2}
    real = Path.rename

    def flaky(self: Path, target: Path) -> Path:
        if self == src and fails["n"] > 0:
            fails["n"] -= 1
            raise PermissionError(13, "in use")
        return real(self, target)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "rename", flaky)
        update.rename_with_retry(src, dst, tries=5, pause_s=0.5, sleep=sleeps.append)
    assert dst.is_dir() and sleeps == [0.5, 0.5]


def test_update_that_cannot_swap_says_which_windows_to_close(tmp_path: Path) -> None:
    from ns_common.paths import Paths
    from tests.test_ns_update import Env, make_app, make_zip

    app, z = make_app(tmp_path), make_zip(tmp_path)
    real = Path.rename

    def locked(self: Path, target: Path) -> Path:
        if self == app:
            raise PermissionError(32, "being used by another process")
        return real(self, target)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Path, "rename", locked)
        mp.setattr(update, "RENAME_PAUSE_S", 0)
        mp.setattr(update, "RENAME_TRIES", 2)
        with pytest.raises(NsError) as ei:
            update.update_app(z, app, Env().env(), paths=Paths(tmp_path / "ns"))
    assert ei.value.code == "NS-E702" and "Close those windows" in ei.value.detail
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
