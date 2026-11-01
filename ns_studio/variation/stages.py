"""Per-Short combo assignment for master-first jobs (spec section 8, component C7).

A master and its Shorts share `structure`, `pov_tone` and `pacing`; each Short draws its own `hook`,
`caption_style`, `visual_treatment` and `title_formula`. `length_bucket` is pinned to the bucket of
the selected moment. C5's `shorts_select` leaves the master's combo id on every Short; this stage
replaces it with the Short's own combo (ledger item `s<k>`).
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Any

from ns_studio.context import JobContext
from ns_studio.orchestrator import FORMAT_STAGES
from ns_studio.stages import Tenant, register
from ns_studio.variation.draw import draw_combo, record_combo

STAGE_NAME = "short_combos"
AFTER = "shorts_select"


class ShortCombosStage:
    """Draw and record one combo per selected Short; rewrite `combo_id` in `shorts_plan.json`."""

    name = STAGE_NAME
    tenant: Tenant = "llama"  # keeps the llama acquisition of the neighbouring text stages
    inputs: tuple[str, ...] = ("shorts_plan.json",)
    outputs: tuple[str, ...] = ("short_combos.json",)

    def run(self, ctx: JobContext) -> None:
        """Idempotent: combos already stored for a Short are reused, never redrawn."""
        plan: list[dict[str, Any]] = ctx.read_json("shorts_plan.json")
        stored = self.outputs[0]
        existing: dict[str, Any] = ctx.read_json(stored) if ctx.file(stored).exists() else {}
        rng = random.Random(ctx.job_id)
        now = datetime.now(UTC)
        combos: dict[str, Any] = {}
        for item in plan:
            k = str(item["k"])
            if k in existing:
                combos[k] = existing[k]
                continue
            fixed = {
                "structure": ctx.combo.structure,
                "pov_tone": ctx.combo.pov_tone,
                "pacing": ctx.combo.pacing,
                "length_bucket": str(item["length_bucket"]),
            }
            combo = draw_combo(ctx.db, ctx.channel, rng=rng, now=now, fixed=fixed)
            record_combo(ctx.db, ctx.channel.slug, ctx.job_id, f"s{k}", combo, now)
            combos[k] = combo.model_dump(mode="json")
        for item in plan:
            item["combo_id"] = combos[str(item["k"])]["combo_id"]
        ctx.write_json("shorts_plan.json", plan)
        ctx.write_json(self.outputs[0], combos)


def install_short_combos(fmt: str) -> bool:
    """Register the stage and place it right after `shorts_select`; True when the plan changed."""
    names = FORMAT_STAGES.get(fmt)
    if not names or AFTER not in names or STAGE_NAME in names:
        return False
    register(ShortCombosStage())
    names.insert(names.index(AFTER) + 1, STAGE_NAME)
    return True
