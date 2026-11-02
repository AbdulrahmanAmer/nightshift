"""C2: the rollback after a failed update waits for the verify engine and retries its renames.

`ns verify --quick` starts a detached engine with the new tree open. When verify fails, `_restore` used to
rename the folder at once and raise WinError 32 inside the `except`, which replaced the real error and left
the install on the new, failing tree.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_supervisor import cli, client, control, update
from tests.test_ns_update import Env, make_app, make_zip

win_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows only")


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "RENAME_PAUSE_S", 0.05)


def old_version(app: Path) -> str:
    return (app / "VERSION").read_text(encoding="utf-8").strip()


def test_the_rollback_renames_retry_while_the_new_tree_is_still_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, z = make_app(tmp_path), make_zip(tmp_path)
    real = Path.rename
    state = {"armed": False, "fails": 4}

    class VerifyFails(Env):
        def verify(self, app: Path) -> bool:
            state["armed"] = True         # from here on the new tree is "held" by a lingering engine
            return False

    def held(self: Path, target: Path) -> Path:
        if state["armed"] and self == app and state["fails"] > 0:
            state["fails"] -= 1
            raise PermissionError(32, "being used by another process")
        return real(self, target)

    monkeypatch.setattr(Path, "rename", held)
    with pytest.raises(NsError) as ei:
        update.update_app(z, app, VerifyFails().env(), paths=Paths(tmp_path / "ns"))
    assert state["fails"] == 0                                  # it did retry
    assert ei.value.code == "NS-E702" and "ns verify --quick failed" in ei.value.detail
    assert old_version(app) == "0.1.0"                          # the previous version is back
    assert not (tmp_path / "NightShift.prev").exists()


@win_only
def test_a_real_open_handle_in_the_new_tree_delays_the_rollback_but_does_not_break_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update, "RENAME_TRIES", 200)
    app, z = make_app(tmp_path), make_zip(tmp_path)
    held: list[Any] = []
    timers: list[threading.Timer] = []

    class VerifyFails(Env):
        def verify(self, app: Path) -> bool:
            fh = (app / "ns_common" / "x.py").open("rb")        # what a lingering llama-server holds
            held.append(fh)
            timers.append(threading.Timer(0.6, fh.close))
            timers[0].start()
            return False

    with pytest.raises(NsError) as ei:
        update.update_app(z, app, VerifyFails().env(), paths=Paths(tmp_path / "ns"))
    assert ei.value.code == "NS-E702" and "ns verify --quick failed" in ei.value.detail
    timers[0].join()
    assert held and held[0].closed
    assert old_version(app) == "0.1.0"
    assert (app / "ns_common" / "x.py").read_text(encoding="utf-8") == "OLD"


def test_the_rollback_waits_for_the_verify_engine_before_it_renames(tmp_path: Path) -> None:
    app, z = make_app(tmp_path), make_zip(tmp_path)
    order: list[str] = []

    class VerifyFails(Env):
        def verify(self, app: Path) -> bool:
            order.append("verify")
            return False

    env = VerifyFails().env()
    env.settle = lambda: order.append(f"settle:{app.name}:{old_version(app)}")
    with pytest.raises(NsError):
        update.update_app(z, app, env, paths=Paths(tmp_path / "ns"))
    # settle ran after verify and while the new tree was still in place
    assert order == ["verify", "settle:NightShift:0.2.0"]


def test_a_rollback_that_cannot_rename_keeps_the_real_error_and_says_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, z = make_app(tmp_path), make_zip(tmp_path)
    real = Path.rename
    armed = {"on": False}

    class VerifyFails(Env):
        def verify(self, app: Path) -> bool:
            armed["on"] = True
            return False

    def locked(self: Path, target: Path) -> Path:
        if armed["on"] and self == app:
            raise PermissionError(32, "being used by another process")
        return real(self, target)

    monkeypatch.setattr(Path, "rename", locked)
    monkeypatch.setattr(update, "RENAME_TRIES", 2)
    with pytest.raises(NsError) as ei:
        update.update_app(z, app, VerifyFails().env(), paths=Paths(tmp_path / "ns"))
    assert ei.value.code == "NS-E702"
    assert "ns verify --quick failed" in ei.value.detail         # not replaced by the PermissionError
    assert "ns update --rollback" in ei.value.detail


def test_a_manual_rollback_retries_its_renames(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app = make_app(tmp_path)
    prev = tmp_path / "NightShift.prev"
    prev.mkdir()
    (prev / "VERSION").write_text("0.0.9\n", encoding="utf-8")
    real = Path.rename
    fails = {"n": 3}

    def flaky(self: Path, target: Path) -> Path:
        if self == prev and fails["n"] > 0:
            fails["n"] -= 1
            raise PermissionError(32, "in use")
        return real(self, target)

    monkeypatch.setattr(Path, "rename", flaky)
    res = update.rollback_app(app, Env().env(), paths=Paths(tmp_path / "ns"))
    assert fails["n"] == 0 and res.new_version == "0.0.9"


# --------------------------------------------------------------------------- the engine `verify` starts
class FakeEngine:
    """`/status` answers for `answers` more calls, then not; `shutdown` is recorded."""

    def __init__(self, answers: int) -> None:
        self.answers = answers
        self.polls = 0
        self.shut = 0

    def is_up(self) -> bool:
        self.polls += 1
        if self.answers > 0:
            self.answers -= 1
            return True
        return False

    def shutdown(self, grace: float = 30) -> None:
        self.shut += 1


def test_wait_down_polls_until_the_engine_is_gone_and_gives_up_after_the_limit() -> None:
    slept: list[float] = []
    eng = FakeEngine(3)
    assert control.wait_down(eng, 10, sleep=slept.append) is True  # type: ignore[arg-type]
    assert len(slept) == 3
    now = {"t": 0.0}

    def tick(s: float) -> None:
        now["t"] += s

    stuck = FakeEngine(10**6)
    assert control.wait_down(stuck, 2, sleep=tick, clock=lambda: now["t"]) is False  # type: ignore[arg-type]


def test_the_verify_engine_is_waited_for_after_it_is_told_to_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = FakeEngine(4)                  # is_up: the check in _stop_started_engine, then 3 waits
    cli._stop_started_engine(eng)  # type: ignore[arg-type]
    assert eng.shut == 1 and eng.answers == 0 and eng.polls >= 5   # it polled until /status was down


def test_the_engine_is_started_in_the_install_home_not_where_the_caller_stands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    seen: dict[str, Any] = {}
    monkeypatch.setattr(client.subprocess, "Popen", lambda argv, **kw: seen.update(kw))
    client.spawn_supervisor(Paths(home))
    assert seen["cwd"] == str(home)
