"""The Nightshift setup wizard (component C1, spec 5 stage 2).

Runs the steps s00 to s10 in order. Every step has ``check`` / ``run`` / ``verify`` and its result is
written to ``install_state.json``, so a crash, reboot or closed window resumes at the failed step.
Interactive errors offer [R]etry / [S]kip (optional steps only) / [L]og / [Q]uit; unattended runs retry
three times with backoff, write the diagnostics bundle and exit non-zero.

The bound of the unattended retries: a failed step runs at most three more rounds, after 5, 15 and 45 s
(``BACKOFF_S``), each said on screen and logged (``stage_retry``) before it starts. A site's NS-E105 is not
retried (it had its ten minutes). On a dead internet line one round of the download step takes about 20 to 25
minutes (each file's ten-minute network window, then at most ten minutes waiting for the network before
another pass; card download-outage), so an unattended setup on a dead line ends by itself after about 80 to
100 minutes, and a line that comes back within that time needs nobody.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from installer.stage2 import model_repair
from installer.stage2.answers import AnswersError, find_answers, load_raw
from installer.stage2.base import Step
from installer.stage2.bridge import BridgeUi, SetupStopped
from installer.stage2.context import StepContext
from installer.stage2.devmode import BANNER, env_enabled, relax_checks, reset_for_mode_switch
from installer.stage2.diagzip import write_console_install_report, write_fallback_bundle
from installer.stage2.errors import describe_code
from installer.stage2.runner import SubprocessRunner
from installer.stage2.secrets import KEY_NAMES, KeyringStore, MemoryStore, ResilientStore
from installer.stage2.simulate import FakeRunner
from installer.stage2.state import StateStore
from installer.stage2.sysinfo import free_gb, load_preflight, sample_preflight
from installer.stage2.ui import Ui
from installer.stage2.web.filesdir import beside as files_beside
from ns_common import __version__
from ns_common.commandline import render
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
    data: dict[str, Any] = field(default_factory=dict)      # NsError.data: host, status, side ...


def describe_failure(exc: Exception) -> Failure:
    """Turn any exception into code + plain message + fix hint."""
    if isinstance(exc, NsError):
        d = exc.definition
        return Failure(exc.code, d.message, d.hint, exc.detail, dict(exc.data))
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
        self._current: Step | None = None

    def run(self) -> int:
        """Run all steps. Exit codes: 0 ok, 2 quit, 3 unattended failure, 75 halted, 130 interrupted."""
        try:
            return self._run()
        except (KeyboardInterrupt, SetupStopped):
            current, self._current = self._current, None
            if current is not None:
                self.ctx.state.mark(current.id, "pending")
                self.ctx.ui.step_status(current.id, "pending", current.title, None)
            self.ctx.ui.say("")
            self.ctx.ui.say("Setup paused. Run INSTALL.cmd again to carry on where it stopped.")
            return EXIT_INTERRUPTED

    def _run(self) -> int:
        ctx = self.ctx
        ctx.resumed = ctx.state.any_done()
        was_complete = ctx.state.state.completed_at is not None    # read before `begin` clears it: a Repair
        ctx.state.begin(ctx.profile, ctx.dev_mode)
        dropped = ctx.reconcile_flags()
        if dropped:
            ctx.log.warn("stale_flags_dropped", f"flags of an install that no longer exists: {dropped}")
        model_repair.begin(ctx, was_complete=was_complete)
        ctx.log.info(
            "wizard_start", resumed=ctx.resumed, simulate=ctx.simulate, unattended=ctx.unattended,
            dev_mode=ctx.dev_mode,
        )
        if ctx.dev_mode:
            ctx.ui.say(f"{BANNER}. Programs are real, models are stubs, engines are fakes.", style="yellow")
        for step in self.steps:
            if ctx.stop_requested.is_set():
                raise SetupStopped()
            code = self._step(step)
            if code is not None:
                return code
            if self.halt_after == step.id:
                ctx.ui.say(f"Halted after {step.id} as requested (--halt-after).")
                ctx.log.info("wizard_halt", step=step.id)
                return EXIT_HALTED
        ctx.state.complete()
        ctx.log.info("wizard_end", warnings=len(ctx.warnings))
        write_console_install_report(self.ctx.paths, outcome="done", version=__version__)
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
            ui.step_status(step.id, "skipped", step.title, reason)
            ui.say(f"[skip] {step.title}: {reason}", style="dim")
            ctx.log.info("stage_skip", step=step.id, reason=reason)
            return None
        if ctx.state.is_done(step.id) and self._safe_check(step):
            ui.step_status(step.id, "done", step.title, "already complete")
            ui.say(f"[done] {step.title} (already complete)", style="green")
            return None
        ui.rule(step.title)
        ui.step_status(step.id, "running", step.title, None)
        self._current = step
        attempts = 0
        while True:
            ctx.log.info("stage_start", step=step.id)
            t0 = time.perf_counter()
            try:
                if not step.check(ctx):
                    # the work starts: what said "finished" (the step's state, its flags and those of the
                    # steps that test its result) is cleared first, so a run that dies half way leaves nothing
                    # that a rerun could take for the finished work
                    if ctx.state.is_done(step.id):
                        ctx.state.mark(step.id, "pending")
                    cleared = ctx.clear_flags(*step.clears_on_run)
                    if cleared:
                        ctx.log.info("flags_cleared", step=step.id, flags=cleared)
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
                    ui.step_status(step.id, "warning", step.title, exc.code)
                    self._current = None
                    return None
                attempts += 1
                action = self._on_failure(step, failure, attempts)
                if action == "retry":
                    ui.step_status(step.id, "running", step.title, None)
                    continue
                self._current = None
                if action == "skip":
                    ctx.state.mark(step.id, "skipped", error=f"{failure.code}: {failure.detail}")
                    ui.step_status(step.id, "skipped", step.title, failure.code)
                    return None
                ctx.state.mark(step.id, "failed", error=f"{failure.code}: {failure.detail}")
                ui.step_status(step.id, "failed", step.title, failure.code)
                return self._give_up(failure)
            ctx.log.info("stage_end", step=step.id, duration_s=round(time.perf_counter() - t0, 3))
            notes = " ".join(ctx.take_notes())
            if notes:
                # finished, with something the owner must know (a damaged file set aside): the row says it
                # (warning) and the state keeps the sentence, which the install report prints as a note
                ctx.state.mark(step.id, "done", error=notes)
                ui.step_status(step.id, "warning", step.title, notes)
            else:
                ctx.state.mark(step.id, "done")
                ui.step_status(step.id, "done", step.title, None)
            self._current = None
            return None

    def _show_failure(self, step: Step, failure: Failure) -> None:
        body = f"{failure.code}  {failure.message}"
        if failure.detail:
            body += f"\n{failure.detail}"
        hint = failure.hint
        if failure.data.get("side") == "site":
            # the site had its ten minutes: the generic "check the internet" would contradict the detail
            hint = "Run INSTALL.cmd again later: setup continues where it stopped."
        elif failure.data.get("side") == "network":
            # the internet stopped the downloads (card download-outage, ruling 6a): what is downloaded is kept
            hint = "Run INSTALL.cmd again when the internet is back: setup continues where it stopped."
        body += f"\n\nWhat to do: {hint}"
        # shown now: the commands it names as this owner types them (FN-3); the error keeps the plain words
        self.ctx.ui.panel(render(body, self.ctx.paths.home), title=f"{step.title} failed")

    def _on_failure(self, step: Step, failure: Failure, attempts: int) -> str:
        """Decide retry / skip / quit for a failed step."""
        ctx, ui = self.ctx, self.ctx.ui
        decision = ui.decide_failure(step.id, failure, step.optional, attempts)
        if decision in ("retry", "skip", "quit"):
            return decision if decision != "skip" or step.optional else "quit"
        self._show_failure(step, failure)
        if ctx.unattended or not ui.interactive:
            if failure.data.get("side") == "site":
                return "quit"       # the site had its ten minutes already; another round would wait ten more
            if attempts <= len(self.backoff):
                wait = self.backoff[attempts - 1]
                # said and logged before the round starts: which round of how many, and why (ruling 6b)
                line = (f"Retry {attempts} of {len(self.backoff)} in {wait:g} s, because this step stopped "
                        f"with {failure.code}: {failure.message}")
                ui.say(line)
                ctx.log.warn("stage_retry", line, step=step.id, round=attempts, of=len(self.backoff),
                             wait_s=wait, code=failure.code)
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
        if ui.bridge:
            return EXIT_QUIT                # the setup window already showed the failure and the choices
        if ctx.unattended or not ui.interactive:
            ui.say(f"Setup stopped: {failure.code}. Writing the diagnostics bundle ...")
            res = ctx.run_tool("ns", ["doctor", "--bundle"], timeout=600)
            ctx.log.info("bundle_attempt", ok=res.ok)
            if res.ok:
                ui.say(res.text)
            else:
                # before s04 there is no engine venv, so `ns doctor` cannot run: write the zip ourselves
                try:
                    out = write_fallback_bundle(ctx.paths)
                except OSError as e:
                    ctx.log.warn("bundle_fallback_failed", error=str(e))
                    ui.say("The diagnostics bundle could not be written; send the logs folder instead: "
                           f"{ctx.paths.logs}")
                else:
                    ctx.log.info("bundle_fallback", path=str(out))
                    ui.say(f"Diagnostics written to {out} (and a copy on the Desktop when there is one).")
            write_console_install_report(
                ctx.paths, outcome="failed", version=__version__,
                last_error={"code": failure.code, "message": failure.message, "detail": failure.detail},
            )
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
    p.add_argument("--ui-bridge", choices=["stdio"], help="talk JSON lines to the setup window (P1) on stdio")
    p.add_argument("--dev", action="store_true",
                   help="dev install (docs/DEV_MODE.md): stub models, fake engines, no GPU packages")
    p.add_argument("--files-dir", help="the files folder beside INSTALL.cmd (default: <app-path>/files)")
    return p.parse_args(list(argv) if argv is not None else None)


def files_dir_of(args: argparse.Namespace, app_path: Path) -> Path | None:
    """The folder of pinned programs this run may take files from, or None (D-17).

    ``--files-dir`` when the setup window names it (the wizard then runs from ``<home>/app``, which never
    holds the folder), else ``files`` in the program folder (the console wizard run from the bundle). A
    folder that is not there is no folder.
    """
    named = getattr(args, "files_dir", None)
    if not named:
        return files_beside(app_path)
    try:
        return Path(named) if Path(named).is_dir() else None
    except OSError:
        return None


def build_context(args: argparse.Namespace, ui: Ui | None = None) -> StepContext:
    """Create the real (or simulated) context from the command line. Raises AnswersError."""
    app_path = Path(args.app_path).resolve() if args.app_path else repo_root()
    paths = Paths(Path(args.home)) if args.home else Paths.from_env()
    explicit = Path(args.answers) if args.answers else None
    answers_path = find_answers(app_path, paths, explicit)
    raw = load_raw(answers_path) if answers_path else {}
    in_config = answers_path is not None and answers_path == paths.config / "answers.json"
    bridge = getattr(args, "ui_bridge", None) == "stdio"
    non_interactive = bool(args.non_interactive) or not sys.stdin.isatty()
    unattended = non_interactive or (answers_path is not None and not in_config)
    if bridge:
        unattended = False      # the setup window decides failures; nobody retries on a timer

    default_pf = Path(tempfile.gettempdir()) / "nightshift-preflight.json"
    pf_path = Path(args.preflight) if args.preflight else default_pf
    preflight = load_preflight(pf_path)
    if preflight is None and args.simulate_downloads:
        preflight = sample_preflight(paths.home, app_path)

    if bridge and not isinstance(ui, BridgeUi):
        ui = BridgeUi()
    ui = ui or Ui()
    if unattended:
        # The one place that decides whether a person can answer: a run nobody answers for (an answers file
        # outside the home, `--non-interactive`, no console) has a Ui that cannot prompt, whatever its stdin
        # is: a redirected `< NUL` and a real console both report a tty, and a secret prompt reads the
        # console, not stdin, so nothing piped could answer it (D-40). No step checks stdin on its own.
        ui.interactive = False
    log = NsLogger("installer", paths.logs)
    warnings_sink: list[tuple[str, str]] = []  # the context's ``warnings`` (the store warns before it exists)
    if args.simulate_downloads:
        runner = FakeRunner(paths.home / "_simulated_desktop")
        store = MemoryStore()
    else:
        runner = SubprocessRunner(bridge=bridge)
        say = ui  # the window (or console) the setup is shown in

        def warn(line: str) -> None:
            """The key file's copy failed (NS-E120 lock, NS-E119, disk), or was saved on a drive without
            permissions (NS-W116, once): log it, say it, and keep it.

            ``say`` alone is a log line: the setup window shows it as the running step's detail (cut to 160
            characters) and the next line replaces it. ``notice`` is the persistent part: the window keeps a
            setup notice with the code's message and hint, and the summary lists it (``ctx.warnings``).
            """
            found = re.search(r"NS-[EW]\d{3}", line)
            code = found.group(0) if found else "NS-E120"
            log.warn("secret_file_unprotected" if code == "NS-W116" else "secret_file_failed", line)
            say.say(line, "yellow")
            if not bridge:  # the console has no notice card: the hint is printed as `ctx.warn` prints it
                hint = describe_code(code).hint
                if hint:
                    say.say(f"    {hint}", "dim")
            # the summary lists `<code> <detail>`: the line starts with the code, so it is not said twice
            warnings_sink.append((code, re.sub(rf"^{code}:\s*", "", line)))
            say.notice(code, line)

        store = ResilientStore(KeyringStore(), paths, warn=warn)
    dev = bool(getattr(args, "dev", False)) or env_enabled() or bool((preflight or {}).get("dev_mode"))
    if dev and preflight is not None:
        preflight = relax_checks(preflight)     # a graphics card is not a red row in a dev install
    if reset_for_mode_switch(paths.home, dev):
        NsLogger("installer", paths.logs, echo=False).info("mode_switch", dev_mode=dev)
    return StepContext(
        app_path=app_path,
        paths=paths,
        ui=ui,
        runner=runner,
        store=store,
        manifest=load_manifest(app_path / "manifest.json"),
        log=log,
        state=StateStore(paths, app_path),
        warnings=warnings_sink,
        simulate=bool(args.simulate_downloads),
        dev_mode=dev,
        unattended=unattended,
        answers_path=answers_path,
        raw_answers=raw,
        preflight=preflight,
        home_forced=bool(args.home),
        head_checks=not args.no_head_checks,
        free_gb_fn=(lambda _p: 500.0) if args.simulate_downloads else free_gb,
        gate=ui.gate if isinstance(ui, BridgeUi) else None,
        stop_requested=ui.stop if isinstance(ui, BridgeUi) else threading.Event(),
        files_dir=files_dir_of(args, app_path),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: ``python -m installer.stage2.wizard``."""
    from installer.stage2.steps import bridge_steps, default_steps

    args = parse_args(argv)
    if args.ui_bridge == "stdio":
        return _main_bridge(args, bridge_steps())
    ui = Ui(interactive=False)
    try:
        ctx = build_context(args)
    except AnswersError as e:
        ui.say(f"answers.json problem: {e}", style="red")
        return EXIT_QUIT
    return Wizard(ctx, default_steps(), halt_after=args.halt_after).run()


def _main_bridge(args: argparse.Namespace, steps: Sequence[Step]) -> int:
    """Run the wizard for the setup window: JSON lines on stdout, commands on stdin (design doc 1.7)."""
    ui = BridgeUi()
    ui.start_reader()
    try:
        ctx = build_context(args, ui)
    except AnswersError as e:
        ui.say(f"answers.json problem: {e}", style="red")
        ui.emit({"type": "bye", "exitCode": EXIT_QUIT})
        return EXIT_QUIT
    ui.emit({"type": "hello", "pid": os.getpid(), "version": __version__, "resumed": ctx.state.any_done(),
             "devMode": ctx.dev_mode})
    keys = ui.wait_keys()
    for name in KEY_NAMES:
        if keys.get(name):
            ctx.raw_answers[name] = keys[name]      # memory only; s01 validates it, then the keyring
    code = Wizard(ctx, steps, halt_after=args.halt_after).run()
    ui.emit({"type": "bye", "exitCode": code})
    return code


if __name__ == "__main__":
    sys.exit(main())
