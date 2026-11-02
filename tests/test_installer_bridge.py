"""C1: the engine bridge: BridgeUi and BridgeReporter in process, plus a real wizard process (simulate)."""

import io
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from installer.stage2.base import Step
from installer.stage2.bridge import BridgeReporter, BridgeUi, SetupStopped
from installer.stage2.downloader import DownloadItem
from installer.stage2.errors import make_error
from installer.stage2.testing import make_test_context
from installer.stage2.web import fakes
from installer.stage2.web.engine import EngineProcess, wizard_argv, wizard_env
from installer.stage2.wizard import EXIT_INTERRUPTED, EXIT_QUIT, Wizard
from ns_common.config import load_install_state
from ns_common.paths import Paths

ROOT = Path(__file__).resolve().parents[1]
KEYS = {"cmd": "keys", "pexels_api_key": "KEY-pexels-9f3a", "pixabay_api_key": "KEY-pixabay-7c1d"}


def lines(out: io.StringIO) -> list[dict]:
    return [json.loads(x) for x in out.getvalue().splitlines() if x.strip()]


def wait_for(pred, timeout: float = 60.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = pred()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


# ---------------------------------------------------------------------------------------- BridgeUi alone
def test_every_ui_call_becomes_one_json_line_and_stdout_stays_clean():
    out = io.StringIO()
    ui = BridgeUi(out=out, inp=io.StringIO(""))
    item = DownloadItem(name="m.gguf", url="https://x/m.gguf", dest=Path("D:/ns/models/llm/m.gguf"), size=100)
    ui.say("hello", style="dim")
    ui.rule("A title")
    ui.table("T", ["a", "b"], [["1", 2]])
    ui.plan([item])
    ui.step_status("s03_download_models", "running", "Download AI models", None)
    ui.notice("NS-W301", "no binary")
    ui.verify_checks([{"name": "llm", "passed": True, "value": 54.2, "limit": None}])
    ui.sample("voice", Path("D:/x/voice.wav"), 1.5)
    ui.bundle(None)
    events = lines(out)
    kinds = [e["type"] for e in events]
    assert kinds == ["say", "say", "table", "plan", "step", "warn", "verify", "sample", "bundle"]
    assert events[1]["style"] == "rule" and events[2]["rows"] == [["1", "2"]]
    assert events[3] == {"type": "plan", "items": [{"key": "m.gguf", "file": "m.gguf", "bytes": 100,
                                                     "via": "download"}], "totalBytes": 100}
    assert events[5] == {"type": "warn", "code": "NS-W301", "detail": "no binary"}
    assert events[6]["checks"] == [{"name": "llm", "passed": True, "value": "54.2", "limit": None}]
    assert events[8]["path"] is None and ui.bridge is True and ui.interactive is False


def test_prompts_never_block_they_return_the_default_and_report_a_bug():
    out = io.StringIO()
    ui = BridgeUi(out=out, inp=io.StringIO(""))
    assert ui.ask("Name?", "Ryan") == "Ryan" and ui.ask("Name?") == ""
    assert ui.confirm("Sure?", False) is False and ui.choose("Pick", ["a", "b"], "b") == "b"
    assert ui.choose("Pick", ["a", "b"]) == "a"
    ui.pause()
    assert [e["code"] for e in lines(out)] == ["prompt"] * 5


def test_commands_set_the_gate_stop_and_keys():
    ui = BridgeUi(out=io.StringIO(), inp=io.StringIO(""))
    assert ui.gate.is_set() and not ui.stop.is_set()
    ui.handle({"cmd": "pause"})
    assert not ui.gate.is_set()
    ui.handle({"cmd": "resume"})
    assert ui.gate.is_set()
    ui.handle({"cmd": "keys", "pexels_api_key": "p", "pixabay_api_key": None})
    assert ui.wait_keys(0.1) == {"pexels_api_key": "p", "pixabay_api_key": None}
    assert ui.wait_keys(0.05) == {}
    ui.handle({"cmd": "pause"})
    ui.handle({"cmd": "stop"})
    assert ui.stop.is_set() and ui.gate.is_set()            # stop wakes anything waiting at the pause gate


def test_the_reader_thread_applies_commands_and_end_of_input_means_stop():
    read, write = os.pipe()
    ui = BridgeUi(out=io.StringIO(), inp=os.fdopen(read, "r", encoding="utf-8"))
    ui.start_reader()
    with os.fdopen(write, "w", encoding="utf-8") as w:
        w.write(json.dumps({"cmd": "pause"}) + "\nnot json\n")
        w.flush()
        wait_for(lambda: not ui.gate.is_set(), 5, "pause")
    wait_for(ui.stop.is_set, 5, "stop after end of input")


# ---------------------------------------------------------------------------------------- reporter
def test_progress_is_throttled_but_the_last_update_is_always_sent():
    out = io.StringIO()
    rep = BridgeReporter(BridgeUi(out=out, inp=io.StringIO("")), 1000)
    rep.start("a.bin", 1000)
    for done in (10, 20, 30, 40):
        rep.update("a.bin", done, 1000)
    rep.update("a.bin", 1000, 1000)
    rep.finish("a.bin", "downloaded")
    rep.finish("b.bin", "missing_optional")
    events = lines(out)
    progress = [e for e in events if e.get("event") == "progress"]
    assert len(progress) == 2 and progress[0]["done"] == 10 and progress[-1]["done"] == 1000
    assert events[0] == {"type": "file", "key": "a.bin", "event": "start", "total": 1000}
    assert events[-2]["outcome"] == "downloaded" and events[-1]["outcome"] == "missing"


def test_pause_holds_progress_until_resume_and_stop_raises():
    ui = BridgeUi(out=io.StringIO(), inp=io.StringIO(""))
    rep = BridgeReporter(ui, 10)
    ui.handle({"cmd": "pause"})
    returned = threading.Event()
    threading.Thread(target=lambda: (rep.update("a", 1, 10), returned.set()), daemon=True).start()
    assert not returned.wait(0.5)                           # held at the gate
    ui.handle({"cmd": "resume"})
    assert returned.wait(5)
    ui.handle({"cmd": "pause"})
    errors: list[BaseException] = []

    def paused_update() -> None:
        try:
            rep.update("a", 2, 10)
        except SetupStopped as e:
            errors.append(e)

    t = threading.Thread(target=paused_update, daemon=True)
    t.start()
    time.sleep(0.3)
    ui.handle({"cmd": "stop"})
    t.join(5)
    assert len(errors) == 1                                 # a paused download that is stopped ends at once
    with pytest.raises(SetupStopped):
        rep.update("a", 3, 10)


def test_setup_stopped_is_not_an_exception_so_no_step_swallows_it():
    assert issubclass(SetupStopped, BaseException) and not issubclass(SetupStopped, Exception)


# ---------------------------------------------------------------------------------------- wizard + bridge
class FlakyStep(Step):
    def __init__(self, step_id: str, fail_times: int = 0, optional: bool = False, stop: bool = False) -> None:
        self.id = step_id
        self.title = f"Title {step_id}"
        self.optional = optional
        self.fail_times = fail_times
        self.stop = stop
        self.runs = 0

    def check(self, ctx) -> bool:
        return False

    def run(self, ctx) -> None:
        self.runs += 1
        if self.stop:
            raise SetupStopped()
        if self.runs <= self.fail_times:
            raise make_error("NS-E109", "qwen-talker.gguf: expected 4b9a33a2, got 0c1d")

    def verify(self, ctx) -> None:
        return None


def bridge_context(tmp_path: Path):
    out = io.StringIO()
    ui = BridgeUi(out=out, inp=io.StringIO(""))
    ctx = make_test_context(tmp_path, ui=ui, unattended=False)
    ctx.gate, ctx.stop_requested = ui.gate, ui.stop
    return ctx, ui, out


def answer_when_failed(ui: BridgeUi, out: io.StringIO, action: str) -> threading.Thread:
    """Play P1: wait for the failure event, then send a decision."""
    def play() -> None:
        wait_for(lambda: any(e["type"] == "failure" for e in lines(out)), 10, "the failure event")
        ui.handle({"cmd": "decision", "action": action})

    t = threading.Thread(target=play, daemon=True)
    t.start()
    return t


def test_a_failure_waits_for_the_window_and_retry_runs_the_step_again(tmp_path: Path):
    ctx, ui, out = bridge_context(tmp_path)
    step = FlakyStep("s03_download_models", fail_times=1)
    player = answer_when_failed(ui, out, "retry")
    assert Wizard(ctx, [step]).run() == 0
    player.join(5)
    assert step.runs == 2
    events = lines(out)
    failure = next(e for e in events if e["type"] == "failure")
    assert failure["step"] == "s03_download_models" and failure["code"] == "NS-E109"
    assert failure["optional"] is False
    assert failure["attempt"] == 1 and "expected 4b9a33a2" in failure["detail"] and failure["hint"]
    statuses = [e["status"] for e in events if e["type"] == "step"]
    assert statuses == ["running", "running", "done"]
    assert load_install_state(ctx.paths).steps["s03_download_models"].status == "done"


def test_skip_works_for_optional_steps_only(tmp_path: Path):
    ctx, ui, out = bridge_context(tmp_path)
    optional = FlakyStep("v01_samples", fail_times=9, optional=True)
    player = answer_when_failed(ui, out, "skip")
    assert Wizard(ctx, [optional, FlakyStep("s09_shortcuts_scheduler")]).run() == 0
    player.join(5)
    assert load_install_state(ctx.paths).steps["v01_samples"].status == "skipped"

    ctx, ui, out = bridge_context(tmp_path / "second")
    required = FlakyStep("s03_download_models", fail_times=9)
    player = answer_when_failed(ui, out, "skip")           # not allowed: treated as quit
    assert Wizard(ctx, [required]).run() == EXIT_QUIT
    player.join(5)
    assert load_install_state(ctx.paths).steps["s03_download_models"].status == "failed"
    assert not any("doctor" in str(c.argv) for c in ctx.runner.calls)         # no console diagnostics bundle


def test_stop_marks_the_running_step_pending_and_exits_130(tmp_path: Path):
    ctx, ui, out = bridge_context(tmp_path)
    step = FlakyStep("s03_download_models", stop=True)
    assert Wizard(ctx, [FlakyStep("s02_download_bins"), step]).run() == EXIT_INTERRUPTED
    st = load_install_state(ctx.paths).steps
    assert st["s02_download_bins"].status == "done" and st["s03_download_models"].status == "pending"
    last = [e for e in lines(out) if e["type"] == "step"][-1]
    assert last["id"] == "s03_download_models" and last["status"] == "pending"


def test_a_stop_before_the_next_step_ends_the_run_cleanly(tmp_path: Path):
    ctx, ui, out = bridge_context(tmp_path)
    ui.handle({"cmd": "stop"})
    first = FlakyStep("s02_download_bins")
    assert Wizard(ctx, [first]).run() == EXIT_INTERRUPTED and first.runs == 0


def test_a_failure_while_stopping_quits_instead_of_waiting(tmp_path: Path):
    ctx, ui, out = bridge_context(tmp_path)
    step = FlakyStep("s03_download_models", fail_times=9)

    def stopper() -> None:
        wait_for(lambda: any(e["type"] == "failure" for e in lines(out)), 10, "the failure event")
        ui.handle({"cmd": "stop"})

    threading.Thread(target=stopper, daemon=True).start()
    assert Wizard(ctx, [step]).run() == EXIT_QUIT


# ------------------------------------------------------------------------------ a real wizard process
class Run:
    """A real ``python -m installer.stage2.wizard --ui-bridge stdio --simulate-downloads`` process."""

    def __init__(self, tmp_path: Path, halt_after: str = "s03_download_models") -> None:
        self.home = tmp_path / "home"
        (self.home / "config").mkdir(parents=True)
        answers = json.loads((ROOT / "installer" / "answers.example.json").read_text(encoding="utf-8"))
        answers["home_path"] = str(self.home)
        self.answers = self.home / "config" / "answers.json"
        self.answers.write_text(json.dumps(answers), encoding="utf-8")
        self.preflight = tmp_path / "preflight.json"
        handover = fakes.sample_handover(ROOT, home=str(self.home))
        self.preflight.write_text(json.dumps(handover), encoding="utf-8")
        self.events: list[dict] = []
        self.lines: list[str] = []
        self.code: list[int] = []
        argv = wizard_argv(sys.executable, app_path=ROOT, home=self.home, answers=self.answers,
                           preflight=self.preflight, bridge=True, simulate=True, halt_after=halt_after)
        env = wizard_env(ROOT)
        env.pop("NIGHTSHIFT_HOME", None)
        self.proc = EngineProcess(argv, env=env, cwd=ROOT, on_event=self.events.append,
                                  on_line=self.lines.append,
                                  on_exit=self.code.append)

    def start(self, **keys) -> None:
        self.proc.start()
        self.proc.send({**KEYS, **keys})

    def wait_exit(self, timeout: float = 120.0) -> int:
        got = wait_for(lambda: self.code, timeout, "the wizard to exit")
        return got[0]

    def steps(self) -> list[tuple[str, str]]:
        return [(e["id"], e["status"]) for e in self.events if e["type"] == "step"]

    def has_step(self, step_id: str, status: str) -> bool:
        return (step_id, status) in self.steps()


@pytest.fixture
def run(tmp_path: Path):
    r = Run(tmp_path)
    yield r
    r.proc.kill()


def test_a_real_wizard_streams_json_events_and_halts_after_the_models(run: Run):
    run.start()
    assert run.wait_exit() == 75                                # --halt-after is the test's way to stop early
    types = {e["type"] for e in run.events}
    assert {"hello", "step", "say", "plan", "file", "bye"} <= types
    assert run.events[0]["type"] == "hello" and run.events[-1] == {"type": "bye", "exitCode": 75}
    steps = run.steps()
    assert steps.index(("s01_questions", "running")) < steps.index(("s01_questions", "done"))
    assert ("s03_download_models", "done") in steps and not any(i == "s00_welcome" for i, _ in steps)
    plan_event = next(e for e in run.events if e["type"] == "plan" and any(
        i["key"] == "Qwen3-14B-Q4_K_M.gguf" for i in e["items"]))
    assert plan_event["totalBytes"] > 9_000_000_000
    finished = {e["key"]: e["outcome"] for e in run.events if e["type"] == "file" and e["event"] == "finish"}
    assert finished["Qwen3-14B-Q4_K_M.gguf"] == "downloaded" and finished["llama_cuda13"] == "downloaded"
    done = [e for e in run.events if e["type"] == "file" and e["event"] == "progress"
            and e["key"] == "Qwen3-14B-Q4_K_M.gguf"]
    assert done and done[-1]["done"] == done[-1]["total"]
    state = load_install_state(Paths(run.home))
    assert [state.steps[s].status for s in ("s01_questions", "s02_download_bins", "s03_download_models")] == [
        "done", "done", "done"]
    assert "KEY-pexels-9f3a" not in json.dumps(run.events)
    assert "KEY-pexels-9f3a" not in run.answers.read_text(encoding="utf-8")


def test_pause_holds_a_real_download_and_resume_lets_it_finish(run: Run):
    run.proc.start()
    run.proc.send(KEYS)
    run.proc.send({"cmd": "pause"})
    wait_for(lambda: run.has_step("s02_download_bins", "running"), 90, "s02 to start")
    time.sleep(1.0)
    assert not [e for e in run.events if e["type"] == "file" and e["event"] == "finish"]   # held
    assert not run.code
    run.proc.send({"cmd": "resume"})
    assert run.wait_exit() == 75
    assert run.has_step("s03_download_models", "done")


def test_stop_exits_130_and_leaves_the_running_step_pending(run: Run):
    run.proc.start()
    run.proc.send(KEYS)
    run.proc.send({"cmd": "pause"})
    wait_for(lambda: run.has_step("s02_download_bins", "running"), 90, "s02 to start")
    time.sleep(0.5)
    run.proc.send({"cmd": "stop"})
    assert run.wait_exit() == 130
    assert run.has_step("s02_download_bins", "pending")
    state = load_install_state(Paths(run.home))
    assert state.steps["s01_questions"].status == "done"
    step = state.steps.get("s02_download_bins")
    assert step is None or step.status == "pending"
