"""H-1: the helper every test that talks to a real host goes through (``tests/online.py``).

A transient answer (HTTP 5xx, 429, a timeout, a reset connection) is retried three times with a short
backoff; what is not transient (404, 403, a wrong size, a wrong hash) fails at once. After the retries a
transient answer is an *upstream outage*: a skip in the online pin checks, a failure that says so in the
real-network tests. No real host is contacted here: the transports are fakes and the sleeps are recorded.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from installer.stage2 import downloader as dl
from tests import online
from tests.online import (
    OUTAGE_MARK,
    RetryTransport,
    UpstreamOutageError,
    give_up_on_outage,
    online_client,
    retry_call,
    skip_on_outage,
    split_outage,
)


@pytest.fixture(autouse=True)
def _fresh_outage_memory(monkeypatch: pytest.MonkeyPatch):
    online.forget_outages()
    monkeypatch.delenv("NS_REQUIRE_ONLINE", raising=False)
    yield
    online.forget_outages()


class Script:
    """A fake transport that answers a list of statuses (or raises an exception), then repeats the last."""

    def __init__(self, *answers: int | Exception) -> None:
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, content=b"body" if answer == 200 else b"")


def client_for(script: Script, sleeps: list[float]) -> httpx.Client:
    transport = RetryTransport(httpx.MockTransport(script), sleep=sleeps.append)
    return httpx.Client(transport=transport)


# ------------------------------------------------------------------------------------------ retrying
def test_504_504_200_is_retried_and_the_third_answer_is_returned():
    script, sleeps = Script(504, 504, 200), []
    r = client_for(script, sleeps).get("https://example.test/file")
    assert r.status_code == 200 and r.content == b"body"
    assert script.calls == 3
    assert len(sleeps) == 2 and all(0 < s <= 10 for s in sleeps)  # a short backoff, twice


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_every_transient_status_is_retried(status: int):
    script = Script(status, 200)
    assert client_for(script, []).get("https://example.test/").status_code == 200
    assert script.calls == 2


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectTimeout("t"),
        httpx.ReadTimeout("t"),
        httpx.ConnectError("refused"),
        httpx.ReadError("reset by peer"),
        httpx.RemoteProtocolError("closed"),
    ],
)
def test_a_timeout_or_reset_connection_is_retried(error: Exception):
    script = Script(error, 200)
    assert client_for(script, []).get("https://example.test/").status_code == 200
    assert script.calls == 2


@pytest.mark.parametrize("status", [404, 403, 401, 400, 416])
def test_a_status_that_is_not_transient_comes_back_at_once_with_no_retry(status: int):
    script, sleeps = Script(status), []
    r = client_for(script, sleeps).get("https://example.test/missing")
    assert r.status_code == status
    assert script.calls == 1 and sleeps == []


def test_a_404_is_still_a_failure_in_the_test_that_checks_it():
    """The pin is wrong: the caller's own assertion sees the 404, not an outage."""
    script, sleeps = Script(404), []
    client = client_for(script, sleeps)
    with pytest.raises(httpx.HTTPStatusError):
        client.get("https://example.test/missing").raise_for_status()
    assert script.calls == 1


def test_504_four_times_is_an_upstream_outage_naming_the_host_and_the_status():
    script, sleeps = Script(504), []
    with pytest.raises(UpstreamOutageError) as ei:
        client_for(script, sleeps).get("https://objects.example.test/file")
    assert script.calls == 4  # the first try and three retries
    assert len(sleeps) == 3
    e = ei.value
    assert e.host == "objects.example.test" and "HTTP 504" in e.detail
    assert OUTAGE_MARK in str(e) and "objects.example.test" in str(e) and "504" in str(e)
    assert isinstance(e, httpx.HTTPError)  # code that catches HTTPError (the product's does) still sees it


def test_a_connection_that_never_comes_back_is_an_outage_too():
    script = Script(httpx.ConnectError("refused"))
    with pytest.raises(UpstreamOutageError) as ei:
        client_for(script, []).head("https://pypi.example.test/")
    assert script.calls == 4 and "ConnectError" in ei.value.detail


def test_a_host_that_just_failed_is_not_waited_on_again():
    """One outage costs the backoff once; the next request to that host is a single try, so a long suite
    does not spend minutes waiting on a dead host."""
    first, sleeps = Script(504), []
    with pytest.raises(UpstreamOutageError):
        client_for(first, sleeps).get("https://dead.example.test/a")
    second = Script(504)
    with pytest.raises(UpstreamOutageError):
        client_for(second, sleeps).get("https://dead.example.test/b")
    assert second.calls == 1 and len(sleeps) == 3
    other = Script(200)  # another host is still tried normally
    assert client_for(other, []).get("https://alive.example.test/").status_code == 200


def test_retry_call_retries_a_raise_for_status_and_lets_a_404_through():
    calls: list[int] = []

    def fetch(status: int):
        def run():
            calls.append(status)
            httpx.Response(status, request=httpx.Request("GET", "https://x.test")).raise_for_status()
            return "ok"

        return run

    assert retry_call(fetch(200), host="x.test", sleep=lambda s: None) == "ok"
    with pytest.raises(httpx.HTTPStatusError):
        retry_call(fetch(404), host="x.test", sleep=lambda s: None)
    assert calls == [200, 404]
    calls.clear()
    with pytest.raises(UpstreamOutageError):
        retry_call(fetch(503), host="x.test", sleep=lambda s: None)
    assert calls == [503] * 4


def test_the_only_other_error_a_test_can_get_is_its_own():
    with pytest.raises(ValueError):
        retry_call(
            lambda: (_ for _ in ()).throw(ValueError("wrong hash")), host="x.test", sleep=lambda s: None
        )


# ------------------------------------------------------------------------ skip, or fail, after the retries
def test_an_outage_in_a_pin_check_becomes_a_skip_that_names_the_host_and_the_status():
    @skip_on_outage
    def check() -> None:
        raise UpstreamOutageError("github.example.test", "HTTP 504 after 4 attempt(s)")

    with pytest.raises(pytest.skip.Exception) as ei:
        check()
    reason = str(ei.value)
    assert reason.startswith(OUTAGE_MARK) and "github.example.test" in reason and "HTTP 504" in reason


def test_ns_require_online_turns_that_skip_into_a_failure(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NS_REQUIRE_ONLINE", "1")

    @skip_on_outage
    def check() -> None:
        raise UpstreamOutageError("github.example.test", "HTTP 504 after 4 attempt(s)")

    with pytest.raises(pytest.fail.Exception) as ei:
        check()
    assert OUTAGE_MARK in str(ei.value) and "github.example.test" in str(ei.value)


def test_a_wrong_pin_is_never_a_skip():
    @skip_on_outage
    def check() -> None:
        assert hashlib.sha256(b"a").hexdigest() == hashlib.sha256(b"b").hexdigest(), "wrong hash"

    with pytest.raises(AssertionError, match="wrong hash"):
        check()


def test_problems_with_an_outage_are_told_apart_from_real_ones():
    problems = [
        f"bins.a: https://x.test/a: {OUTAGE_MARK}: x.test HTTP 504 after 4 attempt(s)",
        "bins.b: https://x.test/b: HTTP 404",
        "profiles.p.llm: sha256 missing",
    ]
    real, outage = split_outage(problems)
    assert real == problems[1:] and outage == problems[:1]


def test_a_real_network_test_skips_on_an_outage_and_never_asserts_about_bytes(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("NS_REQUIRE_ONLINE", raising=False)

    @give_up_on_outage
    def bytes_test() -> None:
        raise UpstreamOutageError("github.example.test", "HTTP 504 after 4 attempt(s)")

    with pytest.raises(pytest.skip.Exception) as ei:
        bytes_test()
    assert str(ei.value).startswith(OUTAGE_MARK) and "github.example.test" in str(ei.value)

    monkeypatch.setenv("NS_REQUIRE_ONLINE", "1")
    with pytest.raises(pytest.fail.Exception) as ei_req:
        bytes_test()
    assert str(ei_req.value).startswith(OUTAGE_MARK) and "github.example.test" in str(ei_req.value)
    monkeypatch.delenv("NS_REQUIRE_ONLINE")

    @give_up_on_outage
    def wrapped_by_the_downloader() -> None:  # the downloader re-raises the outage as NS-E105
        try:
            raise UpstreamOutageError("github.example.test", "HTTP 504 after 4 attempt(s)")
        except httpx.HTTPError as e:
            from installer.stage2.errors import make_error

            raise make_error("NS-E105", f"https://github.example.test/f: {e}") from e

    with pytest.raises(pytest.skip.Exception) as ei2:
        wrapped_by_the_downloader()
    assert OUTAGE_MARK in str(ei2.value)

    @give_up_on_outage
    def real_failure() -> None:
        assert 1 == 2, "504 == 200"

    with pytest.raises(AssertionError):
        real_failure()


def test_online_client_retries_and_keeps_the_callers_options():
    client = online_client(timeout=5, headers={"User-Agent": "nightshift-test"})
    try:
        assert isinstance(client._transport, RetryTransport)
        assert client.headers["User-Agent"] == "nightshift-test"
    finally:
        client.close()


# ------------------------------------------------ the product's own downloader (installer/stage2), item 4
def downloader_client(statuses: list[int], data: bytes, seen: list[tuple[str, str | None]]) -> httpx.Client:
    state = {"gets": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.headers.get("range")))
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": str(len(data))})
        answer = statuses[min(state["gets"], len(statuses) - 1)]
        state["gets"] += 1
        return httpx.Response(answer, content=data if answer == 200 else b"")

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_the_downloader_waits_out_a_504_like_the_tests_do_and_finishes(tmp_path: Path):
    data = b"payload" * 100
    seen: list[tuple[str, str | None]] = []
    waits: list[tuple[str, int, float]] = []
    dest = tmp_path / "f.bin"
    out = dl.download(
        "https://github.example.test/f.bin",
        dest,
        hashlib.sha256(data).hexdigest(),
        client=downloader_client([504, 504, 200], data, seen),
        sleep=lambda s: None,
        on_site_wait=lambda w: waits.append((w.host, w.status, w.wait_s)),
    )
    assert out == dest and dest.read_bytes() == data
    assert [m for m, _ in seen].count("GET") == 3  # 504, 504, then the file
    assert waits == [("github.example.test", 504, 2.0), ("github.example.test", 504, 4.0)]


def test_the_downloader_gives_up_on_a_504_after_the_ten_minute_window_with_ns_e105(tmp_path: Path):
    from ns_common.errors import NsError

    seen: list[tuple[str, str | None]] = []
    sleeps: list[float] = []
    with pytest.raises(NsError) as ei:
        dl.download(
            "https://github.example.test/f.bin",
            tmp_path / "f.bin",
            None,
            client=downloader_client([504], b"x", seen),
            sleep=sleeps.append,
        )
    assert ei.value.code == "NS-E105" and "504" in str(ei.value) and "github.example.test" in str(ei.value)
    assert sum(sleeps) == dl.SITE_WINDOW_S == 600.0  # no attempt count: the window decides


def test_the_downloader_does_not_retry_a_404(tmp_path: Path):
    from ns_common.errors import NsError

    seen: list[tuple[str, str | None]] = []
    with pytest.raises(NsError) as ei:
        dl.download(
            "https://github.example.test/f.bin",
            tmp_path / "f.bin",
            None,
            client=downloader_client([404], b"x", seen),
            sleep=lambda s: None,
        )
    assert ei.value.code == "NS-E105" and "404" in str(ei.value)
    assert [m for m, _ in seen].count("GET") == 1


def test_a_host_name_that_does_not_resolve_is_a_failure_not_an_outage():
    import socket

    err = httpx.ConnectError("[Errno 11001] getaddrinfo failed")
    err.__cause__ = socket.gaierror(11001, "getaddrinfo failed")
    calls: list[int] = []

    def fn():
        calls.append(1)
        raise err

    with pytest.raises(httpx.ConnectError):
        online.retry_call(fn, host="typo.example.test", sleep=lambda s: None)
    assert len(calls) == 1


def test_a_host_that_answers_again_is_waited_on_in_full_next_time():
    online.forget_outages()
    host = "flaky.example.test"
    with pytest.raises(online.UpstreamOutageError):
        online.retry_call(lambda: httpx.Response(504), host=host, sleep=lambda s: None)
    # inside the memory window: one try, which succeeds and clears the memory
    assert online.retry_call(lambda: httpx.Response(200), host=host, sleep=lambda s: None).status_code == 200
    calls: list[int] = []

    def fn():
        calls.append(1)
        return httpx.Response(504 if len(calls) < 3 else 200)

    assert online.retry_call(fn, host=host, sleep=lambda s: None).status_code == 200
    assert len(calls) == 3
    online.forget_outages()
