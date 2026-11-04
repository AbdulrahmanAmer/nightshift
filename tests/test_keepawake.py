"""Gap L2: the PC stays awake during the nightly run and during dashboard jobs."""

from __future__ import annotations

import inspect
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from installer.stage2.web import keepawake as installer_keepawake
from ns_common import keepawake
from ns_studio import daily, jobs

BOTH = keepawake.ES_CONTINUOUS | keepawake.ES_SYSTEM_REQUIRED
NOW = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)


def test_the_flags_and_the_release_after_an_error():
    assert keepawake.ES_CONTINUOUS == 0x80000000 and keepawake.ES_SYSTEM_REQUIRED == 0x00000001
    calls: list[int] = []
    with pytest.raises(RuntimeError):
        with keepawake.keep_awake(lambda flags: (calls.append(flags), 0)[1]):
            raise RuntimeError("the job failed")
    assert calls == [BOTH, keepawake.ES_CONTINUOUS]


def test_not_windows_does_nothing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(keepawake.sys, "platform", "linux")
    assert keepawake._set_execution_state(BOTH) == 0


def test_the_installer_keeps_an_identical_copy():
    """The installer web package may import only the standard library and itself: it keeps a copy."""
    for name in ("ES_CONTINUOUS", "ES_SYSTEM_REQUIRED"):
        assert getattr(keepawake, name) == getattr(installer_keepawake, name)
    for fn in ("_set_execution_state", "keep_awake"):
        mine = inspect.getsource(getattr(keepawake, fn))
        theirs = inspect.getsource(getattr(installer_keepawake, fn))
        assert mine.replace("\r\n", "\n") == theirs.replace("\r\n", "\n"), fn


def _recorder(calls: list[str]):
    def set_state(flags: int) -> int:
        calls.append("hold" if flags & keepawake.ES_SYSTEM_REQUIRED else "release")
        return 0

    return set_state


def _channels(tmp_home: Path) -> None:
    from tests.test_cadence import _channel, _env

    paths, db = _env(tmp_home)
    _channel(paths, db, 9, format="shorts_only", shorts_per_day_max=1)


def test_the_nightly_run_holds_the_pc_awake_for_the_whole_run(tmp_home: Path):
    _channels(tmp_home)
    env = jobs.open_env(light=True)
    calls: list[str] = []

    def awake():
        return keepawake.keep_awake(_recorder(calls))

    def run(ctx):
        calls.append("job")
        return []

    daily.run_daily(env, now=NOW, rng=random.Random(1), run_fn=run, awake=awake)
    assert calls and calls[0] == "hold" and calls[-1] == "release" and "job" in calls
    assert calls.index("job") < len(calls) - 1


def test_the_nightly_run_releases_the_hold_when_the_night_breaks(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio.channels import cadence

    env = jobs.open_env(light=True)
    calls: list[str] = []

    def awake():
        return keepawake.keep_awake(_recorder(calls))

    def broken(*_a, **_k):
        raise RuntimeError("the planner broke")

    monkeypatch.setattr(cadence, "plan_daily", broken)
    with pytest.raises(RuntimeError):
        daily.run_daily(env, now=NOW, rng=random.Random(1), run_fn=lambda ctx: [], awake=awake)
    assert calls == ["hold", "release"]
