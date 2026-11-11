# ruff: noqa: E501
"""FN-4, T1: NS-E201 ("the graphics card is busy") is true for every one of its raisers.

The code is raised for the nightly run, for a dashboard job that waited at the GPU gate, for a dashboard job
whose engine request was refused, for a `studio` command, for Nightshift Code (the `/acquire` answer) and for
a waiter whose owner was released. Its old words named the nightly run for all of them and promised "the
next scheduled run", which does not come while Automatic runs are off. Each case below takes the text the
owner gets on that route.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

import typer  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from ns_common.errors import NsError, describe  # noqa: E402
from ns_common.logging import NsLogger  # noqa: E402
from ns_common.models import Runtime  # noqa: E402
from ns_common.paths import Paths  # noqa: E402
from ns_dashboard.events import EventHub  # noqa: E402
from ns_dashboard.jobs import JobControl, JobStore, JobWorker, make_gpu_gate  # noqa: E402
from ns_dashboard.state import open_db  # noqa: E402
from ns_dashboard.supervisor_link import HttpAcquirer  # noqa: E402
from ns_studio import cli as studio_cli  # noqa: E402
from ns_studio import daily, jobs  # noqa: E402
from ns_studio.ownership import IF_NIGHTLY_ON  # noqa: E402
from ns_supervisor.app import create_app  # noqa: E402
from ns_supervisor.client import SupervisorClient  # noqa: E402
from ns_supervisor.core import Supervisor  # noqa: E402
from ns_supervisor.lock import GpuLock  # noqa: E402
from ns_supervisor.schedule_cfg import write_run_time  # noqa: E402
from ns_supervisor.testing import StubTable, fast_config  # noqa: E402
from tests.test_nightly_promise_is_conditional import PROMISES  # noqa: E402

NOW = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)
BASE = "http://127.0.0.1:18099"   # the engine answers loopback addresses only (FN-1)
CONDITION = IF_NIGHTLY_ON.strip(" ()")    # "while Automatic runs are on in Settings"
CASES = (
    "nightly, Automatic runs on", "nightly, Automatic runs off", "dashboard job at the GPU gate",
    "dashboard job refused by the engine", "studio command", "Nightshift Code", "waiter released",
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[TestClient]:
    """The real engine API over stub services, with the graphics card held by Nightshift Code."""
    sup = Supervisor(
        Paths(tmp_path / "ns"), Runtime.default("gpu"), config=fast_config(idle_unload_s=60), resolver=StubTable()
    )
    with TestClient(create_app(sup), base_url=BASE) as tc:
        assert tc.post("/acquire", json={"service": "llama", "owner": "code"}).status_code == 200
        yield tc


def dashboard_job(tmp_home: Path, handler: Any, **kw: Any) -> Any:
    """One dashboard job run by the real worker: its record is what the Queue's card shows."""
    paths = Paths.from_env()
    store = JobStore(open_db(paths))
    worker = JobWorker(store, {"run": handler}, EventHub(), NsLogger("dashboard", paths.logs), **kw)
    job = store.create("run", "c", {})
    worker.run_next()
    rec = store.get(job.job_id)
    assert rec is not None and rec.status == "failed" and rec.result is not None
    return rec


def shown(case: str, tmp_home: Path, engine: TestClient) -> tuple[str, dict[str, Any]]:
    """The words the owner gets for `case` (message and hint as one text), and what the route knows."""
    client = SupervisorClient(BASE, client=engine)
    if case.startswith("nightly"):
        from tests.test_cadence import _channel, _env

        paths, db = _env(tmp_home)
        _channel(paths, db, 9, format="shorts_only", shorts_per_day_max=1)
        if case.endswith("on"):
            write_run_time(paths, "03:00", "registered")

        def busy(ctx: Any) -> None:
            client.acquire("tts", f"studio:{ctx.job_id}", timeout_s=0)

        report = daily.run_daily(jobs.open_env(light=True), now=NOW, rng=random.Random(1), run_fn=busy)
        # vram-busy: the job that met the busy card is not a failure; it waits for the next night, and what
        # the owner reads is its Queue card (its words, its code and the code's hint)
        (waiting,) = [i for i in report["interrupted_jobs"] if i["state"] == "waiting"]
        import json

        row = db.one("select message, params_json from dashboard_jobs where kind='run' and params_json like ?",
                     (f"%{waiting['job_id']}%",))  # fmt: skip  (the card names the studio job in its result)
        error = json.loads(row["params_json"])["result"]["error"]
        return f"{row['message']} {error['hint']}", {"code": error["code"]}
    if case == "dashboard job at the GPU gate":
        gate = make_gpu_gate(lambda: engine.get("/status").json(), sleeper=lambda s: None, poll_s=10, max_wait_s=30)
        rec = dashboard_job(tmp_home, lambda ctl: None, gpu_gate=gate)
        return f"{rec.message} {rec.result['error']['hint']}", rec.result
    if case == "dashboard job refused by the engine":
        link = HttpAcquirer(BASE, client=engine)

        def handler(ctl: JobControl) -> None:
            link.acquire("tts", "dashboard:preview-1", timeout_s=0)

        rec = dashboard_job(tmp_home, handler)
        return f"{rec.message} {rec.result['error']['hint']}", rec.result["error"]
    if case == "studio command":
        app = typer.Typer()

        @app.command()
        def run() -> None:
            with studio_cli.guarded():
                client.acquire("tts", "studio:by-hand", timeout_s=0)

        res = CliRunner().invoke(app, [])
        assert res.exit_code == 1
        return res.output, {}
    if case == "Nightshift Code":
        r = engine.post("/acquire", json={"service": "tts", "owner": "code-2", "timeout_s": 0.05})
        assert r.status_code == 409
        return f"{r.json()['message']} ({r.json()['detail']}) {describe(r.json()['code']).hint}", r.json()
    assert case == "waiter released"

    async def released() -> str:
        lock = GpuLock()
        await lock.acquire("holder", "llama", None)
        waiting = asyncio.create_task(lock.acquire("studio:gone", "tts", 30))
        await asyncio.sleep(0)
        assert lock.drop_waiters("studio:gone") == 1
        with pytest.raises(NsError) as ei:
            await waiting
        return f"{ei.value} {ei.value.definition.hint}"

    return asyncio.run(released()), {}


@pytest.mark.parametrize("case", CASES)
def test_ns_e201_is_true_for_every_raiser(case: str, tmp_home: Path, engine: TestClient) -> None:
    text, known = shown(case, tmp_home, engine)
    assert "NS-E201" in text or known.get("code") == "NS-E201", text
    # no raiser but the nightly run is "the nightly run", and nobody "gave up" who never waited
    assert "nightly run gave up" not in text and "next scheduled run" not in text, text
    assert describe("NS-E201").message in text
    low = text.lower()
    if any(p in low for p in PROMISES):  # a promised night carries its condition, in the words Settings uses
        assert CONDITION in text, text
    # the way out it names exists on this route: nothing is called "Retry" where the card has no such button
    assert "has Retry" not in text
    if case == "dashboard job at the GPU gate":
        # this card has no "Retry from where it stopped" (ui/JobError.tsx: only a job that made a studio job)
        assert not known.get("studio_job_ids") and "start this task again" in text


def test_the_ns_e201_hint_names_things_that_exist() -> None:
    hint = describe("NS-E201").hint
    root = Path(__file__).resolve().parents[1] / "ns_dashboard" / "ui" / "src"
    # the button of a failed job that can be continued, under its own label
    assert "Retry from where it stopped" in hint
    assert '"Retry from where it stopped"' in (root / "ui" / "JobError.tsx").read_text(encoding="utf-8")
    # the Queue says what holds the graphics card
    assert "the Queue says what holds it" in hint
    assert "GPU held by" in (root / "pages" / "Queue.tsx").read_text(encoding="utf-8")
    assert len(hint) <= 400
