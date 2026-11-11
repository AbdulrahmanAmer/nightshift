# ruff: noqa: E501
"""Card night-fits-b: what the end of the nightly task does to the product's processes, and what the next start
finds. Real processes, started and ended by recorded pid by the test; the real Task Scheduler is never touched.

What Windows does at the task's `ExecutionTimeLimit` (Microsoft Learn): "a task will be stopped 72 hours after
it starts to run. You can change this by changing this setting" (TaskSettings.ExecutionTimeLimit); stopping a
task "Stops only the instances of a program started by a scheduled task. To stop other processes, you must use
the TaskKill command" (schtasks end); and "When the system is terminating a process, it does not terminate any
child processes that the process has created" (Terminating a Process). The product's task starts `cmd.exe`
(`schedule.action_arguments`), so the documented end terminates cmd.exe and nothing below it. Microsoft does
not document whether Task Scheduler also runs the action in a Job Object that it ends with it; if it did,
every process of the tree that did not break away would end at once. Both are modelled here, each with its
own test; which one the GPU PC's Task Scheduler does is the kit's check (`dev/kit/night-task-end.txt`).

1. The documented end (`test_the_tasks_end_ends_cmd_and_the_night_runs_on`): the test terminates the task's
   cmd.exe (TerminateProcess, as for the program the task started). Everything below it runs on: the launch
   script, the studio (its run and job still owned: the next start closes nothing), its FFmpeg (still holding
   its output file), its other child, the engine with llama and TTS. The night ends itself at its own
   deadline (night-fits-a); nothing is told as a dead night, because none is.
2. The whole tree ended (`test_the_exact_recovery_path_after_a_kill`, card test 21): every recorded process is
   ended mid-stage. The next start (the dashboard's `recover_studio_work`) closes the run and the job with the
   cause the evidence allows on this PC (the beat; no Windows log under pytest; the PC did not restart), the
   Queue card and the morning note say it, and the next nightly run continues the job from that stage and
   settles its card.
"""

from __future__ import annotations

import inspect
import json
import os
import random
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psutil
import pytest

from ns_common.db import Db
from ns_common.paths import Paths
from ns_studio import daily, jobs, ledger, ownership
from ns_studio.channels import crud
from tests import d31_driver as driver
from tests import night_tree
from tests.helpers import make_channel

REPO = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="the nightly task's tree is a Windows tree")


def _setup() -> tuple[Paths, Db]:
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    created = (datetime.now(UTC) - timedelta(weeks=11)).isoformat(timespec="seconds")
    crud.add_channel(paths, db, make_channel(format="master_first", created_at=created))
    return paths, db


def _alive(pid: int) -> bool:
    try:
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _end(pid: int) -> None:
    """End one process of the tree by its recorded pid, only while it is still ours (its command line)."""
    try:
        p = psutil.Process(pid)
        if p.name().lower() == "cmd.exe" or "tests.night_tree" in " ".join(p.cmdline()):
            p.kill()
            p.wait(10)
    except psutil.Error:
        pass


@contextmanager
def _night_tree(home: Path, tmp_path: Path) -> Iterator[tuple[subprocess.Popen[bytes], Path, Path]]:
    """Start the task's action (a real `cmd.exe /d /s /c`) with the stand-in tree below it and wait until the
    studio is inside stage d31_b with all its children; on every path end every recorded pid."""
    folder, ready = tmp_path / "tree", tmp_path / "ready"
    folder.mkdir()
    env = {**os.environ, "NIGHTSHIFT_HOME": str(home), "D31_HANG_AT": "d31_b", "D31_READY": str(ready),
           "D31_TREE": str(folder)}
    line = f'"{sys.executable}" -m tests.night_tree stub "{folder}"'
    cmd = subprocess.Popen(f'cmd.exe /d /s /c "{line}"', cwd=REPO, env=env,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        deadline = time.monotonic() + 120
        while not ready.exists():
            assert cmd.poll() is None, "the stand-in night ended before it reached d31_b"
            assert time.monotonic() < deadline, "the stand-in night never reached d31_b"
            time.sleep(0.1)
        yield cmd, folder, ready
    finally:
        recorded = night_tree.pids(folder)
        for name in reversed(night_tree.NAMES):  # children first, then the parents
            if name in recorded:
                _end(recorded[name])
        _end(cmd.pid)
        if cmd.poll() is None:
            cmd.kill()
        cmd.wait(10)


def _run_id(paths: Paths) -> str:
    [run] = [r for r in ownership.list_records(paths) if r.get("kind") == "run"]
    return str(run["id"])


def _beat(paths: Paths) -> dict[str, Any] | None:
    f = paths.home / "run" / "night-beat.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None


def _hhmm(when: datetime) -> str:
    return when.astimezone().strftime("%H:%M")


class NoEvents:
    """The event log of a PC on which nothing was shut down (the shape of `winevents.SystemLog.read`)."""

    def read(self, since: datetime) -> list[Any]:
        return []


def test_the_tasks_end_ends_cmd_and_the_night_runs_on(tmp_home: Path, tmp_path: Path) -> None:
    paths, db = _setup()
    with _night_tree(tmp_home, tmp_path) as (cmd, folder, _ready):
        recorded = night_tree.pids(folder)
        assert set(recorded) == set(night_tree.NAMES), recorded
        run_id = _run_id(paths)
        [job] = [r for r in ownership.list_records(paths) if r.get("kind") == "job"]
        held = paths.job_dir(str(job["id"])) / "render.mp4"

        psutil.Process(cmd.pid).kill()  # the documented end: TerminateProcess of the program the task started
        cmd.wait(10)
        time.sleep(2)

        assert not _alive(cmd.pid)
        survivors = {name: _alive(pid) for name, pid in recorded.items()}
        assert survivors == dict.fromkeys(night_tree.NAMES, True), survivors
        # the next start finds a live night: its run and job are owned, nothing is closed, nothing is told
        assert ownership.liveness(paths, run_id) == "live"
        assert ownership.liveness(paths, str(job["id"])) == "live"
        seams = {"events": NoEvents(), "boot": datetime.now(UTC) - timedelta(days=1)}
        out = ownership.recover(paths, db, **{k: v for k, v in seams.items()
                                              if k in inspect.signature(ownership.recover).parameters})
        assert not out.any
        assert not (tmp_home / "run" / "morning_note.json").exists()
        # FFmpeg still holds the file it writes
        with pytest.raises(PermissionError):
            held.unlink()
        beat = _beat(paths)
        assert beat is not None and beat["run_id"] == run_id


def test_the_exact_recovery_path_after_a_kill(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, db = _setup()
    with _night_tree(tmp_home, tmp_path) as (cmd, folder, _ready):
        recorded = night_tree.pids(folder)
        run_id = _run_id(paths)
        [job] = [r for r in ownership.list_records(paths) if r.get("kind") == "job"]
        job_id = str(job["id"])
        # the engine's own services end with it (its Job Object), before anything else is ended
        _end(recorded["engine"])
        deadline = time.monotonic() + 10
        while (_alive(recorded["llama"]) or _alive(recorded["tts"])) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(recorded["llama"]) and not _alive(recorded["tts"])
        # the rest of the tree, mid-stage, as an end of the whole tree would
        for name in ("ffmpeg", "child", "studio", "stub"):
            _end(recorded[name])
        _end(cmd.pid)
        assert not any(_alive(pid) for pid in recorded.values())

    # what the next start finds: the run and the job dead, the beat of the dead night, no file held
    assert ownership.liveness(paths, run_id) == "dead" and ownership.liveness(paths, job_id) == "dead"
    held = paths.job_dir(job_id) / "render.mp4"
    held.rename(held.with_suffix(".moved"))  # nobody holds it any more
    beat = _beat(paths)
    assert beat is not None and beat["run_id"] == run_id
    # the last sign of life: the beat, or a ledger line of the run written after it
    last = max([datetime.fromisoformat(beat["at"])] + [datetime.fromisoformat(x["at"]) for x in ledger.read(paths)
                                                       if x.get("run_id") == run_id])

    # the dashboard's start closes them, with what the evidence allows here (no Windows log under pytest)
    from ns_dashboard.handlers import build_state
    from ns_dashboard.supervisor_link import SupervisorLink

    driver.install(monkeypatch)  # the plan this process names stages from (the driver's own is the stubs)
    state = build_state(paths, fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2))
    assert state.worker.recover_studio is not None
    state.worker.recover_studio()

    head = (f"The night's run was interrupted at about {_hhmm(last)}, and what stopped it cannot be "
            "told: the PC did not restart, so the run's own process was ended: a crash, a person or a program "
            "ended it")
    notes = json.loads((tmp_home / "run" / "morning_note.json").read_text(encoding="utf-8"))["notes"]
    [note] = [n["message"] for n in notes]
    assert note == (f"{head}, while after-dark's video was at d31_b. It continues at the next nightly run, or "
                    f"press Retry in the Queue. {ownership.WHILE_NIGHTLY_ON}")
    rec = ownership.read_record(paths, job_id) or {}
    assert rec["state"] == "interrupted" and rec["interrupted_at_stage"] == "d31_b"
    assert rec["cause"] == "unknown" and rec["cause_said"] == head
    [row] = db.query("select * from dashboard_jobs where kind='run'")
    card = json.loads(row["params_json"])["result"]
    assert row["message"].startswith(f"Interrupted at d31_b: the night's run was interrupted at about "
                                     f"{_hhmm(last)}")
    assert card["error"] == {"code": "NS-E328", "hint": ownership.INTERRUPTED_HINT_KNOWN, "stage": "d31_b"}
    [run] = db.query("select * from runs where run_id=?", (run_id,))
    summary = json.loads(run["summary_json"])
    assert summary["cause"] == "unknown" and summary["cause_said"] == head
    state_, words = ownership.run_state(paths, run_id, run["started_at"], run["finished_at"], summary)
    assert state_ == "interrupted" and words == "interrupted: t" + head[1:]
    causes = [x for x in ledger.read(paths) if x.get("kind") == "night_cause"]
    assert [c["run_id"] for c in causes] == [run_id]

    # the next nightly run continues the job from that stage, and settles its card
    report = daily.run_daily(jobs.open_env(light=True), rng=random.Random(5))
    assert [(j["job_id"], j.get("resumed")) for j in report["jobs"]] == [(job_id, True)]
    assert driver.calls(tmp_home)[-4:] == ["d31_b:start", "d31_b:done", "d31_c:start", "d31_c:done"]
    [row] = db.query("select * from dashboard_jobs where kind='run'")
    assert row["status"] == "done"
    assert row["message"].endswith("The nightly run continued it and it finished.")
    assert "night's run was interrupted at about" in row["message"]
