"""H-7 / D-26: the fake voice of an engine test speaks the real text with Windows speech (SAPI).

An engine-test install (`tts.engine: fake`, real whisper) used to get tone bursts that real speech recognition
cannot align, so every story or script job stopped at captions with NS-E403. These tests state what the fake
voice is now: the text, spoken by the installed Windows voice picked the same way every time, at the pace the
request asks for, behind the same `/v1/audio/speech` contract; and what it is when this PC has no such voice.

Most tests use a stand-in for the Windows synthesizer (`StandInSapi`: speech-like audio whose length follows
the words and the SAPI rate), so they run on every runner. `test_real_windows_speech_*` use the real one and
skip, with the reason in the skip line (CI runs pytest with `-rs`), where this machine has no SAPI voice.
"""

from __future__ import annotations

import io
import json
import math
import wave
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from ns_common.errors import NsError
from ns_common.logging import NsLogger
from ns_common.models import TtsCfg
from ns_common.pacing import NOMINAL_WPM
from ns_common.paths import Paths
from ns_common.textstats import word_count
from ns_tts import sapi as sapi_mod
from ns_tts.audio import read_wav, trim_silence
from ns_tts.engine import FakeEngine, fake_speech_for, select_engine
from ns_tts.sapi import (
    PEAK_CEILING,
    RATE_MAX,
    RATE_MIN,
    RATE_STEP,
    PowerShellSapi,
    SapiUnavailable,
    SapiVoice,
    limit_peak,
    pick_voice,
    rate_for,
)

pytest.importorskip("fastapi")  # fastapi is not in every minimal install

from fastapi.testclient import TestClient  # noqa: E402

from ns_tts.server import create_app  # noqa: E402
from ns_tts.voices import VoiceLibrary  # noqa: E402

SENTENCE = (
    "The lighthouse keeper climbed the stairs every evening, counting each step as his father taught him."
)
DAVID = SapiVoice("Microsoft David Desktop", "en-US", "Male")
ZIRA = SapiVoice("Microsoft Zira Desktop", "en-US", "Female")
HORTENSE = SapiVoice("Microsoft Hortense Desktop", "fr-FR", "Female")
SR = 24000


class StandInSapi:
    """Windows speech stand-in: one voice-like buzz per word, as long as the SAPI rate makes a word."""

    def __init__(self, voices: Sequence[SapiVoice] = (ZIRA, DAVID), *, wpm0: float = 179.0) -> None:
        self.installed = list(voices)
        self.wpm0 = wpm0
        self.calls: list[dict[str, Any]] = []

    def voices(self) -> list[SapiVoice]:
        return list(self.installed)

    def speak(
        self, voice: str, lines: Sequence[str], *, rate: int, sr: int, timeout_s: float
    ) -> list[np.ndarray]:
        self.calls.append({"voice": voice, "lines": list(lines), "rate": rate, "sr": sr})
        wpm = self.wpm0 * RATE_STEP**rate
        return [self._say(line, wpm, sr) for line in lines]

    @staticmethod
    def _say(line: str, wpm: float, sr: int) -> np.ndarray:
        rng = np.random.default_rng(zlib.crc32(line.encode()))
        word_s = 60.0 / wpm
        parts = [np.zeros(int(0.15 * sr), dtype=np.float32)]  # SAPI starts with some silence
        for _ in line.split():
            n = int(word_s * sr)
            t = np.arange(n) / sr
            f0 = 110.0 + 40.0 * rng.random()
            wave_ = sum(np.sin(2 * math.pi * f0 * k * t) / k for k in range(1, 8))  # harmonics: not a tone
            noise = 0.15 * rng.standard_normal(n)
            env = np.sin(math.pi * np.arange(n) / n) ** 0.5
            parts.append((0.5 * (wave_ / 2.5 + noise) * env).astype(np.float32))
        parts.append(np.zeros(int(0.3 * sr), dtype=np.float32))
        x = np.concatenate(parts)
        return (x / np.max(np.abs(x)) * 0.97).astype(np.float32)  # SAPI voices peak near full scale


def make_app(
    tmp_path: Path, engine: FakeEngine | None = None, *, load: bool = True
) -> tuple[TestClient, FakeEngine]:
    log = NsLogger("tts", tmp_path / "logs")
    e = engine or FakeEngine(speech="sapi", sapi=StandInSapi(), log=log)
    if load:
        e.load()
    return TestClient(create_app(e, VoiceLibrary(tmp_path / "voices"), log=log)), e


def speak(c: TestClient, text: str, **extra: Any) -> tuple[np.ndarray, int]:
    body = {"model": "qwen3-tts", "input": text, "voice": "Ryan", "speed": 1.0, "response_format": "wav"}
    r = c.post("/v1/audio/speech", json={**body, **extra})
    assert r.status_code == 200, r.text
    with wave.open(io.BytesIO(r.content), "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2
        raw = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
        return raw.astype(np.float32) / 32768.0, w.getframerate()


def logged(tmp_path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for f in sorted((tmp_path / "logs").glob("*.jsonl")):
        rows += [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows


# ---------------------------------------------------------------- which voice, which rate


def test_the_same_voice_is_picked_whatever_order_windows_lists_them():
    installed = [HORTENSE, ZIRA, DAVID]
    assert pick_voice(installed) == DAVID == pick_voice(list(reversed(installed)))
    british = SapiVoice("Microsoft Hazel Desktop", "en-GB")
    assert pick_voice([HORTENSE, british]) == british  # any English before another language
    assert pick_voice([HORTENSE]) == HORTENSE  # no English voice: the first by name, not an error
    assert pick_voice([]) is None


def test_the_sapi_rate_is_the_step_closest_to_the_pace_the_request_asks_for():
    assert rate_for(1.0, NOMINAL_WPM) == 0
    assert rate_for(1.0, 179.0) == -1  # David speaks 179 wpm at rate 0: one step down lands on ~160
    faster, slower = rate_for(1.5, 179.0), rate_for(0.75, 179.0)
    assert faster > 0 > slower
    for speed in (0.5, 0.75, 1.0, 1.5, 2.0):
        got = 179.0 * RATE_STEP ** rate_for(speed, 179.0)
        assert (
            abs(math.log(got / (NOMINAL_WPM * speed))) <= math.log(RATE_STEP) / 2 + 1e-9
        )  # within half a step
    assert rate_for(2.0, 30.0) == RATE_MAX and rate_for(0.5, 900.0) == RATE_MIN  # what SAPI allows
    assert rate_for(0.0, 179.0) == 0 and rate_for(1.0, 0.0) == 0  # nonsense in, the neutral rate out


def test_peaks_are_held_below_full_scale_and_quiet_audio_is_left_alone():
    loud = np.array([0.0, 1.0, -0.99], dtype=np.float32)
    assert float(np.max(np.abs(limit_peak(loud)))) == pytest.approx(PEAK_CEILING)
    quiet = np.array([0.0, 0.3, -0.2], dtype=np.float32)
    assert limit_peak(quiet) is quiet and limit_peak(np.zeros(0, dtype=np.float32)).size == 0


# ---------------------------------------------------------------- the service with Windows speech


def test_the_fake_voice_speaks_the_text_it_is_given_at_the_service_sample_rate(tmp_path: Path):
    c, e = make_app(tmp_path)
    assert isinstance(e._sapi, StandInSapi)
    x, sr = speak(c, SENTENCE)
    stand_in = e._sapi
    assert sr == 24000 == e.sr
    spoken = [call for call in stand_in.calls if SENTENCE in call["lines"]]
    assert spoken and spoken[-1]["voice"] == DAVID.name and spoken[-1]["sr"] == 24000  # the text, not a tone
    seconds = len(trim_silence(x, sr)) / sr
    expected = word_count(SENTENCE) / NOMINAL_WPM * 60.0
    assert expected * 0.85 < seconds < expected * 1.15  # the length follows the words at the planner's pace
    assert (
        float(np.max(np.abs(x))) <= PEAK_CEILING + 1e-3
    )  # nothing near full scale reaches the loudness stage


def test_a_longer_text_is_a_longer_clip_and_each_sentence_gets_its_pause(tmp_path: Path):
    c, e = make_app(tmp_path)
    short, sr = speak(c, "Nothing inside had been touched at all.")
    two, _ = speak(c, "Nothing inside had been touched at all. The door was already open when I got home.")
    assert len(two) > 1.9 * len(short)
    # the server's own stitching still puts its sentence pause between the two utterances (350 ms)
    quiet = np.concatenate([[False], np.abs(two) < 0.01, [False]])
    edges = np.flatnonzero(np.diff(quiet.astype(np.int8)))
    longest = int(np.max(edges[1::2] - edges[::2])) if len(edges) >= 2 else 0
    assert longest >= int(0.3 * sr)


def test_the_requested_speed_moves_the_sapi_rate_and_the_clip_length(tmp_path: Path):
    c, e = make_app(tmp_path)
    stand_in: StandInSapi = e._sapi  # type: ignore[assignment]
    slow, sr = speak(c, SENTENCE, speed=0.75)
    fast, _ = speak(c, SENTENCE, speed=1.5)
    rates = [call["rate"] for call in stand_in.calls if SENTENCE in call["lines"]]
    assert rates[0] < 0 < rates[1]
    assert len(slow) > 1.6 * len(fast)  # 1.5 / 0.75 = 2: the steps of SAPI land within a step of it


def test_an_item_batch_is_one_synthesizer_run_with_a_wav_per_item(tmp_path: Path):
    c, e = make_app(tmp_path)
    stand_in: StandInSapi = e._sapi  # type: ignore[assignment]
    before = len(stand_in.calls)
    body = {
        "items": [
            {"id": "p1", "input": "The door was open. Nothing inside had been touched.", "voice": "Ryan"},
            {"id": "p1_hook", "input": "Someone was in my house.", "voice": "Ryan"},
        ],
        "out_dir": str(tmp_path / "raw"),
    }
    r = c.post("/v1/audio/speech/batch", json=body)
    assert r.status_code == 200 and [f["id"] for f in r.json()["files"]] == ["p1", "p1_hook"]
    assert len(stand_in.calls) - before == 1  # both items, one start of the synthesizer
    assert stand_in.calls[-1]["lines"] == [
        "The door was open. Nothing inside had been touched.", "Someone was in my house.",
    ]  # fmt: skip
    for name in ("p1", "p1_hook"):
        x, sr = read_wav(tmp_path / "raw" / f"{name}.wav")
        assert sr == 24000 and len(x) > 24000 // 2


def test_health_says_how_the_fake_speaks_and_with_which_voice(tmp_path: Path):
    c, e = make_app(tmp_path, load=False)
    h = c.get("/health").json()  # asking is enough: the voice is chosen before the first request
    assert h["engine_kind"] == "fake" and h["voice_mode"] == "sapi" and h["voice"] == DAVID.name
    assert h["voice_wpm"] == pytest.approx(NOMINAL_WPM, rel=0.45) and "voice_fallback" not in h
    assert h["ok"] is True


def test_designing_a_voice_stores_a_spoken_sample(tmp_path: Path):
    c, e = make_app(tmp_path)
    r = c.post("/v1/voices/design", json={"name": "velvet", "description": "deep calm narrator"})
    assert r.status_code == 200
    x, sr = read_wav(Path(r.json()["sample_path"]))
    assert sr == 24000 and len(x) / sr > 2.0  # the sample sentence, spoken
    assert speak(c, SENTENCE, voice="velvet")[1] == 24000


def test_an_unknown_voice_is_still_a_404(tmp_path: Path):
    c, _e = make_app(tmp_path)
    r = c.post(
        "/v1/audio/speech",
        json={"input": SENTENCE, "voice": "ghost", "speed": 1.0},
    )
    assert r.status_code == 404  # unknown voice: the same answer as ever


# ---------------------------------------------------------------- no Windows voice: tone bursts, said aloud


def test_no_sapi_voice_falls_back_to_tone_bursts_and_says_so_everywhere(tmp_path: Path):
    log = NsLogger("tts", tmp_path / "logs")
    e = FakeEngine(speech="sapi", sapi=StandInSapi(voices=[]), log=log)
    c, _ = make_app(tmp_path, e)
    x, sr = speak(c, SENTENCE)
    assert sr == 24000 and len(x) > sr and tonality(x) > 0.9  # today's bursts
    h = c.get("/health").json()
    assert h["voice_mode"] == "tones" and h["voice"] is None and h["voice_fallback"] is True
    assert "no Windows speech (SAPI) voice is installed" in h["voice_warning"]
    assert "tone bursts" in h["voice_warning"] and "cannot align" in h["voice_warning"]
    warns = [r for r in logged(tmp_path) if r["event"] == "fake_voice_tones_only"]
    assert len(warns) == 1 and warns[0]["level"] == "WARN" and warns[0]["msg"] == h["voice_warning"]


def test_a_pc_that_is_not_windows_falls_back_without_starting_anything(tmp_path: Path, monkeypatch):
    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("no process may start on a PC without Windows speech")

    monkeypatch.setattr(sapi_mod.subprocess, "run", boom)
    e = FakeEngine(
        speech="sapi", sapi=PowerShellSapi(platform="linux"), log=NsLogger("tts", tmp_path / "logs")
    )
    c, _ = make_app(tmp_path, e)
    h = c.get("/health").json()
    assert (
        h["voice_mode"] == "tones" and h["voice_fallback"] is True and "needs Windows" in h["voice_warning"]
    )
    assert len(speak(c, SENTENCE)[0]) > 24000


def test_a_stripped_windows_without_powershell_falls_back(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(sapi_mod.shutil, "which", lambda name: None)
    monkeypatch.setenv("SystemRoot", str(tmp_path / "no-windows-here"))
    e = FakeEngine(
        speech="sapi", sapi=PowerShellSapi(platform="win32"), log=NsLogger("tts", tmp_path / "logs")
    )
    e.load()
    d = e.describe()
    assert (
        d["voice_mode"] == "tones"
        and d["voice_fallback"] is True
        and "PowerShell was not found" in d["voice_warning"]
    )


def test_a_synthesizer_that_fails_its_start_up_sentence_falls_back(tmp_path: Path):
    class Broken(StandInSapi):
        def speak(self, voice, lines, *, rate, sr, timeout_s):  # type: ignore[no-untyped-def]
            raise NsError("NS-E310", "Windows speech (SAPI) failed (exit 1): the voice is damaged")

    e = FakeEngine(speech="sapi", sapi=Broken(), log=NsLogger("tts", tmp_path / "logs"))
    e.load()
    d = e.describe()
    assert (
        d["voice_mode"] == "tones"
        and "start-up test" in d["voice_warning"]
        and "damaged" in d["voice_warning"]
    )


def test_a_voice_that_speaks_nonsense_at_start_up_is_not_trusted(tmp_path: Path):
    class Mute(StandInSapi):
        def speak(self, voice, lines, *, rate, sr, timeout_s):  # type: ignore[no-untyped-def]
            return [np.zeros(sr // 10, dtype=np.float32) for _ in lines]

    e = FakeEngine(speech="sapi", sapi=Mute(), log=NsLogger("tts", tmp_path / "logs"))
    e.load()
    assert e.describe()["voice_mode"] == "tones"


# ---------------------------------------------------------------- the default stays as it was


def test_the_default_fake_engine_is_the_tone_burst_engine_and_starts_nothing(tmp_path: Path, monkeypatch):
    def boom(*a: Any, **k: Any) -> None:
        raise AssertionError("tone bursts need no process")

    monkeypatch.setattr(sapi_mod.subprocess, "run", boom)
    c, e = make_app(tmp_path, FakeEngine())
    assert e.speech == "tones"
    x, _sr = speak(c, SENTENCE)
    assert tonality(x) > 0.9
    assert c.get("/health").json() == {
        "ok": True, "engine": "fake", "engine_kind": "fake", "model_loaded": True,
        "voice_mode": "tones", "voice": None,
    }  # fmt: skip
    assert [r for r in logged(tmp_path) if r["event"] == "fake_voice_tones_only"] == []  # nothing was lost


def test_only_an_engine_test_asks_for_windows_speech(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NS_TTS_FAKE", raising=False)
    monkeypatch.delenv("NS_TTS_FAKE_SPEECH", raising=False)
    engine_test = TtsCfg(engine="fake")
    assert fake_speech_for(engine_test, dev_mode=False) == "sapi"
    assert fake_speech_for(engine_test, dev_mode=True) == "tones"  # a dev install keeps its tone narration
    assert fake_speech_for(TtsCfg(engine="qwentts"), dev_mode=True) == "tones"
    assert fake_speech_for(TtsCfg(engine="qwentts"), dev_mode=False) == "tones"
    monkeypatch.setenv("NS_TTS_FAKE", "1")
    assert fake_speech_for(engine_test, dev_mode=False) == "tones"  # the test suite and CI
    monkeypatch.setenv("NS_TTS_FAKE_SPEECH", "sapi")
    assert (
        fake_speech_for(TtsCfg(engine="qwentts"), dev_mode=True) == "sapi"
    )  # a developer who wants to hear it
    monkeypatch.setenv("NS_TTS_FAKE_SPEECH", "tones")
    assert fake_speech_for(engine_test, dev_mode=False) == "tones"


def test_select_engine_hands_the_speech_mode_to_the_fake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("NS_TTS_FAKE", raising=False)
    paths = Paths(tmp_path / "home")
    tones = select_engine(TtsCfg(engine="fake"), paths, "fake")
    assert isinstance(tones, FakeEngine) and tones.speech == "tones"
    spoken = select_engine(TtsCfg(engine="fake"), paths, "fake", fake_speech="sapi")
    assert isinstance(spoken, FakeEngine) and spoken.speech == "sapi"
    with pytest.raises(ValueError):
        FakeEngine(speech="whistle")


def _start_service(monkeypatch: pytest.MonkeyPatch, home: Path, *, engine: str, dev_mode: bool) -> FakeEngine:
    """Run `ns_tts.server.main()` as the supervisor starts it (`--engine fake`), without serving."""
    import sys

    import uvicorn

    from ns_common.config import save_runtime
    from ns_common.models import Runtime
    from ns_tts import server

    monkeypatch.setenv("NIGHTSHIFT_HOME", str(home))
    monkeypatch.delenv("NS_TTS_FAKE", raising=False)
    monkeypatch.delenv("NS_TTS_FAKE_SPEECH", raising=False)
    paths = Paths.from_env()
    paths.ensure()
    rt = Runtime.default("cpu-dev")
    rt.tts.engine = engine  # type: ignore[assignment]
    rt.dev_mode = dev_mode
    save_runtime(paths, rt)
    started: list[FakeEngine] = []
    monkeypatch.setattr(sys, "argv", ["ns_tts.server", "--engine", "fake"])
    monkeypatch.setattr(PowerShellSapi, "voices", lambda self: [DAVID])
    monkeypatch.setattr(
        PowerShellSapi, "speak", lambda self, voice, lines, **kw: StandInSapi().speak(voice, lines, **kw)
    )
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: started.append(app.state.engine))
    monkeypatch.setattr(
        server,
        "create_app",
        lambda eng, lib, log=None: type("A", (), {"state": type("S", (), {"engine": eng})})(),
    )
    server.main()
    return started[0]


def test_the_service_entry_point_gives_an_engine_test_the_windows_voice(tmp_path: Path, monkeypatch):
    engine = _start_service(monkeypatch, tmp_path / "engine-test", engine="fake", dev_mode=False)
    assert engine.speech == "sapi" and engine.describe()["voice"] == DAVID.name


def test_the_service_entry_point_keeps_a_dev_install_on_tone_bursts(tmp_path: Path, monkeypatch):
    engine = _start_service(monkeypatch, tmp_path / "dev", engine="qwentts", dev_mode=True)
    assert engine.speech == "tones" and engine.describe()["voice_mode"] == "tones"


# ---------------------------------------------------------------- the fake recognizer path, as today


def test_a_fake_voice_clip_is_transcribed_by_the_fake_recognizer_as_before(tmp_path: Path):
    from ns_studio.media.transcribe import FakeTranscriber

    c, _e = make_app(tmp_path)
    r = c.post(
        "/v1/audio/speech/batch",
        json={
            "items": [{"id": "p1", "input": SENTENCE, "voice": "Ryan"}],
            "out_dir": str(tmp_path / "audio"),
        },
    )
    assert r.status_code == 200
    wav = tmp_path / "audio" / "p1.wav"
    wav.with_suffix(".txt").write_text(SENTENCE, encoding="utf-8")  # the sidecar the voice stage writes
    words = FakeTranscriber().words(wav)
    assert [w.w for w in words][:3] == ["The", "lighthouse", "keeper"] and len(words) == word_count(SENTENCE)
    with wave.open(str(wav), "rb") as w:
        seconds = w.getnframes() / w.getframerate()
    assert 0 <= words[0].start < words[-1].end <= seconds + 1e-6


# ---------------------------------------------------------------- the real synthesizer of this machine


def tonality(x: np.ndarray, sr: int = 24000) -> float:
    """How much of a clip is one pure tone: 1.0 for a sine, about 0.25 for the Windows voices.

    Mean, over the 50 ms frames that carry at least half of the loudest frame's energy, of the share of the
    frame's spectral energy within three bins of its strongest bin. Speech spreads over harmonics, formants
    and noise; the tone bursts of the plain fake put nearly everything in one line.
    """
    n = int(0.05 * sr)
    frames = [np.abs(np.fft.rfft(x[i : i + n] * np.hanning(n))) ** 2 for i in range(0, len(x) - n, n)]
    if not frames:
        return 1.0
    loudest = max(float(f.sum()) for f in frames)
    shares = []
    for f in frames:
        if f.sum() >= 0.5 * loudest and f.sum() > 0:
            k = int(np.argmax(f))
            shares.append(float(f[max(0, k - 3) : k + 4].sum() / f.sum()))
    return float(np.mean(shares)) if shares else 1.0


@pytest.fixture(scope="module")
def real_sapi() -> PowerShellSapi:
    backend = PowerShellSapi()
    try:
        voices = backend.voices()
    except SapiUnavailable as e:
        pytest.skip(f"no Windows speech (SAPI) voice to test with: {e}")
    if not voices:
        pytest.skip("no Windows speech (SAPI) voice is installed on this machine")
    return backend


def test_real_windows_speech_speaks_one_sentence_that_is_not_a_tone(
    real_sapi: PowerShellSapi, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    e = FakeEngine(speech="sapi", sapi=real_sapi, log=NsLogger("tts", tmp_path / "logs"))
    c, _ = make_app(tmp_path, e)
    h = c.get("/health").json()
    assert h["voice_mode"] == "sapi" and h["voice"], h  # a voice was found and it spoke the start-up sentence
    with capsys.disabled():  # the CI log shows which voice the runner has
        print(f"\nSAPI voice of this machine: {h['voice']} ({h['voice_wpm']} words/min at rate 0)")
    x, sr = speak(c, SENTENCE)
    assert sr == 24000
    seconds = len(trim_silence(x, sr)) / sr
    expected = word_count(SENTENCE) / NOMINAL_WPM * 60.0
    assert expected * 0.6 < seconds < expected * 1.6, (
        seconds,
        expected,
    )  # the length of this text, at its pace
    assert int(np.count_nonzero(np.abs(x) > 0.01)) > sr // 2  # there is sound in it
    assert tonality(x) < 0.6, "speech, not a tone"  # David Desktop scores 0.26, the tone bursts 0.98
    tones, _ = make_app(tmp_path / "plain", FakeEngine())
    assert tonality(speak(tones, SENTENCE)[0]) > 0.9  # the measure tells the two apart
    assert float(np.max(np.abs(x))) <= PEAK_CEILING + 1e-3


def test_real_windows_speech_honours_the_requested_speed(real_sapi: PowerShellSapi, tmp_path: Path):
    e = FakeEngine(speech="sapi", sapi=real_sapi, log=NsLogger("tts", tmp_path / "logs"))
    c, _ = make_app(tmp_path, e)
    slow = len(trim_silence(speak(c, SENTENCE, speed=0.75)[0], 24000))
    normal = len(trim_silence(speak(c, SENTENCE, speed=1.0)[0], 24000))
    fast = len(trim_silence(speak(c, SENTENCE, speed=1.5)[0], 24000))
    assert slow > 1.15 * normal > 1.15 * 1.15 * fast * 0.8  # monotone, by clear steps
    assert normal > 1.2 * fast
