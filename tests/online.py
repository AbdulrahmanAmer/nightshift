"""One helper for every test that talks to a real host (H-1).

A real host has bad minutes: GitHub's download servers answered HTTP 504 for a few minutes and turned CI red
on tests whose pins were fine. This module separates the two things a real-host test can see:

* **transient** (HTTP 5xx, 429, a timeout, a reset or refused connection): retried three times with a short
  backoff (1, 2, 4 s). If it lasts, it is an *upstream outage*: ``UpstreamOutageError``, whose text starts
  with ``OUTAGE_MARK`` and names the host and the last status.
* **everything else** (404, 403, a wrong size, a wrong hash, a wrong redirect target): returned or raised at
  once, untouched. That is a pin being wrong, which is what these tests exist to catch.

What an outage then means depends on the test:

* an ``_online`` pin check (``skip_on_outage``) skips, with a reason that names the host and the status. CI
  prints how many were skipped (``dev/ci_outage_report.py``), and a nightly run and every tag run set
  ``NS_REQUIRE_ONLINE=1``, which turns that skip into a failure, so the checks cannot be skipped every day;
* a must-run real-network download test (``fail_on_outage``) fails with ``upstream outage: ...`` instead of an
  assertion about bytes.

A host that has just failed is not waited on again for ``MEMORY_S`` seconds (one try per request), so one dead
host costs the backoff once, not once per request.
"""

from __future__ import annotations

import functools
import os
import socket
import threading
import time
from collections.abc import Callable
from typing import Any, NoReturn

import httpx
import pytest

OUTAGE_MARK = "upstream outage"
RETRIES = 3
BACKOFF_S = (1.0, 2.0, 4.0)
MEMORY_S = 120.0

_lock = threading.Lock()
_failed_hosts: dict[str, float] = {}


class UpstreamOutageError(httpx.TransportError):
    """A real host kept answering with a transient failure through every retry."""

    def __init__(self, host: str, detail: str) -> None:
        self.host = host
        self.detail = detail
        super().__init__(f"{OUTAGE_MARK}: {host} {detail}")


def is_transient_status(code: int) -> bool:
    return code == 429 or 500 <= code <= 599


def transient_detail(exc: BaseException) -> str | None:
    """What to say about ``exc`` when it is a transient failure; None when it is not (it is the test's)."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return f"HTTP {code}" if is_transient_status(code) else None
    if isinstance(exc, httpx.ConnectError) and _is_dns_failure(exc):
        return None  # a host name that does not resolve is a wrong pin, not an outage
    if isinstance(exc, httpx.TimeoutException | httpx.NetworkError | httpx.RemoteProtocolError):
        return f"{type(exc).__name__}: {exc}".rstrip(": ")
    return None


def _is_dns_failure(exc: BaseException) -> bool:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, socket.gaierror):
            return True
        seen.add(id(cur))
        cur = cur.__cause__ or cur.__context__
    return False


def forget_outages() -> None:
    with _lock:
        _failed_hosts.clear()


def _recently_failed(host: str) -> bool:
    with _lock:
        at = _failed_hosts.get(host)
        return at is not None and time.monotonic() - at < MEMORY_S


def _forget(host: str) -> None:
    with _lock:
        _failed_hosts.pop(host, None)


def _remember(host: str) -> None:
    with _lock:
        _failed_hosts[host] = time.monotonic()


def retry_call[T](
    fn: Callable[[], T],
    *,
    host: str,
    retries: int = RETRIES,
    backoff: tuple[float, ...] = BACKOFF_S,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run ``fn``; retry a transient failure ``retries`` times; raise ``UpstreamOutageError`` if it lasts.

    A transient failure is a transient HTTP status (returned as a response or raised by
    ``raise_for_status``), a timeout or a network error. Anything else propagates unchanged."""
    attempts = 1 if _recently_failed(host) else retries + 1
    last = ""
    for n in range(attempts):
        try:
            result = fn()
        except Exception as e:
            detail = transient_detail(e)
            if detail is None:
                raise
        else:
            if not (isinstance(result, httpx.Response) and is_transient_status(result.status_code)):
                _forget(host)
                return result
            detail = f"HTTP {result.status_code}"
            result.close()
        last = detail
        if n < attempts - 1:
            sleep(backoff[min(n, len(backoff) - 1)])
    _remember(host)
    raise UpstreamOutageError(host, f"{last} after {attempts} attempt(s)")


class RetryTransport(httpx.BaseTransport):
    """An httpx transport that applies ``retry_call`` to every request."""

    def __init__(
        self, inner: httpx.BaseTransport | None = None, *, sleep: Callable[[float], None] = time.sleep
    ):
        self.inner = inner or httpx.HTTPTransport()
        self.sleep = sleep

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return retry_call(lambda: self.inner.handle_request(request), host=request.url.host, sleep=self.sleep)

    def close(self) -> None:
        self.inner.close()


def online_client(**kw: Any) -> httpx.Client:
    """An ``httpx.Client`` (keyword arguments as for ``httpx.Client``) whose requests are retried as above."""
    return httpx.Client(transport=RetryTransport(), **kw)


def require_online() -> bool:
    """True where a skipped online check is not acceptable (the nightly run and the tag runs set this)."""
    return bool(os.environ.get("NS_REQUIRE_ONLINE"))


def split_outage(problems: list[str]) -> tuple[list[str], list[str]]:
    """``(real problems, outage problems)`` from the lines ``resolve_manifest.check`` gives."""
    real = [p for p in problems if OUTAGE_MARK not in p]
    return real, [p for p in problems if OUTAGE_MARK in p]


def give_up(text: str) -> NoReturn:
    """The outage is final: skip (an online pin check), or fail where ``NS_REQUIRE_ONLINE`` is set."""
    if text.startswith(OUTAGE_MARK):
        reason = text
    else:
        reason = text[text.index(OUTAGE_MARK) :] if OUTAGE_MARK in text else f"{OUTAGE_MARK}: {text}"
    if require_online():
        pytest.fail(f"{reason} (NS_REQUIRE_ONLINE is set: this check may not be skipped)", pytrace=False)
    pytest.skip(reason)


def outage_in(exc: BaseException) -> str | None:
    """The outage text when ``exc`` is, or was caused by, an outage (the downloader wraps it in NS-E105)."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        text = str(cur)
        if isinstance(cur, UpstreamOutageError):
            return text
        if OUTAGE_MARK in text:
            return text[text.index(OUTAGE_MARK) :]
        cur = cur.__cause__ or cur.__context__
    return None


def skip_on_outage[T](fn: Callable[..., T]) -> Callable[..., T]:
    """For an ``_online`` pin check: an outage that outlasts the retries skips the test (fails it where
    ``NS_REQUIRE_ONLINE`` is set). Any other failure stays a failure."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kw: Any) -> T:
        try:
            return fn(*args, **kw)
        except UpstreamOutageError as e:
            give_up(str(e))

    return wrapper


def fail_on_outage[T](fn: Callable[..., T]) -> Callable[..., T]:
    """For a must-run real-network test: an outage that outlasts the retries fails the test with
    ``upstream outage: <host> <status>``, never with an assertion about bytes. Other failures are kept."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kw: Any) -> T:
        try:
            return fn(*args, **kw)
        except Exception as e:
            text = outage_in(e)
            if text is None:
                raise
            pytest.fail(text, pytrace=False)

    return wrapper
