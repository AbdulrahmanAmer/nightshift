# Changelog

All notable changes to Nightshift are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Milestones map to minors: 0.1.0 is M0, 0.2.0 is M1, and so on
(`docs/ENGINEERING.md` section 7).

## [Unreleased]

### Fixed (studio, from the clean-clone review `D:\NightShift-refs\review-studio.md` and `review-clipper-tts-common.md`)
- **First run**: a channel that exists only as YAML (what setup writes) gets its `channels` row before the first insert
  (`make_job`, `load_job`, clips, ledger); the policy gate and every story embed use a 1,500-character opening window and
  `FakeEmbed` refuses over 512 tokens like `llama-server`; caption drift is the offset between caption and ASR word starts
  (speech rhythm no longer fails NS-E403); the stock-footage keys are read under the names setup and Settings store
  (`ns_common.secrets_names`).
- **Wrong output**: `pacing` sets the TTS speed and Short word bands use the QC +-15 % (masters are capped at 14 minutes);
  each Short is written, titled, captioned and recorded in its own combo; publish slotting follows
  `outbox/manifest.json` and rewrites both sheets; `studio calibrate` takes the embed lease and only advises two-model
  mode when a story model exists; `tag_relevance` really asks the model (policy_check v2).
- **Degrades**: uncoded exceptions become `NS-E327` for their job only; whisper is unloaded when its tenant is released;
  a regenerated job changes seed, temperature and clips; `--resume` no longer redoes LLM stages after the voice stage or a
  chapter expansion; prompt versions are saved per job; words, sentences and captions work in any script;
  apostrophes in FFmpeg paths are escaped, not deleted; Outbox paths stay under 200 characters; the visual_plan prompt asks
  for caption emphasis; master chapters get the caption contrast fallback.
- **Series**: parts use their design (voice, caption style, visual treatment, title, "Part N of M", end card, publish delay)
  and master QC allows 1.15x the part's minutes.
- **Clipper**: clip SEO uses clip rules (not the fiction lists) and a failing clip is skipped, not the job; phone videos
  are probed with their display rotation; one whisper pass; `import_url` can be cancelled (`CancelToken`) and kills yt-dlp;
  rights not ticked is `NS-E410`; the caption style picked on Create is honoured; `captions.remove_fillers` channel setting.
- **New**: your own `exemplars\` and `prompt_overrides\` are read; upload sheets name the time zone; the creator note closes
  every description.

### Fixed (installer and runtime, clean-clone review)
One commit per row of `D:\NightShift-refs\review-installer-runtime.md` (see the pull request), each with a test that fails
without it.
- **Setup could not find uv** (row 1): stage 1 hands the uv it downloaded over in `NIGHTSHIFT_BOOTSTRAP_UV`; the one
  resolver (`ns_common.uv.find_uv`) checks the install folder, that hand-over, the bootstrap folder, then PATH; b02 copies
  uv and verifies the copy; b03 runs the copy.
- **Non-ASCII user or Desktop path** (row 2): PowerShell values are read as base64 UTF-8, so the Desktop path and
  NIGHTSHIFT_HOME survive any console code page.
- **Update swap** (rows 3, 4): shortcuts start in the install home, the Update/Dashboard/Studio launchers also change the
  Win32 working directory, the swap retries a busy rename; `ns update` runs as a detached worker outside the engine's Job
  Object (`CREATE_BREAKAWAY_FROM_JOB`, WMI fallback) and writes `run/update_state.json`.
- **Idle exit with the Dashboard open** (row 5): an open window host or a browser connected to the Dashboard keeps the engine.
- **Update re-sync** (row 6, gap 18): setup and `ns update` read one table of extras (`ns_common.venv_extras`).
- **Uninstall** (row 7): the last deletion (`bin\webview2`) runs from a detached PowerShell after the window exits, and the
  result lists what really went.
- **Setup window** (rows 8, 12, P4): the host starts only when its DLLs exist, has 20 s to show a window, else Edge or
  Chrome opens with `--no-first-run --no-default-browser-check`; Stop and a dying host are explained on the console.
- **install_state.json** (row 9): atomic write, `.bak` copy, tolerant read.
- **Open dashboard** (row 10): the launcher runs with its output captured and its error reaches the window.
- **Stage 1 without winget** (row 11): NS-E105/NS-E110 instead of a raw PowerShell error.
- **Nightly task** (P1, P2, P3): S4U first, interactive logon when Windows refuses (NS-W205), never blocks setup
  (NS-W206); API keys also in a DPAPI file the nightly run can read; a message without a desktop becomes a morning note.
- **Gaps 10, 14, 19, 27, 28, 37, 38, 39**: `ns models get voicedesign|story_llm`, `ns update --git --channel`, launchers use
  `/health`, whisper `int8`, `ns gate` removed, NS-W301 in `ns verify`, the `local/code` check runs after s07.

Fixes from the v0.1.0 code review (installer, launchers, supervisor), found by reading the paths no test had run.

### Fixed (dashboard side of `docs/design/system-gaps.md`)
Calls from the dashboard into the studio, supervisor and Series Workshop that pointed at functions which do not exist,
and routes and buttons that nothing used. A table-driven test (`tests/test_dashboard_wiring.py`) now resolves every
`ns_studio`, `ns_supervisor`, `ns_tts` and `ns_common` name the dashboard takes, so an unwired call fails CI.
- **Run now** (gap 2) calls `ns_studio.jobs` and `policy.regen.run_job_guarded`; progress and Cancel come from the stage
  runner's log events (`ns_dashboard/studio_bridge.py`).
- **Clipper** (gap 3) builds the clip job with `create_clip_job` and runs it with `run_job`; links download through
  `clipper.source.import_url`. **Re-run with another template** (gap 18, `POST /api/jobs/clip/rerun`) resumes the job
  with `resume_clip_job`, keeping the transcript.
- **Series parts** (gap 6): the jobs worker runs `series_part` rows that `Workshop.produce` writes; a failed part goes back
  to approved. The **Series Workshop tools** (gap 7) are in the Assistant (topic series) with Yes/No cards, and
  **Library, Series** (gap 8) lists stories and parts from the workshop files with a "Produce next part" button.
- **Settings, Update** (gap 9) shows "not available in this version yet" instead of an error while `ns update --check` /
  `--github` do not exist. **Diagnostics** (gap 10) calls `doctor.build_bundle` and has a Download button (gap 22).
  **Self-check** (gaps 11, 27) calls `run_verify(paths, rt, ctl, quick=, deps=)`; Settings also has Warm up and Stop all.
  **Results, import CSV** (gap 12) reads the file with `learn.importer` and updates what the pools learned.
  **Run time** (gap 13) re-registers the Windows task with `schedule.install`.
- **Settings** has Export settings, Import settings (gap 17, `ns backup` / `ns restore`) and the real path of
  `UNINSTALL.cmd`; the log viewer filters by component (gap 28).
- **Assistant** (gap 21) lists past conversations and answers requests saved while the engine was busy. **Library,
  Formats** (gap 23) has an edit form (and Duplicate works again). **Queue** (gap 26) shows a "Last night" card.
- **First start** (gap 30) greets the owner by the name chosen in setup and starts in the chosen theme.

### Fixed (dashboard, from the clean-clone review)
- **Jobs run in the studio venv.** The dashboard service lives in the supervisor venv, which has no faster-whisper, GPU DLL
  wheels, spaCy or OpenCV, so Run now, Clipper jobs, series parts and Library, Voices, Clone failed at the first whisper or
  spaCy use. Each of them now starts `<home>\venvs\studio\Scripts\python.exe -m ns_dashboard.studio_job` (cwd = the app
  folder, `NIGHTSHIFT_HOME`, UTF-8; `sys.executable` when that venv does not exist) and reads its progress as JSON lines
  (`ns_dashboard/studio_runner.py`). Cancel kills the process tree with `ns_supervisor.procs.kill_tree`: a link download
  (yt-dlp) no longer outlives a cancelled job and its half-file folder is deleted.
- **API and UI agree.** `tests/test_api_ui_contract.py` seeds a dashboard, calls every route the UI calls and compares the
  JSON field for field with `ui/src/lib/types.ts`. That found and fixed: Library > Stories (white screen: `words`,
  `critic_avg`), Ideas (Approve/Drop/Restore sent `/ideas/undefined`), Footage cards, the Queue's Approved and posted tab
  (`status=approved,uploaded` was a 422; rejected videos had no reason, posted ones no link or path), the Channels editor
  (it sent the card, so every save was a 422; it now loads and saves the whole profile), Settings > Models
  (`[object Object]`; engine settings are flat dotted keys now), Results after a reload, formats' channels, job step/steps.
- **Every page has an error boundary**; a crash resets the remembered Library tab. Demo data appears only when the very
  first status call fails; later the page shows "Engine not answering" and retries with backoff. The 2 s heartbeat no
  longer re-renders every page. The Queue's job bars follow the running job.
- **Settings > Updates > Install** follows the detached updater through `run/update_state.json` across the dashboard's own
  restart. **Import settings** closes the database for the restore and restarts the worker (refused while a job runs).
- **Saved Assistant requests are answered** when a job ends and show in the open chat. Cancel at job start no longer
  loses the race. An interrupted series part goes back to approved. Clip length limits equal `ClipParams`. The Caption
  look picked in Create reaches the clip job. Big uploads use the chunked `/api/uploads` and spool under `<home>/tmp`.
- Also: failed jobs show a red card with code, hint and a Diagnostics button; morning notes (a toast that could not show) in
  the bell and Last night; the three thumbnails of a long video; footage delete; Make a Short / Reuse as series on a
  story; clip results say which layout was used; the run time is the registered task's; `#/design` pages are dev-only;
  Settings > Models can fetch the voice design and story models (`ns models get`).


### Fixed (voice engine, `feat/release-review-tts`: review rows 1-4, 12, 13)
The four stacked blockers that left the partner PC with no working voice (qwentts failed, the PyTorch fallback failed
too, install stopped at `NS-E108`), plus pitch and error codes. Contract and exact command lines: top of `ns_tts/engine.py`.
- **qwentts runs the programs the release zip contains.** `qwentts.exe` does not exist; the engine now starts
  `bin\qwentts\qwen-tts.exe` (text on stdin, `--model --codec --speaker|--ref-spk --ref-rvq --ref-text --lang --seed -o`,
  lowercase speaker names) and `qwen-codec.exe` (`--model --talker -i`) for new voices, using the flags of the pinned
  qwentts.cpp `--help`. The old guessed flags (`--text`, `--out`, `--tokenizer`, `--language`, `--ref-audio`,
  `--save-speaker-embedding`, `--batch-file`) are gone. `tts-server.exe` is shipped but not used (one talker per process).
- **Voice format.** qwentts voices are `speaker.spk` + `speaker.rvq` + `ref.txt` in the voice folder; `voice.json` gains
  `files` (additive, the dashboard still reads `name` and `mode`). Voices with only `speaker.bin` (PyTorch engine, older
  fakes) still load. `GET /v1/voices/{name}/embedding` returns a zip when a voice has several files.
- **PyTorch fallback.** The supervisor starts the voice service with the `tts` venv's Python when `runtime.tts.engine` is
  `reference` (torch lives only there), and the engine reads the models where the installer puts them: `<model_dir>\hf`
  (`ns_common.paths.TTS_HF_SUBDIR`, `Paths.models_tts_hf`).
- **Speed no longer changes pitch.** Narration speed (0.7 to 1.3) is an FFmpeg `atempo` chain per chunk instead of
  resampling; neither qwentts tool has a speed control.
- **Error codes.** Synthesis and TTS HTTP failures are `NS-E310`, a stored voice that is missing files or belongs to the
  other engine is `NS-E311`, a missing VoiceDesign model is the new `NS-E314` ("run Settings > Models > Get voice design"), a voice that
  cannot be built is the new `NS-E315`, and embed-service failures are the new `NS-E316`. `NS-E301` stays the language model's code; `NS-E202` is used only while the voice service starts.
- **`/health` of the voice service** now reports `engine_kind` (`qwentts`, `reference` or `fake`) so `ns verify` can warn
  `NS-W301` when the PyTorch fallback is in use.
### Removed
- `GET /api/templates/{id}/preview.png` and the Pillow preview drawing: nothing called them (gap 28).
- `Paths.outbox_dir`, the old date/job Outbox layout, which no code used (gap 24).

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
