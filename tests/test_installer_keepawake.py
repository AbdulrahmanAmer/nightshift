"""C1: setup holds the PC awake while the pipeline thread runs and lets go when it ends."""

import time

from installer.stage2.web import keepawake
from installer.stage2.web.model import ENGINE_STEPS
from tests.test_installer_web_controller import Setup, finish_script

BOTH = keepawake.ES_CONTINUOUS | keepawake.ES_SYSTEM_REQUIRED


def test_the_flags_are_the_documented_values():
    assert keepawake.ES_CONTINUOUS == 0x80000000 and keepawake.ES_SYSTEM_REQUIRED == 0x00000001


def test_keep_awake_sets_then_resets_even_when_the_block_raises():
    calls: list[int] = []
    try:
        with keepawake.keep_awake(lambda flags: (calls.append(flags), 0)[1]):
            assert calls == [BOTH]
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert calls == [BOTH, keepawake.ES_CONTINUOUS]


def test_the_real_call_does_nothing_off_windows(monkeypatch):
    monkeypatch.setattr(keepawake.sys, "platform", "linux")
    assert keepawake._set_execution_state(BOTH) == 0


def wait_for(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_a_run_holds_the_pc_awake_and_releases_it_when_it_finishes(tmp_path, monkeypatch):
    s = Setup(tmp_path, monkeypatch, script=finish_script)
    s.accept()
    s.ctl.do_action("start")
    s.until(lambda st: st["run"] == "finished")
    assert wait_for(lambda: s.fx.awake[-1:] == [keepawake.ES_CONTINUOUS])
    assert s.fx.awake == [BOTH, keepawake.ES_CONTINUOUS]


def test_a_failed_run_also_releases_it(tmp_path, monkeypatch):
    def script(e):
        assert e.wait_command("keys", 10)
        e.emit({"type": "hello", "pid": 1, "version": "0.1.0", "resumed": False})
        e.emit({"type": "step", "id": ENGINE_STEPS[0], "status": "running", "title": "x", "detail": None})
        e.exit(1)

    s = Setup(tmp_path, monkeypatch, script=script)
    s.accept()
    s.ctl.do_action("start")
    s.until(lambda st: st["run"] == "waiting")
    assert wait_for(lambda: s.fx.awake[-1:] == [keepawake.ES_CONTINUOUS])
    assert s.fx.awake[0] == BOTH
