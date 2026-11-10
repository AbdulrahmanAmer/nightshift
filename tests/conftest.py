import importlib
import importlib.util
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

# No test may change the PC it runs on, whatever it does with its fakes. The product refuses at its lowest
# call points when it runs under a test, and an audit hook covers every process this session starts
# (ns_common/testguard.py). In this process "under test" is known from pytest being imported; this variable
# carries it to every child process a test starts (`ns schedule install` as a child, the wizard,
# uninstall.ps1), and the hook puts it back into any child environment that lacks it. Both are set at import,
# for the whole session, outside any fixture: nothing a test undoes takes them away, and there is no opt-out.
os.environ["NS_UNDER_TEST"] = "1"
if importlib.util.find_spec("ns_common.testguard") is not None:  # absent only on a checkout before the hook
    importlib.import_module("ns_common.testguard").install()
# A NIGHTSHIFT_HOME the session inherited is a real install of this PC's owner (CI has none): dropped here.
os.environ.pop("NIGHTSHIFT_HOME", None)


@pytest.hookimpl(trylast=True)
def pytest_configure(config: pytest.Config) -> None:
    """The home of a test that takes none of its own (and of a test module that loads formats or prompts while
    it is imported): an empty scratch folder under the session's temp folder, never the owner's install and
    never `%LOCALAPPDATA%/Nightshift`, which the guard refuses. `tmp_home` gives a test a home to itself."""
    base = config.getoption("basetemp", None)
    root = Path(base) if base else Path(tempfile.gettempdir()) / f"nightshift-tests-{os.getpid()}"
    NO_HOME.append((root / "no-home").resolve())
    os.environ["NIGHTSHIFT_HOME"] = str(NO_HOME[0])


#: The scratch home of this session (one entry, set by `pytest_configure`).
NO_HOME: list[Path] = []


def left_behind(folder: Path) -> list[str]:
    """The names of what is in `folder`, which is emptied; nothing when it is missing or empty."""
    try:
        names = sorted(p.name for p in folder.iterdir())
    except OSError:
        return []
    shutil.rmtree(folder, ignore_errors=True)
    return names


@pytest.fixture(autouse=True)
def _scratch_home_stays_empty() -> Iterator[None]:
    """The scratch home is shared by every test that takes no home of its own, so nothing may be left in it:
    a test that wrote there (itself, or a child it did not hand its home) fails, by name."""
    yield
    left = left_behind(NO_HOME[0]) if NO_HOME else []
    if left:
        pytest.fail("this test wrote into the home shared by tests that take none (" + ", ".join(left[:8])
                    + "); give it the tmp_home fixture", pytrace=False)


def pytest_sessionfinish(session: pytest.Session) -> None:
    """What no test can be blamed for (written while the tests were collected, or after the last one)."""
    left = left_behind(NO_HOME[0]) if NO_HOME else []
    if left:
        session.exitstatus = 1
        said = "FAILED: the home shared by tests that take none was written to outside any test: "
        session.config.get_terminal_writer().line(said + ", ".join(left[:8]), red=True)


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
