"""Error codes of the voice engine path (review row 13) and the shared fallback model folder (row 4)."""

import io
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest

from ns_common.errors import NsError, describe
from ns_common.logging import NsLogger
from ns_common.paths import TTS_HF_SUBDIR, Paths
from ns_common.tts_client import TtsClient
from ns_tts.engine import FakeEngine
from ns_tts.voices import VoiceLibrary

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from ns_tts.server import create_app  # noqa: E402


def make_client(tmp_path: Path, engine: FakeEngine) -> TestClient:
    engine.load()
    app = create_app(engine, VoiceLibrary(tmp_path / "voices"), log=NsLogger("tts", tmp_path / "l"))
    return TestClient(app, base_url="http://127.0.0.1:18099")


def test_the_tts_codes_exist_and_say_what_they_mean():
    assert "synthesize" in describe("NS-E310").message
    assert "voice" in describe("NS-E315").message.lower()
    assert "embedding" in describe("NS-E316").message.lower()
    assert "language model" in describe("NS-E301").message  # 301 stays the LLM's code


def test_synthesis_failure_reaches_the_client_as_ns_e310(tmp_path: Path):
    class Failing(FakeEngine):
        def synth_batch(self, texts, voice, *, speed=1.0, seed=None):  # type: ignore[no-untyped-def]
            raise NsError("NS-E310", "qwen-tts exited 3: CUDA error: out of memory")

    c = make_client(tmp_path, Failing())
    r = c.post("/v1/audio/speech", json={"input": "hello there", "voice": "Ryan"})
    assert r.status_code == 500 and r.json()["error"] == "NS-E310" and "out of memory" in r.json()["detail"]
    cli = TtsClient("http://127.0.0.1:18099", client=c)  # type: ignore[arg-type]
    with pytest.raises(NsError) as ei:
        cli.speak("hello there", "Ryan", 1.0, tmp_path / "o.wav")
    assert ei.value.code == "NS-E310"


def test_voice_build_failure_reaches_the_client_as_ns_e314(tmp_path: Path):
    class Failing(FakeEngine):
        def design_to_embedding(self, description, language="English"):  # type: ignore[no-untyped-def]
            raise NsError("NS-E315", "qwen-codec exited 1")

    r = make_client(tmp_path, Failing()).post("/v1/voices/design", json={"name": "v", "description": "calm"})
    assert r.status_code == 500 and r.json()["error"] == "NS-E315"


def test_a_voice_of_the_other_engine_is_ns_e311_over_http(tmp_path: Path):
    folder = tmp_path / "voices" / "old"
    folder.mkdir(parents=True)
    (folder / "voice.json").write_text(json.dumps({"name": "old", "mode": "clone"}), encoding="utf-8")
    # no speaker file at all: the fake engine reports a missing embedding as NS-E311, never NS-E301
    c = make_client(tmp_path, FakeEngine())
    r = c.post("/v1/audio/speech", json={"input": "hello there", "voice": "old"})
    assert r.status_code == 500 and r.json()["error"] == "NS-E311"


def test_voices_with_several_files_download_as_a_zip(tmp_path: Path):
    class Multi(FakeEngine):
        def design_to_embedding(self, description, language="English"):  # type: ignore[no-untyped-def]
            sample: np.ndarray = self._tone("one two three", 220.0, 1.0)
            return {"speaker.spk": b"\x01" * 8, "speaker.rvq": b"RVQ", "ref.txt": b"hi\n"}, sample

    c = make_client(tmp_path, Multi())
    assert c.post("/v1/voices/design", json={"name": "velvet", "description": "calm"}).status_code == 200
    r = c.get("/v1/voices/velvet/embedding")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "velvet.voice.zip" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        assert sorted(z.namelist()) == ["ref.txt", "speaker.rvq", "speaker.spk"]


def test_the_fallback_model_folder_is_one_constant_in_ns_common(tmp_path: Path):
    paths = Paths(tmp_path)
    assert TTS_HF_SUBDIR == "hf"
    assert paths.models_tts_hf == paths.models_tts / TTS_HF_SUBDIR == tmp_path / "models" / "tts" / "hf"


def test_missing_voice_design_model_is_ns_e314_with_the_settings_hint(tmp_path: Path):
    from ns_tts.engine import ReferenceEngine

    assert "Get voice design" in describe("NS-E314").hint
    e = ReferenceEngine(tmp_path, device="cuda")
    with pytest.raises(NsError) as ei:
        e.design_to_embedding("calm voice")
    assert ei.value.code == "NS-E314" and "Settings > Models > Get voice design" in ei.value.detail
    assert ei.value.data["missing_model"].endswith("VoiceDesign")


def test_health_exposes_engine_kind_for_ns_verify(tmp_path: Path):
    from ns_tts.engine import ReferenceEngine

    ref = TestClient(
        create_app(ReferenceEngine(tmp_path, device="cuda"), VoiceLibrary(tmp_path / "v")),
        base_url="http://127.0.0.1:18099",
    )
    body = ref.get("/health").json()
    assert body["engine_kind"] == "reference"  # ns verify turns this into NS-W301
    assert body["model_loaded"] is False
    assert make_client(tmp_path, FakeEngine()).get("/health").json()["engine_kind"] == "fake"
