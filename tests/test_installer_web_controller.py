"""C1: the setup controller with fakes: state transitions, actions, bootstrap, resume, wizard events."""

import json
import threading
import time
from pathlib import Path

import pytest

from installer.stage2.web import answers_view, controller, samples
from installer.stage2.web.controller import Config, SetupController
from installer.stage2.web.fakes import ScriptedEngine, fake_effects
from installer.stage2.web.model import ENGINE_STEPS, STEP_ORDER

ROOT = Path(__file__).resolve().parents[1]
KEY_P, KEY_X = "KEY-pexels-1a2b", "KEY-pixabay-3c4d"
LLM = "Qwen3-14B-Q4_K_M.gguf"


def step_event(step_id: str, status: str, detail: str | None = None) -> dict:
    return {"type": "step", "id": step_id, "status": status, "title": step_id, "detail": detail}


def draft(home: Path, **over) -> dict:
    data = {
        "ownerName": "Abdo",
        "channel": {"displayName": "After Dark Tales", "niche": "horror fiction", "language": "en"},
        "homePath": str(home), "llmTier": "small", "voice": {"speaker": "Ryan"},
        "modelsSource": {"kind": "internet", "path": None}, "runTime": "03:00", "mastersPerWeek": 1,
        "installCode": True, "theme": "dark", "pexelsKey": KEY_P, "pixabayKey": KEY_X,
    }
    data.update(over)
    return data


class Setup:
    """A controller on fakes, plus handles to everything it touched."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script=None, present=None) -> None:
        monkeypatch.setenv("NIGHTSHIFT_HOME", "placeholder")     # so the undo removes what b02 sets
        monkeypatch.delenv("NIGHTSHIFT_HOME")
        monkeypatch.setattr(answers_view, "MAX_HOME_LEN", 400)      # pytest's temp folders are deep
        monkeypatch.setattr(controller, "desktop_dir", lambda: tmp_path / "desktop")
        self.home = tmp_path / "home"
        self.fx, self.cmd = fake_effects(tmp_path / "fx", present=present)
        self.clipboard: list[str] = []
        self.fx.clipboard = self.clipboard.append
        self.engine = ScriptedEngine(script) if script else None
        if self.engine:
            self.fx.engine_factory = self.engine.factory()
        self.cfg = Config(app_path=ROOT, data_dir=tmp_path / "data", simulate=True, home=self.home)
        self.ctl = SetupController(self.cfg, self.fx)

    def snap(self) -> dict:
        return self.ctl.snapshot()

    def accept(self, **over) -> dict:
        result = self.ctl.submit_answers(draft(self.home, **over))
        assert result["ok"], result
        return result

    def until(self, pred, timeout: float = 20.0, what: str = "the state to change") -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = self.snap()
            if pred(s):
                return s
            time.sleep(0.02)
        raise AssertionError(f"timed out waiting for {what}; run={self.snap()['run']}")


def finish_script(e: ScriptedEngine, extra=None) -> None:
    """A wizard that does everything and exits 0."""
    assert e.wait_command("keys", 10)
    e.emit({"type": "hello", "pid": 1, "version": "0.1.0", "resumed": False})
    for sid in ENGINE_STEPS:
        e.emit({"type": "step", "id": sid, "status": "running", "title": sid, "detail": None})
        e.emit({"type": "step", "id": sid, "status": "done", "title": sid, "detail": None})
    if extra:
        extra(e)        # after s08 started: its "running" rows would otherwise replace the verify result
    e.exit(0)


# ------------------------------------------------------------------------------------------ first state
def test_the_first_state_is_the_welcome_screen_of_a_green_pc(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    state = s.snap()
    assert [st["id"] for st in state["steps"]] == list(STEP_ORDER)
    assert state["phase"] == "checks" and state["run"] == "idle" and state["uiStep"] == "welcome"
    assert state["blocking"] is False and state["resumed"] is False and state["answers"] is None
    assert state["answersLocked"] is False and state["failure"] is None and state["result"] is None
    assert state["machine"]["gpuName"] == "NVIDIA GeForce RTX 5060 Ti" and len(state["checks"]) == 10
    assert state["plan"]["tier"] == "small" and state["plan"]["estimate"]["label"]
    assert state["version"] == (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert state["rev"] >= 1 and state["host"] == "none"
    keys = {"schema", "rev", "version", "appPath", "phase", "run", "uiStep", "resumed", "prefilledFrom",
            "startedAt", "updatedAt", "host", "machine", "checks", "checkedAt", "checking", "blocking",
            "plan",
            "answers", "answersLocked", "steps", "downloads", "verify", "failure", "notices", "result", "log"}
    assert set(state) == keys
    json.dumps(state)                                   # everything is JSON
    rows = {r["key"]: r for r in state["downloads"]["items"]}
    assert rows["git"]["status"] == "done" and rows["git"]["note"] == "already here"
    assert rows["llama_cuda13"]["status"] == "queued" and rows["Qwen3-14B-Q4_K_M.gguf"]["bytesTotal"] > 9e9


def test_ui_step_moves_between_the_pre_start_screens(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    assert s.ctl.set_ui_step("choices")[0] == 200
    assert s.snap()["uiStep"] == "choices" and s.snap()["phase"] == "choices"
    assert s.ctl.set_ui_step("checks")[0] == 200 and s.snap()["phase"] == "checks"


# ------------------------------------------------------------------------------------------ answers
def test_bad_answers_are_refused_with_field_errors_and_good_ones_rebuild_the_plan(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    bad = s.ctl.submit_answers(draft(s.home, channel={"displayName": "", "niche": ""}, pexelsKey=None))
    assert bad["ok"] is False
    assert {e["field"] for e in bad["errors"]} >= {"channel.displayName", "channel.niche", "pexelsKey"}
    assert s.snap()["answers"] is None
    good = s.accept(llmTier="balanced")
    assert good["plan"]["tier"] == "balanced" and good["answers"]["llmTier"] == "balanced"
    state = s.snap()
    assert state["answers"]["channel"]["displayName"] == "After Dark Tales" and state["phase"] == "choices"
    assert state["answers"]["pexelsKeySet"] is True
    labels = {r["label"] for r in state["downloads"]["items"]}
    assert "Story model (Balanced)" in labels
    assert KEY_P not in json.dumps(state) and KEY_X not in json.dumps(state)      # keys stay in memory only


def test_picking_another_drive_refreshes_the_disk_rows_even_with_an_incomplete_draft(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    letter = str(s.home)[0]
    s.ctl.pf["drives"] = [{"letter": letter, "free_gb": 20.0, "type": "SSD"},
                          {"letter": "E", "free_gb": 300.0, "type": "SSD"}]
    result = s.ctl.submit_answers(draft(s.home, homePath="E:\\Nightshift", channel={}, pexelsKey=None))
    assert result["ok"] is False                                   # the channel is missing...
    disk = next(c for c in s.snap()["checks"] if c["id"] == "disk")
    assert disk["status"] == "ok" and "E:" in disk["value"]        # ...but the drive was re-evaluated
    s.ctl.submit_answers(draft(s.home, homePath=str(s.home), channel={}, pexelsKey=None))
    assert next(c for c in s.snap()["checks"] if c["id"] == "disk")["status"] == "fail"
    assert s.snap()["blocking"] is True


def test_an_answers_file_can_be_loaded_through_the_same_validation(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    raw = json.loads((ROOT / "installer" / "answers.example.json").read_text(encoding="utf-8"))
    raw.update(home_path=str(s.home), pexels_api_key=KEY_P, pixabay_api_key=KEY_X)
    f = tmp_path / "mine.json"
    f.write_text(json.dumps(raw), encoding="utf-8")
    result = s.ctl.submit_answers({"path": str(f)})
    assert result["ok"] and result["answers"]["channel"]["displayName"] == "After Dark Tales"
    missing = s.ctl.submit_answers({"path": str(tmp_path / "nope.json")})
    assert missing["ok"] is False and "could not be read" in missing["errors"][0]["message"]


def test_start_needs_answers_and_a_pc_that_does_not_block(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch, script=finish_script)
    status, body = s.ctl.do_action("start")
    assert status == 409 and body["ok"] is False and "settings" in body["message"]
    s.accept()
    s.ctl.store.update(lambda st: st["checks"][0].update(blocking=True, status="fail"))
    assert s.ctl.do_action("start")[0] == 409 and s.snap()["run"] == "idle"


# ------------------------------------------------------------------------------------------ a whole run
def test_a_whole_scripted_run_bootstraps_then_runs_the_wizard_and_finishes(tmp_path, monkeypatch):
    def extra(e: ScriptedEngine) -> None:
        wav = samples.write_sine_wav(s.home / "cache" / "setup-test" / "voice-sample.wav", 1.0)
        size = 9001752960
        e.emit({"type": "plan", "items": [{"key": LLM, "file": LLM, "bytes": size, "via": "download"}],
                "totalBytes": size})
        e.emit({"type": "file", "key": LLM, "event": "start", "total": size})
        e.emit({"type": "file", "key": LLM, "event": "progress", "done": 100, "total": size})
        e.emit({"type": "file", "key": LLM, "event": "finish", "outcome": "downloaded"})
        e.emit({"type": "warn", "code": "NS-W302", "detail": "plain decoding"})
        e.emit({"type": "verify", "checks": [
            {"name": "llm", "passed": True, "value": "54.2", "limit": None}]})
        e.emit({"type": "sample", "kind": "voice", "path": str(wav), "seconds": 1.0})
        e.emit({"type": "bundle", "path": "C:\\Users\\x\\Desktop\\nightshift-diagnostics-1.zip"})

    s = Setup(tmp_path, monkeypatch, script=lambda e: finish_script(e, extra))
    s.accept()
    status, body = s.ctl.do_action("start")
    assert status == 200 and body["ok"] is True
    state = s.until(lambda st: st["run"] == "finished", what="the run to finish")
    assert state["phase"] == "done" and state["startedAt"] and state["failure"] is None
    assert all(st["status"] == "done" for st in state["steps"])
    # bootstrap: nothing to install with winget, the setup environment is built with uv
    assert not any(c[0].lower().startswith("winget") for c in s.cmd.calls)
    assert any(len(c) > 1 and c[1] == "venv" for c in s.cmd.calls) and any(
        len(c) > 2 and c[1:3] == ["pip", "install"] for c in s.cmd.calls)
    assert (s.home / "venvs" / "studio" / "Scripts" / "python.exe").is_file() and (
        s.home / "bin" / "uv" / "uv.exe").is_file()
    assert s.fx.home_var["NIGHTSHIFT_HOME"] == str(s.home)
    written = json.loads((s.home / "config" / "answers.json").read_text(encoding="utf-8"))
    assert written["channel"]["display_name"] == "After Dark Tales" and written["pexels_api_key"] == ""
    install_state = json.loads((s.home / "config" / "install_state.json").read_text(encoding="utf-8"))
    assert {i: install_state["steps"][i]["status"] for i in ("b01_tools", "b02_home", "b03_setup_env")} == {
        "b01_tools": "done", "b02_home": "done", "b03_setup_env": "done"}
    # the keys went to the wizard on stdin and nowhere else
    assert s.engine.argv[:3] == [s.cfg.python, "-X", "utf8"] and "--ui-bridge" in s.engine.argv
    assert "--simulate-downloads" in s.engine.argv and "--halt-after" not in s.engine.argv
    # events became screen data
    llm = next(r for r in state["downloads"]["items"] if r["key"] == "Qwen3-14B-Q4_K_M.gguf")
    assert llm["status"] == "done" and llm["bytesDone"] == llm["bytesTotal"] == 9001752960
    assert [n["code"] for n in state["notices"]] == ["NS-W302"]
    assert state["verify"]["voice"]["speaker"] == "Ryan" and len(state["verify"]["voice"]["bars"]) == 28
    first_check = state["verify"]["checks"][0]
    assert first_check["label"] == "Story model" and first_check["value"] == "54.2 tokens/s"
    result = state["result"]
    assert result["outbox"] == str(s.home / "outbox") and result["runTime"] == "03:00"
    assert result["diagnosticsZip"].endswith("nightshift-diagnostics-1.zip")
    assert [i["label"] for i in result["installed"]][0] == "Story model (Small)"
    assert KEY_P not in json.dumps(state)
    assert s.ctl.file_path("voice-sample.wav").is_file()


def test_the_keys_are_sent_as_the_first_command_of_the_wizard(tmp_path, monkeypatch):
    got: list[dict] = []

    def script(e: ScriptedEngine) -> None:
        got.append(e.wait_command("keys", 10))
        e.exit(0)

    s = Setup(tmp_path, monkeypatch, script=script)
    s.accept()
    s.ctl.do_action("start")
    s.until(lambda st: st["run"] == "finished")
    assert got[0]["pexels_api_key"] == KEY_P and got[0]["pixabay_api_key"] == KEY_X


# ------------------------------------------------------------------------------------------ failures
def failing_script(optional: bool, decisions: list):
    def script(e: ScriptedEngine) -> None:
        assert e.wait_command("keys", 10)
        e.emit(step_event("s03_download_models", "running"))
        e.emit({"type": "failure", "step": "s03_download_models", "code": "NS-E109", "message": "damaged",
                "hint": "run again", "detail": "qwen.gguf: expected 4b9a33a2, got 0c1d", "optional": optional,
                "attempt": 1})
        decisions.append(e.wait_command("decision", 10))
        e.emit({"type": "step", "id": "s03_download_models", "status": "done", "title": "x", "detail": None})
        e.exit(0)

    return script


def test_a_failure_shows_the_failed_screen_and_retry_sends_the_decision(tmp_path, monkeypatch):
    decisions: list = []
    s = Setup(tmp_path, monkeypatch, script=failing_script(False, decisions))
    s.accept()
    s.ctl.do_action("start")
    state = s.until(lambda st: st["run"] == "waiting", what="the failure")
    assert state["phase"] == "failed"
    f = state["failure"]
    assert f["code"] == "NS-E109" and f["title"] == "A downloaded file is damaged"
    assert f["canSkip"] is False and f["stepTitle"] == "Download AI models" and f["attempts"] == 1
    assert "expected 4b9a33a2" in f["detail"]
    assert "Retry" in f["whatToDo"] and f["at"]
    assert next(st for st in state["steps"] if st["id"] == "s03_download_models")["status"] == "failed"
    assert s.ctl.do_action("skip")[0] == 409                        # a required step cannot be skipped
    assert s.ctl.do_action("start")[0] == 409
    assert s.ctl.do_action("retry")[0] == 200
    s.until(lambda st: st["run"] == "finished")
    assert decisions[0] == {"cmd": "decision", "action": "retry"}
    assert s.snap()["failure"] is None


def test_an_optional_step_can_be_skipped(tmp_path, monkeypatch):
    decisions: list = []
    s = Setup(tmp_path, monkeypatch, script=failing_script(True, decisions))
    s.accept()
    s.ctl.do_action("start")
    state = s.until(lambda st: st["run"] == "waiting")
    assert state["failure"]["canSkip"] is True
    assert s.ctl.do_action("skip")[0] == 200
    s.until(lambda st: st["run"] == "finished")
    assert decisions[0] == {"cmd": "decision", "action": "skip"}


def test_a_wizard_that_dies_without_a_failure_event_becomes_an_unexpected_failure(tmp_path, monkeypatch):
    def script(e: ScriptedEngine) -> None:
        e.wait_command("keys", 10)
        e.on_line("Traceback (most recent call last):")
        e.on_line("ValueError: boom")
        e.exit(1)

    s = Setup(tmp_path, monkeypatch, script=script)
    s.accept()
    s.ctl.do_action("start")
    state = s.until(lambda st: st["run"] == "waiting")
    assert state["failure"]["code"] == "unexpected" and "ValueError: boom" in state["failure"]["detail"]
    assert state["phase"] == "failed" and state["failure"]["canSkip"] is False


# --------------------------------------------------------------------------------- pause, resume, stop
def test_pause_resume_and_stop_reach_the_wizard(tmp_path, monkeypatch):
    got: dict = {}

    def script(e: ScriptedEngine) -> None:
        e.wait_command("keys", 10)
        got["pause"] = e.wait_command("pause", 10)
        got["resume"] = e.wait_command("resume", 10)
        got["stop"] = e.wait_command("stop", 10)
        e.exit(130)

    s = Setup(tmp_path, monkeypatch, script=script)
    s.accept()
    s.ctl.do_action("start")
    s.until(lambda st: st["run"] == "running" and s.engine.alive, what="the wizard to start")
    assert s.ctl.do_action("resume")[0] == 409                           # nothing is paused yet
    assert s.ctl.do_action("pause")[0] == 200
    state = s.snap()
    assert state["run"] == "paused" and state["downloads"]["paused"] is True
    assert s.ctl.do_action("resume")[0] == 200 and s.snap()["run"] == "running"
    assert s.ctl.do_action("close-setup")[0] == 409                      # not while it runs
    assert s.ctl.do_action("cancel")[0] == 200
    state = s.until(lambda st: st["run"] == "stopped", what="the stop")
    assert got["pause"] and got["resume"] and got["stop"]
    assert state["phase"] != "failed" and state["failure"] is None
    assert s.ctl.do_action("close-setup")[0] == 200 and s.ctl.shutdown.is_set()


def test_pausing_before_the_wizard_exists_takes_effect_when_it_starts(tmp_path, monkeypatch):
    release = threading.Event()
    got: dict = {}

    def script(e: ScriptedEngine) -> None:
        e.wait_command("keys", 10)
        got["pause"] = e.wait_command("pause", 10)
        e.exit(0)

    s = Setup(tmp_path, monkeypatch, script=script)
    s.accept()
    venv = s.home / "venvs" / "studio"          # a finished setup environment: b03 waits for no tool
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"MZ")
    (venv / ".ns-setup-env.done").write_text(str(ROOT), encoding="utf-8")
    original = s.ctl._boot

    def slow_boot():
        release.wait(10)                                  # hold the bootstrap so the pause comes first
        return original()

    s.ctl._boot = slow_boot
    s.ctl.do_action("start")
    assert s.ctl.do_action("pause")[0] == 200 and s.snap()["run"] == "pausing"
    release.set()
    s.until(lambda st: st["run"] == "finished", what="the wizard to end")
    assert got["pause"] == {"cmd": "pause"}     # sent as soon as the wizard started


# ------------------------------------------------------------------------------------------ bootstrap
def test_missing_tools_are_installed_with_winget_and_shown_as_rows(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch, script=finish_script, present={"winget.exe"})
    s.accept()
    s.ctl.do_action("start")
    state = s.until(lambda st: st["run"] == "finished")
    installs = [c for c in s.cmd.calls if c[0].lower().startswith("winget")]
    ids = [c[c.index("--id") + 1] for c in installs]
    assert ids == ["Git.Git", "Microsoft.WindowsTerminal", "Gyan.FFmpeg"]
    rows = {r["key"]: r for r in state["downloads"]["items"]}
    assert rows["git"]["status"] == "done" and rows["git"]["note"] == "installed with winget"
    assert rows["ffmpeg"]["status"] == "done" and rows["setup_env"]["status"] == "done"


def test_no_winget_and_no_git_is_ns_e111_and_retry_carries_on(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch, script=finish_script, present=set())
    s.accept()
    s.ctl.do_action("start")
    state = s.until(lambda st: st["run"] == "waiting", what="NS-E111")
    assert state["failure"]["code"] == "NS-E111" and state["failure"]["title"] == "App Installer is missing"
    assert state["failure"]["step"] == "b01_tools" and state["failure"]["canSkip"] is False
    assert state["phase"] == "failed" and not s.engine.alive
    s.cmd.present.update({"git.exe", "ffmpeg.exe", "wt.exe", "winget.exe"})      # the partner installed it
    assert s.ctl.do_action("retry")[0] == 200
    state = s.until(lambda st: st["run"] == "finished")
    assert state["failure"] is None and state["steps"][0]["status"] == "done"


def test_a_failed_uv_install_is_ns_e105_and_a_blocked_file_is_ns_e113(tmp_path, monkeypatch):
    from installer.stage2.web.bootstrap import CmdResult

    s = Setup(tmp_path, monkeypatch, script=finish_script)
    s.cmd.fail["pip install"] = CmdResult(1, "", "error: connection reset")
    s.accept()
    s.ctl.do_action("start")
    state = s.until(lambda st: st["run"] == "waiting")
    assert state["failure"]["code"] == "NS-E105" and "connection reset" in state["failure"]["detail"]
    assert state["failure"]["step"] == "b03_setup_env"
    s.cmd.fail["pip install"] = CmdResult(1, "", "Operation did not complete successfully because the file "
                                          "contains a virus or potentially unwanted software")
    s.ctl.do_action("retry")
    state = s.until(lambda st: st["run"] == "waiting" and st["failure"]["attempts"] == 2)
    assert state["failure"]["code"] == "NS-E113"
    assert state["failure"]["title"] == "Windows Security blocked a file"


# ------------------------------------------------------------------------------------------ resume
def test_a_second_run_resumes_what_the_first_one_finished(tmp_path, monkeypatch):
    first = Setup(tmp_path, monkeypatch)
    cfg_dir = first.home / "config"
    cfg_dir.mkdir(parents=True)
    saved = answers_view.draft_to_answers(draft(first.home))
    answers_view.write_answers(cfg_dir / "answers.json", saved)
    (cfg_dir / "install_state.json").write_text(json.dumps({
        "version": "0.1.0", "app_path": str(ROOT), "profile": "gpu", "started_at": "2026-11-02T21:00:00",
        "completed_at": None,
        "steps": {i: {"status": "done", "error": None, "finished_at": "2026-11-02T21:30:00"} for i in (
            "b01_tools", "b02_home", "b03_setup_env", "s01_questions", "s02_download_bins")}}),
        encoding="utf-8")
    part = first.home / "models" / "llm" / "Qwen3-14B-Q4_K_M.gguf.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"x" * 2048)

    second = Setup(tmp_path, monkeypatch, script=finish_script)
    state = second.snap()
    assert state["resumed"] is True and state["answersLocked"] is True
    assert state["startedAt"] == "2026-11-02T21:00:00"
    assert state["answers"]["channel"]["displayName"] == "After Dark Tales"
    assert state["answers"]["pexelsKeySet"]
    done = [st["id"] for st in state["steps"] if st["status"] == "done"]
    assert done == ["b01_tools", "b02_home", "b03_setup_env", "s01_questions", "s02_download_bins"]
    row = next(r for r in state["downloads"]["items"] if r["key"] == "Qwen3-14B-Q4_K_M.gguf")
    assert row["bytesDone"] == 2048 and row["note"].startswith("will continue from")
    refused = second.ctl.submit_answers(draft(second.home))
    assert refused["ok"] is False                                    # the answers are locked once s01 is done
    assert second.ctl.do_action("start")[0] == 200                   # no new answers needed
    state = second.until(lambda st: st["run"] == "finished")
    assert state["phase"] == "done"
    assert json.loads((cfg_dir / "answers.json").read_text(encoding="utf-8")) == saved      # left alone


# ------------------------------------------------------------------------------------------ events
def test_unknown_download_rows_and_the_whisper_side_files_are_handled(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    ev = s.ctl._on_event
    ev({"type": "plan", "items": [{"key": "Qwen3-TTS-fallback.safetensors", "file": "x.safetensors",
                                   "bytes": 5000, "via": "download"}], "totalBytes": 5000})
    rows = {r["key"]: r for r in s.snap()["downloads"]["items"]}
    assert rows["Qwen3-TTS-fallback.safetensors"]["label"] == "Backup voice engine files"
    assert rows["Qwen3-TTS-fallback.safetensors"]["group"] == "models"
    ev({"type": "file", "key": "whisper/model.bin", "event": "start", "total": 1000})
    ev({"type": "file", "key": "whisper/config.json", "event": "start", "total": 10})
    ev({"type": "file", "key": "whisper/model.bin", "event": "progress", "done": 500, "total": 1000})
    row = next(r for r in s.snap()["downloads"]["items"] if r["key"] == "whisper")
    assert row["status"] == "active" and row["bytesTotal"] == 1010 and row["bytesDone"] == 500
    ev({"type": "file", "key": "whisper/model.bin", "event": "finish", "outcome": "downloaded"})
    assert next(r for r in s.snap()["downloads"]["items"] if r["key"] == "whisper")["status"] == "active"
    ev({"type": "file", "key": "whisper/config.json", "event": "finish", "outcome": "downloaded"})
    row = next(r for r in s.snap()["downloads"]["items"] if r["key"] == "whisper")
    assert row["status"] == "done" and row["bytesDone"] == 1010
    ev({"type": "file", "key": "Qwen3-14B-Q4_K_M.gguf", "event": "start", "total": 100})
    ev({"type": "file", "key": "Qwen3-14B-Q4_K_M.gguf", "event": "finish", "outcome": "copied"})
    llm = next(r for r in s.snap()["downloads"]["items"] if r["key"] == "Qwen3-14B-Q4_K_M.gguf")
    assert llm["status"] == "done" and llm["note"] == "copied from your folder"


def test_warnings_failures_steps_and_verify_rows_map_into_the_state(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    ev = s.ctl._on_event
    ev({"type": "warn", "code": "NS-W302", "detail": ""})
    ev({"type": "warn", "code": "NS-W302", "detail": ""})
    ev({"type": "warn", "code": "prompt", "detail": "Name?"})
    state = s.snap()
    assert [n["code"] for n in state["notices"]] == ["NS-W302"]
    assert state["notices"][0]["title"] == "Faster decoding was not faster here"
    assert any("asked a question" in line["text"] for line in state["log"])
    ev(step_event("s05_opencode_prewarm", "skipped", "not selected"))
    st = next(x for x in s.snap()["steps"] if x["id"] == "s05_opencode_prewarm")
    assert st["status"] == "skipped" and st["detail"] == "not selected" and st["finishedAt"]
    ev(step_event("s06_tune", "running"))
    ev({"type": "say", "text": "Trying the story model at different settings", "style": None})
    st = next(x for x in s.snap()["steps"] if x["id"] == "s06_tune")
    assert st["status"] == "running" and st["detail"].startswith("Trying the story model") and st["startedAt"]
    ev({"type": "verify", "checks": [
        {"name": "llm", "passed": True, "value": "54.2", "limit": None},
        {"name": "embed", "passed": True, "value": None, "limit": None},
        {"name": "tts", "passed": True, "value": "0.31", "limit": None},
        {"name": "whisper", "passed": True, "value": None, "limit": None},
        {"name": "nvenc", "passed": False, "value": None, "limit": None},
        {"name": "mtp", "passed": False, "value": None, "limit": None},
        {"name": "bench.llm", "passed": True, "value": "1", "limit": None}]})
    checks = {c["name"]: c for c in s.snap()["verify"]["checks"]}
    assert list(checks) == ["llm", "embed", "tts", "whisper", "nvenc", "mtp"]      # bench rows stay hidden
    assert checks["tts"]["value"].endswith("0.31 RTF")
    assert (checks["nvenc"]["status"], checks["nvenc"]["value"]) == ("fallback", "libx264 (slower)")
    assert (checks["mtp"]["status"], checks["mtp"]["value"]) == ("fallback", "off")
    assert checks["whisper"]["value"] == "graphics card" and checks["embed"]["value"] == "ready"


def test_the_test_render_note_appears_when_the_samples_step_warns(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    s.ctl._on_event(step_event("v01_samples", "warning", "NS-W403"))
    note = s.snap()["verify"]["renderNote"]
    assert "setup test video could not be made" in note.lower() and "Setup is complete" in note


# ------------------------------------------------------------------------------------------ other actions
def test_recheck_open_dashboard_reveal_elevate_and_diagnostics(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    assert s.ctl.do_action("open-dashboard")[0] == 409
    assert s.ctl.do_action("recheck")[0] == 200
    state = s.until(lambda st: not st["checking"] and any("Checks done" in x["text"] for x in st["log"]),
                    what="the re-check")
    assert state["checkedAt"]

    bump = [c for c in s.snap()["checks"] if c["id"] == "longpaths"][0]      # make the row a warning
    s.ctl.store.update(lambda st: [c for c in st["checks"] if c["id"] == bump["id"]][0].update(status="warn"))
    assert s.ctl.do_action("elevate-longpaths")[0] == 200
    assert next(c for c in s.snap()["checks"] if c["id"] == "longpaths")["status"] == "ok"
    assert s.ctl.do_action("elevate-longpaths")[0] == 409

    s.ctl._started = True
    outbox = str(s.home / "outbox")

    def finish(st: dict) -> None:
        for step in st["steps"]:
            step["status"] = "done"
        st["run"] = "finished"
        st["result"] = {"home": str(s.home), "outbox": outbox, "diagnosticsZip": None}

    s.ctl.store.update(finish)
    assert s.snap()["phase"] == "done"
    status, body = s.ctl.do_action("open-dashboard")
    assert status == 200 and body["message"] == "Opening the dashboard"
    argv = s.fx.launched[-1]
    assert argv[0] == "powershell.exe" and argv[-1].endswith("nightshift-dashboard.ps1")
    assert s.ctl.do_action("reveal", {"path": outbox})[0] == 200
    assert s.fx.launched[-1] == ["explorer.exe", outbox]
    assert s.ctl.do_action("reveal", {"path": "C:\\Windows"})[0] == 409

    status, body = s.ctl.do_action("diagnostics")
    assert status == 200 and body["message"].endswith(".zip") and Path(body["message"]).is_file()
    assert s.clipboard == [body["message"]]                           # the path is copied for the partner
    assert s.ctl.do_action("close-setup")[0] == 200 and s.ctl.shutdown.is_set()


def test_the_file_allow_list_maps_names_to_folders_inside_the_install(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    assert s.ctl.file_path("voice-sample.wav") == s.home / "cache" / "setup-test" / "voice-sample.wav"
    assert s.ctl.file_path("render-test.mp4").name == "render-test.mp4"
    assert s.ctl.file_path("verify-report.json") == s.home / "logs" / "verify-report.json"
    for name in ("secret.txt", "../config/answers.json", "install_state.json"):
        assert s.ctl.file_path(name) is None


def test_the_sample_helpers_and_labels_cover_every_step():
    from installer.stage2.web import labels
    from installer.stage2.web.model import STEP_PHASE

    assert set(labels.STEP_TITLES) == set(STEP_ORDER) == set(STEP_PHASE)
    assert labels.verify_label("bench.llm") is None and labels.verify_label("nvenc_encode") == "Video encoder"
    assert labels.verify_label("odd_check") == "odd_check"
    assert labels.row_key("whisper/config.json") == "whisper" and labels.row_key("yunet") == "yunet"
