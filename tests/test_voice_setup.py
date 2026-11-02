"""Gap 5: `studio voice design|clone|apply-pending` and the voice setup queued for the first run."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from typer.testing import CliRunner

from ns_common.errors import NsError
from ns_studio import jobs, voice_setup
from ns_studio.cli import app
from ns_tts.audio import write_wav
from ns_tts.voices import VoiceLibrary

runner = CliRunner()
SR = 24000


def speechlike(seconds: float = 5.0, amp: float = 0.3) -> np.ndarray:
    """Tone bursts with silence between them: loud frames and a quiet floor, like speech."""
    t = np.arange(int(seconds * SR)) / SR
    gate = (np.mod(t, 0.45) < 0.3).astype(np.float32)
    return (amp * np.sin(2 * np.pi * 180 * t) * gate).astype(np.float32)


def recording(home: Path) -> Path:
    f = home / "me.wav"
    write_wav(f, speechlike(), SR)
    return f


class Recorder:
    """A `post` that stands in for the TTS service and writes the voice like the real one."""

    def __init__(self, paths_root: Path) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.root = paths_root

    def __call__(self, base_url: str, route: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((base_url, route, body))
        folder = self.root / body["name"]
        folder.mkdir(parents=True)
        (folder / "voice.json").write_text(json.dumps({"name": body["name"]}), encoding="utf-8")
        return {"name": body["name"]}


# ---------------------------------------------------------------- the service path


def test_design_goes_through_the_tts_tenant_and_the_design_route(tmp_home: Path):
    env = jobs.open_env(frozenset({"llm"}))  # a dev acquirer, real TTS client settings
    post = Recorder(env.paths.models_tts_voices)
    folder = voice_setup.design_voice(env, "night-voice", "warm, slow, older man", post=post)
    assert folder == env.paths.models_tts_voices / "night-voice"
    (base, route, body), = post.calls
    assert base == env.runtime.tts.base_url and route == "/v1/voices/design"
    assert body == {"name": "night-voice", "description": "warm, slow, older man", "language": "English"}
    kinds = [(e[0], e[1]) for e in env.services.acquirer.events]  # type: ignore[attr-defined]
    assert kinds == [("acquire", "tts"), ("release", "tts")]


def test_tts_is_released_when_the_service_refuses(tmp_home: Path):
    env = jobs.open_env(frozenset({"llm"}))

    def refuse(base_url: str, route: str, body: dict[str, Any]) -> dict[str, Any]:
        raise NsError("NS-E310", "model missing")

    with pytest.raises(NsError):
        voice_setup.design_voice(env, "v", "calm", post=refuse)
    assert env.services.acquirer.events[-1][0] == "release"  # type: ignore[attr-defined]


def test_clone_sends_the_checked_recording_its_words_and_the_consent(tmp_home: Path):
    env = jobs.open_env(frozenset({"llm"}))
    post = Recorder(env.paths.models_tts_voices)
    voice_setup.clone_voice(
        env, "me", recording(tmp_home), " Hello there, this is me. ", rights_confirmed=True, post=post
    )
    (_, route, body), = post.calls
    assert route == "/v1/voices/clone"
    assert body["ref_text"] == "Hello there, this is me." and body["rights_confirmed"] is True
    assert body["ref_audio_path"].endswith("me.wav")


def test_clone_needs_consent_and_a_good_recording(tmp_home: Path):
    env = jobs.open_env(frozenset({"llm"}))
    post = Recorder(env.paths.models_tts_voices)
    with pytest.raises(NsError) as e1:
        voice_setup.clone_voice(env, "me", recording(tmp_home), "hi there", rights_confirmed=False, post=post)
    assert e1.value.code == "NS-E313"
    short = tmp_home / "short.wav"
    write_wav(short, speechlike(1.0), SR)
    with pytest.raises(NsError) as e2:
        voice_setup.clone_voice(env, "me", short, "hi there", rights_confirmed=True, post=post)
    assert e2.value.code == "NS-E312"
    with pytest.raises(NsError) as e3:
        voice_setup.clone_voice(env, "me", tmp_home / "nope.wav", "hi", rights_confirmed=True, post=post)
    assert e3.value.code == "NS-E312"
    with pytest.raises(NsError) as e4:
        voice_setup.clone_voice(env, "me", recording(tmp_home), "  ", rights_confirmed=True, post=post)
    assert e4.value.code == "NS-E312" and post.calls == []


def test_bad_and_taken_names_are_refused(tmp_home: Path):
    env = jobs.open_env(frozenset({"llm"}))
    post = Recorder(env.paths.models_tts_voices)
    with pytest.raises(NsError):
        voice_setup.design_voice(env, "bad name!", "calm", post=post)
    voice_setup.design_voice(env, "ok-name", "calm", post=post)
    with pytest.raises(NsError) as ei:
        voice_setup.design_voice(env, "ok-name", "calm", post=post)
    assert "already exists" in ei.value.detail


def test_http_post_maps_a_missing_model_to_a_clear_error(monkeypatch: pytest.MonkeyPatch):
    import httpx

    def fake_post(url: str, **kw: Any) -> httpx.Response:
        req = httpx.Request("POST", url)
        return httpx.Response(409, json={"missing_model": "Qwen3-TTS-VoiceDesign"}, request=req)

    monkeypatch.setattr(httpx, "post", fake_post)
    with pytest.raises(NsError) as ei:
        voice_setup.http_post("http://127.0.0.1:8091", "/v1/voices/design", {"name": "x"})
    assert ei.value.code == "NS-E310" and "not downloaded" in ei.value.detail


# ---------------------------------------------------------------- the queue written by setup


def queue(home: Path, **req: Any) -> Path:
    f = home / "config" / "pending_voice.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(req), encoding="utf-8")
    return f


def test_apply_pending_creates_the_designed_voice_and_forgets_the_request(tmp_home: Path):
    env = jobs.open_env(frozenset({"llm"}))
    f = queue(tmp_home, channel="c", mode="design", name="c-voice", description="gentle narrator")
    post = Recorder(env.paths.models_tts_voices)
    assert voice_setup.apply_pending(env, post=post) == "c-voice"
    assert not f.exists() and post.calls[0][1] == "/v1/voices/design"
    assert voice_setup.apply_pending(env, post=post) is None  # nothing left to do


def test_apply_pending_clone_and_failure_keeps_the_request(tmp_home: Path):
    env = jobs.open_env(frozenset({"llm"}))
    f = queue(
        tmp_home, channel="c", mode="clone", name="c-voice", ref_audio_path=str(recording(tmp_home)),
        ref_text="hello there friend", rights_confirmed=True,
    )

    def down(base_url: str, route: str, body: dict[str, Any]) -> dict[str, Any]:
        raise NsError("NS-E310", "service down")

    with pytest.raises(NsError):
        voice_setup.apply_pending(env, post=down)
    assert f.exists()  # retried at the next run
    post = Recorder(env.paths.models_tts_voices)
    assert voice_setup.apply_pending(env, post=post) == "c-voice" and not f.exists()


def test_apply_pending_drops_a_request_whose_voice_already_exists(tmp_home: Path):
    env = jobs.open_env(frozenset({"llm"}))
    made = env.paths.models_tts_voices / "c-voice"
    made.mkdir(parents=True)
    (made / "voice.json").write_text("{}", encoding="utf-8")
    f = queue(tmp_home, mode="design", name="c-voice", description="x")
    assert voice_setup.apply_pending(env, post=lambda *a: pytest.fail("no call")) is None
    assert not f.exists()


def test_a_cloned_request_without_consent_is_refused(tmp_home: Path):
    env = jobs.open_env(frozenset({"llm"}))
    wav = str(recording(tmp_home))
    queue(tmp_home, mode="clone", name="c-voice", ref_audio_path=wav, ref_text="hi there")
    with pytest.raises(NsError) as ei:
        voice_setup.apply_pending(env, post=lambda *a: {})
    assert ei.value.code == "NS-E313"


# ---------------------------------------------------------------- the CLI (in-process FakeEngine)


def test_cli_voice_design_and_list(tmp_home: Path):
    args = ["voice", "design", "narrator", "--description", "calm old man", "--fake-services"]
    res = runner.invoke(app, args)
    assert res.exit_code == 0, res.output
    assert "narrator" in res.output
    lib = VoiceLibrary(tmp_home / "models" / "tts" / "voices")
    assert lib.get("narrator").mode == "design"
    again = runner.invoke(app, ["voice", "design", "narrator", "--description", "x", "--fake-services"])
    assert again.exit_code == 1 and "already exists" in again.output


def test_cli_voice_clone_needs_the_consent_flag(tmp_home: Path):
    rec = recording(tmp_home)
    base = ["voice", "clone", "mine", "--audio", str(rec), "--text", "hello there friend", "--fake-services"]
    refused = runner.invoke(app, base)
    assert refused.exit_code == 1 and "NS-E313" in refused.output
    ok = runner.invoke(app, [*base, "--rights-confirmed"])
    assert ok.exit_code == 0, ok.output
    assert VoiceLibrary(tmp_home / "models" / "tts" / "voices").get("mine").mode == "clone"


def test_cli_apply_pending(tmp_home: Path):
    nothing = runner.invoke(app, ["voice", "apply-pending", "--fake-services"])
    assert nothing.exit_code == 0 and "no voice is waiting" in nothing.output
    f = queue(tmp_home, channel="c", mode="design", name="c-voice", description="soft spoken")
    res = runner.invoke(app, ["voice", "apply-pending", "--fake-services"])
    assert res.exit_code == 0 and "c-voice created" in res.output and not f.exists()


def test_a_real_run_creates_the_queued_voice_first_and_only_warns_on_failure(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio import cli

    env = jobs.open_env(frozenset({"llm"}), light=False)
    queue(tmp_home, mode="design", name="c-voice", description="soft")
    monkeypatch.setattr(voice_setup, "apply_pending", lambda e: "c-voice")
    env.fake = frozenset()  # as in a real run
    cli._setup_voice(env)  # creates it (patched) without raising

    def broken(e: Any) -> str | None:
        raise NsError("NS-E310", "no service")

    monkeypatch.setattr(voice_setup, "apply_pending", broken)
    cli._setup_voice(env)  # a failure is a warning, the run goes on
    env.fake = frozenset({"llm"})
    monkeypatch.setattr(voice_setup, "apply_pending", lambda e: pytest.fail("fake runs skip it"))
    cli._setup_voice(env)


def test_http_post_keeps_the_code_the_service_answered_with(monkeypatch: pytest.MonkeyPatch):
    """Re-review row 14: a missing Base (clone) model said 'get voice design' under NS-E310."""
    import httpx

    def answer(body: dict[str, Any]):
        def fake_post(url: str, **kw: Any) -> httpx.Response:
            return httpx.Response(409, json=body, request=httpx.Request("POST", url))

        return fake_post

    clone = {"missing_model": "qwen-talker-1.7b-base-Q8_0.gguf", "error": "NS-E202",
             "detail": "the voice cloning model is not installed (qwen-talker-1.7b-base-Q8_0.gguf)"}
    design = {"missing_model": "voicedesign", "error": "NS-E314", "detail": "run Settings > Models > Get voice design"}
    monkeypatch.setattr(httpx, "post", answer(clone))
    with pytest.raises(NsError) as ei:
        voice_setup.http_post("http://127.0.0.1:8091", "/v1/voices/clone", {"name": "x"})
    assert ei.value.code == "NS-E202" and "cloning model" in ei.value.detail
    monkeypatch.setattr(httpx, "post", answer(design))
    with pytest.raises(NsError) as ei2:
        voice_setup.http_post("http://127.0.0.1:8091", "/v1/voices/design", {"name": "x"})
    assert ei2.value.code == "NS-E314"
