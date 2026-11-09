import io
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.tts_client import TtsClient, TtsItem
from ns_tts.audio import write_wav
from ns_tts.engine import FakeEngine

pytest.importorskip("fastapi")  # fastapi is not in the CI `.[dev]` install until CR-C4-1 lands

from fastapi.testclient import TestClient  # noqa: E402

from ns_tts.server import create_app, voice_seed  # noqa: E402
from ns_tts.voices import PRESETS, VoiceLibrary  # noqa: E402

S1 = "The door was already open when I got home."
S2 = "Nothing inside had been touched at all."
DESIGN_FILE = "qwen-talker-1.7b-voicedesign-Q8_0.gguf"


def make_client(tmp_path: Path, engine: FakeEngine | None = None, *, load: bool = True) -> TestClient:
    e = engine or FakeEngine()
    if load:
        e.load()
    app = create_app(e, VoiceLibrary(tmp_path / "voices"), log=NsLogger("tts", tmp_path / "logs"))
    return TestClient(app)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return make_client(tmp_path)


def wav_info(content: bytes) -> tuple[int, int, int]:
    with wave.open(io.BytesIO(content), "rb") as w:
        return w.getframerate(), w.getnchannels(), w.getnframes()


def speak(c: TestClient, text: str, **extra: Any) -> bytes:
    body = {"model": "qwen3-tts", "input": text, "voice": "Ryan", "speed": 1.0, "response_format": "wav"}
    r = c.post("/v1/audio/speech", json={**body, **extra})
    assert r.status_code == 200, r.text
    return r.content


def test_health_reports_engine_and_lazy_load(tmp_path: Path):
    c = make_client(tmp_path, load=False)
    assert c.get("/health").json() == {
        "ok": True, "engine": "fake", "engine_kind": "fake", "model_loaded": False,
        "voice_mode": "tones", "voice": None,  # H-7: the fake says how it speaks (tone bursts here)
    }  # fmt: skip
    speak(c, "hello there friend")
    assert c.get("/health").json()["model_loaded"] is True


def test_voices_lists_presets(client: TestClient):
    body = client.get("/v1/voices").json()
    assert [v["name"] for v in body] == PRESETS
    assert body[0] == {"name": "Ryan", "mode": "preset", "source": "builtin"}


def test_speech_returns_24k_mono_16bit_wav(client: TestClient):
    r = client.post(
        "/v1/audio/speech",
        json={"model": "qwen3-tts", "input": "hello there friend", "voice": "Ryan", "speed": 1.0},
    )
    assert r.status_code == 200 and r.headers["content-type"].startswith("audio/wav")
    assert r.content[:4] == b"RIFF"
    with wave.open(io.BytesIO(r.content), "rb") as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (24000, 1, 2)
        assert w.getnframes() > 12000  # three words of tone bursts, silence trimmed


def test_pauses_between_sentences_paragraphs_and_cliffs(client: TestClient):
    _, _, joined = wav_info(speak(client, f"{S1} {S2}"))
    # Same two sentences but with a 350 ms gap vs 700 ms gap vs 1200 ms gap: compare against one chunk.
    _, _, a = wav_info(speak(client, S1))
    _, _, b = wav_info(speak(client, S2))
    assert joined == a + b + 8400
    _, _, para = wav_info(speak(client, f"{S1}\n\n{S2}"))
    assert para == a + b + 16800
    _, _, cliff = wav_info(speak(client, f"{S1} {S2}", cliff_lines=[S2]))
    assert cliff == a + b + 28800


def test_engine_gets_a_fixed_seed_per_voice(tmp_path: Path):
    seen: list[int | None] = []

    class Recorder(FakeEngine):
        def synth_batch(self, texts, voice, *, speed=1.0, seed=None):  # type: ignore[no-untyped-def]
            seen.append(seed)
            return super().synth_batch(texts, voice, speed=speed, seed=seed)

    c = make_client(tmp_path, Recorder())
    speak(c, S1)
    speak(c, S1, seed=5)
    c.post(
        "/v1/audio/speech/batch",
        json={"items": [{"id": "a", "input": S1, "voice": "Aiden", "speed": 1.0}], "out_dir": str(tmp_path)},
    )
    c.post(
        "/v1/audio/speech/batch",
        json={
            "items": [{"id": "b", "input": S1, "voice": "Aiden", "speed": 1.0}],
            "out_dir": str(tmp_path),
            "seed": 9,
        },
    )
    assert seen == [voice_seed("Ryan"), 5, voice_seed("Aiden"), 9]
    assert voice_seed("Ryan") == voice_seed("Ryan") and voice_seed("Ryan") != voice_seed("Aiden")


def test_speech_validation_errors(client: TestClient):
    base = {"model": "qwen3-tts", "input": "hello there", "voice": "Ryan", "speed": 1.0}
    assert client.post("/v1/audio/speech", json={**base, "voice": "ghost"}).status_code == 404
    assert client.post("/v1/audio/speech", json={**base, "response_format": "mp3"}).status_code == 400
    assert client.post("/v1/audio/speech", json={**base, "input": ""}).status_code == 422
    assert client.post("/v1/audio/speech", json={**base, "input": "   "}).status_code == 400
    assert client.post("/v1/audio/speech", json={**base, "speed": 9.0}).status_code == 422


def test_batch_writes_files(client: TestClient, tmp_path: Path):
    out = tmp_path / "out" / "raw"
    r = client.post(
        "/v1/audio/speech/batch",
        json={
            "items": [
                {"id": "p1", "input": f"{S1} {S2}", "voice": "Ryan", "speed": 1.0},
                {"id": "p2_hook", "input": "One two three.", "voice": "Aiden", "speed": 1.0},
            ],
            "out_dir": str(out),
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["engine"] == "fake" and isinstance(body["rtf"], float) and body["rtf"] >= 0
    assert [f["id"] for f in body["files"]] == ["p1", "p2_hook"]
    for f in body["files"]:
        p = Path(f["path"])
        assert p.parent == out and p.exists()
        sr, _, frames = wav_info(p.read_bytes())
        assert f["seconds"] == pytest.approx(frames / sr, abs=0.001)


def test_batch_rejects_bad_requests_without_writing(client: TestClient, tmp_path: Path):
    def post(items: list[dict[str, Any]]) -> int:
        return client.post(
            "/v1/audio/speech/batch", json={"items": items, "out_dir": str(tmp_path / "o")}
        ).status_code

    item = {"id": "a", "input": "one two", "voice": "Ryan", "speed": 1.0}
    assert post([{**item, "id": "../evil"}]) == 400
    assert post([item, item]) == 400
    assert post([item, {**item, "id": "b", "voice": "ghost"}]) == 404
    assert not (tmp_path / "o").exists()


def test_batch_empty_items(client: TestClient, tmp_path: Path):
    r = client.post("/v1/audio/speech/batch", json={"items": [], "out_dir": str(tmp_path / "o")})
    assert r.status_code == 200 and r.json()["files"] == [] and r.json()["rtf"] == 0.0


def test_ns_common_tts_client_talks_to_the_server(client: TestClient, tmp_path: Path):
    tts = TtsClient("http://testserver", client=client)  # TestClient is an httpx.Client
    assert tts.health() is True
    assert any(v["name"] == "Ryan" for v in tts.voices())
    secs = tts.speak("hello there friend", "Ryan", 1.0, tmp_path / "s.wav", seed=1)
    assert secs > 0.5
    files = tts.speak_batch([TtsItem("a", S1, "Ryan"), TtsItem("b", S2, "Ryan", 1.2)], tmp_path / "b")
    assert [f.id for f in files] == ["a", "b"] and all(f.path.exists() and f.seconds > 0 for f in files)


def test_design_stores_embedding_and_voice_is_usable(client: TestClient, tmp_path: Path):
    r = client.post("/v1/voices/design", json={"name": "velvet", "description": "deep calm male narrator"})
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "velvet" and Path(body["sample_path"]).name == "sample.wav"
    assert Path(body["sample_path"]).exists()
    listing = {v["name"]: v for v in client.get("/v1/voices").json()}
    assert listing["velvet"] == {"name": "velvet", "mode": "design", "source": "library"}
    emb = client.get("/v1/voices/velvet/embedding")
    assert emb.status_code == 200 and len(emb.content) == 256
    assert emb.headers["content-type"] == "application/octet-stream"
    assert emb.content == (tmp_path / "voices" / "velvet" / "speaker.bin").read_bytes()
    assert speak(client, S1, voice="velvet")[:4] == b"RIFF"
    dup = client.post("/v1/voices/design", json={"name": "velvet", "description": "again"})
    assert dup.status_code == 400


def test_design_without_model_returns_409_with_file_name(tmp_path: Path):
    class NoDesign(FakeEngine):
        def missing_model(self, op):  # type: ignore[no-untyped-def]
            return DESIGN_FILE if op == "design" else None

    c = make_client(tmp_path, NoDesign())
    r = c.post("/v1/voices/design", json={"name": "velvet", "description": "deep calm male narrator"})
    assert r.status_code == 409 and r.json()["missing_model"] == DESIGN_FILE
    assert r.json()["error"] == "NS-E314" and "Get voice design" in r.json()["detail"]
    assert not (tmp_path / "voices").exists()


def test_engine_reporting_a_missing_model_late_is_also_409(tmp_path: Path):
    class Late(FakeEngine):
        def design_to_embedding(self, description, language="English"):  # type: ignore[no-untyped-def]
            raise NsError("NS-E314", "gone", missing_model=DESIGN_FILE)

    r = make_client(tmp_path, Late()).post("/v1/voices/design", json={"name": "v", "description": "calm"})
    assert r.status_code == 409 and r.json()["missing_model"] == DESIGN_FILE
    assert r.json()["error"] == "NS-E314"


def test_clone_requires_rights(client: TestClient, tmp_path: Path):
    r = client.post(
        "/v1/voices/clone",
        json={"name": "x", "ref_audio_path": "r.wav", "ref_text": "t", "rights_confirmed": False},
    )
    assert r.status_code == 400 and "rights_confirmed" in r.json()["detail"]
    r2 = client.post("/v1/voices/clone", json={"name": "x", "ref_audio_path": "r.wav", "ref_text": "t"})
    assert r2.status_code == 400
    assert not (tmp_path / "voices").exists()


def test_clone_with_rights_creates_a_voice(client: TestClient, tmp_path: Path):
    ref = tmp_path / "ref.wav"
    write_wav(ref, (0.2 * np.sin(np.arange(24000) / 10.0)).astype(np.float32), 24000)
    body = {"name": "me", "ref_audio_path": str(ref), "ref_text": "hello there", "rights_confirmed": True}
    r = client.post("/v1/voices/clone", json=body)
    assert r.status_code == 200 and Path(r.json()["sample_path"]).exists()
    assert {"name": "me", "mode": "clone", "source": "library"} in client.get("/v1/voices").json()
    assert len(client.get("/v1/voices/me/embedding").content) == 256
    assert client.post("/v1/voices/clone", json=body).status_code == 400  # name taken
    missing = client.post("/v1/voices/clone", json={**body, "name": "you", "ref_audio_path": "nope.wav"})
    assert missing.status_code == 400


def test_embedding_endpoint_errors(client: TestClient):
    assert client.get("/v1/voices/ghost/embedding").status_code == 404
    assert client.get("/v1/voices/Ryan/embedding").status_code == 404


def test_engine_failure_is_a_coded_500(tmp_path: Path):
    class Broken(FakeEngine):
        def synth_batch(self, texts, voice, *, speed=1.0, seed=None):  # type: ignore[no-untyped-def]
            raise NsError("NS-E310", "qwen-tts exited 1")

    r = make_client(tmp_path, Broken()).post(
        "/v1/audio/speech", json={"input": "hello there", "voice": "Ryan", "model": "qwen3-tts"}
    )
    assert r.status_code == 500
    assert r.json()["error"] == "NS-E310" and "qwen-tts exited 1" in r.json()["detail"]


def test_clone_with_the_base_model_missing_answers_409_with_a_code(tmp_path: Path):
    class NoBase(FakeEngine):
        def missing_model(self, op):  # type: ignore[no-untyped-def]
            return "qwen-talker-1.7b-base-Q8_0.gguf" if op == "clone" else None

    c = make_client(tmp_path, NoBase())
    r = c.post("/v1/voices/clone", json={"name": "me", "ref_audio_path": "x.wav", "ref_text": "hi",
                                         "rights_confirmed": True})
    assert r.status_code == 409 and r.json()["error"] == "NS-E202"
    assert r.json()["missing_model"].endswith("base-Q8_0.gguf") and "cloning model" in r.json()["detail"]
