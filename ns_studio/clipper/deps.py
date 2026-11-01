"""C8 Clipper: seams to the other components that are reached lazily by name.

C5 (`ns_studio.text.prompts.call_json`) and C6 (`ns_studio.media.*`) land independently of C8. Looking them
up with `importlib` keeps this package importable and type-checkable while they are in flight; once they
are on `main` these helpers stay as thin, typed wrappers. The prompt fallback below is used only while C5
has not shipped `clip_select.md` and `clip_seo.md`; it is removed when it does.
"""
from __future__ import annotations

import importlib
import json
from types import ModuleType
from typing import Any

from ns_studio.context import JobContext

CLIP_SELECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "clips": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "start_s": {"type": "number"},
                    "end_s": {"type": "number"},
                    "title": {"type": "string"},
                    "hook_line": {"type": "string"},
                    "why": {"type": "string"},
                    "score": {"type": "number", "minimum": 0, "maximum": 1},
                    "keywords": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["start_s", "end_s", "title", "why", "score"],
            },
        }
    },
    "required": ["clips"],
}

CLIP_SEO_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "description", "tags", "hashtags"],
}

_FALLBACK_SYSTEM: dict[str, str] = {
    "clip_select": (
        "You pick standalone moments from a transcript of a video the owner made. Return 3 to 10 moments "
        "that start on a complete sentence, end on a payoff, need no outside context, contain no private "
        "information about third parties and are not mostly filler. Honest titles of at most 70 "
        "characters. Return JSON only."
    ),
    "clip_seo": (
        "You write honest YouTube Shorts metadata for a clip of the owner's own video: title of at most "
        "70 characters, one or two plain description lines, 10 to 20 relevant tags and 3 to 5 hashtags "
        "including #Shorts. No false claims. Return JSON only."
    ),
}
_FALLBACK_SCHEMA = {"clip_select": CLIP_SELECT_SCHEMA, "clip_seo": CLIP_SEO_SCHEMA}


def module(name: str) -> ModuleType:
    """Import a sibling component module by dotted name (ImportError when it has not landed)."""
    return importlib.import_module(name)


def media(name: str) -> Any:
    """`ns_studio.media.<name>` (C6)."""
    return importlib.import_module(f"ns_studio.media.{name}")


def call_json(ctx: JobContext, name: str, **variables: Any) -> dict[str, Any]:
    """C5's `call_json(ctx, name, **vars)`; direct LLM call with a built-in prompt when C5 lacks `name`."""
    try:
        prompts = importlib.import_module("ns_studio.text.prompts")
        result: dict[str, Any] = prompts.call_json(ctx, name, **variables)
        return result
    except (ImportError, FileNotFoundError, KeyError, AttributeError, AssertionError):
        ctx.log.warn("prompt_fallback", f"using built-in prompt for {name}", prompt=name)
    ctx.prompt_versions.setdefault(name, 0)
    answer: dict[str, Any] = ctx.services.llm.complete_json(
        prompt_name=name,
        system=_FALLBACK_SYSTEM[name],
        user=json.dumps(variables, ensure_ascii=False, default=str),
        schema=_FALLBACK_SCHEMA[name],
        thinking=name == "clip_select",
    )
    return answer
