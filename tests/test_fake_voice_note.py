"""H-7 / D-26: on an engine test the job says how the fake voice spoke, and a captions failure names the fake.

The voice service reports `voice_mode` ("sapi": the real text in a Windows voice; "tones": bursts, as this PC
has no such voice) in `/health`. The voice stage keeps that in `audio/voice_engine.json`, logs a warning for
tone bursts, and the NS-E403 of the captions stage ends with one sentence that names the fake voice, so nobody
reads a caption failure on the engine test as a product defect.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.tts_client import TtsClient
from ns_studio.context import JobContext
from ns_studio.media.audio import VOICE_LUFS, linear_possible, measure_lufs
from ns_studio.media.captions import CaptionsStage
from ns_studio.media.fake_voice import (
    VOICE_ENGINE_FILE,
    captions_note,
    e403_detail,
    fetch_health,
    is_fake_voice,
    note_voice_engine,
)
from ns_studio.media.transcribe import FakeTranscriber
from ns_studio.stages import REGISTRY
from ns_tts.engine import FakeEngine
from ns_tts.voices import VoiceLibrary
from tests.helpers import make_ctx
from tests.test_captions import SCRIPT, _prepare_audio, _ShuffledTranscriber
from tests.test_tts_fake_sapi import StandInSapi
from tests.test_voice_stage import PARTS

needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")

SAPI_HEALTH = {"ok": True, "engine_kind": "fake", "voice_mode": "sapi", "voice": "Microsoft David Desktop"}
TONES_HEALTH = {
    "ok": True, "engine_kind": "fake", "voice_mode": "tones", "voice": None, "voice_fallback": True,
    "voice_warning": "no Windows speech (SAPI) voice is installed on this PC; the fake makes tone bursts",
}  # fmt: skip


def engine_test_ctx(home: Path, *, dev_mode: bool = False) -> JobContext:
    ctx = make_ctx(home)
    ctx.runtime.tts.engine = "fake"
    ctx.runtime.dev_mode = dev_mode
    return ctx


def log_rows(ctx: JobContext) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for f in sorted(ctx.paths.logs.glob("studio-*.jsonl")):
        rows += [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows


def test_only_an_engine_test_has_a_fake_voice_to_report(tmp_home: Path):
    assert is_fake_voice(engine_test_ctx(tmp_home))
    assert not is_fake_voice(
        engine_test_ctx(tmp_home, dev_mode=True)
    )  # a dev install: the FakeTts of the studio
    real = make_ctx(tmp_home)
    assert not is_fake_voice(real)
    assert note_voice_engine(real, lambda url: pytest.fail("a real voice is not asked")) is None
    assert not real.file(VOICE_ENGINE_FILE).exists() and captions_note(real) == ""


def test_the_job_keeps_and_logs_the_windows_voice_that_spoke(tmp_home: Path):
    ctx = engine_test_ctx(tmp_home)
    asked: list[str] = []

    def fetch(url: str) -> dict[str, Any]:
        asked.append(url)
        return SAPI_HEALTH

    info = note_voice_engine(ctx, fetch)
    assert asked == [ctx.runtime.tts.base_url]
    assert info == {"engine": "fake", "mode": "sapi", "voice": "Microsoft David Desktop", "warning": None}
    assert json.loads(ctx.file(VOICE_ENGINE_FILE).read_text(encoding="utf-8")) == info
    rows = [r for r in log_rows(ctx) if r["event"] == "fake_voice_sapi"]
    assert (
        len(rows) == 1
        and rows[0]["level"] == "INFO"
        and rows[0]["data"]["voice"] == "Microsoft David Desktop"
    )
    assert not [r for r in log_rows(ctx) if r["level"] == "WARN"]


def test_tone_bursts_on_an_engine_test_are_a_logged_warning_with_the_reason(tmp_home: Path):
    ctx = engine_test_ctx(tmp_home)
    info = note_voice_engine(ctx, lambda url: TONES_HEALTH)
    assert info is not None and info["mode"] == "tones" and info["voice"] is None
    warns = [r for r in log_rows(ctx) if r["event"] == "fake_voice_tones_only"]
    assert len(warns) == 1 and warns[0]["level"] == "WARN"
    assert warns[0]["msg"] == TONES_HEALTH["voice_warning"] and warns[0]["data"]["job_id"] == ctx.job_id


def test_a_voice_service_that_does_not_answer_is_noted_not_fatal(tmp_home: Path):
    ctx = engine_test_ctx(tmp_home)
    info = note_voice_engine(ctx, lambda url: None)  # an older service, or one that went away
    assert info is not None and info["mode"] is None and info["voice"] is None
    assert "fake one of this engine test" in captions_note(ctx)  # still named as the fake


def test_fetch_health_reads_the_body_and_swallows_a_dead_service(monkeypatch: pytest.MonkeyPatch):
    import httpx

    seen: list[str] = []

    def answer(url: str, timeout: float) -> httpx.Response:
        seen.append(url)
        return httpx.Response(200, json=SAPI_HEALTH)

    monkeypatch.setattr("ns_studio.media.fake_voice.httpx.get", answer)
    assert fetch_health("http://127.0.0.1:8091") == SAPI_HEALTH and seen == ["http://127.0.0.1:8091/health"]

    def refuse(url: str, timeout: float) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr("ns_studio.media.fake_voice.httpx.get", refuse)
    assert fetch_health("http://127.0.0.1:8091") is None
    monkeypatch.setattr(
        "ns_studio.media.fake_voice.httpx.get", lambda url, timeout: httpx.Response(200, text="not json")
    )
    assert fetch_health("http://127.0.0.1:8091") is None


def test_the_captions_failure_names_the_fake_voice(tmp_home: Path):
    ctx = engine_test_ctx(tmp_home)
    note_voice_engine(ctx, lambda url: SAPI_HEALTH)
    note = captions_note(ctx)
    assert "fake one of this engine test" in note and "Microsoft David Desktop" in note
    ctx2 = engine_test_ctx(tmp_home)
    note_voice_engine(ctx2, lambda url: TONES_HEALTH)
    note2 = captions_note(ctx2)
    assert "tone bursts" in note2 and "not a product fault" in note2 and "no Windows speech voice" in note2
    assert e403_detail(ctx2, ["s1: drift inf ms, matched 0%", "s2: drift 400 ms, matched 90%"]) == (
        "s1: drift inf ms, matched 0%; s2: drift 400 ms, matched 90%; " + note2
    )
    plain = make_ctx(tmp_home)
    assert e403_detail(plain, ["s1: drift inf ms, matched 0%"]) == "s1: drift inf ms, matched 0%"


def test_the_captions_stage_raises_ns_e403_that_says_the_voice_was_the_fake(tmp_home: Path):
    ctx = engine_test_ctx(tmp_home)
    _prepare_audio(ctx, SCRIPT, None)
    note_voice_engine(ctx, lambda url: TONES_HEALTH)
    ctx.services.transcriber = _ShuffledTranscriber()  # hears no word of the script, like whisper on tones
    with pytest.raises(NsError) as ei:
        CaptionsStage().run(ctx)
    assert ei.value.code == "NS-E403"
    assert "matched 0%" in ei.value.detail and "tone bursts" in ei.value.detail
    assert "not a product fault" in ei.value.detail


def test_a_real_voice_keeps_the_plain_ns_e403_detail(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _prepare_audio(ctx, SCRIPT, None)
    ctx.services.transcriber = _ShuffledTranscriber()
    with pytest.raises(NsError) as ei:
        CaptionsStage().run(ctx)
    assert ei.value.code == "NS-E403" and "fake" not in ei.value.detail and "tone" not in ei.value.detail


def test_the_fake_recognizer_still_passes_a_matching_voice_on_an_engine_test(tmp_home: Path):
    ctx = engine_test_ctx(tmp_home)
    _prepare_audio(ctx, SCRIPT, None)
    note_voice_engine(ctx, lambda url: SAPI_HEALTH)
    ctx.services.transcriber = FakeTranscriber()
    CaptionsStage().run(ctx)  # no failure: the note is added to failures only
    assert ctx.read_json("captions/report.json")["s1"]["match_ratio"] == 1.0


@needs_ffmpeg
def test_the_voice_stage_treats_the_windows_speaking_fake_like_real_engine_output(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """The whole path of an engine test's narration: the real service code with the fake speaking the script
    text (peaks near full scale, as Windows voices have), the studio's TtsClient, the voice stage's loudness
    normalization. What it leaves is what real engine output is held to: -16 LUFS, peaks where the +2 dB lift
    to -14 stays linear, 48 kHz, the script sidecar next to each clip, and the job's record of the voice."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from ns_tts.server import create_app

    engine = FakeEngine(speech="sapi", sapi=StandInSapi())
    engine.load()
    service = TestClient(create_app(engine, VoiceLibrary(tmp_home / "voices")))
    ctx = engine_test_ctx(tmp_home)
    ctx.services.tts = TtsClient("http://testserver", client=service)
    monkeypatch.setattr(
        "ns_studio.media.voice.note_voice_engine",
        lambda c: note_voice_engine(c, lambda url: service.get("/health").json()),
    )
    ctx.write_json("parts.json", PARTS)
    REGISTRY["voice"].run(ctx)
    spoken = [call["lines"] for call in engine._sapi.calls[1:]]  # type: ignore[attr-defined]
    assert any(PARTS[0]["hook"] in lines for lines in spoken)  # the script text went to the synthesizer
    man = ctx.read_json("audio/manifest.json")
    assert set(man) == {"p1", "p1_hook"}
    for item in man.values():
        lufs, tp = measure_lufs(Path(item["path"]))
        assert -17 <= lufs <= -15 and tp <= -1.0
        assert linear_possible(item["lufs"], item["tp"], i=-14.0, tp=-1.5)
        assert abs(item["lufs"] - VOICE_LUFS) <= 1.0
        assert ff.probe(Path(item["path"]))["streams"][0]["sample_rate"] == "48000"
    assert Path(man["p1"]["path"]).with_suffix(".txt").read_text(encoding="utf-8") == PARTS[0]["text"]
    assert man["p1"]["seconds"] > 5  # 28 words at the planner's pace: about 10 s
    record = json.loads(ctx.file(VOICE_ENGINE_FILE).read_text(encoding="utf-8"))
    assert record["mode"] == "sapi" and record["voice"] == "Microsoft David Desktop"


# ---------------------------------------------------------------- CR-H7-1: the tone-burst warning has a code


def test_the_job_log_and_voice_engine_json_carry_the_tone_burst_warning_code(tmp_home: Path):
    from ns_common.errors import describe

    ctx = engine_test_ctx(tmp_home)
    health = {**TONES_HEALTH, "voice_warning_code": "NS-W303"}
    info = note_voice_engine(ctx, lambda url: health)
    assert info is not None and info["code"] == "NS-W303" and describe(info["code"]).hint
    saved = json.loads(ctx.file(VOICE_ENGINE_FILE).read_text(encoding="utf-8"))
    assert (
        saved["code"] == "NS-W303"
        and saved["mode"] == "tones"
        and saved["warning"] == health["voice_warning"]
    )
    warns = [r for r in log_rows(ctx) if r["event"] == "fake_voice_tones_only"]
    assert (
        len(warns) == 1 and warns[0]["data"]["code"] == "NS-W303" and warns[0]["data"]["job_id"] == ctx.job_id
    )


def test_a_voice_service_that_predates_the_code_still_gets_it_in_the_job_log(tmp_home: Path):
    ctx = engine_test_ctx(tmp_home)
    info = note_voice_engine(ctx, lambda url: TONES_HEALTH)  # no voice_warning_code in /health
    assert info is not None and info["code"] == "NS-W303"
    warns = [r for r in log_rows(ctx) if r["event"] == "fake_voice_tones_only"]
    assert len(warns) == 1 and warns[0]["data"]["code"] == "NS-W303"


def test_a_windows_voice_or_a_silent_service_has_no_warning_code(tmp_home: Path):
    for health in (SAPI_HEALTH, None):
        ctx = engine_test_ctx(tmp_home)
        info = note_voice_engine(ctx, lambda url, h=health: h)
        assert info is not None and "code" not in info
        assert not [r for r in log_rows(ctx) if r["level"] == "WARN"]


def test_the_captions_detail_keeps_its_wording_with_the_code_in_the_record(tmp_home: Path):
    ctx = engine_test_ctx(tmp_home)
    note_voice_engine(ctx, lambda url: TONES_HEALTH)
    assert captions_note(ctx) == (
        "the voice is the fake one of this engine test and it made tone bursts, which speech recognition "
        "cannot align (no Windows speech voice was available): this is the test voice, not a product fault"
    )
