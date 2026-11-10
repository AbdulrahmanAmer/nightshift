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
