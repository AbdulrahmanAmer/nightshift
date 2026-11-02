"""C2 GPU readout for `Status.gpu` (CR-C3-UI-4): name, load, VRAM and temperature from nvidia-smi.

The supervisor asks a `GpuProbe`; the real one shells out to `nvidia-smi` at most every `min_interval_s`
and caches the answer (including "absent"), so polling `/status` never spawns a process per request.
`FakeGpuProbe` serves tests. A probe returns None when there is nothing to report (no NVIDIA driver, the
`cpu-dev` profile, nvidia-smi failing); the status object then carries `gpu: null`.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Protocol

from ns_common.models import GpuStatus

QUERY = "name,utilization.gpu,memory.used,memory.total,temperature.gpu"
_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MIN_INTERVAL_S = 2.0


class GpuProbe(Protocol):
    """Anything that can report the GPU's current numbers."""

    def sample(self) -> GpuStatus | None:
        """Latest reading, or None when no GPU readout is available."""
        ...


def _num(text: str) -> float | None:
    """A float from one CSV cell; None for `[N/A]`, `[Not Supported]` and anything non-numeric."""
    try:
        return float(text.strip())
    except ValueError:
        return None


def parse_nvidia_smi(output: str) -> GpuStatus | None:
    """Parse `nvidia-smi --query-gpu=<QUERY> --format=csv,noheader,nounits`; the first GPU wins."""
    for line in output.splitlines():
        if not line.strip():
            continue
        # The name comes first and may contain a comma in theory; the four numbers are always last.
        parts = line.rsplit(",", 4)
        if len(parts) != 5 or not parts[0].strip():
            continue
        load, used, total, temp = (_num(p) for p in parts[1:])
        return GpuStatus(
            name=parts[0].strip(),
            load_pct=load,
            vram_used_mb=int(used) if used is not None else None,
            vram_total_mb=int(total) if total is not None else None,
            temp_c=temp,
        )
    return None


def run_nvidia_smi() -> str | None:
    """Run the query; None when nvidia-smi is absent, fails or hangs."""
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return None
    try:
        r = subprocess.run(
            [exe, f"--query-gpu={QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


class NvidiaSmiGpuProbe:
    """Cached nvidia-smi sampler: one process at most every `min_interval_s`, thread-safe."""

    def __init__(
        self,
        *,
        min_interval_s: float = MIN_INTERVAL_S,
        runner: Callable[[], str | None] = run_nvidia_smi,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_interval_s = min_interval_s
        self._runner = runner
        self._clock = clock
        self._lock = threading.Lock()
        self._at: float | None = None
        self._value: GpuStatus | None = None

    def sample(self) -> GpuStatus | None:
        """The cached reading while it is younger than `min_interval_s`, else a fresh one."""
        with self._lock:
            now = self._clock()
            if self._at is not None and now - self._at < self.min_interval_s:
                return self._value
            out = self._runner()
            self._value = parse_nvidia_smi(out) if out is not None else None
            self._at = now
            return self._value


class NullGpuProbe:
    """No GPU readout (the `cpu-dev` profile)."""

    def sample(self) -> GpuStatus | None:
        return None


class FakeGpuProbe:
    """Test probe: returns `value` (or the next of `values`, repeating the last) and counts calls."""

    def __init__(self, value: GpuStatus | None = None, *, values: list[GpuStatus | None] | None = None) -> None:
        self._values = list(values) if values is not None else [value]
        self.calls = 0

    def sample(self) -> GpuStatus | None:
        i = min(self.calls, len(self._values) - 1)
        self.calls += 1
        return self._values[i]
