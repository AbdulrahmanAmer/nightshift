"""The setup state and its long-poll cursor (docs/design/installer-ui.md 1.4). Standard library only."""

from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

LONG_POLL_S = 15.0


class SetupStore:
    """Holds the SetupState dict. Every change bumps ``rev`` and wakes the long pollers."""

    def __init__(self, state: dict[str, Any], finalize: Callable[[dict[str, Any]], None] | None = None) -> None:
        self._cond = threading.Condition()
        self._state = state
        self._finalize = finalize
        self._state.setdefault("rev", 1)
        if finalize:
            finalize(self._state)

    def snapshot(self) -> dict[str, Any]:
        """A deep copy of the current state."""
        with self._cond:
            return copy.deepcopy(self._state)

    @property
    def rev(self) -> int:
        """The current revision."""
        with self._cond:
            return int(self._state["rev"])

    def update(self, fn: Callable[[dict[str, Any]], None]) -> int:
        """Apply ``fn`` to the state under the lock, re-derive, bump ``rev`` and wake pollers."""
        with self._cond:
            fn(self._state)
            if self._finalize:
                self._finalize(self._state)
            self._state["rev"] = int(self._state["rev"]) + 1
            self._state["updatedAt"] = datetime.now().isoformat(timespec="seconds")
            self._cond.notify_all()
            return int(self._state["rev"])

    def read(self, fn: Callable[[dict[str, Any]], Any]) -> Any:
        """Run ``fn`` on the live state under the lock (it must not keep references)."""
        with self._cond:
            return fn(self._state)

    def wait_newer(self, since: int | None, timeout: float = LONG_POLL_S) -> dict[str, Any]:
        """Return the state once ``rev > since`` or after ``timeout`` seconds, whichever comes first."""
        deadline = time.monotonic() + timeout
        with self._cond:
            while since is not None and int(self._state["rev"]) <= since:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                self._cond.wait(left)
            return copy.deepcopy(self._state)

    def wake_all(self) -> None:
        """Wake every poller without a change (shutdown)."""
        with self._cond:
            self._cond.notify_all()
