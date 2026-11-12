"""Card verify-job-isolated T1 to T7: the model test's sample job never lands on the owner's channel.

The sample job is a small Python program standing in for `studio` (`FAKE_STUDIO`): it opens its home with the
studio's own code (`jobs.open_env`, `resolve_channels`, `make_job`: the owner file, the ledger row, the job
folder, the database) and writes a Short and a ledger file the way the real one lands in the Outbox, so
whatever home it is handed gets the writes a real job makes. One test runs the real `studio run
--fake-services` when FFmpeg is here (T1, end to end). The home's own trees are compared byte for byte.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ns_common import ffmpeg
from ns_common.config import save_channel, save_runtime
from ns_common.errors import NsError
from ns_common.models import Channel, GateReport, Runtime
from ns_common.paths import Paths
from ns_supervisor import verify
from ns_supervisor.testing import StubTable, SyncSupervisor
from tests.test_ns_verify import good_deps

REPO = Path(__file__).resolve().parents[1]
OWNER_CHANNEL = "after-dark-tales"
NOBODY_ELSE: Callable[[int], bool] = lambda pid: False  # noqa: E731 - no other run's process is alive

FAKE_STUDIO = r'''
import json, os, sys, time
from pathlib import Path

sys.path.insert(0, os.environ["FAKE_STUDIO_REPO"])
flags = Path(os.environ["FAKE_STUDIO_FLAGS"])
home = Path(os.environ["NIGHTSHIFT_HOME"])
listing = sorted(p.relative_to(home).as_posix() for p in home.rglob("*"))
mode = os.environ.get("FAKE_STUDIO_MODE", "ok")

from ns_studio import jobs

env = jobs.open_env(jobs.parse_fake("all"))
channel = jobs.resolve_channels(env, None)[0]
ctx = jobs.make_job(env, channel, "shorts_only")
short = env.paths.outbox / channel.slug / "stories" / "Sample Story" / "shorts"
short.mkdir(parents=True, exist_ok=True)
(short / "Sample Story - Short 01.mp4").write_bytes(b"0" * 4096)
ledger = env.paths.data / "ledger-202611.jsonl"
ledger.write_text(json.dumps({"job_id": ctx.job_id}) + "\n", encoding="utf-8")
(env.paths.data / "held.bin").write_bytes(b"1" * 64)
seen = {
    "home": str(home), "paths_home": str(env.paths.home), "explicit": os.environ.get("NS_HOME_EXPLICIT"),
    "argv": sys.argv[1:], "cwd": os.getcwd(), "temp": os.environ.get("TEMP"), "listing": listing,
    "job_id": ctx.job_id, "channel": channel.slug,
    "runtime_copied": (home / "config" / "runtime.json").is_file(),
}
(flags / "seen.json").write_text(json.dumps(seen), encoding="utf-8")
if mode == "wait":
    (flags / "ready").write_text("1", encoding="utf-8")
    end = time.monotonic() + 60
    while not (flags / "go").exists() and time.monotonic() < end:
        time.sleep(0.05)
if mode == "fail":
    env.log.error("stage_failed", "render failed: the fake encoder stopped", code="NS-E401")
    print("error NS-E401: the fake encoder stopped", file=sys.stderr)
    sys.exit(1)
print(f"job {ctx.job_id} done")
'''


# ------------------------------------------------------------------------------------------------ helpers
def tree(root: Path, skip: Path | None = None) -> dict[str, str]:
    """Every folder and file under `root` with the file's sha256 (byte for byte), links not entered."""
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if skip is not None and (p == skip or skip in p.parents):
            continue
        rel = p.relative_to(root).as_posix()
        out[rel] = "folder" if p.is_dir() else hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def owner_trees(paths: Paths) -> dict[str, dict[str, str]]:
    """The owner's work: data (database, owners, jobs, ledger), Outbox, rejected, his channel files."""
    return {
        "data": tree(paths.data), "outbox": tree(paths.outbox), "rejected": tree(paths.rejected),
        "channels": tree(paths.channels_dir),
    }


def link_folder(link: Path, target: Path) -> None:
    """A junction on Windows (no rights needed), a folder symlink elsewhere."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        os.symlink(target, link, target_is_directory=True)


def marker_text(pids: list[int]) -> str:
    return json.dumps({"kind": verify.SCRATCH_KIND, "home": "x", "pids": pids, "made": "2026-11-12T14:04:15"})


def row(report: GateReport, name: str) -> Any:
    return next(c for c in report.checks if c.name == name)


@pytest.fixture
def home(tmp_home: Path) -> Paths:
    """An install with one channel of the owner's, his settings and one finished Short of his."""
    paths = Paths(tmp_home)
    paths.ensure()
    save_runtime(paths, Runtime.default("cpu-dev"))
    save_channel(paths, Channel(
        slug=OWNER_CHANNEL, display_name="After Dark Tales", niche="original short horror fiction",
        seed_keywords=["horror story"], voice_notes="Plain, intimate first person.", format="shorts_only",
    ))
    mine = paths.outbox / OWNER_CHANNEL / "stories" / "Owner Story" / "shorts"
    mine.mkdir(parents=True)
    (mine / "Owner Story - Short 01.mp4").write_bytes(b"owner" * 200)
    return paths


@pytest.fixture
def studio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    flags = tmp_path / "flags"
    flags.mkdir()
    script = tmp_path / "fake_studio.py"
    script.write_text(FAKE_STUDIO, encoding="utf-8")
    monkeypatch.setenv("FAKE_STUDIO_REPO", str(REPO))
    monkeypatch.setenv("FAKE_STUDIO_FLAGS", str(flags))
    monkeypatch.setenv("FAKE_STUDIO_MODE", "ok")

    def seen() -> dict[str, Any]:
        return json.loads((flags / "seen.json").read_text(encoding="utf-8"))

    def ran() -> bool:
        return (flags / "seen.json").exists()

    return SimpleNamespace(command=[sys.executable, str(script)], flags=flags, seen=seen, ran=ran)


@pytest.fixture
def sync(home: Paths) -> Iterator[SyncSupervisor]:
    s = SyncSupervisor(home, Runtime.default("gpu"), resolver=StubTable())
    yield s
    s.close()


def full_verify(home: Paths, sync: SyncSupervisor, studio: SimpleNamespace) -> GateReport:
    job = lambda p: verify.fake_job(p, command=studio.command, alive=NOBODY_ELSE)  # noqa: E731
    return verify.run_verify(home, Runtime.default("gpu"), sync, deps=good_deps(job=job))


# ------------------------------------------------------------------------------------------------ T1
def test_t1_a_full_verify_leaves_the_owners_work_byte_for_byte(
    home: Paths, sync: SyncSupervisor, studio: SimpleNamespace
) -> None:
    before = owner_trees(home)
    report = full_verify(home, sync, studio)
    r = row(report, "job.fake_services")
    assert r.passed, r.log
    assert isinstance(r.value, float) and r.value > 0
    seen = studio.seen()
    folder = verify.scratch_home(home)
    # the job ran, in its own home under run, handed over on purpose, with the install's settings read
    assert Path(seen["paths_home"]) == folder and Path(seen["explicit"]) == folder
    assert seen["argv"] == list(verify.JOB_ARGS)
    assert seen["runtime_copied"] is True
    assert Path(seen["temp"]).parent == folder
    # a test channel of its own, never the owner's
    assert seen["channel"] != OWNER_CHANNEL
    # nothing of it stays, and the owner's work is what it was
    assert not os.path.lexists(folder)
    assert owner_trees(home) == before
    assert seen["job_id"] not in json.dumps(tree(home.home))


@pytest.mark.skipif(not ffmpeg.available(), reason="FFmpeg not installed")
def test_t1_the_real_studio_run_lands_nothing_in_the_install(home: Paths) -> None:
    """End to end: the product's own `studio run --fake-services` (the real stages, render, QC, Outbox)."""
    before = owner_trees(home)
    command = [sys.executable, "-c",
               f"import sys, runpy; sys.path.insert(0, {str(REPO)!r}); "
               "runpy.run_module('ns_studio.cli', run_name='__main__')"]
    got = verify.fake_job(home, command=command, timeout_s=900, alive=NOBODY_ELSE)
    assert isinstance(got, verify.JobRun) and got.seconds > 0 and got.notes == ()
    assert not os.path.lexists(verify.scratch_home(home))
    assert owner_trees(home) == before


# ------------------------------------------------------------------------------------------------ T2
def test_t2_a_job_that_fails_leaves_nothing_and_its_error_is_in_the_row_and_the_log(
    home: Paths, sync: SyncSupervisor, studio: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_STUDIO_MODE", "fail")
    before = owner_trees(home)
    report = full_verify(home, sync, studio)
    r = row(report, "job.fake_services")
    assert r.passed is False
    assert "exited 1" in r.log and "the fake encoder stopped" in r.log
    assert not os.path.lexists(verify.scratch_home(home))
    assert owner_trees(home) == before
    logged = "".join(f.read_text(encoding="utf-8") for f in home.logs.glob("verify-*.jsonl"))
    assert "sample_job_failed" in logged and "render failed: the fake encoder stopped" in logged


# ------------------------------------------------------------------------------------------------ T3
def test_t3_what_a_killed_run_left_is_removed_at_the_next_start_and_only_that(
    home: Paths, studio: SimpleNamespace
) -> None:
    left = verify.make_scratch(home)          # a run that was killed: its folder, marker and job files stay
    (left / "data").mkdir()
    (left / "data" / "old-job.bin").write_bytes(b"old")
    (left / verify.SCRATCH_MARKER).write_text(marker_text([999_999]), encoding="utf-8")
    run = home.home / "run"
    (run / "supervisor.json").write_text("{}", encoding="utf-8")
    (run / "other").mkdir()
    (run / "other" / "keep.txt").write_text("keep", encoding="utf-8")
    beside = tree(run, skip=left)
    got = verify.fake_job(home, command=studio.command, alive=NOBODY_ELSE)
    assert isinstance(got, verify.JobRun)
    assert not any(name.endswith("old-job.bin") for name in studio.seen()["listing"])  # new for this run
    assert not os.path.lexists(left)
    assert tree(run) == beside


# ------------------------------------------------------------------------------------------------ T4
def test_t4_the_dashboard_and_studio_status_show_nothing_of_the_job_while_it_runs(
    home: Paths, studio: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("fastapi")
    from typer.testing import CliRunner

    from ns_studio.cli import app
    from tests.test_dashboard_api import make_env

    monkeypatch.setenv("FAKE_STUDIO_MODE", "wait")
    done: list[Any] = []
    worker = threading.Thread(
        target=lambda: done.append(verify.fake_job(home, command=studio.command, alive=NOBODY_ELSE)),
        daemon=True,
    )
    worker.start()
    try:
        end = time.monotonic() + 60
        while not (studio.flags / "ready").exists():
            assert time.monotonic() < end and worker.is_alive(), "the sample job did not start"
            time.sleep(0.05)
        job_id = studio.seen()["job_id"]
        assert (verify.scratch_home(home) / "data" / "jobs" / job_id).is_dir()   # it exists, and runs
        status = CliRunner().invoke(app, ["status"])
        assert status.exit_code == 0, status.output
        assert job_id not in status.output and "Sample Story" not in status.output
        client, _state = make_env(home.home)
        for route in ("/api/jobs", "/api/outputs"):
            got = client.get(route)
            assert got.status_code == 200, got.text
            assert job_id not in got.text and "Sample Story" not in got.text, route
    finally:
        (studio.flags / "go").write_text("1", encoding="utf-8")
        worker.join(90)
    assert done and isinstance(done[0], verify.JobRun)
    assert not os.path.lexists(verify.scratch_home(home))


# ------------------------------------------------------------------------------------------------ T5
def test_t5_a_folder_of_that_name_without_the_marker_is_left_and_named(
    home: Paths, studio: SimpleNamespace
) -> None:
    folder = verify.scratch_home(home)
    folder.mkdir(parents=True)
    (folder / "mine.txt").write_text("not the check's", encoding="utf-8")
    with pytest.raises(NsError) as e:
        verify.fake_job(home, command=studio.command, alive=NOBODY_ELSE)
    assert str(folder) in e.value.detail and verify.SCRATCH_MARKER in e.value.detail
    assert (folder / "mine.txt").read_text(encoding="utf-8") == "not the check's"
    assert not studio.ran()


def test_t5_a_link_of_that_name_is_left_and_its_target_untouched(
    home: Paths, studio: SimpleNamespace, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.mp4").write_bytes(b"video")
    (outside / verify.SCRATCH_MARKER).write_text(marker_text([999_999]), encoding="utf-8")  # even marked
    (home.home / "run").mkdir(exist_ok=True)
    folder = verify.scratch_home(home)
    link_folder(folder, outside)
    with pytest.raises(NsError) as e:
        verify.fake_job(home, command=studio.command, alive=NOBODY_ELSE)
    assert "link" in e.value.detail and str(folder) in e.value.detail
    assert (outside / "precious.mp4").read_bytes() == b"video" and os.path.lexists(folder)
    assert not studio.ran()


def test_t5_a_run_folder_that_leads_outside_the_install_is_left(
    home: Paths, studio: SimpleNamespace, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / verify.SCRATCH_NAME).mkdir(parents=True)
    (elsewhere / verify.SCRATCH_NAME / "precious.mp4").write_bytes(b"video")
    (elsewhere / verify.SCRATCH_NAME / verify.SCRATCH_MARKER).write_text(marker_text([999_999]), "utf-8")
    link_folder(home.home / "run", elsewhere)
    with pytest.raises(NsError) as e:
        verify.fake_job(home, command=studio.command, alive=NOBODY_ELSE)
    assert "link" in e.value.detail
    assert (elsewhere / verify.SCRATCH_NAME / "precious.mp4").read_bytes() == b"video"
    assert not studio.ran()


def test_t5_a_link_inside_a_left_folder_is_removed_as_a_name_never_entered(
    home: Paths, studio: SimpleNamespace, tmp_path: Path
) -> None:
    outside = tmp_path / "programs"
    outside.mkdir()
    (outside / "python.exe").write_bytes(b"program")
    left = verify.make_scratch(home)
    (left / verify.SCRATCH_MARKER).write_text(marker_text([999_999]), encoding="utf-8")
    link_folder(left / "venvs", outside)
    got = verify.fake_job(home, command=studio.command, alive=NOBODY_ELSE)
    assert isinstance(got, verify.JobRun)
    assert (outside / "python.exe").read_bytes() == b"program"
    assert not os.path.lexists(left)


def test_t5_a_folder_another_live_model_test_uses_is_left(home: Paths, studio: SimpleNamespace) -> None:
    folder = verify.make_scratch(home)
    (folder / verify.SCRATCH_MARKER).write_text(marker_text([4242]), encoding="utf-8")
    (folder / "busy.txt").write_text("in use", encoding="utf-8")
    with pytest.raises(NsError) as e:
        verify.fake_job(home, command=studio.command, alive=lambda pid: pid == 4242)
    assert "another model test" in e.value.detail and "4242" in e.value.detail
    assert (folder / "busy.txt").read_text(encoding="utf-8") == "in use"
    assert not studio.ran()


@pytest.mark.skipif(sys.platform != "win32", reason="a file held open blocks its removal on Windows only")
def test_t5_a_held_file_never_fails_the_check_and_the_next_run_removes_it(
    home: Paths, studio: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_STUDIO_MODE", "wait")
    folder = verify.scratch_home(home)
    done: list[Any] = []
    worker = threading.Thread(
        target=lambda: done.append(verify.fake_job(home, command=studio.command, alive=NOBODY_ELSE)),
        daemon=True,
    )
    worker.start()
    held = None
    try:
        end = time.monotonic() + 60
        while not (studio.flags / "ready").exists():
            assert time.monotonic() < end and worker.is_alive(), "the sample job did not start"
            time.sleep(0.05)
        held = (folder / "data" / "held.bin").open("rb")  # another program holds a file of the job's
        (studio.flags / "go").write_text("1", encoding="utf-8")
        worker.join(90)
        assert done and isinstance(done[0], verify.JobRun)          # the check passed on the job alone
        assert any("held.bin" in n and "next full ns verify" in n for n in done[0].notes), done[0].notes
        assert (folder / "data" / "held.bin").exists()
    finally:
        if held is not None:
            held.close()
        (studio.flags / "go").write_text("1", encoding="utf-8")
        worker.join(90)
    (studio.flags / "seen.json").unlink()
    (studio.flags / "go").unlink()
    monkeypatch.setenv("FAKE_STUDIO_MODE", "ok")
    again = verify.fake_job(home, command=studio.command, alive=NOBODY_ELSE)
    assert isinstance(again, verify.JobRun) and again.notes == ()
    assert not any(name.endswith("held.bin") for name in studio.seen()["listing"])
    assert not os.path.lexists(folder)


# ------------------------------------------------------------------------------------------------ T6
def test_t6_quick_mode_runs_no_job_and_makes_no_folder(home: Paths, sync: SyncSupervisor) -> None:
    calls: list[Paths] = []

    def job(p: Paths) -> float:
        calls.append(p)
        return 1.0

    quick = verify.run_verify(home, Runtime.default("gpu"), sync, quick=True, deps=good_deps(job=job))
    plain = verify.run_verify(home, Runtime.default("gpu"), sync, quick=True, deps=good_deps())
    assert calls == []
    assert "job.fake_services" not in {c.name for c in quick.checks}
    assert [c.name for c in quick.checks] == [c.name for c in plain.checks]
    assert not os.path.lexists(verify.scratch_home(home))


# ------------------------------------------------------------------------------------------------ T7
def test_t7_the_job_leaves_no_pid_session_owner_or_lock_file_in_the_real_home(
    home: Paths, studio: SimpleNamespace
) -> None:
    def files() -> set[str]:
        return {p.relative_to(home.home).as_posix() for p in home.home.rglob("*") if p.is_file()}

    before = files()
    got = verify.fake_job(home, command=studio.command, alive=NOBODY_ELSE)
    assert isinstance(got, verify.JobRun)
    after = files()
    assert before - after == set()
    new = after - before
    assert all(n.startswith("logs/verify-") and n.endswith(".jsonl") for n in new), new
    assert not any(part in n for n in new for part in (".pid", ".lock", "session", "owners/"))
