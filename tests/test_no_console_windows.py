# ruff: noqa: E501
"""A process without a console never opens a console window per child (review of launcher-reach @ 50dc16e).

Windows gives a console program started by a process that has no console a new console, with a window. The
engine has none (`client.spawn_supervisor` starts it detached), nor has the update worker: the first-start
reach alone starts about ten PowerShells. The rule lives in one place, `ns_supervisor.childenv.window_flags`:
CREATE_NO_WINDOW when this process has no console window, nothing when it has one (the child then shares
the console the owner is looking at). Every place in the product that starts a program passes it, or its
own flags, or is named below with the reason it always has a console.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from installer.stage2.runner import SubprocessRunner
from ns_supervisor import childenv, control, doctor, pwsh, update

ROOT = Path(__file__).resolve().parents[1]
PRODUCT = ("ns_common", "ns_supervisor", "ns_dashboard", "ns_studio", "ns_tts", "installer")
NO_WINDOW = 0x08000000
DETACHED = 0x00000008

#: Starts that pass no flags, with why no window can appear. Anything else must pass `creationflags`.
STUDIO = "the studio always has a console: the dashboard starts it with CREATE_NO_WINDOW, the task's cmd.exe and a terminal have one"
DASHBOARD = "the dashboard is started by the engine with CREATE_NO_WINDOW (procs.start), so it has a console its children share"
SETUP = "the setup server shares INSTALL.cmd's console (stage1.ps1 starts it with -NoNewWindow): setup's own console, which the owner should see"
SHARES_A_CONSOLE = {
    "ns_common/ffmpeg.py::_reads_filter_script_option": STUDIO,
    "ns_common/ffmpeg.py::probe": STUDIO,
    "ns_common/ffmpeg.py::run": STUDIO,
    "ns_studio/clipper/source.py::import_url": STUDIO,
    "ns_dashboard/updater.py::default_runner": DASHBOARD,
    "ns_dashboard/updater.py::stream_command": DASHBOARD,
    "installer/stage2/web/bootstrap.py::real_cmd": SETUP,
    "installer/stage2/web/controller.py::elevate_longpaths": SETUP,
    "installer/stage2/web/controller.py::copy_to_clipboard": SETUP,
    "installer/stage2/web/controller.py::_run_headless": SETUP + " (the console wizard itself: it must stay visible)",
    "installer/stage2/web/preflight.py::default_run": SETUP,
    "installer/stage2/web/uninstall.py::action": "explorer.exe is not a console program",
}


def starts() -> dict[str, bool]:
    """Every `subprocess.run/Popen/...` call of the product: `file::function` -> passes creationflags."""
    found: dict[str, bool] = {}
    for top in PRODUCT:
        for file in sorted((ROOT / top).rglob("*.py")):
            tree = ast.parse(file.read_text(encoding="utf-8"))
            parent: dict[ast.AST, ast.AST] = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                func = node.func
                if not (isinstance(func.value, ast.Name) and func.value.id == "subprocess"):
                    continue
                if func.attr not in ("run", "Popen", "call", "check_output", "check_call"):
                    continue
                at: ast.AST = node
                while at in parent and not isinstance(at, ast.FunctionDef | ast.AsyncFunctionDef):
                    at = parent[at]
                name = at.name if isinstance(at, ast.FunctionDef | ast.AsyncFunctionDef) else "<module>"
                key = f"{file.relative_to(ROOT).as_posix()}::{name}"
                flagged = any(k.arg == "creationflags" or k.arg is None for k in node.keywords)
                found[key] = found.get(key, True) and flagged
    return found


def test_every_place_that_starts_a_program_passes_window_flags_or_says_why_not():
    found = starts()
    assert len(found) > 30                                   # the walk sees the product
    bare = {key for key, flagged in found.items() if not flagged}
    assert bare == set(SHARES_A_CONSOLE), sorted(bare ^ set(SHARES_A_CONSOLE))
    # the two lowest shared points and the update worker's own starts use the one rule
    for file, count in (("ns_supervisor/pwsh.py", 1), ("installer/stage2/runner.py", 1), ("ns_supervisor/update.py", 4),
                        ("ns_supervisor/verify.py", 2), ("ns_supervisor/doctor.py", 1), ("ns_supervisor/control.py", 1)):  # fmt: skip
        assert (ROOT / file).read_text(encoding="utf-8").count("creationflags=window_flags()") == count, file


def test_the_flag_is_no_window_without_a_console_and_nothing_in_the_owners_console(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(childenv, "has_console_window", lambda: False)
    assert childenv.window_flags() == NO_WINDOW == childenv.CREATE_NO_WINDOW
    monkeypatch.setattr(childenv, "has_console_window", lambda: True)
    assert childenv.window_flags() == 0                      # setup's console, a terminal: the child stays in it


class Seen:
    """Stands in for subprocess.run: records the flags of every start."""

    def __init__(self) -> None:
        self.flags: list[Any] = []

    def __call__(self, argv: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        self.flags.append(kw.get("creationflags", "not passed"))
        return subprocess.CompletedProcess(argv, 0, "", "")


@pytest.mark.parametrize(("console", "flags"), [(False, NO_WINDOW), (True, 0)])
def test_every_start_of_the_engine_and_the_update_worker_carries_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, console: bool, flags: int
):
    """`console` False: the engine or the update worker (no console). True: a terminal or setup's console,
    where nothing may be hidden (what `ns verify --quick` prints during `ns update` is read there)."""
    seen = Seen()
    monkeypatch.setattr(childenv, "has_console_window", lambda: console)
    monkeypatch.setattr(subprocess, "run", seen)
    monkeypatch.setattr(pwsh, "find_powershell", lambda: "powershell.exe")
    monkeypatch.setattr(doctor, "find_uv", lambda paths: "uv.exe")
    monkeypatch.setattr(update.shutil, "which", lambda name: "git.exe")
    from ns_common.paths import Paths

    paths = Paths(tmp_path / "home")
    (paths.venvs / "studio" / "Scripts").mkdir(parents=True)
    (paths.venvs / "studio" / "Scripts" / "python.exe").write_bytes(b"")
    (tmp_path / ".git").mkdir()
    pwsh.run_powershell("Write-Output 1")                                            # every PowerShell of the engine
    SubprocessRunner().run(["powershell.exe", "-NoProfile", "-Command", "1"])       # the reach's shortcut changes
    SubprocessRunner().run(["uv.exe", "--version"], capture=False)
    SubprocessRunner(bridge=True).run(["uv.exe", "--version"], capture=False)
    control.image_name(4)
    doctor.freeze_venv(paths, "studio")
    update.git_untracked(tmp_path)
    update._run(["uv.exe", "pip", "install"])
    update.verify_quick(tmp_path, "python.exe")
    update._git_run(tmp_path)(["status"])
    assert seen.flags == [flags] * 10                        # (verify.py's two starts: the source test above)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows consoles")
def test_a_really_detached_process_has_no_console_window_and_asks_for_the_flag(tmp_path: Path):
    """The engine's own situation, for real: a process started DETACHED sees no console window."""
    out = tmp_path / "flags.txt"
    code = (
        "import sys; from pathlib import Path; from ns_supervisor.childenv import has_console_window, window_flags; "
        "Path(sys.argv[1]).write_text(f'{has_console_window()} {window_flags()}')"
    )
    # the interpreter itself, not the venv's python.exe: that one is a small starter whose own child (the
    # real interpreter) would be the console program started by a process without a console
    python = getattr(sys, "_base_executable", None) or sys.executable
    done = subprocess.run(
        [python, "-c", code, str(out)], cwd=str(ROOT), creationflags=DETACHED, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120, check=False,
    )
    assert done.returncode == 0 and out.read_text() == f"False {NO_WINDOW}"
