"""Gap L3: with no stock-footage key a story video is refused up front, before any GPU work."""

from __future__ import annotations

import random
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ns_common.errors import NsError
from ns_common.models import Runtime
from ns_common.paths import Paths
from ns_dashboard.errors import AdapterUnavailable, ApiError
from ns_dashboard.fakes import seed_channel
from ns_dashboard.handlers import Dashboard, build_state
from ns_dashboard.supervisor_link import SupervisorLink
from ns_studio import daily, jobs
from ns_studio.context import build_services
from ns_studio.footage_keys import NO_FOOTAGE_KEY, require_footage_key
from ns_studio.media.clip_sources import ClipCandidate
from ns_studio.media.clips import Searcher

NOW = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)


# ---------------------------------------------------------------- the services know whether a key exists


def test_services_say_whether_a_footage_key_exists(tmp_home: Path):
    paths = Paths.from_env()
    paths.ensure()
    rt = Runtime.default("cpu-dev")
    fake = {"llm", "tts", "whisper", "embed"}
    assert build_services(rt, fake=fake, paths=paths, keys={}).footage_key is False
    assert build_services(rt, fake=fake, paths=paths, keys={"pixabay": "k"}).footage_key is True
    assert build_services(rt, fake=fake, paths=paths, keys={"pexels": "k"}).footage_key is True
    assert build_services(rt, fake=fake | {"clips"}, paths=paths, keys={}).footage_key is True  # fake footage


def test_require_footage_key_raises_ns_e502_with_the_actionable_text(tmp_home: Path):
    paths = Paths.from_env()
    paths.ensure()
    services = build_services(Runtime.default("cpu-dev"), fake={"llm"}, paths=paths, keys={})
    with pytest.raises(NsError) as ei:
        require_footage_key(services)
    assert ei.value.code == "NS-E502"
    assert "Add a Pixabay key (free) in Settings > Stock footage keys" in str(ei.value)
    assert NO_FOOTAGE_KEY.startswith("No stock footage key.")


# ------------------------------------------------- when both sources fail, Pixabay's error is first


class FailingSource:
    def __init__(self, name: str, code: str) -> None:
        self.name = name
        self.code = code

    def search(self, keyword: str, *, orientation: str, per_page: int) -> list[ClipCandidate]:
        raise NsError(self.code, f"{self.name} API key missing")


def test_the_pixabay_error_is_reported_before_the_pexels_one():
    s = Searcher([FailingSource("pexels", "NS-E501"), FailingSource("pixabay", "NS-E502")])  # type: ignore[list-item]
    assert s.search("fog", "landscape") == []
    assert [e.code for e in s.errors] == ["NS-E501", "NS-E502"]
    first = s.first_error()
    assert first is not None and first.code == "NS-E502"
    only_pexels = Searcher([FailingSource("pexels", "NS-E501")])  # type: ignore[list-item]
    only_pexels.search("fog", "landscape")
    first = only_pexels.first_error()
    assert first is not None and first.code == "NS-E501"
    assert Searcher([]).first_error() is None


def test_a_beat_raises_the_pixabay_error_when_both_sources_fail():
    from ns_studio.media.clips import ShotSlot, select_for_beat

    s = Searcher([FailingSource("pexels", "NS-E501"), FailingSource("pixabay", "NS-E502")])  # type: ignore[list-item]
    with pytest.raises(NsError) as ei:
        select_for_beat(s, ["fog"], "eerie", [ShotSlot(0, 0.0, 2.0)], used=set(), blocked=set())
    assert ei.value.code == "NS-E502"


# ---------------------------------------------------------------- the nightly planner


def test_the_nightly_run_refuses_every_job_without_a_key_and_makes_none(tmp_home: Path):
    from tests.test_cadence import _channel, _env

    paths, db = _env(tmp_home)
    _channel(paths, db, 9, format="shorts_only", shorts_per_day_max=2)
    env = jobs.open_env(light=True)
    env.services.footage_key = False
    ran: list[str] = []
    report = daily.run_daily(
        env, now=NOW, rng=random.Random(1), run_fn=lambda ctx: ran.append(ctx.job_id) or [],
        awake=lambda: __import__("contextlib").nullcontext(),
    )
    assert ran == [] and report["jobs"] == []
    assert report["failures"] and {f["code"] for f in report["failures"]} == {"NS-E502"}
    assert all(f["job_id"] == "(not created)" for f in report["failures"])
    assert not paths.jobs_dir.exists() or not list(paths.jobs_dir.iterdir())  # no job folder, no combo drawn
    assert db.query("select * from combos") == []


# ---------------------------------------------------------------- the dashboard


class Keyring:
    def __init__(self, store: dict[str, str] | None = None, unavailable: bool = False) -> None:
        self.store = store or {}
        self.unavailable = unavailable

    def get(self, name: str) -> str | None:
        if self.unavailable:
            raise AdapterUnavailable("no keyring here")
        return self.store.get(name)

    def set(self, name: str, value: str) -> None:
        self.store[name] = value


def make_dash(tmp_home: Path, keyring: Keyring, *, fake_services: bool) -> Dashboard:
    state = build_state(
        Paths.from_env(), fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    )
    state.adapters.keyring = keyring  # type: ignore[assignment]
    state.fake_services = fake_services
    seed_channel(state.db, state.paths, "after-dark")
    return Dashboard(state)


def test_run_now_is_refused_without_a_key_and_nothing_is_queued(tmp_home: Path):
    dash = make_dash(tmp_home, Keyring(), fake_services=False)
    with pytest.raises(ApiError) as ei:
        dash.jobs.create_run({"channel": "after-dark", "format": "shorts_only"})
    assert ei.value.status == 409 and ei.value.code == "NS-E502"
    assert "Add a Pixabay key (free) in Settings > Stock footage keys" in str(ei.value)
    assert dash.state.store.next_queued() is None


@pytest.mark.parametrize("name", ["pixabay_api_key", "pexels_api_key"])
def test_run_now_is_accepted_with_either_key(tmp_home: Path, name: str):
    dash = make_dash(tmp_home, Keyring({name: "k"}), fake_services=False)
    job = dash.jobs.create_run({"channel": "after-dark", "format": "shorts_only"})
    assert job["kind"] == "run" and job["status"] == "queued"


def test_run_now_is_not_blocked_when_the_keyring_cannot_be_read_or_footage_is_fake(tmp_home: Path):
    assert make_dash(tmp_home, Keyring(unavailable=True), fake_services=False).jobs.create_run(
        {"channel": "after-dark", "format": "shorts_only"}
    )["status"] == "queued"
    assert make_dash(tmp_home, Keyring(), fake_services=True).jobs.create_run(
        {"channel": "after-dark", "format": "shorts_only"}
    )["status"] == "queued"


# ------------------------------------------- the fake_footage engine test: real models, placeholder footage


def test_runtime_fake_footage_brings_the_fake_clip_source_and_needs_no_key(tmp_home: Path):
    """docs/DEV_MODE.md "Real engines, fake voice": no dev mode, no key, no `clips` fake, and no NS-E502."""
    paths = Paths.from_env()
    paths.ensure()
    real = Runtime.default("cpu-dev")
    with pytest.raises(NsError):
        require_footage_key(build_services(real, fake=set(), paths=paths, keys={}))
    rt = real.model_copy(update={"fake_footage": True})
    services = build_services(rt, fake=set(), paths=paths, keys={})
    require_footage_key(services)                                   # passes
    assert services.footage_key is True
    assert [s.name for s in services.clip_sources] == ["fake"]
    # the real model services stay real: only the footage is placeholder
    assert type(services.llm).__name__ == "LlmClient"
    assert type(services.transcriber).__name__ != "FakeTranscriber"


def test_the_helper_reads_runtime_json_like_is_dev(tmp_home: Path):
    import json

    from ns_common.devmode import fake_footage, is_dev

    paths = Paths.from_env()
    paths.ensure()
    assert fake_footage(paths) is False                             # no runtime.json: a real install
    paths.runtime_file.write_text(json.dumps({"fake_footage": True}), encoding="utf-8")
    assert fake_footage(paths) is True and is_dev(paths) is False
    paths.runtime_file.write_text(json.dumps({"dev_mode": True}), encoding="utf-8")
    assert fake_footage(paths) is True and is_dev(paths) is True
    paths.runtime_file.write_text(json.dumps({"fake_footage": False}), encoding="utf-8")
    assert fake_footage(paths) is False


def test_the_dashboard_job_refusal_stands_down_for_fake_footage(tmp_home: Path):
    import json

    from ns_dashboard.svc_channels import ChannelsService
    from ns_dashboard.svc_jobs import JobsService

    paths = Paths.from_env()
    paths.ensure()
    state = build_state(paths, fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2))
    state.fake_services = False                                     # real adapters, empty keyring
    jobs_svc = JobsService(state, ChannelsService(state))
    paths.runtime_file.write_text(json.dumps({"fake_footage": True}), encoding="utf-8")
    jobs_svc._require_footage_key()                                 # no NS-E502
