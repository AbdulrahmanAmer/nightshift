import random
from datetime import UTC, datetime, timedelta
from itertools import product
from pathlib import Path

import pytest

from ns_common.db import Db
from ns_common.models import Combo, Pools
from ns_common.paths import Paths
from ns_studio.errors import StudioError
from ns_studio.variation import draw
from tests.helpers import make_channel

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


def _db(tmp_home: Path) -> Db:
    paths = Paths.from_env()
    paths.ensure()
    db = Db(paths.db_path)
    db.migrate()
    return db


def _tiny(**kw):
    pools = Pools(
        hooks=["h1", "h2", "h3"],
        structures=["s1", "s2", "s3"],
        pov_tones=["p1"],
        pacings=["standard"],
        caption_styles=["c1"],
        visual_treatments=["v1"],
        title_formulas=["t1"],
    )
    return make_channel(pools=pools, length_buckets=[50], **kw)


def _combo(h: str, s: str, cid: str) -> Combo:
    return Combo(
        combo_id=cid, hook=h, structure=s, pov_tone="p1", pacing="standard", caption_style="c1",
        visual_treatment="v1", title_formula="t1", length_bucket=50,
    )


def test_apply_floor_lifts_small_weights_and_normalises():
    out = draw.apply_floor({"a": 0.99, "b": 0.01})
    assert out["b"] == pytest.approx(0.05)
    assert sum(out.values()) == pytest.approx(1.0)
    assert out["a"] == pytest.approx(0.95)


def test_apply_floor_many_low_entries_and_degenerate_input():
    out = draw.apply_floor({"a": 100.0, "b": 0.0, "c": 0.0, "d": 0.0})
    assert min(out.values()) >= 0.05 - 1e-9
    assert sum(out.values()) == pytest.approx(1.0)
    assert draw.apply_floor({}) == {}
    assert draw.apply_floor({"a": 0.0, "b": 0.0}) == {"a": 0.5, "b": 0.5}


def test_weights_for_uses_stored_rows(tmp_home: Path):
    db = _db(tmp_home)
    db.execute(
        "insert into pool_weights(channel, pool, option, weight, updated_at) values (?,?,?,?,?)",
        ("after-dark", "hooks", "cold_open", 3.0, "2026-11-01T00:00:00"),
    )
    w = draw.weights_for(db, "after-dark", "hooks", ["cold_open", "question"])
    assert w["cold_open"] == pytest.approx(0.75)
    assert w["question"] == pytest.approx(0.25)


def test_floor_keeps_rare_options_alive(tmp_home: Path):
    db = _db(tmp_home)
    ch = make_channel()
    for opt, wt in (("cold_open", 1000.0), ("question", 0.001), ("confession", 0.001), ("warning", 0.001)):
        db.execute(
            "insert into pool_weights(channel, pool, option, weight, updated_at) values (?,?,?,?,?)",
            (ch.slug, "hooks", opt, wt, "2026-11-01T00:00:00"),
        )
    rng = random.Random(7)
    n = 4000
    counts: dict[str, int] = {}
    for _ in range(n):
        c = draw.draw_combo(db, ch, rng=rng, now=NOW)
        counts[c.hook] = counts.get(c.hook, 0) + 1
    # Pair weights multiply hook and structure weights, so the hook marginal stays at its floored value.
    assert 0.03 <= counts["question"] / n <= 0.08
    assert counts["cold_open"] / n > 0.75


def test_pair_window_never_repeats_inside_window(tmp_home: Path):
    db = _db(tmp_home)
    ch = make_channel()
    window = draw.ledger_window(ch.pools)
    assert window == 10
    rng = random.Random(3)
    pairs: list[tuple[str, str]] = []
    for i in range(40):
        when = NOW + timedelta(minutes=i)
        c = draw.draw_combo(db, ch, rng=rng, now=when)
        assert (c.hook, c.structure) not in pairs[-window:]
        draw.record_combo(db, ch.slug, f"job-{i}", "s1", c, when)
        pairs.append((c.hook, c.structure))


def test_three_by_three_cycles_through_every_pair(tmp_home: Path):
    db = _db(tmp_home)
    ch = _tiny()
    assert draw.ledger_window(ch.pools) == 8
    rng = random.Random(11)
    seen: list[tuple[str, str]] = []
    for i in range(9):
        when = NOW + timedelta(minutes=i)
        c = draw.draw_combo(db, ch, rng=rng, now=when)
        draw.record_combo(db, ch.slug, f"j{i}", "s1", c, when)
        seen.append((c.hook, c.structure))
    assert len(set(seen)) == 9


def test_full_combo_not_repeated_within_30_days(tmp_home: Path):
    db = _db(tmp_home)
    ch = _tiny()
    for i, (h, s) in enumerate(product(ch.pools.hooks, ch.pools.structures)):
        draw.record_combo(db, ch.slug, f"j{i}", "s1", _combo(h, s, f"c-old{i}"), NOW - timedelta(days=29))
    with pytest.raises(StudioError) as ei:
        draw.draw_combo(db, ch, rng=random.Random(1), now=NOW)
    assert ei.value.code == "NS-E325"


def test_combo_older_than_30_days_may_repeat(tmp_home: Path):
    db = _db(tmp_home)
    ch = _tiny()
    for i, (h, s) in enumerate(product(ch.pools.hooks, ch.pools.structures)):
        draw.record_combo(db, ch.slug, f"j{i}", "s1", _combo(h, s, f"c-old{i}"), NOW - timedelta(days=31))
    c = draw.draw_combo(db, ch, rng=random.Random(1), now=NOW)
    assert c.hook in ch.pools.hooks and c.structure in ch.pools.structures


def test_fixed_structure_is_kept_even_when_pairs_are_used_up(tmp_home: Path):
    db = _db(tmp_home)
    ch = make_channel()
    rng = random.Random(5)
    hooks_seen = set()
    for i in range(14):
        when = NOW + timedelta(minutes=i)
        c = draw.draw_combo(db, ch, rng=rng, now=when, fixed={"structure": "twist", "pacing": "measured"})
        assert c.structure == "twist" and c.pacing == "measured"
        hooks_seen.add(c.hook)
        draw.record_combo(db, ch.slug, f"j{i}", f"s{i}", c, when)
    assert len(hooks_seen) > 1


def test_record_combo_roundtrip_and_unique_ids(tmp_home: Path):
    db = _db(tmp_home)
    ch = make_channel()
    rng = random.Random(9)
    ids = set()
    for i in range(20):
        c = draw.draw_combo(db, ch, rng=rng, now=NOW + timedelta(minutes=i))
        assert c.combo_id.startswith("c-") and c.length_bucket in ch.length_buckets
        draw.record_combo(db, ch.slug, f"j{i}", "s1", c, NOW + timedelta(minutes=i))
        ids.add(c.combo_id)
    assert len(ids) == 20
    row = db.one("select * from combos where combo_id=?", (c.combo_id,))
    assert row is not None and row["hook"] == c.hook and row["item"] == "s1"


def test_ledger_window_formula():
    assert draw.ledger_window(Pools(hooks=["a", "b", "c"], structures=["x", "y", "z"])) == 8
    assert draw.ledger_window(Pools()) == 10
