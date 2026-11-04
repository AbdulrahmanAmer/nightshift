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


SMART_APP_CONTROL_HINTS = ("smart app control", "application control policy")
SMART_APP_CONTROL_FIX = (
    "Smart App Control blocks programs it does not know. Turn it off in Windows Security > App & browser "
    "control > Smart App Control settings (this cannot be turned back on without reinstalling Windows), "
    "then press Retry"
)


def is_smart_app_control(error: BaseException | str | None) -> bool:
    """True when the error is Smart App Control's block (WinError 4551, or text that names it)."""
    if error is None:
        return False
    if getattr(error, "winerror", None) == 4551:
        return True
    low = str(error).lower()
    return "winerror 4551" in low or "os error 4551" in low or any(h in low for h in SMART_APP_CONTROL_HINTS)


def blocked_hint(program: str | Path, error: BaseException | str | None = None) -> str:
    """What to tell the user when Windows refused to start ``program`` (names the file, what to do).

    Smart App Control (WinError 4551) cannot allow a single file, so the advice there is to turn it off;
    for an antivirus or a policy the file is allowed in Windows Security.
    """
    name = ntpath.basename(str(program)) or str(program)
    if is_smart_app_control(error):
        return f"Windows blocked {name} when it was started. {SMART_APP_CONTROL_FIX}"
    return (
        f"Windows blocked {name} when it was started (antivirus or a policy). "
        "Allow the file in Windows Security > Protection history, then press Retry"
    )
