"""P02 x H-8: the master's word floor follows the voice's measured pace, and the story profile moves it
through the ONE place H-8 already uses (`master_budget` and its `master_floor_share`).

Profile off (NORMAL): the floor is the words of 9:00 (540 s) at the job's pace, pauses and cards included.
Profile on (TINY): the same planning, scaled by the profile's `master_floor_share`; no second constant.
`plan_meta.json` carries the floor from the plan stage to `enforce_word_floor`, which expands the shortest
chapter or refuses the story.
"""

from pathlib import Path

import pytest

from ns_common.errors import NsError
from ns_common.models import Runtime, Story
from ns_common.pacing import master_fixed_seconds, pacing_speed, words_for_seconds
from ns_common.tts_client import FakeTts, TtsItem, wav_seconds
from ns_studio.context import JobContext
from ns_studio.stages import REGISTRY
from ns_studio.text import stages_master as M
from ns_studio.text import stages_shorts as S
from ns_studio.text.story_profile import TINY
from ns_studio.voice_wpm import record_voice_wpm
from tests.helpers import make_channel, make_ctx

CHANNEL = {"format": "master_first", "long_minutes": (11, 11)}
SLOW = 120.0  # words per minute of the fake voice; the pacing table says 155
SENTENCE = "The doorbell camera pings at three twelve every night and I never look at the clips anymore."
TEXT = " ".join([SENTENCE] * 12)


def _tiny(ctx: JobContext) -> JobContext:
    ctx.runtime = Runtime.model_validate({**ctx.runtime.model_dump(mode="json"), "tiny_story": True})
    return ctx


def _ctx(tmp_home: Path, *, tiny: bool) -> JobContext:
    ctx = make_ctx(tmp_home, fmt="master_first", channel=make_channel(**CHANNEL))
    return _tiny(ctx) if tiny else ctx


def _measured(tmp_home: Path, *, tiny: bool) -> JobContext:
    """A job planned after another job measured the voice at `SLOW` words per minute."""
    first = make_ctx(tmp_home, fmt="master_first", channel=make_channel(**CHANNEL))
    audio = first.subdir("audio")
    items = [TtsItem("p1", TEXT, "Ryan", 1.0)]
    wav = audio / "p1.wav"
    FakeTts(wpm=SLOW).speak(TEXT, "Ryan", 1.0, wav)
    assert record_voice_wpm(first, "Ryan", items, {"p1": {"path": str(wav), "seconds": wav_seconds(wav)}})
    return _ctx(tmp_home, tiny=tiny)


def _plan(ctx: JobContext) -> dict:
    REGISTRY["ideas"].run(ctx)
    REGISTRY["long_plan"].run(ctx)
    return ctx.read_json("plan_meta.json")


# ---- the numbers ------------------------------------------------------------------------------------------


def test_profile_off_the_floor_is_the_words_of_nine_minutes_at_the_table_pace(tmp_home: Path):
    ctx = _ctx(tmp_home, tiny=False)
    mb = S.job_master_budget(ctx)
    assert mb.wpm == 155
    assert mb.min_words == words_for_seconds(540, 155, fixed_s=master_fixed_seconds(5)) == 1259
    assert _plan(ctx)["min_words"] == 1259  # the plan stage hands the same floor to enforce_word_floor


def test_profile_off_the_floor_is_the_words_of_nine_minutes_at_the_voices_measured_pace(tmp_home: Path):
    ctx = _measured(tmp_home, tiny=False)
    wpm = round(SLOW * pacing_speed(ctx.channel.voice.speed, "standard"))
    mb = S.job_master_budget(ctx)
    assert mb.wpm == wpm != 155
    floor = words_for_seconds(540, wpm, fixed_s=master_fixed_seconds(5))
    assert mb.min_words == floor < 1259
    meta = _plan(ctx)
    assert (meta["wpm"], meta["min_words"]) == (wpm, floor)


def test_profile_on_the_floor_is_the_profiles_share_of_the_target_at_the_voices_pace(tmp_home: Path):
    ctx = _measured(tmp_home, tiny=True)
    mb = S.job_master_budget(ctx)
    assert mb.wpm != 155
    assert (mb.target_minutes, mb.chapters) == (9, 7)
    assert TINY.master_floor_share is not None
    assert mb.min_words == round(mb.target_words * TINY.master_floor_share)
    nine_minutes = words_for_seconds(540, mb.wpm, fixed_s=master_fixed_seconds(5))
    assert mb.min_words < nine_minutes  # the profile's floor is under the 9:00 floor
    assert _plan(ctx)["min_words"] == mb.min_words


# ---- the decision -----------------------------------------------------------------------------------------


def _story_of(ctx: JobContext, words: int) -> int:
    """Write a 5-chapter `story.json` of at least `words` words (whole 5-word sentences) and return its
    true word count."""
    per = -(-words // 25) * 5  # words per chapter: whole sentences, rounded up
    chapters = [
        M.build_chapter(
            n,
            f"Chapter {n}",
            " ".join(["Someone waited by the door."] * (per // 5)),
            "x",
            wpm=155,
            is_last=n == 5,
        ).model_dump()
        for n in range(1, 6)
    ]
    story = Story.model_validate({"working_title": "T", "chapters": chapters})
    ctx.write_json("story.json", story.model_dump())
    return sum(c.word_count for c in story.chapters)


def _grow(ctx: JobContext, calls: list[tuple[int, int]], cap: int | None = None):
    """`expand_chapter` stand-in with the real effect: the chapter really gets `add_words` more words
    (at most `cap` when given: a model that keeps writing too little)."""

    def grow(ctx_: JobContext, n: int, add_words: int) -> None:
        calls.append((n, add_words))
        story = M.load_story(ctx_)
        chapters = [c.model_dump() for c in story.chapters]
        for c in chapters:
            if c["n"] == n:
                more = add_words if cap is None else min(add_words, cap)
                text = c["text"] + " " + " ".join(["More."] * more)
                c.update(
                    M.build_chapter(n, c["title"], text, c["summary"], wpm=155, is_last=n == 5).model_dump()
                )
        ctx_.write_json(
            "story.json", Story.model_validate({**story.model_dump(), "chapters": chapters}).model_dump()
        )

    return grow


@pytest.mark.parametrize("tiny", [False, True])
def test_a_story_at_or_over_its_profiles_floor_is_left_alone(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, tiny: bool
):
    ctx = _ctx(tmp_home, tiny=tiny)
    floor = _plan(ctx)["min_words"]
    total = _story_of(ctx, floor + 5)
    assert total >= floor
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(M, "expand_chapter", _grow(ctx, calls))
    M.enforce_word_floor(ctx, total)
    assert calls == []


def test_the_same_story_is_below_the_floor_normally_and_above_it_when_tiny(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    normal, tiny = _ctx(tmp_home, tiny=False), _ctx(tmp_home, tiny=True)
    normal_floor, tiny_floor = _plan(normal)["min_words"], _plan(tiny)["min_words"]
    assert tiny_floor < normal_floor
    words = (tiny_floor + normal_floor) // 2  # between the two floors
    calls: list[tuple[int, int]] = []

    total = _story_of(tiny, words)
    monkeypatch.setattr(M, "expand_chapter", _grow(tiny, calls))
    M.enforce_word_floor(tiny, total)
    assert calls == []  # the tiny install accepts it

    total = _story_of(normal, words)
    monkeypatch.setattr(M, "expand_chapter", _grow(normal, calls))
    M.enforce_word_floor(normal, total)
    assert calls  # the normal install lengthens it ...
    assert sum(c.word_count for c in M.load_story(normal).chapters) >= normal_floor  # ... up to the floor


@pytest.mark.parametrize("tiny", [False, True])
def test_a_story_far_under_the_floor_is_refused_after_three_expansions_on_either_profile(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, tiny: bool
):
    ctx = _ctx(tmp_home, tiny=tiny)
    _plan(ctx)
    total = _story_of(ctx, 100)
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(M, "expand_chapter", _grow(ctx, calls, cap=1))  # three expansions add three words
    with pytest.raises(NsError) as ei:
        M.enforce_word_floor(ctx, total)
    assert ei.value.code == "NS-E303" and len(calls) == M.FLOOR_MAX_EXPANSIONS


def test_an_owners_own_script_is_never_extended_even_on_a_tiny_install(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = _ctx(tmp_home, tiny=True)
    _plan(ctx)
    ctx.write_json("script_source.json", {"owner_script": True})
    assert ctx.owner_script
    total = _story_of(ctx, 100)
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(M, "expand_chapter", _grow(ctx, calls))
    with pytest.raises(NsError) as ei:
        M.enforce_word_floor(ctx, total)
    assert ei.value.code == "NS-E303" and calls == []
