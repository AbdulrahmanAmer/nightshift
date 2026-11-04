import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_studio.media.clip_sources import ClipCandidate, Orientation
from ns_studio.media.clips import (
    ClipCache,
    ClipsStage,
    Searcher,
    blocked_clips,
    dedupe_keywords,
    has_face_hint,
    normalize_keyword,
    pick_for_slots,
    select_for_beat,
)
from ns_studio.media.shotplan import ShotSlot, beat_spans, frame_edges, plan_slots, split_span
from ns_studio.media.transcribe import Word
from tests.helpers import make_ctx

needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")


def cand(cid: str, *, w=1080, h=1920, dur=8.0, tags=(), title="", source="stub") -> ClipCandidate:
    return ClipCandidate(source, cid, f"https://x/{cid}", f"https://dl/{cid}.mp4", w, h, dur, tuple(tags),
                         title, "test")


class StubSource:
    """Search results by (keyword, orientation); `download` writes a few bytes."""

    name = "stub"

    def __init__(self, results: dict[tuple[str, str], list[ClipCandidate]] | None = None,
                 error: NsError | None = None) -> None:
        self.results = results or {}
        self.error = error
        self.searches: list[tuple[str, str]] = []
        self.downloads: list[str] = []

    def search(self, keyword: str, *, orientation: Orientation, per_page: int = 80) -> list[ClipCandidate]:
        self.searches.append((keyword, orientation))
        if self.error:
            raise self.error
        return list(self.results.get((keyword, orientation), []))

    def download(self, c: ClipCandidate, dest: Path) -> Path:
        self.downloads.append(c.id)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"clip")
        return dest


# ---- keywords ---------------------------------------------------------------------------------------------


def test_normalize_and_dedupe_keywords():
    assert normalize_keyword("  Dark   Windows!! ") == "dark window"
    assert normalize_keyword("glass") == "glass" and normalize_keyword("bus") == "bus"
    assert dedupe_keywords(["Dark Hallway", "dark hallway", "empty rooms", "empty room", ""]) == [
        "dark hallway", "empty room",
    ]


# ---- shot planning ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("length", [1.0, 1.8, 2.0, 3.0, 4.0, 7.3, 10.0, 25.0])
def test_split_span_respects_range_when_possible(length: float):
    parts = split_span(length, 1.8, 3.0)
    assert sum(parts) == pytest.approx(length)
    if length >= 1.8:
        assert all(1.8 - 1e-9 <= p <= 3.0 + 1e-9 for p in parts)
    else:
        assert parts == [length]


def test_beat_spans_and_slots_cover_the_timeline():
    words = [Word("a", 0.2, 0.4, 0), Word("b", 4.0, 4.2, 1), Word("c", 8.0, 8.2, 2), Word("h", 0.0, 0.1, -1)]
    beats = [
        {"sentence_start": 2, "sentence_end": 2},
        {"sentence_start": 0, "sentence_end": 1},
    ]
    spans = beat_spans(beats, words, 12.0)
    assert spans == [(1, 0.0, 8.0), (0, 8.0, 12.0)]
    slots = plan_slots(spans, (1.8, 3.0))
    assert slots[0].start == 0.0 and slots[-1].end == 12.0
    assert all(a.end == b.start for a, b in zip(slots, slots[1:], strict=False))
    assert all(1.8 <= s.length <= 3.0 for s in slots)
    edges = frame_edges([s.end for s in slots], 30)
    assert edges[0] == 0 and edges[-1] == 360 and edges == sorted(set(edges))


# ---- reuse rules ------------------------------------------------------------------------------------------


def _usage(ctx, clip_id: str, channel: str, job: str, age_days: float, now: datetime) -> None:
    ctx.db.execute(
        "insert or ignore into clips(source, clip_id, url, license, fetched_at) values ('stub',?,?,?,?)",
        (clip_id, "u", "l", now.isoformat()),
    )
    ctx.db.execute(
        "insert into clip_usage(source, clip_id, channel, job_id, item, used_at) values ('stub',?,?,?,?,?)",
        (clip_id, channel, job, "s1", (now - timedelta(days=age_days)).isoformat()),
    )


def test_blocked_clips_apply_14_day_channel_and_30_day_cross_channel_rules(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    now = datetime(2026, 11, 20, tzinfo=UTC)
    _usage(ctx, "same-10d", "after-dark", "old-1", 10, now)
    _usage(ctx, "same-20d", "after-dark", "old-2", 20, now)
    _usage(ctx, "other-20d", "other-chan", "old-3", 20, now)
    _usage(ctx, "other-40d", "other-chan", "old-4", 40, now)
    _usage(ctx, "mine", "after-dark", ctx.job_id, 1, now)
    blocked = blocked_clips(ctx.db, "after-dark", ctx.job_id, now)
    assert blocked == {("stub", "same-10d"), ("stub", "other-20d")}


# ---- picking ----------------------------------------------------------------------------------------------


def _slots(*lengths: float) -> list[ShotSlot]:
    out, t = [], 0.0
    for ln in lengths:
        out.append(ShotSlot(0, t, t + ln))
        t += ln
    return out


def test_pick_prefers_hd_portrait_and_distinct_clips():
    pool = [cand("sd", w=540, h=960), cand("wide", w=1920, h=1080), cand("a"), cand("b")]
    got = pick_for_slots(pool, _slots(2, 2), used=set(), blocked=set(), dark=False)
    assert [c.id for c in got or []] == ["a", "b"]
    got3 = pick_for_slots(pool, _slots(2, 2, 2), used=set(), blocked=set(), dark=False)
    assert [c.id for c in got3 or []] == ["a", "b", "wide"]


def test_pick_skips_used_blocked_and_too_short_clips():
    pool = [cand("short", dur=2.5), cand("used"), cand("blocked"), cand("ok", dur=4.0)]
    got = pick_for_slots(
        pool, _slots(2.0), used={("stub", "used")}, blocked={("stub", "blocked")}, dark=False
    )
    assert [c.id for c in got or []] == ["ok"]  # "short" lacks the 1 s margin
    # when only the short one is left the margin is dropped
    only = pick_for_slots([cand("short", dur=2.5)], _slots(2.0), used=set(), blocked=set(), dark=False)
    assert [c.id for c in only or []] == ["short"]
    assert pick_for_slots([cand("tiny", dur=1.0)], _slots(2.0), used=set(), blocked=set(), dark=False) is None


def test_dark_moods_skip_face_hints():
    face = cand("f", tags=("woman", "portrait"))
    assert has_face_hint(face) and has_face_hint(cand("g", title="face of a man"))
    assert not has_face_hint(cand("h", tags=("rain", "window")))
    pool = [face, cand("plain", tags=("rain",))]
    assert [c.id for c in pick_for_slots(pool, _slots(2), used=set(), blocked=set(), dark=True) or []] == [
        "plain"
    ]
    assert [c.id for c in pick_for_slots(pool, _slots(2), used=set(), blocked=set(), dark=False) or []] == [
        "f"
    ]


def test_searcher_memoizes_per_source_orientation_keyword():
    src = StubSource({("rain", "portrait"): [cand("r1")]})
    s = Searcher([src])
    for _ in range(3):
        assert [c.id for c in s.search("rain", "portrait")] == ["r1"]
    s.search("rain", "landscape")
    assert src.searches == [("rain", "portrait"), ("rain", "landscape")]


def test_select_widens_to_landscape_then_mood_and_raises_e402():
    src = StubSource({
        ("hallway", "portrait"): [cand("p1")],
        ("hallway", "landscape"): [cand("l1", w=1920, h=1080)],
        ("eerie", "portrait"): [cand("m1"), cand("m2")],
    })
    slots = _slots(2, 2)
    two = select_for_beat(Searcher([src]), ["hallway"], "calm", slots, used=set(), blocked=set())
    assert [c.id for c, _kw in two] == ["p1", "l1"]  # portrait first, landscape added to fill the beat
    three = select_for_beat(Searcher([src]), ["hallway"], "eerie", _slots(2, 2, 2), used=set(), blocked=set())
    assert [c.id for c, _kw in three] == ["p1", "m1", "m2"]
    assert [kw for _c, kw in three][0] == "hallway"
    with pytest.raises(NsError) as ei:
        select_for_beat(Searcher([StubSource()]), ["nothing"], "eerie", slots, used=set(), blocked=set())
    assert ei.value.code == "NS-E402"


def test_select_reraises_api_error_when_every_source_failed():
    src = StubSource(error=NsError("NS-E501", "HTTP 401"))
    with pytest.raises(NsError) as ei:
        select_for_beat(Searcher([src]), ["x"], "eerie", _slots(2), used=set(), blocked=set())
    assert ei.value.code == "NS-E501"


def test_a_failing_source_is_skipped_when_another_delivers():
    bad = StubSource(error=NsError("NS-E502", "down"))
    bad.name = "bad"
    good = StubSource({("x", "portrait"): [cand("g1")]})
    got = select_for_beat(Searcher([bad, good]), ["x"], "eerie", _slots(2), used=set(), blocked=set())
    assert [c.id for c, _ in got] == ["g1"]


# ---- cache ------------------------------------------------------------------------------------------------


def test_clip_cache_downloads_once_and_expires_after_90_days(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    src = StubSource()
    clock = {"now": datetime(2026, 11, 1, tzinfo=UTC)}
    cache = ClipCache(ctx.db, ctx.paths, [src], now=lambda: clock["now"])
    c = cand("abc")
    p1 = cache.ensure(c, ["rain"])
    assert p1 == ctx.paths.cache_clips / "stub" / "abc.mp4" and p1.read_bytes() == b"clip"
    cache.ensure(c, ["window"])
    assert src.downloads == ["abc"]  # cache hit costs nothing
    row = ctx.db.one("select * from clips where source='stub' and clip_id='abc'")
    assert json.loads(row["keywords"]) == ["rain", "window"] and row["license"] == "test"
    clock["now"] += timedelta(days=91)
    assert cache.purge_expired() == 1 and not p1.exists()
    cache.ensure(c, ["rain"])
    assert src.downloads == ["abc", "abc"]


def test_clip_cache_unknown_source_is_e402(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    with pytest.raises(NsError) as ei:
        ClipCache(ctx.db, ctx.paths, []).ensure(cand("zzz", source="nope"), [])
    assert ei.value.code == "NS-E402"


# ---- stage ------------------------------------------------------------------------------------------------


def _prepare_job(ctx) -> None:
    ctx.write_json("parts.json", [{"n": 1, "text": "x", "hook": "", "word_count": 1, "est_seconds": 12.0}])
    ctx.write_json("captions/report.json", {"s1": {}})
    words = [
        {"w": "a", "start": 0.2, "end": 0.5, "sentence_idx": 0},
        {"w": "b", "start": 4.0, "end": 4.3, "sentence_idx": 1},
        {"w": "c", "start": 8.0, "end": 8.3, "sentence_idx": 2},
    ]
    ctx.write_json("captions/s1.json", {"k": 1, "audio_s": 12.0, "words": words})
    ctx.write_json("visual_plan.json", [
        {"part": 1, "beat": 0, "sentence_start": 0, "sentence_end": 1,
         "keywords": ["dark hallway", "flickering light"],
         "mood": "eerie", "need_vertical": True},
        {"part": 1, "beat": 1, "sentence_start": 2, "sentence_end": 2, "keywords": ["rain window"],
         "mood": "intimate", "need_vertical": True},
    ])


@needs_ffmpeg
def test_clips_stage_plans_shots_and_downloads(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _prepare_job(ctx)
    ClipsStage().run(ctx)
    data = ctx.read_json("clips/shots.json")["s1"]
    shots = data["shots"]
    assert data["total_s"] == 12.0 and len(shots) == 5
    assert shots[0]["start"] == 0.0 and shots[-1]["end"] == pytest.approx(12.0)
    assert all(1.8 - 1e-6 <= s["end"] - s["start"] <= 3.0 + 1e-6 for s in shots)
    assert all(a["end"] == pytest.approx(b["start"]) for a, b in zip(shots, shots[1:], strict=False))
    assert len({(s["source"], s["clip_id"]) for s in shots}) == 5
    assert all(Path(s["path"]).exists() and s["ss"] >= 0 for s in shots)
    assert [s["keyword"] for s in shots[3:]] == ["rain window", "rain window"]
    assert ctx.db.one("select count(*) c from clips")["c"] == 5
    src = ctx.services.clip_sources[0]
    assert sorted(src.searches) == [
        ("dark hallway", "portrait"), ("flickering light", "portrait"), ("rain window", "portrait"),
    ]


@needs_ffmpeg
def test_clips_stage_avoids_clips_used_recently_on_the_channel(tmp_home: Path):
    first = make_ctx(tmp_home)
    _prepare_job(first)
    ClipsStage().run(first)
    used = {(s["source"], s["clip_id"]) for s in first.read_json("clips/shots.json")["s1"]["shots"]}
    now = datetime.now(UTC).isoformat()
    for source, clip_id in used:
        first.db.execute(
            "insert into clip_usage(source, clip_id, channel, job_id, item, used_at) values (?,?,?,?,?,?)",
            (source, clip_id, first.channel.slug, first.job_id, "s1", now),
        )
    second = make_ctx(tmp_home)
    _prepare_job(second)
    ClipsStage().run(second)
    again = {(s["source"], s["clip_id"]) for s in second.read_json("clips/shots.json")["s1"]["shots"]}
    assert len(again) == 5 and not (again & used)


def test_clips_stage_raises_e402_when_nothing_found(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _prepare_job(ctx)
    ctx.services.clip_sources = [StubSource()]
    with pytest.raises(NsError) as ei:
        ClipsStage().run(ctx)
    assert ei.value.code == "NS-E402"


def test_a_beat_reuses_footage_with_a_warning_instead_of_failing_when_every_clip_is_blocked():
    """The first dev-mode nightly run died with NS-E402 "24 candidates for 2 slots": the fake source's
    clips had all been used by that day's Shorts (14-day rule). Repeating footage beats losing the video."""
    src = StubSource({
        ("hallway", "portrait"): [cand("p1"), cand("p2"), cand("p3")],
    })
    blocked = {("stub", "p1"), ("stub", "p2"), ("stub", "p3")}
    picks = select_for_beat(Searcher([src]), ["hallway"], "calm", _slots(2), used=set(), blocked=blocked)
    assert [c.id for c, _kw in picks] == ["p1", "p2"]
    # but a clip already used inside this video is still never repeated
    with pytest.raises(NsError) as ei:
        select_for_beat(Searcher([src]), ["hallway"], "calm", _slots(2),
                        used={("stub", "p1"), ("stub", "p2"), ("stub", "p3")}, blocked=blocked)
    assert ei.value.code == "NS-E402"
