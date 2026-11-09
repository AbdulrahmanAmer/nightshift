"""C1: the program lives in ``<home>\\app``, not in the folder the zip was extracted to.

Before, every venv was an editable install of the extracted folder and the shortcuts, the nightly task and
``ns update`` all pointed at it. Extracting to ``D:\\NightShift`` and choosing the default home
``D:\\Nightshift`` was the same folder (NTFS ignores case), so the first ``ns update`` tried to rename
the folder that holds the venvs, the models and the logs; extracting into Downloads or OneDrive left a
program that the owner tidying up, or a sync lock, broke. Everything here uses a fake home in ``tmp_path``.
"""

from __future__ import annotations

import errno
import json
import shutil
from collections.abc import Sequence
from pathlib import Path

import pytest

from installer.stage2.web import appcopy, bootstrap, pathcheck
from installer.stage2.web.bootstrap import Boot, BootstrapError, CmdResult
from ns_common.avblock import WINERROR_VIRUS
from tests.test_uv_shim import UvCmd

ROOT = Path(__file__).resolve().parents[1]


def make_source(root: Path, version: str = "0.1.4", **extra: str) -> Path:
    """A folder shaped like the extracted zip (plus junk that must not be copied)."""
    files = {
        "INSTALL.cmd": "@echo off", "VERSION": version + "\n",
        "manifest.json": json.dumps({"version": version}),
        "pyproject.toml": "[project]\nname='x'", "installer/stage1.ps1": "# ps", "ns_common/__init__.py": "",
        "bin/webview2/WebView2Loader.dll": "MZ", "bin/llama/llama-server.exe": "MZ",
        "venvs/studio/Scripts/python.exe": "MZ", "models/llm/big.gguf": "x" * 10,
        "node_modules/pkg/index.js": "x", "ns_common/__pycache__/a.pyc": "x", ".git/HEAD": "ref: main",
        "launchers/nightshift-dashboard.ps1": "# ps", **extra,
    }
    for rel, text in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text, encoding="utf-8")
    return root


def tree(folder: Path) -> set[str]:
    return {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}


# ---- the copy -------------------------------------------------------------------------------------
def test_a_fresh_install_copies_the_program_without_venvs_models_or_other_bin(tmp_path: Path) -> None:
    src, home = make_source(tmp_path / "Downloads-ish" / "Nightshift-Setup-0.1.4"), tmp_path / "home"
    res = appcopy.install_app(src, home)
    assert res.action == "copied" and res.new_version == "0.1.4"
    got = tree(home / "app")
    assert {"INSTALL.cmd", "VERSION", "manifest.json", "installer/stage1.ps1", ".git/HEAD",
            "bin/webview2/WebView2Loader.dll", appcopy.MARKER} <= got
    assert not any(p.startswith(("venvs/", "models/", "node_modules/")) for p in got)
    assert "bin/llama/llama-server.exe" not in got and not any("__pycache__" in p for p in got)
    assert not (home / "app.new").exists()                                # the staging folder is gone
    assert appcopy.installed_version(home) == "0.1.4" and appcopy.is_current(src, home)


def test_the_install_does_not_depend_on_the_extracted_folder_afterwards(tmp_path: Path) -> None:
    src, home = make_source(tmp_path / "extracted"), tmp_path / "home"
    appcopy.install_app(src, home)
    shutil.rmtree(src)                                                   # the owner tidies Downloads
    assert (home / "app" / "installer" / "stage1.ps1").is_file()
    assert appcopy.installed_version(home) == "0.1.4"


def test_a_changed_version_or_manifest_makes_the_copy_stale(tmp_path: Path) -> None:
    src, home = make_source(tmp_path / "src"), tmp_path / "home"
    appcopy.install_app(src, home)
    assert appcopy.is_current(src, home)
    (src / "VERSION").write_text("0.1.5\n", encoding="utf-8")
    assert not appcopy.is_current(src, home)
    (src / "VERSION").write_text("0.1.4\n", encoding="utf-8")
    (src / "manifest.json").write_text('{"version": "0.1.4", "bins": {}}', encoding="utf-8")
    assert not appcopy.is_current(src, home)                              # a new pin counts as a new program


def test_a_crash_in_the_middle_leaves_nothing_that_looks_finished(tmp_path: Path) -> None:
    src, home = make_source(tmp_path / "src"), tmp_path / "home"
    (home / "app").mkdir(parents=True)                                    # half a program: no marker
    (home / "app" / "VERSION").write_text("0.1.4\n", encoding="utf-8")
    (home / "app.new").mkdir()                                            # and a half staging folder
    (home / "app.new" / "junk.txt").write_text("x", encoding="utf-8")
    assert appcopy.installed_version(home) is None and not appcopy.is_current(src, home)
    res = appcopy.install_app(src, home)
    assert res.action == "copied" and appcopy.is_current(src, home)
    assert not (home / "app.new").exists() and not (home / "app" / "junk.txt").exists()


def test_the_proof_runner_and_the_proofs_are_part_of_the_installed_program(tmp_path: Path) -> None:
    """H-13: `dev/prove.py --home <install>` runs from the install's app folder, proofs/ beside dev/."""
    src = make_source(
        tmp_path / "Nightshift-Setup-0.1.4",
        **{"dev/prove.py": "# runner", "dev/prove_ctx.py": "# ctx", "proofs/__init__.py": "",
           "proofs/f_b18_studio_status.py": "# proof"},
    )
    appcopy.install_app(src, tmp_path / "home")
    got = tree(tmp_path / "home" / "app")
    assert {"dev/prove.py", "dev/prove_ctx.py", "proofs/__init__.py", "proofs/f_b18_studio_status.py"} <= got
    assert {"dev", "proofs"} <= appcopy.ROOT_ENTRIES  # also when the zip is extracted into a drive root


def test_running_setup_from_the_installed_program_folder_copies_nothing(tmp_path: Path) -> None:
    home = tmp_path / "home"
    make_source(home / "app")
    res = appcopy.install_app(home / "app", home)
    assert res.action == "in-place" and appcopy.is_current(home / "app", home)
    assert not (home / "app.new").exists() and not (home / "app.prev").exists()


# ---- the update over an existing install ----------------------------------------------------------
def test_a_newer_zip_over_an_existing_install_does_the_update_swap(tmp_path: Path) -> None:
    home = tmp_path / "home"
    appcopy.install_app(make_source(tmp_path / "old", "0.1.3"), home)
    (home / "app" / "config").mkdir()
    (home / "app" / "config" / "c.json").write_text('{"keep": true}', encoding="utf-8")
    (home / "app" / "answers.json").write_text('{"a": 1}', encoding="utf-8")
    new = make_source(tmp_path / "new", "0.1.4")
    order: list[str] = []
    res = appcopy.install_app(new, home, stop_engine=lambda: order.append("stop"), say=order.append)
    assert res.action == "updated" and (res.old_version, res.new_version) == ("0.1.3", "0.1.4")
    app = home / "app"
    assert order == [
        f"Copying the program files to {app}",
        f"Updating the Nightshift in {app} from 0.1.3 to 0.1.4",
        "stop",
    ]
    assert appcopy.read_version(home / "app") == "0.1.4" and appcopy.is_current(new, home)
    assert appcopy.read_version(home / "app.prev") == "0.1.3"             # the rollback target of ns update
    assert json.loads((home / "app" / "config" / "c.json").read_text("utf-8")) == {"keep": True}
    assert (home / "app" / "answers.json").read_text("utf-8") == '{"a": 1}'
    assert sorted(res.kept) == ["answers.json", "config"]


def test_the_answers_file_next_to_install_cmd_is_not_copied_into_the_program(tmp_path: Path) -> None:
    home = tmp_path / "home"
    secret = '{"pexels_api_key": "SECRET"}'
    appcopy.install_app(make_source(tmp_path / "src", **{"answers.json": secret}), home)
    assert not (home / "app" / "answers.json").exists()                  # its keys go to the keyring, once


def test_a_failing_swap_puts_the_old_program_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    appcopy.install_app(make_source(tmp_path / "old", "0.1.3"), home)
    real = appcopy._rename

    def refuse_the_last_step(src: Path, dst: Path, sleep: object) -> None:
        if src.name == "app.new":
            raise OSError("in use by another program")
        real(src, dst, sleep)  # type: ignore[arg-type]

    monkeypatch.setattr(appcopy, "_rename", refuse_the_last_step)
    with pytest.raises(appcopy.AppCopyError) as ei:
        appcopy.install_app(make_source(tmp_path / "new", "0.1.4"), home)
    assert ei.value.code == "NS-E115"
    assert appcopy.read_version(home / "app") == "0.1.3" and appcopy.installed_version(home) == "0.1.3"
    assert not (home / "app.new").exists()


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (OSError(errno.ENOSPC, "No space left on device"), "NS-E104"),
        (OSError(22, "the file contains a virus or potentially unwanted software"), "NS-E113"),
        (PermissionError(13, "Access is denied"), "NS-E115"),
    ],
)
def test_copy_failures_have_their_own_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: OSError, code: str
) -> None:
    if code == "NS-E113":
        error.winerror = WINERROR_VIRUS  # type: ignore[attr-defined]

    def fail(source: Path, target: Path) -> None:
        raise error

    monkeypatch.setattr(appcopy, "copy_tree", fail)
    with pytest.raises(appcopy.AppCopyError) as ei:
        appcopy.install_app(make_source(tmp_path / "src"), tmp_path / "home")
    assert ei.value.code == code and not (tmp_path / "home" / "app").exists()


def test_overlapping_folders_are_refused_before_anything_is_copied(tmp_path: Path) -> None:
    src = make_source(tmp_path / "src")
    with pytest.raises(appcopy.AppCopyError) as ei:
        appcopy.install_app(src, src / "home")                     # the app folder would be inside the source
    assert ei.value.code == "NS-E115" and not (src / "home").exists()
    home = tmp_path / "home"
    with pytest.raises(appcopy.AppCopyError):
        appcopy.install_app(home / "app" / "extracted-here", home)  # the source inside the destination


# ---- where the install folder may be --------------------------------------------------------------
BOB = {"USERPROFILE": "C:\\Users\\Bob", "OneDrive": "C:\\Users\\Bob\\OneDrive"}


@pytest.mark.parametrize(
    ("home", "app", "words"),
    [
        ("D:\\Nightshift", "D:\\NightShift", "extracted"),               # NTFS: the same folder (audit #2)
        ("d:\\nightshift\\", "D:\\NightShift", "extracted"),
        ("D:\\NightShift\\data", "D:\\NightShift", "inside"),
        ("C:\\Users\\Bob\\Downloads\\Nightshift", "D:\\setup", "Downloads"),
        ("C:\\Users\\Bob\\Downloads", "D:\\setup", "Downloads"),
        ("C:\\Users\\Bob\\OneDrive\\Desktop\\Nightshift", "D:\\setup", "OneDrive"),
        ("E:\\OneDrive - Contoso\\Nightshift", "D:\\setup", "OneDrive"),
        ("C:\\Users\\Bob\\Downloads\\a\\b", "D:\\setup", "Downloads"),
    ],
)
def test_homes_that_clash_with_the_extracted_folder_or_live_in_downloads_or_onedrive(
    home: str, app: str, words: str
) -> None:
    message = pathcheck.layout_problem(home, app, BOB)
    assert message is not None and words in message


@pytest.mark.parametrize(
    ("home", "app"),
    [
        ("D:\\Nightshift", "D:\\setup\\Nightshift-Setup-0.1.4"),
        ("D:\\Nightshift-home", "D:\\NightShift"),                      # a longer name is not "inside"
        ("D:\\NightShift2", "D:\\NightShift"),
        ("C:\\Users\\Bob\\Nightshift", "C:\\Users\\Bob\\Downloads\\Nightshift-Setup"),
        ("C:\\Users\\Bob\\Downloadsx\\ns", "D:\\setup"),
        ("D:\\Nightshift", None),
    ],
)
def test_homes_that_are_fine(home: str, app: str | None) -> None:
    assert pathcheck.layout_problem(home, app, BOB) is None


def test_the_default_home_in_stage1_avoids_the_folder_the_zip_was_extracted_to() -> None:
    text = (ROOT / "installer" / "stage1.ps1").read_text(encoding="utf-8")
    assert "-AppFolder $App" in text and "Nightshift-home" in text
    assert text.index("function Test-PathInside") < text.index("function Select-DefaultHome")


# ---- a zip extracted straight into a drive root ---------------------------------------------------
@pytest.mark.parametrize(
    ("home", "ok"),
    [(r"D:\Nightshift", True), (r"D:\Nightshift-home", True), (r"D:\Data\ns", True), (r"E:\Nightshift", True),
     ("D:" + chr(92), False), ("D:", False), ("d:" + chr(92), False)],
)
def test_a_drive_root_only_refuses_the_root_itself(home: str, ok: bool) -> None:
    for root in ("D:" + chr(92), "D:"):
        message = pathcheck.layout_problem(home, root, BOB)
        assert (message is None) is ok, (home, root, message)


def test_a_folder_that_is_not_a_root_still_refuses_everything_inside_it() -> None:
    assert pathcheck.layout_problem(r"D:\NightShift\data", r"D:\NightShift", BOB) is not None
    assert pathcheck.layout_problem(r"D:\Nightshift", r"D:\setup", BOB) is None


def test_is_drive_root() -> None:
    assert all(appcopy.is_drive_root(p) for p in ("D:" + chr(92), "D:", "d:/", Path("C:" + chr(92))))
    assert not any(appcopy.is_drive_root(p) for p in (r"D:\Nightshift", "", "relative"))


def test_copying_from_a_drive_root_takes_only_the_bundle_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = tmp_path / "D"
    make_source(drive)
    (drive / "Games" / "save").mkdir(parents=True)
    (drive / "Games" / "save" / "big.dat").write_text("x", encoding="utf-8")
    (drive / "$RECYCLE.BIN").mkdir()
    (drive / "holiday.mp4").write_text("x", encoding="utf-8")
    home = drive / "Nightshift"                                   # the default home sits inside the root
    (home / "logs").mkdir(parents=True)
    monkeypatch.setattr(appcopy, "is_drive_root", lambda p: Path(p) == drive)
    res = appcopy.install_app(drive, home)
    assert res.action == "copied" and res.new_version == "0.1.4"
    got = tree(home / "app")
    assert {"INSTALL.cmd", "VERSION", "installer/stage1.ps1", "bin/webview2/WebView2Loader.dll"} <= got
    assert appcopy.MARKER in got
    assert not any(p.startswith(("Games/", "$RECYCLE.BIN/", "Nightshift/")) for p in got)
    assert "holiday.mp4" not in got and not any(p.startswith(("venvs/", "models/")) for p in got)


def test_a_home_inside_a_normal_extracted_folder_is_still_an_overlap(tmp_path: Path) -> None:
    src = make_source(tmp_path / "Nightshift-Setup-0.1.4")
    with pytest.raises(appcopy.AppCopyError) as ei:
        appcopy.install_app(src, src / "home")
    assert ei.value.code == "NS-E115" and "overlap" in ei.value.detail


def test_the_root_entry_list_covers_every_top_level_entry_of_the_repository() -> None:
    import shutil
    import subprocess

    git = shutil.which("git")
    if git is None:
        pytest.skip("git not available")
    listing = subprocess.run([git, "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True)
    out = listing.stdout
    top = {line.split("/")[0].lower() for line in out.splitlines() if line}
    missing = top - appcopy.ROOT_ENTRIES - {".github"}              # .github is not part of the bundle
    assert not missing, f"add to appcopy.ROOT_ENTRIES: {sorted(missing)}"


# ---- b02 and b03 ----------------------------------------------------------------------------------
def make_boot(tmp_path: Path, source: Path, *, home: Path | None = None) -> Boot:
    var: dict[str, str] = {}
    where = home or tmp_path / "home"
    uv = tmp_path / "boot" / "uv.exe"
    uv.parent.mkdir(exist_ok=True)
    uv.write_bytes(b"MZ REAL")
    cmd = UvCmd()
    boot = Boot(
        app_path=source, home=where, uv=str(uv), cmd=cmd,
        set_home_var=lambda n, v: var.update({n: v}) or True, get_home_var=var.get,
        write_answers=lambda: (where / "config" / "answers.json").write_text("{}", encoding="utf-8"),
        install_app=True,
    )
    boot.cmd_log = cmd                                                    # type: ignore[attr-defined]
    return boot


@pytest.fixture(autouse=True)
def home_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NIGHTSHIFT_HOME", "placeholder")


def run_b02(boot: Boot) -> None:
    step = bootstrap.B02Home()
    assert not step.check(boot)
    step.run(boot)
    step.verify(boot)


def test_b02_copies_the_program_and_later_steps_run_from_the_copy(tmp_path: Path) -> None:
    src = make_source(tmp_path / "extracted")
    boot = make_boot(tmp_path, src)
    assert boot.run_app == boot.home / "app" and boot.app_path == src
    run_b02(boot)
    assert (boot.home / "app" / "VERSION").is_file() and (boot.home / "bin" / "uv" / "uv.exe").is_file()
    assert bootstrap.B02Home().check(boot)                                # a second look finds nothing to do


def test_b02_does_nothing_about_the_program_when_the_copy_is_switched_off(tmp_path: Path) -> None:
    src = make_source(tmp_path / "extracted")
    boot = make_boot(tmp_path, src)
    boot.install_app = False
    run_b02(boot)
    assert not (boot.home / "app").exists() and boot.run_app == src


def test_b02_refuses_the_extracted_folder_as_the_install_folder(tmp_path: Path) -> None:
    src = make_source(tmp_path / "NightShift")
    boot = make_boot(tmp_path, src, home=src)
    with pytest.raises(BootstrapError) as ei:
        bootstrap.B02Home().run(boot)
    assert ei.value.code == "NS-E115" and "extracted" in ei.value.detail
    assert not (src / "app").exists()


@pytest.mark.parametrize("marker", ["config/install_state.json", "config/runtime.json"])
def test_b02_repairs_an_install_made_at_the_old_default_folder_with_the_zip_inside_it(
    tmp_path: Path, marker: str
) -> None:
    """Review round 1 of PR 136, finding 6: OPERATOR.md used to say to extract under D:\\NightShift and the
    old default home was D:\\Nightshift (the same folder on NTFS). Running INSTALL.cmd again over that
    install, or over the data it kept, must copy the program into <home>\\app, not stop with NS-E115."""
    home = tmp_path / "Nightshift"
    (home / marker).parent.mkdir(parents=True)
    (home / marker).write_text("{}", encoding="utf-8")
    src = make_source(home / "nightshift-0.1.12")
    boot = make_boot(tmp_path, src, home=home)
    run_b02(boot)
    assert (home / "app" / "VERSION").is_file() and (src / "VERSION").is_file()  # the zip folder is untouched


def test_b02_still_refuses_a_home_with_other_files_that_holds_the_zip(tmp_path: Path) -> None:
    home = tmp_path / "Documents"
    home.mkdir()
    (home / "notes.txt").write_text("mine", encoding="utf-8")
    src = make_source(home / "nightshift-0.1.12")
    boot = make_boot(tmp_path, src, home=home)
    with pytest.raises(BootstrapError) as ei:
        bootstrap.B02Home().run(boot)
    assert ei.value.code == "NS-E115" and "inside it" in ei.value.detail
    assert not (home / "app").exists()


def test_b02_refuses_a_home_in_downloads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "Bob"))
    src = make_source(tmp_path / "setup")
    boot = make_boot(tmp_path, src, home=tmp_path / "Bob" / "Downloads" / "ns")
    with pytest.raises(BootstrapError) as ei:
        bootstrap.B02Home().run(boot)
    assert ei.value.code == "NS-E115" and "Downloads" in ei.value.detail


def test_b02_reports_a_copy_failure_with_the_code_of_the_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def full(source: Path, target: Path) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(appcopy, "copy_tree", full)
    boot = make_boot(tmp_path, make_source(tmp_path / "extracted"))
    with pytest.raises(BootstrapError) as ei:
        bootstrap.B02Home().run(boot)
    assert ei.value.code == "NS-E104"


def test_b02_updates_an_install_that_is_already_there_and_stops_its_engine_first(tmp_path: Path) -> None:
    home = tmp_path / "home"
    appcopy.install_app(make_source(tmp_path / "old", "0.1.3"), home)
    ns_exe = home / "venvs" / "supervisor" / "Scripts" / "ns.exe"
    ns_exe.parent.mkdir(parents=True)
    ns_exe.write_bytes(b"MZ")
    boot = make_boot(tmp_path, make_source(tmp_path / "new", "0.1.4"), home=home)
    said: list[str] = []
    boot.say = said.append
    assert not bootstrap.B02Home().check(boot)                            # 0.1.3 is not 0.1.4
    run_b02(boot)
    assert [str(ns_exe), "stop"] in boot.cmd_log.calls                    # type: ignore[attr-defined]
    assert appcopy.read_version(home / "app") == "0.1.4"
    assert appcopy.read_version(home / "app.prev") == "0.1.3"
    assert any("Updated the install: 0.1.3 -> 0.1.4" in line for line in said)


def test_b03_counts_a_new_version_in_the_same_program_folder_as_stale(tmp_path: Path) -> None:
    src = make_source(tmp_path / "extracted")
    boot = make_boot(tmp_path, src)
    run_b02(boot)
    boot.venv_python.parent.mkdir(parents=True)
    boot.venv_python.write_bytes(b"MZ")
    step = bootstrap.B03SetupEnv()
    assert not step.check(boot)                                           # no marker yet
    boot.venv_marker.write_text(boot.venv_stamp(), encoding="utf-8")
    assert step.check(boot)
    (boot.home / "app" / "VERSION").write_text("0.1.5\n", encoding="utf-8")
    assert not step.check(boot)                                           # a re-run refreshes the packages


def test_b03_installs_from_the_copy_not_from_the_extracted_folder(tmp_path: Path) -> None:
    src = make_source(tmp_path / "extracted")
    boot = make_boot(tmp_path, src)
    run_b02(boot)
    seen: list[tuple[list[str], object]] = []

    def cmd(argv: Sequence[str], env: object = None, cwd: object = None, timeout: object = None) -> CmdResult:
        seen.append(([str(a) for a in argv], cwd))
        if len(seen[-1][0]) > 1 and seen[-1][0][1] == "venv":
            boot.venv_python.parent.mkdir(parents=True, exist_ok=True)
            boot.venv_python.write_bytes(b"MZ")
        return CmdResult(0, "", "")

    boot.cmd = cmd
    bootstrap.B03SetupEnv().run(boot)
    installs = [argv for argv, _cwd in seen if "pip" in argv]
    assert installs and installs[0][-1] == f"{boot.home / 'app'}[studio]"
    assert all(cwd == boot.home / "app" for _argv, cwd in seen)
    assert boot.venv_marker.read_text("utf-8").startswith(str(boot.home / "app"))


# ---- the setup controller ----------------------------------------------------------------------------
def fake_zip_folder(root: Path, version: str = "0.1.4") -> Path:
    """An extracted bundle the controller can read: the real manifest, a VERSION, the launchers it names."""
    root.mkdir(parents=True)
    manifest = (ROOT / "manifest.json").read_text(encoding="utf-8")
    (root / "manifest.json").write_text(manifest, encoding="utf-8")
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (root / "launchers").mkdir()
    (root / "launchers" / "nightshift-dashboard.ps1").write_text("# ps", encoding="utf-8")
    return root


def app_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extracted: Path, script: object = None,
    answers: Path | None = None,
):
    from installer.stage2.web.controller import Config, SetupController
    from tests.test_installer_web_controller import Setup

    s = Setup(tmp_path, monkeypatch, script=script)
    s.cfg = Config(
        app_path=extracted, data_dir=tmp_path / "data-app", simulate=True, home=s.home, copy_app=True,
        answers=answers,
    )
    s.ctl = SetupController(s.cfg, s.fx)
    return s


def test_a_whole_run_puts_the_program_under_home_and_hands_that_folder_to_the_wizard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_installer_web_controller import finish_script

    extracted = fake_zip_folder(tmp_path / "Nightshift-Setup-0.1.4")
    s = app_setup(tmp_path, monkeypatch, extracted, finish_script)
    assert s.ctl.run_app_path == extracted                                  # nothing copied yet
    s.accept()
    s.ctl.do_action("start")
    state = s.until(lambda st: st["run"] == "finished", what="the run to finish")
    app = s.home / "app"
    assert appcopy.installed_version(s.home) == "0.1.4" and s.ctl.run_app_path == app
    argv = s.engine.argv                                                    # type: ignore[union-attr]
    assert argv[argv.index("--app-path") + 1] == str(app)                   # the wizard runs from the copy
    recorded = json.loads((s.home / "config" / "install_state.json").read_text(encoding="utf-8"))
    assert recorded["app_path"] == str(app)         # what ns update, the task and the shortcuts read
    assert state["appPath"] == str(app) and state["result"]["appPath"] == str(app)
    pips = [c for c in s.cmd.calls if len(c) > 2 and c[1:3] == ["pip", "install"]]
    assert pips and pips[0][-1] == f"{app}[studio]"
    shutil.rmtree(extracted)                                                # delete the folder from Downloads
    assert (app / "VERSION").is_file() and s.ctl.run_app_path == app


def test_an_install_folder_that_is_the_extracted_folder_is_refused_in_the_choices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_installer_web_controller import draft

    extracted = fake_zip_folder(tmp_path / "NightShift")
    s = app_setup(tmp_path, monkeypatch, extracted)
    result = s.ctl.submit_answers(draft(extracted))
    assert result["ok"] is False
    assert any(e["field"] == "homePath" and "extracted" in e["message"] for e in result["errors"])
    inside = s.ctl.submit_answers(draft(extracted / "data"))
    assert inside["ok"] is False and "inside" in inside["errors"][0]["message"]
    assert s.ctl.submit_answers(draft(s.home))["ok"] is True                # a folder of its own is fine


def test_the_folder_check_names_an_install_that_setup_would_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    extracted = fake_zip_folder(tmp_path / "new", "0.1.4")
    s = app_setup(tmp_path, monkeypatch, extracted)
    appcopy.install_app(fake_zip_folder(tmp_path / "old", "0.1.3"), s.home)
    note = s.ctl._existing_install_note(str(s.home))
    assert note is not None and "0.1.3 is installed here" in note and "updates it to 0.1.4" in note
    appcopy.install_app(extracted, s.home)
    note = s.ctl._existing_install_note(str(s.home))
    assert note is not None and "already installed here" in note
    assert s.ctl._existing_install_note(str(tmp_path / "empty")) is None
    assert "extracted" in (s.ctl._layout_problem(str(extracted)) or "")


def test_the_disk_check_carries_the_layout_problem_and_the_existing_install_note() -> None:
    from installer.stage2.web import preflight

    drives = [{"letter": "D", "freeGb": 500.0, "type": "SSD"}]
    HOME = "D:/Nightshift".replace("/", chr(92))

    def check(**kw: object) -> dict:
        return preflight.disk_check(HOME, "small", drives, lambda p: 500.0, **kw)  # type: ignore[arg-type]

    clash = check(layout_fn=lambda home: "That is the folder you extracted Nightshift to.")
    assert clash["ok"] is False and "extracted" in clash["message"]
    update = check(existing_fn=lambda home: "Nightshift 0.1.3 is installed here. Setup updates it to 0.1.4")
    assert update["ok"] is True and update["message"].startswith("500 GB free on D:")
    assert "installed here" in update["message"]
    assert check()["ok"] is True and "installed" not in check()["message"]


def test_simulate_mode_never_copies_the_program(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_installer_web_controller import Setup

    s = Setup(tmp_path, monkeypatch)                     # simulate=True, copy_app unset
    assert s.ctl.installs_app is False and s.ctl.run_app_path == s.cfg.app_path
    assert s.ctl._layout_problem(str(s.cfg.app_path)) is None


def test_the_keys_in_the_answers_file_next_to_install_cmd_are_removed_once_they_are_held_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from installer.stage2.web import answers_view
    from tests.test_installer_web_controller import KEY_P, KEY_X, draft, finish_script

    extracted = fake_zip_folder(tmp_path / "Nightshift-Setup-0.1.4")
    source = extracted / "answers.json"
    raw = answers_view.draft_to_answers(draft(tmp_path / "home"))
    raw["pexels_api_key"], raw["pixabay_api_key"] = KEY_P, KEY_X
    source.write_text(json.dumps(raw), encoding="utf-8")
    s = app_setup(tmp_path, monkeypatch, extracted, finish_script, answers=source)
    s.accept()
    s.ctl.do_action("start")
    s.until(lambda st: st["run"] == "finished", what="the run to finish")
    left = json.loads(source.read_text(encoding="utf-8"))
    assert left["pexels_api_key"] == "" and left["pixabay_api_key"] == ""
    assert left["channel"] == raw["channel"]                               # everything else is as it was
    assert not (s.home / "app" / "answers.json").exists()                  # and the program copy never had it
    assert s.ctl._keys["pexels_api_key"] == KEY_P                          # the keyring still gets them
