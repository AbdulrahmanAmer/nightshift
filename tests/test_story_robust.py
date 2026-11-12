# ruff: noqa: E501
"""Card story-robust (block E2): a story job does not die on a short rewrite, and never swaps the owner's idea
in silence. T1 to T11 of the card. The fixture is the owner's job of 2026-11-12: five chapters of a
309-word budget (window 247-371), a rewrite of chapter 1 that came back near 202 words and scored 8.29.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import ns_studio.text.stages_shorts  # noqa: F401  (registers the shorts stages)
from ns_common.errors import NsError
from ns_common.llm_client import FakeLlm
from ns_common.textstats import split_sentences, word_count
from ns_studio import jobs, ownership
from ns_studio import orchestrator as orch
from ns_studio.context import FIXTURES_LLM
from ns_studio.policy import regen
from ns_studio.stages import REGISTRY
from ns_studio.text import stages_master as M
from ns_studio.text.stages_shorts import ideas_budget
from tests.helpers import make_channel, make_ctx

RUBRIC = ("hook", "clarity", "pacing", "payoff", "cliffhangers", "originality", "voice_fit")
BAD = {"scores": dict.fromkeys(RUBRIC, 5), "fixes": ["Fix it."]}
# Box 3: avg 8.2857, low 7 (over the channel's bar of 8.0)
GOOD = {
    "scores": {"hook": 9, "clarity": 9, "pacing": 8, "payoff": 8, "cliffhangers": 9, "originality": 8,
               "voice_fit": 7},
    "fixes": [],
}  # fmt: skip
CHANNEL = {"format": "master_first", "long_minutes": (11, 11)}


class ScriptedLlm(FakeLlm):
    """FakeLlm whose text prompts answer from a script: `texts[prompt]` in order, the last one repeated."""

    def __init__(self, texts: dict[str, list[str]], **kw: Any) -> None:
        super().__init__(FIXTURES_LLM, **kw)
        self.texts = {k: list(v) for k, v in texts.items()}

    def complete_text(self, *, prompt_name: str, system: str, user: str, thinking: bool = False,
                      temperature: float = 0.8, max_tokens: int = 2048) -> str:
        if prompt_name not in self.texts:
            return super().complete_text(prompt_name=prompt_name, system=system, user=user, thinking=thinking)
        self.calls.append({"prompt_name": prompt_name, "user": user, "system": system})
        queue = self.texts[prompt_name]
        return queue.pop(0) if len(queue) > 1 else queue[0]


class Spy:
    """The job's logger, with every event kept as (level, event, fields)."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.events = inner, []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def _keep(self, level: str, event: str, msg: str = "", **data: Any) -> None:
        self.events.append((level, event, data))
        getattr(self.inner, level)(event, msg, **data)

    def info(self, event: str, msg: str = "", **data: Any) -> None:
        self._keep("info", event, msg, **data)

    def warn(self, event: str, msg: str = "", **data: Any) -> None:
        self._keep("warn", event, msg, **data)

    def error(self, event: str, msg: str = "", **data: Any) -> None:
        self._keep("error", event, msg, **data)

    def of(self, event: str) -> list[dict[str, Any]]:
        return [d for _lvl, e, d in self.events if e == event]


def _chapter(n: int) -> str:
    return json.loads((FIXTURES_LLM / f"chapter_draft_{n}.json").read_text(encoding="utf-8"))["text"].strip()


def _cut(text: str, under: int) -> str:
    """The first sentences of `text` with fewer than `under` words in total."""
    kept: list[str] = []
    for s in split_sentences(text):
        if word_count(" ".join([*kept, s])) >= under:
            break
        kept.append(s)
    return " ".join(kept)


def _master(monkeypatch: pytest.MonkeyPatch, tmp_home: Path):
    """A master job with its plan and drafts written (fixtures: 5 chapters of 309 words, window 247-371)."""
    monkeypatch.setitem(orch.FORMAT_STAGES, "master_first", list(M.MASTER_STAGES[:3]))
    ctx = make_ctx(tmp_home, fmt="master_first", channel=make_channel(**CHANNEL))
    orch.run_job(ctx)
    assert [c["word_budget"] for c in ctx.read_json("plan.json")["chapters"]][0] == 309
    ctx.log = Spy(ctx.log)  # type: ignore[misc]
    return ctx


def _calls(ctx, prompt: str) -> list[dict[str, Any]]:
    return [c for c in ctx.services.llm.calls if c["prompt_name"] == prompt]  # type: ignore[attr-defined]


# ---------------------------------------------------------------- S2: the chapter's own loop repairs length
def test_t1_a_short_rewrite_that_scores_over_the_bar_is_lengthened_scored_again_and_accepted(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = _master(monkeypatch, tmp_home)
    full = _chapter(1)
    short = _cut(full, 210)
    assert 150 < word_count(short) < 247 <= word_count(full) <= 371  # the fixture is what the card says
    ctx.services.llm = ScriptedLlm(
        {"revise": [short], "expand": [full]}, overrides={"critic": [BAD, GOOD, GOOD, GOOD, GOOD, GOOD, GOOD]}
    )
    REGISTRY["chapters_critic"].run(ctx)  # before the card: NS-E303 "length: 1xx words, allowed 247-371"
    revised = ctx.read_json("story_revised.json")["chapters"]
    assert revised[0]["text"] == full
    critic_calls = _calls(ctx, "critic")
    assert len(critic_calls) == 7  # chapter 1: draft, rewrite, repaired text; then one per chapter
    assert full.split()[-1] in critic_calls[2]["user"] and split_sentences(full)[-1] in critic_calls[2]["user"]
    events = ctx.log.of("length_repair")
    assert len(events) == 1
    ev = events[0]
    assert (ev["chapter"], ev["words_before"], ev["words_after"], ev["window"], ev["mode"], ev["attempt"]) == (
        1, word_count(short), word_count(full), "247-371", "lengthen", 1,
    )  # fmt: skip


def test_t2_every_repair_comes_back_short_then_ns_e303_names_the_chapter_words_and_window(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = _master(monkeypatch, tmp_home)
    short = _cut(_chapter(1), 210)
    ctx.services.llm = ScriptedLlm(
        {"revise": [short], "expand": [short]}, overrides={"critic": [BAD, GOOD, GOOD, GOOD]}
    )
    with pytest.raises(NsError) as ei:
        REGISTRY["chapters_critic"].run(ctx)
    assert ei.value.code == "NS-E303"
    detail = ei.value.detail
    assert "chapter 1" in detail and f"{word_count(short)} words" in detail and "247-371" in detail
    assert len(_calls(ctx, "expand")) == 2  # two repairs, never a third
    assert [e["attempt"] for e in ctx.log.of("length_repair")] == [1, 2]
    assert len(_calls(ctx, "critic")) == 4  # draft, rewrite, repair 1, repair 2: each scored


def test_t3_a_rewrite_that_comes_back_too_long_is_trimmed_and_accepted(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = _master(monkeypatch, tmp_home)
    full = _chapter(1)
    long = f"{full}\n\n{_chapter(2)}"
    assert word_count(long) > 371
    ctx.services.llm = ScriptedLlm({"revise": [long, full]}, overrides={"critic": [BAD, *[GOOD] * 6]})
    REGISTRY["chapters_critic"].run(ctx)
    assert ctx.read_json("story_revised.json")["chapters"][0]["text"] == full
    events = ctx.log.of("length_repair")
    assert len(events) == 1 and events[0]["mode"] == "trim" and events[0]["words_before"] == word_count(long)
    trim = _calls(ctx, "revise")[1]["user"]
    assert f"{word_count(long)} words" in trim and "between 247 and 371" in trim


def test_t4_a_first_draft_outside_the_window_with_a_passing_score_is_repaired_too(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = _master(monkeypatch, tmp_home)
    full = _chapter(1)
    short = _cut(full, 210)
    draft = ctx.read_json("story_draft.json")
    draft["chapters"][0]["text"] = short
    ctx.write_json("story_draft.json", draft)
    ctx.services.llm = ScriptedLlm({"expand": [full]}, overrides={"critic": GOOD})
    REGISTRY["chapters_critic"].run(ctx)
    assert ctx.read_json("story_revised.json")["chapters"][0]["text"] == full
    assert _calls(ctx, "revise") == []  # no rewrite was needed
    assert [e["mode"] for e in ctx.log.of("length_repair")] == ["lengthen"]


def test_t5_the_shorts_critic_repairs_a_short_rewrite_in_the_storys_window(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.log = Spy(ctx.log)  # type: ignore[misc]
    draft = (FIXTURES_LLM / "story_draft.txt").read_text(encoding="utf-8").strip()
    budget = ideas_budget(ctx)
    short = _cut(draft, budget.min_words // 2)
    assert word_count(short) < budget.min_words <= word_count(draft) <= budget.max_words
    ctx.write_json("story.json", {"text": draft, "word_count": word_count(draft), "revisions": 0})
    ctx.services.llm = ScriptedLlm(
        {"revise": [short], "expand": [draft]}, overrides={"critic": [BAD, GOOD, GOOD]}
    )
    REGISTRY["critic_revise"].run(ctx)  # before the card: NS-E303 at once
    story = ctx.read_json("story.json")
    assert story["text"] == draft and story["revisions"] == 1
    events = ctx.log.of("length_repair")
    assert len(events) == 1
    assert events[0]["window"] == f"{budget.min_words}-{budget.max_words}" and events[0]["mode"] == "lengthen"
    assert len(_calls(ctx, "critic")) == 3


# ---------------------------------------------------------------- S1: the rewrite knows its length
def test_t6_the_revise_prompt_names_the_window_and_the_length_now(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    ctx = _master(monkeypatch, tmp_home)
    full = _chapter(1)
    ctx.services.llm = ScriptedLlm({"revise": [full]}, overrides={"critic": [BAD, *[GOOD] * 5]})
    REGISTRY["chapters_critic"].run(ctx)
    user = _calls(ctx, "revise")[0]["user"]
    assert f"the text is {word_count(full)} words now" in user and "between 247 and 371 words" in user
    # the Short's rewrite names its own window
    short_ctx = make_ctx(tmp_home)
    draft = (FIXTURES_LLM / "story_draft.txt").read_text(encoding="utf-8").strip()
    short_ctx.write_json("story.json", {"text": draft, "word_count": word_count(draft), "revisions": 0})
    short_ctx.services.llm = ScriptedLlm({"revise": [draft]}, overrides={"critic": [BAD, GOOD]})
    REGISTRY["critic_revise"].run(short_ctx)
    b = ideas_budget(short_ctx)
    said = _calls(short_ctx, "revise")[0]["user"]
    assert f"the text is {word_count(draft)} words now" in said and f"between {b.min_words} and {b.max_words}" in said


# ---------------------------------------------------------------- S3: the owner's idea
def _pc(**kw: Any) -> dict[str, Any]:
    base = {"unsafe_categories": [], "hook_honest": True, "title_honest": True, "standalone": None,
            "notes": "", "intensity": "low", "real_setting": False}  # fmt: skip
    return base | kw


REFUSED = _pc(unsafe_categories=["minors"], notes="students in an endangering context")


def _bank(ctx, premise: str, title: str, created_at: str) -> int:
    ctx.db.execute(
        "insert into ideas(channel, premise, twist, created_at, status, title, source) values (?,?,?,?,?,?,?)",
        (ctx.channel.slug, premise, f"twist of {premise}", created_at, "approved", title, "owner"),
    )
    return int(ctx.db.one("select idea_id from ideas where premise=?", (premise,))["idea_id"])


def _row(ctx, idea_id: int):
    return ctx.db.one("select status, used, job_id from ideas where idea_id=?", (idea_id,))


def _policy_ctx(tmp_home: Path):
    jobs.load_stage_modules()  # the gates are in the stage table: the idea check is on
    ctx = make_ctx(tmp_home, policy=True)
    ctx.log = Spy(ctx.log)  # type: ignore[misc]
    return ctx


def test_t7_a_refused_idea_in_a_job_started_by_hand_ends_the_job_and_goes_back_to_new(tmp_home: Path):
    ctx = _policy_ctx(tmp_home)
    mine = _bank(ctx, "A night janitor finds footage of students entering the basement", "Hidden Camera",
                 "2026-11-01")  # fmt: skip
    ctx.services.llm = FakeLlm(FIXTURES_LLM, overrides={"policy_check": [REFUSED, _pc()]})
    with pytest.raises(NsError) as ei:
        regen.run_job_guarded(ctx)  # Run now: the owner record says `manual`
    assert ei.value.code == "NS-E601"
    assert "Hidden Camera" in ei.value.detail and "minors" in ei.value.detail
    assert "students in an endangering context" in ei.value.detail
    row = _row(ctx, mine)
    assert (row["status"], row["used"], row["job_id"]) == ("new", 0, None)
    assert _calls(ctx, "ideas") == []  # no idea of the model's own was asked for
    assert ctx.db.one("select count(*) c from ideas where job_id=?", (ctx.job_id,))["c"] == 0
    assert not ctx.file("idea.json").exists()
    att = ctx.read_json(regen.ATTEMPTS_FILE)
    assert att["regenerations"] == 0  # nothing was retried, nothing spent
    aside = ctx.log.of("idea_set_aside")
    assert len(aside) == 1 and aside[0]["idea_id"] == mine and aside[0]["title"] == "Hidden Camera"
    assert aside[0]["category"] == "minors" and "endangering" in aside[0]["reason"]
    # a Retry without a new approval writes nothing in its place either
    with pytest.raises(NsError) as again:
        regen.run_job_guarded(ctx)
    assert again.value.code == "NS-E601" and _calls(ctx, "ideas") == []


@pytest.mark.parametrize("second", [True, False], ids=["next-approved", "own-ideas"])
def test_t8_a_refused_idea_in_a_nightly_job_goes_back_to_new_and_the_job_says_so(tmp_home: Path, second: bool):
    ctx = _policy_ctx(tmp_home)
    ownership.write_record(ctx.paths, ctx.job_id, origin="daily", kind="job")
    mine = _bank(ctx, "A night janitor finds footage of students", "Hidden Camera", "2026-11-01")
    other = _bank(ctx, "A ferry that docks one stop too many", "Last Ferry", "2026-11-02") if second else None
    ctx.services.llm = FakeLlm(FIXTURES_LLM, overrides={"policy_check": [REFUSED, _pc()]})
    REGISTRY["ideas"].run(ctx)
    row = _row(ctx, mine)
    assert (row["status"], row["used"], row["job_id"]) == ("new", 0, None)
    idea = ctx.read_json("idea.json")
    if other is not None:
        assert idea["premise"] == "A ferry that docks one stop too many"
        assert tuple(_row(ctx, other)) == ("used", 1, ctx.job_id)
        assert _calls(ctx, "ideas") == []
    else:
        assert _calls(ctx, "ideas") and idea["premise"] != "A night janitor finds footage of students"
    told = ctx.log.of("idea_replaced")
    assert len(told) == 1 and len(ctx.log.of("idea_set_aside")) == 1
    message = ctx.read_json("idea_note.json")["message"]
    assert "Hidden Camera" in message and "minors" in message and "endangering" in message
    assert "Library > Ideas" in message


# ---------------------------------------------------------------- S4: one retry per cause
class Stub:
    """A stage that raises the errors of `fails` in turn, then passes; `seen` keeps ctx.attempt per run."""

    tenant = None
    inputs: tuple[str, ...] = ()

    def __init__(self, name: str, fails: list[NsError], seen: list[tuple[str, int]]) -> None:
        self.name, self.fails, self.seen = name, list(fails), seen
        self.outputs = (f"{name}.json",)

    def run(self, ctx) -> None:
        self.seen.append((self.name, ctx.attempt))
        if self.fails:
            raise self.fails.pop(0)
        ctx.write_json(f"{self.name}.json", {"ok": True})


PLAN = ["ideas", "draft", "critic_revise", "voice"]


def _stubs(monkeypatch, fails: dict[str, list[NsError]]) -> list[tuple[str, int]]:
    seen: list[tuple[str, int]] = []
    for n in PLAN:
        monkeypatch.setitem(REGISTRY, n, Stub(n, fails.get(n, []), seen))
    monkeypatch.setitem(orch.FORMAT_STAGES, "robust", list(PLAN))
    return seen


def _idea_refused() -> NsError:
    return NsError("NS-E601", "all 8 ideas were flagged as unsafe: minors; a judge's sentence")


def test_t9_t11_an_idea_refusal_does_not_spend_the_retry_of_the_text(tmp_home: Path, monkeypatch):
    seen = _stubs(monkeypatch, {"ideas": [_idea_refused()], "critic_revise": [NsError("NS-E303", "length")]})
    ctx = make_ctx(tmp_home, fmt="robust")
    # Before the card the second failure ended the job: one counter, spent by the idea (box 4).
    assert regen.run_job_guarded(ctx) == PLAN
    att = ctx.read_json(regen.ATTEMPTS_FILE)
    assert att["causes"] == {"idea": 1, "text": 1, "media": 0}
    assert [h["code"] for h in att["history"]] == ["NS-E601", "NS-E303"]
    # T11: the total of passes keeps its meaning (the prompts run hotter on each)
    assert ctx.attempt == 2 and ctx.generation == 2
    assert [a for n, a in seen if n == "critic_revise"] == [1, 2]


def test_t9_a_second_text_refusal_is_final(tmp_home: Path, monkeypatch):
    e303 = [NsError("NS-E303", "length"), NsError("NS-E303", "length again")]
    seen = _stubs(monkeypatch, {"ideas": [_idea_refused()], "critic_revise": e303})
    ctx = make_ctx(tmp_home, fmt="robust")
    with pytest.raises(NsError) as ei:
        regen.run_job_guarded(ctx)
    assert ei.value.code == "NS-E303" and ei.value.detail == "length again"
    assert [n for n, _a in seen].count("critic_revise") == 2 and "voice" not in [n for n, _a in seen]


#: The text gate refuses the written story (box 3's NS-E601, at story level): `REGEN_FROM` restarts at `ideas`.
def _story_refused(detail: str = "story: minors; the judge's sentence") -> NsError:
    return NsError("NS-E601", detail, check="unsafe_topics")


def _same_idea_plan(monkeypatch, draft_fails: list[NsError]) -> list[tuple[str, int]]:
    """The real ideas stage, then a `draft` that raises `draft_fails` in turn, then `voice`."""
    seen: list[tuple[str, int]] = []
    monkeypatch.setitem(REGISTRY, "draft", Stub("draft", draft_fails, seen))
    monkeypatch.setitem(REGISTRY, "voice", Stub("voice", [], seen))
    monkeypatch.setitem(orch.FORMAT_STAGES, "robust", ["ideas", "draft", "voice"])
    return seen


def test_s3b_a_refused_story_of_the_owners_idea_is_written_again_from_the_same_idea(tmp_home: Path, monkeypatch):
    seen = _same_idea_plan(monkeypatch, [_story_refused()])
    ctx = make_ctx(tmp_home, fmt="robust")
    ctx.log = Spy(ctx.log)  # type: ignore[misc]
    mine = _bank(ctx, "A night janitor finds footage of students", "Hidden Camera", "2026-11-01")
    other = _bank(ctx, "A ferry that docks one stop too many", "Last Ferry", "2026-11-02")
    assert regen.run_job_guarded(ctx) == ["ideas", "draft", "voice"]
    # Before S3b the retry restarted at `ideas`, which drew "Last Ferry" in place of his idea, in silence.
    assert ctx.read_json("idea.json")["premise"] == "A night janitor finds footage of students"
    assert tuple(_row(ctx, mine)) == ("used", 1, ctx.job_id)
    assert tuple(_row(ctx, other)) == ("approved", 0, None)
    assert [n for n, _a in seen] == ["draft", "draft", "voice"]
    assert [e["restart_at"] for e in ctx.log.of("policy_regenerate")] == ["draft"]
    assert ctx.read_json(regen.ATTEMPTS_FILE)["causes"] == {"idea": 0, "text": 1, "media": 0}
    assert _calls(ctx, "ideas") == []


def test_s3b_a_second_refusal_of_the_owners_story_ends_the_job_with_the_reason(tmp_home: Path, monkeypatch):
    fails = [_story_refused(), _story_refused("story: minors; refused again")]
    seen = _same_idea_plan(monkeypatch, fails)
    ctx = make_ctx(tmp_home, fmt="robust")
    mine = _bank(ctx, "A night janitor finds footage of students", "Hidden Camera", "2026-11-01")
    other = _bank(ctx, "A ferry that docks one stop too many", "Last Ferry", "2026-11-02")
    with pytest.raises(NsError) as ei:
        regen.run_job_guarded(ctx)
    assert ei.value.code == "NS-E601" and ei.value.detail == "story: minors; refused again"
    assert [n for n, _a in seen] == ["draft", "draft"]
    assert ctx.read_json("idea.json")["premise"] == "A night janitor finds footage of students"
    assert tuple(_row(ctx, mine)) == ("used", 1, ctx.job_id) and tuple(_row(ctx, other)) == ("approved", 0, None)


class ModelIdea(Stub):
    """The ideas stage of a job with no banked idea: the model's own premise, recorded as `IdeasStage` does."""

    def __init__(self, name: str, fails: list[NsError], seen: list[tuple[str, int]]) -> None:
        super().__init__(name, fails, seen)
        self.outputs = ("idea.json",)  # done means done: only a restart at `ideas` runs it again

    def run(self, ctx) -> None:
        self.seen.append((self.name, ctx.attempt))
        premise = f"A premise of the model's own, pass {ctx.attempt}"
        ctx.db.execute(
            "insert into ideas(channel, job_id, premise, twist, created_at, used) values (?,?,?,?,?,1)",
            (ctx.channel.slug, ctx.job_id, premise, "a twist", "2026-11-12"),
        )
        ctx.write_json("idea.json", {"premise": premise})


def test_s3b_a_refused_story_of_the_models_own_idea_still_restarts_at_ideas(tmp_home: Path, monkeypatch):
    seen = _same_idea_plan(monkeypatch, [_story_refused()])
    monkeypatch.setitem(REGISTRY, "ideas", ModelIdea("ideas", [], seen))
    ctx = make_ctx(tmp_home, fmt="robust")
    assert regen.run_job_guarded(ctx) == ["ideas", "draft", "voice"]
    assert [n for n, _a in seen] == ["ideas", "draft", "ideas", "draft", "voice"]
    assert ctx.read_json("idea.json")["premise"] == "A premise of the model's own, pass 1"


def _critic_plan(monkeypatch, critic_fails: list[NsError]) -> list[tuple[str, int]]:
    """The real ideas stage, `draft`, a `critic_revise` that raises `critic_fails` in turn, then `voice`."""
    seen: list[tuple[str, int]] = []
    for n in ("draft", "voice"):
        monkeypatch.setitem(REGISTRY, n, Stub(n, [], seen))
    monkeypatch.setitem(REGISTRY, "critic_revise", Stub("critic_revise", critic_fails, seen))
    monkeypatch.setitem(orch.FORMAT_STAGES, "robust", ["ideas", "draft", "critic_revise", "voice"])
    return seen


def _missed_bar(n: int) -> NsError:
    return NsError("NS-E304", f"critic avg 6.{n} low 5 after 2 revisions")


def test_s3b_a_story_of_the_owners_idea_that_misses_the_critic_is_rewritten_from_it_never_redrawn(
    tmp_home: Path, monkeypatch
):
    seen = _critic_plan(monkeypatch, [_missed_bar(1), _missed_bar(2)])
    ctx = make_ctx(tmp_home, fmt="robust")
    mine = _bank(ctx, "A night janitor finds footage of students", "Hidden Camera", "2026-11-01")
    other = _bank(ctx, "A ferry that docks one stop too many", "Last Ferry", "2026-11-02")
    with pytest.raises(NsError) as ei:
        regen.run_job_guarded(ctx)
    # Before the Head's ruling the first NS-E304 redrew the idea: "Last Ferry" was written in place of his.
    assert ei.value.code == "NS-E304" and ei.value.detail == "critic avg 6.2 low 5 after 2 revisions"
    assert [n for n, _a in seen] == ["draft", "critic_revise", "draft", "critic_revise"]
    assert ctx.read_json("idea.json")["premise"] == "A night janitor finds footage of students"
    assert tuple(_row(ctx, mine)) == ("used", 1, ctx.job_id) and tuple(_row(ctx, other)) == ("approved", 0, None)
    att = ctx.read_json(regen.ATTEMPTS_FILE)
    assert att["causes"]["text"] == 1 and not any("idea" in h for h in att["history"])
    assert not ctx.file("ideas_failed.json").exists()


def test_s3b_a_story_of_the_models_own_idea_that_misses_the_critic_is_redrawn_as_before(
    tmp_home: Path, monkeypatch
):
    seen = _critic_plan(monkeypatch, [_missed_bar(1)])
    monkeypatch.setitem(REGISTRY, "ideas", ModelIdea("ideas", [], seen))
    ctx = make_ctx(tmp_home, fmt="robust")
    assert regen.run_job_guarded(ctx) == ["ideas", "draft", "critic_revise", "voice"]
    assert [n for n, _a in seen] == ["ideas", "draft", "critic_revise", "ideas", "draft", "critic_revise", "voice"]
    att = ctx.read_json(regen.ATTEMPTS_FILE)
    assert [("idea" in h) for h in att["history"]] == [True]  # a redraw (spec 16.2), not a regeneration
    assert att["regenerations"] == 0


@pytest.mark.parametrize(
    ("stage", "error"),
    [
        ("ideas", _idea_refused()),
        ("critic_revise", NsError("NS-E303", "length")),
        ("voice", NsError("NS-E403", "drift 400 ms")),
    ],
    ids=["idea", "text", "media"],
)
def test_t10_an_old_one_counter_file_leaves_no_retry_for_any_cause(tmp_home: Path, monkeypatch, stage, error):
    seen = _stubs(monkeypatch, {stage: [error]})
    ctx = make_ctx(tmp_home, fmt="robust")
    ctx.write_json(regen.ATTEMPTS_FILE, {"regenerations": 1, "history": []})  # written before the counters
    with pytest.raises(NsError) as ei:
        regen.run_job_guarded(ctx)
    assert ei.value.code == error.code
    assert [n for n, _a in seen].count(stage) == 1  # never a second pass
    assert ctx.read_json(regen.ATTEMPTS_FILE)["regenerations"] == 1
