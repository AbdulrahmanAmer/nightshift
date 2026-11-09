"""Policy gate framework: one class per check (spec section 18.8, component C7)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from ns_common.models import PolicyCheck
from ns_studio.context import JobContext

Gate = Literal["text", "final"]

# Error code raised when a check of this name fails (contracts/errors.json, spec 18.8).
CODES: dict[str, str] = {
    "unsafe_topics": "NS-E601",
    "real_entities": "NS-E602",
    "misleading_claims": "NS-E603",
    "hook_title_honesty": "NS-E604",
    "self_similarity": "NS-E605",
    "metadata_spam": "NS-E606",
    "standalone_short": "NS-E607",
    "render_uniqueness": "NS-E611",
    "long_runtime": "NS-E612",
    "clip_reuse": "NS-E613",
    "audio_only_voice": "NS-E614",
}
POLICY_CODES: frozenset[str] = frozenset(CODES.values())


class Check(ABC):
    """A single policy check. `run` returns a `PolicyCheck`; it never raises for a policy failure."""

    name: str
    gate: Gate

    @property
    def code(self) -> str | None:
        """The NS-E6xx code this check raises on failure (None for advice-only checks)."""
        return CODES.get(self.name)

    @abstractmethod
    def run(self, ctx: JobContext) -> PolicyCheck:
        """Evaluate the check against the job directory and services in `ctx`."""

    def hint(self, ctx: JobContext) -> str | None:
        """A hint for this job that replaces the generic one of the code, or None to keep it.

        Called by `run_gate` right after `run` failed, on the same instance.
        """
        return None


def ok(detail: str = "") -> PolicyCheck:
    """A passing result."""
    return PolicyCheck(passed=True, detail=detail)


def fail(detail: str) -> PolicyCheck:
    """A failing result with a human-readable reason."""
    return PolicyCheck(passed=False, detail=detail)
