"""D-39b: an environment an update, rollback or resync left half-filled is found and finished at the next
start, and nothing rewrites the environments while anything of the install runs from them (setup included).

The update side writes `<home>/run/venv-repair.json` before it changes the program and deletes it after the
last venv; the launch script replays it (tests/test_launchers.py); `ns doctor` and setup's checks find a venv
an older updater left on another version. New names are looked up with `getattr`/`importlib`, so on the base
every test fails at an assertion.
"""

from __future__ import annotations

import base64
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from installer.stage2.web import bootstrap
from installer.stage2.web.bootstrap import CmdResult
from ns_common.paths import Paths
from ns_common.singleton import read_owner
from ns_supervisor import doctor, notify, pwsh, update, update_job
from ns_supervisor.client import SupervisorClient
from tests.test_installer_web_controller import Setup
from tests.test_ns_update import Env, make_zip
from tests.test_update_handover import Died, Uv, env_of, fill, point_home_at, program, raised, updated

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "installer" / "launch.ps1"
needs_powershell = pytest.mark.skipif(pwsh.find_powershell() is None, reason="powershell not on PATH")


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update, "RENAME_PAUSE_S", 0.05)


def venvrepair() -> Any:
    try:
        return importlib.import_module("installer.stage2.web.venvrepair")
    except ImportError:
        return None


def record_file(paths: Paths) -> Path:
    return paths.home / "run" / "venv-repair.json"


def record(paths: Paths) -> dict[str, Any] | None:
    f = record_file(paths)
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else None


class Seen:
    """The order of what an update does: each rename and each uv command, with the record as it is then."""

    def __init__(self, paths: Paths, monkeypatch: pytest.MonkeyPatch) -> None:
        self.paths = paths
        self.events: list[tuple[str, str, dict[str, Any] | None]] = []
        real = update.rename_with_retry

        def rename(src: Path, dst: Path, **kw: Any) -> None:
            self.events.append(("rename", f"{src.name}->{dst.name}", record(paths)))
            real(src, dst, **kw)

        monkeypatch.setattr(update, "rename_with_retry", rename)
        self.uv = Uv(paths)

    def run(self, cmd: list[str]) -> tuple[int, str]:
        py = Path(cmd[cmd.index("--python") + 1])
        self.events.append(("uv", py.parent.parent.name, record(self.paths)))
        return self.uv(cmd)

    def sync(self, app: Path) -> None:
        update.resync_venvs(app, self.paths, uv="uv", run=self.run)

    def first(self, kind: str) -> tuple[str, str, dict[str, Any] | None]:
        return next(e for e in self.events if e[0] == kind)

    def uv_commands(self) -> list[str]:
        return [e[1] for e in self.events if e[0] == "uv"]


def recorded_argv(rec: dict[str, Any]) -> list[list[str]]:
    return [c["argv"] for c in rec["commands"]]


# ------------------------------------------------------------------- 15: the record's place in each sequence
def test_the_repair_record_is_written_before_the_swap_and_removed_after_the_last_venv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, paths = program(tmp_path), Paths(tmp_path / "ns")
    for name in ("studio", "supervisor"):
        fill(paths, name, "0.1.0")
    seen = Seen(paths, monkeypatch)
    ran: list[list[str]] = []
    real_run = seen.run
    seen.run = lambda cmd: ran.append(cmd) or real_run(cmd)  # type: ignore[method-assign]
    # update: written before the first rename, for the NEW program at <app>, with the commands the sync runs
    z = make_zip(tmp_path, extra={"ns_supervisor/cli.py": "x"})
    update.update_app(z, app, env_of(Env(), sync=seen.sync), paths=paths)
    rename = seen.first("rename")
    assert rename[1] == "NightShift->NightShift.prev"
    rec = rename[2]
    assert rec is not None, "the update renamed the program folder with no repair record on disk"
    assert rec["schema"] == 1 and rec["version"] == "0.2.0" and rec["by"] == "update"
    assert Path(rec["app"]) == app.absolute() and rec["match"] == "marker"
    assert rec["for_app"] == (app / ".ns-app.done").read_text(encoding="utf-8")    # the new program's
    assert [a[1:] for a in recorded_argv(rec)] == [c[1:] for c in ran]     # exactly what the sync ran
    assert all(a[0] == rec["uv"] for a in recorded_argv(rec))     # (the home's uv, wherever it is found)
    assert [c["venv"] for c in rec["commands"]] == ["studio", "supervisor"]
    assert all(e[2] is not None for e in seen.events if e[0] == "uv"), "a venv was refreshed with no record"
    assert record(paths) is None, "the record outlived the last venv"
    # rollback: written after the settings were prepared, before the first of the three renames
    seen.events.clear()
    ran.clear()
    update.rollback_app(app, env_of(Env(), sync=seen.sync), paths=paths)
    rec = seen.first("rename")[2]
    assert rec is not None and rec["version"] == "0.1.0" and rec["by"] == "rollback"
    assert seen.uv_commands() == ["studio", "supervisor"] and record(paths) is None
    # a forward update whose check fails: the record names the OLD program before it is put back
    seen.events.clear()
    err = raised(lambda: update.update_app(
        make_zip(tmp_path, "0.3.0"), app, env_of(Env(verify_ok=False), sync=seen.sync), paths=paths))
    assert err is not None and err.code == "NS-E702"
    back = [e for e in seen.events if e[0] == "rename" and e[1].endswith(".failed")]
    assert back and back[0][2] is not None and back[0][2]["version"] == "0.1.0", seen.events
    assert record(paths) is None and (app / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
    # --git: after the checkout (the commit's own files decide), matched by VERSION (a clone has no marker)
    seen.events.clear()
    git_calls: list[list[str]] = []

    def git(args: list[str]) -> tuple[int, str]:
        git_calls.append(args)
        seen.events.append(("git", args[0], record(paths)))
        return 0, "" if args[0] == "status" else "abc123"

    update.git_update(app, env_of(Env(), sync=seen.sync), paths=paths, git=git, latest_tag=lambda: "v0.1.0")
    checkout = seen.events.index(next(e for e in seen.events if e[1] == "checkout"))
    first_uv = seen.events.index(seen.first("uv"))
    assert checkout < first_uv and seen.events[first_uv][2] is not None
    assert seen.events[first_uv][2]["by"] == "git" and seen.events[first_uv][2]["match"] == "version"
    assert record(paths) is None


def test_a_forward_sync_that_does_not_finish_keeps_the_record_and_the_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D-39a review note 3: the way out of a worker that died in the forward sync must keep <app>.prev."""

    def dies(a: Path) -> None:
        raise Died()

    for case, stub in (("no-stub", False), ("stub", True)):
        base = tmp_path / case
        base.mkdir()
        app, paths = program(base), Paths(base / "ns")
        fill(paths, "studio", "0.1.0")
        if stub:       # the home's launch script replays a record (setup or the first start put it there)
            (paths.home).mkdir(parents=True, exist_ok=True)
            shutil.copy(STUB, paths.home / "nightshift-launch.ps1")
        log = paths.logs / "update-x.log"
        update_job.write_status(paths, "running", "Updating.", log=str(log), pid=os.getpid(), old="0.1.0")
        with pytest.raises(Died):
            update.update_app(make_zip(base, extra={"ns_supervisor/cli.py": '"--detach"'}), app,
                              env_of(Env(), sync=dies), paths=paths)
        said = update_job.read_status(paths)["if_stopped_here"]
        assert "run the update again" not in said, "running the update again replaces the only way back"
        assert "ns update --rollback --detach" in said and "When it says 'Now on 0.1.0'" in said, said
        assert (base / "NightShift.prev" / "VERSION").read_text(encoding="utf-8").strip() == "0.1.0"
        rec = record(paths)
        assert rec is not None and rec["version"] == "0.2.0", "the next start has nothing to finish from"
        first = "open Nightshift from a Desktop shortcut: it finishes the Python environments before anything"
        assert (first in said) is stub, said


# -------------------------------------------------------------------------- 18: a record of another program
def test_a_stale_record_is_dropped_and_said(tmp_path: Path) -> None:
    app, paths = updated(tmp_path)
    fill(paths, "studio", "0.2.0")
    stale = {"schema": 1, "app": str(app), "match": "marker", "for_app": "0.0.9|x|y", "version": "0.0.9",
             "by": "update", "uv": "uv", "env": {}, "commands": [{"venv": "studio", "argv": ["uv", "pip"]}]}
    record_file(paths).parent.mkdir(parents=True, exist_ok=True)
    record_file(paths).write_text(json.dumps(stale), encoding="utf-8")
    vr = venvrepair()
    assert vr is not None and vr.matches(stale, app) is False
    # ns update: deleted, one log line, and this run's own record goes after its last venv
    update.update_app(make_zip(tmp_path, "0.3.0"), app, env_of(Env(), sync=Uv(paths).sync), paths=paths)
    assert record(paths) is None
    lines = "".join(f.read_text(encoding="utf-8") for f in paths.logs.glob("update-*.jsonl"))
    assert lines.count('"venv_record_stale"') == 1, lines


# ----------------------------------------------------------- 19: an environment an older updater left mixed
def test_a_mixed_venv_without_a_record_is_found_by_doctor_and_by_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from installer.stage2.steps.s04_venvs import S04Venvs
    from installer.stage2.testing import example_answers, make_test_context
    from installer.stage2.web import appcopy
    from ns_common.config import load_manifest
    from tests.test_installer_rerun import b03_boot, staged_bundle

    # ns doctor, row "python environments": FAIL with the words the launch script uses
    app, paths = updated(tmp_path / "doctor")
    point_home_at(paths, app)
    fill(paths, "studio", "0.2.0")
    fill(paths, "supervisor", "0.1.14")                    # 0.1.14's updater broke off in this venv

    def refuse(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no")

    client = SupervisorClient("http://sup", client=httpx.Client(transport=httpx.MockTransport(refuse)))
    rows = {name: (ok, detail) for name, ok, detail in doctor.diagnose(paths, client)}
    assert "python environments" in rows, "ns doctor does not look at what the environments hold"
    ok, detail = rows["python environments"]
    assert ok is False and detail == (
        "The Python environment 'supervisor' holds Nightshift 0.1.14, but the program is 0.2.0 (an update by "
        "an older version was interrupted). Run INSTALL.cmd from the Nightshift 0.2.0 zip and choose this "
        "folder (Repair)."
    )
    second = paths.venvs / "studio" / "Lib" / "site-packages" / "nightshift-0.1.0.dist-info"
    second.mkdir()
    (second / "METADATA").write_text("Version: 0.1.0\n", encoding="utf-8")    # two of them in one venv
    assert "'studio' holds Nightshift 0.1.0 and 0.2.0" in dict(
        (n, d) for n, _o, d in doctor.diagnose(paths, client))["python environments"]
    # s04: done by its flags and its probes, but a venv holds another version, or a record waits
    home = tmp_path / "setup" / "home"
    appcopy.install_app(staged_bundle(tmp_path / "setup" / "zip", "0.1.7"), home)
    ctx = make_test_context(tmp_path / "setup", raw_answers=example_answers(), resolve=True,
                            app_path=home / "app", manifest=load_manifest(home / "app" / "manifest.json"))
    S04Venvs().run(ctx)
    assert S04Venvs().check(ctx) is True
    sp = ctx.paths.venvs / "studio" / "Lib" / "site-packages" / "nightshift-0.1.6.dist-info"
    sp.mkdir(parents=True)
    (sp / "METADATA").write_text("Version: 0.1.6\n", encoding="utf-8")
    assert S04Venvs().check(ctx) is False, "setup's check does not see a venv on another version"
    shutil.rmtree(sp)
    record_file(ctx.paths).parent.mkdir(parents=True, exist_ok=True)
    record_file(ctx.paths).write_text("{torn", encoding="utf-8")
    assert S04Venvs().check(ctx) is False, "setup's check does not see an unfinished update"
    S04Venvs().run(ctx)
    assert not record_file(ctx.paths).exists() and S04Venvs().check(ctx) is True    # s04 finished it
    # b03 (the setup environment, outside the venvs): the same for the studio venv
    boot = b03_boot(tmp_path / "b03")
    boot.venv_python.parent.mkdir(parents=True, exist_ok=True)
    boot.venv_python.write_bytes(b"MZ")
    boot.venv_marker.write_text(boot.venv_stamp(), encoding="utf-8")
    assert bootstrap.B03SetupEnv().check(boot) is True
    info = boot.home / "venvs" / "studio" / "Lib" / "site-packages" / "nightshift-0.0.1.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text("Version: 0.0.1\n", encoding="utf-8")
    assert bootstrap.B03SetupEnv().check(boot) is False, "b03 keeps a studio venv on another version"


# ------------------------------------------------- 20: setup takes the same lock and the same holder check
def _hold_lock(lock: Path) -> subprocess.Popen[str]:
    code = (
        "import sys, time; from pathlib import Path; from ns_common.singleton import FileLock; "
        "lock = FileLock(Path(sys.argv[1])); lock.acquire(); print('held', flush=True); time.sleep(120)"
    )
    proc = subprocess.Popen([sys.executable, "-c", code, str(lock)], cwd=str(ROOT), stdout=subprocess.PIPE,
                            text=True)
    assert proc.stdout is not None and proc.stdout.readline().strip() == "held"
    return proc


def test_setup_refuses_beside_a_running_update_and_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from installer.stage2.web import controller

    s = Setup(tmp_path, monkeypatch)
    (s.home / "venvs" / "supervisor" / "Scripts").mkdir(parents=True)
    (s.home / "venvs" / "supervisor" / "Scripts" / "ns.exe").write_bytes(b"MZ")
    s.accept()
    holder = _hold_lock(s.home / "run" / "update.lock")      # an update worker holds the update lock
    try:
        assert s.ctl.do_action("start")[0] == 200
        state = s.until(lambda st: st["failure"] is not None or st["run"] in ("finished", "waiting"),
                        what="setup to stop or go on")
        failure = state["failure"] or {}
        assert failure.get("code") == "NS-E705", f"setup went on beside a running update: {state['run']}"
        owner = read_owner(s.home / "run" / "update.lock")      # (a venv's python.exe starts the real one)
        assert f"process {owner}" in failure["detail"] and "Nothing was changed" in failure["detail"]
        assert not any(len(c) > 1 and c[1] in ("venv", "pip") for c in s.cmd.calls), "it changed a venv"
    finally:
        holder.kill()
        holder.wait(timeout=30)
    # the lock is free; a program still runs from the venvs after the engine was asked to stop
    monkeypatch.setattr(controller, "HOLDER_WAIT_S", 0.3, raising=False)
    monkeypatch.setattr(controller, "HOLDER_PAUSE_S", 0.05, raising=False)
    py = s.home / "venvs" / "studio" / "Scripts" / "python.exe"
    listing = f"4711|{py}|\"{py}\" -m ns_studio.cli run\n1|C:\\x.exe|x\n"
    s.cmd.fail["Get-CimInstance"] = CmdResult(0, listing, "")
    s.cmd.calls.clear()
    assert s.ctl.do_action("retry")[0] == 200 or s.ctl.do_action("start")[0] == 200
    state = s.until(lambda st: (st["failure"] or {}).get("code") == "NS-E706"
                    or st["run"] == "finished", what="the holder check")
    failure = state["failure"] or {}
    assert failure.get("code") == "NS-E706" and "python.exe (process 4711)" in failure["detail"]
    assert any(c[0].endswith("ns.exe") and c[-1] == "stop" for c in s.cmd.calls), "the engine was not stopped"
    assert not any(len(c) > 1 and c[1] in ("venv", "pip") for c in s.cmd.calls)


# ------------------------------------------------------------------------------- D-39a review notes 1 and 2
class FailingRestart:
    def observe(self) -> None:
        pass

    def restart(self) -> None:
        raise RuntimeError("the engine did not answer")


def test_an_applied_update_whose_restart_failed_is_not_called_incomplete(tmp_path: Path) -> None:
    paths = Paths(tmp_path / "ns")
    out = update_job.Outcome("Now on 0.2.0 (was 0.1.0).", "0.1.0", "0.2.0", updated=True)
    code = update_job.run_worker(
        paths, lambda: out, log=paths.logs / "update-x.log", relaunch=FailingRestart(), watched=True
    )
    st = update_job.read_status(paths)
    assert code == 1 and st["updated"] is True and st["restart_failed"] is True
    (note,) = notify.read_notes(paths)
    assert "did not complete" not in note["title"], note
    assert note["title"] == "The update or rollback was applied, but Nightshift did not start again"
    page = (ROOT / "ns_dashboard" / "ui" / "src" / "pages" / "Settings.tsx").read_text(encoding="utf-8")
    assert "u.updated ?" in page
    assert "was applied, but Nightshift did not start again by itself" in (
        ROOT / "ns_dashboard" / "static" / "app.js").read_text(encoding="utf-8")
    assert "The update was applied, but Nightshift did not start again" in (
        ROOT / "launchers" / "update.ps1").read_text(encoding="ascii")


def test_no_progress_window_waits_for_ever_without_a_desktop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths(tmp_path / "ns")
    opened: list[list[str]] = []
    monkeypatch.setattr(update_job, "open_window", lambda argv, cwd: opened.append(argv) or 1)
    monkeypatch.setattr(update_job, "has_desktop", lambda: False, raising=False)
    assert update_job.start_follower(paths, os.getpid(), paths.logs / "update-x.log") is False
    assert opened == [], "a window was opened in a session with no desktop"
    script = update_job.follower_script(update_job.status_file(paths), paths.logs / "u.log", 1)
    assert "Read-Host" not in script, "the window waits for Enter with no bound"
    if pwsh.find_powershell() is None:
        return
    # where no key can be read, it ends by itself at once after the result
    log = paths.logs / "u.log"
    log.parent.mkdir(parents=True)
    log.write_text("one\n", encoding="utf-8")
    update_job.write_status(paths, "done", "Now on 0.2.0.", log=str(log))
    argv = update_job.follower_argv(update_job.follower_script(update_job.status_file(paths), log, 1))
    t0 = time.monotonic()
    r = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0 and "Result: Now on 0.2.0." in r.stdout and time.monotonic() - t0 < 60
    decoded = base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")
    assert "AddSeconds(1800)" in decoded


# ------------------------------------------- D-39b review blocker 1: every start from Python passes one gate
def _unfinished_install(tmp_path: Path) -> tuple[Path, Paths]:
    """A home whose program (0.1.0, at `app`) has a repair record waiting: an update broke off in the sync."""
    app, paths = program(tmp_path), Paths(tmp_path / "ns")
    point_home_at(paths, app)
    (paths.home / "config").mkdir(parents=True, exist_ok=True)
    (paths.home / "config" / "runtime.json").write_text('{"dev_mode": true}', encoding="utf-8")
    vr = venvrepair()
    assert vr is not None
    vr.write(paths.home, app=app, for_app="0.1.0|x|y", version="0.1.0", uv="uv", env={},
             commands=[("studio", ["uv", "pip", "install"])], by="update", match="version")
    return app, paths


class Spawns:
    """The engine as the routes start it: never really started here, only counted."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ns_supervisor import client

        self.calls: list[str] = []
        monkeypatch.setattr(client.SupervisorClient, "is_up", lambda self: False)
        monkeypatch.setattr(client, "spawn_supervisor", lambda paths, **kw: self.calls.append("engine"))


SAID = "Nightshift was not started: an interrupted update or rollback left the Python environment 'studio'"


def test_every_python_start_of_the_engine_refuses_an_unfinished_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from ns_studio import cli as studio_cli
    from ns_studio.acquirer import StudioAcquirer
    from ns_supervisor import cli, client

    app, paths = _unfinished_install(tmp_path)
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(paths.home))
    spawns = Spawns(monkeypatch)
    # the one gate: client.ensure_running, before anything is spawned
    down = client.SupervisorClient("http://127.0.0.1:9")
    err = raised(lambda: client.ensure_running(down, paths, wait_s=0.1))
    assert err is not None and err.code == "NS-W210", "the engine was started on unfinished environments"
    assert SAID in err.detail and "INSTALL.cmd from the Nightshift 0.1.0 zip" in err.detail
    assert spawns.calls == []
    # `studio run` and every studio command that needs a model: the acquirer connects through the gate
    acq = StudioAcquirer(update.Runtime.default("gpu"), paths)
    err = raised(acq._connect)
    assert err is not None and err.code == "NS-W210" and spawns.calls == []
    # `studio dashboard`: one line, exit 1, nothing started
    r = CliRunner().invoke(studio_cli.app, ["dashboard"], env={"NIGHTSHIFT_HOME": str(paths.home)})
    assert r.exit_code == 1 and SAID in " ".join(r.output.split()), r.output
    assert spawns.calls == []
    # `ns verify`: says it, starts no engine (the checks then find no engine: exit 1)
    monkeypatch.setattr(cli.verify, "run_verify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
    monkeypatch.setattr(cli, "_client", lambda p: down)                 # never another engine on this PC
    monkeypatch.setattr(cli, "_stop_started_engine", lambda c: None)
    r = CliRunner().invoke(cli.app, ["verify"], env={"NIGHTSHIFT_HOME": str(paths.home)})
    out = " ".join(r.output.split())
    assert out.startswith("Could not start the engine: NS-W210: ") and SAID in out, r.output
    assert r.exit_code == 1 and spawns.calls == []
    # no record, or a record of another program: the start goes on as before
    rec = record(paths)
    assert rec is not None
    record_file(paths).write_text(json.dumps({**rec, "version": "0.0.9"}), encoding="utf-8")       # stale
    raised(lambda: client.ensure_running(down, paths, wait_s=0.1))
    record_file(paths).unlink()
    raised(lambda: client.ensure_running(down, paths, wait_s=0.1))
    assert spawns.calls == ["engine", "engine"]


def test_the_engine_refuses_at_its_own_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ns_supervisor import app as engine

    _app, paths = _unfinished_install(tmp_path)
    monkeypatch.setenv("NIGHTSHIFT_HOME", str(paths.home))
    monkeypatch.setattr(engine, "_serve", lambda *a: pytest.fail("it served on unfinished environments"))
    assert engine.main([]) == 4
    assert [n["message"] for n in notify.read_notes(paths)] == [
        update.venvrepair.start_refusal(paths.home, _app)]
    assert engine.main([]) == 4 and len(notify.read_notes(paths)) == 1          # said once
    record_file(paths).unlink()
    assert engine.refuse_unfinished(paths) == 0


def test_the_gate_needs_nothing_but_the_standard_library(tmp_path: Path) -> None:
    """A process that already runs from a half-filled environment can still ask: the check runs with
    `python -I -S` (no site-packages at all) and the repository on its path, and answers both ways."""
    app, paths = _unfinished_install(tmp_path)
    gate = ("import sys; sys.path.insert(0, sys.argv[1]); from installer.stage2.web import venvrepair; "
            "third = ('pydantic', 'httpx', 'yaml', 'psutil'); "
            "assert not [m for m in sys.modules if m.split('.')[0] in third]; "
            "sys.exit(venvrepair.main(sys.argv[2:]))")

    def ask() -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-I", "-S", "-c", gate, str(ROOT), str(app), str(paths.home)],
                              capture_output=True, text=True, timeout=60)

    r = ask()
    assert r.returncode == 4 and r.stdout.startswith("NS-W210: " + SAID), r.stdout + r.stderr
    record_file(paths).unlink()
    r = ask()
    assert r.returncode == 0 and r.stdout == "", r.stderr


@pytest.mark.skipif(pwsh.find_powershell() is None, reason="powershell not on PATH")
def test_a_launcher_run_directly_starts_no_engine_on_an_unfinished_record(tmp_path: Path) -> None:
    import venv as venv_module

    home = tmp_path / "home"
    app = home / "app"
    shutil.copytree(ROOT / "launchers", app / "launchers")
    for pkg in ("installer/__init__.py", "installer/stage2/__init__.py", "installer/stage2/shortcut_owner.py",
                "installer/stage2/web/__init__.py", "installer/stage2/web/venvrepair.py",
                "installer/stage2/web/deplock.py"):
        (app / pkg).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / pkg, app / pkg)
    shutil.copytree(ROOT / "ns_common", app / "ns_common", ignore=shutil.ignore_patterns("__pycache__"))
    (app / "VERSION").write_text("0.2.0\n", encoding="utf-8")
    (app / ".ns-app.done").write_text("0.2.0|a|b", encoding="utf-8")
    (home / "config").mkdir(parents=True)
    (home / "config" / "runtime.json").write_text('{"supervisor_port": 9}', encoding="utf-8")  # no engine
    venv_module.create(home / "venvs" / "supervisor", with_pip=False)
    vr = venvrepair()
    assert vr is not None
    vr.write(home, app=app, for_app="0.2.0|a|b", version="0.2.0", uv="uv", env={},
             commands=[("supervisor", ["uv", "pip"])], by="update")
    exe = pwsh.find_powershell()
    assert exe is not None
    r = subprocess.run([exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(app / "launchers" / "nightshift-dashboard.ps1")],
                       input="\n", capture_output=True, text=True, timeout=120)
    said = " ".join(r.stdout.split())
    assert r.returncode == 1 and "NS-W210: Nightshift was not started: an interrupted update" in said, said
    assert "The Nightshift engine did not start" not in said      # refused before the start, not timed out


# -------------------------------------------- review notes: setup's headless exit codes, a listing that fails
def test_unattended_setup_refuses_with_15_beside_an_update_and_14_when_it_cannot_tell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from installer.stage2.web import answers_view, controller
    from ns_common.singleton import FileLock
    from tests.test_installer_web_controller import draft

    s = Setup(tmp_path, monkeypatch)
    answers = tmp_path / "answers.json"
    answers_view.write_answers(answers, answers_view.draft_to_answers(draft(s.home)))
    s.cfg.answers, s.cfg.headless = answers, True
    (s.home / "venvs" / "studio").mkdir(parents=True)                  # an install is there
    monkeypatch.setattr(controller.subprocess, "run", lambda *a, **k: pytest.fail("the wizard was started"))
    monkeypatch.setattr(controller, "HOLDER_WAIT_S", 0.2, raising=False)
    monkeypatch.setattr(controller, "HOLDER_PAUSE_S", 0.05, raising=False)
    update_lock = FileLock(s.home / "run" / "update.lock")
    update_lock.acquire()                                              # an update runs
    try:
        assert s.ctl.run_headless() == 15
    finally:
        update_lock.release()
    assert any("NS-E705" in line for line in s.fx.console_lines)
    # the running programs cannot be listed: it refuses (closed) instead of taking that for "none"
    s.cmd.fail["Get-CimInstance"] = CmdResult(1, "", "Get-CimInstance : Access denied")
    assert s.ctl.run_headless() == 14
    told = " ".join(" ".join(s.fx.console_lines).split())
    assert "NS-E706" in told and "could not list the running programs" in told and "Access denied" in told
    assert not any(len(c) > 1 and c[1] in ("venv", "pip") for c in s.cmd.calls), "a venv was changed"