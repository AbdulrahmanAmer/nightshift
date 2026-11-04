"""C3: rules for the install folder name that the console wizard and the setup window share (stdlib only)."""

from __future__ import annotations

NON_ASCII_PROBLEM = (
    "Use a folder name with plain English letters, digits, spaces, - and _ "
    "(the model programs cannot open other paths)"
)


def ascii_problem(path: str) -> str | None:
    """``NON_ASCII_PROBLEM`` when ``path`` has a character outside printable ASCII (space to ~), else None."""
    return NON_ASCII_PROBLEM if any(not 32 <= ord(c) <= 126 for c in path) else None
