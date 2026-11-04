"""Gap P5: a story job that failed late is continued from its failed stage, not repeated with a new story."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_dashboard import studio_bridge, studio_job
from ns_dashboard.errors import ApiError
from ns_dashboard.fakes import seed_channel
from ns_dashboard.handlers import Dashboard, build_state
from ns_dashboard.supervisor_link import SupervisorLink
from ns_studio import jobs
from ns_studio.context import FAKEABLE
from ns_studio.orchestrator import FORMAT_STAGES
from ns_studio.stages import register

CALLS: list[str] = []
FAIL = {"b": True}


class StubA:
    name = "r3_a"
    tenant = "llama"
    inputs = ()
    outputs = ("a.json",)

    def run(self, ctx: Any) -> None:
        CALLS.append("a")
        ctx.write_json("a.json", {"n": 1})


class StubB:
    name = "r3_b"
    tenant = None
    inputs = ("a.json",)
    outputs = ("b.json",)

    def run(self, ctx: Any) -> None:
        CALLS.append("b")
        if FAIL["b"]:
            raise NsError("NS-E310", "tts batch: no answer")
        ctx.write_json("b.json", {"n": 2})


@pytest.fixture
def stubs(monkeypatch: pytest.MonkeyPatch):
    CALLS.clear()
    FAIL["b"] = True
    register(StubA())
    register(StubB())
    monkeypatch.setitem(FORMAT_STAGES, "shorts_only", ["r3_a", "r3_b"])
    monkeypatch.setattr(jobs, "STAGE_MODULES", ())
    monkeypatch.setattr("ns_studio.policy.gate.available", lambda: False)


# ---------------------------------------------------------------- the studio side


def test_the_bridge_continues_the_failed_job_from_its_failed_stage(tmp_home: Path, stubs):
    env = jobs.open_env(FAKEABLE)
    jobs.ensure_demo_channel(env)
    seen: list[str] = []

    def report(stage: str, index: int, total: int, job_index: int, job_total: int) -> None:
        pass

    with pytest.raises(NsError):
        studio_bridge.run_story_jobs("demo", "shorts_only", 1, report, fake=tuple(FAKEABLE), seen=seen)
    assert CALLS == ["a", "b"] and len(seen) == 1
    first = seen[0]

    FAIL["b"] = False
    CALLS.clear()
    again: list[str] = []
    studio_bridge.run_story_jobs(
        "demo", "shorts_only", 1, report, fake=tuple(FAKEABLE), seen=again, resume_job_id=first
    )
    assert CALLS == ["b"]  # stage a had finished: it is not run (and the story not written) again
    assert again == [first]  # the same studio job, not a new one
    assert (Paths.from_env().job_dir(first) / "b.json").exists()


def test_the_child_passes_the_job_to_resume(tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    got: dict[str, Any] = {}

    def fake_run(channel: str, fmt: str, count: int | None, report: Any, **kw: Any) -> list[str]:
        got.update(kw)
        return []

    monkeypatch.setattr(studio_bridge, "run_story_jobs", fake_run)
    args = tmp_path / "args.json"
    base = {"channel": "after-dark", "fmt": "shorts_only", "count": 1}
    args.write_text(json.dumps({**base, "resume_job_id": "after-dark-20261104-ab12"}), encoding="utf-8")
    assert studio_job.main(["run", "--args", str(args)]) == 0
    assert got["resume_job_id"] == "after-dark-20261104-ab12"
    args.write_text(json.dumps(base), encoding="utf-8")
    assert studio_job.main(["run", "--args", str(args)]) == 0
    assert got["resume_job_id"] is None


# ---------------------------------------------------------------- the dashboard side


def make(tmp_home: Path) -> Dashboard:
    state = build_state(
        Paths.from_env(), fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    )
    seed_channel(state.db, state.paths, "after-dark")
    return Dashboard(state)


def failed_run(dash: Dashboard, studio_ids: list[str] | None, *, kind: str = "run") -> str:
    s = dash.state
    rec = s.store.create(kind, "after-dark", {"channel": "after-dark", "format": "shorts_only", "count": 2})
    s.store.mark_running(rec.job_id)
    result: dict[str, Any] = {"error": {"code": "NS-E310", "hint": None, "stage": "voice"}}
    if studio_ids is not None:
        result["studio_job_ids"] = studio_ids
    s.store.finish(rec.job_id, "failed", "voice failed", result)
    for sid in studio_ids or []:
        folder = s.paths.job_dir(sid)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "job.json").write_text("{}", encoding="utf-8")
    return rec.job_id


def test_resume_queues_the_last_studio_job_as_one_run(tmp_home: Path):
    dash = make(tmp_home)
    failed = failed_run(dash, ["after-dark-20261104-aaaa", "after-dark-20261104-bbbb"])
    new = dash.jobs.create_resume(failed)
    assert new["kind"] == "run" and new["status"] == "queued" and new["job_id"] != failed
    assert new["params"] == {
        "channel": "after-dark", "format": "shorts_only", "count": 1,
        "resume_job_id": "after-dark-20261104-bbbb", "resume_of": failed,
    }
    with pytest.raises(ApiError) as ei:  # not twice
        dash.jobs.create_resume(failed)
    assert ei.value.status == 409 and ei.value.code == "already_queued"


def test_the_worker_hands_the_studio_job_to_the_run_adapter(tmp_home: Path):
    dash = make(tmp_home)
    failed = failed_run(dash, ["after-dark-20261104-bbbb"])
    new = dash.jobs.create_resume(failed)
    assert dash.state.worker.run_next()
    done = dash.state.store.get(new["job_id"])
    assert done is not None and done.status == "done"
    assert done.result is not None and done.result["resumed"] == "after-dark-20261104-bbbb"
    assert dash.state.adapters.run.resumed == ["after-dark-20261104-bbbb"]  # type: ignore[attr-defined]
    plain = dash.jobs.create_run({"channel": "after-dark", "format": "shorts_only"})
    assert "resume_job_id" not in plain["params"]  # a normal run does not carry the field


def test_resume_refuses_what_cannot_be_continued(tmp_home: Path):
    dash = make(tmp_home)
    for ids in (None, [], ["gone-20261104-cccc"]):
        failed = failed_run(dash, ids)
        if ids:
            shutil.rmtree(dash.state.paths.job_dir(ids[0]))  # the folder was deleted
        with pytest.raises(ApiError) as ei:
            dash.jobs.create_resume(failed)
        assert ei.value.status == 409 and ei.value.code == "cannot_resume", ids


def test_resume_refuses_a_job_that_did_not_fail_or_is_not_a_story(tmp_home: Path):
    dash = make(tmp_home)
    queued = dash.state.store.create("run", "after-dark", {"channel": "after-dark", "format": "shorts_only"})
    with pytest.raises(ApiError) as ei:
        dash.jobs.create_resume(queued.job_id)
    assert ei.value.code == "not_failed"
    verify = dash.state.store.create("verify", None, {})
    with pytest.raises(ApiError) as ei:
        dash.jobs.create_resume(verify.job_id)
    assert ei.value.status == 404
    with pytest.raises(ApiError) as ei:
        dash.jobs.create_resume("nope")
    assert ei.value.status == 404


def test_resume_still_needs_a_footage_key(tmp_home: Path):
    dash = make(tmp_home)
    failed = failed_run(dash, ["after-dark-20261104-bbbb"])
    dash.state.fake_services = False
    with pytest.raises(ApiError) as ei:
        dash.jobs.create_resume(failed)
    assert ei.value.code == "NS-E502"
    dash.state.adapters.keyring.set("pixabay_api_key", "k")
    assert dash.jobs.create_resume(failed)["status"] == "queued"


def test_the_route_and_the_assistant_tool(tmp_home: Path):
    from fastapi.testclient import TestClient

    from ns_dashboard.assistant.tools import build_registry
    from ns_dashboard.server import create_app

    dash = make(tmp_home)
    failed = failed_run(dash, ["after-dark-20261104-bbbb"])
    app = create_app(state=dash.state, start_worker=False, static_dir=tmp_home / "absent")
    client = TestClient(app, base_url="http://127.0.0.1:8100")
    r = client.post(f"/api/jobs/{failed}/resume")
    assert r.status_code == 201 and r.json()["params"]["resume_job_id"] == "after-dark-20261104-bbbb"
    assert client.post("/api/jobs/nope/resume").status_code == 404

    td = build_registry().get("resume_job")
    assert td is not None and td.spec.side_effect and td.spec.topic == "create"
    assert td.route == ("POST", "/api/jobs/{job_id}/resume")
    assert "{job_id}" in td.spec.confirm_text
