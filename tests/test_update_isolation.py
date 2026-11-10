"""C2: nothing in `venvs\\supervisor` is held open while the update re-installs into it (probable P-A).

`Nightshift - Update` ran `venvs\\supervisor\\Scripts\\ns.exe update <zip>` and kept it alive to follow the
worker, and the worker itself ran with that venv's `python.exe`. uv then had to remove and rewrite
`Scripts\\ns.exe` (and any dependency a release bumps) while programs from the same folder were running; on
Windows that is "Access is denied", which ended in NS-E702 and a rollback whose own re-sync hit the same lock.

Fixes under test: the worker runs from a private copy of the venv (`updater_copy`), the shortcut detaches and
follows the state file from PowerShell, running entry-point launchers are renamed out of uv's way, and
`ns verify --quick` uses the freshly synced venv, not the worker's snapshot.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from ns_common.paths import Paths
from ns_supervisor import update, update_job, updater_copy

LAUNCHERS = Path(__file__).resolve().parents[1] / "launchers"
win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


def make_venv(paths: Paths, name: str = "supervisor") -> Path:
    """A stand-in for an installed venv: interpreter, launchers, a package and a pyvenv.cfg."""
    venv = paths.venvs / name
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Lib" / "site-packages" / "pkg").mkdir(parents=True)
    for exe in ("python.exe", "ns.exe", "studio.exe"):
        (venv / "Scripts" / exe).write_bytes(b"MZ " + exe.encode())
    (venv / "Lib" / "site-packages" / "pkg" / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    (venv / "pyvenv.cfg").write_text("home = C:\\base\\python\nversion = 3.12.7\n", encoding="utf-8")
    return venv


# ------------------------------------------------------------------------------------- the venv copy
def test_the_worker_python_is_a_copy_outside_venvs(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    make_venv(paths)
    py = updater_copy.stage_worker_python(paths, stamp="20261102-120000")
    assert py is not None and py.is_file()
    assert updater_copy.is_inside(py, updater_copy.updater_root(paths))
    assert not updater_copy.is_inside(py, paths.venvs)
    copy = py.parent.parent
    assert (copy / "pyvenv.cfg").is_file()
    assert (copy / "Lib" / "site-packages" / "pkg" / "__init__.py").is_file()


def test_older_copies_are_removed_and_a_missing_venv_means_no_copy(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    assert updater_copy.stage_worker_python(paths) is None                    # no venv yet
    make_venv(paths)
    first = updater_copy.stage_worker_python(paths, stamp="a")
    second = updater_copy.stage_worker_python(paths, stamp="b")
    assert first is not None and second is not None
    assert not first.exists() and second.exists()


def test_a_failing_copy_falls_back_to_no_copy_and_leaves_nothing_behind(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    make_venv(paths)

    def disk_full(src: Any, dst: Any, **kw: Any) -> None:
        Path(dst).mkdir(parents=True)
        raise OSError(28, "No space left on device")

    assert updater_copy.stage_worker_python(paths, stamp="x", copytree=disk_full) is None
    assert not list(updater_copy.updater_root(paths).glob("venv-*"))


def test_launch_starts_the_worker_with_the_copy_not_the_venv_interpreter(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    make_venv(paths)
    seen: list[list[str]] = []
    update_job.launch(paths, ["--github"], wait_pid=7, spawn=lambda argv, cwd: seen.append(argv) or 1)
    exe = Path(seen[0][0])
    assert updater_copy.is_inside(exe, updater_copy.updater_root(paths)) and exe.is_file()
    assert seen[0][1:4] == ["-m", "ns_supervisor.cli", "update"]
    # without a venv (a developer checkout) the caller's own interpreter is used, as before
    bare = Paths(tmp_path / "bare")
    bare.home.mkdir()
    update_job.launch(bare, ["--github"], wait_pid=None, spawn=lambda argv, cwd: seen.append(argv) or 1)
    assert seen[1][0] == sys.executable


# --------------------------------------------------------- no handle on the old venv at the swap window
class World:
    """The processes alive on a fake PC, by the executable each one has open."""

    def __init__(self) -> None:
        self.live: dict[int, str] = {}

    def start(self, pid: int, exe: str) -> None:
        self.live[pid] = exe

    def end(self, pid: int) -> None:
        self.live.pop(pid, None)

    def holders(self, folder: Path) -> list[str]:
        return [e for e in self.live.values() if updater_copy.is_inside(e, folder)]


def run_update(tmp_path: Path, stage: Any) -> tuple[World, list[list[str]], list[str]]:
    """The caller (a venv python) starts the worker and exits; then the worker syncs."""
    paths = Paths(tmp_path / "home")
    make_venv(paths)
    app = tmp_path / "app"
    app.mkdir()
    world = World()
    caller_py = str(paths.venvs / "supervisor" / "Scripts" / "python.exe")
    world.start(100, caller_py)
    spawned: list[list[str]] = []

    def spawn(argv: list[str], cwd: Path) -> int:
        spawned.append(argv)
        world.start(200, argv[0])
        return 200

    update_job.launch(paths, ["--github"], wait_pid=100, spawn=spawn, stage=stage)
    held_at_sync: list[str] = []

    def uv(cmd: list[str]) -> tuple[int, str]:
        held_at_sync.extend(world.holders(paths.venvs))      # what uv would collide with
        return 0, ""

    def body() -> update_job.Outcome:
        update.resync_venvs(app, paths, uv="uv", run=uv)
        return update_job.Outcome("Now on 0.2.0 (was 0.1.0).", "0.1.0", "0.2.0")

    update_job.run_worker(paths, body, wait_pid=100, wait=world.end)     # the caller exits, then the update
    return world, spawned, held_at_sync


def test_no_program_from_the_old_venv_is_running_while_uv_rewrites_it(tmp_path: Path) -> None:
    _world, spawned, held = run_update(tmp_path, stage=None)
    assert held == [], "a process still has a file open in venvs: uv could not replace it"
    assert spawned and "venvs" not in Path(spawned[0][0]).parts


def test_control_the_old_worker_that_used_the_venv_interpreter_is_caught(tmp_path: Path) -> None:
    def old_behaviour(paths: Paths) -> Path:
        return paths.venvs / "supervisor" / "Scripts" / "python.exe"

    _world, _spawned, held = run_update(tmp_path, stage=old_behaviour)
    assert held and held[0].endswith("python.exe")


def test_no_program_of_ours_runs_from_a_venv_while_uv_rewrites_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-39: the same world with the CALLER of a terminal `ns update` in it. The terminal's command runs from
    `venvs/supervisor` (and holds `Scripts/ns.exe` open for as long as it lives), so it has to end before
    the worker syncs: the worker waits for it, and nothing in the terminal waits for the worker."""
    from typer.testing import CliRunner

    from ns_supervisor import cli

    paths = Paths(tmp_path / "home")
    make_venv(paths)
    app = tmp_path / "app"
    app.mkdir()
    world = World()
    world.start(100, str(paths.venvs / "supervisor" / "Scripts" / "ns.exe"))     # the terminal's command
    spawned: list[list[str]] = []
    stayed: list[int] = []

    def spawn(argv: list[str], cwd: Path) -> int:
        spawned.append(argv)
        world.start(200, argv[0])
        return 200

    def window(argv: list[str], cwd: Path) -> int:
        world.start(300, argv[0])                       # the progress window is a process too
        return 300

    monkeypatch.setattr(update_job, "spawn_detached", spawn)
    monkeypatch.setattr(update_job, "open_window", window, raising=False)
    monkeypatch.setattr(update_job, "follow", lambda *a, **k: stayed.append(100) or 0, raising=False)
    r = CliRunner().invoke(cli.app, ["update", "--github"], env={"NIGHTSHIFT_HOME": str(paths.home)})
    assert r.exit_code == 0 and len(spawned) == 1, r.output
    if not stayed:
        world.end(100)                                  # the command returned to the prompt
    held_at_sync: list[str] = []

    def uv(cmd: list[str]) -> tuple[int, str]:
        held_at_sync.extend(world.holders(paths.venvs))
        return 0, ""

    def body() -> update_job.Outcome:
        update.resync_venvs(app, paths, uv="uv", run=uv)
        return update_job.Outcome("Now on 0.2.0 (was 0.1.0).", "0.1.0", "0.2.0")

    told = "--wait-pid" in spawned[0]
    update_job.run_worker(paths, body, wait_pid=100 if told else None, wait=lambda pid: None)
    assert held_at_sync == [], "the terminal's own command was still running from the venv during the sync"
    assert told and "venvs" not in Path(spawned[0][0]).parts
    assert all("venvs" not in Path(exe).parts for exe in world.live.values())


# --------------------------------------------------------------- the shortcut no longer holds ns.exe
def test_the_update_shortcut_detaches_and_follows_the_state_file_without_ns_exe() -> None:
    text = (LAUNCHERS / "update.ps1").read_text(encoding="ascii")
    code = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    body = "\n".join(code)
    assert "ns.exe" not in body and "& $ns " not in body
    assert "-m ns_supervisor.cli update $dlg.FileName --detach" in body
    assert body.index("Set-NsHomeLocation") < body.index("--detach")
    assert "update_state" not in body and "$info.status" in body        # the path comes from the CLI's answer
    assert "'done'" in body and "'failed'" in body and "Get-Process -Id $workerPid" in body


# ------------------------------------------------------------------------ running launchers can be renamed
def test_entry_points_are_moved_aside_for_uv_and_restored_when_the_install_fails(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    venv = make_venv(paths)
    app = tmp_path / "app"
    app.mkdir()
    seen: list[bool] = []

    def failing_uv(cmd: list[str]) -> tuple[int, str]:
        seen.append(not (venv / "Scripts" / "ns.exe").exists())          # uv finds the way free
        return 1, "boom"

    with pytest.raises(update.NsError):
        update.resync_venvs(app, paths, uv="uv", run=failing_uv)
    assert seen == [True]
    assert (venv / "Scripts" / "ns.exe").read_bytes() == b"MZ ns.exe"          # put back: not left without ns
    assert not list((venv / "Scripts").glob("*.old*"))


def test_a_successful_install_leaves_no_old_launchers(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    venv = make_venv(paths)
    app = tmp_path / "app"
    app.mkdir()

    def uv(cmd: list[str]) -> tuple[int, str]:
        (venv / "Scripts" / "ns.exe").write_bytes(b"MZ new ns")          # what uv does
        return 0, ""

    assert update.resync_venvs(app, paths, uv="uv", run=uv) == ["supervisor"]
    assert (venv / "Scripts" / "ns.exe").read_bytes() == b"MZ new ns"
    # uv did not write studio.exe again (nothing changed for it): it must not be lost
    assert (venv / "Scripts" / "studio.exe").read_bytes() == b"MZ studio.exe"
    assert not list((venv / "Scripts").glob("*.old*"))


def test_a_reinstall_that_changes_nothing_does_not_leave_the_venv_without_ns_exe(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    venv = make_venv(paths)
    app = tmp_path / "app"
    app.mkdir()
    update.resync_venvs(app, paths, uv="uv", run=lambda cmd: (0, "Audited 1 package"))
    for leaf in ("ns.exe", "studio.exe", "python.exe"):
        assert (venv / "Scripts" / leaf).is_file()
    assert not list((venv / "Scripts").glob("*.old*"))


@win_only
def test_a_running_launcher_cannot_be_deleted_but_can_be_renamed_out_of_the_way(tmp_path: Path) -> None:
    """The real Windows behaviour behind P-A, with a real running exe."""
    paths = Paths(tmp_path / "home")
    venv = make_venv(paths)
    ping = shutil.which("ping")
    if ping is None:
        pytest.skip("needs ping.exe")
    ns_exe = venv / "Scripts" / "ns.exe"
    shutil.copyfile(ping, ns_exe)
    proc = subprocess.Popen([str(ns_exe), "-n", "60", "127.0.0.1"], stdout=subprocess.DEVNULL)
    try:
        time.sleep(0.5)
        with pytest.raises(OSError):
            ns_exe.unlink()                                          # control: what uv runs into
        moved = update.free_entry_points(venv)
        assert ns_exe in [m[0] for m in moved] and not ns_exe.exists()
        ns_exe.write_bytes(b"MZ new ns")                             # uv can now write the new launcher
        update.finish_entry_points(moved)                            # the running one stays until it ends
        assert any((venv / "Scripts").glob("ns.exe.old*"))
    finally:
        proc.kill()
        proc.wait()
    # the next update clears the leftover
    ns_exe.write_bytes(b"MZ again")
    update.free_entry_points(venv)
    update.discard_entry_points([(ns_exe, venv / "Scripts" / "ns.exe.old")])
    assert not (venv / "Scripts" / "ns.exe.old").exists()


# --------------------------------------------------------------------------------- verify uses the new venv
def test_verify_quick_uses_the_given_interpreter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kw: Any) -> Any:
        calls.append(argv)

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(update.subprocess, "run", fake_run)
    assert update.verify_quick(tmp_path, "C:/home/venvs/supervisor/Scripts/python.exe") is True
    assert update.verify_quick(tmp_path) is True
    assert calls[0][0].endswith("supervisor/Scripts/python.exe") and calls[1][0] == sys.executable
    assert calls[0][1:] == ["-m", "ns_supervisor.cli", "verify", "--quick"]
