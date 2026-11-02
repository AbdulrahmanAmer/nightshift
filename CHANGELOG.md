# Changelog

All notable changes to Nightshift are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Milestones map to minors: 0.1.0 is M0, 0.2.0 is M1, and so on
(`docs/ENGINEERING.md` section 7).

## [Unreleased]

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
- `launchers/nightshift-host.ps1`: single-instance frameless WPF window around WebView2 (title bar protocol, size and
  position kept in `config/window.json`); `nightshift-dashboard.ps1` tries it first and falls back to the
  Edge/Chrome app window, then the default browser. `bins.webview2` (change request CR-C2-host) pins the Microsoft.Web.WebView2 NuGet package;
  the installer unpacks only its three DLLs to `bin/webview2`.

### Changed
- ruff bans `import subprocess` outside `ns_common/ffmpeg.py`, `ns_supervisor/`, `installer/`, `tools/`,
  `dev/` and `tests/` (rule `TID251`), with per-file exceptions for `ns_dashboard/updater.py`, `ns_tts/engine.py`
  and `ns_studio/clipper/source.py`.
- `dev` extra gains `fastapi`, `psutil`, `python-multipart`, `opencv-python-headless`, `Pillow`, `imagehash`.
- `nightshift-dashboard.ps1` opens the dashboard as an Edge or Chrome app window (`--app=`), falling back to the
  default browser.
- The Assistant store no longer defines its own DDL; `Db.migrate()` creates its tables from the shared schema.
