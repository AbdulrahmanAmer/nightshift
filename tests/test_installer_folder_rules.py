"""C1: what is already in the install folder (defects D-21 and D-22).

One rule (``pathcheck.folder_kind``) serves Browse, a typed path and the default suggestion:

* D-22: the folder an uninstall left with the owner's settings and data is a Nightshift home. Browse used to
  append ``\\Nightshift`` to it because the only marker was ``config\\install_state.json``, which the
  uninstaller deletes, so a reinstall made a second, empty install beside the kept one.
* D-21: the default folder ``<drive>:\\Nightshift`` matched a folder that was not empty and not Nightshift's
  (``D:\\NightShift`` is the same folder on NTFS), and setup installed into it without a word. Now a foreign
  folder gets a warning the window can show, is never the default, and a folder that has the extracted
  program inside it is refused.

The folders here are real folders in ``tmp_path``; the uninstall test runs the real ``uninstall.ps1``.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from installer.stage2 import sysinfo
from installer.stage2.steps.s01_questions import S01Questions
from installer.stage2.testing import FakeUi, example_answers, make_test_context
from installer.stage2.web import controller, folderdialog, pathcheck, preflight
from installer.stage2.web.controller import Config, SetupController
from installer.stage2.web.fakes import fake_effects
from installer.stage2.wizard import main
from tests.test_install_done_means_done import ticking_clock, uninstall_with_the_script  # noqa: F401
from tests.test_wizard_e2e import args, write_answers

ROOT = Path(__file__).resolve().parents[1]
STAGE1 = ROOT / "installer" / "stage1.ps1"
PS = shutil.which("powershell") or shutil.which("pwsh")
BOB = {"USERPROFILE": "C:\\Users\\Bob"}


pytestmark = pytest.mark.usefixtures("ticking_clock")  # two installs in one test get two ids


@pytest.fixture(autouse=True)
def _no_real_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NIGHTSHIFT_HOME", raising=False)


def folder(base: Path, name: str, *files: str) -> Path:
    """A folder with the given files (a path with a slash makes the folders in between)."""
    where = base / name
    where.mkdir(parents=True, exist_ok=True)
    for rel in files:
        f = where / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("x", encoding="utf-8")
    return where


# ---- the rule --------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("files", "kind"),
    [
        ((), "empty"),
        (("notes.txt",), "foreign"),
        (("repo/.git/HEAD", "reports/a.md"), "foreign"),
        (("config/install_state.json",), "home"),
        (("app/VERSION", "bin/uv/uv.exe"), "home"),
        (("config/answers.json",), "kept"),  # what the uninstaller leaves
        (("config/runtime.json", "logs/a.jsonl"), "kept"),
        (("data/studio.db", "outbox/a.mp4"), "kept"),
        (("config/Runtime.JSON",), "kept"),  # NTFS ignores case
        (("config/notes.txt", "bin/x.exe"), "foreign"),  # a config folder alone proves nothing
    ],
)
def test_folder_kind_tells_empty_nightshift_kept_and_foreign_folders_apart(
    tmp_path: Path, files: tuple[str, ...], kind: str
) -> None:
    assert pathcheck.folder_kind(str(folder(tmp_path, "pick", *files))) == kind


def test_a_folder_that_is_not_there_or_cannot_be_read_is_new() -> None:
    assert pathcheck.folder_kind("D:\\no\\such\\folder\\anywhere") == "new"

    def denied(_p: str) -> list[str]:
        raise PermissionError("denied")

    assert pathcheck.folder_kind("D:\\Studio", listdir=denied) == "new"


# ---- D-22: Browse to a kept home -------------------------------------------------------------------------
@pytest.mark.parametrize(
    "files", [("config/answers.json",), ("config/runtime.json", "models/llm/a.gguf"), ("data/studio.db",)]
)
def test_browse_to_a_kept_data_home_returns_it_as_picked(tmp_path: Path, files: tuple[str, ...]) -> None:
    kept = folder(tmp_path, "installs", *files)
    assert folderdialog.install_folder_for(str(kept)) == str(kept)
    assert pathcheck.install_folder_for(str(kept) + "\\") == str(kept)


def test_browse_still_nests_a_foreign_folder_and_a_drive_root(tmp_path: Path) -> None:
    docs = folder(tmp_path, "Documents", "a.docx")
    assert folderdialog.install_folder_for(str(docs)) == str(docs) + "\\Nightshift"
    assert folderdialog.install_folder_for("D:\\") == "D:\\Nightshift"
    empty = folder(tmp_path, "fresh")
    assert folderdialog.install_folder_for(str(empty)) == str(empty)


def test_the_folder_an_uninstall_keeping_data_leaves_is_a_kept_home_end_to_end(tmp_path: Path) -> None:
    """A finished (simulated) install, then the real uninstall script answering "keep my data"."""
    home = tmp_path / "home"
    assert main(args(tmp_path, write_answers(tmp_path))) == 0
    assert pathcheck.folder_kind(str(home)) == "home"
    uninstall_with_the_script(home)
    assert not (home / "config" / "install_state.json").exists()  # the marker D-22 said was the only one
    assert (home / "config" / "runtime.json").is_file()  # the owner's settings are kept
    assert pathcheck.folder_kind(str(home)) == "kept"
    assert folderdialog.install_folder_for(str(home)) == str(home)  # Browse hands the kept home back


# ---- D-21: a typed path, the same rule -------------------------------------------------------------------
def check(path: Path, **kw) -> dict:
    letter = str(path)[0].upper()
    drives = [{"letter": letter, "freeGb": 400.0, "type": "SSD"}]
    kw.setdefault("kind_fn", pathcheck.folder_kind)
    return preflight.disk_check(str(path), "small", drives, lambda _p: 0.0, drive_type_fn=lambda _l: 3, **kw)


def test_a_typed_folder_with_other_files_is_ok_but_carries_a_warning_the_window_can_show(
    tmp_path: Path,
) -> None:
    other = folder(tmp_path, "busy", "repo/readme.md", "notes.txt")
    got = check(other)
    assert got["ok"] is True and got["folder"] == "foreign"
    assert got["warning"] == (
        f"{other} already has other files in it. Pick an empty folder or a new one, such as {other}-2"
    )
    assert got["message"].endswith(got["warning"])  # the window shows `message` today
    folder(tmp_path, "busy-2", "x")
    assert check(other)["warning"].endswith(f"such as {other}-3")  # the suggestion is a free name


@pytest.mark.parametrize(
    ("files", "kind"),
    [((), "empty"), (("config/install_state.json",), "home"), (("config/runtime.json",), "kept")],
)
def test_an_empty_folder_a_home_and_kept_data_carry_no_warning(
    tmp_path: Path, files: tuple[str, ...], kind: str
) -> None:
    got = check(folder(tmp_path, "pick", *files))
    assert got["folder"] == kind and got["warning"] is None and got["ok"] is True
    if kind == "kept":
        assert "kept and used" in got["message"]


def test_a_folder_that_does_not_exist_yet_is_new_and_nothing_is_made(tmp_path: Path) -> None:
    target = tmp_path / "later"
    got = check(target)
    assert got["folder"] == "new" and got["warning"] is None and not target.exists()


def test_the_controller_answers_the_warning_for_a_typed_path_and_nests_a_browsed_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(controller, "desktop_dir", lambda: tmp_path / "desktop")
    fx, _cmd = fake_effects(tmp_path / "fx")
    fx.folder_kind = pathcheck.folder_kind  # the real rule, on the real folders
    ctl = SetupController(
        Config(app_path=ROOT, data_dir=tmp_path / "data", simulate=True, home=tmp_path / "home"), fx
    )
    busy = folder(tmp_path, "busy", "notes.txt")
    typed = ctl.disk(str(busy), "small", False)
    assert typed["folder"] == "foreign" and "already has other files" in typed["warning"]
    fx.next_pick.append(str(busy))
    browsed = ctl.pick_folder({"target": "install", "tier": "small"})
    assert browsed["path"] == str(busy) + "\\Nightshift" and browsed["check"]["warning"] is None
    kept = folder(tmp_path, "kept", "config/runtime.json")
    fx.next_pick.append(str(kept))
    assert ctl.pick_folder({"target": "install", "tier": "small"})["path"] == str(kept)


# ---- D-21: the console questions ---------------------------------------------------------------------------
def console_ctx(tmp_path: Path, ui: FakeUi, **answers):
    """A console setup that has not chosen its folder yet (the choice is a question, or home_path)."""
    return make_test_context(tmp_path, raw_answers=example_answers(**answers), ui=ui, home_forced=False)


def test_a_person_typing_a_folder_with_other_files_is_asked_and_can_pick_another(tmp_path: Path) -> None:
    busy = folder(tmp_path, "busy", "notes.txt")
    clean = tmp_path / "clean"
    ui = FakeUi({"Install folder": [str(busy), str(clean)], "Install there anyway": ["n"]})
    ctx = console_ctx(tmp_path, ui)
    S01Questions().run(ctx)
    assert ctx.paths.home == clean  # the second answer, after "no"
    assert "already has other files in it" in ui.text and "such as" in ui.text
    assert not (busy / "config").exists()  # nothing was made in the busy folder


def test_a_person_can_confirm_a_folder_with_other_files(tmp_path: Path) -> None:
    busy = folder(tmp_path, "busy", "notes.txt")
    ui = FakeUi({"Install folder": [str(busy)], "Install there anyway": ["y"]})
    ctx = console_ctx(tmp_path, ui)
    S01Questions().run(ctx)
    assert ctx.paths.home == busy


def test_an_answers_file_naming_a_folder_with_other_files_is_warned_not_refused(tmp_path: Path) -> None:
    busy = folder(tmp_path, "busy", "notes.txt")
    ui = FakeUi(interactive=False)
    ctx = console_ctx(tmp_path, ui, home_path=str(busy))  # nobody is there to confirm
    S01Questions().run(ctx)
    assert ctx.paths.home == busy and "already has other files in it" in ui.text


# ---- D-21: a home that has the extracted program inside it -----------------------------------------------
def test_a_home_that_holds_the_extracted_folder_is_refused() -> None:
    bundle = "D:\\NightShift\\runs\\dev-0.1.12"
    for home in ("D:\\NightShift", "d:\\nightshift\\", "D:\\NightShift\\runs", "D:\\"):
        message = pathcheck.layout_problem(home, bundle, BOB)
        assert message is not None and "extracted" in message and "inside it" in message, home
    assert pathcheck.layout_problem("D:\\Nightshift-2", bundle, BOB) is None
    assert pathcheck.layout_problem("D:\\NightShift2", "D:\\NightShift", BOB) is None


def test_the_installed_copy_in_the_home_is_not_a_bundle_inside_it() -> None:
    assert pathcheck.layout_problem("D:\\Nightshift", "D:\\Nightshift\\app", BOB) is None
    assert pathcheck.layout_problem("D:\\Nightshift", "D:\\Nightshift\\other", BOB) is not None


# ---- D-21: the default suggestion ------------------------------------------------------------------------
def test_the_default_home_skips_a_folder_that_cannot_be_offered() -> None:
    d = sysinfo.Drive
    drives = [d("C", 100, "SSD"), d("D", 900, "SSD"), d("E", 300, "HDD")]
    taken = {Path(f"{x}:\\{n}") for x in "CD" for n in ("Nightshift", "Nightshift-home")}
    assert sysinfo.choose_default_home(drives) == Path("D:\\Nightshift")  # nothing skipped: as before
    only_first = taken - {Path("D:\\Nightshift-home")}
    assert sysinfo.choose_default_home(drives, skip=lambda p: p in only_first) == Path("D:\\Nightshift-home")
    assert sysinfo.choose_default_home(drives, skip=lambda p: p in taken) == Path("E:\\Nightshift")
    assert sysinfo.choose_default_home(drives, skip=lambda _p: True) is None


# ---- the same rule in stage1.ps1, which picks the default folder before any Python exists ----------------
def powershell(tmp_path: Path, *lines: str) -> list[str]:
    script = tmp_path / "probe.ps1"
    script.write_text(
        "\n".join(
            [
                "$ErrorActionPreference = 'Stop'",
                f"$text = Get-Content -LiteralPath '{STAGE1}' -Raw",
                "$start = $text.IndexOf('function Test-PathInside')",
                "$end = $text.IndexOf('# Machine probes')",
                "$script:MinFreeGbRecommended = 80",
                "Invoke-Expression $text.Substring($start, $end - $start)",
                *lines,
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    proc = subprocess.run(
        [str(PS), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return [line for line in proc.stdout.splitlines() if line.strip()]


def test_stage1_classifies_folders_like_pathcheck(tmp_path: Path) -> None:
    cases = {
        "empty": (),
        "foreign": ("notes.txt", "repo/a.md"),
        "home": ("config/install_state.json",),
        "home2": ("app/VERSION",),
        "kept": ("config/answers.json",),
        "kept2": ("data/studio.db",),
        "kept3": ("config/runtime.json", "logs/x.jsonl"),
    }
    paths = {name: folder(tmp_path, name, *files) for name, files in cases.items()}
    out = powershell(
        tmp_path,
        *(f"'{name} => {{0}}' -f (Get-FolderKind -Path '{p}')" for name, p in paths.items()),
        f"'gone => {{0}}' -f (Get-FolderKind -Path '{tmp_path / 'not-there'}')",
    )
    got = dict(line.split(" => ", 1) for line in out)
    for name, p in paths.items():
        assert got[name] == pathcheck.folder_kind(str(p)), name
    assert got["gone"] == "new" and got["empty"] == "empty" and got["foreign"] == "foreign"


def test_stage1_and_pathcheck_list_the_same_markers() -> None:
    text = STAGE1.read_text(encoding="utf-8")
    body = text[text.index("function Get-FolderKind") : text.index("function Select-DefaultHome")]
    lists = re.findall(r"foreach \(\$marker in @\(([^)]*)\)\)", body)
    seen = [tuple(re.findall(r"'([^']+)'", group)) for group in lists]
    want = [
        tuple("\\".join(m) for m in markers) for markers in (pathcheck.HOME_MARKERS, pathcheck.KEPT_MARKERS)
    ]
    assert seen == want


def test_stage1_never_offers_a_foreign_folder_or_one_around_the_extracted_program(tmp_path: Path) -> None:
    out = powershell(
        tmp_path,
        "$drives = @([pscustomobject]@{ letter = 'D'; free_gb = 900; type = 'SSD' },",
        "            [pscustomobject]@{ letter = 'E'; free_gb = 400; type = 'SSD' })",
        "$foreign = { param($p) $p -in @('D:\\Nightshift', 'D:\\Nightshift-home') }",
        "'foreign => ' + (Select-DefaultHome -Drives $drives -AppFolder 'D:\\setup' -IsTaken $foreign)",
        "'free => ' + (Select-DefaultHome -Drives $drives -AppFolder 'D:\\setup' -IsTaken { $false })",
        "'around => ' + (Select-DefaultHome -Drives $drives -AppFolder 'D:\\Nightshift\\runs\\dev-0.1.12'"
        " -IsTaken { $false })",
        "'none => [' + (Select-DefaultHome -Drives $drives -AppFolder 'D:\\setup' -IsTaken { $true }) + ']'",
    )
    got = dict(line.split(" => ", 1) for line in out)
    assert got["foreign"] == "E:\\Nightshift"  # D's two names are taken: the next drive
    assert got["free"] == "D:\\Nightshift"
    assert got["around"] == "D:\\Nightshift-home"  # D:\Nightshift would hold the extracted program
    assert got["none"] == "[]"
