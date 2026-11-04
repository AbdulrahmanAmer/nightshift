"""Installer error helper (component C1).

``make_error`` builds an ``NsError`` for any code in ``contracts/errors.json``. NS-E111, NS-E112 and NS-W112
were once missing there (CR-C1-1); they are published now, so the installer's own pending table is gone.
"""

from __future__ import annotations

from typing import Any

from installer.stage2.runner import RunResult
from ns_common.avblock import looks_blocked
from ns_common.errors import ErrorDef, NsError, describe
from ns_common.uv import looks_full

UV_TAIL_LINES = 15


def make_error(code: str, detail: str = "", **data: Any) -> NsError:
    """Return an NsError for ``code`` (any code in errors.json)."""
    return NsError(code, detail, **data)


def describe_code(code: str) -> ErrorDef:
    """Return the message/hint for ``code`` from errors.json."""
    return describe(code)


def tail_lines(text: str, count: int = UV_TAIL_LINES) -> str:
    """The last ``count`` non-empty lines of a command's output."""
    return "\n".join([line.rstrip() for line in text.splitlines() if line.strip()][-count:])


def uv_error(what: str, res: RunResult) -> NsError:
    """The error for a failed uv call, carrying uv's last lines so the Failed screen shows the cause.

    A full disk is NS-E104 and a program Windows blocked is NS-E113; anything else stays NS-E105 (the code
    that used to be the only answer, shown as "site unreachable" with no reason).
    """
    tail = tail_lines(res.text)
    code = "NS-E105"
    if looks_full(tail):
        code = "NS-E104"
    elif looks_blocked(tail):
        code = "NS-E113"
    detail = f"{what} (exit {res.returncode})"
    return make_error(code, f"{detail}. uv said:\n{tail}" if tail else detail)
