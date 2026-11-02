# Changelog

All notable changes to Nightshift are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Milestones map to minors: 0.1.0 is M0, 0.2.0 is M1, and so on
(`docs/ENGINEERING.md` section 7).

## [Unreleased]

Fixes from the v0.1.0 code review (installer, launchers, supervisor), found by reading the paths no test had run.

### Fixed
- **`ns update` could never swap folders** (`NS-E702` every time): the shortcuts start `ns.exe` with the app folder as
  working directory, and Windows refuses to rename a folder that is any process's working directory. `ns update` (and
  `--rollback`) now steps out of the app folder first, and `Nightshift - Update` does a `Set-Location` before calling it.
- **uv was only looked up on PATH**, but the installer keeps it at `bin\uv\uv.exe` and never adds it to PATH, so
  `ns update` could not re-sync the environments and `ns doctor --bundle` had no `pip freeze`. One resolver
  (`ns_common.uv.find_uv`: install folder first, then PATH) now serves update, doctor, the installer and the setup window.
- **The dashboard could open as a dead page after setup**: `ns verify --quick` left a headless engine running that never
  starts the dashboard service. The Dashboard shortcut (and the setup window's Open dashboard, which runs it) now always
  asks the engine to start the dashboard, waits for port 8100 and never opens a dead address. `ns verify` stops an engine
  it started itself, and the setup summary runs `ns stop`.
- **The smoke test checked check names that do not exist** (`llm`, `mtp`; `ns verify` reports `llama.start`,
  `llama.health`, `llama.json_completion` and has no MTP check) and the fake runner faked the same wrong names, so a
  failing story model was never caught. The names now live in `ns_supervisor.check_names` and are used by `ns verify`, the
  smoke step, the fake and the setup window labels. The dead MTP branch is gone (`ns tune` decides MTP). Setup rows group
  the real checks per area.
- **Paths with spaces broke headless setup** (`C:\Users\John Doe\...`): stage 1 wrapped the server arguments in literal
  quotes and splatted them, which Windows PowerShell 5.1 quotes again. It now passes plain strings (and quotes correctly
  for the window-mode `Start-Process`).
- **Shutdown race in the engine**: a GPU waiter granted the lock while `shutdown()` ran started a new model process after
  the stop and re-created `run\supervisor.json`. The grant is re-checked (`NS-E202`), `warmup` refuses during shutdown and
  `write_state()` does nothing once shutting down.
- **`terminate()` killed services at once**: it ran `taskkill /T /F` first, so `term_grace_s` was never used. It now asks
  politely (`taskkill /T`), waits the grace period when Windows accepted the request, and only then force-kills the tree.
- **A hidden console stayed hidden**: the dashboard launcher returned on any host exit code other than 3 before showing
  the console it had hidden. It now restores it in a `finally` and falls back to the browser window on any exit but 0.
- **The setup and dashboard windows shared one single-instance lock**, so opening the dashboard while setup was open only
  raised the setup window. `nightshift-host.ps1 -Role setup|dashboard` (default dashboard) scopes the mutex and
  `run\apphost-<role>.json`; the setup window passes `-Role setup`.
- **Toasts never showed**: nothing registered the AppUserModelID `Nightshift`. Setup now writes
  `HKCU\Software\Classes\AppUserModelId\Nightshift` (DisplayName, IconUri = the brand icon) and stamps the ID on the
  Desktop dashboard shortcut through IPropertyStore (best effort; the registry key alone is what makes toasts show).
  `UNINSTALL.cmd` removes the key.

### Gaps closed (engine side, `docs/design/system-gaps.md`)
- **The nightly run now starts the services it needs** (gap 1): `studio run` (and `--daily`) used `DevAcquirer`, which
  only hands out port numbers, so at 03:00 the first LLM call hit a closed port. `StudioAcquirer` asks the engine
  (`ns_supervisor`) for llama, embed, tts and whisper, starting the engine if it is down, and waits up to 2 hours for a
  busy GPU (J7). After that the run stops with `NS-E201`, lists the remaining jobs as skipped and tries again the next
  night. Fakes keep `DevAcquirer`.
- **`embed` is started before idea dedupe and the text policy check** (gap 4): stages that embed text declare
  `uses_embed` and hold the service while they run; a test fails when a new stage calls `ctx.services.embed` without it.
- **A designed or cloned voice chosen in setup is created** (gap 5): `studio voice design|clone|apply-pending` use the
  same service calls as the Library. Setup queues `config\pending_voice.json` with a clear message (the TTS engine is
  only settled by the smoke test after it, and the other engine refuses the voice, `NS-E311`); the first real
  `studio run` creates it.
- **End-of-run toast** (gap 14): "1 master + 5 Shorts ready for review" / "run failed (NS-E203), open Dashboard" from the
  nightly path, honouring Settings > Notifications (`ns_common.config.notifications_enabled`).
- **Cliffhanger pauses** (gap 15): chapters send their cliff line to TTS, so the pause before it is 1,200 ms.
- **`ns update --check` and `--github`** (gaps 9 and 16): read the latest GitHub release, download
  `nightshift-app-<version>.zip` and verify its SHA256 (GitHub's digest, else the attached `.sha256`; no checksum, no
  install) before the normal verified update with rollback. The engine runs the check once a week and shows a
  notification when a newer release exists.
- **The engine leaves after the nightly run** (gap 25): the run tells it (`POST /run_finished`) and it exits 60 s later
  unless the dashboard is running or other work started, instead of staying up 30 idle minutes.
- **Error codes**: a whisper failure is `NS-E408` (hint: the audio track) and a stored voice from the other TTS engine is
  `NS-E311` (hint: re-create it), also through `TtsClient`.
- **`series_compilation` is hidden from Run now** (gap 29) in the API and the Assistant until it has stages.
- **Whisper code default is `distil-large-v3` / `int8`** (gap 31), like setup and the spec; the installer still writes
  `float16` explicitly.
- `tests/test_wiring.py` imports every `ns_*` module and checks that every dotted target named in
  `ns_dashboard/adapters.py` and `ns_dashboard/assistant/tools.py` exists.

## [0.1.0] - 2026-11-02

First release (milestone M0, code complete). Highlights since the pipeline landed:

- **Dashboard UI rebuilt** on the ref12 card language: side tab rail, marbled canvas, three themes (light, dark, soft),
  top bar with the inline Assistant, redesigned pages, GPU and VRAM meters, brand assets and a new app icon.
- **Host window:** the dashboard opens in a frameless WebView2 window (single instance, remembered size), with the
  Edge/Chrome app window as the fallback.
- **Voice tools:** design a voice from a description and clone one from a recording (with a consent check, `NS-E313`),
  both previewed in the Library.
- **Scheduling:** Create can hold a story or clip job until a chosen time (`schedule_at`).
- **QC pass rate:** each channel shows how many videos passed QC against a target line (`Channel.qc_pass_target`).
- **Installer web wizard:** `INSTALL.cmd` opens a setup window (stage 1 splash with `-Ui`, `-NoUi` and `-ProbeOnly`; a
  stdlib web server and state machine with resume). Screens: Welcome, This PC, Choices, Download, Installing, Verify, Done,
  Failed and Uninstall; `UNINSTALL.cmd -Ui` opens the Uninstall screen. The console wizard still runs with an
  `answers.json`. The window is built with `npm run build` in `ns_dashboard/ui` (`vite.installer.config.ts`) into the
  committed `installer/ui/static`.

### Added
- Foundation and contracts (C3): shared pydantic models in `ns_common.models` with generated JSON schemas,
  `contracts/errors.json`, `contracts/schema.sql`, config/paths/db/logging helpers, the `contracts-m0` and
  `contracts-m1` freeze tags.
- Studio core (C7): resumable stage runner, orchestrator with the canonical `FORMAT_STAGES`, the `studio` CLI,
  variation pools, policy gates, learning from analytics, cadence and review.
- Text pipeline (C5): shorts-only and master-first story stages, 20 prompts, 12 exemplars.
- Media pipeline (C6): voice, captions, clips, render, QC and the content-organized Outbox; caption placement,
  emphasis, speaker colours and profanity masking; format presets as YAML in `ns_studio/presets/formats/`
  with a generic layout engine.
- TTS service (C4): qwentts.cpp wrapper with a PyTorch reference fallback and voice embeddings.
- Runtime (C2): supervisor with a Job Object, service lifecycle, the `ns` tools and the Windows launchers.
- Installer (C1): `INSTALL.cmd`, `UNINSTALL.cmd`, stage 1, the stage 2 wizard with resumable verified
  downloads, `OPERATOR.md`.
- Clipper (C8): analysis-first clip selection, model-chosen layouts, a per-clip judge, and clips from a link
  through a pinned yt-dlp (`NS-E409`).
- Dashboard (C9): local FastAPI backend (jobs worker, Library, update endpoints, Assistant with a tool
  registry, URL clip jobs) and the React + TypeScript front-end in `ns_dashboard/ui`, built to
  `ns_dashboard/static`.
- Series Workshop (C11) and the integration pass (CLI mounts for `studio clip|series|ask`, idea bank feeding
  the `ideas` stage).
- CI split into parallel jobs: `lint`, `types`, `test`, `ui-build`, `powershell-parse`, `bundle` and (pull
  requests only) `scope-check`, with a per-branch concurrency group and pip caching. `ui-build` rebuilds the
  dashboard with node 20 and fails when the committed `ns_dashboard/static` differs.
- Release workflow: a `v*` tag runs the tests, builds the UI, builds the bundle and publishes a GitHub Release
  with `Nightshift-<ver>.zip`, its `.sha256` and the `nightshift-app-<ver>.zip` update alias.
- `dev/make_bundle.ps1` (git archive of HEAD with the dashboard build, content verification, `-DryRun`).
- `dev/check_scope.py`: pull-request guard that keeps each `feat/c<K>-*` branch inside its component paths.
- `tests/test_hygiene.py`: lazy heavy imports, no `shell=True`, no `print` outside CLI modules, PowerShell
  prelude, error-code registry and schema drift checks.
- `build-qwentts` workflow compiling qwentts.cpp for Windows x64 with CUDA.
- `VERSION` file, checked against `ns_common.__version__` and `pyproject.toml`.
- `docs/REPO_SETTINGS.md`: branch protection, repository settings and how the manifest binary pins are made.
- `manifest.json` pins `bins.yt_dlp`; installer step s02 downloads it to `bin\yt-dlp\yt-dlp.exe`.
- Contracts: `NS-E409` (download failed); `Channel.formats_allowed` and `Channel.captions.mask_profanity`;
  optional `emphasis` on `VisualBeat` and `ClipPlanItem`; the Assistant tables (`conversations`, `messages`,
  `actions`, `queued_messages`) in `contracts/schema.sql`.
- Packaging: `ns_dashboard` is part of the package list and pyright scope; package-data ships
  `ns_dashboard/static`, `ns_dashboard/prompts` and `ns_studio/presets/formats`; a `dashboard` extra
  (`python-multipart`, `Pillow`, `keyring`) is installed into the supervisor venv; `opencv-python-headless`
  joins the `studio` extra.
- UI contracts (C3): installer answers `owner_name`, `ui_theme`, `models_source_kind`; codes `NS-E113`, `NS-W403`, `NS-E312`;
  the `TBD-release` rule for optional manifest pins; `Status` (with optional `gpu`), `JobRunRequest` (`schedule_at`),
  `ChannelStats` (`qc_pass_rate`) and the voice design/clone models (CR-C1-UI-1..3, CR-C3-UI-4..7).
- `launchers/nightshift-host.ps1`: single-instance frameless WPF window around WebView2 (title bar protocol, size and
  position kept in `config/window.json`); `nightshift-dashboard.ps1` tries it first and falls back to the
  Edge/Chrome app window, then the default browser. `bins.webview2` (change request CR-C2-host) pins the Microsoft.Web.WebView2 NuGet package;
  the installer unpacks only its three DLLs to `bin/webview2`.

- Release 0.1.0 prep: `NS-E313` (voice cloning without consent; `POST /api/voices/clone` now answers it instead of
  `consent_required`), optional `Channel.qc_pass_target` (default 0.9, read by the channel stats), `JobClipRequest` for
  `POST /api/jobs/clip` (with `schedule_at`) and its schema.
- Voice-sample uploads accept `.wav`, `.mp3`, `.m4a`, `.flac`, `.ogg` and `.webm`; the dashboard no longer renames
  recordings to `recording.webm`.
- The brand icon (`ns_dashboard/static/brand/nightshift.ico`) is the host window icon and the icon of the shortcuts the
  installer creates; `dev/make_bundle.ps1` requires it in the bundle.

### Changed
- ruff bans `import subprocess` outside `ns_common/ffmpeg.py`, `ns_supervisor/`, `installer/`, `tools/`,
  `dev/` and `tests/` (rule `TID251`), with per-file exceptions for `ns_dashboard/updater.py`, `ns_tts/engine.py`
  and `ns_studio/clipper/source.py`.
- `dev` extra gains `fastapi`, `psutil`, `python-multipart`, `opencv-python-headless`, `Pillow`, `imagehash`.
- `nightshift-dashboard.ps1` opens the dashboard as an Edge or Chrome app window (`--app=`), falling back to the
  default browser.
- The Assistant store no longer defines its own DDL; `Db.migrate()` creates its tables from the shared schema.
