"""The Browse button: folder dialog, path rules, install-folder validation and the /api/pick-folder route."""

import http.client
import json
import subprocess
from pathlib import Path

import pytest

from installer.stage2.web import answers_view, controller, folderdialog, pathcheck, preflight
from installer.stage2.web.controller import Config, SetupController
from installer.stage2.web.fakes import DEFAULT_PICK, fake_effects
from installer.stage2.web.server import SetupServer

ROOT = Path(__file__).resolve().parents[1]
DRIVES = [{"letter": "D", "freeGb": 412.0, "type": "SSD"}, {"letter": "C", "freeGb": 31.0, "type": "HDD"}]


# ---- the path rules --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "path",
    ["D:\\Nightshift", "D:\\Night shift\\Studio 2", "D:\\Nightshift\\", "E:\\Munchen\\Videos",
     "C:\\Users\\John Doe\\Nightshift", "d:\\x", "D:\\" + "a" * 100, "D:\\a.b\\c-d_e", "D:\\Night-shift_2"],
)
def test_good_paths_have_no_problem(path: str) -> None:
    assert pathcheck.home_problem(path) is None


@pytest.mark.parametrize(
    "path",
    ["E:\\M\u00fcnchen\\Videos", "D:\\\u0646\u0627\u064a\u062a", "C:\\Users\\\u0623\u062d\u0645\u062f\\Ns",
     "D:\\Night\u2011shift", "D:\\caf\u00e9", "D:\\\u591c\u73ed", "D:\\a\u00a0b"],
)
def test_a_folder_name_outside_plain_ascii_is_refused_with_the_reason(path: str) -> None:
    # llama.cpp, qwentts.cpp and CTranslate2 open model paths through the ANSI code page
    assert pathcheck.home_problem(path) == (
        "Use a folder name with plain English letters, digits, spaces, - and _ "
        "(the model programs cannot open other paths)"
    )
    assert pathcheck.ascii_problem(path) == pathcheck.NON_ASCII_PROBLEM
    assert pathcheck.ascii_problem("D:\\Nightshift") is None


def test_the_window_mirrors_the_non_ascii_rule() -> None:
    ts = (ROOT / "installer" / "ui" / "src" / "format.ts").read_text(encoding="utf-8")
    assert pathcheck.NON_ASCII_PROBLEM in ts and "[^ -~]" in ts


@pytest.mark.parametrize(
    ("path", "word"),
    [("", "Browse"), ("   ", "Browse"), ("Nightshift", "full path"), ("D:Nightshift", "full path"),
     ("\\\\server\\share\\ns", "Network"), ("//server/share", "Network"),
     ("D:\\a<b", "does not allow"), ("D:\\a|b", "does not allow"), ("D:\\a?b", "does not allow"),
     ("D:\\100%", "does not allow"), ("D:\\a:b", "does not allow"), ("D:\\a\x07b", "does not allow"),
     ("D:\\" + "a" * 118, "120 characters"), ("D:\\a\\\\b", "single backslashes"),
     ("D:\\a\\..\\b", "without"), ("D:\\a \\b", "folder name"), ("D:\\a.\\b", "folder name"),
     ("D:\\CON", "folder name"), ("D:\\nul.txt", "folder name"), ("D:\\x\\LPT1", "folder name")],
)
def test_bad_paths_say_why(path: str, word: str) -> None:
    problem = pathcheck.home_problem(path)
    assert problem is not None and word in problem, problem


def test_the_length_limit_is_120_and_a_parameter() -> None:
    assert pathcheck.MAX_PATH_LEN == 120 and answers_view.MAX_HOME_LEN == 120
    assert pathcheck.home_problem("D:\\" + "a" * 117) is None            # 120 characters in all
    assert pathcheck.home_problem("D:\\" + "a" * 118) is not None
    assert pathcheck.home_problem("D:\\" + "a" * 118, 200) is None


def test_the_choices_validation_uses_the_same_rules() -> None:
    env = answers_view.Env(ram_gb=32, tiers={"small": {"needFreeGb": 60}}, drive_letters={"D"},
                           free_gb=lambda _p: 500.0)
    assert answers_view._home_errors("D:\\Night shift", "small", env) == []
    errors = answers_view._home_errors("D:\\Night shift \u00e9", "small", env)
    assert "plain English letters" in errors[0]["message"]
    assert "Network" in answers_view._home_errors("\\\\nas\\share", "small", env)[0]["message"]
    assert "120" in answers_view._home_errors("D:\\" + "a" * 130, "small", env)[0]["message"]
    assert "fixed drive" in answers_view._home_errors("Z:\\x", "small", env)[0]["message"]


# ---- the disk check ----------------------------------------------------------------------------------------
def check(path: str, **kw) -> dict:
    kw.setdefault("drive_type_fn", lambda _l: 3)
    kw.setdefault("isfile_fn", lambda _p: False)
    return preflight.disk_check(path, "small", DRIVES, kw.pop("free_fn", lambda _p: 0.0), **kw)


def test_a_folder_on_a_roomy_fixed_drive_is_ok_and_spaces_are_fine() -> None:
    r = check("D:\\Night shift\\Studio")
    assert r["ok"] and r["drive"] == "D" and r["freeGb"] == 412.0
    assert r["message"] == "412 GB free on D: (SSD)"


def test_the_checks_reject_what_cannot_hold_an_install() -> None:
    assert not check("\\\\nas\\share")["ok"] and "Network" in check("\\\\nas\\share")["message"]
    assert "120 characters" in check("D:\\" + "a" * 130)["message"]
    assert "does not allow" in check("D:\\a*b")["message"]
    assert "Only 31 GB" in check("C:\\Nightshift")["message"]
    assert "file, not a folder" in check("D:\\x.txt", isfile_fn=lambda _p: True)["message"]


@pytest.mark.parametrize("kind", [2, 4, 5])
def test_removable_network_and_disc_drives_are_refused(kind: int) -> None:
    r = check("F:\\Nightshift", drive_type_fn=lambda _l: kind, free_fn=lambda _p: 900.0)
    assert not r["ok"] and "removable, network or disc" in r["message"]


def test_a_new_fixed_drive_that_stage_1_did_not_list_is_still_accepted() -> None:
    r = check("F:\\Nightshift", drive_type_fn=lambda _l: 3, free_fn=lambda _p: 900.0)
    assert r["ok"] and r["freeGb"] == 900.0


def test_a_check_creates_nothing(tmp_path: Path) -> None:
    target = tmp_path / "not yet"
    preflight.disk_check(str(target), "small", [], lambda _p: 500.0, drive_type_fn=lambda _l: 3)
    assert not target.exists()


# ---- the folder dialog (powershell.exe is faked) ------------------------------------------------------
class FakeRun:
    def __init__(self, text: str | None, code: int = 0, err: bytes = b"") -> None:
        self.text, self.code, self.err = text, code, err
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        out = Path(argv[argv.index("-Out") + 1])
        if self.text is not None:
            out.write_bytes(self.text.encode("utf-8"))
        return subprocess.CompletedProcess(argv, self.code, b"", self.err)


def test_the_dialog_runs_an_sta_powershell_and_reads_the_path_back(tmp_path: Path) -> None:
    run = FakeRun("D:\\Studio \u00e9\\Nightshift")
    got = folderdialog.pick_folder("Choose the install folder", "D:\\My Folder", run=run, tmp_dir=tmp_path)
    assert got == "D:\\Studio \u00e9\\Nightshift"
    argv = run.calls[0]
    assert argv[0] == "powershell.exe" and "-STA" in argv
    assert argv[argv.index("-File") + 1].endswith("pick-folder.ps1")
    assert argv[argv.index("-Title") + 1] == "Choose the install folder"
    assert argv[argv.index("-Start") + 1] == "D:\\My Folder"                    # one argument, spaces intact
    assert list(tmp_path.glob("ns-pick-*")) == []                              # the result file is removed


def test_cancel_is_none_and_a_bom_is_ignored(tmp_path: Path) -> None:
    assert folderdialog.pick_folder("t", run=FakeRun(""), tmp_dir=tmp_path) is None
    assert folderdialog.pick_folder("t", run=FakeRun("\ufeffD:\\x\r\n"), tmp_dir=tmp_path) == "D:\\x"


def test_failures_become_a_folder_dialog_error(tmp_path: Path) -> None:
    with pytest.raises(folderdialog.FolderDialogError, match="exited with 1"):
        folderdialog.pick_folder("t", run=FakeRun(None, 1, b"boom"), tmp_dir=tmp_path)

    def gone(argv, **kw):
        raise FileNotFoundError("powershell.exe")

    with pytest.raises(folderdialog.FolderDialogError):
        folderdialog.pick_folder("t", run=gone, tmp_dir=tmp_path)
    with pytest.raises(folderdialog.FolderDialogError, match="missing"):
        folderdialog.pick_folder("t", script=tmp_path / "nope.ps1", run=FakeRun(""), tmp_dir=tmp_path)
    assert list(tmp_path.glob("ns-pick-*")) == []


def test_the_dialog_script_is_in_front_and_changes_nothing() -> None:
    text = (ROOT / "installer" / "pick-folder.ps1").read_text(encoding="ascii")
    assert "FolderBrowserDialog" in text and "TopMost = $true" in text and "ShowDialog($owner)" in text
    assert "UTF8Encoding($false)" in text                                      # non-ASCII paths survive
    assert not any(w in text for w in ("New-Item", "mkdir", "Remove-Item", "Set-Content", "CreateDirectory"))


@pytest.mark.parametrize(
    ("picked", "listing", "expected"),
    [("D:\\", [], "D:\\Nightshift"), ("D:", [], "D:\\Nightshift"), ("D:\\Studio", [], "D:\\Studio"),
     ("D:\\Studio\\", [], "D:\\Studio"), ("D:\\Documents", ["a.docx"], "D:\\Documents\\Nightshift"),
     ("D:\\Studio", ["config", "bin"], "D:\\Studio\\Nightshift"), ("not a path", [], "not a path")],
)
def test_a_drive_root_or_a_busy_folder_gets_a_nightshift_folder(picked: str, listing, expected: str) -> None:
    assert folderdialog.install_folder_for(picked, listdir=lambda _p: list(listing)) == expected


def test_an_existing_nightshift_install_is_used_as_picked() -> None:
    def listing(path: str) -> list[str]:
        return ["install_state.json"] if path.endswith("\\config") else ["config", "models", "bin"]

    assert folderdialog.install_folder_for("D:\\Nightshift", listdir=listing) == "D:\\Nightshift"


def test_an_unreadable_or_missing_folder_is_used_as_given() -> None:
    def boom(_p: str) -> list[str]:
        raise OSError("denied")

    assert folderdialog.install_folder_for("D:\\Studio", listdir=boom) == "D:\\Studio"


# ---- the controller and the route ---------------------------------------------------------------------
@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(controller, "desktop_dir", lambda: tmp_path / "desktop")
    fx, _cmd = fake_effects(tmp_path / "fx")
    cfg = Config(app_path=ROOT, data_dir=tmp_path / "data", simulate=True, home=tmp_path / "home")
    ctl = SetupController(cfg, fx)
    return ctl, fx


def test_the_simulate_dialog_answers_with_a_path_and_its_check(setup) -> None:
    ctl, fx = setup
    got = ctl.pick_folder({"target": "install", "start": "C:\\x", "tier": "small"})
    assert got["ok"] and got["path"] == DEFAULT_PICK and not got["cancelled"]
    assert got["check"]["path"] == DEFAULT_PICK and got["check"]["ok"] is True      # 500 GB free in fakes
    fx.next_pick.extend(["D:\\", None])
    assert ctl.pick_folder({"target": "install"})["path"] == "D:\\Nightshift"         # a drive root
    cancelled = ctl.pick_folder({"target": "install"})
    assert cancelled == {"ok": True, "path": None, "cancelled": True, "message": None, "check": None}


def test_the_models_target_checks_the_folder_for_model_files(setup, tmp_path: Path) -> None:
    ctl, fx = setup
    fx.next_pick.append(str(tmp_path))
    got = ctl.pick_folder({"target": "models", "tier": "small"})
    assert got["path"] == str(tmp_path) and got["check"]["modelsFound"] is not None


def test_a_dialog_that_cannot_open_is_a_plain_message(setup) -> None:
    ctl, fx = setup

    def broken(title: str, start: str):
        raise folderdialog.FolderDialogError("no powershell")

    fx.pick_folder = broken
    got = ctl.pick_folder({"target": "install"})
    assert got["ok"] is False and "Type the folder instead" in got["message"]


def test_the_route_needs_the_cookie_and_answers_json(setup) -> None:
    ctl, _fx = setup
    server = SetupServer(ctl, static_dir=ROOT / "installer" / "ui" / "static", version="0")
    server.start()
    try:
        def call(headers):
            conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
            head = {"Host": f"127.0.0.1:{server.port}", "Content-Type": "application/json", **headers}
            body = json.dumps({"target": "install", "tier": "small"})
            conn.request("POST", "/api/pick-folder", body=body, headers=head)
            resp = conn.getresponse()
            return resp.status, json.loads(resp.read() or b"{}")

        assert call({})[0] == 401
        status, body = call({"Cookie": f"ns_setup={server.token}"})
        assert status == 200 and body["path"] == DEFAULT_PICK and body["check"]["ok"]
    finally:
        server.stop()


def test_locked_choices_refuse_the_dialog(setup) -> None:
    ctl, fx = setup
    ctl.store.update(lambda s: s.__setitem__("answersLocked", True))
    got = ctl.pick_folder({"target": "install"})
    assert got["ok"] is False and "locked" in got["message"] and not fx.next_pick


def test_the_committed_window_build_has_the_browse_buttons() -> None:
    js = (ROOT / "installer" / "ui" / "static" / "app.js").read_text(encoding="utf-8")
    assert "/api/pick-folder" in js and "Browse\u2026" in js and "Waiting for the folder window" in js
