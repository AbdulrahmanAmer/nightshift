"""LLM judge access for the policy checks (component C7).

C5 owns the `policy_check` prompt. Its schema is `{unsafe_categories[], hook_honest, title_honest,
standalone | null, notes}` and one call judges one story (optionally with a title, a hook and a
selected moment). The checks in this package talk to a mode-based `JudgeFn` that returns a
normalised answer; `default_judge` translates between the two. The prompt module is reached through
`importlib`, and tests inject their own `JudgeFn`.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ns_studio.context import JobContext

JudgeFn = Callable[..., dict[str, Any]]
PROMPT_FILE = Path(__file__).resolve().parents[1] / "prompts" / "policy_check.md"
PASS: dict[str, Any] = {"verdict": "pass", "reason": "", "flags": [], "intensity": "low", "failed_items": []}


def available() -> bool:
    """True when C5 has delivered the `policy_check` prompt file."""
    return PROMPT_FILE.exists()


def _ask(ctx: JobContext, **variables: Any) -> dict[str, Any]:
    prompts = importlib.import_module("ns_studio.text.prompts")
    return dict(prompts.call_json(ctx, "policy_check", **variables))


def default_judge(ctx: JobContext, mode: str, **variables: Any) -> dict[str, Any]:
    """Answer a mode-based question with one or more `policy_check` calls.

    Normalised answer: `{verdict: pass|fail, reason, flags: [{category, reason}], intensity,
    failed_items: [str]}`. `tag_relevance` asks the model, per item, whether the tags describe the story.
    """
    story = str(variables.get("story", ""))
    if mode == "unsafe_topics":
        a = _ask(ctx, story=story)
        notes = str(a.get("notes", ""))
        cats = [str(c) for c in a.get("unsafe_categories", [])]
        return {
            **PASS,
            "verdict": "fail" if cats else "pass",
            "reason": notes,
            "flags": [{"category": c, "reason": notes} for c in cats],
        }
    if mode == "hook_title_honesty":
        failed: list[str] = []
        reasons: list[str] = []
        for it in variables.get("items", []):
            extra = {k: str(it[k]) for k in ("title", "hook") if it.get(k)}
            a = _ask(ctx, story=story, **extra)
            if a.get("hook_honest") is False or a.get("title_honest") is False:
                failed.append(str(it["item"]))
                reasons.append(f"{it['item']}: {a.get('notes', '')}")
        return {**PASS, "verdict": "fail" if failed else "pass", "reason": "; ".join(reasons),
                "failed_items": failed}
    if mode == "standalone_short":
        failed = []
        reasons = []
        for m in variables.get("moments", []):
            a = _ask(ctx, story=story, span_text=str(m["text"]))
            if a.get("standalone") is False:
                failed.append(str(m["k"]))
                reasons.append(f"s{m['k']}: {a.get('notes', '')}")
        return {**PASS, "verdict": "fail" if failed else "pass", "reason": "; ".join(reasons),
                "failed_items": failed}
    if mode == "tag_relevance":
        failed = []
        reasons = []
        for it in variables.get("items", []):
            tags = [str(t) for t in it.get("tags", [])]
            if not tags:
                continue
            extra = {"title": str(it["title"])} if it.get("title") else {}
            a = _ask(ctx, story=story, tags=", ".join(tags), **extra)
            if a.get("tags_relevant") is False:
                failed.append(str(it["item"]))
                reasons.append(f"{it['item']}: {a.get('notes', '')}")
        return {**PASS, "verdict": "fail" if failed else "pass", "reason": "; ".join(reasons),
                "failed_items": failed}
    raise ValueError(f"unknown policy_check mode {mode!r}")


def verdict_failed(answer: dict[str, Any]) -> bool:
    """True when the judge's verdict is a failure or it raised flags."""
    return str(answer.get("verdict", "pass")).lower() == "fail" or bool(answer.get("flags"))
