import re
from pathlib import Path

import pytest

from ns_common import ffmpeg as ff
from ns_common.errors import NsError
from ns_common.tts_client import FakeTts, wav_seconds
from ns_studio.media.captions import (
    DRIFT_LIMIT_MS,
    Box,
    CaptionsStage,
    _merge_pages,
    align_words,
    build_ass,
    choose_margin_v,
    contrast_ratio,
    drift_ms,
    ensure_contrast,
    first_event_start,
    free_bands,
    group_words,
    mask_text,
    profanity_words,
    relative_luminance,
    sample_luma,
    script_words,
)
from ns_studio.media.presets import caption_style, load_caption_styles
from ns_studio.media.transcribe import FakeTranscriber, Word
from tests.helpers import make_combo, make_ctx

SCRIPT = "The doorbell camera pings at 3:12 every night. I stopped checking the clips a week ago."


def _even_asr(tokens: list[str], step: float = 0.4) -> list[Word]:
    return [Word(t, i * step, i * step + 0.3) for i, t in enumerate(tokens)]


def test_script_words_keep_spelling_and_sentence_index():
    ws = script_words("Wait — what? It's 3:12.")
    assert [w for w, _ in ws] == ["Wait —", "what?", "It's", "3:12."]
    assert [s for _, s in ws] == [0, 0, 1, 1]


def test_align_exact_match_uses_asr_times_and_script_spelling():
    asr = _even_asr(["the", "doorbell", "camera", "pings", "at", "three", "twelve", "every", "night"])
    res = align_words("The doorbell camera pings at 3:12 every night.", asr)
    assert [w.w for w in res.words] == ["The", "doorbell", "camera", "pings", "at", "3:12", "every", "night."]
    assert res.matched == res.total == 8
    assert res.words[5].start == pytest.approx(5 * 0.4)  # "3:12" takes the first of its two ASR tokens
    assert res.words[5].end == pytest.approx(6 * 0.4 + 0.3)
    assert res.drift_ms == pytest.approx(0.0, abs=1e-6)
    assert all(w.sentence_idx == 0 for w in res.words)


def test_align_interpolates_unmatched_words_between_neighbours():
    asr = [Word("the", 0.0, 0.3), Word("camera", 2.0, 2.3), Word("pings", 2.5, 2.8)]
    res = align_words("The doorbell ghost camera pings", asr)
    assert res.matched == 3 and res.total == 5
    mid = res.words[1:3]
    assert all(0.3 <= w.start <= 2.0 for w in mid)
    assert mid[0].start < mid[1].start < 2.0
    assert [w.start for w in res.words] == sorted(w.start for w in res.words)


def test_align_handles_leading_trailing_and_inserted_asr_words():
    asr = [Word("uh", 0.0, 0.2), Word("camera", 0.5, 0.9), Word("pings", 1.0, 1.4)]
    res = align_words("Old camera pings loudly", asr, total_s=2.4)
    assert res.words[0].start == 0.0 and res.words[0].end <= 0.5  # leading run before first match
    assert res.words[1].start == pytest.approx(0.5)
    assert res.words[3].start >= 1.4 and res.words[3].end <= 2.4 + 1e-9  # trailing run to total_s


def test_align_without_asr_spreads_words_and_reports_infinite_drift():
    res = align_words("one two three four", [], total_s=4.0)
    assert res.matched == 0 and res.match_ratio == 0 and res.drift_ms == float("inf")
    assert res.words[0].start == 0.0 and res.words[-1].end == pytest.approx(4.0)


def test_drift_ms_is_the_median_offset_between_caption_and_asr_starts():
    asr = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5]
    assert drift_ms(asr, asr) == pytest.approx(0.0, abs=1e-9)
    shifted = [t + 0.2 for t in asr]  # a uniform 200 ms offset is 200 ms of drift
    assert drift_ms(shifted, asr) == pytest.approx(200.0)
    one_late = [0.0, 0.5, 1.2, 1.5, 2.0, 2.5]  # one word 200 ms late: the median ignores it
    assert drift_ms(one_late, asr) == pytest.approx(0.0, abs=1e-9)
    assert drift_ms([], []) == float("inf")


def test_uneven_speech_rhythm_is_not_drift():
    """Review row 3: the old leave-one-out residual measured rhythm; real narration failed the 120 ms gate."""
    script = "I opened the refrigerator slowly"
    # starts 0.00/0.12/0.48/0.58/1.20 from the review: the old metric gave residuals of 120/130/260 ms
    asr = [Word("i", 0.0, 0.1), Word("opened", 0.12, 0.4), Word("the", 0.48, 0.55),
           Word("refrigerator", 0.58, 1.1), Word("slowly", 1.2, 1.6)]
    res = align_words(script, asr)
    assert res.matched == res.total == 5
    assert res.drift_ms == pytest.approx(0.0, abs=1e-6)
    assert res.drift_ms <= DRIFT_LIMIT_MS


def test_asr_that_runs_backwards_is_drift():
    """Captions are forced monotonic; the offset from the ASR start is what the gate measures."""
    script = "one two three four five six"
    asr = [Word(t, s, s + 0.2) for t, s in zip(script.split(), [0.0, 3.0, 1.0, 1.4, 1.8, 2.2], strict=True)]
    res = align_words(script, asr)
    assert [round(w.start, 2) for w in res.words] == [0.0, 3.0, 3.0, 3.0, 3.0, 3.0]
    assert res.drift_ms == pytest.approx(1000.0)  # offsets 0, 0, 2.0, 1.6, 1.2, 0.8 s -> median 1.0 s
    assert res.drift_ms > DRIFT_LIMIT_MS


def test_group_words_sizes_and_sentence_boundaries():
    style = caption_style("punch_yellow")  # up to 3 words
    words = [Word(t, i * 0.3, i * 0.3 + 0.2) for i, t in enumerate("a b c d e f g. h i".split())]
    groups = group_words(words, style)
    assert [[w.w for w in g] for g in groups] == [["a", "b", "c"], ["d", "e"], ["f", "g."], ["h", "i"]]
    # a long pause splits a run
    gapped = [Word("x", 0.0, 0.2), Word("y", 0.3, 0.5), Word("z", 3.0, 3.2), Word("w", 3.3, 3.5)]
    assert [[w.w for w in g] for g in group_words(gapped, style)] == [["x", "y"], ["z", "w"]]
    assert all(1 <= len(g) <= 4 for g in groups)


def test_all_styles_load_and_groups_respect_limits():
    styles = load_caption_styles()
    assert {"punch_yellow", "clean_white", "boxed_dark", "minimal_lower"} <= set(styles)
    words = [Word(f"w{i}", i * 0.3, i * 0.3 + 0.2) for i in range(23)]
    for st in styles.values():
        assert 2 <= st.words_on_screen[0] <= st.words_on_screen[1] <= 4
        assert max(len(g) for g in group_words(words, st)) <= st.words_on_screen[1]


def _words(n: int = 8) -> list[Word]:
    toks = "it was a quiet night in town.".split()[:n]
    return [Word(t, 0.2 + i * 0.35, 0.2 + i * 0.35 + 0.3) for i, t in enumerate(toks)]


def test_build_ass_vertical_punch_yellow(tmp_path: Path):
    ass = build_ass(_words(), "punch_yellow", fonts_dir=tmp_path)
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    assert first_event_start(ass) == 0.0  # first caption at 0.0 s although speech starts at 0.2
    assert "Style: Default,Impact," in ass  # fallback font while Anton is absent
    assert "&H00D4FF&" in ass  # active word colour #FFD400 as BGR
    assert "QUIET" in ass  # uppercase style
    events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert len(events) == len(_words())  # one event per word
    for ev in events:
        text = ev.split(",", 9)[9]
        visible = re.sub(r"\{[^}]*\}", "", text).split()
        assert 1 <= len(visible) <= 3


def test_build_ass_uses_anton_when_installed(tmp_path: Path):
    (tmp_path / "Anton-Regular.ttf").write_bytes(b"")
    assert "Style: Default,Anton," in build_ass(_words(), "punch_yellow", fonts_dir=tmp_path)


def test_build_ass_landscape_and_karaoke_variants():
    land = build_ass(_words(), "clean_white", variant="landscape")
    assert "PlayResX: 1920" in land and "PlayResY: 1080" in land
    kara = build_ass(_words(), "boxed_dark")
    assert "\\kf" in kara and kara.count("Dialogue:") < len(_words())  # one event per group
    assert re.search(r"Style: Default,Arial,70,.*,3,14", kara)  # opaque box border style


def test_ass_times_are_monotonic_and_end_after_start():
    ass = build_ass(_words(), "clean_white")
    pat = re.compile(r"Dialogue: 0,(\d+):(\d\d):(\d\d)\.(\d\d),(\d+):(\d\d):(\d\d)\.(\d\d),")
    last = -1.0
    for m in pat.finditer(ass):
        a = [int(x) for x in m.groups()]
        s, e = a[0] * 3600 + a[1] * 60 + a[2] + a[3] / 100, a[4] * 3600 + a[5] * 60 + a[6] + a[7] / 100
        assert e > s and s >= last - 1e-9
        last = s


# ---- stage ----------------------------------------------------------------------------------------------


def _prepare_audio(ctx, text: str, hook: str | None):
    parts = [{"n": 1, "text": text, "hook": hook or "", "word_count": len(text.split()), "est_seconds": 5.0,
              "cliffhanger": False}]
    ctx.write_json("parts.json", parts)
    manifest = {}
    audio = ctx.subdir("audio")
    for ident, t in (("p1", text), ("p1_hook", hook)):
        if t is None:
            continue
        wav = audio / f"{ident}.wav"
        FakeTts().speak(t, "Ryan", 1.0, wav)
        wav.with_suffix(".txt").write_text(t, encoding="utf-8")
        manifest[ident] = {"path": str(wav), "seconds": round(wav_seconds(wav), 3)}
    ctx.write_json("audio/manifest.json", manifest)
    return manifest


def test_captions_stage_with_hook(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    man = _prepare_audio(ctx, SCRIPT, "My doorbell camera records someone every night.")
    CaptionsStage().run(ctx)
    rep = ctx.read_json("captions/report.json")["s1"]
    assert rep["drift_ms"] == pytest.approx(0.0, abs=1e-6) and rep["match_ratio"] == 1.0
    assert rep["first_caption_s"] == 0.0
    tl = ctx.read_json("captions/s1.json")
    offset = man["p1_hook"]["seconds"] + 0.15
    assert tl["body_offset_s"] == pytest.approx(offset)
    assert tl["audio_s"] == pytest.approx(offset + man["p1"]["seconds"], abs=1e-3)
    hook_words = [w for w in tl["words"] if w["sentence_idx"] == -1]
    assert [w["w"] for w in hook_words][:2] == ["My", "doorbell"]
    body_first = next(w for w in tl["words"] if w["sentence_idx"] == 0)
    assert body_first["start"] == pytest.approx(offset, abs=1e-3)
    assert ctx.file("words/p1.json").exists() and ctx.file("words/p1_hook.json").exists()
    assert ctx.file("captions/s1.ass").read_text(encoding="utf-8").startswith("[Script Info]")


def test_captions_stage_without_hook(tmp_home: Path):
    ctx = make_ctx(tmp_home, combo=make_combo(caption_style="clean_white"))
    _prepare_audio(ctx, SCRIPT, None)
    CaptionsStage().run(ctx)
    tl = ctx.read_json("captions/s1.json")
    assert tl["body_offset_s"] == 0.0 and tl["hook_s"] == 0.0
    assert tl["words"][0]["w"] == "The"


class _ShuffledTranscriber(FakeTranscriber):
    """Hears the right number of words, but not the script."""

    def words(self, wav, language="en"):
        return [Word(f"zzz{i}", w.start, w.end) for i, w in enumerate(super().words(wav, language))]


def test_captions_stage_fails_with_e403_when_asr_disagrees(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _prepare_audio(ctx, SCRIPT, None)
    ctx.services.transcriber = _ShuffledTranscriber()
    with pytest.raises(NsError) as ei:
        CaptionsStage().run(ctx)
    assert ei.value.code == "NS-E403"
    assert ctx.file("captions/report.json").exists()  # evidence is written before failing


class _LateTranscriber(FakeTranscriber):
    """The first word is timestamped 5 s late (a VAD glitch); every later caption is pushed behind it."""

    def words(self, wav, language="en"):
        return [
            Word(w.w, w.start + (5.0 if i == 0 else 0.0), w.end + (5.0 if i == 0 else 0.0))
            for i, w in enumerate(super().words(wav, language))
        ]


def test_captions_stage_fails_with_e403_on_drift(tmp_home: Path):
    ctx = make_ctx(tmp_home)
    _prepare_audio(ctx, SCRIPT, None)
    ctx.services.transcriber = _LateTranscriber()
    with pytest.raises(NsError) as ei:
        CaptionsStage().run(ctx)
    assert ei.value.code == "NS-E403" and "drift" in ei.value.detail


# ---- spec 17.2: smart placement, emphasis, speakers, timing, contrast, profanity ------------------------


def _style(**kw):
    return caption_style("clean_white").model_copy(update=kw)


def test_free_bands_and_choose_margin_v():
    face = Box(0, 1000, 1080, 500)
    assert free_bands(1920, [face], 220, 320, 0, 1080) == [(220, 1000), (1500, 1600)]
    kw = {"h_px": 1920, "block_h": 220, "x_range": (70, 1010)}
    # the style's own place collides with the face: move to the largest free band, bottom aligned
    assert choose_margin_v(preferred_margin_v=576, avoid=[face], safe_zones=(220, 320), **kw) == 932
    # a region elsewhere leaves the preferred place alone
    far = Box(0, 100, 1080, 200)
    assert choose_margin_v(preferred_margin_v=576, avoid=[far], safe_zones=(220, 320), **kw) == 576
    # a region that does not overlap the caption columns is ignored
    side = Box(0, 1000, 30, 500)
    assert choose_margin_v(preferred_margin_v=576, avoid=[side], safe_zones=(220, 320), **kw) == 576
    # the bottom safe zone pushes a too-low caption up
    assert choose_margin_v(preferred_margin_v=100, safe_zones=(220, 320), **kw) == 332
    # nowhere to go: keep the preferred margin
    wall = Box(0, 0, 1080, 1920)
    assert choose_margin_v(preferred_margin_v=576, avoid=[wall], safe_zones=(220, 320), **kw) == 576


def test_build_ass_moves_anchor_away_from_face_and_is_unchanged_without_regions():
    plain = build_ass(_words(), "punch_yellow")
    assert ",2,70,70,576,1" in plain
    moved = build_ass(
        _words(), "punch_yellow", avoid_regions=[Box(0, 1000, 1080, 500)], safe_zones=(220, 320)
    )
    assert ",2,70,70,932,1" in moved
    assert plain == build_ass(_words(), "punch_yellow", emphasis=None, speakers=None)


def test_emphasis_word_gets_accent_colour_and_larger_size():
    st = _style(accent="#FF0000")
    ass = build_ass(_words(), st, emphasis={0: "Quiet"})
    assert "\\c&H0000FF&\\fscx115\\fscy115" in ass
    quiet_events = [ln for ln in ass.splitlines() if "quiet" in ln]
    assert quiet_events and all("\\fscx115" in ln for ln in quiet_events)
    assert "fscx115" not in build_ass(_words(), st)
    assert "fscx115" not in build_ass(_words(), st, emphasis={3: "quiet"})  # other sentence: no match


def test_speakers_get_palette_colours_and_breaks_split_groups():
    st = _style(palette=["#FFFFFF", "#00FF00"])
    ass = build_ass(_words(), st, speakers={0: 1, 1: 0})
    assert "{\\c&H00FF00&}" in ass
    words = [Word(t, i * 0.3, i * 0.3 + 0.2) for i, t in enumerate("a b c d e f g.".split())]
    style = caption_style("punch_yellow")
    groups = group_words(words, style, breaks_before={3})
    assert [w.w for w in groups[0]] == ["a", "b", "c"]
    assert all(not (set(map(id, g)) & {id(words[2])} and set(map(id, g)) & {id(words[3])}) for g in groups)


def test_group_timing_halves_long_pages_and_merges_short_neighbours():
    style = caption_style("clean_white")  # up to 4 words
    slow = [Word(f"w{i}", i * 1.0, i * 1.0 + 0.3) for i in range(4)]
    grouped = group_words(slow, style, max_pause_s=2.0)
    assert [[w.w for w in g] for g in grouped] == [["w0", "w1"], ["w2", "w3"]]
    quick = [Word("a", 0.0, 0.2), Word("b", 0.3, 0.5)]
    assert _merge_pages([[quick[0]], [quick[1]]], 3, 0.9, 2.2) == [quick]
    assert _merge_pages([[quick[0]], [quick[1]]], 1, 0.9, 2.2) == [[quick[0]], [quick[1]]]  # word limit wins
    long_ = [[Word("a", 0.0, 0.2)], [Word("b", 3.0, 3.2)]]
    assert len(_merge_pages(long_, 3, 5.0, 2.2)) == 2  # merging may not exceed the 2.2 s maximum


def test_contrast_helpers_and_boxed_fallback():
    assert contrast_ratio(1.0, 0.0) == pytest.approx(21.0)
    assert relative_luminance("#000000") == 0.0 and relative_luminance("#FFFFFF") == pytest.approx(1.0)
    st = caption_style("clean_white")
    assert ensure_contrast(st, [20.0, 30.0]) is st  # white on dark: fine
    boxed = ensure_contrast(st, [235.0, 40.0])  # one bright backdrop frame is enough
    assert boxed.box and boxed.vertical.outline >= 12 and not st.box
    assert ensure_contrast(boxed, [235.0]) is boxed
    assert re.search(r",3,12", build_ass(_words(), st, backdrop_luma=[235.0]))
    assert re.search(r",1,5", build_ass(_words(), st, backdrop_luma=[20.0]))


@pytest.mark.skipif(not ff.available(), reason="ffmpeg required")
def test_sample_luma_reads_backdrop_brightness(tmp_path: Path):
    clip = ff.make_test_clip(tmp_path / "grey.mp4", width=320, height=180, seconds=1.0, kind="color",
                             color="0x808080")
    ys = sample_luma(clip, Box(10, 10, 100, 100), [0.1, 0.5])
    assert len(ys) == 2 and all(100 < y < 150 for y in ys)


def test_profanity_masking():
    assert "shit" in profanity_words()
    assert mask_text("What the shit, damn it.") == "What the s***, d*** it."
    words = [Word("Oh", 0.0, 0.2), Word("shit", 0.3, 0.5)]
    assert "s***" in build_ass(words, "clean_white", mask_profanity=True)
    assert "shit" in build_ass(words, "clean_white")


def test_captions_align_a_russian_script(tmp_path: Path):
    """Review row 13: script_words returned nothing for non-ASCII text, so there were no captions."""
    text = "Это очень длинная история. Дверь открылась сама."
    ws = script_words(text)
    assert [w for w, _ in ws] == ["Это", "очень", "длинная", "история.", "Дверь", "открылась", "сама."]
    assert [s for _, s in ws] == [0, 0, 0, 0, 1, 1, 1]
    wav = tmp_path / "p1.wav"
    FakeTts().speak(text, "Ryan", 1.0, wav)
    wav.with_suffix(".txt").write_text(text, encoding="utf-8")
    res = align_words(text, FakeTranscriber().words(wav))
    assert res.matched == res.total == 7 and res.drift_ms <= 1.0


def test_cjk_characters_are_separate_caption_words():
    assert [w for w, _ in script_words("夜中にドアが開いた。")] == [*"夜中にドアが開い", "た 。"]
