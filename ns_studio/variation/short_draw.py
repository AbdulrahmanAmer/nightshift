"""Draw and store the combo of every Short of a master-first job (spec section 8, component C7).

A master and its Shorts share `structure`, `pov_tone` and `pacing`; each Short draws its own `hook`,
`caption_style`, `visual_treatment` and `title_formula`, and its `length_bucket` is the bucket of its moment.
The combos live in `short_combos.json` (k -> combo); `shorts_select` draws them before it asks for hooks, and
the SEO, derive and outbox stages read them back, so the combo recorded in the ledger is the one applied.
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from ns_common.models import Combo
from ns_studio.context import JobContext
from ns_studio.variation.draw import draw_combo, record_combo

STORED = "short_combos.json"


def load_short_combos(ctx: JobContext) -> dict[str, Combo]:
    """The stored per-Short combos of the job by `k` as strings; empty when none were drawn yet."""
    if not ctx.file(STORED).exists():
        return {}
    return {str(k): Combo.model_validate(v) for k, v in ctx.read_json(STORED).items()}


def draw_short_combos(
    ctx: JobContext, moments: Sequence[Mapping[str, Any]], *, extra_fixed: Mapping[str, str] | None = None
) -> dict[str, Combo]:
    """Combo per moment (`k`, `length_bucket`): stored ones with the same bucket are kept, others drawn.

    `extra_fixed` pins more combo fields (a series design fixes caption style and visual treatment). Each new
    combo is recorded in the ledger as item `s<k>`; `short_combos.json` is rewritten and the map returned.
    """
    stored = load_short_combos(ctx)
    rng = random.Random(ctx.job_id)
    now = datetime.now(UTC)
    out: dict[str, Combo] = {}
    for m in moments:
        k, bucket = str(m["k"]), int(m["length_bucket"])
        prev = stored.get(k)
        if prev is not None and prev.length_bucket == bucket and all(
            getattr(prev, f) == v for f, v in (extra_fixed or {}).items()
        ):
            out[k] = prev
            continue
        fixed = {
            "structure": ctx.combo.structure,
            "pov_tone": ctx.combo.pov_tone,
            "pacing": ctx.combo.pacing,
            "length_bucket": str(bucket),
            **(extra_fixed or {}),
        }
        combo = draw_combo(
            ctx.db, ctx.channel, rng=rng, now=now, fixed=fixed,
            on_repeat=lambda why, k=k, fixed=fixed: ctx.log.warn(
                "variation_repeated", f"Short {k}: {why}", code="NS-W325", job_id=ctx.job_id, fixed=fixed
            ),
        )
        record_combo(ctx.db, ctx.channel.slug, ctx.job_id, f"s{k}", combo, now)
        out[k] = combo
    ctx.write_json(STORED, {k: c.model_dump(mode="json") for k, c in out.items()})
    return out
