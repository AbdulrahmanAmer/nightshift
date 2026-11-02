"""C8 Clipper: selection post-processing (windows, merge, snap, filler, spacing, NS-E407)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ns_common.errors import NsError
from ns_common.models import Transcript, Word
from ns_studio.clipper import select as sel
from ns_studio.clipper.models import ClipParams
from tests.fixtures.clipper.make_sample import build_transcript

FIXTURE_LLM = Path(__file__).parent / "fixtures" / "clipper" / "llm" / "clip_select.json"


def sample() -> Transcript:
    return Transcript.model_validate(build_transcript())


def params(**kw: Any) -> ClipParams:
    return ClipParams(source="sample.mp4", channel="after-dark", **kw)


def mk_words(n: int, per_s: float, *, filler_every: int = 0, start: float = 0.0) -> list[Word]:
    out: list[Word] = []
    for i in range(n):
        t = start + i / per_s
        text = "um" if filler_every and i % filler_every == 0 else f"w{i}"
        out.append(Word(w=text, start=t, end=t + 0.8 / per_s, seg=0))
    return out


class Asker:
    def __init__(self, raw: Any) -> None:
        self.raw = raw
        self.calls: list[tuple[float, float]] = []

    def __call__(self, ws: float, we: float) -> Any:
        self.calls.append((ws, we))
        return self.raw


def fixture_raw() -> Any:
    return json.loads(FIXTURE_LLM.read_text(encoding="utf-8"))


# ---------- windows ----------
def test_single_window_for_short_video() -> None:
    assert sel.make_windows(600.0) == [(0.0, 600.0)]


def test_windows_overlap_two_minutes() -> None:
    assert sel.make_windows(1800.0) == [(0.0, 720.0), (600.0, 1320.0), (1200.0, 1800.0)]


def test_windows_cover_four_hours() -> None:
    wins = sel.make_windows(4 * 3600.0)
    assert wins[0][0] == 0.0 and wins[-1][1] == 4 * 3600.0
    assert all(b - a <= 720.0 for a, b in wins)
    for (_, e1), (s2, _) in zip(wins, wins[1:], strict=False):
        assert e1 - s2 == pytest.approx(120.0)


def test_format_window_lists_segments_in_range() -> None:
    txt = sel.format_window(sample(), 0.0, 20.0)
    assert txt.count("\n") == 1 and txt.startswith("[0.5-")


# ---------- raw answers ----------
def test_extract_items_shapes() -> None:
    item = {"start_s": 1, "end_s": 2}
    assert sel.extract_items([item]) == [item]
    assert sel.extract_items({"clips": [item]}) == [item]
    assert sel.extract_items({"candidates": [item, 3]}) == [item]
    assert sel.extract_items({"other": 1}) == []
    assert sel.extract_items("nope") == []


def test_to_cand_clamps_and_rejects() -> None:
    c = sel.to_cand({"start": -4, "end": 500, "score": 3, "title": " T "}, 100.0)
    assert c is not None and (c.start, c.end, c.score, c.title) == (0.0, 100.0, 1.0, "T")
    assert sel.to_cand({"start_s": 5}, 100.0) is None
    assert sel.to_cand({"start_s": 50, "end_s": 40}, 100.0) is None


# ---------- merge ----------
def test_merge_keeps_best_title_and_union() -> None:
    a = sel.Cand(10, 40, title="a", score=0.5, keywords=["x"])
    b = sel.Cand(12, 42, title="b", score=0.9, keywords=["y"])
    out = sel.merge_overlaps([a, b], max_s=60)
    assert len(out) == 1
    m = out[0]
    assert (m.start, m.end, m.title, m.score) == (10, 42, "b", 0.9)
    assert m.keywords == ["y", "x"]


def test_merge_leaves_small_overlaps_alone() -> None:
    out = sel.merge_overlaps([sel.Cand(10, 40), sel.Cand(35, 65)], max_s=60)
    assert len(out) == 2


def test_merge_union_too_long_keeps_the_better_span() -> None:
    a = sel.Cand(0, 50, title="a", score=0.4)
    b = sel.Cand(20, 70, title="b", score=0.8)
    out = sel.merge_overlaps([a, b], max_s=60)
    assert [(m.start, m.end, m.title) for m in out] == [(20, 70, "b")]


# ---------- sentences and snapping ----------
def test_sentence_spans_match_fixture() -> None:
    spans = sel.sentence_spans(sample().words)
    assert len(spans) == 18
    assert spans[0][0] == pytest.approx(0.5)


def test_sentence_split_on_long_pause() -> None:
    ws = mk_words(10, 4.0) + mk_words(10, 4.0, start=20.0)
    assert len(sel.sentence_spans(ws)) == 2


def test_snap_moves_edges_onto_sentence_boundaries() -> None:
    tr = sample()
    seg1_end = max(w.end for w in tr.words if w.seg == 1)
    c = sel.snap_to_sentences(sel.Cand(0.6, 29.0), tr.words, min_s=20, max_s=60, duration_s=90)
    assert c is not None
    assert c.start == pytest.approx(0.4, abs=1e-6)
    assert c.end == pytest.approx(seg1_end + sel.PAD_AFTER_S, abs=1e-3)


def test_snap_shrinks_to_max() -> None:
    tr = sample()
    c = sel.snap_to_sentences(sel.Cand(0.6, 80.0), tr.words, min_s=20, max_s=60, duration_s=90)
    assert c is not None and 55 < c.duration <= 60


def test_snap_extends_to_min() -> None:
    tr = sample()
    c = sel.snap_to_sentences(sel.Cand(0.6, 8.0), tr.words, min_s=20, max_s=60, duration_s=90)
    assert c is not None and 20 <= c.duration <= 60


def test_snap_returns_none_when_no_sentence_end_fits() -> None:
    words = mk_words(100, 2.0)  # one 50 s sentence with no punctuation or pauses
    assert sel.snap_to_sentences(sel.Cand(0, 50), words, min_s=5, max_s=20, duration_s=60) is None


# ---------- filler ----------
def test_filler_filter_sparse_speech() -> None:
    assert sel.passes_filler_filter(sel.Cand(0, 10), mk_words(30, 3.0))
    assert not sel.passes_filler_filter(sel.Cand(0, 30), mk_words(30, 1.0))


def test_filler_filter_um_ratio() -> None:
    assert not sel.passes_filler_filter(sel.Cand(0, 10), mk_words(30, 3.0, filler_every=2))
    assert sel.passes_filler_filter(sel.Cand(0, 10), mk_words(30, 3.0, filler_every=4))


def test_filler_filter_empty_span() -> None:
    assert not sel.passes_filler_filter(sel.Cand(100, 120), mk_words(30, 3.0))


# ---------- ranking ----------
def test_pick_top_enforces_spacing_and_count() -> None:
    a = sel.Cand(0, 30, score=0.9)
    b = sel.Cand(40, 70, score=0.8)
    c = sel.Cand(100, 130, score=0.7)
    d = sel.Cand(200, 230, score=0.6)
    assert sel.pick_top([a, b, c, d], max_clips=2, spacing_s=60) == [a, c]
    assert sel.pick_top([a, b, c, d], max_clips=6, spacing_s=60) == [a, c, d]


def test_pick_top_never_overlaps_even_without_spacing() -> None:
    a = sel.Cand(0, 30, score=0.9)
    b = sel.Cand(20, 50, score=0.8)
    assert sel.pick_top([a, b], max_clips=6, spacing_s=0) == [a]


# ---------- end to end ----------
def test_select_clips_on_fixture() -> None:
    ask = Asker(fixture_raw())
    clips = sel.select_clips(sample(), params(), ask)
    assert [c.k for c in clips] == [1, 2]
    assert clips[0].start_s == pytest.approx(0.4, abs=0.05)
    assert 63.0 < clips[1].start_s < 65.0
    assert all(20 <= c.duration_s <= 60 for c in clips)
    assert clips[1].start_s - clips[0].start_s >= 60
    assert clips[0].title == "The mountain route in one take"
    assert len(ask.calls) == 1


def test_select_is_rerunnable_with_new_params_without_new_llm_calls() -> None:
    ask = Asker(fixture_raw())
    cache: dict[str, Any] = {}
    tr = sample()
    two = sel.select_clips(tr, params(), ask, cache)
    one = sel.select_clips(tr, params(max_clips=1), ask, cache)
    assert len(two) == 2 and len(one) == 1
    assert len(ask.calls) == 1
    assert one[0].title == two[0].title


def test_select_asks_again_when_length_bounds_change() -> None:
    ask = Asker(fixture_raw())
    cache: dict[str, Any] = {}
    tr = sample()
    sel.select_clips(tr, params(), ask, cache)
    sel.select_clips(tr, params(min_s=15, max_s=45), ask, cache)
    assert len(ask.calls) == 2


def test_select_none_left_is_e407() -> None:
    with pytest.raises(NsError) as ei:
        sel.select_clips(sample(), params(), Asker({"clips": []}))
    assert ei.value.code == "NS-E407"


def test_select_all_filler_is_e407() -> None:
    tr = sample()
    tr = tr.model_copy(update={"words": [w.model_copy(update={"w": "um"}) for w in tr.words]})
    with pytest.raises(NsError) as ei:
        sel.select_clips(tr, params(), Asker(fixture_raw()))
    assert ei.value.code == "NS-E407"


# ---------- plan with judge, layouts and drop reasons (spec 23.5) ----------
def always(ok: bool, *reasons: str):
    calls: list[sel.Cand] = []

    def judge(c: sel.Cand) -> tuple[bool, list[str]]:
        calls.append(c)
        return ok, list(reasons)

    judge.calls = calls  # type: ignore[attr-defined]
    return judge


def test_plan_lists_kept_clips_then_dropped_ones_with_reasons() -> None:
    items = sel.select_plan(sample(), params(), Asker(fixture_raw()), judge=always(True))
    assert [(i.k, i.status) for i in items] == [(1, "kept"), (2, "kept"), (3, "dropped")]
    assert "within 60 s" in items[2].reasons[0]
    assert items[0].layout == "gaming_facecam" and items[0].layout_reason
    assert items[1].layout == "clip_podcast_stacked"


def test_judge_rejection_is_recorded_and_the_next_candidate_is_tried() -> None:
    seen: list[str] = []

    def judge(c: sel.Cand) -> tuple[bool, list[str]]:
        seen.append(c.title)
        return (c.title.startswith("Beat"), ["ends mid-thought"])

    items = sel.select_plan(sample(), params(), Asker(fixture_raw()), judge=judge)
    kept = [i for i in items if i.status == "kept"]
    dropped = [i for i in items if i.status == "dropped"]
    assert [k.title for k in kept] == ["Beat the boss with four laps"] and kept[0].k == 1
    assert any(d.reasons == ["ends mid-thought"] for d in dropped)
    assert seen[0] == "The mountain route in one take"  # best score is judged first


def test_loop_stops_at_max_clips_without_judging_the_rest() -> None:
    judge = always(True)
    items = sel.select_plan(sample(), params(max_clips=1), Asker(fixture_raw()), judge=judge)
    assert [i.status for i in items][:1] == ["kept"]
    assert len(judge.calls) == 1  # type: ignore[attr-defined]


def test_everything_rejected_is_e407_with_the_reasons() -> None:
    items = sel.select_plan(sample(), params(), Asker(fixture_raw()), judge=always(False, "not standalone"))
    assert items and all(i.status == "dropped" for i in items)
    with pytest.raises(NsError) as ei:
        sel.ensure_some_kept(items)
    assert ei.value.code == "NS-E407" and "not standalone" in ei.value.detail


def test_select_clips_uses_the_judge_too() -> None:
    clips = sel.select_clips(sample(), params(), Asker(fixture_raw()), judge=always(True))
    assert [c.k for c in clips] == [1, 2] and all(c.status == "kept" for c in clips)


def test_filtered_candidates_are_reported_as_dropped() -> None:
    raw = {"clips": [{"start_s": 0.6, "end_s": 3.0, "title": "tiny", "why": "x", "score": 0.99}]}
    # the first sentence ends near 6 s and the next one near 10 s: no end fits 8 to 9 s
    items = sel.select_plan(sample(), params(min_s=8, max_s=9), Asker(raw), judge=always(True))
    assert [i.status for i in items] == ["dropped"]
    assert "sentence boundary" in items[0].reasons[0]


def test_hesitations_can_be_left_out_of_the_captions():
    """System gap 39: filler removal (spec 17.2) is a channel setting, off by default; "like" is never dropped."""
    from ns_studio.clipper.select import is_filler_word
    from tests.helpers import make_channel

    assert [is_filler_word(w) for w in ("Um,", "uh", "ERM.", "hmm...", "like", "umbrella", "the")] == [
        True, True, True, True, False, False, False,
    ]
    assert make_channel().captions.remove_fillers is False
    assert make_channel(captions={"remove_fillers": True}).captions.remove_fillers is True
