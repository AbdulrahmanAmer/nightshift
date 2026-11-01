"""Installer error helper (component C1).

``make_error`` builds an ``NsError`` for any code in ``contracts/errors.json``. NS-E111, NS-E112 and NS-W112
were once missing there (CR-C1-1); they are published now, so the installer's own pending table is gone.
"""

from __future__ import annotations

from typing import Any

from ns_common.errors import ErrorDef, NsError, describe


def make_error(code: str, detail: str = "", **data: Any) -> NsError:
    """Return an NsError for ``code`` (any code in errors.json)."""
    return NsError(code, detail, **data)


def describe_code(code: str) -> ErrorDef:
    """Return the message/hint for ``code`` from errors.json."""
    return describe(code)
