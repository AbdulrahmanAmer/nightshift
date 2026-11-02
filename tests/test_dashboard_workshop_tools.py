"""Gap 7: the Series Workshop tools are mounted in the Assistant registry and run behind confirm cards."""

from __future__ import annotations

from pathlib import Path

import pytest

from ns_dashboard.assistant.llm import ScriptedToolLlm, call, say
from ns_dashboard.assistant.registry import ToolContext, render_confirm
from ns_dashboard.assistant.tools import build_registry
from ns_dashboard.fakes import seed_channel
from ns_studio.series.tools import TOOLS
from tests.test_dashboard_assistant import kinds, make, run_chat


def test_every_workshop_tool_is_in_the_registry():
    reg = build_registry()
    assert len(TOOLS) >= 11
    for d in TOOLS:
        td = reg.get(d["name"])
        assert td is not None, d["name"]
        assert td.spec.side_effect == d["side_effect"] and td.spec.topic == "series"
        assert td.route is None
        if d["side_effect"]:
            assert td.spec.confirm_text == d["confirm_text"]
    produce = reg.get("workshop_produce")
    assert produce is not None
    assert reg.validate("workshop_produce", {"slug": "the-lift", "part": 2}) is None
    assert reg.validate("workshop_produce", {"slug": "the-lift", "part": "all"}) is None
    assert reg.validate("workshop_produce", {"slug": "the-lift", "part": 1.5}) is not None
    assert reg.validate("workshop_produce", {}) is not None
    assert "Queue production of story the-lift" in render_confirm(produce.spec, {"slug": "the-lift"})


def test_read_only_workshop_tools_run_at_once(tmp_home: Path):
    llm = ScriptedToolLlm([call("workshop_list", {}), say("There are no stories yet.")])
    dash, _state, _ = make(tmp_home, llm)
    events = run_chat(dash, "which series do we have?")
    assert kinds(events) == ["conversation", "tool_call", "tool_result", "message", "done"]
    result = events[2]["payload"]
    assert result["ok"] is True and result["result"] == []


def test_starting_a_story_waits_for_yes_then_creates_the_folder(tmp_home: Path):
    llm = ScriptedToolLlm([
        call("workshop_start", {"channel": "after-dark"}),
        say("I asked you to confirm."),
    ])
    dash, state, _ = make(tmp_home, llm)
    seed_channel(state.db, state.paths, "after-dark")
    events = run_chat(dash, "start a series about a lift")
    card = next(e["payload"] for e in events if e["kind"] == "confirm")
    assert card["tool"] == "workshop_start" and "after-dark" in card["text"]
    assert not (tmp_home / "stories").exists() or not any((tmp_home / "stories").iterdir())
    out = dash.assistant.confirm(card["action_id"])
    assert out["status"] == "done", out
    slug = out["result"]["slug"]
    assert (tmp_home / "stories" / slug / "concept.md").exists()
    listed = dash.registry.get("workshop_list")
    assert listed is not None
    rows = listed.call(ToolContext(dash), {})
    assert [r["slug"] for r in rows] == [slug]


def test_a_wrong_argument_is_returned_to_the_model(tmp_home: Path):
    llm = ScriptedToolLlm([call("workshop_map", {"slug": "x"}), say("I need the number of parts.")])
    dash, _state, _ = make(tmp_home, llm)
    events = run_chat(dash, "map it")
    bad = next(e["payload"] for e in events if e["kind"] == "tool_result")
    assert bad["ok"] is False and "parts" in bad["error"]


@pytest.mark.parametrize("name", [d["name"] for d in TOOLS if d["side_effect"]])
def test_workshop_changes_never_run_without_a_card(tmp_home: Path, name: str):
    required = next(d for d in TOOLS if d["name"] == name)["args_schema"]["required"]
    args = {k: ("A" if k == "letter" else 1 if k in ("n", "parts") else "x") for k in required}
    dash, _state, _ = make(tmp_home, ScriptedToolLlm([call(name, args), say("ok")]))
    events = run_chat(dash, "do it")
    assert [e["kind"] for e in events if e["kind"] == "confirm"] == ["confirm"]
