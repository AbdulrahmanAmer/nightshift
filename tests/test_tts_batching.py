"""Re-review row 8: one `qwen-tts` process per job (`--stream-by-line -o -`), never one per sentence."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pytest

from ns_common.errors import NsError
from ns_tts.audio import split_wav_stream
from ns_tts.engine import QwenttsEngine
from ns_tts.voices import VoiceLibrary
from tests.test_tts_qwentts import BASE, CUSTOM, DESIGN, RYAN, SHIM, TOKENIZER_FILE, launcher, recorded

_spec = importlib.util.spec_from_file_location("fake_qwen", SHIM)
assert _spec is not None and _spec.loader is not None
fake_qwen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fake_qwen)  # the stand-in's own header and tone helpers

SENTENCES = [
    "The door was already open when I got home.",
    "Nothing inside had been touched at all.",
    "I called out twice and nobody answered me.",
    "Then I heard the kettle start to whistle.",
    "It was only then that I saw the second cup.",
]


@pytest.fixture
def models(tmp_path: Path) -> Path:
    d = tmp_path / "models"
    d.mkdir()
    for name in (CUSTOM, BASE, DESIGN, TOKENIZER_FILE):
        (d / name).write_bytes(b"gguf")
    return d


@pytest.fixture
def calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("NS_FAKE_QWEN_LOG", str(log))
    monkeypatch.delenv("NS_FAKE_QWEN_FAIL", raising=False)
    monkeypatch.delenv("NS_FAKE_QWEN_SHORT", raising=False)
    return log


@pytest.fixture
def eng(tmp_path: Path, models: Path, calls: Path) -> QwenttsEngine:
    e = QwenttsEngine(tmp_path / "bin", models, timeout_s=60, launcher=launcher())
    e.load()
    return e


def seconds(x: np.ndarray) -> float:
    return len(x) / 24000


def test_every_chunk_of_a_batch_goes_through_one_process(eng: QwenttsEngine, calls: Path, models: Path):
    out = eng.synth_batch(SENTENCES, RYAN, seed=5)
    (call,) = recorded(calls)  # before: five processes, one per sentence
    argv = call["argv"]
    assert call["tool"] == "qwen-tts" and "--stream-by-line" in argv and argv[-2:] == ["-o", "-"]
    assert argv[argv.index("--speaker") + 1] == "ryan" and argv[argv.index("--seed") + 1] == "5"
    assert argv[argv.index("--lang") + 1] == "English"
    assert call["stdin"] == "\n".join(SENTENCES) + "\n"  # one chunk per line; the text stays off argv
    assert len(out) == len(SENTENCES)
    for text, x in zip(SENTENCES, out, strict=True):
        assert seconds(x) == pytest.approx(max(0.3, 0.2 * len(text.split())), abs=0.01)  # its own utterance


def test_a_single_chunk_keeps_the_file_route(eng: QwenttsEngine, calls: Path):
    (x,) = eng.synth_batch([SENTENCES[0]], RYAN)
    (call,) = recorded(calls)
    assert "--stream-by-line" not in call["argv"] and call["argv"][-2] == "-o" and len(x) > 0


def test_newlines_in_a_chunk_cannot_split_it_into_two_utterances(eng: QwenttsEngine, calls: Path):
    out = eng.synth_batch(["First half\nsecond half of one sentence.", "Another sentence follows here."], RYAN)
    (call,) = recorded(calls)
    assert call["stdin"].splitlines() == [
        "First half second half of one sentence.", "Another sentence follows here.",
    ]  # fmt: skip
    assert len(out) == 2


def test_a_wrong_number_of_utterances_is_ns_e310(eng: QwenttsEngine, calls: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NS_FAKE_QWEN_SHORT", "1")
    with pytest.raises(NsError) as ei:
        eng.synth_batch(SENTENCES, RYAN)
    assert ei.value.code == "NS-E310" and "4 utterances for 5 lines" in ei.value.detail


def test_a_failing_batch_process_is_ns_e310(eng: QwenttsEngine, calls: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("NS_FAKE_QWEN_FAIL", "qwen-tts")
    with pytest.raises(NsError) as ei:
        eng.synth_batch(SENTENCES, RYAN)
    assert ei.value.code == "NS-E310" and "exited 3" in ei.value.detail


def test_the_process_time_limit_grows_with_the_batch(eng: QwenttsEngine, calls: Path, monkeypatch: pytest.MonkeyPatch):
    import subprocess

    seen: list[float] = []
    real = subprocess.run

    def spy(cmd, **kw):  # type: ignore[no-untyped-def]
        seen.append(kw["timeout"])
        return real(cmd, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    eng.synth_batch(SENTENCES, RYAN)
    assert seen[0] > eng.timeout_s


# ---------------------------------------------------------------- the stream format


def _segment(seconds_: float) -> bytes:
    return fake_qwen.stream_header() + fake_qwen.tone_bytes(seconds_, 300.0)


def test_split_wav_stream_cuts_on_the_riff_headers():
    blob = b"banner text\n" + _segment(0.5) + _segment(0.25) + _segment(1.0)
    parts = split_wav_stream(blob)
    assert [round(len(x) / sr, 2) for x, sr in parts] == [0.5, 0.25, 1.0]
    assert all(sr == 24000 and x.dtype == np.float32 for x, sr in parts)
    assert split_wav_stream(b"no audio here") == []


def test_the_stand_in_streams_what_the_real_binary_documents():
    """44-byte header, both sizes 0x7FFFFFFF (qwentts.cpp `wav_stream_write_header`)."""
    head = fake_qwen.stream_header()
    assert len(head) == 44 and head[:4] == b"RIFF" and head[8:16] == b"WAVEfmt " and head[36:40] == b"data"
    assert head[4:8] == head[40:44] == b"\xff\xff\xff\x7f"


# ---------------------------------------------------------------- the service collects a whole job


def _client(tmp_path: Path, engine: QwenttsEngine):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_tts.server import create_app

    return TestClient(create_app(engine, VoiceLibrary(tmp_path / "voices")))


def test_a_batch_request_of_several_items_starts_one_process(tmp_path: Path, eng: QwenttsEngine, calls: Path):
    """A master is chapters plus hooks and closing lines: all of it in one `qwen-tts` run."""
    client = _client(tmp_path, eng)
    chapter = " ".join(SENTENCES)
    items = [
        {"id": "c1", "input": chapter, "voice": "Ryan", "speed": 1.0, "language": "en"},
        {"id": "c2", "input": " ".join(reversed(SENTENCES)), "voice": "Ryan", "speed": 1.0, "language": "en"},
        {"id": "h1", "input": "Nobody believed me at first.", "voice": "Ryan", "speed": 1.0, "language": "en"},
    ]
    r = client.post("/v1/audio/speech/batch", json={"items": items, "out_dir": str(tmp_path / "out")})
    assert r.status_code == 200, r.text
    assert len(recorded(calls)) == 1  # 11 sentences, one process
    assert recorded(calls)[0]["stdin"].count("\n") == 11
    files = {f["id"]: f["seconds"] for f in r.json()["files"]}
    assert set(files) == {"c1", "c2", "h1"} and all((tmp_path / "out" / f"{i}.wav").is_file() for i in files)
    # each item keeps its own sentences and pauses: 5 sentences + 4 x 350 ms
    speech = sum(max(0.3, 0.2 * len(s.split())) for s in SENTENCES)
    assert files["c1"] == pytest.approx(speech + 4 * 0.35, abs=0.15)


def test_items_with_another_voice_language_or_speed_get_their_own_process(
    tmp_path: Path, eng: QwenttsEngine, calls: Path
):
    client = _client(tmp_path, eng)
    items = [
        {"id": "a", "input": SENTENCES[0] + " " + SENTENCES[1], "voice": "Ryan"},
        {"id": "b", "input": SENTENCES[2] + " " + SENTENCES[3], "voice": "Ryan"},
        {"id": "c", "input": SENTENCES[2] + " " + SENTENCES[3], "voice": "Aiden"},
        {"id": "d", "input": SENTENCES[2] + " " + SENTENCES[3], "voice": "Ryan", "language": "ja"},
    ]
    r = client.post("/v1/audio/speech/batch", json={"items": items, "out_dir": str(tmp_path / "out")})
    assert r.status_code == 200, r.text
    runs = recorded(calls)
    assert len(runs) == 3  # (Ryan, English) once for a+b, Aiden once, (Ryan, Japanese) once
    langs = sorted(c["argv"][c["argv"].index("--lang") + 1] for c in runs)
    assert langs == ["English", "English", "Japanese"]
