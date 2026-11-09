"""D-31b follow-up (with F-9b): an owner lock that cannot be probed means "held", never "free".

`lock_held` opens the owner's lock file to see whether a live process holds it. An `OSError` there (an
antivirus or a backup tool holding the file, a permission the folder lost) said "not held", so `liveness`
called live work "dead" and `recover` closed a running job as interrupted. Every caller now gets the cautious
answer:
`liveness` says "live", `recover` leaves the job open (the next call tries again), the planner counts it.
"""

from __future__ import annotations

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


@pytest.fixture(autouse=True)
def pids(monkeypatch: pytest.MonkeyPatch) -> Pids:
    fake = Pids()
    monkeypatch.setattr(ownership, "_pid_alive", fake)
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
        announced_at=datetime(2026, 11, 9, 1, 0, tzinfo=UTC).isoformat(),
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
    out = ownership.recover(paths, NoRows(), note=False)
    rec = ownership.read_record(paths, job_id)
    assert rec is not None and rec["state"] == "interrupted", "recover can now close it"
    assert [j.job_id for j in out.jobs] == [job_id] or out.any


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
