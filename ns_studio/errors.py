"""Studio-level errors for conditions that have no code in contracts/errors.json yet (component C7).

`StudioError` prefers the definition in `errors.json` when the orchestrator has accepted the code
(see contracts/CHANGE_REQUESTS.md CR-C7-1) and falls back to the local table below otherwise.
"""

from __future__ import annotations

from ns_common.errors import load_errors

_LOCAL: dict[str, tuple[str, str]] = {
    "NS-E320": (
        "The channel profile is invalid.",
        "Fix the listed rule in config/channels/<slug>.yaml or via 'studio channel edit'.",
    ),
    "NS-E321": (
        "Channels are not distinct enough.",
        "Change the niche, voice, caption styles or visual treatments of one channel.",
    ),
    "NS-E322": ("Unknown channel.", "Run 'studio channel list' to see the configured channels."),
    "NS-E323": (
        "The analytics CSV could not be read.",
        "Export the table from YouTube Studio again and keep its header row.",
    ),
    "NS-E324": (
        "Calibration data is missing or incomplete.",
        "Run 'studio calibrate --channel <slug>' first and fill in every score.",
    ),
    "NS-E325": (
        "The variation pools are exhausted.",
        "Widen the pools with 'studio channel edit' or wait for the 30-day ledger to clear.",
    ),
    "NS-E326": (
        "Output not found or not in a state that allows this action.",
        "Run 'studio review' to list the outputs and their status.",
    ),
}


class StudioError(Exception):
    """A coded studio condition; `code`, `message` and `hint` mirror `NsError` for the CLI."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        known = load_errors().get(code)
        if known is not None:
            self.message, self.hint = known.message, known.hint
        else:
            self.message, self.hint = _LOCAL.get(code, ("Studio error.", ""))
        super().__init__(str(self))

    def __str__(self) -> str:
        base = f"{self.code}: {self.message}"
        return f"{base} ({self.detail})" if self.detail else base
