"""Unsafe-topic check of the chosen idea, before any planning (spec 18.3: ideas and stories)."""

from __future__ import annotations

from typing import Any

from ns_common.errors import NsError
from ns_studio.context import JobContext
from ns_studio.policy.gate import TEXT_STAGE
from ns_studio.policy.judge import JudgeFn, default_judge, verdict_failed
from ns_studio.policy.meta import unsafe_categories


def enabled(ctx: JobContext) -> bool:
    """True when the policy gates are part of this job's plan (so not for `--no-policy`)."""
    from ns_studio.orchestrator import FORMAT_STAGES

    return TEXT_STAGE in FORMAT_STAGES.get(ctx.fmt, ())


def idea_text(idea: dict[str, Any]) -> str:
    """The idea as the classifier reads it: premise, twist, setting and protagonist."""
    parts = [
        ("Premise", "premise"),
        ("Twist", "twist"),
        ("Setting", "setting"),
        ("Protagonist", "protagonist"),
    ]
    return "\n".join(f"{label}: {idea[k]}" for label, k in parts if str(idea.get(k, "")).strip())


def idea_problem(ctx: JobContext, idea: dict[str, Any], judge: JudgeFn | None = None) -> str | None:
    """Why the classifier flags `idea` ("graphic_harm; ..."), or None when it is fine or the gates are off."""
    if not enabled(ctx):
        return None
    answer = (judge or default_judge)(
        ctx,
        "unsafe_topics",
        story=idea_text(idea),
        categories=list(unsafe_categories()),
        audience=ctx.channel.audience,
    )
    if not verdict_failed(answer):
        return None
    cats = ", ".join(str(f.get("category", "?")) for f in answer.get("flags", [])) or "unspecified"
    return f"{cats}; {answer.get('reason', '')}".strip()


def require_safe_idea(ctx: JobContext, idea: dict[str, Any], judge: JudgeFn | None = None) -> None:
    """Raise NS-E601 when the classifier flags `idea`."""
    why = idea_problem(ctx, idea, judge)
    if why is not None:
        raise NsError("NS-E601", f"idea {str(idea.get('premise', ''))[:80]!r}: {why}", check="unsafe_topics")
