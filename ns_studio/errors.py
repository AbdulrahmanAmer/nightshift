"""Studio-level coded conditions (component C7).

`StudioError` carries one of the studio codes NS-E320 to NS-E326 and takes message and hint from
`contracts/errors.json` through `ns_common.errors.describe` (CR-C7-1 is applied).
"""

from __future__ import annotations

from ns_common.errors import describe


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
