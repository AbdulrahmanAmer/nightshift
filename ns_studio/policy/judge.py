"""LLM judge access for the policy checks (component C7).

The `policy_check` prompt is owned by C5 (CR-C7-2). It is reached through `importlib` so this package
imports cleanly before C5's modules exist, and tests inject their own `JudgeFn`.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ns_studio.context import JobContext

JudgeFn = Callable[..., dict[str, Any]]
PROMPT_FILE = Path(__file__).resolve().parents[1] / "prompts" / "policy_check.md"


def available() -> bool:
    """True when C5 has delivered the `policy_check` prompt file."""
    return PROMPT_FILE.exists()


def default_judge(ctx: JobContext, mode: str, **variables: Any) -> dict[str, Any]:
    """Call the `policy_check` prompt with `mode` and return its JSON answer."""
    prompts = importlib.import_module("ns_studio.text.prompts")
    return dict(prompts.call_json(ctx, "policy_check", mode=mode, **variables))


def verdict_failed(answer: dict[str, Any]) -> bool:
    """True when the judge's verdict is a failure or it raised flags."""
    return str(answer.get("verdict", "pass")).lower() == "fail" or bool(answer.get("flags"))
