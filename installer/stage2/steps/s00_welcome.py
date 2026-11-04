"""s00_welcome: the spec A.2 welcome text and the hardware check summary from stage 1 (component C1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.errors import make_error

WELCOME_FILE = Path(__file__).resolve().parents[2] / "welcome.txt"


def hardware_rows(preflight: dict[str, Any]) -> list[list[str]]:
    """Rows [check, result, detail] from the stage 1 summary."""
    rows: list[list[str]] = []
    for c in preflight.get("checks", []):
        result = str(c.get("status", "ok"))
        code = f" {c['code']}" if c.get("code") else ""
        rows.append([str(c.get("label", c.get("id", ""))), result, f"{c.get('value', '')}{code}"])
    return rows


class S00Welcome(Step):
    """Show the welcome screen and the machine check table."""

    id = "s00_welcome"
    title = "Welcome"

    def check(self, ctx: StepContext) -> bool:
        """On a resume the long welcome text is not shown again."""
        return ctx.resumed

    def run(self, ctx: StepContext) -> None:
        """Print the welcome text and the hardware table; wait for Enter when a person is watching."""
        ctx.ui.markdown(WELCOME_FILE.read_text(encoding="utf-8"))
        pf = ctx.preflight
        if pf is None:
            ctx.ui.say("No hardware summary from stage 1 was found; the check table is not shown.", "yellow")
        else:
            ctx.ui.table("Your PC", ["Check", "Result", "Detail"], hardware_rows(pf), status_col=1)
            gpu = pf.get("gpu") or {}
            ctx.ui.say(
                f"Profile: {pf.get('profile', 'gpu')}   GPU: {gpu.get('name') or 'none'}   "
                f"CUDA: {pf.get('cuda') or 'n/a'}   RAM: {pf.get('ram_gb', '?')} GB"
            )
            for c in pf.get("checks", []):
                if c.get("status") == "warn" and c.get("hint"):
                    ctx.ui.say(f"  ! {c.get('code', '')} {c['hint']}", "yellow")
        if ctx.unattended:
            ctx.ui.say("Unattended setup: starting now.")
        else:
            ctx.ui.pause("Press Enter to start setup")

    def verify(self, ctx: StepContext) -> None:
        """A red row in the stage 1 summary stops the install with that row's code."""
        for c in (ctx.preflight or {}).get("checks", []):
            if c.get("status") == "fail" and c.get("code"):
                raise make_error(str(c["code"]), str(c.get("value", "")))
