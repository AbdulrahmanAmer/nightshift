# M3 C8 Clipper Implementation Plan

> **For agentic workers:** Execute task by task; steps use checkbox syntax. Nothing runs on the authoring laptop (ENGINEERING.md section 1, rule 13): "Run" means commit, push, read CI. A task is done only with a green CI run URL.

**Goal:** `studio clip <video> --channel <slug>` turns one user-owned long video into vertical 1080x1920 clips (captions, layout template, QC, Outbox, sheets), runnable offline with `--fake-services` on a synthetic fixture.

**Architecture:** `ns_studio/clipper/` is a set of pure, individually testable modules (models, selection post-processing, detection, layout, render graph builders) plus thin stage classes in `stages_clip.py` that wire them to C5 (`call_json`), C6 (captions, QC, outbox) and C7 (`JobContext`, stage registry). Heavy dependencies (OpenCV, YuNet) are reached through lazy `importlib` imports; every module imports cleanly in CI without them. A pure-numpy fallback covers the facecam scan so the gate runs without OpenCV.

**Tech Stack:** Python 3.12, pydantic v2, numpy, PyYAML, typer, FFmpeg filter graphs via `ns_common.ffmpeg.run`, optional `opencv-python-headless` (YuNet `FaceDetectorYN`, Canny).

**Spec:** BUILD_SPEC.md section 19 (all), 15.6 (schemas), 7.6-7.7, 9.2, 17 (caption styles), 22 J4. Process: docs/ENGINEERING.md sections 2, 4, 5, 6, 8.

**Contracts pin:** `main` at c33275c (no `contracts-m3` tag yet). Local pydantic models for `transcript`, `clip_plan`, `clip_layout`, `clip_template` live in `ns_studio/clipper/models.py` until CR-C8-1 is accepted.

## Global Constraints

- Owned paths only: `ns_studio/clipper/`, `ns_studio/presets/clip_templates.yaml`, `tests/test_clipper_*.py`, `tests/fixtures/clipper/`, plus `dev/gate_m3_clipper.py` and this plan. Contracts changes are CRs.
- Output: 1080x1920, 30 fps, AAC 192k 48 kHz, faststart, loudnorm to -14 LUFS (two-pass), encoder from `runtime.video`.
- Clip bounds: `min_s` 20 to `max_s` 60 (configurable), start spacing >= 60 s, never overlapping, filler filter (< 2.2 words/s or > 40 % filler tokens dropped), edges snapped to sentence boundaries.
- Face rules: YuNet score >= 0.8, stable = present in >= 70 % of sampled frames within +-15 % of frame width. Facecam: rectangle edges persistent in >= 8/10 frames, aspect 4:3 to 16:9, area within [min, 30] % (min default 5 %, see Deviations), corner preferred.
- Error codes: NS-E405 (source), NS-E406 (< 30 words), NS-E407 (no candidates), NS-W402 (template fallback). No new codes.
- Tests skip when FFmpeg or OpenCV is unavailable (`ns_common.ffmpeg.available()`, `pytest.importorskip("cv2")`).
- Deviations and change requests are collected at the bottom of this file.

## File structure

```
ns_studio/clipper/
  __init__.py  models.py  source.py  transcribe_stage.py  select.py  detect.py  layout.py
  render.py  seo.py  stages_clip.py  cli.py
ns_studio/presets/clip_templates.yaml
tests/fixtures/clipper/make_sample.py   (sample.mp4 + sample.transcript.json generator; llm/clip_select.json, llm/clip_seo.json)
tests/test_clipper_{models,fixture,select,detect,layout,source,transcribe,render,seo,stages,cli,gate}.py
dev/gate_m3_clipper.py
```

---

### Task 1: Models, fixture generator

**Files:** `ns_studio/clipper/{__init__,models}.py`, `tests/fixtures/clipper/make_sample.py`, `tests/fixtures/clipper/llm/*.json`, `tests/test_clipper_models.py`, `tests/test_clipper_fixture.py`

**Interfaces (produces):**
```python
class Rect(x, y, w, h: int)                      # pixels, source coordinates
class Transcript(source, duration_s, language, segments[], words[])   # spec 15.6
class ClipItem(k, start_s, end_s, title, hook_line?, why, score, speakers?, keywords)   # clip_plan item
class ClipLayout(template, facecam?, faces[{t, boxes[]}], crops[{t_start, t_end, region, region_b?, speaking?}])
class ClipTemplate(...)                          # one entry of clip_templates.yaml
class ClipParams                                 # dataclass persisted as clip_params.json
make_sample(out_dir) -> SamplePaths(video, transcript, truth)   # truth = facecam rectangle in source pixels
```

- [x] Tests first: model round-trips, bounds (`score` 0-1, `end > start`), fixture transcript has 6 segments, words cover 90 s, truth rect aspect 16:9; ffmpeg-gated test builds `sample.mp4` and probes 1280x720, 90 s, one audio stream.
- [x] Implement, commit `feat(c8): clipper models and synthetic fixture generator`.

### Task 2: Selection (window, merge, snap, filter, spacing)

**Files:** `ns_studio/clipper/select.py`, `tests/test_clipper_select.py`

**Interfaces:**
```python
def make_windows(duration_s, window_s=720.0, overlap_s=120.0) -> list[tuple[float, float]]
def extract_items(raw) -> list[dict]                          # tolerant: list | {"clips": [...]} | {"candidates": [...]}
def merge_overlaps(items) -> list[Cand]
def snap_to_sentences(c, words, *, min_s, max_s, duration_s) -> Cand | None
def passes_filler_filter(c, words) -> bool
def pick_top(cands, *, max_clips, spacing_s) -> list[Cand]
def select_clips(transcript, params, ask) -> list[ClipItem]   # ask(window_text, w_start, w_end) -> raw; raises NS-E407
```
- [x] Tests: windows of a 30 min file; overlap merge keeps the best title and the union; snapping moves edges onto sentence ends; too long/short dropped; filler (sparse and "um/like") dropped; spacing keeps higher score; none left -> `NS-E407`; re-run with other params changes the result without calling the LLM twice for identical windows (cache by window).
- [x] Implement, commit `feat(c8): clip selection post-processing`.

### Task 3: Detection

**Files:** `ns_studio/clipper/detect.py`, `tests/test_clipper_detect.py`

**Interfaces:**
```python
class FaceDetector(Protocol): def detect(self, frame_bgr: ndarray) -> list[Box]
class YuNetDetector(model_path)      # cv2.FaceDetectorYN, lazy
class FakeFaceDetector(script)       # for tests
def sample_frames(video, times, *, width=640) -> list[Frame]            # ffmpeg raw gray/bgr via files
def cluster_faces(face_frames, frame_w, n_frames) -> list[FaceTrack]    # stable faces
def scan_facecam(gray_frames, *, params) -> Rect | None                 # numpy gradient edges or cv2.Canny
def speaking_scores(gray_frames, times, track) -> list[float]
def decide_template(requested, facecam, stable_faces) -> tuple[str, str | None]  # (template, warning code or None)
def run_detection(video, ranges, duration_s, detector, params) -> DetectResult
```
- [x] Tests on synthetic numpy frames (rectangle with moving blob, clutter, no-rect negative), cluster stability thresholds, template decision table incl. NS-W402, ffmpeg-gated fixture scan within 5 %.
- [x] Implement, commit `feat(c8): detection - frames, faces, facecam scan, speaking`.

### Task 4: Templates and layout engine

**Files:** `ns_studio/presets/clip_templates.yaml`, `ns_studio/clipper/layout.py`, `tests/test_clipper_layout.py`

**Interfaces:**
```python
def load_templates() -> dict[str, ClipTemplate]
def plan_layout(template, source_w, source_h, layout: ClipLayout, *, t_start=0.0, t_end=None) -> list[Crop]
def smooth_track(samples, crop_w, ...) -> list[tuple[float, float]]    # dead-band re-centre, 1 s ramps
```
- [x] Tests: six templates load and satisfy safe margins; gaming layout cam height <= 640 and game region excludes cam; stacked yields two 1080x960 panels in bounds; tracked crop stays inside the frame and only moves when the face leaves the middle 40 %.
- [x] Implement, commit `feat(c8): clip templates and layout engine`. Push, CI.

### Task 5: Source import, audio, transcribe stage

**Files:** `ns_studio/clipper/{source,transcribe_stage,deps}.py`, `tests/test_clipper_source.py`, `tests/test_clipper_transcribe.py`

- [x] Import: copy to `imports/clipper/<source_id>/source.<ext>` (+ sidecar transcript), ffprobe, sha256, reject > 4 h or no audio (`NS-E405`), `sources` row, `source.json` (includes `rights_confirmed`). 16 kHz mono WAV extract.
- [x] Transcribe: sidecar mode, speaker-change heuristic (pause >= 1.2 s or question to answer), `NS-E406` below 30 words, writes `transcript.json`.
- [x] Commit `feat(c8): source import and transcription stage`.

### Task 6: Render

**Files:** `ns_studio/clipper/render.py`, `tests/test_clipper_render.py`

- [x] Filter graph builders per template (vstack, crop+scale, blurred fill, facecam overlay, border for speaker), `ass=` burn, two-pass loudnorm, encoder args from runtime; `render_clip(...)` writes `clips/c<k>.mp4`.
- [x] ffmpeg-gated tests: every template renders 1080x1920 from the fixture with a layout built by `plan_layout`.
- [x] Commit `feat(c8): per-template FFmpeg render`.

### Task 7: SEO, stages, CLI, gate

**Files:** `ns_studio/clipper/{seo,stages_clip,cli}.py`, `dev/gate_m3_clipper.py`, `tests/test_clipper_{seo,stages,cli,gate}.py`

- [x] SEO: `call_json("clip_seo")`, finalize per spec 9.2, metadata-only policy checks.
- [x] Stages import, audio, transcribe, select, detect, captions, render, seo, qc, outbox; `FORMAT_STAGES["clip"]`; `ClipParams` in `clip_params.json`.
- [x] CLI `ns_studio.clipper.cli.app` (mounted by C7), gate script, tests.
- [x] Merge `origin/main`, replace `deps.py` shims with direct imports, remove skips.
- [x] Commit per sub-task; open PR when green.

---

## Deviations and change requests

Filled in as they arise; mirrored in `contracts/CHANGE_REQUESTS.md` when that file exists on `main`.

- **Facecam area bound.** Spec 19.2 says 8-30 % of frame area; the prescribed fixture facecam (320x180 in 1280x720) is 6.25 %. The scan takes `min_area_frac` as a parameter, default 0.05. Spec change proposed.
- **CR-C8-1** Move `transcript`, `clip_plan`, `clip_layout`, `clip_template` models into `ns_common/models.py` (+ exported schemas).
- **CR-C8-2** Add `opencv-python-headless` to the `dev` and `studio` extras (numpy is already a dependency).
- **CR-C8-3** `sources` table needs `rights_confirmed INTEGER` and `title TEXT` (spec 19.1 stores the confirmation on the row).
- **CR-C8-4** `metadata.kind` gains `"clip"`; add `source_video` and `template` fields (spec 15.6).
- **CR-C8-5** C7 `ns_studio/cli.py` mounts `ns_studio.clipper.cli.app` as `studio clip`; `JobContext.fmt` literal gains `"clip"`.
- **CR-C8-6** `clip_layout.crops[]` gains optional `region_b` and `speaking` (second panel and speaking index for `podcast_stacked`).

### Outcome notes (builder)

- `deps.py` shims were used while C5/C6 were unmerged and removed after they landed (direct imports of `ns_studio.text.prompts.call_json` and `ns_studio.media.{captions,qc,outbox,transcribe}`). The local error-code shim was removed once `errors.json` listed E405-E407 and W402.
- CLI deviation: `studio clip` is a Typer app whose callback takes the video; a bare `--fake-services` needs the root group's normalisation (C7) or `main()` here. CR-C8-5 is the mount request.
- `--confirm-rights` flag (or an interactive prompt) records the rights confirmation; it is stored in `source.json` until CR-C8-3.
- Speaker heuristic toggles between two indexes (0/1); it is a heuristic, the visual refinement is deferred (spec 19.2).
- `main` is red since the C5 and C6 merges (5 tests in test_text_stages, test_media_pipeline, test_voice_stage disagree on `FORMAT_STAGES["shorts_only"]` order). Not caused by C8; no C8 test is in that failure set.
