import json
import sys
from pathlib import Path

import pytest

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_studio.media import master as M
from ns_studio.media.captions import first_event_start
from ns_studio.media.master import (
    Chapter,
    DurationCheckStage,
    ShortsTimingStage,
    load_chapters,
    load_shorts,
    master_timeline,
    resolve_span,
    run_captions_master,
    run_voice_master,
    timeline_from_manifest,
    voice_items,
)
from ns_studio.media.transcribe import Word
from tests.helpers import make_ctx

needs_ffmpeg = pytest.mark.skipif(not ff.available(), reason="ffmpeg required")


def chapter_text(prefix: str, sentences: int = 3, words: int = 10) -> str:
    out = []
    for s in range(sentences):
        body = " ".join(f"{prefix}{chr(97 + s)}{chr(97 + i)}" for i in range(words - 1))
        out.append(f"{body} end.")
    return " ".join(out)


def write_story(ctx, chapters: int = 3, sentences: int = 3) -> list[str]:
    texts = [chapter_text(chr(120 - i), sentences) for i in range(chapters)]
    ctx.write_json("story.json", {"chapters": [
        {"n": i + 1, "title": f"Chapter title {i + 1}", "text": t} for i, t in enumerate(texts)
    ]})
    return texts


def write_shorts_plan(ctx) -> None:
    ctx.write_json("shorts_plan.json", [
        {"k": 1, "chapter": 1, "sentence_start": 0, "sentence_end": 1,
         "hook": "Listen to this opening line now.",
         "closing_line": "The full story is on the channel.", "length_bucket": 35},
        {"k": 2, "chapter": 2, "sentence_start": 1, "sentence_end": 2, "hook": "Nobody believed me at first.",
         "closing_line": "Watch the whole thing on the channel."},
    ])


def test_load_story_and_shorts_shapes(tmp_home: Path):
    ctx = make_ctx(tmp_home, fmt="master_first")
    write_story(ctx)
    ctx.write_json("shorts_plan.json", {"shorts": [{"chapter": 2, "sentence_start": 0, "sentence_end": 1,
                                                    "hook": " h ", "closing": "c"}]})
    chs = load_chapters(ctx)
    assert [c.n for c in chs] == [1, 2, 3] and chs[0].title == "Chapter title 1"
    sh = load_shorts(ctx)
    assert (sh[0].k, sh[0].chapter, sh[0].hook, sh[0].closing_line) == (1, 2, "h", "c")
    assert load_shorts(make_ctx(tmp_home, fmt="master_first")) == []


def test_master_timeline_puts_cards_between_chapters():
    chs = [Chapter(1, "A", ""), Chapter(2, "B", ""), Chapter(3, "C", "")]
    tl = master_timeline(chs, {1: 100.0, 2: 80.0, 3: 60.0}, card_s=1.2)
    assert (tl[0].seg_start, tl[0].audio_start, tl[0].end) == (0.0, 0.0, 100.0)
    assert (tl[1].seg_start, tl[1].audio_start, tl[1].end) == (100.0, 101.2, 181.2)
    assert (tl[2].seg_start, tl[2].audio_start, tl[2].end) == (181.2, 182.4, pytest.approx(242.4))


def _words(ss: list[int]) -> list[Word]:
    """Two words per sentence index, 0.5 s apart, sentences 1 s apart."""
    out = []
    for s in ss:
        out += [Word(f"w{s}a", s * 1.0, s * 1.0 + 0.3, s), Word(f"w{s}b", s * 1.0 + 0.5, s * 1.0 + 0.8, s)]
    return out


def test_resolve_span_snaps_to_sentence_boundaries():
    words = _words([0, 1, 2, 3])
    start, end = resolve_span(words, 1, 2, chapter_s=4.0)
    assert start == pytest.approx(0.95)  # 50 ms before the first word of sentence 1
    assert end == pytest.approx(2.8 + 0.2)  # last word end + 250 ms, but stops at the next sentence (3.0)
    s2, e2 = resolve_span(words, 3, 3, chapter_s=3.9)
    assert e2 == pytest.approx(3.9)  # capped at the chapter end
    with pytest.raises(NsError) as ei:
        resolve_span(words, 2, 9, chapter_s=4.0)
    assert ei.value.code == "NS-E303"


# ---- voice, captions, timing with fakes ------------------------------------------------------


@needs_ffmpeg
def test_voice_master_items_and_second_run(tmp_home: Path):
    ctx = make_ctx(tmp_home, fmt="master_first")
    write_story(ctx)
    write_shorts_plan(ctx)
    assert [i.id for i in voice_items(ctx)] == ["c1", "c2", "c3", "h1", "x1", "h2", "x2"]
    run_voice_master(ctx)
    man = ctx.read_json("audio/manifest.json")
    assert set(man) == {"c1", "c2", "c3", "h1", "x1", "h2", "x2"}
    assert Path(man["c1"]["path"]).with_suffix(".txt").exists()
    tl = timeline_from_manifest(ctx)
    assert tl[1].seg_start == pytest.approx(man["c1"]["seconds"])
    assert tl[1].audio_start - tl[1].seg_start == pytest.approx(1.2)
    ctx.services.tts.speak_batch = None  # type: ignore[assignment,method-assign]
    run_voice_master(ctx)  # nothing changed: no TTS call
    assert ctx.read_json("audio/manifest.json") == man


@needs_ffmpeg
def test_captions_master_and_shorts_timing(tmp_home: Path):
    ctx = make_ctx(tmp_home, fmt="master_first")
    write_story(ctx)
    write_shorts_plan(ctx)
    run_voice_master(ctx)
    run_captions_master(ctx)
    rep = ctx.read_json("captions/report.json")
    assert set(rep) == {"long", "h1", "x1", "h2", "x2"}
    assert rep["long"]["drift_ms"] == pytest.approx(0.0, abs=1e-6) and rep["long"]["first_caption_s"] == 0.0
    doc = ctx.read_json("captions/long.json")
    tl = timeline_from_manifest(ctx)
    assert [c["audio_start"] for c in doc["chapters"]] == [pytest.approx(t.audio_start) for t in tl]
    first_ch2 = next(w for w in doc["words"] if w["w"] == "waa")
    assert first_ch2["start"] == pytest.approx(tl[1].audio_start, abs=1e-3)
    local = ctx.file("captions/long_c2.ass").read_text(encoding="utf-8")
    assert "PlayResX: 1920" in local
    assert first_event_start(local) == pytest.approx(1.2, abs=0.02)  # captions start after the 1.2 s card

    ShortsTimingStage().run(ctx)
    timing = ctx.read_json("shorts_timing.json")
    assert [t["k"] for t in timing] == [1, 2]
    t1 = timing[0]
    ch1 = [Word.from_json(w) for w in ctx.read_json("words/c1.json")]
    first_s1 = next(w for w in ch1 if w.sentence_idx == 0)
    assert t1["rel_start_s"] == pytest.approx(max(0.0, first_s1.start - 0.05), abs=1e-3)
    assert t1["start_s"] == pytest.approx(t1["rel_start_s"], abs=1e-3)  # chapter 1 starts at 0
    t2 = timing[1]
    assert t2["start_s"] == pytest.approx(tl[1].audio_start + t2["rel_start_s"], abs=1e-3)
    assert t2["end_s"] > t2["start_s"]


@needs_ffmpeg
def test_shorts_timing_unknown_span_is_e303(tmp_home: Path):
    ctx = make_ctx(tmp_home, fmt="master_first")
    write_story(ctx)
    ctx.write_json("shorts_plan.json", [{"k": 1, "chapter": 1, "sentence_start": 0, "sentence_end": 7,
                                         "hook": "h", "closing_line": "c"}])
    run_voice_master(ctx)
    run_captions_master(ctx)
    with pytest.raises(NsError) as ei:
        ShortsTimingStage().run(ctx)
    assert ei.value.code == "NS-E303"


# ---- duration check ----------------------------------------------------------------------------


def _fake_expand(calls: list[tuple[int, int]], words_added: int):
    def expand(ctx, n: int, add_words: int) -> None:
        calls.append((n, add_words))
        story = ctx.read_json("story.json")
        for c in story["chapters"]:
            if c["n"] == n:
                c["text"] += " " + " ".join(f"extra{i}" for i in range(words_added - 1)) + " done."
        ctx.write_json("story.json", story)

    return expand


@needs_ffmpeg
def test_duration_check_passes_without_expansion(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(M, "FLOOR_S", 20.0)
    ctx = make_ctx(tmp_home, fmt="master_first")
    write_story(ctx)
    run_voice_master(ctx)
    DurationCheckStage().run(ctx)
    doc = ctx.read_json("duration_check.json")
    assert doc["expansions"] == [] and doc["total_s"] > 20.0


@needs_ffmpeg
def test_duration_check_expands_shortest_chapter_and_revoices_only_it(
    tmp_home: Path, monkeypatch: pytest.MonkeyPatch
):
    ctx = make_ctx(tmp_home, fmt="master_first")
    texts = write_story(ctx)
    story = ctx.read_json("story.json")
    story["chapters"][1]["text"] = chapter_text("q", sentences=2)  # chapter 2 is the shortest
    ctx.write_json("story.json", story)
    run_voice_master(ctx)
    before = ctx.read_json("audio/manifest.json")
    total = timeline_from_manifest(ctx)[-1].end
    monkeypatch.setattr(M, "FLOOR_S", total + 8.0)
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(M, "load_expand_chapter", lambda: _fake_expand(calls, 60))
    seen: list[list[str]] = []
    real = ctx.services.tts.speak_batch

    def spy(items, out_dir, *, seed=None):
        seen.append([i.id for i in items])
        return real(items, out_dir, seed=seed)

    ctx.services.tts.speak_batch = spy  # type: ignore[method-assign]
    DurationCheckStage().run(ctx)
    assert calls == [(2, 120)]  # shortest chapter, add_words clamped to 120-200
    assert seen == [["c2"]]
    after = ctx.read_json("audio/manifest.json")
    assert after["c1"] == before["c1"] and after["c2"]["seconds"] > before["c2"]["seconds"]
    doc = ctx.read_json("duration_check.json")
    assert len(doc["expansions"]) == 1 and doc["expansions"][0]["chapter"] == 2
    assert texts  # fixture sanity
    events = [e for e in ctx.services.acquirer.events if e[1] in ("llama",)]  # type: ignore[attr-defined]
    assert [e[0] for e in events] == ["acquire", "release"]


@needs_ffmpeg
def test_duration_check_fails_with_e305_after_two_expansions(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(M, "FLOOR_S", 1000.0)
    ctx = make_ctx(tmp_home, fmt="master_first")
    write_story(ctx)
    run_voice_master(ctx)
    calls: list[tuple[int, int]] = []
    monkeypatch.setattr(M, "load_expand_chapter", lambda: _fake_expand(calls, 4))
    with pytest.raises(NsError) as ei:
        DurationCheckStage().run(ctx)
    assert ei.value.code == "NS-E305" and len(calls) == 2
    assert json.loads(ctx.file("duration_check.json").read_text(encoding="utf-8"))["expansions"]


@needs_ffmpeg
def test_duration_check_without_expand_helper_is_e305(tmp_home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "ns_studio.text.stages_master", None)  # import raises ImportError
    monkeypatch.setattr(M, "FLOOR_S", 1000.0)
    ctx = make_ctx(tmp_home, fmt="master_first")
    write_story(ctx)
    run_voice_master(ctx)
    with pytest.raises(NsError) as ei:
        DurationCheckStage().run(ctx)
    assert ei.value.code == "NS-E305"
