import os
import sys
import types
from pathlib import Path

import pytest

from ns_common.errors import NsError
from ns_common.models import WhisperCfg
from ns_common.paths import Paths
from ns_common.tts_client import FakeTts, wav_seconds
from ns_studio.media import transcribe as tr
from ns_studio.media.transcribe import FakeTranscriber, WhisperTranscriber, Word, ensure_gpu_dlls_on_path


def _wav(tmp_path: Path, text: str) -> Path:
    wav = tmp_path / "p1.wav"
    FakeTts().speak(text, "Ryan", 1.0, wav)
    wav.with_suffix(".txt").write_text(text, encoding="utf-8")
    return wav


def test_fake_transcriber_spreads_words_evenly(tmp_path: Path):
    text = "The doorbell camera pings at 3:12 every night."
    wav = _wav(tmp_path, text)
    words = FakeTranscriber().words(wav)
    assert [w.w for w in words] == ["The", "doorbell", "camera", "pings", "at", "3", "12", "every", "night"]
    steps = [b.start - a.start for a, b in zip(words, words[1:], strict=False)]
    assert max(steps) - min(steps) < 1e-6
    assert words[0].start == 0.0 and words[-1].end <= wav_seconds(wav) + 0.01
    assert all(w.start < w.end for w in words)


def test_fake_transcriber_segments_follow_sentences(tmp_path: Path):
    wav = _wav(tmp_path, "One two three. Four five!")
    segs = FakeTranscriber().segments(wav)
    assert [s.text for s in segs] == ["One two three.", "Four five!"]
    assert [len(s.words) for s in segs] == [3, 2]
    assert segs[0].end <= segs[1].start


def test_fake_transcriber_needs_sidecar(tmp_path: Path):
    wav = tmp_path / "x.wav"
    FakeTts().speak("hello there", "Ryan", 1.0, wav)
    with pytest.raises(NsError) as ei:
        FakeTranscriber().words(wav)
    assert ei.value.code == "NS-E403"


def test_word_json_roundtrip():
    w = Word("night.", 1.23456, 1.9, 2)
    assert Word.from_json(w.to_json()) == Word("night.", 1.235, 1.9, 2)


def test_ensure_gpu_dlls_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PATH", "C:\\existing")
    cublas = tmp_path / "nvidia" / "cublas" / "bin"
    cudnn = tmp_path / "nvidia" / "cudnn" / "bin"
    cublas.mkdir(parents=True)
    cudnn.mkdir(parents=True)
    added = ensure_gpu_dlls_on_path([tmp_path, tmp_path / "missing"])
    assert set(added) == {cublas, cudnn}
    path = os.environ["PATH"].split(os.pathsep)
    assert str(cublas) in path and str(cudnn) in path and "C:\\existing" in path
    assert ensure_gpu_dlls_on_path([tmp_path]) == []  # idempotent


class _W:
    def __init__(self, word: str, start: float, end: float) -> None:
        self.word, self.start, self.end = word, start, end


class _Seg:
    def __init__(self, start: float, end: float, text: str, words: list[_W]) -> None:
        self.start, self.end, self.text, self.words = start, end, text, words


def _install_fake_faster_whisper(monkeypatch: pytest.MonkeyPatch) -> dict:
    record: dict = {}

    class WhisperModel:
        def __init__(self, path: str, device: str, compute_type: str) -> None:
            record["init"] = (path, device, compute_type)

        def transcribe(self, audio: str, **kw):
            record["kw"] = kw
            record["audio"] = audio
            words = [_W(" Hello", 0.0, 0.4), _W(" there.", 0.5, 1.0), _W(" ", 1.0, 1.0)]
            seg = _Seg(0.0, 1.0, " Hello there. ", words)
            return iter([seg]), object()

    mod = types.SimpleNamespace(WhisperModel=WhisperModel)
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    return record


def test_whisper_transcriber_uses_model_dir_vad_and_word_timestamps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    record = _install_fake_faster_whisper(monkeypatch)
    calls: list[int] = []
    monkeypatch.setattr(tr, "ensure_gpu_dlls_on_path", lambda *a, **k: calls.append(1) or [])
    paths = Paths(tmp_path)
    t = WhisperTranscriber(WhisperCfg(model="small.en", device="cuda", compute_type="float16"), paths)
    words = t.words(tmp_path / "a.wav", "en")
    assert [(w.w, w.start, w.end) for w in words] == [("Hello", 0.0, 0.4), ("there.", 0.5, 1.0)]
    assert record["init"] == (str(paths.models_whisper / "small.en"), "cuda", "float16")
    assert record["kw"]["word_timestamps"] is True and record["kw"]["vad_filter"] is True
    assert calls == [1]
    segs = t.segments(tmp_path / "a.wav")
    assert segs[0].text == "Hello there." and len(segs[0].words) == 2
    assert calls == [1]  # model loaded once


def test_whisper_cpu_skips_dll_injection_and_load_failure_is_coded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    calls: list[int] = []
    monkeypatch.setattr(tr, "ensure_gpu_dlls_on_path", lambda *a, **k: calls.append(1) or [])
    monkeypatch.delitem(sys.modules, "faster_whisper", raising=False)
    monkeypatch.setitem(sys.modules, "faster_whisper", None)  # import raises ImportError
    t = WhisperTranscriber(WhisperCfg(model="tiny", device="cpu", compute_type="int8"), Paths(tmp_path))
    with pytest.raises(NsError) as ei:
        t.words(tmp_path / "a.wav")
    assert ei.value.code == "NS-E202" and calls == []


def test_whisper_transcription_failure_is_ns_e408_with_the_audio_track_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Gap 19: a failure while transcribing carries NS-E408, whose hint names the audio track."""
    monkeypatch.setattr(tr, "ensure_gpu_dlls_on_path", lambda *a, **k: [])

    class WhisperModel:
        def __init__(self, path: str, device: str, compute_type: str) -> None:
            pass

        def transcribe(self, audio: str, **kw):
            raise RuntimeError("no audio stream in file")

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=WhisperModel))
    t = WhisperTranscriber(WhisperCfg(model="tiny", device="cpu", compute_type="int8"), Paths(tmp_path))
    with pytest.raises(NsError) as ei:
        t.segments(tmp_path / "talk.wav")
    assert ei.value.code == "NS-E408"
    assert "talk.wav" in ei.value.detail and "no audio stream" in ei.value.detail
    assert "audio track" in ei.value.definition.hint
