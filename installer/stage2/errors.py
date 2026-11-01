"""Installer error helper (component C1).

``make_error`` builds an ``NsError`` for any code in ``contracts/errors.json``. Three codes the installer
needs are still missing there (change request CR-C1-1); for those it builds an ``InstallerError`` that
carries the same message and hint, so callers can catch ``NsError`` either way.
"""

from __future__ import annotations

from typing import Any

from ns_common.errors import ErrorDef, NsError, load_errors

_PENDING: dict[str, ErrorDef] = {
    "NS-E111": ErrorDef(
        "NS-E111",
        "winget (Windows App Installer) is missing.",
        "Install 'App Installer' from the Microsoft Store (or use the direct installer links shown), "
        "then run setup again.",
        "error",
    ),
    "NS-E112": ErrorDef(
        "NS-E112",
        "faster-whisper cannot load on the GPU (cuBLAS 12 / cuDNN 9 DLLs not found).",
        "In the studio venv run: uv pip install nvidia-cublas-cu12 nvidia-cudnn-cu12, then re-run "
        "'ns verify --quick'.",
        "error",
    ),
    "NS-W112": ErrorDef(
        "NS-W112",
        "Windows long paths are disabled.",
        "Run setup once as administrator to enable them, or keep NIGHTSHIFT_HOME on a short path such as "
        "D:\\Nightshift.",
        "warning",
    ),
}


class InstallerError(NsError):
    """NsError stand-in for codes that are not in errors.json yet (same attributes, same message format)."""

    def __init__(self, code: str, detail: str = "", **data: Any) -> None:
        self.code = code
        self.detail = detail
        self.data = data
        self.definition = _PENDING[code]
        Exception.__init__(self, str(self))


def make_error(code: str, detail: str = "", **data: Any) -> NsError:
    """Return an NsError for ``code`` (from errors.json, or the pending table when not yet published)."""
    if code in load_errors():
        return NsError(code, detail, **data)
    return InstallerError(code, detail, **data)


def describe_code(code: str) -> ErrorDef:
    """Return the message/hint for ``code`` from errors.json or the pending table."""
    known = load_errors()
    return known[code] if code in known else _PENDING[code]
