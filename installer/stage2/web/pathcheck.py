"""Shape rules for the install folder, shared by the Choices validation and the disk check.

Standard library only. The same rules are mirrored in the window (``installer/ui/src/format.ts``,
``homePathProblem``) for instant feedback; the server's answer stays authoritative. A path may hold spaces and
non-ASCII letters. The folder is never created here: setup creates it when Install starts.
"""

from __future__ import annotations

import re

MAX_PATH_LEN = 120
DRIVE_PATH_RE = re.compile(r"^[A-Za-z]:\\")
ILLEGAL_CHARS = '<>:"|?*%'
RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
)
EXAMPLE = "D:\\Nightshift"


def home_problem(path: str, max_len: int = MAX_PATH_LEN) -> str | None:
    """Why ``path`` cannot be the install folder, in plain words; None when its shape is fine."""
    p = (path or "").strip()
    if not p:
        return f"Type a folder such as {EXAMPLE}, or press Browse"
    if p.startswith(("\\\\", "//")):
        return "Network folders are not supported. Pick a folder on a drive of this PC"
    if not DRIVE_PATH_RE.match(p):
        return f"Use a full path such as {EXAMPLE}"
    rest = p[3:]
    if any(c in ILLEGAL_CHARS or ord(c) < 32 for c in rest):
        return 'That path has a character Windows does not allow: < > : " | ? * %'
    if len(p) > max_len:
        return f"Use a path of {max_len} characters or fewer"
    parts = rest.split("\\")
    if parts and parts[-1] == "":
        parts = parts[:-1]                          # one trailing backslash is fine
    for name in parts:
        if name == "":
            return "Use single backslashes in the path"
        if name in (".", ".."):
            return "Use a plain folder path without . or .."
        if name != name.rstrip(" .") or name.split(".")[0].upper() in RESERVED_NAMES:
            return f'Windows does not allow the folder name "{name}"'
    return None
