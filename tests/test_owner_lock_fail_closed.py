"""D-31b follow-up (with F-9b): an owner lock that cannot be probed means "held", never "free".

`lock_held` opens the owner's lock file to see whether a live process holds it. An `OSError` there (an
antivirus or a backup tool holding the file, a permission the folder lost) said "not held", so `liveness`
called live work "dead" and `recover` closed a running job as interrupted. Every caller now gets the cautious
answer:
`liveness` says "live", `recover` leaves the job open (the next call tries again), the planner counts it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
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
