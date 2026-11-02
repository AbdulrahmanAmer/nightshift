"""C3: the API keys in a file Windows encrypts (DPAPI), for runs where Credential Manager is out of reach.

The nightly task may run in a logon session that has no access to the user's Credential Manager (an S4U
session: "run whether the user is logged on or not", no stored password). `keyring` then finds nothing, stock
footage silently turned off. Setup therefore also writes the keys here, and the nightly path reads this file
when `keyring` has no answer.

The blob is protected with DPAPI in *machine* scope (`CRYPTPROTECT_LOCAL_MACHINE`): user-scope DPAPI needs the
user's master key, which an S4U session does not have either. The file lives under `config/` and the
installer restricts its ACL to the current user; the blob alone is useless on another PC. Standard library only.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import json
import sys
from collections.abc import Callable
from pathlib import Path

from ns_common.atomic import write_text_atomic
from ns_common.paths import Paths

FILE_NAME = "secrets.dpapi"
ENTROPY = b"Nightshift/api-keys/v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x1
CRYPTPROTECT_LOCAL_MACHINE = 0x4

ProtectFn = Callable[[bytes], bytes]


def secret_path(paths: Paths) -> Path:
    """`config\\secrets.dpapi`."""
    return paths.config / FILE_NAME


def _crypt(data: bytes, *, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise OSError("DPAPI is only available on Windows")
    from ctypes import wintypes

    class DataBlob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.c_void_p)]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    in_buf = ctypes.create_string_buffer(data, len(data))
    ent_buf = ctypes.create_string_buffer(ENTROPY, len(ENTROPY))
    blob_in = DataBlob(len(data), ctypes.cast(in_buf, ctypes.c_void_p).value)
    entropy = DataBlob(len(ENTROPY), ctypes.cast(ent_buf, ctypes.c_void_p).value)
    blob_out = DataBlob()
    flags = CRYPTPROTECT_UI_FORBIDDEN | (CRYPTPROTECT_LOCAL_MACHINE if protect else 0)
    if protect:
        ok = fn(ctypes.byref(blob_in), "Nightshift", ctypes.byref(entropy), None, None, flags,
                ctypes.byref(blob_out))
    else:
        ok = fn(ctypes.byref(blob_in), None, ctypes.byref(entropy), None, None, flags, ctypes.byref(blob_out))
    if not ok:
        raise OSError(f"DPAPI call failed (error {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def dpapi_protect(data: bytes) -> bytes:
    """Encrypt with DPAPI (machine scope, fixed entropy). Raises `OSError` where DPAPI is unavailable."""
    return _crypt(data, protect=True)


def dpapi_unprotect(blob: bytes) -> bytes:
    """Decrypt a blob made by `dpapi_protect`. Raises `OSError` on a different PC or a damaged blob."""
    return _crypt(blob, protect=False)


def _read_all(paths: Paths) -> dict[str, str]:
    try:
        data = json.loads(secret_path(paths).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def store_secret(paths: Paths, name: str, value: str, *, protect: ProtectFn = dpapi_protect) -> Path:
    """Add or replace one secret in `config\\secrets.dpapi` (written atomically)."""
    data = _read_all(paths)
    data[name] = base64.b64encode(protect(value.encode("utf-8"))).decode("ascii")
    return write_text_atomic(secret_path(paths), json.dumps(data, indent=2) + "\n")


def load_secret(paths: Paths, name: str, *, unprotect: ProtectFn = dpapi_unprotect) -> str | None:
    """One secret from the file; None when it is absent or cannot be decrypted. Never raises."""
    raw = _read_all(paths).get(name)
    if not raw:
        return None
    try:
        return unprotect(base64.b64decode(raw, validate=True)).decode("utf-8") or None
    except (OSError, ValueError, binascii.Error):
        return None
