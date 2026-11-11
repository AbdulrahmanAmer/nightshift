# ruff: noqa: E501
"""Re-review row 9: the voice setup queued is created by every path that runs a job, not only `studio run`."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from ns_common.errors import NsError
from ns_common.tts_client import FakeTts, TtsClient, TtsItem
from ns_studio import voice_setup
from ns_studio.policy import regen
from ns_supervisor import notify
from tests.helpers import make_ctx

NOT_TTS = {"llm", "embed", "whisper", "clips"}  # a real run: the TTS service is a real client


def queue(home: Path) -> Path:
    f = home / "config" / "pending_voice.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"channel": "after-dark", "mode": "design", "name": "c-voice",
                             "description": "soft"}), encoding="utf-8")
    return f


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    monkeypatch.setattr(regen, "run_job", lambda ctx, resume=True: seen.append("run") or ["voice"])
    return seen


def test_run_job_guarded_creates_the_queued_voice_before_the_job(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, runs: list[str]
):
    """The dashboard's Run now and series parts call run_job_guarded and used to skip `_setup_voice`."""
    queue(tmp_home)
    ctx = make_ctx(tmp_home, fake=NOT_TTS)
    assert not isinstance(ctx.services.tts, FakeTts)

    def apply(env: Any, **kw: Any) -> voice_setup.Applied:
        runs.append("voice created")
        return voice_setup.Applied("c-voice")

    monkeypatch.setattr(voice_setup, "apply_pending_report", apply)
    assert regen.run_job_guarded(ctx) == ["voice"]
    assert runs == ["voice created", "run"]


def test_a_failure_to_create_it_is_logged_and_the_job_still_runs(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, runs: list[str]
):
    queue(tmp_home)
    ctx = make_ctx(tmp_home, fake=NOT_TTS)

    def broken(env: Any, **kw: Any) -> str:
        raise NsError("NS-E310", "no service")

    monkeypatch.setattr(voice_setup, "apply_pending_report", broken)
    assert regen.run_job_guarded(ctx) == ["voice"] and runs == ["run"]
    # and the owner of a run nobody watched reads it in the morning note, with the way to try again
    (note,) = notify.read_notes(ctx.paths)
    assert "not created yet" in note["message"] and "no service" in note["message"]
    assert "Create it now in Library > Voices" in note["message"]


def test_nothing_queued_or_a_fake_tts_does_nothing(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, runs: list[str]
):
    monkeypatch.setattr(voice_setup, "apply_pending_report", lambda env, **kw: pytest.fail("not wanted"))
    regen.run_job_guarded(make_ctx(tmp_home, fake=NOT_TTS))  # nothing queued
    queue(tmp_home)
    assert voice_setup.apply_pending_voices(make_ctx(tmp_home)) is None  # fake TTS needs no voice


def test_apply_pending_voices_builds_the_voice_through_the_service(tmp_home: Path):
    """End to end with a stand-in service: the same design call `studio run` makes."""
    f = queue(tmp_home)
    ctx = make_ctx(tmp_home, fake=NOT_TTS)
    sent: list[tuple[str, dict]] = []

    def post(base_url: str, route: str, body: dict) -> dict:
        sent.append((route, body))
        folder = ctx.paths.models_tts_voices / body["name"]
        folder.mkdir(parents=True)
        (folder / "voice.json").write_text("{}", encoding="utf-8")
        return {}

    real = voice_setup.apply_pending
    made = real(voice_setup.Env(ctx.paths, ctx.db, ctx.runtime, ctx.services, ctx.log, frozenset()), post=post)
    assert made == "c-voice" and sent[0][0] == "/v1/voices/design" and not f.exists()


def test_an_unknown_voice_is_ns_e311_with_a_create_hint_not_an_engine_failure(tmp_path: Path):
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "unknown voice 'c-voice'"})

    cli = TtsClient("http://x", client=httpx.Client(transport=httpx.MockTransport(handler)))
    for call in (
        lambda: cli.speak("hi", "c-voice", 1.0, tmp_path / "a.wav"),
        lambda: cli.speak_batch([TtsItem("a", "hi", "c-voice")], tmp_path),
    ):
        with pytest.raises(NsError) as ei:
            call()
        assert ei.value.code == "NS-E311" and "create the voice" in ei.value.detail
