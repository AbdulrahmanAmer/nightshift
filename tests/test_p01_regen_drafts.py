"""P01 (review fix): a regeneration really rebuilds the text; a resume still reuses it.

Review of PR 164: `chapters_draft` reuses `drafts/ch<n>.json` whenever the plan is unchanged, so the one
regeneration of NS-E303, NS-E305, NS-E602 and NS-E612 in a master rebuilt the story that had just failed (on
main the regeneration of NS-E602 and NS-E612 was a no-op for the same reason). A saved draft now carries the
generation it was written in (`JobContext.generation`: regenerations plus idea redraws): the same generation
(a resume, a retry of an interrupted job) reuses it, the next one drafts again.

The fake model answers differently on every call, so "the chapters differ" and "the chapters are byte for
byte the same" can only come from the stages.
"""

import json
from pathlib import Path

import pytest

import ns_studio.text.stages_master  # noqa: F401  (registers the master stages)
import ns_studio.text.stages_shorts  # noqa: F401  (registers the shorts stages)
from ns_common.errors import NsError
from ns_common.llm_client import FakeLlm
from ns_common.models import Runtime
from ns_common.tts_client import TtsItem
from ns_studio import orchestrator as orch
from ns_studio.context import FIXTURES_LLM
from ns_studio.media.voice import _signature
from ns_studio.policy import regen
from ns_studio.stages import REGISTRY, StageState
from tests.helpers import make_channel, make_ctx

MASTER_CHANNEL = {"format": "master_first", "long_minutes": (11, 11)}
MASTER_PLAN = ["ideas", "long_plan", "chapters_draft", "gate"]
SHORTS_PLAN = ["ideas", "plan", "draft", "critic_revise", "gate"]
RUBRIC = ("hook", "clarity", "pacing", "payoff", "cliffhangers", "originality", "voice_fit")
BAD_CRITIC = {"scores": {k: 5 for k in RUBRIC}, "fixes": ["Fix it."]}
GOOD_CRITIC = {"scores": {k: 9 for k in RUBRIC}, "fixes": []}


class VaryingLlm(FakeLlm):
    """A FakeLlm that writes a different chapter and a different story on every call.

    `crash_on` is the number of the `chapter_draft` call (counted over all chapters) that fails once with
    NS-E301, the way a model server that went away does in the middle of a long draft.
    """

    def __init__(self, fixtures_dir: Path, *, crash_on: int | None = None, **kw) -> None:
        super().__init__(fixtures_dir, **kw)
        self.crash_on = crash_on

    def _takes(self, prompt_name: str) -> int:
        return sum(1 for c in self.calls if c["prompt_name"] == prompt_name)

    def chapter_calls(self) -> int:
        return sum(1 for c in self.calls if c["prompt_name"].startswith("chapter_draft_"))

    def complete_json(self, **kw):
        data = super().complete_json(**kw)
        name = kw["prompt_name"]
        if name.startswith("chapter_draft_"):
            if self.crash_on is not None and self.chapter_calls() == self.crash_on:
                self.crash_on = None
                raise NsError("NS-E301", "the model server went away")
            data = {**data, "text": f"{data['text']}\n\nTake {self._takes(name)}."}
        return data

    def complete_text(self, **kw):
        text = super().complete_text(**kw)
        if kw["prompt_name"] == "story_draft":  # another word, the same length and reading level
            text = text.replace(
                "cold", ("cold", "icy", "chilly", "damp")[(self._takes("story_draft") - 1) % 4]
            )
        return text


class Gate:
    """Last stage: remembers the text it was given; fails the first `fail_times` times."""

    name, tenant, outputs = "gate", None, ("gate.json",)

    def __init__(self, code: str, read: str, fail_times: int = 1) -> None:
        self.code, self.read, self.fail_times = code, read, fail_times
        self.inputs = (read,)
        self.seen: list[str] = []

    def run(self, ctx) -> None:
        self.seen.append(ctx.file(self.read).read_text(encoding="utf-8"))
        if len(self.seen) <= self.fail_times:
            raise NsError(self.code, "seeded")
        ctx.write_json("gate.json", {"passes": len(self.seen)})


def _chapters(raw: str) -> list[str]:
    return [c["text"] for c in json.loads(raw)["chapters"]]


def _master(monkeypatch, tmp_home: Path, gate: Gate, *, tiny: bool = False, crash_on: int | None = None):
    monkeypatch.setitem(REGISTRY, "gate", gate)
    monkeypatch.setitem(orch.FORMAT_STAGES, "gen_master", list(MASTER_PLAN))
    ctx = make_ctx(tmp_home, fmt="gen_master", channel=make_channel(**MASTER_CHANNEL))
    if tiny:
        ctx.runtime = Runtime.model_validate({**ctx.runtime.model_dump(mode="json"), "tiny_story": True})
    ctx.services.llm = VaryingLlm(FIXTURES_LLM, crash_on=crash_on)
    return ctx


def _shorts(monkeypatch, tmp_home: Path, gate: Gate, **llm):
    monkeypatch.setitem(REGISTRY, "gate", gate)
    monkeypatch.setitem(orch.FORMAT_STAGES, "gen_shorts", list(SHORTS_PLAN))
    ctx = make_ctx(tmp_home, fmt="gen_shorts")
    ctx.services.llm = VaryingLlm(FIXTURES_LLM, overrides={"critic": GOOD_CRITIC}, **llm)
    return ctx


def _drafts(ctx) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(ctx.file("drafts").glob("ch*.json"))}


# ------------------------------------------------------------ master: the regeneration drafts again
@pytest.mark.parametrize("tiny", [False, True], ids=["profile-normal", "profile-tiny"])
@pytest.mark.parametrize("code", ["NS-E303", "NS-E305", "NS-E602", "NS-E612"])
def test_a_regenerated_master_has_other_chapters_than_the_first_attempt(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, code: str, tiny: bool
):
    gate = Gate(code, "story_draft.json")
    ctx = _master(monkeypatch, tmp_home, gate, tiny=tiny)
    assert regen.run_job_guarded(ctx) == MASTER_PLAN  # the one regeneration is enough
    first, second = (_chapters(s) for s in gate.seen)
    assert len(gate.seen) == 2 and len(first) == len(second) >= 5
    assert all(a != b for a, b in zip(first, second, strict=True)), "a chapter was reused"
    assert ctx.generation == 1 and ctx.attempt == 1
    assert all(
        json.loads(b)["generation"] == 1 for b in _drafts(ctx).values()
    )  # keyed, and kept for a resume


@pytest.mark.parametrize("tiny", [False, True], ids=["profile-normal", "profile-tiny"])
def test_a_second_failure_still_ends_a_master_job_after_exactly_one_regeneration(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, tiny: bool
):
    """The J3 rule with the story profile on and off: one regeneration, never a third pass."""
    gate = Gate("NS-E303", "story_draft.json", fail_times=9)
    ctx = _master(monkeypatch, tmp_home, gate, tiny=tiny)
    with pytest.raises(NsError) as ei:
        regen.run_job_guarded(ctx)
    assert ei.value.code == "NS-E303" and len(gate.seen) == 2
    assert ctx.read_json(regen.ATTEMPTS_FILE)["regenerations"] == 1
    assert ctx.services.llm.chapter_calls() == 10  # 5 chapters, drafted again once; nothing more


def test_a_regeneration_after_the_drafts_still_keeps_them(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """NS-E607 restarts after the chapters: they stay as they are (no needless hour of drafting)."""
    gate = Gate("NS-E607", "story_draft.json")
    ctx = _master(monkeypatch, tmp_home, gate)
    names = [*MASTER_PLAN[:-1], "shorts_select", "gate"]
    monkeypatch.setitem(orch.FORMAT_STAGES, "gen_master", names)

    class Select:
        name, tenant, inputs, outputs = "shorts_select", None, ("story_draft.json",), ("shorts_plan.json",)

        def run(self, ctx) -> None:
            ctx.write_json("shorts_plan.json", [])

    monkeypatch.setitem(REGISTRY, "shorts_select", Select())
    regen.run_job_guarded(ctx)
    assert len(gate.seen) == 2 and gate.seen[0] == gate.seen[1]
    assert ctx.services.llm.chapter_calls() == 5


# ------------------------------------------------------------ master: a resume keeps its drafts
def test_a_resumed_master_reuses_its_chapters_byte_for_byte(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    gate = Gate("NS-E303", "story_draft.json", fail_times=0)
    ctx = _master(monkeypatch, tmp_home, gate)
    regen.run_job_guarded(ctx)
    draft, drafts, calls = (
        ctx.file("story_draft.json").read_bytes(),
        _drafts(ctx),
        ctx.services.llm.chapter_calls(),
    )
    # the stage forgot it finished (a crash right after the last chapter was written)
    state = StageState.load(ctx.job_dir)
    state.entries.pop("chapters_draft")
    state.entries.pop("gate")
    state.save(ctx.job_dir)
    regen.run_job_guarded(ctx)
    assert ctx.services.llm.chapter_calls() == calls  # not one chapter was asked for again
    assert ctx.file("story_draft.json").read_bytes() == draft and _drafts(ctx) == drafts


def test_a_job_that_crashed_in_chapter_three_resumes_with_chapters_one_and_two(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    gate = Gate("NS-E303", "story_draft.json", fail_times=0)
    ctx = _master(monkeypatch, tmp_home, gate, crash_on=3)
    with pytest.raises(NsError) as ei:
        regen.run_job_guarded(ctx)
    assert ei.value.code == "NS-E301"  # not a policy code: no regeneration, the job stops as before
    assert ctx.generation == 0
    before = _drafts(ctx)
    assert sorted(before) == ["ch1.json", "ch2.json"]
    regen.run_job_guarded(ctx)  # the resume
    after = _drafts(ctx)
    assert after["ch1.json"] == before["ch1.json"] and after["ch2.json"] == before["ch2.json"]
    llm = ctx.services.llm
    assert [llm._takes(f"chapter_draft_{n}") for n in (1, 2, 3)] == [1, 1, 2]  # only chapter 3 was redone
    assert len(gate.seen) == 1


def test_a_crash_after_a_regeneration_resumes_inside_that_regeneration(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """Generation 1 crashed in chapter three: the resume keeps the chapters of generation 1, not 0."""
    gate = Gate("NS-E303", "story_draft.json", fail_times=0)
    ctx = _master(monkeypatch, tmp_home, gate, crash_on=3)
    ctx.write_json(regen.ATTEMPTS_FILE, {"regenerations": 1, "history": []})
    with pytest.raises(NsError):
        regen.run_job_guarded(ctx)
    before = _drafts(ctx)
    assert all(json.loads(before[f"ch{n}.json"])["generation"] == 1 for n in (1, 2))
    regen.run_job_guarded(ctx)
    assert _drafts(ctx)["ch1.json"] == before["ch1.json"] and _drafts(ctx)["ch2.json"] == before["ch2.json"]
    assert ctx.services.llm._takes("chapter_draft_1") == 1


def test_drafts_of_an_old_job_folder_without_a_generation_still_load(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    gate = Gate("NS-E303", "story_draft.json", fail_times=0)
    ctx = _master(monkeypatch, tmp_home, gate)
    regen.run_job_guarded(ctx)
    first = ctx.file("story_draft.json").read_bytes()
    for f in ctx.file("drafts").glob("ch*.json"):  # the shape before this fix: no `generation` key
        data = json.loads(f.read_text(encoding="utf-8"))
        data.pop("generation")
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    calls = ctx.services.llm.chapter_calls()
    state = StageState.load(ctx.job_dir)
    state.entries.pop("chapters_draft")
    state.save(ctx.job_dir)
    regen.run_job_guarded(ctx)
    assert ctx.services.llm.chapter_calls() == calls and ctx.file("story_draft.json").read_bytes() == first


def test_an_old_job_folder_that_was_already_regenerated_drafts_again_once(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """Drafts without a generation, in a folder whose history says it was sent back: they are generation 0."""
    gate = Gate("NS-E303", "story_draft.json", fail_times=0)
    ctx = _master(monkeypatch, tmp_home, gate)
    regen.run_job_guarded(ctx)
    for f in ctx.file("drafts").glob("ch*.json"):
        data = json.loads(f.read_text(encoding="utf-8"))
        data.pop("generation")
        f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    old_history = [{"code": "NS-E606", "detail": "tag", "rejected_to": "somewhere"}]  # no `idea` key
    ctx.write_json(regen.ATTEMPTS_FILE, {"regenerations": 1, "history": old_history})
    state = StageState.load(ctx.job_dir)
    state.entries.pop("chapters_draft")
    state.save(ctx.job_dir)
    regen.run_job_guarded(ctx)
    assert ctx.services.llm.chapter_calls() == 10 and ctx.generation == 1


# ------------------------------------------------------------ shorts only
@pytest.mark.parametrize("code", ["NS-E303", "NS-E602", "NS-E612"])
def test_a_regenerated_short_has_another_story_than_the_first_attempt(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch, code: str
):
    gate = Gate(code, "story.json")
    ctx = _shorts(monkeypatch, tmp_home, gate)
    assert regen.run_job_guarded(ctx) == SHORTS_PLAN
    first, second = (json.loads(s)["text"] for s in gate.seen)
    assert len(gate.seen) == 2 and first != second
    assert ctx.generation == 1


def test_a_resumed_short_keeps_its_story_and_its_hooks(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    gate = Gate("NS-E303", "story.json", fail_times=0)
    ctx = _shorts(monkeypatch, tmp_home, gate)
    regen.run_job_guarded(ctx)
    story = ctx.file("story.json").read_bytes()
    calls = len(ctx.services.llm.calls)
    state = StageState.load(ctx.job_dir)
    state.entries.pop("gate")
    state.save(ctx.job_dir)
    regen.run_job_guarded(ctx)
    assert ctx.file("story.json").read_bytes() == story and len(ctx.services.llm.calls) == calls


def test_a_regenerated_short_after_a_story_failure_ends_after_one_regeneration(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    gate = Gate("NS-E303", "story.json", fail_times=9)
    ctx = _shorts(monkeypatch, tmp_home, gate)
    with pytest.raises(NsError) as ei:
        regen.run_job_guarded(ctx)
    assert ei.value.code == "NS-E303" and len(gate.seen) == 2
    assert ctx.services.llm._takes("story_draft") == 2


# ------------------------------------------------------------ the generation itself
def test_generation_counts_regenerations_and_idea_redraws(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    assert ctx.generation == 0
    ctx.write_json(regen.ATTEMPTS_FILE, {"regenerations": 1, "history": [{"code": "NS-E606"}]})
    assert ctx.generation == 1  # a history entry of a regeneration is already in `regenerations`
    ctx.write_json(
        regen.ATTEMPTS_FILE,
        {"regenerations": 1, "history": [{"code": "NS-E606"}, {"code": "NS-E304", "idea": 1}]},
    )
    assert ctx.generation == 2 and ctx.attempt == 1  # a redraw is a new generation, not a hotter retry
    ctx.file(regen.ATTEMPTS_FILE).write_text('{"regenerations": 1, "hist', encoding="utf-8")
    assert ctx.generation == 0


def test_a_resume_does_not_change_the_generation(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    gate = Gate("NS-E602", "story_draft.json")
    ctx = _master(monkeypatch, tmp_home, gate)
    regen.run_job_guarded(ctx)
    assert ctx.generation == 1
    regen.run_job_guarded(ctx)  # nothing to do, nothing sent back
    assert ctx.generation == 1


def test_the_voice_take_of_an_earlier_generation_is_not_reused():
    item = TtsItem("p1", "One two three four.", "Ryan", 1.0)
    assert _signature(item) == _signature(item, 0)  # generation 0 keeps the cache of earlier jobs
    assert _signature(item, 1) != _signature(item, 0) and _signature(item, 1) == _signature(item, 1)
