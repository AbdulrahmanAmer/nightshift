"""s05_opencode_prewarm: confirm the OpenCode binary runs (component C1; skipped unless install_code).

This step proves the binary runs. Whether OpenCode sees the ``local/code`` model cannot be checked here:
``config/opencode.json`` is written by s07, after this step. ``confirm_local_code`` does that check and is
called by s08, once the file exists.
"""

from __future__ import annotations

from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.errors import make_error


def confirm_local_code(ctx: StepContext) -> bool:
    """Ask OpenCode (with ``config/opencode.json``) for its models and look for ``local/code``.

    Never raises: a missing model only means Nightshift Code will not work, the rest of Nightshift does.
    """
    exe = ctx.paths.bin_opencode / "opencode.exe"
    res = ctx.runner.run(
        [str(exe), "models"], timeout=120, tag="opencode models",
        env={"OPENCODE_CONFIG": str(ctx.paths.opencode_file)},
    )
    ok = res.ok and "local/code" in res.stdout
    ctx.set_flag("opencode_local_code", ok)
    if ok:
        ctx.ui.say("Nightshift Code sees its model (local/code).")
    else:
        ctx.ui.say("Nightshift Code cannot find local/code; see config/opencode.json.", "yellow")
        ctx.log.warn("opencode_local_code_missing", res.text[-200:])
    return ok


class S05OpencodePrewarm(Step):
    """Run ``opencode --version`` once."""

    id = "s05_opencode_prewarm"
    title = "Nightshift Code check"
    optional = True

    def skip_reason(self, ctx: StepContext) -> str | None:
        """Only when the operator chose Nightshift Code (a dev install never does: no OpenCode download)."""
        if ctx.dev_mode:
            return "dev mode: Nightshift Code is not installed"
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
