# ruff: noqa: E501
import json
import string
from pathlib import Path
from typing import Any

import pytest

from ns_common.paths import Paths, repo_root
from ns_dashboard.assistant import docs_index
from ns_dashboard.assistant.llm import ScriptedToolLlm, StaticProvider, call, calls, say
from ns_dashboard.assistant.loop import Assistant, fill_from_context, load_prompt
from ns_dashboard.assistant.orientation import compact_map, system_map
from ns_dashboard.assistant.registry import TOPICS, ToolContext, check_value, obj, render_confirm
from ns_dashboard.assistant.store import AssistantStore
from ns_dashboard.assistant.tools import NO_TOOL, build_registry
from ns_dashboard.errors import ApiError
from ns_dashboard.fakes import seed_channel, seed_output
from ns_dashboard.handlers import Dashboard, build_state
from ns_dashboard.supervisor_link import SupervisorLink

NEVER_TOOLS = {
    "approve_output", "reject_output", "mark_uploaded", "set_key", "set_keys", "uninstall", "apply_update",
    "update_apply", "set_flags", "edit_flags", "stop_all", "approve", "reject",
}


def make(tmp_home: Path, llm: ScriptedToolLlm | None = None, busy: str | None = None):
    state = build_state(
        Paths.from_env(), fake_services=True, link=SupervisorLink("http://127.0.0.1:9", timeout_s=0.2)
    )
    provider = StaticProvider(llm or ScriptedToolLlm([]), busy_owner=busy)
    dash = Dashboard(state, llm_provider=provider)
    return dash, state, provider


def kinds(events: list[dict[str, Any]]) -> list[str]:
    return [e["kind"] for e in events]


def run_chat(dash: Dashboard, text: str, **kw: Any) -> list[dict[str, Any]]:
    return list(dash.assistant.chat(text, **kw))


# ------------------------------------------------------------------ descriptors


def test_every_descriptor_validates():
    jsonschema = pytest.importorskip("jsonschema")
    reg = build_registry()
    assert len(reg.names()) >= 40
    for td in reg.all():
        s = td.spec
        assert s.name.replace("_", "").isalnum() and s.name == s.name.lower(), s.name
        assert 10 <= len(s.description) <= 160, s.name
        assert isinstance(s.side_effect, bool) and s.topic in TOPICS, s.name
        jsonschema.Draft202012Validator.check_schema(s.args_schema)
        assert s.args_schema["type"] == "object" and s.args_schema["additionalProperties"] is False
        props = s.args_schema["properties"]
        assert set(s.args_schema["required"]) <= set(props), s.name
        for p in props.values():
            assert p.get("description") or p.get("type") in ("boolean",) or "enum" in p or "pattern" in p, s.name
        fn = s.openai()["function"]
        assert fn["name"] == s.name and fn["parameters"] is s.args_schema
        fields = {f for _, f, _, _ in string.Formatter().parse(s.confirm_text) if f}
        if s.side_effect:
            assert s.confirm_text.strip(), f"{s.name} needs confirm_text"
            assert fields <= set(props), f"{s.name} confirm_text uses unknown fields {fields - set(props)}"
        else:
            assert s.confirm_text == "", f"{s.name} is read-only but has confirm_text"


def test_never_registered_actions_are_not_tools():
    reg = build_registry()
    assert not (set(reg.names()) & NEVER_TOOLS)
    routes = reg.routes()
    for key in (
        ("POST", "/api/outputs/{output_id:path}/approve"),
        ("POST", "/api/outputs/{output_id:path}/reject"),
        ("POST", "/api/outputs/{output_id:path}/uploaded"),
        ("POST", "/api/settings/keys"),
        ("POST", "/api/update/apply"),
    ):
        assert key not in routes and key in NO_TOOL and NO_TOOL[key].startswith("never")
    # editing a channel or the schedule never reaches engine flags or keys
    assert set(reg.get("set_schedule").spec.args_schema["properties"]) == {"schedule_time", "notifications"}  # type: ignore[union-attr]


def test_route_table_has_no_overlaps_and_reasons():
    reg = build_registry()
    routes = reg.routes()
    assert not (set(routes) & set(NO_TOOL))
    assert all(reason.strip() for reason in NO_TOOL.values())
    assert all(path.startswith("/api/") for _m, path in {*routes, *NO_TOOL})
    assert len(routes) == len({t for t in routes.values()})
    names = set(reg.names())
    assert {"system_map", "find", "where_is", "describe_tool", "list_tools", "search_docs", "explain_error",
            "status"} <= names
    for must in ("list_queue", "get_output", "open_folder", "start_story_job", "start_clip_job", "cancel_job",
                 "list_jobs", "pause_channel", "resume_channel", "edit_channel", "set_schedule", "add_idea",
                 "set_idea_status", "list_formats", "duplicate_format", "import_analytics", "summary_results",
                 "tail_logs", "make_diagnostics", "check_update", "warmup_engine", "shutdown_engine"):
        assert must in names, must


def test_argument_checker_and_confirm_rendering():
    schema = obj(
        {"n": {"type": "integer", "minimum": 1, "maximum": 5}, "s": {"type": "string", "enum": ["a", "b"]},
         "t": {"type": "string", "pattern": r"^\d\d:\d\d$"}, "xs": {"type": "array", "items": {"type": "integer"}}},
        ["n"],
    )
    assert check_value(schema, {"n": 2, "s": "a", "t": "03:00", "xs": [1]}) is None
    assert "required" in check_value(schema, {})
    assert "integer" in check_value(schema, {"n": "2"})
    assert "integer" in check_value(schema, {"n": True})
    assert "at most" in check_value(schema, {"n": 9})
    assert "one of" in check_value(schema, {"n": 2, "s": "c"})
    assert "wrong format" in check_value(schema, {"n": 2, "t": "3am"})
    assert "[1]" in check_value(schema, {"n": 2, "xs": [1, "x"]})
    assert "not an accepted" in check_value(schema, {"n": 2, "zzz": 1})
    reg = build_registry()
    spec = reg.get("edit_channel").spec  # type: ignore[union-attr]
    text = render_confirm(spec, {"slug": "after-dark", "changes": {"voice_notes": "calmer", "critic_bar": 8}})
    assert text == "Change channel after-dark: voice_notes: calmer; critic_bar: 8"
    assert "(default)" in render_confirm(reg.get("start_story_job").spec, {"channel": "x"})  # type: ignore[union-attr]


def test_fill_from_context():
    schema = obj({"output_id": {"type": "string"}, "channel": {"type": "string"}, "idea_id": {"type": "integer"}})
    ctx = {"page": "Queue", "output_id": "j/s1", "channel": "after-dark", "idea_id": 4, "item_ids": ["j/s9"]}
    assert fill_from_context(schema, {}, ctx) == {"output_id": "j/s1", "channel": "after-dark", "idea_id": 4}
    assert fill_from_context(schema, {"channel": "other"}, ctx)["channel"] == "other"
    assert fill_from_context(schema, {}, {"item_ids": ["j/s9"]}) == {"output_id": "j/s9"}
    assert fill_from_context(schema, {}, {"idea_id": "four"}) == {}


# ------------------------------------------------------------------ prompt, map, docs


def test_prompt_has_versioned_front_matter():
    meta, body = load_prompt()
    assert isinstance(meta["version"], int) and meta["version"] >= 1
    assert "{{system_map}}" in body and "{{context}}" in body
    for rule in ("confirm card", "never claim", "approve or reject"):
        assert rule.lower() in body.lower()


def test_system_map_is_compact_and_complete(tmp_home: Path):
    dash, state, _ = make(tmp_home)
    seed_channel(state.db, state.paths, "after-dark")
    seed_output(state.db, state.paths)
    ctx = ToolContext(dash)
    m = system_map(ctx)
    assert set(m["pages"]) == {"Queue", "Create", "Library", "Assistant", "Channels", "Results", "Settings"}
    assert "after-dark(active)" in m["channels"] and m["videos_waiting_for_review"] == 1
    assert {"llama", "supervisor", "dashboard"} <= set(m["services"]) and "E6xx" in m["errors"]
    assert {"outbox", "logs", "config", "models"} <= set(m["folders"]) and m["engine"].startswith("idle")
    text = compact_map(ctx)
    assert len(text) <= 6000  # about 1,500 tokens
    assert "\n" not in text and json.loads(text)["home"] == str(tmp_home)


def test_system_prompt_injects_map_and_context(tmp_home: Path):
    dash, _, _ = make(tmp_home)
    prompt = dash.assistant.system_prompt(ToolContext(dash, {"page": "Queue", "output_id": "j/s1"}))
    assert "{{" not in prompt and '"pages"' in prompt and '"output_id": "j/s1"' in prompt


def test_search_docs_finds_the_queue_passage_for_posting():
    idx = docs_index.DocsIndex.from_root(repo_root())
    hits = idx.search("post to youtube", 3)
    assert hits, "the spec should have passages about posting to YouTube"
    assert any("posting checklist" in h["text"] and h["file"] == "BUILD_SPEC.md" for h in hits), hits
    assert any("Queue" in h["text"] for h in hits)


def test_docs_index_chunking_and_errors_file():
    md = (
        "# Title\n\nIntro paragraph about apples.\n\n## Pages\n\n| A | B |\n|---|---|\n"
        "| **Queue** | post videos |\n| Create | make videos |\n\n```\ncode block ignored\n```\n"
    )
    chunks = docs_index.chunk_markdown("x.md", md)
    texts = [c.text for c in chunks]
    assert "Intro paragraph about apples." in texts
    assert "**Queue** | post videos" in texts and "Create | make videos" in texts
    assert not any("code block" in t or "---" in t for t in texts)
    assert next(c for c in chunks if c.text.startswith("**Queue**")).heading == "Pages"
    err = docs_index.chunk_errors("e.json", json.dumps({"NS-E203": {"message": "Service health check timed out.", "hint": "Run ns tune."}}))
    assert err[0].heading == "NS-E203"
    idx = docs_index.DocsIndex(chunks + err)
    assert idx.search("health check timeout")[0]["heading"] == "NS-E203"
    assert docs_index.tokenize("Posting, posted and posts the YouTube videos") == ["post", "post", "post", "youtube", "video"]
    assert idx.search("the of and") == []


def test_orientation_tools_through_the_registry(tmp_home: Path):
    dash, state, _ = make(tmp_home)
    seed_channel(state.db, state.paths, "after-dark")
    oid = seed_output(state.db, state.paths, title="The Doorbell Camera")
    state.log.error("stage_failed", "boom NS-E203 while loading")
    ctx = ToolContext(dash)
    reg = dash.registry
    hits = reg.get("find").call(ctx, {"query": "doorbell"})["hits"]  # type: ignore[union-attr]
    assert {"output"} <= {h["type"] for h in hits} and any(h["id"] == oid for h in hits)
    assert reg.get("find").call(ctx, {"query": "after-dark"})["hits"][0]["type"] in ("output", "channel")  # type: ignore[union-attr]
    where = reg.get("where_is").call
    assert where(ctx, {"thing": oid})["page"] == "Queue"  # type: ignore[union-attr]
    assert where(ctx, {"thing": "logs"})["path"] == str(state.paths.logs)  # type: ignore[union-attr]
    assert where(ctx, {"thing": "after-dark"})["kind"] == "channel"  # type: ignore[union-attr]
    assert where(ctx, {"thing": "schedule"})["page"] == "Settings"  # type: ignore[union-attr]
    assert where(ctx, {"thing": "zzz"})["kind"] == "unknown"  # type: ignore[union-attr]
    err = reg.get("explain_error").call(ctx, {"code": "ns-e203"})  # type: ignore[union-attr]
    assert err["known"] and "health" in err["message"].lower() and len(err["recent_log_lines"]) == 1
    assert reg.get("explain_error").call(ctx, {"code": "NS-E999"})["known"] is False  # type: ignore[union-attr]
    desc = reg.get("describe_tool").call(ctx, {"name": "pause_channel"})  # type: ignore[union-attr]
    assert desc["changes_something"] is True and desc["api_route"] == "POST /api/channels/{slug}/pause"
    assert reg.get("describe_tool").call(ctx, {"name": "nope"})["found"] is False  # type: ignore[union-attr]
    grouped = reg.get("list_tools").call(ctx, {"topic": "channels"})  # type: ignore[union-attr]
    assert set(grouped) == {"channels"} and {t["name"] for t in grouped["channels"]} >= {"pause_channel", "get_channel"}
    docs = reg.get("search_docs").call(ctx, {"query": "post to youtube"})  # type: ignore[union-attr]
    assert docs["hits"]


# ------------------------------------------------------------------ the loop


def test_read_only_tool_runs_immediately(tmp_home: Path):
    llm = ScriptedToolLlm([call("list_queue", {"status": "pending"}), say("You have one video waiting.")])
    dash, state, provider = make(tmp_home, llm)
    oid = seed_output(state.db, state.paths, title="The Doorbell Camera")
    events = run_chat(dash, "What is waiting for review?", context={"page": "Queue"})
    assert kinds(events) == ["conversation", "tool_call", "tool_result", "message", "done"]
    assert events[2]["payload"]["ok"] is True and events[2]["payload"]["result"][0]["output_id"] == oid
    assert events[3]["payload"]["text"] == "You have one video waiting."
    # the model saw the tool result on its second request, and a system prompt with the map
    second = llm.requests[1]
    assert second[0]["role"] == "system" and "Queue" in second[0]["content"]
    assert second[-1]["role"] == "tool" and oid in second[-1]["content"]
    assert len(llm.tool_lists[0]) == len(dash.registry.names())
    assert provider.released == 1
    cid = events[0]["payload"]["conversation_id"]
    roles = [m["role"] for m in dash.assistant.history(cid)["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]


def test_side_effect_tools_wait_for_confirmation(tmp_home: Path):
    llm = ScriptedToolLlm([
        call("pause_channel", {"slug": "after-dark", "note": "strike"}),
        say("I asked you to confirm pausing after-dark."),
    ])
    dash, state, _ = make(tmp_home, llm)
    seed_channel(state.db, state.paths, "after-dark")
    events = run_chat(dash, "Pause after-dark, we got a strike")
    assert kinds(events) == ["conversation", "confirm", "message", "done"]
    card = events[1]["payload"]
    assert card["tool"] == "pause_channel" and card["text"] == "Pause channel after-dark. Nothing is made for it until you resume it."
    # nothing happened yet
    assert dash.channels.load("after-dark").status == "active"
    assert state.db.one("SELECT COUNT(*) AS n FROM incidents")["n"] == 0
    tool_msg = llm.requests[1][-1]
    assert tool_msg["role"] == "tool" and "awaiting_owner_confirmation" in tool_msg["content"]

    out = dash.assistant.confirm(card["action_id"])
    assert out["status"] == "done" and out["result"]["profile"]["status"] == "paused"
    assert dash.channels.load("after-dark").status == "paused"
    assert state.db.one("SELECT note FROM incidents")["note"] == "strike"
    with pytest.raises(ApiError) as ei:
        dash.assistant.confirm(card["action_id"])
    assert ei.value.status == 409 and ei.value.code == "already_decided"
    notes = [m["content"] for m in dash.assistant.history(events[0]["payload"]["conversation_id"])["messages"]]
    assert any(n.startswith("Done: Pause channel after-dark") for n in notes)


def test_dismissed_action_changes_nothing(tmp_home: Path):
    llm = ScriptedToolLlm([call("pause_channel", {"slug": "after-dark"}), say("Waiting for your answer.")])
    dash, state, _ = make(tmp_home, llm)
    seed_channel(state.db, state.paths, "after-dark")
    events = run_chat(dash, "pause it")
    aid = events[1]["payload"]["action_id"]
    assert dash.assistant.dismiss(aid)["status"] == "dismissed"
    assert dash.channels.load("after-dark").status == "active"
    for fn in (dash.assistant.confirm, dash.assistant.dismiss):
        with pytest.raises(ApiError) as ei:
            fn(aid)
        assert ei.value.code == "already_decided"
    with pytest.raises(ApiError) as ei2:
        dash.assistant.confirm("deadbeef")
    assert ei2.value.status == 404


def test_job_actions_run_only_after_confirmation(tmp_home: Path):
    llm = ScriptedToolLlm([
        calls([("start_story_job", {"channel": "after-dark", "format": "shorts_only"}),
               ("start_clip_job", {"path": str(tmp_home / "talk.mp4"), "channel": "after-dark", "max_clips": 3})]),
        say("Two things need your OK."),
    ])
    dash, state, _ = make(tmp_home, llm)
    seed_channel(state.db, state.paths, "after-dark")
    (tmp_home / "talk.mp4").write_bytes(b"video")
    events = run_chat(dash, "make a short and cut clips from talk.mp4")
    cards = [e["payload"] for e in events if e["kind"] == "confirm"]
    assert len(cards) == 2 and state.store.counts() == {"queued": 0, "running": 0}
    assert "Make a new shorts_only for after-dark" in cards[0]["text"]
    assert "you confirm you own this video" in cards[1]["text"] and "up to 3 clips" in cards[1]["text"]
    for c in cards:
        assert dash.assistant.confirm(c["action_id"])["status"] == "done"
    jobs = state.store.list_jobs()
    assert {j.kind for j in jobs} == {"run", "clip"} and state.store.counts()["queued"] == 2
    clip = next(j for j in jobs if j.kind == "clip")
    assert clip.params["rights_confirmed"] is True and clip.params["max_clips"] == 3


def test_unknown_and_never_registered_tools_do_nothing(tmp_home: Path):
    llm = ScriptedToolLlm([
        call("approve_output", {"output_id": "x/s1"}), call("set_key", {"name": "pexels", "value": "k"}),
        say("I cannot approve videos; use the Queue page."),
    ])
    dash, state, _ = make(tmp_home, llm)
    oid = seed_output(state.db, state.paths)
    llm.script[0] = call("approve_output", {"output_id": oid})
    events = run_chat(dash, "approve the video")
    results = [e for e in events if e["kind"] == "tool_result"]
    assert [r["payload"]["ok"] for r in results] == [False, False]
    assert state.db.one("SELECT status FROM outputs WHERE output_id = ?", (oid,))["status"] == "pending"
    assert state.adapters.keyring.get("pexels_api_key") is None
    assert "unknown_tool" in llm.requests[1][-1]["content"]


def test_bad_arguments_are_returned_to_the_model(tmp_home: Path):
    llm = ScriptedToolLlm([
        call("set_idea_status", {"idea_id": "four", "status": "approved"}),
        call("list_queue", {"limit": 500}),
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c9", "type": "function", "function": {"name": "status", "arguments": "{not json"}}]},
        say("Sorry, that did not work."),
    ])
    dash, _, _ = make(tmp_home, llm)
    events = run_chat(dash, "do things")
    bad = [e["payload"] for e in events if e["kind"] == "tool_result"]
    assert [b["ok"] for b in bad] == [False, False, False]
    assert "integer" in bad[0]["error"] and "at most" in bad[1]["error"] and bad[2]["error"] == "bad arguments"
    assert kinds(events)[-2:] == ["message", "done"]


def test_context_supplies_missing_ids(tmp_home: Path):
    llm = ScriptedToolLlm([call("get_output", {}), say("It is the doorbell video.")])
    dash, state, _ = make(tmp_home, llm)
    oid = seed_output(state.db, state.paths, title="The Doorbell Camera")
    events = run_chat(dash, "tell me about this video", context={"page": "Queue", "output_id": oid})
    result = next(e for e in events if e["kind"] == "tool_result")["payload"]
    assert result["ok"] and result["result"]["title"] == "The Doorbell Camera"


def test_tool_errors_do_not_crash_the_turn(tmp_home: Path):
    llm = ScriptedToolLlm([call("get_channel", {"slug": "ghost"}), say("There is no such channel.")])
    dash, _, _ = make(tmp_home, llm)
    events = run_chat(dash, "show ghost")
    res = next(e for e in events if e["kind"] == "tool_result")["payload"]
    assert res["ok"] is False and res["result"]["error"]["code"] == "channel_not_found"


def test_step_limit_ends_the_turn(tmp_home: Path):
    llm = ScriptedToolLlm([call("status", {}, call_id=f"c{i}") for i in range(20)])
    dash, _, _ = make(tmp_home, llm)
    dash.assistant.max_steps = 3
    events = run_chat(dash, "loop forever")
    assert kinds(events).count("tool_call") == 3
    assert "stopped after several steps" in next(e for e in events if e["kind"] == "message")["payload"]["text"]


def test_busy_engine_queues_the_request(tmp_home: Path):
    llm = ScriptedToolLlm([say("Done while you were away.")])
    dash, state, provider = make(tmp_home, llm, busy="studio:after-dark-20261101-ab12")
    events = run_chat(dash, "write me an idea")
    assert kinds(events) == ["conversation", "busy", "done"]
    assert events[1]["payload"]["queued"] is True and "studio:after-dark" in events[1]["payload"]["message"]
    queued = dash.assistant_store.queued()
    assert len(queued) == 1 and queued[0]["text"] == "write me an idea"
    assert dash.assistant.run_queued() == []  # still busy: stays queued
    provider.busy_owner = None
    done = dash.assistant.run_queued()
    assert len(done) == 1 and kinds(done[0]["events"]) == ["conversation", "message", "done"]
    assert dash.assistant_store.queued() == []
    cid = events[0]["payload"]["conversation_id"]
    users = [m for m in dash.assistant.history(cid)["messages"] if m["role"] == "user"]
    assert len(users) == 1  # the queued text is not recorded twice


def test_empty_message_and_unknown_conversation(tmp_home: Path):
    llm = ScriptedToolLlm([say("Hello!")])
    dash, _, _ = make(tmp_home, llm)
    assert kinds(run_chat(dash, "   ")) == ["error", "done"]
    events = run_chat(dash, "hi", conversation_id="does-not-exist")
    assert events[0]["payload"]["conversation_id"] != "does-not-exist"
    with pytest.raises(ApiError):
        dash.assistant.history("does-not-exist")


def test_transcripts_persist_in_assistant_db(tmp_home: Path):
    llm = ScriptedToolLlm([say("Hello!"), say("Still here.")])
    dash, state, _ = make(tmp_home, llm)
    e1 = run_chat(dash, "hi there")
    cid = e1[0]["payload"]["conversation_id"]
    e2 = run_chat(dash, "and again", conversation_id=cid)
    assert e2[0]["payload"]["conversation_id"] == cid
    assert (state.paths.data / "assistant.db").exists()
    again = AssistantStore(state.paths)  # a fresh handle sees the same transcript
    assert [m["content"] for m in again.messages(cid)] == ["hi there", "Hello!", "and again", "Still here."]
    assert again.conversations()[0]["title"] == "hi there"
    # the second turn replays the first one to the model
    assert [m["role"] for m in llm.requests[1]] == ["system", "user", "assistant", "user"]
