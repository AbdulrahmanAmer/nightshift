# ruff: noqa: E501
"""Re-review row 3: the channel's language reaches `qwen-tts --lang` (it was always English)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ns_common import ffmpeg as ff
from ns_common.config import save_channel
from ns_common.languages import QWEN_LANGUAGES, qwen_language
from ns_common.paths import Paths
from ns_common.tts_client import FakeTts, TtsClient, TtsItem
from ns_studio import jobs, voice_setup
from ns_studio.media.voice import _signature, narration_items
from ns_studio.orchestrator import FORMAT_STAGES
from ns_studio.stages import REGISTRY
from ns_tts.engine import QWEN_CODEC, QWEN_TTS, FakeEngine, QwenttsEngine
from ns_tts.voices import VoiceLibrary
from tests.helpers import make_channel, make_ctx
from tests.test_tts_qwentts import BASE, CUSTOM, DESIGN, SHIM, TOKENIZER_FILE, recorded

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from ns_tts.server import create_app  # noqa: E402


@pytest.mark.parametrize(
    "code,name",
    [("en", "English"), ("ja", "Japanese"), ("zh", "Chinese"), ("ko", "Korean"), ("ru", "Russian"),
     ("es", "Spanish"), ("de", "German"), ("fr", "French"), ("it", "Italian"), ("pt", "Portuguese"),
     ("pt-BR", "Portuguese"), ("zh_CN", "Chinese"), ("JA", "Japanese"), ("japanese", "Japanese"),
     ("Korean", "Korean"), ("eng", "English"), ("", "English"), (None, "English")],
)  # fmt: skip
def test_codes_and_names_map_to_what_the_binary_accepts(code: str | None, name: str):
    assert qwen_language(code) == (name, True) and name in QWEN_LANGUAGES


@pytest.mark.parametrize("code", ["hi", "th", "ar", "xx", "tlh"])
def test_an_unsupported_language_is_english_and_flagged(code: str):
    assert qwen_language(code) == ("English", False)


def test_items_carry_the_channel_language_and_the_cache_signature_follows_it():
    parts = [{"n": 1, "text": "彼女は扉を開けた。", "hook": "扉が開いた。"}]
    items = narration_items(parts, "Ryan", 1.0, "ja")
    assert {i.language for i in items} == {"ja"}
    plain = TtsItem("a", "x", "Ryan", 1.0)
    assert _signature(plain) == _signature(TtsItem("a", "x", "Ryan", 1.0, language="en"))  # English: same cache
    assert _signature(plain) != _signature(TtsItem("a", "x", "Ryan", 1.0, language="ja"))


needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")


@needs_ffmpeg
def test_the_voice_stage_sends_the_channel_language_to_the_tts_service(tmp_home: Path):
    ctx = make_ctx(tmp_home, channel=make_channel(language="ja"))
    ctx.write_json("parts.json", [{"n": 1, "text": "彼女は扉を開けた。", "hook": "扉が開いた。"}])
    jobs.load_stage_modules()  # the stages register themselves on import: do not depend on test order or shard
    REGISTRY["voice"].run(ctx)
    tts = ctx.services.tts
    assert isinstance(tts, FakeTts) and tts.items and {i.language for i in tts.items} == {"ja"}


def test_master_voice_items_carry_the_language(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_studio.media.master import voice_items

    monkeypatch.setitem(FORMAT_STAGES, "master_first", ["voice"])
    ctx = make_ctx(tmp_home, fmt="master_first", channel=make_channel(language="ru", format="master_first"))
    ctx.write_json("story.json", {"working_title": "T", "chapters": [
        {"n": 1, "title": "A", "text": "Она открыла дверь.", "summary": "s", "word_count": 3,
         "est_seconds": 1.0, "cliff_line_idx": None, "sentences": [{"idx": 0, "text": "Она открыла дверь."}]},
    ]})
    ctx.write_json("shorts_plan.json", [
        {"k": 1, "chapter": 1, "sentence_start": 0, "sentence_end": 0, "chosen_hook": "Дверь открылась.",
         "closing_line": "Полная история на канале."},
    ])
    items = voice_items(ctx)
    assert [i.id for i in items] == ["c1", "h1", "x1"] and {i.language for i in items} == {"ru"}


def test_client_sends_the_language_in_single_and_batch_bodies(tmp_path: Path):
    import httpx

    bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        if req.url.path.endswith("/batch"):
            return httpx.Response(200, json={"files": []})
        wav = tmp_path / "s.wav"
        FakeTts().speak("hi", "Ryan", 1.0, wav)
        return httpx.Response(200, content=wav.read_bytes())

    cli = TtsClient("http://x", client=httpx.Client(transport=httpx.MockTransport(handler)))
    cli.speak_batch([TtsItem("a", "hola", "Ryan", language="es"), TtsItem("b", "hi", "Ryan")], tmp_path)
    cli.speak("hola", "Ryan", 1.0, tmp_path / "o.wav", language="es")
    assert bodies[0]["items"][0]["language"] == "es" and "language" not in bodies[0]["items"][1]
    assert bodies[1]["language"] == "es"


def _app(tmp_path: Path, engine: FakeEngine | QwenttsEngine) -> TestClient:
    engine.load()
    return TestClient(create_app(engine, VoiceLibrary(tmp_path / "voices")), base_url="http://127.0.0.1:18099")


def test_the_service_maps_the_item_language_before_the_engine_sees_it(tmp_path: Path):
    eng = FakeEngine()
    client = _app(tmp_path, eng)
    items = [
        {"id": "a", "input": "Hello there friend.", "voice": "Ryan", "language": "ja"},
        {"id": "b", "input": "Hello there friend.", "voice": "Ryan"},
        {"id": "c", "input": "Hello there friend.", "voice": "Ryan", "language": "hi"},
    ]
    r = client.post("/v1/audio/speech/batch", json={"out_dir": str(tmp_path / "o"), "items": items})
    assert r.status_code == 200, r.text
    assert eng.languages == ["Japanese", "English", "English"]  # unknown -> English (with a warning)


def test_real_qwentts_engine_gets_lang_for_the_channel_language(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    models = tmp_path / "models"
    models.mkdir()
    for n in (CUSTOM, BASE, DESIGN, TOKENIZER_FILE):
        (models / n).write_bytes(b"gguf")
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("NS_FAKE_QWEN_LOG", str(log))
    monkeypatch.delenv("NS_FAKE_QWEN_FAIL", raising=False)
    launcher = {t: [sys.executable, str(SHIM), t] for t in (QWEN_TTS, QWEN_CODEC)}
    eng = QwenttsEngine(tmp_path / "bin", models, timeout_s=60, launcher=launcher)
    client = _app(tmp_path, eng)
    r = client.post("/v1/audio/speech", json={"input": "こんにちは。", "voice": "Ryan", "language": "ja"})
    assert r.status_code == 200, r.text
    langs = {c["argv"][c["argv"].index("--lang") + 1] for c in recorded(log)}
    assert langs == {"Japanese"}
    refused = subprocess.run(  # the stand-in refuses a name the real binary refuses
        [sys.executable, str(SHIM), "qwen-tts", "--model", str(models / CUSTOM), "--codec",
         str(models / TOKENIZER_FILE), "--speaker", "ryan", "--lang", "ja", "-o", str(tmp_path / "x.wav")],
        input=b"hello there", capture_output=True, check=False, timeout=60,
    )  # fmt: skip
    assert refused.returncode == 1


def test_a_designed_voice_is_made_in_the_requested_language(tmp_path: Path):
    client = _app(tmp_path, FakeEngine())
    r = client.post("/v1/voices/design", json={"name": "v1", "description": "calm", "language": "ru"})
    assert r.status_code == 200, r.text
    meta = json.loads((tmp_path / "voices" / "v1" / "voice.json").read_text(encoding="utf-8"))
    assert meta["language"] == "Russian"


def test_pending_voice_takes_the_language_of_its_channel(tmp_home: Path):
    save_channel(Paths.from_env(), make_channel(slug="kyoto", language="ja"))
    env = jobs.open_env(frozenset({"llm"}))
    f = tmp_home / "config" / "pending_voice.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(
        json.dumps({"channel": "kyoto", "mode": "design", "name": "k-voice", "description": "calm"}),
        encoding="utf-8",
    )
    sent: list[dict] = []

    def post(base_url: str, route: str, body: dict) -> dict:
        sent.append(body)
        folder = env.paths.models_tts_voices / body["name"]
        folder.mkdir(parents=True)
        (folder / "voice.json").write_text("{}", encoding="utf-8")
        return {}

    assert voice_setup.apply_pending(env, post=post) == "k-voice"
    assert sent[0]["language"] == "ja"
