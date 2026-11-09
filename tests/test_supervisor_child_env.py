"""D-3 / F-D4: NS_DASHBOARD_NO_HOST (the setup window's "skip the host window" switch) reaches no child.

Every direct `subprocess.run` / `Popen` the supervisor package builds takes its environment from
`ns_supervisor.childenv`, so a launcher-only variable never leaks into an engine child, a probe or a helper.
The ps1 path (`Start-NsSupervisor`) is in `test_launchers.py`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

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
    with pytest.raises(NsError):
        verify.fake_job(paths)
    assert len(seen) == 2
    check(seen, tmp_path)


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
