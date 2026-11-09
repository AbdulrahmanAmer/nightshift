"""Assistant tool descriptors of the Series Workshop (component C11; spec 20.3a, 23.3).

`TOOLS` holds one dict per tool with the `ToolSpec` fields (`name`, `description`, `args_schema`,
`side_effect`, `confirm_text`) plus `fn`, a plain callable `fn(ws, **args)` that takes a `Workshop` (see
`default_workshop`) and returns JSON-ready data. C9 owns the registry and mounts these (CR-C11-2). Read-only
tools run immediately; the others get a confirm card whose text is `confirm_text.format(**args)` and only use
required arguments, so formatting never fails on an omitted option.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from ns_studio.series.store import WorkshopError, stories_dir
from ns_studio.series.workshop import Workshop

ToolFn = Callable[..., Any]
_STR: dict[str, Any] = {"type": "string"}
_SLUG: dict[str, Any] = {"type": "string", "description": "story slug, see workshop_list"}
_PART: dict[str, Any] = {"type": ["integer", "string"], "description": "part number, or 'all'"}


def _json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _spec(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
    side_effect: bool,
    confirm_text: str,
    fn: ToolFn,
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "args_schema": {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        },
        "side_effect": side_effect,
        "confirm_text": confirm_text,
        "fn": fn,
    }


def _start(ws: Workshop, channel: str, premise: str | None = None) -> dict[str, Any]:
    slug = ws.start(channel, premise)
    return {"slug": slug, "folder": str(stories_dir(ws.paths) / slug), "next": "workshop_concepts"}


def _concepts(ws: Workshop, slug: str, n: int = 3) -> dict[str, Any]:
    return {"slug": slug, "letters": ws.concepts(slug, n), "next": "workshop_choose"}


def _regenerate(ws: Workshop, slug: str, letter: str, feedback: str) -> dict[str, Any]:
    return {"slug": slug, "letter": ws.regenerate(slug, letter, feedback)}


def _choose(ws: Workshop, slug: str, letter: str) -> dict[str, Any]:
    return {"slug": slug, "title": ws.choose(slug, letter), "next": "workshop_map"}


def _map(ws: Workshop, slug: str, parts: int, minutes_per_part: int = 15) -> dict[str, Any]:
    return _json(ws.map_parts(slug, parts, minutes_per_part))


def _write(ws: Workshop, slug: str, n: int) -> dict[str, Any]:
    return ws.write_part(slug, n)


def _revise(ws: Workshop, slug: str, n: int, feedback: str) -> dict[str, Any]:
    return ws.revise_part(slug, n, feedback)


def _approve(ws: Workshop, slug: str, part: int | str = "all") -> dict[str, Any]:
    return _json(ws.approve(slug, part))


def _design(ws: Workshop, slug: str, **fields: Any) -> dict[str, Any]:
    return _json(ws.design(slug, **fields))


def _produce(ws: Workshop, slug: str, part: int | str = "all") -> list[dict[str, Any]]:
    return ws.produce(slug, part)


def _status(ws: Workshop, slug: str) -> dict[str, Any]:
    return {**ws.status(slug), "design_choices": ws.design_choices(slug)}


def _list(ws: Workshop) -> list[dict[str, Any]]:
    return ws.list_stories()


TOOLS: list[dict[str, Any]] = [
    _spec(
        "workshop_start",
        "Start a new story workshop: creates the story folder with a concept skeleton.",
        {"channel": {**_STR, "description": "channel slug"}, "premise": _STR},
        ["channel"],
        True,
        "Start a new story workshop on channel {channel}. This creates a folder with a concept.md skeleton.",
        _start,
    ),
    _spec(
        "workshop_concepts",
        "Write concept drafts (premise, cast, five beats, series potential, risks) to choose from.",
        {"slug": _SLUG, "n": {"type": "integer", "minimum": 1, "maximum": 6}},
        ["slug"],
        True,
        "Write concept drafts for story {slug} (concept-A.md and the others).",
        _concepts,
    ),
    _spec(
        "workshop_regenerate",
        "Rewrite one concept draft with the owner's notes.",
        {"slug": _SLUG, "letter": {**_STR, "description": "draft letter, A to F"}, "feedback": _STR},
        ["slug", "letter", "feedback"],
        True,
        "Rewrite draft {letter} of story {slug} with your notes: {feedback}",
        _regenerate,
    ),
    _spec(
        "workshop_choose",
        "Promote one concept draft to concept.md.",
        {"slug": _SLUG, "letter": {**_STR, "description": "draft letter, A to F"}},
        ["slug", "letter"],
        True,
        "Make draft {letter} the concept of story {slug} (writes concept.md).",
        _choose,
    ),
    _spec(
        "workshop_map",
        "Write series.md: the part map with hook, cliff, reveals and runtime per part (12-25 minutes).",
        {
            "slug": _SLUG,
            "parts": {"type": "integer", "minimum": 1, "maximum": 40},
            "minutes_per_part": {"type": "integer", "minimum": 12, "maximum": 25},
        },
        ["slug", "parts"],
        True,
        "Write the part map (series.md) of story {slug} with {parts} parts.",
        _map,
    ),
    _spec(
        "workshop_write_part",
        "Write the full script and the stand-alone Shorts of one part, plus review notes.",
        {"slug": _SLUG, "n": {"type": "integer", "minimum": 1}},
        ["slug", "n"],
        True,
        "Write the script and Shorts files of part {n} of story {slug}. This takes several minutes.",
        _write,
    ),
    _spec(
        "workshop_revise_part",
        "Apply the owner's notes to a part's script and nothing else.",
        {"slug": _SLUG, "n": {"type": "integer", "minimum": 1}, "feedback": _STR},
        ["slug", "n", "feedback"],
        True,
        "Revise the script of part {n} of story {slug} with your notes: {feedback}",
        _revise,
    ),
    _spec(
        "workshop_approve",
        "Approve a written part (or all written parts) for production.",
        {"slug": _SLUG, "part": _PART},
        ["slug"],
        True,
        "Approve the script of story {slug} for production.",
        _approve,
    ),
    _spec(
        "workshop_design",
        "Save the series design (format, visual treatment, caption style, voice, thumbnail style, cadence). "
        "A long or square format shapes each part's full video, a short format its Shorts; a format that "
        "is missing or cannot be drawn is refused (NS-E320).",
        {
            "slug": _SLUG,
            "format": _STR,
            "visual_treatment": _STR,
            "caption_style": _STR,
            "voice": {**_STR, "description": "speaker name of a preset voice"},
            "thumbnail_style": _STR,
            "publish_cadence_days": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        ["slug"],
        True,
        "Save the design of story {slug} (design.yaml).",
        _design,
    ),
    _spec(
        "workshop_produce",
        "Queue production of approved parts: one master-first job per part, Shorts from the Shorts files.",
        {"slug": _SLUG, "part": _PART},
        ["slug"],
        True,
        "Queue production of story {slug}. Videos render overnight and land in the Outbox for review.",
        _produce,
    ),
    _spec(
        "workshop_status",
        "Show a story's status, parts, drafts, allowed design choices and the suggested next step.",
        {"slug": _SLUG},
        ["slug"],
        False,
        "Show the status of story {slug}.",
        _status,
    ),
    _spec(
        "workshop_list",
        "List every workshop story with its status.",
        {},
        [],
        False,
        "List the workshop stories.",
        _list,
    ),
]

TOOLS_BY_NAME: dict[str, dict[str, Any]] = {t["name"]: t for t in TOOLS}


def call_tool(ws: Workshop, name: str, args: dict[str, Any] | None = None) -> Any:
    """Run tool `name` with `args` (required keys checked, unknown keys rejected); returns JSON-ready data."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        raise WorkshopError(f"unknown workshop tool {name!r}")
    args = dict(args or {})
    schema = tool["args_schema"]
    missing = [k for k in schema["required"] if k not in args]
    unknown = [k for k in args if k not in schema["properties"]]
    if missing or unknown:
        raise WorkshopError(f"{name}: missing {missing or 'nothing'}, unknown {unknown or 'nothing'}")
    return _json(tool["fn"](ws, **args))
