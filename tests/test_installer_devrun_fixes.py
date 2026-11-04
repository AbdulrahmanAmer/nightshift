"""Found by the first dev-mode run on a laptop (INSTALL.cmd --dev, unattended): the welcome step waited for
Enter and died with EOFError when stdin was not a console, and the diagnostics bundle could not be written
because the engine venv (and `ns doctor`) does not exist before s04."""

import io
import json
import zipfile
from pathlib import Path

from rich.console import Console

from installer.stage2 import ui as ui_mod
from installer.stage2.diagzip import write_fallback_bundle
from installer.stage2.steps.s00_welcome import S00Welcome
from installer.stage2.testing import FakeUi, make_test_context
from ns_common.paths import Paths


def _eof_console() -> Console:
    return Console(file=io.StringIO(), width=100, force_terminal=False)


def test_console_prompts_take_the_default_when_stdin_ends(monkeypatch):
    u = ui_mod.Ui(_eof_console(), interactive=True)

    def boom(*a, **k):
        raise EOFError

    monkeypatch.setattr(ui_mod.Prompt, "ask", boom)
    monkeypatch.setattr(ui_mod.Confirm, "ask", boom)
    u.pause("Press Enter")                       # no exception
    assert u.interactive is False                # and the UI remembers it
    assert u.ask("name", "dflt") == "dflt"
    assert u.confirm("ok?", default=False) is False
    assert u.choose("pick", ["a", "b"], default="b") == "b"


def test_welcome_does_not_wait_for_enter_on_an_unattended_run(tmp_path: Path):
    ui = FakeUi(interactive=True)                # a scripted prompt would raise on an unscripted pause
    ctx = make_test_context(tmp_path, ui=ui)
    ctx.unattended = True
    S00Welcome().run(ctx)
    assert "Unattended setup: starting now." in ui.text
    assert "Press Enter" not in ui.text


def test_fallback_bundle_holds_logs_state_and_answers(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    paths = Paths(home)
    paths.logs.mkdir(parents=True)
    paths.config.mkdir(parents=True)
    (paths.logs / "installer-20261104.jsonl").write_text("{}\n", encoding="utf-8")
    (paths.config / "install_state.json").write_text("{}", encoding="utf-8")
    (paths.config / "answers.json").write_text("{\"pexels_api_key\": \"\"}", encoding="utf-8")
    boot = tmp_path / "lad" / "Nightshift" / "bootstrap"
    boot.mkdir(parents=True)
    (boot / "preflight.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    desk = tmp_path / "Desktop"
    desk.mkdir()
    out = write_fallback_bundle(paths, desktop=desk)
    assert out.parent == paths.logs and out.name.startswith("nightshift-diagnostics-")
    names = set(zipfile.ZipFile(out).namelist())
    assert {"logs/installer-20261104.jsonl", "config/install_state.json", "config/answers.json",
            "bootstrap/bootstrap/preflight.json"} <= names
    assert (desk / out.name).is_file()


def test_console_install_report_is_built_from_the_install_state(tmp_path: Path):
    from installer.stage2.diagzip import write_console_install_report

    paths = Paths(tmp_path / "home")
    paths.config.mkdir(parents=True)
    (paths.config / "install_state.json").write_text(json.dumps({
        "version": "0.1.7", "app_path": "x", "profile": "cpu-dev", "dev_mode": True,
        "started_at": "2026-11-04T11:59:18", "completed_at": None,
        "steps": {"s01_questions": {"status": "done", "error": None, "finished_at": "2026-11-04T12:00:00"},
                  "s02_download_bins": {"status": "failed", "error": "NS-E105", "finished_at": None}},
    }), encoding="utf-8")
    (paths.config / "answers.json").write_text(json.dumps({"home_path": "x", "pixabay_api_key": "SECRET"}),
                                               encoding="utf-8")
    err = {"code": "NS-E105", "message": "site unreachable", "detail": "d"}
    out = write_console_install_report(paths, outcome="failed", version="0.1.7", last_error=err)
    assert out is not None and out.name == "install-report.json" and out.parent == paths.logs
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["outcome"] == "failed" and data["mode"] == "console" and data["dev_mode"] is True
    assert data["steps"]["s02_download_bins"]["status"] == "failed"
    assert data["last_error"]["code"] == "NS-E105"
    assert "pixabay_api_key" not in data["answers"] and data["answers"]["home_path"] == "x"
    assert write_console_install_report(Paths(tmp_path / "none"), outcome="done", version="0.1.7") is not None
