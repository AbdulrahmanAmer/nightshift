"""Thread-safe pub/sub for dashboard events and the server-sent-events framing (component C9)."""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable, Iterator
from typing import Any


class Subscription:
    """One consumer of an `EventHub`; `get` blocks up to `timeout` seconds."""

    def __init__(self, hub: EventHub) -> None:
        self._hub = hub
        self._q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=500)

    def _put(self, item: dict[str, Any]) -> None:
        """Enqueue `item`, dropping the oldest entry when the consumer is too slow."""
        try:
            self._q.put_nowait(item)
        except queue.Full:
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            self._q.put_nowait(item)

    def get(self, timeout: float) -> dict[str, Any] | None:
        """Next event as `{kind, data}` or None when nothing arrived within `timeout` seconds."""
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def close(self) -> None:
        """Detach from the hub."""
        self._hub.remove(self)


class EventHub:
    """Fan-out of `{kind, data}` events to any number of subscribers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[Subscription] = []

    def subscribe(self) -> Subscription:
        """Create and register a new subscription."""
        sub = Subscription(self)
        with self._lock:
            self._subs.append(sub)
        return sub

    def remove(self, sub: Subscription) -> None:
        """Unregister `sub` (idempotent)."""
        with self._lock:
            if sub in self._subs:
                self._subs.remove(sub)

    def publish(self, kind: str, data: dict[str, Any]) -> None:
        """Deliver `{kind, data}` to every current subscriber."""
        with self._lock:
            subs = list(self._subs)
        for s in subs:
            s._put({"kind": kind, "data": data})

    @property
    def subscriber_count(self) -> int:
        """Number of live subscriptions."""
        with self._lock:
            return len(self._subs)


def sse_frame(kind: str, data: Any) -> str:
    """One server-sent-events frame."""
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


def iter_sse(
    sub: Subscription,
    status_fn: Callable[[], dict[str, Any]],
    *,
    heartbeat_s: float = 2.0,
    limit: int | None = None,
    stop: threading.Event | None = None,
) -> Iterator[str]:
    """Yield SSE frames: a `status` frame first, every relayed event followed by a fresh `status`
    frame, and a `status` frame whenever `heartbeat_s` passes without events.

    Stops after `limit` frames (tests) or when `stop` is set; always detaches the subscription.
    """
    stop = stop or threading.Event()
    sent = 0
    try:
        yield sse_frame("status", status_fn())
        sent += 1
        while not stop.is_set() and (limit is None or sent < limit):
            item = sub.get(heartbeat_s)
            if item is not None and item["kind"] != "status":
                yield sse_frame(item["kind"], item["data"])
                sent += 1
                if limit is not None and sent >= limit:
                    break
            yield sse_frame("status", status_fn())
            sent += 1
    finally:
        sub.close()
