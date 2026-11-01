"""C1: wizard framework (resume, error UX, unattended retries, halting) with fake steps."""

import json
from pathlib import Path

import pytest

from installer.stage2.answers import AnswersError
from installer.stage2.base import Step
from installer.stage2.errors import make_error
from installer.stage2.secrets import MemoryStore
from installer.stage2.simulate import FakeRunner
from installer.stage2.steps import default_steps
from installer.stage2.testing import FakeUi, make_test_context
from installer.stage2.wizard import (
    EXIT_FAILED,
    EXIT_HALTED,
    EXIT_INTERRUPTED,
    EXIT_QUIT,
    Wizard,
    build_context,
    describe_failure,
    parse_args,
)
from ns_common.config import load_install_state
from ns_common.errors import NsError


class SimulatedKill(BaseException):
    """Stands in for a power cut or a closed window."""


class Probe(Step):
    """A configurable fake step."""

    def __init__(self, step_id: str, *, fail_times: int = 0, error: Exception | None = None,
                 satisfied: bool = False, optional: bool = False, kill: bool = False,
                 skip: str | None = None) -> None:
        self.id = step_id
        self.title = f"Title {step_id}"
        self.optional = optional
        self.fail_times = fail_times
        self.error = error or make_error("NS-E105", "boom")
        self.satisfied = satisfied
        self.kill = kill
        self.skip = skip
        self.runs = 0
        self.verifies = 0

    def skip_reason(self, ctx) -> str | None:
        return self.skip

    def check(self, ctx) -> bool:
        return self.satisfied

    def run(self, ctx) -> None:
        self.runs += 1
        if self.kill:
            raise SimulatedKill()
        if self.runs <= self.fail_times:
            raise self.error

    def verify(self, ctx) -> None:
        self.verifies += 1


def status(ctx) -> dict[str, str]:
    st = load_install_state(ctx.paths)
    return {k: v.status for k, v in st.steps.items()}


# ---------------------------------------------------------------------------------------------- contract
def test_every_step_defines_check_run_verify_and_ids_are_ordered():
    steps = default_steps()
    ids = [s.id for s in steps]
    assert ids == [
        "s00_welcome", "s01_questions", "s02_download_bins", "s03_download_models", "s04_venvs",
        "s05_opencode_prewarm", "s06_tune", "s07_configs", "s08_smoke", "s09_shortcuts_scheduler",
        "s10_summary",
    ]
    assert len(set(ids)) == 11
    for s in steps:
        assert s.title
        for name in ("check", "run", "verify"):
            assert callable(getattr(s, name))
            assert getattr(type(s), name) is not getattr(Step, name), f"{s.id} must override {name}"


def test_step_base_is_abstract():
    with pytest.raises(TypeError):
        Step()  # type: ignore[abstract]


# ---------------------------------------------------------------------------------------------- flow
def test_happy_path_writes_state_after_every_step(tmp_path: Path):
    ctx = make_test_context(tmp_path)
    steps = [Probe("a"), Probe("b"), Probe("c")]
    assert Wizard(ctx, steps).run() == 0
    assert status(ctx) == {"a": "done", "b": "done", "c": "done"}
    st = load_install_state(ctx.paths)
    assert st.completed_at and st.started_at and st.app_path == str(ctx.app_path)
    assert st.profile == "gpu"
    assert all(s.verifies == 1 and s.runs == 1 for s in steps)


def test_resume_skips_done_steps_whose_check_is_true(tmp_path: Path):
    ctx = make_test_context(tmp_path)
    Wizard(ctx, [Probe("a"), Probe("b")]).run()
    again = [Probe("a", satisfied=True), Probe("b", satisfied=False)]
    ctx2 = make_test_context(tmp_path)
    assert Wizard(ctx2, again).run() == 0
    assert again[0].runs == 0 and again[0].verifies == 0
    assert again[1].runs == 1          # done earlier, but its check says the result is gone
    assert ctx2.resumed is True


def test_kill_then_resume(tmp_path: Path):
    ctx = make_test_context(tmp_path)
    steps = [Probe("a"), Probe("b"), Probe("c", kill=True), Probe("d")]
    with pytest.raises(SimulatedKill):
        Wizard(ctx, steps).run()
    assert status(ctx) == {"a": "done", "b": "done"}

    ctx2 = make_test_context(tmp_path)
    resumed = [Probe("a", satisfied=True), Probe("b", satisfied=True), Probe("c"), Probe("d")]
    assert Wizard(ctx2, resumed).run() == 0
    assert [s.runs for s in resumed] == [0, 0, 1, 1]
    assert status(ctx2) == {"a": "done", "b": "done", "c": "done", "d": "done"}


def test_halt_after_returns_75_and_keeps_state(tmp_path: Path):
    ctx = make_test_context(tmp_path)
    steps = [Probe("a"), Probe("b"), Probe("c")]
    assert Wizard(ctx, steps, halt_after="b").run() == EXIT_HALTED
    assert status(ctx) == {"a": "done", "b": "done"} and steps[2].runs == 0


def test_skip_reason_marks_skipped(tmp_path: Path):
    ctx = make_test_context(tmp_path)
    step = Probe("opt", skip="not selected")
    assert Wizard(ctx, [step]).run() == 0
    assert status(ctx) == {"opt": "skipped"} and step.runs == 0
    assert "not selected" in ctx.ui.text


def test_keyboard_interrupt_exits_130(tmp_path: Path):
    ctx = make_test_context(tmp_path)

    class Ctrl(Probe):
        def run(self, ctx) -> None:
            raise KeyboardInterrupt

    assert Wizard(ctx, [Ctrl("a")]).run() == EXIT_INTERRUPTED
    assert "INSTALL.cmd" in ctx.ui.text


def test_warning_codes_never_abort(tmp_path: Path):
    ctx = make_test_context(tmp_path)
    step = Probe("a", fail_times=1, error=make_error("NS-W106", "D: is a hard disk"))
    assert Wizard(ctx, [step, Probe("b")]).run() == 0
    assert ("NS-W106", "D: is a hard disk") in ctx.warnings
    assert status(ctx) == {"a": "done", "b": "done"}


def test_stage_start_and_end_are_logged(tmp_path: Path):
    ctx = make_test_context(tmp_path)
    Wizard(ctx, [Probe("a")]).run()
    lines = [json.loads(x) for f in ctx.paths.logs.glob("installer-*.jsonl")
             for x in f.read_text(encoding="utf-8").splitlines()]
    events = [(r["event"], r["data"].get("step")) for r in lines if r["event"].startswith("stage_")]
    assert ("stage_start", "a") in events and ("stage_end", "a") in events
    assert all(r["component"] == "installer" for r in lines)


# ---------------------------------------------------------------------------------------------- error UX
def test_interactive_retry_then_success(tmp_path: Path):
    ui = FakeUi({"What now": ["r"]})
    ctx = make_test_context(tmp_path, ui=ui, unattended=False)
    step = Probe("a", fail_times=1)
    assert Wizard(ctx, [step]).run() == 0
    assert step.runs == 2
    shown = ui.text
    assert "NS-E105" in shown and "What to do:" in shown and "Check the internet" in shown


def test_interactive_quit_marks_failed_and_exits_2(tmp_path: Path):
    ui = FakeUi({"What now": ["q"]})
    ctx = make_test_context(tmp_path, ui=ui, unattended=False)
    assert Wizard(ctx, [Probe("a", fail_times=5), Probe("b")]).run() == EXIT_QUIT
    st = load_install_state(ctx.paths)
    assert st.steps["a"].status == "failed" and "NS-E105" in (st.steps["a"].error or "")
    assert "b" not in st.steps


def test_skip_is_offered_only_for_optional_steps(tmp_path: Path):
    ui = FakeUi({"What now": ["s"]})
    ctx = make_test_context(tmp_path, ui=ui, unattended=False)
    assert Wizard(ctx, [Probe("a", fail_times=5, optional=True)]).run() == 0
    assert status(ctx) == {"a": "skipped"}
    assert "[S]kip" in ui.asked[0]

    ui2 = FakeUi({"What now": ["q"]})
    ctx2 = make_test_context(tmp_path / "second", ui=ui2, unattended=False)
    Wizard(ctx2, [Probe("a", fail_times=5, optional=False)]).run()
    assert "[S]kip" not in ui2.asked[0]


def test_log_choice_shows_log_tail_then_asks_again(tmp_path: Path):
    ui = FakeUi({"What now": ["l", "q"]})
    ctx = make_test_context(tmp_path, ui=ui, unattended=False)
    Wizard(ctx, [Probe("a", fail_times=5)]).run()
    assert "Last log lines" in ui.text and "stage_error" in ui.text
    assert len(ui.asked) == 2


def test_unattended_retries_three_times_with_backoff_then_bundles(tmp_path: Path):
    sleeps: list[float] = []
    runner = FakeRunner(tmp_path / "desktop")
    ctx = make_test_context(tmp_path, runner=runner, unattended=True)
    ctx.sleep = sleeps.append
    step = Probe("a", fail_times=99, error=make_error("NS-E109", "bad hash"))
    assert Wizard(ctx, [step, Probe("b")]).run() == EXIT_FAILED
    assert step.runs == 4                      # first try + three retries
    assert sleeps == [5, 15, 45]
    assert runner.called("ns doctor --bundle")
    assert "NS-E109" in ctx.ui.text
    assert status(ctx) == {"a": "failed"}


def test_unattended_recovers_when_a_retry_works(tmp_path: Path):
    ctx = make_test_context(tmp_path, unattended=True)
    step = Probe("a", fail_times=2)
    assert Wizard(ctx, [step]).run() == 0 and step.runs == 3


def test_unexpected_exception_is_shown_without_a_code(tmp_path: Path):
    ctx = make_test_context(tmp_path, unattended=True)
    ctx.sleep = lambda _s: None
    step = Probe("a", fail_times=9, error=RuntimeError("kaboom"))
    assert Wizard(ctx, [step], backoff=(0,)).run() == EXIT_FAILED
    assert "kaboom" in ctx.ui.text and "unexpected" in ctx.ui.text


# ---------------------------------------------------------------------------------------------- helpers
def test_describe_failure_variants():
    f = describe_failure(make_error("NS-E104", "10 GB free"))
    assert (f.code, f.detail) == ("NS-E104", "10 GB free") and "60 GB" in f.hint
    g = describe_failure(AnswersError("bad run_time"))
    assert g.code == "answers.json" and "bad run_time" in g.detail
    h = describe_failure(ValueError("x"))
    assert h.code == "unexpected" and "ValueError" in h.detail


def test_formerly_pending_codes_come_from_errors_json():
    for code, kind in (("NS-E111", "error"), ("NS-E112", "error"), ("NS-W112", "warning")):
        err = make_error(code, "detail")
        assert isinstance(err, NsError) and err.code == code
        assert err.definition.kind == kind and err.definition.hint
        assert str(err).startswith(f"{code}: ")
    assert type(make_error("NS-E101")) is NsError


def test_parse_args_defaults_and_flags():
    a = parse_args(["--simulate-downloads", "--home", "X", "--halt-after", "s03_download_models"])
    assert a.simulate_downloads and a.home == "X" and a.halt_after == "s03_download_models"
    assert a.answers is None and a.non_interactive is False
    b = parse_args(["--answers", "a.json", "--non-interactive", "--no-head-checks"])
    assert b.answers == "a.json" and b.non_interactive and b.no_head_checks


def test_build_context_simulate_uses_fakes(tmp_path: Path, repo_root: Path):
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({"channel": {"display_name": "X", "niche": "y"}}), encoding="utf-8")
    args = parse_args(["--simulate-downloads", "--home", str(tmp_path / "h"), "--answers", str(answers),
                       "--app-path", str(repo_root), "--preflight", str(tmp_path / "none.json")])
    ctx = build_context(args)
    assert ctx.simulate and ctx.unattended and ctx.home_forced
    assert isinstance(ctx.runner, FakeRunner) and isinstance(ctx.store, MemoryStore)
    assert ctx.raw_answers["channel"]["display_name"] == "X"
    assert ctx.preflight and ctx.preflight["simulated"] is True and ctx.profile == "gpu"
    assert ctx.free_gb_fn(tmp_path) >= 500
    assert ctx.paths.home == tmp_path / "h"


def test_build_context_rejects_a_broken_answers_file(tmp_path: Path, repo_root: Path):
    answers = tmp_path / "answers.json"
    answers.write_text("{ nope", encoding="utf-8")
    args = parse_args(["--simulate-downloads", "--home", str(tmp_path / "h"), "--answers", str(answers),
                       "--app-path", str(repo_root)])
    with pytest.raises(AnswersError):
        build_context(args)
