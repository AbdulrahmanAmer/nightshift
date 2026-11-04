"""C1: what the owner sees from far away (status line, install-report.json, one diagnostics zip)
and the Done and Failed screens a partner meets on the first run. Fakes only; the UI half reads the
TypeScript sources because the project has no JavaScript test runner (the `ui-build` job type-checks them)."""

import json
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path

from installer.stage2.web import report
from installer.stage2.web.model import STEP_ORDER
from tests.test_installer_web_controller import (
    KEY_P,
    KEY_X,
    ROOT,
    Setup,
    failing_script,
    finish_script,
)

UI = ROOT / "installer" / "ui" / "src"


def fake_state(**over) -> dict:
    steps = [
        {
            "id": sid,
            "title": f"Title {i}",
            "status": "pending",
            "detail": None,
            "startedAt": None,
            "finishedAt": None,
        }
        for i, sid in enumerate(STEP_ORDER, start=1)
    ]
    base = {
        "version": "0.1.7",
        "run": "running",
        "phase": "installing",
        "failure": None,
        "notices": [],
        "startedAt": "2026-11-04T12:00:00",
        "steps": steps,
        "answers": {"homePath": "D:\\Nightshift"},
        "machine": {
            "windows": "Windows 11",
            "gpuName": "RTX 5060 Ti",
            "vramMb": 16311,
            "driver": "581.57",
            "cuda": "13.0",
            "ramGb": 32.0,
            "profile": "gpu",
            "isAdmin": False,
        },
        "downloads": {"overall": {"bytesTotal": 1000, "bytesDone": 410}},
    }
    base.update(over)
    return base


def mark(state: dict, done: int) -> dict:
    for s in state["steps"][:done]:
        s["status"] = "done"
    return state


NOW = datetime(2026, 11, 4, 12, 12, 3)


def read_report(path: Path, outcome: str, timeout: float = 10.0) -> dict:
    """The report is written just after the state changes: wait for the file that says `outcome`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("outcome") == outcome:
                return data
        except (OSError, ValueError):
            pass
        time.sleep(0.02)
    raise AssertionError(f"{path} never said outcome={outcome}")


# ------------------------------------------------------------------------------------------ the status line
def test_the_status_line_names_version_step_percent_and_time():
    s = mark(fake_state(phase="downloading"), 5)
    line = report.status_line(s, None, NOW)
    total = len(STEP_ORDER)
    assert line == f"Nightshift setup 0.1.7 · step 6/{total} Title 6 · 41% · 12:03 elapsed"


def test_the_status_line_outside_downloading_counts_finished_steps_and_long_runs_show_hours():
    s = mark(fake_state(phase="installing", startedAt="2026-11-04T10:00:00"), 7)
    line = report.status_line(s, None, NOW)
    assert "step 8/" in line and f"{int(100 * 7 / len(STEP_ORDER))}%" in line and "2:12:03 elapsed" in line


def test_the_last_error_stays_in_the_line_after_a_retry_and_a_failure_says_stopped():
    err = {"code": "NS-E105", "title": "A download site cannot be reached", "step": "s03_download_models"}
    s = mark(fake_state(phase="downloading"), 5)
    assert report.status_line(s, err, NOW).endswith(
        " · last error NS-E105 (A download site cannot be reached)"
    )
    failed = fake_state(
        phase="failed", run="waiting", failure={"step": "s03_download_models", "code": "NS-E105"}
    )
    mark(failed, 5)
    line = report.status_line(failed, err, NOW)
    assert "STOPPED at step 6/" in line and "last error NS-E105" in line


def test_the_status_line_for_stopped_paused_not_started_and_finished():
    s = mark(fake_state(run="stopped", phase="downloading"), 5)
    assert "stopped at step 6/" in report.status_line(s, None, NOW)
    s = mark(fake_state(run="paused", phase="downloading"), 5)
    assert "step 6/" in report.status_line(s, None, NOW) and "(paused)" in report.status_line(s, None, NOW)
    s = mark(fake_state(run="idle", phase="downloading", startedAt=None), 5)
    line = report.status_line(s, None, NOW)
    assert "not started, next step 6/" in line and "elapsed" not in line
    s = mark(fake_state(run="finished", phase="done"), len(STEP_ORDER))
    line = report.status_line(s, None, NOW)
    assert "finished" in line and "100%" in line and "step " not in line


def test_scrub_answers_drops_every_key_even_nested():
    raw = {
        "pexels_api_key": "A",
        "pixabay_api_key": "B",
        "channel": {"x_api_key": "C", "name": "n"},
        "home_path": "D:/n",
    }
    assert report.scrub_answers(raw) == {"channel": {"name": "n"}, "home_path": "D:/n"}


def test_the_install_report_lists_every_step_with_status_and_seconds_and_no_keys():
    s = mark(fake_state(), 3)
    s["steps"][0].update(startedAt="2026-11-04T12:00:00", finishedAt="2026-11-04T12:00:42")
    s["steps"][4].update(status="skipped", detail="not selected")
    data = report.build_install_report(
        s,
        outcome="done",
        last_error=None,
        attempts={},
        flags={"tts_fallback": True, "schedule": "installed"},
        runtime={
            "tts": {"engine": "reference"},
            "whisper": {"device": "cuda"},
            "video": {"encoder": "h264_nvenc"},
        },
        answers={"home_path": "D:/n", "pexels_api_key": "SECRET"},
        free_gb=210.5,
        now=NOW,
    )
    assert data["version"] == "0.1.7" and data["outcome"] == "done" and data["durationS"] == 723
    assert set(data["steps"]) == set(STEP_ORDER)
    assert data["steps"][STEP_ORDER[0]] == {"status": "done", "seconds": 42.0}
    assert data["steps"][STEP_ORDER[4]]["status"] == "skipped" and data["steps"][STEP_ORDER[4]]["detail"]
    assert data["machine"]["gpu"] == "RTX 5060 Ti" and data["machine"]["freeGb"] == 210.5
    assert data["engines"]["tts"] == "reference" and data["engines"]["ttsFallback"] is True
    assert data["answers"] == {"home_path": "D:/n"} and "SECRET" not in json.dumps(data)
    assert data["failedStep"] is None and data["code"] is None


# ------------------------------------------------------------------------------------------ the controller
def test_copy_status_puts_the_line_on_the_clipboard_on_any_screen(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    status, body = s.ctl.do_action("copy-status")
    assert status == 200 and body["ok"] is True
    assert body["message"].startswith("Nightshift setup ") and s.clipboard == [body["message"]]
    s.ctl._set_failure("s03_download_models", "NS-E105", "", "", "no route", False)
    s.ctl._clear_failure()  # the partner pressed Retry
    line = s.ctl.do_action("copy-status")[1]["message"]
    assert "last error NS-E105 (A download site cannot be reached)" in line


def test_a_failure_writes_install_report_json_next_to_the_verify_report(tmp_path, monkeypatch):
    decisions: list = []
    s = Setup(tmp_path, monkeypatch, script=failing_script(False, decisions))
    s.accept()
    s.ctl.do_action("start")
    s.until(lambda st: st["run"] == "waiting", what="the failure")
    target = s.home / "logs" / "install-report.json"
    data = read_report(target, "failed")
    assert (
        data["outcome"] == "failed"
        and data["code"] == "NS-E109"
        and data["failedStep"] == "s03_download_models"
    )
    assert data["attempts"] == 1 and data["lastError"]["code"] == "NS-E109"
    assert data["steps"]["s03_download_models"]["status"] == "failed"
    assert data["answers"]["channel"]["display_name"] == "After Dark Tales"
    assert KEY_P not in target.read_text(encoding="utf-8") and "_api_key" not in json.dumps(data["answers"])
    s.ctl.do_action("retry")
    s.until(lambda st: st["run"] == "finished")
    read_report(target, "done")  # replaced at the end of the run


def test_a_finished_run_writes_the_report_and_adds_it_to_the_wizards_zip(tmp_path, monkeypatch):
    bundle = tmp_path / "desk" / "nightshift-diagnostics-2026-11-04-1200.zip"
    bundle.parent.mkdir()
    with zipfile.ZipFile(bundle, "w") as zf:
        zf.writestr("VERSION", "0.1.7\n")

    def extra(e) -> None:
        e.emit({"type": "bundle", "path": str(bundle)})

    s = Setup(tmp_path, monkeypatch, script=lambda e: finish_script(e, extra))
    s.accept()
    s.ctl.do_action("start")
    state = s.until(lambda st: st["run"] == "finished")
    data = read_report(s.home / "logs" / "install-report.json", "done")
    assert data["outcome"] == "done" and data["failedStep"] is None
    assert all(v["status"] == "done" for v in data["steps"].values())
    assert data["statusLine"].startswith("Nightshift setup ") and "finished" in data["statusLine"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and "install-report.json" not in zipfile.ZipFile(bundle).namelist():
        time.sleep(0.02)
    with zipfile.ZipFile(bundle) as zf:
        assert "install-report.json" in zf.namelist()
        assert json.loads(zf.read("install-report.json"))["outcome"] == "done"
    assert state["result"]["diagnosticsZip"] == str(bundle)


def test_a_stopped_run_writes_the_report_too(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    s.ctl._finish_stopped()
    data = json.loads((s.cfg.data_dir / "bootstrap" / "install-report.json").read_text(encoding="utf-8"))
    assert data["outcome"] == "stopped"  # no install folder yet: next to the setup logs


def test_the_diagnostics_zip_holds_the_setup_evidence_and_no_keys(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    s.accept()
    (s.home / "config").mkdir(parents=True, exist_ok=True)
    (s.home / "logs").mkdir(parents=True, exist_ok=True)
    (s.home / "logs" / "installer-2026-11-04.jsonl").write_text("{}\n", encoding="utf-8")
    (s.home / "config" / "wizard_flags.json").write_text('{"tts_fallback": true}', encoding="utf-8")
    (s.home / "config" / "install_state.json").write_text('{"steps": {}}', encoding="utf-8")
    (s.home / "config" / "answers.json").write_text(
        json.dumps({"home_path": str(s.home), "pexels_api_key": KEY_P, "pixabay_api_key": KEY_X}),
        encoding="utf-8",
    )
    s.cfg.data_dir.mkdir(parents=True, exist_ok=True)
    (s.cfg.data_dir / "setup-server.err").write_text("Traceback: boom\n", encoding="utf-8")
    (s.cfg.data_dir / "bootstrap").mkdir(exist_ok=True)
    (s.cfg.data_dir / "bootstrap" / "setup-ui.jsonl").write_text('{"text": "hello"}\n', encoding="utf-8")

    status, body = s.ctl.do_action("diagnostics")
    zip_path = Path(body["message"])
    assert status == 200 and zip_path.parent == tmp_path / "desktop"
    assert re.fullmatch(r"nightshift-diagnostics-\d{4}-\d{2}-\d{2}-\d{4}\.zip", zip_path.name)
    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert {
            "install-report.json",
            "install_state.json",
            "setup/wizard_flags.json",
            "setup/answers.json",
            "setup/preflight.json",
            "setup/setup-server.err",
            "setup/bootstrap/setup-ui.jsonl",
            "logs/installer-2026-11-04.jsonl",
        } <= names
        everything = b"".join(zf.read(n) for n in names)
        assert KEY_P.encode() not in everything and KEY_X.encode() not in everything
        assert "pexels_api_key" not in json.loads(zf.read("setup/answers.json"))
        assert json.loads(zf.read("install-report.json"))["version"]
        assert json.loads(zf.read("setup/preflight.json"))["gpu"]  # the hardware check
    assert s.clipboard[-1] == str(zip_path)


def test_the_logs_folder_can_be_opened_from_the_failed_screen_and_windows_pages_by_name(
    tmp_path, monkeypatch
):
    s = Setup(tmp_path, monkeypatch)
    s.accept()
    logs = s.snap()["answers"]["homePath"].rstrip("\\") + "\\logs"
    assert s.ctl.do_action("reveal", {"path": logs})[0] == 200
    assert s.fx.launched[-1] == ["explorer.exe", logs]
    assert s.ctl.do_action("open-windows-page", {"path": "power"})[0] == 200
    assert s.fx.launched[-1] == ["control.exe", "powercfg.cpl"]
    assert s.ctl.do_action("open-windows-page", {"path": "protection-history"})[0] == 200
    assert s.fx.launched[-1] == ["explorer.exe", "windowsdefender://threathistory"]
    assert s.ctl.do_action("open-windows-page", {"path": "cmd.exe"})[0] == 409


def test_the_result_says_whether_a_stock_footage_key_is_stored(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with_keys = Setup(tmp_path / "a", monkeypatch, script=finish_script)
    with_keys.accept()
    with_keys.ctl.do_action("start")
    assert with_keys.until(lambda st: st["run"] == "finished")["result"]["stockKeyMissing"] is False
    without = Setup(tmp_path / "b", monkeypatch, script=finish_script)
    without.accept(pexelsKey=None, pixabayKey=None)
    without.ctl.do_action("start")
    assert without.until(lambda st: st["run"] == "finished")["result"]["stockKeyMissing"] is True


def test_the_support_contact_comes_from_installer_support_json(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    assert set(s.snap()["support"]) == {"name", "driveUrl"}
    app = tmp_path / "app"
    (app / "installer").mkdir(parents=True)
    (app / "installer" / "support.json").write_text(
        json.dumps({"name": " Abdo ", "driveUrl": "https://drive.google.com/x"}), encoding="utf-8"
    )
    s.ctl.cfg.app_path = app
    assert s.ctl._load_support() == {"name": "Abdo", "driveUrl": "https://drive.google.com/x"}
    (app / "installer" / "support.json").write_text(
        json.dumps({"name": 5, "driveUrl": "javascript:alert(1)"}), encoding="utf-8"
    )
    assert s.ctl._load_support() == {"name": None, "driveUrl": None}


# ----------------------------------------------------------------------------------- the screens (sources)
def ui(name: str) -> str:
    return (UI / name).read_text(encoding="utf-8")


def test_every_screen_after_start_has_the_copy_status_button():
    for screen in ("Download", "Installing", "Verify", "Failed", "Done"):
        assert "<CopyStatusButton />" in ui(f"screens/{screen}.tsx"), screen
    welcome = ui("screens/Welcome.tsx")
    assert "{resume && <CopyStatusButton />}" in welcome  # the resume variant, not the first Welcome
    assert '"copy-status"' in ui("parts/CopyStatus.tsx") and "copy-status" in ui("api/types.ts")


def test_the_failed_screen_has_help_file_log_folder_and_the_protection_history_button():
    src, copy = ui("screens/Failed.tsx"), ui("copy.ts")
    assert 'act("diagnostics")' in src and 'act("reveal", { path: logs })' in src
    assert 'f.code === "NS-E113"' in src and 'path: "protection-history"' in src
    assert "state.support.name" in src
    assert 'saveHelp: "Save a help file"' in copy and 'openLogs: "Open log folder"' in copy
    assert "Send this file to ${who}" in copy and "Saved on your Desktop as ${name}" in copy


def test_the_done_screen_has_three_numbered_steps_the_key_reminder_and_keeps_the_warning_instructions():
    src, copy = ui("screens/Done.tsx"), ui("copy.ts")
    assert "<ol" in src and "t.steps[0], t.steps[1]" in src and "r.stockKeyMissing" in src
    assert "w.whatToDo" in src  # the instruction travels with the warning
    block = copy[copy.index("export const done = {") : copy.index("export const failed = {")]
    assert block.count('"Press ') == 2 and "Create it now" in block and "Open dashboard" in block
    assert "tomorrow morning" in block.lower() and "schedule" in block
    assert "Add a Pixabay key in Settings before the first video" in block


def test_the_copy_no_longer_says_what_is_false_and_the_code_table_is_the_servers():
    copy = ui("copy.ts")
    for gone in (
        "black setup window",
        "black window",
        "Nothing else was changed",
        "one click away",
        "Copy diagnostics",
        "50 Mbit/s",
        "Memory index",
        "libx264 (slower)",
    ):
        assert gone not in copy, gone
    assert 'import operatorCopy from "../../stage2/web/operator_copy.json"' in copy
    assert "export const codes: Record<string, CodeText> = operatorCopy;" in copy
    assert "Add a Pixabay key" in copy and "Kept on this PC" not in copy  # the honest list is the server's
    assert "Also left on this PC" in copy


def test_the_dashboard_settings_copy_names_the_update_zip_and_the_folder():
    settings = (ROOT / "ns_dashboard" / "ui" / "src" / "pages" / "Settings.tsx").read_text(encoding="utf-8")
    assert "A Nightshift-Update zip you were given (some updates still need the internet)" in settings
    assert "nightshift-app zip" not in settings and "in the Nightshift folder" not in settings
    assert "uninstallDir" in settings


def test_operator_md_and_the_server_copy_use_the_buttons_not_menu_paths():
    op = (ROOT / "OPERATOR.md").read_text(encoding="utf-8")
    assert "Open Protection history" in op and "Open power settings" in op and "Save a help file" in op
    assert "black window" not in op
    copy = json.loads((ROOT / "installer" / "stage2" / "web" / "operator_copy.json").read_text("utf-8"))
    assert (
        "Open power settings" in copy["NS-W107"]["whatToDo"]
        and "Power Options" not in copy["NS-W107"]["whatToDo"]
    )
    assert "Open Protection history" in copy["NS-E113"]["whatToDo"]
    assert all("Copy diagnostics" not in v["whatToDo"] for v in copy.values())
    hints = json.loads((ROOT / "contracts" / "errors.json").read_text("utf-8"))
    assert (
        "Open power settings" in hints["NS-W107"]["hint"]
        and "Open Protection history" in hints["NS-E113"]["hint"]
    )
    assert "allow list" in hints["NS-E113"]["hint"]
