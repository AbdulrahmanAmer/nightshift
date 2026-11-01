"""Readers for the job-directory files the policy checks look at (component C7).

The shapes are tolerant on purpose: shorts-only jobs write `parts.json` + `seo.json`, master-first
jobs write `seo_long.json`, `seo_shorts.json` and `shorts_plan.json`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache, lru_cache
from pathlib import Path
from typing import Any

import yaml

from ns_common.paths import contracts_dir
from ns_studio.context import JobContext


@dataclass
class MetaItem:
    """Metadata of one video of the job (`long`, `s1`, `s2`, ...)."""

    name: str
    title: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    hook: str = ""

    def texts(self) -> list[str]:
        """Every user-visible string of the item (title, description, tags, hashtags, hook)."""
        return [self.title, self.description, *self.tags, *self.hashtags, self.hook]


def _as_list(data: Any, key: str) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        inner = data.get(key)
        if isinstance(inner, list):
            return inner
    return []


def _read(ctx: JobContext, name: str) -> Any:
    return ctx.read_json(name) if ctx.file(name).exists() else None


def _from_seo(name: str, seo: Any, hook: str = "") -> MetaItem:
    d = seo if isinstance(seo, dict) else {}
    return MetaItem(
        name=name,
        title=str(d.get("title", "")),
        description=str(d.get("description", "")),
        tags=[str(t) for t in d.get("tags", [])],
        hashtags=[str(t) for t in d.get("hashtags", [])],
        hook=hook,
    )


def collect_items(ctx: JobContext) -> list[MetaItem]:
    """All metadata items found in the job directory."""
    items: list[MetaItem] = []
    long_seo = _read(ctx, "seo_long.json")
    if isinstance(long_seo, dict):
        items.append(_from_seo("long", long_seo))
    parts = _as_list(_read(ctx, "parts.json"), "parts")
    short_seos = _as_list(_read(ctx, "seo_shorts.json"), "shorts") or _as_list(_read(ctx, "seo.json"), "seo")
    plan_hooks = {}
    for i, mom in enumerate(_as_list(_read(ctx, "shorts_plan.json"), "shorts"), 1):
        if isinstance(mom, dict):
            plan_hooks[i] = str(mom.get("hook", ""))
    for i, seo in enumerate(short_seos, 1):
        hook = ""
        if i - 1 < len(parts) and isinstance(parts[i - 1], dict):
            hook = str(parts[i - 1].get("hook", ""))
        hook = hook or plan_hooks.get(i, "")
        items.append(_from_seo(f"s{i}", seo, hook))
    return items


def story_text(ctx: JobContext) -> str:
    """The story text: `story.json` text/chapters, else the concatenated parts."""
    story = _read(ctx, "story.json")
    if isinstance(story, dict):
        if story.get("text"):
            return str(story["text"])
        chapters = story.get("chapters")
        if isinstance(chapters, list):
            return "\n\n".join(str(c.get("text", "")) for c in chapters if isinstance(c, dict))
    parts = _as_list(_read(ctx, "parts.json"), "parts")
    return "\n\n".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))


def short_moments(ctx: JobContext) -> list[dict[str, Any]]:
    """Selected Short moments (`shorts_plan.json`) as `{k, text, hook}` dicts."""
    out: list[dict[str, Any]] = []
    for i, mom in enumerate(_as_list(_read(ctx, "shorts_plan.json"), "shorts"), 1):
        if not isinstance(mom, dict):
            continue
        text = mom.get("text") or " ".join(str(s) for s in mom.get("sentences", []))
        out.append({"k": mom.get("k", i), "text": str(text), "hook": str(mom.get("hook", ""))})
    return out


# ---------------------------------------------------------------- policy lists
def _lines(path: Path) -> list[str]:
    return [
        ln.strip().lower()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]


@cache
def policy_list(name: str) -> tuple[str, ...]:
    """Lower-cased entries of `contracts/policy/<name>` (comments and blanks removed)."""
    return tuple(_lines(contracts_dir() / "policy" / name))


@lru_cache(maxsize=1)
def unsafe_categories() -> tuple[dict[str, Any], ...]:
    """Categories of `contracts/policy/unsafe_topics.yaml`."""
    data = yaml.safe_load((contracts_dir() / "policy" / "unsafe_topics.yaml").read_text(encoding="utf-8"))
    return tuple(data["categories"])
