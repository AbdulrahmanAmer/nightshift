"""D-46: stopping Nightshift for an update is one sequence, and a refusal never leaves Nightshift down.

The block D proof: `ns update --rollback` in a terminal; the engine was asked to stop, stopped its services,
closed its port and never exited (its venv launcher still waited for it). The holder check refused, named
the launcher as a program to close, nothing ended the engine, and its restart failed (the hung engine held
`run/supervisor.lock`), so Nightshift was left down.

Every process here is a real one started by the test: an engine stand-in is a venv's `python.exe` (the
launcher, under `<home>/venvs/supervisor`, exactly as the engine runs) whose child holds this home's
`run/supervisor.lock` and runs `ns_supervisor.app` on its command line; a foreign program is a venv's python
that holds nothing of ours. Each is ended by its recorded pid when the test ends, whatever happened.
New names are looked up with `getattr`, so on the base each test fails at an assertion.
"""

from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
import time
import venv
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest

from ns_common.paths import Paths
from ns_supervisor import control, update, update_job
from ns_supervisor.client import SupervisorClient
from tests.test_ns_update import Env
from tests.test_update_handover import Recorder, env_of, on_disk, outcome, raised, updated

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = "Scripts" if sys.platform == "win32" else "bin"
windows = pytest.mark.skipif(sys.platform != "win32", reason="the engine's lock and launcher are Windows")

#: The engine stand-in: takes the home's engine lock like `ns_supervisor.app.main`, says so, then either
#: ends by itself after `late` seconds or never ends (`late` < 0). "ns_supervisor.app" is on its command line.
ENGINE = (
    "import sys, time; from pathlib import Path; from ns_common.singleton import FileLock; "
    "lock = FileLock(Path(sys.argv[2])); lock.acquire(); print('held', flush=True); "
    "late = float(sys.argv[3]); time.sleep(late if late >= 0 else 600)"
)


def venv_python(paths: Paths, name: str) -> Path:
    folder = paths.venvs / name
    exe = folder / SCRIPTS / ("python.exe" if sys.platform == "win32" else "python")
    if not exe.exists():
        venv.create(folder, with_pip=False)
    return exe


class Started:
    """The processes this test started, each ended by its recorded pid (and its children) at the end."""

    def __init__(self) -> None:
        self.procs: list[subprocess.Popen[Any]] = []
        self.family: dict[int, psutil.Process] = {}     # every process of theirs, noted while it lived

    def _note(self, proc: subprocess.Popen[Any]) -> None:
        """Note the process and the interpreter its venv launcher starts (a launcher killed alone would leave
        that child running)."""
        self.procs.append(proc)
        deadline = time.monotonic() + 10
        while True:
            try:
                me = psutil.Process(proc.pid)
                kids = me.children(recursive=True)
            except psutil.Error:
                return
            self.family[me.pid] = me
            self.family.update({k.pid: k for k in kids})
            if kids or proc.poll() is not None or time.monotonic() > deadline:
                return
            time.sleep(0.05)

    def engine(self, paths: Paths, late: float) -> subprocess.Popen[str]:
        exe = venv_python(paths, "supervisor")
        lock = paths.home / "run" / "supervisor.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [str(exe), "-c", ENGINE, "ns_supervisor.app", str(lock), str(late)], cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT)}, stdout=subprocess.PIPE, text=True,
        )
        self._note(proc)
        assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
        return proc

    def foreign(self, paths: Paths, python: Path | None = None) -> subprocess.Popen[Any]:
        """A program the owner started from the studio environment (a Python prompt): not ours."""
        exe = python or venv_python(paths, "studio")
        proc: subprocess.Popen[Any] = subprocess.Popen([str(exe), "-c", "import time; time.sleep(600)"])
        self._note(proc)
        return proc

    def end(self) -> None:
        for proc in self.procs:
            try:
                self.family.update({k.pid: k for k in psutil.Process(proc.pid).children(recursive=True)})
            except psutil.Error:
                pass
        for p in self.family.values():      # (psutil refuses a pid reused since it was noted)
            try:
                p.kill()
            except psutil.Error:
                pass
        for proc in self.procs:
            proc.wait(timeout=30)


@pytest.fixture
def started() -> Any:
    s = Started()
    try:
        yield s
    finally:
        s.end()


def down_client() -> SupervisorClient:
    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    return SupervisorClient("http://sup", client=httpx.Client(transport=httpx.MockTransport(refuse)))


def gone(proc: subprocess.Popen[Any], wait_s: float = 10.0) -> bool:
    try:
        proc.wait(timeout=wait_s)
    except subprocess.TimeoutExpired:
        return False
    return True


def stop_lines(paths: Paths, client: SupervisorClient, said: list[str]) -> list[str]:
    """`control.stop` as the update calls it (on the base it has no holder list and no progress lines)."""
    if "holders" in inspect.signature(control.stop).parameters:
        return control.stop(paths, client, holders=lambda: update.holders(paths), say=said.append)
    return control.stop(paths, client)


def real_stop(paths: Paths, said: list[str]) -> update.UpdateEnv:
    """The update's environment with the product's own stop (the engine does not answer: hung)."""
    env = env_of(Env(), holder_wait_s=1.0)
    env.stop = lambda: said.extend(stop_lines(paths, down_client(), said))
    if hasattr(env, "say"):
        env.say = said.append
    return env


@pytest.fixture(autouse=True)
def short_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "RENAME_PAUSE_S", 0.05)
    stopping = getattr(__import__("ns_supervisor", fromlist=["x"]), "stopping", None)
    if stopping is not None:
        monkeypatch.setattr(stopping, "STOP_WAIT_S", 10.0)
        monkeypatch.setattr(stopping, "END_WAIT_S", 5.0)


# ------------------------------------------------------------------------ 1: stops late, inside the bound
@windows
def test_an_engine_that_stops_late_is_waited_for_and_the_rollback_goes_on(
    tmp_path: Path, started: Any
) -> None:
    app, paths = updated(tmp_path)
    engine = started.engine(paths, late=6.0)
    said: list[str] = []
    err = raised(lambda: update.rollback_app(app, real_stop(paths, said), paths=paths))
    assert err is None, f"the rollback was refused: {err}"
    assert gone(engine, 0) and engine.returncode == 0, "it was ended instead of waited for"
    waiting = [s for s in said if s.startswith("Waiting for Nightshift to stop: ")]
    assert waiting and f"(process {engine.pid}" in waiting[0], said      # the launcher, by name and pid
    assert not any(s.startswith("Ended ") for s in said)


# ------------------------------------------------------------------------- 2: never stops, and it is ours
@windows
def test_an_engine_that_never_stops_is_ended_by_its_recorded_process_and_the_rollback_goes_on(
    tmp_path: Path, started: Any
) -> None:
    app, paths = updated(tmp_path)
    engine = started.engine(paths, late=-1)
    child = psutil.Process(engine.pid).children()[0].pid       # the interpreter that holds the lock
    said: list[str] = []
    err = raised(lambda: update.rollback_app(app, real_stop(paths, said), paths=paths))
    assert err is None, f"the rollback was refused: {err}"
    assert gone(engine), "the engine that never stopped is still there"
    ended = [s for s in said if s.startswith("Ended Nightshift's own ")]
    assert any(f"(process {child}, the engine)" in s for s in ended), said
    assert not control_lock_held(paths)


def control_lock_held(paths: Paths) -> bool:
    try:
        with open(paths.home / "run" / "supervisor.lock", "rb") as fh:
            fh.read(1)
    except PermissionError:
        return True
    except OSError:
        return False
    return False


# ------------------------------------------- 3: a foreign holder: refused, named, Nightshift started again
class Restarts(Recorder):
    """A relauncher that saw Nightshift running before the update (`Relaunch.wanted`)."""

    wanted = True


@windows
def test_a_foreign_holder_refuses_names_that_program_and_nightshift_is_started_again(
    tmp_path: Path, started: Any
) -> None:
    app, paths = updated(tmp_path)
    engine = started.engine(paths, late=-1)                 # ours, and it never stops
    other = started.foreign(paths)                          # the owner's Python prompt
    relaunch, said = Restarts(), []
    before = on_disk(app, paths)
    code = update_job.run_worker(
        paths, lambda: outcome(update.rollback_app(app, real_stop(paths, said), paths=paths)),
        relaunch=relaunch,
    )
    st = update_job.read_status(paths)
    message = str(st.get("message"))
    assert code == 1 and st["state"] == "failed" and message.startswith("NS-E706"), st
    assert f"process {other.pid}," in message, "the program in the way is not the one named"
    assert f"process {engine.pid}," not in message, "Nightshift's own engine was named as a program to close"
    assert gone(engine), "Nightshift's own engine was not ended"
    assert on_disk(app, paths) == before and "Nothing was changed" in message
    assert relaunch.calls == ["observe", "restart"], "Nightshift was left down"
    assert message.endswith(getattr(update_job, "STARTED_AGAIN", "<no sentence>")), message


# ------------------------------------------ 4: the refusal leaves Nightshift running (a real engine back)
@windows
def test_after_a_refusal_an_engine_runs_again(tmp_path: Path, started: Any) -> None:
    from ns_supervisor.relaunch import Relaunch

    app, paths = updated(tmp_path)
    started.engine(paths, late=-1)
    started.foreign(paths)

    def answers(req: httpx.Request) -> httpx.Response:      # the engine answers while one holds its lock
        if control_lock_held(paths):
            return httpx.Response(200, json={"services": {}, "state": "ready", "ok": True})
        raise httpx.ConnectError("refused")

    client = SupervisorClient("http://sup", client=httpx.Client(transport=httpx.MockTransport(answers)))
    back: list[subprocess.Popen[str]] = []
    relaunch = Relaunch(paths=paths, client=client, window_open=lambda: False,
                        start_engine=lambda: back.append(started.engine(paths, late=-1)),
                        start_window=lambda: pytest.fail("no window was open"), wait_s=30.0)
    said: list[str] = []
    env = real_stop(paths, said)
    env.stop = lambda: said.extend(stop_lines(paths, client, said))
    code = update_job.run_worker(paths, lambda: outcome(update.rollback_app(app, env, paths=paths)),
                                 relaunch=relaunch)
    st = update_job.read_status(paths)
    assert code == 1 and str(st["message"]).startswith("NS-E706"), st
    assert len(back) == 1 and back[0].poll() is None and control_lock_held(paths), "Nightshift is down"
    assert not st.get("restart_failed"), st


# --------------------------------------------------------------------- "ours" is never a look-alike pid
@windows
def test_a_recorded_pid_of_a_newer_or_unrelated_process_is_not_ours(tmp_path: Path, started: Any) -> None:
    stopping = getattr(__import__("ns_supervisor", fromlist=["x"]), "stopping", None)
    assert stopping is not None, "there is no stop sequence that knows what is ours"
    paths = Paths(tmp_path / "ns")
    record = paths.home / "run" / "supervisor.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    # a process that has nothing of this install on its command line, under a recorded pid
    unrelated = started.foreign(paths, python=Path(sys.executable))
    record.write_text(json.dumps({"pid": unrelated.pid, "children": {}}), encoding="utf-8")
    assert stopping.ours(paths) == [], "an unrelated program was taken for the engine"
    # a program of this install's environments, started after the record was written (a reused pid)
    newer = started.foreign(paths)
    record.write_text(json.dumps({"pid": newer.pid, "children": {"tts": newer.pid}}), encoding="utf-8")
    old = time.time() - 3600
    os.utime(record, (old, old))
    assert stopping.ours(paths) == [], "a pid given to another program after the record was taken for ours"
    # the engine: whoever holds this home's engine lock, and its venv launcher
    engine = started.engine(paths, late=-1)
    lock_holder = psutil.Process(engine.pid).children()[0].pid
    pids = {o.pid for o in stopping.ours(paths)}
    assert pids == {lock_holder, engine.pid}, pids


# ------------------------------------------------------------ the engine's own exit is bounded (the cause)
def test_the_engine_ends_within_its_bound_even_with_a_thread_that_never_returns() -> None:
    code = (
        "import threading, time\n"
        "threading.Thread(target=time.sleep, args=(600,)).start()   # not a daemon: it keeps us alive\n"
        "from ns_supervisor.app import exit_watchdog\n"
        "exit_watchdog(0.5)\n"
    )
    t0 = time.monotonic()
    proc = subprocess.Popen([sys.executable, "-c", code], cwd=str(ROOT), stderr=subprocess.PIPE, text=True,
                            env={**os.environ, "PYTHONPATH": str(ROOT)})
    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        for p in [*psutil.Process(proc.pid).children(recursive=True), psutil.Process(proc.pid)]:
            p.kill()        # the launcher and the interpreter it started, by their pids
        proc.wait(timeout=30)
        pytest.fail("the process outlived its exit bound")
    assert proc.returncode == 0, proc.stderr.read() if proc.stderr else ""
    assert time.monotonic() - t0 < 30


# ------------------------------------------------- setup's Repair: the same guard, and Nightshift put back
def test_setups_refusal_starts_the_engine_it_stopped_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from installer.stage2.web import controller
    from installer.stage2.web.bootstrap import CmdResult
    from tests.test_installer_web_controller import Setup

    s = Setup(tmp_path, monkeypatch)
    (s.home / "venvs" / "supervisor" / "Scripts").mkdir(parents=True)
    (s.home / "venvs" / "supervisor" / "Scripts" / "ns.exe").write_bytes(b"MZ")
    s.accept()
    monkeypatch.setattr(controller, "HOLDER_WAIT_S", 0.3, raising=False)
    monkeypatch.setattr(controller, "HOLDER_PAUSE_S", 0.05, raising=False)
    monkeypatch.setattr(controller, "ENGINE_BACK_WAIT_S", 2.0, raising=False)
    engine = {"up": True}                       # the engine runs when setup starts
    real_cmd = s.fx.cmd

    def cmd(argv: list[str], *a: Any, **k: Any) -> Any:
        if argv[0].endswith("ns.exe") and argv[-1] == "stop":
            engine["up"] = False                # `ns stop`: the update's stop sequence
        return real_cmd(argv, *a, **k)

    def launch(argv: list[str], env: Any = None) -> None:
        s.fx.launched.append(list(argv))       # type: ignore[attr-defined]
        engine["up"] = True

    s.fx.cmd = cmd
    s.fx.launch = launch
    s.fx.engine_answers = lambda home: engine["up"]     # (fac2213 looked at a lock: engine_running)
    s.fx.engine_running = s.fx.engine_answers  # type: ignore[attr-defined]
    py = s.home / "venvs" / "studio" / "Scripts" / "python.exe"
    s.cmd.fail["Get-CimInstance"] = CmdResult(0, f"4711|{py}|\"{py}\" -i\n1|C:/x.exe|x\n", "")
    assert s.ctl.do_action("start")[0] == 200
    st = s.until(lambda x: (x["failure"] or {}).get("code") == "NS-E706" or x["run"] == "finished",
                 what="the holder check")
    detail = (st["failure"] or {}).get("detail", "")
    assert "python.exe (process 4711)" in detail
    started = [a for a in s.fx.launched if a[1:] == ["-m", "ns_supervisor.app"]]     # type: ignore[attr-defined]
    assert started and engine["up"], "setup's refusal left Nightshift down"
    assert detail.endswith(getattr(controller, "ENGINE_BACK", "<no sentence>")), detail


# ------------------------------------------- review blocker 1: "ours" is tied to THIS install, by one rule
def test_the_rule_of_this_install_row_by_row(tmp_path: Path) -> None:
    from installer.stage2.web import venvrepair

    rule = getattr(venvrepair, "is_this_install", None)
    assert rule is not None, "there is no one rule for what is this install's own"
    home, other = tmp_path / "NS", tmp_path / "NS2"
    rec = 1000.0
    P = venvrepair.Proc

    def proc(exe: str = "C:/Py/python.exe", cmd: str = "python -m ns_dashboard.server",
             env: str | None = None, created: float | None = 900.0) -> Any:
        return P(1, None, exe, cmd, created, env)

    rows = [
        (proc(exe=str(home / "venvs" / "supervisor" / "Scripts" / "python.exe")), True),   # program in home
        (proc(cmd=f'python "{home / "app" / "x.py"}"'), True),                            # home on its line
        (proc(env=str(home)), True),                                                     # its NIGHTSHIFT_HOME
        (proc(), False),                                    # a Nightshift module, nothing of this install
        (proc(exe=str(other / "venvs" / "supervisor" / "Scripts" / "python.exe"), env=str(other)), False),
        (proc(cmd=f"python {other}"), False),                         # NS2 is not NS
        (proc(env=str(home), created=1003.0), False),                 # started after the record
        (proc(env=str(home), created=None), False),                   # start not known
    ]
    for p, want in rows:
        assert rule(home, p, rec) is want, p


@windows
def test_a_second_installs_process_under_a_recorded_pid_survives(tmp_path: Path, started: Any) -> None:
    """Install B's dashboard runs under a pid install A's record still lists (Windows reused it): A's update
    must not end it, however Nightshift-like its command line."""
    app, paths = updated(tmp_path)
    other = tmp_path / "second-install"
    (other / "run").mkdir(parents=True)
    b = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)", "-m", "ns_dashboard.server", str(other)],
        env={**os.environ, "NIGHTSHIFT_HOME": str(other)},
    )
    started._note(b)
    time.sleep(0.5)
    engine = started.engine(paths, late=-1)                 # A's engine, which never stops
    record = paths.home / "run" / "supervisor.json"
    data = {"pid": engine.pid, "port": 8099, "children": {"dashboard": b.pid}}
    record.write_text(json.dumps(data), encoding="utf-8")
    said: list[str] = []
    err = raised(lambda: update.rollback_app(app, real_stop(paths, said), paths=paths))
    assert b.poll() is None, f"install B's process was ended as A's own: {said}"
    assert err is None and gone(engine), f"A's own engine was not ended: {err}"


# --------------------------------------- review blocker 2: setup over an old install ends what is its own
def _setup_over_old_install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: Any, *, endable: bool
                            ) -> tuple[Any, subprocess.Popen[Any], list[str]]:
    from installer.stage2.web import controller, venvrepair
    from installer.stage2.web.bootstrap import CmdResult
    from tests.test_installer_web_controller import Setup

    s = Setup(tmp_path, monkeypatch)
    (s.home / "venvs" / "supervisor" / "Scripts").mkdir(parents=True)
    s.accept()
    paths = Paths(s.home)
    engine = started.engine(paths, late=-1)        # the old engine that closed its port and never exits
    (s.home / "venvs" / "supervisor" / "Scripts" / "ns.exe").write_bytes(b"MZ")
    monkeypatch.setattr(controller, "HOLDER_WAIT_S", 15.0, raising=False)
    monkeypatch.setattr(controller, "HOLDER_PAUSE_S", 0.2, raising=False)
    monkeypatch.setattr(controller, "OWN_WAIT_S", 1.0, raising=False)
    monkeypatch.setattr(controller, "ENGINE_BACK_WAIT_S", 1.0, raising=False)
    real_cmd, ended = s.fx.cmd, []

    def cmd(argv: list[str], *a: Any, **k: Any) -> Any:
        if argv[0].endswith("ns.exe") and argv[-1] == "stop":
            return CmdResult(0, "Engine stopped through the supervisor API.\n", "")   # 0.1.15-rc4's stop
        if argv[0] == "powershell.exe" and "Get-CimInstance" in argv[-1]:
            listing = subprocess.run([argv[0], *argv[1:-1], getattr(venvrepair, "PROCESS_LIST", argv[-1])],
                                     capture_output=True, text=True, timeout=120)       # read only
            return CmdResult(listing.returncode, listing.stdout, listing.stderr)
        if argv[0] == "taskkill":
            pid = int(argv[argv.index("/PID") + 1])
            ended.append(str(pid))
            if pid not in started.family:
                pytest.fail(f"setup tried to end a process this test did not start: {pid}")
            if endable:
                try:
                    family = [*started.family[pid].children(recursive=True), started.family[pid]]
                except psutil.Error:
                    family = []
                for p in family:
                    try:
                        p.kill()
                    except psutil.Error:
                        pass
                return CmdResult(0, "", "")
            return CmdResult(1, "", "ERROR: Access is denied.")
        return real_cmd(argv, *a, **k)

    s.fx.cmd = cmd
    s.fx.engine_answers = lambda home: True       # it answered when setup started  # type: ignore[attr-defined]
    s.fx.engine_running = lambda home: True       # (fac2213's probe: the lock)       # type: ignore[attr-defined]
    return s, engine, ended


@windows
def test_setup_over_an_old_install_ends_its_hung_engine_and_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: Any
) -> None:
    s, engine, ended = _setup_over_old_install(tmp_path, monkeypatch, started, endable=True)
    assert s.ctl.do_action("start")[0] == 200
    st = s.until(lambda x: x["failure"] is not None or x["run"] in ("finished", "waiting"), timeout=90.0,
                 what="setup to pass its holder check")
    failure = st["failure"] or {}
    assert failure.get("code") != "NS-E706", f"setup refused on its own engine: {failure.get('detail')}"
    assert gone(engine) and str(engine.pid) in ended, "the hung engine of the old install was not ended"


@windows
def test_setup_with_an_own_process_it_cannot_end_says_restart_and_not_that_nightshift_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, started: Any
) -> None:
    from installer.stage2.web import controller

    s, engine, _ended = _setup_over_old_install(tmp_path, monkeypatch, started, endable=False)
    assert s.ctl.do_action("start")[0] == 200
    st = s.until(lambda x: (x["failure"] or {}).get("code") == "NS-E706" or x["run"] == "finished",
                 timeout=90.0, what="the holder check")
    detail = " ".join(str((st["failure"] or {}).get("detail", "")).split())
    assert not detail.endswith(controller.ENGINE_BACK.strip()), "it says Nightshift runs; nothing answers"
    assert "Nightshift's own processes did not stop" in detail and "restart the PC" in detail, detail
    assert f"(process {engine.pid}" in detail and "Close them" not in detail, detail


# ---------------------------------------------------------------------------------- review notes
def test_a_stop_request_that_does_not_answer_is_shown_and_bounded(monkeypatch: pytest.MonkeyPatch,
                                                                    tmp_path: Path) -> None:
    from ns_supervisor import stopping

    monkeypatch.setattr(stopping, "PROGRESS_EVERY_S", 0.2)
    monkeypatch.setattr(stopping, "SHUTDOWN_WAIT_S", 1.0, raising=False)

    class Slow:
        def is_up(self) -> bool:
            return False if getattr(self, "asked", False) else True

        def shutdown(self, grace_s: float) -> None:
            self.asked = True
            time.sleep(5)               # a request that hangs (the client's own timeout is 150 s)

    said: list[str] = []
    t0 = time.monotonic()
    done = stopping.stop_nightshift(Paths(tmp_path / "ns"), Slow(), say=said.append, note=lambda p: [])
    assert time.monotonic() - t0 < 4, "the sequence waited for the request's own timeout"
    assert any("has not answered its stop request yet" in s for s in said), said
    assert any("had not answered its stop request" in s for s in done.lines), done.lines


def test_an_own_process_that_cannot_be_ended_gets_no_shortcut_promise(tmp_path: Path) -> None:
    from ns_common.errors import NsError

    paths, relaunch = Paths(tmp_path / "ns"), Restarts()

    def body() -> update_job.Outcome:
        raise NsError("NS-E706", "Nightshift's own processes did not stop when asked and could not be ended: "
                      "python.exe (process 9, the engine). Nothing was changed. "
                      + getattr(update, "OWN_STUCK_WAY_OUT", ""), own_left=True)

    assert update_job.run_worker(paths, body, relaunch=relaunch) == 1
    st = update_job.read_status(paths)
    assert "restart" not in relaunch.calls, "a start was tried while its own engine holds the lock"
    said = st["message"]
    assert update_job.RESTART_HINT not in said and "restart the PC, then open Nightshift" in said, said
