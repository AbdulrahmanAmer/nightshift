"""The PC's memory beside the card for a fitted llama load (plan 2026-11-12 rule 3, GPU PC section 14).

Two numbers decide whether system memory can take what llama.cpp moves off the card:
- available physical memory: the experts fit moves live in PINNED host memory (`CUDA_Host`), which must be
  physical;
- commit headroom against the limit the page file CAN grow to. Measured: llama-server's private bytes stay
  14.4-16.7 GB in every setting and the PC's commit rose from 23.0 to 37.4-39.8 GB of a 41.4 GB CURRENT
  limit; a system-managed page file grows (the limit was 50.4 at 17:13). So the headroom is counted against
  the limit after growth, never today's limit alone, and a load the page file can absorb is not refused.

The growth bound per page file (`PagingFiles` in the registry, what Windows' own dialog writes):
`?:\\pagefile.sys` (every drive automatic) or `<drive>\\pagefile.sys 0 0` (system managed): up to
3 x RAM or 4 GiB, whichever is larger, and at most an eighth of the volume (Microsoft's documented
system-managed maximum, UNVERIFIED on Windows 11 26200); `<drive>\\pagefile.sys <min> <max>`: `max` MiB.
Either way no more than the page file now plus the volume's free space. Anything that does not read: None,
and a None never refuses (fail open: the after-ready check still catches a real spill).
"""

from __future__ import annotations

import ctypes
import shutil
import sys
from dataclasses import dataclass

MIB = 1024 * 1024
_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management"


@dataclass(frozen=True)
class Room:
    """MiB. None: not read."""

    avail_mb: int | None = None  # available physical memory
    commit_mb: int | None = None  # committed now
    limit_mb: int | None = None  # the commit limit now
    limit_max_mb: int | None = None  # the commit limit once the page file has grown as far as it may

    @property
    def commit_room_mb(self) -> int | None:
        """Commit headroom against the limit after growth (or today's, when growth was not read)."""
        top = self.limit_max_mb if self.limit_max_mb is not None else self.limit_mb
        if top is None or self.commit_mb is None:
            return None
        return max(0, top - self.commit_mb)

    def as_dict(self) -> dict[str, int]:
        out = {"ram_avail_mb": self.avail_mb, "commit_mb": self.commit_mb, "commit_limit_mb": self.limit_mb,
               "commit_limit_max_mb": self.limit_max_mb}  # fmt: skip
        return {k: v for k, v in out.items() if v is not None}


class _MemStatus(ctypes.Structure):
    _fields_ = [  # noqa: RUF012 (ctypes' own declaration)
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _status() -> tuple[int, int, int, int] | None:
    """(total phys, avail phys, commit limit, commit available) MiB from GlobalMemoryStatusEx."""
    if sys.platform != "win32":
        return None
    st = _MemStatus()
    st.dwLength = ctypes.sizeof(_MemStatus)
    try:
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):  # type: ignore[attr-defined]
            return None
    except (OSError, AttributeError):
        return None
    return (st.ullTotalPhys // MIB, st.ullAvailPhys // MIB, st.ullTotalPageFile // MIB,
            st.ullAvailPageFile // MIB)  # fmt: skip


def _paging_files() -> list[str] | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _KEY) as k:
            value, _ = winreg.QueryValueEx(k, "PagingFiles")
    except OSError:
        return None
    if isinstance(value, str):
        value = [value]
    return [v for v in value if isinstance(v, str) and v.strip()] if isinstance(value, list) else None


def growth_mb(entries: list[str], ram_mb: int, page_now_mb: int, system_drive: str,
              disk: object = shutil.disk_usage) -> int | None:  # fmt: skip
    """How much the page files together may still grow (MiB) beyond `page_now_mb` (their size now, all
    together: the commit limit minus RAM). None when an entry does not read. Pure but for `disk`."""
    top = 0
    for raw in entries:
        parts = raw.split()
        if not parts:
            continue
        path = parts[0]
        drive = system_drive if path.startswith("?") else path[:2]
        try:
            usage = disk(drive + "\\")  # type: ignore[operator]
        except OSError:
            return None
        if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit() and int(parts[2]) > 0:
            cap = int(parts[2])
        elif len(parts) == 1 or (len(parts) >= 3 and parts[1] == "0" and parts[2] == "0"):
            cap = min(max(3 * ram_mb, 4096), usage.total // MIB // 8)
        else:
            return None
        top += min(cap, page_now_mb + usage.free // MIB)
    return max(0, top - page_now_mb)


def read() -> Room:
    """The PC's memory now (MiB); fields that do not read are None."""
    st = _status()
    if st is None:
        return Room()
    total, avail, limit, commit_free = st
    commit = max(0, limit - commit_free)
    entries = _paging_files()
    grow = None
    if entries is not None:
        import os

        sysdrive = os.environ.get("SystemDrive", "C:")
        grow = growth_mb(entries, total, max(0, limit - total), sysdrive)
    return Room(avail, commit, limit, None if grow is None else limit + grow)
