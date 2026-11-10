"""s10_summary: status table, where things live, next steps and the diagnostics bundle (component C1).

Runs ``ns doctor --bundle`` and tells the operator exactly which file to upload to the Drive folder.
"""

from __future__ import annotations

from pathlib import Path

from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.devmode import BANNER
from installer.stage2.errors import make_error
from installer.stage2.web import nightly_view
from installer.stage2.winenv import desktop_dir

NEXT_STEPS = (
    "Next: open 'Nightshift Dashboard' on your Desktop and press Run now to make your first video."
)
DEV_NEXT_STEPS = (
    "Dev mode: open 'Nightshift Dashboard' and press Create it now. The job runs every stage on fake engines "
    "and renders a real video into the outbox. Nothing is scheduled."
)
#: The flags the sentence about Automatic runs is made from (`nightly_view.summary`).
NIGHTLY_FLAGS = ("schedule", "nightly_line", "nightly_state")


def newest_bundle(folder: Path | None) -> Path | None:
    """The most recent nightshift-diagnostics zip in ``folder``."""
    if folder is None or not folder.is_dir():
        return None
    zips = sorted(folder.glob("nightshift-diagnostics-*.zip"), key=lambda p: p.stat().st_mtime)
    return zips[-1] if zips else None


class S10Summary(Step):
    """Final report."""

    id = "s10_summary"
    title = "Summary"

    def check(self, ctx: StepContext) -> bool:
        """The summary is shown every time the wizard reaches it."""
        return False

    def run(self, ctx: StepContext) -> None:
        """Print results, locations and next steps; write the diagnostics zip."""
        ui = ctx.ui
        rows = [[s.id, ctx.state.status(s.id)] for s in _all_steps(ctx)]
        ui.table("Setup result", ["Step", "Result"], rows, status_col=1)
        if ctx.warnings:
            ui.say("Warnings (setup works, but read these):", "yellow")
            for code, detail in ctx.warnings:
                ui.say(f"  {code} {detail}", "yellow")
        ans = ctx.ensure_answers()
        ui.table(
            "Where things live", ["What", "Where"],
            [
                ["Everything Nightshift makes", str(ctx.paths.home)],
                ["Videos to review and upload", str(ctx.paths.outbox)],
                ["Settings", str(ctx.paths.config)],
                ["Logs", str(ctx.paths.logs)],
                ["Program files", str(ctx.app_path)],
            ],
        )
        schedule = ctx.flag("schedule")
        if ctx.dev_mode:
            ui.say(f"{BANNER}.", "yellow")
        if ctx.dev_mode:
            if schedule == "installed" and ctx.flag("schedule_mode") == "disabled":
                ui.say(f"Nightly task: registered for {ans.run_time} but DISABLED (dev mode); it never runs.")
        else:
            # one sentence, true of what the record says now: the scheduler step's own when the state is still
            # the one it left, else the state as it is (a run in which that step had nothing to do)
            flags = {name: ctx.flag(name) for name in NIGHTLY_FLAGS}
            ui.say(nightly_view.summary(ctx.paths.home, flags)["line"])
        ui.say(DEV_NEXT_STEPS if ctx.dev_mode else NEXT_STEPS, "bold green")

        res = ctx.run_tool("ns", ["doctor", "--bundle"], timeout=900)
        folder = ctx.desktop_override or desktop_dir(ctx.runner)
        bundle = newest_bundle(folder) if res.ok else None
        ui.bundle(bundle)
        if bundle is not None:
            ctx.set_flag("bundle", str(bundle))
            ui.panel(
                f"Please upload this file to the 'diagnostics' folder on Google Drive:\n{bundle}",
                title="Send this back", style="cyan",
            )
        else:
            ui.say("The diagnostics zip is not available in this version; send the logs folder.", "yellow")
            ctx.log.warn("bundle_missing", res.text[-200:])
        # The headless engine `ns verify` may have left behind never starts the dashboard service; make sure
        # the first Dashboard click starts a fresh engine that does. `ns stop` is a no-op when nothing runs.
        ctx.run_tool("ns", ["stop"], timeout=120)
        ctx.set_flag("summary_shown", True)

    def verify(self, ctx: StepContext) -> None:
        """All earlier steps ended done or skipped."""
        earlier = [s.id for s in _all_steps(ctx) if s.id != self.id]
        bad = [i for i in earlier if ctx.state.status(i) not in ("done", "skipped")]
        if bad:
            raise make_error("NS-E202", f"steps not finished: {', '.join(bad)}")


def _all_steps(ctx: StepContext) -> list[Step]:
    from installer.stage2.steps import bridge_steps, default_steps

    return bridge_steps() if ctx.ui.bridge else default_steps()
