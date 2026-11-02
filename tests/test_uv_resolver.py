"""C3/C1/C2: one resolver finds uv for the installer context, `ns update` and `ns doctor`.

The installer puts uv at `<home>/bin/uv/uv.exe` and never on PATH, so PATH-only lookups failed in the
field while every test with a faked PATH passed.
"""

from pathlib import Path

import pytest

from installer.stage2.testing import example_answers, make_test_context
from ns_common.paths import Paths
from ns_common.uv import find_uv, home_uv
from ns_supervisor import doctor, update


def put_home_uv(home: Path) -> Path:
    exe = home_uv(home)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    return exe


def test_home_copy_wins_over_path(tmp_path: Path) -> None:
    exe = put_home_uv(tmp_path)
    assert find_uv(tmp_path, which=lambda n: "C:/other/uv.exe") == str(exe)
    assert find_uv(Paths(tmp_path), which=lambda n: None) == str(exe)


def test_falls_back_to_path_then_none(tmp_path: Path) -> None:
    assert find_uv(tmp_path, which=lambda n: "C:/other/uv.exe") == "C:/other/uv.exe"
    assert find_uv(tmp_path, which=lambda n: None) is None


def test_update_resync_uses_the_home_copy_with_an_empty_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: None)  # PATH has no uv
    paths = Paths(tmp_path / "ns")
    exe = put_home_uv(paths.home)
    py = paths.venvs / "studio" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    cmds: list[list[str]] = []
    done = update.resync_venvs(tmp_path / "app", paths, run=lambda c: (cmds.append(c) or 0, ""))
    assert done == ["studio"] and cmds[0][0] == str(exe)


def test_doctor_freeze_uses_the_home_copy_with_an_empty_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: None)
    paths = Paths(tmp_path / "ns")
    exe = put_home_uv(paths.home)
    py = paths.venvs / "studio" / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True)
    py.write_text("", encoding="utf-8")
    seen: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = "pkg==1\n"

    def fake_run(cmd: list[str], **kw: object) -> Done:
        seen.append(cmd)
        return Done()

    monkeypatch.setattr(doctor.subprocess, "run", fake_run)
    assert doctor.freeze_venv(paths, "studio") == "pkg==1\n"
    assert seen[0][0] == str(exe)


def test_installer_context_uv_is_the_home_copy_with_an_empty_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda name, *a, **k: None)
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True)
    assert ctx.uv() == "uv"  # nothing installed yet: plain name, the step then fails with a clear error
    exe = put_home_uv(ctx.paths.home)
    assert ctx.uv() == str(exe)
