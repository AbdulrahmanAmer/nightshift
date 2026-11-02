"""C2 GPU lock: FIFO, per-waiter timeout, owner tracking (spec 6.1, ENGINEERING rule 8)."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field

from ns_common.errors import NsError


@dataclass
class GpuState:
    """Who holds the GPU right now and through which service."""

    owner: str | None = None
    service: str | None = None


@dataclass
class _Waiter:
    owner: str
    service: str
    fut: asyncio.Future[None]
    granted: bool = False


@dataclass
class GpuLock:
    """One GPU tenant at a time. Waiters are served strictly in arrival order.

    The holder may re-acquire with another service (a swap without leaving the queue). A waiter that
    times out raises `NS-E201` and leaves the queue intact.
    """

    state: GpuState = field(default_factory=GpuState)
    _queue: deque[_Waiter] = field(default_factory=deque, init=False, repr=False)

    def waiters(self) -> list[tuple[str, str]]:
        """Pending (owner, service) pairs in FIFO order."""
        return [(w.owner, w.service) for w in self._queue if not w.fut.done()]

    def _grant(self, w: _Waiter) -> None:
        self.state.owner = w.owner
        self.state.service = w.service
        w.granted = True
        if not w.fut.done():
            w.fut.set_result(None)

    def _pass_on(self) -> None:
        """Hand the free lock to the first live waiter, or mark it free."""
        self.state.owner = None
        self.state.service = None
        while self._queue:
            w = self._queue.popleft()
            if w.fut.done():
                continue
            self._grant(w)
            return

    async def acquire(self, owner: str, service: str, timeout_s: float | None) -> None:
        """Wait for the GPU. Raises `NS-E201` when `timeout_s` elapses (0 fails at once if busy)."""
        if self.state.owner == owner:
            self.state.service = service
            return
        if self.state.owner is None and not self.waiters():
            self.state.owner = owner
            self.state.service = service
            return
        if timeout_s is not None and timeout_s <= 0:
            raise NsError("NS-E201", f"GPU held by {self.state.owner}; {owner} did not wait")
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        w = _Waiter(owner, service, fut)
        self._queue.append(w)
        try:
            await asyncio.wait_for(fut, timeout_s)
        except TimeoutError:
            self._abandon(w)
            raise NsError(
                "NS-E201", f"{owner} waited {timeout_s}s for {service}; GPU held by {self.state.owner}"
            ) from None
        except asyncio.CancelledError:
            self._abandon(w)
            raise

    def _abandon(self, w: _Waiter) -> None:
        """A waiter gives up. If it had already been granted the lock, pass the lock on."""
        if w.granted:
            self._pass_on()
            return
        if not w.fut.done():
            w.fut.cancel()
        try:
            self._queue.remove(w)
        except ValueError:
            pass

    def release(self, owner: str) -> bool:
        """Release if `owner` holds the lock; returns False (and changes nothing) otherwise."""
        if self.state.owner != owner:
            return False
        self._pass_on()
        return True

    def drop_waiters(self, owner: str) -> int:
        """Remove `owner` from the queue (its client is gone); each pending `acquire` fails with `NS-E201`.

        A waiter that was already granted the lock is the holder, not a waiter: use `release` for that.
        """
        dropped = 0
        for w in list(self._queue):
            if w.owner != owner or w.fut.done():
                continue
            self._queue.remove(w)
            w.fut.set_exception(NsError("NS-E201", f"{owner} was released while it waited for {w.service}"))
            dropped += 1
        return dropped
