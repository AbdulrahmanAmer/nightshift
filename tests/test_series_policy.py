from pathlib import Path
from typing import Any

import pytest

from ns_common.errors import NsError
from ns_common.llm_client import FakeLlm
from ns_studio.context import FIXTURES_LLM
from ns_studio.series import policy as G
from tests.helpers import make_ctx

CLEAN = {
    "unsafe_categories": [],
    "hook_honest": True,
    "title_honest": True,
    "standalone": True,
    "notes": "Fine.",
}


def ctx_with(tmp_home: Path, answer: dict[str, Any] | None):
    ctx = make_ctx(tmp_home)
    ctx.services.llm = FakeLlm(FIXTURES_LLM, overrides={} if answer is None else {"policy_check": answer})
    return ctx


def calls(ctx) -> list[dict]:
    return ctx.services.llm.calls


def test_policy_lists_are_lower_cased_without_comments():
    block = G.policy_list("real_entities_block.txt")
    assert "taylor swift" in block and all(p == p.lower() and not p.startswith("#") for p in block)
    assert "true story" in G.policy_list("misleading_claims.txt")


def test_real_entity_hits_use_word_boundaries():
    assert G.real_entity_hits("She met Taylor Swift at the gate.") == ["taylor swift"]
    assert G.real_entity_hits("The googled answer was wrong.") == []
    assert G.real_entity_hits("Nothing here.") == []


def test_claim_hits():
    assert G.claim_hits("A True Story of the night") == ["true story"]
    assert G.claim_hits("An original tale") == []


def test_entities_gate_raises_e602_before_any_llm_call(tmp_home: Path):
    ctx = ctx_with(tmp_home, CLEAN)
    with pytest.raises(NsError) as err:
        G.text_gate(ctx, "Taylor Swift found a camera on her porch.", what="concept A")
    assert err.value.code == "NS-E602" and "taylor swift" in err.value.detail
    assert calls(ctx) == []


def test_titles_are_checked_for_real_names_and_claims(tmp_home: Path):
    ctx = ctx_with(tmp_home, CLEAN)
    with pytest.raises(NsError) as e1:
        G.text_gate(ctx, "A quiet story.", what="map", titles=["My True Story"])
    assert e1.value.code == "NS-E603" and calls(ctx) == []
    with pytest.raises(NsError) as e2:
        G.text_gate(ctx, "A quiet story.", what="map", titles=["Elon Musk Vanishes"])
    assert e2.value.code == "NS-E602"


def test_clean_text_passes_and_asks_the_classifier_once(tmp_home: Path):
    ctx = ctx_with(tmp_home, None)  # the shipped policy_check fixture
    G.text_gate(ctx, "A nurse hears a chime.", what="script", titles=["Wet Footprints"])
    assert [c["prompt_name"] for c in calls(ctx)] == ["policy_check"]
    assert "A nurse hears a chime." in calls(ctx)[0]["user"]


def test_classifier_categories_map_to_codes(tmp_home: Path):
    ctx = ctx_with(tmp_home, {**CLEAN, "unsafe_categories": ["graphic_harm"], "notes": "Explicit gore."})
    with pytest.raises(NsError) as err:
        G.text_gate(ctx, "A calm text.", what="script")
    assert err.value.code == "NS-E601" and "graphic_harm" in err.value.detail

    ctx = ctx_with(tmp_home, {**CLEAN, "unsafe_categories": ["real_people", "hate"]})
    with pytest.raises(NsError) as err2:
        G.text_gate(ctx, "A calm text.", what="script")
    assert err2.value.code == "NS-E602"


def test_title_and_hook_honesty_is_judged_when_given(tmp_home: Path):
    ctx = ctx_with(tmp_home, {**CLEAN, "hook_honest": False, "notes": "Promises a death."})
    G.text_gate(ctx, "A calm text.", what="script")  # no title or hook given: not judged
    with pytest.raises(NsError) as err:
        G.text_gate(ctx, "A calm text.", what="script", title="Wet Footprints", hook="Someone dies.")
    assert err.value.code == "NS-E604"
    user = calls(ctx)[-1]["user"]
    assert "Title: Wet Footprints" in user and "Hook: Someone dies." in user


def test_is_standalone(tmp_home: Path):
    ctx = ctx_with(tmp_home, {**CLEAN, "standalone": False})
    assert G.is_standalone(ctx, story="Long chapter.", moment="A moment.") is False
    assert "A moment." in calls(ctx)[-1]["user"]
    ctx = ctx_with(tmp_home, {**CLEAN, "standalone": None})
    assert G.is_standalone(ctx, story="Long chapter.", moment="A moment.") is True
    ctx = ctx_with(tmp_home, CLEAN)
    assert G.is_standalone(ctx, story="Long chapter.", moment="A moment.") is True
