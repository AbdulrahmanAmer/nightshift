"""C1: the setup window's state machine: phase derivation, action preconditions, the store's long poll."""

import threading
import time

import pytest

from installer.stage2.web import machine
from installer.stage2.web.model import STEP_ORDER, STEP_PHASE
from installer.stage2.web.store import SetupStore


def steps(done: int, **status_of: str) -> list[dict]:
    """Every step, the first ``done`` of them finished; ``status_of`` overrides single steps."""
    out = []
    for i, sid in enumerate(STEP_ORDER):
        status = "done" if i < done else "pending"
        out.append({"id": sid, "status": status_of.get(sid, status)})
    return out


def phase(done: int = 0, **kw) -> str:
    args = {"ui_step": None, "answers_accepted": True, "started": True, "run": "running", "failure": None}
    args.update(kw)
    return machine.derive_phase(steps(done), **args)


@pytest.mark.parametrize("n", range(len(STEP_ORDER)))
def test_phase_follows_the_first_unfinished_step(n: int):
    assert phase(n) == STEP_PHASE[STEP_ORDER[n]]


def test_every_step_done_or_skipped_is_done():
    assert phase(len(STEP_ORDER)) == "done"
    all_skipped = [{"id": s, "status": "skipped"} for s in STEP_ORDER]
    assert machine.derive_phase(all_skipped, ui_step=None, answers_accepted=True, started=True,
                                run="finished", failure=None) == "done"
    with_warning = steps(len(STEP_ORDER), v01_samples="warning")
    assert machine.derive_phase(with_warning, ui_step=None, answers_accepted=True, started=True,
                                run="finished", failure=None) == "done"


def test_the_phases_group_the_steps_as_the_spec_says():
    assert STEP_PHASE["s03_download_models"] == "downloading" and STEP_PHASE["b01_tools"] == "downloading"
    assert STEP_PHASE["s04_venvs"] == "installing" and STEP_PHASE["s07_configs"] == "installing"
    assert STEP_PHASE["s08_smoke"] == "verifying" and STEP_PHASE["s10_summary"] == "verifying"


@pytest.mark.parametrize(
    ("ui_step", "accepted", "expected"),
    [("welcome", False, "checks"), ("checks", False, "checks"), ("choices", False, "choices"),
     ("welcome", True, "choices"), ("choices", True, "choices")],
)
def test_before_start_the_phase_follows_the_screen_and_the_answers(ui_step, accepted, expected):
    assert machine.derive_phase(steps(0), ui_step=ui_step, answers_accepted=accepted, started=False,
                                run="idle", failure=None) == expected


def test_a_failure_only_wins_while_the_engine_waits_for_a_decision():
    failure = {"step": "s03_download_models", "canSkip": False}
    assert phase(5, run="waiting", failure=failure) == "failed"
    assert phase(5, run="running", failure=failure) == "downloading"
    assert phase(5, run="stopped") == "downloading"


def state(**over) -> dict:
    base = {"run": "idle", "phase": "choices", "answers": {"homePath": "D:\\Nightshift"}, "blocking": False,
            "failure": None, "checking": False, "result": None,
            "checks": [{"id": "longpaths", "status": "ok"}]}
    base.update(over)
    return base


@pytest.mark.parametrize(
    ("action", "ok_state", "bad_state"),
    [
        ("start", state(), state(answers=None)),
        ("start", state(run="stopped"), state(blocking=True)),
        ("start", state(), state(run="running")),
        ("pause", state(run="running", phase="downloading"), state(run="running", phase="installing")),
        ("resume", state(run="paused"), state(run="running")),
        ("resume", state(run="stopped"), state(run="idle")),
        ("retry", state(run="waiting"), state(run="running")),
        ("skip", state(run="waiting", failure={"canSkip": True}),
         state(run="waiting", failure={"canSkip": False})),
        ("cancel", state(run="paused"), state(run="idle")),
        ("recheck", state(), state(checking=True)),
        ("recheck", state(run="stopped"), state(run="running")),
        ("open-dashboard", state(phase="done", run="finished"), state(phase="verifying")),
        ("close-setup", state(run="finished"), state(run="running")),
        ("close-setup", state(run="idle"), state(run="waiting")),
        ("elevate-longpaths", state(checks=[{"id": "longpaths", "status": "warn"}]), state()),
    ],
)
def test_action_preconditions(action, ok_state, bad_state):
    assert machine.check(action, ok_state) is None
    assert machine.check(action, bad_state)       # a plain sentence the window can show


def test_diagnostics_is_always_allowed_and_unknown_actions_are_not():
    for run in ("idle", "running", "waiting", "finished"):
        assert machine.check("diagnostics", state(run=run)) is None
    assert machine.check("explode", state())


def test_reveal_only_opens_paths_the_state_lists():
    result = {"outbox": "D:\\Nightshift\\outbox", "home": "D:\\Nightshift", "diagnosticsZip": None}
    s = state(phase="done", run="finished", result=result)
    assert machine.check("reveal", s, "D:\\Nightshift\\outbox") is None
    assert machine.check("reveal", s, "D:\\Nightshift\\logs") is None        # the logs folder of the install
    assert machine.check("reveal", s, "C:\\Windows")
    assert machine.check("reveal", s, None)


def test_finalize_derives_blocking_phase_and_clears_the_screen_after_start():
    s = {"checks": [{"blocking": False}, {"blocking": True}], "steps": steps(0), "run": "idle",
         "uiStep": "checks", "answers": None, "failure": None}
    machine.finalize(s, started=False)
    assert s["blocking"] is True and s["phase"] == "checks" and s["uiStep"] == "checks"
    s["answers"] = {"x": 1}
    s["checks"] = [{"blocking": False}]
    machine.finalize(s, started=True)
    assert s["blocking"] is False and s["uiStep"] is None and s["phase"] == "downloading"


def test_store_bumps_rev_and_wakes_a_waiting_poller():
    store = SetupStore({"rev": 1, "n": 0}, None)
    got: list[dict] = []
    t = threading.Thread(target=lambda: got.append(store.wait_newer(1, 5.0)))
    t.start()
    time.sleep(0.2)
    assert not got
    store.update(lambda s: s.__setitem__("n", 7))
    t.join(timeout=5)
    assert got and got[0]["n"] == 7 and got[0]["rev"] == 2 and "updatedAt" in got[0]


def test_store_long_poll_returns_the_state_after_its_timeout():
    store = SetupStore({"rev": 3}, None)
    t0 = time.monotonic()
    state_now = store.wait_newer(3, 0.3)
    assert 0.2 <= time.monotonic() - t0 < 3 and state_now["rev"] == 3
    assert store.wait_newer(None, 5.0)["rev"] == 3        # no cursor: answer at once
    assert store.wait_newer(1, 5.0)["rev"] == 3           # already newer: answer at once


def test_store_snapshots_are_copies():
    store = SetupStore({"rev": 1, "items": [1]}, None)
    snap = store.snapshot()
    snap["items"].append(2)
    assert store.snapshot()["items"] == [1]
