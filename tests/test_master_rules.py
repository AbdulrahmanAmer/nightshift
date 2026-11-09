"""P02: a master-first job drops ideas without chapter potential before it picks (F-H55) and a story below
the 9:00 planning floor is expanded or refused by the continuity stage (F-H51)."""

import json
from pathlib import Path

import pytest

from ns_common.errors import NsError
from ns_common.llm_client import FakeLlm
from ns_common.models import Story
from ns_common.textstats import word_count
from ns_studio.context import FIXTURES_LLM
from ns_studio.orchestrator import FORMAT_STAGES, run_job
from ns_studio.stages import REGISTRY
from ns_studio.text import stages_master as M
from ns_studio.text import stages_shorts as S
from tests.helpers import make_channel, make_ctx

CHANNEL = {"format": "master_first", "long_minutes": (11, 11)}


def _idea(n: int, why: str) -> dict:
    return {
        "premise": f"Premise number {n} is about a house that keeps one more room than it should have.",
        "twist": f"Twist {n}: the extra room is the one the narrator sleeps in.",
        "setting": "an old terraced house",
        "protagonist": "a night nurse",
        "why_chapters": why,
        "keywords": ["house", "stairs", "door"],
    }


def _ideas_ctx(tmp_home: Path, whys: list[str], *, judge: int = 0, fmt: str = "master_first"):
    ctx = make_ctx(tmp_home, fmt=fmt, channel=make_channel(**CHANNEL))
    ideas = {"ideas": [_idea(i, w) for i, w in enumerate(whys, start=1)]}
    ctx.services.llm = FakeLlm(
        FIXTURES_LLM, overrides={"ideas": ideas, "judge": {"index": judge, "reason": "x"}}
    )
    return ctx


def _calls(ctx, name: str) -> list[dict]:
    return [c for c in ctx.services.llm.calls if c["prompt_name"] == name]


# ---- F-H55 ------------------------------------------------------------------------------------------------


def test_an_idea_with_n_a_chapters_is_not_picked_for_a_master(tmp_home: Path):
    ctx = _ideas_ctx(tmp_home, ["n/a", "Each night the room moves one door closer to the nurse."])
    REGISTRY["ideas"].run(ctx)
    assert ctx.read_json("idea.json")["premise"].startswith("Premise number 2")
    assert _calls(ctx, "judge") == []  # one idea left: nothing to choose
    assert len(ctx.read_json("ideas_all.json")) == 2  # the candidates stay on record


def test_ideas_with_an_empty_why_chapters_are_dropped_too(tmp_home: Path):
    ctx = _ideas_ctx(tmp_home, ["", "N/A.", "  ", "Five nights, each with one more room."])
    REGISTRY["ideas"].run(ctx)
    assert ctx.read_json("idea.json")["premise"].startswith("Premise number 4")


def test_a_judge_chooses_among_the_ideas_that_stretch_to_chapters(tmp_home: Path):
    ctx = _ideas_ctx(
        tmp_home, ["n/a", "Room moves closer each night.", "The tenants change each chapter."], judge=1
    )
    REGISTRY["ideas"].run(ctx)
    assert ctx.read_json("idea.json")["premise"].startswith("Premise number 3")
    (call,) = _calls(ctx, "judge")
    assert "Premise number 2" in call["user"] and "Premise number 3" in call["user"]
    assert "Premise number 1" not in call["user"]  # the 'n/a' idea is not even an option


def test_when_no_idea_has_chapter_potential_the_first_survivor_is_kept(tmp_home: Path):
    ctx = _ideas_ctx(tmp_home, ["n/a", "n/a"])
    REGISTRY["ideas"].run(ctx)
    assert ctx.read_json("idea.json")["premise"].startswith("Premise number 1")


def test_a_shorts_job_still_takes_the_first_survivor(tmp_home: Path):
    ctx = _ideas_ctx(tmp_home, ["n/a", "Room moves closer each night."], fmt="shorts_only")
    REGISTRY["ideas"].run(ctx)
    assert ctx.read_json("idea.json")["premise"].startswith("Premise number 1")
    assert _calls(ctx, "judge") == []


def test_chapter_potential_reads_the_text_not_the_case():
    assert not S.has_chapter_potential({"why_chapters": "N/A"})
    assert not S.has_chapter_potential({})
    assert S.has_chapter_potential({"why_chapters": "five escalating nights"})


# ---- F-H51 ------------------------------------------------------------------------------------------------


def _upto_critic(monkeypatch: pytest.MonkeyPatch, tmp_home: Path):
    monkeypatch.setitem(FORMAT_STAGES, "master_first", list(M.MASTER_STAGES[:4]))
    ctx = make_ctx(tmp_home, fmt="master_first", channel=make_channel(**CHANNEL))
    run_job(ctx)
    return ctx


def _set_floor(ctx, floor: int) -> int:
    revised = ctx.read_json("story_revised.json")
    total = sum(word_count(c["text"]) for c in revised["chapters"])
    meta = ctx.read_json("plan_meta.json")
    ctx.write_json("plan_meta.json", {**meta, "min_words": floor})
    return total


def test_a_story_below_the_floor_is_expanded_by_the_continuity_stage(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = _upto_critic(monkeypatch, tmp_home)
    total = _set_floor(ctx, 0)
    _set_floor(ctx, total + 100)  # 100 words short of the floor
    REGISTRY["continuity"].run(ctx)
    assert len(_calls(ctx, "expand")) == 1
    story = Story.model_validate(ctx.read_json("story.json"))
    assert sum(c.word_count for c in story.chapters) >= total + 100
    assert story.chapters[0].word_count > 400  # the shortest chapter (1) took the expansion
    row = ctx.db.one("select word_count from stories where job_id=?", (ctx.job_id,))
    assert row["word_count"] == sum(c.word_count for c in story.chapters)


def test_a_story_that_stays_below_the_floor_after_three_expansions_is_ns_e303(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = _upto_critic(monkeypatch, tmp_home)
    _set_floor(ctx, 100_000)
    with pytest.raises(NsError) as ei:
        REGISTRY["continuity"].run(ctx)
    assert ei.value.code == "NS-E303" and "floor" in ei.value.detail
    assert len(_calls(ctx, "expand")) == M.FLOOR_MAX_EXPANSIONS


def test_a_story_at_the_floor_is_left_alone(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = _upto_critic(monkeypatch, tmp_home)
    total = _set_floor(ctx, 0)
    _set_floor(ctx, total)
    REGISTRY["continuity"].run(ctx)
    assert _calls(ctx, "expand") == []


def test_no_plan_meta_means_no_floor(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = _upto_critic(monkeypatch, tmp_home)
    ctx.file("plan_meta.json").unlink()
    REGISTRY["continuity"].run(ctx)
    assert json.loads(ctx.file("story.json").read_text(encoding="utf-8"))["chapters"]
