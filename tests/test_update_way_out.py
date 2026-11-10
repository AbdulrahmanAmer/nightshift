"""D-39a, review round 1: the owner is told, in words that work with the program that will run them.

1. Every venv is an editable install of `<app>`, so after a rollback the `ns` the owner types is the OLDER
   program's code. From 0.1.14 to 0.1.15-rc3 its terminal `ns update` is D-39 itself. So every way out names a
   command the program in place has and that does not take that route (`--detach`), or INSTALL.cmd.
2. A run that ends with Nightshift not running (an environment left unfinished) is shown by nobody on the
   Settings route: the worker opens the progress window itself and leaves a morning note, and the page shows
   a failed state when it loads.
3. `--wait` gives a script the real result, where waiting is sound; `--detach` names a script that waits.
"""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from ns_common.paths import Paths
from ns_dashboard import updater
from ns_supervisor import cli, notify, procs, update, update_job
from tests.test_ns_update import Env, make_zip
from tests.test_update_handover import (
    OLD_CLI,
    Died,
    Hand,
    Recorder,
    Uv,
    env_of,
    fill,
    invoke,
    needs_powershell,
    outcome,
    point_home_at,
    program,
    raised,
    updated,
)

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()
COMMAND = re.compile(r"'(ns update[^']*)'")


def commands(message: str) -> list[str]:
    """Every `ns update ...` command a message tells the owner to type."""
    return COMMAND.findall(message)


def held(paths: Paths) -> Any:
    for name in ("studio", "supervisor"):
        fill(paths, name, "0.2.0")
    return open(paths.venvs / "supervisor" / "Scripts" / "ns.exe", "rb")  # noqa: SIM115 - os error 32


def unfinished_rollback(tmp_path: Path, cli_text: str | None) -> tuple[str, Paths]:
    app, paths = updated(tmp_path, cli_text)
    handle = held(paths)
    try:
        err = raised(lambda: update.rollback_app(app, env_of(Env(), sync=Uv(paths).sync), paths=paths))
    finally:
        handle.close()
    assert err is not None and err.data.get("venvs_left") == ["supervisor"]
    return err.detail, paths


def unfinished_update(tmp_path: Path, cli_text: str | None) -> tuple[str, Paths]:
    app, paths = program(tmp_path, cli_text), Paths(tmp_path / "ns")
    for name in ("studio", "supervisor"):
        fill(paths, name, "0.1.0")
    handle = open(paths.venvs / "supervisor" / "Scripts" / "ns.exe", "rb")  # noqa: SIM115
    try:
        env = env_of(Env(), sync=Uv(paths).sync)
        err = raised(lambda: update.update_app(make_zip(tmp_path), app, env, paths=paths))
    finally:
        handle.close()
    assert err is not None and err.data.get("venvs_left") == ["supervisor"]
    return err.detail, paths


def dead_in_the_sync(tmp_path: Path, kind: str) -> tuple[str, Paths]:
    """The worker dies while it refreshes the environments: what `if_stopped_here` then says."""
    app, paths = updated(tmp_path, OLD_CLI)
    env = env_of(Env())

    def die(a: Path) -> None:
        raise Died

    env.sync = die
    z = make_zip(tmp_path, "0.3.0", extra={"ns_supervisor/cli.py": OLD_CLI})

    def body() -> update_job.Outcome:
        if kind == "update":
            return outcome(update.update_app(z, app, env, paths=paths))
        return outcome(update.rollback_app(app, env, paths=paths))

    with pytest.raises(Died):
        update_job.run_worker(paths, body, old_version="0.2.0")
    shown = update_job.effective_status(update_job.read_status(paths), alive=lambda pid: False)
    return str(shown["message"]), paths


STATES = {
    "a rollback not finished, an older program in place": lambda t: unfinished_rollback(t, OLD_CLI),
    "an update put back, an environment left": lambda t: unfinished_update(t, OLD_CLI),
    "the worker died in a rollback's sync": lambda t: dead_in_the_sync(t, "rollback"),
    "the worker died in an update's sync": lambda t: dead_in_the_sync(t, "update"),
}


@pytest.mark.parametrize("state", list(STATES))
def test_every_way_out_is_a_command_the_program_in_place_has(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    message, paths = STATES[state](tmp_path)
    typed = commands(message)
    assert typed, f"no command in: {message}"
    # the program in place is 0.1.14's kind: without --detach its `ns update` follows from the venv (D-39)
    assert all("--detach" in c for c in typed), f"{state}: a command takes the broken route: {typed}"
    assert "twice" not in message and "the last line of the log it names" in message
    assert "run/update_state.json" in message            # where a --detach run's result shows
    assert "If 'ns' does not start, run INSTALL.cmd from the Nightshift" in message
    # each command exists in a program that has this code too, and is its background route (no window,
    # nothing follows): the second run of a rollback pair is typed into the newer program
    hand = Hand(monkeypatch)
    point_home_at(paths, tmp_path / "NightShift")    # (a rollback put the stand-in's install_state back)
    for command in typed:
        args = command.split()[1:]
        args = [str(make_zip(tmp_path, "0.4.0")) if a == "<zip>" else a for a in args]
        if "--git" in args:
            continue
        r = invoke(paths.home, *args)
        assert r.exit_code == 0 and json.loads(r.output.strip().splitlines()[-1])["started"] is True, r.output
    assert hand.windows == [] and hand.followed == [] and len(hand.workers) == len(typed)


def test_a_program_without_detach_is_sent_to_install_cmd_and_setup_runs_its_environment_step(
    tmp_path: Path,
) -> None:
    message, paths = unfinished_rollback(tmp_path, None)         # a program older than 0.1.14: no --detach
    assert commands(message) == [], message
    assert "then run INSTALL.cmd from the Nightshift 0.2.0 zip and choose this folder" in message
    # setup fills the environments again only for a version other than the one its step last ran for
    (paths.config / "wizard_flags.json").write_text(json.dumps({"venvs_version": "0.2.0"}), encoding="utf-8")
    app = tmp_path / "NightShift"
    assert "from the Nightshift 0.1.0 zip" in update.rollback_way_out(paths, app, "0.1.0", "0.2.0")
    assert "from the Nightshift 0.1.0 zip" in update.rollback_way_out(paths, app, "0.2.0", "0.1.0")
    flags = paths.config / "wizard_flags.json"
    flags.write_text(json.dumps({"venvs_version": "0.1.0"}), encoding="utf-8")
    assert "from the Nightshift 0.2.0 zip" in update.rollback_way_out(paths, app, "0.2.0", "0.1.0")
    # the two programs of a rollback pair must BOTH have --detach for the command pair to be named
    (app / "ns_supervisor").mkdir(exist_ok=True)
    (app / "ns_supervisor" / "cli.py").write_text(OLD_CLI, encoding="utf-8")
    assert commands(update.rollback_way_out(paths, app, "0.1.0", "0.2.0")) == []
    assert update.takes_detach(ROOT) and not update.takes_detach(tmp_path / "nowhere")
    assert "'ns update --git --detach'" in (ROOT / "ns_supervisor" / "update.py").read_text(encoding="utf-8")


# ------------------------------------------------------------------ the Settings route, end to end
def test_an_update_from_settings_whose_resync_fails_twice_is_told_to_the_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings > Updates > From a file; a program holds a file of the supervisor environment, so the new
    version's sync and the restore's re-sync both fail. Nightshift is not started again, so no page can
    show the result: the worker opens the progress window itself, a morning note waits for the next
    start, and the state the page reads when it loads says what is in place and what to do."""
    app, paths = program(tmp_path), Paths(tmp_path / "ns")
    point_home_at(paths, app)
    for name in ("studio", "supervisor"):
        fill(paths, name, "0.1.0")
    hand, uv, relaunch = Hand(monkeypatch), Uv(paths), Recorder()
    monkeypatch.setattr(cli, "_stop_engine", lambda p: None)
    monkeypatch.setattr(cli, "_sync", lambda p, a: uv.sync(a))
    monkeypatch.setattr(cli, "_reregister", lambda p, a: [])
    monkeypatch.setattr(cli.relaunch, "default_relaunch", lambda p: relaunch)
    monkeypatch.setattr(update, "verify_quick", lambda a, python=None: True)
    monkeypatch.setattr(cli.control, "wait_down", lambda *a, **k: True)
    monkeypatch.setattr(update_job, "wait_for_parent", lambda pid, **k: None)   # the page's `ns` child ended
    under_engine = {"NIGHTSHIFT_HOME": str(paths.home), procs.UNDER_ENGINE_ENV: "1"}

    def run(cmd: list[str], timeout_s: int) -> tuple[int, str, str]:      # what the dashboard service runs
        r = runner.invoke(cli.app, cmd[cmd.index("update"):], env=under_engine)
        return r.exit_code, r.output, ""

    z = make_zip(tmp_path, extra={"ns_supervisor/cli.py": OLD_CLI})
    answer = updater.RealUpdateAdapter(paths, run).apply_file(z)
    assert answer["state"] == "started" and len(hand.workers) == 1 and hand.windows == []
    argv = hand.workers[0]
    assert "--watched" not in argv, "nothing follows a run started from the page"
    handle = open(paths.venvs / "supervisor" / "Scripts" / "ns.exe", "rb")  # noqa: SIM115
    try:
        worker = runner.invoke(cli.app, argv[argv.index("update"):], env={"NIGHTSHIFT_HOME": str(paths.home)})
    finally:
        handle.close()
    assert worker.exit_code == 1, worker.output
    state = updater.read_update_state(paths)              # GET /api/update/state: what the page loads
    message = str(state["message"])
    assert state["state"] == "failed" and "Nightshift was not started" in message
    assert "0.1.0's program is back, but the Python environment 'supervisor' could not be" in message
    assert all("--detach" in c for c in commands(message)) and commands(message)
    assert "restart" not in relaunch.calls and uv.tries.count("supervisor") == 6     # twice, three tries
    # told now: the worker's own window, for this run's log, following the worker itself
    assert len(hand.windows) == 1
    window = hand.windows[0]
    script = base64.b64decode(window[window.index("-EncodedCommand") + 1]).decode("utf-16-le")
    log = argv[argv.index("--log") + 1]
    assert log in script and f"$startPid = {os.getpid()}" in script
    # told later: a note for whoever opens Nightshift next
    notes = notify.read_notes(paths)
    assert len(notes) == 1 and notes[0]["message"] == message
    # the same run from a terminal or the Update shortcut is followed already: no second window
    fill(paths, "supervisor", "0.1.0")
    handle = open(paths.venvs / "supervisor" / "Scripts" / "ns.exe", "rb")  # noqa: SIM115
    try:
        again = [*argv[argv.index("update"):], "--watched"]
        assert runner.invoke(cli.app, again, env={"NIGHTSHIFT_HOME": str(paths.home)}).exit_code == 1
    finally:
        handle.close()
    assert len(hand.windows) == 1 and len(notify.read_notes(paths)) == 2


def test_the_settings_page_shows_a_failed_update_when_it_loads_and_claims_nothing() -> None:
    page = (ROOT / "ns_dashboard" / "ui" / "src" / "pages" / "Settings.tsx").read_text(encoding="utf-8")
    assert "nothing was changed" not in page
    assert 'else if (u.state === "failed") setUpdateNote(failedNote(u))' in page
    built = (ROOT / "ns_dashboard" / "static" / "app.js").read_text(encoding="utf-8")
    assert "The last update or rollback did not complete" in built
    assert "did not work and nothing was changed" not in built
    assert "Wait for its window to finish" not in built
    launcher = (ROOT / "launchers" / "update.ps1").read_text(encoding="ascii")
    assert "--detach --watched" in launcher


# ----------------------------------------------------------------------- the result, for a script
def test_wait_follows_to_the_real_result_and_is_refused_where_it_cannot_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, paths = updated(tmp_path)
    started: list[list[str]] = []
    result = {"state": "done", "message": "Now on 0.1.0 (was 0.2.0).", "code": 0}

    def spawn(argv: list[str], cwd: Path) -> int:
        started.append(argv)
        log = Path(argv[argv.index("--log") + 1])
        log.write_text("step one\n", encoding="utf-8")
        update_job.write_status(
            paths, str(result["state"]), str(result["message"]), log=str(log), exit_code=result["code"]
        )
        return os.getpid()

    monkeypatch.setattr(update_job, "spawn_detached", spawn)
    # an earlier run's result is in the state file: it must not be taken for this run's
    update_job.write_status(paths, "failed", "EARLIER", log="other.log", exit_code=1)
    monkeypatch.setattr(update_job, "runs_from_venvs", lambda p: False, raising=False)   # outside the venvs
    r = invoke(paths.home, "update", "--rollback", "--wait")
    assert r.exit_code == 0 and "step one" in r.output and "Result: Now on 0.1.0 (was 0.2.0)." in r.output
    assert "EARLIER" not in r.output
    argv = started[0]
    assert "--wait-pid" not in argv and "--watched" in argv      # it stays; the worker waits for nobody
    result.update(state="failed", message="NS-E706: close them.", code=1)
    r = invoke(paths.home, "update", "--rollback", "--wait")
    assert r.exit_code == 1 and "Result: NS-E706: close them." in r.output
    # Nightshift's own `ns` runs from the environment the update replaces: refused before anything starts
    monkeypatch.setattr(update_job, "runs_from_venvs", lambda p: True, raising=False)
    r = invoke(paths.home, "update", "--rollback", "--wait")
    assert r.exit_code == 2 and len(started) == 2 and "Nothing was started" in r.output
    assert "--detach" in r.output and "follow" in r.output
    assert invoke(paths.home, "update", "--rollback", "--wait", "--detach").exit_code == 2
    # a worker that dies without a word: its last `if_stopped_here`, exit 1
    log = paths.logs / "died.log"
    left = {"log": str(log), "pid": 999_999, "if_stopped_here": "Left so."}
    update_job.write_status(paths, "running", "Updating.", **left)
    said: list[str] = []
    code = update_job.follow(paths, 999_999, log, said.append, alive=lambda pid: False, sleep=lambda s: None)
    assert code == 1 and said[-1] == "Result: Left so."


@needs_powershell
def test_detach_names_a_script_that_waits_and_returns_the_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess

    _app, paths = updated(tmp_path)
    Hand(monkeypatch)
    r = invoke(paths.home, "update", "--rollback", "--detach")
    info = json.loads(r.output.strip().splitlines()[-1])
    assert r.exit_code == 0 and info.get("pid") == 4242 and Path(info.get("follow", "none")).is_file()
    assert Path(info["follow"]).parent == paths.home / "run"          # outside the program and the venvs
    update_job.write_status(paths, "failed", "NS-E702: not finished.", log=info["log"], exit_code=1)
    exe = update_job.follower_argv("")[0]
    done = subprocess.run(
        [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", info["follow"]],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert done.returncode == 1 and "Result: NS-E702: not finished." in done.stdout
    # the next run removes the earlier script
    again = json.loads(invoke(paths.home, "update", "--rollback", "--detach").output.strip().splitlines()[-1])
    assert Path(again["follow"]).is_file()
    assert len(list((paths.home / "run").glob("update-wait-*.ps1"))) == 1
