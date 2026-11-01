# Nightshift M3 - C9 Dashboard (local FastAPI API + offline single-page UI) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Windows-local web dashboard (`python -m ns_dashboard.server`, 127.0.0.1:8100) that lets the owner see engine state, review and approve outputs, drop a video into the Clipper, edit channels, read logs, import analytics and change settings, with one background worker that runs `run|clip|learn|verify` jobs through small adapters. Everything is testable on a machine with no GPU, no models and no running supervisor.

**Architecture:** All behaviour lives in FastAPI-free modules (`handlers.py` service layer, `jobs.py`, `uploads.py`, `media.py`, `logs_tail.py`, `templates.py`, `supervisor_link.py`, `adapters.py`) so pytest in CI exercises them even before `fastapi` is a dev dependency. `server.py` is a thin routing layer (pure ASGI loopback middleware, route -> handler, range streaming, SSE) and is covered by `TestClient` tests guarded with `pytest.importorskip("fastapi")`. Heavy collaborators (orchestrator `run`, Clipper, learn import, doctor bundle, keyring, TTS preview, open-folder, schedule) sit behind Protocols in `adapters.py` with lazy imports and fakes in `fakes.py` that write real files. The UI is vanilla HTML/CSS/JS, no bundler, no CDN, one `api.js`-style fetch layer inside `app.js`.

**Tech Stack:** Python 3.12, FastAPI + uvicorn (target and dev), python-multipart, httpx (loopback only, inside `supervisor_link.py`), Pillow (template previews; lazy), SQLite via `ns_common.db`, FFmpeg through `ns_common.ffmpeg.run` (thumbnails), vanilla JS (ES2020), CSS custom properties.

**Spec:** `D:\NightShift\BUILD_SPEC.md` v2 - section 6.2 (lifecycle), 15.3 (supervisor API incl. `/shutdown /warmup /events`), 19.3 (clip templates), 20 (all), 22 (J2-J8, J10).

## Global Constraints

- Everything in the Slice 0 plan's Global Constraints and ENGINEERING.md sections 4, 5, 6, 8 apply. Pinned to the `contracts-m0` tag; anything missing is a CR in `contracts/CHANGE_REQUESTS.md`.
- **Nothing executes locally.** Verification is the CI run URL of branch `feat/c9-dashboard`.
- Owned paths: `ns_dashboard/` (including `static/`), `tests/test_dashboard_*.py`, `dev/gate_m3_dashboard.py`, this plan. Never edit `ns_common/`, `contracts/` (except appending to `CHANGE_REQUESTS.md`), `pyproject.toml`, `.github/`, `launchers/` or other components' directories.
- Loopback only: bind `127.0.0.1`; reject any request whose `Host` (or `Origin`) is not `127.0.0.1`, `localhost` or `[::1]`. No authentication.
- No `print(` outside the `__main__` entry; logging through `NsLogger("dashboard", paths.logs)`. Secrets (API keys) are never logged and never returned by the API; only `{set: bool}`.
- No subprocess in `ns_dashboard` except through `ns_common.ffmpeg.run`; opening a folder uses `os.startfile` behind the `FolderOpener` adapter.
- HTTP client imports (`httpx`) appear only in `ns_dashboard/supervisor_link.py` (loopback supervisor). CR asks C10 to add it to the hygiene allow-list.
- No literal `NS-E`/`NS-W` codes in dashboard source that are not in `contracts/errors.json`; API errors use `{"error": {"code", "message", "hint"}}` where `code` is an `NS-` code carried from an `NsError`, or a lowercase dashboard code (`not_found`, `rights_required`, ...).
- Heavy imports (`PIL`, `keyring`, `fastapi` outside `server.py`) only inside functions.
- UI: no external URL in any `src`/`href`, no inline event handlers or inline `<script>` (the server sends a CSP of `default-src 'self'`), no `window.confirm/alert/prompt`, dark default with light via `prefers-color-scheme` and `data-theme`, usable at 360 px width.
- Worker allocation: Tasks 1-7, 9 -> `sonnet`-grade engineering (written by the C9 builder); Task 8 (static UI) -> delegated to a `sonnet` agent against Appendix A.

---

## File structure created by this plan

```
ns_dashboard/
  __init__.py
  security.py         # is_loopback_host, safe_join, origin check helpers
  events.py           # EventHub (thread-safe pub/sub), iter_sse()
  state.py            # AppState, LockedDb, dashboard.json settings, DDL safety net
  jobs.py             # JobStore, JobControl, JobWorker (single FIFO thread), JobCancelled
  adapters.py         # Protocols + lazy real loaders (run, clip, learn, verify, doctor, keyring, tts, folder, schedule)
  fakes.py            # Fake* adapters, FakeSupervisor (stdlib http.server), seed helpers
  supervisor_link.py  # SupervisorLink (status/warmup/shutdown/events), HttpAcquirer
  uploads.py          # UploadManager: 8 MB chunks, resumable by offset, sha256 at end
  media.py            # parse_range, open_range, thumbnail cache (ffmpeg)
  logs_tail.py        # tail logs\*.jsonl with component/level filters
  templates.py        # clip templates (yaml or builtin), normalisation, Pillow previews
  channels.py         # profile validation + distinctness warnings
  handlers.py         # Dashboard service layer: every /api operation as a method
  server.py           # FastAPI app factory, middleware, routes, SSE, __main__
  static/index.html app.css app.js
tests/
  test_dashboard_security.py test_dashboard_events.py test_dashboard_jobs.py test_dashboard_adapters.py
  test_dashboard_uploads.py test_dashboard_media.py test_dashboard_logs.py test_dashboard_templates.py
  test_dashboard_channels.py test_dashboard_handlers.py test_dashboard_api.py test_dashboard_static.py
  test_dashboard_gate.py
dev/gate_m3_dashboard.py
```

---

### Task 1: Foundations - security, events, state (C9)

**Files:** Create `ns_dashboard/__init__.py security.py events.py state.py`, `tests/test_dashboard_security.py`, `tests/test_dashboard_events.py`.

**Interfaces:**
```python
# security.py
def is_loopback_host(host_header: str) -> bool        # strips port; accepts 127.0.0.1, localhost, ::1, [::1]
def is_loopback_origin(origin: str | None) -> bool    # None/"null" -> True (non-browser callers); else the netloc must be loopback
def safe_join(root: Path, *parts: str) -> Path        # resolves; raises ApiError-free ValueError if outside root
# events.py
class EventHub: publish(kind: str, data: dict) -> None; subscribe() -> Subscription
class Subscription: get(timeout: float) -> dict | None; close() -> None
def iter_sse(sub, status_fn, *, heartbeat_s: float, limit: int | None, stop: threading.Event) -> Iterator[str]
# state.py
class LockedDb: query/one/execute/executemany/tx()  (RLock around ns_common.db.Db)
@dataclass class AppState: paths, db, log, hub, link, adapters, store, worker, settings_file ...
def load_dashboard_settings(paths) -> dict ; save_dashboard_settings(paths, data) -> dict   # config\dashboard.json
```

- [ ] **Step 1: Write failing tests** (host variants incl. `127.0.0.1:8100`, `LOCALHOST`, `[::1]:8100`, `evil.com`, `127.0.0.1.evil.com`, empty; `safe_join` rejects `..`; hub fan-out to two subscribers, `iter_sse` yields `event:`/`data:` frames, honours `limit`, emits a `status` heartbeat on idle; `LockedDb.tx` rolls back on error; settings round trip with defaults).
- [ ] **Step 2: Commit/push -> CI** (tests reference modules that do not exist until Step 3; not pushed alone).
- [ ] **Step 3: Implement** the three modules.
- [ ] **Step 4: Commit** `feat(c9): dashboard security helpers, event hub, state`.

### Task 2: Job store and worker (C9)

**Files:** Create `ns_dashboard/jobs.py`, `tests/test_dashboard_jobs.py`.

**Interfaces:**
```python
KINDS = ("run", "clip", "learn", "verify"); STATUSES = ("queued","running","done","failed","cancelled")
class JobCancelled(Exception)
@dataclass class JobRecord: job_id, kind, channel, status, progress, message, created_at, started_at, finished_at, params, result, cancel_requested ; to_dict()
class JobStore: create(kind, channel, params) -> JobRecord; get; list(status, kind, limit); counts(); recover_interrupted()
class JobControl: job_id, kind, channel, params; progress(frac, message=None); cancelled() -> bool; check_cancel(); stages(names) -> Iterator[str]; waiting(message)
Handler = Callable[[JobControl], dict | None]
class JobWorker:
    def __init__(store, handlers, hub, log, *, gpu_gate=None, sleeper=time.sleep)
    start(); stop(timeout=5.0); enqueue(job_id); cancel(job_id) -> str; cancel_running() -> str | None
    run_next() -> bool       # runs exactly one queued job synchronously (what the thread loops on)
    wait_idle(timeout) -> bool
```
Rules: FIFO by insertion; progress/message written to `dashboard_jobs` per stage and published as `dashboard_job` events; `ctl.stages()` checks the cancel flag before each stage; a queued job cancels immediately; a running job finishes its current stage then ends `cancelled`; handler exception -> `failed` with the `NsError` text; `gpu_gate(ctl)` (optional) blocks while the supervisor GPU is held by someone else and reports "Waiting for GPU (held by X)"; interrupted `running` rows are failed at startup (`recover_interrupted`).

- [ ] **Step 1: Write failing tests** (FIFO order with two jobs, progress rows written per stage, cancel queued, cancel running between stages, failure path records message, `recover_interrupted`, GPU gate waits then proceeds using an injected sleeper, `params_json` layout `{"params":..., "result":...}`).
- [ ] **Step 2/3: Implement** `jobs.py`.
- [ ] **Step 4: Commit** `feat(c9): dashboard job store and FIFO worker`.

### Task 3: Adapters, fakes, supervisor link (C9)

**Files:** Create `ns_dashboard/adapters.py fakes.py supervisor_link.py`, `tests/test_dashboard_adapters.py`.

**Interfaces:**
```python
# adapters.py  (all real loaders import lazily; missing symbol -> AdapterUnavailable(message naming it))
class RunAdapter(Protocol):    run(ctl, channel: str, fmt: str, count: int | None) -> list[str]        # output ids
class ClipAdapter(Protocol):   clip(ctl, source_path: Path, source_id: str, params: dict) -> list[str]
class LearnAdapter(Protocol):  import_csv(ctl, csv_path: Path) -> dict ; summary() -> dict
class VerifyAdapter(Protocol): verify(ctl, quick: bool) -> dict
class DoctorAdapter(Protocol): bundle() -> Path
class KeyringAdapter(Protocol):get(name) -> str | None ; set(name, value) -> None
class TtsPreviewAdapter(Protocol): voices() -> list[dict] ; preview(voice, text, speed) -> bytes
class FolderOpener(Protocol):  open(path: Path) -> None
class ScheduleAdapter(Protocol): apply(run_time: str) -> dict
@dataclass class Adapters: run, clip, learn, verify, doctor, keyring, tts, folder, schedule
def real_adapters(paths, link, log) -> Adapters
# supervisor_link.py
class SupervisorLink: status() -> dict | None; warmup(service); shutdown(grace_s); stop_all(); events(stop) -> Iterator[dict]
class HttpAcquirer: acquire/release over HTTP (ns_common.acquire.Acquirer shape); make_acquirer(url) prefers ns_supervisor.client.SupervisorClient
```
The real run/clip adapters expect these C7/C8 symbols (dependency list in the report): `ns_studio.orchestrator.run_job_with_progress(paths=, channel=, fmt=, count=, on_stage=, should_cancel=, acquirer=) -> list[str]` and `ns_studio.clipper.run_clip_job(paths=, source_path=, source_id=, params=, on_stage=, should_cancel=, acquirer=) -> list[str]`; `ns_studio.learn.import_csv(paths, csv_path) -> dict` and `ns_studio.learn.summary(paths) -> dict`. Until they exist the loaders raise `AdapterUnavailable`, which the worker records as a failed job with that message (never a crash).

- [ ] **Step 1: Write failing tests** (every real adapter raises `AdapterUnavailable` on a clean install; `FakeSupervisor` serves `/status /acquire /release /warmup /shutdown /stop_all /events`; `SupervisorLink` round trips against it; `status()` is `None` when nothing listens; `events()` parses SSE frames; fake clip adapter copies the fixture into an outbox path and inserts `outputs` + `sources` rows).
- [ ] **Step 2/3: Implement.**
- [ ] **Step 4: Commit** `feat(c9): dashboard adapters, fakes, supervisor link`.

### Task 4: Chunked uploads (C9)

**Files:** Create `ns_dashboard/uploads.py`, `tests/test_dashboard_uploads.py`.

**Interfaces:**
```python
CHUNK = 8 * 1024 * 1024
class UploadManager(paths):
    start(filename, size) -> UploadInfo      # id == source_id; dir imports\clipper\<id>\; .part file + upload.json
    status(upload_id) -> UploadInfo          # offset for resume
    append(upload_id, offset, data: bytes) -> UploadInfo   # offset must equal current (else UploadOffsetMismatch with the true offset)
    complete(upload_id) -> UploadInfo        # size check, sha256 (streamed), rename to source.<ext>
```
Extensions allowed: mp4 mkv mov webm avi m4v mpg mpeg ts. Chunks larger than 16 MB rejected.

- [ ] Tests: two chunks complete + sha256 equals `hashlib`; resume after a "dropped" chunk (status offset); wrong offset conflict; size mismatch; bad extension; path traversal in filename neutralised.
- [ ] Implement; **Commit** `feat(c9): resumable chunked upload manager`.

### Task 5: Media, logs, templates, channels (C9)

**Files:** Create `ns_dashboard/media.py logs_tail.py templates.py channels.py`, tests `test_dashboard_media.py test_dashboard_logs.py test_dashboard_templates.py test_dashboard_channels.py`.

**Interfaces:**
```python
# media.py
class RangeError(Exception) (carries size)
def parse_range(header: str | None, size: int) -> tuple[int, int] | None   # None = serve whole file; RangeError -> 416
def open_range(path: Path, header: str | None, *, chunk=1 << 20) -> RangeResult(status, headers, body_iter)
def thumbnail(paths, output_id, video: Path, log) -> Path     # ffmpeg first frame, cached under cache\dashboard\thumbs
# logs_tail.py
def tail(logs_dir, *, component=None, level=None, n=200, after=None) -> dict    # level = minimum level
# templates.py
def load_templates(paths) -> list[dict]; def render_preview(paths, template_id) -> Path   # Pillow, cached PNG
# channels.py
def validate_profile(raw: dict, others: list[Channel]) -> dict   # {ok, errors:[{field,message}], warnings:[{field,message}]}
```
- [ ] Tests (CI-safe): `parse_range` table (`bytes=0-9`, `bytes=10-`, `bytes=-5`, open end past EOF clamps, `bytes=999-` -> RangeError, garbage -> None, multi-range -> None); `open_range` yields exact bytes; logs filter by component/level/n/after and tolerate bad lines; templates normalise yaml variants and builtin fallback; previews need Pillow (`importorskip("PIL")`); thumbnail needs FFmpeg (`ffmpeg.available()` skip); channel warnings for tiny pools, identical pools, same niche.
- [ ] Implement; **Commit** `feat(c9): media range streaming, log tail, templates, channel validation`.

### Task 6: Service layer `handlers.py` (C9)

**Files:** Create `ns_dashboard/handlers.py`, `tests/test_dashboard_handlers.py`.

`Dashboard(state)` exposes one method per API operation (names in Appendix A) and raises `ApiError(status, code, message, hint=None)`. It owns: status/strip composition (engine state machine below), outputs CRUD against the `outputs`/`uploads` tables, channel CRUD through `ns_common.config`, pause/resume with `incidents` rows, job creation (`run`, `clip` incl. rights check and `rights.json`, `learn`, `verify`) and enqueue, settings (runtime read-only unless `unlock`), keys via keyring adapter, diagnostics, voices/preview with on-disk cache, learn summary (DB fallback), engine warmup/shutdown/stop_all, `events_iter`.

Engine strip state machine: supervisor unreachable -> `offline` "Engine not running"; any service `starting` -> `loading` "Loading <LLM (9 GB)|speech|speech recognition>..." with `elapsed_s` since first seen; dashboard job running -> `running` with the job message; running job but GPU held by another owner -> `waiting` "Waiting for GPU (held by X)"; gpu_owner or any service `ready` -> `ready`; otherwise `idle` "Services idle". `draining` after Quit.

- [ ] Tests (CI-safe, no FastAPI): every handler against a temp `NIGHTSHIFT_HOME` with seeded rows, `FakeSupervisor`, and fake adapters: outputs list/detail/approve/reject (reason required)/uploaded (URL required)/open-folder; channels create/update/pause/resume/validate; clip job needs rights (`rights_required`), `{path}` and `{upload_id}` and `{source_id}` re-run; run job validates channel/format; settings lock; keys never echoed; logs/runs/learn summary; engine strip transitions for all six states.
- [ ] Implement; **Commit** `feat(c9): dashboard service layer`.

### Task 7: FastAPI app (C9)

**Files:** Create `ns_dashboard/server.py`, `tests/test_dashboard_api.py` (guarded by `pytest.importorskip("fastapi")`), `tests/test_dashboard_security.py` additions.

`create_app(paths=None, *, adapters=None, link=None, start_worker=True) -> FastAPI`; pure-ASGI `LoopbackMiddleware` (Host/Origin check -> 403 JSON, `nosniff`, CSP, `Cache-Control: no-store` on `/api/` JSON); exception handlers for `ApiError`, `NsError`, validation; routes exactly Appendix A; `StaticFiles` mount at `/` with `html=True` registered last; media route supports GET/HEAD, `Range` (206), `Accept-Ranges: bytes`, 416 with `Content-Range: bytes */<size>`; `/api/events` SSE (`?limit=N` closes after N events for tests); `python -m ns_dashboard.server [--port 8100] [--open]` runs `uvicorn.run(app, host="127.0.0.1", port=...)`.

- [ ] Tests: TestClient (base_url `http://127.0.0.1:8100`) for every route; Host `evil.example` -> 403 and `Origin: http://evil.example` on POST -> 403; media 206 partial bytes, 416, `Accept-Ranges`, full 200, thumb 200 `image/jpeg` + cached second call (skip without FFmpeg; MP4 from `ns_common.ffmpeg.make_test_clip`); upload two chunks through the HTTP endpoints; multipart clip job and learn import (skip without `python_multipart`); `/api/events?limit=2` yields frames; every `api('...')` path used by `app.js` resolves to a registered route.
- [ ] Implement; **Commit** `feat(c9): dashboard FastAPI app and routes`.

### Task 8: Static UI (C9, delegated to `sonnet`)

**Files:** Create `ns_dashboard/static/index.html app.css app.js`, `tests/test_dashboard_static.py`.

Pages per spec 20.2 (Overview, Review, Clipper, Channels, Runs & Logs, Learn, Settings) plus the status strip, driven by Appendix A only. Tests (no FastAPI): regex that `index.html` has no `http(s)://` in any `src`/`href`, loads only `app.css` and `app.js`, has no inline `<script>` body, no `on*=` attributes; `app.js` contains no `confirm(`/`alert(`/`prompt(`, no `https?://` literals other than comments, defines the seven routes, and every `/api/...` string literal in it matches a route pattern from Appendix A (a static table in the test, plus the registered-route check in Task 7).

- [ ] Delegate with the Appendix A contract; review output; **Commit** `feat(c9): offline dashboard UI`.

### Task 9: Gate script, CRs, CI (C9)

**Files:** Create `dev/gate_m3_dashboard.py`, `tests/test_dashboard_gate.py`; append to `contracts/CHANGE_REQUESTS.md`.

`dev/gate_m3_dashboard.py` starts the app in-process with `TestClient`, makes a fixture MP4 with `ns_common.ffmpeg.make_test_clip`, POSTs `/api/jobs/clip {path, channel, template, rights_confirmed:true}` with the fake clip adapter, waits for the job to be `done`, asserts the clip is in `/api/outputs?status=pending&kind=clip`, approves it and asserts `status == "approved"`; writes a `GateReport` JSON to `logs\gate-m3-dashboard-<ts>.json`; `--fake-services` accepted (always fake). The same logic is exposed as `run_checks()` so `test_dashboard_gate.py` runs it when `fastapi` is installed.

- [ ] Implement, push, read CI (`gh run list --branch feat/c9-dashboard --limit 1`, `gh run view <id> --log-failed`), fix until green, `gh pr create --fill`.

---

## Appendix A - API contract (prefix `/api`, all JSON unless stated)

Errors: HTTP 4xx/5xx with `{"error": {"code": str, "message": str, "hint": str | null}}`.

**Job summary** `J`: `{job_id, kind: "run"|"clip"|"learn"|"verify", channel, status: "queued"|"running"|"done"|"failed"|"cancelled", progress: 0..1, message, created_at, started_at, finished_at, params: {...}, result: {...}|null, cancel_requested: bool}`. Clip results: `{output_ids: [...], source_id}`; run results: `{output_ids: [...]}`; learn result: the import summary; verify: `{checks: [...]}`.

**Output item** `O`: `{output_id, job_id, channel, kind: "long"|"short"|"clip", k, title, duration_s, status: "pending"|"approved"|"rejected"|"uploaded", review_note, source_id, combo_id, created_at, media_url, thumb_url}`. Output ids contain a `/` (e.g. `ana-20261101-ab12/s2`); URLs carry them unescaped as extra path segments: `/api/outputs/{output_id}`, `/api/media/{output_id}`, `/api/media/{output_id}/thumb.jpg`.

| Method + path | Request | Response |
|---|---|---|
| `GET /status` | - | `{version, strip, engine: {reachable, state, gpu_owner, services: {name: {state,pid,port,vram_mb,model_alias}}}, job: J\|null, queue: {queued, running}, next_run, disk_free_gb, outbox: {pending,approved,rejected,uploaded}, channels: [{slug, display_name, status, niche, format, pending, tonight}], last_run: R\|null, incidents: [{channel,ts,kind,note}], last_error: {job_id,message}\|null}` |
| `GET /events` (SSE) | - | frames `event: <kind>\ndata: <json>\n\n`; kinds: `status` (data = `strip`, sent on every change and every 2 s), `service` / `job` (supervisor relay: `{ts, kind, data}`), `dashboard_job` (data = J). Query `limit=N` closes after N frames (tests). UI falls back to polling `GET /status` every 3 s when the stream errors. |
| `POST /engine/warmup` | `{service?: "llama"}` | `{state}` |
| `POST /engine/shutdown` | `{grace_s?: 30}` | `{ok}`; cancels the running job at its next stage boundary, stops accepting jobs (`strip.draining = true`) |
| `POST /engine/stop_all` | - | `{ok}` |
| `GET /channels` | - | `{channels: [Card]}`, `Card = {slug, display_name, status, niche, language, format, voice, pending, approved, last_output_at}` |
| `POST /channels` | full channel profile (spec 11 / `ns_common.models.Channel`) | 201 `{profile, warnings}`; 409 if exists; 422 `error.code="invalid_profile"` with `error.details = [{field,message}]` |
| `GET /channels/{slug}` | - | `{profile, warnings}` |
| `PUT /channels/{slug}` | full profile (slug immutable) | `{profile, warnings}` |
| `POST /channels/{slug}/pause` / `resume` | `{note?: str}` | `{profile}` |
| `POST /channels/{slug}/validate` | optional draft profile | `{ok, errors: [{field,message}], warnings: [{field,message}]}` (always 200) |
| `GET /outputs` | `?status=&kind=&channel=&limit=100&offset=0` | `{items: [O], total, counts: {pending,approved,rejected,uploaded}}` |
| `GET /outputs/{id}` | - | `O + {sheet: {...}\|null, sheet_text: str\|null, policy: {...}\|null, advice: {ai_disclosure, made_for_kids, age_restrict_suggested}\|null, story_excerpt, transcript_excerpt, folder, upload: {video_url, published_at}\|null, file_exists}` |
| `POST /outputs/{id}/approve` | - | `O` |
| `POST /outputs/{id}/reject` | `{reason}` (required, non-empty) | `O` |
| `POST /outputs/{id}/uploaded` | `{url}` (http/https) | `O` |
| `POST /outputs/{id}/open-folder` | - | `{ok, folder}` |
| `GET /media/{id}` | `Range` header | MP4 bytes: 200 / 206 / 416; `Accept-Ranges: bytes` |
| `GET /media/{id}/thumb.jpg` | - | JPEG (generated on first request, cached) |
| `POST /uploads` | `{filename, size}` | 201 `{upload_id, source_id, chunk_size, offset: 0, size, status}` |
| `GET /uploads/{upload_id}` | - | `{upload_id, source_id, offset, size, status: "uploading"\|"complete"}` |
| `PUT /uploads/{upload_id}?offset=N` | raw body (<= 8 MB chunk, `Content-Type: application/octet-stream`) | `{offset, size, status}`; 409 `error.code="offset_mismatch"` with `error.details = {offset}` |
| `POST /uploads/{upload_id}/complete` | - | `{upload_id, source_id, path, bytes, sha256}` |
| `POST /jobs/run` | `{channel, format: "master_first"\|"shorts_only"\|"series_compilation", count?}` | 201 `J` |
| `POST /jobs/clip` | JSON `{upload_id\|source_id\|path, channel, template: "auto"\|..., max_clips, min_s, max_s, caption_style?, rights_confirmed}`; or multipart with the same fields plus `file` | 201 `J`; 400 `rights_required` unless `rights_confirmed` is true |
| `POST /jobs/verify` | `{quick?: bool}` | 201 `J` |
| `GET /jobs` | `?status=&kind=&limit=50` | `{jobs: [J]}` newest first |
| `GET /jobs/{id}` | - | `J` |
| `POST /jobs/{id}/cancel` | - | `J` (`cancel_requested` true while the current stage finishes) |
| `GET /runs` | `?limit=30` | `{runs: [R]}`, `R = {run_id, started_at, finished_at, mode, duration_s, summary: {...}}` |
| `GET /logs/tail` | `?component=&level=&n=200&after=<iso ts>` | `{lines: [{ts,level,component,event,msg,data}], components: [str]}`; `level` is a minimum (`WARN` -> WARN and ERROR) |
| `POST /learn/import` | multipart `file` (CSV) | 201 `J` (kind `learn`); progress and the import summary arrive via the job |
| `GET /learn/summary` | - | `{pools: {pool: {top: [{option,score,n}], bottom: [...]}}, weights: [{channel,pool,option,weight,updated_at}], last_import: {...}\|null, unmatched_uploads: [str]}` |
| `GET /settings` | - | `{version, runtime: {...}\|null, runtime_locked: true, schedule: {run_time}, notifications: bool, keys: {pexels_api_key: {set}, pixabay_api_key: {set}}, install: {version, app_path, profile}, next_run}` |
| `PUT /settings` | `{unlock?: bool, schedule?: {run_time: "HH:MM"}, notifications?: bool, runtime?: {...}}` | `GET /settings` shape; changing `runtime` without `unlock: true` -> 409 `runtime_locked` |
| `POST /settings/keys` | `{name: "pexels_api_key"\|"pixabay_api_key", value}` | `{name, set: true}` (value is never returned) |
| `POST /diagnostics` | - | `{path, name, size, download_url: "/api/diagnostics/latest"}` |
| `GET /diagnostics/latest` | - | the latest bundle zip (`application/zip`) |
| `GET /templates` | - | `{templates: [{id, label, description, when, regions: [{name, x, y, w, h}], caption_y_pct, title_card, preview_url}]}`; `x,y,w,h` are fractions of the 1080x1920 frame |
| `GET /templates/{id}/preview.png` | - | PNG drawn with Pillow (503 `pillow_missing` otherwise); the UI draws its own CSS cards from `regions` and may ignore the PNG |
| `GET /voices` | - | `{voices: [{name, mode, source}], available: bool, note}` |
| `POST /voices/preview` | `{voice, text?, speed?}` | `audio/wav` bytes |
