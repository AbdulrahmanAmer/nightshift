"""C2: an open dashboard (window host or browser) is activity, so the 30-minute idle exit leaves it alone."""

from __future__ import annotations

import asyncio
import json
import socket
import sys
from pathlib import Path

import pytest

from ns_common.paths import Paths
from ns_supervisor import presence
from tests.test_supervisor_core import make, running, until

NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:8100           0.0.0.0:0              LISTENING       4120
  TCP    127.0.0.1:8100         127.0.0.1:53211        ESTABLISHED     4120
  TCP    127.0.0.1:53211        127.0.0.1:8100         ESTABLISHED     9876
  TCP    127.0.0.1:8099         127.0.0.1:53300        ESTABLISHED     4000
  TCP    127.0.0.1:8100         127.0.0.1:53290        TIME_WAIT       0
"""


def test_only_established_connections_to_the_dashboard_port_count() -> None:
    assert presence.count_established(NETSTAT, 8100) == 1       # the browser's end (local port 53211) is not ours
    assert presence.count_established(NETSTAT, 8099) == 1
    assert presence.count_established(NETSTAT, 9999) == 0
    assert presence.count_established("", 8100) == 0


def write_host_file(paths: Paths, pid: int) -> None:
    f = paths.home / "run" / presence.HOST_RUN_FILE
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"hwnd": 1234, "pid": pid}), encoding="utf-8")


def test_a_running_window_host_counts_and_a_stale_file_does_not(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")
    assert presence.host_window_open(paths, image=lambda pid: "powershell.exe") is False      # no file
    write_host_file(paths, 4242)
    assert presence.host_window_open(paths, image=lambda pid: "powershell.exe") is True
    assert presence.host_window_open(paths, image=lambda pid: None) is False                  # process gone
    assert presence.host_window_open(paths, image=lambda pid: "notepad.exe") is False         # pid was reused
    (paths.home / "run" / presence.HOST_RUN_FILE).write_text("{broken", encoding="utf-8")
    assert presence.host_window_open(paths, image=lambda pid: "powershell.exe") is False


@pytest.mark.skipif(sys.platform != "win32", reason="real netstat")
def test_a_real_connection_to_a_port_is_seen_by_netstat() -> None:
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    assert presence.browser_connected(port) is False                 # listening is not a viewer
    client = socket.create_connection(("127.0.0.1", port))
    conn, _ = server.accept()
    try:
        assert presence.browser_connected(port) is True
    finally:
        client.close()
        conn.close()
        server.close()


def test_idle_exit_waits_while_the_dashboard_is_open_and_comes_after_it_closes(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, auto_exit_idle_s=0.4, idle_unload_s=60)
    viewing = {"open": True}
    asked: list[int] = []
    sup.in_use = lambda: asked.append(1) or viewing["open"]

    async def main() -> None:
        async with running(sup):
            assert await sup.warmup("dashboard") == "ready"
            await asyncio.sleep(1.6)                     # four idle periods with the page open
            assert not sup.exit_requested.is_set() and asked
            viewing["open"] = False                      # the window is closed
            assert await until(sup.exit_requested.is_set, 10)

    asyncio.run(main())


def test_without_a_dashboard_service_the_probe_is_not_asked(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, auto_exit_idle_s=0.4, idle_unload_s=60)
    sup.in_use = lambda: pytest.fail("no dashboard service: nobody can be looking at it")

    async def main() -> None:
        async with running(sup):
            assert await until(sup.exit_requested.is_set, 10)

    asyncio.run(main())


def test_a_failing_probe_does_not_keep_the_engine_alive_or_break_the_monitor(tmp_path: Path) -> None:
    sup, _, _ = make(tmp_path, auto_exit_idle_s=0.4, idle_unload_s=60)

    def broken() -> bool:
        raise OSError("netstat missing")

    sup.in_use = broken

    async def main() -> None:
        async with running(sup):
            assert await sup.warmup("dashboard") == "ready"
            assert await until(sup.exit_requested.is_set, 10)

    asyncio.run(main())
