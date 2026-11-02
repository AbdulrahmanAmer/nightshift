# ruff: noqa: E501
"""Settings actions that reach other components: update (gap 9), diagnostics (10), run time (13), backup and
restore (17), engine buttons (27), log components (28) and the stored UI preferences (30)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ns_common.paths import Paths
from ns_dashboard import updater
from ns_dashboard.fakes import FakeUpdateAdapter
from tests.test_dashboard_api import err, make_env

pytest.importorskip("fastapi")


# ---------------------------------------------------------------- gap 9: ns update --check / --github


def runner_returning(code: int, out: str = "", error: str = ""):
    calls: list[list[str]] = []

    def run(cmd: list[str], timeout_s: int) -> tuple[int, str, str]:
        calls.append(cmd)
        return code, out, error

    return run, calls


def test_update_check_calls_ns_update_check_and_reads_the_answer(tmp_home: Path):
    run, calls = runner_returning(0, '{"update_available": true, "current": "0.1.0", "latest": "0.2.0"}\n')
    res = updater.RealUpdateAdapter(Paths.from_env(), run).check()
    assert calls[0][-2:] == ["update", "--check"]
    assert res["state"] == "available" and res["up_to_date"] is False and res["latest"] == "0.2.0"
    run2, calls2 = runner_returning(0, "Nightshift 0.2.0 is up to date.\n")
    same = updater.RealUpdateAdapter(Paths.from_env(), run2).check()
    assert calls2[0][-2:] == ["update", "--check"] and same["state"] == "up_to_date" and same["up_to_date"] is True


def test_missing_update_command_is_a_clear_state_not_an_error(tmp_home: Path):
    # what `ns update --check` does today: the command only takes a zip or --rollback
    run, calls = runner_returning(2, "", "give either a zip file or --rollback")
    adapter = updater.RealUpdateAdapter(Paths.from_env(), run)
    res = adapter.check()
    assert res["state"] == "unavailable" and "Update shortcut" in res["message"] and res["up_to_date"] is False
    applied = adapter.apply()
    assert calls[-1][-2:] == ["update", "--github"] and applied["state"] == "unavailable"
    gone = updater.RealUpdateAdapter(Paths.from_env(), runner_returning(127, "", "[WinError 2]")[0]).check()
    assert gone["state"] == "unavailable"
    failed = updater.RealUpdateAdapter(Paths.from_env(), runner_returning(1, "", "network down")[0]).check()
    assert failed["state"] == "error" and failed["message"] == "network down"


def test_update_routes_report_the_unavailable_state(tmp_home: Path):
    client, state = make_env(tmp_home)
    assert isinstance(state.adapters.update, FakeUpdateAdapter)
    assert client.get("/api/update/check").json()["state"] == "available"
    state.adapters.update.missing = True
    chk = client.get("/api/update/check").json()
    assert chk["state"] == "unavailable" and chk["message"]
    r = client.post("/api/update/apply")
    assert r.status_code == 501 and err(r)["code"] == "update_unavailable"
