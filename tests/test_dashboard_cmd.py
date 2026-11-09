"""`studio dashboard [--port 8100] [--open]` (spec 15.9): engine up, dashboard ready, URL printed."""

import http.server
import threading
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ns_common.errors import NsError
from ns_studio import dashboard_cmd
from ns_studio.cli import app
from ns_supervisor.services import DASHBOARD_PORT

runner = CliRunner()


class FakeEngine:
    """Stands in for the supervisor client: records warmups and answers with a service state."""

    def __init__(self, state: str = "ready") -> None:
        self.state = state
        self.warmed: list[str] = []

    def warmup(self, service: str) -> str:
        self.warmed.append(service)
        return self.state


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, tmp_home: Path):
    """A fake engine and a recording health probe, so no process or socket is touched."""
    engine = FakeEngine()
    probes: list[int] = []
    opened: list[str] = []
    monkeypatch.setattr(dashboard_cmd, "open_engine", lambda paths: engine)
    monkeypatch.setattr(dashboard_cmd, "probe_health", lambda port, wait_s=30.0: probes.append(port) or True)
    monkeypatch.setattr(dashboard_cmd, "open_in_browser", lambda url: opened.append(url) or True)
    return engine, probes, opened


def test_default_port_matches_the_supervisor_service():
    assert dashboard_cmd.DEFAULT_PORT == DASHBOARD_PORT


def test_prints_the_url_and_starts_the_dashboard_service(wired):
    engine, probes, opened = wired
    r = runner.invoke(app, ["dashboard"])
    assert r.exit_code == 0, r.output
    assert "http://127.0.0.1:8100" in r.output
    assert engine.warmed == ["dashboard"]
    assert probes == [8100]
    assert opened == []  # no browser without --open


def test_open_starts_the_default_browser_on_the_url(wired):
    _engine, _probes, opened = wired
    r = runner.invoke(app, ["dashboard", "--open"])
    assert r.exit_code == 0, r.output
    assert opened == ["http://127.0.0.1:8100"]


def test_port_changes_the_reported_and_probed_url_only(wired):
    engine, probes, opened = wired
    r = runner.invoke(app, ["dashboard", "--port", "9123", "--open"])
    assert r.exit_code == 0, r.output
    assert "http://127.0.0.1:9123" in r.output
    assert probes == [9123]
    assert opened == ["http://127.0.0.1:9123"]
    assert engine.warmed == ["dashboard"]


def test_help_says_the_port_is_fixed():
    r = runner.invoke(app, ["dashboard", "--help"])
    assert r.exit_code == 0
    assert "8100" in r.output
    assert "DASHBOARD_PORT" in r.output.replace("\n", " ").replace("  ", " ")


def test_engine_that_cannot_start_gives_a_plain_message_and_exit_1(
    monkeypatch: pytest.MonkeyPatch, tmp_home: Path
):
    def boom(paths):
        raise NsError("NS-E202", "supervisor did not answer at http://127.0.0.1:8099 within 30s")

    monkeypatch.setattr(dashboard_cmd, "open_engine", boom)
    monkeypatch.setattr(dashboard_cmd, "open_in_browser", lambda url: pytest.fail("must not open"))
    r = runner.invoke(app, ["dashboard", "--open"])
    assert r.exit_code == 1
    assert "NS-E202" in r.output
    assert "Traceback" not in r.output
    assert "http://127.0.0.1:8100" not in r.output  # no URL for a dashboard that is not there


def test_dashboard_that_never_answers_exits_1_with_a_hint(monkeypatch: pytest.MonkeyPatch, tmp_home: Path):
    monkeypatch.setattr(dashboard_cmd, "open_engine", lambda paths: FakeEngine("failed"))
    monkeypatch.setattr(dashboard_cmd, "probe_health", lambda port, wait_s=30.0: False)
    monkeypatch.setattr(dashboard_cmd, "open_in_browser", lambda url: pytest.fail("must not open"))
    r = runner.invoke(app, ["dashboard"])
    assert r.exit_code == 1
    assert "dashboard" in r.output.lower()
    assert "8100" in r.output


def test_a_failed_warmup_call_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch, tmp_home: Path):
    class Broken:
        def warmup(self, service: str) -> str:
            raise NsError("NS-E202", "supervisor unreachable at http://127.0.0.1:8099")

    monkeypatch.setattr(dashboard_cmd, "open_engine", lambda paths: Broken())
    r = runner.invoke(app, ["dashboard"])
    assert r.exit_code == 1
    assert "NS-E202" in r.output


def test_browser_failure_is_a_note_not_an_error(wired, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(dashboard_cmd, "open_in_browser", lambda url: False)
    r = runner.invoke(app, ["dashboard", "--open"])
    assert r.exit_code == 0
    assert "http://127.0.0.1:8100" in r.output
    assert "Open that address" in r.output


def test_open_in_browser_uses_startfile(monkeypatch: pytest.MonkeyPatch):
    seen: list[str] = []
    monkeypatch.setattr(dashboard_cmd.os, "startfile", seen.append, raising=False)
    assert dashboard_cmd.open_in_browser("http://127.0.0.1:8100") is True
    assert seen == ["http://127.0.0.1:8100"]


def test_open_in_browser_without_startfile_is_false(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delattr(dashboard_cmd.os, "startfile", raising=False)
    assert dashboard_cmd.open_in_browser("http://127.0.0.1:8100") is False


class _Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = b'{"ok": true, "service": "dashboard"}' if self.path == "/api/health" else b"{}"
        self.send_response(200 if self.path == "/api/health" else 404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass


def test_probe_health_reads_api_health_and_gives_up():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert dashboard_cmd.probe_health(server.server_address[1], wait_s=5) is True
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    # the server is closed now: nothing answers on that port
    assert dashboard_cmd.probe_health(server.server_address[1], wait_s=0.5) is False
