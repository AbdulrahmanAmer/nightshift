"""Gap 21: the Assistant lists past conversations and answers requests saved while the engine was busy."""

from __future__ import annotations

from pathlib import Path

from ns_dashboard.assistant.llm import ScriptedToolLlm, say
from tests.test_dashboard_api import make_env, sse_events


def test_conversation_list_shows_saved_requests_and_the_queue_replays_them(tmp_home: Path):
    client, _state = make_env(tmp_home, llm=ScriptedToolLlm([say("All quiet tonight.")]))
    provider = client.app.state.dashboard.assistant.provider  # type: ignore[attr-defined]
    provider.busy_owner = "studio:after-dark-20261101-ab12"
    events = sse_events(client.post("/api/assistant/chat", json={"message": "What is waiting?"}))
    cid = events[0]["payload"]["conversation_id"]
    convs = client.get("/api/assistant/conversations").json()
    assert convs[0]["conversation_id"] == cid and convs[0]["title"] == "What is waiting?"
    assert convs[0]["queued"] == 1
    assert client.post("/api/assistant/queue/run").json() == []  # still busy: stays saved
    assert client.get("/api/assistant/conversations").json()[0]["queued"] == 1
    provider.busy_owner = None
    ran = client.post("/api/assistant/queue/run").json()
    assert len(ran) == 1 and [e["kind"] for e in ran[0]["events"]] == ["conversation", "message", "done"]
    assert client.get("/api/assistant/conversations").json()[0]["queued"] == 0
    hist = client.get("/api/assistant/history", params={"conversation_id": cid}).json()
    texts = [m["content"] for m in hist["messages"] if m["role"] == "assistant"]
    assert "All quiet tonight." in texts
