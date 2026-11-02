"""Studio-level coded conditions (component C7).

`StudioError` carries one of the studio codes NS-E320 to NS-E326 and takes message and hint from
`contracts/errors.json` through `ns_common.errors.describe` (CR-C7-1 is applied).
"""

from __future__ import annotations

from ns_common.errors import describe

#: Code used when a job dies on an exception that carries no code of its own (a bug or a corrupt file).
UNEXPECTED = "NS-E327"


def describe_unexpected(e: BaseException) -> str:
    """One-line detail for an uncoded exception: its type, message and where it was raised."""
    tb = e.__traceback__
    while tb is not None and tb.tb_next is not None:
        tb = tb.tb_next
    where = f" at {tb.tb_frame.f_code.co_filename}:{tb.tb_lineno}" if tb is not None else ""
    return f"{type(e).__name__}: {e}{where}"


class StudioError(Exception):
    """A coded studio condition; `code`, `message` and `hint` mirror `NsError` for the CLI."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        definition = describe(code)
        self.message, self.hint = definition.message, definition.hint
        super().__init__(str(self))

    def __str__(self) -> str:
        base = f"{self.code}: {self.message}"
        return f"{base} ({self.detail})" if self.detail else base
