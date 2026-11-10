"""C1: the uninstall window's server side (no PowerShell is run)."""

import io
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from installer.stage2.web import uninstall

APP = Path("D:/ns-app")


class FakeProc:
    def __init__(self, text: str) -> None:
        self.stdout = io.StringIO(text)

    def wait(self) -> int:
        return 0


def make_home(tmp_path: Path) -> Path:
    home = tmp_path / "ns"
    for rel, size in (("venvs/a.bin", 100), ("bin/b.bin", 50), ("models/m.gguf", 4000), ("outbox/v.mp4", 700),
                      ("data/db.sqlite", 30), ("config/answers.json", 5)):
        f = home / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x" * size)
    return home


def test_the_command_line_passes_a_switch_only_when_it_is_ticked():
    plain = uninstall.uninstall_argv(APP, Path("D:/ns"), False, False)
    assert plain[:6] == ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                         str(APP / "installer" / "uninstall.ps1")]
    assert "-DeleteModels" not in plain and "-DeleteData" not in plain
    assert plain[-3:] == ["-HomeDir", "D:\\ns", "-KeepBin"]        # the window itself holds bin\webview2 DLLs
    both = uninstall.uninstall_argv(APP, Path("D:/ns"), True, True)
    assert "-DeleteModels" in both and "-DeleteData" in both


def test_the_view_lists_what_goes_and_what_stays_with_sizes(tmp_path: Path):
    home = make_home(tmp_path)
    view = uninstall.UninstallApp(APP, home).view()
    assert view["home"] == str(home) and view["running"] is False and view["result"] is None
    assert [a["label"] for a in view["always"]] == [
        "The nightly task, when it runs this install", "Desktop shortcuts that open this install",
        "Programs and Python environments",
        "Windows' note of where Nightshift is installed, when it names this install",
    ]
    assert view["always"][2]["bytes"] == 150 and view["always"][0]["bytes"] is None
    assert view["models"] == {"path": str(home / "models"), "bytes": 4000}
    assert view["data"] == {"path": str(home / "outbox"), "bytes": 730}
    empty = uninstall.UninstallApp(APP, None).view()
    assert empty["home"] is None and empty["models"]["bytes"] == 0


def test_removing_runs_the_script_logs_its_output_and_reports_what_stayed(tmp_path: Path):
    home = make_home(tmp_path)
    started: list[list[str]] = []
    logged: list[str] = []

    def popen(argv, **kw):
        started.append(list(argv))
        assert kw["env"]["NS_NONINTERACTIVE"] == "1"
        shutil.rmtree(home / "venvs")                  # what the script really removes
        shutil.rmtree(home / "outbox")
        return FakeProc("  removing D:\\ns\\venvs\n\n  removed shortcut Nightshift Dashboard.lnk\n")

    deferred: list[Path] = []
    app = uninstall.UninstallApp(
        APP, home, popen=popen, log=logged.append, deferred=lambda h: deferred.append(h) or True,
        env_var=lambda name: None,
    )
    status, body = app.run(delete_models=False, delete_data=True)
    assert status == 200 and body["ok"] is True
    deadline = time.monotonic() + 10
    while app.view()["result"] is None and time.monotonic() < deadline:
        time.sleep(0.02)
    result = app.view()["result"]
    assert result["kept"] == [{"label": "AI models", "path": str(home / "models")}]
    assert "Videos and history" in result["removed"] and "AI models" not in result["removed"]
    assert deferred == [home]                                    # bin is left for the detached PowerShell
    # the closing screen is honest: what an uninstall never touches is listed, not "nothing else was changed"
    stays = " ".join(result["stays"])
    assert "Visual C++ runtime" in stays and "download cache" in stays
    assert str(home / "config") in stays and str(home / "logs") in stays
    assert any(r.startswith("Programs and Python environments (the last files go") for r in result["removed"])
    assert logged == ["  removing D:\\ns\\venvs", "  removed shortcut Nightshift Dashboard.lnk"]
    assert "-DeleteData" in started[0] and "-DeleteModels" not in started[0]
    assert app.view()["running"] is False
    assert app.run(False, False)[0] == 409                           # only once


def test_close_and_reveal_rules(tmp_path: Path):
    home = make_home(tmp_path)
    app = uninstall.UninstallApp(APP, home, popen=lambda argv, **kw: FakeProc(""))
    assert app.action("reveal", {"path": str(home / "models")})[0] == 409       # nothing kept yet
    assert app.action("explode", {})[0] == 409
    assert app.action("close-setup", {})[0] == 200 and app.shutdown.is_set()
    nowhere = uninstall.UninstallApp(APP, None)
    assert nowhere.run(False, False)[0] == 409



# ------------------------------------------------------ the DLLs the window itself holds (review row 7)
def finished(app: uninstall.UninstallApp) -> dict:
    app.run(False, False)
    deadline = time.monotonic() + 10
    while app.view()["result"] is None and time.monotonic() < deadline:
        time.sleep(0.02)
    return app.view()["result"]


def test_a_file_that_could_not_be_removed_is_reported_as_kept_not_removed(tmp_path: Path):
    home = make_home(tmp_path)  # the fake script removes nothing
    app = uninstall.UninstallApp(
        APP, home, popen=lambda argv, **kw: FakeProc(""), deferred=lambda h: False,
        env_var=lambda n: str(home))
    result = finished(app)
    labels = [k["label"] for k in result["kept"]]
    assert "Python environments (a file is in use)" in labels
    assert "Programs (delete this folder by hand)" in labels
    assert "Windows' note of where Nightshift is installed (could not be removed)" in labels
    assert not any(r.startswith("Programs") for r in result["removed"])
    assert "The nightly task" not in result["removed"]


def test_a_script_that_stops_is_not_reported_as_a_success(tmp_path: Path):
    class Failing(FakeProc):
        def wait(self) -> int:
            return 1

    home = make_home(tmp_path)
    app = uninstall.UninstallApp(
        APP, home, popen=lambda argv, **kw: Failing(""), deferred=lambda h: True, env_var=lambda n: None)
    result = finished(app)
    assert result["kept"][0]["label"].startswith("Nightshift (the removal did not finish")
    assert "Desktop shortcuts" not in result["removed"]


def test_the_deferred_script_waits_for_the_window_then_deletes_bin_with_retries(tmp_path: Path):
    home = tmp_path / "José's ns"
    script = uninstall.deferred_script(home, [111, 222])
    assert "@(111,222)" in script and "Wait-Process" in script
    assert str(home / "bin").replace("'", "''") in script
    assert f"for ($i = 0; $i -lt {uninstall.REMOVE_TRIES}" in script
    left = home / "app.swap-left-20261110-0312"  # (card 206-carried: a folder a start set aside)
    left.mkdir(parents=True)
    again = uninstall.deferred_script(home, [111])
    assert str(left).replace("'", "''") in again and "nightshift-launch.ps1" in again
    assert uninstall.REMOVE_TRIES >= 120                                      # at least two minutes
    started: list[dict] = []
    ok = uninstall.spawn_deferred(home, popen=lambda argv, **kw: started.append({"argv": argv, **kw}))
    assert ok and started[0]["argv"][0] == "powershell.exe" and "-EncodedCommand" in started[0]["argv"]
    assert started[0]["creationflags"] & 0x8                                   # DETACHED_PROCESS


@pytest.mark.skipif(sys.platform != "win32" or shutil.which("powershell") is None, reason="needs PowerShell")
def test_a_file_held_open_blocks_deletion_until_the_deferred_script_runs_after_exit(tmp_path: Path):
    """The real failure: a loaded DLL under bin cannot be deleted while its process lives."""
    home = tmp_path / "ns"
    held_file = home / "bin" / "webview2" / "x.dll"
    held_file.parent.mkdir(parents=True)
    code = f"import time; f = open({str(held_file)!r}, 'wb'); print('held', flush=True); time.sleep(60)"
    holder = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        shutil.rmtree(home / "bin", ignore_errors=True)
        assert held_file.exists()                                               # Windows refuses
        script = uninstall.deferred_script(home, [holder.pid])
        proc = subprocess.Popen(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script])
        holder.kill()                                                           # the window exits
        holder.wait()
        assert proc.wait(timeout=120) == 0
    finally:
        holder.kill()
    assert not (home / "bin").exists()


def _remove_and_wait(app: uninstall.UninstallApp) -> dict:
    assert app.run(delete_models=False, delete_data=False)[0] == 200
    deadline = time.monotonic() + 10
    while app.view()["result"] is None and time.monotonic() < deadline:
        time.sleep(0.02)
    return app.view()["result"]


def _updater_copy(home: Path) -> Path:
    copy = home / "run" / "updater" / "venv-20261103-030000" / "Scripts" / "python.exe"
    copy.parent.mkdir(parents=True)
    copy.write_bytes(b"x" * 300)
    (home / "run" / "apphost-setup.json").write_text('{"pid": 7}', encoding="utf-8")
    return home / "run" / "updater"


def test_the_update_workers_venv_copy_counts_as_a_program_and_is_in_the_report(tmp_path: Path):
    home = make_home(tmp_path)
    updater = _updater_copy(home)
    assert uninstall.UninstallApp(APP, home).view()["always"][2]["bytes"] == 150 + 300 + len(
        '{"pid": 7}'
    )  # venvs + bin + the whole run folder

    def popen(argv, **kw):
        shutil.rmtree(updater)                         # what uninstall.ps1 removes now (run itself waits)
        shutil.rmtree(home / "venvs")
        return FakeProc("")

    app = uninstall.UninstallApp(APP, home, popen=popen, deferred=lambda h: True, env_var=lambda n: None)
    result = _remove_and_wait(app)
    assert "Update files (the private Python copy that updates use)" in result["removed"]
    assert not any("Update files" in k["label"] for k in result["kept"])


def test_a_copy_that_could_not_be_removed_is_reported_as_kept(tmp_path: Path):
    home = make_home(tmp_path)
    updater = _updater_copy(home)

    def popen(argv, **kw):
        shutil.rmtree(home / "venvs")
        return FakeProc("")                            # the updater folder stays (a file is in use)

    app = uninstall.UninstallApp(APP, home, popen=popen, deferred=lambda h: True, env_var=lambda n: None)
    result = _remove_and_wait(app)
    assert {"label": "Update files (a file is in use)", "path": str(updater)} in result["kept"]
    assert "Update files (the private Python copy that updates use)" not in result["removed"]


def test_no_update_ever_made_means_no_update_files_line(tmp_path: Path):
    home = make_home(tmp_path)

    def popen(argv, **kw):
        shutil.rmtree(home / "venvs")
        return FakeProc("")

    app = uninstall.UninstallApp(APP, home, popen=popen, deferred=lambda h: True, env_var=lambda n: None)
    result = _remove_and_wait(app)
    assert not any("Update files" in r for r in result["removed"])


def test_the_deferred_script_also_removes_the_run_folder_after_the_windows_are_gone(tmp_path: Path):
    script = uninstall.deferred_script(tmp_path / "ns", [1])
    assert str(tmp_path / "ns" / "run").replace("'", "''") in script
    assert str(tmp_path / "ns" / "bin").replace("'", "''") in script


def test_the_closing_screen_says_what_happened_to_the_keys(tmp_path: Path):
    """The uninstall program forgets the stock-footage keys (or leaves them for another Nightshift folder):
    the closing screen shows its own lines about them, and no longer lists the keys under what stays."""
    home = make_home(tmp_path)
    file_line = f"removed {home}\\config\\secrets.dpapi (the copy of the Pixabay key for nightly runs)"
    said = "\n".join([
        "Left the notification registration: the Nightshift at D:\\Other uses it.",
        "  forgot the Pixabay key in Credential Manager",
        "  " + file_line,
        "  removed user environment variable NIGHTSHIFT_HOME",
        "",
    ])
    app = uninstall.UninstallApp(
        APP, home, popen=lambda argv, **kw: FakeProc(said), deferred=lambda h: True, env_var=lambda n: None)
    result = finished(app)
    assert not any("stock footage keys" in s or "Credential Manager" in s for s in result["stays"])
    assert result.get("keys") == ["forgot the Pixabay key in Credential Manager", file_line]
    # another Nightshift folder uses them: the program's "Left" line is the key line, shown once
    left = "Left the stock-footage keys in Credential Manager: the Nightshift at D:\\Other uses them."
    other = uninstall.UninstallApp(
        APP, make_home(tmp_path / "b"), popen=lambda argv, **kw: FakeProc(left + "\n"),
        deferred=lambda h: True, env_var=lambda n: None)
    result = finished(other)
    assert result.get("keys") == [left]
    assert left not in [k["label"] for k in result["kept"]]
    # a program that said nothing about the keys (it stopped early): nothing is claimed
    silent = uninstall.UninstallApp(
        APP, make_home(tmp_path / "c"), popen=lambda argv, **kw: FakeProc(""), deferred=lambda h: True,
        env_var=lambda n: None)
    assert finished(silent).get("keys") == []
