"""Per-Short combo assignment for master-first jobs (spec section 8, component C7).

A master and its Shorts share `structure`, `pov_tone` and `pacing`; each Short draws its own `hook`,
`caption_style`, `visual_treatment` and `title_formula`. `length_bucket` is pinned to the bucket of
the selected moment. `shorts_select` draws each Short's combo through `short_draw` (it needs the hook
style before asking for hooks); this stage draws for plans `shorts_select` did not make (a series part) and
puts the Short's own `combo_id` (ledger item `s<k>`) into every plan item.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ns_common.atomic import read_text_retry
from ns_studio.context import JobContext
from ns_studio.orchestrator import FORMAT_STAGES
from ns_studio.stages import Tenant, register
from ns_studio.variation.short_draw import draw_short_combos

STAGE_NAME = "short_combos"
AFTER = "shorts_select"
# Where the stage sits in each master-first plan: series parts have no `shorts_select`, `script_import` makes
# their Shorts plan.
AFTER_BY_FORMAT = {"master_from_script": "script_import"}


class ShortCombosStage:
    """Draw and record one combo per selected Short; rewrite `combo_id` in `shorts_plan.json`."""

    name = STAGE_NAME
    tenant: Tenant = "llama"  # keeps the llama acquisition of the neighbouring text stages
    inputs: tuple[str, ...] = ("shorts_plan.json",)
    outputs: tuple[str, ...] = ("short_combos.json",)

    def run(self, ctx: JobContext) -> None:
        """Idempotent: combos already stored for a Short are reused, never redrawn.

        `shorts_select` normally drew them already (it needs each Short's hook style); this stage draws for
        plans that did not come from it (a series part) and writes `combo_id` into every plan item.
        """
        plan: list[dict[str, Any]] = ctx.read_json("shorts_plan.json")
        combos = draw_short_combos(ctx, plan, extra_fixed=series_fixed(ctx.series))
        for item in plan:
            item["combo_id"] = combos[str(item["k"])].combo_id
        ctx.write_json("shorts_plan.json", plan)


def series_fixed(series: Mapping[str, Any]) -> dict[str, str]:
    """Combo fields a series pins for every video of the series.

    The design pins the caption style and visual treatment. `pacing` is the pace the scripts were written at
    (`series_context.json`, stored by `Workshop.produce`): a part drawn at another pace would run its words
    at another speed and miss the part's runtime, and its Shorts would no longer fit their time bands.
    """
    design = series.get("design") or {}
    fixed = {f: str(design[f]) for f in ("caption_style", "visual_treatment") if design.get(f)}
    if series.get("pacing"):
        fixed["pacing"] = str(series["pacing"])
    if series.get("hook"):
        # The hook texts come from the workshop's `sNN.md` files, written in one hook style. A hook style
        # drawn per Short would be credited with views it never earned and would burn the 30-day no-repeat
        # window, so the ledger records the style the hooks were really written in.
        fixed["hook"] = str(series["hook"])
    return fixed


def job_series_fixed(job_dir: Path) -> dict[str, str]:
    """`series_fixed` for the job directory of a series part (before its `JobContext` exists)."""
    try:
        return series_fixed(json.loads(read_text_retry(job_dir / "series_context.json")))
    except (OSError, ValueError, AttributeError):
        return {}


def install_short_combos(fmt: str) -> bool:
    """Register the stage and place it right after `shorts_select`; True when the plan changed."""
    current = FORMAT_STAGES.get(fmt)
    after = AFTER_BY_FORMAT.get(fmt, AFTER)
    if not current or after not in current or STAGE_NAME in current:
        return False
    register(ShortCombosStage())
    names = list(current)
    names.insert(names.index(after) + 1, STAGE_NAME)
    FORMAT_STAGES[fmt] = names
    return True
