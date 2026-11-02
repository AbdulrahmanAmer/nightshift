"""C2 process helpers: start with logs to JSONL, terminate, Windows Job Object, VRAM probes.

The Job Object (`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`) makes every child die with the supervisor, so a
crash, log-off or shutdown never leaves model processes holding VRAM (spec 6.1, risk register 8).
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_supervisor.services import ServiceDef

#: Tells `ns update` it runs below the engine, so it must detach from the engine's process tree.
UNDER_ENGINE_ENV = "NIGHTSHIFT_UNDER_ENGINE"

_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------------------------------------
# Windows Job Object (ctypes). Other platforms get a no-op object so imports and tests stay portable.
# ---------------------------------------------------------------------------------------------
if sys.platform == "win32":
    from ctypes import wintypes

    _KILL_ON_JOB_CLOSE = 0x2000
    _BREAKAWAY_OK = 0x0800          # a child created with CREATE_BREAKAWAY_FROM_JOB leaves the job (ns update)
    _EXTENDED_LIMIT_INFO_CLASS = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    class _BasicLimit(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _ExtendedLimit(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimit),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _k32_lib: ctypes.WinDLL | None = None

    def _k32() -> ctypes.WinDLL:
        global _k32_lib
        if _k32_lib is None:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CreateJobObjectW.restype = wintypes.HANDLE
            k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            k.SetInformationJobObject.restype = wintypes.BOOL
            k.SetInformationJobObject.argtypes = [
                wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
            ]  # fmt: skip
            k.AssignProcessToJobObject.restype = wintypes.BOOL
            k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            k.OpenProcess.restype = wintypes.HANDLE
            k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k.CloseHandle.restype = wintypes.BOOL
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            _k32_lib = k
        return _k32_lib

    def _create_job() -> int | None:
        k = _k32()
        handle = k.CreateJobObjectW(None, None)
        if not handle:
            return None
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE | _BREAKAWAY_OK
        ok = k.SetInformationJobObject(
            handle, _EXTENDED_LIMIT_INFO_CLASS, ctypes.byref(info), ctypes.sizeof(info)
        )
        if not ok:
            k.CloseHandle(handle)
            return None
        return int(handle)

    def _assign_to_job(job: int, pid: int) -> bool:
        k = _k32()
        proc = k.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not proc:
            return False
        try:
            return bool(k.AssignProcessToJobObject(job, proc))
        finally:
            k.CloseHandle(proc)

    def _close_handle(handle: int) -> None:
        _k32().CloseHandle(handle)

else:

    def _create_job() -> int | None:
        return None

    def _assign_to_job(job: int, pid: int) -> bool:
        return False

    def _close_handle(handle: int) -> None:
        return None


class JobObject:
    """A Windows Job Object that kills all assigned processes when it is closed (or its owner exits).

    On other platforms (or if creation fails) `available` is False and `assign` returns False.
    """

    def __init__(self) -> None:
        self._handle: int | None = _create_job()

    @property
    def available(self) -> bool:
        """True while the job handle is open."""
        return self._handle is not None

    def assign(self, pid: int) -> bool:
        """Put process `pid` into the job; False when unavailable or the OS refused."""
        if self._handle is None:
            return False
        return _assign_to_job(self._handle, pid)

    def close(self) -> None:
        """Close the job handle: every assigned process is terminated by the OS. Idempotent."""
        handle, self._handle = self._handle, None
        if handle is not None:
            _close_handle(handle)


# ---------------------------------------------------------------------------------------------
# Child processes
# ---------------------------------------------------------------------------------------------
@dataclass
class Proc:
    """A running service process and the threads that copy its output to the JSONL log."""

    service: str
    popen: subprocess.Popen[bytes]
    log_path: Path
    started_at: float
    pumps: list[threading.Thread] = field(default_factory=list)

    @property
    def pid(self) -> int:
        """OS process id."""
        return self.popen.pid

    def alive(self) -> bool:
        """True while the process has not exited."""
        return self.popen.poll() is None

    def returncode(self) -> int | None:
        """Exit code, or None while running."""
        return self.popen.poll()


def _pump(stream: IO[bytes], log: NsLogger, event: str, lock: threading.Lock) -> None:
    """Copy a child's output line by line into the JSONL log until EOF."""
    try:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line:
                with lock:
                    log.info(event, line)
    except (OSError, ValueError):
        pass
    finally:
        with contextlib.suppress(OSError):
            stream.close()


def start(service: ServiceDef, logs_dir: Path, *, job: JobObject | None = None) -> Proc:
    """Launch `service`; stdout/stderr go to `logs\\<service>-YYYYMMDD.jsonl` as `stdout`/`stderr` events.

    Raises `NS-E202` when the executable cannot be started.
    """
    log = NsLogger(service.name, logs_dir, echo=False)
    env = {**os.environ, **service.env, UNDER_ENGINE_ENV: "1"}
    try:
        popen = subprocess.Popen(
            service.cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            creationflags=_NO_WINDOW,
        )
    except OSError as e:
        raise NsError("NS-E202", f"cannot start {service.name}: {e}", cmd=service.cmd) from e
    if job is not None:
        job.assign(popen.pid)
    lock = threading.Lock()
    proc = Proc(
        service=service.name,
        popen=popen,
        log_path=log.file_for(log.clock()),
        started_at=time.monotonic(),
    )
    for stream, event in ((popen.stdout, "stdout"), (popen.stderr, "stderr")):
        if stream is not None:
            t = threading.Thread(target=_pump, args=(stream, log, event, lock), daemon=True)
            t.start()
            proc.pumps.append(t)
    return proc


def kill_tree(pid: int) -> None:
    """Best-effort hard kill of `pid` and its descendants (Windows `taskkill /T /F`)."""
    if sys.platform != "win32":
        return
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=20,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def soft_stop(pid: int) -> bool:
    """Ask `pid` and its descendants to close politely (`taskkill /T` without `/F`).

    True when Windows accepted the request, so waiting for the exit makes sense. Console services without a
    window refuse it (taskkill says they can only be terminated forcefully) and get False at once.
    """
    if sys.platform != "win32":
        return False
    try:
        r = subprocess.run(
            ["taskkill", "/PID", str(pid), "/T"],
            capture_output=True,
            timeout=20,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def terminate(
    proc: Proc,
    grace_s: float = 10,
    *,
    soft: Callable[[int], bool] = soft_stop,
    kill: Callable[[int], None] = kill_tree,
) -> None:
    """Stop the process tree: ask politely, wait up to `grace_s`, then kill the tree. Returns once it is gone.

    The hard kill (`taskkill /T /F`) only happens when the polite request was refused or the process was
    still alive after the grace period, so a service that accepts the request can finish its writes.
    """
    if proc.popen.poll() is None:
        if soft(proc.pid):
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.popen.wait(timeout=grace_s)
        if proc.popen.poll() is None:
            kill(proc.pid)
            with contextlib.suppress(OSError):
                proc.popen.kill()
            proc.popen.wait(timeout=10)
    for t in proc.pumps:
        t.join(timeout=2)


# ---------------------------------------------------------------------------------------------
# nvidia-smi probes
# ---------------------------------------------------------------------------------------------
def _nvsmi(args: list[str]) -> str | None:
    """Run nvidia-smi; None when it is absent or fails."""
    try:
        r = subprocess.run(
            ["nvidia-smi", *args],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def vram_mb(pid: int) -> int | None:
    """VRAM (MiB) used by `pid` per `nvidia-smi --query-compute-apps`; None if nvidia-smi is absent.

    Under WDDM the driver may report `[N/A]` per process; then None is returned (unknown).
    """
    out = _nvsmi(["--query-compute-apps=pid,used_memory", "--format=csv,noheader"])
    if out is None:
        return None
    total = 0
    unknown = False
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2 or not parts[0].isdigit() or int(parts[0]) != pid:
            continue
        m = re.match(r"(\d+)", parts[1])
        if m:
            total += int(m.group(1))
        else:
            unknown = True
    if unknown and total == 0:
        return None
    return total


def gpu_memory_used_mb() -> int | None:
    """Total used GPU memory (MiB) across GPUs; None if nvidia-smi is absent."""
    out = _nvsmi(["--query-gpu=memory.used", "--format=csv,noheader,nounits"])
    if out is None:
        return None
    vals = [int(m.group(1)) for line in out.splitlines() if (m := re.match(r"\s*(\d+)", line))]
    return sum(vals) if vals else None


def nvidia_smi_report() -> str | None:
    """Full `nvidia-smi` text (for the diagnostics bundle); None if absent."""
    return _nvsmi([])


def gpu_name() -> str | None:
    """Name of the first GPU per `nvidia-smi`; None if absent."""
    out = _nvsmi(["--query-gpu=name", "--format=csv,noheader"])
    if out is None:
        return None
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    return lines[0] if lines else None
