"""Fake llama-server and embedding server for a dev install (docs/DEV_MODE.md). Standard library only.

`INSTALL.cmd --dev` downloads no model. In a dev install the supervisor starts this module in place of
`llama-server.exe`, so `ns verify`, the Assistant and every page that talks to the story or memory model get
an answer in the shape the real clients read (`ns_common.llm_client`, `ns_common.embed_client`):

    GET  /health               {"status": "ok", ...}
    POST /v1/chat/completions  the schema's own example (response_format json_schema) or a short text
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
            msg = {"role": "assistant", "content": chat_reply(body)}
            self._send(200, {"choices": [{"index": 0, "message": msg, "finish_reason": "stop"}]})
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
