"""Prompt loader, JSON schemas for LLM answers and the `call_json` / `call_text` helpers (component C5)."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

import jinja2
import yaml
from pydantic import BaseModel, ValidationError

from ns_common.errors import NsError
from ns_common.models import Idea, Part, Seo, VisualBeat
from ns_common.paths import contracts_dir, repo_root
from ns_studio.context import JobContext
from ns_studio.text.exemplars import load_exemplars
from ns_studio.text.models_local import ClipPlanItem, PolicyCheckResult

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "prompts"
FIXTURES = repo_root() / "tests" / "fixtures" / "llm"
_ENV = jinja2.Environment(
    undefined=jinja2.StrictUndefined, trim_blocks=True, lstrip_blocks=True, autoescape=False
)


def _arr(item: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"type": "array", "items": item, **extra}


RUBRIC_KEYS = ("hook", "clarity", "pacing", "payoff", "cliffhangers", "originality", "voice_fit")
SCORES: dict[str, Any] = {
    "type": "object",
    "properties": {k: {"type": "number", "minimum": 1, "maximum": 10} for k in RUBRIC_KEYS},
    "required": list(RUBRIC_KEYS),
}

PROMPT_SCHEMAS: dict[str, dict[str, Any]] = {
    "ideas": {
        "type": "object",
        "properties": {"ideas": _arr(Idea.model_json_schema(), minItems=1)},
        "required": ["ideas"],
    },
    "story_plan": {
        "type": "object",
        "properties": {
            "beats": _arr(
                {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "summary": {"type": "string"}},
                    "required": ["name", "summary"],
                },
                minItems=3,
            ),
            "part_breaks": _arr({"type": "integer"}),
            "ending_line_idea": {"type": "string"},
        },
        "required": ["beats", "part_breaks", "ending_line_idea"],
    },
    "critic": {
        "type": "object",
        "properties": {"scores": SCORES, "fixes": _arr({"type": "string"}), "verdict": {"type": "string"}},
        "required": ["scores", "fixes"],
    },
    "split": {
        "type": "object",
        "properties": {"parts": _arr(Part.model_json_schema(), minItems=1)},
        "required": ["parts"],
    },
    "hook": {
        "type": "object",
        "properties": {"hooks": _arr({"type": "string"}, minItems=5, maxItems=5)},
        "required": ["hooks"],
    },
    "judge": {
        "type": "object",
        "properties": {"index": {"type": "integer", "minimum": 0}, "reason": {"type": "string"}},
        "required": ["index", "reason"],
    },
    "seo": Seo.model_json_schema(),
    "visual_plan": {
        "type": "object",
        "properties": {"beats": _arr(VisualBeat.model_json_schema(), minItems=1)},
        "required": ["beats"],
    },
}


_STR = {"type": "string"}

# clip_select also returns the layout (a format preset id) the model picked per candidate, and why (C8).
_CLIP_ITEM = ClipPlanItem.model_json_schema()
_CLIP_ITEM["properties"] = {**_CLIP_ITEM["properties"], "layout": _STR, "layout_reason": _STR}
_INT = {"type": "integer"}
_SEO_PROPS = Seo.model_json_schema()["properties"]
_SEO_TITLES = Seo.model_json_schema()
_SEO_TITLES["properties"] = {**_SEO_PROPS, "titles": _arr(_STR, minItems=1, maxItems=8)}

PROMPT_SCHEMAS.update(
    {
        "seo_titles": _SEO_TITLES,
        "long_plan": {
            "type": "object",
            "properties": {
                "working_title": _STR,
                "chapters": _arr(
                    {
                        "type": "object",
                        "properties": {
                            "n": _INT,
                            "title": _STR,
                            "beats": _arr(_STR, minItems=1),
                            "cliff": _STR,
                            "word_budget": _INT,
                        },
                        "required": ["title", "beats", "cliff"],
                    },
                    minItems=5,
                    maxItems=7,
                ),
            },
            "required": ["working_title", "chapters"],
        },
        "chapter_draft": {
            "type": "object",
            "properties": {"text": _STR, "summary": _STR},
            "required": ["text", "summary"],
        },
        "continuity": {
            "type": "object",
            "properties": {
                "ok": {"type": "boolean"},
                "issues": _arr(
                    {
                        "type": "object",
                        "properties": {"chapter": _INT, "problem": _STR, "fix": _STR},
                        "required": ["chapter", "problem", "fix"],
                    }
                ),
            },
            "required": ["ok", "issues"],
        },
        "long_seo": {
            "type": "object",
            "properties": {
                "titles": _arr(_STR, minItems=5, maxItems=5),
                "description": _STR,
                "tags": _arr(_STR, minItems=1),
                "hashtags": _arr(_STR),
                "keywords": _arr(_STR),
                "chapter_titles": _arr(
                    {
                        "type": "object",
                        "properties": {"n": _INT, "title": _STR},
                        "required": ["n", "title"],
                    },
                    minItems=1,
                ),
            },
            "required": ["titles", "description", "tags", "hashtags", "chapter_titles"],
        },
        "shorts_select": {
            "type": "object",
            "properties": {
                "moments": _arr(
                    {
                        "type": "object",
                        "properties": {
                            "chapter": _INT,
                            "sentence_start": {"type": "integer", "minimum": 0},
                            "sentence_end": {"type": "integer", "minimum": 0},
                            "length_bucket": _INT,
                            "why_standalone": _STR,
                        },
                        "required": ["chapter", "sentence_start", "sentence_end", "why_standalone"],
                    },
                    minItems=1,
                )
            },
            "required": ["moments"],
        },
        "short_hook": {
            "type": "object",
            "properties": {"hooks": _arr(_STR, minItems=5, maxItems=5), "closing_line": _STR},
            "required": ["hooks", "closing_line"],
        },
        "policy_check": PolicyCheckResult.model_json_schema(),
        "clip_judge": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}, "reasons": _arr(_STR)},
            "required": ["ok", "reasons"],
        },
        "clip_plan": {
            "type": "object",
            "properties": {"clips": _arr(_CLIP_ITEM, minItems=1)},
            "required": ["clips"],
        },
    }
)


@dataclass(frozen=True)
class Prompt:
    """A parsed prompt file: front-matter fields, the fixed system text and the user template."""

    name: str
    version: int
    thinking: bool
    schema: str | None
    system: str
    user_template: jinja2.Template


@cache
def load_prompt(name: str) -> Prompt:
    """Load `prompts/<name>.md`; raises ValueError when the YAML front-matter is missing or incomplete."""
    raw = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").replace("\r\n", "\n")
    if not raw.startswith("---\n"):
        raise ValueError(f"prompt {name}: missing front-matter")
    parts = raw.split("---\n", 2)
    if len(parts) < 3:
        raise ValueError(f"prompt {name}: unterminated front-matter")
    meta = yaml.safe_load(parts[1])
    if not isinstance(meta, dict) or not {"version", "thinking", "schema"} <= set(meta):
        raise ValueError(f"prompt {name}: front-matter needs version, thinking, schema")
    system, _, user = parts[2].partition("---USER---")
    return Prompt(
        name=name,
        version=int(meta["version"]),
        thinking=bool(meta["thinking"]),
        schema=meta["schema"],
        system=system.strip(),
        user_template=_ENV.from_string(user.strip()),
    )


@lru_cache(maxsize=1)
def unsafe_categories() -> tuple[dict[str, Any], ...]:
    """The categories of `contracts/policy/unsafe_topics.yaml` (id, label, description, examples)."""
    path = contracts_dir() / "policy" / "unsafe_topics.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return tuple(data["categories"])


@lru_cache(maxsize=1)
def policy_rules_text() -> str:
    """Bullet list of the content rules injected into prompts (unsafe topics plus misleading claims)."""
    lines = [f"- {c['label']}: {c['description']}" for c in unsafe_categories()]
    raw = (contracts_dir() / "policy" / "misleading_claims.txt").read_text(encoding="utf-8").splitlines()
    claims = [ln.strip() for ln in raw if ln.strip() and not ln.startswith("#")]
    lines.append("- Never claim the story is real. Forbidden phrases include: " + ", ".join(claims[:8]) + ".")
    return "\n".join(lines)


def render(prompt: Prompt, **vars: Any) -> str:
    """Render the user part of `prompt`; undefined variables raise (StrictUndefined)."""
    return prompt.user_template.render(**vars)


def _common_vars(ctx: JobContext) -> dict[str, Any]:
    return {
        "channel": ctx.channel,
        "combo": ctx.combo,
        "policy_rules": policy_rules_text(),
        "categories": unsafe_categories(),
        "exemplars": load_exemplars(ctx.channel.niche),
    }


def call_json(
    ctx: JobContext,
    name: str,
    *,
    fixture: str | None = None,
    schema_name: str | None = None,
    **vars: Any,
) -> dict[str, Any]:
    """Render prompt `name`, call the LLM for JSON and record the prompt version on `ctx`.

    `fixture` is the `prompt_name` passed to the LLM (a fake serves `<fixture>.json`); `schema_name`
    overrides the front-matter schema. Template variables go in `vars`.
    """
    p = load_prompt(name)
    schema_key = schema_name or p.schema
    if not schema_key:
        raise ValueError(f"prompt {name} has no schema")
    ctx.prompt_versions[name] = p.version
    user = render(p, **(_common_vars(ctx) | vars))
    return ctx.services.llm.complete_json(
        prompt_name=fixture or name,
        system=p.system,
        user=user,
        schema=PROMPT_SCHEMAS[schema_key],
        thinking=p.thinking,
    )


def call_text(ctx: JobContext, name: str, *, fixture: str | None = None, **vars: Any) -> str:
    """Render prompt `name` and return the LLM's plain-text answer; records the prompt version."""
    p = load_prompt(name)
    ctx.prompt_versions[name] = p.version
    user = render(p, **(_common_vars(ctx) | vars))
    return ctx.services.llm.complete_text(
        prompt_name=fixture or name, system=p.system, user=user, thinking=p.thinking
    )


def parse_model[M: BaseModel](model: type[M], data: Any, what: str) -> M:
    """Validate an LLM answer with a pydantic model; a mismatch raises NS-E302."""
    try:
        return model.model_validate(data)
    except ValidationError as e:
        raise NsError("NS-E302", f"{what}: {e.error_count()} problem(s): {str(e)[:300]}") from e
