"""Dashboard error types (component C9)."""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """An error the API reports as `{"error": {code, message, hint, details?}}` with an HTTP status."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        hint: str | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details

    def to_body(self) -> dict[str, Any]:
        """JSON body for the response."""
        err: dict[str, Any] = {"code": self.code, "message": self.message, "hint": self.hint}
        if self.details is not None:
            err["details"] = self.details
        return {"error": err}


class AdapterUnavailable(Exception):
    """A collaborator owned by another component (C2, C7, C8) is not installed yet."""


class SupervisorUnavailable(ApiError):
    """The supervisor does not answer on its port."""

    def __init__(self, detail: str = "") -> None:
        msg = "The Nightshift engine is not running."
        super().__init__(
            503,
            "engine_offline",
            f"{msg} {detail}".strip(),
            hint="Open Nightshift Dashboard from the desktop shortcut to start the engine.",
        )
