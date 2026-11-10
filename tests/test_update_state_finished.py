"""D-48 (a): once an interrupted update or rollback is finished, its state says so, whoever finished it.

The block D proof: after the launch script's replay finished an interrupted update, `run/update_state.json`
stayed `failed` with the sentence "open Nightshift from a Desktop shortcut ... rollback"; after setup's Repair
it stayed `running` with the dead worker's pid, and Settings (`/api/update/state`) told the owner to do what
was already done. Every finisher now writes the final state (the launch script's replay, setup's Repair; the
next update writes its own), and a reader that finds an interrupted state with nothing left to finish says
what is true. New names are looked up with `getattr`, so on the base each test fails at an assertion.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from installer.stage2.web import venvrepair
from ns_common.paths import Paths
from ns_dashboard import updater
from ns_supervisor import update, update_job
from tests.test_launchers import _program_home, _record, _stub, _uv, needs_powershell
from tests.test_update_handover import fill, point_home_at, program

#: What the dead worker left while it refreshed the environments (the proof's state, as rc4 writes it).
SYNC_LEFT = (
    "The update to 0.2.0 stopped after 0.2.0's program was put in place, while it was refreshing or checking "
    "the Python environments. To fill every environment: open Nightshift from a Desktop shortcut: it "
    "finishes the Python environments before anything else starts. If that does not work, run "
    "'ns update --rollback --detach'."
)


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=60)
    return proc.pid


def interrupted(home: Path, **extra: Any) -> Path:
    """The state a worker killed in the sync leaves: `running`, its pid gone, `if_stopped_here` set."""
    f = home / "run" / "update_state.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({
        "state": "running", "old": "0.2.0", "new": "", "message": "Refreshing the Python environments.",
        "updated_at": "2026-11-11T00:36:26", "if_stopped_here": SYNC_LEFT, "pid": dead_pid(),
        "log": str(home / "logs" / "update-x.log"), **extra,
    }), encoding="utf-8")
    return f


def state(home: Path) -> dict[str, Any]:
    return json.loads((home / "run" / "update_state.json").read_text(encoding="utf-8-sig"))


# ---------------------------------------------------------------------- finisher 1: the launch script
@needs_powershell
def test_the_launch_scripts_replay_closes_the_interrupted_state(tmp_path: Path) -> None:
    home, launcher = _program_home(tmp_path)
    _record(home, _uv(tmp_path))
    interrupted(home)
    proc = _stub(home, "-Launcher", launcher)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    st = state(home)
    assert st["state"] == "done", f"the finished update still reads {st['state']}: {st['message']}"
    assert st["message"].startswith("An update or rollback was interrupted and has since been finished by "
                                    "Nightshift's start"), st
    assert "Nightshift 0.2.0, its program and Python environments, is in place." in st["message"]
    # a failed state that sends nobody to finish anything (a refusal) is left as it was
    refused = {"state": "failed", "message": "NS-E706: ... Nothing was changed.", "exit_code": 1}
    (home / "run" / "update_state.json").write_text(json.dumps(refused), encoding="utf-8")
    _record(home, _uv(tmp_path))
    assert _stub(home, "-Launcher", launcher).returncode == 0
    assert state(home) == refused


# ----------------------------------------------------------------------- finisher 2: setup's Repair (s04)
def test_setups_repair_closes_the_interrupted_state(tmp_path: Path) -> None:
    from installer.stage2.steps.s04_venvs import S04Venvs
    from installer.stage2.testing import example_answers, make_test_context
    from installer.stage2.web import appcopy
    from ns_common.config import load_manifest
    from tests.test_installer_rerun import staged_bundle

    home = tmp_path / "setup" / "home"
    appcopy.install_app(staged_bundle(tmp_path / "setup" / "zip", "0.1.7"), home)
    ctx = make_test_context(tmp_path / "setup", raw_answers=example_answers(), resolve=True,
                            app_path=home / "app", manifest=load_manifest(home / "app" / "manifest.json"))
    S04Venvs().run(ctx)
    interrupted(ctx.paths.home, record=True)
    venvrepair.record_path(ctx.paths.home).write_text("{torn", encoding="utf-8")     # it waits
    S04Venvs().run(ctx)
    st = state(ctx.paths.home)
    assert st["state"] == "done", f"Repair finished it, the state still reads {st['state']}"
    assert "finished by setup (INSTALL.cmd, Repair)" in st["message"] and "Nightshift 0.1.7" in st["message"]


def test_a_finisher_only_closes_a_state_that_sends_the_owner_to_finish(tmp_path: Path) -> None:
    close = getattr(venvrepair, "close_update_state", None)
    assert close is not None, "nobody closes the state of a finished update"
    app = program(tmp_path)
    home = tmp_path / "home"
    for kept in (
        {"state": "done", "message": "Now on 0.2.0 (was 0.1.0)."},
        {"state": "failed", "message": "NS-E706: These programs ... Nothing was changed."},
        {"state": "failed", "if_stopped_here": update_job.unchanged_sentence("0.1.0"), "message": "x"},
    ):
        f = home / "run" / "update_state.json"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(kept), encoding="utf-8")
        assert close(home, app, "x") is False and state(home) == kept
    for left in ({"venvs_left": ["studio"]}, {"record_left": True}, {"record": True}, {}):
        interrupted(home, **left)
        assert close(home, app, "a test") is True and state(home)["state"] == "done"


# --------------------------------------------------- the worker marks every step taken while a record waits
def test_a_step_taken_while_the_record_waits_is_marked(tmp_path: Path) -> None:
    import os

    paths = Paths(tmp_path / "ns")
    update_job.write_status(paths, "running", "Updating.", pid=os.getpid(), log="l")
    update_job.step(paths, "Putting 0.2.0's program in place.", "s")
    assert "record" not in update_job.read_status(paths)
    app = program(tmp_path)
    venvrepair.write(paths.home, app=app, for_app="f", version="0.1.0", uv="uv", env={},
                     commands=[("studio", ["uv"])], by="update")
    update_job.step(paths, "Refreshing the Python environments.", "s")
    assert update_job.read_status(paths).get("record") is True


# -------------------------------------------------------- the reader: Settings and every other reader
def test_a_reader_says_an_interrupted_update_was_finished_when_nothing_is_left(tmp_path: Path) -> None:
    app, paths = program(tmp_path), Paths(tmp_path / "ns")
    point_home_at(paths, app)
    for name in ("studio", "supervisor"):
        fill(paths, name, "0.1.0")
    interrupted(paths.home, record=True)
    # the record still waits: the sentence that sends the owner to finish it is true
    venvrepair.write(paths.home, app=app, for_app="f", version="0.1.0", uv="uv", env={},
                     commands=[("studio", ["uv"])], by="update", match="version")
    page = updater.read_update_state(paths)
    assert page["state"] == "failed" and "To fill every environment" in str(page["message"])
    # finished by something that did not close the state (an older launch script): no record, venvs match
    venvrepair.record_path(paths.home).unlink()
    for st in (updater.read_update_state(paths), update_job.read_status_checked(paths)):
        assert st["state"] == "done", f"Settings still says: {st['message']}"
        said = str(st["message"])
        assert "interrupted earlier and has been finished since" in said and "To fill" not in said
        assert update.where_now(paths, app) in said
    # an environment that holds another Nightshift: not finished, the stored words stay
    fill(paths, "supervisor", "0.0.9")
    assert updater.read_update_state(paths)["state"] == "failed"
