"""C1/C3: install_state.json survives a crash mid-write and a torn file never blocks setup."""

import json
import os
from pathlib import Path

import pytest

from installer.stage2.state import StateStore
from installer.stage2.web import controller
from ns_common import atomic
from ns_common.config import load_install_state, save_install_state
from ns_common.models import InstallState, StepState
from ns_common.paths import Paths


def good_state() -> InstallState:
    st = InstallState()
    st.app_path = "D:/app"
    st.steps["b01_tools"] = StepState(status="done", error=None, finished_at="2026-11-02T10:00:00")
    return st


def test_a_torn_file_falls_back_to_the_last_good_copy_and_is_kept_as_bad(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    save_install_state(paths, good_state())
    save_install_state(paths, good_state())            # the second save leaves the first as .bak
    paths.install_state_file.write_text('{"version": "0.1.0", "steps": {"b01_', encoding="utf-8")   # torn
    st = load_install_state(paths)
    assert st.app_path == "D:/app" and "b01_tools" in st.steps
    assert paths.install_state_file.with_name("install_state.json.bad").exists()


def test_a_torn_file_without_a_backup_is_an_empty_state_not_a_crash(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    paths.config.mkdir(parents=True)
    paths.install_state_file.write_text("{ nope", encoding="utf-8")
    assert load_install_state(paths).steps == {}
    store = StateStore(paths, tmp_path / "app")            # what every wizard start does
    assert store.status("s01_questions") == "pending"


def test_the_setup_window_reads_the_backup_of_a_torn_file(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "install_state.json").write_text('{"steps": {"b01_tools"', encoding="utf-8")
    (config / "install_state.json.bak").write_text(json.dumps({"steps": {"b01_tools": {}}}), encoding="utf-8")
    assert "b01_tools" in controller.read_install_state(tmp_path)["steps"]
    controller.write_step(tmp_path, "0.1.0", Path("D:/app"), "b02_home", "done", None)
    data = json.loads((config / "install_state.json").read_text(encoding="utf-8"))
    assert "b02_home" in data["steps"] and "b01_tools" in data["steps"]


def test_a_crash_during_the_swap_leaves_the_old_file_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    atomic.write_text_atomic(target, '{"v": 1}')

    def die(src: object, dst: object) -> None:
        raise OSError("power lost")

    monkeypatch.setattr(atomic.os, "replace", die)
    with pytest.raises(OSError):
        atomic.write_text_atomic(target, '{"v": 2, "padding": "' + "x" * 100_000 + '"}')
    monkeypatch.undo()
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 1}


def test_saving_leaves_no_temp_files_and_replaces_in_one_piece(tmp_path: Path) -> None:
    paths = Paths(tmp_path)
    for _ in range(3):
        save_install_state(paths, good_state())
    names = sorted(p.name for p in paths.config.iterdir())
    assert names == ["install_state.json", "install_state.json.bak"]
    assert not any(n.endswith(".tmp") for n in os.listdir(paths.config))
