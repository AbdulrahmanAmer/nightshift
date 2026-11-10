"""H-8b: the tiny-story profile reaches voice, render, QC and the outbox, and its output is never mistaken for
a real video.

H-8 put the text stages on the thresholds a 1 to 2 GB model can meet and left the duration thresholds alone,
so a tiny master usually stopped at the duration check (NS-E305) and a tiny Short outside +-15 % at QC
(NS-E404). Here LENGTH is part of the profile and quality is not: the duration estimate, the master floor
and ceiling, the Shorts window and QC's duration window come from `ns_studio/text/story_profile.py`, while
loudness, true peak, resolution, caption drift, black frames and file integrity stay as they were. The
output carries `profile: tiny_story` in the render report, the QC report and the outbox sidecar, the Queue
says so, and every helper that publishes or marks a video as published refuses it with `NS-E329`.

Every test with the profile on has its twin with the profile off (today's numbers, byte for byte). Nothing
here spawns FFmpeg: the media checks are patched where a test needs a "video", so these run on the laptop;
the real-FFmpeg run of a tiny master is `test_a_tiny_master_runs_through_render_qc_and_the_outbox` (CI
only).
"""

from __future__ import annotations

import json
import random
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from ns_common import ffmpeg as ff
from ns_common.errors import NsError, describe
from ns_common.models import Metadata, Plan, QcCheck, QcResult, Runtime
from ns_common.pacing import PAUSE_PER_WORD_S, master_fixed_seconds, narration_seconds
from tests.helpers import add_output, make_channel, make_combo, make_ctx
from tests.test_tiny_story import tiny_runtime

needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")

# What the real Qwen3-1.7B wrote on the engine-test laptop (job engine-test-20261109-8071, 2026-11-09 11:21 to
# 11:35, asked for 5 chapters of 394 words): the chapters of its draft in words. The stage said chapter 1 was
# 120 words after the critic's rewrite.
OBSERVED_CHAPTERS = (146, 99, 126, 162, 323)
STANDARD_WPM = 155


def profiles() -> Any:
    """The profile module, imported when a test runs so each test fails by itself where it is missing."""
    from ns_studio.text import story_profile

    return story_profile


def text_of(words: int) -> str:
    """`words` words in sentences of ten (a plain chapter for the duration estimate)."""
    sentences = [" ".join(f"w{i}x{j}" for j in range(10)) + "." for i in range(words // 10)]
    return " ".join(sentences)


def seconds_of(words: int, chapters: int) -> float:
    """What `ns_common.pacing` says `words` take to narrate at 155 wpm in `chapters` chapters."""
    return words * (60.0 / STANDARD_WPM + PAUSE_PER_WORD_S) + master_fixed_seconds(chapters)


# ============================================================ the numbers, from one place
def test_the_normal_profile_has_no_duration_numbers_of_its_own():
    sp = profiles()
    n = sp.NORMAL
    assert n.master_max_s is None and n.master_qc_s is None and n.mark is None
    assert n.short_band is None and n.master_floor_s is None
    assert sp.duration_profile(_Job(Runtime.default("cpu-dev"))) is sp.NORMAL


class _Job:
    """All the duration code needs of a job: the install's runtime and whether the script is the owner's."""

    def __init__(self, runtime: Runtime, owner_script: bool = False, job_dir: Path | None = None) -> None:
        self.runtime = runtime
        self.owner_script = owner_script
        if job_dir is not None:
            self.job_dir = job_dir


def test_the_tiny_duration_numbers_follow_from_one_target():
    """A tiny master is asked for 4 minutes in 5 chapters, accepted from half of it up to twice it.

    The arithmetic (docs/DEV_MODE.md): the 1.7B wrote chapters of 99 to 163 words (one of 323); five chapters
    of 110 words are 549 words at 155 wpm, 4:00 with the pauses and cards; half is 2:00, double 8:00."""
    sp = profiles()
    t = sp.TINY
    target_s = t.master_minutes * 60.0
    assert (t.master_minutes, t.master_chapters, t.mark) == (4, 5, "tiny_story")
    assert t.master_floor_s == target_s * t.master_floor_share == 120.0
    assert t.master_max_s == 2 * target_s == 480.0
    assert t.master_qc_s == (t.master_floor_s - 10.0, 900.0) == (110.0, 900.0)
    assert t.short_band == (0.35, 1.6)


def test_the_tiny_plan_is_four_minutes_in_five_chapters():
    sp = profiles()
    from ns_studio.text.budget import chapter_budget, master_budget

    mb = master_budget(11, "standard", profile=sp.TINY)
    assert (mb.target_minutes, mb.chapters) == (4, 5)
    assert abs(seconds_of(mb.target_words, 5) - 240.0) < 1.0  # the words fill the 4 minutes
    assert mb.min_words == round(mb.target_words * 0.5)  # half the words, the same share as the seconds
    assert max(mb.chapter_budgets) == 110
    Plan.model_validate(  # the Plan model takes it (it refused anything under 9 minutes before)
        {
            "target_minutes": mb.target_minutes,
            "target_words": mb.target_words,
            "chapters": [
                {"n": i, "title": "t", "beats": ["b"], "cliff": "c", "word_budget": w}
                for i, w in enumerate(mb.chapter_budgets, 1)
            ],
        }
    )
    assert chapter_budget(110, 155, sp.TINY).max_words == 330


def test_a_plan_under_one_minute_is_still_refused():
    chapter = {"n": 1, "title": "t", "beats": ["b"], "cliff": "c", "word_budget": 10}
    five = [{**chapter, "n": i} for i in range(1, 6)]
    Plan.model_validate({"target_minutes": 1, "target_words": 50, "chapters": five})
    with pytest.raises(ValueError):
        Plan.model_validate({"target_minutes": 0, "target_words": 50, "chapters": five})


def test_what_the_real_1_7b_wrote_fits_the_tiny_numbers_and_far_less_does_not():
    """The logged chapters (146, 99, 126, 162, 323 words) pass every tiny threshold; a tenth of that fails."""
    from ns_studio.text.budget import chapter_budget, master_budget

    sp = profiles()
    mb = master_budget(14, "standard", profile=sp.TINY)
    for words, budget in zip(OBSERVED_CHAPTERS, mb.chapter_budgets, strict=True):
        b = chapter_budget(budget, STANDARD_WPM, sp.TINY)
        assert b.min_words <= words <= b.max_words, (words, b)
    total = sum(OBSERVED_CHAPTERS)  # 856 words
    secs = seconds_of(total, 5)
    assert total >= mb.min_words
    assert sp.TINY.master_floor_s <= secs <= sp.TINY.master_max_s  # 6:09
    assert sp.TINY.master_qc_s[0] < secs < sp.TINY.master_qc_s[1]
    # one tenth of that: every chapter under the band, the master under the floor and under QC's minimum
    few = tuple(w // 10 for w in OBSERVED_CHAPTERS)
    assert all(
        w < chapter_budget(b, STANDARD_WPM, sp.TINY).min_words
        for w, b in zip(few, mb.chapter_budgets, strict=True)
    )
    assert seconds_of(sum(few), 5) < sp.TINY.master_qc_s[0]


def test_the_normal_master_numbers_are_todays():
    from ns_studio.media import qc
    from ns_studio.text.budget import MASTER_MAX_SECONDS, master_budget

    assert qc.LONG_RANGE_S == (510.0, 900.0) and qc.BUCKET_TOLERANCE == 0.15 and MASTER_MAX_SECONDS == 870
    mb = master_budget(9, "standard")
    assert (mb.target_minutes, mb.chapters, mb.target_words, mb.min_words) == (9, 5, 1259, 1259)


# ============================================================ the text estimate: the master's ceiling
def test_the_master_ceiling_of_the_text_estimate_follows_the_profile():
    from ns_studio.text.validators import check_master_length

    sp = profiles()
    long_master = [text_of(300)] * 5  # 1,500 words, about 10:40
    assert check_master_length(long_master, 155) is None  # normal: well under 14:30
    assert check_master_length(long_master, 155, profile=sp.NORMAL) is None
    v = check_master_length(long_master, 155, profile=sp.TINY)
    assert v is not None and v.rule == "master_length" and "at most 480 s" in v.detail
    fits = [text_of(190)] * 5  # about 7:00
    assert check_master_length(fits, 155, profile=sp.TINY) is None
    huge = [text_of(600)] * 5  # 3,000 words: over 14:30 on any install
    normal = check_master_length(huge, 155)
    assert normal is not None and "at most 870 s" in normal.detail


# ======================================================================= the duration check (NS-E305)
def duration_ctx(tmp_home: Path, *, tiny: bool, chapters: list[int], owner_script: bool = False):
    """A master job whose story has chapters of the given word counts; the voice is the pacing module's."""
    ctx = make_ctx(tmp_home, fmt="master_first", channel=make_channel(format="master_first"))
    if tiny:
        ctx.runtime = tiny_runtime(ctx.runtime)
    ctx.write_json(
        "story.json",
        {
            "working_title": "T",
            "chapters": [
                {"n": i, "title": f"Chapter {i}", "text": text_of(w)} for i, w in enumerate(chapters, 1)
            ],
        },
    )
    if owner_script:
        ctx.write_json("script_source.json", {"kind": "script_import", "owner_script": True})
    return ctx


def fake_voice(ctx: Any) -> None:
    """What `run_voice_master` leaves behind, without FFmpeg: a manifest with the seconds the pauses give."""
    from ns_studio.media import master as M

    manifest = {
        f"c{c.n}": {
            "path": f"audio/c{c.n}.wav",
            "seconds": round(narration_seconds(c.text, 155, c.cliff_lines), 3),
        }
        for c in M.load_chapters(ctx)
    }
    ctx.write_json("audio/manifest.json", manifest)


def run_duration_check(ctx: Any, monkeypatch: pytest.MonkeyPatch, *, add: int = 20):
    """Run the stage with the pacing module as the voice and an expansion that adds `add` words."""
    from ns_studio.media import master as M

    calls: list[tuple[int, int]] = []

    def expand(c: Any, n: int, add_words: int) -> None:
        calls.append((n, add_words))
        story = c.read_json("story.json")
        for ch in story["chapters"]:
            if ch["n"] == n:
                ch["text"] += " " + text_of(add)
        c.write_json("story.json", story)

    monkeypatch.setattr(M, "run_voice_master", fake_voice)
    monkeypatch.setattr(M, "load_expand_chapter", lambda: expand)
    fake_voice(ctx)
    M.DurationCheckStage().run(ctx)
    return calls


def test_a_tiny_master_at_the_scale_the_model_writes_passes_the_duration_check(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = duration_ctx(tmp_home, tiny=True, chapters=[120, 120, 120, 120, 120])  # 4:20 of narration
    assert run_duration_check(ctx, monkeypatch) == []  # no expansion was needed
    doc = ctx.read_json("duration_check.json")
    assert doc["floor_s"] == 120.0 and doc["expansions"] == [] and doc["total_s"] > 240


def test_the_same_master_with_the_switch_off_is_expanded_and_then_stops_at_the_normal_floor(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = duration_ctx(tmp_home, tiny=False, chapters=[120, 120, 120, 120, 120])
    with pytest.raises(NsError) as ei:
        run_duration_check(ctx, monkeypatch)
    assert ei.value.code == "NS-E305"
    assert ei.value.detail.endswith("after 2 expansions (floor 540 s)")  # today's text, byte for byte
    assert ctx.read_json("duration_check.json")["floor_s"] == 540.0


def test_a_tiny_master_that_writes_far_less_still_fails_with_e305(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = duration_ctx(tmp_home, tiny=True, chapters=[20, 20, 20, 20, 20])  # a minute of narration
    with pytest.raises(NsError) as ei:
        run_duration_check(ctx, monkeypatch)
    assert ei.value.code == "NS-E305"
    assert "after 2 expansions" in ei.value.detail and "(floor 120 s, 2:00)" in ei.value.detail


def test_the_e305_text_does_not_promise_nine_minutes():
    known = describe("NS-E305")
    assert "9 minutes" not in known.message and "minimum" in known.message
    assert "detail" in known.hint and "9 minutes" in known.hint and "tiny_story" in known.hint


def test_an_owners_own_script_keeps_the_normal_floor_on_a_tiny_install(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = duration_ctx(tmp_home, tiny=True, chapters=[120, 120, 120, 120, 120], owner_script=True)
    with pytest.raises(NsError) as ei:
        run_duration_check(ctx, monkeypatch)
    assert ei.value.code == "NS-E305" and "script too short" in ei.value.detail
    assert ei.value.detail.endswith("the floor is 540 s: script too short")


# ============================================================================= QC's duration window
def test_a_tiny_masters_qc_window_is_the_profiles_and_the_normal_one_is_todays():
    from ns_studio.media.qc import duration_check

    sp = profiles()
    for d, ok in ((110.0, True), (300.0, True), (510.0, True), (109.0, False), (901.0, False)):
        assert duration_check(d, "long", None, profile=sp.TINY).passed is ok, d
    assert duration_check(110.0, "long", None, profile=sp.TINY).limit == "110-900 s"
    for d, ok in ((510.0, True), (900.0, True), (509.0, False), (901.0, False), (300.0, False)):
        assert duration_check(d, "long", None).passed is ok, d  # no profile: today
        assert duration_check(d, "long", None, profile=sp.NORMAL).passed is ok, d
    assert duration_check(600.0, "long", None).limit == "510-900 s"


def test_a_tiny_job_planned_on_0_1_14_and_resumed_still_passes_qc_and_the_final_gate(tmp_home: Path):
    """A 0.1.14 tiny job was planned 9:00 in 7 chapters with a 520 s floor, so its voiced master is 520 to
    560 s. Resumed on this version it must not fail QC's duration window (a 510 s ceiling failed all three).
    """
    from ns_studio.media.qc import duration_check
    from ns_studio.policy import final_checks as fc

    sp = profiles()
    for d in (520.0, 540.0, 560.0):
        got = duration_check(d, "long", None, profile=sp.TINY)
        assert got.passed, (d, got.limit)
    ctx = make_ctx(tmp_home, fmt="master_first")
    ctx.runtime = tiny_runtime(ctx.runtime)
    ctx.file("long").mkdir(parents=True, exist_ok=True)
    ctx.file("long/master.mp4").write_bytes(b"x")
    assert fc.LongRuntime(lambda p: 540.0).run(ctx).passed


def test_a_series_parts_own_ceiling_still_wins_on_a_tiny_install():
    from ns_studio.media.qc import duration_check, series_max_s

    sp = profiles()
    assert duration_check(14 * 60.0, "long", None, series_max_s(15), profile=sp.TINY).passed  # 15 min part
    assert not duration_check(18 * 60.0, "long", None, series_max_s(15), profile=sp.TINY).passed
    assert not duration_check(100.0, "long", None, series_max_s(15), profile=sp.TINY).passed  # the floor


def test_a_tiny_shorts_qc_window_is_the_text_band_and_the_normal_one_is_todays():
    from ns_studio.media.qc import duration_check

    sp = profiles()
    for d, ok in ((17.5, True), (17.4, False), (80.0, True), (80.1, False)):
        assert duration_check(d, "short", 50, profile=sp.TINY).passed is ok, d
    assert not duration_check(
        180.0, "short", 175, profile=sp.TINY
    ).passed  # the 179 s cap is a platform limit
    assert duration_check(120.0, "short", None, profile=sp.TINY).passed  # no bucket: only the cap
    for d, ok in ((42.6, True), (42.4, False), (57.4, True), (57.6, False)):
        assert duration_check(d, "short", 50).passed is ok, d  # today, with no profile and with NORMAL
        assert duration_check(d, "short", 50, profile=sp.NORMAL).passed is ok, d
    assert duration_check(50.0, "short", 50).limit == "42.5-57.5 s (bucket 50 +-15 %), <= 179 s"


@pytest.mark.parametrize("bucket", [35, 50, 75])
@pytest.mark.parametrize("pacing", ["brisk", "standard", "measured"])
def test_a_short_the_tiny_text_stage_accepts_is_a_short_tiny_qc_accepts(bucket: int, pacing: str):
    """The text band (0.35 to 1.6 of the words) and the QC band (the same shares of the bucket) agree: a story
    either edge of the text band, voiced with its hook, lands inside the QC window of its bucket."""
    from ns_common.pacing import PACING_WPM
    from ns_studio.media.qc import duration_check
    from ns_studio.text.budget import short_budget

    sp = profiles()
    wpm = PACING_WPM[pacing]
    b = short_budget(bucket, pacing, hook_words=12, profile=sp.TINY)
    for words in (b.min_words, b.max_words):
        seconds = (words + 12) * (60.0 / wpm + PAUSE_PER_WORD_S)  # the story and its 12 word hook
        assert duration_check(seconds, "short", bucket, profile=sp.TINY).passed, (words, seconds)
    far_less = b.min_words // 3  # a third of the least the text stage accepts: both stages say no
    assert far_less + 12 < b.min_words
    assert not duration_check(
        (far_less + 12) * (60.0 / wpm + PAUSE_PER_WORD_S) * 0.5, "short", bucket, profile=sp.TINY
    ).passed


# --------------------------------------------------------- every other QC check still fails under the profile
def patch_media(
    monkeypatch: pytest.MonkeyPatch,
    *,
    seconds: float,
    lufs: float = -14.0,
    tp: float = -2.0,
    black: int = 0,
    size: tuple[int, int] = (1080, 1920),
    opens: bool = True,
    onset_ok: bool = True,
) -> None:
    """Replace what QC reads from a video file, so a dummy file stands in for the video (no FFmpeg)."""
    from ns_studio.media import qc as Q
    from ns_studio.media.audio import Loudness

    monkeypatch.setattr(Q.ff, "duration_s", lambda _p: seconds)
    monkeypatch.setattr(Q, "measure_loudness", lambda _p: Loudness(lufs, tp))
    monkeypatch.setattr(Q.ff, "video_size", lambda _p: size)
    monkeypatch.setattr(
        Q, "black_check", lambda _p: QcCheck(passed=black == 0, value=black, limit="0 intervals")
    )
    monkeypatch.setattr(
        Q,
        "opens_check",
        lambda _p: QcCheck(passed=opens, value="1 video, 1 audio, faststart=True", limit="x"),
    )
    monkeypatch.setattr(Q, "hook_check", lambda _p, _c: QcCheck(passed=onset_ok, value="x", limit="x"))


DEFECTS: dict[str, tuple[dict[str, Any], dict[str, Any], str]] = {
    # name: (media patch, run_qc arguments, the one check that fails)
    "loudness": ({"lufs": -20.0}, {}, "loudness"),
    "true_peak": ({"tp": -0.2}, {}, "loudness"),
    "drift": ({}, {"drift_ms": 500.0}, "caption_drift_ms"),
    "black_frames": ({"black": 2}, {}, "black_frames"),
    "resolution": ({"size": (1280, 720)}, {}, "aspect"),
    "unreadable": ({"opens": False}, {}, "file_opens"),
    "hook": ({"onset_ok": False}, {}, "hook_first_1500ms"),
}


@pytest.mark.parametrize("tiny", [True, False])
@pytest.mark.parametrize("defect", sorted(DEFECTS))
def test_every_check_but_the_duration_fails_the_same_way_with_the_profile_on_or_off(
    defect: str, tiny: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio.media.qc import run_qc

    sp = profiles()
    profile = sp.TINY if tiny else sp.NORMAL
    media, args, failing = DEFECTS[defect]
    video = tmp_path / "short.mp4"
    video.write_bytes(b"x")
    good = {"drift_ms": 20.0, "first_caption_s": 0.0}
    seconds = 50.0  # inside both profiles' window for bucket 50
    patch_media(monkeypatch, seconds=seconds, **media)
    res = run_qc("s1", video, "short", bucket_s=50.0, profile=profile, **{**good, **args})
    assert not res.passed and res.reason == failing, res
    patch_media(monkeypatch, seconds=seconds)
    assert run_qc("s1", video, "short", bucket_s=50.0, profile=profile, **good).passed


@pytest.mark.parametrize("tiny", [True, False])
def test_the_duration_check_alone_fails_a_video_outside_the_window(
    tiny: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio.media.qc import run_qc

    sp = profiles()
    profile = sp.TINY if tiny else sp.NORMAL
    video = tmp_path / "long.mp4"
    video.write_bytes(b"x")
    patch_media(
        monkeypatch, seconds=300.0, size=(1920, 1080)
    )  # 5:00: inside the tiny window, under QC's 8:30
    res = run_qc("long", video, "long", drift_ms=10.0, profile=profile)
    assert res.passed is tiny, res
    assert res.reason == (None if tiny else "duration")
    patch_media(monkeypatch, seconds=60.0, size=(1920, 1080))  # a minute: under every window
    assert run_qc("long", video, "long", drift_ms=10.0, profile=profile).reason == "duration"


# -------------------------------------------------------------------- QC stages: thresholds and markers
def qc_job(tmp_home: Path, *, tiny: bool, owner_script: bool = False, fmt: str = "shorts_only"):
    ctx = make_ctx(
        tmp_home,
        fmt=fmt,
        channel=make_channel(format="master_first" if fmt == "master_first" else "shorts_only"),
        combo=make_combo(length_bucket=50),
    )
    if tiny:
        ctx.runtime = tiny_runtime(ctx.runtime)
    if owner_script:
        ctx.write_json("script_source.json", {"kind": "script_import", "owner_script": True})
    return ctx


def write_short_report(ctx: Any, tmp_path: Path) -> None:
    video = tmp_path / "short.mp4"
    video.write_bytes(b"x")
    ctx.write_json("render/report.json", {"s1": {"path": str(video), "duration_s": 20.0}})
    ctx.write_json("captions/report.json", {"s1": {"drift_ms": 20.0, "first_caption_s": 0.0}})


def test_the_shorts_qc_stage_uses_the_profile_and_marks_its_reports(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio.media.qc import QcStage

    ctx = qc_job(tmp_home, tiny=True)
    write_short_report(ctx, tmp_path)
    patch_media(monkeypatch, seconds=20.0)  # 0.4 of a 50 s bucket: NS-E404 today, a pass when tiny
    QcStage().run(ctx)
    assert ctx.read_json("qc/report.json") == {
        "s1": {"passed": True, "reason": None, "profile": "tiny_story"}
    }
    per_item = ctx.read_json("qc/s1.json")
    assert per_item["profile"] == "tiny_story" and per_item["passed"] is True
    assert QcResult.model_validate(per_item).profile == "tiny_story"


def test_the_shorts_qc_stage_with_the_switch_off_is_todays_and_writes_no_marker(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio.media.qc import QcStage

    ctx = qc_job(tmp_home, tiny=False)
    write_short_report(ctx, tmp_path)
    patch_media(monkeypatch, seconds=20.0)
    with pytest.raises(NsError) as ei:
        QcStage().run(ctx)
    assert ei.value.code == "NS-E404" and "duration" in ei.value.detail  # the same code as today
    assert ctx.read_json("qc/report.json") == {"s1": {"passed": False, "reason": "duration"}}
    assert "profile" not in ctx.read_json("qc/s1.json")
    assert "profile" not in ctx.file("qc/s1.json").read_text(encoding="utf-8")


def test_a_tiny_short_far_below_its_bucket_still_stops_at_qc_with_e404(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio.media.qc import QcStage

    ctx = qc_job(tmp_home, tiny=True)
    write_short_report(ctx, tmp_path)
    patch_media(monkeypatch, seconds=8.0)  # 0.16 of the bucket: under the tiny window as well
    with pytest.raises(NsError) as ei:
        QcStage().run(ctx)
    assert ei.value.code == "NS-E404"
    assert ctx.read_json("qc/report.json")["s1"] == {
        "passed": False,
        "reason": "duration",
        "profile": "tiny_story",
    }


def master_qc_files(ctx: Any, tmp_path: Path, *, total_s: float) -> None:
    master = tmp_path / "master.mp4"
    master.write_bytes(b"x")
    short = tmp_path / "short.mp4"
    short.write_bytes(b"x")
    ctx.write_json("captions/report.json", {"long": {"drift_ms": 20.0, "first_caption_s": 0.0}})
    ctx.write_json("render/long.json", {"path": str(master), "duration_s": total_s})
    starts = [0.0, total_s / 3, 2 * total_s / 3]
    ends = [*starts[1:], total_s]
    ctx.write_json(
        "long/chapters.json",
        {
            "total_s": total_s,
            "midroll_suggestions_s": [],
            "chapters": [
                {
                    "n": i + 1,
                    "timestamp": "00:00" if i == 0 else f"{int(s // 60):02d}:{int(s % 60):02d}",
                    "title": f"c{i}",
                    "start_s": s,
                    "end_s": e,
                }
                for i, (s, e) in enumerate(zip(starts, ends, strict=True))
            ],
        },
    )
    ctx.write_json(
        "render/report.json",
        {"s1": {"path": str(short), "duration_s": 20.0, "drift_ms": 20.0, "first_caption_s": 0.0}},
    )
    ctx.write_json(
        "shorts_plan.json",
        [
            {
                "k": 1,
                "chapter": 1,
                "sentence_start": 0,
                "sentence_end": 1,
                "hook": "h",
                "closing_line": "c",
                "length_bucket": 50,
            }
        ],
    )


def test_the_master_qc_stage_uses_the_profile_for_the_master_and_its_shorts_and_marks_both(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio.media.qc import QcStage

    ctx = qc_job(tmp_home, tiny=True, fmt="master_first")
    master_qc_files(ctx, tmp_path, total_s=200.0)  # 3:20 master, a 20 s Short in a 50 s bucket
    patch_media(monkeypatch, seconds=20.0, size=(1080, 1920))
    from ns_studio.media import qc as Q

    sizes = {"master.mp4": (1920, 1080), "short.mp4": (1080, 1920)}
    monkeypatch.setattr(Q.ff, "video_size", lambda p: sizes[Path(p).name])
    monkeypatch.setattr(Q.ff, "duration_s", lambda p: 200.0 if Path(p).name == "master.mp4" else 20.0)
    QcStage().run(ctx)
    report = ctx.read_json("qc/report.json")
    assert report == {
        "long": {"passed": True, "reason": None, "profile": "tiny_story"},
        "s1": {"passed": True, "reason": None, "profile": "tiny_story"},
    }
    assert ctx.read_json("qc/long.json")["profile"] == "tiny_story"


def test_the_master_qc_stage_with_the_switch_off_stops_a_three_minute_master_as_today(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio.media import qc as Q
    from ns_studio.media.qc import QcStage

    ctx = qc_job(tmp_home, tiny=False, fmt="master_first")
    master_qc_files(ctx, tmp_path, total_s=200.0)
    patch_media(monkeypatch, seconds=20.0)
    monkeypatch.setattr(
        Q.ff, "video_size", lambda p: (1920, 1080) if Path(p).name == "master.mp4" else (1080, 1920)
    )
    monkeypatch.setattr(Q.ff, "duration_s", lambda p: 200.0 if Path(p).name == "master.mp4" else 50.0)
    with pytest.raises(NsError) as ei:
        QcStage().run(ctx)
    assert ei.value.code == "NS-E404" and "long: duration" in ei.value.detail
    assert ctx.read_json("qc/report.json")["long"] == {"passed": False, "reason": "duration"}


def test_an_owners_own_script_gets_the_normal_qc_window_but_the_output_is_still_marked(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Length of an owner's script is not a model-size problem (as the floor); the install's videos are
    still test videos (fake voice and footage), so the marker stays."""
    from ns_studio.media.qc import QcStage

    ctx = qc_job(tmp_home, tiny=True, owner_script=True)
    write_short_report(ctx, tmp_path)
    patch_media(monkeypatch, seconds=20.0)
    with pytest.raises(NsError) as ei:
        QcStage().run(ctx)
    assert ei.value.code == "NS-E404"
    assert ctx.read_json("qc/report.json")["s1"] == {
        "passed": False,
        "reason": "duration",
        "profile": "tiny_story",
    }


def test_clip_qc_ignores_the_profile_and_marks_nothing(
    tmp_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A clip is a cut of the owner's own video, not a model's story: the profile must not leak into it."""
    from ns_studio.clipper.stages_clip import QcStage as ClipQc

    ctx = qc_job(tmp_home, tiny=True, fmt="clip")
    write_short_report(ctx, tmp_path)
    ctx.write_json("render/report.json", {"c1": {"path": str(tmp_path / "short.mp4"), "duration_s": 20.0}})
    ctx.write_json("captions/report.json", {"c1": {"drift_ms": 20.0, "first_caption_s": 0.0}})
    patch_media(monkeypatch, seconds=20.0)
    ClipQc().run(ctx)
    assert ctx.read_json("qc/report.json") == {"c1": {"passed": True, "reason": None}}
    assert "profile" not in ctx.read_json("qc/c1.json")


# ===================================================================== the policy gate's runtime check
def test_the_final_gates_runtime_check_scales_with_the_profile_and_keeps_its_midroll_rule(tmp_home: Path):
    from ns_studio.policy.final_checks import LongRuntime

    sp = profiles()
    for tiny, minutes_s, ok in ((True, 200.0, True), (True, 100.0, False), (False, 200.0, False)):
        ctx = make_ctx(tmp_home, fmt="master_first")
        if tiny:
            ctx.runtime = tiny_runtime(ctx.runtime)
        ctx.file("long").mkdir(parents=True, exist_ok=True)
        ctx.file("long/master.mp4").write_bytes(b"x")
        res = LongRuntime(duration_fn=lambda _p, s=minutes_s: s).run(ctx)
        assert res.passed is ok, (tiny, minutes_s, res.detail)
    ctx = make_ctx(tmp_home, fmt="master_first")
    ctx.runtime = tiny_runtime(ctx.runtime)
    ctx.file("long").mkdir(parents=True, exist_ok=True)
    ctx.file("long/master.mp4").write_bytes(b"x")
    ctx.write_json("long/chapters.json", {"midroll_suggestions_s": [60.0]})  # inside the first two minutes
    res = LongRuntime(duration_fn=lambda _p: 400.0).run(ctx)
    assert not res.passed and "first or last 2 minutes" in res.detail
    assert sp.TINY.master_qc_s[0] <= 200.0


# ================================================================ render report and the sticky job mark
def test_the_render_reports_carry_the_marker_only_on_a_tiny_job(tmp_home: Path):
    sp = profiles()
    report = {"s1": {"path": "a.mp4", "duration_s": 1.0}, "s2": {"path": "b.mp4", "duration_s": 2.0}}
    tiny = make_ctx(tmp_home)
    tiny.runtime = tiny_runtime(tiny.runtime)
    stamped = sp.with_mark(tiny, report)
    assert all(v["profile"] == "tiny_story" for v in stamped.values())
    assert (
        stamped["s1"]["path"] == "a.mp4" and "profile" not in report["s1"]
    )  # a copy: the input is untouched
    assert sp.entry_with_mark(tiny, {"path": "m.mp4"}) == {"path": "m.mp4", "profile": "tiny_story"}
    normal = make_ctx(tmp_home)
    assert sp.with_mark(normal, report) == report and "profile" not in sp.entry_with_mark(
        normal, {"path": "m"}
    )


def test_a_job_that_ran_tiny_stays_marked_when_the_switch_is_turned_off_and_it_resumes(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """The resume route: started with the switch on, resumed after it was turned off. Thresholds follow the
    install (so the strict ones apply), but the video is still a test video and says so."""
    from ns_studio import orchestrator as orch

    sp = profiles()
    monkeypatch.setattr(orch, "plan_job", lambda *_plan: [])
    ctx = make_ctx(tmp_home, fmt="master_first")
    ctx.runtime = tiny_runtime(ctx.runtime)
    assert sp.job_mark(ctx) == "tiny_story"  # the live switch
    orch.run_job(ctx)  # the first run notes the profile in the job folder
    assert json.loads(ctx.file("profile.json").read_text(encoding="utf-8")) == {"profile": "tiny_story"}
    ctx.runtime = Runtime.default("cpu-dev")  # the owner turned the switch off
    assert sp.profile_of(ctx) is sp.NORMAL and sp.duration_profile(ctx) is sp.NORMAL  # strict numbers
    assert sp.job_mark(ctx) == "tiny_story"  # and the output is still marked
    orch.run_job(ctx)  # a resume under the normal profile keeps the note
    assert sp.job_mark(ctx) == "tiny_story"


def test_a_job_started_normal_and_resumed_with_the_switch_on_is_marked_from_then_on(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio import orchestrator as orch

    sp = profiles()
    monkeypatch.setattr(orch, "plan_job", lambda *_plan: [])
    ctx = make_ctx(tmp_home, fmt="master_first")
    orch.run_job(ctx)
    assert not ctx.file("profile.json").exists() and sp.job_mark(ctx) is None
    ctx.runtime = tiny_runtime(ctx.runtime)
    orch.run_job(ctx)
    assert sp.job_mark(ctx) == "tiny_story" and ctx.file("profile.json").exists()


def test_a_normal_job_gets_no_note_and_a_damaged_note_is_not_a_marker(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio import orchestrator as orch

    sp = profiles()
    monkeypatch.setattr(orch, "plan_job", lambda *_plan: [])
    ctx = make_ctx(tmp_home, fmt="master_first")
    orch.run_job(ctx)
    assert not ctx.file("profile.json").exists() and sp.job_mark(ctx) is None
    ctx.file("profile.json").write_text("not json", encoding="utf-8")
    assert sp.job_mark(ctx) is None
    ctx.file("profile.json").write_text('{"profile": "something else"}', encoding="utf-8")
    assert sp.job_mark(ctx) is None


def test_a_clip_job_is_never_noted_or_marked_on_a_tiny_install(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    from ns_studio import orchestrator as orch

    sp = profiles()
    monkeypatch.setattr(orch, "plan_job", lambda *_plan: [])
    ctx = make_ctx(tmp_home, fmt="clip")
    ctx.runtime = tiny_runtime(ctx.runtime)
    orch.run_job(ctx)
    assert not ctx.file("profile.json").exists()
    assert sp.job_mark(ctx) is None  # the live switch alone must not mark a clip


def test_a_series_part_job_is_noted_when_its_folder_is_made_on_a_tiny_install(tmp_home: Path):
    from ns_studio.series.workshop import ProduceRequest
    from tests.test_tiny_story import workshop_with

    for tiny in (True, False):
        ws, slug = workshop_with(tmp_home, tiny=tiny, overrides={})
        job_id = f"{slug}-20261109-{'aaaa' if tiny else 'bbbb'}"
        req = ProduceRequest(
            job_id=job_id,
            channel="after-dark",
            fmt="master_first",
            plan="master_from_script",
            story_slug=slug,
            part_n=1,
            parts_total=3,
            series_title="S",
            series_slug="s",
            title="S - Part 01",
            short_titles=[],
            script="x",
            shorts=[],
            concept="c",
            design={},
            end_card="e",
            publish_not_before_days=0,
        )
        ws._prepare_job(req)
        note = ws.paths.job_dir(job_id) / "profile.json"
        assert note.exists() is tiny


# ============================================================================ the outbox, sidecar and rows
SEO = {
    "title": "Scary Story: The Doorbell Camera",
    "description": "A camera records someone at 3:12.\nOriginal fiction.",
    "tags": [f"tag{i}" for i in range(10)],
    "hashtags": ["#Shorts", "#horror", "#scarystory"],
    "keywords": ["scary story"],
}


def outbox_job(tmp_home: Path, tmp_path: Path, *, tiny: bool):
    from tests.test_outbox import _prepare

    ctx = make_ctx(tmp_home)
    if tiny:
        ctx.runtime = tiny_runtime(ctx.runtime)
    _prepare(ctx, tmp_path)
    return ctx


def short_files(ctx: Any) -> tuple[Path, Path, Path]:
    d = ctx.paths.outbox / "after-dark" / "stories" / "Scary Story The Doorbell Camera" / "shorts"
    stem = d / "Scary Story The Doorbell Camera - Short 01"
    return stem.with_suffix(".mp4"), stem.with_suffix(".json"), stem.with_suffix(".txt")


def test_a_tiny_outputs_sidecar_sheet_and_row_say_test_output(tmp_home: Path, tmp_path: Path):
    from ns_studio.media.outbox import OutboxStage

    ctx = outbox_job(tmp_home, tmp_path, tiny=True)
    OutboxStage().run(ctx)
    mp4, side, sheet = short_files(ctx)
    meta = json.loads(side.read_text(encoding="utf-8"))
    assert meta["profile"] == "tiny_story" and Metadata.model_validate(meta).profile == "tiny_story"
    text = sheet.read_text(encoding="utf-8")
    assert (
        text.startswith("TEST OUTPUT (tiny_story)") and "FILE: " in text and "do not upload" in text.lower()
    )
    row = ctx.db.one("select * from outputs where output_id=?", (f"{ctx.job_id}/s1",))
    assert row["profile"] == "tiny_story" and row["status"] == "pending"
    assert mp4.exists()


def test_a_normal_outputs_sidecar_sheet_and_row_are_todays(tmp_home: Path, tmp_path: Path):
    from ns_studio.media.outbox import OutboxStage

    ctx = outbox_job(tmp_home, tmp_path, tiny=False)
    OutboxStage().run(ctx)
    _mp4, side, sheet = short_files(ctx)
    text = side.read_text(encoding="utf-8")
    assert "profile" not in text and set(json.loads(text)) == set(Metadata.model_fields) - {"profile"}
    assert sheet.read_text(encoding="utf-8").startswith("FILE: ")
    row = ctx.db.one("select * from outputs where output_id=?", (f"{ctx.job_id}/s1",))
    assert row["profile"] is None


def test_a_rerun_under_a_normal_profile_keeps_a_tiny_outputs_marker(tmp_home: Path, tmp_path: Path):
    from ns_studio.media.outbox import OutboxStage

    ctx = outbox_job(tmp_home, tmp_path, tiny=True)
    OutboxStage().run(ctx)
    ctx.write_json("profile.json", {"profile": "tiny_story"})  # what the first run of the job noted
    ctx.runtime = Runtime.default("cpu-dev")
    OutboxStage().run(ctx)
    row = ctx.db.one("select profile from outputs where output_id=?", (f"{ctx.job_id}/s1",))
    assert row["profile"] == "tiny_story"
    assert json.loads(short_files(ctx)[1].read_text(encoding="utf-8"))["profile"] == "tiny_story"


def test_the_master_first_outbox_marks_the_master_and_its_shorts(tmp_home: Path, tmp_path: Path):
    from ns_studio.media.outbox import run_outbox_master

    ctx = make_ctx(tmp_home, fmt="master_first", channel=make_channel(format="master_first"))
    ctx.runtime = tiny_runtime(ctx.runtime)
    master = tmp_path / "master.mp4"
    master.write_bytes(b"x" * 64)
    short = tmp_path / "short.mp4"
    short.write_bytes(b"x" * 64)
    ctx.write_json(
        "long/chapters.json",
        {
            "total_s": 200.0,
            "midroll_suggestions_s": [],
            "chapters": [
                {"timestamp": "00:00", "title": "One", "start_s": 0.0},
                {"timestamp": "01:10", "title": "Two", "start_s": 70.0},
                {"timestamp": "02:20", "title": "Three", "start_s": 140.0},
            ],
        },
    )
    ctx.write_json("render/long.json", {"path": str(master), "duration_s": 200.0})
    ctx.write_json("clips/shots.json", {"long": {"shots": []}})
    ctx.write_json("seo_long.json", {**SEO, "title": "The Long Night"})
    ctx.write_json("render/report.json", {"s1": {"path": str(short), "duration_s": 30.0}})
    ctx.write_json("clips/shorts_shots.json", {"s1": {"shots": []}})
    ctx.write_json("seo_shorts.json", [SEO])
    ctx.write_json(
        "shorts_plan.json",
        [
            {
                "k": 1,
                "chapter": 1,
                "sentence_start": 0,
                "sentence_end": 1,
                "hook": "h",
                "closing_line": "c",
            }
        ],
    )
    run_outbox_master(ctx)
    manifest = ctx.read_json("outbox/manifest.json")
    for item in ("long", "s1"):
        meta = json.loads(Path(manifest[item]["json"]).read_text(encoding="utf-8"))
        assert meta["profile"] == "tiny_story", item
        assert Path(manifest[item]["txt"]).read_text(encoding="utf-8").startswith("TEST OUTPUT (tiny_story)")
        row = ctx.db.one("select profile from outputs where output_id=?", (f"{ctx.job_id}/{item}",))
        assert row["profile"] == "tiny_story"


@needs_ffmpeg
def test_a_clip_job_on_a_tiny_install_ends_with_unmarked_rows_and_sidecars(tmp_home: Path, tmp_path: Path):
    """A clip is a cut of the owner's own video: the profile must not leak into it (CI only: it renders)."""
    import shutil

    from ns_studio import orchestrator
    from ns_studio.clipper import stages_clip as sc
    from ns_studio.clipper.models import ClipParams
    from ns_studio.context import FAKEABLE
    from tests.fixtures.clipper.make_sample import shared_sample
    from tests.test_clipper_stages import LLM_FIXTURES

    vod = tmp_path / "vod"
    vod.mkdir()
    shutil.copy2(shared_sample().video, vod / "sample.mp4")
    shutil.copy2(shared_sample().transcript, vod / "sample.transcript.json")
    params = ClipParams(source=str(vod / "sample.mp4"), channel="after-dark", rights_confirmed=True)
    from ns_common.paths import Paths

    ctx = sc.create_clip_job(
        Paths.from_env(),
        make_channel(),
        params,
        fake=FAKEABLE,
        llm_fixtures=LLM_FIXTURES,
        runtime=tiny_runtime(),
    )
    orchestrator.run_job(ctx)
    assert not ctx.file("profile.json").exists()
    assert all("profile" not in v for v in ctx.read_json("qc/report.json").values())
    assert all("profile" not in v for v in ctx.read_json("render/report.json").values())
    rows = ctx.db.query("SELECT profile, path FROM outputs WHERE job_id=?", (ctx.job_id,))
    assert rows and all(r["profile"] is None for r in rows)
    for r in rows:
        assert "profile" not in Path(r["path"]).with_suffix(".json").read_text(encoding="utf-8")


def test_old_sidecars_and_qc_results_without_the_marker_still_load():
    old_meta = {
        "channel": "after-dark",
        "job_id": "after-dark-20261101-abcd",
        "kind": "short",
        "k": 1,
        "title": "t",
        "description": "d",
        "tags": [],
        "hashtags": [],
        "publish_after": "2026-11-02T18:00",
        "ai_disclosure": "no",
        "made_for_kids": False,
        "age_restrict_suggested": False,
        "clips": [],
        "combo_id": "c",
        "prompt_versions": {},
    }
    assert Metadata.model_validate(old_meta).profile is None
    assert "profile" not in json.loads(Metadata.model_validate(old_meta).model_dump_json())
    old_qc = {
        "item": "s1",
        "passed": True,
        "checks": {"duration": {"passed": True, "value": 1.0, "limit": "x"}},
    }
    assert QcResult.model_validate(old_qc).profile is None
    assert "profile" not in QcResult.model_validate(old_qc).model_dump(mode="json")
    with pytest.raises(ValueError):  # the marker is the one word, not free text
        Metadata.model_validate({**old_meta, "profile": "something"})


def test_a_database_made_before_the_column_gets_it_and_keeps_its_rows(tmp_path: Path):
    from ns_common.db import Db

    db_path = tmp_path / "studio.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "create table outputs (output_id text primary key, job_id text not null, channel text not null, "
        "kind text not null, k integer, title text not null, path text not null, phash text, "
        "duration_s real, "
        "combo_id text, status text not null default 'pending', review_note text, source_id text, "
        "created_at text not null)"
    )
    con.execute(
        "insert into outputs(output_id, job_id, channel, kind, title, path, created_at) "
        "values ('j/s1','j','c','short','t','p','2026-11-01')"
    )
    con.commit()
    con.close()
    db = Db(db_path)
    db.migrate()
    db.migrate()  # twice: the column is added once
    row = db.one("select * from outputs where output_id='j/s1'")
    assert row is not None and row["profile"] is None and row["title"] == "t"


# ================================================================== the publish helpers refuse a test output
def tiny_row(db: Any, job_id: str = "after-dark-20261101-abcd", status: str = "approved") -> str:
    oid = add_output(db, job_id, "s1", status=status)
    db.execute("update outputs set profile='tiny_story' where output_id=?", (oid,))
    return oid


def test_the_error_code_for_a_test_output_exists():
    known = describe("NS-E329")
    assert "test output" in known.message.lower() and "tiny_story" in known.hint


def test_mark_uploaded_refuses_a_tiny_output_with_the_code_and_changes_nothing(tmp_home: Path):
    from ns_common.db import Db
    from ns_common.paths import Paths
    from ns_studio import review
    from ns_studio.errors import StudioError

    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    tiny = tiny_row(db)
    with pytest.raises(StudioError) as ei:
        review.mark_uploaded(db, "after-dark-20261101-abcd", "s1", "https://youtu.be/abcdefghijk")
    assert ei.value.code == "NS-E329" and "NS-E329" in str(ei.value)
    assert db.one("select status from outputs where output_id=?", (tiny,))["status"] == "approved"
    assert db.one("select count(*) c from uploads")["c"] == 0
    normal = add_output(db, "after-dark-20261101-beef", "s1", status="approved")
    assert (
        review.mark_uploaded(db, "after-dark-20261101-beef", "s1", "https://youtu.be/abcdefghijk") == normal
    )


def test_the_cli_mark_uploaded_prints_the_coded_refusal(tmp_home: Path):
    from typer.testing import CliRunner

    from ns_common.db import Db
    from ns_common.paths import Paths
    from ns_studio.cli import app

    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    tiny_row(db)
    res = CliRunner().invoke(
        app, ["mark-uploaded", "after-dark-20261101-abcd", "s1", "https://youtu.be/abcdefghijk"]
    )
    assert res.exit_code == 1 and "NS-E329" in res.output
    shown = CliRunner().invoke(app, ["review", "show", "after-dark-20261101-abcd/s1"])
    assert shown.exit_code == 0 and "TEST OUTPUT (tiny_story)" in shown.output


def test_the_dashboards_posted_route_refuses_a_tiny_output_and_says_which_it_is(tmp_home: Path):
    from ns_dashboard.errors import ApiError
    from ns_dashboard.fakes import seed_channel, seed_output
    from tests.test_dashboard_handlers import make

    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)
    oid = seed_output(state.db, state.paths, kind="short", k=1, title="Tiny")
    state.db.execute("UPDATE outputs SET profile='tiny_story' WHERE output_id=?", (oid,))
    dash.outputs.approve(oid)  # a reviewer may approve a test output (the checklist is for real videos)
    with pytest.raises(ApiError) as ei:
        dash.outputs.mark_uploaded(oid, "https://youtu.be/abcdefghijk")
    assert ei.value.status == 409 and ei.value.code == "NS-E329"
    assert "test output" in ei.value.message.lower() and ei.value.hint
    assert state.db.one("select status from outputs where output_id=?", (oid,))["status"] == "approved"
    assert state.db.one("select count(*) c from uploads")["c"] == 0
    real = seed_output(state.db, state.paths, kind="short", k=2, title="Real")
    dash.outputs.approve(real)
    assert dash.outputs.mark_uploaded(real, "https://youtu.be/abcdefghijk")["status"] == "uploaded"


def test_analytics_rows_are_never_matched_by_title_to_a_test_output(tmp_home: Path):
    from ns_common.db import Db
    from ns_common.paths import Paths
    from ns_studio.learn.importer import Row, match_rows

    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    real = add_output(db, "after-dark-20261101-beef", "s1", title="The Doorbell")
    tiny = add_output(db, "after-dark-20261101-abcd", "s1", title="Doorbell Test")
    db.execute("update outputs set profile='tiny_story' where output_id=?", (tiny,))
    rows = [Row(video_id="", url="", title="The Doorbell"), Row(video_id="", url="", title="Doorbell Test")]
    matched, unmatched = match_rows(db, rows)
    assert list(matched) == [real] and [r.title for r in unmatched] == ["Doorbell Test"]


def test_the_queue_items_carry_the_profile_for_the_label(tmp_home: Path):
    from ns_dashboard.fakes import seed_channel, seed_output
    from tests.test_dashboard_handlers import make

    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)
    tiny = seed_output(state.db, state.paths, kind="short", k=1, title="Tiny")
    real = seed_output(state.db, state.paths, kind="short", k=2, title="Real")
    state.db.execute("UPDATE outputs SET profile='tiny_story' WHERE output_id=?", (tiny,))
    by_id = {o["output_id"]: o for o in dash.outputs.list_outputs()}
    assert by_id[tiny]["profile"] == "tiny_story" and by_id[real]["profile"] is None
    assert dash.outputs.detail(tiny)["profile"] == "tiny_story"


def test_an_approved_test_output_is_not_listed_as_a_video_waiting_for_its_link(tmp_home: Path):
    """The Results page lists approved videos with no link yet. A test output can never get one (NS-E329)."""
    from ns_dashboard.fakes import seed_channel, seed_output
    from tests.test_dashboard_handlers import make

    dash, state = make(tmp_home)
    seed_channel(state.db, state.paths)
    tiny = seed_output(state.db, state.paths, kind="short", k=1, title="Tiny", status="approved")
    real = seed_output(state.db, state.paths, kind="short", k=2, title="Real", status="approved")
    state.db.execute("UPDATE outputs SET profile='tiny_story' WHERE output_id=?", (tiny,))
    assert dash.settings._unmatched_uploads() == [real]


def test_the_queue_cards_label_a_test_output_and_do_not_offer_to_finish_posting_it():
    """The Posted card and the posting calendar say `Test output`; the card has no `Finish posting` for it."""
    root = Path(__file__).resolve().parent.parent / "ns_dashboard"
    src = (root / "ui" / "src" / "pages" / "Queue.tsx").read_text(encoding="utf-8")
    card = src[src.index("function ListView") : src.index("function Review")]
    assert "x.profile ?" in card and "Test output" in card
    assert card.index('x.status === "approved" && x.profile') < card.index("Finish posting")
    calendar = src[src.index("function PostingCalendar") : src.index("function RenderJobs")]
    assert "o.profile" in calendar and "Test output" in calendar
    built = (root / "static" / "app.js").read_text(encoding="utf-8")
    assert built.count("Test output") >= 3  # the built bundle was rebuilt from this source


def test_the_publish_scheduler_leaves_a_test_output_unscheduled_and_does_not_count_its_slot(tmp_home: Path):
    from ns_studio import daily, jobs
    from tests.test_cadence import NOW, _channel, _env, _finished_job

    paths, db = _env(tmp_home)
    _channel(paths, db, 9)
    env = jobs.open_env(light=True)
    job = "after-dark-20261120-abcd"
    manifest = _finished_job(paths, job, "The Long Night", 2, master=True)
    before = {}
    for item, entry in manifest.items():
        p = Path(entry["json"])
        data = json.loads(p.read_text(encoding="utf-8"))
        data["profile"] = "tiny_story"
        p.write_text(json.dumps(data), encoding="utf-8")
        before[item] = data["publish_after"]
        add_output(db, job, item, path=entry["mp4"])
        db.execute("update outputs set profile='tiny_story' where output_id=?", (f"{job}/{item}",))
    assert daily.apply_publish_times(env, [job], random.Random(3), NOW) == 0
    for item, entry in manifest.items():
        assert json.loads(Path(entry["json"]).read_text(encoding="utf-8"))["publish_after"] == before[item]
    today = NOW.astimezone(UTC).date()
    assert daily.scheduled_shorts(env, "after-dark", today, set(), datetime(2026, 11, 21, tzinfo=UTC)) == {}


# ============================================================================== the real run (CI only)
def long_chapter(prefix: str, sentences: int = 14) -> str:
    """`sentences` sentences of ten words (as `test_master_pipeline.chapter_text`, only longer)."""
    out = []
    for s in range(sentences):
        words = " ".join(f"{prefix}{chr(97 + s)}{chr(97 + i)}" for i in range(9))
        out.append(f"{words} end.")
    return " ".join(out)


@needs_ffmpeg
def test_a_tiny_master_runs_through_render_qc_and_the_outbox(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """A fake master at the tiny scale (three chapters of 140 words, about 3 minutes) goes through voice, the
    duration check, captions, render, Shorts, QC and the outbox on the profile's numbers, with nothing patched
    but the thumbnails; the same story with the switch off stops at the duration check (NS-E305).

    Only CI runs it: it spawns FFmpeg and the laptop lock is QA's."""
    from ns_studio.media import master as M
    from ns_studio.media import thumbnails as T
    from ns_studio.media.stages_media import MASTER_MEDIA_STAGES
    from ns_studio.orchestrator import FORMAT_STAGES, run_job
    from tests.test_master_pipeline import beat, fake_make_thumbnails

    names = [s.name for s in MASTER_MEDIA_STAGES]
    monkeypatch.setitem(FORMAT_STAGES, "master_first", names)
    monkeypatch.setattr(T, "make_thumbnails", fake_make_thumbnails)

    def make(tiny: bool):
        ctx = make_ctx(tmp_home, fmt="master_first", combo=make_combo(visual_treatment="cinematic_teal"))
        if tiny:
            ctx.runtime = tiny_runtime(ctx.runtime)
        ctx.write_json(
            "story.json",
            {
                "working_title": "The Long Night Story",
                "chapters": [
                    {"n": i, "title": f"Chapter title {i}", "text": long_chapter(chr(120 - i))}
                    for i in (1, 2, 3)
                ],
            },
        )
        ctx.write_json(
            "shorts_plan.json",
            [
                {
                    "k": 1,
                    "chapter": 1,
                    "sentence_start": 0,
                    "sentence_end": 3,
                    "hooks": ["Listen to this now."],
                    "chosen_hook": "Listen to this opening line now.",
                    "closing_line": "The full story is on the channel.",
                    "combo_id": "c-s1",
                    "why_standalone": "x",
                    "length_bucket": 35,
                },
            ],
        )
        ctx.write_json(
            "short_combos.json", {"1": make_combo(combo_id="c-s1", length_bucket=35).model_dump(mode="json")}
        )
        ctx.write_json(
            "visual_plan.json",
            [
                beat(1, 0, 0, 3, True),
                beat(1, 1, 4, 13, False),
                beat(2, 0, 0, 13, False),
                beat(3, 0, 0, 13, False),
            ],
        )
        ctx.write_json(
            "seo_long.json",
            {
                "title": "The Long Night Story",
                "description": "A drive.\n\n[[CHAPTERS]]\n\nOriginal fiction.",
                "tags": [f"tag{i}" for i in range(10)],
                "hashtags": ["#horror", "#story", "#shorts"],
                "keywords": [],
                "chapter_titles": [{"n": i, "title": f"Chapter title {i}"} for i in (1, 2, 3)],
            },
        )
        ctx.write_json(
            "seo_shorts.json",
            [
                {
                    "title": "Short 1: The Long Night",
                    "description": "A hook.\nOriginal fiction.",
                    "tags": [f"tag{i}" for i in range(10)],
                    "hashtags": ["#Shorts", "#horror", "#story"],
                    "keywords": [],
                },
            ],
        )
        return ctx

    ctx = make(tiny=True)
    assert run_job(ctx) == names
    doc = ctx.read_json("duration_check.json")
    assert doc["floor_s"] == 120.0 and doc["expansions"] == [] and 120.0 < doc["total_s"] < 300.0
    assert ctx.read_json("render/long.json")["profile"] == "tiny_story"
    assert all(v["profile"] == "tiny_story" for v in ctx.read_json("render/report.json").values())
    qc = ctx.read_json("qc/report.json")
    assert set(qc) == {"long", "s1"} and all(
        v["passed"] and v["profile"] == "tiny_story" for v in qc.values()
    ), qc
    manifest = ctx.read_json("outbox/manifest.json")
    assert json.loads(Path(manifest["long"]["json"]).read_text(encoding="utf-8"))["profile"] == "tiny_story"
    rows = ctx.db.query("select profile from outputs where job_id=?", (ctx.job_id,))
    assert [r["profile"] for r in rows] == ["tiny_story"] * 2

    off = make(tiny=False)
    monkeypatch.setattr(M, "load_expand_chapter", lambda: lambda _ctx, _n, _words: None)  # no model here
    with pytest.raises(NsError) as ei:
        run_job(off)
    assert ei.value.code == "NS-E305" and "(floor 540 s)" in ei.value.detail
