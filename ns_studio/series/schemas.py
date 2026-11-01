"""JSON schemas of the workshop prompts, registered with the C5 prompt loader (component C11).

`ns_studio.text.prompts.call_json` looks schemas up by name in `PROMPT_SCHEMAS`; importing this module adds
`workshop_concepts`, `workshop_map` and `workshop_part_outline`.
"""

from __future__ import annotations

from typing import Any

from ns_studio.series.store import MAX_MINUTES, MIN_MINUTES
from ns_studio.text.prompts import PROMPT_SCHEMAS

_STR: dict[str, Any] = {"type": "string"}


def _arr(item: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"type": "array", "items": item, **extra}


CONCEPT_ITEM: dict[str, Any] = {
    "type": "object",
    "properties": {
        "letter": _STR,
        "title": _STR,
        "premise": _STR,
        "cast": _arr(_STR, minItems=1),
        "setting": _STR,
        "tone": _STR,
        "beats": _arr(_STR, minItems=5, maxItems=5),
        "why_it_works": _STR,
        "audience": _STR,
        "series_potential": _STR,
        "risks": _arr(_STR),
    },
    "required": ["letter", "title", "premise", "cast", "beats", "series_potential", "risks"],
}

MAP_PART: dict[str, Any] = {
    "type": "object",
    "properties": {
        "n": {"type": "integer", "minimum": 1},
        "title": _STR,
        "hook": _STR,
        "cliff": _STR,
        "reveals": _STR,
        "minutes": {"type": "integer", "minimum": MIN_MINUTES, "maximum": MAX_MINUTES},
        "recap": _STR,
        "standalone": {"type": "boolean"},
    },
    "required": ["n", "title", "hook", "cliff", "reveals", "minutes"],
}

OUTLINE_CHAPTER: dict[str, Any] = {
    "type": "object",
    "properties": {
        "n": {"type": "integer", "minimum": 1},
        "title": _STR,
        "beats": _arr(_STR, minItems=1),
        "cliff": _STR,
    },
    "required": ["title", "beats", "cliff"],
}

WORKSHOP_SCHEMAS: dict[str, dict[str, Any]] = {
    "workshop_concepts": {
        "type": "object",
        "properties": {"concepts": _arr(CONCEPT_ITEM, minItems=1, maxItems=6)},
        "required": ["concepts"],
    },
    "workshop_map": {
        "type": "object",
        "properties": {
            "series_title": _STR,
            "parts": _arr(MAP_PART, minItems=1),
            "arc_payoff": _STR,
            "issues": _arr(_STR),
        },
        "required": ["parts"],
    },
    "workshop_part_outline": {
        "type": "object",
        "properties": {
            "working_title": _STR,
            "chapters": _arr(OUTLINE_CHAPTER, minItems=5, maxItems=12),
        },
        "required": ["working_title", "chapters"],
    },
}

PROMPT_SCHEMAS.update(WORKSHOP_SCHEMAS)
