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

## CR-C2-1: fastapi in the `dev` extra (same need as CR-C4-1)
- **From:** C2 runtime / Task 5
- **Change:** add `"fastapi>=0.115"` to `[project.optional-dependencies].dev` in `pyproject.toml`. `httpx` is already a core dependency (the starlette `TestClient` and `SupervisorClient` use it), `uvicorn` is only imported by `python -m ns_supervisor.app` (lazily), and `psutil` is not used by C2 (process control is `subprocess` + Job Object + `taskkill`), so neither is requested. After it lands, delete the `# pyright: ignore[reportMissingImports]` comments in `ns_supervisor/app.py` and the `pytest.importorskip("fastapi")` in `tests/test_supervisor_app.py`.
- **Why:** CI installs only `.[dev]`; without fastapi the HTTP-layer tests (`tests/test_supervisor_app.py`) are skipped and pyright cannot resolve the imports. All supervisor logic is tested without it through `ns_supervisor.core`.
- **Status:** open

## CR-C2-2: supervisor `POST /events` (publish a job event)
- **From:** C2 runtime / Task 5
- **Change:** add to spec 15.3: `POST /events {kind:"service"|"job", data:{...}}` publishes one event to every `GET /events` subscriber. Implemented additively in `ns_supervisor/app.py` and `SupervisorClient.publish_event`. Also additive: `GET /events?limit=N` closes after N events (test aid), `GET /health` returns `{ok:true}`, `/status` carries extra keys (`gpu_service`, `waiting`, `shutting_down`, `uptime_s`, and per service `owners`, `crashes`, `error`, `elapsed_s`).
- **Why:** the dashboard status strip needs job progress (spec 6.2), and only the studio process knows the stage; `/events` as specified is read-only.
- **Status:** open

## CR-C2-3: error codes the runtime tools need
- **From:** C2 runtime / Tasks 6-10
- **Change:** add to `contracts/errors.json`: `NS-E112` (faster-whisper cannot load on CUDA: cuBLAS 12 / cuDNN 9 DLLs; named in spec 15.12 and ENGINEERING 6 but missing from the file), `NS-E704` "The scheduled task could not be registered, removed or read" (hint: run PowerShell as the same user; see the detail), `NS-E705` "The file is not a valid Nightshift backup" (restore). Until then `ns verify` reports a failed whisper load as `NS-E202` with the cuBLAS/cuDNN fix in the detail, schedule errors are printed without a code, and `ns restore` raises `NS-E701`.
- **Why:** the operator should see a code that names the real problem.
- **Status:** open

## CR-C2-4: `ruff` TID251 per-file ignores for the supervisor package
- **From:** C2 runtime / Task 2
- **Change:** when TID251 bans `subprocess` (ENGINEERING 8), add `ns_supervisor/*.py` to the per-file-ignores and to the hygiene allow-lists for subprocess and `httpx`. C2 starts services, runs `taskkill`, `nvidia-smi`, `uv`, `powershell`, `studio` and `git`.
- **Why:** section 8 names the supervisor as the place services are spawned; the listed exception paths do not yet include the extra tools.
- **Status:** open

## CR-C2-5: per-model llama flags for two-model mode (`ns tune`)
- **From:** C2 runtime / Task 9
- **Change:** `Runtime.llama.flags` is shared by both aliases. To tune the `story` model separately, add `LlamaCfg.story_flags: LlamaFlags | None = None` (None means use `flags`) and regenerate the runtime schema. C2 will then tune each model and pass the right flags per alias.
- **Why:** spec 3 says tune runs per model in two-model mode; today `ns tune` tunes the primary (`code`) model only and says so.
- **Status:** open
