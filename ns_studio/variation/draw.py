"""Weighted combo draw with the spec section 8 ledger rules (component C7).

Rules enforced here:
* weights come from `pool_weights` (default 1.0) and no option falls under a 5 % floor;
* no full combo repeats on a channel within 30 days;
* no `{hook, structure}` pair repeats within the last `min(10, pairs - 1)` recorded videos.
"""

from __future__ import annotations

import itertools
import random
from datetime import UTC, datetime, timedelta

from ns_common.db import Db
from ns_common.models import Channel, Combo, Pools
from ns_studio.errors import StudioError

FLOOR = 0.05
LEDGER_DAYS = 30
MAX_PAIR_WINDOW = 10
RANDOM_ATTEMPTS = 60

POOLS = (
    "hooks",
    "structures",
    "pov_tones",
    "pacings",
    "caption_styles",
    "visual_treatments",
    "title_formulas",
    "length_buckets",
)
# Combo field filled by each pool.
POOL_FIELD = {
    "hooks": "hook",
    "structures": "structure",
    "pov_tones": "pov_tone",
    "pacings": "pacing",
    "caption_styles": "caption_style",
    "visual_treatments": "visual_treatment",
    "title_formulas": "title_formula",
    "length_buckets": "length_bucket",
}
_FULL_KEY_FIELDS = (
    "hook",
    "structure",
    "pov_tone",
    "pacing",
    "caption_style",
    "visual_treatment",
    "title_formula",
    "length_bucket",
)


def apply_floor(weights: dict[str, float], floor: float = FLOOR) -> dict[str, float]:
    """Normalise `weights` to sum 1 and lift every entry to at least `floor` (renormalising)."""
    if not weights:
        return {}
    n = len(weights)
    floor = min(floor, 1.0 / n)
    total = sum(max(w, 0.0) for w in weights.values())
    if total <= 0:
        return {k: 1.0 / n for k in weights}
    probs = {k: max(w, 0.0) / total for k, w in weights.items()}
    # Pin entries below the floor at the floor, scale the rest into the remaining mass; repeat until stable.
    pinned: set[str] = set()
    for _ in range(n + 1):
        low = {k for k, p in probs.items() if p < floor - 1e-12 and k not in pinned}
        if not low:
            break
        pinned |= low
        free_mass = 1.0 - floor * len(pinned)
        free_total = sum(probs[k] for k in probs if k not in pinned)
        probs = {
            k: (floor if k in pinned else (probs[k] / free_total * free_mass if free_total > 0 else 0.0))
            for k in probs
        }
    return probs


def weights_for(db: Db, channel: str, pool: str, options: list[str]) -> dict[str, float]:
    """Floored selection probabilities for `options` of `pool` (missing rows weigh 1.0)."""
    rows = db.query("select option, weight from pool_weights where channel=? and pool=?", (channel, pool))
    stored = {r["option"]: float(r["weight"]) for r in rows}
    return apply_floor({o: stored.get(o, 1.0) for o in options})


def ledger_window(pools: Pools) -> int:
    """Number of recent videos whose `{hook, structure}` pair may not repeat: `min(10, pairs - 1)`."""
    pairs = len(pools.hooks) * len(pools.structures)
    return max(0, min(MAX_PAIR_WINDOW, pairs - 1))


def recent_pairs(db: Db, channel: str, window: int) -> set[tuple[str, str]]:
    """The `{hook, structure}` pairs of the last `window` recorded videos on `channel`."""
    if window <= 0:
        return set()
    rows = db.query(
        "select hook, structure from combos where channel=? order by created_at desc, rowid desc limit ?",
        (channel, window),
    )
    return {(r["hook"], r["structure"]) for r in rows}


def used_full_combos(
    db: Db, channel: str, now: datetime, days: int = LEDGER_DAYS
) -> set[tuple[str | int | None, ...]]:
    """Full combo tuples recorded on `channel` within the last `days` days."""
    since = (now - timedelta(days=days)).isoformat(timespec="seconds")
    cols = ", ".join(_FULL_KEY_FIELDS)
    rows = db.query(f"select {cols} from combos where channel=? and created_at >= ?", (channel, since))
    return {tuple(r[c] for c in _FULL_KEY_FIELDS) for r in rows}


def _choice(rng: random.Random, probs: dict[str, float]) -> str:
    keys = list(probs)
    return rng.choices(keys, weights=[probs[k] for k in keys], k=1)[0]


def _pool_options(channel: Channel, pool: str) -> list[str]:
    if pool == "length_buckets":
        return [str(b) for b in channel.length_buckets]
    options: list[str] = getattr(channel.pools, pool)
    return list(options)


def _full_key(values: dict[str, str]) -> tuple[str | int | None, ...]:
    out: list[str | int | None] = []
    for f in _FULL_KEY_FIELDS:
        v = values.get(f)
        out.append(int(v) if f == "length_bucket" and v is not None else v)
    return tuple(out)


def draw_combo(
    db: Db,
    channel: Channel,
    *,
    rng: random.Random,
    now: datetime | None = None,
    fixed: dict[str, str] | None = None,
) -> Combo:
    """Draw one combo for `channel`, honouring weights, the pair window and the 30-day ledger.

    `fixed` pins combo fields (for example `structure`, `pov_tone`, `pacing` for a Short derived
    from a master). Raises StudioError NS-E325 when the ledger leaves nothing to draw.
    """
    now = now or datetime.now(UTC)
    fixed = dict(fixed or {})
    slug = channel.slug
    probs = {p: weights_for(db, slug, p, _pool_options(channel, p)) for p in POOLS}
    for pool, fld in POOL_FIELD.items():
        if fld in fixed:
            probs[pool] = {fixed[fld]: 1.0}

    banned_pairs = recent_pairs(db, slug, ledger_window(channel.pools))
    all_pairs = {
        (h, s): ph * ps for h, ph in probs["hooks"].items() for s, ps in probs["structures"].items()
    }
    pair_weight = {p: w for p, w in all_pairs.items() if p not in banned_pairs}
    if not pair_weight and ("hook" in fixed or "structure" in fixed):
        # A Short derived from a master shares its structure, so the pair window cannot apply to it.
        pair_weight = all_pairs
    if not pair_weight:
        raise StudioError("NS-E325", "every hook/structure pair is inside the ledger window")

    used = used_full_combos(db, slug, now)
    others = [p for p in POOLS if p not in ("hooks", "structures")]
    chosen: dict[str, str] | None = None
    for _ in range(RANDOM_ATTEMPTS):
        h, s = rng.choices(list(pair_weight), weights=list(pair_weight.values()), k=1)[0]
        values = {"hook": h, "structure": s}
        for p in others:
            values[POOL_FIELD[p]] = _choice(rng, probs[p])
        if _full_key(values) not in used:
            chosen = values
            break
    if chosen is None:
        chosen = _enumerate_unused(probs, pair_weight, others, used)
    if chosen is None:
        raise StudioError("NS-E325", "every combination was used in the last 30 days")

    combo_id = _fresh_id(db, rng)
    return Combo(
        combo_id=combo_id,
        hook=chosen["hook"],
        structure=chosen["structure"],
        pov_tone=chosen["pov_tone"],
        pacing=chosen["pacing"],
        caption_style=chosen["caption_style"],
        visual_treatment=chosen["visual_treatment"],
        title_formula=chosen["title_formula"],
        length_bucket=int(chosen["length_bucket"]),
    )


def _enumerate_unused(
    probs: dict[str, dict[str, float]],
    pair_weight: dict[tuple[str, str], float],
    others: list[str],
    used: set[tuple[str | int | None, ...]],
) -> dict[str, str] | None:
    """Exhaustive fallback: the heaviest unused combination, or None when every one is used."""
    best: tuple[float, dict[str, str]] | None = None
    pair_items = sorted(pair_weight.items(), key=lambda kv: -kv[1])
    for (h, s), w_pair in pair_items:
        for combo in itertools.product(*(list(probs[p].items()) for p in others)):
            values = {"hook": h, "structure": s}
            weight = w_pair
            for p, (opt, w) in zip(others, combo, strict=True):
                values[POOL_FIELD[p]] = opt
                weight *= w
            if _full_key(values) in used:
                continue
            if best is None or weight > best[0]:
                best = (weight, values)
    return best[1] if best else None


def _fresh_id(db: Db, rng: random.Random) -> str:
    for _ in range(20):
        cid = f"c-{rng.getrandbits(32):08x}"
        if db.one("select 1 from combos where combo_id=?", (cid,)) is None:
            return cid
    raise StudioError("NS-E325", "could not mint a unique combo id")


def record_combo(
    db: Db, channel: str, job_id: str, item: str, combo: Combo, now: datetime | None = None
) -> None:
    """Write `combo` to the ledger (`combos` table) for `job_id` and `item` (`long` or `s<k>`)."""
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    db.execute(
        "insert or replace into combos(combo_id, channel, job_id, item, hook, structure, pov_tone, pacing, "
        "caption_style, visual_treatment, title_formula, length_bucket, created_at) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            combo.combo_id,
            channel,
            job_id,
            item,
            combo.hook,
            combo.structure,
            combo.pov_tone,
            combo.pacing,
            combo.caption_style,
            combo.visual_treatment,
            combo.title_formula,
            combo.length_bucket,
            stamp,
        ),
    )
