import json
from pathlib import Path

import httpx
import pytest

from ns_common.errors import NsError
from ns_common.llm_client import FakeLlm, LlmClient

SCHEMA = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}


def _client(handler):
    transport = httpx.MockTransport(handler)
    return LlmClient("http://127.0.0.1:8080", "story", client=httpx.Client(transport=transport))


def test_complete_json_sends_schema_and_parses():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(json.loads(req.content))
        content = "<think>hm</think>{\"a\": 3}"
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    llm = _client(handler)
    out = llm.complete_json(prompt_name="ideas", system="S", user="U", schema=SCHEMA, thinking=True)
    assert out == {"a": 3}
    assert seen["model"] == "story"
    assert seen["response_format"]["type"] == "json_schema"
    assert seen["response_format"]["json_schema"]["schema"] == SCHEMA
    assert seen["chat_template_kwargs"] == {"enable_thinking": True}
    assert seen["messages"][0] == {"role": "system", "content": "S"}


def test_bad_json_triggers_one_repair_then_e302():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "not json"}}]})

    with pytest.raises(NsError) as ei:
        _client(handler).complete_json(prompt_name="x", system="S", user="U", schema=SCHEMA)
    assert ei.value.code == "NS-E302"
    assert len(calls) == 2
    assert "repair" in calls[1]["messages"][-1]["content"].lower()


def test_transport_error_is_e301():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(NsError) as ei:
        _client(handler).complete_text(prompt_name="x", system="S", user="U")
    assert ei.value.code == "NS-E301"


def test_health():
    ok = _client(lambda r: httpx.Response(200, json={"status": "ok"})).health()
    assert ok is True
    bad = _client(lambda r: httpx.Response(503, json={"status": "loading"})).health()
    assert bad is False


def test_fake_llm_fixtures_and_overrides(tmp_path: Path):
    (tmp_path / "ideas.json").write_text(json.dumps({"a": 1}), encoding="utf-8")
    fake = FakeLlm(tmp_path, overrides={"judge": [{"a": 9}, {"a": 8}]})
    assert fake.complete_json(prompt_name="ideas", system="", user="", schema=SCHEMA) == {"a": 1}
    assert fake.complete_json(prompt_name="judge", system="", user="", schema=SCHEMA) == {"a": 9}
    assert fake.complete_json(prompt_name="judge", system="", user="", schema=SCHEMA) == {"a": 8}
    assert fake.complete_json(prompt_name="judge", system="", user="", schema=SCHEMA) == {"a": 9}
    assert fake.complete_text(prompt_name="nothing", system="", user="").startswith("FAKE TEXT")
    assert [c["prompt_name"] for c in fake.calls] == ["ideas", "judge", "judge", "judge", "nothing"]
    with pytest.raises(FileNotFoundError):
        fake.complete_json(prompt_name="missing", system="", user="", schema=SCHEMA)
