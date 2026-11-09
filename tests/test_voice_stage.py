import math
from pathlib import Path

import pytest

from ns_common import ffmpeg as ff
from ns_studio.media.audio import VOICE_LUFS, VOICE_TP, linear_possible, parse_ebur128, parse_silences
from ns_studio.media.stages_media import MASTER_MEDIA_STAGES, SHORTS_MEDIA_STAGES
from ns_studio.media.voice import loudnorm, measure_lufs, narration_items
from ns_studio.orchestrator import FORMAT_STAGES, SHORTS_ONLY_STAGES
from ns_studio.stages import REGISTRY
from ns_studio.text import stages_shorts  # noqa: F401
from tests.helpers import make_ctx

needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")

PARTS = [
    {
        "n": 1,
        "text": "The doorbell camera pings at 3:12 every night. I stopped checking the clips a week ago. "
        "Tonight my phone buzzed while I was standing on the porch.",
        "hook": "My doorbell camera records someone every night at 3:12.",
        "word_count": 28,
        "est_seconds": 10.8,
        "cliffhanger": False,
    }
]


def test_items_include_body_and_hook():
    items = narration_items(PARTS, "Ryan", 1.0)
    assert [i.id for i in items] == ["p1", "p1_hook"]


def test_hook_equal_to_first_sentence_is_not_voiced_twice():
    parts = [dict(PARTS[0], hook="The doorbell camera pings at 3:12 every night.")]
    assert [i.id for i in narration_items(parts, "Ryan", 1.0)] == ["p1"]


def test_media_stages_register_in_order():
    assert [s.name for s in SHORTS_MEDIA_STAGES] == ["voice", "captions", "clips", "render", "qc", "outbox"]
    assert all(s.name in REGISTRY for s in [*SHORTS_MEDIA_STAGES, *MASTER_MEDIA_STAGES])
    assert FORMAT_STAGES["shorts_only"] == list(SHORTS_ONLY_STAGES)
    assert FORMAT_STAGES["shorts_only"][-6:] == [s.name for s in SHORTS_MEDIA_STAGES]
    assert FORMAT_STAGES["master_first"][-10:] == [s.name for s in MASTER_MEDIA_STAGES]
    assert all(n in REGISTRY for n in SHORTS_ONLY_STAGES)
    assert REGISTRY["voice"].tenant == "tts" and REGISTRY["captions"].tenant == "whisper"


def test_parse_ebur128_summary():
    text = (
        "[Parsed_ebur128_0 @ 0x1] t: 0.1 M: -120 S: -120 I: -70.0 LUFS LRA: 0.0 LU\n"
        "[Parsed_ebur128_0 @ 0x1] Summary:\n\n  Integrated loudness:\n    I:         -14.2 LUFS\n"
        "    Threshold: -24.2 LUFS\n\n  Loudness range:\n    LRA:         2.1 LU\n\n  True peak:\n"
        "    Peak:       -1.6 dBFS\n"
    )
    m = parse_ebur128(text)
    assert (m.lufs, m.true_peak) == (-14.2, -1.6)
    assert math.isnan(parse_ebur128("nothing here").lufs)


def test_parse_silences():
    text = (
        "[silencedetect @ 0x1] silence_start: -0.0012\n"
        "[silencedetect @ 0x1] silence_end: 0.52 | silence_duration: 0.52\n"
        "[silencedetect @ 0x1] silence_start: 3.1\n"
    )
    assert parse_silences(text) == [(-0.0012, 0.52), (3.1, None)]


@needs_ffmpeg
def test_voice_stage_normalizes(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    # The fake TTS tone never needs the limiter, so the numbers below cannot show that voice.py asks for the
    # voice target and its lowered true-peak ceiling; record what it passes to loudnorm and check that.
    calls: list[dict] = []

    def recording_loudnorm(in_wav: Path, out_wav: Path, **kw):
        calls.append(kw)
        return loudnorm(in_wav, out_wav, **kw)

    monkeypatch.setattr("ns_studio.media.voice.loudnorm", recording_loudnorm)
    ctx = make_ctx(tmp_home)
    ctx.write_json("parts.json", PARTS)
    REGISTRY["voice"].run(ctx)
    assert len(calls) == 2
    assert all(c["i"] == VOICE_LUFS and c["tp"] == VOICE_TP for c in calls)
    man = ctx.read_json("audio/manifest.json")
    assert set(man) == {"p1", "p1_hook"}
    lufs, tp = measure_lufs(Path(man["p1"]["path"]))
    assert -17 <= lufs <= -15 and tp <= -1.0
    for item in man.values():     # peaks already sit where the Short's +2 dB lift stays linear (limit once)
        assert linear_possible(item["lufs"], item["tp"], i=-14.0, tp=-1.5)
    parts = ctx.read_json("parts.json")
    assert parts == PARTS  # measured seconds live in the manifest; parts.json is an input of the LLM stages
    sidecar = Path(man["p1"]["path"]).with_suffix(".txt")
    assert sidecar.read_text(encoding="utf-8") == PARTS[0]["text"]
    assert ff.probe(Path(man["p1"]["path"]))["streams"][0]["sample_rate"] == "48000"


@needs_ffmpeg
def test_voice_stage_second_run_does_not_call_tts(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("parts.json", PARTS)
    REGISTRY["voice"].run(ctx)
    first = ctx.read_json("audio/manifest.json")

    def boom(*a, **k):
        raise AssertionError("TTS must not be called again")

    ctx.services.tts.speak_batch = boom  # type: ignore[method-assign]
    REGISTRY["voice"].run(ctx)
    assert ctx.read_json("audio/manifest.json") == first


@needs_ffmpeg
def test_voice_stage_resynthesizes_only_changed_items(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("parts.json", PARTS)
    REGISTRY["voice"].run(ctx)
    first = ctx.read_json("audio/manifest.json")
    seen: list[list[str]] = []
    real = ctx.services.tts.speak_batch

    def spy(items, out_dir, *, seed=None, timeout_s=None):
        seen.append([i.id for i in items])
        return real(items, out_dir, seed=seed, timeout_s=timeout_s)

    ctx.services.tts.speak_batch = spy  # type: ignore[method-assign]
    parts = [dict(PARTS[0], hook="Someone keeps ringing my bell at 3:12.")]
    ctx.write_json("parts.json", parts)
    REGISTRY["voice"].run(ctx)
    assert seen == [["p1_hook"]]
    second = ctx.read_json("audio/manifest.json")
    assert second["p1"] == first["p1"] and second["p1_hook"]["sig"] != first["p1_hook"]["sig"]


@needs_ffmpeg
def test_loudnorm_hits_target_on_sparse_tone_bursts(tmp_path: Path):
    from ns_common.tts_client import FakeTts

    src = tmp_path / "raw.wav"
    FakeTts().speak("one two three four five six seven eight nine ten eleven twelve", "Ryan", 1.0, src)
    out = tmp_path / "n.wav"
    loudnorm(src, out, i=-14.0, tp=-1.5)
    lufs, tp = measure_lufs(out)
    assert abs(lufs - (-14.0)) <= 0.8 and tp <= -1.0


def test_cliffhanger_part_sends_its_last_sentence_as_cliff_line():
    """Gap 15: a part flagged cliffhanger gets 1,200 ms before its closing sentence."""
    parts = [dict(PARTS[0], cliffhanger=True), dict(PARTS[0], n=2, cliffhanger=False)]
    one, two = (i for i in narration_items(parts, "Ryan", 1.0) if i.id in ("p1", "p2"))
    assert one.cliff_lines == ("Tonight my phone buzzed while I was standing on the porch.",)
    assert two.cliff_lines == ()


def test_cliff_line_changes_the_cache_signature_only_when_present():
    from ns_common.tts_client import TtsItem
    from ns_studio.media.voice import _signature

    plain = TtsItem("a", "One. Two.", "Ryan", 1.0)
    with_cliff = TtsItem("a", "One. Two.", "Ryan", 1.0, ("Two.",))
    assert _signature(plain) != _signature(with_cliff)
    assert _signature(plain) == _signature(TtsItem("b", "One. Two.", "Ryan", 1.0))
