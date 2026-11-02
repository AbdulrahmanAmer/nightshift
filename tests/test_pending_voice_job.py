"""Pre-tag item 4 (system-gaps rows 4, 5, 6; final-engine row 1): the pending setup voice.

a) the voice made at the start of a job is held under the job's own GPU owner, so Cancel frees it;
b) "Create it now" is a dashboard job (kind `apply_voice`) with progress and Cancel, not an HTTP request;
c) the setup window no longer promises an automatic voice design download.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ns_dashboard.adapters import RealVoiceMaker
from ns_dashboard.events import EventHub
from ns_dashboard.jobs import JobCancelled, JobControl
from ns_dashboard.supervisor_link import SupervisorLink
from ns_studio import voice_setup
from tests.helpers import make_ctx
from tests.test_dashboard_api import make_env
from tests.test_pending_voice_run import NOT_TTS, queue

ROOT = Path(__file__).resolve().parents[1]


def queue_setup_voice(paths: Any, **kw: Any) -> None:
    f = paths.config / "pending_voice.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    body = {"channel": "after-dark", "mode": "design", "name": "Hollow", "description": "a slow voice", **kw}
    f.write_text(json.dumps(body), encoding="utf-8")


# ---------------------------------------------------------------- a) the owner


def test_the_voice_made_at_the_start_of_a_job_is_held_under_the_jobs_own_owner(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    queue(tmp_home)
    ctx = make_ctx(tmp_home, fake=NOT_TTS)
    owners: list[tuple[str, str]] = []
    real_acquire, real_release = ctx.services.acquirer.acquire, ctx.services.acquirer.release

    def acquire(service, owner, *a, **kw):  # type: ignore[no-untyped-def]
        owners.append(("acquire", owner))
        return real_acquire(service, owner, *a, **kw)

    def release(service, owner):  # type: ignore[no-untyped-def]
        owners.append(("release", owner))
        return real_release(service, owner)

    def post(base_url: str, route: str, body: dict[str, Any]) -> dict[str, Any]:
        folder = ctx.paths.models_tts_voices / body["name"]
        folder.mkdir(parents=True)
        (folder / "voice.json").write_text("{}", encoding="utf-8")
        return {}

    monkeypatch.setattr(ctx.services.acquirer, "acquire", acquire)
    monkeypatch.setattr(ctx.services.acquirer, "release", release)
    monkeypatch.setattr(voice_setup, "http_post", post)
    assert voice_setup.apply_pending_voices(ctx) == "c-voice"
    mine = f"studio:{ctx.job_id}"  # not studio:voice-c-voice: the dashboard's Cancel frees the job's owners
    assert owners == [("acquire", mine), ("release", mine)]


def test_without_a_job_the_voice_keeps_its_own_owner(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    queue(tmp_home)
    ctx = make_ctx(tmp_home, fake=NOT_TTS)
    seen: list[str] = []
    real_acquire = ctx.services.acquirer.acquire

    def acquire(service, owner, *a, **kw):  # type: ignore[no-untyped-def]
        seen.append(owner)
        return real_acquire(service, owner, *a, **kw)

    monkeypatch.setattr(ctx.services.acquirer, "acquire", acquire)
    env = voice_setup.Env(ctx.paths, ctx.db, ctx.runtime, ctx.services, ctx.log, frozenset())
    voice_setup.apply_pending(env, post=lambda *a: {})  # `studio voice apply-pending` and the Library job
    assert seen == ["studio:voice-c-voice"]


# ---------------------------------------------------------------- b) Create it now is a job


def test_create_it_now_queues_an_apply_voice_job_and_asking_again_returns_the_same_one(tmp_home: Path):
    client, state = make_env(tmp_home)
    nothing = {"created": None, "pending": None, "job_id": None}
    assert client.post("/api/voices/apply-pending").json() == nothing
    queue_setup_voice(state.paths)
    first = client.post("/api/voices/apply-pending").json()
    again = client.post("/api/voices/apply-pending").json()
    assert first["job_id"] and again["job_id"] == first["job_id"]
    job = state.store.get(first["job_id"])
    assert job is not None and job.kind == "apply_voice" and job.status == "queued"
    assert job.params == {"name": "Hollow"} and job.channel == "after-dark"


def test_the_job_reports_progress_and_makes_the_voice(tmp_home: Path):
    client, state = make_env(tmp_home)
    queue_setup_voice(state.paths)
    job_id = client.post("/api/voices/apply-pending").json()["job_id"]
    state.worker.run_next()
    job = client.get(f"/api/jobs/{job_id}").json()
    assert job["status"] == "done" and job["result"]["created"] == "Hollow"
    assert ("apply_pending", "Hollow", "") in state.adapters.voice_maker.calls  # type: ignore[attr-defined]
    assert client.get("/api/voices/pending").json() == {"pending": None}


def test_the_job_can_be_cancelled_from_the_queue_and_the_request_stays(tmp_home: Path):
    client, state = make_env(tmp_home)
    queue_setup_voice(state.paths)
    job_id = client.post("/api/voices/apply-pending").json()["job_id"]
    assert client.post(f"/api/jobs/{job_id}/cancel").json()["status"] == "cancelled"
    assert state.worker.run_next() is False  # nothing left to run
    assert client.get("/api/voices/pending").json()["pending"]["name"] == "Hollow"
    again = client.post("/api/voices/apply-pending").json()  # a new attempt is a new job
    assert again["job_id"] and again["job_id"] != job_id


class CancellingRunner:
    """Stands in for `StudioRunner`: the owner pressed Cancel while the child was making the voice."""

    def run(self, kind: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
        assert kind == "apply_voice"
        kw["check_cancel"]()
        raise AssertionError("check_cancel should have raised")


class RecordingLink(SupervisorLink):
    def __init__(self) -> None:
        super().__init__("http://127.0.0.1:9", timeout_s=0.2)
        self.released: list[str] = []

    def release_owner(self, owner: str, *, unless_alive: bool = False) -> bool:
        self.released.append(owner)
        return True


def test_cancelling_the_real_job_releases_the_lease_of_the_voice(tmp_home: Path):
    from ns_common.paths import Paths

    paths = Paths.from_env()
    paths.ensure()
    queue_setup_voice(paths)
    _client, state = make_env(tmp_home)
    rec = state.store.create("apply_voice", "after-dark", {"name": "Hollow"})
    ctl = JobControl(rec, state.store, EventHub())
    state.store.request_cancel(rec.job_id)  # the Cancel click
    link = RecordingLink()
    maker = RealVoiceMaker(paths, link, runner=CancellingRunner())  # type: ignore[arg-type]
    with pytest.raises(JobCancelled):
        maker.apply_pending(ctl)
    assert link.released == ["studio:voice-Hollow"]  # not left for the 10-minute lease expiry


def test_the_real_job_passes_the_result_on_and_reports_the_end(tmp_home: Path):
    from ns_common.paths import Paths

    paths = Paths.from_env()
    paths.ensure()
    queue_setup_voice(paths)
    store = make_env(tmp_home)[1].store
    rec = store.create("apply_voice", "after-dark", {"name": "Hollow"})
    ctl = JobControl(rec, store, EventHub())

    class Done:
        def run(self, kind: str, args: dict[str, Any], **kw: Any) -> dict[str, Any]:
            return {"name": "Hollow"}

    maker = RealVoiceMaker(paths, RecordingLink(), runner=Done())  # type: ignore[arg-type]
    assert maker.apply_pending(ctl) == "Hollow"
    assert store.get(rec.job_id).progress == 1.0  # type: ignore[union-attr]
    assert maker.apply_pending() == "Hollow"  # the old call without a job still works


def test_a_busy_gpu_makes_the_job_wait_in_the_queue_not_the_request():
    from ns_dashboard.jobs import GPU_KINDS, KINDS

    assert "apply_voice" in KINDS and "apply_voice" in GPU_KINDS


def test_the_page_follows_the_job_with_progress_and_cancel():
    src = (ROOT / "ns_dashboard" / "ui" / "src" / "pages" / "Library.tsx").read_text(encoding="utf-8")
    assert "r.job_id" in src and "/jobs/${encodeURIComponent(jobId)}" in src and "/cancel" in src
    assert ">Cancel</Button>" in src and "Voice progress" in src
    queue = (ROOT / "ns_dashboard" / "ui" / "src" / "pages" / "Queue.tsx").read_text(encoding="utf-8")
    assert 'j.kind === "apply_voice"' in queue
    assert "job_id?: string | null" in (ROOT / "ns_dashboard" / "ui" / "src" / "lib" / "types.ts").read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------- c) the setup hint


def test_the_setup_window_no_longer_promises_an_automatic_voice_design_download():
    copy = (ROOT / "installer" / "ui" / "src" / "copy.ts").read_text(encoding="utf-8")
    assert "downloads it then" not in copy and "the first voice you design" not in copy
    hint = next(line for line in copy.splitlines() if "voicedesignHint" in line)
    assert "Settings › Models › Get voice design model" in hint
    assert "nothing downloads it by itself" in hint
