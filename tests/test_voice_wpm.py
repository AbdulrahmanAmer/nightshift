"""P02: the words per minute a voice really speaks are measured and planned with (F-H48), and the TTS seed
is the voice's own except after a regeneration (F-H69).

No FFmpeg here: the narration is `FakeTts` tone bursts, which carry the same pauses the real service adds,
and the voice stage's loudness steps are replaced by a copy so `synthesize` runs end to end.
"""

import json
import shutil
from pathlib import Path

import pytest

from ns_common.pacing import PACING_WPM, pacing_speed
from ns_common.textstats import word_count
from ns_common.tts_client import FakeTts, TtsItem, wav_seconds
from ns_studio.context import JobContext
from ns_studio.media import voice as voice_mod
from ns_studio.media.voice import narration_items, synthesize, tts_seed
from ns_studio.text.budget import master_budget, short_budget
from ns_studio.text.stages_shorts import ideas_budget, job_master_budget
from ns_studio.voice_wpm import (
    MIN_SAMPLE_WORDS,
    PINNED_FILE,
    job_wpm,
    load_voice_wpm,
    measured_wpm_1x,
    record_voice_wpm,
    voice_wpm_path,
)
from tests.helpers import make_channel, make_combo, make_ctx

SENTENCE = "The doorbell camera pings at three twelve every night and I never look at the clips anymore."
TEXT = " ".join([SENTENCE] * 12)  # 12 sentences, 192 words
SLOW = 120.0  # words per minute of the fake voice in these tests; the table says 155


def _voiced(ctx: JobContext, tts: FakeTts, items: list[TtsItem]) -> dict[str, dict]:
    """The manifest the voice stage would write: each item spoken by `tts`, its seconds measured."""
    out: dict[str, dict] = {}
    audio = ctx.subdir("audio")
    for it in items:
        wav = audio / f"{it.id}.wav"
        tts.speak(it.text, it.voice, it.speed, wav, cliff_lines=it.cliff_lines)
        out[it.id] = {"path": str(wav), "seconds": round(wav_seconds(wav), 3)}
    return out


def _measure(ctx: JobContext, wpm: float, *, speed: float = 1.0, voice: str = "Ryan") -> float | None:
    items = [TtsItem("p1", TEXT, voice, speed)]
    return record_voice_wpm(ctx, voice, items, _voiced(ctx, FakeTts(wpm=wpm), items))


def test_measured_pace_is_words_over_speech_seconds_without_the_pauses(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    assert word_count(TEXT) >= MIN_SAMPLE_WORDS
    wpm_1x = _measure(ctx, SLOW)
    # 192 words spoken at 120 wpm; the 11 x 0.35 s of silence between the sentences is not speech
    assert wpm_1x == pytest.approx(SLOW, abs=1.0)
    assert measured_wpm_1x(ctx.paths, "Ryan") == wpm_1x
    stored = json.loads(voice_wpm_path(ctx.paths).read_text(encoding="utf-8"))
    assert stored["voices"]["Ryan"]["wpm_1x"] == wpm_1x and stored["voices"]["Ryan"]["jobs"] == 1


def test_measurement_is_normalized_to_speed_one(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    # the same voice asked for speed 1.2 speaks 1.2 x faster; the stored figure is the 1.0 one
    assert _measure(ctx, SLOW, speed=1.2) == pytest.approx(SLOW, abs=1.0)


def test_a_second_pass_of_the_same_job_replaces_its_sample(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _measure(ctx, 100.0)
    _measure(ctx, 130.0)  # e.g. the voice stage ran again after a chapter was expanded
    entry = load_voice_wpm(ctx.paths)["voices"]["Ryan"]
    assert entry["jobs"] == 1 and entry["wpm_1x"] == pytest.approx(130.0, abs=1.0)


def test_two_jobs_average_by_words(tmp_home: Path):
    first = make_ctx(tmp_home)
    _measure(first, 100.0)
    second = make_ctx(tmp_home)
    _measure(second, 140.0)
    entry = load_voice_wpm(second.paths)["voices"]["Ryan"]
    assert entry["jobs"] == 2 and 100.0 < entry["wpm_1x"] < 140.0


def test_too_little_narration_or_an_impossible_pace_is_not_stored(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    short = [TtsItem("p1", "Only five words here today.", "Ryan", 1.0)]
    assert record_voice_wpm(ctx, "Ryan", short, _voiced(ctx, FakeTts(wpm=SLOW), short)) is None
    assert _measure(ctx, 20.0) is None  # 20 words per minute is a broken sample, not a voice
    assert not voice_wpm_path(ctx.paths).exists()


def test_a_damaged_file_means_no_measurement_and_the_table(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    voice_wpm_path(ctx.paths).write_text("{not json", encoding="utf-8")
    assert measured_wpm_1x(ctx.paths, "Ryan") is None
    assert job_wpm(ctx) == PACING_WPM["standard"]
    assert _measure(ctx, SLOW) == pytest.approx(SLOW, abs=1.0)  # and it is replaced by a good one


def test_a_fake_voice_is_never_recorded_as_the_pace_of_the_real_one(tmp_home: Path):
    """The tone-burst engine speaks 155 x speed by construction: stored as Ryan's, it would drive budgets."""
    ctx = make_ctx(tmp_home)
    ctx.runtime.tts.engine = "fake"
    assert _measure(ctx, SLOW) is None
    assert not voice_wpm_path(ctx.paths).exists()
    dev = make_ctx(tmp_home)
    dev.runtime.dev_mode = True
    assert _measure(dev, SLOW) is None and not voice_wpm_path(dev.paths).exists()
    real = make_ctx(tmp_home)
    assert _measure(real, SLOW) == pytest.approx(SLOW, abs=1.0)  # the same samples on a real engine count


def test_one_malformed_sample_does_not_block_every_later_record(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    bad = {"voices": {"Ryan": {"wpm_1x": 150, "samples": {"j-old": {"speech_s_1x": 60}, "j-str": "x"}}}}
    voice_wpm_path(ctx.paths).write_text(json.dumps(bad), encoding="utf-8")
    assert _measure(ctx, SLOW) == pytest.approx(SLOW, abs=1.0)  # no KeyError swallowed into a warning
    stored = load_voice_wpm(ctx.paths)["voices"]["Ryan"]
    assert list(stored["samples"]) == [ctx.job_id] and stored["jobs"] == 1  # the unusable ones are dropped


def test_a_job_planned_before_the_pace_was_pinned_keeps_the_table(tmp_home: Path):
    """A resume of an older job must not plan chapters with a pace the first half of the plan never saw."""
    _measure(make_ctx(tmp_home), SLOW)
    older = make_ctx(tmp_home, fmt="master_first", channel=make_channel(format="master_first"))
    older.write_json("plan_meta.json", {"working_title": "t", "wpm": 155, "min_words": 1395})
    assert not older.file(PINNED_FILE).exists()
    assert job_wpm(older) == PACING_WPM["standard"]
    pinned = json.loads(older.file(PINNED_FILE).read_text(encoding="utf-8"))
    assert pinned["source"] == "table" and pinned["wpm_1x"] == pytest.approx(SLOW, abs=1.0)
    assert job_master_budget(older).wpm == 155
    fresh = make_ctx(tmp_home, fmt="master_first", channel=make_channel(format="master_first"))
    fresh.write_json("idea.json", {"premise": "p", "twist": "t"})  # a banked idea is not a plan
    assert job_wpm(fresh) == round(SLOW * pacing_speed(fresh.channel.voice.speed, "standard"))


def test_budgets_take_a_measured_pace_over_the_pacing_table():
    table = short_budget(50, "standard")
    slow = short_budget(50, "standard", wpm=120)
    assert table.wpm == 155 and slow.wpm == 120 and slow.words < table.words
    assert master_budget(11, "standard", wpm=120).target_words < master_budget(11, "standard").target_words
    assert master_budget(11, "standard", wpm=120).wpm == 120


def test_the_second_job_plans_with_the_pace_the_first_job_measured(tmp_home: Path):
    first = make_ctx(tmp_home, fmt="master_first", channel=make_channel(format="master_first"))
    assert job_master_budget(first).wpm == 155  # nothing measured yet: the table
    assert json.loads(first.file(PINNED_FILE).read_text(encoding="utf-8"))["source"] == "table"
    _measure(first, SLOW)
    assert job_master_budget(first).wpm == 155  # the job in flight keeps the pace it started with

    second = make_ctx(tmp_home, fmt="master_first", channel=make_channel(format="master_first"))
    mb = job_master_budget(second)
    assert mb.wpm == round(SLOW * pacing_speed(second.channel.voice.speed, "standard"))
    assert mb.wpm != PACING_WPM["standard"]
    table = job_master_budget(first)  # the target minutes are drawn from the job id: compare words per minute
    assert mb.target_words / mb.target_minutes < table.target_words / table.target_minutes
    pinned = json.loads(second.file(PINNED_FILE).read_text(encoding="utf-8"))
    assert pinned["source"] == "measured" and pinned["table_wpm"] == 155 and pinned["voice"] == "Ryan"


def test_a_shorts_job_budget_follows_the_measured_pace_too(tmp_home: Path):
    first = make_ctx(tmp_home)
    before = ideas_budget(first)  # nothing measured yet
    _measure(first, SLOW)
    after = ideas_budget(make_ctx(tmp_home))
    assert before.wpm == 155 and after.wpm == 120
    assert after.words < before.words


def test_the_planning_pace_follows_the_job_speed_and_stays_inside_a_sane_band(tmp_home: Path):
    _measure(make_ctx(tmp_home), SLOW)
    brisk = make_ctx(tmp_home, combo=make_combo(pacing="brisk"))
    # brisk asks the voice for 175/155 of its speed: the voice measured at 120 now speaks about 135
    assert job_wpm(brisk) == round(SLOW * pacing_speed(brisk.channel.voice.speed, "brisk"))
    assert 0.6 * 175 <= job_wpm(brisk) <= 1.6 * 175


def test_a_measurement_of_another_voice_is_not_used(tmp_home: Path):
    _measure(make_ctx(tmp_home), SLOW, voice="Aiden")
    assert job_wpm(make_ctx(tmp_home)) == 155  # the channel's voice is Ryan


# ---- F-H69: the seed ------------------------------------------------------------------------------------


def _no_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    """`synthesize` without FFmpeg: the loudness pass copies, the measurements are stubs."""
    monkeypatch.setattr(voice_mod, "loudnorm", lambda src, dst, **kw: shutil.copyfile(src, dst))
    monkeypatch.setattr(voice_mod, "measure_lufs", lambda p: (-16.0, -3.0))
    monkeypatch.setattr(voice_mod.ff, "duration_s", wav_seconds)


def _spy_seeds(ctx: JobContext) -> list[int | None]:
    seeds: list[int | None] = []
    real = ctx.services.tts.speak_batch

    def spy(items, out_dir, *, seed=None, timeout_s=None):  # type: ignore[no-untyped-def]
        seeds.append(seed)
        return real(items, out_dir, seed=seed, timeout_s=timeout_s)

    ctx.services.tts.speak_batch = spy  # type: ignore[method-assign]
    return seeds


def test_the_voice_stage_sends_no_seed_so_the_voice_keeps_its_own(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    _no_ffmpeg(monkeypatch)
    seeds_by_job: list[list[int | None]] = []
    for _ in range(2):  # two different jobs, same voice
        ctx = make_ctx(tmp_home)
        seeds = _spy_seeds(ctx)
        synthesize(ctx, narration_items([{"n": 1, "text": TEXT, "hook": ""}], "Ryan", 1.0))
        seeds_by_job.append(seeds)
    assert seeds_by_job == [[None], [None]]  # the TTS service then uses voice_seed("Ryan") for both


def test_a_regeneration_gets_a_seed_of_its_own_attempt(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    _no_ffmpeg(monkeypatch)
    ctx = make_ctx(tmp_home)
    assert tts_seed(ctx) is None
    ctx.write_json("policy_attempts.json", {"regenerations": 1})
    first = tts_seed(ctx)
    ctx.write_json("policy_attempts.json", {"regenerations": 2})
    second = tts_seed(ctx)
    assert isinstance(first, int) and isinstance(second, int) and first != second
    seeds = _spy_seeds(ctx)
    synthesize(ctx, narration_items([{"n": 1, "text": TEXT, "hook": ""}], "Ryan", 1.0))
    assert seeds == [second]


def test_the_voice_stage_stores_the_pace_it_measured_and_the_next_job_plans_with_it(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """F-H48 through `synthesize` (the call at the end of the voice stage), not `record_voice_wpm` alone:
    the voice speaks at `SLOW`, and a second job of the same voice plans with that pace, not the table's."""
    _no_ffmpeg(monkeypatch)
    ctx = make_ctx(tmp_home)
    ctx.services.tts = FakeTts(wpm=SLOW)
    assert not voice_wpm_path(ctx.paths).exists()
    synthesize(ctx, narration_items([{"n": 1, "text": TEXT, "hook": ""}], "Ryan", 1.0))
    stored = load_voice_wpm(ctx.paths)["voices"]["Ryan"]
    assert stored["wpm_1x"] == pytest.approx(SLOW, abs=1.0) and stored["jobs"] == 1
    nxt = make_ctx(tmp_home)
    assert job_wpm(nxt) == round(SLOW * pacing_speed(nxt.channel.voice.speed, nxt.combo.pacing))
    assert job_wpm(nxt) != PACING_WPM[nxt.combo.pacing]
    assert ideas_budget(nxt).wpm == job_wpm(nxt)
