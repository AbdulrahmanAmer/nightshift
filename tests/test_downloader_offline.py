"""C1: a PC that wakes from sleep has no network for a while; that must not use up the download attempts."""

from pathlib import Path

import pytest

from installer.stage2 import downloader as dl
from ns_common.errors import NsError
from tests.test_downloader import DATA, SHA, make_client

URL = "https://x.test/a.bin"


def scripted_probe(answers: list[bool]):
    seen: list[str] = []

    def probe(url: str, timeout: float = 5.0) -> bool:
        seen.append(url)
        return answers.pop(0) if answers else True

    probe.seen = seen  # type: ignore[attr-defined]
    return probe


def test_wait_for_network_is_up_at_once_when_the_first_probe_answers():
    sleeps: list[float] = []
    assert dl.wait_for_network(URL, sleep=sleeps.append, probe=lambda u: True) == "up" and sleeps == []


def test_wait_for_network_backs_off_until_the_network_returns():
    sleeps: list[float] = []
    probe = scripted_probe([False, False, False, True])
    assert dl.wait_for_network(URL, sleep=sleeps.append, probe=probe) == "back"
    assert sleeps == [2, 4, 8]


def test_wait_for_network_gives_up_after_two_minutes():
    sleeps: list[float] = []
    assert dl.wait_for_network(URL, sleep=sleeps.append, probe=lambda u: False) == "down"
    assert sum(sleeps) == dl.OFFLINE_WAIT_S == 120.0 and max(sleeps) <= dl.BACKOFF_CAP_S


def test_a_wake_from_sleep_does_not_count_as_an_attempt(tmp_path: Path, monkeypatch):
    # three failures in a row would be NS-E105 with the default 3 attempts; the network was down for each
    probe = scripted_probe([False, True, False, True, False, True])
    monkeypatch.setattr(dl, "network_is_up", probe)
    sleeps: list[float] = []
    retries: list[tuple[int, str, float]] = []
    dest = tmp_path / "a.bin"
    dl.download(URL, dest, SHA, client=make_client(fail_first=3, fail_error=True), sleep=sleeps.append,
                on_retry=lambda attempt, reason, wait: retries.append((attempt, reason, wait)))
    assert dest.read_bytes() == DATA
    assert [r[1] for r in retries] == ["offline"] * 3 and sleeps == [2, 2, 2]


def test_a_site_that_fails_with_the_network_up_still_ends_in_e105(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dl, "network_is_up", lambda url, timeout=5.0: True)
    with pytest.raises(NsError) as ei:
        dl.download(URL, tmp_path / "a.bin", SHA, client=make_client(fail_first=9, fail_error=True),
                    sleep=lambda s: None)
    assert ei.value.code == "NS-E105"


def test_a_network_that_never_returns_still_ends_in_e105(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dl, "network_is_up", lambda url, timeout=5.0: False)
    sleeps: list[float] = []
    with pytest.raises(NsError) as ei:
        dl.download(URL, tmp_path / "a.bin", SHA, client=make_client(fail_first=9, fail_error=True),
                    sleep=sleeps.append)
    assert ei.value.code == "NS-E105"
    assert sum(sleeps) >= 3 * dl.OFFLINE_WAIT_S        # every attempt waited the full two minutes first


def test_the_real_probe_is_false_for_a_closed_port():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]                       # bound, never listened on, then closed: refuses
    real = dl.wait_for_network.__kwdefaults__["probe"]        # conftest patches dl.network_is_up
    assert real(f"http://127.0.0.1:{port}/", timeout=0.5) is False


def test_the_row_note_says_the_internet_is_gone():
    from installer.stage2.web.controller import retry_note

    assert retry_note("offline", 4.0) == "No internet right now, waiting for it to come back in 4 s"
