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

## CR-C9-1: dashboard dependencies, packaging and type-check scope
- **From:** C9 ns_dashboard / Tasks 5-9
- **Change:** in `pyproject.toml` (a) add `"fastapi>=0.115"`, `"python-multipart>=0.0.9"` and `"Pillow>=10"` to the `dev` extra (`httpx` is already a core dependency and is what `fastapi.testclient` needs); (b) add an extra `dashboard = ["fastapi>=0.115", "uvicorn>=0.30", "python-multipart>=0.0.9", "Pillow>=10", "keyring>=25"]` for the target machine; (c) add `"ns_dashboard*"` to `[tool.setuptools.packages.find].include` and `ns_dashboard = ["static/*"]` to `[tool.setuptools.package-data]`; (d) add `"ns_dashboard"` to `[tool.pyright].include`.
- **Why:** CI installs only `.[dev]`, so until (a) lands every `TestClient`, multipart and Pillow test of the dashboard is skipped (the framework-free service layer, job worker, uploads, range parsing, log tail and templates are tested without them). (c) is needed for the bundle to ship the UI; (d) puts the dashboard under pyright like the other packages (the code is written to pass `standard` mode).
- **Status:** open

## CR-C9-2: `sources.rights_confirmed` and `dashboard_jobs.result_json`
- **From:** C9 ns_dashboard / Task 6
- **Change:** in `contracts/schema.sql` add to `sources`: `rights_confirmed INTEGER NOT NULL DEFAULT 0, rights_confirmed_at TEXT`; add to `dashboard_jobs`: `result_json TEXT`. Add a pydantic `DashboardJob` model (fields as the API object `J`: job_id, kind, channel, status, progress, message, stage, created_at, started_at, finished_at, params, result) to `ns_common/models.py` and export `dashboard_job.schema.json` (spec 15.6 lists the schema, the file does not exist yet).
- **Why:** spec 19.1 says the rights confirmation is stored on the `sources` row, which has no column for it. Until then the dashboard writes `imports/clipper/<source_id>/rights.json` and keeps `{"params":..., "result":...}` inside `dashboard_jobs.params_json`; C8 can read `rights.json` and set the column when it creates the `sources` row.
- **Status:** open

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
- **Status:** open
