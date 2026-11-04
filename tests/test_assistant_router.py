# ruff: noqa: E501
"""The topic router: which tools the model sees for a question."""

from __future__ import annotations

from pathlib import Path

from ns_dashboard.assistant import router
from ns_dashboard.assistant.llm import ScriptedToolLlm, call, say
from ns_dashboard.assistant.registry import TOPICS
from ns_dashboard.assistant.tools import build_registry
from tests.test_dashboard_assistant import make, run_chat

SLUGS = ["after-dark", "wholesome-twist"]


def test_topics_follow_phrases_pages_codes_and_entities():
    assert router.route("Why did last night's run fail?") == ["diagnostics"]
    assert router.route("What went wrong with NS-E316?")[0] == "diagnostics"
    assert router.route("Anything to check before I post this?", {"page": "queue"})[0] == "queue"
    assert router.route("What runs tonight?")[0] == "create"
    assert router.route("Which hooks work best on After Dark?", slugs=SLUGS)[0] == "results"
    assert router.route("Never use spiders on Wholesome Twist.", slugs=SLUGS) == ["channels"]
    assert router.route("Get me foggy forest clips") == ["library"]
    assert router.route("In the lighthouse series, did we reveal who wrote the letters?") == ["series"]
    assert router.route("What is after-dark-20261101-ab12/s1 about?")[0] == "queue"
    assert router.route("hello") == []
    assert router.route("hello", {"page": "settings"}) == ["settings"]
    # the previous turn's topic is sticky for one turn but not strong enough alone
    assert router.route("and the next one?", previous=["library"]) == []
    assert router.route("and the next one?", {"page": "library"}, previous=["library"]) == ["library"]


def test_at_most_two_topics_and_the_second_needs_a_real_hit():
    out = router.route("Why did the clips job fail?")
    assert len(out) <= 2 and "diagnostics" in out
    assert router.route("Make the narration calmer") == ["channels"]


def test_offer_is_core_plus_at_most_ten_and_never_more_than_eighteen():
    reg = build_registry()
    assert len(router.CORE) == 8 and router.MAX_TOOLS == 18
    for question, topics in (
        ("hello", []),
        ("Why did the clips job fail?", ["diagnostics", "create"]),
        ("Give me new topics from what worked", ["library", "results"]),
        ("which series do we have", ["series"]),
        ("anything", [t for t in TOPICS if t != "orientation"][:2]),
    ):
        names = router.select_tools(reg, topics, question)
        assert names[:8] == sorted(router.CORE) and len(names) <= 18, (question, names)
        assert len(set(names)) == len(names) and all(reg.get(n) for n in names)
    assert len(router.select_tools(reg, [], "hello")) == 8
    assert len(router.select_tools(reg, ["series"], "series")) == 18  # a full pack, 12 tools cut to 10


def test_every_registered_tool_is_in_some_pack_or_core():
    reg = build_registry()
    reachable = set(router.CORE)
    for t in TOPICS:
        reachable |= set(router._pack(reg, t, ""))
    assert set(reg.names()) <= reachable, set(reg.names()) - reachable
    assert set(router.PACKS) <= set(TOPICS)


def test_fetch_footage_lands_in_the_library_topic():
    reg = build_registry()
    spec = reg.get("fetch_footage")
    assert spec is not None and spec.spec.topic == "library" and spec.spec.side_effect
    assert "fetch_footage" in router.PACKS["library"]
    for q in ("Get me 3 vertical foggy forest clips", "fetch rain footage", "do we have rain on window clips"):
        topics = router.route(q)
        assert "library" in topics, q
        assert "fetch_footage" in router.select_tools(reg, topics, q), q


def test_hints_pull_the_right_tool_out_of_a_long_pack():
    reg = build_registry()
    assert "design_voice" in router.select_tools(reg, ["library", "queue"], "design a new gravelly voice")
    assert "clone_voice" in router.select_tools(reg, ["library", "queue"], "clone the voice in my recording")
    names = router.select_tools(reg, ["series"], "rewrite part 2 of the series")
    assert {"workshop_revise_part", "workshop_status", "workshop_list"} <= set(names)


def test_list_tools_escape_hatch_adds_a_topic_once_per_turn(tmp_home: Path):
    llm = ScriptedToolLlm([
        call("list_tools", {"topic": "series"}, call_id="a"),
        call("list_tools", {"topic": "series"}, call_id="b"),
        say("Now I can see the series tools."),
    ])
    dash, _state, _ = make(tmp_home, llm)
    events = run_chat(dash, "hello")
    first = [t["function"]["name"] for t in llm.tool_lists[0]]
    second = [t["function"]["name"] for t in llm.tool_lists[1]]
    assert "workshop_list" not in first and "workshop_list" in second and len(second) <= 18
    assert second[:8] == first[:8]
    results = [e["payload"]["result"] for e in events if e["kind"] == "tool_result"]
    assert "workshop_list" in results[0]["tools_added"] and "Tools added" in results[0]["note"]
    assert "tools_added" not in results[1]  # a second request for the same topic adds nothing
    assert [t["function"]["name"] for t in llm.tool_lists[2]] == second


def test_list_tools_without_a_topic_is_names_only_and_small(tmp_home: Path):
    dash, _state, _ = make(tmp_home)
    from ns_dashboard.assistant.prompt import estimate_json
    from ns_dashboard.assistant.registry import ToolContext

    td = dash.registry.get("list_tools")
    assert td is not None
    out = td.call(ToolContext(dash), {})
    assert set(out["topics"]) >= {"queue", "series", "library"} and out["topics"]["queue"].count("review_brief") == 1
    assert estimate_json(out) < 1200
    bad = td.call(ToolContext(dash), {"topic": "nonsense"})
    assert bad["error"]["code"] == "unknown_topic"


def test_previous_turn_topics_stay_in_play(tmp_home: Path):
    llm = ScriptedToolLlm([call("list_ideas", {}), say("Two ideas."), say("Here is one more.")])
    dash, _state, _ = make(tmp_home, llm)
    events = run_chat(dash, "show me the ideas", context={"page": "library"})
    cid = events[0]["payload"]["conversation_id"]
    run_chat(dash, "and the next one?", conversation_id=cid, context={"page": "library"})
    last = [t["function"]["name"] for t in llm.tool_lists[-1]]
    assert "list_ideas" in last and "add_idea" in last
