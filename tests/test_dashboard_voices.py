# ruff: noqa: E501
"""Voice design and clone (CR-C3-UI-7): service, routes, sample checks, the real adapter and the Assistant."""

import json
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

from ns_common import ffmpeg
from ns_common.acquire import Endpoint
from ns_common.errors import NsError
from ns_common.models import VoiceMakeResponse
from ns_common.paths import Paths
from ns_dashboard.adapters import RealVoiceMaker
from ns_dashboard.assistant.llm import ScriptedToolLlm, StaticProvider, call, say
from ns_dashboard.assistant.tools import build_registry
from ns_dashboard.errors import ApiError
from ns_dashboard.fakes import FakeVoiceMaker
from ns_dashboard.handlers import Dashboard, build_state
from ns_dashboard.supervisor_link import SupervisorLink
from ns_tts import sample
from ns_tts.audio import wav_bytes, write_wav
from ns_tts.voices import VoiceLibrary

pytest.importorskip("fastapi")
from tests.test_dashboard_api import make_env  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not ffmpeg.available(), reason="FFmpeg not installed")
SR = 24000


def speechlike(seconds: float, amp: float = 0.3) -> np.ndarray:
    """Tone bursts with silence between them: loud frames and a quiet floor, like speech."""
    t = np.arange(int(seconds * SR)) / SR
    gate = (np.mod(t, 0.45) < 0.3).astype(np.float32)
    return (amp * np.sin(2 * np.pi * 180 * t) * gate).astype(np.float32)


def wav(path: Path, x: np.ndarray) -> Path:
    write_wav(path, x, SR)
    return path


def good_sample(tmp_home: Path, seconds: float = 6.0) -> Path:
    return wav(tmp_home / "me.wav", speechlike(seconds))


def body_clone(path: Path, **kw: Any) -> dict[str, Any]:
    return {"name": "Mine", "sample_path": str(path), "consent": True, "test_line": "Hello there.", **kw}


BODY_DESIGN = {"name": "Narrator1", "description": "A warm, slow baritone", "test_line": "Once upon a midnight."}


def maker_of(state: Any) -> FakeVoiceMaker:
    assert isinstance(state.adapters.voice_maker, FakeVoiceMaker)
    return state.adapters.voice_maker


# ------------------------------------------------------------------ sample checks (ns_tts.sample)


def test_sample_report_of_speechlike_audio_is_clean():
    r = sample.analyse(speechlike(6.0), SR)
    assert r.seconds == pytest.approx(6.0) and sample.verdict(r) is None
    assert r.snr_db > 40 and r.level_dbfs > -30


@pytest.mark.parametrize(
    ("x", "word"),
    [
        (speechlike(1.5), "long"),
        (speechlike(6.0, amp=0.0005), "quiet"),
        (np.zeros(6 * SR, dtype=np.float32), "quiet"),
        (0.1 * np.random.default_rng(1).standard_normal(6 * SR).astype(np.float32), "noise"),
        (np.sign(speechlike(6.0)).astype(np.float32), "distorted"),
    ],
    ids=["short", "quiet", "silent", "noisy", "clipped"],
)
def test_prepare_sample_rejects_bad_recordings_with_ns_e312(tmp_path: Path, x: np.ndarray, word: str):
    p = wav(tmp_path / "s.wav", x)
    with pytest.raises(NsError) as ei:
        sample.prepare_sample(p, tmp_path / "work")
    assert ei.value.code == "NS-E312" and word in str(ei.value)


def test_prepare_sample_uses_a_good_wav_as_is(tmp_path: Path):
    p = wav(tmp_path / "s.wav", speechlike(5.0))
    out, rep = sample.prepare_sample(p, tmp_path / "work")
    assert out == p and rep.seconds == pytest.approx(5.0)


@needs_ffmpeg
def test_prepare_sample_cuts_long_recordings_and_rejects_non_audio(tmp_path: Path):
    p = wav(tmp_path / "long.wav", speechlike(40.0))
    out, rep = sample.prepare_sample(p, tmp_path / "work")
    assert out != p and rep.seconds == pytest.approx(sample.MAX_SECONDS, abs=0.2)
    junk = tmp_path / "notes.mp3"
    junk.write_bytes(b"this is not audio at all")
    with pytest.raises(NsError) as ei:
        sample.prepare_sample(junk, tmp_path / "work2")
    assert ei.value.code == "NS-E312"


# ------------------------------------------------------------------ routes with the fake voice maker


def test_design_route_creates_a_voice_with_a_preview(tmp_home: Path):
    client, state = make_env(tmp_home)
    r = client.post("/api/voices/design", json=BODY_DESIGN)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"voice_id", "preview_wav_path"} and VoiceMakeResponse.model_validate(body)
    folder = state.paths.models_tts_voices / "Narrator1"
    assert body["voice_id"] == "Narrator1" and Path(body["preview_wav_path"]) == folder / "preview.wav"
    assert {f.name for f in folder.iterdir()} >= {"voice.json", "speaker.bin", "sample.wav", "preview.wav"}
    assert json.loads((folder / "voice.json").read_text(encoding="utf-8"))["mode"] == "design"
    assert (folder / "preview.wav").read_bytes()[:4] == b"RIFF"
    assert VoiceLibrary(state.paths.models_tts_voices).get("Narrator1").mode == "design"
    assert any(v["name"] == "Narrator1" for v in client.get("/api/voices").json())
    assert maker_of(state).calls == [("design", "Narrator1", "Once upon a midnight.")]


@pytest.mark.parametrize(
    ("patch", "status", "code"),
    [
        ({"name": "bad name!"}, 422, "invalid_voice"),
        ({"name": ""}, 422, "invalid_voice"),
        ({"name": "Ryan"}, 409, "voice_exists"),
        ({"name": "ryan"}, 409, "voice_exists"),
        ({"description": "  "}, 422, "invalid_voice"),
        ({"test_line": ""}, 422, "invalid_voice"),
        ({"test_line": "x" * 301}, 422, "invalid_voice"),
    ],
)
def test_design_validation(tmp_home: Path, patch: dict[str, Any], status: int, code: str):
    client, state = make_env(tmp_home)
    r = client.post("/api/voices/design", json={**BODY_DESIGN, **patch})
    assert r.status_code == status and r.json()["error"]["code"] == code
    assert maker_of(state).calls == []


def test_design_twice_with_one_name_is_a_conflict(tmp_home: Path):
    client, _ = make_env(tmp_home)
    assert client.post("/api/voices/design", json=BODY_DESIGN).status_code == 200
    r = client.post("/api/voices/design", json=BODY_DESIGN)
    assert r.status_code == 409 and r.json()["error"]["code"] == "voice_exists"


def test_clone_route_creates_a_voice(tmp_home: Path):
    client, state = make_env(tmp_home)
    r = client.post("/api/voices/clone", json=body_clone(good_sample(tmp_home)))
    assert r.status_code == 200
    body = r.json()
    assert VoiceMakeResponse.model_validate(body).voice_id == "Mine"
    folder = state.paths.models_tts_voices / "Mine"
    assert json.loads((folder / "voice.json").read_text(encoding="utf-8"))["mode"] == "clone"
    assert (folder / "preview.wav").is_file() and (folder / "speaker.bin").stat().st_size > 0


@pytest.mark.parametrize("consent", [False, None, "true", 1])
def test_clone_needs_consent_true(tmp_home: Path, consent: Any):
    client, state = make_env(tmp_home)
    payload = body_clone(good_sample(tmp_home), consent=consent)
    if consent is None:
        payload.pop("consent")
    r = client.post("/api/voices/clone", json=payload)
    assert r.status_code == 422 and r.json()["error"]["code"] == "consent_required"
    assert maker_of(state).calls == []
    assert not (state.paths.models_tts_voices / "Mine").exists()


def test_clone_sample_too_short_is_422_ns_e312(tmp_home: Path):
    client, state = make_env(tmp_home)
    p = wav(tmp_home / "short.wav", speechlike(1.0))
    r = client.post("/api/voices/clone", json=body_clone(p))
    err = r.json()["error"]
    assert r.status_code == 422 and err["code"] == "NS-E312" and "Voice sample too short or noisy" in err["message"]
    assert err["hint"] and "clean sample" in err["hint"]
    assert maker_of(state).calls == []


def test_clone_noisy_sample_is_422_ns_e312(tmp_home: Path):
    client, _ = make_env(tmp_home)
    p = wav(tmp_home / "noisy.wav", 0.1 * np.random.default_rng(2).standard_normal(6 * SR).astype(np.float32))
    r = client.post("/api/voices/clone", json=body_clone(p))
    assert r.status_code == 422 and r.json()["error"]["code"] == "NS-E312"


def test_clone_missing_sample_or_name_problems(tmp_home: Path):
    client, _ = make_env(tmp_home)
    gone = client.post("/api/voices/clone", json=body_clone(tmp_home / "nope.wav"))
    assert gone.status_code == 400 and gone.json()["error"]["code"] == "bad_path"
    none = client.post("/api/voices/clone", json={"name": "Mine", "consent": True, "test_line": "Hi."})
    assert none.status_code == 400 and none.json()["error"]["code"] == "no_sample"
    dup = client.post("/api/voices/clone", json=body_clone(good_sample(tmp_home), name="Aiden"))
    assert dup.status_code == 409


def test_new_routes_refuse_while_shutting_down(tmp_home: Path):
    client, state = make_env(tmp_home)
    state.draining = True
    assert client.post("/api/voices/design", json=BODY_DESIGN).status_code == 503


def test_ns_e312_from_the_engine_is_reported_as_422(tmp_home: Path):
    client, state = make_env(tmp_home)
    maker_of(state).fail_with = NsError("NS-E312", "no speech could be found")
    r = client.post("/api/voices/clone", json=body_clone(good_sample(tmp_home)))
    assert r.status_code == 422 and r.json()["error"]["code"] == "NS-E312"


# ------------------------------------------------------------------ the real adapter against a mocked TTS service


class RecordingAcquirer:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def acquire(self, service: str, owner: str, *, model_alias: str | None = None, timeout_s: int = 7200) -> Endpoint:
        self.events.append(("acquire", service))
        return Endpoint("http://tts.test")

    def release(self, service: str, owner: str) -> None:
        self.events.append(("release", service))


def tts_service(posts: list[tuple[str, dict[str, Any]]], voice_status: int = 200, detail: str = "") -> httpx.Client:
    audio = wav_bytes(speechlike(1.0), SR)

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content or b"{}")
        posts.append((req.url.path, body))
        if req.url.path.startswith("/v1/voices/"):
            if voice_status == 409:
                return httpx.Response(409, json={"missing_model": "qwen-talker-1.7b-voicedesign-Q8_0.gguf"})
            if voice_status == 400:
                return httpx.Response(400, json={"detail": detail})
            if voice_status >= 500:
                return httpx.Response(voice_status, json={"error": "NS-E310", "detail": "boom"})
            return httpx.Response(200, json={"name": body["name"], "sample_path": "x"})
        return httpx.Response(200, content=audio, headers={"content-type": "audio/wav"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def real(http: httpx.Client, words: str = "this is what I said") -> tuple[RealVoiceMaker, RecordingAcquirer]:
    acq = RecordingAcquirer()
    maker = RealVoiceMaker(
        Paths.from_env(),
        SupervisorLink("http://127.0.0.1:9", timeout_s=0.2),
        acquirer=acq,  # type: ignore[arg-type]
        http=http,
        transcribe=lambda _p: words,
    )
    return maker, acq


def test_real_design_posts_then_speaks_the_test_line_and_releases_tts(tmp_home: Path):
    posts: list[tuple[str, dict[str, Any]]] = []
    maker, acq = real(tts_service(posts))
    made = maker.design("Deep", "a deep voice", "Good evening.")
    assert posts[0] == ("/v1/voices/design", {"name": "Deep", "description": "a deep voice"})
    assert posts[1][0] == "/v1/audio/speech" and posts[1][1]["input"] == "Good evening." and posts[1][1]["voice"] == "Deep"
    assert made.voice_id == "Deep"
    assert Path(made.preview_wav_path) == Paths.from_env().models_tts_voices / "Deep" / "preview.wav"
    assert Path(made.preview_wav_path).read_bytes()[:4] == b"RIFF"
    assert acq.events == [("acquire", "tts"), ("release", "tts")]


def test_real_clone_sends_the_transcript_and_the_rights_flag(tmp_home: Path):
    posts: list[tuple[str, dict[str, Any]]] = []
    maker, acq = real(tts_service(posts))
    src = good_sample(tmp_home)
    made = maker.clone("Mine", src, "Welcome back.")
    path, body = posts[0]
    assert path == "/v1/voices/clone" and body["ref_text"] == "this is what I said"
    assert body["ref_audio_path"] == str(src) and body["rights_confirmed"] is True
    assert made.voice_id == "Mine" and acq.events == [("acquire", "tts"), ("release", "tts")]


def test_real_clone_without_speech_in_the_sample_is_ns_e312(tmp_home: Path):
    maker, acq = real(tts_service([]), words="  ")
    with pytest.raises(NsError) as ei:
        maker.clone("Mine", good_sample(tmp_home), "Hi.")
    assert ei.value.code == "NS-E312" and acq.events == []


def test_real_missing_model_rejection_and_service_errors_release_the_gpu(tmp_home: Path):
    maker, acq = real(tts_service([], voice_status=409))
    with pytest.raises(ApiError) as ei:
        maker.design("Deep", "a deep voice", "Hi there.")
    assert ei.value.status == 409 and ei.value.code == "model_missing" and ei.value.hint
    assert acq.events[-1] == ("release", "tts")
    maker, _ = real(tts_service([], voice_status=400, detail="a voice named 'Deep' already exists"))
    with pytest.raises(ApiError) as ei2:
        maker.design("Deep", "a deep voice", "Hi there.")
    assert ei2.value.status == 400 and "already exists" in ei2.value.message
    maker, _ = real(tts_service([], voice_status=500))
    with pytest.raises(NsError) as ei3:
        maker.design("Deep", "a deep voice", "Hi there.")
    assert ei3.value.code == "NS-E310"


# ------------------------------------------------------------------ the Assistant picks the routes up


def assistant(tmp_home: Path, llm: ScriptedToolLlm) -> tuple[Dashboard, Any]:
    state = build_state(Paths.from_env(), fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2))
    return Dashboard(state, llm_provider=StaticProvider(llm)), state


def test_registry_has_both_voice_tools_with_confirm_cards():
    reg = build_registry()
    routes = reg.routes()
    assert routes[("POST", "/api/voices/design")] == "design_voice"
    assert routes[("POST", "/api/voices/clone")] == "clone_voice"
    for name in ("design_voice", "clone_voice"):
        spec = reg.get(name).spec  # type: ignore[union-attr]
        assert spec.side_effect and spec.confirm_text and spec.topic == "library"
    clone_args = reg.get("clone_voice").spec.args_schema["properties"]  # type: ignore[union-attr]
    assert "consent" not in clone_args  # the owner's Yes is the consent


def test_assistant_designs_a_voice_only_after_the_owner_confirms(tmp_home: Path):
    dash, state = assistant(tmp_home, ScriptedToolLlm([call("design_voice", BODY_DESIGN), say("Waiting for your OK.")]))
    events = list(dash.assistant.chat("make me a warm baritone narrator"))
    card = next(e["payload"] for e in events if e["kind"] == "confirm")
    assert "Narrator1" in card["text"] and "warm, slow baritone" in card["text"]
    assert not (state.paths.models_tts_voices / "Narrator1").exists()
    out = dash.assistant.confirm(card["action_id"])
    assert out["status"] == "done" and out["result"]["voice_id"] == "Narrator1"
    assert (state.paths.models_tts_voices / "Narrator1" / "preview.wav").is_file()


def test_assistant_clone_card_states_the_rights_and_passes_consent(tmp_home: Path):
    src = good_sample(tmp_home)
    llm = ScriptedToolLlm([
        call("clone_voice", {"name": "Mine", "sample_path": str(src), "test_line": "Hello there."}),
        say("Please confirm."),
    ])
    dash, state = assistant(tmp_home, llm)
    card = next(e["payload"] for e in dash.assistant.chat("clone my voice") if e["kind"] == "confirm")
    assert "right to clone" in card["text"] and str(src) in card["text"]
    assert maker_of(state).calls == []
    assert dash.assistant.confirm(card["action_id"])["status"] == "done"
    assert maker_of(state).calls[0][:2] == ("clone", "Mine")
