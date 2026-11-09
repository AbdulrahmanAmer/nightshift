"""P03: the final-gate clip reuse check on real picks, keyword lemmas, dry-pool widening, watermark filter.

Boxes F-H62 (gate reads the picks), F-H64 (lemmatized keywords), F-H66 (widen before relaxing, relaxation is
only a warning) and F-H67 (watermark filter, +1 s margin kept per slot). Spec 7.4 and 18.8.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ns_common.errors import NsError
from ns_common.paths import Paths
from ns_studio.media.clips import (
    ClipCache,
    ClipsStage,
    Searcher,
    assign_clips,
    dedupe_keywords,
    expansion_keywords,
    has_watermark_hint,
    make_lemmatizer,
    normalize_keyword,
    pick_for_slots,
    select_for_beat,
)
from ns_studio.media.shotplan import Shot, ShotSlot
from ns_studio.policy import text_checks
from ns_studio.policy.final_checks import ClipReuse, picked_clip_uses
from ns_studio.policy.gate import PolicyFinalStage
from tests.helpers import make_ctx
from tests.test_clips import StubSource, _prepare_job, _slots, cand

NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _no_spacy(monkeypatch):
    """Keyword tests expect the trailing-s rule unless they hand in a lemmatizer."""
    monkeypatch.setitem(text_checks._NLP_CACHE, "nlp", None)


# ---------------------------------------------------------------- helpers
def _shot(clip_id: str, *, relaxed: bool = False, source: str = "stub", start: float = 0.0) -> dict:
    shot = Shot(0, start, start + 2.0, source, clip_id, "x.mp4", 0.0, 1080, 1920, "u", "l", "kw", relaxed)
    return shot.to_json()


def _write_shots(ctx, name: str, items: dict[str, list[dict]]) -> None:
    ctx.write_json(
        name, {item: {"total_s": 10.0, "treatment": "t", "shots": shots} for item, shots in items.items()}
    )


def _earlier(
    ctx, clip_id: str, channel: str, days_ago: float, source: str = "stub", now: datetime = NOW
) -> None:
    """A clip_usage row of another job, as the outbox stage of an earlier video wrote it."""
    ctx.db.execute(
        "insert or ignore into channels(slug, display_name, created_at) values (?,?,?)",
        (channel, channel, "2026-01-01T00:00:00"),
    )
    ctx.db.execute(
        "insert or ignore into clips(source, clip_id, url, license, fetched_at) values (?,?,?,?,?)",
        (source, clip_id, "u", "l", now.isoformat()),
    )
    ctx.db.execute(
        "insert into clip_usage(source, clip_id, channel, job_id, item, used_at) values (?,?,?,?,?,?)",
        (
            source,
            clip_id,
            channel,
            f"old-{channel}-{clip_id}",
            "s1",
            (now - timedelta(days=days_ago)).isoformat(),
        ),
    )


def _gate(ctx, now: datetime = NOW) -> None:
    """Run the final gate stage with only the clip reuse check (the others need rendered files)."""
    PolicyFinalStage(checks=[ClipReuse(clock=lambda: now)]).run(ctx)


def _result(ctx) -> dict:
    return ctx.read_json("policy_final.json")["checks"]["clip_reuse"]


# ---------------------------------------------------------------- F-H62: the gate reads the picks
def test_gate_reads_picks_from_shot_files_when_no_clip_usage_rows_exist_yet(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _write_shots(ctx, "clips/shots.json", {"s1": [_shot("a"), _shot("b", start=2.0)]})
    assert ctx.db.query("select * from clip_usage") == []  # outbox has not run
    assert sorted(picked_clip_uses(ctx) or []) == [("stub", "a", "s1", False), ("stub", "b", "s1", False)]
    _gate(ctx)
    assert _result(ctx)["passed"] and "2 clip uses" in _result(ctx)["detail"]


def test_a_clip_used_on_this_channel_within_14_days_fails_e613(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _write_shots(ctx, "clips/shots.json", {"s1": [_shot("a"), _shot("b", start=2.0)]})
    _earlier(ctx, "b", ctx.channel.slug, 3)
    with pytest.raises(NsError) as ei:
        _gate(ctx)
    assert ei.value.code == "NS-E613" and "stub/b" in str(ei.value) and "this channel" in str(ei.value)
    assert not _result(ctx)["passed"]
    # past the 14 days the same pick is fine
    other = make_ctx(tmp_home)
    _write_shots(other, "clips/shots.json", {"s1": [_shot("c")]})
    _earlier(other, "c", other.channel.slug, 15)
    _gate(other)
    assert _result(other)["passed"]


def test_a_clip_used_on_another_channel_within_30_days_fails_e613(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _write_shots(ctx, "clips/shots.json", {"s1": [_shot("a")]})
    _earlier(ctx, "a", "other-ch", 20)
    with pytest.raises(NsError) as ei:
        _gate(ctx)
    assert ei.value.code == "NS-E613" and "other-ch" in str(ei.value)
    later = make_ctx(tmp_home)
    _write_shots(later, "clips/shots.json", {"s1": [_shot("z")]})
    _earlier(later, "z", "other-ch", 31)
    _gate(later)
    assert _result(later)["passed"]


def test_a_clip_twice_in_one_video_fails_e613(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _write_shots(ctx, "clips/shots.json", {"long": [_shot("a"), _shot("a", start=2.0)]})
    with pytest.raises(NsError) as ei:
        _gate(ctx)
    assert ei.value.code == "NS-E613" and "twice in long" in str(ei.value)


def test_a_short_may_use_the_clips_of_its_own_master(tmp_home: Path):
    ctx = make_ctx(tmp_home, fmt="master_first")
    _write_shots(ctx, "clips/shots.json", {"long": [_shot("a"), _shot("b", start=2.0)]})
    _write_shots(
        ctx, "clips/shorts_shots.json", {"s1": [_shot("a"), _shot("b", start=2.0)], "s2": [_shot("a")]}
    )
    uses = picked_clip_uses(ctx) or []
    assert {i for _s, _c, i, _r in uses} == {"long", "s1", "s2"}
    _gate(ctx)
    assert _result(ctx)["passed"] and "NS-W405" not in _result(ctx)["detail"]


def test_a_short_clip_inside_the_window_still_fails_in_master_first_jobs(tmp_home: Path):
    ctx = make_ctx(tmp_home, fmt="master_first")
    _write_shots(ctx, "clips/shots.json", {"long": [_shot("a")]})
    _write_shots(ctx, "clips/shorts_shots.json", {"s1": [_shot("q")]})
    _earlier(ctx, "q", ctx.channel.slug, 1)
    with pytest.raises(NsError) as ei:
        _gate(ctx)
    assert ei.value.code == "NS-E613" and "stub/q" in str(ei.value)


def test_clip_usage_rows_still_count_for_a_job_without_shot_files(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    assert picked_clip_uses(ctx) is None
    _earlier(ctx, "a", ctx.channel.slug, 2)
    ctx.db.execute(
        "insert into clip_usage(source, clip_id, channel, job_id, item, used_at) values (?,?,?,?,?,?)",
        ("stub", "a", ctx.channel.slug, ctx.job_id, "s1", NOW.isoformat()),
    )
    with pytest.raises(NsError) as ei:
        _gate(ctx)
    assert ei.value.code == "NS-E613"


# ---------------------------------------------------------------- F-H66: only the relaxation is a warning
def test_a_relaxed_pick_is_a_warning_not_a_failure(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _write_shots(ctx, "clips/shots.json", {"s1": [_shot("a", relaxed=True), _shot("b", start=2.0)]})
    _earlier(ctx, "a", ctx.channel.slug, 3)
    _gate(ctx)  # does not raise
    res = _result(ctx)
    assert res["passed"] and "NS-W405" in res["detail"] and "stub/a" in res["detail"]
    # the same clip picked without the relaxation flag is a breach
    plain = make_ctx(tmp_home)
    _write_shots(plain, "clips/shots.json", {"s1": [_shot("a")]})
    with pytest.raises(NsError) as ei:
        _gate(plain)
    assert ei.value.code == "NS-E613"


def test_a_repeat_inside_a_video_is_only_a_warning_when_it_was_the_relaxation(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _write_shots(ctx, "clips/shots.json", {"long": [_shot("a"), _shot("a", relaxed=True, start=2.0)]})
    _gate(ctx)
    assert _result(ctx)["passed"] and "twice in long" in _result(ctx)["detail"]


def test_a_relaxed_clip_that_also_broke_a_window_unrelaxed_still_fails(tmp_home: Path):
    ctx = make_ctx(tmp_home, fmt="master_first")
    _write_shots(ctx, "clips/shots.json", {"long": [_shot("a")]})
    _write_shots(ctx, "clips/shorts_shots.json", {"s1": [_shot("a", relaxed=True)]})
    _earlier(ctx, "a", ctx.channel.slug, 3)
    with pytest.raises(NsError) as ei:
        _gate(ctx)
    assert ei.value.code == "NS-E613"


def test_pipeline_clips_stage_then_final_gate(tmp_home: Path):
    """The clips stage writes the picks, the gate reads them: fresh footage passes, a window breach fails."""
    ctx = make_ctx(tmp_home)
    _prepare_job(ctx)
    ctx.services.clip_sources = [
        StubSource(
            {
                (kw, "portrait"): [cand(f"{kw[:2]}{i}", dur=8.0) for i in range(6)]
                for kw in ("dark hallway", "flickering light", "rain window")
            }
        )
    ]
    ClipsStage().run(ctx)
    picks = ctx.read_json("clips/shots.json")["s1"]["shots"]
    assert len(picks) == 5 and not any(s["relaxed"] for s in picks)
    now = datetime.now(UTC)
    _gate(ctx, now)
    assert _result(ctx)["passed"]
    # a clip another job used 3 days ago, recorded after the picks were made, is caught by the gate
    _earlier(ctx, picks[0]["clip_id"], ctx.channel.slug, 3, now=now)
    with pytest.raises(NsError) as ei:
        _gate(ctx, now)
    assert ei.value.code == "NS-E613"


def test_pipeline_a_dry_pool_relaxes_with_a_warning_the_gate_accepts(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _prepare_job(ctx)
    pool = {
        (kw, "portrait"): [cand(f"{kw[:2]}{i}", dur=8.0) for i in range(2)]
        for kw in ("dark hallway", "flickering light", "rain window")
    }
    ctx.services.clip_sources = [StubSource(pool)]
    now = datetime.now(UTC)  # the clips stage reads the real clock for its 14 and 30 day windows
    for clips in pool.values():  # every clip of the pool was used 3 days ago on this channel
        for c in clips:
            _earlier(ctx, c.id, ctx.channel.slug, 3, now=now)
    ClipsStage().run(ctx)
    picks = ctx.read_json("clips/shots.json")["s1"]["shots"]
    assert any(s["relaxed"] for s in picks)
    _gate(ctx, now)  # a relaxed pick is a warning
    assert "NS-W405" in _result(ctx)["detail"]


# ---------------------------------------------------------------- F-H66: widen before relaxing
def test_widening_finds_fresh_clips_so_nothing_is_relaxed():
    src = StubSource(
        {
            ("haunted hallway", "portrait"): [cand("h1"), cand("h2")],
            ("hallway", "portrait"): [cand("w1")],
            ("fog", "portrait"): [cand("f1")],
        }
    )
    blocked = {("stub", "h1"), ("stub", "h2")}
    relaxed: set[tuple[str, str]] = set()
    picks = select_for_beat(
        Searcher([src]),
        ["haunted hallway"],
        "eerie",
        _slots(2, 2),
        used=set(),
        blocked=blocked,
        relaxed=relaxed,
    )
    assert sorted(c.id for c, _k in picks) == ["f1", "w1"]
    assert relaxed == set()
    assert ("hallway", "portrait") in src.searches  # the keyword part
    assert ("fog", "portrait") in src.searches  # a mood that reads like "eerie"


def test_channel_seed_keywords_and_other_beats_are_searched_when_the_beat_is_dry():
    src = StubSource(
        {
            ("haunted hallway", "portrait"): [cand("h1")],
            ("lighthouse", "portrait"): [cand("l1")],
            ("seed story", "portrait"): [cand("s1")],
        }
    )
    picks = select_for_beat(
        Searcher([src]),
        ["haunted hallway"],
        "calm",
        _slots(2, 2),
        used=set(),
        blocked={("stub", "h1")},
        extra_keywords=["lighthouse", "seed story"],
    )
    assert [c.id for c, _k in picks] == ["l1", "s1"]


def test_relaxation_is_last_and_reports_the_clips_it_reused():
    src = StubSource({("hallway", "portrait"): [cand("p1"), cand("p2")]})
    blocked = {("stub", "p1"), ("stub", "p2")}
    relaxed: set[tuple[str, str]] = set()
    picks = select_for_beat(
        Searcher([src]), ["hallway"], "calm", _slots(2, 2), used=set(), blocked=blocked, relaxed=relaxed
    )
    assert [c.id for c, _k in picks] == ["p1", "p2"]
    assert relaxed == {("stub", "p1"), ("stub", "p2")}
    with pytest.raises(NsError) as ei:  # an empty pool is still E402
        select_for_beat(Searcher([StubSource()]), ["nothing"], "calm", _slots(2), used=set(), blocked=set())
    assert ei.value.code == "NS-E402"


def test_assign_clips_marks_relaxed_shots(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    src = StubSource({("hallway", "portrait"): [cand("p1"), cand("p2"), cand("p3")]})
    cache = ClipCache(ctx.db, ctx.paths, [src])
    slots = [ShotSlot(0, 0.0, 2.0), ShotSlot(0, 2.0, 4.0)]
    beats = [{"keywords": ["hallway"], "mood": "calm"}]
    fresh = assign_clips(
        beats=beats, slots=slots, searcher=Searcher([src]), cache=cache, blocked=set(), seed=1
    )
    assert [s.relaxed for s in fresh] == [False, False]
    blocked = {("stub", "p1"), ("stub", "p2"), ("stub", "p3")}
    again = assign_clips(
        beats=beats, slots=slots, searcher=Searcher([src]), cache=cache, blocked=blocked, seed=1
    )
    assert [s.relaxed for s in again] == [True, True]
    assert Shot.from_json(again[0].to_json()) == again[0]
    old = again[0].to_json()
    del old["relaxed"]  # shot files written before this field existed still load
    assert Shot.from_json(old).relaxed is False


def test_expansion_keywords_leave_out_what_the_beat_already_searched():
    got = expansion_keywords(["haunted hallway", "the old door"], "horror", ["door", "lighthouse"])
    assert got == ["haunted", "hallway", "door", "lighthouse", "dark", "creepy", "night"]


# ---------------------------------------------------------------- F-H64: lemmatized keyword dedupe
LEMMAS = {"mice": "mouse", "running": "run", "ran": "run", "windows": "window", "glass": "glass"}


def _lemma(phrase: str) -> list[str]:
    return [LEMMAS.get(w, w) for w in phrase.split()]


def test_keywords_are_deduped_by_lemma():
    assert normalize_keyword("Mice", _lemma) == "mouse"
    assert dedupe_keywords(["mice", "mouse", "Mice!"], _lemma) == ["mouse"]
    assert dedupe_keywords(["running man", "run man", "ran man"], _lemma) == ["run man"]
    assert dedupe_keywords(["dark windows", "dark window"], _lemma) == ["dark window"]


def test_without_a_lemmatizer_the_trailing_s_rule_is_the_fallback():
    assert normalize_keyword("windows") == "window" and normalize_keyword("glass") == "glass"
    assert dedupe_keywords(["empty rooms", "empty room"]) == ["empty room"]
    # a lemmatizer that gives nothing usable (or breaks) falls back to the same rule
    assert normalize_keyword("empty rooms", lambda _p: []) == "empty room"

    def boom(_p: str) -> list[str]:
        raise RuntimeError("pipeline broke")

    assert normalize_keyword("empty rooms", boom) == "empty room"


def test_the_searcher_searches_once_for_keywords_that_share_a_lemma():
    src = StubSource({("mouse", "portrait"): [cand("m1"), cand("m2")]})
    picks = select_for_beat(
        Searcher([src], lemma=_lemma), ["mice", "mouse"], "calm", _slots(2, 2), used=set(), blocked=set()
    )
    assert [c.id for c, _k in picks] == ["m1", "m2"]
    assert src.searches == [("mouse", "portrait")]


def test_make_lemmatizer_uses_the_spacy_pipeline_the_policy_ner_loads(tmp_home: Path, monkeypatch):
    table = {"running": "run", "mice": "mouse", "dogs": "dog"}

    def nlp(text: str) -> list[SimpleNamespace]:
        return [SimpleNamespace(lemma_=table.get(w, w)) for w in text.split()]

    monkeypatch.setitem(text_checks._NLP_CACHE, "nlp", nlp)
    lemma = make_lemmatizer(Paths.from_env())
    assert lemma is not None
    assert normalize_keyword("Running Mice", lemma) == "run mouse"
    assert dedupe_keywords(["running dogs", "run dog"], lemma) == ["run dog"]
    monkeypatch.setitem(text_checks._NLP_CACHE, "nlp", None)
    assert make_lemmatizer(Paths.from_env()) is None  # no spaCy: the caller uses the trailing-s rule


def test_real_spacy_lemmas_when_the_model_is_installed(tmp_home: Path, monkeypatch):
    pytest.importorskip("spacy")
    monkeypatch.delitem(text_checks._NLP_CACHE, "nlp", raising=False)
    lemma = make_lemmatizer(Paths.from_env())
    if lemma is None:
        pytest.skip("spaCy model en_core_web_md is not installed")
    assert normalize_keyword("mice", lemma) == "mouse"
    assert dedupe_keywords(["running", "run"], lemma) == ["run"]


# ---------------------------------------------------------------- F-H67: watermark filter and the margin
def test_watermark_logo_and_text_overlay_clips_are_dropped():
    assert has_watermark_hint(cand("a", tags=("rain", "watermark")))
    assert has_watermark_hint(cand("b", title="City logo animation"))
    assert has_watermark_hint(cand("c", tags=("night", "text overlay")))
    assert has_watermark_hint(cand("d", title="Watermarked sample, street"))
    assert not has_watermark_hint(cand("e", tags=("rain", "window"), title="Rain on a window"))
    pool = [
        cand("wm", tags=("watermark",)),
        cand("lg", title="logo reveal"),
        cand("ov", tags=("text overlay",)),
        cand("ok"),
    ]
    got = pick_for_slots(pool, _slots(2), used=set(), blocked=set(), dark=False)
    assert [c.id for c in got or []] == ["ok"]
    assert pick_for_slots(pool[:3], _slots(2), used=set(), blocked=set(), dark=False) is None


def test_a_watermarked_clip_is_never_used_even_by_the_reuse_relaxation():
    src = StubSource({("hallway", "portrait"): [cand("wm", tags=("watermark",)), cand("p1")]})
    picks = select_for_beat(
        Searcher([src]), ["hallway"], "calm", _slots(2), used=set(), blocked={("stub", "p1")}
    )
    assert [c.id for c, _k in picks] == ["p1"]


def test_the_one_second_margin_is_kept_for_every_slot_that_can_have_it():
    pool = [cand("tight", dur=2.5), cand("roomy", dur=4.0)]
    got = pick_for_slots(pool, _slots(2.0, 2.0), used=set(), blocked=set(), dark=False)
    # the first slot keeps its margin (roomy); only the slot nothing else fits drops it
    assert [c.id for c in got or []] == ["roomy", "tight"]


def test_shot_files_round_trip_the_relaxed_flag(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _write_shots(ctx, "clips/shots.json", {"s1": [_shot("a", relaxed=True)]})
    raw = json.loads(ctx.file("clips/shots.json").read_text(encoding="utf-8"))
    assert raw["s1"]["shots"][0]["relaxed"] is True
