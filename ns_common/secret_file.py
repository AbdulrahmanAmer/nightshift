"""C3: the API keys in a file Windows encrypts (DPAPI), for runs where Credential Manager is out of reach.

The nightly task may run in a logon session that has no access to the user's Credential Manager (an S4U
session: "run whether the user is logged on or not", no stored password). `keyring` then finds nothing and
stock footage is silently off. Setup therefore also writes the keys here, and the nightly path reads this
file when `keyring` has no answer.

The blob is protected with DPAPI in *machine* scope (`CRYPTPROTECT_LOCAL_MACHINE`): user-scope DPAPI needs
the user's master key, which an S4U session does not have either. The file lives under `config/`; the blob
is useless on another PC. Machine scope with a fixed entropy means any program of any user on this PC can
decrypt it, so the file's ACL (the account this program runs as and SYSTEM, nothing inherited:
`restrict_to_user`) is its ONLY protection, and a lock that does not hold is an error (`NS-E120`), never a
silent shrug.

Every write puts the new content in a temporary file beside the old one and swaps it in. A new file takes the
folder's ACL, so `store_secret` and `delete_secret` lock the temporary file while it is still EMPTY, write
the keys into it, swap it in (a rename keeps the file's ACL) and check the lock on the final name once more.
A lock that fails leaves the old file as it was and no temporary file. The temporary files a crash left in
`config/` are swept by the next write (and by the engine at start). A restore from a backup does the same for
the restored file and for the copy it keeps of the old one (`ns_supervisor/backup.py`). Standard library
only.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ns_common.atomic import read_text_retry, sweep_stale_tmp, write_text_atomic
from ns_common.errors import NsError
from ns_common.paths import Paths

FILE_NAME = "secrets.dpapi"
ENTROPY = b"Nightshift/api-keys/v1"
CRYPTPROTECT_UI_FORBIDDEN = 0x1
CRYPTPROTECT_LOCAL_MACHINE = 0x4

SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
OWNER_RIGHTS_SID = "S-1-3-4"
#: A SID in its string form: `S-1-<authority>-<sub>...` with at least one sub-authority.
SID_RE = re.compile(r"^S-1-\d+(?:-\d+)+$", re.IGNORECASE)
ICACLS_TIMEOUT_S = 30.0
_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_ACE_RE = re.compile(r"\(([^()]*)\)")

ProtectFn = Callable[[bytes], bytes]
LockFn = Callable[[Path], None]
#: Runs icacls with these arguments: (exit code, what it printed). May raise OSError / SubprocessError.
IcaclsFn = Callable[[list[str]], tuple[int, str]]
SidFn = Callable[[], str | None]


def secret_path(paths: Paths) -> Path:
    """`config\\secrets.dpapi`."""
    return paths.config / FILE_NAME


class SecretFileLockError(NsError, PermissionError):
    """`NS-E120`: a file that holds the keys could not be limited to this account and SYSTEM.

    It is a `PermissionError` (an `OSError`) as well as an `NsError`, like `FileInUseError`: the callers that
    catch `OSError` around a key write (the installer's store, Settings > Keys, a restore's roll back) keep
    catching it, and say so.
    """

    def __init__(self, path: Path, why: str) -> None:
        self.why = why
        NsError.__init__(self, "NS-E120", f"{path}: {why}")


def _lock_failed(path: Path, why: str) -> PermissionError:
    try:
        return SecretFileLockError(path, why)
    except (OSError, KeyError, ValueError):  # contracts/errors.json unreadable: say it plainly instead
        return PermissionError(f"{path} could not be limited to this account: {why}")


def _system_tool(name: str) -> str:
    """`%SystemRoot%\\System32\\<name>` when it is there (no look-up in the current folder), else `name`."""
    full = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / name
    return str(full) if full.is_file() else name


def _run_icacls(args: list[str]) -> tuple[int, str]:
    r = subprocess.run(
        [_system_tool("icacls.exe"), *args],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=ICACLS_TIMEOUT_S,
        check=False,
        creationflags=_NO_WINDOW,
    )
    return r.returncode, f"{r.stdout}\n{r.stderr}".strip()


class _Win32:
    """The few advapi32/kernel32 calls that read SIDs (own library objects: no shared `argtypes`)."""

    def __init__(self) -> None:
        from ctypes import wintypes

        self.advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k, a = self.kernel32, self.advapi32
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.LocalFree.argtypes = [ctypes.c_void_p]
        k.LocalFree.restype = ctypes.c_void_p
        a.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
        a.OpenProcessToken.restype = wintypes.BOOL
        a.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        a.GetTokenInformation.restype = wintypes.BOOL
        a.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
        a.ConvertSidToStringSidW.restype = wintypes.BOOL
        a.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
        a.ConvertStringSidToSidW.restype = wintypes.BOOL

    def sid_text(self, psid: Any) -> str | None:
        text = ctypes.c_void_p()
        if not psid or not self.advapi32.ConvertSidToStringSidW(psid, ctypes.byref(text)):
            return None
        try:
            return ctypes.wstring_at(text.value) if text.value else None
        finally:
            self.kernel32.LocalFree(text.value)

    def token_user(self) -> str | None:
        from ctypes import wintypes

        token_query, token_user = 0x0008, 1
        token = wintypes.HANDLE()
        if not self.advapi32.OpenProcessToken(
            self.kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
        ):
            return None
        try:
            need = wintypes.DWORD(0)
            self.advapi32.GetTokenInformation(token, token_user, None, 0, ctypes.byref(need))
            if not need.value:
                return None
            buf = ctypes.create_string_buffer(need.value)
            if not self.advapi32.GetTokenInformation(token, token_user, buf, need, ctypes.byref(need)):
                return None
            # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first field is the PSID
            return self.sid_text(ctypes.c_void_p.from_buffer(buf).value)
        finally:
            self.kernel32.CloseHandle(token)

    def expand(self, alias: str) -> str | None:
        psid = ctypes.c_void_p()
        if not self.advapi32.ConvertStringSidToSidW(alias, ctypes.byref(psid)):
            return None
        try:
            return self.sid_text(psid.value)
        finally:
            self.kernel32.LocalFree(psid.value)


def _token_user_sid() -> str | None:
    """The user SID of this process's token (`OpenProcessToken` + `GetTokenInformation(TokenUser)`)."""
    return _Win32().token_user() if sys.platform == "win32" else None


def _whoami_sid() -> str | None:
    """The SID `whoami /user` prints for the account this process runs as."""
    if sys.platform != "win32":
        return None
    r = subprocess.run(
        [_system_tool("whoami.exe"), "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=ICACLS_TIMEOUT_S,
        check=False,
        creationflags=_NO_WINDOW,
    )
    m = re.search(r"S-1-\d+(?:-\d+)+", r.stdout) if r.returncode == 0 else None
    return m.group(0) if m else None


def current_user_sid() -> str | None:
    """The SID (`S-1-5-21-...`) of the account this process runs as: from its token, else from `whoami`.

    None when neither answers. The account is named by its SID because a name may not map back to it
    (`%USERNAME%` of an Entra ID sign-in is `AzureAD\\<name>`; a process may have no `%USERNAME%` at all).
    """
    for read in (_token_user_sid, _whoami_sid):
        try:
            sid = read()
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            sid = None
        if sid and SID_RE.fullmatch(sid.strip()):
            return sid.strip().upper()
    return None


def _sid_of(name: str) -> str:
    """The SID of an SDDL entry: `S-1-...` as it is, `SY` as SYSTEM, any other alias as Windows expands it."""
    text = name.strip().upper()
    if text.startswith("S-1-"):
        return text
    if text == "SY":
        return SYSTEM_SID
    try:
        expanded = _Win32().expand(text) if sys.platform == "win32" else None
    except (OSError, ValueError, AttributeError):
        expanded = None
    return (expanded or text).upper()


def _dacl(saved: str) -> tuple[str, list[tuple[str, str, str]]] | None:
    """(DACL flags, [(entry, entry flags, SID)]) of what `icacls /save` wrote; None when it holds no DACL."""
    line = next((ln.strip() for ln in saved.splitlines() if ln.strip().startswith("D:")), None)
    if line is None:
        return None
    entries: list[tuple[str, str, str]] = []
    for ace in _ACE_RE.findall(line):
        parts = ace.split(";")
        if len(parts) < 6:
            entries.append((ace, "", ace))  # not an entry we can read: it names nobody we allow
            continue
        entries.append((ace, parts[1].upper(), _sid_of(parts[5])))
    return line[2:].split("(", 1)[0].upper(), entries


def _pairs(flags: str) -> set[str]:
    """SDDL entry flags are two letters each (`OICIID`): {"OI", "CI", "ID"}."""
    return {flags[i : i + 2] for i in range(0, len(flags), 2)}


def _others(saved: str, sid: str) -> list[str]:
    """The SIDs of entries other than `sid` and SYSTEM (the ones `icacls /remove` can take out)."""
    found = _dacl(saved)
    allowed = {sid.upper(), SYSTEM_SID}
    out: list[str] = []
    for _ace, _flags, who in found[1] if found else []:
        if who not in allowed and SID_RE.fullmatch(who) and who not in out:
            out.append(who)
    return out


def acl_problem(saved: str, sid: str) -> str | None:
    """What keeps the ACL in `saved` (what `icacls <file> /save` wrote: SDDL) from being `sid` + SYSTEM only.

    None when the DACL is protected (nothing inherited, now or later), every entry names `sid` or SYSTEM and
    `sid` has its entry (so the account still reads its keys).
    """
    found = _dacl(saved)
    if found is None:
        return "icacls showed no permission list (DACL) for the file"
    flags, entries = found
    if "NO_ACCESS_CONTROL" in flags:
        return "the file has no permission list at all, so every account may open it"
    if "P" not in flags:
        return "the file still takes its folder's permissions (inheritance is on)"
    allowed = {sid.upper(), SYSTEM_SID}
    inherited = [ace for ace, ace_flags, _ in entries if "ID" in _pairs(ace_flags)]
    if inherited:
        return "entries are still inherited from the folder: " + " ".join(f"({a})" for a in inherited)
    others = [who for _, _, who in entries if who not in allowed]
    if others:
        return "other accounts are still on the file: " + ", ".join(dict.fromkeys(others))
    if sid.upper() not in {who for _, _, who in entries}:
        return f"the entry for this account ({sid}) is missing"
    return None


def _icacls(icacls: IcaclsFn, path: Path, args: list[str], what: str) -> None:
    try:
        code, out = icacls(args)
    except (OSError, subprocess.SubprocessError) as e:
        raise _lock_failed(path, f"icacls could not {what} ({type(e).__name__}: {e})") from e
    if code != 0:
        said = " ".join(out.split())[:300]
        raise _lock_failed(path, f"icacls could not {what} (exit {code}: {said})")


def _saved_acl(path: Path, icacls: IcaclsFn) -> str:
    """The file's ACL as SDDL, read back with `icacls <file> /save` (every entry by its SID)."""
    try:
        folder = Path(tempfile.mkdtemp(prefix="ns-acl-"))
    except OSError as e:
        raise _lock_failed(path, f"no temporary folder to read the permissions back into ({e})") from e
    try:
        out = folder / "acl.txt"
        _icacls(icacls, path, [str(path), "/save", str(out)], "read the permissions back")
        try:
            raw = out.read_bytes()
        except OSError as e:
            raise _lock_failed(path, f"icacls /save left nothing to read back ({e})") from e
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return raw.decode("utf-16-le", errors="replace").lstrip("\ufeff")


def restrict_to_user(path: Path, *, icacls: IcaclsFn | None = None, user_sid: SidFn | None = None) -> None:
    """Limit `path`'s ACL to the account this process runs as and SYSTEM, then read it back.

    The account is granted by its SID (`current_user_sid`), never by `%USERNAME%`. Inheritance is cut and the
    Administrators group and OWNER RIGHTS are removed: a file created in a folder that passes nothing down
    takes the token's default ACL, which names both (and on a signed-in desktop the logon session too, which
    the read-back finds and removes by its SID). The account keeps full control, so nothing shuts us out.

    Raises `SecretFileLockError` (`NS-E120`, also a `PermissionError`) when no SID can be read, when icacls
    cannot run, times out or exits non-zero, or when the ACL read back holds anything but that SID and
    SYSTEM. Off Windows (no icacls, no such ACL) it does nothing.
    """
    if icacls is None:
        if os.name != "nt":
            return
        icacls = _run_icacls
    sid = (user_sid or current_user_sid)()
    if not sid or not SID_RE.fullmatch(sid.strip()):
        raise _lock_failed(path, f"the security ID (SID) of this account could not be read (got {sid!r})")
    sid = sid.strip().upper()
    _icacls(
        icacls,
        path,
        [
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"*{sid}:F",
            "/grant:r",
            f"*{SYSTEM_SID}:F",
            "/remove:g",
            f"*{ADMINISTRATORS_SID}",
            f"*{OWNER_RIGHTS_SID}",
        ],
        "limit the permissions to this account",
    )
    saved = _saved_acl(path, icacls)
    others = _others(saved, sid)
    if others:
        _icacls(icacls, path, [str(path), "/remove", *(f"*{s}" for s in others)], "remove the other entries")
        saved = _saved_acl(path, icacls)
    problem = acl_problem(saved, sid)
    if problem is not None:
        raise _lock_failed(path, problem)


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
    """Add or replace one secret in `config\\secrets.dpapi`.

    The new file is locked (`lock_down`, default `restrict_to_user`) while it is still an empty temporary
    file, then the keys go in and it is swapped in whole; the final name is checked once more. A lock that
    fails on the temporary file raises (`NS-E120`) and leaves the old file as it was; the check on the final
    name failing raises too, saying that the new file is in place.
    """
    sweep_stale_tmp(paths.config)  # a crash between write and swap left every key blob in a ~ns*.tmp
    data = _read_all(paths)
    data[name] = base64.b64encode(protect(value.encode("utf-8"))).decode("ascii")
    return _write(paths, data, lock_down)


def delete_secret(paths: Paths, name: str, *, lock_down: LockFn = restrict_to_user) -> bool:
    """Remove one secret from `config\\secrets.dpapi`; True when it was there. No file: False.

    Written like `store_secret`: a lock that fails raises (`NS-E120`) and the file keeps the key.
    """
    sweep_stale_tmp(paths.config)
    data = _read_all(paths)
    if name not in data:
        return False
    del data[name]
    _write(paths, data, lock_down)
    return True


def _write(paths: Paths, data: dict[str, str], lock_down: LockFn) -> Path:
    path = write_text_atomic(secret_path(paths), json.dumps(data, indent=2) + "\n", prepare=lock_down)
    try:
        lock_down(path)  # a rename keeps the temp file's ACL: this checks that the final name has it
    except SecretFileLockError as e:
        raise _lock_failed(path, f"the new file is in place, but checking its lock failed: {e.why}") from e
    return path


def relock_existing(paths: Paths, *, lock_down: LockFn = restrict_to_user) -> list[tuple[Path, str]]:
    """Lock the key files that already exist; returns `(path, why)` for each one whose lock did not hold.

    A `config/secrets.dpapi` written by 0.1.0 to 0.1.14 (icacls could not map `%USERNAME%`, so it exited 1332
    and the file kept `Users` and `Authenticated Users`) stays readable by every local account until the next
    key save. So do the `data/backup/pre-restore-*/config/secrets.dpapi` copies a restore made with
    `shutil.copy2`. The engine calls this at start; it never raises (the caller logs each `NS-E120`).
    """
    found: list[Path] = []
    live = secret_path(paths)
    if live.is_file():
        found.append(live)
    try:
        found += sorted(paths.backup_dir.glob(f"pre-restore-*/config/{FILE_NAME}"))
    except OSError:
        pass
    problems: list[tuple[Path, str]] = []
    for path in found:
        try:
            lock_down(path)
        except SecretFileLockError as e:
            problems.append((path, f"{e.code}: {e.why}"))
        except OSError as e:
            problems.append((path, f"NS-E120: {e}"))
    return problems


def load_secret(paths: Paths, name: str, *, unprotect: ProtectFn = dpapi_unprotect) -> str | None:
    """One secret from the file; None when it is absent or cannot be decrypted. Never raises."""
    raw = _read_all(paths).get(name)
    if not raw:
        return None
    try:
        return unprotect(base64.b64decode(raw, validate=True)).decode("utf-8") or None
    except (OSError, ValueError, binascii.Error):
        return None
