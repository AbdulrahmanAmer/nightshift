"""C1: the uninstall window's server side (no PowerShell is run)."""

import io
import time
from pathlib import Path

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
    assert plain[-2:] == ["-HomeDir", "D:\\ns"]
    both = uninstall.uninstall_argv(APP, Path("D:/ns"), True, True)
    assert "-DeleteModels" in both and "-DeleteData" in both


def test_the_view_lists_what_goes_and_what_stays_with_sizes(tmp_path: Path):
    home = make_home(tmp_path)
    view = uninstall.UninstallApp(APP, home).view()
    assert view["home"] == str(home) and view["running"] is False and view["result"] is None
    assert [a["label"] for a in view["always"]] == [
        "The nightly task", "Desktop shortcuts", "Programs and Python environments",
        "The install folder setting",
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
        return FakeProc("  removing D:\\ns\\venvs\n\n  removed shortcut Nightshift Dashboard.lnk\n")

    app = uninstall.UninstallApp(APP, home, popen=popen, log=logged.append)
    status, body = app.run(delete_models=False, delete_data=True)
    assert status == 200 and body["ok"] is True
    deadline = time.monotonic() + 10
    while app.view()["result"] is None and time.monotonic() < deadline:
        time.sleep(0.02)
    result = app.view()["result"]
    assert result["kept"] == [{"label": "AI models", "path": str(home / "models")}]
    assert "Videos and history" in result["removed"] and "AI models" not in result["removed"]
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
