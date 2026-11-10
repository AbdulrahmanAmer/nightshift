import os
from pathlib import Path

import pytest

# No test may reach the machine's real Task Scheduler or keyring, whatever it does with its fakes. The product
# refuses at the lowest call points when it runs under a test (ns_supervisor/testguard.py). In this process
# that is known from pytest being imported; this variable carries it to every child process a test starts
# (`ns schedule install` as a child, the wizard, uninstall.ps1). Set at import, for the whole session, outside
# any fixture: no `monkeypatch.undo()` takes it away, and there is no opt-out.
os.environ["NS_UNDER_TEST"] = "1"


@pytest.fixture
def tmp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(home))
    return home


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _roomy_outbox_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """pytest's temp folders on the CI runner are about 110 characters deep; the Outbox path guard (200) is
    meant for a real home like C:/Users/Name/Nightshift, so ordinary tests run with a generous limit and the
    guard's own test sets 200 back."""
    from ns_studio.media import outbox

    monkeypatch.setattr(outbox, "MAX_PATH", 400)


@pytest.fixture(autouse=True)
def _network_is_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """The downloader probes the network after a failed transfer (a PC that woke from sleep). Tests use fake
    hosts and fake sleeps, so the probe says "up" unless a test sets its own."""
    from installer.stage2 import downloader

    monkeypatch.setattr(downloader, "network_is_up", lambda url, timeout=5.0: True)
