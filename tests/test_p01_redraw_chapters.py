"""P01 x P02: an idea redrawn after a critic failure goes through the same pick as a first draw.

A master-first job never continues with an idea that has no chapter potential while one that has it is left
in the batch (P02, F-H55), also when the idea is the second or third of the job (P01, spec 16.2).
"""

from pathlib import Path

from ns_common.llm_client import FakeLlm
from ns_studio.context import FIXTURES_LLM
from ns_studio.text import stages_shorts as S
from tests.helpers import make_channel, make_ctx


def _idea(n: int, why: str) -> dict:
    return {
        "premise": f"Premise number {n} is about a house that keeps one more room than it should have.",
        "twist": f"Twist {n}: the extra room is the one the narrator sleeps in.",
        "setting": "an old terraced house",
        "protagonist": "a night nurse",
        "why_chapters": why,
        "keywords": ["house", "stairs", "door"],
    }


def _redraw_ctx(tmp_home: Path, whys: list[str], *, fmt: str):
    ctx = make_ctx(tmp_home, fmt=fmt, channel=make_channel(format="master_first", long_minutes=(11, 11)))
    batch = [_idea(i, w) for i, w in enumerate(whys, start=1)]
    ctx.services.llm = FakeLlm(FIXTURES_LLM, overrides={"ideas": {"ideas": batch}})
    ctx.write_json("ideas_all.json", batch)
    ctx.write_json("ideas_failed.json", [batch[0]["premise"]])  # idea 1 missed the critic's bar
    return ctx


def test_a_master_redraw_skips_an_idea_without_chapter_potential(tmp_home: Path):
    ctx = _redraw_ctx(
        tmp_home, ["Five nights, each with one more room.", "n/a", "The tenants change."], fmt="master_first"
    )
    S.IdeasStage().run(ctx)
    assert ctx.read_json("idea.json")["premise"].startswith("Premise number 3")
    assert ctx.db.one("select count(*) c from ideas where used=1")["c"] == 1
    assert [c for c in ctx.services.llm.calls if c["prompt_name"] == "ideas"] == []  # drawn from the batch


def test_a_shorts_redraw_still_takes_the_next_idea_in_order(tmp_home: Path):
    ctx = _redraw_ctx(
        tmp_home, ["Five nights, each with one more room.", "n/a", "The tenants change."], fmt="shorts_only"
    )
    S.IdeasStage().run(ctx)
    assert ctx.read_json("idea.json")["premise"].startswith("Premise number 2")
