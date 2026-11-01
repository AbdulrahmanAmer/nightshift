# Nightshift M2 — C6 Media (master-first: landscape render, chapters, thumbnails, derived Shorts) Plan

> **For agentic workers:** Same execution rules as `m1-c6-media.md`: commit, push, read CI, fix. Nothing runs on the authoring laptop. Starts after the M1 media code is green.

**Goal:** For `fmt == "master_first"`, voice and caption each chapter, enforce the 9:00 floor with C5's expansion loop, render a 1920x1080 master with chapter title cards, write `chapters.json` and mid-roll suggestions, make three thumbnails, derive 4-6 vertical Shorts (audio cut + voiced hook + closing line, fresh vertical edit), QC both formats and publish `long/` + `shorts/` to the Outbox.

**Stage order registered into `FORMAT_STAGES["master_first"]` after C5's text stages:** `voice, duration_check, captions, shorts_timing, clips, render_master, thumbnails, shorts_derive, qc, outbox`. Names shared with `shorts_only` (`voice, captions, clips, qc, outbox`) are the same registered stage objects; each dispatches on `ctx.fmt`.

## Assumed upstream shapes (C5 owns them; any mismatch becomes a CR)

- `story.json`: `{"chapters": [{"n": 1, "title": str, "text": str}, ...], ...}` (loader also accepts `chapter` instead of `n`).
- `visual_plan.json`: beats with `chapter` (int), `beat`, `sentence_start`, `sentence_end` (sentence index within the chapter text), `keywords`, `mood`, `need_vertical`.
- `shorts_plan.json`: list of `{"k", "chapter", "sentence_start", "sentence_end", "hook", "closing_line", "length_bucket"}`; C6 adds `start_s`, `end_s` (master timeline) in `shorts_timing`.
- `seo_long.json`: Seo-shaped plus `chapter_titles`; `seo_shorts.json`: list of Seo-shaped, index k-1.
- C5 exposes `ns_studio.text.stages_long.expand_chapter(ctx, n) -> None` (rewrites chapter n in `story.json`, +120-200 words); imported lazily (CR-2).

## Global Constraints

- Master: 1920x1080, shot length 5-9 s (`long_cut_range_s`), landscape clips only (portrait never used), chapter title card <= 1.5 s with no sound between chapters, clean caption style on (landscape variant), runtime 8:30-15:00 at QC, planning floor 9:00 with at most 2 expansions then `NS-E305`.
- Mid-roll suggestions only at chapter breaks, none in the first or last 120 s.
- Master loudness -14 LUFS (chapters -16 then whole-master pass); chapter audio joined with 700 ms silence.
- Shorts derived from the master are freshly edited: vertical clips (not a crop of the master), own caption style and visual treatment, cuts every 1.5-3 s, voiced hook, closing line; audio 150 ms silence between hook and body.
- Descriptions follow §9.1 (chapter list, `00:00` first, >= 3 chapters, each >= 10 s) and §9.2 (Shorts description carries "From the full story: <master title>"); each Short's sheet says "Set Related video -> <master title>".
- Pillow is imported lazily (`importlib`); tests that draw use `pytest.importorskip("PIL")`; CR-3 asks to add `Pillow` to the `dev` extra so CI runs them.

## Tasks

### Task 14: Master voice, duration check and expansion loop
**Files:** `ns_studio/media/master.py`, `tests/test_master_voice.py`
**Interfaces:** `load_chapters(ctx)`, `chapter_items(...)`, `VoiceStage` master branch (items `c<n>`, `h<k>`, `x<k>`), `DurationCheckStage` (sum of chapter seconds + gaps; < 540 s -> `expand_chapter` on the shortest chapter, re-voice only that chapter, max 2, else `NS-E305`).
- [ ] Tests (floor passes; one expansion; two expansions then E305; only the changed chapter re-voiced)
- [ ] Implementation, push, CI green

### Task 15: Master captions and Shorts timing (M11b)
**Files:** `master.py` (captions branch), `shorts_timing` in `master.py`, `tests/test_master_captions.py`
**Interfaces:** `words/c<n>.json`, `captions/long.ass` (landscape, chapter-offset timeline), `captions/long.json`; `ShortsTimingStage` resolves sentence spans to `start_s/end_s`, snapped to sentence boundaries.
- [ ] Tests (timeline offsets with chapter gaps, span snapping, drift per chapter)
- [ ] Implementation, push, CI green

### Task 16: Landscape clips, chapter cards, render_master, chapters.json
**Files:** `presets/chapter_cards.yaml`, `ns_studio/media/longform.py`, `tests/test_longform.py`
**Interfaces:** `build_landscape_graph` (in `render.py`), `card_graph`, `chapters_json(...)`, `midroll_suggestions(chapters, total_s)`, `RenderMasterStage` (renders per chapter segment to stay under the Windows command length, then concat-copies).
- [ ] Tests (mid-roll rules, chapters.json timestamps equal ffprobe segment sums, card <= 1.5 s, real render of a 2-chapter fake master)
- [ ] Implementation, push, CI green

### Task 17: Thumbnails
**Files:** `presets/thumbnails.yaml`, `ns_studio/media/thumbnails.py`, `tests/test_thumbnails.py`
**Interfaces:** `pick_frames(video, n)` (luminance variance via FFmpeg `signalstats`/sampled frames), `make_thumbnails(frame_paths, text, layouts, out_dir)` (Pillow, <= 4 words, three layouts), `ThumbnailsStage`.
- [ ] Tests (word limit, three files at 1280x720, layout differences) behind `importorskip("PIL")`
- [ ] Implementation, push, CI green

### Task 18: Derived Shorts (S1, S2)
**Files:** `ns_studio/media/derive.py`, `tests/test_derive.py`
**Interfaces:** `cut_master_audio(...)`, `ShortsDeriveStage` per Short: audio cut + voiced hook + closing line -> `shorts/s<k>/audio.wav`, captions (hook, body from master words, closing aligned via transcriber), vertical clips for beats inside the span, render with the M1 renderer -> `shorts/s<k>/short.mp4`.
- [ ] Tests (audio duration = hook + gap + span + gap + closing; caption timeline; render of one derived Short)
- [ ] Implementation, push, CI green

### Task 19: QC for long and outbox for `long/` + `shorts/`
**Files:** `qc.py`, `outbox.py` (extensions), `tests/test_qc.py`, `tests/test_outbox.py`
**Interfaces:** `QcStage`/`OutboxStage` master branches; `long_description(...)` per §9.1; `shorts_description(...)` per §9.2; `long/master.{mp4,json,txt}`, `long/thumb_[1-3].jpg`, `shorts/s<k>.{mp4,json,txt}`.
- [ ] Tests (8:30-15:00 window, chapter rule, descriptions, related-video line, schema validation)
- [ ] Implementation, push, CI green

### Task 20: Master pipeline test
**Files:** `tests/test_master_pipeline.py` — all ten stages on fakes with a 9-minute fake story (FakeTts at 155 wpm), asserting the Outbox.
- [ ] Test, push, CI green
