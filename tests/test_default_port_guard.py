"""tests/conftest.py refuses a connection to the product's default ports (8099, 8100, 8080) on loopback, and
fails the test that made it, by name, even when the product swallowed the refusal.

The guard is exercised the way it is used: in a pytest process of its own (this session's conftest copied
beside the probe tests), so the probes fail there without failing this session.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

PROBES = '''
import asyncio
import socket
import threading
import urllib.request

import httpx
import pytest

import conftest


def swallow(call):
    try:
        call()
    except Exception:       # the product's own habit: an engine that does not answer is "down"
        pass


def test_raw_socket_to_the_engine_port():
    swallow(lambda: socket.create_connection(("127.0.0.1", 8099), timeout=1))


def test_httpx_to_the_dashboard_port():
    swallow(lambda: httpx.get("http://127.0.0.1:8100/status", timeout=1))


def test_urllib_to_localhost_story_model_port():
    swallow(lambda: urllib.request.urlopen("http://localhost:8080/health", timeout=1))


def test_asyncio_to_the_engine_port():
    swallow(lambda: asyncio.run(asyncio.open_connection("127.0.0.1", 8099)))


def test_a_thread_of_the_test_to_the_engine_port():
    def work():
        swallow(lambda: socket.create_connection(("127.0.0.1", 8099), timeout=1))

    t = threading.Thread(target=work)
    t.start()
    t.join()


def test_a_closed_port_of_its_own_is_refused_by_the_system_and_the_test_passes(closed_port):
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", closed_port), timeout=1)


def test_a_stand_in_server_on_its_own_port_is_reachable_and_the_test_passes():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        socket.create_connection(server.getsockname(), timeout=1).close()


def test_default_port_of_names_loopback_on_a_default_port_only():
    assert conftest.default_port_of("192.0.2.1", 8099) is None
    assert conftest.default_port_of("127.0.0.1", 9000) is None
    assert conftest.default_port_of("localhost", "8080") == 8080
    assert conftest.default_port_of("127.0.0.2", 8100) == 8100
    assert conftest.default_port_of("::1", 8099) == 8099
'''


def run_probes(tmp_path: Path) -> str:
    folder = tmp_path / "probe"
    folder.mkdir()
    conftest = (ROOT / "tests" / "conftest.py").read_text(encoding="utf-8")
    (folder / "conftest.py").write_text(conftest, encoding="utf-8")
    (folder / "test_probes.py").write_text(PROBES, encoding="utf-8")
    drop = ("PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST", "NIGHTSHIFT_HOME")
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["PYTHONPATH"] = str(ROOT)
    env["COLUMNS"] = "300"      # the failure text is read whole
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-rA", "-p", "no:cacheprovider",
         f"--basetemp={tmp_path / 'bt'}", "test_probes.py"],
        cwd=folder, env=env, capture_output=True, text=True, timeout=300,
    )
    return done.stdout + done.stderr


def outcome(output: str, name: str) -> str:
    for line in output.splitlines():
        for word in ("PASSED", "FAILED"):
            if line.startswith(f"{word} test_probes.py::{name}"):
                return word
    return "missing"


def test_a_connection_to_a_default_port_fails_the_test_that_made_it_even_when_the_product_swallows_it(
    tmp_path: Path,
) -> None:
    out = run_probes(tmp_path)
    for name, port, label in (
        ("test_raw_socket_to_the_engine_port", 8099, "the engine"),
        ("test_httpx_to_the_dashboard_port", 8100, "the dashboard"),
        ("test_urllib_to_localhost_story_model_port", 8080, "the story model"),
        ("test_asyncio_to_the_engine_port", 8099, "the engine"),
        ("test_a_thread_of_the_test_to_the_engine_port", 8099, "the engine"),
    ):
        assert outcome(out, name) == "FAILED", (name, out)
        said = f"test_probes.py::{name} connected to the product's default port {port} ({label})"
        assert said in out, (name, out)


def test_a_port_of_its_own_is_not_touched_by_the_guard(tmp_path: Path) -> None:
    out = run_probes(tmp_path)
    for name in (
        "test_a_closed_port_of_its_own_is_refused_by_the_system_and_the_test_passes",
        "test_a_stand_in_server_on_its_own_port_is_reachable_and_the_test_passes",
        "test_default_port_of_names_loopback_on_a_default_port_only",
    ):
        assert outcome(out, name) == "PASSED", (name, out)
    assert "5 failed, 3 passed" in out, out
