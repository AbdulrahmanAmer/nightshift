"""P29a: setup and ``ns update`` install the packages the committed ``uv.lock`` pins.

The invariant: a venv gets third-party packages only from the lock of the program being installed, checked
against that program's ``pyproject.toml`` before the first venv is created or changed. A program from before
the lock (0.1.14, the 0.1.15 candidates up to rc3) is installed its own way.

Most tests record the uv commands on fakes. Two run the real pinned uv when this machine has it (CI runners do
not): the product's own export calls against the committed lock, and a whole locked install of a hand-made
stand-in wheel from a local file index, offline.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path

import pytest

from installer.stage2.runner import RunResult
from installer.stage2.simulate import Call, FakeRunner
from installer.stage2.steps.s04_venvs import S04Venvs
from installer.stage2.testing import example_answers, make_test_context
from installer.stage2.web import appcopy, bootstrap
from installer.stage2.web.bootstrap import BootstrapError, CmdResult
from ns_common import rollback_intent
from ns_common import uv as ns_uv
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_common.venv_extras import DEV_VENV_EXTRAS, VENV_EXTRAS
from ns_supervisor import update
from tests.test_installer_app_copy import make_boot, make_source, run_b02
from tests.test_installer_web_controller import Setup, finish_script
from tests.test_ns_update import Env, make_app, make_zip

try:
    from installer.stage2.web import deplock
except ImportError:  # the code before P29a has no lock module: each test then fails at its own assertion
    deplock = None

ROOT = Path(__file__).resolve().parents[1]
LOCK_CODE = "installer/stage2/web/deplock.py"  # its presence marks a release that installs from a lock
NO_VENV_CHANGED = "No Python environment was changed."

STANDIN_PROJECT = """[project]
name = "nightshift"
version = "{version}"
requires-python = ">=3.12,<3.13"
dependencies = ["standin>=1"]

[project.optional-dependencies]
studio = ["standin-studio>=2,<3"]
dev-install = []
supervisor = []
tts = []
dashboard = []
"""
STANDIN_LOCK = """version = 1
revision = 5
requires-python = "==3.12.*"

[[package]]
name = "nightshift"
version = "{version}"
source = {{ editable = "." }}

[package.metadata]
requires-dist = [
    {{ name = "standin", specifier = ">=1" }},
    {{ name = "standin-studio", marker = "extra == 'studio'", specifier = ">=2,<3" }},
]
provides-extras = ["studio", "dev-install", "supervisor", "tts", "dashboard"]
"""


def program_files(version: str = "9.9.9", *, lock: str = "good") -> dict[str, str]:
    """The files of a stand-in program that installs from a lock. ``lock``: good, stale (made for another
    version) or none (the lock file is missing)."""
    files = {
        "VERSION": version + "\n",
        "pyproject.toml": STANDIN_PROJECT.format(version=version),
        LOCK_CODE: "# stand-in: marks a release that installs from a lock\n",
    }
    if lock != "none":
        files["uv.lock"] = STANDIN_LOCK.format(version=version if lock == "good" else "0.0.1")
    return files


def write_program(root: Path, version: str = "9.9.9", *, lock: str = "good") -> Path:
    for rel, text in program_files(version, lock=lock).items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return root


def locked_zip(tmp_path: Path, version: str = "9.9.9", *, lock: str = "good") -> Path:
    files = program_files(version, lock=lock)
    del files["VERSION"]  # make_zip writes it
    return make_zip(tmp_path, version, extra=files)


def raised(call: Callable[[], object]) -> NsError | BootstrapError | None:
    """The coded error ``call`` raised, or None when it returned."""
    try:
        call()
    except (NsError, BootstrapError) as e:
        return e
    return None


def uv_tags(runner: object, start: int = 0) -> list[str]:
    assert isinstance(runner, FakeRunner)
    return [c.tag for c in runner.calls[start:] if c.tag.startswith("uv ")]


def uv_argv(runner: object, tag: str) -> list[str]:
    assert isinstance(runner, FakeRunner)
    found = [c.argv for c in runner.calls if c.tag == tag]
    assert found, f"no command tagged {tag!r}; seen: {uv_tags(runner)}"
    return [str(a) for a in found[-1][1:]]


def kind(cmd: list[str]) -> str:
    """What one uv command of ``resync_venvs`` does, and for which venv."""
    if cmd[1] == "export":
        return "export " + Path(cmd[cmd.index("-o") + 1]).stem.removeprefix("pins-")
    venv = Path(cmd[cmd.index("--python") + 1]).parts[-3]
    if "-r" in cmd:
        return "packages " + venv
    spec = cmd[cmd.index("-e") + 1]
    return ("ranges " if "[" in spec else "app ") + venv


def make_venvs(paths: Paths, *names: str) -> None:
    for name in names:
        py = paths.venvs / name / "Scripts" / "python.exe"
        py.parent.mkdir(parents=True, exist_ok=True)
        py.write_text("", encoding="utf-8")


def version_of(folder: Path) -> str:
    return (folder / "VERSION").read_text(encoding="utf-8").strip()


@pytest.fixture(autouse=True)
def home_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NIGHTSHIFT_HOME", "placeholder")  # b02 sets it in os.environ: undone at teardown


# ---- the committed lock --------------------------------------------------------------------------------
def test_the_lock_matches_pyproject_offline(tmp_path: Path) -> None:
    assert (ROOT / "uv.lock").is_file(), "the repository has no uv.lock"
    assert deplock is not None
    stale = deplock.problem(ROOT)
    assert stale is None, f"{stale} Make the lock again: {deplock.RELOCK}"
    # the check is not one that always passes: each way a lock goes stale is named
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    version = re.search(r'(?m)^version = "([^"]+)"', project)
    assert version is not None
    changes = {
        "was made for version": project.replace(version.group(0), 'version = "99.0.0"', 1),
        "changed: rich": project.replace('"rich>=13"', '"rich>=13.5"'),
        "not in the lock: brand-new (supervisor)": project.replace(
            '"psutil>=6"]', '"psutil>=6", "brand_new>=1"]', 1
        ),
        "only in the lock: typer": project.replace('  "typer>=0.12",\n', ""),
        "the extras differ: later": project.replace("\ntts = [", "\nlater = []\ntts = ["),
    }
    for words, text in changes.items():
        assert text != project, words
        case = tmp_path / str(len(words))
        case.mkdir()
        (case / "pyproject.toml").write_text(text, encoding="utf-8")
        (case / "uv.lock").write_text(lock, encoding="utf-8")
        assert words in (deplock.problem(case) or ""), words
    (tmp_path / "torn").mkdir()
    (tmp_path / "torn" / "pyproject.toml").write_text(project, encoding="utf-8")
    (tmp_path / "torn" / "uv.lock").write_text(lock[: len(lock) // 2], encoding="utf-8")
    assert "cannot be read" in (deplock.problem(tmp_path / "torn") or "")
    (tmp_path / "none").mkdir()
    assert "uv.lock is missing" in (deplock.problem(tmp_path / "none") or "")


def test_the_lock_pins_no_torch_and_every_package_has_a_hash() -> None:
    assert (ROOT / "uv.lock").is_file(), "the repository has no uv.lock"
    import tomllib

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    names = {p["name"] for p in lock["package"]}
    assert not {"torch", "torchaudio", "qwen-tts"} & names  # the tts venv stays on the manifest pins
    assert not [n for n in names if n.startswith("nvidia-")]  # so do the cuBLAS and cuDNN wheels
    for package in lock["package"]:
        if package["name"] == "nightshift":
            continue
        files = [*package.get("wheels", []), *([package["sdist"]] if "sdist" in package else [])]
        assert files and all(f.get("hash", "").startswith("sha256:") for f in files), package["name"]


def pinned_uv() -> str | None:
    """The uv ``manifest.json`` pins, when this machine has it (stage 1's download or PATH)."""
    if deplock is None:
        return None
    from dev import lock_deps

    return lock_deps.pinned_uv()[0]


def real_uv_env(tmp_path: Path, **more: str) -> dict[str, str]:
    """An environment in which uv reads no user settings, downloads nothing and keeps its cache in tmp."""
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("UV_")}
    env.update(
        UV_CACHE_DIR=str(tmp_path / "uv-cache"),
        UV_OFFLINE="1",
        UV_NO_CONFIG="1",
        UV_PYTHON_DOWNLOADS="never",
        UV_PYTHON=sys.executable,
        UV_LINK_MODE="copy",
        UV_NO_PROGRESS="1",
        **more,
    )
    return env


def test_the_pinned_uv_exports_every_venv_from_the_committed_lock(tmp_path: Path) -> None:
    assert (ROOT / "uv.lock").is_file(), "the repository has no uv.lock"
    assert deplock is not None
    uv = pinned_uv()
    if uv is None:
        pytest.skip("the uv that manifest.json pins is not on this machine")
    for label, table in (("install", VENV_EXTRAS), ("dev", DEV_VENV_EXTRAS)):
        for venv in ("studio", "supervisor"):
            out = tmp_path / f"{label}-{venv}.txt"
            res = subprocess.run(
                [uv, *deplock.export_args(ROOT, table[venv], out)],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
                env=real_uv_env(tmp_path),
                cwd=tmp_path,
            )
            assert res.returncode == 0, res.stderr
            lines = [ln for ln in out.read_text(encoding="utf-8").splitlines() if ln and ln[0].isalnum()]
            assert lines and all("==" in ln for ln in lines), lines[:3]
            names = {ln.split("==")[0] for ln in lines}
            assert "pydantic" in names and "nightshift" not in names and "torch" not in names
            assert ("faster-whisper" in names) == (label == "install" and venv == "studio")
            assert out.read_text(encoding="utf-8").count("--hash=sha256:") >= len(lines)


# ---- setup: s04 -----------------------------------------------------------------------------------------
def test_s04_installs_each_venv_from_the_exported_lock_with_hashes(tmp_path: Path) -> None:
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True)
    step = S04Venvs()
    step.run(ctx)
    tags = uv_tags(ctx.runner)
    # every venv's set is exported before the first venv exists
    assert tags[: tags.index("uv venv studio")] == ["uv export studio", "uv export supervisor"], tags
    app = str(ctx.app_path)
    for venv in ("studio", "supervisor"):
        pins, py = str(ctx.paths.logs / f"pins-{venv}.txt"), str(ctx.venv_python(venv))
        extras = [word for extra in VENV_EXTRAS[venv] for word in ("--extra", extra)]
        assert uv_argv(ctx.runner, f"uv export {venv}") == [
            "export",
            "--locked",
            "--offline",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            *extras,
            "--project",
            app,
            "-o",
            pins,
        ]
        assert uv_argv(ctx.runner, f"uv pip install {venv} locked packages") == [
            "pip",
            "install",
            "--python",
            py,
            "--require-hashes",
            "-r",
            pins,
        ]
        assert uv_argv(ctx.runner, f"uv pip install {venv} app") == [
            "pip",
            "install",
            "--python",
            py,
            "--no-deps",
            "-e",
            app,
        ]
        order = [
            tags.index(f"uv venv {venv}"),
            tags.index(f"uv pip install {venv} locked packages"),
            tags.index(f"uv pip install {venv} app"),
        ]
        assert order == sorted(order), tags
    # no command lets uv choose versions from the ranges in pyproject.toml
    assert isinstance(ctx.runner, FakeRunner)
    assert not [c.tag for c in ctx.runner.calls if any(str(a).startswith(app + "[") for a in c.argv)]
    # second run: the step is done (asked of the venvs themselves), and asking ran no uv
    before = len(ctx.runner.calls)
    assert step.check(ctx) is True
    assert uv_tags(ctx.runner, before) == []


def test_a_dev_install_exports_the_dev_extras_from_the_same_lock(tmp_path: Path) -> None:
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, dev_mode=True)
    S04Venvs().run(ctx)
    export = uv_argv(ctx.runner, "uv export studio")
    assert [export[i + 1] for i, word in enumerate(export) if word == "--extra"] == [
        "dev-install",
        "supervisor",
    ]
    assert "--require-hashes" in uv_argv(ctx.runner, "uv pip install studio locked packages")


@pytest.mark.parametrize(
    ("lock", "words"), [("none", "uv.lock is missing"), ("stale", "was made for version 0.0.1")]
)
def test_a_stale_lock_stops_s04_before_any_venv_changes_with_ns_e124(
    tmp_path: Path, lock: str, words: str
) -> None:
    app = write_program(tmp_path / "program", lock=lock)
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, app_path=app)
    err = raised(lambda: S04Venvs().run(ctx))
    assert err is not None and err.code == "NS-E124", err
    assert words in err.detail and err.detail.endswith(NO_VENV_CHANGED)
    assert uv_tags(ctx.runner) == []  # not even uv was asked
    assert not any(ctx.paths.venvs.iterdir()) and not ctx.flag("venvs_installed", False)


def test_a_lock_uv_refuses_stops_s04_before_any_venv_changes(tmp_path: Path) -> None:
    said = "error: The lockfile at `uv.lock` needs to be updated, but `--locked` was provided."
    runner = FakeRunner(tmp_path / "desktop", results={"uv export supervisor": RunResult(2, "", said)})
    app = write_program(tmp_path / "program")
    ctx = make_test_context(
        tmp_path, raw_answers=example_answers(), resolve=True, app_path=app, runner=runner
    )
    err = raised(lambda: S04Venvs().run(ctx))
    assert err is not None and err.code == "NS-E124", err
    assert (
        "needs to be updated" in err.detail and "supervisor" in err.detail and NO_VENV_CHANGED in err.detail
    )
    # the studio export had passed: still no venv was created for it
    assert uv_tags(runner) == ["uv export studio", "uv export supervisor"]
    assert not any(ctx.paths.venvs.iterdir())
    # a full disk is not called a bad lock
    runner.results["uv export supervisor"] = RunResult(2, "", "error: No space left on device (os error 112)")
    err = raised(lambda: S04Venvs().run(ctx))
    assert err is not None and err.code == "NS-E104", err


def test_setup_killed_in_the_venv_step_fills_the_venvs_again_on_the_next_run(tmp_path: Path) -> None:
    class Killed(BaseException):
        pass

    def die(call: Call) -> RunResult:
        raise Killed()

    runner = FakeRunner(tmp_path / "desktop", results={"uv pip install studio app": die})
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, runner=runner)
    step = S04Venvs()
    with pytest.raises(Killed):
        step.run(ctx)
    assert not ctx.flag("venvs_installed", False) and step.check(ctx) is False
    del runner.results["uv pip install studio app"]
    before = len(runner.calls)
    step.run(ctx)
    step.verify(ctx)
    tags = uv_tags(runner, before)
    assert tags[:2] == ["uv export studio", "uv export supervisor"], tags  # the lock is asked again
    assert "uv venv studio" not in tags  # the interpreter is there
    assert "uv pip install studio locked packages" in tags and "uv pip install studio app" in tags
    assert step.check(ctx) is True


def test_a_download_that_fails_in_s04_is_coded_leaves_the_other_venv_alone_and_a_retry_works(
    tmp_path: Path,
) -> None:
    said = "error: Failed to fetch: `https://files.pythonhosted.org/packages/x.whl`\n  Caused by: dns error"
    tag = "uv pip install studio locked packages"
    runner = FakeRunner(tmp_path / "desktop", results={tag: RunResult(1, "", said)})
    ctx = make_test_context(tmp_path, raw_answers=example_answers(), resolve=True, runner=runner)
    step = S04Venvs()
    err = raised(lambda: step.run(ctx))
    assert err is not None and err.code == "NS-E105", err
    assert "Failed to fetch" in err.detail and "studio locked packages" in err.detail
    # policy: a venv whose packages cannot be fetched stops the step; the next venv is not started
    assert "uv venv supervisor" not in uv_tags(runner) and "uv pip install studio app" not in uv_tags(runner)
    assert not ctx.flag("venvs_installed", False)
    del runner.results[tag]
    step.run(ctx)
    step.verify(ctx)
    assert step.check(ctx) is True


# ---- setup: b03, the first venv a fresh install makes ------------------------------------------------------
def b03_boot(tmp_path: Path, *, lock: str = "good") -> tuple[bootstrap.Boot, list[list[str]]]:
    source = make_source(tmp_path / "extracted", "9.9.9", **program_files(lock=lock))
    if lock == "none":
        (source / "uv.lock").unlink(missing_ok=True)  # (make_source writes a good one by default)
    boot = make_boot(tmp_path, source)
    run_b02(boot)
    seen: list[list[str]] = []

    def cmd(argv: list[str], env: object = None, cwd: object = None, timeout: object = None) -> CmdResult:
        seen.append([str(a) for a in argv])
        if seen[-1][1] == "venv":
            boot.venv_python.parent.mkdir(parents=True, exist_ok=True)
            boot.venv_python.write_bytes(b"MZ")
        return CmdResult(0, "", "")

    boot.cmd = cmd
    return boot, seen


def test_b03_fills_the_setup_environment_from_the_lock(tmp_path: Path) -> None:
    boot, seen = b03_boot(tmp_path)
    step = bootstrap.B03SetupEnv()
    step.run(boot)
    app, py, pins = str(boot.run_app), str(boot.venv_python), str(boot.home / "logs" / "pins-setup.txt")
    assert [c[1:] for c in seen] == [
        [
            "export",
            "--locked",
            "--offline",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--extra",
            "studio",
            "--project",
            app,
            "-o",
            pins,
        ],
        ["venv", "--python", boot.python_version, str(boot.home / "venvs" / "studio")],
        ["pip", "install", "--python", py, "--require-hashes", "-r", pins],
        ["pip", "install", "--python", py, "--no-deps", "-e", app],
    ]
    assert step.check(boot) is True


@pytest.mark.parametrize(
    ("lock", "words"), [("none", "uv.lock is missing"), ("stale", "was made for version 0.0.1")]
)
def test_b03_stops_with_ns_e124_before_the_venv_exists(tmp_path: Path, lock: str, words: str) -> None:
    boot, seen = b03_boot(tmp_path, lock=lock)
    rows: list[tuple[str, str]] = []
    boot.on_row = lambda key, status, detail: rows.append((key, status))
    err = raised(lambda: bootstrap.B03SetupEnv().run(boot))
    assert err is not None and err.code == "NS-E124", err
    assert words in err.detail and err.detail.endswith(NO_VENV_CHANGED)
    assert seen == [] and not (boot.home / "venvs" / "studio").exists()
    assert rows[-1] == ("setup_env", "failed")
    assert bootstrap.B03SetupEnv().check(boot) is False  # a Retry runs the step again


def test_the_setup_window_shows_ns_e124_with_the_way_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Setup(tmp_path, monkeypatch, script=finish_script)
    s.cmd.fail["export"] = CmdResult(2, "", "error: The lockfile at `uv.lock` needs to be updated")
    s.accept()
    s.ctl.do_action("start")
    state = s.until(lambda st: st["run"] in ("waiting", "finished"), what="the setup environment step")
    failure = state["failure"]
    assert failure is not None and failure["code"] == "NS-E124", state["run"]
    assert failure["step"] == "b03_setup_env"
    assert failure["title"] == "This copy of Nightshift is incomplete"
    assert "needs to be updated" in failure["detail"] and NO_VENV_CHANGED in failure["detail"]
    assert not (s.home / "venvs" / "studio").exists()


def test_ns_e124_says_what_is_true_and_the_way_out() -> None:
    table = json.loads((ROOT / "contracts" / "errors.json").read_text(encoding="utf-8"))
    assert "NS-E124" in table
    entry = table["NS-E124"]
    assert "uv.lock" in entry["message"] and "missing or does not match pyproject.toml" in entry["message"]
    assert "Download the release again and run INSTALL.cmd" in entry["hint"]
    assert "run ns update again with a complete package" in entry["hint"]
    # setup has already made the install folder when it finds this: the hint claims only what holds everywhere
    assert entry["hint"].endswith(NO_VENV_CHANGED) and "Nothing was changed" not in entry["hint"]
    copy = json.loads((ROOT / "installer/stage2/web/operator_copy.json").read_text(encoding="utf-8"))[
        "NS-E124"
    ]
    assert "download the Nightshift zip again" in copy["whatToDo"] and "INSTALL.cmd" in copy["whatToDo"]
    assert "Retry cannot fix that" in copy["whatToDo"]


# ---- ns update ------------------------------------------------------------------------------------------
def test_resync_venvs_uses_the_lock_and_rolls_back_on_failure(tmp_path: Path) -> None:
    app, z = make_app(tmp_path), locked_zip(tmp_path)  # from a program without a lock to one with
    paths = Paths(tmp_path / "ns")
    make_venvs(paths, "studio", "supervisor", "tts")
    launcher = paths.venvs / "supervisor" / "Scripts" / "ns.exe"
    launcher.write_text("old launcher", encoding="utf-8")
    cmds: list[list[str]] = []

    def run(cmd: list[str]) -> tuple[int, str]:
        cmds.append(cmd)
        if kind(cmd) == "packages supervisor":
            return 1, "error: Failed to fetch `https://pypi.org/simple/standin/`"
        return 0, ""

    e = Env()
    env = dataclasses.replace(e.env(), sync=lambda a: update.resync_venvs(a, paths, uv="uv", run=run))
    err = raised(lambda: update.update_app(z, app, env, paths=paths))
    assert err is not None and err.code == "NS-E702", err
    assert "venv supervisor" in err.detail and "Failed to fetch" in err.detail
    kinds = [kind(c) for c in cmds]
    # the new program: both sets exported first, studio filled, supervisor's download failed
    assert kinds[:5] == [
        "export studio",
        "export supervisor",
        "packages studio",
        "app studio",
        "packages supervisor",
    ], kinds
    assert launcher.read_text(encoding="utf-8") == "old launcher"  # the launcher uv never replaced is back
    # rolled back as before: the old program is in place, and its venvs are filled again by ITS rule
    assert version_of(app) == "0.1.0" and not (tmp_path / "NightShift.new").exists()
    assert kinds[5:] == ["ranges studio", "ranges supervisor", "ranges tts"], kinds
    assert "verify" not in e.calls


def test_an_update_from_a_program_without_a_lock_ends_on_the_locked_packages(tmp_path: Path) -> None:
    app, z = make_app(tmp_path), locked_zip(tmp_path)
    paths = Paths(tmp_path / "ns")
    make_venvs(paths, "studio", "supervisor", "tts")
    cmds: list[list[str]] = []
    e = Env()
    sync = lambda a: update.resync_venvs(a, paths, uv="uv", run=lambda c: (cmds.append(c) or 0, ""))  # noqa: E731
    res = update.update_app(z, app, dataclasses.replace(e.env(), sync=sync), paths=paths)
    assert res.new_version == "9.9.9" and version_of(app) == "9.9.9" and "reregister" in e.calls
    locked = [
        "export studio",
        "export supervisor",
        "packages studio",
        "app studio",
        "packages supervisor",
        "app supervisor",
        "app tts",
    ]
    assert [kind(c) for c in cmds] == locked
    by_kind = {kind(c): c for c in cmds}
    for venv in ("studio", "supervisor"):
        pins, py = str(paths.logs / f"pins-{venv}.txt"), str(paths.venvs / venv / "Scripts" / "python.exe")
        extras = [word for extra in VENV_EXTRAS[venv] for word in ("--extra", extra)]
        assert by_kind[f"export {venv}"][1:] == [
            "export",
            "--locked",
            "--offline",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            *extras,
            "--project",
            str(app),
            "-o",
            pins,
        ]
        assert by_kind[f"packages {venv}"][1:] == [
            "pip",
            "install",
            "--python",
            py,
            "--require-hashes",
            "-r",
            pins,
        ]
        assert by_kind[f"app {venv}"][1:] == ["pip", "install", "--python", py, "--no-deps", "-e", str(app)]
    assert "--no-deps" in by_kind["app tts"]  # the tts venv: the program only, as before
    # second run: the same commands again, nothing else (uv finds everything in place)
    cmds.clear()
    assert update.resync_venvs(app, paths, uv="uv", run=lambda c: (cmds.append(c) or 0, "")) == [
        "studio",
        "supervisor",
        "tts",
    ]
    assert [kind(c) for c in cmds] == locked
    # back to the program from before the lock: filled its own way, as that release installed itself
    cmds.clear()
    back = update.rollback_app(app, dataclasses.replace(Env().env(), sync=sync), paths=paths)
    assert back.new_version == "0.1.0"
    assert [kind(c) for c in cmds] == ["ranges studio", "ranges supervisor", "ranges tts"]
    assert cmds[0][:5] == ["uv", "pip", "install", "-e", f"{app}[studio,supervisor]"]


@pytest.mark.parametrize(
    ("lock", "words"), [("none", "uv.lock is missing"), ("stale", "was made for version 0.0.1")]
)
def test_a_package_with_a_missing_or_stale_lock_is_refused_before_the_swap(
    tmp_path: Path, lock: str, words: str
) -> None:
    app, z = make_app(tmp_path), locked_zip(tmp_path, lock=lock)
    paths = Paths(tmp_path / "ns")
    make_venvs(paths, "studio")
    e = Env()
    err = raised(lambda: update.update_app(z, app, e.env(), paths=paths))
    assert err is not None and err.code == "NS-E124", err
    assert words in err.detail and "The update did not start. Nothing was changed" in err.detail
    assert version_of(app) == "0.1.0" and (app / "ns_common" / "x.py").read_text(encoding="utf-8") == "OLD"
    assert not (tmp_path / "NightShift.new").exists() and not (tmp_path / "NightShift.prev").exists()
    assert "sync" not in e.calls and "verify" not in e.calls


def test_a_lock_uv_refuses_after_the_swap_changes_no_venv_and_rolls_back(tmp_path: Path) -> None:
    app, z = make_app(tmp_path), locked_zip(tmp_path)
    paths = Paths(tmp_path / "ns")
    make_venvs(paths, "studio", "supervisor")
    cmds: list[list[str]] = []

    def run(cmd: list[str]) -> tuple[int, str]:
        cmds.append(cmd)
        return (
            (2, "error: The lockfile at `uv.lock` needs to be updated")
            if kind(cmd) == "export supervisor"
            else (0, "")
        )

    env = dataclasses.replace(Env().env(), sync=lambda a: update.resync_venvs(a, paths, uv="uv", run=run))
    err = raised(lambda: update.update_app(z, app, env, paths=paths))
    assert err is not None and err.code == "NS-E702", err
    assert "needs to be updated" in err.detail and NO_VENV_CHANGED in err.detail
    kinds = [kind(c) for c in cmds]
    assert kinds[:2] == ["export studio", "export supervisor"] and not [
        k for k in kinds if k.startswith(("packages", "app"))
    ]
    assert version_of(app) == "0.1.0"


def test_a_rollback_to_a_program_whose_lock_is_gone_is_refused_before_anything_changes(
    tmp_path: Path,
) -> None:
    app = write_program(tmp_path / "NightShift", "9.9.9")
    prev = write_program(tmp_path / "NightShift.prev", "9.9.8", lock="none")
    paths = Paths(tmp_path / "ns")
    e = Env()
    err = raised(lambda: update.rollback_app(app, e.env(), paths=paths))
    assert err is not None and err.code == "NS-E702", err
    assert "uv.lock is missing" in err.detail and "ns update --rollback did not start" in err.detail
    assert "run INSTALL.cmd from its zip" in err.detail
    assert version_of(app) == "9.9.9" and version_of(prev) == "9.9.8"
    assert not rollback_intent.path_for(app).exists() and "sync" not in e.calls
    # with its lock back the same rollback goes through, on the locked packages of that program
    write_program(prev, "9.9.8")
    make_venvs(paths, "studio")
    cmds: list[list[str]] = []
    sync = lambda a: update.resync_venvs(a, paths, uv="uv", run=lambda c: (cmds.append(c) or 0, ""))  # noqa: E731
    assert (
        update.rollback_app(app, dataclasses.replace(Env().env(), sync=sync), paths=paths).new_version
        == "9.9.8"
    )
    assert [kind(c) for c in cmds] == ["export studio", "packages studio", "app studio"]


def test_a_program_from_before_the_lock_is_still_filled_from_its_ranges(tmp_path: Path) -> None:
    # control: what 0.1.14's own updater runs against any program folder, and what this one runs for 0.1.14
    paths = Paths(tmp_path / "ns")
    make_venvs(paths, "studio", "tts")
    app = make_app(tmp_path)
    cmds: list[list[str]] = []
    assert update.resync_venvs(app, paths, uv="uv", run=lambda c: (cmds.append(c) or 0, "")) == [
        "studio",
        "tts",
    ]
    assert [c[:5] for c in cmds] == [
        ["uv", "pip", "install", "-e", f"{app}[studio,supervisor]"],
        ["uv", "pip", "install", "-e", f"{app}[tts]"],
    ]
    # and the locked releases keep the extras that command names, so 0.1.14's updater can still install them
    import tomllib

    extras = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    assert {"studio", "supervisor", "tts", "dashboard"} <= set(extras)


# ---- the real uv, a stand-in wheel, no network ----------------------------------------------------------
def standin_wheel(folder: Path, version: str, note: str = "") -> Path:
    name, info = f"standin-{version}-py3-none-any.whl", f"standin-{version}.dist-info"
    files = {
        "standin/__init__.py": f'VERSION = "{version}"\n{note}',
        f"{info}/METADATA": f"Metadata-Version: 2.1\nName: standin\nVersion: {version}\n",
        f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    record = []
    for rel, text in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(text.encode()).digest()).rstrip(b"=").decode()
        record.append(f"{rel},sha256={digest},{len(text.encode())}")
    files[f"{info}/RECORD"] = "\n".join([*record, f"{info}/RECORD,,"]) + "\n"
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(folder / name, "w") as zf:
        for rel, text in files.items():
            zf.writestr(rel, text)
    return folder / name


def file_index(wheels: Path, simple: Path) -> str:
    """A package index made of files: one page that lists every stand-in wheel with its SHA256."""
    rows = [
        f'<a href="{w.resolve().as_uri()}#sha256={hashlib.sha256(w.read_bytes()).hexdigest()}">{w.name}</a>'
        for w in sorted(wheels.glob("standin-*.whl"))
    ]
    (simple / "standin").mkdir(parents=True, exist_ok=True)
    (simple / "standin" / "index.html").write_text(
        "<!DOCTYPE html><html><body>\n" + "<br/>\n".join(rows) + "\n</body></html>\n", encoding="utf-8"
    )
    return simple.resolve().as_uri()


def test_the_real_uv_installs_the_locked_version_not_the_newest(tmp_path: Path) -> None:
    assert deplock is not None, "no lock code"
    uv = pinned_uv()
    if uv is None:
        pytest.skip("the uv that manifest.json pins is not on this machine")
    app, wheels = tmp_path / "program", tmp_path / "wheels"
    (app / "src" / "nightshift").mkdir(parents=True)
    (app / "src" / "nightshift" / "__init__.py").write_text("", encoding="utf-8")
    version = subprocess.run([uv, "--version"], capture_output=True, text=True, check=False).stdout.split()[1]
    (app / "pyproject.toml").write_text(
        f'[build-system]\nrequires = ["uv_build=={version}"]\nbuild-backend = "uv_build"\n\n'
        + STANDIN_PROJECT.format(version="9.9.9").replace('["standin-studio>=2,<3"]', "[]"),
        encoding="utf-8",
    )
    standin_wheel(wheels, "1.0")
    env = real_uv_env(tmp_path, UV_DEFAULT_INDEX=file_index(wheels, tmp_path / "simple"))

    def real(cmd: list[str]) -> tuple[int, str]:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300, check=False, env=env, cwd=tmp_path
        )
        return res.returncode, (res.stderr or res.stdout).strip()

    assert real([uv, "lock", "--project", str(app)])[0] == 0  # the day the release is tested: 1.0
    assert deplock.problem(app) is None  # uv's own lock passes the product's check
    standin_wheel(wheels, "2.0")  # install day: a newer release is out
    file_index(wheels, tmp_path / "simple")
    paths = Paths(tmp_path / "ns")
    py = paths.venvs / "studio" / "Scripts" / "python.exe"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(py.parent.parent)], check=True, timeout=300
    )

    def installed() -> str:
        res = subprocess.run(
            [str(py), "-I", "-c", "import standin, nightshift; print(standin.VERSION)"],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        return res.stdout.strip() or res.stderr.strip()[-200:]

    assert update.resync_venvs(app, paths, uv=uv, run=real) == ["studio"]
    assert installed() == "1.0"  # the tested one, not the newest
    assert "standin==1.0" in (paths.logs / "pins-studio.txt").read_text(encoding="utf-8")
    assert update.resync_venvs(app, paths, uv=uv, run=real) == [
        "studio"
    ]  # second run: nothing to do, no error
    assert installed() == "1.0"
    # a wheel that is not the locked file (same name and version, other bytes) is refused by its hash
    shutil.rmtree(py.parent.parent)
    shutil.rmtree(tmp_path / "uv-cache", ignore_errors=True)
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(py.parent.parent)], check=True, timeout=300
    )
    standin_wheel(wheels, "1.0", note="# not the file that was locked\n")
    file_index(wheels, tmp_path / "simple")
    err = raised(lambda: update.resync_venvs(app, paths, uv=uv, run=real))
    assert err is not None and err.code == "NS-E702" and "hash" in err.detail.lower(), err
    assert "No module named" in installed()


# ---- the bundle, the copy, the uv rule ------------------------------------------------------------------
def test_the_bundle_ships_uv_lock(tmp_path: Path) -> None:
    script = (ROOT / "dev" / "make_bundle.ps1").read_text(encoding="utf-8")
    required = re.search(r"(?m)^\$RequiredEntries = @\(([^)]*)\)", script)
    assert required is not None
    assert "'uv.lock'" in required.group(1)  # a bundle without the lock is not built
    git = shutil.which("git")
    if git is not None and (ROOT / ".git").exists():  # the bundle is `git archive`: tracked files only
        tracked = subprocess.run([git, "-C", str(ROOT), "cat-file", "-e", ":uv.lock"], check=False)
        assert tracked.returncode == 0, "uv.lock is not tracked by git"
    # setup copies it into the install folder, also from a zip extracted straight into a drive root
    assert "uv.lock" in appcopy.ROOT_ENTRIES
    appcopy.copy_tree(write_program(tmp_path / "extracted"), tmp_path / "copy")
    assert (tmp_path / "copy" / "uv.lock").is_file()


def test_find_uv_pin_is_untouched(tmp_path: Path) -> None:
    # control: the lock changes what uv installs, not which uv runs (install folder, hand-over, then PATH)
    home = tmp_path / "home"
    handed = tmp_path / "boot" / "uv.exe"
    handed.parent.mkdir()
    handed.write_bytes(b"MZ")
    env = {ns_uv.BOOTSTRAP_ENV: str(handed), "LOCALAPPDATA": str(tmp_path / "none")}
    assert (
        ns_uv.find_uv(home, which=lambda n: "path-uv", env={"LOCALAPPDATA": str(tmp_path / "none")})
        == "path-uv"
    )
    assert ns_uv.find_uv(home, which=lambda n: "path-uv", env=env) == str(handed)
    ns_uv.home_uv(home).parent.mkdir(parents=True)
    ns_uv.home_uv(home).write_bytes(b"MZ")
    assert ns_uv.find_uv(home, which=lambda n: "path-uv", env=env) == str(ns_uv.home_uv(home))
    assert "bins.uv" in (ROOT / "installer" / "stage1.ps1").read_text(encoding="utf-8")
