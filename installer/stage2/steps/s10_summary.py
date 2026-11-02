"""s10_summary: status table, where things live, next steps and the diagnostics bundle (component C1).

Runs ``ns doctor --bundle`` and tells the operator exactly which file to upload to the Drive folder.
"""

from __future__ import annotations

from pathlib import Path

from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.errors import make_error
from installer.stage2.winenv import desktop_dir

NEXT_STEPS = (
    "Next: open 'Nightshift Dashboard' on your Desktop and press Run now to make your first video."
)


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
        if schedule == "installed":
            ui.say(f"Nightly run: every night at {ans.run_time} (the PC wakes itself).")
        ui.say(NEXT_STEPS, "bold green")

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
