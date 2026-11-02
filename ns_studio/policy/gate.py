"""Policy gate stages and plan wiring (spec section 18.8, component C7)."""

from __future__ import annotations

from collections.abc import Sequence

from ns_common.errors import NsError
from ns_common.models import PolicyAdvice, PolicyCheck, PolicyResult
from ns_studio.context import JobContext
from ns_studio.orchestrator import FORMAT_STAGES
from ns_studio.policy import judge
from ns_studio.policy.base import Check, Gate
from ns_studio.policy.final_checks import compute_advice, final_checks
from ns_studio.policy.text_checks import text_checks
from ns_studio.stages import Tenant, register

TEXT_STAGE = "policy_text"
FINAL_STAGE = "policy_final"
_TEXT_AFTER = (
    "visual_plan",
    "visual_plan_long",
    "shorts_plan",
    "shorts_select",
    "short_combos",
    "seo_shorts",
    "seo",
    "split",
)
_TEXT_BEFORE = ("voice",)
_FINAL_BEFORE = ("outbox",)
_FINAL_AFTER = ("qc",)


def run_gate(ctx: JobContext, gate: Gate, checks: Sequence[Check]) -> PolicyResult:
    """Run every check, write `policy_<gate>.json`, and raise the first failing check's NS-E6xx code."""
    results: dict[str, PolicyCheck] = {}
    for chk in checks:
        results[chk.name] = chk.run(ctx)
        level = "info" if results[chk.name].passed else "warn"
        getattr(ctx.log, level)(
            "policy_check", check=chk.name, passed=results[chk.name].passed, detail=results[chk.name].detail,
            job_id=ctx.job_id,
        )
    advice = compute_advice(ctx) if gate == "final" else PolicyAdvice()
    result = PolicyResult(
        stage=gate, passed=all(r.passed for r in results.values()), checks=results, advice=advice
    )
    ctx.write_json(f"policy_{gate}.json", result.model_dump(mode="json"))
    for chk in checks:
        res = results[chk.name]
        if not res.passed and chk.code:
            raise NsError(chk.code, f"{chk.name}: {res.detail}", check=chk.name)
    return result


class PolicyTextStage:
    """Text gate: runs after the visual plan, before voice rendering."""

    name = TEXT_STAGE
    tenant: Tenant = "llama"
    uses_embed: bool = True  # calls the embed service; the orchestrator leases it around run()
    inputs: tuple[str, ...] = (
        "story.json",
        "parts.json",
        "seo.json",
        "seo_long.json",
        "seo_shorts.json",
        "shorts_plan.json",
        "visual_plan.json",
    )
    outputs: tuple[str, ...] = ("policy_text.json",)

    def __init__(self, checks: Sequence[Check] | None = None) -> None:
        self.checks = list(checks) if checks is not None else text_checks()

    def run(self, ctx: JobContext) -> None:
        """Evaluate the text checks."""
        run_gate(ctx, "text", self.checks)


class PolicyFinalStage:
    """Final gate: runs after QC, before the outbox is written."""

    name = FINAL_STAGE
    tenant: Tenant = None
    inputs: tuple[str, ...] = ("policy_text.json", "long/master.mp4")
    outputs: tuple[str, ...] = ("policy_final.json",)

    def __init__(self, checks: Sequence[Check] | None = None) -> None:
        self.checks = list(checks) if checks is not None else final_checks()

    def run(self, ctx: JobContext) -> None:
        """Evaluate the final checks and write the disclosure advice."""
        run_gate(ctx, "final", self.checks)


def available() -> bool:
    """True when the LLM-judged checks can run (C5 delivered the `policy_check` prompt)."""
    return judge.available()


def _insert_after(names: list[str], new: str, anchors: Sequence[str]) -> bool:
    """Insert `new` after the last stage of the plan that is one of `anchors`."""
    positions = [i for i, n in enumerate(names) if n in anchors]
    if not positions:
        return False
    names.insert(max(positions) + 1, new)
    return True


def _insert_before(names: list[str], new: str, anchors: Sequence[str]) -> bool:
    for a in anchors:
        if a in names:
            names.insert(names.index(a), new)
            return True
    return False


def install_gates(fmt: str) -> bool:
    """Register the gate stages and place them in `FORMAT_STAGES[fmt]`; idempotent.

    The text gate goes after the last of `_TEXT_AFTER` found, else before `voice`; the final gate
    goes before `outbox`, else after `qc`, else at the end. Returns True when the plan changed.
    """
    current = FORMAT_STAGES.get(fmt)
    if not current:
        return False
    names = list(current)  # never mutate a list another module may hold
    register(PolicyTextStage())
    register(PolicyFinalStage())
    changed = False
    if TEXT_STAGE not in names:
        placed = _insert_after(names, TEXT_STAGE, _TEXT_AFTER) or _insert_before(
            names, TEXT_STAGE, _TEXT_BEFORE
        )
        changed = True
        if not placed:
            names.append(TEXT_STAGE)
    if FINAL_STAGE not in names:
        placed = _insert_before(names, FINAL_STAGE, _FINAL_BEFORE) or _insert_after(
            names, FINAL_STAGE, _FINAL_AFTER
        )
        if not placed:
            names.append(FINAL_STAGE)
        changed = True
    FORMAT_STAGES[fmt] = names
    return changed
