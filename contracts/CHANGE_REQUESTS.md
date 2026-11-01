# Change requests to frozen contracts

Format per entry:

## CR-<n>: <short title>
- **From:** WP-<letter> / task
- **Change:** what to add or alter (exact names, types)
- **Why:** the concrete need
- **Status:** open | applied in <commit> | rejected (<reason>)

(none yet)

## CR-C4-1: fastapi (and httpx test client) in the `dev` extra
- **From:** C4 ns_tts / Task 4
- **Change:** add `"fastapi>=0.115"` to `[project.optional-dependencies].dev` in `pyproject.toml`. `numpy` is already a core dependency, so no change for it. After it lands, delete the `# pyright: reportMissingImports=false` line in `ns_tts/server.py` and the `pytest.importorskip("fastapi")` in `tests/test_tts_server.py`.
- **Why:** CI installs only `.[dev]`; without fastapi the server tests are skipped and pyright cannot resolve the imports. `uvicorn` is not needed in CI (only `ns_tts.server.main` imports it, lazily).
- **Status:** open

## CR-C4-2: `TtsCfg.engine` must accept `"qwentts"`
- **From:** C4 ns_tts / Task 3
- **Change:** in `ns_common/models.py` change `TtsCfg.engine` to `Literal["qwentts", "reference"]` with default `"qwentts"` (spec 15.4 `/health` engine names; the old `"faster"` engine was dropped). `select_engine` already treats a legacy `"faster"` value as qwentts, so the change is backward compatible. `Runtime.default("cpu-dev")` keeps `engine="reference"`. Also update the tests that assert the default.
- **Why:** with the current Literal a config cannot name the default engine, so `ns verify` cannot write `qwentts`/`reference` choices symmetrically (NS-W301 fallback writes `reference`; the happy path writes nothing).
- **Status:** applied on main (engine literal qwentts|reference|faster, default qwentts; NS-E310/E311 added)

## CR-C4-3: allow `subprocess` in `ns_tts/engine.py` once TID251 is enabled
- **From:** C4 ns_tts / Task 3
- **Change:** when `ruff` gets `TID251` banning `subprocess` (ENGINEERING.md section 8), add `ns_tts/engine.py` to the per-file-ignores list and to the hygiene-test allow-list, next to `ns_common/ffmpeg.py`.
- **Why:** `QwenttsEngine` runs the `qwentts.exe` CLI with `subprocess.run` (list args, never `shell=True`, with a timeout), as spec 6.1 describes.
- **Status:** open

## CR-C4-4: TTS-specific error codes
- **From:** C4 ns_tts / Task 3
- **Change:** add to `contracts/errors.json`: `NS-E31x` "The TTS engine failed (qwentts exited, timed out, or produced no audio)" and "A stored voice embedding is missing or was made by another engine". Until then `ns_tts` raises `NS-E202` (engine process/model failure) and `NS-E301` (bad voice data), whose messages do not fit well.
- **Why:** error messages shown to the operator should name TTS, not the language model.
- **Status:** applied on main (engine literal qwentts|reference|faster, default qwentts; NS-E310/E311 added)

## CR-C4-5: optional `cliff_lines` on `TtsItem`
- **From:** C4 ns_tts / Task 4
- **Change:** add `cliff_lines: tuple[str, ...] = ()` to `ns_common.tts_client.TtsItem` and send it as `cliff_lines` in `TtsClient.speak_batch` / `speak` bodies (the server already accepts the additive field). C6's voice stage fills it from `story.json`.
- **Why:** spec 7.5 wants 1,200 ms before a cliff line; the spec 15.4 request has no field to carry which lines are cliffs.
- **Status:** open

## CR-C8-0: register the clipper error codes in `errors.json`
- **From:** C8 Clipper / Task 1
- **Change:** add `NS-E405` (unsupported source: too long, no audio, unreadable, or rights not confirmed), `NS-E406` (transcription produced fewer than 30 words), `NS-E407` (no clip candidates passed the filters) and `NS-W402` (requested template not applicable, fell back to center_crop). Texts are in `ns_studio/clipper/errors.py`.
- **Why:** spec 19.7 defines them but `contracts/errors.json` lacks them, so `NsError` raised `KeyError`. C8 registers them at import (`setdefault`) until this lands; then `ns_studio/clipper/errors.py` is deleted.
- **Status:** open

## CR-C8-1: move the clipper schemas into `ns_common`
- **From:** C8 Clipper / Task 1
- **Change:** move `Transcript`, `Segment`, `Word`, `ClipItem` (`clip_plan` item), `ClipLayout` (+ `Crop`, `FaceFrame`, `Rect`) and `ClipTemplate` from `ns_studio/clipper/models.py` to `ns_common/models.py`, register them in `SCHEMA_MODELS` and export `transcript`, `clip_plan`, `clip_layout`, `clip_template` schemas.
- **Why:** spec 15.6 lists them as contracts (also read by C9). They are local pydantic models for now.
- **Status:** open

## CR-C8-2: OpenCV in the `dev` and `studio` extras
- **From:** C8 Clipper / Task 3
- **Change:** add `"opencv-python-headless>=4.9"` to `[project.optional-dependencies].dev` and `.studio` in `pyproject.toml` (`numpy` is already core).
- **Why:** with it CI runs the YuNet/Canny paths (`pytest.importorskip("cv2")` tests are skipped without it). The YuNet ONNX also needs a `manifest.json` pin (`models\cv\yunet.onnx`) so the installer downloads it.
- **Status:** open

## CR-C8-3: `sources` row stores the rights confirmation
- **From:** C8 Clipper / Task 5
- **Change:** `contracts/schema.sql` `sources` gains `rights_confirmed INTEGER NOT NULL DEFAULT 0` and `title TEXT`.
- **Why:** spec 19.1 says the owner's rights confirmation is stored on the `sources` row; the table has no column. C8 keeps it in `imports/clipper/<source_id>/source.json` and the job's `source.json` meanwhile.
- **Status:** open

## CR-C8-4: `metadata.kind` accepts `"clip"`
- **From:** C8 Clipper / Task 7
- **Change:** `Metadata.kind` becomes `Literal["long","short","clip"]` and gains optional `source_video: str | None` and `template: str | None` (spec 15.6).
- **Why:** clip upload sheets use `ClipMetadata` (local) until then.
- **Status:** open

## CR-C8-5: mount `studio clip`
- **From:** C8 Clipper / Task 7
- **Change:** in `ns_studio/cli.py` add `app.add_typer(ns_studio.clipper.cli.app, name="clip")` (the entry is `ns_studio.clipper.cli.app`; it takes `studio clip <video> --channel <slug> [--template ...] [--max-clips N] [--min-s S] [--max-s S] [--confirm-rights] [--reselect <job_id>] [--fake-services[=list]]`). `JobContext.fmt` Literal gains `"clip"`; `orchestrator.FORMAT_STAGES` gets `"clip"` from `ns_studio.clipper.stages_clip` (set at import).
- **Why:** spec 15.9. C8 never edits C7 files.
- **Status:** open

## CR-C8-6: optional fields on `clip_layout`
- **From:** C8 Clipper / Task 4
- **Change:** `crops[]` gains optional `region_b` (second panel: camera, or the lower speaker) and `speaking` (0 top, 1 bottom); `faces[]` gains optional `speaking`; `clip_layout` gains optional `note` (NS-W402 fallback text).
- **Why:** `gaming_facecam`, `screen_plus_cam` and `podcast_stacked` need two source regions per time slice.
- **Status:** open

## CR-C8-7: facecam area lower bound
- **From:** C8 Clipper / Task 3
- **Change:** spec 19.2 C5/19.4 say 8-30 % of the frame; the prescribed fixture facecam (320x180 in 1280x720) is 6.25 %. C8 uses 5-30 % (`FacecamParams.min_area_frac`). Spec text should say 5 %.
- **Why:** small overlay cams are common (about 3-8 % of a 1080p stream frame).
- **Status:** open
