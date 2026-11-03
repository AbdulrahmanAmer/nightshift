"""C1: stage 1's health ping against a live setup server, run under Windows PowerShell 5.1.

The v0.1.1 first install stopped with "the setup program did not start" because `Invoke-ServerPing` passed the
session cookie through `-Headers`, and Windows PowerShell 5.1 silently drops that restricted header. These tests
pin both halves of the fix: the server accepts the token as an `X-NS-Token` header, and the real `Invoke-ServerPing`
function from `stage1.ps1` answers True under `powershell.exe` (5.1), while the old form answers False.
"""

from __future__ import annotations

import http.client
import json
import re
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from installer.stage2.web.server import SetupServer
from installer.stage2.web.store import SetupStore

ROOT = Path(__file__).resolve().parents[1]
STAGE1 = ROOT / "installer" / "stage1.ps1"
POWERSHELL = shutil.which("powershell")  # Windows PowerShell 5.1, the one every stock PC has


class _Stub:
    version = "9.9.9"

    def __init__(self) -> None:
        self.store = SetupStore({"rev": 1, "phase": "checks"}, None)
        self.shutdown = threading.Event()

    def wait_state(self, since, timeout=15.0):
        return self.store.wait_newer(since, timeout)

    def file_path(self, name):
        return None


@pytest.fixture
def server(tmp_path: Path):
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<!doctype html><title>setup</title>", encoding="utf-8")
    srv = SetupServer(_Stub(), static_dir=static, version="9.9.9", long_poll_s=0.5, on_open_window=lambda: None)
    srv.start()
    yield srv
    srv.stop()


def _get(port: int, headers: dict[str, str]) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/api/ping", headers={"Host": f"127.0.0.1:{port}", **headers})
    status = conn.getresponse().status
    conn.close()
    return status


def test_the_token_header_is_accepted_and_a_wrong_one_is_not(server: SetupServer):
    assert _get(server.port, {"X-NS-Token": server.token}) == 200
    assert _get(server.port, {"X-NS-Token": "nope"}) == 401
    assert _get(server.port, {}) == 401


def _function_source(name: str) -> str:
    text = STAGE1.read_text(encoding="utf-8")
    start = text.index(f"function {name} ")
    nxt = re.compile(r"^function |^# -{20,}", re.MULTILINE)
    m = nxt.search(text, start + 1)
    return text[start : m.start() if m else len(text)]


def _run_ps(script: str) -> str:
    assert POWERSHELL is not None
    out = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    return (out.stdout or "").strip() + ("\n" + out.stderr.strip() if out.stderr.strip() else "")


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell 5.1 is not on PATH")
def test_the_real_invoke_serverping_answers_true_under_windows_powershell_5_1(server: SetupServer):
    info = json.dumps({"port": server.port, "token": server.token})
    script = (
        _function_source("Invoke-ServerPing")
        + f"\n$Info = ('{info}' | ConvertFrom-Json)\n"
        + "Write-Output ('PSV=' + $PSVersionTable.PSVersion.Major)\n"
        + "Write-Output ('PING=' + [string](Invoke-ServerPing -Info $Info -TimeoutSec 5))\n"
    )
    out = _run_ps(script)
    assert "PSV=5" in out, out  # the shell under test is Windows PowerShell 5.1
    assert "PING=True" in out, out


@pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell 5.1 is not on PATH")
def test_the_old_cookie_header_form_is_dropped_by_windows_powershell_5_1(server: SetupServer):
    # Pins the regression: a Cookie passed through -Headers never reaches the server on 5.1 (401 -> False).
    script = (
        f"$uri = 'http://127.0.0.1:{server.port}/api/ping'\n"
        f"$headers = @{{ Cookie = ('ns_setup={server.token}') }}\n"
        "try { $r = Invoke-RestMethod -Uri $uri -Headers $headers -TimeoutSec 5 -UseBasicParsing; "
        "Write-Output ('OLD=' + [string][bool]$r.ok) } catch { Write-Output 'OLD=False' }\n"
    )
    out = _run_ps(script)
    assert "OLD=False" in out, out
