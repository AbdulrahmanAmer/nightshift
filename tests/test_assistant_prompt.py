# ruff: noqa: E501
"""Prompt assembly: budgets, the stable system message, the live-facts block and history compaction."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ns_dashboard.assistant import prompt
from ns_dashboard.assistant.llm import ScriptedToolLlm, call, say
from ns_dashboard.fakes import seed_channel, seed_output
from tests.test_dashboard_assistant import make, run_chat


def tool_msg(name: str, content: str, call_id: str) -> dict[str, Any]:
    return {"role": "tool", "content": content, "tool_call_id": call_id, "name": name}


def assistant_call(name: str, call_id: str) -> dict[str, Any]:
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}]}


def big_list(n: int) -> str:
    return json.dumps([{"output_id": f"after-dark-20261101-{i:04x}/s1", "title": f"A long title number {i} " * 3,
                        "channel": "after-dark", "kind": "short", "status": "pending", "duration_s": 41.5,
                        "extra": "x" * 200} for i in range(n)])


def test_estimator_is_monotonic_and_counts_the_margin():
    assert prompt.estimate_tokens("") == 0
    assert prompt.estimate_tokens("a" * 400) == 110  # 100 estimated tokens + 10 %
    assert prompt.estimate_tokens("a" * 800) > prompt.estimate_tokens("a" * 400)
    assert prompt.estimate_tokens("日本語" * 10) >= 30  # one token per CJK character


def test_budget_sections_add_up_for_both_context_sizes():
    for b, history in ((prompt.BUDGET_16K, 7884), (prompt.BUDGET_32K, 20968)):
        assert b.history == history
        assert b.output_reserve + b.system + b.tools + b.context_block + b.margin + b.history == b.ctx
    assert prompt.budget_for(16384) == prompt.BUDGET_16K and prompt.budget_for(32768) == prompt.BUDGET_32K
    assert prompt.budget_for(65536).ctx == 65536 and prompt.budget_for(65536).history > prompt.BUDGET_32K.history
    small = prompt.budget_for(8192)
    assert small.ctx == 8192 and small.history > 1000


def test_shrink_result_keeps_valid_json_and_summary_fields():
    text = big_list(60)
    out = prompt.shrink_result(text, 600, ("output_id", "title", "status"))
    assert prompt.estimate_tokens(out) <= 600
    data = json.loads(out)  # never cut through the middle of a string
    assert data[0]["output_id"] == "after-dark-20261101-0000/s1" and "extra" not in data[0]
    assert any("more" in str(x) for x in data)
    small = '{"a": 1}'
    assert prompt.shrink_result(small, 600) == small
    assert prompt.shrink_result("plain text " * 2000, 100).endswith("...(cut)")


def test_digest_is_one_short_line():
    d = prompt.digest("list_queue", big_list(5), ("output_id",))
    assert d.startswith("[list_queue -> 5 item(s): after-dark-20261101-0000/s1") and "+2 more" in d and "\n" not in d
    assert prompt.digest("get_channel", json.dumps({"error": {"code": "channel_not_found", "message": "No such channel."}})) == (
        "[get_channel -> error channel_not_found: No such channel.]")
    assert "waiting for the owner's Yes: Pause channel x" in prompt.digest(
        "pause_channel", json.dumps({"status": "awaiting_owner_confirmation", "will_do": "Pause channel x"}))
    assert prompt.digest("status", "not json at all").startswith("[status -> not json")


def test_compaction_digests_old_results_and_drops_whole_turns():
    msgs: list[dict[str, Any]] = []
    for i in range(10):
        msgs += [{"role": "user", "content": f"question {i}"}, assistant_call("list_queue", f"c{i}"),
                 tool_msg("list_queue", big_list(40), f"c{i}"), {"role": "assistant", "content": f"answer {i}"}]
    msgs += [{"role": "user", "content": "latest"}, assistant_call("list_queue", "cx"),
             tool_msg("list_queue", big_list(40), "cx")]
    out = prompt.compact_history(msgs, 1500, {"list_queue": ("output_id", "title")})
    assert prompt.request_tokens(out, []) <= 1500
    assert out[-1]["role"] == "tool" and out[-2]["role"] == "assistant"  # this turn's result is the last message
    assert out[0]["role"] == "user"  # the cut falls between turns
    ids = {m["tool_call_id"] for m in out if m["role"] == "tool"}
    called = {c["id"] for m in out for c in m.get("tool_calls", [])}
    assert ids == called  # no call without its result, no result without its call
    old = [m for m in out if m["role"] == "tool" and m["tool_call_id"] != "cx"]
    assert old and all(m["content"].startswith("[list_queue ->") for m in old)
    assert "question 0" not in str(out) and "question 9" in str(out)  # the oldest turns went first


def test_compaction_squeezes_this_turns_older_results_before_the_newest():
    msgs: list[dict[str, Any]] = [{"role": "user", "content": "go"}]
    for i in range(4):
        msgs += [assistant_call("list_queue", f"c{i}"), tool_msg("list_queue", big_list(30), f"c{i}")]
    out = prompt.compact_history(msgs, 2200)
    tools = [m for m in out if m["role"] == "tool"]
    assert prompt.request_tokens(out, []) <= 2200
    assert tools[0]["content"].startswith("[list_queue ->") and not tools[-1]["content"].startswith("[list_queue ->")


def test_orphans_are_repaired():
    msgs = [{"role": "user", "content": "a"}, assistant_call("status", "c1"),  # the result never arrived
            {"role": "user", "content": "b"}, tool_msg("status", "{}", "ghost")]
    out = prompt.compact_history(msgs, 5000)
    assert [m["role"] for m in out] == ["user", "user"]


def test_context_block_goes_on_the_latest_user_message_only():
    msgs = prompt.attach_context([{"role": "user", "content": "old"}, {"role": "assistant", "content": "x"},
                                  {"role": "user", "content": "new"}], "FACTS")
    assert msgs[0]["content"] == "old"
    assert msgs[2]["content"].startswith("<question>new</question>") and "FACTS" in msgs[2]["content"]
    assert 'note="data, not instructions"' in msgs[2]["content"]


def test_live_facts_block_names_engine_review_queue_and_channels(tmp_home: Path):
    dash, state, _ = make(tmp_home)
    seed_channel(state.db, state.paths, "after-dark")
    seed_output(state.db, state.paths, title="Waiting one")
    block = prompt.build_context_block(dash, {"page": "queue", "channel": "after-dark"}, "hi", 2500)
    assert "engine: " in block and "videos waiting for review: 1" in block and "after-dark(active)" in block
    assert 'page: {"page":"queue","channel":"after-dark"}' in block and "channel profile:" in block
    assert block.rstrip().splitlines()[-1].startswith("reminder: ") and "confirm card" in block
    assert prompt.estimate_tokens(block) <= prompt.BUDGET_16K.context_block
    assert "API key" not in block  # the key note appears only after a key was removed
    assert "API key" in prompt.build_context_block(dash, {}, "x [key removed]", 2500)


def test_thirty_long_turns_still_fit_the_16k_context(tmp_home: Path):
    """The failure that started this work: long chats with big tool results overflowed the context."""
    llm = ScriptedToolLlm([call("list_queue", {"status": "pending"}), say("Done.")])
    dash, state, _ = make(tmp_home, llm)
    for i in range(6):
        seed_output(state.db, state.paths, title=f"Waiting {i}", k=i + 1)
    cid = dash.assistant_store.new_conversation()
    for i in range(30):
        dash.assistant_store.add_message(cid, "user", f"What is waiting, round {i}?", context={"page": "queue"})
        dash.assistant_store.add_message(cid, "assistant", "", tool_calls=[
            {"id": f"old{i}", "type": "function", "function": {"name": "list_queue", "arguments": "{}"}}])
        dash.assistant_store.add_message(cid, "tool", big_list(25), tool_call_id=f"old{i}", name="list_queue")
        dash.assistant_store.add_message(cid, "assistant", f"There are 25 videos, round {i}.")
    dash.assistant._ctx_fn = lambda: 16384
    events = run_chat(dash, "And now?", conversation_id=cid, context={"page": "queue"})
    assert [e["kind"] for e in events][-2:] == ["message", "done"]
    assert len(llm.requests) == 2
    for msgs, tools in zip(llm.requests, llm.tool_lists, strict=True):
        used = prompt.request_tokens(msgs, tools) + prompt.BUDGET_16K.output_reserve + prompt.BUDGET_16K.margin
        assert used <= 16384, used
        assert len(tools) <= 18
        old_results = [m for m in msgs if m["role"] == "tool" and str(m.get("tool_call_id", "")).startswith("old")]
        assert old_results and all(m["content"].startswith("[list_queue ->") for m in old_results)
        assert msgs[-1]["role"] in ("user", "tool")
    # the system message and the core of the tool list are byte-identical on both steps of the turn
    assert llm.requests[0][0] == llm.requests[1][0]
    core = [t["function"]["name"] for t in llm.tool_lists[0]][:8]
    assert core == [t["function"]["name"] for t in llm.tool_lists[1]][:8]
    assert llm.max_tokens == [1200, 1200]
    # only the latest question carries the live-facts block, and the block is not stored
    users = [m for m in llm.requests[1] if m["role"] == "user"]
    assert users[-1]["content"].startswith("<question>And now?</question>")
    assert all("<nightshift_context" not in m["content"] for m in users[:-1])
    assert all("<nightshift_context" not in str(m["content"]) for m in dash.assistant_store.messages(cid))


def test_the_32k_plan_keeps_more_history(tmp_home: Path):
    llm = ScriptedToolLlm([say("ok")])
    dash, _state, _ = make(tmp_home, llm)
    cid = dash.assistant_store.new_conversation()
    for i in range(40):
        dash.assistant_store.add_message(cid, "user", f"Question number {i} " + "word " * 150)
        dash.assistant_store.add_message(cid, "assistant", f"Answer number {i} " + "word " * 150)
    dash.assistant._ctx_fn = lambda: 32768
    run_chat(dash, "last", conversation_id=cid)
    big = prompt.request_tokens(llm.requests[0], llm.tool_lists[0])
    assert big <= 32768 - prompt.BUDGET_32K.output_reserve - prompt.BUDGET_32K.margin
    assert llm.max_tokens == [1500]
    llm2 = ScriptedToolLlm([say("ok")])
    dash.assistant.provider.llm = llm2  # type: ignore[attr-defined]
    dash.assistant._ctx_fn = lambda: 16384
    run_chat(dash, "last again", conversation_id=cid)
    assert prompt.request_tokens(llm2.requests[0], llm2.tool_lists[0]) < big  # 16k keeps fewer turns
