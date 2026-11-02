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
    exe.parent.mkdir(parents=True, exist_ok=True)
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


# --- the stage 1 hand-over: uv lives in %LOCALAPPDATA%\Nightshift\bootstrap\uv, never in the home or on PATH ---
def put_bootstrap_uv(local: Path) -> Path:
    exe = local / "Nightshift" / "bootstrap" / "uv" / "uv.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    return exe


def test_handover_env_beats_path_and_the_bootstrap_folder(tmp_path: Path) -> None:
    handed = tmp_path / "elsewhere" / "uv.exe"
    handed.parent.mkdir()
    handed.write_bytes(b"MZ")
    put_bootstrap_uv(tmp_path / "local")
    env = {"NIGHTSHIFT_BOOTSTRAP_UV": str(handed), "LOCALAPPDATA": str(tmp_path / "local")}
    assert find_uv(tmp_path / "home", which=lambda n: "C:/other/uv.exe", env=env) == str(handed)


def test_bootstrap_folder_is_found_without_env_or_path(tmp_path: Path) -> None:
    exe = put_bootstrap_uv(tmp_path / "local")
    assert find_uv(tmp_path / "home", which=lambda n: None, env={"LOCALAPPDATA": str(tmp_path / "local")}) == str(
        exe
    )


def test_missing_handover_file_is_ignored(tmp_path: Path) -> None:
    env = {"NIGHTSHIFT_BOOTSTRAP_UV": str(tmp_path / "gone" / "uv.exe"), "LOCALAPPDATA": str(tmp_path / "none")}
    assert find_uv(tmp_path / "home", which=lambda n: None, env=env) is None


def test_setup_server_default_uv_finds_the_stage1_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real lookup the setup window uses, with the real folder layout stage 1 produces (no fake uv_path)."""
    from installer.stage2.web import controller

    monkeypatch.setattr("shutil.which", lambda name, *a, **k: None)
    monkeypatch.delenv("NIGHTSHIFT_BOOTSTRAP_UV", raising=False)
    local = tmp_path / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    exe = put_bootstrap_uv(local)
    assert controller.default_uv_path(tmp_path / "home") == str(exe)
    monkeypatch.setenv("NIGHTSHIFT_BOOTSTRAP_UV", str(exe))
    assert controller.default_uv_path(tmp_path / "home") == str(exe)


def test_b02_copies_the_stage1_uv_and_b03_runs_the_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from installer.stage2.web import bootstrap, controller
    from installer.stage2.web.fakes import FakeCmd

    monkeypatch.setenv("NIGHTSHIFT_HOME", "placeholder")    # b02 sets it in os.environ: undone at teardown

    monkeypatch.setattr("shutil.which", lambda name, *a, **k: None)
    monkeypatch.delenv("NIGHTSHIFT_BOOTSTRAP_UV", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    stage1_uv = put_bootstrap_uv(tmp_path / "local")
    (stage1_uv.parent / "uvx.exe").write_bytes(b"MZ")
    (stage1_uv.parent / "notes.txt").write_text("x", encoding="utf-8")
    home = tmp_path / "home"
    var: dict[str, str] = {}
    cmd = FakeCmd()
    boot = bootstrap.Boot(
        app_path=tmp_path / "app", home=home, uv=controller.default_uv_path(home), cmd=cmd,
        set_home_var=lambda n, v: var.update({n: v}) or True, get_home_var=var.get,
    )
    b02, b03 = bootstrap.B02Home(), bootstrap.B03SetupEnv()
    assert not b02.check(boot)
    b02.run(boot)
    b02.verify(boot)
    assert (home / "bin" / "uv" / "uv.exe").is_file() and (home / "bin" / "uv" / "uvx.exe").is_file()
    assert not (home / "bin" / "uv" / "notes.txt").exists()
    b03.run(boot)
    assert cmd.calls and all(c[0] == str(home / "bin" / "uv" / "uv.exe") for c in cmd.calls)


def test_b02_without_any_uv_is_a_clear_ns_e105_not_a_later_winerror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from installer.stage2.web import bootstrap, controller

    monkeypatch.setenv("NIGHTSHIFT_HOME", "placeholder")

    monkeypatch.setattr("shutil.which", lambda name, *a, **k: None)
    monkeypatch.delenv("NIGHTSHIFT_BOOTSTRAP_UV", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    home = tmp_path / "home"
    var: dict[str, str] = {}
    boot = bootstrap.Boot(
        app_path=tmp_path / "app", home=home, uv=controller.default_uv_path(home),
        set_home_var=lambda n, v: var.update({n: v}) or True, get_home_var=var.get,
    )
    with pytest.raises(bootstrap.BootstrapError) as err:
        bootstrap.B02Home().run(boot)
    assert err.value.code == "NS-E105"
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.B02Home().verify(boot)
