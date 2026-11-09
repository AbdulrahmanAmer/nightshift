"""Dashboard error types (component C9)."""

from __future__ import annotations

from typing import Any

from ns_common.errors import NsError


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


def room_refusal(e: NsError) -> ApiError:
    """The API answer (507) for an NS-E104 raised before a job starts: nothing was queued or written (D-33).

    The message says what THIS job needs and what the drive has (``This needs about 100 MB (...); D: has 20 MB
    free``), the hint what to do about it; ``details`` carries the two numbers in bytes.
    """
    return ApiError(
        507, "NS-E104", f"{e.definition.message} {e.detail}".strip(), hint=e.definition.hint,
        details={"need_bytes": e.data.get("need_bytes"), "free_bytes": e.data.get("free_bytes")},
    )


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
