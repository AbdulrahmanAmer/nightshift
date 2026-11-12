# ruff: noqa: E501
"""D-3 / F-D4: NS_DASHBOARD_NO_HOST (the setup window's "skip the host window" switch) reaches no child.

Every direct `subprocess.run` / `Popen` the supervisor package builds takes its environment from
`ns_supervisor.childenv`, so a launcher-only variable never leaks into an engine child, a probe or a helper.
The ps1 path (`Start-NsSupervisor`) is in `test_launchers.py`.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from installer.stage2.runner import SubprocessRunner
from ns_common.errors import NsError
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_supervisor import childenv, client, control, doctor, gpu, presence, procs, pwsh, update, verify

SWITCH = "NS_DASHBOARD_NO_HOST"


@pytest.fixture
def seen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The environment of every child started through `subprocess` while the test runs."""
    monkeypatch.setenv(SWITCH, "1")
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path / "home"))
    calls: list[dict[str, Any]] = []

    def fake_run(argv: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(kw)
        return subprocess.CompletedProcess(argv, 1, "", "")

    def fake_popen(argv: Any, **kw: Any) -> None:
        calls.append(kw)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return calls


def check(calls: list[dict[str, Any]], home: Path) -> None:
    assert calls, "no child was started"
    for kw in calls:
        env = kw.get("env")
        assert env is not None, "the child inherits the whole environment"
        assert SWITCH not in env
        assert env["NIGHTSHIFT_HOME"] == str(home / "home")


# ---------------------------------------------------------------------------------------------- the helper
def test_child_env_drops_the_switch_keeps_the_rest_and_adds_extras(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SWITCH, "1")
    monkeypatch.setenv("NIGHTSHIFT_HOME", "C:/h")
    monkeypatch.setenv("NS_TEST_EXTRA_DROP", "x")
    env = childenv.child_env({"A": "1"}, also=("NS_TEST_EXTRA_DROP",))
    assert SWITCH not in env and "NS_TEST_EXTRA_DROP" not in env
    assert env["NIGHTSHIFT_HOME"] == "C:/h" and env["A"] == "1"
    assert childenv.LAUNCHER_ONLY_ENV == (SWITCH,)
    assert procs.scrubbed is childenv.scrubbed and procs.LAUNCHER_ONLY_ENV is childenv.LAUNCHER_ONLY_ENV


# ------------------------------------------------------------------------------------------- one per module
def test_pwsh_run_powershell_child_env(
    seen: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pwsh, "find_powershell", lambda: "powershell.exe")
    pwsh.run_powershell("1", env_extra={"NS_PS1": "x"})
    check(seen, tmp_path)
    assert seen[0]["env"]["NS_PS1"] == "x"


def test_procs_helpers_child_env(
    seen: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(procs.sys, "platform", "win32")
    monkeypatch.setattr(gpu.shutil, "which", lambda name: "nvidia-smi")  # found on PATH: it is run
    procs.kill_tree(1)
    procs.soft_stop(1)
    procs._nvsmi(["-L"])
    assert len(seen) == 3
    check(seen, tmp_path)


def test_doctor_freeze_venv_child_env(
    seen: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths(tmp_path / "home")
    py = paths.venvs / "studio" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="ascii")
    monkeypatch.setattr(doctor, "find_uv", lambda p: "uv")
    assert doctor.freeze_venv(paths, "studio") is None
    check(seen, tmp_path)


def test_gpu_run_nvidia_smi_child_env(
    seen: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gpu.shutil, "which", lambda name: "nvidia-smi")
    assert gpu.run_nvidia_smi() is None
    check(seen, tmp_path)


def test_presence_netstat_child_env(
    seen: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(presence.sys, "platform", "win32")
    assert presence.netstat_text() == ""
    check(seen, tmp_path)


def test_control_image_name_child_env(seen: list[dict[str, Any]], tmp_path: Path) -> None:
    assert control.image_name(4242) is None
    check(seen, tmp_path)


def test_verify_probes_child_env(
    seen: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths(tmp_path / "home")
    for rel in ("studio/Scripts/python.exe", "studio/Scripts/studio.exe"):
        f = paths.venvs / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("", encoding="ascii")
    with pytest.raises(NsError):
        verify.whisper_probe(paths, Runtime.default("gpu"))
    assert len(seen) == 1
    check(seen, tmp_path)

    # The sample job (stale since card verify-job-isolated J1, which hands the job a home of its own on
    # purpose): its NIGHTSHIFT_HOME and the marker that vouches for it are the scratch home, and the switch
    # still reaches it not. The stand-in refuses to start the program, as a program that is not there would.
    def refused(argv: Any, **kw: Any) -> None:
        seen.append(kw)
        raise OSError("stand-in: the program did not start")

    monkeypatch.setattr(subprocess, "Popen", refused)
    with pytest.raises(NsError):
        verify.fake_job(paths)
    assert len(seen) == 2
    job = seen[-1]["env"]
    scratch = str(verify.scratch_home(paths))
    assert SWITCH not in job and job["NIGHTSHIFT_HOME"] == scratch and job["NS_HOME_EXPLICIT"] == scratch
    assert not verify.scratch_home(paths).exists()  # and its folder went again


def test_update_children_child_env(
    seen: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = tmp_path / "app"
    (app / ".git").mkdir(parents=True)
    monkeypatch.setattr(update.shutil, "which", lambda name: "git")
    assert update.git_untracked(app) == []
    assert update._run(["uv", "--version"], {"UV_X": "1"})[0] == 1
    assert update.verify_quick(app, python="python") is False
    assert update._git_run(app)(["status"])[0] == 1
    assert len(seen) == 4
    check(seen, tmp_path)
    assert seen[1]["env"]["UV_X"] == "1"


def test_client_spawn_supervisor_child_env(
    seen: list[dict[str, Any]], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(client, "venv_python", lambda paths, name: sys.executable)
    client.spawn_supervisor(Paths(tmp_path / "home"))
    check(seen, tmp_path)
    assert procs.UNDER_ENGINE_ENV not in seen[0]["env"]


# ----------------------------------------------------------------------------------------------------
# no console window per child (review of launcher-reach @ 50dc16e)
# ----------------------------------------------------------------------------------------------------
# A process without a console never opens a console window per child (review of launcher-reach @ 50dc16e).
#
# Windows gives a console program started by a process that has no console a new console, with a window. The
# engine has none (`client.spawn_supervisor` starts it detached), nor has the update worker: the first-start
# reach alone starts about ten PowerShells. The rule lives in one place, `ns_supervisor.childenv.window_flags`:
# CREATE_NO_WINDOW when this process has no console window, nothing when it has one (the child then shares
# the console the owner is looking at). Every place in the product that starts a program passes it, or its
# own flags, or is named below with the reason it always has a console.
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
                        # verify.py: the whisper probe, the sample job, and its `tasklist` (pid_alive, review E2 finding 6)
                        ("ns_supervisor/verify.py", 3), ("ns_supervisor/doctor.py", 1), ("ns_supervisor/control.py", 1)):  # fmt: skip
        assert (ROOT / file).read_text(encoding="utf-8").count("creationflags=window_flags()") == count, file


def test_the_flag_is_no_window_without_a_console_and_nothing_in_the_owners_console(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(childenv, "has_console_window", lambda: False)
    assert childenv.window_flags() == NO_WINDOW == childenv.CREATE_NO_WINDOW
    monkeypatch.setattr(childenv, "has_console_window", lambda: True)
    assert childenv.window_flags() == 0                      # setup's console, a terminal: the child stays in it


class Flags:
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
    seen = Flags()
    monkeypatch.setattr(childenv, "has_console_window", lambda: console)
    monkeypatch.setattr(subprocess, "run", seen)
    monkeypatch.setattr(pwsh, "find_powershell", lambda: "powershell.exe")
    monkeypatch.setattr(doctor, "find_uv", lambda paths: "uv.exe")
    monkeypatch.setattr(update.shutil, "which", lambda name: "git.exe")
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
