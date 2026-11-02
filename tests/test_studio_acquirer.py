"""Gap 1 and J7: the studio acquires real services through the supervisor instead of `DevAcquirer`."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ns_common.acquire import DevAcquirer, Endpoint
from ns_common.errors import NsError
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_studio import daily, jobs
from ns_studio import orchestrator as orch
from ns_studio.acquirer import WAIT_CAP_S, StudioAcquirer, wants_supervisor
from ns_studio.context import FAKEABLE, build_services
from ns_studio.stages import register
from tests.helpers import make_ctx


class FakeClient:
    """Records the calls a `SupervisorClient` would receive."""

    def __init__(self, fail_with: NsError | None = None) -> None:
        self.calls: list[tuple] = []
        self.fail_with = fail_with

    def acquire(self, service, owner, *, model_alias=None, timeout_s=7200) -> Endpoint:
        self.calls.append(("acquire", service, owner, model_alias, timeout_s))
        if self.fail_with is not None:
            raise self.fail_with
        return Endpoint(f"http://127.0.0.1/{service}", model_alias)

    def release(self, service, owner) -> None:
        self.calls.append(("release", service, owner))
        if self.fail_with is not None:
            raise self.fail_with

    def warmup(self, service) -> str:
        self.calls.append(("warmup", service))
        if self.fail_with is not None:
            raise self.fail_with
        return "ready"


def make(tmp_home: Path, client: FakeClient | None = None, **kw) -> tuple[StudioAcquirer, FakeClient, list]:
    paths = Paths.from_env()
    paths.ensure()
    client = client or FakeClient()
    ensured: list = []
    acq = StudioAcquirer(Runtime.default("cpu-dev"), paths, client=client, ensure=ensured.append, **kw)
    return acq, client, ensured


# ---------------------------------------------------------------- wiring


def test_build_services_uses_the_supervisor_acquirer_unless_a_model_service_is_faked(tmp_home: Path):
    paths = Paths.from_env()
    paths.ensure()
    rt = Runtime.default("cpu-dev")
    real = build_services(rt, fake=set(), paths=paths)
    assert isinstance(real.acquirer, StudioAcquirer)  # building it contacts nothing (lazy)
    only_clips = build_services(rt, fake={"clips"}, paths=paths)
    assert isinstance(only_clips.acquirer, StudioAcquirer)
    for faked in ("llm", "tts", "whisper", "embed"):
        assert isinstance(build_services(rt, fake={faked}, paths=paths).acquirer, DevAcquirer), faked
    assert isinstance(build_services(rt, fake=set(FAKEABLE), paths=paths).acquirer, DevAcquirer)
    mine = DevAcquirer(rt)
    assert build_services(rt, fake=set(), paths=paths, acquirer=mine).acquirer is mine


def test_wants_supervisor():
    assert wants_supervisor(set()) and wants_supervisor({"clips"})
    assert not wants_supervisor({"llm"}) and not wants_supervisor(set(FAKEABLE))


# ---------------------------------------------------------------- the acquirer


def test_first_use_starts_the_supervisor_once_then_forwards(tmp_home: Path):
    acq, client, ensured = make(tmp_home)
    assert ensured == [] and acq.client is None  # nothing happens until a service is needed
    ep = acq.acquire("llama", "studio:j1", model_alias="story")
    assert ep == Endpoint("http://127.0.0.1/llama", "story")
    acq.acquire("embed", "studio:j1")
    assert ensured == [client]  # ensure_running ran once, not per call
    acq.release("embed", "studio:j1")
    acq.release("llama", "studio:j1")
    assert [c[:3] for c in client.calls] == [
        ("acquire", "llama", "studio:j1"),
        ("acquire", "embed", "studio:j1"),
        ("release", "embed", "studio:j1"),
        ("release", "llama", "studio:j1"),
    ]
    assert acq.warmup("embed") == "ready" and client.calls[-1] == ("warmup", "embed")


def test_default_connection_uses_ns_supervisor_ensure_running(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    import ns_supervisor.client as sc

    seen: list = []
    monkeypatch.setattr(sc, "ensure_running", lambda client, paths, **kw: seen.append((client, kw)))
    paths = Paths.from_env()
    paths.ensure()
    client = FakeClient()
    acq = StudioAcquirer(Runtime.default("cpu-dev"), paths, client=client)
    acq.acquire("tts", "studio:j1")
    assert seen == [(client, {"headless": True})]


def test_supervisor_client_is_built_from_runtime_when_not_injected(tmp_home: Path, monkeypatch):
    import ns_supervisor.client as sc

    monkeypatch.setattr(sc, "ensure_running", lambda client, paths, **kw: None)
    paths = Paths.from_env()
    paths.ensure()
    rt = Runtime.default("cpu-dev").model_copy(update={"supervisor_port": 18099})
    acq = StudioAcquirer(rt, paths)
    assert acq.client is None
    acq._connect()
    assert acq.client is not None
    assert acq.client.base_url == "http://127.0.0.1:18099"  # type: ignore[attr-defined]


def test_wait_for_a_busy_gpu_is_capped_at_two_hours(tmp_home: Path):
    """J7: the nightly run waits up to 2 h for the GPU, whatever a caller asks for."""
    acq, client, _ = make(tmp_home)
    assert WAIT_CAP_S == 7200
    acq.acquire("llama", "o")
    acq.acquire("tts", "o", timeout_s=10**6)
    acq.acquire("whisper", "o", timeout_s=300)
    assert [c[4] for c in client.calls] == [7200, 7200, 300]


def test_busy_gpu_after_the_wait_raises_ns_e201(tmp_home: Path):
    busy = NsError("NS-E201", "GPU held by opencode")
    acq, _, _ = make(tmp_home, FakeClient(fail_with=busy))
    with pytest.raises(NsError) as ei:
        acq.acquire("llama", "studio:j1")
    assert ei.value.code == "NS-E201"


def test_unreachable_supervisor_is_ns_e202_and_release_never_masks_an_error(tmp_home: Path):
    paths = Paths.from_env()
    paths.ensure()

    def refuse(client) -> None:
        raise NsError("NS-E202", "supervisor did not answer")

    acq = StudioAcquirer(Runtime.default("cpu-dev"), paths, client=FakeClient(), ensure=refuse)
    with pytest.raises(NsError) as ei:
        acq.acquire("llama", "o")
    assert ei.value.code == "NS-E202"
    acq.release("llama", "o")  # never connected: nothing to release, nothing raised

    acq2, _, _ = make(tmp_home, FakeClient(fail_with=NsError("NS-E202", "gone")))
    acq2._connect()
    acq2.release("llama", "o")  # the supervisor died during the job: logged, not raised
    assert acq2.warmup("embed") == "unavailable"


# ---------------------------------------------------------------- the orchestrator really goes through it


def _stage(name: str, tenant):
    class S:
        inputs = ()
        outputs = (f"{name}.json",)

        def run(self, ctx):
            ctx.write_json(f"{name}.json", {"ok": True})

    S.name, S.tenant = name, tenant
    return S()


def test_run_job_acquires_and_releases_through_the_supervisor(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    for n, t in (("g1", "llama"), ("g2", "tts"), ("g3", "whisper")):
        register(_stage(n, t))
    monkeypatch.setitem(orch.FORMAT_STAGES, "gap1_fmt", ["g1", "g2", "g3"])
    ctx = make_ctx(tmp_home, fmt="gap1_fmt")
    acq, client, ensured = make(tmp_home)
    ctx.services.acquirer = acq
    assert orch.run_job(ctx) == ["g1", "g2", "g3"]
    owner = f"studio:{ctx.job_id}"
    assert [c[:3] for c in client.calls] == [
        ("acquire", "llama", owner), ("release", "llama", owner),
        ("acquire", "tts", owner), ("release", "tts", owner),
        ("acquire", "whisper", owner), ("release", "whisper", owner),
    ]
    assert client.calls[0][3] == "story" and len(ensured) == 1


# ---------------------------------------------------------------- the nightly run skips a night on E201

NOW = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)


def _channels(tmp_home: Path) -> None:
    from tests.test_cadence import _channel, _env

    paths, db = _env(tmp_home)
    _channel(paths, db, 9, format="shorts_only", shorts_per_day_max=3)  # three Shorts due tonight


def test_daily_stops_when_the_gpu_stayed_busy_and_reports_it(tmp_home: Path):
    _channels(tmp_home)
    env = jobs.open_env(light=True)
    calls: list[str] = []

    def busy(ctx):
        calls.append(ctx.job_id)
        raise NsError("NS-E201", "waited 7200 s")

    report = daily.run_daily(env, now=NOW, rng=random.Random(1), run_fn=busy)
    assert len(calls) == 1  # no second two-hour wait for the remaining jobs
    assert [f["code"] for f in report["failures"]] == ["NS-E201"]
    assert report["jobs"] == [] and report["skipped"] == [{"channel": "after-dark", "jobs": 3}]
    assert Path(report["backup"]).exists()  # the night still ends with its backup and report


# ---------------------------------------------------------------- gap 25: the end-of-run signal


def test_acquirer_signals_run_finished_only_once_connected(tmp_home: Path):
    acq, client, _ = make(tmp_home)
    client.run_finished = lambda: True  # type: ignore[attr-defined]
    assert acq.run_finished() is False  # this run never used the supervisor: nothing to tell it
    acq.acquire("llama", "o")
    assert acq.run_finished() is True


def test_acquirer_run_finished_swallows_a_dead_supervisor(tmp_home: Path):
    acq, client, _ = make(tmp_home)
    acq.acquire("llama", "o")

    def gone() -> bool:
        raise NsError("NS-E202", "gone")

    client.run_finished = gone  # type: ignore[attr-defined]
    assert acq.run_finished() is False


def test_daily_tells_the_supervisor_the_night_is_over(tmp_home: Path):
    _channels(tmp_home)
    env = jobs.open_env(light=True)

    class Spy(DevAcquirer):
        told = 0

        def run_finished(self) -> bool:
            Spy.told += 1
            return True

    env.services.acquirer = Spy(env.runtime)
    daily.run_daily(env, now=NOW, rng=random.Random(1), run_fn=lambda ctx: [])
    assert Spy.told == 1


# ---------------------------------------------------------------- lease heartbeat (dashboard re-review row 1)


class BeatClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.beats: list[str] = []

    def heartbeat(self, owner) -> bool:
        self.beats.append(owner)
        return True


def test_a_held_service_is_renewed_on_its_own_thread_until_it_is_released(tmp_home: Path):
    import time

    client = BeatClient()
    acq, _c, _e = make(tmp_home, client, beat_s=0.05)
    acq.acquire("llama", "studio:j1")
    deadline = time.monotonic() + 5
    while len(client.beats) < 3 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert client.beats[:3] == ["studio:j1"] * 3
    acq.release("llama", "studio:j1")
    time.sleep(0.3)  # the thread notices that nothing is held and ends
    settled = len(client.beats)
    time.sleep(0.3)
    assert len(client.beats) == settled
    assert acq._beat is None or not acq._beat.is_alive()


def test_a_client_without_heartbeat_is_fine(tmp_home: Path):
    acq, client, _e = make(tmp_home, beat_s=0.02)  # the plain FakeClient has no heartbeat
    acq.acquire("tts", "studio:j2")
    import time

    time.sleep(0.15)
    acq.release("tts", "studio:j2")
    assert ("acquire", "tts", "studio:j2", None, WAIT_CAP_S) in client.calls
