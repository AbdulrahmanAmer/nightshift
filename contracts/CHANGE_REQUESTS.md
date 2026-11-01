# Change requests to frozen contracts

Format per entry:

## CR-<n>: <short title>
- **From:** WP-<letter> / task
- **Change:** what to add or alter (exact names, types)
- **Why:** the concrete need
- **Status:** open | applied in <commit> | rejected (<reason>)

(none yet)

## CR-C7-1: studio error codes NS-E320 to NS-E326
- **From:** C7 / m1-c7-studio-core Task 2 (also used by the M2 learn, review and cadence tasks)
- **Change:** add to `contracts/errors.json` (message and hint exactly as in the local shim `ns_studio/errors.py`, table `_LOCAL`):
  `NS-E320` The channel profile is invalid. · `NS-E321` Channels are not distinct enough. · `NS-E322` Unknown channel. · `NS-E323` The analytics CSV could not be read. · `NS-E324` Calibration data is missing or incomplete. · `NS-E325` The variation pools are exhausted. · `NS-E326` Output not found or not in a state that allows this action.
- **Why:** spec section 8 (pool minimums, distinctness), 16.5 (calibration), 18.9 (CSV import) and section 10 (review, mark-uploaded) have conditions with no code today; builders may not raise a code that is not in `errors.json`.
- **Workaround until applied:** `ns_studio.errors.StudioError` carries the same codes with a local message table and prefers `errors.json` when the code exists. When this CR is applied, empty `_LOCAL` and nothing else changes. Note: `tests` that scan source for `NS-E` literals will flag these codes until then.
- **Status:** open

## CR-C7-2: `policy_check` prompt, schema and fixture (owner of the files: C5)
- **From:** C7 / m2-c7-studio-core (policy gate)
- **Change:** C5 adds `ns_studio/prompts/policy_check.md` (front-matter `version: 1`, `thinking: true`, `schema: policy_check`), a `policy_check` entry in `PROMPT_SCHEMAS`, and a fixture `tests/fixtures/llm/policy_check.json` that passes every mode. C7 calls it as `call_json(ctx, "policy_check", mode=<mode>, ...)`.
  Template variables per `mode`:
  - `unsafe_topics`: `story` (str), `categories` (list of `{id, label, description, examples}` from `contracts/policy/unsafe_topics.yaml`), `audience` (`general|mature`).
  - `hook_title_honesty`: `story`, `items` (list of `{item, title, hook}`).
  - `standalone_short`: `story`, `moments` (list of `{k, text, hook}`).
  - `tag_relevance`: `story`, `items` (list of `{item, title, tags}`).
  Response schema (all modes, keys not relevant to a mode may be empty): `{"verdict": "pass"|"fail", "reason": str, "flags": [{"category": str, "reason": str}], "intensity": "low"|"medium"|"high", "failed_items": [str]}`. `flags[].category` is an id from `unsafe_topics.yaml`; `failed_items` holds item names (`long`, `s1`, ...) or Short numbers as strings.
- **Why:** spec section 18.8 checks `unsafe_topics`, `hook_title_honesty`, `standalone_short` and the tag relevance half of `metadata_spam` are judged by the LLM (section 16.1 lists `policy_check.md`, thinking on). C7 cannot add files to C5's directories.
- **Workaround until applied:** C7 imports `ns_studio.text.prompts` only through `importlib` inside `ns_studio/policy/judge.py`; with no `policy_check.md` present the CLI leaves the policy gate stages out of the plan (`policy.available()` is false) so `studio run --fake-services` keeps working.
- **Status:** open

## CR-C7-3: nothing new in pyproject (informational)
- **From:** C7
- **Change:** none. Perceptual hashing is implemented with FFmpeg and numpy (both already available), so `imagehash` and Pillow are not needed by C7. spaCy and keyring are only imported lazily from the existing `studio` extra.
- **Status:** informational
## CR-C4-1: fastapi (and httpx test client) in the `dev` extra
- **From:** C4 ns_tts / Task 4
- **Change:** add `"fastapi>=0.115"` to `[project.optional-dependencies].dev` in `pyproject.toml`. `numpy` is already a core dependency, so no change for it. After it lands, delete the `# pyright: reportMissingImports=false` line in `ns_tts/server.py` and the `pytest.importorskip("fastapi")` in `tests/test_tts_server.py`.
- **Why:** CI installs only `.[dev]`; without fastapi the server tests are skipped and pyright cannot resolve the imports. `uvicorn` is not needed in CI (only `ns_tts.server.main` imports it, lazily).
- **Status:** applied in fb54431 (fastapi, psutil, python-multipart, opencv-python-headless, Pillow, imagehash added to the dev extra; C4 may now drop the importorskip and the pyright pragma)

## CR-C4-2: `TtsCfg.engine` must accept `"qwentts"`
- **From:** C4 ns_tts / Task 3
- **Change:** in `ns_common/models.py` change `TtsCfg.engine` to `Literal["qwentts", "reference"]` with default `"qwentts"` (spec 15.4 `/health` engine names; the old `"faster"` engine was dropped). `select_engine` already treats a legacy `"faster"` value as qwentts, so the change is backward compatible. `Runtime.default("cpu-dev")` keeps `engine="reference"`. Also update the tests that assert the default.
- **Why:** with the current Literal a config cannot name the default engine, so `ns verify` cannot write `qwentts`/`reference` choices symmetrically (NS-W301 fallback writes `reference`; the happy path writes nothing).
- **Status:** applied on main (engine literal qwentts|reference|faster, default qwentts; NS-E310/E311 added)

## CR-C4-3: allow `subprocess` in `ns_tts/engine.py` once TID251 is enabled
- **From:** C4 ns_tts / Task 3
- **Change:** when `ruff` gets `TID251` banning `subprocess` (ENGINEERING.md section 8), add `ns_tts/engine.py` to the per-file-ignores list and to the hygiene-test allow-list, next to `ns_common/ffmpeg.py`.
- **Why:** `QwenttsEngine` runs the `qwentts.exe` CLI with `subprocess.run` (list args, never `shell=True`, with a timeout), as spec 6.1 describes.
- **Status:** applied in the C10 pyproject commit (per-file-ignore for ns_tts/engine.py; the hygiene test bans only shell=True, so it needs no allow-list)

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
- **Change:** add `NS-E405` (unsupported source: too long, no audio, unreadable, or rights not confirmed), `NS-E406` (transcription produced fewer than 30 words), `NS-E407` (no clip candidates passed the filters) and `NS-W402` (requested template not applicable, fell back to center_crop). 
- **Why:** spec 19.7 defines them but `contracts/errors.json` lacks them, so `NsError` raised `KeyError`.
- **Status:** applied on main (2a3d772); C8's local registration shim was removed

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
## CR-C2-1: fastapi in the `dev` extra (same need as CR-C4-1)
- **From:** C2 runtime / Task 5
- **Change:** add `"fastapi>=0.115"` to `[project.optional-dependencies].dev` in `pyproject.toml`. `httpx` is already a core dependency (the starlette `TestClient` and `SupervisorClient` use it), `uvicorn` is only imported by `python -m ns_supervisor.app` (lazily), and `psutil` is not used by C2 (process control is `subprocess` + Job Object + `taskkill`), so neither is requested. After it lands, delete the `# pyright: ignore[reportMissingImports]` comments in `ns_supervisor/app.py` and the `pytest.importorskip("fastapi")` in `tests/test_supervisor_app.py`.
- **Why:** CI installs only `.[dev]`; without fastapi the HTTP-layer tests (`tests/test_supervisor_app.py`) are skipped and pyright cannot resolve the imports. All supervisor logic is tested without it through `ns_supervisor.core`.
- **Status:** applied in fb54431 (fastapi, psutil, python-multipart, opencv-python-headless, Pillow, imagehash are in the dev extra)

## CR-C2-2: supervisor `POST /events` (publish a job event)
- **From:** C2 runtime / Task 5
- **Change:** add to spec 15.3: `POST /events {kind:"service"|"job", data:{...}}` publishes one event to every `GET /events` subscriber. Implemented additively in `ns_supervisor/app.py` and `SupervisorClient.publish_event`. Also additive: `GET /events?limit=N` closes after N events (test aid), `GET /health` returns `{ok:true}`, `/status` carries extra keys (`gpu_service`, `waiting`, `shutting_down`, `uptime_s`, and per service `owners`, `crashes`, `error`, `elapsed_s`).
- **Why:** the dashboard status strip needs job progress (spec 6.2), and only the studio process knows the stage; `/events` as specified is read-only.
- **Status:** open

## CR-C2-3: error codes the runtime tools need
- **From:** C2 runtime / Tasks 6-10
- **Change:** add to `contracts/errors.json`: `NS-E112` (faster-whisper cannot load on CUDA: cuBLAS 12 / cuDNN 9 DLLs; named in spec 15.12 and ENGINEERING 6 but missing from the file), `NS-E704` "The scheduled task could not be registered, removed or read" (hint: run PowerShell as the same user; see the detail), `NS-E705` "The file is not a valid Nightshift backup" (restore). Until then `ns verify` reports a failed whisper load as `NS-E202` with the cuBLAS/cuDNN fix in the detail, schedule errors are printed without a code, and `ns restore` raises `NS-E701`.
- **Why:** the operator should see a code that names the real problem.
- **Status:** applied on main (NS-E112, NS-E204 scheduled task, NS-E703 restore). C2 now uses them; the proposed NS-E704 and NS-E705 are withdrawn.

## CR-C2-4: `ruff` TID251 per-file ignores for the supervisor package
- **From:** C2 runtime / Task 2
- **Change:** when TID251 bans `subprocess` (ENGINEERING 8), add `ns_supervisor/*.py` to the per-file-ignores and to the hygiene allow-lists for subprocess and `httpx`. C2 starts services, runs `taskkill`, `nvidia-smi`, `uv`, `powershell`, `studio` and `git`.
- **Why:** section 8 names the supervisor as the place services are spawned; the listed exception paths do not yet include the extra tools.
- **Status:** applied in fb54431 (ruff per-file-ignore `ns_supervisor/**` for TID251; the hygiene test bans only shell=True and has no subprocess or httpx allow-list)

## CR-C2-5: per-model llama flags for two-model mode (`ns tune`)
- **From:** C2 runtime / Task 9
- **Change:** `Runtime.llama.flags` is shared by both aliases. To tune the `story` model separately, add `LlamaCfg.story_flags: LlamaFlags | None = None` (None means use `flags`) and regenerate the runtime schema. C2 will then tune each model and pass the right flags per alias.
- **Why:** spec 3 says tune runs per model in two-model mode; today `ns tune` tunes the primary (`code`) model only and says so.

## CR-C5-1: master-first text models and schemas in `ns_common.models`
- **From:** C5 story text / M2 Task 6
- **Change:** move these pydantic models (all `Strict`, `extra="forbid"`) from `ns_studio/text/models_local.py` into `ns_common/models.py`, add them to `SCHEMA_MODELS` and export `contracts/schemas/<name>.schema.json` (spec 15.6). Exact fields:
  - `plan`: `Plan{target_minutes:int>=9, target_words:int>=1, chapters:list[PlanChapter] (5..7)}`, `PlanChapter{n:int>=1, title:str, beats:list[str] (>=1), cliff:str, word_budget:int>=1}`.
  - `story`: `Story{working_title:str, chapters:list[StoryChapter] (>=1), revisions:int>=0 =0}`, `StoryChapter{n:int>=1, title:str, text:str, summary:str, word_count:int>=1, est_seconds:float>0, cliff_line_idx:int|None>=0 =None, sentences:list[Sentence] (>=1)}`, `Sentence{idx:int>=0, text:str}`.
  - `shorts_plan` (list of `ShortsPlanItem`): `{k:int>=1, chapter:int>=1, sentence_start:int>=0, sentence_end:int>=0, start_s:float|None, end_s:float|None, hooks:list[str] (1..5), chosen_hook:str, closing_line:str, length_bucket:int>=1, combo_id:str, why_standalone:str}`. Note: spec says `hooks[5]`; the model accepts 1..5 because hooks over 12 words are dropped in code.
  - `seo_long`: `SeoLong(Seo){chapter_titles:list[ChapterTitle] (>=1)}`, `ChapterTitle{n:int>=1, title:str}`.
  - `clip_plan` item `ClipPlanItem{k:int>=1, start_s:float>=0, end_s:float>0, title:str<=100, hook_line:str|None, why:str, score:float 0..1, speakers:list[str]|None, keywords:list[str]=[]}`; the `clip_select` prompt answers `{"clips":[ClipPlanItem]}`, the file `clip_plan.json` is the bare list.
  - `policy_check` (prompt answer, used by C7's policy gate): `PolicyCheckResult{unsafe_categories:list[str], hook_honest:bool, title_honest:bool, standalone:bool|None, notes:str}`; the ids in `unsafe_categories` are `id` values of `contracts/policy/unsafe_topics.yaml`.
- **Why:** C6 (voice, captions, render), C7 (policy gate, orchestrator) and C8 (clipper) read and write these files; the single source of truth must be `ns_common`. C5 uses local copies meanwhile, so nothing blocks.
- **Migration:** C5 replaces `from ns_studio.text.models_local import X` by `from ns_common.models import X` and deletes `models_local.py` once this lands; field names do not change.
- **Status:** open

## CR-C1-1: error codes NS-E111, NS-E112, NS-W112 are missing from errors.json
- **From:** C1 installer / stage1.ps1 and steps s01, s08
- **Change:** add to `contracts/errors.json`: `NS-E111` ("winget (Windows App Installer) is missing.", hint "Install 'App Installer' from the Microsoft Store (or use the direct installer links shown), then run setup again."), `NS-E112` ("faster-whisper cannot load on the GPU (cuBLAS 12 / cuDNN 9 DLLs not found).", hint "In the studio venv run: uv pip install nvidia-cublas-cu12 nvidia-cudnn-cu12, then re-run 'ns verify --quick'."), `NS-W112` ("Windows long paths are disabled.", hint "Run setup once as administrator to enable them, or keep NIGHTSHIFT_HOME on a short path such as D:/Nightshift.").
- **Why:** spec 1.2 / 15.12 and ENGINEERING.md section 6 name them; `describe()` raises KeyError for them today. The installer works around it with `installer/stage2/errors.py::make_error` (a pending table with the same text), which becomes a plain `NsError` as soon as the codes exist. `stage1.ps1` has the same texts as a fallback.
- **Status:** open

## CR-C1-2: Answers model and answers.schema.json lack the installer-only fields
- **From:** C1 installer / answers.example.json, s01_questions
- **Change:** add to `Answers`: `home_path: str | None = None`, `models_source: str | None = None`, `llm_tier: Literal["small","balanced","best"] = "small"`, `restore_from: str | None = None`; regenerate `answers.schema.json`. Voice stays as the model has it (`voice` plus flat `voice_ref_audio_path`, `voice_ref_text`, `voice_rights_confirmed`); spec 5 describes it as a nested object, the model is the source of truth.
- **Why:** spec 5 and 1.3 list these keys for unattended installs. `installer/stage2/answers.py::InstallerAnswers` subclasses `Answers` and adds them, and `tests/test_installer_files.py` accepts exactly these four keys as "not in the schema yet"; delete that allowance when this lands.
- **Status:** open

## CR-C1-3: record the chosen llama.cpp build, and Paths for bin/uv, bin/qwentts, models/cv
- **From:** C1 installer / s02_download_bins (coordinator decision: CUDA 13.4 pair when nvidia-smi reports CUDA >= 13.0, else the 12.4 pair)
- **Change:** `InstallState`: add `cuda: str | None` and `llama_pair: Literal["cuda13","cuda12"] | None`. `Runtime.llama`: add `build: Literal["cuda13","cuda12","cpu"] | None`. `Paths`: add `bin_qwentts`, `bin_uv`, `models_cv`.
- **Why:** InstallState and Runtime are `extra="forbid"`, so the installer keeps the pair in `config/wizard_flags.json` (`llama_pair`, `cuda`) for now. C2 could read the pair to pick DLL paths. `Paths` is missing three folders the spec lists (21.2, 21.5); the installer builds them from `paths.home` meanwhile.
- **Status:** open

## CR-C1-4: pins the installer needs in manifest.json / pyproject.toml
- **From:** C1 installer / s04_venvs, fallback.py
- **Change:** (a) manifest: spaCy model wheel `en_core_web_md` version and SHA256 (the installer hard-codes 3.8.0 and cannot verify the hash); (b) manifest or `studio` extra: `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` pins (the installer uses ranges `>=12.8,<13` and `>=9,<10`); (c) manifest: the PyTorch fallback HF repos (`Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice`, `-Base`) instead of constants in `installer/stage2/fallback.py`; (d) `studio` extra: `opencv-python-headless` (spec 21.4, needed from M3); (e) a Blackwell-capable CUDA 12.8 llama.cpp pin if one exists, so CUDA 12.4 to 12.7 drivers are not limited to the 12.4 build.
- **Why:** ENGINEERING.md DoD 6 (no unpinned dependency). The installer runs today with the ranges above.
- **Status:** open

## CR-C1-5: what the installer expects from the `ns` CLI (C2)
- **From:** C1 installer / s06, s08, s09, s10
- **Change:** none to contracts; confirm behaviour in the C2 plan. The wizard calls these from the studio venv (installed with extras `studio,supervisor` so `ns_supervisor` imports resolve there), with `NIGHTSHIFT_HOME` set and the app folder as cwd: `ns tune` (exit 0 on success; a failure only warns), `ns verify --quick` (JSON on stdout or in `logs/verify-report.json`: `{"checks":[{"name","passed"}]}` where names contain `llm`, `embed`, `tts`, `whisper`, `nvenc`, `mtp`), `ns doctor --bundle` (writes `nightshift-diagnostics-<date>.zip` to the Desktop), `ns schedule install --time HH:MM` (a "No such command" answer is tolerated until the scheduler exists), `ns restore <zip>`.
- **Why:** the smoke step applies NS-W401 / NS-W301 / NS-W302 / NS-E112 from those check names.
- **Status:** open

## CR-C6-1: move `ensure_gpu_dlls_on_path` into `ns_common`
- **From:** C6 media / transcribe.py
- **Change:** add `ns_common/gpu_dlls.py` with `ensure_on_path(search_roots=None) -> list[Path]` (the planned `ns_common.gpu_dlls.ensure_on_path()`). C6 ships a local copy, `ns_studio.media.transcribe.ensure_gpu_dlls_on_path`: it prepends `<site-packages>/nvidia/{cublas,cudnn,cuda_nvrtc}/bin` to `PATH` and calls `os.add_dll_directory`; idempotent, tested with temp folders.
- **Why:** `ns_common` is frozen for C6, and `ns verify` (C2) needs the same helper. Once the common version lands, C6 deletes its copy and imports it.
- **Status:** open

## CR-C6-2: Pillow in the `dev` extra
- **From:** C6 media / thumbnails.py
- **Change:** add `"Pillow>=10"` to `[project.optional-dependencies].dev` in `pyproject.toml`.
- **Why:** CI installs only `.[dev]`, so the Pillow thumbnail tests (`tests/test_thumbnails.py`, guarded by `pytest.importorskip("PIL")`) are skipped in CI today. `thumbnails.py` imports Pillow lazily through `importlib`, so pyright is unaffected.
- **Status:** open

## CR-C6-3: error code for a failed transcription
- **From:** C6 media / transcribe.py, captions.py
- **Change:** add to `contracts/errors.json` an `NS-E4xx` "Speech recognition failed (model could not load or transcribe)" with the hint to run `ns verify`.
- **Why:** C6 currently raises `NS-E202` (service failed to start) when faster-whisper cannot load or run and `NS-E403` when the fake transcriber has no script sidecar; neither message names transcription.
- **Status:** open

## CR-C6-4: `contracts/policy/profanity.txt`
- **From:** C6 media / captions.py (spec 17.2 `mask_profanity`)
- **Change:** C6 created `contracts/policy/profanity.txt` (short list, one lower-case word per line, `#` comments) at the coordinator's request. The orchestrator may own and extend it.
- **Why:** `build_ass(..., mask_profanity=True)` reads it; the file is display-only masking, not a policy gate.
- **Status:** open (informational)

## CR-C6-5: `series_context.json` and Outbox layout need a published contract
- **From:** C6 media / outbox.py (spec 15.2a)
- **Change:** document in `contracts/CONTRACTS.md` that `data/jobs/<job_id>/series_context.json` is `{series_slug: str, part_n: int, series_title: str}` and that `ns_studio.media.outbox.{outbox_target, safe_slug, safe_filename, rejected_path}` own the Outbox/rejected tree shape (a `.nightshift-job` marker file in each story folder records the owning job for `-2` collision suffixes).
- **Why:** C7 (series), C8 (`clips/`) and C11 write into the same tree.
- **Status:** open

## CR-C8-8: migrate clip layouts to format presets (spec 17.1)
- **From:** C8 Clipper / spec 23.5
- **Change:** `ns_studio/presets/formats/*.yaml` and `ns_studio/media/layout.py` (the generic region renderer) are not on `main`, so C8 keeps `ns_studio/presets/clip_templates.yaml` and its own graph builders (`ns_studio/clipper/render.py`). When the format presets land, `load_templates()` should read `clip_gaming_facecam`, `clip_podcast_stacked`, `clip_podcast_single`, `clip_talking_head`, `clip_center_crop`, `clip_screen_plus_cam` and the render should use the generic region builder. The model's `layout` answers already accept both `gaming_facecam` and `clip_gaming_facecam` ids.
- **Why:** formats are data, not code (spec 17.1).
- **Status:** open

## CR-C8-9: C5-owned files edited by C8 (informational, per coordinator instruction)
- **From:** C8 Clipper / spec 23.5
- **Change:** `ns_studio/prompts/clip_select.md` (v2: `analysis` input, `layout` and `layout_reason` output), new `ns_studio/prompts/clip_judge.md` (thinking on, schema `clip_judge` = `{ok, reasons[]}`), `ns_studio/text/prompts.py` (`clip_plan` items accept `layout` / `layout_reason`; new `clip_judge` schema), fixture `tests/fixtures/llm/clip_judge.json`.
- **Why:** the revised clipper flow lets the model choose a layout per candidate and adds a logic judge before each clip.
- **Status:** applied on feat/c8-clipper
