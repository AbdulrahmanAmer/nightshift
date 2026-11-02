"""Re-review row 7: the regenerate-once table knows the master_first stage names."""

from pathlib import Path

import pytest

from ns_studio import orchestrator as orch
from ns_studio.policy import regen
from ns_studio.stages import REGISTRY
from tests.helpers import make_ctx
from tests.test_policy_gate import Counter, FlakyGate

MASTER = list(orch.MASTER_FIRST_STAGES)
SCRIPT = list(orch.MASTER_FROM_SCRIPT_STAGES)
SHORTS = list(orch.SHORTS_ONLY_STAGES)


@pytest.mark.parametrize(
    ("code", "master", "shorts"),
    [
        ("NS-E602", "chapters_draft", "draft"),
        ("NS-E603", "seo_long", "seo"),
        ("NS-E604", "shorts_select", "hooks"),
        ("NS-E606", "seo_long", "seo"),
        ("NS-E607", "shorts_select", "ideas"),
        ("NS-E612", "chapters_draft", "draft"),
    ],
)
def test_every_plan_has_a_restart_stage_that_is_not_ideas(code: str, master: str, shorts: str):
    """Before: only the shorts_only names matched, so a master fell back to `ideas` (about an hour)."""
    assert regen.earliest_stage(code, MASTER) == master
    assert regen.earliest_stage(code, SHORTS) == shorts


def test_a_series_part_regenerates_its_metadata_in_place():
    assert regen.earliest_stage("NS-E606", SCRIPT) == "seo_long"
    assert regen.earliest_stage("NS-E603", SCRIPT) == "seo_long"


def test_one_disliked_tag_redoes_only_the_seo_stages(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    """NS-E606 on a master: the chapters, the continuity pass and the Shorts selection stay done."""
    counts: dict[str, int] = {}
    plan = ["ideas", "long_plan", "chapters_draft", "continuity", "seo_long", "shorts_select", "seo_shorts"]
    for name in plan:
        monkeypatch.setitem(REGISTRY, name, Counter(name, "llama", counts))
    monkeypatch.setitem(REGISTRY, "gate_x", FlakyGate("NS-E606", 1, counts))
    monkeypatch.setitem(orch.FORMAT_STAGES, "rg", [*plan, "gate_x"])
    ctx = make_ctx(tmp_home, fmt="rg")
    ran = regen.run_job_guarded(ctx)
    assert ran == [*plan, "gate_x"]
    assert counts == {
        "ideas": 1, "long_plan": 1, "chapters_draft": 1, "continuity": 1, "shorts_select": 1,
        "seo_long": 2, "seo_shorts": 2, "gate_x": 2,
    }  # fmt: skip


def test_a_second_dislike_still_ends_the_job(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    from ns_common.errors import NsError

    counts: dict[str, int] = {}
    plan = ["ideas", "chapters_draft", "seo_long"]
    for name in plan:
        monkeypatch.setitem(REGISTRY, name, Counter(name, "llama", counts))
    monkeypatch.setitem(REGISTRY, "gate_x", FlakyGate("NS-E606", 5, counts))
    monkeypatch.setitem(orch.FORMAT_STAGES, "rg", [*plan, "gate_x"])
    ctx = make_ctx(tmp_home, fmt="rg")
    with pytest.raises(NsError) as ei:
        regen.run_job_guarded(ctx)
    assert ei.value.code == "NS-E606" and counts["chapters_draft"] == 1 and counts["seo_long"] == 2


def test_other_codes_still_restart_from_their_stage(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    counts: dict[str, int] = {}
    plan = ["ideas", "chapters_draft", "seo_long", "shorts_select"]
    for name in plan:
        monkeypatch.setitem(REGISTRY, name, Counter(name, "llama", counts))
    monkeypatch.setitem(REGISTRY, "gate_x", FlakyGate("NS-E607", 1, counts))
    monkeypatch.setitem(orch.FORMAT_STAGES, "rg", [*plan, "gate_x"])
    regen.run_job_guarded(make_ctx(tmp_home, fmt="rg"))
    assert counts["chapters_draft"] == 1 and counts["seo_long"] == 1 and counts["shorts_select"] == 2
