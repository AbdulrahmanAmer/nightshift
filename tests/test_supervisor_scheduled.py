"""A scheduled dashboard job never keeps the engine awake, and a due one wakes the dashboard at startup."""

import asyncio
import contextlib
import json
import sqlite3
import time
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ns_common.db import Db
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_supervisor.core import Supervisor
from ns_supervisor.testing import StubTable, fast_config

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ns_supervisor.app import create_app  # noqa: E402


def put_job(paths: Paths, schedule_at: datetime) -> None:
    paths.db_path.parent.mkdir(parents=True, exist_ok=True)
    db = Db(paths.db_path)
    db.migrate()
    db.close()
    con = sqlite3.connect(paths.db_path)
    at = schedule_at.isoformat(timespec="seconds")
    params = {"params": {"channel": "x", "schedule_at": at}, "result": None}
    con.execute(
        "INSERT INTO dashboard_jobs(job_id, kind, channel, status, progress, message, created_at,"
        " params_json) VALUES ('j1', 'run', 'x', 'queued', 0, 'Scheduled', '2026-11-02T00:00:00', ?)",
        (json.dumps(params),),
    )
    con.commit()
    con.close()


def make(tmp_path: Path, **cfg: Any) -> tuple[Supervisor, Paths]:
    paths = Paths(tmp_path / "ns")
    sup = Supervisor(paths, Runtime.default("gpu"), config=fast_config(**cfg), resolver=StubTable())
    return sup, paths


@contextlib.asynccontextmanager
async def running(sup: Supervisor) -> AsyncIterator[Supervisor]:
    mon = asyncio.create_task(sup.run_monitor())
    try:
        yield sup
    finally:
        mon.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await mon
        await sup.stop_all(include_persistent=True)
        sup.close_job()


async def until(pred: Any, timeout: float = 25.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        await asyncio.sleep(0.05)
    return False


def test_a_scheduled_job_does_not_keep_the_engine_awake(tmp_path: Path) -> None:
    sup, paths = make(tmp_path, auto_exit_idle_s=0.4)
    put_job(paths, datetime.now().astimezone() + timedelta(minutes=10))  # even one that is due soon

    async def main() -> None:
        async with running(sup):
            assert sup._busy() is False
            assert await until(sup.exit_requested.is_set)

    asyncio.run(main())


def test_startup_wakes_the_dashboard_for_an_overdue_scheduled_job(tmp_path: Path) -> None:
    sup, paths = make(tmp_path)
    put_job(paths, datetime.now().astimezone() - timedelta(minutes=3))
    warmed: list[str] = []

    async def fake_warmup(service: str) -> str:
        warmed.append(service)
        return "ready"

    sup.warmup = fake_warmup  # type: ignore[method-assign]
    with TestClient(create_app(sup, start_dashboard=False, run_monitor=False)):
        assert until_sync(lambda: warmed == ["dashboard"])


def test_startup_leaves_the_dashboard_alone_without_due_jobs(tmp_path: Path) -> None:
    sup, paths = make(tmp_path)
    put_job(paths, datetime.now().astimezone() + timedelta(hours=4))
    warmed: list[str] = []

    async def fake_warmup(service: str) -> str:
        warmed.append(service)
        return "ready"

    sup.warmup = fake_warmup  # type: ignore[method-assign]
    with TestClient(create_app(sup, start_dashboard=False, run_monitor=False)) as tc:
        assert tc.get("/health").json() == {"ok": True}
        time.sleep(0.3)
    assert warmed == []


def until_sync(pred: Any, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False
