"""Step base class for the wizard (component C1; ENGINEERING.md rule 5: check / run / verify)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from installer.stage2.context import StepContext


class Step(ABC):
    """One resumable install step.

    ``check()`` answers "is this already satisfied?" cheaply; ``run()`` does the work; ``verify()`` proves the
    result and raises ``NsError`` (code from errors.json) when it is not there. Steps must be idempotent:
    running one twice changes nothing the second time.
    """

    id: str = ""
    title: str = ""
    optional: bool = False
    #: Flags the wizard forgets just before ``run()`` starts. A step lists the flags that say its own work is
    #: finished (a run that dies half way must not leave them true) and the flags of later steps that its work
    #: makes untrue (``smoke`` after anything the smoke test would test). Set again by the step at its end.
    clears_on_run: tuple[str, ...] = ()

    def skip_reason(self, ctx: StepContext) -> str | None:
        """Return a reason when this step does not apply (it is then recorded as skipped)."""
        return None

    @abstractmethod
    def check(self, ctx: StepContext) -> bool:
        """True when the step's result is already in place."""

    @abstractmethod
    def run(self, ctx: StepContext) -> None:
        """Do the work."""

    @abstractmethod
    def verify(self, ctx: StepContext) -> None:
        """Raise NsError when the result is missing or wrong."""
