"""C3: recognise "antivirus or policy blocked this file" (standard library only).

Windows reports a quarantined download as ``WinError 225`` ("Operation did not complete successfully because
the file contains a virus or potentially unwanted software"). A scanner that removes the file after it was
written gives a file that is simply gone, and one that removes a program from an unpacked archive leaves the
archive's other files in place. All of them end as ``NS-E113`` with the allow-list hint, not as a download or
a hash problem.
"""

from __future__ import annotations

#: Windows error "the file contains a virus or potentially unwanted software".
WINERROR_VIRUS = 225
BLOCKED_HINTS = (
    "virus", "os error 225", "winerror 225", "[winerror 225]", "blocked by your organization",
    "potentially unwanted",
)


def looks_blocked(text: str) -> bool:
    """True when command or exception text says a scanner or policy blocked a file."""
    low = text.lower()
    return any(hint in low for hint in BLOCKED_HINTS)


def is_blocked_error(error: BaseException) -> bool:
    """True for an ``OSError`` with WinError 225 (or text that says so)."""
    if getattr(error, "winerror", None) == WINERROR_VIRUS:
        return True
    return isinstance(error, OSError) and looks_blocked(str(error))
