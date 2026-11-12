"""s06_tune: write the baseline runtime.json and run ``ns tune`` (component C1, spec 5).

A failed tune is not fatal: the safe defaults in the baseline stay and the operator is told. A dev install
(docs/DEV_MODE.md) has no model to tune: it keeps the baseline.
"""

from __future__ import annotations

import re

from installer.stage2 import reinstall
from installer.stage2.base import Step
from installer.stage2.context import StepContext
from installer.stage2.runtime_cfg import runtime_settled
from ns_common.commandline import typed
from ns_common.config import load_runtime

#: ` with {"n_gpu_layers": ...}`: the settings an older `ns tune` printed after its speed.
_JSON_TAIL = re.compile(r"\s+with\s+\{.*\}\s*$")


class S06Tune(Step):
    """Baseline runtime.json, then the tuning sweep (1 to 3 minutes)."""

    id = "s06_tune"
    title = "Tune for this PC"
    # the baseline runtime.json is written first: a run killed during the sweep must not read as tuned
    clears_on_run = ("tuned", "smoke", "smoke_version")

    def check(self, ctx: StepContext) -> bool:
        """True once tuning ran (or fell back to defaults) on an earlier run, runtime.json is there AND loads
        as it is (a damaged one is not "done": ``run`` sets it aside and writes a new one), it was made for
        this kind of install, and no change that the answers asked of it (a new model, the engine test) is
        still owed (D-14). One question to the file's one mechanism: ``runtime_cfg.runtime_settled``."""
        return ctx.flag("tuned") in ("tuned", "defaults") and runtime_settled(ctx)

    def run(self, ctx: StepContext) -> None:
        """Write the baseline, make runtime.json follow the answers, then call ``ns tune``."""
        # One pass over the file. The models are downloaded and verified (s03): only now does runtime.json
        # name the new one, so the nightly run never starts llama-server on a file that is still
        # downloading (D-14).
        reinstall.apply_pending_runtime(ctx)
        if ctx.dev_mode:
            ctx.ui.say("Dev mode: the language model is a fake, so there is nothing to tune.")
            ctx.set_flag("tuned", "defaults")
            return
        ctx.ui.say(
            "Tuning the language model for your graphics card (a few minutes: it waits for the card to "
            "settle between tries and times each one three times) ..."
        )
        res = ctx.run_tool("ns", ["tune"], timeout=3600)
        # the result in words (what `ns tune` prints); the settings as raw JSON (an older `ns tune`'s "Best:"
        # line, G-26) are never shown
        shown = [_JSON_TAIL.sub("", ln) for ln in res.stdout.strip().splitlines() if ln.strip()]
        for line in shown[-8:]:
            ctx.ui.say(f"  {line}")
        if res.ok:
            ctx.set_flag("tuned", "tuned")
            return
        ctx.log.warn("tune_failed", f"exit {res.returncode}: {res.text[-300:]}")
        ctx.ui.say(
            "Tuning did not finish; Nightshift will use safe default settings. "
            f"You can run '{typed(ctx.paths.home, 'ns tune')}' again later.", "yellow",
        )
        ctx.set_flag("tuned", "defaults")

    def verify(self, ctx: StepContext) -> None:
        """runtime.json exists and validates."""
        load_runtime(ctx.paths)
