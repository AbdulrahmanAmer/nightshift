"""D-39a: no update or rollback rewrites a Python environment its own processes run from, and no message
says "rolled back" or "updated" unless that is what is on the disk.

The cause (reproduced, see the item's report): a console command started as the venv's `ns.exe`
runs as `python.exe <...>\\Scripts\\ns.exe`, so that file is `sys.path[0]`; the first pydantic model asks
`importlib.metadata` for every distribution, which opens each `sys.path` entry that is a file as a zip and
keeps it open (no delete sharing). uv then cannot remove `ns.exe` (os error 32) for as long as that command
lives, and a terminal `ns update` lived to follow its worker.

New names are looked up with `getattr`, so on the base every test fails at an assertion.
"""

from __future__ import annotations

import base64
import inspect
import json
import os
import subprocess
import sys
import threading
import time
import venv
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil
import pytest
from typer.testing import CliRunner

from ns_common import rollback_intent
from ns_common.config import save_install_state
from ns_common.errors import NsError
from ns_common.models import InstallState
from ns_common.paths import Paths
from ns_supervisor import cli, control, pwsh, update, update_job
from tests.test_ns_update import Env, make_app, make_zip

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()
needs_powershell = pytest.mark.skipif(pwsh.find_powershell() is None, reason="powershell not on PATH")
SCRIPTS = "Scripts" if sys.platform == "win32" else "bin"
OLD_CLAIM = "rolled back to the previous version"


class Died(BaseException):
    """The worker process dying at this point (not an Exception: nothing may catch and report it)."""


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "RENAME_PAUSE_S", 0.05)


def raised(fn: Any) -> NsError | None:
    try:
        fn()
    except NsError as e:
        return e
    return None


def env_of(e: Env, **fields: Any) -> update.UpdateEnv:
    """The test environment, with this item's fields set where the code has them (the base has none)."""
    env = e.env()
    for key, value in fields.items():
        if hasattr(env, key):
            setattr(env, key, value)
    return env


def point_home_at(paths: Paths, app: Path) -> None:
    save_install_state(paths, InstallState(version="0.2.0", app_path=str(app)))


#: The `ns update` of a program from 0.1.14 to 0.1.15-rc3, as its `ns_supervisor/cli.py` has it: `--detach`
#: exists, and without it the command follows its worker from the venv (D-39's broken route).
OLD_CLI = (
    'typer.Option("--detach", help="Start the update in the background and return at once."),\n'
    "background = detach or update_job.under_engine()\n"
    "raise typer.Exit(update_job.follow(paths, pid, log_file, typer.echo))\n"
)


def program(tmp_path: Path, cli_text: str | None = OLD_CLI) -> Path:
    """`make_app`, with the `ns` of that program (None: a program too old to have `--detach`)."""
    app = make_app(tmp_path)
    if cli_text is not None:
        (app / "ns_supervisor").mkdir()
        (app / "ns_supervisor" / "cli.py").write_text(cli_text, encoding="utf-8")
    return app


def updated(tmp_path: Path, cli_text: str | None = OLD_CLI) -> tuple[Path, Paths]:
    """An install on 0.2.0 whose `<app>.prev` is 0.1.0; both programs have the `ns` of `cli_text`."""
    app, paths = program(tmp_path, cli_text), Paths(tmp_path / "ns")
    extra = {} if cli_text is None else {"ns_supervisor/cli.py": cli_text}
    update.update_app(make_zip(tmp_path, extra=extra), app, Env().env(), paths=paths)
    point_home_at(paths, app)
    return app, paths


def fill(paths: Paths, name: str, version: str) -> Path:
    """A stand-in venv that holds Nightshift `version` (what a re-sync leaves behind)."""
    folder = paths.venvs / name
    (folder / "Scripts").mkdir(parents=True, exist_ok=True)
    (folder / "Scripts" / "python.exe").write_bytes(b"MZ")
    (folder / "Scripts" / "ns.exe").write_bytes(b"MZ " + version.encode())
    site = folder / "Lib" / "site-packages"
    for old in site.glob("nightshift-*.dist-info"):
        (old / "METADATA").unlink()
        old.rmdir()
    info = site / f"nightshift-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(  # noqa: E501
        f"Metadata-Version: 2.1\nName: nightshift\nVersion: {version}\n", "utf-8")
    return folder


def holds(paths: Paths, name: str) -> str:
    site = paths.venvs / name / "Lib" / "site-packages"
    return ",".join(sorted(p.name for p in site.glob("nightshift-*")))


class Uv:
    """What uv does to a venv, in the order uv does it: remove the old `Scripts/ns.exe`, write the new one."""

    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.tries: list[str] = []

    def __call__(self, cmd: list[str]) -> tuple[int, str]:
        py = Path(cmd[cmd.index("--python") + 1])
        name, app = py.parent.parent.name, Path(cmd[cmd.index("-e") + 1].split("[")[0])
        self.tries.append(name)
        exe = py.parent / "ns.exe"
        try:
            if exe.exists():
                os.remove(exe)
        except OSError:
            return 2, (
                f"error: failed to remove file `{exe}`: The process cannot access the file because it is "
                "being used by another process. (os error 32)"
            )
        fill(self.paths, name, (app / "VERSION").read_text(encoding="utf-8").strip())
        return 0, ""

    def sync(self, app: Path) -> None:
        kw: dict[str, Any] = {"uv": "uv", "run": self}
        if "pause_s" in inspect.signature(update.resync_venvs).parameters:
            kw["pause_s"] = 0.01       # the bound between two tries, shortened by its parameter
        update.resync_venvs(app, self.paths, **kw)


def tree(folder: Path) -> dict[str, bytes]:
    if not folder.exists():
        return {}
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def on_disk(app: Path, paths: Paths) -> dict[str, Any]:
    prev = app.with_name(app.name + ".prev")
    return {
        "app": tree(app), "prev": tree(prev), "config": tree(paths.config), "venvs": tree(paths.venvs),
        "new": app.with_name(app.name + ".new").exists(), "record": rollback_intent.path_for(app).exists(),
    }


class Sleeper:
    """A real process whose image is under `<home>/venvs`: a venv's python that sleeps."""

    def __init__(self, paths: Paths, name: str = "supervisor") -> None:
        folder = paths.venvs / name
        venv.create(folder, with_pip=False)
        self.exe = folder / SCRIPTS / ("python.exe" if sys.platform == "win32" else "python")
        self.proc = subprocess.Popen([str(self.exe), "-c", "import time; time.sleep(120)"])
        self.pid = self.proc.pid

    def stop(self) -> None:
        try:
            family = [*psutil.Process(self.pid).children(recursive=True), psutil.Process(self.pid)]
        except psutil.Error:
            family = []
        for p in family:
            try:
                p.kill()
            except psutil.Error:
                pass
        self.proc.wait(timeout=30)


def invoke(home: Path, *args: str) -> Any:
    return runner.invoke(cli.app, list(args), env={"NIGHTSHIFT_HOME": str(home)})


class Hand:
    """Stand-ins for the two processes the terminal command starts: the worker and the progress window."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.workers: list[list[str]] = []
        self.windows: list[list[str]] = []
        self.followed: list[int] = []
        monkeypatch.setattr(update_job, "spawn_detached", lambda argv, cwd: self.workers.append(argv) or 4242)
        monkeypatch.setattr(update_job, "open_window", self._window, raising=False)
        monkeypatch.setattr(update_job, "follow", self._follow, raising=False)
        # a desktop session (a CI runner is a service in session 0, where no window is opened: D-39b)
        monkeypatch.setattr(update_job, "has_desktop", lambda: True, raising=False)

    def _window(self, argv: list[str], cwd: Path) -> int:
        self.windows.append(argv)
        return 4343

    def _follow(self, paths: Paths, pid: int, *a: Any, **k: Any) -> int:
        self.followed.append(pid)
        return 0


# ----------------------------------------------------------------------- F1 to F6: the terminal hand-over
@pytest.mark.parametrize("route", ["zip", "--github", "--git", "--rollback"])
def test_a_terminal_update_hands_over_before_any_change_and_does_not_follow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    app, paths = updated(tmp_path)
    before = on_disk(app, paths)
    hand = Hand(monkeypatch)
    args = [str(make_zip(tmp_path, "0.3.0"))] if route == "zip" else [route]
    r = invoke(paths.home, "update", *args)
    assert r.exit_code == 0, r.output
    assert len(hand.workers) == 1 and on_disk(app, paths) == before          # nothing changed by the caller
    argv = hand.workers[0]
    assert "--wait-pid" in argv and argv[argv.index("--wait-pid") + 1] == str(os.getpid()), argv
    assert hand.followed == [], "the terminal's command must end: it holds Scripts/ns.exe while it lives"
    assert len(hand.windows) == 1
    window = hand.windows[0]
    assert Path(window[0]).name.lower() == "powershell.exe" and "-EncodedCommand" in window
    script = base64.b64decode(window[window.index("-EncodedCommand") + 1]).decode("utf-16-le")
    for folder in (paths.venvs, app):                # the window runs nothing from the venvs or the program
        assert str(folder).lower() not in " ".join(window).lower() + script.lower()
    assert str(update_job.status_file(paths)) in script and "Nightshift update" in script
    names = {"zip": "The update to 0.3.0 runs", "--rollback": "The rollback runs"}
    what = names.get(route, "The update runs")
    out = " ".join(r.output.split())
    assert f"{what} in its own window, 'Nightshift update', which shows each step and the result." in out
    assert "This terminal is free" in out and "does not stop" in out and "Log: " in out
    assert not update_job.status_file(paths).exists()       # only the worker (the lock holder) writes it


def test_the_hand_over_says_so_when_the_window_cannot_be_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app, paths = updated(tmp_path)
    hand = Hand(monkeypatch)

    def refused(argv: list[str], cwd: Path) -> int:
        raise OSError(5, "no desktop session")

    monkeypatch.setattr(update_job, "open_window", refused, raising=False)
    r = invoke(paths.home, "update", "--rollback")
    out = " ".join(r.output.split())
    assert r.exit_code == 0 and len(hand.workers) == 1 and hand.followed == []
    assert "the progress window could not be opened" in out and "Settings > Updates" in out and "Log: " in out


def test_a_rollback_with_nothing_to_go_back_to_is_refused_in_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, paths = make_app(tmp_path), Paths(tmp_path / "ns")
    point_home_at(paths, app)
    hand = Hand(monkeypatch)
    r = invoke(paths.home, "update", "--rollback")
    assert "no previous version at" in r.output and "NS-E702" in r.output
    assert (r.exit_code, hand.workers, hand.windows) == (1, [], []), r.output
    assert not update_job.status_file(paths).exists() and not list(paths.logs.glob("update-*.log"))


# ------------------------------------------------------------------------------ W3: who runs from a venv
def test_a_process_running_from_a_venv_stops_the_update_before_anything_changes(tmp_path: Path) -> None:
    app, paths = updated(tmp_path)
    z = make_zip(tmp_path, "0.3.0")
    sleeper = Sleeper(paths)
    try:
        before = on_disk(app, paths)
        for run in (
            lambda e: update.update_app(z, app, e, paths=paths),
            lambda e: update.rollback_app(app, e, paths=paths),
        ):
            fake = Env()
            err = raised(lambda run=run, fake=fake: run(env_of(fake, holder_wait_s=0.3)))
            assert err is not None and err.code == "NS-E706", f"the update went on: {fake.calls}"
            assert f"process {sleeper.pid}" in err.detail and "python" in err.detail.lower()
            assert "Nothing was changed" in err.detail and "sync" not in fake.calls
            assert on_disk(app, paths) == before
    finally:
        sleeper.stop()
    # the way out the message names: close it, run it again
    assert update.rollback_app(app, env_of(Env(), holder_wait_s=0.3), paths=paths).new_version == "0.1.0"
    # the worker's own process among them (its venv copy could not be made): said at once, as what it is
    me = [update.Holder(os.getpid(), "python.exe", "12:00:00")]
    slept: list[float] = []
    err = raised(lambda: update.wait_no_holders(paths, 60.0, find=lambda: me, sleep=slept.append))
    assert err is not None and err.code == "NS-E706" and slept == []
    assert "its private copy of the supervisor environment could not be made" in err.detail


def test_the_engine_must_be_gone_not_only_quiet(tmp_path: Path) -> None:
    """`/status` stops answering before the engine's processes have ended: the worker waits for them, up to
    its bound. One that ends inside the bound costs nothing; one that stays is named and nothing changes."""
    app, paths = updated(tmp_path)
    sleeper = Sleeper(paths, "studio")
    relaunch = Recorder()
    try:
        wait_gone = getattr(control, "wait_gone", None)
        assert wait_gone is not None, "control has no wait for the engine's recorded processes"
        assert wait_gone([sleeper.pid], wait_s=0.3) == [sleeper.pid]
        before, t0 = on_disk(app, paths), time.monotonic()
        code = update_job.run_worker(
            paths, lambda: outcome(update.rollback_app(app, env_of(Env(), holder_wait_s=0.6), paths=paths)),
            relaunch=relaunch,
        )
        st = update_job.read_status(paths)
        assert code == 1 and st["state"] == "failed" and st["message"].startswith("NS-E706"), st
        assert f"process {sleeper.pid}" in st["message"] and time.monotonic() - t0 >= 0.6
        assert on_disk(app, paths)["app"] == before["app"] and "Nothing was changed" in st["message"]
        threading.Timer(0.5, sleeper.stop).start()             # the slow engine: gone inside the bound
        res = update.rollback_app(app, env_of(Env(), holder_wait_s=30.0), paths=paths)
        assert res.new_version == "0.1.0" and wait_gone([sleeper.pid], wait_s=5.0) == []
    finally:
        sleeper.stop()


# ---------------------------------------------------- W6 to W10: a sync that cannot finish, and its words
class Recorder:
    """A relauncher that only records (`update_job.Relauncher`)."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def observe(self) -> None:
        self.calls.append("observe")

    def restart(self) -> None:
        self.calls.append("restart")


def outcome(res: update.UpdateResult) -> update_job.Outcome:
    said = f"Now on {res.new_version} (was {res.old_version})."
    return update_job.Outcome(said, res.old_version, res.new_version)


def test_a_file_held_open_during_the_sync_never_says_rolled_back(tmp_path: Path) -> None:
    app, paths = updated(tmp_path)
    for name in ("studio", "supervisor"):
        fill(paths, name, "0.2.0")
    uv, relaunch, log = Uv(paths), Recorder(), paths.logs / "u.log"
    env = env_of(Env(), sync=uv.sync)

    def body() -> update_job.Outcome:
        return outcome(update.rollback_app(app, env, paths=paths))

    held = open(paths.venvs / "supervisor" / "Scripts" / "ns.exe", "rb")   # noqa: SIM115 - QA's os error 32
    try:
        code = update_job.run_worker(paths, body, log=log, relaunch=relaunch)
    finally:
        held.close()
    st, said = update_job.read_status(paths), log.read_text(encoding="utf-8")
    assert (app / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"       # the program did go back
    assert holds(paths, "studio") == "nightshift-0.1.0.dist-info"
    assert holds(paths, "supervisor") == "nightshift-0.2.0.dist-info"            # the venv did not
    assert OLD_CLAIM not in st["message"] + said, "it claims a rollback that is not on disk"
    assert code == 1 and st["state"] == "failed"
    msg = st["message"]
    assert "The rollback to 0.1.0 is not finished" in msg and "os error 32" in msg
    assert "0.1.0's program is in place, but the Python environment 'supervisor' could not be" in msg
    assert "it may still hold 0.2.0's packages" in msg
    assert "Nightshift was not started" in msg and "restart" not in relaunch.calls
    assert uv.tries.count("supervisor") == getattr(update, "SYNC_TRIES", 0) == 3  # waited, tried again
    assert st.get("venvs_left") == ["supervisor"] and msg.split(": ", 1)[1] in said
    # failed, then retried: the way out the message names, with the file free again
    assert "run 'ns update --rollback --detach'" in msg and "When it says 'Now on 0.2.0'" in msg
    from ns_supervisor import notify

    assert [n["message"] for n in notify.read_notes(paths)] == [msg]     # nothing runs: a note waits
    for _ in range(2):
        assert update_job.run_worker(paths, body, log=log, relaunch=relaunch) == 0
    assert holds(paths, "studio") == holds(paths, "supervisor") == "nightshift-0.1.0.dist-info"
    assert update_job.read_status(paths)["message"].startswith("Now on 0.1.0")


def test_an_update_whose_restore_resync_fails_says_which_environment_is_left(tmp_path: Path) -> None:
    app, paths = program(tmp_path), Paths(tmp_path / "ns")
    for name in ("studio", "supervisor"):
        fill(paths, name, "0.1.0")
    uv, relaunch = Uv(paths), Recorder()
    env = env_of(Env(), sync=uv.sync)
    z = make_zip(tmp_path)
    held = open(paths.venvs / "supervisor" / "Scripts" / "ns.exe", "rb")   # noqa: SIM115 - one bad file
    try:
        code = update_job.run_worker(
            paths, lambda: outcome(update.update_app(z, app, env, paths=paths)), relaunch=relaunch
        )
    finally:
        held.close()
    msg = update_job.read_status(paths)["message"]
    assert code == 1 and (app / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    assert holds(paths, "studio") == "nightshift-0.1.0.dist-info"      # filled with 0.2.0, then put back
    assert OLD_CLAIM not in msg
    assert "The update to 0.2.0 failed at the refresh of the Python environments" in msg
    assert "0.1.0's program is back, but the Python environment 'supervisor' could not be refreshed" in msg
    assert "Nightshift was not started" in msg and "restart" not in relaunch.calls
    assert "run the update again with the 'Nightshift - Update' shortcut or 'ns update <zip> --detach'" in msg
    # the other end: the restore's re-sync works, and the words say the whole of 0.1.0 is back
    fill(paths, "supervisor", "0.1.0")
    err = raised(lambda: update.update_app(z, app, env_of(Env(verify_ok=False), sync=uv.sync), paths=paths))
    assert err is not None and "failed at the check of the new version" in err.detail
    assert "0.1.0's program is in place again, with its Python environments." in err.detail
    assert holds(paths, "studio") == holds(paths, "supervisor") == "nightshift-0.1.0.dist-info"


def test_every_update_failure_ends_with_where_things_are(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_ns_update_git import FakeGit

    app, paths = updated(tmp_path)
    z, where_now = make_zip(tmp_path, "0.3.0"), getattr(update, "where_now", None)

    def stuck(src: Path, dst: Path, **kw: Any) -> None:
        raise PermissionError(32, "being used by another process")

    def staged() -> None:
        with monkeypatch.context() as m:
            m.setattr(update, "rename_with_retry", stuck)
            update.update_app(z, app, Env().env(), paths=paths)

    git = FakeGit(dirty=" M ns_common/x.py")
    faults = {
        "the folders cannot be renamed": staged,
        "verify fails": lambda: update.update_app(z, app, Env(verify_ok=False).env(), paths=paths),
        "the sync fails": lambda: update.update_app(z, app, Env(sync_error="uv exploded").env(), paths=paths),
        "local changes in a clone": lambda: update.git_update(app, Env().env(), paths=paths, git=git),
        "a rollback whose backup is gone": lambda: update.rollback_app(
            make_app(tmp_path / "bare"), Env().env(), paths=Paths(tmp_path / "bare-ns")
        ),
    }
    for name, fault in faults.items():
        err = raised(fault)
        assert err is not None and err.code == "NS-E702", name
        where = err.data.get("where")
        assert where and err.detail.endswith(where), f"{name}: no closing sentence in {err.detail!r}"
        assert OLD_CLAIM not in str(err), name
    assert where_now is not None and where_now(paths, app) == "In place now: Nightshift 0.2.0."
    fill(paths, "studio", "0.2.0")
    fill(paths, "supervisor", "0.1.0")
    assert where_now(paths, app) == (
        "In place now: the program of Nightshift 0.2.0, but the Python environment 'supervisor' has "
        "Nightshift 0.1.0 installed."
    )
    fill(paths, "supervisor", "0.2.0")
    assert where_now(paths, app) == "In place now: Nightshift 0.2.0, program and Python environments."
    assert where_now(paths, app.with_name("nowhere")).startswith("In place now: no program folder at ")


# --------------------------------------------------------------------------------- F2, W0: one at a time
def lock_holder(paths: Paths) -> tuple[subprocess.Popen[str], int]:
    """A child process that holds `run/update.lock` the way a running worker does, and its own pid (the
    interpreter's: a venv's `python.exe` is a launcher with another pid)."""
    code = (
        "import os, sys, time; from ns_common.singleton import FileLock; lock = FileLock(sys.argv[1]); "
        "lock.acquire(); print(os.getpid(), flush=True); time.sleep(120)"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(paths.home / "run" / "update.lock")], stdout=subprocess.PIPE,
        text=True, cwd=str(ROOT), env={**os.environ, "PYTHONPATH": str(ROOT)},
    )
    assert child.stdout is not None
    return child, int(child.stdout.readline().strip())


def test_a_second_update_is_refused_while_one_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    app, paths = updated(tmp_path)
    hand = Hand(monkeypatch)
    child, holder = lock_holder(paths)
    try:
        running_log = paths.logs / "update-20261110-114303.log"
        update_job.write_status(paths, "running", "Updating.", pid=holder, log=str(running_log))
        state_before, disk_before = update_job.status_file(paths).read_bytes(), on_disk(app, paths)
        for args in ([str(make_zip(tmp_path, "0.3.0"))], ["--rollback"], ["--rollback", "--detach"]):
            r = invoke(paths.home, "update", *args)
            out = " ".join(r.output.split())
            assert r.exit_code == 1 and "NS-E705" in out, out
            assert f"process {holder}" in out and "Nothing was changed" in out and str(running_log) in out
        assert update_job.status_file(paths).read_bytes() == state_before
        assert (hand.workers, hand.windows) == ([], []) and on_disk(app, paths) == disk_before
    finally:
        child.kill()
        child.wait(timeout=30)
    assert invoke(paths.home, "update", "--rollback", "--detach").exit_code == 0 and len(hand.workers) == 1


def test_only_the_lock_holder_writes_the_state_file(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "home")
    inside, leave, ran = threading.Event(), threading.Event(), []

    def first_body() -> update_job.Outcome:
        inside.set()
        leave.wait(60)
        return update_job.Outcome("Now on 0.2.0 (was 0.1.0).", "0.1.0", "0.2.0")

    first = threading.Thread(target=lambda: update_job.run_worker(paths, first_body, old_version="0.1"))
    first.start()
    try:
        assert inside.wait(30)
        mine = update_job.status_file(paths).read_bytes()
        log = paths.logs / "second.log"
        code = update_job.run_worker(
            paths, lambda: ran.append(1) or update_job.Outcome("x"), log=log, old_version="0.1.0"
        )
        said = log.read_text(encoding="utf-8")
        assert code == 5 and ran == [], "a second worker ran beside the first"
        assert "Another update, rollback or repair of this Nightshift is running" in said
        assert f"process {os.getpid()}" in said and "This one did nothing." in said
        assert update_job.status_file(paths).read_bytes() == mine
    finally:
        leave.set()
        first.join(60)
    assert update_job.read_status(paths)["state"] == "done"
    assert update_job.run_worker(paths, lambda: update_job.Outcome("y")) == 0    # the lock is free again


def test_a_worker_whose_caller_never_exits_changes_nothing(tmp_path: Path) -> None:
    paths, ran, slept = Paths(tmp_path / "home"), [], []
    err = raised(lambda: update_job.wait_for_parent(7, alive=lambda pid: True, sleep=slept.append, max_s=2.0))
    assert err is not None and err.code == "NS-E706" and sum(slept) == 2.0, "it went on after the bound"
    assert "process 7" in err.detail and "did not end within 2 s" in err.detail

    def wait(pid: int) -> None:
        update_job.wait_for_parent(pid, alive=lambda p: True, sleep=lambda s: None, max_s=1.0)

    code = update_job.run_worker(
        paths, lambda: ran.append(1) or update_job.Outcome("x"), wait_pid=7, wait=wait, relaunch=Recorder()
    )
    st = update_job.read_status(paths)
    assert code == 1 and ran == [] and st["state"] == "failed"
    assert st["message"].startswith("NS-E706") and "Nothing was changed" in st["message"]


# ------------------------------------------------------------------- 4b.4: the worker dies between steps
def die(*a: Any, **k: Any) -> Any:
    raise Died


ROWS = {
    "before anything changed": ("rollback", "stop", "stopped before it changed anything. 0.2.0 is in place."),
    "update, between the renames": ("update", "rename2", "while it was swapping the program folders"),
    "update, during the sync": ("update", "sync", "0.3.0's program was put in place"),
    "rollback, settings prepared": ("rollback", "prepare", "stopped before the program was changed"),
    "rollback, inside the renames": ("rollback", "swap", "while it was swapping the program folders"),
    "rollback, during the sync": ("rollback", "sync", "the environments may still hold 0.2.0's packages"),
    "update, putting the old one back": ("update", "restore", "while it was putting 0.2.0 back"),
    "finished, not restarted": ("rollback", "restart", "stopped before it restarted Nightshift"),
}


@pytest.mark.parametrize("row", list(ROWS))
def test_a_dead_worker_is_described_by_the_step_it_died_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, row: str
) -> None:
    kind, where, words = ROWS[row]
    app, paths = updated(tmp_path)
    z, fake, relaunch = make_zip(tmp_path, "0.3.0"), Env(), Recorder()
    env = env_of(fake)
    if where == "stop":
        env.stop = die
    elif where == "sync":
        env.sync = die
    elif where == "restart":
        relaunch.restart = die  # type: ignore[method-assign]
    elif where == "prepare":
        monkeypatch.setattr(update, "_prepare_settings", die)
    elif where == "swap":
        monkeypatch.setattr(update, "_swap_programs", die)
    elif where == "restore":
        fake.verify_ok = False
        monkeypatch.setattr(update, "_restore", die)
    else:
        real, seen = update.rename_with_retry, []

        def second_dies(src: Path, dst: Path, **kw: Any) -> None:
            seen.append(src)
            if len(seen) == 2:
                raise Died
            real(src, dst, **kw)

        monkeypatch.setattr(update, "rename_with_retry", second_dies)

    def body() -> update_job.Outcome:
        if kind == "update":
            return outcome(update.update_app(z, app, env, paths=paths))
        return outcome(update.rollback_app(app, env, paths=paths))

    with pytest.raises(Died):
        update_job.run_worker(paths, body, old_version="0.2.0", relaunch=relaunch)
    left = update_job.read_status(paths)                      # the state file as the dead worker left it
    assert left["state"] == "running" and left["pid"] == os.getpid()
    shown = update_job.effective_status(left, alive=lambda pid: False)
    assert shown["state"] == "failed" and words in shown["message"], shown["message"]
    assert "previous version is still in place" not in shown["message"]
    # a second worker can start afterwards: the dead one's lock went with it
    assert update_job.run_worker(paths, lambda: update_job.Outcome("z"), old_version="0.2.0") == 0


def test_a_dead_worker_of_an_older_release_is_described_from_the_disk(tmp_path: Path) -> None:
    app, paths = updated(tmp_path)              # 0.2.0 is in place: the swap had happened when it died
    fill(paths, "supervisor", "0.1.0")
    rc3 = {
        "state": "running", "old": "0.1.0", "new": "", "message": "Updating.", "pid": 999_999,
        "updated_at": datetime.now().isoformat(timespec="seconds"), "log": str(paths.logs / "u.log"),
    }
    update_job.status_file(paths).parent.mkdir(parents=True, exist_ok=True)
    update_job.status_file(paths).write_text(json.dumps(rc3), encoding="utf-8")
    shown = update_job.read_status_checked(paths, alive=lambda pid: False)
    where_now = getattr(update, "where_now", None)
    expected = where_now(paths, app) if where_now else "In place now:"
    assert shown["state"] == "failed" and expected in shown["message"], shown["message"]
    assert "the program of Nightshift 0.2.0" in shown["message"]
    assert "'supervisor' has Nightshift 0.1.0 installed" in shown["message"]
    assert "previous version is still in place" not in shown["message"]


# ------------------------------------------------------------------------- F5: the window, under 5.1
@needs_powershell
def test_the_follower_window_follows_to_the_end_under_powershell_5_1(tmp_path: Path) -> None:
    make = getattr(update_job, "follower_script", None)
    argv_of = getattr(update_job, "follower_argv", None)
    assert make is not None and argv_of is not None, "there is no progress window outside the venvs"
    paths = Paths(tmp_path / "home with a space")
    log, old_log = paths.logs / "update-new.log", paths.logs / "update-old.log"
    log.parent.mkdir(parents=True)
    update_job.write_status(paths, "done", "Now on 0.0.9 (EARLIER).", log=str(old_log), exit_code=0)

    def run(pid: int) -> subprocess.CompletedProcess[str]:
        script = make(update_job.status_file(paths), log, pid, wait=False)
        assert all(ord(ch) < 128 for ch in script)
        return subprocess.run(argv_of(script), capture_output=True, text=True, timeout=120, check=False)

    def worker() -> None:
        time.sleep(1.5)                                  # the window is up before the worker's first word
        base = {"log": str(log), "pid": os.getpid(), "if_stopped_here": "It stopped early."}
        for line in ("step one", "step two"):
            with log.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            update_job.write_status(paths, "running", "Updating.", **base)
            time.sleep(1.2)
        with log.open("a", encoding="utf-8") as fh:
            fh.write("step three\n")
        update_job.write_status(paths, "done", "Now on 0.2.0 (was 0.1.0).", exit_code=0, **base)

    t = threading.Thread(target=worker)
    t.start()
    r = run(os.getpid())
    t.join(60)
    out = r.stdout
    assert r.returncode == 0, r.stdout + r.stderr
    assert "EARLIER" not in out, "the window showed the result of an earlier update"
    order = [out.index(s) for s in ("step one", "step two", "step three", "Now on 0.2.0 (was 0.1.0).")]
    assert order == sorted(order) and out.count("step two") == 1
    # a worker that died: the sentence of the step it died in, exit 1
    update_job.write_status(
        paths, "running", "Updating.", log=str(log), pid=999_999,
        if_stopped_here="The update stopped while it was swapping the program folders.",
    )
    r = run(999_999)
    assert r.returncode == 1 and "The update stopped while it was swapping the program folders." in r.stdout
    # a failed one: its message, exit 1
    update_job.write_status(paths, "failed", "NS-E706: close them.", log=str(log), pid=1, exit_code=1)
    r = run(os.getpid())
    assert r.returncode == 1 and "NS-E706: close them." in r.stdout
