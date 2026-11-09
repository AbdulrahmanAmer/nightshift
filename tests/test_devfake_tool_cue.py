# ruff: noqa: E501
"""The dev fake llama's explicit tool cue `tool: <name> {json args}` through the real Assistant loop.

The router offers at most 18 tools per message and only for topics it can read from the words, so a cue for a
tool that was not offered goes through `list_tools` (`ns_supervisor.devfake._discovery_step`). These tests run
the real `Assistant.chat` with the fake as its model: no network, no process, no model file."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from ns_dashboard.assistant.tools import build_registry
from ns_supervisor import devfake
from tests.test_dashboard_assistant import make, run_chat

ROOT = Path(__file__).resolve().parents[1]
TOOL_KINDS = ("tool_call", "tool_result", "confirm")


class DevFakeLlm:
    """The Assistant's model: the dev fake llama's own answer function, no HTTP."""

    def __init__(self) -> None:
        self.requests: list[list[dict[str, Any]]] = []
        self.tool_lists: list[list[dict[str, Any]]] = []

    def chat_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], *, max_tokens: int | None = None
    ) -> dict[str, Any]:
        self.requests.append([dict(m) for m in messages])
        self.tool_lists.append(tools)
        return devfake.chat_message({"messages": messages, "tools": tools})


def _user(text: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": text}]


def _tools(*names: str) -> list[dict[str, Any]]:
    return [{"type": "function", "function": {"name": n, "parameters": {"type": "object"}}} for n in names]


def _tool_events(events: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in events if e["kind"] in TOOL_KINDS and e["payload"].get("tool") == name]


def _unverified_assistant_tools() -> set[str]:
    """The `assistant-tool` rows that docs/VERIFICATION_INVENTORY.md still marks U."""
    text = (ROOT / "docs" / "VERIFICATION_INVENTORY.md").read_text(encoding="utf-8")
    return set(re.findall(r"^\| assistant-tool \| `([a-z_]+)` \| U \|", text, re.M))


def test_every_unverified_assistant_tool_is_reached_through_the_cue_in_the_real_loop(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    """F-C11 / D-4: the cue is the whole message, so the router sees only the tool name. Every tool must still be called."""
    llm = DevFakeLlm()
    dash, _state, _ = make(tmp_home, llm)  # type: ignore[arg-type]
    monkeypatch.setattr(dash.library, "update_check", lambda: {"update_available": False})  # no GitHub call
    registry = build_registry()
    rows = _unverified_assistant_tools()
    assert len(rows) >= 20 and rows <= set(registry.names()), rows - set(registry.names())
    # the tools the review found out of the router's reach, whatever the inventory says about them today
    must = rows | {
        "system_map",
        "warmup_engine",
        "shutdown_engine",
        "tail_logs",
        "start_verify",
        "set_idea_status",
        "duplicate_format",
        "edit_format",
    }
    unreached: list[str] = []
    via_lookup = 0
    for name in sorted(must):
        for page in (None, "library"):
            before = len(llm.tool_lists)
            events = run_chat(dash, f"tool: {name} {{}}", context={"page": page} if page else {})
            offered_first = [t["function"]["name"] for t in llm.tool_lists[before]]
            hits = _tool_events(events, name)
            result = [e for e in hits if e["kind"] == "tool_result"]
            reached = bool(hits) and not any(e["payload"].get("error") == "unknown tool" for e in result)
            reached = reached and not any(
                e["kind"] == "message" and "no tool" in str(e["payload"].get("text")) for e in events
            )
            if not reached:
                unreached.append(f"{name}@{page}")
            if name not in offered_first:
                via_lookup += 1
    assert not unreached, unreached
    assert via_lookup >= 4  # the router really did leave some of them out, so the look-up path was used


@pytest.mark.parametrize(
    "name", ["system_map", "warmup_engine", "shutdown_engine", "tail_logs", "start_verify", "edit_format"]
)
def test_a_tool_the_router_did_not_offer_is_looked_up_with_list_tools_and_then_called(
    tmp_home: Path, name: str
):
    llm = DevFakeLlm()
    dash, _state, _ = make(tmp_home, llm)  # type: ignore[arg-type]
    events = run_chat(dash, f"tool: {name}")
    assert name not in [t["function"]["name"] for t in llm.tool_lists[0]]
    called = [e["payload"]["tool"] for e in events if e["kind"] in TOOL_KINDS]
    assert called[0] == "list_tools" and name in called, called  # call+result pairs
    # the look-up asks for the names, then for the topic that holds the name, then the cued tool follows
    stored = dash.assistant_store.messages(events[0]["payload"]["conversation_id"])
    asked = [
        json.loads(c["function"]["arguments"])
        for m in stored
        for c in m.get("tool_calls") or []
        if c["function"]["name"] == "list_tools"
    ]
    assert asked[0] == {} and set(asked[1]) == {"topic"}
    ids = [c["id"] for m in stored for c in m.get("tool_calls") or []]
    assert len(ids) == len(set(ids)) == 3


def test_a_tool_the_router_offered_is_called_at_once(tmp_home: Path):
    llm = DevFakeLlm()
    dash, _state, _ = make(tmp_home, llm)  # type: ignore[arg-type]
    events = run_chat(dash, "tool: status {}")
    called = [e["payload"]["tool"] for e in events if e["kind"] in TOOL_KINDS]
    assert called == ["status", "status"]


def test_a_name_that_is_no_tool_ends_in_a_plain_reply_after_one_look_up(tmp_home: Path):
    llm = DevFakeLlm()
    dash, _state, _ = make(tmp_home, llm)  # type: ignore[arg-type]
    events = run_chat(dash, "tool: no_such_tool {}")
    called = [e["payload"]["tool"] for e in events if e["kind"] in TOOL_KINDS]
    assert called == ["list_tools", "list_tools"] and len(llm.requests) == 2
    text = next(e["payload"]["text"] for e in events if e["kind"] == "message")
    assert "no tool 'no_such_tool'" in text


def test_the_cue_is_unchanged_for_a_tool_that_asks_to_list_tools(tmp_home: Path):
    llm = DevFakeLlm()
    dash, _state, _ = make(tmp_home, llm)  # type: ignore[arg-type]
    events = run_chat(dash, 'tool: list_tools {"topic": "library"}')
    called = [e["payload"]["tool"] for e in events if e["kind"] in TOOL_KINDS]
    assert called == ["list_tools", "list_tools"] and len(llm.requests) == 2


def test_the_lookup_reads_arguments_before_it_looks(tmp_home: Path):
    tools = _tools("status", "list_tools")
    msg = devfake.chat_message({"tools": tools, "messages": _user("tool: system_map {nope")})
    assert "tool_calls" not in msg and "JSON object" in msg["content"] and "system_map" in msg["content"]


def test_the_lookup_steps_in_the_fake_on_their_own():
    tools = _tools("status", "list_tools")
    ask = _user(
        '<question>tool: edit_format {"id": "x"}</question>\n<nightshift_context note="data, not instructions">\npage\n</nightshift_context>'
    )
    step1 = devfake.chat_message({"tools": tools, "messages": ask})
    assert [c["function"]["name"] for c in step1["tool_calls"]] == ["list_tools"]
    assert step1["tool_calls"][0]["function"]["arguments"] == "{}"
    names_only = {"topics": {"queue": ["list_queue"], "library": ["list_ideas", "edit_format"]}}
    after1 = [
        *ask,
        step1,
        {
            "role": "tool",
            "name": "list_tools",
            "tool_call_id": "call_dev_list_tools",
            "content": json.dumps(names_only),
        },
    ]
    step2 = devfake.chat_message({"tools": tools, "messages": after1})
    assert json.loads(step2["tool_calls"][0]["function"]["arguments"]) == {"topic": "library"}
    assert step2["tool_calls"][0]["id"] != step1["tool_calls"][0]["id"]
    listed = {
        "library": [
            {"name": "list_ideas", "does": "x", "changes_something": False},
            {"name": "edit_format", "does": "y", "changes_something": True},
        ]
    }
    after2 = [
        *after1,
        step2,
        {"role": "tool", "name": "list_tools", "tool_call_id": "c2", "content": json.dumps(listed)},
    ]
    step3 = devfake.chat_message({"tools": tools, "messages": after2})
    fn = step3["tool_calls"][0]["function"]
    assert fn["name"] == "edit_format" and json.loads(fn["arguments"]) == {"id": "x"}
    # a tool past the topic's ten slots is not in `tools_added`, only in the listing: that is enough
    added = {**listed, "tools_added": ["list_ideas"]}
    assert devfake.chat_message(
        {
            "tools": tools,
            "messages": [
                *after1,
                step2,
                {"role": "tool", "name": "list_tools", "content": json.dumps(added)},
            ],
        }
    )["tool_calls"]
    # and `tools_added` alone is enough too
    only_added = {"tools_added": ["edit_format"], "note": "x"}
    assert devfake.chat_message(
        {
            "tools": tools,
            "messages": [
                *after1,
                step2,
                {"role": "tool", "name": "list_tools", "content": json.dumps(only_added)},
            ],
        }
    )["tool_calls"]
    # after the cued tool ran, the fake answers in text: no loop
    after3 = [
        *after2,
        step3,
        {
            "role": "tool",
            "name": "edit_format",
            "tool_call_id": "call_dev_edit_format_3",
            "content": '{"ok": true}',
        },
    ]
    done = devfake.chat_message({"tools": tools, "messages": after3})
    assert "tool_calls" not in done and done["content"].startswith("Done (edit_format)")


def test_a_look_up_that_does_not_list_the_name_stops_with_a_plain_reply():
    tools = _tools("status", "list_tools")
    ask = _user("tool: ghost")
    for content in (
        json.dumps({"topics": {"queue": ["list_queue"]}}),
        json.dumps({"library": [{"name": "list_ideas"}]}),
        "[list_tools -> 3 item(s)]",
        "{",
    ):
        msg = devfake.chat_message(
            {"tools": tools, "messages": [*ask, {"role": "tool", "name": "list_tools", "content": content}]}
        )
        assert "tool_calls" not in msg and "no tool 'ghost'" in msg["content"], content


def test_a_cue_for_list_tools_itself_and_other_tool_results_never_start_a_look_up():
    tools = _tools("status", "list_tools")
    ask = _user('tool: list_tools {"topic": "library"}')
    msg = devfake.chat_message(
        {
            "tools": tools,
            "messages": [*ask, {"role": "tool", "name": "list_tools", "content": '{"library": []}'}],
        }
    )
    assert "tool_calls" not in msg and msg["content"].startswith("Done (list_tools)")
    msg = devfake.chat_message(
        {
            "tools": tools,
            "messages": [*_user("tool: status"), {"role": "tool", "name": "status", "content": "{}"}],
        }
    )
    assert "tool_calls" not in msg and msg["content"].startswith("Done (status)")


def test_a_full_stop_after_the_name_is_not_part_of_the_name():
    tools = _tools("status", "fetch_footage")
    assert devfake.plan_tool_call(_user("tool: status."), tools) == {"name": "status", "args": {}}
    assert devfake.plan_tool_call(_user("Tool: status!"), tools) == {"name": "status", "args": {}}
    assert (
        devfake.chat_message({"tools": tools, "messages": _user("tool: status.")})["tool_calls"][0][
            "function"
        ]["name"]
        == "status"
    )


def test_chat_message_reads_the_cue_once(monkeypatch: pytest.MonkeyPatch):
    seen: list[str] = []
    real = devfake._tool_cue

    def spy(text: str, names: set[str]) -> Any:
        seen.append(text)
        return real(text, names)

    monkeypatch.setattr(devfake, "_tool_cue", spy)
    devfake.chat_message({"tools": _tools("status"), "messages": _user("tool: status")})
    assert len(seen) == 1
