"""F-8b: the `~ns*.tmp` file a crash leaves between "create" and "swap" is swept at start-up.

`ns_common.atomic` names every temporary file `~ns<pid>-<thread>-<n>.tmp` and sweeps the ones older than an
hour (`sweep_stale_tmp`), but only where a writer called it. The PowerShell launchers and the installer
scripts make the same names in `run` and `config` of the home and in `%TEMP%`; nothing visited those
folders. The engine now sweeps `run` and `config` when its process starts (a launch that reuses a running
engine does not sweep), and the setup server sweeps `%TEMP%` and the `Nightshift` folder of
`%LOCALAPPDATA%` when it starts.

"Old" is made the way `tests/test_atomic_writes.py` makes it: the age limit is moved, never the real TEMP.
"""

from __future__ import annotations

import importlib.util
import os
import tempfile
from pathlib import Path

import pytest

from ns_common import atomic

OURS = "~ns4242-0-1234567.tmp"  # the name Write-NsFileAtomic / Write-NsFileSwap give a temp file
OTHERS = (
    "notes.tmp", "~ns-not-ours.tmp", "~ns4242-0-1234567.tmp.bak", "nightshift-preflight.json", "state.json",
)  # fmt: skip


def _plant(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / OURS).write_bytes(b"half a hand-over file")
    for name in OTHERS:
        (folder / name).write_text("keep", encoding="utf-8")


def _age(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every file is now older than the limit ("an hour ago" is in the future of a file made a moment ago)."""
    monkeypatch.setattr(atomic, "STALE_TMP_S", -5.0)


def _survivors(folder: Path) -> set[str]:
    return {p.name for p in folder.iterdir()}


# --- the engine --------------------------------------------------------------------------------------------
def test_the_engine_sweeps_run_and_config_when_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_common.models import Runtime
    from ns_common.paths import Paths
    from ns_supervisor.app import create_app
    from ns_supervisor.core import Supervisor
    from ns_supervisor.testing import StubTable, fast_config

    paths = Paths(tmp_path / "ns")
    for folder in (paths.home / "run", paths.config):
        _plant(folder)

    def start_engine() -> TestClient:
        sup = Supervisor(
            paths, Runtime.default("gpu"), config=fast_config(idle_unload_s=60), resolver=StubTable()
        )
        return TestClient(create_app(sup, run_monitor=False))

    # a file made a moment ago is not a leftover
    with start_engine():
        assert OURS in _survivors(paths.config)
    _age(monkeypatch)
    with start_engine():
        for folder in (paths.home / "run", paths.config):
            assert OURS not in _survivors(folder)  # the crash leftover is gone ...
            assert set(OTHERS) <= _survivors(folder)  # ... and nothing else was touched


def test_the_sweep_of_the_home_does_not_stop_the_engine_when_a_folder_is_missing(tmp_path: Path) -> None:
    from ns_common.paths import Paths
    from ns_supervisor import tmpsweep

    assert tmpsweep.sweep_state_tmp(Paths(tmp_path / "nothing-here")) == 0


# --- the setup server --------------------------------------------------------------------------------------
@pytest.mark.skipif(importlib.util.find_spec("installer") is None, reason="needs the installer package")
def test_the_setup_server_sweeps_temp_and_its_data_folder_when_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from installer.stage2.web import __main__ as web_main

    temp = tmp_path / "temp"
    local = tmp_path / "local"
    data = local / "Nightshift"
    _plant(temp)
    _plant(data)
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    started: list[str] = []
    monkeypatch.setattr(web_main, "run_ui", lambda args, cfg: started.append("ui") or 0)

    _age(monkeypatch)
    assert web_main.main(["--no-window", "--simulate", "--app-path", str(tmp_path)]) == 0
    assert started == ["ui"]
    for folder in (temp, data):
        assert OURS not in _survivors(folder)
        assert set(OTHERS) <= _survivors(folder)  # only the product's own temp name is ever swept
    assert os.path.isdir(temp)  # the folder itself stays


def test_launch_sweep_leaves_a_young_temp_file_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from installer.stage2.web import launch

    temp = tmp_path / "temp"
    _plant(temp)
    assert launch.sweep_leftover_tmp(temp_dir=temp, env={"LOCALAPPDATA": str(tmp_path / "local")}) == 0
    assert OURS in _survivors(temp)
    _age(monkeypatch)
    assert launch.sweep_leftover_tmp(temp_dir=temp, env={"LOCALAPPDATA": str(tmp_path / "local")}) == 1
    assert OURS not in _survivors(temp)
