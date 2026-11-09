"""Tool descriptors and the registry the Assistant reads (component C9, spec 20.3a)."""
# Dense descriptor tables and long signatures: wrapping them hurts readability.
# ruff: noqa: E501

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ns_common.textstats import word_count

if TYPE_CHECKING:
    from ns_dashboard.handlers import Dashboard

NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
TOPICS = (
    "queue", "create", "library", "channels", "results", "settings", "engine", "diagnostics", "orientation",
    "series",
)
JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": list,
    "object": dict,
}


@dataclass(frozen=True)
class ToolSpec:
    """What the model sees: name, one-line description, JSON-schema arguments, and whether it changes state."""

    name: str
    description: str
    args_schema: dict[str, Any]
    side_effect: bool
    confirm_text: str = ""
    topic: str = "queue"
    #: Result fields that survive when a long result is shrunk or digested for the model (`prompt.shrink_result`).
    summary_fields: tuple[str, ...] = ()

    def openai(self) -> dict[str, Any]:
        """OpenAI-compatible function-tool object."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_schema,
            },
        }


@dataclass
class ToolContext:
    """What a tool body receives: the service layer plus the page/item context of the conversation."""

    dash: Dashboard
    context: dict[str, Any] = field(default_factory=dict)


ToolCall = Callable[[ToolContext, dict[str, Any]], Any]


@dataclass(frozen=True)
class ToolDef:
    """A descriptor plus the function that runs it; `route` ties it to an `/api` route (None = orientation)."""

    spec: ToolSpec
    call: ToolCall
    route: tuple[str, str] | None = None
    precheck: Callable[[dict[str, Any]], str | None] | None = None  # extra argument rules a schema cannot say


def obj(props: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    """JSON-schema object with the given properties."""
    return {
        "type": "object",
        "properties": props or {},
        "required": required or [],
        "additionalProperties": False,
    }


def _is_bool(value: Any) -> bool:
    """isinstance(value, bool) without narrowing `value` for the type checker (its `Any` must stay `Any`)."""
    return isinstance(value, bool)


def check_value(schema: dict[str, Any], value: Any, path: str = "args") -> str | None:
    """Validate `value` against the small JSON-schema subset the descriptors use; returns an error or None."""
    typ = schema.get("type")
    if isinstance(typ, list):  # a union such as ["integer", "string"]: any one member is enough
        if any(check_value({**schema, "type": t}, value, path) is None for t in typ):
            return None
        return f"{path} must be one of {typ}"
    if typ is not None:
        expected = JSON_TYPES[typ]
        if _is_bool(value) and typ in ("integer", "number"):
            return f"{path} must be {typ}"
        if not isinstance(value, expected):
            return f"{path} must be {typ}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path} must be one of {schema['enum']}"
    if typ == "object":
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in value:
                return f"{path}.{req} is required"
        for k, v in value.items():
            if k in props:
                bad = check_value(props[k], v, f"{path}.{k}")
                if bad:
                    return bad
            elif schema.get("additionalProperties") is False:
                return f"{path}.{k} is not an accepted argument"
    if typ == "array" and "items" in schema:
        for i, item in enumerate(value):
            bad = check_value(schema["items"], item, f"{path}[{i}]")
            if bad:
                return bad
    if typ == "string" and "pattern" in schema and not re.search(schema["pattern"], value):
        return f"{path} has the wrong format"
    if typ == "string" and "maxLength" in schema and len(value) > schema["maxLength"]:
        return f"{path} is longer than {schema['maxLength']} characters"
    if typ in ("integer", "number"):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path} must be at least {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path} must be at most {schema['maximum']}"
    return None


class _Safe(dict):
    def __missing__(self, key: str) -> str:
        return "(default)"


def _plain(value: Any) -> Any:
    if isinstance(value, list):
        return ", ".join(str(_plain(v)) for v in value)
    if isinstance(value, dict):
        return "; ".join(f"{k}: {_plain(v)}" for k, v in value.items())
    return value


def render_confirm(spec: ToolSpec, args: dict[str, Any]) -> str:
    """The confirm-card text in the owner's words (unknown placeholders render as `(default)`)."""
    flat = _Safe({k: _plain(v) for k, v in args.items()})
    for key, prop in spec.args_schema.get("properties", {}).items():  # a default the tool applies shows as such
        if "default" in prop:
            flat.setdefault(key, str(prop["default"]))
    flat.setdefault("source", args.get("url") or args.get("path") or "(default)")
    if "script" in args:  # the card says how long the text is, never the text itself
        flat.setdefault("words", str(word_count(str(args["script"]))))
        flat.setdefault("format", "video")
    flat.setdefault("schedule_at", "now")
    return (spec.confirm_text or spec.description).format_map(flat)


class Registry:
    """All tool definitions by name."""

    def __init__(self, tools: list[ToolDef]) -> None:
        self._tools: dict[str, ToolDef] = {}
        for t in tools:
            if t.spec.name in self._tools:
                raise ValueError(f"duplicate tool name {t.spec.name!r}")
            self._tools[t.spec.name] = t

    def get(self, name: str) -> ToolDef | None:
        """The tool or None."""
        return self._tools.get(name)

    def names(self) -> list[str]:
        """Sorted tool names."""
        return sorted(self._tools)

    def all(self) -> list[ToolDef]:
        """Every tool definition."""
        return list(self._tools.values())

    def by_topic(self, topic: str | None = None) -> dict[str, list[ToolSpec]]:
        """Specs grouped by topic (optionally only one topic)."""
        out: dict[str, list[ToolSpec]] = {}
        for t in self._tools.values():
            if topic and t.spec.topic != topic:
                continue
            out.setdefault(t.spec.topic, []).append(t.spec)
        return {k: sorted(v, key=lambda s: s.name) for k, v in sorted(out.items())}

    def openai_tools(self) -> list[dict[str, Any]]:
        """Function-tool list for the chat-completions request."""
        return [t.spec.openai() for t in sorted(self._tools.values(), key=lambda t: t.spec.name)]

    def validate(self, name: str, args: Any) -> str | None:
        """Error text for bad arguments of tool `name`, else None."""
        t = self._tools.get(name)
        if t is None:
            return f"unknown tool {name!r}"
        if not isinstance(args, dict):
            return "arguments must be a JSON object"
        bad = check_value(t.spec.args_schema, args)
        if bad is None and t.precheck is not None:
            bad = t.precheck(args)
        return bad

    def routes(self) -> dict[tuple[str, str], str]:
        """Map (method, path) -> tool name for route-derived tools."""
        return {t.route: t.spec.name for t in self._tools.values() if t.route is not None}
