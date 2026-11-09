"""D-31b: a missing or briefly unusable owner lock file never loses a night.

D-31 (PR 168) made "who owns this run" an OS lock on `data/owners/<id>.lock`. Review's note on that PR was
that a missing lock file now refuses the whole night. What is true, established here with the real
`FileLock` on a temp home (and a home the REAL 0.1.14 nightly left behind, checked once by hand):

* a MISSING lock file or a missing `data/owners` folder is not a problem: `FileLock.acquire` makes both. The
  first night after an update from 0.1.14, a home restored from a backup, a cleaned folder and a lock file
  that a crash left empty all run. Those tests pin that down (they pass on the code before this change).
* what did refuse a night: an `OSError` while making the folder or opening the file (an antivirus scan
  holding the new file for a moment, a sharing violation while another process deletes the old one). It was
  not retried, and for the nightly's own run lock it escaped `run_daily` before anything was written: no
  `runs` row, no morning note, only a one-line toast with a code. A permanent cause (the folder is a file, the
  lock path is a folder, the disk is read-only) vanished the same way. A run lock held by another live
  process vanished the same way too, but only with a forced run id: the id is `daily-<date>-<4 hex>`, so
  two real nightlies do not share a run lock and are NOT refused by it (they meet at a job's lock, which
  `_attempt` already treats as not-a-failure). Case 7 therefore fixes the id to test the code path; it is not
  a claim that two nightlies at once is an ordinary event.

After: a transient error is retried (5 tries over about 1 s, never for a lock another process holds), and a
night that still cannot be owned is refused on purpose and says so: the `runs` row, a morning note in plain
words naming the cause, the error for the exit code.

No render, no FFmpeg, no engine: the nightly runs with the fake services and the three stub stages of
`tests/d31_driver.py`; the "other nightly" is a tiny Python process holding the real lock.
"""

from __future__ import annotations

import builtins
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from ns_common import singleton
from ns_common.db import Db
from ns_common.paths import Paths
from ns_common.singleton import FileLock
from ns_studio import daily, jobs, notifications, ownership
from ns_studio.channels import crud
from ns_studio.cli import app
from ns_studio.errors import StudioError
from ns_studio.stages import REGISTRY, StageState, inputs_hash
from tests import d31_driver as driver
from tests.test_interrupted_nightly import (
    _add_long_output,
    _dashboard,
    _dead_daily_job,
    _long_jobs,
    _notes,
    _queue_rows,
    _runs,
    _setup,
)

REPO = Path(__file__).resolve().parents[1]
TRANSIENT = PermissionError(
    13, "The process cannot access the file because it is being used by another process"
)


# ------------------------------------------------------------------ helpers
def _fixed_run_id(monkeypatch: pytest.MonkeyPatch, now: datetime) -> str:
    """Make the nightly's run id known (`daily-<date>-beef`), so a test can prepare its lock file."""
    monkeypatch.setattr(daily, "secrets", SimpleNamespace(token_hex=lambda n=2: "beef"))
    return f"daily-{now:%Y%m%d}-beef"


def _night(env: jobs.Env, now: datetime, **kw: Any) -> dict[str, Any]:
    return daily.run_daily(env, now=now, rng=random.Random(1), **kw)


def _opens(
    monkeypatch: pytest.MonkeyPatch, match: Any, failures: int, error: OSError = TRANSIENT
) -> list[str]:
    """Make `open(<lock file>, "a+b")` in `ns_common.singleton` raise `error` the first `failures` times for a
    path `match` accepts; every try (failed or not) is listed in the returned list by file name."""
    real = builtins.open
    left = {"n": failures}
    tries: list[str] = []

    def flaky(file: Any, mode: str = "r", *a: Any, **kw: Any) -> Any:
        if "a" in mode and match(Path(file)):
            tries.append(Path(file).name)
            if left["n"] > 0:
                left["n"] -= 1
                raise error
        return real(file, mode, *a, **kw)

    monkeypatch.setattr(singleton, "open", flaky, raising=False)
    return tries


@pytest.fixture
def fast_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shorten the waits between tries of a lock file that cannot be opened (a test of a lock that never works
    would sleep a second for every ask). The number of tries stays what it is."""
    monkeypatch.setattr(ownership, "OPEN_RETRY_WAITS_S", (0.01,) * 4, raising=False)


def _stage(ctx_dir: Path, ctx: Any, *names: str) -> None:
    """Stages the stub pipeline finished, with their real inputs hash (a made-up one is run again)."""
    state = StageState()
    for name in names:
        ctx.write_json(f"{name}.json", {"stage": name})  # what the stub stage writes
        state.mark_done(name, inputs_hash(ctx, REGISTRY[name]))
    state.save(ctx_dir)


class Toasts:
    def __init__(self) -> None:
        self.shown: list[str] = []

    def __call__(self, title: str, message: str) -> bool:
        self.shown.append(message)
        return True


def _cli_nightly(monkeypatch: pytest.MonkeyPatch, toasts: Toasts) -> Any:
    """`studio run --daily --fake-services`: the real entry, with the stub stages for the story pipeline."""
    driver.install(monkeypatch)
    monkeypatch.setattr(jobs, "STAGE_MODULES", ())
    monkeypatch.setattr("ns_studio.policy.gate.available", lambda: False)
    monkeypatch.setattr(notifications, "default_notifier", lambda: toasts)
    return CliRunner().invoke(app, ["run", "--daily", "--fake-services"])


def _the_failed_row(db: Db, run_id: str) -> dict[str, Any]:
    row = db.one("select finished_at, summary_json from runs where run_id=?", (run_id,))
    assert row is not None, "the refused night left no `runs` row: the dashboard knows nothing about it"
    return {"finished_at": row["finished_at"], "summary": json.loads(row["summary_json"] or "{}")}


# ------------------------------------------------------------------ case 1: the first night after 0.1.14
def _home_as_0_1_14_left_it(paths: Paths, db: Db, env: jobs.Env) -> dict[str, str]:
    """The layout 0.1.14 leaves (checked against a home made by the real v0.1.14 nightly, killed mid-job):

    no `data/owners` and no `run/` folder; a `runs` row with no finish time whose `started_at` equals the
    `combos.created_at` of the job it made; that job's folder with `job.json`, `stage_state.json` and the
    output of the stage it had finished; no owner record or lock file for anything. Here: the night 20 hours
    ago was killed, the one 4 days ago too (too old to continue by itself), and a job from 10 days ago
    finished."""
    now = datetime.now(UTC)
    ch = crud.get_channel(paths, "after-dark")
    made: dict[str, str] = {}
    for key, age in (
        ("killed", timedelta(hours=20)),
        ("older", timedelta(days=4)),
        ("finished", timedelta(days=10)),
    ):
        when = now - age
        run_id = f"daily-{when:%Y%m%d}-{key[:4]}"
        db.execute(
            "insert into runs(run_id, started_at, mode) values (?,?,'daily')",
            (run_id, when.isoformat(timespec="seconds")),
        )
        ctx = jobs.make_job(env, ch, "master_first", now=when)
        made[key] = ctx.job_id
        if key == "finished":
            _stage(ctx.job_dir, ctx, "d31_a", "d31_b", "d31_c")
            _add_long_output(db, ctx.job_id)
            ended = (when + timedelta(hours=2)).isoformat(timespec="seconds")
            db.execute("update outputs set created_at=? where job_id=?", (ended, ctx.job_id))
            db.execute("update runs set finished_at=?, summary_json='{}' where run_id=?", (ended, run_id))
        else:
            _stage(ctx.job_dir, ctx, "d31_a")
    shutil.rmtree(ownership.owners_dir(paths))  # `make_job` announces the job; 0.1.14 knew no such folder
    assert not (paths.data / "owners").exists() and not (paths.home / "run").exists()
    return made


def test_the_first_night_after_an_update_from_0_1_14_runs_and_loses_nothing(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first thing QA does with 0.1.15: `studio run --daily` on the home 0.1.14 left."""
    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    made = _home_as_0_1_14_left_it(paths, db, jobs.open_env(light=True))
    killed, older, finished = made["killed"], made["older"], made["finished"]
    taken: list[tuple[str, bool]] = []

    class Spy(FileLock):
        def acquire(self) -> None:
            super().acquire()
            taken.append(
                (self.path.name, self.path.exists())
            )  # the file is there the moment the lock is held

    monkeypatch.setattr(ownership, "FileLock", Spy)
    toasts = Toasts()

    result = _cli_nightly(monkeypatch, toasts)

    assert result.exit_code == 0, result.output
    report = json.loads((paths.logs / f"nightly-{datetime.now(UTC):%Y%m%d}.json").read_text(encoding="utf-8"))
    assert report["failures"] == [] and "failed" not in report
    assert [j["job_id"] for j in report["jobs"]] == [killed] and report["jobs"][0]["resumed"] is True
    fates = {i["job_id"]: i["state"] for i in report["interrupted_jobs"]}
    assert fates == {killed: "resumed", older: "open"}
    assert f"job {killed} done" in result.output and f"job {older} was interrupted" in result.output

    # the folder and the locks were made on the way, and nothing is left held
    assert ownership.owners_dir(paths).is_dir()
    run_lock = [n for n, _ in taken if n.startswith("daily-")]
    assert len(run_lock) == 1 and all(exists for _, exists in taken), taken
    assert {n for n, _ in taken} >= {"recover.lock", run_lock[0], f"{killed}.lock"}
    assert sorted(p.name for p in ownership.owners_dir(paths).glob("*.lock")) == ["recover.lock"]

    # nothing was lost: the same three jobs, the killed one finished from where it stopped, none twice
    assert sorted(_long_jobs(db)) == sorted([killed, older, finished])
    assert [
        r["job_id"] for r in db.query("select job_id from outputs where kind='long' order by job_id")
    ] == sorted([killed, finished])
    assert driver.calls(tmp_home) == ["d31_b:start", "d31_b:done", "d31_c:start", "d31_c:done"]
    assert ownership.read_record(paths, killed)["state"] == "done"  # type: ignore[index]
    done_night, older_night, killed_night, tonight = sorted(_runs(db), key=lambda r: r["started_at"])
    for night in (older_night, killed_night):  # both open rows were closed, owner unknown (written by 0.1.14)
        assert (
            night["finished_at"]
            and night["summary"]["interrupted"]
            and night["summary"]["owner"] == "unknown"
        )
    assert (
        done_night["summary"] == {} and tonight["finished_at"] and not tonight["summary"].get("interrupted")
    )
    # the owner is told; the Queue has a card for each: done for the one continued, Retry for the old one
    said = " ".join(n["message"] for n in _notes(tmp_home))
    assert "continued it and it finished" in said and "Retry" in said and "owner unknown" in said
    cards = {r["result"]["studio_job_ids"][0]: r["status"] for r in _queue_rows(db)}
    assert cards == {killed: "done", older: "failed"}
    assert toasts.shown and "resumed" in toasts.shown[-1]


# ------------------------------------------------------------------ case 2: a home restored from a backup
@pytest.mark.parametrize("shape", ["stale_lock_files", "records_only"])
def test_a_home_restored_from_a_backup_runs_with_stale_lock_files_or_none(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, shape: str
) -> None:
    """The backup brought back records that say `running` (a recycled pid of a live process in them) and, or
    not, the stale lock files; nobody holds any lock: both are dead owners, closed and continued, not a
    refusal."""
    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    dead = _dead_daily_job(paths, db)
    db.execute(
        "insert into runs(run_id, started_at, mode) values ('daily-restored', ?, 'daily')",
        ((datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds"),),
    )
    ownership.write_record(
        paths,
        "daily-restored",
        kind="run",
        state="running",
        pid=os.getpid(),
        origin="daily",
        jobs=[{"job_id": dead, "channel": "after-dark"}],
    )
    ownership.write_record(
        paths, dead, pid=os.getpid()
    )  # that pid is alive: only the lock can say who owns it
    if shape == "stale_lock_files":
        for owner in ("daily-restored", dead, "recover"):
            stale = FileLock(ownership.owners_dir(paths) / f"{owner}.lock")
            stale.acquire()
            stale.release()  # released but not deleted: what a copied backup brings
    else:
        assert not list(ownership.owners_dir(paths).glob("*.lock"))
    assert ownership.liveness(paths, dead) == "dead"

    report = _night(jobs.open_env(light=True), datetime.now(UTC))

    assert report["failures"] == [] and [j["job_id"] for j in report["jobs"]] == [dead]
    assert report["jobs"][0]["resumed"] is True
    restored, tonight = _runs(db)
    assert restored["summary"]["interrupted"] is True and tonight["finished_at"]
    assert _long_jobs(db) == [dead], "no second master was started for it"
    assert ownership.read_record(paths, dead)["state"] == "done"  # type: ignore[index]


# ------------------------------------------------------------------ case 3: the folder is cleaned away
def test_a_cleaned_owners_folder_between_nights_is_made_again_and_the_night_runs(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    now = datetime.now(UTC)
    first = _night(jobs.open_env(light=True), now)
    assert len(first["jobs"]) == 1 and ownership.owners_dir(paths).is_dir()

    shutil.rmtree(ownership.owners_dir(paths))  # a cleaner tool, a hand-made "free some space"
    later = _night(jobs.open_env(light=True), now + timedelta(days=10))

    assert later["failures"] == [] and len(later["jobs"]) == 1, "a second master is due and was made"
    assert len(_long_jobs(db)) == 2
    assert ownership.owners_dir(paths).is_dir() and ownership.read_record(paths, later["jobs"][0]["job_id"])
    assert [r["finished_at"] is not None for r in _runs(db)] == [True, True]


# ------------------------------------------------------------------ case 4: briefly unusable (antivirus)
@pytest.mark.parametrize("failures", [1, 2])
@pytest.mark.parametrize("which", ["the_nights_run_lock", "a_jobs_lock"])
def test_a_lock_file_that_cannot_be_opened_for_a_moment_does_not_lose_the_night(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, which: str, failures: int
) -> None:
    """An antivirus scan of the new file, a sharing violation: `open()` raises once or twice, then works."""
    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    prefix = "daily-" if which == "the_nights_run_lock" else "after-dark-"
    tries = _opens(monkeypatch, lambda p: p.suffix == ".lock" and p.name.startswith(prefix), failures)

    report = _night(jobs.open_env(light=True), datetime.now(UTC))

    assert report["failures"] == [], report["failures"]
    assert len(report["jobs"]) == 1 and report["jobs"][0]["channel"] == "after-dark"
    assert len(tries) >= failures + 1, "the failed tries were repeated"
    (row,) = _runs(db)
    assert row["finished_at"] and not row["summary"].get("failed")
    assert len(db.query("select 1 from outputs where kind='long'")) == 1
    assert _notes(tmp_home) == [], "a blip that cleared on its own is not a message"


def test_the_owners_folder_that_cannot_be_made_for_a_moment_does_not_lose_the_night(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    owners = ownership.owners_dir(paths)
    shutil.rmtree(owners, ignore_errors=True)
    real = Path.mkdir
    left = {"n": 2}

    def flaky(self: Path, *a: Any, **kw: Any) -> None:
        if self == owners and left["n"] > 0:
            left["n"] -= 1
            raise TRANSIENT
        real(self, *a, **kw)

    monkeypatch.setattr(Path, "mkdir", flaky)

    report = _night(jobs.open_env(light=True), datetime.now(UTC))

    assert left["n"] == 0, "the folder really failed twice"
    assert report["failures"] == [] and len(report["jobs"]) == 1
    assert owners.is_dir() and _notes(tmp_home) == []


def test_the_retry_is_five_tries_over_about_a_second_and_never_for_a_lock_that_is_held(
    tmp_home: Path,
) -> None:
    waits = ownership.OPEN_RETRY_WAITS_S  # type: ignore[attr-defined]
    assert len(waits) + 1 == 5 and 0.8 <= sum(waits) <= 1.2, waits
    paths = Paths.from_env()
    paths.ensure()

    # a file error: the work is refused after exactly five tries, with the waits between them
    with pytest.MonkeyPatch.context() as mp:
        sleeps: list[float] = []
        mp.setattr(ownership.time, "sleep", lambda s: sleeps.append(s))
        tries = _opens(mp, lambda p: p.suffix == ".lock", 99)
        with pytest.raises(StudioError):
            with ownership.hold_job(paths, "j-1"):
                pytest.fail("never run unowned")
        assert len(tries) == 5 and sleeps == list(waits), (tries, sleeps)

    # a lock another process holds: the old short wait for a probe, and none of the file-error waits
    other = FileLock(ownership.lock_path(paths, "j-2"))
    other.acquire()
    try:
        with pytest.MonkeyPatch.context() as mp:
            sleeps = []
            mp.setattr(ownership.time, "sleep", lambda s: sleeps.append(s))
            with pytest.raises(ownership.JobBusy), ownership.hold_job(paths, "j-2"):
                pytest.fail("another process owns it")
    finally:
        other.release()
    assert sleeps == [ownership.ACQUIRE_WAIT_S] * (ownership.ACQUIRE_TRIES - 1), sleeps


def test_every_other_caller_gets_the_same_retry(tmp_home: Path) -> None:
    """`studio run`, `--resume` and the dashboard's studio child take `hold_job`; the engine start and the
    nightly call `recover` (its guard) and every liveness question probes the lock with `lock_held`."""
    paths, db = _setup(tmp_home)

    # hold_job (studio run, studio run --resume, the dashboard's Run now / Retry / series part)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ownership.time, "sleep", lambda s: None)
        tries = _opens(mp, lambda p: p.name == "j-1.lock", 2)
        with ownership.hold_job(paths, "j-1", channel="after-dark"):
            assert len(tries) == 3, "two refused tries, then the lock"
            assert ownership.liveness(paths, "j-1") == "live"

    # recover (engine start, studio run, the nightly): it stays guarded, it does not fall back to unguarded
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ownership.time, "sleep", lambda s: None)
        tries = _opens(mp, lambda p: p.name == "recover.lock", 1)
        ownership.recover(paths, db)
        assert tries == ["recover.lock", "recover.lock"], "one refused try, then the guard was taken"

    # lock_held (liveness, the planner, /api/status): a probe that cannot open the file once is not "not held"
    owner = FileLock(ownership.lock_path(paths, "j-live"))
    owner.acquire()
    ownership.write_record(paths, "j-live", kind="job", state="running", pid=os.getpid(), origin="daily")
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(ownership.time, "sleep", lambda s: None)
            _opens(mp, lambda p: p.name == "j-live.lock", 1)
            assert ownership.liveness(paths, "j-live") == "live", "a live job must not be closed as dead"
    finally:
        owner.release()


# ------------------------------------------------------------------ case 5: a crash left an empty lock file
def test_an_empty_lock_file_left_by_a_crash_before_the_pid_was_written_does_not_stop_the_night(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    now = datetime.now(UTC)
    run_id = _fixed_run_id(monkeypatch, now)
    dead = _dead_daily_job(paths, db)
    for owner in (run_id, dead, "recover"):  # created, then the process died before it wrote a byte
        empty = ownership.lock_path(paths, owner)
        empty.parent.mkdir(parents=True, exist_ok=True)
        empty.write_bytes(b"")
        assert empty.stat().st_size == 0 and singleton.read_owner(empty) is None
    assert ownership.liveness(paths, dead) == "dead"  # nobody holds an empty file

    report = _night(jobs.open_env(light=True), now)

    assert report["failures"] == [] and report["jobs"][0]["job_id"] == dead and report["jobs"][0]["resumed"]
    assert report["run_id"] == run_id
    assert ownership.read_record(paths, run_id)["state"] == "done"  # type: ignore[index]
    assert any("continued it and it finished" in n["message"] for n in _notes(tmp_home))


# ------------------------------------------------------------------ case 6: a lock that can never be made
@pytest.mark.parametrize("why", ["owners_is_a_file", "the_lock_path_is_a_folder"])
def test_a_lock_that_cannot_be_made_refuses_the_night_and_says_so_in_plain_words(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, fast_retry: None, why: str
) -> None:
    """Refusing is right (a night run with no owner would be closed as dead by the next start and continued
    next to itself). Losing it unseen was the bug: no `runs` row, no morning note, only a code in a toast."""
    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    now = datetime.now(UTC)
    run_id = _fixed_run_id(monkeypatch, now)
    owners = ownership.owners_dir(paths)
    shutil.rmtree(owners, ignore_errors=True)
    if why == "owners_is_a_file":
        owners.write_text("not a folder", encoding="utf-8")
    else:
        ownership.lock_path(paths, run_id).mkdir(parents=True)

    started = time.monotonic()
    with pytest.raises(StudioError) as ei:
        _night(jobs.open_env(light=True), now)
    assert time.monotonic() - started < 10, "bounded: five tries, never an endless wait"
    assert ei.value.code == "NS-E327" and not isinstance(ei.value, ownership.JobBusy)

    assert _long_jobs(db) == [], "no video was started without an owner"
    row = _the_failed_row(db, run_id)
    assert row["finished_at"] and row["summary"]["failed"] == "NS-E327"
    (note,) = _notes(tmp_home)
    text = note["message"]
    assert "did not start" in text and "nothing was made" in text.lower()
    assert "owners" in text, "it names the place that cannot be used"
    assert "next nightly run" in text and "NS-E327" not in text, (
        "what happens next, in plain words, not a code"
    )
    state, why_text = ownership.run_state(paths, run_id, None, row["finished_at"], row["summary"])
    assert state == "failed" and "owners" in why_text, "the dashboard's last-run line names the cause too"
    assert isinstance(ei.value, ownership.LockUnavailable)  # type: ignore[attr-defined]


def test_a_home_that_cannot_be_written_at_all_still_refuses_with_the_owner_error_not_a_database_one(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, fast_retry: None
) -> None:
    """Everything fails (read-only disk): the refusal is still the one that names the lock, and it still tries
    to tell. What cannot be written cannot be told; the exit code and the log stay."""
    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    env = jobs.open_env(light=True)
    shutil.rmtree(ownership.owners_dir(paths), ignore_errors=True)
    ownership.owners_dir(paths).write_text("blocked", encoding="utf-8")
    real = env.db.execute

    def read_only(sql: str, params: Any = ()) -> Any:
        if sql.lstrip().lower().startswith(("insert", "update")):
            raise sqlite3.OperationalError("attempt to write a readonly database")
        return real(sql, params)

    monkeypatch.setattr(env.db, "execute", read_only)
    with pytest.raises(StudioError) as ei:
        _night(env, datetime.now(UTC))
    assert isinstance(ei.value, ownership.LockUnavailable)  # type: ignore[attr-defined]
    assert len(_notes(tmp_home)) == 1 and "did not start" in _notes(tmp_home)[0]["message"]


# ------------------------------------------------------------------ case 7: another live nightly holds it
HOLDER = (
    "import sys\n"
    "from pathlib import Path\n"
    "from ns_common.singleton import FileLock\n"
    "lock = FileLock(Path(sys.argv[1]))\n"
    "lock.acquire()\n"
    "import os\n"
    "print('held', os.getpid(), flush=True)\n"
    "sys.stdin.readline()\n"
)


@pytest.fixture
def other_nightly(tmp_home: Path) -> Any:
    """Start a process that holds a lock file for real; `hold(path)` returns its pid once it holds it."""
    procs: list[subprocess.Popen[str]] = []

    def hold(path: Path) -> int:
        proc = subprocess.Popen(
            [sys.executable, "-c", HOLDER, str(path)],
            cwd=REPO,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        procs.append(proc)
        assert proc.stdout is not None
        word, pid = (proc.stdout.readline().split() + ["", ""])[:2]
        assert word == "held", "the holder did not start"
        return int(pid)  # the interpreter's own pid: a venv's python.exe starts the real one as a child

    yield hold
    for proc in procs:
        if proc.stdin is not None:
            proc.stdin.close()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=30)


def test_a_night_that_another_live_nightly_owns_is_refused_and_the_morning_note_says_so(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, other_nightly: Any
) -> None:
    paths, db = _setup(tmp_home)
    toasts = Toasts()
    now = datetime.now(UTC)
    run_id = _fixed_run_id(monkeypatch, now)
    pid = other_nightly(ownership.lock_path(paths, run_id))
    assert ownership.lock_held(paths, run_id) is True

    result = _cli_nightly(monkeypatch, toasts)

    assert result.exit_code == 1, result.output
    assert "NS-E327" in result.output and f"process {pid}" in result.output, result.output
    assert _long_jobs(db) == [], "the refused night made nothing"
    assert ownership.lock_held(paths, run_id) is True, "the other nightly still owns it, untouched"
    row = _the_failed_row(db, run_id)
    assert row["finished_at"] and row["summary"]["failed"] == "NS-E327"
    (stop,) = row["summary"]["failures"]  # what the "Last night" card shows, whatever happens to the note
    assert stop["job_id"] == daily.NIGHT_NOT_STARTED and f"process {pid}" in stop["error"]
    (note,) = _notes(tmp_home)
    text = note["message"]
    assert "did not start" in text and "already running" in text and f"process {pid}" in text, text
    assert "nothing was lost" in text.lower() and "NS-E327" not in text
    assert toasts.shown == ["run failed (NS-E327), open Dashboard"], "the toast the CLI always sends is kept"


def test_the_refused_night_also_tells_the_jobs_it_had_closed_to_continue_tonight(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, other_nightly: Any
) -> None:
    """`recover` closed a dead job before the lock was asked for; the note for a job the night would continue
    is left only after it tried, which a refused night never does: the refusal must not swallow it."""
    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    now = datetime.now(UTC)
    run_id = _fixed_run_id(monkeypatch, now)
    dead = _dead_daily_job(paths, db)
    other_nightly(ownership.lock_path(paths, run_id))

    with pytest.raises(ownership.JobBusy):
        _night(jobs.open_env(light=True), now)

    messages = [n["message"] for n in _notes(tmp_home)]
    assert len(messages) == 2, messages
    assert any("interrupted" in m and "next nightly run" in m for m in messages), "the job is told as waiting"
    assert any("did not start" in m for m in messages)
    assert ownership.read_record(paths, dead)["state"] == "interrupted"  # type: ignore[index]
    (card,) = _queue_rows(db)
    assert card["result"]["studio_job_ids"] == [dead] and card["result"]["interrupted"]["resume"] == "pending"


def test_the_refusal_words_do_not_invent_a_process_when_the_lock_file_names_none() -> None:
    named = daily._refusal_words(ownership.JobBusy("NS-E327", "run x is already running", 4242))
    unnamed = daily._refusal_words(ownership.JobBusy("NS-E327", "run x is already running", None))
    assert "already running" in named[0] and "process 4242" in named[0]
    assert "process" not in unnamed[1].replace("another process is holding", "")
    assert "did not start" in unnamed[0] and "NS-E327" not in unnamed[0]


@pytest.mark.parametrize("notes", ["cleared_with_mark_all_read", "notifications_off"])
def test_a_refused_night_stays_on_screen_in_last_night_after_the_note_is_gone(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, fast_retry: None, notes: str
) -> None:
    """The Queue's "Last night" card shows a stop only from `summary.failures` (red dot, "<channel>: <code>.
    <error>"); the morning-note list in it is cleared by "Mark all read" and is never left when notifications
    are off. So the cause must be in what GET /runs serves, not only in the note (review finding, D31B)."""
    paths, db = _setup(tmp_home)
    driver.install(monkeypatch)
    now = datetime.now(UTC)
    run_id = _fixed_run_id(monkeypatch, now)
    shutil.rmtree(ownership.owners_dir(paths), ignore_errors=True)
    ownership.owners_dir(paths).write_text("not a folder", encoding="utf-8")
    if notes == "notifications_off":
        monkeypatch.setattr(ownership, "notifications_enabled", lambda _paths: False)
    with pytest.raises(ownership.LockUnavailable):
        _night(jobs.open_env(light=True), now)

    dash = _dashboard(tmp_home)
    if notes == "cleared_with_mark_all_read":
        assert len(dash.engine.notes()) == 1
        dash.engine.clear_notes()  # what Shell.tsx `markRead` posts to /notes/clear
    assert dash.engine.notes() == [], "the morning note is gone from the screen"

    (last,) = dash.engine.runs(1)  # what the Queue page reads for its "Last night" card
    assert last["run_id"] == run_id and last["state"] == "failed" and "owners" in last["note"]
    (stop,) = last["summary"]["failures"]  # the card's red dot and its red line come from this list
    assert stop["code"] == "NS-E327" and stop["job_id"] == daily.NIGHT_NOT_STARTED
    assert stop["channel"] == "The night", "shown as written: 'The night: NS-E327. <reason>'"
    assert "owners" in stop["error"] and "cannot be made" in stop["error"], stop
    assert last["summary"]["jobs"] == [], "nothing was made"
