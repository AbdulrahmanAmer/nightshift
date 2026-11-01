from pathlib import Path

import pytest

from ns_dashboard.security import is_loopback_host, is_loopback_origin, is_within, safe_join


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "127.0.0.1:8100", "localhost", "LOCALHOST:8100", "[::1]:8100", "[::1]", "::1"],
)
def test_loopback_hosts_accepted(host: str):
    assert is_loopback_host(host)


@pytest.mark.parametrize(
    "host",
    ["", None, "evil.example", "evil.example:8100", "127.0.0.1.evil.com", "192.168.1.5:8100", "0.0.0.0"],
)
def test_non_loopback_hosts_rejected(host: str | None):
    assert not is_loopback_host(host)


def test_origin_rules():
    assert is_loopback_origin(None)
    assert is_loopback_origin("")
    assert is_loopback_origin("http://127.0.0.1:8100")
    assert is_loopback_origin("http://localhost:8100")
    assert not is_loopback_origin("null")
    assert not is_loopback_origin("https://evil.example")
    assert not is_loopback_origin("http://127.0.0.1.evil.example")


def test_safe_join_stays_inside(tmp_path: Path):
    assert safe_join(tmp_path, "a", "b.txt") == (tmp_path / "a" / "b.txt").resolve()
    with pytest.raises(ValueError):
        safe_join(tmp_path, "..", "etc")
    with pytest.raises(ValueError):
        safe_join(tmp_path, "a", "..", "..", "x")


def test_is_within(tmp_path: Path):
    inside = tmp_path / "x" / "y.mp4"
    assert is_within(tmp_path, inside)
    assert is_within(tmp_path, tmp_path)
    assert not is_within(tmp_path / "x", tmp_path / "other.mp4")
