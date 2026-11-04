"""Keep Windows awake while setup runs (standard library only).

Network traffic does not stop Windows' idle sleep, so a PC left alone for the 1 to 2 hour download used to
sleep in the middle of it. ``SetThreadExecutionState`` with ``ES_CONTINUOUS | ES_SYSTEM_REQUIRED`` holds the
system awake for as long as the calling thread lives or until it is reset, so the thread that drives the
steps calls ``keep_awake()``. The display may still turn off. Not Windows: does nothing.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set_execution_state(flags: int) -> int:
    """Call kernel32 (0 when this is not Windows or the call fails)."""
    if sys.platform != "win32":
        return 0
    try:
        import ctypes

        return int(ctypes.windll.kernel32.SetThreadExecutionState(ctypes.c_uint32(flags)))  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return 0


@contextmanager
def keep_awake(set_state: Callable[[int], int] = _set_execution_state) -> Iterator[None]:
    """Hold the system awake inside the ``with`` block on this thread; always release it afterwards."""
    set_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    try:
        yield
    finally:
        set_state(ES_CONTINUOUS)
