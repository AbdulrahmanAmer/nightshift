"""C1: "done" in the installer means the effect is there (defect D-15).

A resumed install used to report outcome done while its engine could not start. Two causes met:

1. ``S04Venvs.check()`` took "the interpreters exist" for "the packages are installed", so a setup killed
   after ``uv venv`` was skipped on rerun with an empty site-packages.
2. ``config/wizard_flags.json`` survived an uninstall that keeps data, with ``venvs_installed`` and
   ``smoke: passed`` of the install that was removed. The reinstall skipped its venvs and its smoke test.

These tests end at the effect: the venv loads the product, and ``ns verify`` ran after the venvs were redone.
The fakes (``FakeRunner``) keep their state on disk, as the real thing does, so a second ``main()`` call
finds what the first one left.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from installer.stage2.base import Step
from installer.stage2.simulate import PRODUCT_DIST_INFO, SITE_PACKAGES, FakeRunner
from installer.stage2.state import INSTALL_KEY, load_flags, reconcile_flag_file, save_flags
from installer.stage2.steps import default_steps
from installer.stage2.steps.s04_venvs import PROBE_CODE, S04Venvs
from installer.stage2.steps.s08_smoke import S08Smoke
from installer.stage2.testing import example_answers, make_test_context
from installer.stage2.wizard import Wizard, main
from ns_common.config import load_install_state
from ns_common.errors import NsError
from ns_common.paths import Paths
from tests.test_wizard_e2e import STEP_IDS, args, started_steps, write_answers

ROOT = Path(__file__).resolve().parents[1]
UNINSTALL = ROOT / "installer" / "uninstall.ps1"
PS = shutil.which("powershell") or shutil.which("pwsh")


class Killed(BaseException):
    """The console was closed: nothing after this point runs, nothing is cleaned up."""


@pytest.fixture(autouse=True)
def _no_real_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NIGHTSHIFT_HOME", raising=False)


def resolved(tmp_path: Path, runner: FakeRunner | None = None, **answers):
    return make_test_context(tmp_path, raw_answers=example_answers(**answers), resolve=True, runner=runner)


def site_packages(ctx, name: str) -> Path:
    return ctx.paths.venvs / name / SITE_PACKAGES


def hollow_out(ctx, name: str) -> None:
    """What a killed ``uv pip install`` leaves: the interpreter is there, site-packages is empty."""
    shutil.rmtree(site_packages(ctx, name) / PRODUCT_DIST_INFO)


# ---------------------------------------------------------------------------------------------- the flag file
def test_set_flag_stamps_the_install_and_another_installs_flags_read_as_unset(tmp_path: Path, ticking_clock):
    ctx = resolved(tmp_path)
    ctx.state.begin(ctx.profile, ctx.dev_mode)
    ctx.set_flag("venvs_installed", True)
    assert ctx.flag("venvs_installed") is True
    assert load_flags(ctx.paths)[INSTALL_KEY] == ctx.install_id

    (ctx.paths.config / "install_state.json").unlink()  # the uninstaller removed the state ...
    next_install = resolved(tmp_path)  # ... and kept this file (old uninstaller)
    next_install.state.begin(next_install.profile, next_install.dev_mode)
    assert next_install.install_id != ctx.install_id
    assert next_install.flag("venvs_installed") is None  # they describe the install that is gone
    assert next_install.flag("venvs_installed", False) is False
    next_install.set_flag("tuned", "tuned")
    assert load_flags(next_install.paths) == {"tuned": "tuned", INSTALL_KEY: next_install.install_id}


def test_reconcile_drops_the_flags_of_a_removed_install_and_keeps_those_of_this_one(tmp_path: Path):
    paths = Paths(tmp_path / "home")
    paths.ensure()
    flags = {"venvs_installed": True, "smoke": "passed", INSTALL_KEY: "first"}
    save_flags(paths, dict(flags))
    assert reconcile_flag_file(paths, "first", resumed=True) == []
    assert load_flags(paths) == flags
    assert reconcile_flag_file(paths, "second", resumed=True) == ["smoke", "venvs_installed"]
    assert load_flags(paths) == {INSTALL_KEY: "second"}


def test_flags_without_a_stamp_belong_to_a_resumed_install_but_not_to_a_fresh_one(tmp_path: Path):
    paths = Paths(tmp_path / "home")
    paths.ensure()
    old = {"venvs_installed": True, "smoke": "passed"}  # written before the stamp existed
    save_flags(paths, dict(old))
    assert reconcile_flag_file(paths, "running", resumed=True) == []
    assert load_flags(paths) == {**old, INSTALL_KEY: "running"}
    save_flags(paths, dict(old))
    assert reconcile_flag_file(paths, "fresh", resumed=False) == ["smoke", "venvs_installed"]
    assert load_flags(paths) == {INSTALL_KEY: "fresh"}


class Spy(Step):
    """A step that records what it can see when its work starts."""

    id = "spy"
    title = "Spy"

    def __init__(self, ctx, *, clears: tuple[str, ...] = ()) -> None:
        self.ctx = ctx
        self.clears_on_run = clears
        self.seen: dict[str, object] | None = None

    def check(self, ctx) -> bool:
        return False

    def run(self, ctx) -> None:
        self.seen = {
            "smoke": ctx.flag("smoke"),
            "own": ctx.flag("own"),
            "status": ctx.state.status(self.id),
        }
        ctx.set_flag("own", "again")

    def verify(self, ctx) -> None:
        return None


def test_the_wizard_drops_stale_flags_before_any_step_looks_at_them(tmp_path: Path, ticking_clock):
    ctx = resolved(tmp_path)
    save_flags(ctx.paths, {"smoke": "passed", "venvs_installed": True})  # install_state.json: removed
    spy = Spy(ctx)
    assert Wizard(ctx, [spy]).run() == 0
    assert spy.seen is not None and spy.seen["smoke"] is None
    log = "\n".join(f.read_text(encoding="utf-8") for f in ctx.paths.logs.glob("installer-*.jsonl"))
    assert "stale_flags_dropped" in log and "venvs_installed" in log


def test_the_wizard_clears_a_steps_done_state_and_flags_before_its_work_starts(tmp_path: Path, ticking_clock):
    ctx = resolved(tmp_path)
    assert Wizard(ctx, [Spy(ctx)]).run() == 0
    ctx.set_flag("smoke", "passed")
    ctx.set_flag("own", "first")
    assert ctx.state.status("spy") == "done"

    again = resolved(tmp_path)
    spy = Spy(again, clears=("own", "smoke"))
    assert Wizard(again, [spy]).run() == 0
    assert spy.seen == {"smoke": None, "own": None, "status": "pending"}  # nothing says "done" mid-work
    assert again.flag("own") == "again" and again.state.status("spy") == "done"
    assert again.flag("smoke") is None  # the pass is gone for good


def test_steps_that_change_what_the_smoke_test_tests_void_its_pass():
    by_id = {s.id: s for s in default_steps()}
    for sid in (
        "s01_questions",
        "s02_download_bins",
        "s03_download_models",
        "s04_venvs",
        "s05_opencode_prewarm",
        "s06_tune",
        "s07_configs",
        "s08_smoke",
    ):
        assert {"smoke", "smoke_version"} <= set(by_id[sid].clears_on_run), sid
    assert {"venvs_installed", "venvs_version", "venvs_app", "tts_venv_installed"} <= set(
        by_id["s04_venvs"].clears_on_run
    )
    assert "tuned" in by_id["s06_tune"].clears_on_run
    assert "schedule" in by_id["s09_shortcuts_scheduler"].clears_on_run


# ---------------------------------------------------------------------------------------------- s04 (cause 1)
def test_s04_is_not_done_when_the_interpreters_exist_but_site_packages_is_empty(tmp_path: Path):
    """D-15 as found: the flags of a finished install and a killed run's interpreters, no packages."""
    ctx = resolved(tmp_path)
    for name in ("studio", "supervisor"):
        py = ctx.venv_python(name)
        py.parent.mkdir(parents=True)
        py.write_bytes(b"")
        site_packages(ctx, name).mkdir(parents=True)
    for name, value in (
        ("venvs_installed", True),
        ("venvs_version", ctx.manifest.version),
        ("venvs_app", str(ctx.app_path)),
    ):
        ctx.set_flag(name, value)
    assert S04Venvs().check(ctx) is False
    with pytest.raises(NsError) as ei:
        S04Venvs().verify(ctx)
    assert ei.value.code == "NS-E105" and "studio" in ei.value.detail


@pytest.mark.parametrize("hollow", ["studio", "supervisor"])
def test_s04_is_done_only_while_every_venv_loads_the_product(tmp_path: Path, hollow: str):
    ctx = resolved(tmp_path)
    step = S04Venvs()
    step.run(ctx)
    step.verify(ctx)
    assert step.check(ctx) is True
    hollow_out(ctx, hollow)
    assert step.check(ctx) is False  # flags and interpreters are all still there
    with pytest.raises(NsError) as ei:
        step.verify(ctx)
    assert ei.value.code == "NS-E105" and f"the {hollow} environment" in ei.value.detail


def test_s04_checks_the_tts_venv_when_the_fallback_is_flagged(tmp_path: Path):
    ctx = resolved(tmp_path)
    ctx.set_flag("tts_fallback", True)
    step = S04Venvs()
    step.run(ctx)
    assert step.check(ctx) is True
    hollow_out(ctx, "tts")
    assert step.check(ctx) is False
    with pytest.raises(NsError) as ei:
        step.verify(ctx)
    assert ei.value.code == "NS-E108"


def test_the_probe_runs_isolated_and_away_from_the_program_folder(tmp_path: Path):
    """``python -c "import ns_studio"`` in the program folder finds the source there: an empty venv passes."""
    runner = FakeRunner(tmp_path / "desktop")
    ctx = resolved(tmp_path, runner)
    step = S04Venvs()
    step.run(ctx)
    step.verify(ctx)
    probes = runner.called("supervisor import check") + runner.called("studio import check")
    assert probes
    for call in probes:
        assert call.argv[1:3] == ["-I", "-c"]  # isolated: no current folder, no user site-packages
        assert call.cwd is not None and call.cwd != ctx.app_path
    studio = runner.called("studio import check")[0].argv
    assert "import:ns_studio" in studio and "find:spacy" in studio
    assert f"find:{ctx.manifest.spacy_model}" in studio
    assert "import:ns_dashboard" in runner.called("supervisor import check")[0].argv


def test_the_probe_names_what_cannot_be_loaded(monkeypatch: pytest.MonkeyPatch, capsys):
    code = compile(PROBE_CODE, "<probe>", "exec")
    monkeypatch.setattr(sys, "argv", ["-c", "import:json", "find:os"])
    exec(code, {})  # everything there: no exit
    monkeypatch.setattr(sys, "argv", ["-c", "import:json", "import:ns_no_such_module_zz", "find:no_such_zz"])
    with pytest.raises(SystemExit) as ei:
        exec(code, {})
    assert ei.value.code == 1
    err = capsys.readouterr().err
    assert "ns_no_such_module_zz" in err and "no_such_zz" in err and "json" not in err


def test_s04_clears_its_done_state_before_the_work_and_sets_it_after_both_venvs_are_filled(
    tmp_path: Path, ticking_clock
):
    runner = FakeRunner(tmp_path / "desktop")
    ctx = resolved(tmp_path, runner)
    seen: dict[str, object] = {}

    def spy(call):
        on_disk = load_flags(ctx.paths)
        seen["flags"] = [on_disk.get(k) for k in ("venvs_installed", "venvs_version", "venvs_app", "smoke")]
        seen["state"] = load_install_state(ctx.paths).steps["s04_venvs"].status
        return runner._default(call)

    assert Wizard(ctx, [S04Venvs()]).run() == 0  # first install: venvs done
    ctx.set_flag("smoke", "passed")
    shutil.rmtree(ctx.paths.venvs / "studio")  # something removed an environment
    runner.results["uv venv studio"] = spy
    again = resolved(tmp_path, runner)
    assert Wizard(again, [S04Venvs()]).run() == 0
    assert seen == {"flags": [None, None, None, None], "state": "pending"}  # nothing said "done" mid-work
    assert again.flag("venvs_installed") is True and again.flag("smoke") is None
    assert S04Venvs().check(again) is True


def test_a_killed_s04_leaves_no_done_state_behind(tmp_path: Path, ticking_clock):
    class KillSecondVenv(FakeRunner):
        def run(self, argv, **kw):
            if kw.get("tag") == "uv pip install supervisor app":
                raise Killed()
            return super().run(argv, **kw)

    done = resolved(tmp_path)
    assert Wizard(done, [S04Venvs()]).run() == 0
    done.set_flag("smoke", "passed")
    redo = resolved(tmp_path, KillSecondVenv(tmp_path / "desktop"))
    hollow_out(redo, "supervisor")  # forces s04 to work, and the work dies
    with pytest.raises(Killed):
        Wizard(redo, [S04Venvs()]).run()
    assert redo.venv_python("supervisor").exists() and not list(site_packages(redo, "supervisor").iterdir())
    survivor = resolved(tmp_path)
    assert survivor.flag("venvs_installed") is None and survivor.flag("smoke") is None
    assert load_install_state(survivor.paths).steps["s04_venvs"].status == "pending"
    assert S04Venvs().check(survivor) is False


# --------------------------------------------------------------------------------------------- s08 (cause 2c)
def test_s08_runs_again_whenever_s04_redid_its_work_and_not_otherwise(tmp_path: Path, ticking_clock):
    runner = FakeRunner(tmp_path / "desktop")

    def wizard() -> Wizard:
        return Wizard(resolved(tmp_path, runner), [S04Venvs(), S08Smoke()])

    assert wizard().run() == 0
    assert len(runner.called("ns verify --quick")) == 1
    assert wizard().run() == 0
    assert len(runner.called("ns verify --quick")) == 1  # nothing was redone: nothing to test again

    hollow_out(resolved(tmp_path, runner), "supervisor")  # the environments are redone ...
    assert wizard().run() == 0
    assert len(runner.called("ns verify --quick")) == 2  # ... so the smoke test runs again

    assert wizard().run() == 0
    assert len(runner.called("ns verify --quick")) == 2


# ---------------------------------------------------------------------------------------------- the product
def venv_loads_product(home: Path, name: str) -> bool:
    """Ask the (simulated) venv the question the real probe asks."""
    python = home / "venvs" / name / "Scripts" / "python.exe"
    probe = [str(python), "-I", "-c", PROBE_CODE, "import:ns_common", "import:ns_supervisor"]
    return FakeRunner(home).run(probe, tag=f"{name} import check").ok


def killing_runner(monkeypatch: pytest.MonkeyPatch, tag: str) -> None:
    class KillingRunner(FakeRunner):
        def run(self, argv, **kw):
            if kw.get("tag") == tag:
                raise Killed(tag)
            return super().run(argv, **kw)

    monkeypatch.setattr("installer.stage2.wizard.FakeRunner", KillingRunner)


def uninstall_like_0_1_12(home: Path) -> None:
    """The first uninstaller, keeping data: venvs, bin, run, state and install state go; the flags stay."""
    for folder in ("venvs", "bin", "run", "state", "tmp"):
        shutil.rmtree(home / folder, ignore_errors=True)
    for leaf in ("install_state.json", "install_state.json.bak", "secrets.dpapi"):
        (home / "config" / leaf).unlink(missing_ok=True)
    flags = load_flags(Paths(home))
    flags.pop(INSTALL_KEY, None)  # 0.1.12 wrote its flags without a stamp
    save_flags(Paths(home), flags)


def uninstall_with_the_script(home: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "NIGHTSHIFT_HOME"}
    env["NS_NONINTERACTIVE"] = "1"
    cmd = [str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(UNINSTALL)]
    cmd += ["-HomeDir", str(home), "-SkipSystem"]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def killed_then_rerun(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, answers: Path, uninstall) -> list[str]:
    """Install, uninstall keeping data, install again but die 8 s into s04, install a third time.

    Returns the steps the third run started. Every assertion on the way is about the effect.
    """
    home = tmp_path / "home"
    assert main(args(tmp_path, answers)) == 0
    assert venv_loads_product(home, "studio") and venv_loads_product(home, "supervisor")
    assert load_flags(Paths(home))["venvs_installed"] is True

    uninstall(home)
    assert not (home / "venvs").exists() and (home / "config" / "answers.json").is_file()
    (home / "logs" / "verify-report.json").unlink(missing_ok=True)  # the earlier install's smoke report

    with monkeypatch.context() as m:
        killing_runner(m, "uv pip install supervisor app")
        with pytest.raises(Killed):
            main(args(tmp_path, answers))
    # the state D-15 found: interpreters, the studio venv filled, the supervisor venv empty
    assert (home / "venvs" / "supervisor" / "Scripts" / "python.exe").exists()
    assert not list((home / "venvs" / "supervisor" / SITE_PACKAGES).iterdir())
    assert venv_loads_product(home, "studio") and not venv_loads_product(home, "supervisor")
    assert not (home / "logs" / "verify-report.json").exists()

    before = len(started_steps(home))
    assert main(args(tmp_path, answers)) == 0
    return started_steps(home)[before:]


def assert_the_install_really_finished(home: Path, started: list[str]) -> None:
    assert "s04_venvs" in started and "s08_smoke" in started
    assert started.index("s04_venvs") < started.index("s08_smoke")
    assert venv_loads_product(home, "studio") and venv_loads_product(home, "supervisor")
    report = home / "logs" / "verify-report.json"
    assert report.is_file(), "the smoke test did not run after the venvs were redone"
    flags = load_flags(Paths(home))
    assert flags["smoke"] == "passed" and flags["venvs_installed"] is True
    state = load_install_state(Paths(home))
    assert [s for s in STEP_IDS if state.steps[s].status == "done"] == STEP_IDS and state.completed_at


def test_a_killed_s04_after_an_old_uninstall_is_redone_and_smoke_tested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ticking_clock
):
    """The uninstaller of 0.1.12 kept wizard_flags.json: venvs_installed and smoke: passed survived."""
    answers = write_answers(tmp_path)
    started = killed_then_rerun(tmp_path, monkeypatch, answers, uninstall_like_0_1_12)
    assert_the_install_really_finished(tmp_path / "home", started)
    assert "s01_questions" not in started  # the same answers file: still answered


@pytest.mark.skipif(PS is None, reason="PowerShell not available")
def test_a_killed_s04_after_the_real_uninstall_script_is_redone_and_smoke_tested(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ticking_clock
):
    answers = write_answers(tmp_path)
    home = tmp_path / "home"

    def uninstall(h: Path) -> None:
        assert load_flags(Paths(h))["smoke"] == "passed"
        uninstall_with_the_script(h)
        assert not (h / "config" / "wizard_flags.json").exists()  # progress flags go with the install
        assert (h / "config" / "answers.json").is_file()  # the owner's choices stay

    started = killed_then_rerun(tmp_path, monkeypatch, answers, uninstall)
    assert_the_install_really_finished(home, started)


def test_an_uninstall_that_left_the_flags_does_not_make_a_clean_reinstall_skip_s04_or_s08(
    tmp_path: Path, ticking_clock
):
    answers = write_answers(tmp_path)
    home = tmp_path / "home"
    assert main(args(tmp_path, answers)) == 0
    uninstall_like_0_1_12(home)
    survived = load_flags(Paths(home))
    assert survived["venvs_installed"] is True and survived["smoke"] == "passed"
    before = len(started_steps(home))
    assert main(args(tmp_path, answers)) == 0
    later = started_steps(home)[before:]
    assert "s04_venvs" in later and "s08_smoke" in later
    assert venv_loads_product(home, "studio") and venv_loads_product(home, "supervisor")


# ---------------------------------------------------------------------------------------------- the audit
def test_s05_is_not_done_when_the_program_it_ran_is_gone(tmp_path: Path):
    """The flag says OpenCode ran; the effect is the program. An uninstall, or a scanner, can remove it."""
    from installer.stage2.steps.s05_opencode_prewarm import S05OpencodePrewarm

    ctx = resolved(tmp_path)
    exe = ctx.paths.bin_opencode / "opencode.exe"
    exe.write_bytes(b"")
    step = S05OpencodePrewarm()
    step.run(ctx)
    assert step.check(ctx) is True
    exe.unlink()
    assert step.check(ctx) is False
