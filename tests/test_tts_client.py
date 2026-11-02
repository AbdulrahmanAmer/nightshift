import json
import wave
from pathlib import Path

import httpx
import pytest

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


def test_client_keeps_ns_e311_from_the_service(tmp_path: Path):
    """Gap 20: a voice stored by the other engine reaches the studio as NS-E311, not a generic NS-E301."""
    import pytest

    from ns_common.errors import NsError

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "NS-E311", "detail": "voice 'v' was created by ..."})

    cli = TtsClient("http://127.0.0.1:8091", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(NsError) as one:
        cli.speak("hi", "v", 1.0, tmp_path / "o.wav")
    with pytest.raises(NsError) as many:
        cli.speak_batch([TtsItem("a", "x", "v")], tmp_path)
    assert one.value.code == many.value.code == "NS-E311"

    def other(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "NS-E310", "detail": "boom"})

    cli2 = TtsClient("http://127.0.0.1:8091", client=httpx.Client(transport=httpx.MockTransport(other)))
    with pytest.raises(NsError) as generic:
        cli2.speak("hi", "v", 1.0, tmp_path / "o.wav")
    assert generic.value.code == "NS-E310"  # the service's own TTS code is kept (was a generic NS-E301)


def test_client_failures_are_tts_codes_not_llm_codes(tmp_path: Path):
    import pytest

    from ns_common.errors import NsError

    def down(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    def boom(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="oops")

    def check(cli: TtsClient) -> None:
        calls = (
            lambda: cli.speak("hi", "v", 1.0, tmp_path / "o.wav"),
            lambda: cli.speak_batch([TtsItem("a", "x", "v")], tmp_path),
            cli.voices,
        )
        for call in calls:
            with pytest.raises(NsError) as ei:
                call()
            assert ei.value.code == "NS-E310"

    for handler in (down, boom):
        transport = httpx.MockTransport(handler)
        check(TtsClient("http://127.0.0.1:8091", client=httpx.Client(transport=transport)))

    def build_failed(req: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "NS-E315", "detail": "qwen-codec exited 1"})

    transport = httpx.MockTransport(build_failed)
    cli2 = TtsClient("http://127.0.0.1:8091", client=httpx.Client(transport=transport))
    with pytest.raises(NsError) as built:
        cli2.speak("hi", "v", 1.0, tmp_path / "o.wav")
    assert built.value.code == "NS-E315"


def test_fake_tts_inserts_the_service_pauses(tmp_path: Path):
    """The fake used to be pause-free, so word budgets looked 8 % shorter than the real audio."""
    one = FakeTts().speak("w1 w2 w3 w4 w5 w6 w7 w8 w9 w10.", "Ryan", 1.0, tmp_path / "a.wav")
    two = FakeTts().speak(
        "w1 w2 w3 w4 w5 w6 w7 w8 w9 w10. w1 w2 w3 w4 w5 w6 w7 w8 w9 w10.", "Ryan", 1.0, tmp_path / "b.wav"
    )
    para = FakeTts().speak(
        "w1 w2 w3 w4 w5 w6 w7 w8 w9 w10.\n\nw1 w2 w3 w4 w5 w6 w7 w8 w9 w10.", "Ryan", 1.0, tmp_path / "c.wav"
    )
    assert two - 2 * one == pytest.approx(0.35, abs=0.02)  # 350 ms between sentences
    assert para - 2 * one == pytest.approx(0.70, abs=0.02)  # 700 ms between paragraphs
