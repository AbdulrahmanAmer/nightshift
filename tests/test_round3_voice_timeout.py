"""Gap L4: the voice stage's time limit follows the measured speed, and a timeout is retried once."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_common.tts_timeout import (
    TTS_TIMEOUT_CAP_S,
    TTS_TIMEOUT_PER_LINE_S,
    batch_timeout_s,
    load_rtf,
    per_line_s,
    save_rtf,
    speed_path,
    tts_timeout_s,
)
from tests.helpers import make_ctx
from tests.test_voice_stage import PARTS

# ---------------------------------------------------------------- the formula


def test_the_per_line_figure_follows_the_measured_speed_with_a_floor():
    assert per_line_s(None) == TTS_TIMEOUT_PER_LINE_S == 3.0
    assert per_line_s(0.3) == 3.0  # a fast engine keeps the floor: 6 * 0.3 * 1.5 = 2.7
    assert per_line_s(0.5) == pytest.approx(4.5)
    assert per_line_s(0.8) == pytest.approx(7.2)
    assert per_line_s(1.5) == pytest.approx(13.5)
    for bad in (0.0, -1.0, float("inf"), float("nan")):
        assert per_line_s(bad) == 3.0


def test_a_slow_engine_gets_a_batch_limit_that_fits_a_14_minute_master():
    lines = 170  # about 2,170 words
    assert tts_timeout_s(lines) == 120 + 3 * 170  # the old limit: 630 s, too short above RTF 0.55
    assert tts_timeout_s(lines, 0.8) == pytest.approx(120 + 7.2 * 170)  # 1,344 s
    assert tts_timeout_s(lines, 0.8) > 170 * 6.0 * 0.8  # the engine needs 816 s of speech time, plus the load
    assert tts_timeout_s(10**6, 2.0) == TTS_TIMEOUT_CAP_S
    assert batch_timeout_s(["One. Two."], 1.0) == pytest.approx(120 + 9.0 * 2)
    assert batch_timeout_s(["One. Two."]) == 120 + 3 * 2  # unchanged without a measurement


# ---------------------------------------------------------------- the stored speed


def test_the_speed_is_stored_and_read_back(tmp_home: Path):
    paths = Paths.from_env()
    paths.ensure()
    assert load_rtf(paths) is None
    save_rtf(paths, 0.4237, quick=True)
    doc = json.loads(speed_path(paths).read_text(encoding="utf-8"))
    assert doc["rtf"] == 0.424 and doc["quick"] is True and doc["measured_at"]
    assert load_rtf(paths) == 0.424
    save_rtf(paths, 0.6, quick=False)  # a full verify replaces the quick figure
    assert load_rtf(paths) == 0.6


BAD_FILES = ["", "not json", "[]", '{"rtf": "fast"}', '{"rtf": -1}', '{"rtf": true}', "{}"]


@pytest.mark.parametrize("text", BAD_FILES)
def test_a_damaged_speed_file_means_no_measurement(tmp_home: Path, text: str):
    paths = Paths.from_env()
    paths.ensure()
    speed_path(paths).write_text(text, encoding="utf-8")
    assert load_rtf(paths) is None


def test_an_unusable_measurement_is_not_stored(tmp_home: Path):
    paths = Paths.from_env()
    paths.ensure()
    for bad in (0.0, -2.0, float("inf"), float("nan")):
        save_rtf(paths, bad, quick=True)
    assert not speed_path(paths).exists()


def test_verify_stores_the_slowest_voice_mode(tmp_home: Path):
    """`ns verify` writes the figure its own check rows show (the worst of the modes it exercised)."""
    from ns_supervisor import verify

    src = Path(verify.__file__).read_text(encoding="utf-8")
    # verify-limits L4: the stored figure is still the single calls' whole ratio, the worst of them; the new
    # speaking rate (`tts.rate`) is never stored, it would shrink a real job's time limit.
    assert "save_rtf(paths, max(rtfs), quick=quick)" in src


# ---------------------------------------------------------------- the voice stage


def _timeout() -> NsError:
    detail = "tts batch: no answer after 630s (asked the service to cancel it)"
    return NsError("NS-E310", detail, timed_out=True)


@pytest.mark.skipif(not ff.available(), reason="ffmpeg required")
def test_the_voice_stage_scales_its_limit_from_the_stored_speed(tmp_home: Path):
    from ns_studio.stages import REGISTRY

    ctx = make_ctx(tmp_home)
    save_rtf(ctx.paths, 0.8, quick=False)
    ctx.write_json("parts.json", PARTS)
    seen: list[float | None] = []
    real = ctx.services.tts.speak_batch

    def spy(items, out_dir, *, seed=None, timeout_s=None):  # type: ignore[no-untyped-def]
        seen.append(timeout_s)
        return real(items, out_dir, seed=seed, timeout_s=timeout_s)

    ctx.services.tts.speak_batch = spy  # type: ignore[method-assign]
    REGISTRY["voice"].run(ctx)
    assert seen == [batch_timeout_s([PARTS[0]["text"], PARTS[0]["hook"]], 0.8)]
    assert seen[0] is not None and seen[0] > batch_timeout_s([PARTS[0]["text"], PARTS[0]["hook"]])


@pytest.mark.skipif(not ff.available(), reason="ffmpeg required")
def test_a_timeout_is_retried_once_with_a_longer_limit_and_the_stage_goes_on(tmp_home: Path):
    from ns_studio.stages import REGISTRY

    ctx = make_ctx(tmp_home)
    ctx.write_json("parts.json", PARTS)
    seen: list[float | None] = []
    real = ctx.services.tts.speak_batch

    def flaky(items, out_dir, *, seed=None, timeout_s=None):  # type: ignore[no-untyped-def]
        seen.append(timeout_s)
        if len(seen) == 1:
            raise _timeout()
        return real(items, out_dir, seed=seed, timeout_s=timeout_s)

    ctx.services.tts.speak_batch = flaky  # type: ignore[method-assign]
    REGISTRY["voice"].run(ctx)
    assert len(seen) == 2 and seen[0] is not None and seen[1] == min(TTS_TIMEOUT_CAP_S, seen[0] * 2)
    assert ctx.file("audio/manifest.json").exists()


@pytest.mark.skipif(not ff.available(), reason="ffmpeg required")
def test_a_second_timeout_fails_the_stage_and_other_errors_are_not_retried(tmp_home: Path):
    from ns_studio.stages import REGISTRY

    ctx = make_ctx(tmp_home)
    ctx.write_json("parts.json", PARTS)
    calls: list[int] = []

    def always(items, out_dir, *, seed=None, timeout_s=None):  # type: ignore[no-untyped-def]
        calls.append(1)
        raise _timeout()

    ctx.services.tts.speak_batch = always  # type: ignore[method-assign]
    with pytest.raises(NsError) as ei:
        REGISTRY["voice"].run(ctx)
    assert ei.value.code == "NS-E310" and len(calls) == 2  # one retry, no loop

    calls.clear()

    def refused(items, out_dir, *, seed=None, timeout_s=None):  # type: ignore[no-untyped-def]
        calls.append(1)
        raise NsError("NS-E311", "unknown voice")

    ctx.services.tts.speak_batch = refused  # type: ignore[method-assign]
    with pytest.raises(NsError) as ei:
        REGISTRY["voice"].run(ctx)
    assert ei.value.code == "NS-E311" and len(calls) == 1

    calls.clear()

    def failed(items, out_dir, *, seed=None, timeout_s=None):  # type: ignore[no-untyped-def]
        calls.append(1)
        raise NsError("NS-E310", "tts batch: HTTP 500")  # not a time-out: a retry would only repeat it

    ctx.services.tts.speak_batch = failed  # type: ignore[method-assign]
    with pytest.raises(NsError):
        REGISTRY["voice"].run(ctx)
    assert len(calls) == 1


def test_the_client_marks_a_timeout_for_the_retry(tmp_path: Path):
    import httpx

    from ns_common.tts_client import TtsClient, TtsItem

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/batch"):
            raise httpx.ReadTimeout("no answer")
        return httpx.Response(200, json={"cancelled": True})

    cli = TtsClient("http://x", client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(NsError) as ei:
        cli.speak_batch([TtsItem("a", "One.", "Ryan")], tmp_path)
    assert ei.value.code == "NS-E310" and ei.value.data == {"timed_out": True}
    err = NsError("NS-E310", "tts batch: HTTP 500")
    assert err.data == {}
