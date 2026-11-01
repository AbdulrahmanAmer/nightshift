import json
import wave
from pathlib import Path

import httpx

from ns_common.tts_client import FakeTts, TtsClient, TtsItem, wav_seconds


def test_fake_tts_duration_tracks_words(tmp_path: Path):
    fake = FakeTts(wpm=150)
    secs = fake.speak("one two three four five six seven eight nine ten", "Ryan", 1.0, tmp_path / "a.wav")
    assert abs(secs - 4.0) < 0.01
    with wave.open(str(tmp_path / "a.wav"), "rb") as w:
        assert w.getframerate() == 24000 and w.getnchannels() == 1
    assert abs(wav_seconds(tmp_path / "a.wav") - 4.0) < 0.01


def test_fake_batch(tmp_path: Path):
    items = [TtsItem("s1", "hello there", "Ryan"), TtsItem("s2", "bye", "Ryan")]
    files = FakeTts().speak_batch(items, tmp_path)
    assert [f.id for f in files] == ["s1", "s2"]
    assert all(f.path.exists() and f.seconds >= 0.4 for f in files)


def test_client_speak_writes_wav(tmp_path: Path):
    wav_bytes = tmp_path / "src.wav"
    FakeTts().speak("hi", "Ryan", 1.0, wav_bytes)

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["model"] == "qwen3-tts" and body["response_format"] == "wav" and body["voice"] == "Ryan"
        return httpx.Response(200, content=wav_bytes.read_bytes(), headers={"content-type": "audio/wav"})

    cli = TtsClient("http://127.0.0.1:8091", client=httpx.Client(transport=httpx.MockTransport(handler)))
    secs = cli.speak("hi", "Ryan", 1.0, tmp_path / "out.wav")
    assert (tmp_path / "out.wav").exists() and secs > 0


def test_client_batch_and_voices(tmp_path: Path):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/batch"):
            files = [{"id": "a", "path": str(tmp_path / "a.wav"), "seconds": 1.5}]
            return httpx.Response(200, json={"files": files, "engine": "faster", "rtf": 0.3})
        return httpx.Response(200, json=[{"name": "Ryan", "mode": "preset", "source": "builtin"}])

    cli = TtsClient("http://127.0.0.1:8091", client=httpx.Client(transport=httpx.MockTransport(handler)))
    out = cli.speak_batch([TtsItem("a", "x", "Ryan")], tmp_path)
    assert out[0].seconds == 1.5 and out[0].path == tmp_path / "a.wav"
    assert cli.voices()[0]["name"] == "Ryan"


def test_cliff_lines_are_sent_per_item_only_when_set(tmp_path: Path):
    bodies: list[dict] = []

    def handler(req: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"files": []})

    cli = TtsClient("http://127.0.0.1:8091", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert TtsItem("a", "x", "Ryan", 1.1).cliff_lines == ()
    with_cliff = TtsItem("a", "x", "Ryan", 1.0, ("The door opened.",))
    cli.speak_batch([with_cliff, TtsItem("b", "y", "Ryan")], tmp_path)
    first, second = bodies[0]["items"]
    assert first["cliff_lines"] == ["The door opened."]
    assert "cliff_lines" not in second
