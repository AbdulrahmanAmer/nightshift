"""D-31b follow-up (with F-9b): an owner lock that cannot be probed means "held", never "free".

`lock_held` opens the owner's lock file to see whether a live process holds it. An `OSError` there (an
antivirus or a backup tool holding the file, a permission the folder lost) said "not held", so `liveness`
called live work "dead" and `recover` closed a running job as interrupted. Every caller now gets the cautious
answer:
`liveness` says "live", `recover` leaves the job open (the next call tries again), the planner counts it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ns_common.paths import Paths
from ns_studio import ownership


class UnopenableLock:
    """A `FileLock` whose file cannot be opened right now (PermissionError is an OSError)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def acquire(self) -> None:
        raise PermissionError(13, "The process cannot access the file", str(self.path))

    def release(self) -> None:
        return None


class NoRows:
    """A database with nothing in it, recording what was asked."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def query(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Sequence[Any]:
        self.asked.append(sql)
        return []

    def one(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Any:
        self.asked.append(sql)
        return None

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Any:
        self.asked.append(sql)
        return None


class Pids:
    """Which pids are alive; `ownership._pid_alive` is replaced by this (no tasklist in a test)."""

    def __init__(self) -> None:
        self.alive: set[int] = {4242}

    def __call__(self, pid: int) -> bool:
        return pid in self.alive


#: The product's own pid check, kept for the tests that need it (the fixture below replaces it).
REAL_PID_ALIVE = ownership._pid_alive  # noqa: SLF001
#: The product's own question to the Windows Restart Manager, for the one test that asks it for real.
REAL_PID_HOLDS = getattr(ownership, "_pid_holds", lambda pid, lock: None)
#: When the fake live processes started: long before any record of these tests, so they are its owners.
LONG_AGO = datetime(2026, 11, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def pids(monkeypatch: pytest.MonkeyPatch) -> Pids:
    fake = Pids()
    monkeypatch.setattr(ownership, "_pid_alive", fake)
    # raising=False: the name does not exist on the base, where these tests must fail at an assertion
    monkeypatch.setattr(
        ownership, "_pid_started", lambda pid: LONG_AGO if pid in fake.alive else None, raising=False
    )
    # Windows will not say who has a lock file open, so the times decide unless a test says otherwise
    monkeypatch.setattr(ownership, "_pid_holds", lambda pid, lock: None, raising=False)
    monkeypatch.setattr(ownership, "_pid_name", lambda pid: "python.exe", raising=False)
    ownership._UNPROBED.clear()  # noqa: SLF001 - per-process state of the bound
    return fake


@pytest.fixture
def running_job(tmp_path: Path) -> tuple[Paths, str]:
    paths = Paths(tmp_path / "home")
    job_id = "j-20261109-0001"
    ownership.write_record(
        paths,
        job_id,
        kind="job",
        state="running",
        pid=4242,
        channel="c1",
        # a minute ago: inside the hour, so the tests of the probe count are not decided by the time bound
        announced_at=(datetime.now(UTC) - timedelta(seconds=60)).isoformat(),
    )
    ownership.lock_path(paths, job_id).write_text("4242", encoding="utf-8")
    return paths, job_id


def test_an_os_error_while_probing_the_owner_lock_means_held(
    running_job: tuple[Paths, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, job_id = running_job
    monkeypatch.setattr(ownership, "FileLock", UnopenableLock)
    assert ownership.lock_held(paths, job_id) is True
    assert ownership.liveness(paths, job_id) == "live"
    now = datetime.now(UTC)
    assert ownership.in_progress(paths, job_id, None, now) is True, "the planner still counts it"


def test_recovery_does_not_close_a_job_whose_lock_it_cannot_probe(
    running_job: tuple[Paths, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, job_id = running_job
    monkeypatch.setattr(ownership, "FileLock", UnopenableLock)
    out = ownership.recover(paths, NoRows(), note=False)
    assert out.jobs == [] and out.runs == []
    rec = ownership.read_record(paths, job_id)
    assert rec is not None and rec["state"] == "running", "left open: the next recovery looks again"


def test_a_lock_nobody_holds_is_still_free(running_job: tuple[Paths, str]) -> None:
    paths, job_id = running_job
    assert ownership.lock_held(paths, job_id) is False
    assert ownership.liveness(paths, job_id) == "dead"


class UnlookableLockPath:
    """The lock file's path when even `exists()` is refused (a permission the folder lost)."""

    def exists(self) -> bool:
        raise PermissionError(13, "Access is denied")


def test_a_lock_file_that_cannot_even_be_looked_up_is_held(
    running_job: tuple[Paths, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, job_id = running_job
    monkeypatch.setattr(ownership, "lock_path", lambda p, owner: UnlookableLockPath())
    assert ownership.lock_held(paths, job_id) is True
    assert ownership.liveness(paths, job_id) == "live"


def test_a_run_whose_lock_cannot_be_probed_is_running_and_studio_status_lists_it_as_working(
    running_job: tuple[Paths, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other callers the docstring of `lock_held` names: `run_state` and `status_lines`."""
    paths, job_id = running_job
    run_id = "r-20261109-0001"
    ownership.write_record(paths, run_id, kind="run", state="running", pid=4243)
    ownership.lock_path(paths, run_id).write_text("4243", encoding="utf-8")
    started = datetime.now(UTC).isoformat()
    assert ownership.run_state(paths, run_id, started, None, None)[0] == "interrupted", "a free lock: gone"
    monkeypatch.setattr(ownership, "FileLock", UnopenableLock)
    assert ownership.run_state(paths, run_id, started, None, None) == ("running", "")
    lines = ownership.status_lines(paths, NoRows(), "c1")
    assert any(ln.startswith("  working:") for ln in lines), lines


# --------------------------------------------------------------------- the bound (a Head decision on PR 193)
def notes(paths: Paths) -> list[str]:
    from ns_supervisor.notify import read_notes

    return [str(n["message"]) for n in read_notes(paths)]


def test_a_lock_that_cannot_be_probed_stays_held_while_its_process_is_alive_and_the_owner_is_told_once(
    running_job: tuple[Paths, str], monkeypatch: pytest.MonkeyPatch, pids: Pids
) -> None:
    paths, job_id = running_job
    monkeypatch.setattr(ownership, "FileLock", UnopenableLock)
    for _ in range(ownership.UNPROBED_LIMIT * 4):  # far past the limit: a live process is a reason to wait
        assert ownership.lock_held(paths, job_id) is True
    assert ownership.liveness(paths, job_id) == "live"
    [note] = notes(paths)  # once, not once per probe
    assert note.startswith("NS-W117:") and job_id in note and "process 4242 is still running" in note, note
    assert ownership.unprobed_owners(paths) == [job_id]
    lines = ownership.status_lines(paths, NoRows(), "c1")
    assert any(ln.startswith("  held: NS-W117") for ln in lines), lines
    assert any(ln.startswith("  working:") for ln in lines), lines


def test_a_lock_that_cannot_be_probed_is_dead_after_the_limit_when_its_process_is_gone(
    running_job: tuple[Paths, str], monkeypatch: pytest.MonkeyPatch, pids: Pids
) -> None:
    paths, job_id = running_job
    pids.alive.clear()  # pid 4242 is gone
    monkeypatch.setattr(ownership, "FileLock", UnopenableLock)
    for _ in range(ownership.UNPROBED_LIMIT - 1):  # a few failed probes in a row are not yet proof
        assert ownership.lock_held(paths, job_id) is True
    assert ownership.lock_held(paths, job_id) is False
    assert ownership.liveness(paths, job_id) == "dead"
    assert notes(paths) == [], "its process was gone from the start: nothing to explain, it settled by itself"
    ownership.recover(paths, NoRows(), note=False)
    rec = ownership.read_record(paths, job_id)
    assert rec is not None and rec["state"] == "interrupted", "recover can now close it"


def test_failed_probes_must_be_in_a_row_a_good_probe_starts_the_count_again(
    running_job: tuple[Paths, str], monkeypatch: pytest.MonkeyPatch, pids: Pids
) -> None:
    paths, job_id = running_job
    pids.alive.clear()
    monkeypatch.setattr(ownership, "FileLock", UnopenableLock)
    for _ in range(ownership.UNPROBED_LIMIT - 1):
        assert ownership.lock_held(paths, job_id) is True
    monkeypatch.undo()  # the file opens again: free (nobody holds it)
    monkeypatch.setattr(ownership, "_pid_alive", pids)
    assert ownership.lock_held(paths, job_id) is False
    monkeypatch.setattr(ownership, "FileLock", UnopenableLock)
    for _ in range(ownership.UNPROBED_LIMIT - 1):
        assert ownership.lock_held(paths, job_id) is True, "the earlier failures do not count any more"


def test_a_record_without_a_pid_is_held_for_an_hour_and_then_dead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths(tmp_path / "home")
    monkeypatch.setattr(ownership, "FileLock", UnopenableLock)
    young = datetime.now(UTC).isoformat()
    old = (datetime.now(UTC) - timedelta(seconds=ownership.UNPROBED_UNKNOWN_PID_S + 60)).isoformat()
    for owner, announced in (("j-young", young), ("j-old", old)):
        ownership.write_record(
            paths, owner, kind="job", state="running", channel="c1", announced_at=announced
        )
        ownership.lock_path(paths, owner).write_text("", encoding="utf-8")
    assert ownership.lock_held(paths, "j-young") is True
    assert ownership.lock_held(paths, "j-old") is False


# ------------------------------------------------------------------- every hold has a bound (card lock-bound)
BOUND = ownership.UNPROBED_UNKNOWN_PID_S  # 3600 s; each bound is asked 60 s before it and 60 s after it


class Clock:
    """`ownership._now`, moved by hand."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.start = datetime.now(UTC).replace(microsecond=0)
        self.now = self.start
        monkeypatch.setattr(ownership, "_now", lambda: self.now)

    def at(self, seconds: float) -> None:
        self.now = self.start + timedelta(seconds=seconds)


class Rows(NoRows):
    """`NoRows` that keeps what was written."""

    def __init__(self) -> None:
        super().__init__()
        self.written: list[tuple[str, Any]] = []

    def execute(self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()) -> Any:
        self.written.append((sql, params))
        return None


def unopenable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ownership, "FileLock", UnopenableLock)


def job(paths: Paths, job_id: str, **fields: Any) -> None:
    """A running job record and its lock file, with exactly `fields` beside id, kind, state and channel."""
    ownership.write_record(paths, job_id, kind="job", state="running", channel="c1", **fields)
    ownership.lock_path(paths, job_id).write_text("", encoding="utf-8")


def held_through(paths: Paths, clock: Clock, owner: str, rec: Mapping[str, Any] | None = None) -> list[bool]:
    """Whether `owner` counts as live now, 60 s before the bound and 60 s after it."""
    out = []
    for seconds in (0, BOUND - 60, BOUND + 60):
        clock.at(seconds)
        if rec is None:
            out.append(ownership.lock_held(paths, owner))
        else:
            out.append(ownership.liveness(paths, owner, rec) == "live")
    return out


def test_a_record_that_cannot_be_read_is_held_at_most_the_bound_then_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One bad file among good ones: `recover` read the record, then it was torn (a half-written file)."""
    paths = Paths(tmp_path / "home")
    clock = Clock(monkeypatch)
    job(paths, "j-1", started_at=clock.start.isoformat())  # a Ctrl+C record: `hold` wrote pid=None
    ownership.write_record(paths, "j-2", kind="job", state="done", channel="c1")
    [rec] = [r for r in ownership.list_records(paths) if r["id"] == "j-1"]
    unopenable(monkeypatch)
    torn = ownership.record_path(paths, "j-1")
    torn.write_text('{"id": "j-1",', encoding="utf-8")
    os.utime(torn, (clock.start.timestamp(), clock.start.timestamp()))
    assert ownership.read_record(paths, "j-1") is None
    assert held_through(paths, clock, "j-1", rec) == [True, True, False]  # by the record it was handed
    assert ownership.liveness(paths, "j-1", rec) == "dead"
    [note] = notes(paths)
    assert note.startswith("NS-W118:") and "j-1" in note, note
    ownership._UNPROBED.clear()  # noqa: SLF001 - asked with no record at all: the torn file's own time
    assert held_through(paths, clock, "j-1") == [True, True, False]
    assert [r["id"] for r in ownership.list_records(paths)] == ["j-2"], "the good record is still listed"


def test_a_record_without_pid_or_times_is_held_at_most_the_bound_then_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Killed halfway and asked again; also what a record written by an earlier build may look like."""
    paths = Paths(tmp_path / "home")
    clock = Clock(monkeypatch)
    job(paths, "j-1")
    assert set(ownership.read_record(paths, "j-1") or {}) == {"id", "kind", "state", "channel"}
    for f in (ownership.record_path(paths, "j-1"), ownership.lock_path(paths, "j-1")):
        os.utime(f, (clock.start.timestamp(), clock.start.timestamp()))
    unopenable(monkeypatch)
    assert held_through(paths, clock, "j-1") == [True, True, False]
    assert ownership.liveness(paths, "j-1") == "dead"
    [note] = notes(paths)
    assert note.startswith("NS-W118:") and "j-1" in note, note
    for seconds, held in ((BOUND - 60, True), (BOUND + 60, False)):
        ownership._UNPROBED.clear()  # noqa: SLF001 - a new process: its first look decides the same
        clock.at(seconds)
        assert ownership.lock_held(paths, "j-1") is held, seconds


class NoFile:
    """A path nothing can be asked of (the folder lost every permission)."""

    def exists(self) -> bool:
        raise PermissionError(13, "Access is denied")

    def read_text(self, encoding: str = "utf-8") -> str:
        raise PermissionError(13, "Access is denied")

    def stat(self) -> Any:
        raise PermissionError(13, "Access is denied")


def test_with_no_time_readable_on_disk_the_bound_counts_from_the_first_failed_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths(tmp_path / "home")
    clock = Clock(monkeypatch)
    monkeypatch.setattr(ownership, "lock_path", lambda p, owner: NoFile())
    monkeypatch.setattr(ownership, "record_path", lambda p, owner: NoFile())
    assert held_through(paths, clock, "j-1") == [True, True, False]
    assert ownership.liveness(paths, "j-1") == "unknown", "no record to start from: its callers' 12 h rule"


def test_a_reused_pid_is_not_the_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Pid 4242 is alive in all three, but only a process that started before the record wrote it."""
    paths = Paths(tmp_path / "home")
    clock = Clock(monkeypatch)
    started = {"j-reused": timedelta(hours=2), "j-late": timedelta(seconds=3), "j-mine": timedelta(seconds=1)}
    for owner in started:
        job(paths, owner, pid=4242, started_at=clock.start.isoformat())
    unopenable(monkeypatch)
    limit = ownership.UNPROBED_LIMIT
    for owner, after in started.items():
        monkeypatch.setattr(ownership, "_pid_started", lambda pid, a=after: clock.start + a, raising=False)
        answers = [ownership.lock_held(paths, owner) for _ in range(limit * 2)]
        if owner == "j-mine":  # inside the 2 s slack: record times are whole seconds
            assert answers == [True] * (limit * 2), owner
        else:
            assert answers == [True] * (limit - 1) + [False] * (limit + 1), owner
    said = notes(paths)
    assert len(said) == 1 and said[0].startswith("NS-W117:") and "j-mine" in said[0], said


def test_a_pid_that_cannot_be_checked_falls_back_to_the_time_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows and runner differences: tasklist or psutil missing, refused or failing is "cannot tell"."""
    import ns_supervisor.update_job as update_job

    def refused(pid: int) -> bool:
        raise OSError("tasklist cannot be started")

    paths = Paths(tmp_path / "home")
    clock = Clock(monkeypatch)
    job(paths, "j-nocheck", pid=4243, started_at=clock.start.isoformat())
    unopenable(monkeypatch)
    monkeypatch.setattr(ownership, "_pid_started", lambda pid: None, raising=False)
    monkeypatch.setattr(ownership, "_pid_alive", REAL_PID_ALIVE)
    monkeypatch.setattr(update_job, "pid_alive", refused)
    assert held_through(paths, clock, "j-nocheck") == [True, True, False], "the check itself failed"
    [note] = notes(paths)
    assert note.startswith("NS-W118:") and "j-nocheck" in note, note


def test_our_own_pid_is_alive_only_while_this_process_holds_the_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The engine's pid in a record whose `done` could not be written must not hold the work for weeks."""
    paths = Paths(tmp_path / "home")
    clock = Clock(monkeypatch)
    monkeypatch.setattr(ownership, "_pid_alive", REAL_PID_ALIVE)
    job(paths, "j-left", pid=os.getpid(), started_at=clock.start.isoformat())
    limit = ownership.UNPROBED_LIMIT
    with ownership.hold_job(paths, "j-held", channel="c1"):
        unopenable(monkeypatch)
        clock.at(24 * 3600)
        assert ownership.lock_held(paths, "j-held") is True, "this process is running it"
        clock.at(0)
        assert [ownership.lock_held(paths, "j-left") for _ in range(limit)] == [True] * (limit - 1) + [False]
    [note] = notes(paths)
    assert note.startswith("NS-W117:") and "j-held" in note and f"process {os.getpid()}" in note, note


def story_job(paths: Paths, job_id: str, when: datetime, **fields: Any) -> None:
    """A job `make_job` announced (it gets a Queue card), with its folder."""
    job(paths, job_id, origin="manual", fmt="long", announced_at=when.isoformat(), **fields)
    folder = paths.job_dir(job_id)
    folder.mkdir(parents=True)
    info = {"channel": "c1", "fmt": "long", "created_at": when.isoformat()}
    (folder / "job.json").write_text(json.dumps(info), encoding="utf-8")


def test_recover_closes_work_released_by_the_bound_with_its_retry_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed, then retried, by the route that closes: ONE `recover` in a new process (it asks once, so it
    never reaches the probe count), for a job and the night that made it, two hours after they started."""
    paths = Paths(tmp_path / "home")
    clock = Clock(monkeypatch)
    began = clock.start - timedelta(hours=2)
    story_job(paths, "j-1", began, pid=4243)  # its process is gone
    listed = [{"job_id": "j-1", "channel": "c1"}]
    ownership.write_record(
        paths, "r-1", kind="run", state="running", pid=4243, started_at=began.isoformat(), jobs=listed
    )
    ownership.lock_path(paths, "r-1").write_text("", encoding="utf-8")
    unopenable(monkeypatch)
    db = Rows()
    out = ownership.recover(paths, db)
    assert [j.job_id for j in out.jobs] == ["j-1"] and [r["run_id"] for r in out.runs] == ["r-1"]
    for owner in ("j-1", "r-1"):
        rec = ownership.read_record(paths, owner)
        assert rec is not None and rec["state"] == "interrupted", owner
    [card] = [params for sql, params in db.written if sql.startswith("insert into dashboard_jobs")]
    assert "NS-E328" in str(card) and "j-1" in str(card)
    [note] = notes(paths)
    assert "interrupted" in note and "Press Retry in the Queue" in note, note
    assert ownership.recover(paths, db).any is False, "a second call finds nothing"


def test_a_live_owner_stays_held_past_every_bound_and_its_note_says_how_to_clear_it(
    running_job: tuple[Paths, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one state with no bound: releasing it would let `recover` close a job a process still writes."""
    paths, job_id = running_job
    clock = Clock(monkeypatch)
    unopenable(monkeypatch)
    clock.at(24 * 3600)
    for _ in range(ownership.UNPROBED_LIMIT * 4):
        assert ownership.lock_held(paths, job_id) is True
    assert ownership.recover(paths, NoRows(), note=False).any is False
    [note] = notes(paths)
    assert note.startswith("NS-W117:") and "process 4242" in note and "restart" in note, note


def test_the_live_owner_note_names_the_process_to_end_and_says_that_release_does_not_clear_it(
    running_job: tuple[Paths, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, job_id = running_job
    unopenable(monkeypatch)
    assert ownership.lock_held(paths, job_id) is True
    [note] = notes(paths)
    assert "Ending process 4242 in Task Manager clears it" in note, note
    assert "Release in the Queue does not clear it" in note, note
    [held] = [ln for ln in ownership.status_lines(paths, NoRows(), "c1") if ln.startswith("  held:")]
    assert held == f"  held: {note}", "studio status says the same words as the note"


def test_work_with_no_known_process_says_ns_w118_once_with_the_bound_and_what_follows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths(tmp_path / "home")
    job(paths, "j-1", announced_at=datetime.now(UTC).isoformat())
    unopenable(monkeypatch)
    for _ in range(ownership.UNPROBED_LIMIT * 4):
        assert ownership.lock_held(paths, "j-1") is True
    [note] = notes(paths)  # once, not once per look
    assert note.startswith("NS-W118: Nightshift cannot tell whether this work is still running, and no ")
    for words in ("j-1", "at most an hour", "interrupted", "Retry", "Release in the Queue does", "owners"):
        assert words in note, (words, note)
    lines = ownership.status_lines(paths, NoRows(), "c1")
    assert f"  held: {note}" in lines and any(ln.startswith("  working:") for ln in lines), lines


def test_a_gone_process_short_of_the_limit_is_listed_by_studio_status_and_leaves_no_note(
    running_job: tuple[Paths, str], monkeypatch: pytest.MonkeyPatch, pids: Pids
) -> None:
    """`studio status` looks twice and exits: it never reaches the count, so it says what the hold is."""
    paths, job_id = running_job
    pids.alive.clear()
    unopenable(monkeypatch)
    lines = ownership.status_lines(paths, NoRows(), "c1")
    assert any(ln.startswith("  held: NS-W118:") for ln in lines), lines
    assert notes(paths) == []


# ------------------------------------------------------------------- the reviewer's notes on 9fc64bf
def test_a_failed_tasklist_is_not_a_gone_process(monkeypatch: pytest.MonkeyPatch) -> None:
    """tasklist answers "no process" also when it timed out: only psutil agreeing makes it gone."""
    import ns_supervisor.update_job as update_job

    monkeypatch.setattr(update_job, "pid_alive", lambda pid: False)
    assert REAL_PID_ALIVE(os.getpid()) is None, "psutil sees this process: tasklist failed, cannot tell"
    assert REAL_PID_ALIVE(0x7FFFFFF0) is False, "both say there is no such process"
    monkeypatch.setattr(update_job, "pid_alive", lambda pid: True)
    assert REAL_PID_ALIVE(0x7FFFFFF0) is True


def test_a_process_that_has_the_lock_file_open_is_the_owner_whatever_the_clocks_say(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A clock set back after the process started (a resync on wake) makes it look started 2 h after its
    record. The lock file it holds open is the stronger evidence; a process without it is a reused pid."""
    paths = Paths(tmp_path / "home")
    clock = Clock(monkeypatch)
    for owner in ("j-live", "j-other"):
        job(paths, owner, pid=4242, started_at=clock.start.isoformat())
    unopenable(monkeypatch)
    asked: list[Any] = []

    def holds(pid: int, lock: Path) -> bool:
        asked.append((pid, lock))
        return lock == ownership.lock_path(paths, "j-live")

    monkeypatch.setattr(ownership, "_pid_holds", holds, raising=False)
    monkeypatch.setattr(
        ownership, "_pid_started", lambda pid: clock.start + timedelta(hours=2), raising=False
    )
    clock.at(24 * 3600)
    limit = ownership.UNPROBED_LIMIT
    assert [ownership.lock_held(paths, "j-live") for _ in range(limit * 2)] == [True] * (limit * 2)
    assert (4242, ownership.lock_path(paths, "j-live")) in asked
    [note] = notes(paths)
    assert note.startswith("NS-W117:") and "j-live" in note and "process 4242" in note, note
    clock.at(0)  # the same late process does not have the other job's lock file open: a reused pid
    assert [ownership.lock_held(paths, "j-other") for _ in range(limit)] == [True] * (limit - 1) + [False]


def test_a_live_process_whose_start_time_cannot_be_read_is_never_cut_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A long healthy stage: the pid exists, psutil cannot read its start or its files. Held, with NS-W117."""
    paths = Paths(tmp_path / "home")
    clock = Clock(monkeypatch)
    job(paths, "j-1", pid=4242, started_at=clock.start.isoformat())
    unopenable(monkeypatch)
    monkeypatch.setattr(ownership, "_pid_started", lambda pid: None, raising=False)
    assert held_through(paths, clock, "j-1") == [True, True, True]
    clock.at(24 * 3600)
    assert ownership.liveness(paths, "j-1") == "live"
    assert ownership.recover(paths, NoRows(), note=False).any is False
    [note] = notes(paths)
    assert note.startswith("NS-W117:") and "process 4242, python.exe, has the number" in note, note
    assert "it may be another program" in note and "Do not end process 4242 unless" in note, note
    assert "Ending process 4242" not in note and "restarting the PC clears this safely" in note, note
    [held] = [ln for ln in ownership.status_lines(paths, NoRows(), "c1") if ln.startswith("  held:")]
    assert held == f"  held: {note}"


def test_an_unreadable_start_time_does_not_hold_work_for_a_program_that_is_not_nightshifts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pid went to another program (its name says so): never named to the owner, never held for it."""
    paths = Paths(tmp_path / "home")
    job(paths, "j-1", pid=4242, started_at=datetime.now(UTC).isoformat())
    job(paths, "j-2", pid=4242, started_at=datetime.now(UTC).isoformat())
    unopenable(monkeypatch)
    monkeypatch.setattr(ownership, "_pid_started", lambda pid: None, raising=False)
    monkeypatch.setattr(ownership, "_pid_name", lambda pid: "chrome.exe", raising=False)
    limit = ownership.UNPROBED_LIMIT
    assert [ownership.lock_held(paths, "j-1") for _ in range(limit)] == [True] * (limit - 1) + [False]
    assert notes(paths) == []
    monkeypatch.setattr(ownership, "_pid_name", lambda pid: None, raising=False)
    assert [ownership.lock_held(paths, "j-2") for _ in range(limit * 2)] == [True] * (limit * 2)
    [note] = notes(paths)
    assert "process 4242, a program whose name cannot be read, has the number" in note, note


def test_windows_is_asked_who_has_the_lock_file_open_once_a_minute_not_on_every_look(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = Paths(tmp_path / "home")
    clock = Clock(monkeypatch)
    job(paths, "j-1", pid=4242, started_at=clock.start.isoformat())
    unopenable(monkeypatch)
    asked: list[int] = []
    monkeypatch.setattr(ownership, "_pid_holds", lambda pid, lock: asked.append(pid) or True, raising=False)
    monkeypatch.setattr(
        ownership, "_pid_started", lambda pid: clock.start + timedelta(hours=2), raising=False
    )
    keep = ownership.PID_HOLDS_KEEP_S
    for seconds, calls in ((0, 1), (5, 1), (keep - 1, 1), (keep + 1, 2), (keep + 30, 2), (2 * keep + 2, 3)):
        clock.at(seconds)
        assert ownership.lock_held(paths, "j-1") is True
        assert len(asked) == calls, (seconds, asked)


HOLDER = (
    "import os, sys; from pathlib import Path; from ns_common.singleton import FileLock; "
    "lock = FileLock(Path(sys.argv[1])); lock.acquire(); print(os.getpid(), flush=True); sys.stdin.readline()"
)


@pytest.mark.skipif(sys.platform != "win32", reason="the Restart Manager is a Windows service")
def test_the_real_restart_manager_names_the_process_that_holds_a_lock_file(tmp_path: Path) -> None:
    """No stand-in: `_pid_holds` itself, against a child that holds a real `FileLock`."""
    lock = tmp_path / "owners" / "j-1.lock"
    other = tmp_path / "owners" / "j-2.lock"
    lock.parent.mkdir()
    other.write_text("", encoding="utf-8")
    repo = Path(__file__).resolve().parents[1]
    child = subprocess.Popen(
        [sys.executable, "-c", HOLDER, str(lock)],
        cwd=repo, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    pid = 0
    try:
        assert child.stdout is not None and child.stdin is not None
        pid = int(child.stdout.readline())  # the interpreter's own pid (a venv launcher has another one)
        assert REAL_PID_HOLDS(pid, lock) is True
        assert REAL_PID_HOLDS(os.getpid(), lock) is False, "this process does not have it open"
        assert REAL_PID_HOLDS(pid, other) is False, "the holder does not have the other file open"
        child.stdin.write("\n")  # the holder ends by itself; nothing is killed
        child.stdin.flush()
        child.wait(30)
        assert REAL_PID_HOLDS(pid, lock) is False, "after it ended nobody has the file open"
    finally:
        if child.poll() is None and child.stdin is not None:
            child.stdin.close()  # its readline returns and it ends
            try:
                child.wait(30)
            except subprocess.TimeoutExpired:
                if pid:  # the interpreter, by the pid it reported
                    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, check=False)
                child.kill()
