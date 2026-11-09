"""Fake llama-server and embedding server for a dev install (docs/DEV_MODE.md). Standard library only.

`INSTALL.cmd --dev` downloads no model. In a dev install the supervisor starts this module in place of
`llama-server.exe`, so `ns verify`, the Assistant and every page that talks to the story or memory model get
an answer in the shape the real clients read (`ns_common.llm_client`, `ns_common.embed_client`):

    GET  /health               {"status": "ok", ...}
    POST /v1/chat/completions  the schema's own example (response_format json_schema) or a short text;
                               with `tools`, one tool call picked from the owner's words (`plan_tool_call`),
                               or exactly the call named by the cue `tool: <name> {json args}`
    POST /completion           llama.cpp's native route with timings (the benchmark in `ns verify`)
    POST /v1/embeddings        a deterministic unit vector per text

Run: `python -m ns_supervisor.devfake --service llama|embed --port N [--host H]`. The voice engine is not
here: a dev install starts the real `ns_tts.server` with its fake engine.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SERVICES = ("llama", "embed")
EMBED_DIM = 64
TEXT_REPLY = (
    "This is the dev-mode language model. It writes no real text: it answers so that the pages and the "
    "pipeline can be tried on a PC with no graphics card and no model files."
)


def _resolve(schema: Any, root: dict[str, Any]) -> Any:
    """Follow a local `$ref` ("#/$defs/Name")."""
    while isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
        node: Any = root
        for part in schema["$ref"].lstrip("#/").split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        schema = node
    return schema


def example_for(schema: Any, root: dict[str, Any] | None = None, depth: int = 0) -> Any:
    """A small value that satisfies a JSON schema: listed properties filled, enums take their first value,
    numbers their minimum, booleans true, lists their minimum length (at least one item)."""
    root = root if root is not None else (schema if isinstance(schema, dict) else {})
    schema = _resolve(schema, root)
    if not isinstance(schema, dict) or depth > 8:
        return None
    if "const" in schema:
        return schema["const"]
    if isinstance(schema.get("enum"), list) and schema["enum"]:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf"):
        resolved = [_resolve(o, root) for o in schema.get(key) or []]
        options = [o for o in resolved if not (isinstance(o, dict) and o.get("type") == "null")]
        if options:
            return example_for(options[0], root, depth + 1)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        return {name: example_for(sub, root, depth + 1) for name, sub in props.items()}
    if kind == "array":
        count = max(1, int(schema.get("minItems") or 1))
        return [example_for(schema.get("items") or {}, root, depth + 1) for _ in range(count)]
    if kind == "string":
        return "x" * int(schema.get("minLength") or 0) or "dev"
    if kind == "integer":
        return int(schema.get("minimum", 0))
    if kind == "number":
        return float(schema.get("minimum", 0))
    if kind == "boolean":
        return True
    return None


def embedding_for(text: str, dim: int = EMBED_DIM) -> list[float]:
    """A deterministic unit vector for `text` (the same text always gets the same vector)."""
    raw: list[float] = []
    counter = 0
    while len(raw) < dim:
        digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
        raw += [(b - 127.5) / 127.5 for b in digest]
        counter += 1
    vec = raw[:dim]
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [round(x / norm, 6) for x in vec]


def chat_reply(body: dict[str, Any]) -> str:
    """The assistant message for a chat request: JSON for a schema request, else a short text."""
    fmt = body.get("response_format")
    if isinstance(fmt, dict):
        spec = fmt.get("json_schema")
        schema = spec.get("schema") if isinstance(spec, dict) else None
        if isinstance(schema, dict):
            return json.dumps(example_for(schema))
    return TEXT_REPLY


# ---------------------------------------------------------------------------------------------- tool calls
_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_SUBJECT = re.compile(
    r"\b(?:of|for|about|showing|with)\s+(?:an?\s+|the\s+|some\s+)?(.+?)(?=\s+(?:for|into|and|to|in)\b|[.,!?]|$)",
    re.I,
)
_SLUG = re.compile(r"\b[a-z0-9]+(?:-[a-z0-9]+)+\b")
_TITLE = re.compile(r"\b([A-Z][a-z0-9]+(?:\s+[A-Z][a-z0-9]+)+)\b")
# "make a short from this script: ...", "use this script ...", "make a video of this text ...": the owner's
# own story follows the cue (after a colon or a line break); what comes before says what to make and for whom.
_SCRIPT_CUE = re.compile(
    r"\b(?:from|using|use|of|out of|with)\s+(?:this|my|the following)\s+(?:script|text|story)\b", re.I
)
_SCRIPT_MIN_CHARS = 200  # `ns_common.models.SCRIPT_MIN_CHARS`; this module takes nothing from the packages
_SCRIPT_TITLE = re.compile(r"\b(?:titled|called)\s+[\"“']?([^\"”'\n:,.]{2,80})", re.I)


def _tool_names(tools: Any) -> set[str]:
    out: set[str] = set()
    for t in tools or []:
        fn = t.get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            out.add(str(fn["name"]))
    return out


_QUESTION = re.compile(r"<question>(.*?)</question>", re.S)


def _last_user(messages: list[dict[str, Any]]) -> str:
    """The owner's own words: the Assistant wraps them as <question> and appends a facts block."""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = str(m.get("content") or "")
            q = _QUESTION.search(content)
            return q.group(1) if q else content
    return ""


def _count(low: str, default: int) -> int:
    m = re.search(r"\b(\d+|one|two|three|four|five)\b", low)
    if not m:
        return default
    word = m.group(1)
    return min(5, max(1, int(word) if word.isdigit() else _NUMBER_WORDS[word]))


def _subject(text: str, fallback: str) -> str:
    m = _SUBJECT.search(text)
    return (m.group(1).strip() if m else fallback)[:60] or fallback


def _channel(text: str) -> str | None:
    m = _SLUG.search(text)
    if m:
        return m.group(0)
    t = _TITLE.search(text)
    return re.sub(r"\s+", "-", t.group(1).lower()) if t else None


# The explicit cue: the whole question is `tool: <name>` or `tool: <name> {json args}`. It makes any tool
# reachable through the chat on a dev install (docs/DEV_MODE.md), whatever the phrasings below know. A tool
# the router did not offer for that message is reached through `list_tools` (see `_discovery_step`).
_TOOL_CUE = re.compile(r"\s*tool\s*:\s*([A-Za-z_][A-Za-z0-9_]*)\s*(.*)", re.I | re.S)
_LIST_TOOLS = "list_tools"


def _cue_parts(text: str) -> tuple[str, str] | None:
    """(tool name, text after the name) when the whole text is a cue, else None."""
    m = _TOOL_CUE.fullmatch(text)
    if not m:
        return None
    rest = m.group(2).strip()
    return m.group(1), ("" if re.fullmatch(r"[.!?]+", rest) else rest)  # "tool: status." ends in a full stop


def _cue_args(name: str, rest: str) -> tuple[dict[str, Any] | None, str | None]:
    """(arguments, None), or (None, plain reply) when `rest` is not one JSON object (or empty)."""
    args: Any = {}
    if rest:
        try:
            args = json.loads(rest)
        except ValueError as exc:
            return None, _bad_args(name, str(exc))
    if not isinstance(args, dict):
        return None, _bad_args(name, "it is not a JSON object")
    return args, None


def _tool_cue(text: str, names: set[str]) -> tuple[dict[str, Any] | None, str | None]:
    """(call, None) for a valid cue, (None, plain reply) for a cue that cannot be followed, (None, None)
    when the text is not a cue at all. A cued tool that is not offered is looked up with `list_tools` when
    that is offered: the call returned is then `list_tools` and `_discovery_step` finishes the job."""
    parts = _cue_parts(text)
    if parts is None:
        return None, None
    name, rest = parts
    lookup = _LIST_TOOLS in names and name != _LIST_TOOLS
    if name not in names and not lookup:
        return None, (f"The dev fake llama has no tool '{name}': it is not among the offered tools "
                      f"({', '.join(sorted(names))}).")
    args, problem = _cue_args(name, rest)
    if args is None:
        return None, problem
    if name not in names:
        return {"name": _LIST_TOOLS, "args": {}}, None
    return {"name": name, "args": args}, None


def _bad_args(name: str, why: str) -> str:
    return (f"The dev fake llama could not read the arguments of '{name}': after the tool name it needs "
            f"one JSON object, for example tool: {name} {{\"q\": \"fog\"}} ({why}).")


def _listed_names(data: Any) -> set[str]:
    """Tool names in a `list_tools` result: `tools_added` and the `{topic: [{name, ...}]}` listing."""
    out: set[str] = set()
    if not isinstance(data, dict):
        return out
    for key, value in data.items():
        if not isinstance(value, list):
            continue
        for item in value:
            if key == "tools_added" and isinstance(item, str):
                out.add(item)
            elif isinstance(item, dict) and isinstance(item.get("name"), str):
                out.add(item["name"])
    return out


def _discovery_step(messages: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None]:
    """The step after a `list_tools` result in a turn that began with a cue for a tool that was not offered.

    The router offers tools by topic words and at most ten per topic, so `tool: system_map` or
    `tool: edit_format` may name a tool that is not in the request. The cue then goes: `list_tools {}` (all
    names by topic), `list_tools {"topic": T}` for the topic that holds the name, and the cued call. The
    Assistant runs any registered tool, offered or not; a topic listing that holds the name is the proof that
    it is one (`tools_added` alone would miss a tool past the ten slots of its topic)."""
    parts = _cue_parts(_last_user(messages))
    if parts is None or parts[0] == _LIST_TOOLS or messages[-1].get("name") != _LIST_TOOLS:
        return None, None
    name, rest = parts
    args, problem = _cue_args(name, rest)
    if args is None:
        return None, problem
    try:
        data = json.loads(str(messages[-1].get("content") or ""))
    except ValueError:
        data = None
    topics = data.get("topics") if isinstance(data, dict) else None
    if isinstance(topics, dict):  # the names-only listing: ask for the topic that holds the cued name
        for topic, listed in topics.items():
            if isinstance(listed, list) and name in listed:
                return {"name": _LIST_TOOLS, "args": {"topic": str(topic)}}, None
    elif name in _listed_names(data):
        return {"name": name, "args": args}, None
    return None, f"The dev fake llama has no tool '{name}': list_tools does not list a tool of that name."


def _plan(messages: list[dict[str, Any]], tools: Any) -> tuple[dict[str, Any] | None, str | None]:
    """(call, None), (None, plain reply) for a cue that cannot be followed, or (None, None)."""
    names = _tool_names(tools)
    if not names or not messages:
        return None, None
    role = messages[-1].get("role")
    if role == "tool":
        return _discovery_step(messages)
    if role != "user":
        return None, None
    text = _last_user(messages)
    cued, problem = _tool_cue(text, names)
    if cued is not None or problem is not None:  # a cue is never mixed with the phrasings below
        return cued, problem
    return _phrasing(text, names), None


def plan_tool_call(messages: list[dict[str, Any]], tools: Any) -> dict[str, Any] | None:
    """One tool call for the owner's last message, picked by its words, or None for a plain text answer.

    The real model reads the tool list; this stand-in matches a handful of phrasings so the Assistant's
    whole path (confirm card, tool run, result shown) can be tried on a dev install: "fetch two clips of a
    dark hallway", "what is the status", "pause Dev Night Tales", "where is the footage cache", "make a
    Short for dev-night-tales". Only tools in `tools` are called; everything else gets the text reply.

    The explicit cue `tool: <name> {json args}` (args optional) returns exactly that call when `<name>` is
    offered. When it is not offered and `list_tools` is, the first call is `list_tools` and the cued call
    follows its result (`_discovery_step`). An unknown name or arguments that are not one JSON object return
    None here and `chat_message` answers in plain words.
    """
    return _plan(messages, tools)[0]


def _phrasing(text: str, names: set[str]) -> dict[str, Any] | None:
    """The tool picked by the words of `text` (see `plan_tool_call`)."""
    low = text.lower()

    def pick(name: str, **args: Any) -> dict[str, Any] | None:
        if name not in names:
            return None
        return {"name": name, "args": {k: v for k, v in args.items() if v is not None}}

    cue = _SCRIPT_CUE.search(text)
    if cue:  # first: the owner's story may itself contain words such as "get" and "video"
        tail = text[cue.end():]
        parts = re.split(r"[:\n]", tail, maxsplit=1)
        intro = text[: cue.end()] + parts[0]
        body = (parts[1] if len(parts) == 2 else "").strip()
        long_video = re.search(r"\b(long|long-form|full-length|master)\b", intro.lower())
        fmt = "master_first" if long_video else None
        if re.search(r"\bshorts?\b", intro.lower()):
            fmt = "shorts_only"
        title = _SCRIPT_TITLE.search(intro)
        # a real script is a whole story, not an idea or a note: a short body or the word "idea" is not one
        if len(body) >= _SCRIPT_MIN_CHARS and not re.search(r"\bideas?\b", intro, re.I):
            return pick("start_script_job", channel=_channel(intro), format=fmt, script=body,
                        title=title.group(1).strip() if title else None)
    if re.search(r"\b(fetch|pull|download|grab|get)\b.*\b(clips?|footage|stock|videos?)\b", low):
        orientation = "portrait" if re.search(r"\b(vertical|portrait|shorts?)\b", low) else "landscape"
        return pick("fetch_footage", q=_subject(text, "night street"), count=_count(low, 1),
                    orientation=orientation)
    if re.search(r"\b(search|look for|find)\b.*\b(clips?|footage|stock)\b", low):
        return pick("search_footage", q=_subject(text, "night"))
    where = re.search(r"\bwhere (?:is|are)\s+(?:the\s+|my\s+)?(.+?)(?:[.,!?]|$)", text, re.I)
    if where:
        return pick("where_is", thing=where.group(1).strip()[:60] or "logs")
    if re.search(r"\bpause\b", low):
        return pick("pause_channel", slug=_channel(text), note="asked in chat")
    if re.search(r"\bresume\b", low):
        return pick("resume_channel", slug=_channel(text), note="asked in chat")
    if re.search(r"\b(make|start|write|create)\b.*\b(shorts?|story|video)\b", low):
        fmt = "shorts_only" if re.search(r"\bshorts?\b", low) else None
        return pick("start_story_job", channel=_channel(text), format=fmt)
    if re.search(r"\b(queue|ready|waiting|review)\b", low):
        return pick("list_queue", status="pending")
    if re.search(r"\bchannels?\b", low):
        return pick("list_channels")
    if re.search(r"\bideas?\b", low):
        return pick("list_ideas")
    if re.search(r"\b(status|engine|running|how are things)\b", low):
        return pick("status")
    return None


def _after_tool(messages: list[dict[str, Any]]) -> str:
    """The text answer after a tool ran: the paths it returned, the confirm card it raised, or its gist."""
    last = messages[-1]
    raw = str(last.get("content") or "")
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict):
        if data.get("status") == "awaiting_owner_confirmation":
            return f"I need your Yes first: {data.get('will_do', 'see the card above')}"
        if "error" in data and isinstance(data["error"], dict):
            err = data["error"]
            return f"That did not work: {err.get('message') or err.get('code')}"
        paths = [str(i.get("path")) for k in ("clips", "items", "results") for i in data.get(k) or []
                 if isinstance(i, dict) and i.get("path")]
        if paths:
            return "Done. The files are here:\n" + "\n".join(paths)
    gist = raw if len(raw) <= 600 else raw[:600] + "..."
    return f"Done ({last.get('name') or 'tool'}). {gist}"


def _turn_start(messages: list[dict[str, Any]]) -> int:
    """Index of the owner's last message: what follows it belongs to this turn."""
    return next((i for i in range(len(messages) - 1, -1, -1) if messages[i].get("role") == "user"), 0)


def chat_message(body: dict[str, Any]) -> dict[str, Any]:
    """The whole assistant message for a chat request, with `tool_calls` when a tool fits."""
    raw = body.get("messages")
    messages: list[dict[str, Any]] = [m for m in raw if isinstance(m, dict)] if isinstance(raw, list) else []
    if body.get("tools"):
        planned, problem = _plan(messages, body.get("tools"))
        if problem:
            return {"role": "assistant", "content": problem}
        if planned:
            # a second call in one turn (the cue's look-up with list_tools) gets its own id
            made = sum(1 for m in messages[_turn_start(messages):] if m.get("role") == "tool")
            call_id = f"call_dev_{planned['name']}" + (f"_{made + 1}" if made else "")
            call = {"id": call_id, "type": "function",
                    "function": {"name": planned["name"], "arguments": json.dumps(planned["args"])}}
            return {"role": "assistant", "content": "", "tool_calls": [call]}
        if messages and messages[-1].get("role") == "tool":
            return {"role": "assistant", "content": _after_tool(messages)}
    return {"role": "assistant", "content": chat_reply(body)}


class Handler(BaseHTTPRequestHandler):
    """One request. The service name ("llama" or "embed") is set on the server object."""

    server_version = "nightshift-devfake"

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - the base class's name
        return

    def _send(self, code: int, obj: object) -> None:
        raw = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}") if length else {}
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @property
    def service(self) -> str:
        """The service this server stands in for."""
        return str(getattr(self.server, "service", "llama"))

    def do_GET(self) -> None:  # noqa: N802 - the http.server hook names
        """Health and the model list."""
        if self.path.startswith("/health"):
            self._send(
                200, {"status": "ok", "ok": True, "dev": True, "engine": "devfake", "service": self.service}
            )
        elif self.path.startswith("/v1/models"):
            self._send(200, {"data": [{"id": "code"}, {"id": "story"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        """Chat, completion and embeddings."""
        body = self._body()
        if self.service == "llama" and self.path == "/v1/chat/completions":
            msg = chat_message(body)
            reason = "tool_calls" if msg.get("tool_calls") else "stop"
            self._send(200, {"choices": [{"index": 0, "message": msg, "finish_reason": reason}]})
        elif self.service == "llama" and self.path == "/completion":
            n = int(body.get("n_predict") or 16)
            self._send(200, {
                "content": "dev " * min(n, 8), "tokens_predicted": n,
                "timings": {"predicted_n": n, "predicted_per_second": 100.0, "prompt_per_second": 1000.0},
            })
        elif self.service == "embed" and self.path == "/v1/embeddings":
            raw = body.get("input") or [""]
            texts = [raw] if isinstance(raw, str) else [str(t) for t in raw]
            data = [{"index": i, "embedding": embedding_for(t)} for i, t in enumerate(texts)]
            self._send(200, {"data": data})
        else:
            self._send(404, {"error": "not found"})


def make_server(service: str, port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """The server for `service` ("llama" or "embed"); `port` 0 picks a free one."""
    if service not in SERVICES:
        raise ValueError(f"service must be one of {SERVICES}, not {service!r}")
    server = ThreadingHTTPServer((host, port), Handler)
    server.service = service  # type: ignore[attr-defined]
    return server


def main(argv: list[str] | None = None) -> int:
    """`python -m ns_supervisor.devfake --service llama|embed --port N`."""
    ap = argparse.ArgumentParser(prog="ns_supervisor.devfake", description=__doc__)
    ap.add_argument("--service", choices=SERVICES, required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args(argv)
    server = make_server(args.service, args.port, args.host)
    sys.stdout.write(f"devfake {args.service} listening on {args.host}:{args.port}\n")
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
