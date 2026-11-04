"""C3: recognise "antivirus or policy blocked this file" (standard library only).

Windows reports a quarantined download as ``WinError 225`` ("Operation did not complete successfully because
the file contains a virus or potentially unwanted software"). A scanner that removes the file after it was
written gives a file that is simply gone, and one that removes a program from an unpacked archive leaves the
archive's other files in place. A program that Smart App Control or an Application Control policy refuses to
start gives ``WinError 4551`` (or 1260). All of them end as ``NS-E113`` with the allow-list hint, not as a
download, a hash or a "service failed" problem.
"""

from __future__ import annotations

import ntpath
from pathlib import Path

#: Windows error "the file contains a virus or potentially unwanted software".
WINERROR_VIRUS = 225
#: 225 virus found, 226 file deleted as a virus, 1260 blocked by group policy / WDAC, 4551 blocked by an
#: Application Control policy (Smart App Control, on by default in evaluation mode on a clean Windows 11).
BLOCK_WINERRORS = frozenset({225, 226, 1260, 4551})
BLOCKED_HINTS = (
    "virus", "blocked by your organization", "potentially unwanted", "smart app control",
    "application control policy", "blocked by group policy", "blocked this file",
    *(f"os error {n}" for n in sorted(BLOCK_WINERRORS)),
    *(f"winerror {n}" for n in sorted(BLOCK_WINERRORS)),
)


def looks_blocked(text: str) -> bool:
    """True when command or exception text says a scanner or policy blocked a file."""
    low = text.lower()
    return any(hint in low for hint in BLOCKED_HINTS)


def is_blocked_error(error: BaseException) -> bool:
    """True for an ``OSError`` with WinError 225, 226, 1260 or 4551 (or text that says so)."""
    if getattr(error, "winerror", None) in BLOCK_WINERRORS:
        return True
    return isinstance(error, OSError) and looks_blocked(str(error))


def blocked_hint(program: str | Path) -> str:
    """What to tell the user when Windows refused to start ``program`` (names the file, where to allow it)."""
    name = ntpath.basename(str(program)) or str(program)
    return (
        f"Windows blocked {name} when it was started (Smart App Control or antivirus). "
        "Allow it in Windows Security > App & browser control, then press Retry"
    )
