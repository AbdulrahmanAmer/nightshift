# Nightshift M1 — C6 Media (voice, captions, clips, render, QC, outbox) Implementation Plan

> **For agentic workers:** Execute task by task. Every "Run" step means: commit, push, `gh run list --branch feat/c6-media --limit 1`, `gh run view <id> --log-failed`, fix, repeat. Nothing runs on the authoring laptop.

**Goal:** A fake-services job (`FakeTts`, `FakeTranscriber`, `FakeClipSource`, real FFmpeg) turns `parts.json` + `visual_plan.json` + `seo.json` into a captioned 1080x1920 Short that passes QC and lands in the Outbox with a valid `metadata` JSON and an upload sheet.

**Branch:** `feat/c6-media` from `origin/main`. Component C6 owns `ns_studio/media/`, `ns_studio/presets/` and the `tests/test_{voice_stage,transcribe,captions,clip_sources,clips,render,qc,outbox,thumbnails}.py` files plus `tests/fixtures/clips/`.

**Spec:** BUILD_SPEC.md §2 (stack), §7.4 (clip budget), §7.5 (TTS chunking and loudness), §7.6 (caption alignment), §7.7 (QC), §9.2 (Shorts SEO), §15.4/15.8 (TTS API, job dir), §17 (presets), §18.5 (retention). Interfaces consumed: Task 1 of `_s1_part1.md` (context, stages, orchestrator), Task 7 of `_s1_part3.md` (voice stage).

## Global Constraints

- Shorts: 1080x1920, 30 fps, AAC 192k 48 kHz, `+faststart`; encoder from `runtime.video` (NVENC with libx264 retry, `NS-W401`).
- Loudness: voice items -16 LUFS / -1.5 dBTP; final audio -14 LUFS / -1.5 dBTP (two-pass `loudnorm`, then a gain touch-up when off by > 0.5 LU). QC accepts -14 +-1 LUFS, TP <= -1.
- Captions: words come from the script, never ASR; 2-4 words on screen; first caption at 0.0 s; median drift <= 120 ms else `NS-E403`.
- Clips: one search per unique keyword per source per orientation (page 80); Pexels 180/h, Pixabay 90/min token buckets; Pixabay responses cached 24 h; clip cache 90 days; no clip twice in a video, 14 days per channel, 30 days across channels; shot length 1.5-3 s; dark moods skip clips whose tags/title mention faces; `NS-E402` after a mood-only fallback search.
- Heavy imports (`faster_whisper`) are lazy; HTTP only in `clip_sources.py`; subprocesses only through `ns_common.ffmpeg`; all errors are `NsError`; logging through `NsLogger`.
- Stage classes are annotated `name: str`, `tenant: Tenant`, `inputs/outputs: tuple[str, ...]` so pyright accepts them as `Stage`.
- FFmpeg-dependent tests carry `pytest.mark.skipif(not ff.available(), ...)`.

## File structure

```
ns_studio/media/  audio.py voice.py transcribe.py captions.py presets.py shotplan.py
                  clip_sources.py clips.py render.py qc.py outbox.py stages_media.py
ns_studio/presets/ caption_styles.yaml visual_treatments.yaml fonts/README.md
tests/  test_voice_stage.py test_transcribe.py test_captions.py test_clip_sources.py test_clips.py
        test_render.py test_qc.py test_outbox.py  fixtures/clips/{pexels_search,pixabay_search}.json
```

Job files: `audio/{manifest.json,p<n>.wav,p<n>.txt,p<n>_hook.*}`, `words/p<n>[_hook].json`, `captions/s<k>.{ass,json}`, `captions/report.json`, `clips/shots.json`, `shorts/s<k>/{audio.wav,short.mp4}`, `render/report.json`, `qc/{s<k>.json,report.json}`, `outbox/manifest.json`.

---

### Task 7: Voice stage (as in `_s1_part3.md` Task 7, plus `audio.py`)

**Files:** `ns_studio/media/audio.py`, `voice.py`, `stages_media.py`, `tests/test_voice_stage.py`
**Interfaces:** `narration_items`, `loudnorm` (two-pass), `measure_lufs`; `audio.assemble_audio`, `speech_onset_s`, `parse_ebur128`; `VoiceStage` writes `audio/manifest.json` with `{path, seconds, lufs, tp, sig}` and a `<id>.txt` script sidecar; re-run with unchanged text is a no-op (signature match).
- [ ] Tests written (items, normalization range, sidecar, second run does not call TTS)
- [ ] Implementation, push, CI green

### Task 8: Transcribe and captions

**Files:** `transcribe.py`, `captions.py`, `presets.py`, `presets/caption_styles.yaml`, `tests/test_transcribe.py`, `tests/test_captions.py`
**Interfaces:**
```python
@dataclass(frozen=True) class Word: w; start; end; sentence_idx=0   # to_json/from_json
class Transcriber(Protocol): name; words(wav, language) -> list[Word]; segments(wav, language) -> list[Segment]
class FakeTranscriber            # script words from <wav>.txt spread evenly over the WAV; NS-E403 without sidecar
class WhisperTranscriber(cfg, paths)   # lazy faster_whisper, model dir paths.models_whisper/<cfg.model>, VAD on
def ensure_gpu_dlls_on_path(search_roots=None) -> list[Path]   # local copy; CR-1 asks to move to ns_common
def align_words(script, asr, *, total_s=None) -> AlignResult   # difflib over normalize_tokens, interpolation
def build_ass(words, style, *, variant="vertical"|"landscape") -> str
class CaptionsStage  # tenant whisper; NS-E403 if drift > 120 ms or < 60 % words matched
```
- [ ] Tests: alignment (exact, substitution, insertion, numbers, interpolation), drift, ASS (group sizes, first event 0.0, active colour, both variants), whisper with a stubbed module, DLL path injection, stage end to end on fakes
- [ ] Implementation, push, CI green

### Task 9: Clip sources and cache

**Files:** `clip_sources.py`, `clips.py` (cache part), `tests/test_clip_sources.py`, `tests/fixtures/clips/*.json`
**Interfaces:** `ClipCandidate`, `ClipSource` protocol (`search(keyword, orientation=, per_page=)`, `download(cand, dest)`), `TokenBucket(capacity, refill_per_s, clock, sleep)`, `PexelsSource(key, cache_dir)` (Authorization header, `orientation`, `size=medium`, 429 handling), `PixabaySource(key, cache_dir)` (24 h cache, ratio filter), `FakeClipSource(root)`; `ClipCache.ensure(cand, keywords)` with the `clips` table and 90-day expiry.
- [ ] Tests with `httpx.MockTransport` and recorded JSON; bucket with injected clock; 24 h cache hit/expiry; `NS-E501/E502/E503`
- [ ] Implementation, push, CI green

### Task 10: Clips stage

**Files:** `clips.py`, `shotplan.py`, `tests/test_clips.py`
**Interfaces:** `normalize_keyword`, `dedupe_keywords`, `blocked_clips(db, channel, job_id, now)`, `Searcher` (memoized), `pick_for_slots`, `select_for_beat` (orientation widening, mood fallback, `NS-E402`), `assign_clips`; `beat_spans`, `plan_slots`, `split_span`, `frame_edges`; `ClipsStage` writes `clips/shots.json`.
- [ ] Tests: dedupe, 14/30-day rules, face skip, fallback and `NS-E402`, slot lengths 1.5-3 s, one search per keyword
- [ ] Implementation, push, CI green

### Task 11: Vertical render

**Files:** `render.py`, `presets/visual_treatments.yaml`, `tests/test_render.py`
**Interfaces:** `build_vertical_graph`, `shot_chain`, `post_chain`, `encoder_args`, `render_video`, `build_short_audio` (hook + 150 ms + body, normalized to -14), `RenderStage`.
- [ ] Tests: graph string content per treatment and clip shape; real render of a 3-shot clip set (size, duration, audio stream, faststart); NVENC fallback
- [ ] Implementation, push, CI green

### Task 12: QC and outbox

**Files:** `qc.py`, `outbox.py`, `tests/test_qc.py`, `tests/test_outbox.py`
**Interfaces:** `run_qc(item, video, kind, ...) -> QcResult`, checks per §7.7, `QcStage` (`NS-E404`); `publish_after`, `build_metadata`, `write_sheet_txt`, `OutboxStage` (copies to `paths.outbox_dir`, writes `outputs` and `clip_usage`).
- [ ] Tests: each check passing and failing (silence, loud, black video, wrong size, missing faststart), metadata validates against `contracts/schemas/metadata.schema.json`, jitter within +-45 min and deterministic, outputs/clip_usage rows, idempotent re-run
- [ ] Implementation, push, CI green

### Task 13: End-to-end media pipeline test

**Files:** `tests/test_media_pipeline.py`
Runs `voice, captions, clips, render, qc, outbox` through `run_job` on fakes and asserts the Outbox files.
- [ ] Test, push, CI green

Registration: `stages_media.py` appends the six stage names to `FORMAT_STAGES["shorts_only"]` in order `voice, captions, clips, render, qc, outbox`.
