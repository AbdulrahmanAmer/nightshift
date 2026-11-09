"""C1: the setup controller with fakes: state transitions, actions, bootstrap, resume, wizard events."""

import json
import threading
import time
from pathlib import Path

import pytest

from installer.stage2.web import answers_view, controller, preflight, samples
from installer.stage2.web.controller import Config, SetupController
from installer.stage2.web.fakes import ScriptedEngine, fake_effects, sample_handover
from installer.stage2.web.model import ENGINE_STEPS, STEP_ORDER
from ns_common.paths import Paths
from ns_common.secret_file import store_secret

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
    assert state["machine"]["gpuName"] == "NVIDIA GeForce RTX 5060 Ti" and len(state["checks"]) == 11
    assert state["plan"]["tier"] == "balanced" and state["plan"]["recommendedTier"] == "balanced"
    assert state["plan"]["estimate"]["label"]
    assert state["version"] == (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert state["rev"] >= 1 and state["host"] == "none"
    keys = {"schema", "rev", "version", "appPath", "phase", "run", "uiStep", "resumed", "prefilledFrom",
            "startedAt", "updatedAt", "host", "devMode", "machine", "checks", "checkedAt", "checking",
            "blocking", "plan", "support",
            "answers", "answersLocked", "steps", "downloads", "verify", "failure", "notices", "result", "log"}
    assert set(state) == keys
    json.dumps(state)                                   # everything is JSON
    rows = {r["key"]: r for r in state["downloads"]["items"]}
    assert rows["vcruntime"]["status"] == "done" and rows["vcruntime"]["note"] == "already here"
    assert "git" not in rows                        # Git is not something setup installs
    assert rows["llama_cuda13"]["status"] == "queued"
    assert rows["Qwen3.6-35B-A3B-APEX-MTP-I-Mini.gguf"]["bytesTotal"] > 14e9    # Balanced is the default


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
    assert {e["field"] for e in bad["errors"]} >= {"channel.displayName", "channel.niche"}
    assert "pexelsKey" not in {e["field"] for e in bad["errors"]}     # Pexels is optional (Pixabay given)
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
            {"name": "llama.tok_s", "passed": True, "value": "54.2", "limit": None}]})
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
    assert first_check["label"] == "Story model" and first_check["value"] == "works"
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
    assert ids == ["Microsoft.VCRedist.2015+.x64", "Gyan.FFmpeg", "Microsoft.WindowsTerminal"]
    assert "Git.Git" not in ids                     # no machine-wide installer, no admin prompt for Git
    assert all("--source" in c and "winget" in c for c in installs)
    rows = {r["key"]: r for r in state["downloads"]["items"]}
    assert rows["vcruntime"]["status"] == "done" and rows["vcruntime"]["note"] == "installed with winget"
    assert rows["ffmpeg"]["status"] == "done" and rows["setup_env"]["status"] == "done"
    assert s.fx.fetched == [] and s.fx.elevated == []       # winget did it: no direct download


def test_no_winget_means_the_pinned_direct_downloads(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch, script=finish_script, present=set())
    s.accept()
    s.ctl.do_action("start")
    state = s.until(lambda st: st["run"] == "finished")
    pins = s.ctl.manifest["bins"]
    assert [u for u, _ in s.fx.fetched] == [pins["vcredist"]["url"], pins["ffmpeg"]["url"]]
    assert [h for _, h in s.fx.fetched] == [pins["vcredist"]["sha256"], pins["ffmpeg"]["sha256"]]
    exe, args = s.fx.elevated[0]
    assert exe.endswith("vc_redist.x64.exe") and args == ["/install", "/quiet", "/norestart"]
    ffmpeg_dir = s.home / "bin" / "ffmpeg"
    assert (ffmpeg_dir / "ffmpeg.exe").is_file() and (ffmpeg_dir / "ffprobe.exe").is_file()
    assert not (ffmpeg_dir / "ffplay.exe").exists()
    assert s.fx.path_adds == [str(ffmpeg_dir)]              # the folder joins the user's PATH
    assert not any(c[0].lower().startswith("winget") for c in s.cmd.calls)
    rows = {r["key"]: r for r in state["downloads"]["items"]}
    assert rows["vcruntime"]["note"] == "installed from the pinned download"
    assert rows["ffmpeg"]["note"] == "installed from the pinned download"
    assert rows["terminal"]["status"] == "skipped"          # optional, and winget is the only way to get it
    assert state["failure"] is None and state["steps"][0]["status"] == "done"


def test_a_declined_permission_prompt_is_ns_e114_and_retry_carries_on(tmp_path, monkeypatch):
    from installer.stage2.web.bootstrap import CmdResult

    s = Setup(tmp_path, monkeypatch, script=finish_script, present={"ffmpeg.exe", "wt.exe"})
    s.fx.run_elevated = lambda exe, args: CmdResult(1223, "", "The operation was canceled by the user.")
    s.accept()
    s.ctl.do_action("start")
    state = s.until(lambda st: st["run"] == "waiting", what="NS-E114")
    assert state["failure"]["code"] == "NS-E114" and state["failure"]["step"] == "b01_tools"
    assert state["failure"]["title"] == "A Windows component is missing"
    assert "declined" in state["failure"]["detail"] and state["failure"]["canSkip"] is False
    s.cmd.present.add("vcruntime")                          # the partner installed it from the link
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
    store_secret(Paths(first.home), "pexels_api_key", KEY_P, protect=lambda b: b)     # s01 stored it earlier

    second = Setup(tmp_path, monkeypatch, script=finish_script)
    state = second.snap()
    assert state["resumed"] is True and state["answersLocked"] is True
    assert state["phase"] == "downloading" and state["run"] == "idle"   # Welcome resume screen, not Choices
    assert state["startedAt"] == "2026-11-02T21:00:00"
    assert state["answers"]["channel"]["displayName"] == "After Dark Tales"
    assert state["answers"]["pexelsKeySet"] and not state["answers"]["pixabayKeySet"]
    assert state["notices"] == []                                    # one key is enough
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


# ------------------------------------------------------------------- INSTALL.cmd over a finished install
def finished_install(s, old_zip: Path) -> None:
    """What a completed setup leaves behind: the program copy, the answers and every step done."""
    from installer.stage2.web import appcopy

    appcopy.install_app(old_zip, s.home)
    cfg_dir = s.home / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    answers_view.write_answers(cfg_dir / "answers.json", answers_view.draft_to_answers(draft(s.home)))
    (cfg_dir / "install_state.json").write_text(json.dumps({
        "version": "0.1.4", "app_path": str(s.home / "app"), "profile": "gpu",
        "started_at": "2026-11-02T21:00:00", "completed_at": "2026-11-02T22:05:00",
        "steps": {i: {"status": "done", "error": None, "finished_at": "2026-11-02T22:00:00"}
                  for i in STEP_ORDER}}), encoding="utf-8")


def test_install_cmd_over_a_finished_install_of_the_same_program_shows_done(tmp_path, monkeypatch):
    from tests.test_installer_app_copy import app_setup, fake_zip_folder

    zip_a = fake_zip_folder(tmp_path / "zip-a", "0.1.4")
    first = app_setup(tmp_path, monkeypatch, zip_a)
    finished_install(first, zip_a)

    again = app_setup(tmp_path, monkeypatch, zip_a)             # a new server over the finished install
    state = again.snap()
    assert state["phase"] == "done" and state["run"] == "finished" and state["answersLocked"] is True
    assert all(st["status"] == "done" for st in state["steps"])
    result = state["result"]
    assert result["home"] == str(again.home) and result["appPath"] == str(again.home / "app")
    assert result["outbox"] == str(again.home / "outbox") and result["runTime"] == "03:00"
    assert result["durationS"] == 65 * 60                          # from the saved start and finish
    assert again.ctl.do_action("open-dashboard")[0] == 200         # the Done screen's button works
    assert any(a[-1].endswith("nightshift-dashboard.ps1") for a in again.fx.launched)
    assert KEY_P not in json.dumps(state)


def test_a_window_over_a_finished_install_whose_program_is_gone_installs_again(tmp_path, monkeypatch):
    """The windowed dev install after an uninstall (config kept) reported phase done, run finished: the old
    install_state.json said every step was done although app and venvs were gone."""
    import shutil

    from tests.test_installer_app_copy import app_setup, fake_zip_folder

    zip_a = fake_zip_folder(tmp_path / "zip-a", "0.1.4")
    first = app_setup(tmp_path, monkeypatch, zip_a)
    finished_install(first, zip_a)
    shutil.rmtree(first.home / "app")                           # what an uninstall removes

    again = app_setup(tmp_path, monkeypatch, zip_a)
    state = again.snap()
    assert state["phase"] != "done" and state["run"] != "finished" and state["answersLocked"] is True
    by_id = {st["id"]: st["status"] for st in state["steps"]}
    assert by_id["s01_questions"] == "done"
    assert by_id["s04_venvs"] == "pending" and by_id["s08_smoke"] == "pending"
    saved = json.loads((again.home / "config" / "install_state.json").read_text(encoding="utf-8"))
    assert saved["steps"]["s04_venvs"]["status"] == "pending"
    assert state["startedAt"] is None and saved["started_at"] is None   # the duration counts this run only


def test_a_window_over_a_console_install_without_samples_still_shows_done(tmp_path, monkeypatch):
    """`steps.default_steps` (console, unattended) has no v01_samples; the window's STEP_ORDER does. The first
    dev-mode run finished unattended, then `INSTALL.cmd -Ui` opened on "Setup is halfway"."""
    from tests.test_installer_app_copy import app_setup, fake_zip_folder

    zip_a = fake_zip_folder(tmp_path / "zip-a", "0.1.4")
    first = app_setup(tmp_path, monkeypatch, zip_a)
    finished_install(first, zip_a)
    state_file = first.home / "config" / "install_state.json"
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    saved["steps"].pop("v01_samples", None)                       # the console wizard never marks it
    saved["started_at"] = "2026-11-04T11:59:18"                   # naive local (old window stamps) ...
    saved["completed_at"] = "2026-11-04T10:18:51+00:00"           # ... and aware UTC (console wizard)
    state_file.write_text(json.dumps(saved), encoding="utf-8")

    again = app_setup(tmp_path, monkeypatch, zip_a)
    state = again.snap()
    assert state["phase"] == "done" and state["run"] == "finished"
    v01 = next(st for st in state["steps"] if st["id"] == "v01_samples")
    assert v01["status"] == "skipped"
    assert json.loads(state_file.read_text(encoding="utf-8"))["steps"]["v01_samples"]["status"] == "skipped"
    assert state["result"] is not None and again.ctl.do_action("open-dashboard")[0] == 200
    assert state["result"]["durationS"] >= 0                      # mixed stamps no longer raise


def test_span_seconds_tolerates_naive_and_aware_stamps():
    from installer.stage2.web.report import span_seconds

    assert span_seconds("2026-11-04T10:00:00+00:00", "2026-11-04T10:01:05+00:00") == 65
    assert span_seconds("2026-11-04T10:00:00", "2026-11-04T10:00:30") == 30
    assert span_seconds("2026-11-04T10:00:00", "2026-11-04T10:00:30+00:00") is not None
    assert span_seconds(None, "2026-11-04T10:00:30") is None and span_seconds("bad", "worse") is None


def test_install_cmd_from_a_newer_zip_over_a_finished_install_updates(tmp_path, monkeypatch):
    from installer.stage2.web import appcopy
    from installer.stage2.web.controller import UPDATE_STEPS
    from tests.test_installer_app_copy import app_setup, fake_zip_folder

    zip_old = fake_zip_folder(tmp_path / "zip-old", "0.1.4")
    finished_install(app_setup(tmp_path, monkeypatch, zip_old), zip_old)
    zip_new = fake_zip_folder(tmp_path / "zip-new", "0.1.5")

    s = app_setup(tmp_path, monkeypatch, zip_new, finish_script)
    state = s.snap()
    status = {st["id"]: st["status"] for st in state["steps"]}
    assert {i for i, v in status.items() if v == "pending"} == set(UPDATE_STEPS)
    assert set(UPDATE_STEPS) >= {"b02_home", "b03_setup_env", "s04_venvs", "s08_smoke",
                                 "s09_shortcuts_scheduler", "s10_summary"}
    assert state["result"] is None and state["run"] == "idle" and state["resumed"] is True
    assert state["phase"] == "downloading"                  # the Welcome resume screen, Continue updates
    saved = json.loads((s.home / "config" / "install_state.json").read_text(encoding="utf-8"))
    assert saved["steps"]["s04_venvs"]["status"] == "pending"
    assert saved["steps"]["s01_questions"]["status"] == "done"
    assert appcopy.installed_version(s.home) == "0.1.4"             # nothing is copied before Continue

    assert s.ctl.do_action("start")[0] == 200
    state = s.until(lambda st: st["run"] == "finished", what="the update to finish")
    assert state["phase"] == "done" and state["result"] is not None
    assert appcopy.installed_version(s.home) == "0.1.5"
    assert (s.home / "app.prev" / "VERSION").read_text(encoding="utf-8").strip() == "0.1.4"
    argv = s.engine.argv
    assert argv[argv.index("--app-path") + 1] == str(s.home / "app")


def test_a_locked_resume_on_a_nearly_full_drive_is_not_blocked_by_its_own_files(tmp_path, monkeypatch):
    from tests.test_installer_app_copy import app_setup, fake_zip_folder

    zip_a = fake_zip_folder(tmp_path / "zip-a", "0.1.4")
    s = app_setup(tmp_path, monkeypatch, zip_a)
    finished_install(s, zip_a)
    state = json.loads((s.home / "config" / "install_state.json").read_text(encoding="utf-8"))
    state["steps"]["s08_smoke"]["status"] = "pending"
    (s.home / "config" / "install_state.json").write_text(json.dumps(state), encoding="utf-8")
    letter = str(s.home)[0].upper()
    monkeypatch.setattr(preflight, "folder_bytes", lambda _home: 40 * 1024 ** 3)       # setup's own downloads
    again = app_setup(tmp_path, monkeypatch, zip_a)
    again.ctl.pf["drives"] = [{"letter": letter, "free_gb": 30.0, "type": "SSD"}]
    again.fx.probe = lambda app, out, home: again.ctl.pf
    again.ctl._recheck()
    snap = again.snap()
    disk = next(c for c in snap["checks"] if c["id"] == "disk")
    assert snap["answersLocked"] is True
    assert disk["status"] == "warn" and "setup already holds 40 GB" in disk["value"] and not snap["blocking"]
    # the same drive on a first run (nothing locked) still blocks
    fresh = Setup(tmp_path / "other", monkeypatch)
    fresh.ctl.pf["drives"] = [{"letter": str(fresh.home)[0].upper(), "free_gb": 30.0, "type": "SSD"}]
    fresh.fx.probe = lambda app, out, home: fresh.ctl.pf
    fresh.ctl._recheck()
    assert fresh.snap()["blocking"] is True


def test_a_half_finished_install_is_left_alone_by_a_newer_zip(tmp_path, monkeypatch):
    from tests.test_installer_app_copy import app_setup, fake_zip_folder

    zip_a = fake_zip_folder(tmp_path / "zip-a", "0.1.4")
    s = app_setup(tmp_path, monkeypatch, zip_a)
    finished_install(s, zip_a)
    state = json.loads((s.home / "config" / "install_state.json").read_text(encoding="utf-8"))
    state["steps"]["s08_smoke"]["status"] = "pending"           # setup stopped before the smoke test
    (s.home / "config" / "install_state.json").write_text(json.dumps(state), encoding="utf-8")
    half = app_setup(tmp_path, monkeypatch, fake_zip_folder(tmp_path / "zip-b", "0.1.5"))
    snap = half.snap()
    assert snap["result"] is None and snap["run"] == "idle"
    assert [st["id"] for st in snap["steps"] if st["status"] == "pending"] == ["s08_smoke"]


def test_a_resume_with_no_stored_key_says_so_instead_of_showing_a_key_as_set(tmp_path, monkeypatch):
    from tests.test_installer_app_copy import app_setup, fake_zip_folder

    zip_a = fake_zip_folder(tmp_path / "zip-a", "0.1.4")
    s = app_setup(tmp_path, monkeypatch, zip_a)
    finished_install(s, zip_a)
    state = json.loads((s.home / "config" / "install_state.json").read_text(encoding="utf-8"))
    state["steps"]["s08_smoke"]["status"] = "pending"
    (s.home / "config" / "install_state.json").write_text(json.dumps(state), encoding="utf-8")

    bare = app_setup(tmp_path, monkeypatch, zip_a).snap()  # no key stored anywhere
    assert bare["answers"]["pexelsKeySet"] is False and bare["answers"]["pixabayKeySet"] is False
    assert [n["code"] for n in bare["notices"]] == ["no-stock-key"]
    assert "Pixabay key" in bare["notices"][0]["whatToDo"] and bare["notices"][0]["title"]

    store_secret(Paths(s.home), "pixabay_api_key", KEY_X, protect=lambda b: b)     # s01 stored it
    kept = app_setup(tmp_path, monkeypatch, zip_a).snap()
    assert kept["answers"]["pixabayKeySet"] is True and kept["answers"]["pexelsKeySet"] is False
    assert kept["notices"] == []
    assert KEY_X not in json.dumps(kept)                                # only the fact, never the value


def test_the_unattended_run_keeps_the_pc_awake_like_the_window_does(tmp_path, monkeypatch):
    from installer.stage2.web import keepawake

    s = Setup(tmp_path, monkeypatch)
    answers = tmp_path / "answers.json"
    answers_view.write_answers(answers, answers_view.draft_to_answers(draft(s.home)))
    s.cfg.answers, s.cfg.headless = answers, True
    awake_at_wizard: list[list[int]] = []

    def fake_run(argv, **kw):
        awake_at_wizard.append(list(s.fx.awake))                 # what was set when the wizard started
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr(controller.subprocess, "run", fake_run)
    assert s.ctl.run_headless() == 0
    both = keepawake.ES_CONTINUOUS | keepawake.ES_SYSTEM_REQUIRED
    assert awake_at_wizard == [[both]]                           # held while the wizard ran ...
    assert s.fx.awake == [both, keepawake.ES_CONTINUOUS]         # ... and given back afterwards


def test_a_second_unattended_setup_is_refused_while_the_first_one_runs(tmp_path, monkeypatch):
    from ns_common.singleton import FileLock

    s = Setup(tmp_path, monkeypatch)
    answers = tmp_path / "answers.json"
    answers_view.write_answers(answers, answers_view.draft_to_answers(draft(s.home)))
    s.cfg.answers, s.cfg.headless = answers, True
    first = FileLock(s.cfg.data_dir / "bootstrap" / "setup.lock")
    first.acquire()                                               # the first INSTALL.cmd is running
    try:
        assert s.ctl.run_headless() == controller.EXIT_ALREADY_RUNNING == 15
    finally:
        first.release()
    assert any("already running" in line for line in s.fx.console_lines)
    assert s.fx.awake == []                                       # it did nothing at all
    assert not (s.home / "venvs").exists()


def test_open_dashboard_starts_the_engine_without_stage_ones_pythonpath(tmp_path, monkeypatch):
    """The engine must import the program from <home>\\app, not from the folder the zip was extracted to."""
    seen: list[dict] = []
    s = Setup(tmp_path, monkeypatch)
    s.fx.launch_captured = lambda argv, env=None, **kw: (seen.append({"argv": argv, "env": env}), (0, ""))[1]
    s.ctl.store.update(lambda st: st.update(phase="done"))
    assert s.ctl._act_open_dashboard(None) == "Opening the dashboard"
    env = seen[0]["env"]
    assert env["PYTHONPATH"] is None and env["NIGHTSHIFT_BOOTSTRAP_UV"] is None      # None: removed
    assert env["NS_DASHBOARD_NO_HOST"] == "1" and env["NIGHTSHIFT_HOME"] == str(s.home)
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.setenv("NIGHTSHIFT_BOOTSTRAP_UV", "C:/x/uv.exe")
    child = controller.child_env(env)
    assert "PYTHONPATH" not in child and "NIGHTSHIFT_BOOTSTRAP_UV" not in child
    assert child["NS_DASHBOARD_NO_HOST"] == "1" and "PATH" in {k.upper() for k in child}


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
    assert state["notices"][0]["title"] == "The speed boost did not help here"
    assert any("asked a question" in line["text"] for line in state["log"])
    ev(step_event("s05_opencode_prewarm", "skipped", "not selected"))
    st = next(x for x in s.snap()["steps"] if x["id"] == "s05_opencode_prewarm")
    assert st["status"] == "skipped" and st["detail"] == "not selected" and st["finishedAt"]
    ev(step_event("s06_tune", "running"))
    ev({"type": "say", "text": "Trying the story model at different settings", "style": None})
    st = next(x for x in s.snap()["steps"] if x["id"] == "s06_tune")
    assert st["status"] == "running" and st["detail"].startswith("Trying the story model") and st["startedAt"]
    ev({"type": "verify", "checks": [
        {"name": "supervisor.status", "passed": True, "value": "up", "limit": None},
        {"name": "llama.start", "passed": True, "value": "12.0", "limit": None},
        {"name": "llama.health", "passed": True, "value": None, "limit": None},
        {"name": "llama.json_completion", "passed": True, "value": "json ok", "limit": None},
        {"name": "llama.tok_s", "passed": True, "value": "54.2", "limit": None},
        {"name": "embed.start", "passed": True, "value": "1.0", "limit": None},
        {"name": "embed.vector", "passed": True, "value": "768.0", "limit": None},
        {"name": "tts.start", "passed": True, "value": "3.0", "limit": None},
        {"name": "tts.preset", "passed": True, "value": "0.31", "limit": None},
        {"name": "tts.voices", "passed": False, "value": "preset", "limit": None},
        {"name": "whisper.load", "passed": True, "value": "1.5", "limit": None},
        {"name": "nvenc.probe", "passed": False, "value": None, "limit": None},
        {"name": "vram.idle", "passed": True, "value": "120", "limit": None},
        {"name": "bench.idle_vram_mb", "passed": True, "value": "1", "limit": None}]})
    checks = {c["name"]: c for c in s.snap()["verify"]["checks"]}
    # one row per area, real `ns verify` names grouped; bench rows stay hidden; no invented "mtp" row
    assert list(checks) == ["engine", "llm", "embed", "tts", "whisper", "nvenc", "vram"]
    assert checks["llm"]["label"] == "Story model" and checks["llm"]["status"] == "passed"
    assert checks["llm"]["value"] == "works"
    assert checks["tts"]["value"] == "voice works" and checks["tts"]["status"] == "fallback"
    assert (checks["nvenc"]["status"], checks["nvenc"]["value"]) == ("fallback", "processor video (slower)")
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
    assert labels.verify_label("bench.idle_vram_mb") is None
    assert labels.verify_label("nvenc.probe") == "Video encoder"
    assert labels.verify_label("llama.health") == "Story model"
    assert labels.verify_label("tts.preset") == "Voice engine" and labels.verify_label("mtp") == "mtp"
    assert labels.verify_label("odd_check") == "odd_check"
    assert labels.row_key("whisper/config.json") == "whisper" and labels.row_key("yunet") == "yunet"



def test_a_pc_under_32_gb_starts_on_small_and_the_plan_says_so(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    pf = sample_handover(ROOT, home=str(s.home))
    pf["ram_gb"] = 16.0
    pf_file = tmp_path / "pf.json"
    pf_file.write_text(json.dumps(pf), encoding="utf-8")
    cfg = Config(app_path=ROOT, data_dir=tmp_path / "data2", simulate=True, home=s.home, preflight=pf_file)
    state = SetupController(cfg, s.fx).snapshot()
    assert state["plan"]["tier"] == "small" and state["plan"]["recommendedTier"] == "small"
    assert state["plan"]["tiers"]["balanced"]["allowed"] is False
    assert state["plan"]["tiers"]["small"]["allowed"] is True


def test_a_stall_retry_shows_on_the_row_and_clears_when_the_bytes_flow_again(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    s.accept()
    size = 9001752960
    ctl = s.ctl
    ctl._ev_file({"type": "file", "key": LLM, "event": "start", "total": size})
    ctl._ev_file({"type": "file", "key": LLM, "event": "progress", "done": 1000, "total": size})
    retry = {"type": "file", "key": LLM, "event": "retry", "attempt": 1, "reason": "stall", "waitS": 2.0}
    ctl._ev_file(retry)
    row = next(r for r in s.snap()["downloads"]["items"] if r["key"] == LLM)
    assert row["status"] == "active" and row["speedBps"] is None and row["etaS"] is None
    assert row["note"] == "No data for 60 s, trying again in 2 s"
    ctl._ev_file({"type": "file", "key": LLM, "event": "progress", "done": 5000, "total": size})
    row = next(r for r in s.snap()["downloads"]["items"] if r["key"] == LLM)
    assert row["note"] is None and row["bytesDone"] == 5000


def test_retry_notes_name_the_reason():
    assert controller.retry_note("network", 4.0) == "The connection dropped, trying again in 4 s"
    assert controller.retry_note("hash", 0.0) == "The file failed its check, downloading it again"
    assert controller.retry_note("???", 2.0) == "The download stopped, trying again in 2 s"


def test_the_retry_note_says_which_try_is_next(tmp_path, monkeypatch):
    assert controller.retry_note("network", 4.0, 1, 3) == (
        "The connection dropped, trying again in 4 s (try 2 of 3)")
    assert controller.retry_note("hash", 2.0, 2, 3) == (
        "The file failed its check, downloading it again in 2 s (try 3 of 3)")
    assert controller.retry_note("stall", 2.0, 3, 3) == "No data for 60 s, trying again in 2 s"   # none left
    s = Setup(tmp_path, monkeypatch)
    s.accept()
    ctl = s.ctl
    ctl._ev_file({"type": "file", "key": LLM, "event": "start", "total": 9001752960})
    ctl._ev_file({"type": "file", "key": LLM, "event": "retry", "attempt": 1, "reason": "network",
                  "waitS": 2.0, "of": 3})
    row = next(r for r in s.snap()["downloads"]["items"] if r["key"] == LLM)
    assert row["note"] == "The connection dropped, trying again in 2 s (try 2 of 3)"


# ------------------------------------------------------------------------------------------ custom model
CUSTOM = {"repo": "bartowski/Qwen2.5-14B-Instruct-GGUF", "file": "Qwen2.5-14B-Instruct-Q4_K_M.gguf",
          "size": 8_988_110_976}


def test_the_model_check_goes_through_the_effects_head_and_simulate_has_a_fake(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    ok = s.ctl.check_model("Qwen/Qwen3-14B-GGUF", "Qwen3-14B-Q4_K_M.gguf")
    assert ok == {"ok": True, "size": 9_000_000_000, "message": None, "code": None}     # fake_head: Q4 = 9 GB
    big = s.ctl.check_model("meta/Llama-70B-GGUF", "llama-70B-Q4.gguf")
    assert big["ok"] is False and "15.5 GB" in big["message"]
    assert s.ctl.check_model("a/b", "missing.gguf")["ok"] is False
    s.fx.head = lambda url: (200, 5_500_000_000)                    # the effect is replaceable
    assert s.ctl.check_model("a/b", "m.gguf")["size"] == 5_500_000_000


def test_a_custom_model_is_accepted_into_the_answers_the_plan_and_the_download_rows(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    good = s.accept(llmTier="best", llmCustom=dict(CUSTOM))
    assert good["answers"]["llmCustom"] == CUSTOM and good["answers"]["llmTier"] == "small"
    state = s.snap()
    rows = {r["key"]: r for r in state["downloads"]["items"]}
    row = rows[CUSTOM["file"]]
    assert row["label"] == "Story model (Custom)" and row["bytesTotal"] == CUSTOM["size"]
    assert LLM not in rows and "Qwen3.6-35B-A3B-UD-Q4_K_XL.gguf" not in rows
    assert state["answers"]["llmCustom"] == CUSTOM and state["plan"]["tier"] == "small"
    again = s.ctl.submit_answers(draft(s.home, llmCustom=None))
    assert again["ok"] and again["answers"]["llmCustom"] is None
    assert LLM in {r["key"] for r in s.snap()["downloads"]["items"]}


def test_a_custom_model_over_the_limit_is_a_field_error_and_changes_nothing(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    bad = s.ctl.submit_answers(draft(s.home, llmCustom={**CUSTOM, "size": 17_000_000_000}))
    assert bad["ok"] is False
    err = next(e for e in bad["errors"] if e["field"] == "llmCustom")
    assert "17.0 GB" in err["message"] and s.snap()["answers"] is None


def test_the_written_answers_file_carries_the_custom_model(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch, script=finish_script)
    s.accept(llmCustom=dict(CUSTOM))
    assert s.ctl.do_action("start")[0] == 200
    s.until(lambda st: st["run"] == "finished", what="the run to finish")
    written = json.loads((s.home / "config" / "answers.json").read_text(encoding="utf-8"))
    assert written["llm_custom"] == CUSTOM and written["llm_tier"] == "small"


# ------------------------------------------------------------------ "Open dashboard" shows what went wrong
def finished_setup(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch)
    s.ctl._started = True

    def finish(st: dict) -> None:
        for step in st["steps"]:
            step["status"] = "done"
        st["run"] = "finished"
        st["result"] = {"home": str(s.home), "outbox": str(s.home / "outbox"), "diagnosticsZip": None}

    s.ctl.store.update(finish)
    return s


def test_a_launcher_that_fails_is_reported_in_the_window_with_its_own_message(tmp_path, monkeypatch):
    s = finished_setup(tmp_path, monkeypatch)
    s.fx.launch_captured = lambda argv, env=None, **kw: (
        1, "The Nightshift engine did not start. Run \"Nightshift - Diagnostics\" and send the zip.")
    status, body = s.ctl.do_action("open-dashboard")
    assert status == 200 and body["ok"] is False
    assert "engine did not start" in body["message"]


class FakeLauncher:
    """A launcher process: prints its lines, exits with a code (or keeps running)."""

    def __init__(self, text: str, code: int | None) -> None:
        import io

        self.stdout = io.StringIO(text)
        self.code = code

    def poll(self):
        return self.code


def test_launch_captured_reads_the_output_of_a_failed_launcher():
    seen = {}

    def popen(argv, **kw):
        seen.update(kw)
        return FakeLauncher("The dashboard did not start.\nRun Diagnostics.\n", 1)

    code, tail = controller.launch_captured(["x"], popen=popen, sleep=lambda s: None, up=lambda: False)
    assert code == 1 and "did not start" in tail and "Run Diagnostics." in tail
    assert seen["stdout"] is not None and seen["stdin"] is not None      # captured, and no console to wait on


def test_launch_captured_succeeds_when_the_dashboard_answers_while_the_launcher_is_busy():
    code, tail = controller.launch_captured(
        ["x"], popen=lambda argv, **kw: FakeLauncher("", None), sleep=lambda s: None, up=lambda: True)
    assert code is None


def test_launch_captured_gives_up_waiting_but_not_on_the_launcher():
    code, _ = controller.launch_captured(
        ["x"], popen=lambda argv, **kw: FakeLauncher("", None), sleep=lambda s: None, up=lambda: False,
        wait_s=0.0)
    assert code is None
