import importlib
import importlib.util
import os
import shutil
import socket
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

# The GPU kit's cases live in tests/gpukit_cases.py and are collected from the tests/test_gpu_kit*.py part
# files (CI splits by file). pytest rewrites the asserts of test_*.py files only: this adds that module.
pytest.register_assert_rewrite("tests.gpukit_cases")

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

# No test may talk to the real engine, dashboard or story model of the PC it runs on. Their ports are fixed
# (ns_common.models, ns_supervisor.client, ns_dashboard: 8099, 8100, 8080), a test that builds a client or a
# runtime config without its own port connects to them, and the day a real engine runs on the machine (a QA
# proof, the owner's install) such a test talks to it: `ns restore` was refused because "the engine runs",
# and a test that reaches `ns stop` or an update would shut a real engine down. So a connection to one of them
# on loopback is refused here, whatever the test does, and the test fails, by name, when it is done. Installed
# once, at import, for the whole session. See `default_port_of` and the limits below.
DEFAULT_PORTS = {8099: "the engine", 8100: "the dashboard", 8080: "the story model"}
#: (test, port) of every connection refused so far, in order; `_reported` is how many of them were reported.
DEFAULT_PORT_HITS: list[tuple[str, int]] = []
_reported = [0]


def default_port_of(host: object, port: object) -> int | None:
    """The default port when ``host`` is this PC's loopback and ``port`` is one of `DEFAULT_PORTS`, else None.
    Takes what `socket.connect` and `loop.create_connection` take (an int or a numeric text for the port)."""
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    try:
        number = int(port)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return None
    name = host.lower() if isinstance(host, str) else ""
    loopback = name in ("", "localhost", "::1", "::", "0.0.0.0") or name.startswith(("127.", "::ffff:127."))
    return number if number in DEFAULT_PORTS and loopback else None


def _refuse_default_port(port: int) -> None:
    """Record the connection for the test that is running and refuse it, as a closed port would."""
    DEFAULT_PORT_HITS.append((os.environ.get("PYTEST_CURRENT_TEST", "(outside any test)"), port))
    raise ConnectionRefusedError(
        f"refused by tests/conftest.py: port {port} ({DEFAULT_PORTS[port]}) is the product's default, where "
        "a real Nightshift engine may be running; the test must use a port of its own"
    )


def _install_default_port_guard() -> None:
    """The audit hook sees every `socket.connect` of this interpreter, whoever calls it (httpx, urllib, a
    raw socket, a thread) and no test can remove it (`sys.addaudithook` has no remove, and nothing a test
    undoes reaches it). On Windows asyncio's default loop (Proactor) connects through `_overlapped`, which
    raises no such event, so `loop.create_connection` (what `asyncio.open_connection` and an async httpx
    client call) is checked too. NOT covered: a child process a test starts (it has no conftest; a test that
    starts one must hand it its own ports), `loop.sock_connect` called directly on a raw socket under the
    Proactor loop, a name that resolves to loopback but is not spelled as one (``localhost`` is), and UDP."""
    import asyncio

    def hook(event: str, args: tuple[object, ...]) -> None:
        if event == "socket.connect" and isinstance(args[1], tuple) and len(args[1]) >= 2:
            port = default_port_of(args[1][0], args[1][1])
            if port is not None:
                _refuse_default_port(port)

    sys.addaudithook(hook)
    real = asyncio.BaseEventLoop.create_connection

    async def create_connection(self, protocol_factory, host=None, port=None, *a, **k):  # type: ignore[no-untyped-def]
        found = default_port_of(host, port)
        if found is not None:
            _refuse_default_port(found)
        return await real(self, protocol_factory, host, port, *a, **k)

    asyncio.BaseEventLoop.create_connection = create_connection  # type: ignore[method-assign,assignment]


_install_default_port_guard()


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


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):  # type: ignore[no-untyped-def]
    """A test that connected to a default port (refused by `_refuse_default_port`) FAILS, naming itself, even
    when the product caught the refusal and went on as if the engine were down. Judged at each phase of the
    test, so a connection from its setup or teardown is its failure too."""
    outcome = yield
    report = outcome.get_result()
    mine = DEFAULT_PORT_HITS[_reported[0]:]
    _reported[0] = len(DEFAULT_PORT_HITS)
    if not mine:
        return
    ports = ", ".join(sorted({f"{p} ({DEFAULT_PORTS[p]})" for _t, p in mine}))
    said = (
        f"{item.nodeid} connected to the product's default port {ports} on 127.0.0.1, where a real "
        "Nightshift engine may be running. The connection was refused, so it reached nothing. Give the test "
        "a port of its own through the product's seam: the `closed_port` fixture in a runtime.json "
        "(supervisor_port, or llama.port), a client built with its own URL, or an httpx MockTransport."
    )
    if report.failed:
        report.sections.append(("default ports", said))
        return
    report.outcome = "failed"
    report.longrepr = said


@pytest.fixture
def closed_port() -> int:
    """A loopback port nothing listens on (free a moment ago): the stand-in for an engine that is down."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.hookimpl(specname="pytest_sessionfinish")
def _default_ports_at_session_end(session: pytest.Session) -> None:
    """A connection refused outside any test (while the tests were collected, or after the last one)."""
    late = DEFAULT_PORT_HITS[_reported[0]:]
    if late:
        session.exitstatus = 1
        said = "FAILED: a default port was connected to outside any test: "
        names = ", ".join(f"{t} -> {p}" for t, p in late[:8])
        session.config.get_terminal_writer().line(said + names, red=True)


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
