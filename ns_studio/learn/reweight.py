"""Pool reweighting from imported analytics (spec section 18.9 step 3, component C7).

Score of a Short: viewed % x average % viewed (both as fractions). Score of a master: average view
duration / runtime. Each pool option's weight moves towards its relative score by exponential
smoothing and never falls under the 5 % floor.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

from ns_common.db import Db
from ns_common.models import Channel
from ns_common.paths import Paths
from ns_studio.variation.draw import FLOOR, POOL_FIELD, POOLS, apply_floor

ALPHA = 0.3
SNAPSHOT_FILE = "learn_last.json"

_LATEST_SQL = (
    "select o.output_id, o.kind, o.duration_s, a.viewed_pct, a.avg_pct_viewed, a.avg_view_duration_s, "
    "c.hook, c.structure, c.pov_tone, c.pacing, c.caption_style, c.visual_treatment, c.title_formula, "
    "c.length_bucket "
    "from outputs o join combos c on c.combo_id = o.combo_id "
    "join analytics a on a.output_id = o.output_id "
    "and a.date = (select max(date) from analytics where output_id = o.output_id) "
    "where o.channel = ?"
)


def output_score(
    kind: str,
    duration_s: float | None,
    viewed_pct: float | None,
    avg_pct_viewed: float | None,
    avg_view_duration_s: float | None,
) -> float | None:
    """Performance score of one output, or None when its metrics are missing."""
    if kind == "long":
        if avg_view_duration_s is None or not duration_s or duration_s <= 0:
            return None
        return avg_view_duration_s / duration_s
    if viewed_pct is None or avg_pct_viewed is None:
        return None
    return (viewed_pct / 100.0) * (avg_pct_viewed / 100.0)


def scored_outputs(db: Db, channel: str) -> list[tuple[str, float, dict[str, str]]]:
    """`(output_id, score, combo fields)` for every output of `channel` that has analytics."""
    out: list[tuple[str, float, dict[str, str]]] = []
    for r in db.query(_LATEST_SQL, (channel,)):
        score = output_score(
            r["kind"], r["duration_s"], r["viewed_pct"], r["avg_pct_viewed"], r["avg_view_duration_s"]
        )
        if score is None:
            continue
        fields = {f: str(r[f]) for f in POOL_FIELD.values() if r[f] is not None}
        out.append((r["output_id"], score, fields))
    return out


def option_means(scored: list[tuple[str, float, dict[str, str]]]) -> dict[str, dict[str, float]]:
    """Mean score per option, per pool name."""
    acc: dict[str, dict[str, list[float]]] = {p: {} for p in POOLS}
    for _oid, score, fields in scored:
        for pool, fld in POOL_FIELD.items():
            if fld in fields:
                acc[pool].setdefault(fields[fld], []).append(score)
    return {p: {o: mean(v) for o, v in opts.items()} for p, opts in acc.items() if opts}


def _options(channel: Channel, pool: str) -> list[str]:
    if pool == "length_buckets":
        return [str(b) for b in channel.length_buckets]
    return list(getattr(channel.pools, pool))


def reweight(
    db: Db, channel: Channel, *, now: datetime | None = None, alpha: float = ALPHA
) -> dict[str, dict[str, tuple[float, float]]]:
    """Update `pool_weights` for `channel`; returns `{pool: {option: (old, new)}}` for what changed."""
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    means = option_means(scored_outputs(db, channel.slug))
    changes: dict[str, dict[str, tuple[float, float]]] = {}
    for pool, seen in means.items():
        options = _options(channel, pool)
        if not options:
            continue
        rows = db.query(
            "select option, weight from pool_weights where channel=? and pool=?", (channel.slug, pool)
        )
        stored = {r["option"]: float(r["weight"]) for r in rows}
        old_raw = {o: stored.get(o, 1.0) for o in options}
        avg_old = mean(old_raw.values())
        old_rel = {o: w / avg_old for o, w in old_raw.items()}
        overall = mean(seen.values())
        if overall <= 0:
            continue
        new_rel = {
            o: (1 - alpha) * old_rel[o] + alpha * (seen[o] / overall) if o in seen else old_rel[o]
            for o in options
        }
        probs = apply_floor(new_rel, FLOOR)
        old_probs = apply_floor(old_raw, FLOOR)
        for o in options:
            db.execute(
                "insert or replace into pool_weights(channel, pool, option, weight, updated_at) "
                "values (?,?,?,?,?)",
                (channel.slug, pool, o, probs[o], stamp),
            )
        changes[pool] = {o: (old_probs[o], probs[o]) for o in options if abs(old_probs[o] - probs[o]) > 1e-9}
    return {p: c for p, c in changes.items() if c}


def save_snapshot(
    paths: Paths,
    channel: str,
    changes: dict[str, dict[str, tuple[float, float]]],
    now: datetime | None = None,
) -> Path:
    """Remember what the last import changed (shown by `studio status`)."""
    f = paths.data / SNAPSHOT_FILE
    data = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    data[channel] = {
        "at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "changes": {
            p: {o: [round(a, 4), round(b, 4)] for o, (a, b) in c.items()} for p, c in changes.items()
        },
    }
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return f


def load_snapshot(paths: Paths, channel: str) -> dict:
    """The snapshot saved by the last import for `channel` (empty dict when none)."""
    f = paths.data / SNAPSHOT_FILE
    if not f.exists():
        return {}
    return dict(json.loads(f.read_text(encoding="utf-8")).get(channel, {}))
