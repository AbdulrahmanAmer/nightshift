"""The Nightshift setup wizard (component C1, spec 5 stage 2).

Runs the steps s00 to s10 in order. Every step has ``check`` / ``run`` / ``verify`` and its result is
written to ``install_state.json``, so a crash, reboot or closed window resumes at the failed step.
Interactive errors offer [R]etry / [S]kip (optional steps only) / [L]og / [Q]uit; unattended runs retry
three times with backoff, write the diagnostics bundle and exit non-zero.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from installer.stage2.answers import AnswersError, find_answers, load_raw
from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.runner import SubprocessRunner
from installer.stage2.secrets import KeyringStore, MemoryStore
from installer.stage2.simulate import FakeRunner
from installer.stage2.state import StateStore
from installer.stage2.sysinfo import free_gb, load_preflight, sample_preflight
from installer.stage2.ui import Ui
from ns_common.config import load_manifest
from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.paths import Paths, repo_root

EXIT_OK = 0
EXIT_QUIT = 2
EXIT_FAILED = 3
EXIT_HALTED = 75
EXIT_INTERRUPTED = 130
BACKOFF_S = (5, 15, 45)


@dataclass(frozen=True)
class Failure:
    """What the operator sees when a step fails."""

    code: str
    message: str
    hint: str
    detail: str


def describe_failure(exc: Exception) -> Failure:
    """Turn any exception into code + plain message + fix hint."""
    if isinstance(exc, NsError):
        d = exc.definition
        return Failure(exc.code, d.message, d.hint, exc.detail)
    if isinstance(exc, AnswersError):
        return Failure(
            "answers.json",
            "The answers file has a problem.",
            "Fix the value named below, or remove answers.json to be asked the questions.",
            str(exc),
        )
    return Failure(
        "unexpected",
        "An unexpected error stopped this step.",
        "Choose L to see the log, then send the diagnostics zip (see OPERATOR.md).",
        f"{type(exc).__name__}: {exc}",
    )


class Wizard:
    """Runs the steps with resume, error UX and optional halt (for kill-and-resume tests)."""

    def __init__(
        self,
        ctx: StepContext,
        steps: Sequence[Step],
        *,
        halt_after: str | None = None,
        backoff: Sequence[float] = BACKOFF_S,
    ) -> None:
        self.ctx = ctx
        self.steps = list(steps)
        self.halt_after = halt_after
        self.backoff = tuple(backoff)

    def run(self) -> int:
        """Run all steps. Exit codes: 0 ok, 2 quit, 3 unattended failure, 75 halted, 130 interrupted."""
        try:
            return self._run()
        except KeyboardInterrupt:
            self.ctx.ui.say("")
            self.ctx.ui.say("Setup paused. Run INSTALL.cmd again to carry on where it stopped.")
            return EXIT_INTERRUPTED

    def _run(self) -> int:
        ctx = self.ctx
        ctx.resumed = ctx.state.any_done()
        ctx.state.begin(ctx.profile)
        ctx.log.info("wizard_start", resumed=ctx.resumed, simulate=ctx.simulate, unattended=ctx.unattended)
        for step in self.steps:
            code = self._step(step)
            if code is not None:
                return code
            if self.halt_after == step.id:
                ctx.ui.say(f"Halted after {step.id} as requested (--halt-after).")
                ctx.log.info("wizard_halt", step=step.id)
                return EXIT_HALTED
        ctx.state.complete()
        ctx.log.info("wizard_end", warnings=len(ctx.warnings))
        return EXIT_OK

    # ---- one step ----
    def _safe_check(self, step: Step) -> bool:
        try:
            return bool(step.check(self.ctx))
        except Exception as e:
            self.ctx.log.warn("check_failed", f"{step.id}: {e}")
            return False

    def _step(self, step: Step) -> int | None:
        ctx, ui = self.ctx, self.ctx.ui
        reason = step.skip_reason(ctx)
        if reason:
            ctx.state.mark(step.id, "skipped")
            ui.say(f"[skip] {step.title}: {reason}", style="dim")
            ctx.log.info("stage_skip", step=step.id, reason=reason)
            return None
        if ctx.state.is_done(step.id) and self._safe_check(step):
            ui.say(f"[done] {step.title} (already complete)", style="green")
            return None
        ui.rule(step.title)
        attempts = 0
        while True:
            ctx.log.info("stage_start", step=step.id)
            t0 = time.perf_counter()
            try:
                if not step.check(ctx):
                    step.run(ctx)
                step.verify(ctx)
            except Exception as exc:
                failure = describe_failure(exc)
                ctx.log.error(
                    "stage_error", failure.message, step=step.id, code=failure.code, detail=failure.detail
                )
                if isinstance(exc, NsError) and exc.code.startswith("NS-W"):
                    ctx.warn(exc.code, exc.detail)
                    ctx.state.mark(step.id, "done")
                    return None
                attempts += 1
                action = self._on_failure(step, failure, attempts)
                if action == "retry":
                    continue
                if action == "skip":
                    ctx.state.mark(step.id, "skipped", error=f"{failure.code}: {failure.detail}")
                    return None
                ctx.state.mark(step.id, "failed", error=f"{failure.code}: {failure.detail}")
                return self._give_up(failure)
            ctx.log.info("stage_end", step=step.id, duration_s=round(time.perf_counter() - t0, 3))
            ctx.state.mark(step.id, "done")
            return None

    def _show_failure(self, step: Step, failure: Failure) -> None:
        body = f"{failure.code}  {failure.message}"
        if failure.detail:
            body += f"\n{failure.detail}"
        body += f"\n\nWhat to do: {failure.hint}"
        self.ctx.ui.panel(body, title=f"{step.title} failed")

    def _on_failure(self, step: Step, failure: Failure, attempts: int) -> str:
        """Decide retry / skip / quit for a failed step."""
        ctx, ui = self.ctx, self.ctx.ui
        self._show_failure(step, failure)
        if ctx.unattended or not ui.interactive:
            if attempts <= len(self.backoff):
                wait = self.backoff[attempts - 1]
                ui.say(f"Retry {attempts} of {len(self.backoff)} in {wait:g} s ...")
                ctx.sleep(wait)
                return "retry"
            return "quit"
        options = ["r", "l", "q"] + (["s"] if step.optional else [])
        menu = "[R]etry  " + ("[S]kip  " if step.optional else "") + "[L]og  [Q]uit"
        while True:
            choice = ui.choose(f"What now? {menu}", options, default="r").lower()
            if choice == "r":
                return "retry"
            if choice == "s" and step.optional:
                return "skip"
            if choice == "l":
                self._show_log_tail()
                continue
            return "quit"

    def _show_log_tail(self, lines: int = 20) -> None:
        logs = sorted(self.ctx.paths.logs.glob("installer-*.jsonl"))
        if not logs:
            self.ctx.ui.say("(no log yet)")
            return
        tail = logs[-1].read_text(encoding="utf-8").splitlines()[-lines:]
        self.ctx.ui.say(f"Last log lines from {logs[-1]}:")
        for line in tail:
            self.ctx.ui.say(line)

    def _give_up(self, failure: Failure) -> int:
        ctx, ui = self.ctx, self.ctx.ui
        if ctx.unattended or not ui.interactive:
            ui.say(f"Setup stopped: {failure.code}. Writing the diagnostics bundle ...")
            res = ctx.run_tool("ns", ["doctor", "--bundle"], timeout=600)
            ctx.log.info("bundle_attempt", ok=res.ok)
            fail_msg = "The diagnostics bundle could not be written; see the logs folder."
            ui.say(res.text if res.ok else fail_msg)
            ui.say(f"Send the nightshift-diagnostics zip from your Desktop. Error code: {failure.code}")
            return EXIT_FAILED
        ui.say("Setup stopped. Run INSTALL.cmd again to continue from this step.")
        return EXIT_QUIT


# ---------------------------------------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------------------------------------
def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse the wizard command line."""
    p = argparse.ArgumentParser(prog="python -m installer.stage2.wizard", description="Nightshift setup")
    p.add_argument("--app-path", help="the extracted Nightshift folder (default: this checkout)")
    p.add_argument("--answers", help="answers.json for unattended setup")
    p.add_argument("--home", help="install folder (default: the NIGHTSHIFT_HOME user variable)")
    p.add_argument("--simulate-downloads", action="store_true",
                   help="CI mode: HEAD checks + tiny fixture files, scripted commands, nothing installed")
    p.add_argument("--non-interactive", action="store_true", help="never prompt; use defaults")
    p.add_argument("--halt-after", metavar="STEP", help="stop with exit code 75 after this step (tests)")
    p.add_argument("--preflight", help="stage 1 hardware summary (default: the temp-folder JSON)")
    p.add_argument("--no-head-checks", action="store_true", help="simulate mode: skip HEAD checks")
    return p.parse_args(list(argv) if argv is not None else None)


def build_context(args: argparse.Namespace, ui: Ui | None = None) -> StepContext:
    """Create the real (or simulated) context from the command line. Raises AnswersError."""
    app_path = Path(args.app_path).resolve() if args.app_path else repo_root()
    paths = Paths(Path(args.home)) if args.home else Paths.from_env()
    explicit = Path(args.answers) if args.answers else None
    answers_path = find_answers(app_path, paths, explicit)
    raw = load_raw(answers_path) if answers_path else {}
    in_config = answers_path is not None and answers_path == paths.config / "answers.json"
    non_interactive = bool(args.non_interactive) or not sys.stdin.isatty()
    unattended = non_interactive or (answers_path is not None and not in_config)

    default_pf = Path(tempfile.gettempdir()) / "nightshift-preflight.json"
    pf_path = Path(args.preflight) if args.preflight else default_pf
    preflight = load_preflight(pf_path)
    if preflight is None and args.simulate_downloads:
        preflight = sample_preflight(paths.home, app_path)

    ui = ui or Ui(interactive=not non_interactive)
    if args.simulate_downloads:
        runner = FakeRunner(paths.home / "_simulated_desktop")
        store = MemoryStore()
    else:
        runner = SubprocessRunner()
        store = KeyringStore()
    return StepContext(
        app_path=app_path,
        paths=paths,
        ui=ui,
        runner=runner,
        store=store,
        manifest=load_manifest(app_path / "manifest.json"),
        log=NsLogger("installer", paths.logs),
        state=StateStore(paths, app_path),
        simulate=bool(args.simulate_downloads),
        unattended=unattended,
        answers_path=answers_path,
        raw_answers=raw,
        preflight=preflight,
        home_forced=bool(args.home),
        head_checks=not args.no_head_checks,
        free_gb_fn=(lambda _p: 500.0) if args.simulate_downloads else free_gb,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: ``python -m installer.stage2.wizard``."""
    from installer.stage2.steps import default_steps

    args = parse_args(argv)
    ui = Ui(interactive=False)
    try:
        ctx = build_context(args)
    except AnswersError as e:
        ui.say(f"answers.json problem: {e}", style="red")
        return EXIT_QUIT
    return Wizard(ctx, default_steps(), halt_after=args.halt_after).run()


if __name__ == "__main__":
    sys.exit(main())
