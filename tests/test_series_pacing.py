"""Re-review row 4: a series part keeps the pacing its script was written at (it was a fresh random draw)."""

import json
import random
from datetime import UTC, datetime
from pathlib import Path

from ns_common.models import Pools
from ns_studio.media.voice import job_speed
from ns_studio.series import workshop as W
from ns_studio.variation.draw import draw_combo
from ns_studio.variation.stages import ShortCombosStage, job_series_fixed, series_fixed
from tests.helpers import make_channel, make_ctx
from tests.test_series_workshop import Recorder, make_workshop, ready_to_produce

NOW = datetime(2026, 11, 2, 2, 0, tzinfo=UTC)


def test_produce_stores_the_pacing_the_workshop_wrote_the_script_at(tmp_home: Path):
    rec = Recorder()
    ws, slug = ready_to_produce(tmp_home, rec)
    ws.produce(slug, "all")
    assert {r.pacing for r in rec.requests} == {"standard"}  # pools.pacings[0] of the default channel
    for req in rec.requests:
        ctx = json.loads((ws.paths.job_dir(req.job_id) / "series_context.json").read_text(encoding="utf-8"))
        assert ctx["pacing"] == "standard"
        assert job_series_fixed(ws.paths.job_dir(req.job_id))["pacing"] == "standard"


def test_prepare_job_writes_the_pacing_of_the_request(tmp_home: Path):
    ws = make_workshop(tmp_home)
    req = W.ProduceRequest(
        job_id="j1", channel="after-dark", fmt="master_first", plan="master_from_script", story_slug="s",
        part_n=1, parts_total=2, series_title="S", series_slug="s", title="S - Part 01", short_titles=[],
        script="s.md", shorts=[], concept="c.md", design={}, end_card="", publish_not_before_days=0,
        pacing="measured",
    )
    ws._prepare_job(req)
    saved = json.loads((ws.paths.job_dir("j1") / "series_context.json").read_text(encoding="utf-8"))
    assert saved["pacing"] == "measured"
    assert series_fixed(saved) == {"pacing": "measured"}


def test_every_part_draws_the_pinned_pacing_whatever_the_rng(tmp_home: Path):
    ch = make_channel(format="master_first", pools=Pools(pacings=["standard", "measured"]))
    ctx = make_ctx(tmp_home, fmt="master_first", channel=ch)
    ctx.write_json("series_context.json", {"part_n": 1, "pacing": "measured", "design": {}})
    fixed = job_series_fixed(ctx.job_dir)
    drawn = {draw_combo(ctx.db, ch, rng=random.Random(i), now=NOW, fixed=fixed).pacing for i in range(40)}
    assert drawn == {"measured"}  # before: both, so half the parts were spoken at 0.903x
    unpinned = {draw_combo(ctx.db, ch, rng=random.Random(i), now=NOW).pacing for i in range(40)}
    assert unpinned == {"standard", "measured"}


def test_the_series_shorts_share_the_pinned_pacing(tmp_home: Path):
    ch = make_channel(format="master_first", pools=Pools(pacings=["standard", "measured"]))
    ctx = make_ctx(tmp_home, fmt="master_first", channel=ch)
    ctx.write_json("series_context.json", {"part_n": 1, "pacing": "measured", "design": {}})
    ctx.write_json("shorts_plan.json", [
        {"k": 1, "length_bucket": 50, "combo_id": ctx.combo.combo_id},
        {"k": 2, "length_bucket": 35, "combo_id": ctx.combo.combo_id},
    ])
    ShortCombosStage().run(ctx)
    assert {c["pacing"] for c in ctx.read_json("short_combos.json").values()} == {"measured"}


def test_a_pinned_pacing_gives_every_part_the_same_tts_speed(tmp_home: Path):
    ch = make_channel(format="master_first", pools=Pools(pacings=["standard", "measured"]))
    speeds = set()
    for i in range(6):
        combo = draw_combo(
            make_ctx(tmp_home, fmt="master_first", channel=ch).db, ch, rng=random.Random(i), now=NOW,
            fixed={"pacing": "measured"},
        )
        speeds.add(job_speed(make_ctx(tmp_home, fmt="master_first", channel=ch, combo=combo)))
    assert len(speeds) == 1
