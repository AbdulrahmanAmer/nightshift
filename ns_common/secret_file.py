"""C3: the API keys in a file Windows encrypts (DPAPI), for runs where Credential Manager is out of reach.

The nightly task may run in a logon session that has no access to the user's Credential Manager (an S4U
session: "run whether the user is logged on or not", no stored password). `keyring` then finds nothing and
stock footage is silently off. Setup therefore also writes the keys here, and the nightly path reads this
file when `keyring` has no answer.

The blob is protected with DPAPI in *machine* scope (`CRYPTPROTECT_LOCAL_MACHINE`): user-scope DPAPI needs
the user's master key, which an S4U session does not have either. The file lives under `config/`; the blob
is useless on another PC. Machine scope with a fixed entropy means any program of any user on this PC can
decrypt it, so the file's ACL (the current user and SYSTEM, nothing inherited: `restrict_to_user`) is what
protects it. Every write swaps in a NEW file, which takes the folder's inherited ACL, so `store_secret` and
`delete_secret` re-apply the restriction after each write, and so does a restore from a backup
(`ns_supervisor/backup.py`). Standard library only.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from ns_common.atomic import read_text_retry, write_text_atomic
from ns_common.paths import Paths

FILE_NAME = "secrets.dpapi"
ENTROPY = b"Nightshift/api-keys/v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x1
CRYPTPROTECT_LOCAL_MACHINE = 0x4

ProtectFn = Callable[[bytes], bytes]
LockFn = Callable[[Path], None]


def secret_path(paths: Paths) -> Path:
    """`config\\secrets.dpapi`."""
    return paths.config / FILE_NAME


def restrict_to_user(path: Path) -> None:
    """Limit a file's ACL to the current user (and SYSTEM) with icacls. Best effort; never raises.

    Besides the inherited entries it removes the Administrators group and OWNER RIGHTS: a file created in a
    folder that passes nothing down takes the creator's default ACL, which names both, and `/inheritance:r`
    leaves those (they are explicit). The user keeps full control as the owner, so nothing shuts us out.
    """
    user = os.environ.get("USERNAME")
    if not user or os.name != "nt":
        return
    try:
        subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{user}:F",
                "/grant:r",
                "*S-1-5-18:F",
                "/remove:g",
                "*S-1-5-32-544",
                "*S-1-3-4",
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return


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
        ok = fn(
            ctypes.byref(blob_in),
            "Nightshift",
            ctypes.byref(entropy),
            None,
            None,
            flags,
            ctypes.byref(blob_out),
        )
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
        data = json.loads(read_text_retry(secret_path(paths)))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def stored_names(paths: Paths) -> set[str]:
    """Names with a blob in `config/secrets.dpapi` (nothing is decrypted; empty when there is no file)."""
    return {name for name, blob in _read_all(paths).items() if blob}


def store_secret(
    paths: Paths,
    name: str,
    value: str,
    *,
    protect: ProtectFn = dpapi_protect,
    lock_down: LockFn = restrict_to_user,
) -> Path:
    """Add or replace one secret in `config\\secrets.dpapi` (written atomically, then limited to the user)."""
    data = _read_all(paths)
    data[name] = base64.b64encode(protect(value.encode("utf-8"))).decode("ascii")
    path = write_text_atomic(secret_path(paths), json.dumps(data, indent=2) + "\n")
    lock_down(path)  # the swap made a new file: it has the folder's inherited ACL until this
    return path


def delete_secret(paths: Paths, name: str, *, lock_down: LockFn = restrict_to_user) -> bool:
    """Remove one secret from `config\\secrets.dpapi`; True when it was there. No file: False."""
    data = _read_all(paths)
    if name not in data:
        return False
    del data[name]
    lock_down(write_text_atomic(secret_path(paths), json.dumps(data, indent=2) + "\n"))
    return True


def load_secret(paths: Paths, name: str, *, unprotect: ProtectFn = dpapi_unprotect) -> str | None:
    """One secret from the file; None when it is absent or cannot be decrypted. Never raises."""
    raw = _read_all(paths).get(name)
    if not raw:
        return None
    try:
        return unprotect(base64.b64decode(raw, validate=True)).decode("utf-8") or None
    except (OSError, ValueError, binascii.Error):
        return None
