import json
from pathlib import Path

import pytest

from ns_common.errors import NsError
from ns_common.models import PolicyCheck
from ns_studio import orchestrator as orch
from ns_studio.policy import gate, judge, regen
from ns_studio.policy.base import Check, fail, ok
from ns_studio.policy.text_checks import MisleadingClaims
from ns_studio.stages import REGISTRY, StageState, run_stage
from tests.helpers import make_ctx


class Always(Check):
    def __init__(self, name: str, passed: bool, gate_name: str = "text") -> None:
        self.name = name
        self.gate = gate_name  # type: ignore[assignment]
        self._passed = passed

    def run(self, ctx) -> PolicyCheck:
        return ok("fine") if self._passed else fail(f"{self.name} failed")


def test_run_gate_writes_all_results_then_raises_first_code(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    checks = [
        Always("real_entities", False),
        Always("misleading_claims", False),
        Always("unsafe_topics", True),
    ]
    with pytest.raises(NsError) as ei:
        gate.run_gate(ctx, "text", checks)
    assert ei.value.code == "NS-E602"
    data = ctx.read_json("policy_text.json")
    assert data["stage"] == "text" and data["passed"] is False
    assert set(data["checks"]) == {"real_entities", "misleading_claims", "unsafe_topics"}
    assert data["checks"]["unsafe_topics"]["passed"] is True


def test_run_gate_passes_and_final_has_advice(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("story.json", {"text": "It happened in Chicago."})
    res = gate.run_gate(ctx, "final", [Always("disclosure_advice", True, "final")])
    assert res.passed and res.advice.ai_disclosure == "yes"
    assert ctx.read_json("policy_final.json")["advice"]["ai_disclosure"] == "yes"


def test_text_stage_is_idempotent_and_detects_misleading_metadata(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.write_json("story.json", {"text": "A quiet house."})
    ctx.write_json("seo.json", [{"title": "True story of a quiet house", "description": "d",
                                 "tags": ["a"], "hashtags": ["#Shorts"]}])
    stage = gate.PolicyTextStage([MisleadingClaims()])
    with pytest.raises(NsError) as ei:
        run_stage(ctx, stage)
    assert ei.value.code == "NS-E603"
    ctx.write_json("seo.json", [{"title": "A quiet house", "description": "d", "tags": ["a"],
                                 "hashtags": ["#Shorts"]}])
    assert run_stage(ctx, stage) is True
    assert run_stage(ctx, stage) is False


def test_install_gates_places_stages_and_is_idempotent(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(orch.FORMAT_STAGES, "pf", ["ideas", "plan", "visual_plan", "voice", "qc", "outbox"])
    assert gate.install_gates("pf") is True
    assert orch.FORMAT_STAGES["pf"] == [
        "ideas", "plan", "visual_plan", "policy_text", "voice", "qc", "policy_final", "outbox",
    ]
    assert gate.install_gates("pf") is False
    assert {"policy_text", "policy_final"} <= set(REGISTRY)


def test_install_gates_fallbacks(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(orch.FORMAT_STAGES, "pg", ["a", "voice", "render", "qc"])
    gate.install_gates("pg")
    assert orch.FORMAT_STAGES["pg"] == ["a", "policy_text", "voice", "render", "qc", "policy_final"]
    monkeypatch.setitem(orch.FORMAT_STAGES, "ph", ["only"])
    gate.install_gates("ph")
    assert orch.FORMAT_STAGES["ph"] == ["only", "policy_text", "policy_final"]
    assert gate.install_gates("does_not_exist") is False


def test_judge_availability_matches_prompt_file():
    assert judge.available() == judge.PROMPT_FILE.exists() == gate.available()
    assert judge.verdict_failed({"verdict": "fail"})
    assert judge.verdict_failed({"flags": [{"category": "x"}]})
    assert not judge.verdict_failed({"verdict": "pass", "flags": []})


# ---------------------------------------------------------------- regenerate once
class Counter:
    """A stage that counts its runs in a shared dict."""

    def __init__(self, name: str, tenant, counts: dict[str, int]) -> None:
        self.name, self.tenant, self.inputs, self.outputs = name, tenant, (), (f"{name}.json",)
        self.counts = counts

    def run(self, ctx) -> None:
        self.counts[self.name] = self.counts.get(self.name, 0) + 1
        ctx.write_json(f"{self.name}.json", {"n": self.counts[self.name]})


class FlakyGate:
    """Fails with `code` for the first `fail_times` runs, then passes."""

    name, tenant, inputs, outputs = "gate_x", None, (), ("gate_x.json",)

    def __init__(self, code: str, fail_times: int, counts: dict[str, int]) -> None:
        self.code, self.fail_times, self.counts = code, fail_times, counts

    def run(self, ctx) -> None:
        n = self.counts.get("gate_x", 0)
        self.counts["gate_x"] = n + 1
        if n < self.fail_times:
            raise NsError(self.code, "seeded failure")
        ctx.write_json("gate_x.json", {"ok": True})


def _setup(monkeypatch, code: str, fail_times: int, counts: dict[str, int], plan: list[str]):
    for st in (
        Counter("ideas", "llama", counts),
        Counter("seo", "llama", counts),
        Counter("render", None, counts),
        FlakyGate(code, fail_times, counts),
    ):
        monkeypatch.setitem(REGISTRY, st.name, st)
    monkeypatch.setitem(orch.FORMAT_STAGES, "rg", plan)


def test_regenerates_once_from_earliest_stage_and_succeeds(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    counts: dict[str, int] = {}
    _setup(monkeypatch, "NS-E601", 1, counts, ["ideas", "seo", "gate_x", "render"])
    ctx = make_ctx(tmp_home, fmt="rg")
    ran = regen.run_job_guarded(ctx)
    assert ran == ["ideas", "seo", "gate_x", "render"]
    assert counts == {"ideas": 2, "seo": 2, "gate_x": 2, "render": 1}
    att = json.loads(ctx.file(regen.ATTEMPTS_FILE).read_text(encoding="utf-8"))
    assert att["regenerations"] == 1 and att["history"][0]["code"] == "NS-E601"
    rejected = list(ctx.paths.rejected.rglob("reason.json"))
    assert len(rejected) == 1 and json.loads(rejected[0].read_text(encoding="utf-8"))["code"] == "NS-E601"


def test_restart_stage_depends_on_code(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    counts: dict[str, int] = {}
    _setup(monkeypatch, "NS-E603", 1, counts, ["ideas", "seo", "gate_x", "render"])
    ctx = make_ctx(tmp_home, fmt="rg")
    regen.run_job_guarded(ctx)
    assert counts["ideas"] == 1 and counts["seo"] == 2
    assert regen.earliest_stage("NS-E999", ["first", "second"]) == "first"


def test_second_failure_ends_the_job(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    counts: dict[str, int] = {}
    _setup(monkeypatch, "NS-E605", 5, counts, ["ideas", "seo", "gate_x", "render"])
    ctx = make_ctx(tmp_home, fmt="rg")
    with pytest.raises(NsError) as ei:
        regen.run_job_guarded(ctx)
    assert ei.value.code == "NS-E605"
    assert counts == {"ideas": 2, "seo": 2, "gate_x": 2}
    assert len(list(ctx.paths.rejected.rglob("reason.json"))) == 2
    with pytest.raises(NsError):  # a resumed job does not get a second regeneration
        regen.run_job_guarded(ctx)
    assert counts["gate_x"] == 3


def test_non_policy_errors_pass_through(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    counts: dict[str, int] = {}
    _setup(monkeypatch, "NS-E301", 5, counts, ["ideas", "gate_x"])
    ctx = make_ctx(tmp_home, fmt="rg")
    with pytest.raises(NsError) as ei:
        regen.run_job_guarded(ctx)
    assert ei.value.code == "NS-E301" and counts == {"ideas": 1, "gate_x": 1}
    assert not ctx.file(regen.ATTEMPTS_FILE).exists()


def test_media_is_moved_to_rejected_on_final_gate_failure(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    counts: dict[str, int] = {}
    _setup(monkeypatch, "NS-E611", 1, counts, ["ideas", "render", "gate_x"])
    ctx = make_ctx(tmp_home, fmt="rg")
    ctx.file("long").mkdir()
    ctx.file("long/master.mp4").write_bytes(b"video")
    regen.run_job_guarded(ctx)
    moved = list(ctx.paths.rejected.rglob("master.mp4"))
    assert len(moved) == 1 and not ctx.file("long").exists()


def test_invalidate_from_drops_later_stage_state(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    st = StageState()
    for n in ("a", "b", "c"):
        st.mark_done(n, "h")
    st.save(ctx.job_dir)
    regen.invalidate_from(ctx, ["a", "b", "c"], "b")
    assert set(StageState.load(ctx.job_dir).entries) == {"a"}


# ---------------------------------------------------------------- default judge (C5 policy_check)
def _llm(answers):
    from ns_common.llm_client import FakeLlm
    from ns_studio.context import FIXTURES_LLM

    return FakeLlm(FIXTURES_LLM, overrides={"policy_check": answers})


def _pc(**kw):
    base = {
        "unsafe_categories": [], "hook_honest": True, "title_honest": True, "standalone": None, "notes": "",
    }
    return base | kw


def test_default_judge_unsafe_topics(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    ctx.services.llm = _llm(_pc(unsafe_categories=["minors"], notes="child in danger"))
    ans = judge.default_judge(ctx, "unsafe_topics", story="A story.")
    assert judge.verdict_failed(ans) and ans["flags"][0]["category"] == "minors"
    ctx.services.llm = _llm(_pc())
    assert not judge.verdict_failed(judge.default_judge(ctx, "unsafe_topics", story="A story."))


def test_default_judge_hook_title_and_standalone_per_item(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    items = [{"item": "s1", "title": "T1", "hook": "H1"}, {"item": "s2", "title": "T2", "hook": ""}]
    ctx.services.llm = _llm([_pc(hook_honest=False, notes="bait"), _pc()])
    ans = judge.default_judge(ctx, "hook_title_honesty", story="S", items=items)
    assert ans["failed_items"] == ["s1"] and "bait" in ans["reason"]
    moments = [{"k": 1, "text": "m1", "hook": ""}, {"k": 2, "text": "m2", "hook": ""}]
    ctx.services.llm = _llm([_pc(standalone=True), _pc(standalone=False, notes="needs context")])
    ans2 = judge.default_judge(ctx, "standalone_short", story="S", moments=moments)
    assert ans2["failed_items"] == ["2"]
    assert not judge.verdict_failed(judge.default_judge(ctx, "tag_relevance", story="S", items=[]))
    with pytest.raises(ValueError):
        judge.default_judge(ctx, "bogus", story="S")


def test_default_judge_passes_with_shipped_fixture(tmp_home: Path):
    from ns_studio.policy.text_checks import UnsafeTopics

    ctx = make_ctx(tmp_home)
    ctx.write_json("story.json", {"text": "A quiet house at night."})
    assert UnsafeTopics().run(ctx).passed


def test_tag_relevance_really_asks_the_model(tmp_home: Path):
    """System gap 13: the mode returned PASS without a call, so NS-E606 could never fire on irrelevant tags."""
    ctx = make_ctx(tmp_home)
    items = [{"item": "s1", "title": "T", "tags": ["horror story", "crypto giveaway"]}]
    ctx.services.llm = _llm(_pc(tags_relevant=False, notes="crypto giveaway is unrelated"))
    ans = judge.default_judge(ctx, "tag_relevance", story="S", items=items)
    assert judge.verdict_failed(ans) and ans["failed_items"] == ["s1"] and "crypto" in ans["reason"]
    call = ctx.services.llm.calls[-1]  # type: ignore[attr-defined]
    assert "Tags: horror story, crypto giveaway" in call["user"]
    ctx.services.llm = _llm(_pc(tags_relevant=True))
    assert not judge.verdict_failed(judge.default_judge(ctx, "tag_relevance", story="S", items=items))
