"""s05_opencode_prewarm: confirm the OpenCode binary runs (component C1; skipped unless install_code).

The full prewarm (headless run against a live ``llama`` with ``config/opencode.json``, confirming
``local/code``) arrives with the Nightshift Code plan in M4; in M0 this step proves the binary works.
"""

from __future__ import annotations

from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.errors import make_error


class S05OpencodePrewarm(Step):
    """Run ``opencode --version`` once."""

    id = "s05_opencode_prewarm"
    title = "Nightshift Code check"
    optional = True

    def skip_reason(self, ctx: StepContext) -> str | None:
        """Only when the operator chose Nightshift Code."""
        return None if ctx.ensure_answers().install_code else "Nightshift Code was not selected"

    def check(self, ctx: StepContext) -> bool:
        """True after a successful earlier run."""
        return bool(ctx.flag("opencode_ok", False))

    def run(self, ctx: StepContext) -> None:
        """Start the binary once."""
        exe = ctx.paths.bin_opencode / "opencode.exe"
        res = ctx.runner.run([str(exe), "--version"], timeout=120, tag="opencode --version")
        if res.ok:
            ctx.ui.say(f"OpenCode {res.stdout.strip()} is ready.")
            ctx.set_flag("opencode_ok", True)

    def verify(self, ctx: StepContext) -> None:
        """The binary exists and ran."""
        if not (ctx.paths.bin_opencode / "opencode.exe").exists():
            raise make_error("NS-E110", "bin/opencode/opencode.exe is missing")
        if not ctx.flag("opencode_ok", False):
            raise make_error("NS-E110", "opencode --version did not run")
