"""C1: INSTALL.cmd run again over a half-made venv.

uv 0.10 and later refuse ``uv venv`` in a folder that already exists ("use --clear"). A ``venvs/studio``
that lost its ``python.exe`` (an uninstall that could not remove all of it, a scanner that quarantined the
interpreter, a crash while the venv was made) therefore failed every Retry and every ``INSTALL.cmd`` with
NS-E105. The venv steps now pass ``--clear`` once when the folder is there without a ``python.exe``, and
never in a loop.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from installer.stage2.runner import RunResult
from installer.stage2.simulate import FakeRunner
from installer.stage2.steps.s04_venvs import S04Venvs
from installer.stage2.testing import example_answers, make_test_context
from installer.stage2.web import bootstrap
from installer.stage2.web.bootstrap import BootstrapError, CmdResult
from ns_common.errors import NsError
from ns_common.uv import venv_args
from tests.test_installer_app_copy import make_boot, make_source, run_b02


@pytest.fixture(autouse=True)
def home_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NIGHTSHIFT_HOME", "placeholder")     # b02 sets it in os.environ: undone at teardown


def test_a_fresh_folder_gets_no_flag_and_an_existing_one_gets_clear(tmp_path: Path) -> None:
    target = tmp_path / "venvs" / "studio"
    assert venv_args(target, "3.12") == ["venv", "--python", "3.12", str(target)]    # older uv has no --clear
    target.mkdir(parents=True)
    assert venv_args(target, "3.12") == ["venv", "--python", "3.12", "--clear", str(target)]
    (target / "Lib").mkdir()
    assert venv_args(target, "3.12")[3] == "--clear"                 # what is inside does not matter


# ---- s04 -------------------------------------------------------------------------------------------
def test_s04_clears_a_half_venv_and_leaves_a_fresh_one_alone(tmp_path: Path) -> None:
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True)
    half = ctx.paths.venvs / "studio"
    (half / "Lib").mkdir(parents=True)
    (half / "Lib" / "left-over.txt").write_text("x", encoding="utf-8")
    step = S04Venvs()
    step._venv(ctx, "studio")
    step._venv(ctx, "supervisor")
    runner = ctx.runner
    assert isinstance(runner, FakeRunner)
    studio = runner.called("uv venv studio")
    supervisor = runner.called("uv venv supervisor")
    assert len(studio) == 1 and studio[0].argv[-5:] == ["venv", "--python", "3.12", "--clear", str(half)]
    assert len(supervisor) == 1 and "--clear" not in supervisor[0].argv
    step._venv(ctx, "studio")                                       # python.exe exists now: nothing to do
    assert len(runner.called("uv venv studio")) == 1


def test_s04_does_not_loop_when_uv_refuses_even_with_clear(tmp_path: Path) -> None:
    runner = FakeRunner(tmp_path / "desktop", results={"uv venv studio": RunResult(2, "", "error")})
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, runner=runner)
    (ctx.paths.venvs / "studio").mkdir(parents=True)
    with pytest.raises(NsError) as ei:
        S04Venvs()._venv(ctx, "studio")
    assert ei.value.code == "NS-E105" and len(runner.called("uv venv studio")) == 1


# ---- b03 -------------------------------------------------------------------------------------------
def b03_boot(tmp_path: Path):
    boot = make_boot(tmp_path, make_source(tmp_path / "extracted"))
    run_b02(boot)
    half = boot.home / "venvs" / "studio"
    half.mkdir(parents=True, exist_ok=True)
    (half / "pyvenv.cfg").write_text("home = gone", encoding="utf-8")        # a venv without its interpreter
    assert not boot.venv_python.exists()
    return boot


def test_b03_clears_a_half_venv_once_and_goes_on(tmp_path: Path) -> None:
    boot = b03_boot(tmp_path)
    seen: list[list[str]] = []

    def cmd(argv, env=None, cwd=None, timeout=None) -> CmdResult:
        seen.append([str(a) for a in argv])
        if seen[-1][1] == "venv":
            boot.venv_python.parent.mkdir(parents=True, exist_ok=True)
            boot.venv_python.write_bytes(b"MZ")
        return CmdResult(0, "", "")

    boot.cmd = cmd
    bootstrap.B03SetupEnv().run(boot)
    venv_calls = [c for c in seen if c[1] == "venv"]
    assert len(venv_calls) == 1 and "--clear" in venv_calls[0]
    assert venv_calls[0][-1] == str(boot.home / "venvs" / "studio")
    assert any(c[1:3] == ["pip", "install"] for c in seen)                   # and the packages went in


def test_b03_gives_up_after_one_refused_attempt_with_the_real_reason(tmp_path: Path) -> None:
    boot = b03_boot(tmp_path)
    seen: list[list[str]] = []

    def cmd(argv, env=None, cwd=None, timeout=None) -> CmdResult:
        seen.append([str(a) for a in argv])
        return CmdResult(2, "", "error: failed to remove directory: Access is denied. (os error 5)")

    boot.cmd = cmd
    with pytest.raises(BootstrapError) as ei:
        bootstrap.B03SetupEnv().run(boot)
    assert ei.value.code == "NS-E105" and "Access is denied" in ei.value.detail
    assert len([c for c in seen if c[1] == "venv"]) == 1 and len(seen) == 1   # no retry loop


def test_b03_leaves_a_whole_venv_alone(tmp_path: Path) -> None:
    boot = b03_boot(tmp_path)
    boot.venv_python.parent.mkdir(parents=True, exist_ok=True)
    boot.venv_python.write_bytes(b"MZ")
    seen: list[list[str]] = []

    def cmd(argv, env=None, cwd=None, timeout=None) -> CmdResult:
        seen.append([str(a) for a in argv])
        return CmdResult(0, "", "")

    boot.cmd = cmd
    bootstrap.B03SetupEnv().run(boot)
    assert not [c for c in seen if c[1] == "venv"]                           # python.exe is there: no uv venv


# ---- A2-#13: an update through INSTALL.cmd re-keys s04 and s08 ------------------------------------------
def staged_bundle(root: Path, version: str) -> Path:
    """What dev/make_bundle.ps1 stages: the repo manifest with its version set from VERSION."""
    import re

    root.mkdir(parents=True)
    text = (Path(__file__).resolve().parents[1] / "manifest.json").read_text(encoding="utf-8")
    top_level = re.compile(r'(?m)^(  "version"\s*:\s*")[^"]*(")')
    text = top_level.sub(lambda m: m.group(1) + version + m.group(2), text, count=1)
    (root / "manifest.json").write_text(text, encoding="utf-8")
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return root


def test_an_update_through_install_cmd_makes_s04_and_s08_run_again(tmp_path: Path) -> None:
    from installer.stage2.steps.s08_smoke import S08Smoke
    from installer.stage2.web import appcopy
    from ns_common.config import load_manifest

    home = tmp_path / "home"
    appcopy.install_app(staged_bundle(tmp_path / "zip-old", "0.1.7"), home)

    def context():
        app = home / "app"                           # the wizard reads the manifest of the installed copy
        return make_test_context(
            tmp_path, raw_answers=example_answers(), resolve=True, app_path=app,
            manifest=load_manifest(app / "manifest.json"),
        )

    ctx = context()
    assert ctx.manifest.version == "0.1.7"
    for name in ("studio", "supervisor"):
        python = ctx.venv_python(name)
        python.parent.mkdir(parents=True, exist_ok=True)
        python.write_text("MZ", encoding="utf-8")
    ctx.set_flag("venvs_installed", True)
    ctx.set_flag("venvs_version", ctx.manifest.version)
    ctx.set_flag("venvs_app", str(ctx.app_path))
    ctx.set_flag("smoke", "passed")
    ctx.set_flag("smoke_version", ctx.manifest.version)
    assert S04Venvs().check(ctx) is True and S08Smoke().check(ctx) is True        # nothing to do for 0.1.7

    appcopy.install_app(staged_bundle(tmp_path / "zip-new", "0.1.8"), home)  # INSTALL.cmd from the new zip
    updated = context()
    assert updated.manifest.version == "0.1.8"
    assert S04Venvs().check(updated) is False                      # the environments are refreshed ...
    assert S08Smoke().check(updated) is False                      # ... and the final test runs again
