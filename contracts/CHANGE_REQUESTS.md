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
- **Status:** applied in 95580e6 (errors.json has NS-E320..E326; C7 may empty `_LOCAL` in `ns_studio/errors.py`); `_LOCAL` emptied in 50742f8 and `StudioError` reads `errors.json` via `describe`

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
- **Status:** applied in e0c34f5 (`TtsItem.cliff_lines`, sent per item by `speak_batch` and by `TtsClient.speak(cliff_lines=...)` only when non-empty)

## CR-C9-1: dashboard dependencies, packaging and type-check scope
- **From:** C9 ns_dashboard / Tasks 5-9
- **Change:** in `pyproject.toml` (a) add `"fastapi>=0.115"`, `"python-multipart>=0.0.9"` and `"Pillow>=10"` to the `dev` extra (`httpx` is already a core dependency and is what `fastapi.testclient` needs); (b) add an extra `dashboard = ["fastapi>=0.115", "uvicorn>=0.30", "python-multipart>=0.0.9", "Pillow>=10", "keyring>=25"]` for the target machine; (c) add `"ns_dashboard*"` to `[tool.setuptools.packages.find].include` and `ns_dashboard = ["static/*"]` to `[tool.setuptools.package-data]`; (d) add `"ns_dashboard"` to `[tool.pyright].include`.
- **Why:** CI installs only `.[dev]`, so until (a) lands every `TestClient`, multipart and Pillow test of the dashboard is skipped (the framework-free service layer, job worker, uploads, range parsing, log tail and templates are tested without them). (c) is needed for the bundle to ship the UI; (d) puts the dashboard under pyright like the other packages (the code is written to pass `standard` mode).
- **Status:** applied on feat/release-readiness: (a) was already in the dev extra, (b) a `dashboard` extra with `python-multipart`, `Pillow`, `keyring` (fastapi and uvicorn come from `supervisor`; installer s04 installs `[supervisor,tts,dashboard]` into the supervisor venv where the dashboard runs), (c) `ns_dashboard*` in the package list and package-data, (d) `ns_dashboard` in the pyright include

## CR-C9-2: `sources.rights_confirmed` and `dashboard_jobs.result_json`
- **From:** C9 ns_dashboard / Task 6
- **Change:** in `contracts/schema.sql` add to `sources`: `rights_confirmed INTEGER NOT NULL DEFAULT 0, rights_confirmed_at TEXT`; add to `dashboard_jobs`: `result_json TEXT`. Add a pydantic `DashboardJob` model (fields as the API object `J`: job_id, kind, channel, status, progress, message, stage, created_at, started_at, finished_at, params, result) to `ns_common/models.py` and export `dashboard_job.schema.json` (spec 15.6 lists the schema, the file does not exist yet).
- **Why:** spec 19.1 says the rights confirmation is stored on the `sources` row, which has no column for it. Until then the dashboard writes `imports/clipper/<source_id>/rights.json` and keeps `{"params":..., "result":...}` inside `dashboard_jobs.params_json`; C8 can read `rights.json` and set the column when it creates the `sources` row.
- **Status:** partly applied on main (PR #15: `sources.rights_confirmed` and `sources.title`). `dashboard_jobs.result_json` is not needed: the dashboard keeps `{"params","result"}` in `params_json`. The `dashboard_job` schema file is still open.

## CR-C9-3: supervisor service entry for the dashboard
- **From:** C9 ns_dashboard / Task 7
- **Change:** C2 adds service `dashboard` (not a GPU tenant, no idle unload, health `GET http://127.0.0.1:8100/api/status`) with the exact command line `python -m ns_dashboard.server --port 8100` run from the supervisor/studio venv (needs the `dashboard` extra of CR-C9-1). The dashboard talks to the supervisor only over HTTP (`NS_SUPERVISOR_URL` overrides `http://127.0.0.1:<runtime.supervisor_port>`).
- **Why:** spec 20.1: the supervisor starts the dashboard; `Nightshift Dashboard.lnk` only opens the browser.
- **Status:** open

## CR-C9-4: hygiene allow-list for the dashboard
- **From:** C9 ns_dashboard / Task 3
- **Change:** add `ns_dashboard/supervisor_link.py` to the HTTP-client import allow-list (it is the only dashboard module that imports `httpx`, loopback supervisor only). `ns_dashboard` uses no `subprocess` (thumbnails go through `ns_common.ffmpeg.run`; the diagnostics bundle is an in-process call into C2), so no TID251 exemption is needed. Static UI files must not load anything from the network (the server sends `Content-Security-Policy: default-src 'self'`).
- **Why:** spec section 8 "Network" rule.
- **Status:** open

## CR-C9-5: dashboard integration points other components must provide
- **From:** C9 ns_dashboard / Task 3 (adapters import these lazily; a missing one fails only that job or action, with a plain message)
- **Change:** C7: `ns_studio.orchestrator.run_job_with_progress(paths=, channel=, fmt=, count=, on_stage=, acquirer=) -> list[str]` (new `outputs` ids; call `on_stage(name, index, total)` before each stage, let its exceptions propagate so cancel unwinds between stages), `ns_studio.learn.import_csv(paths, csv_path) -> dict` (`matched`, `unmatched`, `weight_changes[{pool,option,delta}]`, optional `summary[str]`) and `ns_studio.learn.summary(paths) -> dict`. C8: `ns_studio.clipper.run_clip_job(paths=, source_path=, source_id=, params=, on_stage=, acquirer=) -> list[str]` inserting `outputs` rows with `kind='clip'` and `source_id`, plus `FORMAT_STAGES["clip"]` and `ns_studio/presets/clip_templates.yaml` (regions as fractions or pixels of 1080x1920, `caption_y_pct`). C2: `ns_supervisor.client.SupervisorClient(base_url)` implementing `ns_common.acquire.Acquirer`; `ns_supervisor.doctor.make_bundle(paths) -> Path`; `ns_supervisor.verify.run_verify(paths=, quick=, on_stage=) -> dict`; `ns_supervisor.scheduler.set_daily_time(paths, "HH:MM")`.
- **Why:** see spec 20.4; the dashboard cannot reimplement these.


## CR-C8-0: register the clipper error codes in `errors.json`
- **From:** C8 Clipper / Task 1
- **Change:** add `NS-E405` (unsupported source: too long, no audio, unreadable, or rights not confirmed), `NS-E406` (transcription produced fewer than 30 words), `NS-E407` (no clip candidates passed the filters) and `NS-W402` (requested template not applicable, fell back to center_crop). 
- **Why:** spec 19.7 defines them but `contracts/errors.json` lacks them, so `NsError` raised `KeyError`.
- **Status:** applied on main (2a3d772); C8's local registration shim was removed

## CR-C8-1: move the clipper schemas into `ns_common`
- **From:** C8 Clipper / Task 1
- **Change:** move `Transcript`, `Segment`, `Word`, `ClipItem` (`clip_plan` item), `ClipLayout` (+ `Crop`, `FaceFrame`, `Rect`) and `ClipTemplate` from `ns_studio/clipper/models.py` to `ns_common/models.py`, register them in `SCHEMA_MODELS` and export `transcript`, `clip_plan`, `clip_layout`, `clip_template` schemas.
- **Why:** spec 15.6 lists them as contracts (also read by C9). They are local pydantic models for now.
- **Status:** applied in e0c34f5 (`Transcript`, `Segment`, `Word`, `Rect`, `FaceFrame`, `Crop`, `ClipLayout`, `Region`, `ClipTemplate` and `ClipPlanItem` in `ns_common.models`; schemas `transcript`, `clip_layout`, `clip_template`, `clip_plan`). C8 may now import them from `ns_common.models` (its `ClipItem` is `ClipPlanItem`) and delete its local copies; clipper consumers migrated in 9627b9e (`ns_studio/clipper/models.py` re-exports them, `ClipItem` is `ClipPlanItem`)

## CR-C8-2: OpenCV in the `dev` and `studio` extras
- **From:** C8 Clipper / Task 3
- **Change:** add `"opencv-python-headless>=4.9"` to `[project.optional-dependencies].dev` and `.studio` in `pyproject.toml` (`numpy` is already core).
- **Why:** with it CI runs the YuNet/Canny paths (`pytest.importorskip("cv2")` tests are skipped without it). The YuNet ONNX also needs a `manifest.json` pin (`models\cv\yunet.onnx`) so the installer downloads it.
- **Status:** applied (`dev` extra earlier; `studio` extra and the `bins.yunet` manifest pin on feat/release-readiness)

## CR-C8-3: `sources` row stores the rights confirmation
- **From:** C8 Clipper / Task 5
- **Change:** `contracts/schema.sql` `sources` gains `rights_confirmed INTEGER NOT NULL DEFAULT 0` and `title TEXT`.
- **Why:** spec 19.1 says the owner's rights confirmation is stored on the `sources` row; the table has no column. C8 keeps it in `imports/clipper/<source_id>/source.json` and the job's `source.json` meanwhile.
- **Status:** applied in e0c34f5 (`sources.rights_confirmed`, `sources.title`; `Db.migrate()` also adds the columns to an existing database)

## CR-C8-4: `metadata.kind` accepts `"clip"`
- **From:** C8 Clipper / Task 7
- **Change:** `Metadata.kind` becomes `Literal["long","short","clip"]` and gains optional `source_video: str | None` and `template: str | None` (spec 15.6).
- **Why:** clip upload sheets use `ClipMetadata` (local) until then.
- **Status:** applied in e0c34f5 (`Metadata.kind` accepts `clip`; `source_video`, `template` added)

## CR-C8-5: mount `studio clip`
- **From:** C8 Clipper / Task 7
- **Change:** in `ns_studio/cli.py` add `app.add_typer(ns_studio.clipper.cli.app, name="clip")` (the entry is `ns_studio.clipper.cli.app`; it takes `studio clip <video> --channel <slug> [--template ...] [--max-clips N] [--min-s S] [--max-s S] [--confirm-rights] [--reselect <job_id>] [--fake-services[=list]]`). `JobContext.fmt` Literal gains `"clip"`; `orchestrator.FORMAT_STAGES` gets `"clip"` from `ns_studio.clipper.stages_clip` (set at import).
- **Why:** spec 15.9. C8 never edits C7 files.
- **Status:** applied in 48457df (mount) and 919a915 (`JobContext.fmt` literal). `FORMAT_STAGES["clip"]` is set by `ns_studio.clipper.stages_clip` when `studio clip` imports it

## CR-C8-6: optional fields on `clip_layout`
- **From:** C8 Clipper / Task 4
- **Change:** `crops[]` gains optional `region_b` (second panel: camera, or the lower speaker) and `speaking` (0 top, 1 bottom); `faces[]` gains optional `speaking`; `clip_layout` gains optional `note` (NS-W402 fallback text).
- **Why:** `gaming_facecam`, `screen_plus_cam` and `podcast_stacked` need two source regions per time slice.
- **Status:** applied in e0c34f5 (already in C8's model; now in `ns_common.models` and `clip_layout.schema.json`)

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
- **Status:** no contract change needed (documented in spec 15.3 only; there is no pydantic model for the supervisor payloads)

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
- **Status:** applied in e0c34f5 (`LlamaCfg.story_flags: LlamaFlags | None = None`; `runtime.schema.json` regenerated)
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
- **Status:** applied in e0c34f5 (`Plan`, `PlanChapter`, `Story`, `StoryChapter`, `Sentence`, `ShortsPlanItem`, `ShortsPlan`, `SeoLong`, `ChapterTitle`, `ClipPlanItem`, `ClipPlan`, `PolicyCheckResult`; schemas `plan`, `story`, `shorts_plan`, `seo_long`, `clip_plan`, `policy_check`). `clip_plan` reconciles with C8: `speakers` is `list[int] | None`, `why` defaults to empty, plus `layout`, `layout_reason`, `caption_style`, `status`, `reasons`. C5 migrates by importing from `ns_common.models` and deleting `models_local.py`; consumers migrated in 9627b9e (`ns_studio/text/models_local.py` is now a deprecated alias module; `ChapterDraft`, which has no shared model, stays there)

## CR-C9-6: idea-bank columns and the assistant database
- **From:** C9 ns_dashboard / Library + Assistant
- **Change:** (a) `contracts/schema.sql`, table `ideas`: add `status TEXT NOT NULL DEFAULT 'new'` (`new|approved|used|dropped`), `setting TEXT`, `protagonist TEXT`, `keywords TEXT` (JSON array), `source TEXT` (`owner|nightshift|assistant`), `updated_at TEXT`. M1 stage `ideas` (C5/C7) then draws `status='approved'` rows of the channel first (set `status='used'` and `used=1` when consumed) and invents only when none are left. (b) New `contracts/assistant_schema.sql` (or an `assistant.db` section of `schema.sql`) with the tables created by `ns_dashboard/assistant/store.py`: `conversations`, `messages`, `actions`, `queued_messages` (DDL is in that module's `DDL` constant). (c) `ns_common/llm_client.py`: move `chat_tools(messages, tools)` (OpenAI-compatible tool calling, see `ns_dashboard/assistant/llm.py::ToolLlmClient`) into `LlmClient`. (d) `contracts/schemas/format.schema.json` generated from a pydantic `Format` model (the dashboard validates against `ns_dashboard/formats.py::FORMAT_SCHEMA` until then).
- **Why:** spec 20.2 Library > Ideas and 20.3a. Until then the dashboard adds the idea columns itself with a guarded `ALTER TABLE ... ADD COLUMN` at startup and creates `data/assistant.db` with `CREATE TABLE IF NOT EXISTS`.
- **Status (integration pass):** the `ideas` stage draws `status='approved'` rows first and marks them used, applied in e2449f1. The rest of this CR is tracked by the status line below.
- **Status:** mostly applied on main (PR #15: `ideas.status|title|source`, `assistant_messages`, `Paths.assistant_db`, the `Format` model). The dashboard uses them (status `rejected` instead of `dropped`; `dropped` is accepted as an alias) and no longer adds columns or defines a format schema. Still open: see CR-C9-9.

## CR-C9-7: mount `studio ask` and ship the prompt
- **From:** C9 ns_dashboard / Assistant CLI
- **Change:** C7 mounts the command: in `ns_studio/cli.py` call `from ns_dashboard.assistant.cli import register; register(app)` (it adds `studio ask "<text>" [--yes] [-c conversation] [--fake-services]`). `pyproject.toml` (extends CR-C9-1): `[tool.setuptools.package-data] ns_dashboard = ["static/*", "prompts/*.md"]`.
- **Why:** spec 20.3a "`studio ask` on the command line for the same loop". The prompt file `ns_dashboard/prompts/assistant.md` must ship with the package.
- **Status:** applied (`studio ask` mounted in 48457df; package-data `ns_dashboard = ["static/**/*", "prompts/*.md"]` on feat/release-readiness)

## CR-C9-8: `ns update --check` / `--github` output and subprocess allow-list
- **From:** C9 ns_dashboard / Update endpoints
- **Change:** C2's `ns update --check` should print one JSON line `{"update_available": bool, "current": "x.y.z", "latest": "x.y.z", "notes": "..."}` and exit 0 (non-zero on failure); `ns update --github` downloads, verifies and installs the newest release and rolls back on failure (exit code 0 / non-zero). The dashboard falls back to reading versions and "up to date" out of plain text. C10: add `ns_dashboard/updater.py` to the `subprocess` per-file-ignores and hygiene allow-list (list arguments, never a shell, timeouts of 120 s and 1 h; it runs `venvs\supervisor\Scripts\ns.exe`).
- **Why:** spec 20.2 Settings > Update and 20.3a `/api/update/check|apply`.

## CR-C1-1: error codes NS-E111, NS-E112, NS-W112 are missing from errors.json
- **From:** C1 installer / stage1.ps1 and steps s01, s08
- **Change:** add to `contracts/errors.json`: `NS-E111` ("winget (Windows App Installer) is missing.", hint "Install 'App Installer' from the Microsoft Store (or use the direct installer links shown), then run setup again."), `NS-E112` ("faster-whisper cannot load on the GPU (cuBLAS 12 / cuDNN 9 DLLs not found).", hint "In the studio venv run: uv pip install nvidia-cublas-cu12 nvidia-cudnn-cu12, then re-run 'ns verify --quick'."), `NS-W112` ("Windows long paths are disabled.", hint "Run setup once as administrator to enable them, or keep NIGHTSHIFT_HOME on a short path such as D:/Nightshift.").
- **Why:** spec 1.2 / 15.12 and ENGINEERING.md section 6 name them; `describe()` raises KeyError for them today. The installer works around it with `installer/stage2/errors.py::make_error` (a pending table with the same text), which becomes a plain `NsError` as soon as the codes exist. `stage1.ps1` has the same texts as a fallback.
- **Status:** applied on main earlier (NS-E111, NS-W112, NS-E112 are in errors.json); `installer/stage2/errors.py` can drop its pending table; the pending table was dropped in 50742f8

## CR-C1-2: Answers model and answers.schema.json lack the installer-only fields
- **From:** C1 installer / answers.example.json, s01_questions
- **Change:** add to `Answers`: `home_path: str | None = None`, `models_source: str | None = None`, `llm_tier: Literal["small","balanced","best"] = "small"`, `restore_from: str | None = None`; regenerate `answers.schema.json`. Voice stays as the model has it (`voice` plus flat `voice_ref_audio_path`, `voice_ref_text`, `voice_rights_confirmed`); spec 5 describes it as a nested object, the model is the source of truth.
- **Why:** spec 5 and 1.3 list these keys for unattended installs. `installer/stage2/answers.py::InstallerAnswers` subclasses `Answers` and adds them, and `tests/test_installer_files.py` accepts exactly these four keys as "not in the schema yet"; delete that allowance when this lands.
- **Status:** applied in e0c34f5 (`Answers.home_path`, `models_source`, `llm_tier`, `restore_from`; `answers.schema.json` regenerated)

## CR-C1-3: record the chosen llama.cpp build, and Paths for bin/uv, bin/qwentts, models/cv
- **From:** C1 installer / s02_download_bins (coordinator decision: CUDA 13.4 pair when nvidia-smi reports CUDA >= 13.0, else the 12.4 pair)
- **Change:** `InstallState`: add `cuda: str | None` and `llama_pair: Literal["cuda13","cuda12"] | None`. `Runtime.llama`: add `build: Literal["cuda13","cuda12","cpu"] | None`. `Paths`: add `bin_qwentts`, `bin_uv`, `models_cv`.
- **Why:** InstallState and Runtime are `extra="forbid"`, so the installer keeps the pair in `config/wizard_flags.json` (`llama_pair`, `cuda`) for now. C2 could read the pair to pick DLL paths. `Paths` is missing three folders the spec lists (21.2, 21.5); the installer builds them from `paths.home` meanwhile.
- **Status:** partly applied in e0c34f5: `Paths.bin_uv`, `bin_qwentts`, `models_cv` (plus `stories`, `assistant_db`) and `LlamaCfg.build` (`cuda13|cuda12|null`; the `cpu` value was dropped per coordinator). Not applied: `InstallState.cuda` and `llama_pair` (not in the C3 amendment list); the installer keeps them in `config/wizard_flags.json`

## CR-C1-4: pins the installer needs in manifest.json / pyproject.toml
- **From:** C1 installer / s04_venvs, fallback.py
- **Change:** (a) manifest: spaCy model wheel `en_core_web_md` version and SHA256 (the installer hard-codes 3.8.0 and cannot verify the hash); (b) manifest or `studio` extra: `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` pins (the installer uses ranges `>=12.8,<13` and `>=9,<10`); (c) manifest: the PyTorch fallback HF repos (`Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice`, `-Base`) instead of constants in `installer/stage2/fallback.py`; (d) `studio` extra: `opencv-python-headless` (spec 21.4, needed from M3); (e) a Blackwell-capable CUDA 12.8 llama.cpp pin if one exists, so CUDA 12.4 to 12.7 drivers are not limited to the 12.4 build.
- **Why:** ENGINEERING.md DoD 6 (no unpinned dependency). The installer runs today with the ranges above.
- **Status:** (a), (b), (c), (d) applied; (e) open. (d) on feat/release-readiness: `opencv-python-headless>=4.9,<5` is in the `studio` extra. On feat/release-whisper-cuda: (a) `manifest.json` `python.spacy_wheel` = `en_core_web_md` 3.8.0 with the wheel's sha256 from the spacy-models release notes; (b) `python.whisper_cuda` = `nvidia-cublas-cu12==12.8.4.1` and `nvidia-cudnn-cu12==9.10.2.21`, each with the sha256 PyPI lists for its win_amd64 wheel (CTranslate2 4.8.5's Windows build uses CUDA 12.8 / cuBLAS 12.8.4.1 and cuDNN 9.10.2; `ctranslate2>=4.6.3,<4.9` in the `studio` extra keeps the install on CUDA 12.8 builds), installed by s04 with `uv pip install --no-deps --require-hashes -r logs/pins-*.txt`; (c) `tts_fallback_repos`. `Manifest` gained `python: PythonPins` and `tts_fallback_repos` (both defaulted, so older manifests still load) and `contracts/schemas/manifest.schema.json` follows. Also adds `NS-W404` (whisper on the CPU). Still open: (e) a Blackwell-capable CUDA 12.8 llama.cpp pin; it needs a llama.cpp Windows build for CUDA 12.8 (the manifest has the 12.4 and 13.4 builds only), so CUDA 12.4 to 12.7 drivers stay on the 12.4 build; not part of this change

## CR-C1-5: what the installer expects from the `ns` CLI (C2)
- **From:** C1 installer / s06, s08, s09, s10
- **Change:** none to contracts; confirm behaviour in the C2 plan. The wizard calls these from the studio venv (installed with extras `studio,supervisor` so `ns_supervisor` imports resolve there), with `NIGHTSHIFT_HOME` set and the app folder as cwd: `ns tune` (exit 0 on success; a failure only warns), `ns verify --quick` (JSON on stdout or in `logs/verify-report.json`: `{"checks":[{"name","passed"}]}` where names contain `llm`, `embed`, `tts`, `whisper`, `nvenc`, `mtp`), `ns doctor --bundle` (writes `nightshift-diagnostics-<date>.zip` to the Desktop), `ns schedule install --time HH:MM` (a "No such command" answer is tolerated until the scheduler exists), `ns restore <zip>`.
- **Why:** the smoke step applies NS-W401 / NS-W301 / NS-W302 / NS-E112 from those check names.
- **Status:** open

## CR-C11-1: error codes for Series Workshop usage errors
- **From:** C11 series workshop / `ns_studio/series/store.py`
- **Change:** add to `contracts/errors.json`: `NS-E801` "Unknown story, part or draft." (hint: run `studio series list` / `status`), `NS-E802` "This step is not allowed in the part's current status." (hint: the detail names the status the step needs, e.g. write before approve, approve before produce, no rewrite once producing), `NS-E803` "Workshop value out of range." (hint: minutes per part 12-25, parts 1-40, 1-6 concept drafts), `NS-E804` "Design value not allowed for this channel." (hint: `studio series design <slug>` lists the allowed formats, visual treatments and caption styles).
- **Why:** these are owner-facing usage errors that no existing code describes. Until the codes exist C11 raises `WorkshopError(ValueError)` with a plain-English message; content rejections already use NS-E303, NS-E601, NS-E602, NS-E603, NS-E604, NS-E607 and NS-E302.
- **Migration:** C11 replaces `WorkshopError` by `NsError` with these codes; the CLI already prints coded errors with their hint.
- **Status:** open

## CR-C11-2: C9 mounts the workshop tools and handles `series_part` jobs
- **From:** C11 series workshop / `ns_studio/series/tools.py`, `ns_studio/series/workshop.py`
- **Change:** (1) `ns_studio.series.tools.TOOLS` is a list of 12 dicts with the `ToolSpec` fields `name, description, args_schema, side_effect, confirm_text` plus `fn`, a plain callable `fn(ws, **args)` where `ws` comes from `ns_studio.series.workshop.default_workshop()` (pass `fake=` in tests). Mount them in the Assistant registry; `workshop_status` and `workshop_list` are `side_effect=false`, the other ten return a confirm card whose text is `confirm_text.format(**args)` (only required arguments are used). `call_tool(ws, name, args)` validates required/unknown keys and returns JSON-ready data. (2) Library -> Stories: `Workshop.list_stories()` and `Workshop.status(slug)`. (3) `workshop_produce` inserts a `queued` row into `dashboard_jobs` with `kind='series_part'`, `message` = the video title and `params_json` = the `ProduceRequest` (job id, channel, plan `master_from_script`, story slug, part, series title/slug, script and Shorts paths, design, end card, `publish_not_before_days`). The job directory already holds `script_source.json` and `series_context.json`; the worker should run that job id with the `master_from_script` stage list (CR-C11-3) and honour `publish_not_before_days` in its scheduling. Pass your own enqueue callback to `Workshop(..., enqueue=fn)` if the worker wants a different hand-off.
- **Why:** spec 20.3a makes every feature an Assistant tool; C9 owns the registry.
- **Migration:** none for C11; C9 adds the mount and a worker branch for `kind='series_part'`.
- **Status:** open

## CR-C11-3: C7 mounts `studio series`, registers `script_import` and adds the `master_from_script` stage list
- **From:** C11 series workshop / `ns_studio/series/cli.py`, `ns_studio/series/stages.py`
- **Change:** (1) In `ns_studio/cli.py`: `from ns_studio.series.cli import app as series_app` and `app.add_typer(series_app, name="series")`. (2) Add `"ns_studio.series.stages"` to `STAGE_MODULES` so stage `script_import` (tenant `None`; inputs `script_source.json`; outputs `idea.json plan.json plan_meta.json story.json shorts_plan.json`) is registered. (3) Add the stage list `master_from_script` = `script_import, visual_plan_long, seo_long, seo_shorts, voice, duration_check, captions, shorts_timing, clips, render_master, thumbnails, shorts_derive, qc, outbox` (names as in C7's `MASTER_FIRST_STAGES`, minus `ideas, long_plan, chapters_draft, chapters_critic, continuity, shorts_select`; `visual_plan_long` stays because `script_import` writes `idea.json` for it) and make `plan_job` / `run_job` use it whenever the job is `master_first` and `ctx.file("script_source.json")` exists. (4) Re-running a finished job after editing `script.md` needs `force`: the stage hash only covers `script_source.json` because the stage reads the live files on purpose. (5) Cadence governor: `script_source.json` carries `publish_not_before_days` (0, 7, 14, ... from the design's `publish_cadence_days`); add it to the part's `publish_after`.
- **Why:** spec 23.3 step 6; the orchestrator must skip M1-M4 for series parts.
- **Migration:** none for C11; `--fake-services` works the same way as `studio run`.
- **Status:** applied in 48457df (mount, `ns_studio.series.stages` in `STAGE_MODULES`, policy gates installed on the `master_from_script` plan too). The stage list and the `run_job` switch were already in `orchestrator.py`; items 4 and 5 are C11 behaviour

## CR-C11-4: C5 text models and prompts for series parts
- **From:** C11 series workshop / `ns_studio/series/importer.py`, `ns_studio/series/workshop.py`
- **Change:** (1) `Plan.chapters` allows 5..12 chapters (today 5..7): a 20-minute part has 9 chapters and a 25-minute part 11. C11 writes `plan.json` with the master shape and only validates it with `Plan` when it has 5-7 chapters; `expand_chapter` calls `parse_model(Plan, ...)` and would fail on a longer plan. (2) `chapter_draft.md` gets an optional `series` variable (`{title, part_n, parts_total}`): when defined, skip "first sentence is the story's hook" for part > 1 and "pay off the twist and echo the first line" for parts before the last. C11 passes the series context inside `idea.premise` meanwhile. (3) `seo_long` / `seo` (and `long_seo.md`) should read `script_source.json` when present and use `series_title`, `part_n`, `parts_total` and `end_card` for "Part N of M" in titles and descriptions (spec 9) and the next-part end-card line; titles are "<series title> - Part NN" and "<series title> - Part NN - Short 01". (4) `tests/test_prompts.py::test_every_json_fixture_validates_against_its_prompt_schema` maps each `tests/fixtures/llm/<name>[_n].json` to a prompt of the same name; C11's fixtures follow that rule (`workshop_concepts`, `workshop_concepts_2`, `workshop_regenerate`, `workshop_map`, `workshop_part_outline`) and need `ns_studio.series.schemas` imported (the full suite does; running that single test file alone would not).
- **Why:** series parts are longer than the master the C5 models were sized for.
- **Migration:** C11 needs no change when (1)-(3) land.

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
- **Status:** applied on feat/media-finish (clip_templates.yaml is now a thin map from template ids to the six clip_* presets; the render builds its panels with ns_studio/media/layout.py)

## CR-C8-9: C5-owned files edited by C8 (informational, per coordinator instruction)
- **From:** C8 Clipper / spec 23.5
- **Change:** `ns_studio/prompts/clip_select.md` (v2: `analysis` input, `layout` and `layout_reason` output), new `ns_studio/prompts/clip_judge.md` (thinking on, schema `clip_judge` = `{ok, reasons[]}`), `ns_studio/text/prompts.py` (`clip_plan` items accept `layout` / `layout_reason`; new `clip_judge` schema), fixture `tests/fixtures/llm/clip_judge.json`.
- **Why:** the revised clipper flow lets the model choose a layout per candidate and adds a logic judge before each clip.
- **Status:** applied on feat/c8-clipper


## CR-C6-1: `ns_common/gpu_dlls.py` (cuBLAS / cuDNN on PATH)
- **From:** C6 / coordinator
- **Change:** `ensure_on_path(venv_site_packages: Path | None = None) -> list[Path]` prepends `nvidia/cublas/bin` and `nvidia/cudnn/bin` to `PATH` (and `os.add_dll_directory` on Windows) when they exist; idempotent.
- **Why:** faster-whisper on CUDA needs the cuBLAS 12 / cuDNN 9 DLLs (NS-E112).
- **Status:** applied in e0c34f5

## CR-C6-3: `NS-E408` transcription failed
- **From:** C6 / coordinator
- **Change:** `contracts/errors.json` gains `NS-E408` "Transcription failed." with a hint.
- **Status:** applied in 95580e6

## CR-C3-1: `format` schema, idea bank columns, assistant transcript table
- **From:** coordinator (spec 17.1, 20.3a)
- **Change:** `ns_common.models.Format` (+ `FormatCanvas`, `FormatDuration`, `FormatCaptions`, `FormatRegion`, `FormatLayout`, `FormatMargins`, `FormatShots`, `FormatEncoder`, `FormatSheet`) exported as `format.schema.json`; `ideas` gains `status TEXT NOT NULL DEFAULT 'new'`, `title TEXT`, `source TEXT` (`owner|assistant|generated`); new table `assistant_messages(id, ts, role, content, tool_calls_json)`. `Db.migrate()` adds missing columns to databases made by an older `schema.sql`.
- **Status:** applied in e0c34f5

## CR-C6-6: per-channel options, `emphasis` fields and packaging of the format presets
- **From:** C6 media finish (spec 17.1, 17.2)
- **Change:** (1) `ns_common.models.Channel` gains `formats_allowed: list[str] = []` and `captions: {mask_profanity: bool = False}`. `Channel` forbids unknown keys, so `config.load_channel` rejects a profile that carries them today; `ns_studio/media/channel_opts.py` already reads both (model attribute first, YAML second). (2) `VisualBeat` gains optional `emphasis` (mapping sentence index to one key word) and `ClipPlanItem` gains optional `emphasis` (list, one key word per sentence); `ns_studio/clipper/models.py` `ClipItem` has it already and the stages read both when present as plain extra keys. The `visual_plan`, `clip_select` prompts and `text/prompts.py` schemas should ask for and pass it through. (3) `pyproject.toml` `[tool.setuptools.package-data]` needs `presets/formats/*.yaml` for `ns_studio`. (4) `ns_dashboard/templates.py` reads regions and caption anchors from `clip_templates.yaml`; they now live in `ns_studio/presets/formats/clip_*.yaml` (load with `ns_studio.media.formats.load_formats()`), and the Library → Formats page can list every preset.
- **Why:** spec 17.1 (formats allowed per channel, formats as data) and 17.2 (emphasis, profanity masking) need fields the shared models and packaging do not carry.
- **Status:** applied on feat/release-readiness: `Channel.formats_allowed` and `Channel.captions` (`mask_profanity`) exist and the channel schema is regenerated; `VisualBeat.emphasis` is a mapping (`dict[str, str]`, sentence index as the JSON key) and `ClipPlanItem.emphasis` a list (`list[str]`), both optional, in the `visual_plan` and `clip_plan` schemas; `presets/formats/*.yaml` is in the `ns_studio` package-data. `ns_studio/media/channel_opts.py` is gone: the stages read `channel.captions.mask_profanity` and `channel.formats_allowed` directly. Still open: the `visual_plan`, `clip_select` prompts and `text/prompts.py` schemas do not ask the model for `emphasis` yet; `ns_dashboard/templates.py` still reads `clip_templates.yaml`

## CR-C9-9: assistant conversations and pending actions (shared schema)
- **From:** C9 ns_dashboard / Assistant
- **Change:** `assistant_messages` (flat: ts, role, content, tool_calls_json) cannot hold conversations, tool-call ids or pending confirm cards. `ns_dashboard/assistant/store.py` therefore creates `conversations`, `messages`, `actions` and `queued_messages` in `data/assistant.db` itself (and mirrors every message into `assistant_messages`). Move those four tables into `contracts/schema.sql` next to `assistant_messages` (or extend `assistant_messages` with `conversation_id`, `tool_call_id`, `name`), then delete `DDL` from the store.
- **Why:** local DDL for a shared database is a shim; the owner of `contracts/` should own it.
- **Status:** applied on feat/release-readiness: the four tables are in `contracts/schema.sql` next to `assistant_messages` and `DDL` is gone from the store; the flat `assistant_messages` mirror stays

## CR-C8-10: URL sources via a pinned yt-dlp (`bins.yt_dlp`, `NS-E409`, subprocess allow-list)
- **From:** C8 Clipper / URL source task
- **Change:** (1) `manifest.json` `bins.yt_dlp`: `{version, url, sha256}` for the `yt-dlp.exe` asset of a release from https://github.com/yt-dlp/yt-dlp/releases (pin the latest release at the time, SHA256 from the release's `SHA2-256SUMS`); the installer downloads it to `<home>\bin\yt-dlp\yt-dlp.exe` (C1). (2) `contracts/errors.json`: `NS-E409` message "Download failed.", hint "Check the link and that the video is yours or you have permission to use it." (3) `pyproject.toml` ruff per-file-ignores: `"ns_studio/clipper/source.py" = ["TID251"]` (it runs yt-dlp with `subprocess.Popen`, list arguments, never a shell); until then the import carries `# noqa: TID251`.
- **Why:** `studio clip <url>` downloads the owner's video before the normal import (ffprobe, sha256, rights gate). Until `errors.json` lists the code, `ns_studio/clipper/source.py` spells it `"NS-E" + "409"` and registers the text at import so the hygiene test (unregistered code literals) stays green; replace both with the plain literal and delete `register_download_code` when this lands.
- **Status:** applied on feat/release-readiness: `errors.json` has `NS-E409` (`NS-E410` was not needed: a missing yt-dlp raises `NS-E409` with the path in the detail), the `TID251` per-file ignore is in `pyproject.toml` and the `noqa` and the split-literal workaround are gone from `ns_studio/clipper/source.py`, `manifest.json` pins `bins.yt_dlp` and installer step s02 downloads it

## CR-C1-UI-1: installer answers gain `owner_name`, `ui_theme`, `models_source_kind`
- **From:** installer UI design (`docs/design/installer-ui.md` section 5.5); owner decisions of 2026-11-02
- **Change:** add to `Answers` (and `answers.schema.json`, `installer/answers.example.json`): `owner_name: str | None = None` (max 40), `ui_theme: Literal["light","dark","soft"] = "dark"`, `models_source_kind: Literal["internet","folder","drive_zip"] = "internet"`. All three are optional, so every existing answers file stays valid. `InstallerAnswers` inherits them.
- **Why:** the setup window asks for the owner's first name (dashboard greeting), the theme, and where the models come from (download, a folder, or the zips from the Google Drive models folder).
- **Differences from the design doc:** `ui_theme` also allows `"soft"` (the third dashboard theme) and the Drive kind is spelled `drive_zip` (snake case, like every other value in this file), not `drive-zip`.
- **Status:** applied in 060cbe0

## CR-C1-UI-2: error codes NS-E113 and NS-W403
- **From:** installer UI design (`docs/design/installer-ui.md` section 5.5 and the error table); owner decisions of 2026-11-02
- **Change:** add to `contracts/errors.json`: `NS-E113` "Antivirus or another program blocked a file Nightshift needs." (fix: add the Nightshift folder to the antivirus allow list, then retry) and `NS-W403` "The setup test video did not finish." (fix: setup can continue; run Diagnostics after install).
- **Why:** the setup window maps a file that was blocked or removed right after download (zip item missing after extraction, `PermissionError`, winerror 225) to a single code with a fix, and the final test video is allowed to fail without failing setup.
- **Status:** applied in 3342839

## CR-C1-UI-3: manifest `bins.webview2` and the `TBD-release` rule
- **From:** installer UI design (`docs/design/installer-ui.md` section 5.5) and the app host; owner decisions of 2026-11-02
- **Change:** `bins.webview2` is pinned exactly as `CR-C2-host` below specifies (real sha256, keys `{version, url, sha256}`; members and destination in `downloader.py`). In addition `BinPin` gains `optional: bool = False`, and the placeholder `sha256` `"TBD-release"` is accepted only on entries marked `optional: true` (`tools/resolve_manifest.py --check` skips the hash test for such an entry only; `tests/test_resolve_manifest.py` and `tests/test_contracts_ui.py` accept it for no other; the model does not reject it, because `tests/test_webview2_pin.py` builds a plain `BinPin` with the placeholder). Rule documented in `docs/REPO_SETTINGS.md` after the `bins.qwentts` row. `manifest.schema.json` regenerated.
- **Why:** the owner asked for the TBD rule to be enforced rather than implied; the real hash for `webview2` arrived (CR-C2-host) so the entry does not use it today.
- **Differences from the design doc:** the doc sketched a `files` map of per-DLL hashes; the whole `.nupkg` is hash-checked instead. An earlier draft of this CR added `extract` and `dest` to `BinPin`; dropped because `CR-C2-host` keeps them in code.
- **Status:** applied in ccf6c7b

## CR-C3-UI-4: `Status` model with an optional `gpu` object
- **From:** dashboard UI (GPU and VRAM meters, HANDOFF "UI gaps"); owner decisions of 2026-11-02
- **Change:** new `ns_common.models.Status` (exported as `contracts/schemas/status.schema.json`) describes `GET /api/status` and the `status` SSE event, with sub-models `StatusEngine`, `StatusService`, `StatusDisk`, `StatusChannel`, `StatusLastError` and the new `GpuStatus` `{name: str, load_pct: float|None, vram_used_mb: int|None, vram_total_mb: int|None, temp_c: float|None}`. `Status.gpu` is optional (`None` = not reported). `engine.vram_gb` and `services[].vram_mb` are kept unchanged. The hand-written TypeScript in `ns_dashboard/ui/src/lib/types.ts` gets `GpuStatus` and `Status.gpu?`.
- **Why:** the Home and Queue GPU meters need load, VRAM used and total, and temperature; there was no schema for the status object at all.
- **Not done here:** `ns_dashboard/svc_engine.py` does not fill `gpu` yet (it needs a GPU probe in the supervisor; C9/C2 follow-up), and `ns_dashboard/static` is not rebuilt (the TypeScript change is type-only; the next UI PR rebuilds).
- **Status:** applied in c175bdd

## CR-C3-UI-5: job request `schedule_at` (null = run now)
- **From:** dashboard Create wizard, "When" step; owner decisions of 2026-11-02
- **Change:** new `ns_common.models.JobRunRequest` (`contracts/schemas/job_run_request.schema.json`) for `POST /api/jobs/run`: `channel: str`, `format: str | None = None`, `count: int | None = None` (1..20), and the new optional `schedule_at: datetime | None = None`. Rule: **`null` (or absent) = run now**. A time without a zone is the PC's local time.
- **Why:** the wizard's last-but-one step lets the owner pick a start time; the request had no field for it.
- **Not done here:** `ns_dashboard.svc_jobs.JobsService.create_run` still ignores the key (it reads the body as a dict), so the UI must not send `schedule_at` until C9 honours it (hold the job until that time, then enqueue). The clip request (`POST /api/jobs/clip`, multipart) gets the same field when its model is written (done: `JobClipRequest`, CR-C9-1).
- **Status:** applied in d2a6f4f

## CR-C3-UI-6: channel stats `qc_pass_rate` and `qc_pass_target`
- **From:** dashboard Channels page (QC pass-rate meter); owner decisions of 2026-11-02
- **Change:** new `ns_common.models.ChannelStats` (`contracts/schemas/channel_stats.schema.json`), the `stats` object of a channel: `week`, `watched_pct`, `spark` (as the page already reads them) plus `qc_pass_rate: float | None = None` (0..1; `None` = no finished videos yet) and `qc_pass_target: float = 0.9` (0..1). `ns_dashboard/ui/src/lib/types.ts` `Channel.stats` gets both fields.
- **Why:** the Channels page shows how many of a channel's videos passed QC against a target line; the stats object had no field for it.
- **Not done here:** `GET /api/channels` does not fill `stats` at all yet (the page only has demo data), so C9 fills `qc_pass_rate` from the `outputs`/QC tables when it adds the stats object.
- **Status:** applied in b97b36d

## CR-C3-UI-7: voice design and clone endpoints, and `NS-E312`
- **From:** dashboard Library (voice design and clone, `api.voiceDesign` / `api.voiceClone` stubs, BUILD_SPEC J6); owner decisions of 2026-11-02
- **Change:** new models (schemas `voice_design_request`, `voice_clone_request`, `voice_make_response` under `contracts/schemas/`): `POST /api/voices/design` takes `VoiceDesignRequest {name, description, test_line}`; `POST /api/voices/clone` takes `VoiceCloneRequest {name, sample_path, consent: true, test_line}` (`consent` is `Literal[True]`, so `false` or a missing value fails validation); both answer `VoiceMakeResponse {voice_id, preview_wav_path}`. New error code `NS-E312` "Voice sample too short or noisy." (fix: record a longer, clean sample of one speaker, no music or background noise, and try again).
- **Code number:** the owner suggested `NS-E330` "if the 33x range is TTS". It is not: `errors.json` uses 30x for text, 31x for TTS (`NS-E310`, `NS-E311`, `NS-W301`, `NS-W302`), 32x for studio, and 33x is unused. The code therefore takes the next free TTS number, `NS-E312`.
- **Why:** the endpoints are planned but had no contract; the Library UI cannot be wired until request, response and the failure code are fixed.
- **Not done here:** the endpoints themselves (C9 route plus C4 `ns_tts` VoiceDesign / Base calls). `sample_path` is a path on this PC, so the UI uploads the recording first (existing chunked upload) and sends the stored path; the multipart variant in the `api.ts` stub is superseded.
- **Status:** applied in 23f3377

## CR-C2-host: `bins.webview2` (Microsoft.Web.WebView2 NuGet package for the frameless app host)
- **From:** app host builder (`launchers/nightshift-host.ps1`, `docs/design/installer-ui.md` section 6)
- **Change:** `manifest.json` gains `bins.webview2`, shape `{version, url, sha256}` (`BinPin` is strict, so the extract list and destination live in `installer/stage2/downloader.py` `WEBVIEW2_MEMBERS`, not in the file):
  ```json
  "webview2": {
    "version": "1.0.2903.40",
    "url": "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/1.0.2903.40/microsoft.web.webview2.1.0.2903.40.nupkg",
    "sha256": "ef128016dd1e51c59178c827ed5b8aa3322c57afa8675d930f8109505542ad74"
  }
  ```
  The installer verifies the `.nupkg` (a zip, 8,769,233 bytes) and extracts only `lib/net462/Microsoft.Web.WebView2.Wpf.dll`, `lib/net462/Microsoft.Web.WebView2.Core.dll` and `runtimes/win-x64/native/WebView2Loader.dll` into `<home>\bin\webview2\` (this package version has no `lib/net45`; the code falls back to it for older versions).
- **Why:** the host needs the three DLLs; nothing binary is committed. The sha256 was computed from the downloaded package inside a GitHub Actions run (not on the authoring laptop); the package listing was read in the same run (`lib/net462/*.dll`, `runtimes/win-x64/native/WebView2Loader.dll` present).
- **Status:** applied in ccf6c7b (`manifest.json` `bins.webview2`, on `feat/c3-ui-contracts`). The pin, the installer unpack (`WEBVIEW2_MEMBERS`) and the host are all on main; a PC where the DLLs or the runtime are missing still falls back to the Edge/Chrome `--app=` window (host exits 3). Nothing left open; the window icon is covered by CR-C9-brand-icon.

## CR-C1-UI-8: the setup window's UI preferences file (`config/ui_prefs.json`)
- **From:** installer web wizard (`docs/design/installer-ui.md` section 5.5; the `ui` block of `runtime.json` was not added)
- **Change:** step `s07_configs` writes `<home>\config\ui_prefs.json`: `{"owner_name": str | null, "theme": "dark" | "light" | "soft"}`, copied from `owner_name` and `ui_theme` of the answers. The file is installer-owned and not part of any schema. The dashboard (C9) may read it for the greeting name and the default theme; until it does, nothing breaks.
- **Why:** the setup window asks for the owner's first name and the theme, and the dashboard has nowhere else to find them.
- **Status:** open (C9 to read it; no contract change needed).

## CR-C9-brand-icon: app icon for the host window and the shortcuts
- **From:** dashboard brand pass (branch `feat/c9-brand-and-wiring`); owner decision of 2026-11-02 (owner logo vectorized, see `D:\NightShift-refs\logo-work\owner-vector\README.md`)
- **Change:** C2 sets `Window.Icon` in `launchers/nightshift-host.ps1` and C1 sets the icon of every shortcut the installer creates (Dashboard, Studio, Outbox, Update, Uninstall) to `ns_dashboard\static\brand\nightshift.ico` (7 sizes, 16 to 256 px, 372 KB; the mark on the `#0b0b1c` squircle). The file ships in the bundle with the static UI; resolve it relative to the install folder, and fall back to the shortcut's default icon when it is missing. The same folder holds `favicon.svg` and the PNG set (`icon-16` to `icon-256`) if a PNG is easier for WPF (`BitmapFrame` from `icon-256.png` also works).
- **Why:** the brand slot, favicon and in-page marks are done in the UI; the window icon and shortcut icons are the remaining places the old placeholder shows, and they live in C1/C2 files (`launchers/`, `installer/`) that a C9 branch may not edit.
- **Not done here:** the PowerShell and installer edits. The owner asked for a copy at `launchers/nightshift.ico`; `dev/check_scope.py` rejects `launchers/` on a `feat/c9-*` branch, so the file was not copied there. If C1/C2 prefer it next to the launcher, copy it from `ns_dashboard/static/brand/nightshift.ico` in their own branch.
- **Status:** applied on `feat/release-0.1.0`: `launchers/nightshift-host.ps1` sets `Window.Icon` from `<app>\ns_dashboard\static\brand\nightshift.ico` (largest frame; no icon when the file is missing); installer step `s09_shortcuts_scheduler` passes the same file to `winenv.create_shortcut`, which sets `IconLocation` (every shortcut the installer creates is a Desktop shortcut; there are no Start-menu shortcuts yet, and a missing file keeps the default icon); `dev/make_bundle.ps1` lists the `.ico` in its required entries. No copy at `launchers/nightshift.ico`: the host and the installer read the one in `static/brand`.

## CR-C9-1: backend gaps behind the UI contracts (GPU status, voices, scheduling, QC pass rate)
- **From:** C9 backend builder (`feat/c9-backend-gaps`); implements CR-C3-UI-4 to CR-C3-UI-7
- **Change (request, nothing applied here):**
  1. `contracts/errors.json`: add `NS-E313` "Voice cloning needs the owner's consent." (hint: "Tick the box that says you have the right to use this voice, then try again."). `POST /api/voices/clone` answers `422` with the dashboard code `consent_required` until it exists, because no existing code fits and `errors.json` is protected.
  2. `ns_common.models.Channel`: optional `qc_pass_target: float = Field(default=0.9, ge=0, le=1)`. `ns_dashboard.channel_stats.qc_pass_target` already reads the attribute when a profile has it (`Channel` forbids extra keys, so a profile cannot carry it today) and falls back to 0.9.
  3. `ns_common.models.VoiceMakeResponse`: if voice making should become a job later (VoiceDesign model load plus synthesis can take about a minute), add `job_id: str | None = None`. Today the two routes are synchronous and answer exactly `{voice_id, preview_wav_path}`.
  4. `POST /api/jobs/clip` also honours `schedule_at` (same rules as `JobRunRequest`); the clip request has no model yet, so please add `schedule_at: datetime | None = None` to it when one is written.
  5. The owner suggested `NS-E330` for a bad voice sample; CR-C3-UI-7 already landed `NS-E312` ("Voice sample too short or noisy."), which is what the code raises.
  6. Scope guard: `dev/check_scope.py` allows branch `feat/c9-*` only `ns_dashboard/` plus `tests/`. This work also changes `ns_supervisor/` (GPU probe, scheduled-job wake-up), `ns_tts/` (clone sample checks), `OPERATOR.md` and `docs/OPERATOR_CLI.md`, as the task asked. The PR's scope-check job reports those paths until the orchestrator accepts or re-scopes them.
- **Why:** the four UI gaps of CR-C3-UI-4..7 needed backend fields and routes; the points above are the contract-side edges found while building them.
- **Status:** applied on `feat/release-0.1.0`: points 1, 2 and 4 landed (`NS-E313` in `errors.json`, and `POST /api/voices/clone` answers it with 422 instead of `consent_required`; optional `Channel.qc_pass_target` with `ns_dashboard.channel_stats.qc_pass_target` reading it directly; new `ns_common.models.JobClipRequest` with `schedule_at` and `contracts/schemas/job_clip_request.schema.json`). Point 3 stays as written: voice making is synchronous, so `VoiceMakeResponse` keeps `{voice_id, preview_wav_path}` until it becomes a job. Point 5 is informational. Point 6 is the orchestrator's call.

## CR-C1-UI-9: `Answers.llm_custom` (a story model the owner names) and `NS-W113`
- **From:** installer model picker (`feat/release-installer-picker`); owner decision of 2026-11-02 (the picker gets an "Other" card)
- **Change:** `ns_common.models` gains `LlmCustom {repo, file, size}` (`repo` as `owner/name`, `file` 6 to 240 characters, `size` in bytes above 0) and `Answers.llm_custom: LlmCustom | None = None`; `contracts/schemas/answers.schema.json` carries both. `installer/answers.example.json` ships `"llm_custom": null`. When set, it replaces the tier's LLM: the installer downloads `https://huggingface.co/<repo>/resolve/main/<file>` to `models\llm\` with **no SHA256** (allowed only for this user-supplied model) and `runtime.json` points `llama.model_path` at it (`runtime_cfg.llm_file`, no `s07` change). The setup window resolves `size` with a HEAD request and refuses files over 15.5 GB; `installer.stage2.answers.build_answers` enforces the same limit and the `.gguf` suffix for unattended answers. New warning code `NS-W113` "A custom model was downloaded without a checksum." in `contracts/errors.json` (raised by `s03_download_models`, plain text in `operator_copy.json` and the window's `copy.ts`).
- **Why:** the owner wants to try a model the manifest does not list. The pinned-hash rule (`ENGINEERING.md` rule 2) cannot hold for a file the owner picks, so the exception is explicit, narrow and logged.
- **Status:** applied on `feat/release-installer-picker` (`ns_common/models.py`, `contracts/schemas/answers.schema.json`, `contracts/errors.json`)

## CR-GAPS-1: protected-path edits of the engine-side gap PR (`feat/release-gaps-engine`)
- **From:** release builder closing `docs/design/system-gaps.md` rows 1, 4, 5, 9, 14, 15, 16, 19, 20, 25, 29, 31
- **Change (all on a `release-*` branch, minimal, no schema shape change):**
  1. `ns_common.models.WhisperCfg` defaults: `model` `small.en` -> `distil-large-v3`, `compute_type` `float16` -> `int8` (gap 31). `contracts/schemas/runtime.schema.json` updated by hand to equal `model_json_schema()` (the hygiene test compares them). The installer still writes `float16` for the GPU profile.
  2. `ns_common.config.notifications_enabled(paths)`: reads `notifications` from `config/dashboard.json` (the dashboard's Settings file), default on. One reader for the nightly toast (C7) and the weekly update toast (C2) instead of two copies of the file format.
  3. `ns_common.tts_client.TtsClient`: a 4xx/5xx answer whose JSON is `{"error": "NS-E311"}` raises `NS-E311` instead of the generic `NS-E301` (gap 20). Other failures are unchanged.
  4. No change to `contracts/errors.json`: `NS-E311` and `NS-E408` already exist and now have raise sites.
- **Other new interfaces (not contracts, listed for reviewers):** supervisor `POST /run_finished` (additive route, gap 25); `config/pending_voice.json` written by installer step `s07_configs` and read by `ns_studio.voice_setup` (plain JSON, keys `channel, mode, name, description | ref_audio_path, ref_text, rights_confirmed`); `config/update_check.json` (weekly check state).
- **Why:** each is the smallest place the fix could live; the first two keep the setup, the spec and the code in agreement, the third lets the studio show the re-create hint.
- **Status:** applied on `feat/release-gaps-engine`.

## CR-STUDIO-1: embed server batch size and embed input window (clean-clone review, studio row 2)
- **From:** studio review builder (`feat/release-review-studio`)
- **Change (applied, all on a `release-*` branch):**
  1. `ns_common.models.EmbedCfg` gains `ctx_size` and `batch_size` (both default 2048); `contracts/schemas/runtime.schema.json` updated by hand to equal `model_json_schema()`.
  2. `ns_common.config.embed_server_args(cfg)` returns `["-c", ctx, "-b", batch, "-ub", batch]`.
  3. `ns_common.embed_client`: `EMBED_CHARS` (1500), `embed_window(text)`, `approx_tokens(text)` and `FakeEmbed(max_tokens=512)`, which raises `NS-E301` for an input over the estimate, as `llama-server` does with its default `-ub 512`.
- **Why:** `SelfSimilarity` embedded a whole master (2,000 to 3,500 tokens) and the embed server answered HTTP 500 "input is too large to process", failing every `master_first` job at `policy_text`. The studio now embeds the 1,500-character opening everywhere (the same input `ContinuityStage` stores), and the fake rejects longer input so the next regression is caught in CI.
- **Open for the runtime builder (A2):** `ns_supervisor/services.py` `_embed()` must append `*embed_server_args(cfg)` to the `llama-server --embeddings` command (one line; not edited here by instruction). Until then the server keeps `-ub 512`, which the 1,500-character window fits for English text (about 400 tokens).
- **Status:** applied on `feat/release-review-studio` (the supervisor line is open).

## CR-STUDIO-2: applied by the studio review builder (`feat/release-review-studio`)
- **From:** studio review builder. All on a `release-*` branch; `ns_common` and `contracts` edits are minimal.
- **Applied:**
  1. `ns_common/secrets_names.py` (new): `KEYRING_SERVICE`, `PEXELS_KEY`, `PIXABAY_KEY`, `KEY_NAMES`, `SHORT_NAMES`. The studio reads the keys through it. **Open for A2:** make `installer/stage2/secrets.py` and `ns_dashboard/adapters.py` import these instead of their own copies (`tests/test_secret_names.py` already fails if they drift).
  2. `ns_common/pacing.py` (new): `PACING_WPM`, `NOMINAL_WPM`, `pacing_speed(voice_speed, pacing)`. Every `TtsItem.speed` of a job is now `pacing_speed(voice.speed, combo.pacing)`; `TtsItem` already had `speed`, so no `TtsRequest` change was needed. **For the TTS builder:** speeds now range 0.7 to 1.3 per pacing (brisk is 1.13x, measured 0.90x), so the engine's speed handling must keep pitch (`atempo`), see review row 12.
  3. `ns_common.models`: `Channel.long_minutes` upper bound 14; `CaptionsOpts.remove_fillers` (default false); `PolicyCheckResult.tags_relevant` (optional); `EmbedCfg` (CR-STUDIO-1). The matching `contracts/schemas/*.schema.json` files were edited by hand to equal `model_json_schema()`.
  4. `ns_common/paths.py`: `Paths.exemplars` and `Paths.prompt_overrides` (both already in the backup trees).
  5. `ns_common/textstats.py`: `WORD_PATTERN`, `CJK_RANGES`; `word_count`, `split_sentences` and `normalize_tokens` are Unicode-aware.
  6. `ns_common/llm_client.py`: `FakeLlm` records the `temperature` of each call.
  7. `contracts/errors.json`: `NS-E327` (a job stopped on an unexpected problem), `NS-E410` (confirm you have the right to use this video). `contracts/policy/clip_misleading_claims.txt` (new list for clip metadata).
  8. Series: `ProduceRequest.minutes`; `series_context.json` now also holds `parts_total`, `title`, `short_titles`, `end_card`, `publish_not_before_days`, `minutes` and `design`; `DesignDoc.voice` is optional (None keeps the channel voice).
- **Touched outside `ns_studio` (coordinator's brief):** `ns_dashboard/studio_bridge.py` (`download_source(cancel_token=)`, `run_clip` passes `caption_style`, `run_series_part` pins the combo from the design). **Open for C:** call `CancelToken.cancel()` (`ns_studio.clipper.source`) from the job's Cancel handler for download jobs.
- **Needs the runtime builder (A2):** `ns_supervisor/services.py` `_embed()` must append `*embed_server_args(cfg)` (CR-STUDIO-1).
- **Spec follow-up for the orchestrator:** BUILD_SPEC 7.6 step 4 still describes the old drift ("ASR time vs interpolated time"); the code now measures caption start vs ASR start. `studio dashboard` (spec 15.9) does not exist and no code refers to it.
- **Status:** applied on `feat/release-review-studio`.


## CR-REVIEW-RUNTIME-1: protected-path edits of the installer/runtime review PR (`feat/release-review-runtime`)
- **From:** runtime/installer builder closing the clean-clone review rows 1 to 12, P1 to P4 and gaps 10, 14, 18, 19, 20, 27, 28, 37 to 39
- **Change (all on a `release-*` branch, no schema shape change):**
  1. `ns_common.uv.find_uv`: order is install folder, `NIGHTSHIFT_BOOTSTRAP_UV` (set by stage 1), `%LOCALAPPDATA%\Nightshift\bootstrap\uv\uv.exe`, PATH.
  2. New stdlib modules `ns_common/atomic.py` (temp file + `os.replace`, `.bak`, `.bad`), `ns_common/venv_extras.py` (the one table of extras per venv, read by setup and by `ns update`) and `ns_common/secret_file.py` (DPAPI file `config/secrets.dpapi`, fallback when Credential Manager is out of reach, e.g. an S4U nightly session; `load_secret(paths, name)` is what the nightly path should call after `keyring` returns nothing).
  3. `ns_common.config.save_install_state` is atomic and keeps `install_state.json.bak`; `load_install_state` falls back to it, then to an empty state, and keeps a torn file as `.bad`.
  4. `contracts/errors.json`: `NS-W205` (task registered for "only while logged on") and `NS-W206` (task not registered, setup went on).
  5. `pyproject.toml`: `installer*` joins the installed packages so `ns models get` can reuse the installer downloader.
- **Schedule source (decided with the dashboard builder):** `config/schedule.json` (`{run_time, task_name}`, read through `ns_supervisor.schedule_cfg.read_run_time`) is the one source of the nightly time. `ns schedule install` writes it after Windows accepted the task; the installer also writes it when registration failed, so the dashboard shows the time the owner chose together with an `applied: false` note.
- **New interfaces (not contracts):** `run/update_state.json` `{state, old, new, message, updated_at}` (detached `ns update`); `run/morning_note.json` `{notes: [{at, title, message}]}` written by `ns_supervisor.notify.toast_or_note` when the session has no desktop (the dashboard shows and clears it with `notify.read_notes/clear_notes`); `ns models get voicedesign|story_llm`; `ns update --git [--channel stable|main]`; `include_voicedesign` in answers.json.
- **Status:** applied on `feat/release-review-runtime`.

## CR-C4-6: voice engine contract fixes (`feat/release-review-tts`: review rows 1-4, 12, 13)
- **From:** TTS builder A (`ns_tts`, voice service start in `ns_supervisor/services.py`); protected paths edited on this `release-*` branch.
- **Change (applied here):**
  1. `contracts/errors.json`: new `NS-E314` "The voice design model is not installed." (hint: Settings > Models > Get voice design, or `ns models get voicedesign`; the 409 `missing_model` answers of `POST /v1/voices/design` now also carry `error` and `detail`), `NS-E315` "A voice could not be built from the recording or description." (qwen-codec or VoiceDesign failure, empty speaker data) and `NS-E316` "The embedding service did not return vectors." The owner suggested `NS-E33x` for embedding failures; as with `NS-E312`, 33x is not the TTS range (30x text, 31x TTS, 32x studio), so the next free TTS numbers are used. `NS-E310` (synthesis) and `NS-E311` (unusable stored voice) already existed and now have the raise sites listed in the review (row 13). `NS-E301` is again only the language model's code, `NS-E202` only service start-up.
  2. `ns_common.paths`: `TTS_HF_SUBDIR = "hf"` and `Paths.models_tts_hf` (`models\tts\hf`). `runtime.tts.model_dir` keeps meaning "the TTS models root" (the installer already writes `models\tts`); qwentts reads the GGUFs from it and the reference engine reads the safetensors from `<model_dir>\hf`. One folder on both sides.
  3. `ns_common.tts_client.TtsClient`: transport failures and non-JSON 5xx answers raise `NS-E310` (were `NS-E301`); a service answer carrying `NS-E310`, `NS-E311` or `NS-E315` keeps that code. `ns_common.embed_client.EmbedClient` raises `NS-E316` (was `NS-E301`).
- **voice.json (dashboard-facing), additive:** `files: [str]` lists the speaker files in the voice folder (`["ref.txt", "speaker.rvq", "speaker.spk"]` for qwentts, `["speaker.bin"]` for the PyTorch engine and the fake). Existing keys (`name`, `mode`, `description`, `language`, `engine`, `ref_text`, `rights_confirmed`) are unchanged; a folder without `files` is read as `speaker.bin`. `ns_dashboard` only reads `name` and `mode`, so nothing there has to change. `GET /v1/voices/{name}/embedding` answers a zip (`<name>.voice.zip`) when a voice has several files.
- **For C1 / C2 (installer, supervisor; not done here):** (a) `installer/stage2/fallback.py` should build its destination from `ctx.paths.models_tts_hf` instead of the literal `"hf"` (same folder today; `tests/test_tts_engine.py::test_installer_downloads_where_the_reference_engine_reads` fails if they drift). (b) `ns verify` / s08 should keep treating `qwen-tts.exe` as the binary to probe (`bin\qwentts`), not `qwentts.exe`. (c) The `tts` venv gets numpy, pydantic and pyyaml through torch, fastapi and transformers; `ffmpeg` must be on PATH for the service (speed change), as for the studio.
- **Why:** `QwenttsEngine` started a `qwentts.exe` that is not in the release zip with flags the upstream tools do not have, the fallback then ran in a venv without torch looking in a folder the installer never fills, and speed changed pitch.
- **Status:** applied on `feat/release-review-tts`.

## CR-LOOSE-1: `NS-W114` and `include_voicedesign` inference (`feat/release-loose-ends`)
- **From:** release builder, loose ends after the review round (coordinator addition to item 4)
- **Change:** `contracts/errors.json` gains `NS-W114` ("A designed voice was chosen but the voice design model is not being downloaded."). `installer.stage2.answers.InstallerAnswers.include_voicedesign` becomes `bool | None = None` (installer-only field, not in `ns_common.models.Answers`): absent means "infer", so a designed voice fetches the VoiceDesign talker in `s03_download_models`; an explicit `false` with a designed voice is honoured and warns `NS-W114`.
- **Why:** a designed voice cannot be created without the model; an unattended answers file that forgot the flag should not leave the partner with a voice that fails on first use.
- **Status:** applied on `feat/release-loose-ends`.

## CR-REREVIEW-C9-1: `Status.schedule_registered` (`feat/release-rereview-dashboard`)
- **From:** release builder, dashboard re-review (runtime re-review item: no nightly task, NS-W206)
- **Change:** `ns_common.models.Status` gains `schedule_registered: bool | None = None` and `contracts/schemas/status.schema.json` the same property. `GET /api/status` and `GET /api/settings` carry it: False when `config/schedule.json` does not exist, so Windows has no `Nightshift Daily` task.
- **Why:** the top-bar next-run chip and the Schedule card say "No nightly task; videos are made only while Nightshift is open" and offer Try again, instead of showing a run time nothing will start.
- **Status:** applied on `feat/release-rereview-dashboard`.

## CR-PRETAG-C9-1: `Status.schedule_mode` (`feat/release-0.1.1-pretag`)
- **From:** release builder, system-gaps row 3 (NS-W205: the task is registered "run only when the user is logged on")
- **Change:** `ns_common.models.Status` gains `schedule_mode: Literal["registered", "interactive_only", "none"] | None = None` and `contracts/schemas/status.schema.json` the same property. `schedule_registered` stays (true for `registered` and `interactive_only`). `config/schedule.json` gains `mode` (`registered` | `interactive_only`, written by `ns schedule install` from the PowerShell result; a file without it reads as `registered`); `ns_supervisor.schedule_cfg.read_schedule_mode` returns `none` without a valid file. `GET /api/status` and `GET /api/settings` carry it.
- **Why:** the top-bar chip and Settings say "Runs at 03:00 only while you are signed in" in a warning style; without it an owner who signs out instead of locking the screen gets no run and no hint. The next-run countdown is unchanged.
- **Status:** applied on `feat/release-0.1.1-pretag`.

## CR-DL-1: Settings > Models "Verify downloads" button (`feat/release-download-hardening`)
- **From:** release builder, download hardening item 4
- **Change:** `ns verify --downloads` exists now (`installer/stage2/verify_downloads.py`; exit 0 all fine, 1 something to fix, 2 no install; prints a table of ok / missing / bad hash with the fix for each). The dashboard has no route for it (`POST /api/models/get` only starts `ns models get`), and adding one means a server route, a `ns_dashboard/adapters.py` method, a UI button and a rebuilt `ns_dashboard/static`, which cannot be built on the authoring laptop. Proposed: C9 adds `POST /api/models/verify` that runs `ns verify --downloads` as a job and returns the table lines (status per file), and the Models card gets a "Verify downloads" button next to "Get voice design model".
- **Why:** the operator should be able to prove, from the dashboard, that nothing in the 20 GB is damaged.
- **Workaround until applied:** run `ns verify --downloads` in a terminal (documented in OPERATOR.md, "If a download stops").
- **Status:** open

## CR-DEVMODE-1: `dev_mode` in `Runtime`, `InstallState` and `GateReport` (`orchestrator/installer-dev-mode`)
- **From:** orchestrator, `INSTALL.cmd --dev` (docs/DEV_MODE.md)
- **Change:** `ns_common.models.Runtime`, `InstallState` and `GateReport` gain `dev_mode: bool = False`, and `contracts/schemas/runtime.schema.json`, `install_state.schema.json` and `gate_report.schema.json` the same property. `config/runtime.json` of a dev install says `"profile": "cpu-dev", "dev_mode": true`; `install_state.json` records it; `logs/verify-report.json` carries it. Every reader goes through `ns_common.devmode`.
- **Why:** a real end-to-end install on a PC with no GPU and no model: the supervisor starts the fake llama and embedding servers and the fake voice engine, the studio runs jobs on its in-process fakes, `ns verify` passes against them, and the report says so.
- **Status:** applied on `orchestrator/installer-dev-mode`.

## CR-H7-1: `ns_tts/sapi.py` and the fake voice's coded warning (`wf/h7-sapi-fake-voice`)
- **From:** builder, head order H-7 (the fake voice speaks the real text with Windows SAPI), defect D-26
- **Change (two small things on a protected path, for an `orchestrator/*` branch):**
  1. `pyproject.toml` `[tool.ruff.lint.per-file-ignores]` gains `"ns_tts/sapi.py" = ["TID251"]`, next to `ns_tts/engine.py`. The module starts Windows PowerShell with `subprocess.run` and a list of arguments (never a shell) to drive `System.Speech.Synthesis.SpeechSynthesizer`.
  2. `contracts/errors.json` gains a warning for "the fake voice made tone bursts": proposed `NS-W303` "The fake voice made tone bursts, which speech recognition cannot align." (hint: "This PC has no Windows speech voice. Install one (Settings > Time & language > Speech) or expect NS-E403 at the captions stage; it is the test voice, not a product fault."). `ns_tts.engine.FakeEngine` and `ns_studio.media.fake_voice.note_voice_engine` log the event `fake_voice_tones_only` and would add `code="NS-W303"`.
- **Why:** no existing `NS-W` code means this (`NS-W301` is the PyTorch fallback of the real engine), and the branch is `wf/*`, which may not edit `contracts/` or `pyproject.toml`.
- **Workaround until applied:** the `subprocess` import carries `# noqa: TID251`; the warning is logged uncoded as the event `fake_voice_tones_only` and is in `/health` (`voice_warning`), `audio/voice_engine.json` and the NS-E403 detail.
- **Status:** open
