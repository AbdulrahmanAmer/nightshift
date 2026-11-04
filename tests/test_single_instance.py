"""One owner per lock: the engine (P8) and the lock file helper both start from."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ns_common.paths import Paths
from ns_common.singleton import AlreadyRunning, FileLock, read_owner, single_instance


def test_a_second_lock_on_the_same_file_is_refused_until_the_first_lets_go(tmp_path: Path) -> None:
    path = tmp_path / "run" / "x.lock"
    first = FileLock(path)
    first.acquire()
    assert first.held and read_owner(path) == os.getpid()
    with pytest.raises(AlreadyRunning) as ei:
        FileLock(path).acquire()
    assert ei.value.owner == os.getpid() and ei.value.path == path
    first.release()
    assert not first.held
    again = FileLock(path)
    again.acquire()                                # free again: the lock died with its owner's release
    again.release()
    first.release()                                # releasing twice is harmless


def test_the_context_manager_holds_for_the_block_and_releases_on_an_error(tmp_path: Path) -> None:
    path = tmp_path / "y.lock"
    with pytest.raises(RuntimeError), single_instance(path):
        with pytest.raises(AlreadyRunning):
            FileLock(path).acquire()
        raise RuntimeError("boom")
    with single_instance(path):
        pass


# ---- the engine ------------------------------------------------------------------------------------
@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path / "home"


def test_a_second_engine_exits_cleanly_and_leaves_the_first_ones_state_file_alone(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from ns_supervisor import app

    state = home / "run" / "supervisor.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps({"pid": 4242, "port": 8099}), encoding="utf-8")
    served: list[int] = []
    monkeypatch.setattr(app, "_serve", lambda args, paths: served.append(1) or 5)
    first = app.engine_lock(Paths(home))
    first.acquire()                                          # the first engine is up
    try:
        assert app.main(["--headless"]) == 0
    finally:
        first.release()
    assert served == [] and json.loads(state.read_text(encoding="utf-8"))["pid"] == 4242
    err = capsys.readouterr().err
    assert "already running" in err and str(os.getpid()) in err


def test_the_first_engine_serves_and_gives_the_lock_back(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ns_supervisor import app

    held: list[bool] = []

    def serve(args: object, paths: Paths) -> int:
        with pytest.raises(AlreadyRunning):
            app.engine_lock(paths).acquire()                # a second start now would be refused
        held.append(True)
        return 7

    monkeypatch.setattr(app, "_serve", serve)
    assert app.main(["--headless"]) == 7 and held == [True]
    probe = app.engine_lock(Paths(home))
    probe.acquire()                                          # released: the next start may take it
    probe.release()


def test_an_engine_that_dies_leaves_no_stale_lock(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ns_supervisor import app

    def crash(args: object, paths: Paths) -> int:
        raise RuntimeError("engine crashed")

    monkeypatch.setattr(app, "_serve", crash)
    with pytest.raises(RuntimeError):
        app.main(["--headless"])
    probe = app.engine_lock(Paths(home))
    probe.acquire()
    probe.release()
