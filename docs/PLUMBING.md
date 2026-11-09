# Nightshift plumbing map

Every connection point of the shipped product, derived from the code, with what has been verified and what
has not. `docs/VERIFICATION.md` is the standard (blocks, angles, evidence levels) and
`docs/VERIFICATION_INVENTORY.md` the generated checklist of routes, commands, stages, steps, launchers, pages
and tools. This file is the third view: **the wiring**. Ports, processes, files, environment variables, event
channels, external hosts, Windows integration points and configuration keys, each with who creates it, who
consumes it, and where in the code. It exists because the installer, the engine, the studio and the dashboard
were built by different builders and the places where they meet are where the real defects were found
(the voice service pointed at a venv that is never created on a PC without a graphics card; an edited format
the studio never read; a job killed mid-run losing its stage; the window left open on a dead page).

How it was made (2026-11-08, `VERSION` 0.1.11 on `main` after PR #88, line numbers as of that commit):
sections 1 to 11 were compiled from the code by grep and reading (a `sonnet` agent with three `sonnet`
sub-passes for the dashboard routes, the environment variables and external endpoints, and the database and
configuration keys; every row cites `file:line`). "No caller found", "not found" and "NONE" mean the search
came up empty, not that the thing is unused for certain. The orchestrator wrote section 0 (coverage) and
section 12 (what became of the leftovers) and checked the rows the fixes touch. Line numbers drift with every
merge; the file and symbol names are what to search for.

Evidence levels are those of `docs/VERIFICATION.md` section 1: **V-dev** (observed on the orchestrator's
laptop against an install made from the official zip: dev mode with fake engines, or the **engine test** of
2026-11-08, a real install with the real language model, embeddings and speech recognition and the fake voice,
`docs/DEV_MODE.md` "Real engines, fake voice"), **V-ci** (a test in `tests/`), **V-gpu** (only the partner's
GPU PC can show it; a line in the first-run checklist), **U** (nobody has verified it).

Counts: processes 30, ports 13, HTTP routes 132 (supervisor 12, dashboard 95, voice 8, setup 17),
environment variables 49, files and folders 92, database tables 29, event channels 27, external endpoints 40,
Windows integration points 36, configuration keys 90, leftover candidates 34.

## 0. Coverage by section (what we have covered, what we had not)

| Section | Verified (how) | Not verified before 2026-11-08 or still open |
|---|---|---|
| 1 Processes | Supervisor, dashboard, studio children (`run`, `clip`, `series`, `download` of a URL), WebView2 host and the browser fallback, setup server and wizard engine, the dashboard launched after Done, deferred uninstaller, update worker and post-update relaunch, dashboard-spawned `ns` commands, FFmpeg, the scheduled task firing `studio run --daily`: **V-dev** (loops of 2026-11-04/05). `llama-server` and the embed server for real, whisper in-process for real, the voice server with its fake engine in a real install: **V-dev** (engine test 2026-11-08: `ns tune` 22.7 tok/s on Qwen3-0.6B, all 12 smoke checks ok, a clip job through real speech recognition). | `qwen-tts.exe` / `qwen-codec.exe` and the real voice server: **V-gpu**. `opencode.exe` and its prewarm: **U** (no dev or engine-test install selects Nightshift Code). `nvidia-smi` probes: **V-gpu**. The `transcribe` and `apply_voice` studio children: **V-dev** only through dev clips and the pending voice; real-engine runs of both: **U**. |
| 2 Ports | 8099, 8100, 8091 (fake voice), the setup server's ephemeral port and token, the `/api/ping` hand-off to stage 1: **V-dev**. 8080 and 8081 with the real `llama-server`: **V-dev** (engine test). Nothing binds `0.0.0.0`; Host/Origin checks: **V-dev** (security probes 2026-11-05). | A foreign program on a product port (`NS-E116`): **V-ci** only. Dev-only ports (8765, 5173, 5179, 8790): not product. |
| 3 HTTP routes | Per route in `docs/VERIFICATION_INVENTORY.md` (83 dashboard routes with a status each). Supervisor: `/health /status /acquire /release /heartbeat /stop_all /shutdown /warmup /run_finished /events` (SSE): **V-dev**. Voice: `/health /v1/voices /v1/audio/speech`, design and clone on the fake engine: **V-dev**. Setup: `/api/state`, `/api/action/*` driven through the API: **V-dev** (2026-11-05). | Supervisor `POST /events` and `/release_owner`: no product caller / **V-ci**. Voice `GET /v1/voices/{name}/embedding`: no caller. Setup window actions by clicking (Playwright): **U**. Dashboard routes still `U` in the inventory (`/api/events` consumer, `engine/warmup`, `engine/shutdown`, the Results page data routes, bridge-only routes). |
| 4 Environment variables | `NIGHTSHIFT_HOME`, `NIGHTSHIFT_DEV_MODE`, `NIGHTSHIFT_BOOTSTRAP_UV`, `NS_NONINTERACTIVE`, `NS_DASHBOARD_NO_HOST`, `NS_JOB_CANCEL_FILE`, `NIGHTSHIFT_UNDER_ENGINE`, `NS_TOAST_*`: **V-dev** (install, headless starts, cancel, kill-mid-job). | `NS_TTS_FAKE`, `NS_DEVTOOLS`, `NS_FFMPEG`, `NS_FFPROBE`, `NS_LOG_STDERR`, `NS_SUPERVISOR_URL`: read by the code, set by no product path (**V-ci** where a test sets them, otherwise **U**). Two prefixes (`NIGHTSHIFT_*`, `NS_*`): by history, kept (renaming breaks installed homes). |
| 5 Files and folders | `config\runtime.json`, `install_state.json`, `wizard_flags.json`, `answers.json`, `schedule.json`, `supervisor.json`, `run\apphost-*.json`, `run\update_state.json`, `logs\*.jsonl`, `verify-report.json`, `install-report.json`, the diagnostics zip, `%LOCALAPPDATA%\Nightshift\setup-server.json`, job folders, outbox layout, backup zip and restore: **V-dev**. Model files with their `.ok` hash marks: **V-dev** (engine test, SHA256 of the 639 MB GGUF re-hashed by hand). | `config\secrets.dpapi` read by the S4U nightly task: **V-gpu** (this laptop's task is interactive-only, `NS-W205`). `models\tts_hf` (PyTorch fallback files): **V-gpu**. `exemplars` and `prompt_overrides` were never created by `Paths.ensure()` (fixed, section 12). |
| 6 Database tables | Jobs, outputs, runs, uploads, clips, sources, combos and the 30-day ledger, analytics import and learning, channels, dashboard_jobs, assistant conversations and messages, backup and restore of both databases: **V-dev**. Schema equality with `ns_common.models`: **V-ci**. | Tables the code writes but never reads (`calibration` beyond its own command, `queued_messages`) and `parts` (read, never written) are listed in section 6; dispositions in section 12. No `PRAGMA user_version`: migrations are idempotent `CREATE IF NOT EXISTS`, by design. |
| 7 Event channels | Supervisor SSE → dashboard relay → `status` frames in the UI; studio child `NSJOB` protocol; cancel flag files; lease heartbeat; `run_finished`; setup bridge over stdio and the long-poll state channel; update worker state file; pending voice hand-off; error mapping: **V-dev** / **V-ci**. | WebView2 host ↔ page messages (minimize, maximize, drag, close, `open-external`): the close path **V-dev** (stop-all closes the window, the window closes itself), the buttons by clicking: **U**. Windows toasts actually shown: **U** on this laptop (the morning-note path is **V-dev**). Lease expiry after 600 s: **V-gpu**. |
| 8 External endpoints | Hugging Face `resolve` downloads with pinned SHA256 (LLM, embed, whisper), GitHub release assets (llama.cpp CPU build, yt-dlp, YuNet, uv), nuget WebView2 package, spaCy wheel, PyPI, the pre-flight reachability probe: **V-dev** (engine test, 749 MiB in 7 files, every hash checked). `api.github.com` latest release and the update zip download: **V-dev** (0.1.10 → 0.1.11). Pixabay key check and video search and CDN download: **V-dev** (owner's key, 2026-11-04). | Pexels (key check, search, CDN): **U** (needs a Pexels key from the owner). qwentts.cpp engine zip and the CUDA llama.cpp pair, cudart: **V-gpu**. OpenCode zip: **U**. PyTorch cu128 index (fallback): **V-gpu**. BtbN FFmpeg zip and the VC++ redistributable: **U** (this laptop had both). `git fetch` for `ns update --git`: **U**. yt-dlp against a real URL: **V-dev** (URL clip 2026-11-04). The pre-flight probe listed 7 hosts and the run needs 10 (fixed, section 12). |
| 9 Windows integration | Scheduled task (register, fire, interactive-only mark, remove), shortcuts, AUMID registration, Credential Manager keys (store, check, forget on uninstall), `NIGHTSHIFT_HOME` user variable, Job Object (children die with the engine), breakaway update worker, loopback-only binding, host mutex and file locks, WebView2 runtime detection and browser fallback, VC++ presence check, console title, process-tree kills, shell-folder lookup, execution-policy bypass on every spawn: **V-dev**. OneDrive refusal: **V-ci**. | DPAPI secrets for S4U, `WakeToRun`, keep-awake at night, the 6-hour task limit, session-0 restart: **V-gpu**. Elevated VC++ install, winget installs, the FFmpeg PATH entry, Mark-of-the-Web stripping, long-paths elevation: **U** (not needed on this laptop). Toast display: **U**. |
| 10 Configuration keys | `runtime.json` written by setup and tuned by `ns tune`, read by the supervisor, studio and dashboard; schema equality: **V-ci**; the real-model values (`ctx` 32768, `kv_type` q8_0, `threads` 4, `ngl` 0, `tts.engine` fake, whisper tiny on cpu, libx264): **V-dev** (engine test). | `llama.story_flags` and `llama.build`: written and read by nothing (section 12). `models_source_kind`: setup-UI round trip only. `tts_speed.json` extra keys, `update_check.json` `latest`, `schedule.json` `task_name`: written, not read (harmless, section 12). |

## 1. Processes

Paths are relative to the repo. `<home>` = `Paths.home` (`NIGHTSHIFT_HOME`, else `%LOCALAPPDATA%\Nightshift`, ns_common/paths.py:28-32). Log files are `<home>\logs\<component>-YYYYMMDD.jsonl` (ns_common/logging.py:34) unless stated.

| process | started by (file:line) | stopped by | which venv / binary | port(s) | log file |
|---|---|---|---|---|---|
| Supervisor / engine (`python -m ns_supervisor.app`, FastAPI+uvicorn, GPU lock, owner of the model children) | `launchers/_common.ps1:160` (Start-NsSupervisor, minimized, or `--headless` hidden); `ns_supervisor/client.py:143` (`spawn_supervisor`, detached; used by studio `ns_studio/acquirer.py:92`, `ns verify` `ns_supervisor/cli.py:123`, update relaunch `relaunch.py:95,109`) | `POST /shutdown` (`ns_supervisor/app.py:153`; callers `ns stop` `control.py:79`, `launchers/_common.ps1:103`, dashboard `/api/engine/shutdown`); idle auto-exit `core.py:727-752` (1800 s, `core.py:46`); post-run exit `core.py:718-725`; fallback kill of pids in `run/supervisor.json` `control.py:83-105`; lifespan end runs `stop_all(include_persistent=True)` `app.py:109` | `<home>\venvs\supervisor\Scripts\python.exe` (`services.py:54-57`, `launchers/_common.ps1:147`) | 8099 (`runtime.supervisor_port`, `app.py:236,245`) | `logs\supervisor-*.jsonl` (`core.py:114`) |
| Dashboard (`python -m ns_dashboard.server`, FastAPI+uvicorn; hosts the JobWorker thread `jobs.py:582` and EventRelay thread `supervisor_link.py:145`) | supervisor `procs.start` (`procs.py:217`) via `services._dashboard` (`services.py:149-160`); warmed at engine start `app.py:84-100`, or `POST /warmup {service:dashboard}` from `launchers/_common.ps1:218` | supervisor `stop_all(include_persistent=True)` `app.py:109`; `procs.terminate` `procs.py:283` (taskkill /T then /F); Job Object kill-on-close `procs.py:105`; `persistent=True` (`services.py:158`) so a plain `stop_all` keeps it | supervisor venv python (`services.py:150`) | 8100 (constant `ns_dashboard/__init__.py:5`, `server.py:715,727`; not in runtime.json) | `logs\dashboard-*.jsonl` (`ns_dashboard/handlers.py:78`) plus child stdout/stderr pump into the same component file (`procs.py:214,238`) |
| llama-server (story / code LLM, GPU tenant) | supervisor `services._llama` (`services.py:70-109`), `procs.start` (`procs.py:217`), on `POST /acquire service=llama` (`core.py:357-406`) | swapped out by another GPU tenant `core.py:333-339`; idle unload `core.py:639-666` (`idle_unload_s` 600, `core.py:39`); `release`/`stop_all`/`shutdown`; crash policy 3 in 600 s `core.py:43-44,206-216` | `<home>\bin\llama\llama-server.exe` (`services.py:79`); dev mode: `ns_supervisor.devfake` (`services.py:64-67,98-99`) | 8080 (`runtime.llama.port`, models.py:93); host `runtime.llama.host` (`--host`, `services.py:82`) | `logs\llama-*.jsonl` (stdout/stderr events, `procs.py:214,238`) |
| Embed server (llama-server `--embeddings`, CPU, `CUDA_VISIBLE_DEVICES=-1`) | supervisor `services._embed` (`services.py:112-128`) on first acquire (non-GPU owners set `core.py:388-396`) | `stop_all`/`shutdown`; not a GPU tenant so never swapped | same `bin\llama\llama-server.exe` (`services.py:115`); dev: devfake | 8081 (`runtime.embed.port`, models.py:108) | `logs\embed-*.jsonl` |
| TTS server (`python -m ns_tts.server`) | supervisor `services._tts` (`services.py:131-146`); dev: `--engine fake` (`services.py:141`) | as llama (GPU tenant when `device==cuda`, `services.py:144`) | `venvs\supervisor` python for engine `qwentts`, `venvs\tts` python for `reference` (`services.py:135-136`) | 8091 (`runtime.tts.port`, models.py:122; `--port` passed `services.py:136`; server binds `rt.tts.host` `ns_tts/server.py:406`) | `logs\tts-*.jsonl` (`ns_tts/server.py:395` plus supervisor pump) |
| qwen-tts.exe / qwen-codec.exe (one process per batch / per new voice) | `ns_tts/engine.py:439` (`subprocess.run`), `engine.py:468` (`Popen`, stream-by-line, cancellable `engine.py:283-300`) | exits itself; `POST /v1/audio/speech/cancel` kills the running batch (`ns_tts/server.py:302`) | `<home>\bin\qwentts\*.exe` (`paths.bin_qwentts`; `engine.py:86`) | none | stderr tail only in the raised error |
| Whisper (faster-whisper) | not a process: in-process `WhisperTranscriber` in the studio venv (`ns_studio/media/transcribe.py:230`); supervisor treats `whisper` as a virtual GPU tenant (`core.py:371-382`, `services.py:1-5`); probe `ns_supervisor/verify.py:127` runs `venvs\studio python -c WHISPER_SNIPPET` | ends with the studio / job child | `venvs\studio` python | none | `logs\studio-*.jsonl` |
| Dashboard studio child (`python -m ns_dashboard.studio_job <kind> --args <f>`; kinds `run, clip, rerun, series, download, transcribe, apply_voice`, `studio_job.py:38`) | `ns_dashboard/studio_runner.py:212` (`subprocess.Popen`, cwd = app folder, env `NIGHTSHIFT_HOME`, `NS_JOB_CANCEL_FILE`, `PYTHONUTF8`, `studio_runner.py:134-148`), called from `ns_dashboard/adapters.py:324` | cancel flag file `studio_runner.py:175`, then after `grace_s` 10 s `kill_tree` `studio_runner.py:61`; `adapters.release_leases` `adapters.py:327-330` | `<home>\venvs\studio\Scripts\python.exe` (`studio_runner.py:49-52`), fallback `sys.executable` | none (reaches 8099 through the `supervisor_url` arg `adapters.py:321`) | child stdout `NSJOB ` lines parsed by the runner; studio logs `logs\studio-*.jsonl`; args file `<home>\tmp\studio-<kind>-*.json` (`studio_runner.py:154-160`) |
| Link download yt-dlp | `ns_studio/clipper/source.py:333` (`Popen`), inside the `download` studio child | `CancelToken.cancel` kills it and removes its folder (`source.py:270-300`); flag `tmp\cancel-download-<source_id>` (`adapters.py:403`) | `<home>\bin\yt-dlp\yt-dlp.exe` (`source.py:231-233`) | none | 12-line stdout tail in the error |
| FFmpeg / ffprobe | `ns_common/ffmpeg.py:57,68` (`subprocess.run`) | exits itself; timeout 3600 s (`ffmpeg.py:50`) | PATH, `NS_FFMPEG`, `NS_FFPROBE`, `<home>\bin\ffmpeg` (`ffmpeg.py:15-17`) | none | `ffmpeg_run` debug event in the studio log |
| Studio CLI `studio.exe` (nightly `studio run --daily`, manual runs, verify) | task action `ns_supervisor/schedule.py:60,68`; `launchers/nightshift-studio.ps1:13,18`; `ns_supervisor/verify.py:180` (`studio run --fake-services`) | finishes; the nightly run calls `POST /run_finished` (`ns_studio/daily.py:216` -> `acquirer.py:206`) | `<home>\venvs\studio\Scripts\studio.exe` (`schedule.py:36-39`) | none | `logs\studio-*.jsonl` |
| Scheduled task `Nightshift Daily` | registered by `ns schedule install` (`schedule.py:42-88`; called by installer `s09_shortcuts_scheduler.py:140-141`) | `ns schedule remove` (`schedule.py:91-103,164-168`); `installer/uninstall.ps1:171`; execution limit 6 h (`schedule.py:73`) | action `cmd.exe /d /s /c "set NIGHTSHIFT_HOME=... && studio.exe run --daily"` (`schedule.py:60`) | none | Task Scheduler history; studio log |
| `opencode.exe` (Nightshift Code shortcut) | `launchers/nightshift-code.ps1:28` (after `POST /acquire llama alias code` `:20`) | exits; `finally` posts `POST /release` (`nightshift-code.ps1:36`) | `<home>\bin\opencode\opencode.exe`, `OPENCODE_CONFIG=config\opencode.json` (`:23`) | dials llama 8080 (baked into config, `s07_configs.py:30`) | none (console) |
| opencode prewarm (`--version`, `models`) | `installer/stage2/steps/s05_opencode_prewarm.py:22,55` | exits | `bin\opencode\opencode.exe` | none | installer log |
| WebView2 host (`powershell.exe -STA -File launchers/nightshift-host.ps1`) | dashboard: `launchers/nightshift-dashboard.ps1:56` (hidden, `-Url http://127.0.0.1:8100`); setup: `installer/stage2/web/launch.py:92-98,150` (`-Role setup`); after update: `relaunch.py:54` runs the dashboard launcher | window `cmd:close`; WM_CLOSE from `launchers/_common.ps1:112-143` (stop-all); self-closes after 3 missed 5 s pings (`nightshift-host.ps1:43-44,419-435`); uninstall `installer/stage2/web/uninstall.py:176` | `powershell.exe`; DLLs `<home>\bin\webview2` (`nightshift-host.ps1:72-81`) | none (dials the URL it is given) | none; state `run\apphost-<role>.json`, `config\window.json` |
| Browser app window fallback (Edge / Chrome `--app=`) | dashboard: `nightshift-dashboard.ps1:91`; setup: `launch.py:170-176` (profile `<data_dir>\browser`) | user closes | `msedge.exe` / `chrome.exe` | none | none |
| Setup web server (`python -m installer.stage2.web`, ThreadingHTTPServer) | `installer/stage1.ps1:1339` (window mode, stderr -> `setup-server.err`); `stage1.ps1:1324` (headless, in console) | window action `close-setup` / run finished (`installer/stage2/web/__main__.py:142-150`); killed by stage1 if it never answers within 60 s (`stage1.ps1:1350-1365`) | uv-managed Python 3.12 (`stage1.ps1:1262-1263`), `PYTHONPATH=<extracted app>` (`stage1.ps1:1320`) | random free port (`SetupServer(port=0)` `server.py:343`; `--port` `__main__.py:39`) | `%LOCALAPPDATA%\Nightshift\setup-server.err`, `bootstrap\setup-ui.jsonl` (`controller.py:385`), `logs\installer-*.jsonl` |
| Setup wizard engine (`python -X utf8 -m installer.stage2.wizard --ui-bridge stdio`) | `installer/stage2/web/engine.py:27-40,84` (`EngineProcess.start`) | `stop` command / `kill()` (`engine.py:107`) | same Python, `PYTHONPATH=<app>` (`engine.py:43-48`) | none (JSON lines on stdio) | `logs\installer-*.jsonl` (`wizard.py:331`) |
| Dashboard launch after setup Done | `installer/stage2/web/controller.py:184-190` (`launch_detached`), `:212` (`launch_captured`, probes 8100) | n/a | `launchers/nightshift-dashboard.ps1` through powershell | 8100 probe (`controller.py:194`) | launcher output captured |
| Installer tool runs (uv, ns, winget, vc_redist, powercfg, clip.exe, explorer) | `installer/stage2/runner.py:88`; `steps/s09_shortcuts_scheduler.py:141`; `web/bootstrap.py:77`; `web/controller.py:255,273,1878`; `secrets.py:91`; `web/preflight.py:322`; `web/uninstall.py:346` | exit | uv at `<home>\bin\uv\uv.exe` (`bootstrap.py:424,602`) | none | installer log |
| Deferred uninstaller | `installer/stage2/web/uninstall.py:183` (`spawn_deferred`); `UNINSTALL.cmd` -> `installer/uninstall.ps1` | exits | `powershell.exe` | none | `Nightshift-uninstall-leftovers.txt` (`uninstall.py:34`) |
| Update worker (`python -m ns_supervisor.cli update <zip> --worker --log --home`) | `ns_supervisor/update_job.py:233` (detached + breakaway), fallback WMI `Win32_Process.Create` (`update_job.py:191-208`), last resort plain detached; started from `launchers/update.ps1:25` (`--detach`) and the dashboard (`ns_dashboard/updater.py:114,128`); private venv copy `run\updater\venv-<stamp>` (`updater_copy.py:32-60`) | finishes; writes `run\update_state.json` (`update_job.py:68-110`); a helper deletes the venv copy afterwards (`update_job.py:388`) | copy of the supervisor venv python (`update_job.py:286`) | none | `logs\update-<timestamp>.log` (`update_job.py:174-177`) and `logs\update-*.jsonl` (`update.py:283`) |
| Post-update relaunch | `ns_supervisor/relaunch.py:54` (hidden powershell running the dashboard launcher) and `relaunch.py:95,109` (`spawn_supervisor`) | n/a | powershell | n/a | none |
| Dashboard-spawned `ns` commands (`update --check`, `update --github`, `update <zip>`, `models get <name>`, `models --help`, `backup`, `restore`) | `ns_dashboard/updater.py:28-43` (`stream_command` Popen), `:46-56` (`default_runner`), commands built `:105-128,187-217`; model download thread `ns-model-<name>` `ns_dashboard/svc_library.py:458` | exits (timeouts 60-3600 s, `updater.py:110-123`) | `venvs\supervisor\Scripts\ns.exe` else `sys.executable -m ns_supervisor.cli` (`updater.py:187-190`) | none | lines streamed to the UI |
| PowerShell helper (toast, task registration, WMI create) | `ns_supervisor/pwsh.py:33` (`run_powershell`), callers `notify.py:47`, `schedule.py:125`, `update_job.py:200` | exits | `powershell.exe` | none | none |
| `nvidia-smi` probes | `ns_supervisor/procs.py:314`, `ns_supervisor/gpu.py:67`, `ns_supervisor/doctor.py:63` | exits (20 s timeout) | `nvidia-smi` on PATH | none | none |
| `netstat` / `tasklist` / `taskkill` | `ns_supervisor/presence.py:78`, `control.py:36`, `procs.py:251,271` | exit | Windows system tools | none | none |
| Dev fakes (`python -m ns_supervisor.devfake`) | `services._dev_cmd` (`services.py:64-67`) when `runtime.dev_mode` | as llama / embed | supervisor venv python | the llama / embed ports | as llama / embed |
| Dev-only: `dev/run_services_dev.ps1` (llama, embed, tts in own consoles) | `dev/run_services_dev.ps1:10,17,18` (manual) | user | per script | 8080/8081/8091 from runtime.json | console |
| Dev-only demo server (`node dev/demo/serve.js`) | `DEMO.cmd` | Ctrl+C | Node.js | first free from 8790 (`dev/demo/serve.js:21`) | console |

## 2. Ports and bind addresses

No code binds `0.0.0.0` (grep found none); every server binds `127.0.0.1` (or `rt.<svc>.host` for llama/embed/tts, default `127.0.0.1`). Only the dashboard's Host/Origin check (`ns_dashboard/security.py:8-35`) and the setup server's Host check (`installer/stage2/web/server.py:101`) validate the peer name.

| port | owner process | who dials it | config key that sets it | file:line |
|---|---|---|---|---|
| 8099 | supervisor (uvicorn, host hard-coded `127.0.0.1`) | `SupervisorClient` (`ns_supervisor/client.py:23,37-38`: ns CLI, studio acquirer, update relaunch); dashboard `SupervisorLink` (`ns_dashboard/supervisor_link.py:24,28-45,49-105`) and `HttpAcquirer` (`:189,202`); studio_job child via `supervisor_url` arg (`studio_job.py:83`); launchers (`launchers/_common.ps1:44-54,65,75,203`); `nightshift-code.ps1:20,36`; supervisor health `ns stop` (`control.py:79`) | `Runtime.supervisor_port` (default 8099, `ns_common/models.py:151`); CLI `--port` (`app.py:211,236`); env `NS_SUPERVISOR_URL` overrides the URL only for the dashboard link (`supervisor_link.py:30-31`) | `ns_supervisor/app.py:236,245` |
| 8099 (duplicate constants) | - | the literal 8099 also appears as fallback in `ns_supervisor/client.py:23`, `ns_dashboard/supervisor_link.py:24`, `launchers/_common.ps1:50`, `contracts/schemas/runtime.schema.json:353` | same key | see left |
| 8100 | dashboard (uvicorn, host hard-coded `127.0.0.1`) | supervisor health poll `GET /api/health` (`ns_supervisor/services.py:18,156,30`; `core.py:284`); presence probe counts established connections to it (`presence.py:103-112`, port from `services["dashboard"].definition.port` `core.py:131`); `launchers/nightshift-dashboard.ps1:11,38`; WebView2 host `-Url` (`:55`); installer `controller.py:194,212`; `dev/gate_m3_dashboard.py:27`; vite proxy `ns_dashboard/ui/vite.config.ts:24` | no config key. Constants: `ns_dashboard/__init__.py:5` (`DEFAULT_PORT`), `ns_supervisor/services.py:18` (`DASHBOARD_PORT`), `launchers/nightshift-dashboard.ps1:11`, `installer/stage2/web/controller.py:194`; CLI `--port` (`server.py:715`) is not passed by the supervisor (`services.py:150`) | `ns_dashboard/server.py:727` |
| 8080 | llama-server | `ns_common/llm_client.py:52,93` (`LlamaCfg.base_url` models.py:101-103), supervisor health `/health`; opencode via `config\opencode.json` baseURL literal `http://127.0.0.1:8080/v1` | `Runtime.llama.port` / `.host` (models.py:93-92); passed as `--port`/`--host` (`services.py:81-82`) | `ns_supervisor/services.py:81-82` |
| 8081 | embed llama-server | `ns_common/embed_client.py:115,124` (`EmbedCfg.base_url`); supervisor health | `Runtime.embed.port` / `.host` (models.py:108-107) | `ns_supervisor/services.py:118-119` |
| 8091 | TTS server (uvicorn, host `rt.tts.host`) | `ns_common/tts_client.py:107,137,153,160,168` (`TtsCfg.base_url`); dashboard `RealTtsAdapter` fallback literal `http://127.0.0.1:8091` (`ns_dashboard/adapters.py:671`) and voice design/clone (`adapters.py:768,820`); `ns_supervisor/verify.py:200`; studio `voice_setup.py:145,183` | `Runtime.tts.port` / `.host` (models.py:122-121); `--port` passed (`services.py:136`) and `args.port or rt.tts.port` (`ns_tts/server.py:404`) | `ns_tts/server.py:406` |
| ephemeral (OS-chosen, `port=0`) | setup web server (ThreadingHTTPServer `127.0.0.1`) | setup window (WebView2/Edge/Chrome, URL `http://127.0.0.1:<port>/?t=<token>`, `server.py:354`); `stage1.ps1:1023` (`/api/ping`, header `X-NS-Token`); `stage1.ps1:1042` (`/api/internal/open-window`); port recorded in `%LOCALAPPDATA%\Nightshift\setup-server.json` (`launch.py:25,38-52`) | CLI `--port` (`installer/stage2/web/__main__.py:39`, default 0); no config file key | `installer/stage2/web/server.py:343` |
| 8765 | dev-only: setup server in `--simulate --no-window --port 8765` | vite dev proxy (`ns_dashboard/ui/vite.installer.config.ts:52`), docs in `installer/stage2/web/fixtures.py:3` | CLI `--port` | dev only |
| 5173 / 5179 | dev-only: vite dev servers | developer browser | `ns_dashboard/ui/vite.config.ts:24`, `vite.installer.config.ts:52` | dev only |
| 8790+ (first free) | dev-only: `dev/demo/serve.js` | developer browser (`DEMO.cmd`) | `--port` (`serve.js:20-21`) | dev only |
| any | `dev/run_services_dev.ps1` starts llama / embed / tts with the runtime.json ports | - | `$rt.llama.port`, `$rt.embed.port`, `$rt.tts.port` | `dev/run_services_dev.ps1:10,17,18` |
| port-free check (not a bind) | `ns_common/ports.py:25-34` (`is_free`, binds `127.0.0.1:<port>` briefly); `ensure_free` -> `NS-E116` | called from `ns_supervisor/procs.py:213` (before each service start), `ns_supervisor/client.py:169` (before spawning the engine), `installer/stage2/steps/s08_smoke.py:72` | ports table `LABELS` (`ports.py:19-22`) | - |
| documentation-only port strings | assistant orientation text lists ports as prose | `ns_dashboard/assistant/orientation.py:39-44` | none (hard-coded text, not read from runtime.json) | - |

### Outbound sockets that are not local servers
Hosts are listed in section 8. The WebView2 host health pings `http://<start-url authority>/` with HEAD every 5 s (`launchers/nightshift-host.ps1:211-229,43-44`).

## 3. HTTP routes

### 3a. Supervisor / engine API (`ns_supervisor/app.py`, 127.0.0.1:8099; NsError -> HTTP map `_STATUS` {E201:409, E202:502, E203:504} `app.py:31`, handler `app.py:115`)

| method | path | handler file:line | called by |
|---|---|---|---|
| GET | `/health` | `ns_supervisor/app.py:120` | `launchers/_common.ps1:65` (Test-NsSupervisor, used by every launcher and `Wait-NsSupervisor` `:189-197`) |
| GET | `/status` | `ns_supervisor/app.py:124` (runs `sup.status`, nvidia-smi) | `ns_supervisor/client.py:96` (`is_up`, ns CLI, studio); `ns_dashboard/supervisor_link.py:52` (dashboard status); `launchers/_common.ps1:75` (`Get-NsStatus`, dashboard launcher session check) |
| POST | `/acquire` | `ns_supervisor/app.py:128` (body `AcquireBody` :34) | `ns_supervisor/client.py:78` (studio acquirer, verify, tune); `ns_dashboard/supervisor_link.py:189` (`HttpAcquirer` fallback); `launchers/nightshift-code.ps1:20` |
| POST | `/release` | `ns_supervisor/app.py:134` | `ns_supervisor/client.py:83`; `ns_dashboard/supervisor_link.py:202`; `launchers/nightshift-code.ps1:36` |
| POST | `/heartbeat` | `ns_supervisor/app.py:138` | `ns_supervisor/client.py:87` (studio lease beat `ns_studio/acquirer.py:169`); `ns_dashboard/supervisor_link.py:90` (job pulse `ns_dashboard/handlers.py:91`, assistant lease) |
| POST | `/release_owner` | `ns_supervisor/app.py:142` | `ns_supervisor/client.py:91`; `ns_dashboard/supervisor_link.py:103` (called `adapters.py:339,802`, `svc_engine.py:297`) |
| POST | `/stop_all` | `ns_supervisor/app.py:148` | `ns_supervisor/client.py:108`; `ns_dashboard/supervisor_link.py:84` (via `/api/engine/stop_all`) |
| POST | `/shutdown` | `ns_supervisor/app.py:153` | `ns_supervisor/client.py:112` (`ns stop`, update); `ns_dashboard/supervisor_link.py:80`; `launchers/_common.ps1:103` (Stop-NsEngine) |
| POST | `/warmup` | `ns_supervisor/app.py:158` | `ns_supervisor/client.py:116` (studio `acquirer.py:221`); `ns_dashboard/supervisor_link.py:76`; `launchers/_common.ps1:218` (Start-NsDashboardService) |
| POST | `/run_finished` | `ns_supervisor/app.py:162` | `ns_supervisor/client.py:122` <- `ns_studio/acquirer.py:206` <- `ns_studio/daily.py:216` |
| POST | `/events` | `ns_supervisor/app.py:166` (body `EventBody` :70) | no caller found in product code; `SupervisorClient.publish_event` (`ns_supervisor/client.py:124`) has no caller either (only `tests/test_supervisor_client.py:55`) |
| GET | `/events` (SSE; first frame `{kind:"service", data:{event:"snapshot",...}}` `app.py:176-178`, keepalive `: keepalive` every 15 s `:184`) | `ns_supervisor/app.py:171` | `ns_dashboard/supervisor_link.py:114` (EventRelay) |


### 3b. Dashboard API (`ns_dashboard/server.py`, 127.0.0.1:8100) - generated by a separate pass, see its own notes

All 84 dynamic routes plus 1 static mount are registered inside `create_app()` in `ns_dashboard\server.py`. No other module registers dashboard routes (grep for `@app.`, `APIRouter`, `include_router`, `add_api_route` finds only server.py; handlers are thin wrappers over `dash.*` service objects in svc_*.py, uploads.py, media.py, assistant/*). `ns_supervisor/app.py` is a separate server (/health, /status, /acquire ...) and is NOT part of this table.

Handler column = decorator line (and `def` line) in `ns_dashboard/server.py`. Caller paths are relative to ``. `api.get("/x")` in the UI prefixes `/api` (ns_dashboard/ui/src/lib/api.ts:8). ns_dashboard/ui/src/lib/demo.ts also mimics many routes for the offline demo layer; that is not counted as a caller. No caller was found in launchers/*.ps1, installer/, ns_studio/, ns_common/ for any route other than those cited (the launchers and supervisor only use the supervisor's own /health and /status, plus `GET /` and `/api/health` on the dashboard).

#### /api/health, /api/status

| method | path | handler (file:line) | called by |
|---|---|---|---|
| GET | `/api/health` | ns_dashboard/server.py:198 (`def health` :199) | ns_supervisor/core.py:284 and ns_supervisor/tune.py:258 poll ServiceDef.health_url (ns_supervisor/services.py:42,153 health_path=/api/health); tests/test_dashboard_devrun.py:34 (tests also) |
| GET | `/api/status` | ns_dashboard/server.py:204 (`def status` :205) | ns_dashboard/ui/src/lib/api.ts:53 (api.status(), called from ns_dashboard/ui/src/lib/app.tsx:46); ns_dashboard/ui/src/pages/Settings.tsx:70; dev/gate_m3_dashboard.py:71 |

#### /api/events

| method | path | handler (file:line) | called by |
|---|---|---|---|
| GET | `/api/events` | ns_dashboard/server.py:208 (`def events` :209) | ns_dashboard/ui/src/lib/app.tsx:84 (new EventSource("/api/events")) |

#### /api/engine

| method | path | handler (file:line) | called by |
|---|---|---|---|
| POST | `/api/engine/warmup` | ns_dashboard/server.py:218 (`def engine_warmup` :219) | ns_dashboard/ui/src/pages/Settings.tsx:129 |
| POST | `/api/engine/shutdown` | ns_dashboard/server.py:223 (`def engine_shutdown` :224) | ns_dashboard/ui/src/shell/Shell.tsx:155; ns_dashboard/ui/src/shell/TitleBar.tsx:54 |
| POST | `/api/engine/release_gpu` | ns_dashboard/server.py:228 (`def engine_release_gpu` :229) | ns_dashboard/ui/src/pages/Queue.tsx:362 |
| POST | `/api/engine/stop_all` | ns_dashboard/server.py:233 (`def engine_stop_all` :234) | ns_dashboard/ui/src/pages/Settings.tsx:132 |

#### /api/channels

| method | path | handler (file:line) | called by |
|---|---|---|---|
| GET | `/api/channels` | ns_dashboard/server.py:238 (`def channels_list` :239) | ns_dashboard/ui/src/pages/Channels.tsx:26 |
| POST | `/api/channels` | ns_dashboard/server.py:242 (`def channels_create` :243) | ns_dashboard/ui/src/pages/Channels.tsx:137; dev/gate_m3_dashboard.py:77 |
| GET | `/api/channels/{slug}` | ns_dashboard/server.py:246 (`def channels_get` :247) | ns_dashboard/ui/src/pages/Channels.tsx:95 |
| PUT | `/api/channels/{slug}` | ns_dashboard/server.py:250 (`def channels_update` :251) | ns_dashboard/ui/src/pages/Channels.tsx:137 |
| POST | `/api/channels/{slug}/pause` | ns_dashboard/server.py:254 (`def channels_pause` :255) | ns_dashboard/ui/src/pages/Channels.tsx:31; ns_dashboard/ui/src/pages/Queue.tsx:356 |
| POST | `/api/channels/{slug}/resume` | ns_dashboard/server.py:259 (`def channels_resume` :260) | ns_dashboard/ui/src/pages/Channels.tsx:31 |
| POST | `/api/channels/{slug}/validate` | ns_dashboard/server.py:264 (`def channels_validate` :265) | ns_dashboard/ui/src/pages/Channels.tsx:130 |

#### /api/outputs

| method | path | handler (file:line) | called by |
|---|---|---|---|
| GET | `/api/outputs` | ns_dashboard/server.py:270 (`def outputs_list` :271) | ns_dashboard/ui/src/pages/Queue.tsx:45; ns_dashboard/ui/src/pages/Create.tsx:33; dev/gate_m3_dashboard.py:105,121 |
| POST | `/api/outputs/{output_id:path}/approve` | ns_dashboard/server.py:283 (`def outputs_approve` :284) | ns_dashboard/ui/src/pages/Queue.tsx:158; dev/gate_m3_dashboard.py:116 |
| POST | `/api/outputs/{output_id:path}/reject` | ns_dashboard/server.py:287 (`def outputs_reject` :288) | ns_dashboard/ui/src/pages/Queue.tsx:162 |
| POST | `/api/outputs/{output_id:path}/uploaded` | ns_dashboard/server.py:292 (`def outputs_uploaded` :293) | ns_dashboard/ui/src/pages/Queue.tsx:166 |
| POST | `/api/outputs/{output_id:path}/open-folder` | ns_dashboard/server.py:297 (`def outputs_open_folder` :298) | ns_dashboard/ui/src/pages/Queue.tsx:169 |
| GET | `/api/outputs/{output_id:path}` | ns_dashboard/server.py:301 (`def outputs_detail` :302) | ns_dashboard/ui/src/pages/Queue.tsx:138; dev/gate_m3_dashboard.py:118 |

#### /api/media, /api/thumbs

| method | path | handler (file:line) | called by |
|---|---|---|---|
| GET | `/api/thumbs/{n}/{output_id:path}` | ns_dashboard/server.py:306 (`def media_thumb_choice` :307) | ns_dashboard/ui/src/lib/api.ts:44 (api.thumbChoiceUrl) used at ns_dashboard/ui/src/pages/Queue.tsx:190 |
| GET | `/api/media/{output_id:path}/thumb.jpg` | ns_dashboard/server.py:311 (`def media_thumb` :312) | ns_dashboard/ui/src/lib/api.ts:42 (api.thumbUrl) used at ns_dashboard/ui/src/pages/Queue.tsx:223 (video poster) |
| GET, HEAD | `/api/media/{output_id:path}` | ns_dashboard/server.py:316 (`def media_stream` :317) | ns_dashboard/ui/src/lib/api.ts:41 (api.mediaUrl) used at ns_dashboard/ui/src/pages/Queue.tsx:223 (video src); dev/gate_m3_dashboard.py:113 |

#### /api/uploads

| method | path | handler (file:line) | called by |
|---|---|---|---|
| POST | `/api/uploads` | ns_dashboard/server.py:327 (`def uploads_start` :328) | ns_dashboard/ui/src/lib/api.ts:87 (api.uploadFile; used by ns_dashboard/ui/src/pages/Create.tsx:77 and by api.voiceClone api.ts:79 -> ns_dashboard/ui/src/pages/Library.tsx:309) |
| GET | `/api/uploads/{upload_id}` | ns_dashboard/server.py:338 (`def uploads_status` :339) | tests/test_dashboard_api.py:330 (tests only) |
| PUT | `/api/uploads/{upload_id}` | ns_dashboard/server.py:342 (`def uploads_append` :343) | ns_dashboard/ui/src/lib/api.ts:90 (chunk PUT inside api.uploadFile) |
| POST | `/api/uploads/{upload_id}/complete` | ns_dashboard/server.py:349 (`def uploads_complete` :350) | ns_dashboard/ui/src/lib/api.ts:94 (api.uploadFile) |

#### /api/jobs

| method | path | handler (file:line) | called by |
|---|---|---|---|
| POST | `/api/jobs/run` | ns_dashboard/server.py:354 (`def jobs_run` :355) | ns_dashboard/ui/src/pages/Create.tsx:72 |
| POST | `/api/jobs/verify` | ns_dashboard/server.py:358 (`def jobs_verify` :359) | ns_dashboard/ui/src/pages/Settings.tsx:137 |
| POST | `/api/jobs/clip` | ns_dashboard/server.py:362 (`def jobs_clip` :363) | ns_dashboard/ui/src/pages/Create.tsx:78 and :80; dev/gate_m3_dashboard.py:83,87 |
| POST | `/api/jobs/clip/rerun` | ns_dashboard/server.py:379 (`def jobs_clip_rerun` :380) | ns_dashboard/ui/src/pages/Create.tsx:330 |
| GET | `/api/jobs` | ns_dashboard/server.py:383 (`def jobs_list` :384) | ns_dashboard/ui/src/pages/Create.tsx:32; pages/Assistant.tsx:179; pages/Queue.tsx:49; shell/Shell.tsx:102; pages/Library.tsx:239 (?kind=apply_voice); pages/Settings.tsx:134 (?kind=verify) |
| GET | `/api/jobs/{job_id}` | ns_dashboard/server.py:391 (`def jobs_get` :392) | ns_dashboard/ui/src/pages/Library.tsx:245; dev/gate_m3_dashboard.py:43 |
| POST | `/api/jobs/{job_id}/resume` | ns_dashboard/server.py:395 (`def jobs_resume` :396) | ns_dashboard/ui/src/ui/JobError.tsx:16 |
| POST | `/api/jobs/{job_id}/cancel` | ns_dashboard/server.py:399 (`def jobs_cancel` :400) | ns_dashboard/ui/src/pages/Create.tsx:39; ns_dashboard/ui/src/pages/Library.tsx:268 |
| POST | `/api/jobs/{job_id}/dismiss` | ns_dashboard/server.py:403 (`def jobs_dismiss` :404) | ns_dashboard/ui/src/pages/Create.tsx:332 |

#### /api/notes, /api/runs, /api/logs, /api/learn

| method | path | handler (file:line) | called by |
|---|---|---|---|
| GET | `/api/notes` | ns_dashboard/server.py:408 (`def notes` :409) | ns_dashboard/ui/src/pages/Queue.tsx:474; ns_dashboard/ui/src/shell/Shell.tsx:106 |
| POST | `/api/notes/clear` | ns_dashboard/server.py:412 (`def notes_clear` :413) | ns_dashboard/ui/src/shell/Shell.tsx:109 |
| GET | `/api/runs` | ns_dashboard/server.py:416 (`def runs` :417) | ns_dashboard/ui/src/pages/Queue.tsx:473 |
| GET | `/api/logs/tail` | ns_dashboard/server.py:420 (`def logs_tail` :421) | ns_dashboard/ui/src/pages/Settings.tsx:27 |
| GET | `/api/logs/components` | ns_dashboard/server.py:432 (`def logs_components` :433) | ns_dashboard/ui/src/pages/Settings.tsx:24 |
| POST | `/api/learn/import` | ns_dashboard/server.py:436 (`def learn_import` :437) | ns_dashboard/ui/src/pages/Results.tsx:26 |
| GET | `/api/learn/summary` | ns_dashboard/server.py:446 (`def learn_summary` :447) | ns_dashboard/ui/src/pages/Results.tsx:22 |

#### /api/settings, /api/diagnostics

| method | path | handler (file:line) | called by |
|---|---|---|---|
| GET | `/api/settings` | ns_dashboard/server.py:451 (`def settings_get` :452) | ns_dashboard/ui/src/pages/Settings.tsx:17; ns_dashboard/ui/src/lib/app.tsx:65 |
| PUT | `/api/settings` | ns_dashboard/server.py:455 (`def settings_put` :456) | ns_dashboard/ui/src/pages/Settings.tsx:31 |
| POST | `/api/settings/keys` | ns_dashboard/server.py:461 (`def settings_keys` :462) | ns_dashboard/ui/src/pages/Settings.tsx:35 |
| POST | `/api/diagnostics` | ns_dashboard/server.py:466 (`def diagnostics` :467) | ns_dashboard/ui/src/pages/Settings.tsx:109; ns_dashboard/ui/src/ui/JobError.tsx:13 |
| GET | `/api/diagnostics/latest` | ns_dashboard/server.py:470 (`def diagnostics_latest` :471) | ns_dashboard/ui/src/pages/Settings.tsx:228 (LinkButton href=report.download_url; the value "/api/diagnostics/latest" is built at ns_dashboard/svc_settings.py:202) |
| POST | `/api/settings/export` | ns_dashboard/server.py:475 (`def settings_export` :476) | ns_dashboard/ui/src/pages/Settings.tsx:111 |
| GET | `/api/settings/export/latest` | ns_dashboard/server.py:479 (`def settings_export_latest` :480) | ns_dashboard/ui/src/pages/Settings.tsx:188 (LinkButton href=backup.download_url; the value "/api/settings/export/latest" is built at ns_dashboard/svc_settings.py:345) |
| POST | `/api/settings/import` | ns_dashboard/server.py:484 (`def settings_import` :485) | ns_dashboard/ui/src/pages/Settings.tsx:116 |
| GET | `/api/settings/uninstall` | ns_dashboard/server.py:494 (`def settings_uninstall` :495) | ns_dashboard/ui/src/pages/Settings.tsx:120 and :284 |

#### /api/templates, /api/voices

| method | path | handler (file:line) | called by |
|---|---|---|---|
| GET | `/api/templates` | ns_dashboard/server.py:499 (`def templates` :500) | ns_dashboard/ui/src/pages/Create.tsx:49 |
| GET | `/api/voices` | ns_dashboard/server.py:503 (`def voices` :504) | ns_dashboard/ui/src/pages/Channels.tsx:103; ns_dashboard/ui/src/pages/Library.tsx:284 |
| POST | `/api/voices/preview` | ns_dashboard/server.py:507 (`def voices_preview` :508) | ns_dashboard/ui/src/lib/api.ts:66 (api.voicePreview; used at pages/Channels.tsx:121 and pages/Library.tsx:162) |
| GET | `/api/voices/pending` | ns_dashboard/server.py:512 (`def voices_pending` :513) | ns_dashboard/ui/src/pages/Library.tsx:232 |
| POST | `/api/voices/apply-pending` | ns_dashboard/server.py:516 (`def voices_apply_pending` :517) | ns_dashboard/ui/src/pages/Library.tsx:264 |
| POST | `/api/voices/design` | ns_dashboard/server.py:520 (`def voices_design` :521) | ns_dashboard/ui/src/lib/api.ts:74 (api.voiceDesign; used at pages/Library.tsx:308) |
| POST | `/api/voices/clone` | ns_dashboard/server.py:524 (`def voices_clone` :525) | ns_dashboard/ui/src/lib/api.ts:80 (api.voiceClone; used at pages/Library.tsx:309) |

#### /api/library

| method | path | handler (file:line) | called by |
|---|---|---|---|
| GET | `/api/library/ideas` | ns_dashboard/server.py:529 (`def library_ideas` :530) | ns_dashboard/ui/src/pages/Library.tsx:63; ns_dashboard/ui/src/pages/Create.tsx:228 |
| POST | `/api/library/ideas` | ns_dashboard/server.py:541 (`def library_ideas_add` :542) | ns_dashboard/ui/src/pages/Library.tsx:71 |
| PUT | `/api/library/ideas/{idea_id}` | ns_dashboard/server.py:545 (`def library_ideas_status` :546) | ns_dashboard/ui/src/pages/Library.tsx:74 |
| GET | `/api/library/stories` | ns_dashboard/server.py:552 (`def library_stories` :553) | ns_dashboard/ui/src/pages/Library.tsx:117 |
| GET | `/api/library/footage` | ns_dashboard/server.py:560 (`def library_footage` :561) | ns_dashboard/ui/src/pages/Library.tsx:456 |
| POST | `/api/library/footage/fetch` | ns_dashboard/server.py:567 (`def library_footage_fetch` :568) | ns_dashboard/ui/src/pages/Library.tsx:465 |
| DELETE | `/api/library/footage/{source}/{clip_id}` | ns_dashboard/server.py:575 (`def library_footage_delete` :576) | ns_dashboard/ui/src/pages/Library.tsx:460 |
| GET | `/api/library/formats` | ns_dashboard/server.py:579 (`def library_formats` :580) | ns_dashboard/ui/src/pages/Library.tsx:369; ns_dashboard/ui/src/pages/Channels.tsx:27 |
| POST | `/api/library/formats` | ns_dashboard/server.py:583 (`def library_formats_create` :584) | ns_dashboard/ui/src/pages/Library.tsx:370 |
| PUT | `/api/library/formats/{format_id}` | ns_dashboard/server.py:587 (`def library_formats_update` :588) | ns_dashboard/ui/src/pages/Library.tsx:426 |
| GET | `/api/library/series` | ns_dashboard/server.py:591 (`def library_series` :592) | ns_dashboard/ui/src/pages/Library.tsx:508 |
| POST | `/api/library/series/{slug}/produce` | ns_dashboard/server.py:595 (`def library_series_produce` :596) | ns_dashboard/ui/src/pages/Library.tsx:515 |

#### /api/update, /api/models

| method | path | handler (file:line) | called by |
|---|---|---|---|
| GET | `/api/update/check` | ns_dashboard/server.py:600 (`def update_check` :601) | ns_dashboard/ui/src/pages/Settings.tsx:37 |
| GET | `/api/update/state` | ns_dashboard/server.py:604 (`def update_state` :605) | ns_dashboard/ui/src/pages/Settings.tsx:59 and :86 |
| POST | `/api/models/get` | ns_dashboard/server.py:608 (`def models_get` :609) | ns_dashboard/ui/src/pages/Settings.tsx:125; ns_dashboard/ui/src/pages/Library.tsx:197 |
| GET | `/api/models/state` | ns_dashboard/server.py:613 (`def models_state` :614) | ns_dashboard/ui/src/pages/Settings.tsx:118 |
| POST | `/api/update/file` | ns_dashboard/server.py:617 (`def update_file` :618) | ns_dashboard/ui/src/pages/Settings.tsx:104 |
| POST | `/api/update/apply` | ns_dashboard/server.py:630 (`def update_apply` :631) | ns_dashboard/ui/src/pages/Settings.tsx:90 |

#### /api/assistant

| method | path | handler (file:line) | called by |
|---|---|---|---|
| POST | `/api/assistant/chat` | ns_dashboard/server.py:635 (`def assistant_chat` :636) | ns_dashboard/ui/src/lib/api.ts:117 (api.chat; consumed at pages/Assistant.tsx:60) |
| POST | `/api/assistant/confirm` | ns_dashboard/server.py:655 (`def assistant_confirm` :656) | ns_dashboard/ui/src/pages/Assistant.tsx:99 |
| POST | `/api/assistant/dismiss` | ns_dashboard/server.py:660 (`def assistant_dismiss` :661) | ns_dashboard/ui/src/pages/Assistant.tsx:100 |
| GET | `/api/assistant/history` | ns_dashboard/server.py:665 (`def assistant_history` :666) | ns_dashboard/ui/src/pages/Assistant.tsx:82 and :92 |
| GET | `/api/assistant/conversations` | ns_dashboard/server.py:670 (`def assistant_conversations` :671) | ns_dashboard/ui/src/pages/Assistant.tsx:80 and :180 |
| GET | `/api/assistant/tools` | ns_dashboard/server.py:674 (`def assistant_tools` :675) | ns_dashboard/ui/src/pages/Assistant.tsx:43 |
| POST | `/api/assistant/queue/run` | ns_dashboard/server.py:682 (`def assistant_queue_run` :683) | ns_dashboard/ui/src/pages/Assistant.tsx:193 |

#### Static file / SPA routes

| method | path | handler (file:line) | called by |
|---|---|---|---|
| GET, HEAD | `/` and everything under it (index.html via `html=True`; `/app.js`, `/app.css`, `/brand/*` incl. favicon.svg, nightshift.ico, icon-256.png; `/bg/*`; `/assets/*` fonts) | ns_dashboard/server.py:690 (`app.mount("/", StaticFiles(directory=ns_dashboard/static, html=True), name="static")`, mounted only when the folder exists; folder from ns_dashboard/handlers.py:103 `default_static_dir`) | launchers/nightshift-dashboard.ps1:11,38,55,91,98 (opens http://127.0.0.1:8100; Wait-NsPort at launchers/_common.ps1:232 GETs it); installer/stage2/web/controller.py:194 (`dashboard_answers`, GET http://127.0.0.1:8100/); ns_dashboard/static/index.html:8-12 (refs ./app.js, ./app.css, ./brand/*) |

There is no separate SPA fallback route and no `/static` URL prefix (the mount name is only an internal label): unknown non-API paths 404 through the mount; any 404 is rendered as JSON by the StarletteHTTPException handler (server.py:192).

#### Websocket / SSE routes

There are no websocket routes (LoopbackMiddleware server.py:62-63 would close a websocket with code 1008 on a bad Host/Origin, but none is registered). The two SSE (`text/event-stream`) endpoints are rows in the tables above: `GET /api/events` (server.py:208, group /api/health, /api/status) and `POST /api/assistant/chat` (server.py:635, group /api/assistant).

#### Dashboard server bind

- Host/port: `uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")` at ns_dashboard/server.py:727 (loopback only, hard-coded host). Entry point `python -m ns_dashboard.server [--port N] [--open] [--fake-services]` (`main`, server.py:711-728; prints the URL at :723).
- Port choice: constant `DEFAULT_PORT = 8100` (ns_dashboard/__init__.py:5) used as the argparse default (server.py:715). No env var and no runtime.json key selects the port. The supervisor starts it with no `--port` flag (ns_supervisor/services.py:146-157: cmd `-m ns_dashboard.server`, `DASHBOARD_PORT = 8100` at services.py:18, `health_path="/api/health"` at :153), and launchers/nightshift-dashboard.ps1:11 and installer/stage2/web/controller.py:194 hard-code `127.0.0.1:8100`.
- Middleware: one pure-ASGI `LoopbackMiddleware` (server.py:55-102), added at server.py:692 (after all routes and the static mount). Checks, applied to `http` and `websocket` scopes (server.py:62-63):
  - Host header must be 127.0.0.1, localhost or ::1 (port allowed): `is_loopback_host` ns_dashboard/security.py:21-25 (via `_host_only` :11-18).
  - Origin header must be absent/empty or loopback; the literal `null` origin is refused: `is_loopback_origin` ns_dashboard/security.py:28-34.
  - On failure (server.py:71-91): HTTP gets 403 JSON `{"error":{"code":"forbidden_host",...}}` (:77-90) and a `request_refused` warning is logged (:73); a websocket is closed with code 1008 (:75). dev/gate_m3_dashboard.py:74 and tests/test_dashboard_security.py exercise it.
  - Response headers added (server.py:90-99): `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer` on everything; `Content-Security-Policy` (CSP constant server.py:40-44: default-src 'self', script-src 'self', connect-src 'self', frame-ancestors 'none', base-uri 'none', form-action 'self') on non-`/api/` paths; `Cache-Control: no-store` on `/api/` paths except `/api/media/*` and `/api/events`.
  - No auth token, cookie or CSRF token exists in the dashboard; security is only the loopback Host/Origin check. security.py also has path helpers `safe_join` (:37) and `is_within` (:46) used for file access. FastAPI docs/redoc/openapi are disabled (server.py:172-174 `docs_url=None, redoc_url=None, openapi_url=None`).
  - Lifespan (server.py:160-170 (`lifespan`)): starts `st.worker` (job thread) and `EventRelay` (supervisor event relay) unless `start_worker=False`; stops them and `dash.close()` on exit. `configure_tmp` (server.py:695-710) points temp/upload spooling at the Nightshift `tmp` folder.

#### SSE / event stream kinds published to the browser

Browser-facing SSE endpoint 1: `GET /api/events` (server.py:208 -> `dash.engine.events` ns_dashboard/svc_engine.py:325 -> `iter_sse` ns_dashboard/events.py:82-111). Frame = `data: {"kind":..., "payload":...}` built by `sse_frame` (events.py:76-79). Only two frame kinds reach the browser:

| frame kind | emitted at | source events (hub publish calls) | consumed by |
|---|---|---|---|
| `status` | events.py:100 (first frame), :109 (after every event and every `heartbeat_s` = 2.0 s idle); payload = the `/api/status` object (`svc_engine.status`) | any hub event triggers a fresh status; supervisor `service` events (supervisor_link.py:161, kind falls back to `service`) only cause a fresh `status` frame | ns_dashboard/ui/src/lib/app.tsx:84 (`m.kind === "status"` -> `publish(m.payload)`; other kinds ignored; polling at app.tsx fallback when EventSource fails) |
| `job` | events.py:105 (for hub kinds `dashboard_job` and `job`) | hub kind `dashboard_job`: ns_dashboard/jobs.py:418 (JobContext._publish on progress/step), jobs.py:669 (JobWorker._publish), ns_dashboard/svc_jobs.py:230 (job created), ns_dashboard/svc_library.py:538 (series parts queued), ns_dashboard/svc_voices.py:101 (apply_voice job queued); hub kind `job`: ns_dashboard/svc_engine.py:278 (`{"draining": True}` on shutdown) and supervisor `job` events relayed at ns_dashboard/supervisor_link.py:161 (`EventRelay.relay_once`, reading supervisor `GET /events` via supervisor_link.py:114) | no UI consumer: app.tsx:84 only handles `status`; `job` frames are dropped (ui/src/lib/types.ts:12 comment mentions them but nothing reads them) |

Browser-facing SSE endpoint 2: `POST /api/assistant/chat` (server.py:635-653, frames via `sse_frame(e["kind"], e["payload"])` at server.py:647; generator `Assistant.chat` in ns_dashboard/assistant/loop.py). Consumed by `api.chat` (ui/src/lib/api.ts:115-130) and handled in ns_dashboard/ui/src/pages/Assistant.tsx:60-68. Kinds and publish lines (all in ns_dashboard/assistant/loop.py):

| chat frame kind | emitted at | handled in UI |
|---|---|---|
| `conversation` | loop.py:167 | Assistant.tsx:61 |
| `message` | loop.py:225, :228 (partial), :233 | Assistant.tsx:62 |
| `confirm` | loop.py:268 | Assistant.tsx:63 |
| `tool_call` | loop.py:270 | Assistant.tsx:64 |
| `tool_result` | loop.py:250, :254, :261, :278 | not handled (ignored by the if-chain, Assistant.tsx:60-67) |
| `busy` | loop.py:176 | Assistant.tsx:65 |
| `error` | loop.py:159, :181, :185, :191 | Assistant.tsx:66 |
| `done` | loop.py:160, :177, :182, :186, :194 | not handled (stream end) |

Typed in ns_dashboard/ui/src/lib/types.ts:34-41 (`ChatEvent`); demo layer ns_dashboard/ui/src/lib/demo.ts:282 mimics the stream.

### 3c. TTS server (`ns_tts/server.py`, 127.0.0.1:8091 by default)

| method | path | handler file:line | called by |
|---|---|---|---|
| GET | `/health` (returns `engine_kind`) | `ns_tts/server.py:199` | supervisor health poll (`ns_supervisor/services.py:30`); `ns_common/tts_client.py:168`; `ns_supervisor/verify.py:200` |
| GET | `/v1/voices` | `ns_tts/server.py:209` | `ns_common/tts_client.py:160` (dashboard `RealTtsAdapter.voices` `adapters.py:673`) |
| POST | `/v1/audio/speech` (wav only) | `ns_tts/server.py:216` | `ns_common/tts_client.py:107` |
| POST | `/v1/audio/speech/batch` | `ns_tts/server.py:241` | `ns_common/tts_client.py:137` (studio narration `ns_studio/media/voice.py`) |
| POST | `/v1/audio/speech/cancel` | `ns_tts/server.py:302` | `ns_common/tts_client.py:153` |
| POST | `/v1/voices/design` | `ns_tts/server.py:311` | `ns_studio/voice_setup.py:145`; `ns_dashboard/adapters.py:768` |
| POST | `/v1/voices/clone` | `ns_tts/server.py:329` | `ns_studio/voice_setup.py:183`; `ns_dashboard/adapters.py:820` |
| GET | `/v1/voices/{name}/embedding` | `ns_tts/server.py:358` | no caller found in product code (tests only: `tests/test_tts_server.py:186,235,242,243`) |

llama-server (`/v1/chat/completions`, `/health`) and the embed server (`/v1/embeddings`, `/health`) are third-party endpoints; callers: `ns_common/llm_client.py:52,93`, `ns_common/embed_client.py:115,124`; stand-ins in dev mode: `ns_supervisor/devfake.py:295` (`/v1/embeddings`).


### 3d. Setup web server (`installer/stage2/web/server.py`, ThreadingHTTPServer, random port; Host must equal `127.0.0.1:<port>` `server.py:101`; cookie `ns_setup` or header `X-NS-Token` `server.py:104-118`; CSP `server.py:26`; JSON body limit 64 KiB `server.py:24`)

| method | path | handler file:line | called by |
|---|---|---|---|
| GET | `/?t=<token>` (sets HttpOnly cookie, 302 to `/`) | `server.py:74-76,171-180` | the window URL from `SetupServer.url` (`server.py:354`), opened by `launch.py:150,170,185`, address file `setup-address.txt` |
| POST | `/api/internal/open-window` (token header only) | `server.py:77,182-190` | `installer/stage1.ps1:1042` |
| GET | `/api/ping` | `server.py:255` | `installer/stage1.ps1:1023` (`Invoke-ServerPing`) |
| GET | `/api/state?since=<n>` (long poll 15 s `server.py:333`) | `server.py:257` -> `controller.wait_state` | `installer/ui/src/api/client.ts:66`, `useSetup.tsx` loop |
| POST | `/api/ui-step` (`uiStep` in `UI_STEPS`: welcome, checks, choices `model.py:37`) | `server.py:260` -> `controller.set_ui_step` | `installer/ui/src/api/client.ts:67` |
| POST | `/api/answers` (draft, or `{path}` to load answers.json) | `server.py:269` -> `controller.submit_answers` | `client.ts:68,69` |
| POST | `/api/check-key` | `server.py:273` -> `controller.check_key` | `client.ts:70` |
| GET | `/api/model-check?repo=&file=` | `server.py:278` -> `controller.check_model` | `client.ts:73` |
| GET | `/api/disk?path=&tier=&source=` | `server.py:280` -> `controller.disk` | `client.ts:71` |
| POST | `/api/pick-folder` | `server.py:283` -> `controller.pick_folder` | `client.ts:72` |
| POST | `/api/action/<name>` (ACTIONS list `model.py:38-41`: start, pause, resume, retry, skip, cancel, recheck, open-dashboard, diagnostics, reveal, elevate-longpaths, close-setup, copy-status, open-windows-page; handlers `controller.py:846-1021` `_act_*`) | `server.py:287-295` | `client.ts:74`; per action: start `Choices.tsx:186`; pause/resume `Download.tsx:34`; retry `Failed.tsx:43`; skip `Failed.tsx:42`; cancel `App.tsx:169`; recheck `Checks.tsx:56`; open-dashboard `Done.tsx:22`; diagnostics `Done.tsx:93`, `Failed.tsx:29`; reveal `Done.tsx:72,86`; elevate-longpaths `Checks.tsx:55`; close-setup `App.tsx:91,95`; copy-status `parts/CopyStatus.tsx:11`; open-windows-page `Checks.tsx:54` (WINDOWS_PAGES power, protection-history `model.py:43`) |
| GET | `/api/file/<name>` (FILE_NAMES `model.py:44`: voice-sample.wav, render-test.mp4, render-test.jpg, verify-report.json) | `server.py:296-301` -> `controller.file_path` (`controller.py:650`) | `client.ts:86` (verify-report.json via `useCopyReport.tsx:15`); the other three are reached by URL strings placed in state (`controller.py:1711-1716`) and played by `screens/Verify.tsx:32,63,64`; `api.fileUrl` (`client.ts:84`) itself has no caller outside the demo |
| GET | `/api/uninstall` (view; 404 when not in uninstall mode) | `server.py:302-304` -> `UninstallApp.view` (`uninstall.py:237`) | `client.ts:77` (`useSetup.tsx:66`) |
| POST | `/api/uninstall` (`deleteModels`, `deleteData`) | `server.py:305-310` -> `UninstallApp.run` (`uninstall.py:251`) | `client.ts:83` |
| POST | `/api/action/<name>` in uninstall mode (names `close-setup`, `reveal`) | `server.py:311-315` -> `UninstallApp.action` (`uninstall.py:335-342`) | `client.ts:74` |
| GET | any other path -> static file from `installer/ui/static` (`index.html`, `app.js`, assets), Range supported | `server.py:90,193-249` | the window |
| any | unknown `/api/*` -> 404 JSON | `server.py:316-317` | - |

## 4. Environment variables


"Set by" = the product writes/exports it (process env, child env or the user registry). "external/OS" = only read. Last column flags set-but-never-read / read-but-never-set.

| # | Name | Set by | Read by | Purpose | Flag |
|---|------|--------|---------|---------|------|
| 1 | NIGHTSHIFT_HOME | User registry: installer/stage2/web/bootstrap.py:110-118 (`set_user_variable`, HKCU\Environment) and :700-702 (also `os.environ`); installer/stage2/winenv.py:33 (`SetEnvironmentVariable ... 'User'`) called from installer/stage2/steps/s01_questions.py:290. Process env: launchers/_common.ps1:24; ns_supervisor/cli.py:264; ns_dashboard/studio_runner.py:140; ns_supervisor/services.py:61; installer/stage2/context.py:157; installer/stage2/steps/v01_samples.py:70,89; installer/stage2/web/bootstrap.py:671; installer/stage2/web/controller.py:973,1114,1877; dev/gate_m3_dashboard.py:138. Scheduled-task action `set "NIGHTSHIFT_HOME=..."`: ns_supervisor/schedule.py:60. Removed by installer/uninstall.ps1:254 | ns_common/paths.py:28 (`Paths.from_env`, ~30 callers, e.g. ns_supervisor/cli.py:63, ns_dashboard/handlers.py:75, ns_studio/jobs.py:144, ns_tts/server.py:387); ns_common/ffmpeg.py:16; installer/stage1.ps1:826,828; installer/uninstall.ps1:100,101; launchers/_common.ps1:21; launchers/nightshift-host.ps1:47; dev/run_services_dev.ps1:4; installer/stage2/web/controller.py:441; installer/stage2/web/launch.py:104; dev/gate_m3_dashboard.py:134 | Install folder. Default when unset: `%LOCALAPPDATA%\Nightshift` (ns_common/paths.py:31) | set and read |
| 2 | NIGHTSHIFT_DEV_MODE | installer/stage1.ps1:1156 (sets `1` after reading `-Dev`/env) | installer/stage1.ps1:1155; installer/stage2/devmode.py:25,39 (`env_enabled`, called from installer/stage2/web/controller.py:389, installer/stage2/web/__main__.py:201, installer/stage2/wizard.py:319) | Dev install switch (stub models, fake engines); accepts 1/true/yes | only stage1 itself re-sets it; otherwise user-supplied |
| 3 | NIGHTSHIFT_BOOTSTRAP_UV | installer/stage1.ps1:1258 | ns_common/uv.py:20,33 (`bootstrap_uv`) | Path of the uv.exe stage 1 downloaded, handed to the setup server | cleared again by launchers/_common.ps1:159 and installer/stage2/web/controller.py:178 (`STAGE1_ONLY_ENV`, value None) |
| 4 | NIGHTSHIFT_UNDER_ENGINE | ns_supervisor/procs.py:215 (`"1"` for every service child) | ns_supervisor/update_job.py:65 (`under_engine`) | Marks a process that runs below the engine (engine shutdown would kill it) | removed for children by ns_supervisor/client.py:138 and ns_supervisor/relaunch.py:31 |
| 5 | NS_NONINTERACTIVE | installer/stage2/web/uninstall.py:267 (`"1"`) | installer/stage1.ps1:1159; installer/uninstall.ps1:42 | Headless mode, no prompts | for stage1: read but never set by the product (user/CI sets it); the product sets it only for uninstall.ps1 |
| 6 | NS_DASHBOARD_NO_HOST | installer/stage2/web/controller.py:973 (`"1"`) | launchers/nightshift-dashboard.ps1:51 | Skip the WebView2 host window (setup window already shows it) | scrubbed from relaunch children at ns_supervisor/relaunch.py:31 |
| 7 | NS_DEVTOOLS | external/OS | launchers/nightshift-host.ps1:438 (`AreDevToolsEnabled`) | WebView2 dev tools on when `1` | read but never set |
| 8 | NS_FFMPEG | external/OS | ns_common/ffmpeg.py:22,34,38 (error text at :40 tells the user to set it) | Override path of ffmpeg.exe | read but never set |
| 9 | NS_FFPROBE | external/OS | ns_common/ffmpeg.py:22,34,45 (error text :47) | Override path of ffprobe.exe | read but never set |
| 10 | NS_LOG_STDERR | external/OS | ns_common/logging.py:29 | Echo log lines to stderr when `1` | read but never set |
| 11 | NS_SUPERVISOR_URL | external/OS | ns_dashboard/supervisor_link.py:30 | Override supervisor base URL (else runtime.json port, else 8099) | read but never set; the PowerShell launchers ignore it (launchers/_common.ps1:53-55 always builds `http://127.0.0.1:<port>`) |
| 12 | NS_TTS_FAKE | external/OS | ns_tts/engine.py:865; ns_tts/server.py:388 | Force the fake TTS engine when `1` | read but never set (dev installs pass `--engine fake`, ns_supervisor/services.py:142) |
| 13 | NS_JOB_CANCEL_FILE | ns_dashboard/studio_runner.py:147 (value = flag-file path; constant `CANCEL_ENV` at ns_dashboard/studio_job.py:39) | ns_dashboard/studio_job.py:48 | Job child polls this file to learn about Cancel | set and read |
| 14 | NS_TOAST_TITLE | ns_supervisor/notify.py:48 (via `env_extra`, ns_supervisor/pwsh.py:32) | ns_supervisor/notify.py:36 (inside the PowerShell script text) | Toast title for PowerShell | set and read |
| 15 | NS_TOAST_MESSAGE | ns_supervisor/notify.py:48 | ns_supervisor/notify.py:37 | Toast body | set and read |
| 16 | NS_CIM_CMD | ns_supervisor/update_job.py:201 | ns_supervisor/update_job.py:195 (PowerShell `$env:NS_CIM_CMD`) | Command line of the detached update worker started through CIM | set and read |
| 17 | NS_CIM_CWD | ns_supervisor/update_job.py:201 | ns_supervisor/update_job.py:195 | Working directory of that worker | set and read |
| 18 | NS_NETWORK_TESTS | .github/workflows/ci.yml:53 (CI only) | tests/test_downloader_network.py:26 (tests only); mentioned in pyproject.toml:67 | Enables real-network tests | no product-code reader |
| 19 | NS_EXIT | cmd-local variable: INSTALL.cmd:5, UNINSTALL.cmd:9 | INSTALL.cmd:7,9; UNINSTALL.cmd:12 | Holds %ERRORLEVEL% inside the .cmd (not a cross-process variable) | set and read in same file |
| 20 | PATH / Path | Session PATH rebuilt: installer/stage1.ps1:117 (`$env:Path`); installer/stage2/web/bootstrap.py:107 (machine+user from registry); launchers/nightshift-host.ps1:235 (prepends WebView2 dir); ns_common/gpu_dlls.py:40 and ns_studio/media/transcribe.py:211 (prepend cuBLAS/cuDNN dirs). User PATH registry entry appended by installer/stage2/web/bootstrap.py:135-160 (FFmpeg fallback folder) | ns_common/gpu_dlls.py:26; ns_studio/media/transcribe.py:199; installer/stage1.ps1:115-116; installer/stage2/web/bootstrap.py:97-107; implicit via `shutil.which` (ns_common/ffmpeg.py:22, ns_supervisor/update.py:150,476,511) | DLL search and tool lookup | set and read |
| 21 | LOCALAPPDATA | external/OS | ns_common/paths.py:31; ns_common/uv.py:34; ns_supervisor/doctor.py:99; installer/stage2/diagzip.py:73; installer/stage2/web/launch.py:33,81; installer/stage2/web/uninstall.py:168; installer/stage1.ps1:137; installer/uninstall.ps1:102,113,242; launchers/_common.ps1:26; launchers/nightshift-host.ps1:48; launchers/nightshift-dashboard.ps1:72; dev/run_services_dev.ps1:4 | Default home, bootstrap uv, setup-server files, Chrome path | read but never set |
| 22 | USERPROFILE | external/OS | installer/stage2/web/controller.py:133 (Desktop); installer/stage2/web/pathcheck.py:91 (Downloads check); launchers/nightshift-code.ps1:24 (Projects folder) | Desktop, Downloads, Projects locations | read but never set |
| 23 | USERNAME | external/OS | installer/stage2/web/controller.py (`_summary`, the admin-run warning text) | admin-run warning text only: `ns_common/secret_file.py` `restrict_to_user` no longer reads it, it grants the token's SID (`current_user_sid`) | read but never set |
| 24 | TEMP | external/OS | installer/stage1.ps1:862 (uv zip); installer/stage2/web/uninstall.py:153 (PS `$env:TEMP`); UNINSTALL.cmd:7 (`cd /d "%TEMP%"`); implicit via `tempfile.gettempdir()` at ns_supervisor/doctor.py:187, ns_supervisor/update.py:234, installer/stage2/steps/s01_questions.py:89, installer/stage2/web/uninstall.py:93, installer/stage2/wizard.py:304 | Scratch files. The dashboard overrides Python's tempdir with `<home>\tmp` instead (ns_dashboard/server.py:696-709) | read but never set |
| 25 | SystemRoot | external/OS | ns_common/vcruntime.py:22; installer/stage1.ps1:274 | Locate System32 for the VC++ DLL check | read but never set |
| 26 | windir | external/OS | ns_common/vcruntime.py:22 (fallback for SystemRoot) | same | read but never set |
| 27 | SystemDrive | external/OS | installer/stage2/steps/s04_venvs.py:50 | Free-space check of the system drive when the uv cache cannot go to the install drive | read but never set |
| 28 | ProgramFiles | external/OS | installer/stage2/web/launch.py:80; installer/stage1.ps1:274; launchers/nightshift-dashboard.ps1:72 | Edge/Chrome lookup | read but never set |
| 29 | ProgramFiles(x86) | external/OS | installer/stage2/web/launch.py:79; launchers/nightshift-dashboard.ps1:72 | Edge lookup | read but never set |
| 30 | OneDrive | external/OS | installer/stage2/web/pathcheck.py:98-100 | Refuse installs inside synced folders | read but never set |
| 31 | OneDriveConsumer | external/OS | installer/stage2/web/pathcheck.py:98-100 | same | read but never set |
| 32 | OneDriveCommercial | external/OS | installer/stage2/web/pathcheck.py:98-100 | same | read but never set |
| 33 | PROCESSOR_ARCHITECTURE | external/OS | installer/stage1.ps1:431 | CPU architecture check (NS-E101) | read but never set |
| 34 | PROCESSOR_ARCHITEW6432 | external/OS | installer/stage1.ps1:430 | Real arch under WOW64 | read but never set |
| 35 | PYTHONUTF8 | installer/stage1.ps1:1319; installer/uninstall.ps1:121; installer/stage2/web/engine.py:46; installer/stage2/context.py:157; installer/stage2/steps/v01_samples.py:70,89; installer/stage2/whisper_probe.py:47; installer/stage2/web/controller.py:1115; ns_dashboard/studio_runner.py:141 | the Python interpreter only | UTF-8 mode for child Pythons | set, consumed by interpreter only |
| 36 | PYTHONPATH | installer/stage1.ps1:1320; installer/uninstall.ps1:122; installer/stage2/web/engine.py:47; installer/stage2/web/controller.py:1114 | interpreter; deliberately removed by launchers/_common.ps1:158, installer/stage2/web/controller.py:178 (None) and dropped by ns_dashboard/studio_runner.py:41 (`DROP_ENV`) | Point setup children at the extracted app folder; scrubbed so the engine runs from `<home>\app` | set and cleared |
| 37 | PYTHONIOENCODING | ns_dashboard/studio_runner.py:142 (`utf-8`) | interpreter | UTF-8 stdio for studio job children | set, consumed by interpreter only |
| 38 | PYTHONUNBUFFERED | ns_dashboard/studio_runner.py:143 (`1`) | interpreter | Live NSJOB protocol lines | set, consumed by interpreter only |
| 39 | PYTHONHOME | none | none (only dropped: ns_dashboard/studio_runner.py:41) | Removed from job children | dropped only |
| 40 | PYTHONSTARTUP | none | none (only dropped: studio_runner.py:41) | Removed from job children | dropped only |
| 41 | VIRTUAL_ENV | none | none (only dropped: studio_runner.py:41) | Removed from job children | dropped only |
| 42 | __PYVENV_LAUNCHER__ | none | none (only dropped: studio_runner.py:41) | Removed from job children | dropped only |
| 43 | UV_LINK_MODE | ns_common/uv.py:83 (`copy`, via `uv_env`); installer/stage1.ps1:1262 | uv (external) | Copy rather than hardlink into venvs | consumed by uv only |
| 44 | UV_PYTHON_PREFERENCE | ns_common/uv.py:83 (`only-managed`); installer/stage1.ps1:1263 | uv (external) | Never use a PC-installed Python (MS Store virtualization) | consumed by uv only |
| 45 | UV_CACHE_DIR | ns_common/uv.py:89 (`<home>/cache/uv`) | uv (external); installer/stage2/steps/s04_venvs.py:48 only tests whether the key is in the dict | uv cache on the install drive | consumed by uv only |
| 46 | OPENCODE_CONFIG | installer/stage2/steps/s05_opencode_prewarm.py:23; launchers/nightshift-code.ps1:23 | opencode.exe (external) | Config file with the local provider | consumed by OpenCode only |
| 47 | CUDA_VISIBLE_DEVICES | ns_supervisor/services.py:127 (`-1` for the embed service) | CUDA runtime in the llama-server child (external) | Keep the embedding server on CPU | consumed externally only |
| 48 | GITHUB_HEAD_REF | external/OS (GitHub Actions) | dev/check_scope.py:166 | Branch name for the scope gate | read but never set |
| 49 | GITHUB_REF_NAME | external/OS (GitHub Actions) | dev/check_scope.py:166 | fallback branch name | read but never set |

Searched and not found in product code: HF_*, HUGGINGFACE_*, TORCH*, PYTORCH_*, OMP_*, MKL_*, APPDATA (other than LOCALAPPDATA), COMPUTERNAME, HOME, TMP/TMPDIR, HTTP_PROXY/HTTPS_PROXY/NO_PROXY, COMSPEC, PATHEXT. No `process.env` in ns_dashboard/ui/src or installer/ui/src; only `import.meta.env.DEV` (ns_dashboard/ui/src/main.tsx:19). `NIGHTSHIFT_OWNERS` (ns_dashboard/svc_engine.py:24) and `NS_CODE` (ns_dashboard/assistant/composite.py:26) are Python constants (tuple, regex), not environment variables. CI-only variables in .github/workflows (QWENTTS_REPO, QWENTTS_SHA, CUDA_VERSION, CUDA_ARCHS, GH_TOKEN, PIP_DISABLE_PIP_VERSION_CHECK) are out of scope.

#### Flags summary

- Set but never read by product code (only an external program or the OS consumes them): PYTHONUTF8, PYTHONIOENCODING, PYTHONUNBUFFERED, UV_LINK_MODE, UV_PYTHON_PREFERENCE, UV_CACHE_DIR, OPENCODE_CONFIG, CUDA_VISIBLE_DEVICES. PYTHONPATH is set and then deliberately cleared in several places.
- Read but never set by the product (rely on user, OS or CI): NS_DEVTOOLS, NS_FFMPEG, NS_FFPROBE, NS_LOG_STDERR, NS_SUPERVISOR_URL, NS_TTS_FAKE, NS_NETWORK_TESTS (tests only; set in CI), NS_NONINTERACTIVE (for stage1), NIGHTSHIFT_DEV_MODE (stage1 only re-exports it), plus all OS variables (rows 21-34, 48-49).
- Dropped only, never set or read: PYTHONHOME, PYTHONSTARTUP, VIRTUAL_ENV, __PYVENV_LAUNCHER__.

## 5. Files and folders on disk

`<home>` = `%NIGHTSHIFT_HOME%` else `%LOCALAPPDATA%\Nightshift` (ns_common/paths.py:28-32). Directory attributes of `Paths` are created by `Paths.ensure()` (paths.py:127-129) from `all_dirs()` (paths.py:117-125). Lifetime: permanent = survives restarts and updates until uninstall; per run = recreated each engine/app run; per job = inside a job folder.

### 5a. `Paths` attributes (ns_common/paths.py)

| path | writer | reader | lifetime |
|---|---|---|---|
| `<home>\bin\llama\` (`bin_llama` :36) | installer `downloader.py:725` (llama.cpp zip, `s02_download_bins`) | supervisor `services.py:79,115` | permanent |
| `<home>\bin\opencode\` (`bin_opencode` :38) | installer `downloader.py:732` | `launchers/nightshift-code.ps1:8`; `s05_opencode_prewarm.py:20,54` | permanent (only with Nightshift Code) |
| `<home>\bin\uv\` (`bin_uv` :40) | stage 2 bootstrap copies uv to a literal `home/bin/uv/uv.exe` (`web/bootstrap.py:424,602`) | `bootstrap.py:424`; `installer/uninstall.ps1:114` | permanent; the `Paths.bin_uv` attribute itself is read only by `all_dirs()` (candidate unused) |
| `<home>\bin\qwentts\` (`bin_qwentts` :42) | installer (voice engine zip, `.ns-qwentts.done` marker `s08_smoke.py:110`) | `ns_tts/engine.py:873`; `s08_smoke.py:38,106` | permanent |
| `<home>\bin\yt-dlp\yt-dlp.exe` (literal) | installer `downloader.py:739` | `ns_studio/clipper/source.py:233` | permanent |
| `<home>\bin\ffmpeg\` (literal) | setup bootstrap fallback unpack (`web/bootstrap.py:435,568`, PATH entry added) | `ns_common/ffmpeg.py:15-17`; `installer/uninstall.ps1:185` | permanent |
| `<home>\bin\webview2\` (literal; 3 DLLs) | installer `downloader.py:659-760`, bundle copy `adopt_bundled_webview2` | `launchers/nightshift-host.ps1:72-81` | permanent |
| `<home>\bin\_downloads\` (literal) | installer `downloader.py:709`, `bootstrap.py:440` | same | transient (downloads, vc_redist) |
| `<home>\models\llm\` (`models_llm` :44) | installer `downloader.py:812`; `ns_supervisor/models_get.py:94` | `runtime.llama.model_path` consumers (`services.py:76`) | permanent |
| `<home>\models\embed\` (`models_embed` :46) | installer `downloader.py:813`; `runtime_cfg.py:37` | `services.py:116` | permanent |
| `<home>\models\tts\` (`models_tts` :48) and `\voices\` (:50) and `\hf\` (`TTS_HF_SUBDIR` :10, `models_tts_hf` :52) | installer `downloader.py`, `fallback.py:20`; `models_get.py:61-90`; voices written by `ns_tts/voices.py` (design/clone) | `ns_tts/engine.py:857`; dashboard voices `adapters.py:646,761,834` | permanent |
| `<home>\models\whisper\` (`models_whisper` :54) | installer s03 | `ns_studio/media/transcribe.py:231`; `ns_supervisor/verify.py:123` | permanent |
| `<home>\models\spacy\` (`models_spacy` :56) | installer (spaCy model) | `ns_studio/policy/text_checks.py:46` | permanent |
| `<home>\models\cv\` (`models_cv` :58) | created by `ensure()` only | no reader found (candidate unused) | permanent, empty |
| `<home>\venvs\{supervisor,studio,tts}\` (`venvs` :60) | installer `s04_venvs.py`, `fallback.py` (tts); update `update.py:418-435` | everything via `venv_python`/`studio_exe` (`services.py:54`, `schedule.py:36`, `studio_runner.py:49-52`) | permanent |
| `<home>\config\` (`config` :62) | many (see 5b) | many | permanent |
| `<home>\config\channels\<slug>.yaml` (`channel_file` :108) | installer `s07_configs.py:150` (`save_channel`); dashboard channel CRUD (`ns_studio/channels/crud.py`) | `ns_common/config.py:39-59`; studio, dashboard | permanent |
| `<home>\data\` (`data` :66) | - | - | permanent |
| `<home>\data\studio.db` (`db_path` :102) | studio (`ns_studio/jobs.py`), dashboard (`ns_dashboard/adapters.py:480`) | `ns_supervisor/scheduled.py:25` (read-only), `backup.py:59` | permanent (see section 6) |
| `<home>\data\assistant.db` (`assistant_db` :104) | `ns_dashboard/assistant/store.py:33` | same | permanent |
| `<home>\data\backup\` (`backup_dir` :68) | `ns_studio/daily.py:202` (`env.db.backup`), `ns_supervisor/backup.py:86` (pre-restore) | restore | permanent |
| `<home>\data\jobs\<job_id>\` (`job_dir` :111) | `ns_studio/jobs.py:228,244` and every stage (see 5c) | studio resume, dashboard, `doctor.py:91-93` | per job (kept) |
| `<home>\data\calibration\<slug>\` (`calibration_dir` :70) | `ns_studio/calibrate/run.py:194,325` | `ns studio calibrate` | permanent |
| `<home>\cache\clips\<source>\<clip_id>.mp4` (`cache_clips` :74) | `ns_studio/media/clips.py:87-109` (90-day refresh) | same | cache |
| `<home>\cache\clips\index.sqlite` (`clips_index` :106) | no writer found | no reader found; the `Paths.clips_index` property is never used (candidate; clips are indexed in `studio.db` table `clips`, `clips.py:114`) | - |
| `<home>\cache\search\{pixabay,pexels}\` (`cache_search` :76) | `ns_studio/media/clip_sources.py:346`; `ns_dashboard/svc_library.py:68-70` | same (24 h response cache `clip_sources.py:349`) | cache |
| `<home>\cache\tts-tmp\` (`cache_tts_tmp` :78) | `ns_tts/engine.py:268,878` | same | per batch |
| `<home>\cache\dashboard\thumbs\`, `voice\<key>.wav`, `voice-preview.wav` (literal) | `ns_dashboard/media.py:102`; `svc_settings.py:237`; `adapters.py:693` | dashboard | cache |
| `<home>\cache\uv\` | uv cache via `ns_common/uv.py` `uv_env` | uv | cache |
| `<home>\imports\analytics\` (`imports_analytics` :80) | `ns_dashboard/svc_jobs.py:365` (CSV upload) | `ns_studio/learn/importer.py` | permanent |
| `<home>\imports\clipper\<source_id>\` and `_downloads\` (literal) | `ns_dashboard/uploads.py:56`; `studio_bridge.py:157`; `source.py:182` writes `source.json` | `svc_outputs.py:181-182` (`transcript.json`), clipper stages | per source |
| `<home>\imports\restore\<stamp>-<name>` (literal) | `ns_dashboard/svc_settings.py:390` | `ns restore` | transient |
| `<home>\outbox\<slug>\...` (`outbox` :82) | `ns_studio/media/outbox.py` (`outbox_target`, files named in 5c) | owner; `launchers/open-outbox.ps1:7`; dashboard Results `svc_outputs.py` | permanent |
| `<home>\rejected\<slug>\<date>\<job_id>\` (`rejected_dir` :114) | `ns_studio/policy/regen.py:118` (`reason.json`), `outbox.py:123` | owner | permanent |
| `<home>\stories\` (`stories` :86) | only created by `ensure()`; outbox writes `outbox\<slug>\stories\...` instead (`outbox.py:114`) | no reader found (candidate unused) | permanent, empty |
| `<home>\exemplars\*\meta.yaml` (`exemplars` :88) | the owner | `ns_studio/text/exemplars.py:56-62` | permanent (not created by `ensure()`) |
| `<home>\prompt_overrides\<name>.md` (`prompt_overrides` :90) | the owner | `ns_studio/text/prompts.py:222` | permanent (not created by `ensure()`) |
| `<home>\logs\` (`logs` :92) | `NsLogger` everywhere (ns_common/logging.py:34): `supervisor-`, `llama-`, `embed-`, `tts-`, `dashboard-`, `studio-`, `installer-`, `update-`, `verify-`, `tune-` `YYYYMMDD.jsonl` | `ns doctor --bundle` (`doctor.py`), dashboard logs view (`ns_dashboard/logs_tail.py`) | permanent (no rotation found) |
| `<home>\logs\verify-report.json` | `ns_supervisor/verify.py:503-504` | installer `controller.py` (file route), `doctor.py:179`; deleted by `s08_smoke.py:92` | per verify |
| `<home>\logs\install-report.json` | `installer/stage2/diagzip.py:65`; `web/controller.py:1047` | `doctor.py:119-121` | per install |
| `<home>\logs\update-<timestamp>.log` | `ns_supervisor/update_job.py:174-177` | `launchers/update.ps1:44` | per update |

### 5b. `<home>\config\` and `<home>\run\` and other home-level files (literals)

| path | writer | reader | lifetime |
|---|---|---|---|
| `config\runtime.json` | installer `runtime_cfg.py:70`; `ns tune` `tune.py:194`; `models_get.py:156`; dashboard Settings `svc_settings.py:148` | `ns_common/config.py:29-32` (all components); PowerShell `launchers/_common.ps1:45-48`; `ns_common/devmode.py:26` | permanent |
| `config\install_state.json` | `installer/stage2/state.py:41` | `ns_common/config.py:62`; `control.py:22` (app path); dashboard `svc_engine.py` install summary | permanent |
| `config\wizard_flags.json` | `installer/stage2/state.py:75` (every flag is stamped with the install it describes, `_install` = `install_state.started_at`) | `doctor.py:124`; `web/controller.py:1055,1068`; `verify_downloads.py` | one install: removed by `installer/uninstall.ps1` with `install_state.json`; a stamp from another install reads as unset |
| `config\opencode.json` | `s07_configs.py:157` | `launchers/nightshift-code.ps1:23`; `s05_opencode_prewarm.py:23` | permanent |
| `config\schedule.json` | `ns_supervisor/schedule_cfg.py:43-53` | `schedule_cfg.py:27`, `core.py` (run_due_soon), `doctor.py:180`, dashboard | permanent while task registered |
| `config\supervisor.json` (`health_timeout_s`) | installer `s08_smoke.py:94` via `ns_supervisor/health_cfg.py:38-42` | `ns_supervisor/app.py:242` (`read_health_timeout`) | permanent |
| `config\update_check.json` | `ns_supervisor/update_check.py:31-32` | same | permanent |
| `config\ui_prefs.json` | `s07_configs.py:59-69` | `ns_dashboard/state.py:168-169` | permanent |
| `config\dashboard.json` | dashboard settings (`ns_dashboard/state.py:90-91`); `s09_shortcuts_scheduler.py:83-91` | `ns_common/config.py:96-102` (notifications), dashboard | permanent |
| `config\pending_voice.json` | `s07_configs.py:99-101` | `studio_bridge.py:492`, `adapters.py:126,830`; deleted when voice made | until applied |
| `config\window.json` | `launchers/nightshift-host.ps1:248-278` | same | permanent |
| `config\tts_speed.json` | `ns_common/tts_timeout.py:33,63` | same | permanent |
| `config\secrets.dpapi` | `ns_common/secret_file.py:26-35,95` (DPAPI blob per secret) | `ns_studio/jobs.py:85-115`; `ns_common/secret_file.py:90` | permanent; removed by `installer/uninstall.ps1:216-219` |
| `config\answers.json` | `installer/stage2/web/bootstrap.py:592,695`; keys blanked by `s07_configs.py:163` | `installer/stage2/*`, `doctor.py:127` (scrubbed copy) | permanent |
| `config\answers.kept.json` | `web/answers_view.py` `keep_earlier_answers` (the window copies the old `answers.json` here before it writes its choices over it) | `installer/stage2/reinstall.py` (`kept_answers`, `unfinished_choices`, `changes`); deleted by `forget_kept` once s01 applied the choices (or found nothing to apply) | between the window's write and the end of s01; it is the baseline for whoever runs setup next, because a window that stopped early leaves choices in `answers.json` that were never applied. The reinstall rule (D-14): the new answers win for what they name (a tier without a custom model drops the earlier custom model; an API key is stored in the key store, then removed from the file); a changed tier or custom model, the engine-test answers and a `runtime.json` of another kind are recorded as owed in the flag `runtime_pending` and applied by s06 once s03 verified the new model (`apply_pending_runtime`), so the nightly run keeps the model it has until then; `fake_voice` changing forgets the s02 flags `qwentts` and `tts_fallback`; the channel yaml and the voice change field by field only where the answers changed; every kept file is named on screen |
| `<app>\answers.json` (next to INSTALL.cmd; carried over) | the owner | `s07_configs.py:107`; carried by `appcopy.py:33`, `update.py:31` | until blanked |
| `run\supervisor.json` | `ns_supervisor/core.py:166-184` | `control.py:83-105` (`ns stop` fallback) | per engine run |
| `run\supervisor.lock` | `ns_supervisor/app.py:196-198` (OS file lock via `ns_common/singleton.py:63-93`) | second engine start | per engine run |
| `run\apphost-dashboard.json`, `run\apphost-setup.json` `{hwnd,pid,ready}` | `launchers/nightshift-host.ps1:130,198-209` | `ns_supervisor/presence.py:27-33`; `launchers/_common.ps1:121-141,170`; `launch.py:113-115`; `uninstall.py:176` | per window |
| `run\update_state.json` | `ns_supervisor/update_job.py:68-110` | `launchers/update.ps1:37`; dashboard `updater.py:229-235`, `svc_library.py:466` | per update |
| `run\updater\venv-<stamp>\` | `ns_supervisor/updater_copy.py:32-60` | update worker | per update (deleted after) |
| `run\morning_note.json` | `ns_supervisor/notify.py:79-93` | dashboard `svc_engine.py:241,256` (displays then deletes) | until shown |
| `data\owners\<id>.lock` (OS lock via `ns_common/singleton.py`) and `data\owners\<id>.json` (kind, origin, pid, state, jobs of a run) for every nightly run and story job | `ns_studio/ownership.py` (`announce_job` from `jobs.make_job`; `hold` from `daily.py`, `policy/regen.py` `run_job_guarded`) | `ns_studio/ownership.py` (`liveness`, `recover`), `channels/cadence.py` (the planner counts live work only), dashboard `svc_engine.py` (`last_run.state`), `JobWorker.start` (`recover_studio`) | the lock while the process lives (the OS drops it on a kill); the json 30 days after a clean end |
| `state\webview2\` (WebView2 user-data folder) | WebView2 (`nightshift-host.ps1:391`) | WebView2 | permanent; removed by `uninstall.ps1:245` |
| `tmp\` (`studio-<kind>-*.json`, `*.cancel`, `cancel-download-<id>`, uploads `upload.json`) | `ns_dashboard/uploads.py:49-51`, `studio_runner.py:154-160`, `adapters.py:403`; `server.py:700-708` clears it at dashboard start | the same | per run (wiped on dashboard start) |
| `.ns-app.done` (inside `<home>\app\`) | `installer/stage2/web/appcopy.py:32`; `ns_supervisor/update.py:45` | `appcopy.py`, `update.py` (app copy complete marker) | permanent |
| `.ns-setup-env.done` (inside `venvs\studio\`) | `installer/stage2/web/bootstrap.py:450` | `bootstrap.py` | permanent |
| `.ns-qwentts.done` (inside `bin\qwentts\`) | installer download | `s08_smoke.py:110` | permanent |
| `<model file>.dev-stub` marker next to 4 KB stub model files (dev install) | `installer/stage2/devmode.py:30` (`STUB_SUFFIX`), written by s03 in dev mode | `installer/stage2/verify_downloads.py` ("stub (dev)"); real installs never accept it (CHANGELOG.md:156) | permanent until a real install |
| `<home>\app\` (`app.new`, `app.prev`, `app.old`) | `installer/stage2/web/appcopy.py`, `ns_supervisor/update.py:287,297-308,346` | launchers (`Get-NsAppPath`), shortcuts | permanent / transient during update |
| `<home>\app.prev-config.zip` (the settings files `config\` held before the update, paired with `app.prev` by a fingerprint) | `ns_common/config_snapshot.py`, written by `update.update_app` / `rollback_app`, `appcopy.install_app`, and by the first start after an update an older updater made (`ns_common/first_start.py`, called by every settings writer) | `update.rollback_app` (`ns update --rollback`); removed by the uninstallers | until the next update (one pair only) |
| `<home>\app\.ns-first-start.json` (the first start of this program could not save the settings copy; so no later start takes a copy of settings it already changed); `<home>\config\.ns-first-start.json` when the program folder cannot be written | `ns_common/first_start.py` (`write_state`) | `first_start.read_state` | until the program folder is replaced |
| `<home>\app.prev-config.zip.lock` (held while one process looks at or writes the settings copy: a first start, a rollback) | `ns_common/config_snapshot.py` (`locked`) | `first_start`, `update.rollback_app` | permanent (the OS drops the lock with the process); removed by the uninstallers |
| `<home>\app.rollback-pending.json` (a rollback in progress: which programs, the settings from before it, what is left after the swap) | `ns_common/rollback_intent.py`, written first by `update.rollback_app` | `rollback_intent.recover` from `first_start` (every settings writer, the engine start), `ns update`, `ns update --rollback` | until the rollback ends, or the next start finishes or undoes it |
| `backup zips`: `nightshift-backup-<date>.zip` with `backup-manifest.json` | `ns_supervisor/backup.py:18,51-62` (Desktop default `cli.py:322`) | `ns restore`, dashboard import `svc_settings.py:390` | owner-kept |
| diagnostics zip with `bundle-info.json`, `nvidia-smi.txt` | `ns_supervisor/doctor.py:203,...` (Desktop) | the owner / support | owner-kept |

### 5c. `%LOCALAPPDATA%\Nightshift\` (setup program data, independent of a custom home) and temp files

| path | writer | reader | lifetime |
|---|---|---|---|
| `setup-server.json` (`pid, port, token, started, host, host_text`) | `installer/stage2/web/launch.py:25,38-52,205` | `installer/stage1.ps1` (`Read-ServerInfo` :1023,1042) | per setup run (deleted on clean exit) |
| `setup-address.txt` | `launch.py:26,38-52` | `stage1.ps1:1379-1381`; `doctor.py` | permanent |
| `setup-server.err` | `stage1.ps1:1339` redirect | `stage1.ps1:1354-1370`; `doctor.py:138` | per setup run |
| `bootstrap\setup.lock` | `web/controller.py:1822` (`FileLock`) | second setup | per setup run |
| `bootstrap\setup-ui.jsonl` | `web/controller.py:385` | support | permanent |
| `bootstrap\preflight.json` | `ns_supervisor/doctor.py:132` reads it; stage1 hand-off | `doctor.py:132` | per setup |
| `browser\` (Edge/Chrome profile for the setup window) | browser (`launch.py:164`) | browser | permanent |
| `%TEMP%\nightshift-preflight.json` | `stage1.ps1:1301-1309` | `installer/stage2/wizard.py:304`; `doctor.py:132` | per setup |
| `%TEMP%\nightshift-uv.zip`, `%TEMP%\nightshift-vc_redist.x64.exe` | `stage1.ps1:862,917` | stage1 | transient |
| `%USERPROFILE%\Projects\` | `launchers/nightshift-code.ps1:24-25` (OpenCode cwd) | OpenCode | permanent |
| Desktop `Nightshift*.lnk` (7) | `installer/stage2/steps/s09_shortcuts_scheduler.py:128-136` | user | removed by `uninstall.ps1:186-191` |
| `Nightshift-uninstall-leftovers.txt` | `installer/stage2/web/uninstall.py:34` | user | permanent |

### 5d. Files inside a job folder `data\jobs\<job_id>\` (writer / reader examples; all JSON unless noted)

| file | writer | reader |
|---|---|---|
| `job.json`, `stage_state.json` | `ns_studio/jobs.py:228,244`; `ns_studio/stages.py:15,33` | resume `jobs.load_job`; dashboard `channel_stats.py:88` |
| `idea.json`, `ideas_all.json`, `hooks_all.json` | `ns_studio/text/stages_shorts.py:112-132,270-280`; `policy/regen.py:47,71` | later stages, regen policy |
| `story_draft.json`, `story_revised.json`, `critic.json`, `continuity.json`, `story.json`, `plan.json`, `plan_meta.json` | `ns_studio/text/stages_master.py:123-365`; series `series/stages.py:38-81` | media stages `captions.py:692`, `clips.py:385`, dashboard |
| `parts.json`, `visual_plan.json`, `shorts_plan.json`, `short_combos.json`, `shorts_timing.json`, `duration_check.json` | `media/voice.py:135`, `media/captions.py:716-719`, `media/master.py:114-256`, `media/derive.py:158-164` | render, outbox `outbox.py:350` |
| `seo.json`, `seo_long.json`, `seo_shorts.json`, `seo_clips.json` | `text/stages_*`, `clipper/stages_clip.py:660-815` | `media/outbox.py:64,350-483`, `policy/gate.py:67` |
| `policy_text.json`, `policy_final.json`, `policy_notes.json`, `policy_attempts.json`, `reason.json` | `policy/final_checks.py:269`, `policy/regen.py:20-118`, `clipper/stages_clip.py:660-826` | `outbox.py:172`; assistant `composite.py:124` |
| clipper: `source.json`, `clip_params.json`, `transcript.json`, `analysis.json`, `clip_candidates.json`, `clip_plan.json`, `clip_layout.json` | `clipper/stages_clip.py:70-482`, `clipper/source.py:182` | later clip stages; `svc_outputs.py:181-182` |
| `series_context.json`, `script_source.json` | `studio/context.py:144`, `orchestrator.py:119`, `series/stages.py` | stages, outbox `outbox.py:103` |
| (not in the job folder) `data\phash_index.jsonl` | `ns_studio/policy/final_checks.py:28,92` | same |

## 6. Database tables


All tables are created by ONE file: contracts/schema.sql, executed by `Db.migrate()` with `conn.executescript` (ns_common/db.py:39-44). There is no other CREATE TABLE in any *.py or *.ps1. studio.db and assistant.db both receive the whole schema.sql (assistant.db gets all 21 tables; the assistant code uses only rows 17-21). `Db.migrate()` callers: ns_studio/jobs.py:147, ns_studio/clipper/stages_clip.py:129, ns_studio/series/workshop.py:1129, ns_dashboard/state.py:79,85, ns_dashboard/adapters.py:481, ns_dashboard/assistant/store.py:36.

| # | db file | table | created at | columns (brief) | main writers | main readers | flag |
|---|---|---|---|---|---|---|---|
| 1 | data/studio.db | channels | schema.sql:4 | slug PK, display_name, created_at, status(active) | ns_studio/channels/crud.py:173 (upsert); ns_studio/series/stages.py:59; ns_studio/variation/draw.py:238; ns_dashboard/svc_channels.py:100; ns_dashboard/svc_library.py:181; ns_dashboard/fakes.py:600 | NONE found (only an FK target of ideas/stories; channel data is read from config/channels/*.yaml) | candidate: write-only |
| 2 | data/studio.db | runs | schema.sql:11 | run_id PK, started_at, finished_at, mode(daily/manual/gate), summary_json | ns_studio/daily.py:161 (insert), :207 (update finished_at/summary_json) | ns_dashboard/svc_engine.py:137,234; ns_dashboard/assistant/composite.py:102 | |
| 3 | data/studio.db | ideas | schema.sql:19 | idea_id PK AUTOINCREMENT, channel FK, job_id, premise, twist, embedding BLOB, created_at, used, status(new/approved/used/rejected), title, source | ns_studio/text/stages_shorts.py:144 (insert), :97 (claim update); ns_dashboard/svc_library.py:185 (insert), :208 (status update) | ns_studio/text/stages_shorts.py:61,125; ns_dashboard/svc_library.py:160,196; ns_dashboard/assistant/composite.py:345; ns_dashboard/assistant/orientation.py:137 | |
| 4 | data/studio.db | stories | schema.sql:33 | job_id PK, channel FK, format, working_title, text, word_count, opening_line, embedding BLOB, critic_avg, revisions, created_at | ns_studio/text/stages_master.py:349 (insert or replace), :793 (update); ns_studio/text/stages_shorts.py:222; ns_studio/series/stages.py:63 | ns_studio/text/stages_master.py:111; ns_studio/text/stages_shorts.py:211; ns_studio/policy/text_checks.py:204; ns_dashboard/svc_outputs.py:175; ns_dashboard/svc_library.py:227; ns_dashboard/assistant/composite.py:334; ns_dashboard/assistant/orientation.py:130 | |
| 5 | data/studio.db | parts | schema.sql:47 | (job_id, n) PK, text, hook, word_count, est_seconds | NONE found (no INSERT/UPDATE/REPLACE into parts in any .py or .ps1) | ns_dashboard/svc_outputs.py:171; ns_dashboard/assistant/composite.py:337 | candidate: read-only |
| 6 | data/studio.db | clips | schema.sql:57 | (source, clip_id) PK, url, license, width, height, duration, local_path, fetched_at, keywords JSON | ns_studio/media/clips.py:116 (insert or replace), :102 (keywords), :134 (local_path NULL on expiry); ns_dashboard/svc_library.py:398 (delete) | ns_studio/media/clips.py:93,128; ns_dashboard/svc_library.py:261,348,393 | |
| 7 | data/studio.db | clip_usage | schema.sql:69 | source, clip_id, channel, job_id, item, used_at; FK to clips | ns_studio/media/outbox.py:311 (delete), :314 (insert); ns_dashboard/svc_library.py:397 (delete) | ns_studio/media/clips.py:147; ns_studio/policy/final_checks.py:189,199; ns_dashboard/svc_library.py:261 | |
| 8 | data/studio.db | combos | schema.sql:81 | combo_id PK, channel, job_id, item, hook, structure, pov_tone, pacing, caption_style, visual_treatment, title_formula, length_bucket, created_at | ns_studio/variation/draw.py:242 (insert or replace) | ns_studio/channels/cadence.py:69,167,185,199; ns_studio/variation/draw.py:101,113,225; ns_studio/learn/reweight.py:27; ns_dashboard/svc_settings.py:318; ns_dashboard/assistant/composite.py:182,200 | |
| 9 | data/studio.db | sources | schema.sql:92 | source_id PK, channel, path, duration_s, sha256, imported_at, template, status, rights_confirmed (also ALTER-added), title (also ALTER-added) | ns_studio/clipper/source.py:185 (insert), :200,:202 (status/template); ns_dashboard/svc_jobs.py:441 (rights_confirmed); ns_dashboard/fakes.py:176 | ns_dashboard/svc_jobs.py:446 (path only); outputs.source_id FK | only one SELECT site (candidate: nearly write-only; sha256/duration_s/template/title never selected) |
| 10 | data/studio.db | outputs | schema.sql:105 | output_id PK ("<job>/long","<job>/s<k>"), job_id, channel, kind, k, title, path, phash, duration_s, combo_id, status(pending/approved/rejected/uploaded), review_note, source_id FK, created_at | ns_studio/media/outbox.py:304; ns_studio/clipper/stages_clip.py:862 (insert), :877 (delete); ns_studio/policy/final_checks.py:128 (phash); ns_studio/review.py:77,110,129; ns_dashboard/svc_outputs.py:198,215,251; ns_dashboard/fakes.py:139,190,206,523,632 | very many: ns_studio/review.py:24,27,32,159; ns_studio/channels/cadence.py:94,183,195; ns_studio/learn/status.py:44,58,62; ns_studio/policy/final_checks.py:94; ns_dashboard/svc_outputs.py:23,68; ns_dashboard/svc_channels.py:80,85; ns_dashboard/svc_engine.py:150; ns_dashboard/channel_stats.py:143 | |
| 11 | data/studio.db | uploads | schema.sql:123 | output_id PK FK, video_url, published_at | ns_studio/review.py:126 (insert or replace); ns_dashboard/svc_outputs.py:248 | ns_studio/learn/importer.py:175; ns_studio/learn/status.py:145; ns_dashboard/svc_outputs.py:23,136; ns_dashboard/svc_library.py:235; ns_dashboard/svc_settings.py:298 | |
| 12 | data/studio.db | analytics | schema.sql:129 | (output_id, date) PK, views, viewed_pct, avg_pct_viewed, avg_view_duration_s, likes, comments, shares, subs_gained | ns_studio/learn/importer.py:209 (insert or replace) | ns_studio/channels/cadence.py:81; ns_studio/learn/reweight.py:28; ns_studio/learn/status.py:124,134,165,177; ns_dashboard/channel_stats.py:110,118; ns_dashboard/learn_view.py:22,47,53,71; ns_dashboard/assistant/composite.py:297,305 | candidate: columns likes/comments/shares written, no SELECT names them (status.py:134 builds a column name dynamically, not checked further) |
| 13 | data/studio.db | pool_weights | schema.sql:137 | (channel, pool, option) PK, weight, updated_at | ns_studio/learn/reweight.py:110 (insert or replace) | ns_studio/learn/reweight.py:93; ns_studio/variation/draw.py:85; ns_dashboard/svc_settings.py:307 | |
| 14 | data/studio.db | calibration | schema.sql:146 | (channel, story_id) PK, critic_json, human_json | ns_studio/calibrate/run.py:223 (insert or replace), :301 (update human_json) | only a self-subselect at ns_studio/calibrate/run.py:224 (keeps human_json on re-insert); no other SELECT | candidate: write-only |
| 15 | data/studio.db | incidents | schema.sql:154 | incident_id PK AUTOINCREMENT, channel, ts, kind(pause/resume/warning/strike), note | ns_studio/channels/cadence.py:153; ns_studio/review.py:151,173; ns_dashboard/svc_channels.py:145 | ns_studio/channels/cadence.py:144; ns_studio/learn/status.py:68; ns_dashboard/svc_engine.py:139 | |
| 16 | data/studio.db | dashboard_jobs | schema.sql:162 | job_id PK, kind, channel, status, progress, message, created_at, started_at, finished_at, params_json | ns_dashboard/jobs.py:191,281,291,294,311,330,336,373,395; ns_studio/series/workshop.py:1107; ns_dashboard/fakes.py:501 | ns_dashboard/jobs.py:204,218,229,252,257,262,267,273,300,323,364,383; ns_dashboard/svc_engine.py:142; ns_dashboard/svc_voices.py:95; ns_dashboard/assistant/orientation.py:149,175; ns_supervisor/scheduled.py:35 (read-only sqlite URI) | |
| 17 | data/assistant.db (schema also lands in studio.db) | assistant_messages | schema.sql:176 | id PK, ts, role, content, tool_calls_json | ns_dashboard/assistant/store.py:80 | only ns_dashboard/assistant/eval.py:117 (self-test scanning stored strings) | candidate: write-only (outside eval.py) |
| 18 | data/assistant.db | conversations | schema.sql:186 | conversation_id PK, title, created_at, updated_at | store.py:53 (insert), :78,:89 (update) | store.py:61,117 | |
| 19 | data/assistant.db | messages | schema.sql:192 | msg_id PK, conversation_id FK, role, content, tool_calls_json, tool_call_id, name, context_json, created_at | store.py:68 | store.py:96; eval.py:117-119 | |
| 20 | data/assistant.db | actions | schema.sql:204 | action_id PK, conversation_id FK, tool, args_json, confirm_text, context_json, status, result_json, created_at, decided_at | store.py:130 (insert), :152,:160 (update) | store.py:139,168; eval.py:120 | note: store.py:152 sets status 'running' but the schema comment lists only pending/done/failed/dismissed |
| 21 | data/assistant.db | queued_messages | schema.sql:216 (+claimed_at, claimed_by by ALTER at store.py:44) | queue_id PK, conversation_id FK, text, context_json, status(queued/done), created_at, claimed_at, claimed_by | store.py:178 (insert), :198,:208,:216 (update) | store.py:117 (subquery), :186 | |

Also (indexes / views / triggers):
- Indexes, all in schema.sql: idx_clip_usage_clip ON clip_usage(source, clip_id, used_at) :78; idx_clip_usage_channel ON clip_usage(channel, used_at) :79; idx_combos_channel ON combos(channel, created_at) :90; idx_outputs_channel ON outputs(channel, created_at) :121; idx_messages_conv ON messages(conversation_id, msg_id) :203.
- Views: none. Triggers: none. CREATE VIRTUAL TABLE / FTS: none.
- Columns used but NOT in schema.sql (read/written only when present, probed via row.keys()): ideas.setting, ideas.protagonist, ideas.keywords, ideas.updated_at (ns_studio/text/stages_shorts.py:57-95, "CR-C9-6"). No code creates them (candidate: dead optional columns).

#### Schema-version / migration mechanism (extra rows)

| # | item | where | detail |
|---|---|---|---|
| M1 | PRAGMA user_version | NONE | never read or set (grep user_version over *.py, *.sql, *.ps1 found nothing); no schema version number exists |
| M2 | idempotent re-run of the whole schema | ns_common/db.py:39-44 `Db.migrate()` | reads contracts/schema.sql (every statement IF NOT EXISTS) and executescripts it on every open |
| M3 | ALTER TABLE ADD COLUMN (generic) | ns_common/db.py:19-22 `_ADDED_COLUMNS`, :45-49 | adds sources.rights_confirmed, sources.title, ideas.status, ideas.title, ideas.source when `PRAGMA table_info` lacks them (also in CREATE TABLE, so only old DBs are affected) |
| M4 | ALTER TABLE ADD COLUMN (assistant) | ns_dashboard/assistant/store.py:39-44 `_add_claim_columns` | adds queued_messages.claimed_at / claimed_by; these two columns exist ONLY via ALTER (not in schema.sql) |
| M5 | connection PRAGMAs | ns_common/db.py:34-36; schema.sql:2 | journal_mode=WAL, foreign_keys=ON, busy_timeout=5000 |
| M6 | backup / restore | ns_common/db.py:62-80 `Db.backup` (called ns_studio/daily.py:202) -> data/backup/studio-YYYYMMDD.db, keep 14; ns_supervisor/backup.py:26-34 `snapshot_db`; restore allow-list "data/studio.db" backup.py:22 | |
| M7 | cache/clips/index.sqlite | ns_common/paths.py:106 `Paths.clips_index` | property defined but NEVER referenced elsewhere (only paths.py and docs). Nothing opens or creates it; the clips index is the `clips` table of studio.db. candidate: dead path |
| M8 | other sqlite3 users | ns_supervisor/scheduled.py:30, ns_supervisor/backup.py:28,30 | read-only URI opens of studio.db; no other DB files |

Row counts: 21 table rows + 8 mechanism rows (M1-M8).
Candidates: channels write-only; parts read-only; calibration write-only; assistant_messages write-only (outside eval.py); sources nearly write-only; analytics likes/comments/shares columns unread; ideas optional columns never created; M1 user_version absent; M7 clips_index unused.

---------------------------------------------------------------------------------------------------

## 7. Event channels and message protocols

| channel | sender | receiver | message kinds | file:line |
|---|---|---|---|---|
| Supervisor SSE `GET /events` (frames `data: {"ts","kind","data"}`; keepalive comment every 15 s) | `Supervisor.publish` (`ns_supervisor/core.py:153-160`, queue size 1000, slow consumers drop events); first frame is a synthetic snapshot `{kind:"service", data:{event:"snapshot", ...status}}` | dashboard `EventRelay` (`ns_dashboard/supervisor_link.py:109-126,129-170`) | kind `service` with `event` = `starting, ready, stopping, stopped, crash, error` (`core.py:216,245,254,266,303,310,326`), `acquired` (`core.py:381,405`), `released` (`:417,424`), `owner_released` (`:454`), `lease_expired` (`:520`), `stop_all` (`:540`), `shutdown` (`:546`), `snapshot` (`app.py:176`); kind `update` (weekly check result, `core.py:684`); kind `run` `{event:"finished"}` (`core.py:723`); kind `job` (only via `POST /events`, no product sender found) | `ns_supervisor/app.py:171-191`; `core.py:142-163` |
| Supervisor `POST /events` -> same SSE | `SupervisorClient.publish_event` (`ns_supervisor/client.py:124`) | dashboard relay | kind `job` / any string + `data` dict | no caller found in product code (tests only: `tests/test_supervisor_client.py:55`) |
| Dashboard relay -> in-process `EventHub` | `EventRelay.relay_once` (`supervisor_link.py:155-162`) republishes with kind `service` or `job`; any other kind (`update`, `run`) is relabelled `service` | `iter_sse` (`ns_dashboard/events.py:90-110`) | hub kinds `service`, `job`, `dashboard_job` | `supervisor_link.py:161`; `events.py:42-67` |
| Dashboard hub `dashboard_job` | `ns_dashboard/jobs.py:418,669`; `svc_jobs.py:230`; `svc_library.py:538`; `svc_voices.py:101` (payload = `JobRecord.to_dict`: `job_id, kind, channel, status, progress, stage, step, steps, message, created_at, started_at, finished_at, params, result, cancel_requested, schedule_at, dismissed_at`, `jobs.py:79-99`) | `iter_sse` -> SSE `job` frames | `dashboard_job` | as left |
| Dashboard hub `job` `{draining:true}` | `ns_dashboard/svc_engine.py:278` | `iter_sse` -> SSE `job` frame | `job` | as left |
| Dashboard SSE `GET /api/events` (frames `data: {"kind","payload"}`, default `message` event; heartbeat 2 s, `status` frame first and after each event) | `ns_dashboard/svc_engine.py:324-327` -> `events.iter_sse` | browser `EventSource("/api/events")` `ns_dashboard/ui/src/lib/app.tsx:84` (handles only `kind==="status"`) | `status` (payload = `/api/status` object), `job` (no UI consumer found; the page polls status every 10 s / 2 s on failure, `app.tsx:78,86`) | `ns_dashboard/server.py:208`; `events.py:76-110` |
| Assistant chat SSE over `POST /api/assistant/chat` | `ns_dashboard/assistant/loop.py:154-270` | `ns_dashboard/ui/src/lib/api.ts:114-117` consumed by `pages/Assistant.tsx:57-70` | `conversation`, `message` (maybe `partial`), `confirm`, `tool_call`, `tool_result`, `busy`, `error`, `done`; UI ignores `tool_result` and `done` | `ns_dashboard/server.py:635`; `loop.py:159-270` |
| Studio child protocol (stdout of `python -m ns_dashboard.studio_job`; each line `NSJOB {"ev": ...}`) | `ns_dashboard/studio_job.py:37-54` (`emit`) | `ns_dashboard/studio_runner.py` reader (`:229` thread, parse at ~`:80`) -> `adapters.py:300-319` `on_event` | `job` (`job_id`), `stage` (`stage, index, total, job_index, job_total`), `download` (`percent`), `result` (payload e.g. `output_ids`, `job_ids`, `path`, `text`, `name`), `error` (`code, detail, message`), `cancelled`; `adapters.child` acts on `stage`, `download`, `job` only; `result`/`error`/`cancelled` end the run (`studio_job.py:244-254`) | `studio_job.py:7-15,52-54,238-254` |
| Cancel flag file `NS_JOB_CANCEL_FILE` (env var set per child; value = `<home>\tmp\studio-<kind>-*.cancel`) | `StudioRunner._stop` creates it (`studio_runner.py:175`); env set `studio_runner.py:147` | child `report()` checks before each stage and raises `CancelRequested` (`studio_job.py:46-71`) | file exists = cancel | `studio_job.py:39,46-49` |
| Download cancel flag `<home>\tmp\cancel-download-<source_id>` | `adapters.py:403-412` (`signal_cancel`) | `studio_job.watch_cancel_file` polls every 0.2 s (`studio_job.py:150-167`) -> `CancelToken.cancel` | file exists = cancel | `studio_job.py:170-187` |
| Studio GPU lease heartbeat | `ns_studio/acquirer.py:169` (thread `ns-lease-beat`), dashboard job pulse `ns_dashboard/jobs.py:708` (`ns-job-pulse`), `handlers.py:91`, assistant `assistant/llm.py` | supervisor `POST /heartbeat` -> `core.py:482-495`, lease expiry `core.py:497-520` (`lease_expiry_s` 600 s, prefixes `studio:`, `dashboard:assistant` `core.py:58`) | request/response | `ns_supervisor/app.py:138` |
| Studio end-of-run signal | `ns_studio/daily.py:216` | supervisor `POST /run_finished` -> exit decision `core.py:718-725,727-752` | `{exit_scheduled}` | `ns_supervisor/client.py:119-122` |
| WebView2 host -> page (`PostWebMessageAsJson`) | `launchers/nightshift-host.ps1:293-307,508` | `ns_dashboard/ui/src/lib/host.ts:31-37`; `installer/ui/src/host.ts` (re-export) | `{event:"window-state", state:"normal|maximized|minimized", maximized}`, `{event:"close-requested"}` | `nightshift-host.ps1:16-19,301-307,508` |
| Page -> WebView2 host (`window.chrome.webview.postMessage`) | dashboard `shell/TitleBar.tsx:50-79`; setup `installer/ui/src/App.tsx:90-107,177`, `parts/WindowNotches.tsx:50-64`, `host.ts:10` | `nightshift-host.ps1:457-490` (`WebMessageReceived`) | `{cmd:"minimize"}`, `toggle-maximize`, `drag-start`, `close`, `close-ack`, `ready`, `open-external` (+`url`; used only by the setup UI `installer/ui/src/host.ts:10`; typed `HostCommand` in `lib/host.ts:5-11` lacks it) | `nightshift-host.ps1:15-19`; `lib/host.ts:5-15` |
| Host state file `run\apphost-<role>.json` `{hwnd,pid,ready}` | `nightshift-host.ps1:198-209` | `launchers/_common.ps1:163-187` (`Wait-NsHostReady`), `_common.ps1:112-143`, `ns_supervisor/presence.py:31-41`, installer `launch.py:113-115`, `uninstall.py:176` | file protocol | as left |
| Host -> server liveness (HEAD `/` every 5 s, 3 misses closes window) | `nightshift-host.ps1:211-229,419-435` | dashboard / setup server | HTTP HEAD | as left |
| Installer wizard bridge, wizard -> setup server (one JSON object per line on wizard stdout) | `installer/stage2/bridge.py:116-122` (`emit`); `say, rule, markdown, panel` `:186-200`, `table` `:202-206`, `step_status` `:235-237`, `decide_failure` `:239-257`, `notice` `:259-261`, `plan` `:263-266`, `verify_checks` `:268-273`, `sample` `:275-277`, `bundle` `:279-281`, file progress `:58-95` | `installer/stage2/web/engine.py:114-128` -> `controller._on_event` (`controller.py:1501-1505`) dispatching `_ev_<type>` | `type` in `step, say, table, plan, file, warn, failure, verify, sample, bundle` (handlers `controller.py:1507-1739`); `file.event` in `start, progress, finish, retry`; prompts surface as `warn` with `code:"prompt"` (`bridge.py:215,220,225`) | `bridge.py`; `controller.py:1501-1739` |
| Installer bridge, setup server -> wizard (stdin lines) | `EngineProcess.send` (`engine.py:96-105`) | `bridge.py:146-162` (`handle`) | `{cmd:"keys"}`, `{cmd:"decision", action: retry|skip|quit}` (`DECISIONS` `bridge.py:31`), `{cmd:"pause"}`, `{cmd:"resume"}`, `{cmd:"stop"}` (also issued when stdin closes `bridge.py:144`) | `bridge.py:130-162` |
| Setup long-poll state channel | `SetupController.wait_state` (`controller.py:642`, 15 s) | setup UI `useSetup.tsx` via `GET /api/state?since=` | full state JSON (camelCase, `schema:1`, `STEP_ORDER`, `PHASES`, `RUN_STATES` `web/model.py:11-34`) | `server.py:257` |
| Setup server file hand-off to stage 1 | `launch.write_server_files` (`installer/stage2/web/launch.py:38-52`), `update_server_info` `:205` | `installer/stage1.ps1:1010-1045` (`Read-ServerInfo`, `Invoke-ServerPing`) | `setup-server.json {pid, port, token, started, host, host_text}`; `setup-address.txt` | as left |
| Windows toast / morning note | `ns_supervisor/notify.py:44-58,106-123` (callers: `ns notify` `cli.py:393`, studio `ns_studio/notifications.py` (nightly summary `daily.py:215`, failure `cli.py:215`), weekly update check `ns_supervisor/update_check.py:89`) | Windows shell; fallback `run\morning_note.json` read by dashboard `svc_engine.py:241-256` (`GET /api/notes`) | env `NS_TOAST_TITLE`, `NS_TOAST_MESSAGE` carry title / message to PowerShell (`notify.py:47`); notes `{at,title,message}` newest 20 | `notify.py:24-123` |
| In-page toasts (React) | `toast()` from `ns_dashboard/ui/src/lib/app.tsx` and `installer/ui/src/parts/Toasts.tsx` | the same page | UI-local, no wire protocol | - |
| Update worker progress | `ns_supervisor/update_job.py:68-110` writes `run\update_state.json {state: running|done|failed, message, pid...}` and `logs\update-<ts>.log` | `launchers/update.ps1:25-44,48-72`; dashboard `updater.py:229-235` / `svc_library.py:466` (`GET /api/update/state`) | state values `running`, `done`, `failed` | as left |
| Engine state file | `Supervisor.write_state` (`core.py:171-184`) `run\supervisor.json {pid, port, started_at, children}` | `control.stop` (`control.py:67-105`) | file protocol | as left |
| Backup / restore manifest | `ns_supervisor/backup.py:18` (`backup-manifest.json`) | restore | zip protocol | - |
| Pending voice hand-off | installer `s07_configs.py:99-101` writes `config\pending_voice.json {channel, mode, name, description | ref_audio_path, ref_text, rights_confirmed}` | `ns_studio/voice_setup.py` via `studio voice apply-pending` (`ns_studio/cli.py:408`), `policy/regen.py:132`, dashboard `adapters.py:126,830`, job kind `apply_voice` | file protocol | as left |
| Supervisor error mapping | `NsError` -> JSON `{code, message, detail}` with HTTP 409 (NS-E201), 502 (NS-E202), 504 (NS-E203), else 500 | clients: `SupervisorClient._call` (`client.py`), dashboard `_post` (`supervisor_link.py:59-72`) | error codes from `contracts/errors.json` | `ns_supervisor/app.py:31,115-118` |

## 8. External endpoints


UA column: `httpx` default is `python-httpx/<ver>`, urllib default is `Python-urllib/3.x`, Windows PowerShell 5.1 `Invoke-WebRequest` sends its own `WindowsPowerShell/...` UA. These are library defaults, not constants in the repo, so they are marked "library default".

| # | Host / URL pattern | Purpose | Caller file:line | User-agent | Failure handling / timeout / retry |
|---|--------------------|---------|------------------|------------|------------------------------------|
| 1 | https://huggingface.co/{repo}/resolve/{revision}/{file}; manifest.json repos: :31 `Qwen/Qwen3-14B-GGUF`, :38 `mudler/Qwen3.6-35B-A3B-APEX-MTP-GGUF`, :45 `unsloth/Qwen3.6-35B-A3B-MTP-GGUF`, :52 `Youssofal/Qwen3.6-35B-A3B-Abliterated-Heretic-GGUF`, :108 `Qwen/Qwen3-1.7B-GGUF` (cpu-dev) | LLM GGUF models (pinned commit, sha256, bytes) | installer/stage2/downloader.py:47 (`HF`), :117-119 (`hf_url`), :681-687 (`_hf_item`), :787-845 (`plan_models`, LLM at :812); also ns_supervisor/models_get.py:84,94 | none set / library default (httpx) | NS-E105 on network/404 (downloader.py:284,318,481-490); NS-E109 wrong hash (:368; HASH_ATTEMPTS=2 :55); NS-E104 disk full (:190); NS-E113 AV block (:228). Retries DEFAULT_RETRIES=3 (:52), MAX 5 (:53), backoff min(2^n,30 s) (:54), stall timeout 60 s (:49), client 30 s connect / 60 s read (:216), offline wait up to 120 s with 5 s TCP probes (:56-58,378-383) |
| 2 | https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF/resolve/0188c9bf.../nomic-embed-text-v1.5.Q8_0.gguf | Embedding model (manifest.json:59,116) | downloader.py:813 | none set / httpx default | same as row 1 |
| 3 | https://huggingface.co/Serveurperso/Qwen3-TTS-GGUF/resolve/b7ee2e8c.../ qwen-talker-1.7b-customvoice, qwen-talker-1.7b-base, qwen-tokenizer-12hz, qwen-talker-1.7b-voicedesign (optional), qwen-talker-0.6b-customvoice (cpu-dev), all `-Q8_0.gguf` | TTS GGUFs (manifest.json:67-95,124-136) | downloader.py:815-817 | none set / httpx default | same as row 1 |
| 4 | https://huggingface.co/Systran/faster-distil-whisper-large-v3 (manifest.json:98) and Systran/faster-whisper-tiny (manifest.json:141, cpu-dev) `/resolve/{rev}/` model.bin, config.json, tokenizer.json, preprocessor_config.json, vocabulary.json, vocabulary.txt | Whisper CTranslate2 files (only model.bin hash-pinned) | downloader.py:819-835; side-file lists :64-65 | none set / httpx default | same as row 1; optional files may 404 and are reported `missing_optional` (downloader.py:1019-1020) |
| 5 | https://huggingface.co/api/models/{repo}/tree/{revision}?recursive=true | Lists files and LFS sha for the PyTorch TTS fallback repos (manifest.json:24-27 `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice`, `...-Base`; revisions manifest.json:216-220, also `...-VoiceDesign`), followed by `resolve` downloads | downloader.py:870-880; installer/stage2/fallback.py:33-38; ns_supervisor/models_get.py:43-45 | none set / httpx default | NS-E105 on HTTP/JSON error (downloader.py:879-880); timeouts: 30 s connect / 120 s read (installer/stage2/context.py:31) or 60 s read (downloader.py:216) |
| 6 | https://huggingface.co/{owner-typed repo}/resolve/main/{typed file}.gguf (no sha) | Custom story model chosen by the owner (size pre-check, then download) | download: downloader.py:690-697 (`custom_item`); pre-check: installer/stage2/web/modelcheck.py:19,35-37,65-80 (`urllib_head`) | pre-check: `NightshiftSetup` (modelcheck.py:68); download: httpx default | pre-check: OSError -> `network` code NS-E105 (modelcheck.py `_REJECT`), 401/403 login, 404 missing, timeout 15 s (:21), 3 redirect hops (:22). Download: as row 1 |
| 7 | https://github.com/ggml-org/llama.cpp/releases/download/b12032/ llama-b12032-bin-win-cuda-12.4-x64.zip, cudart-llama-bin-win-cuda-12.4-x64.zip, llama-b12032-bin-win-cuda-13.4-x64.zip, cudart-llama-bin-win-cuda-13.4-x64.zip, llama-b12032-bin-win-cpu-x64.zip | llama.cpp server builds and CUDA runtimes (manifest.json:149-174) | downloader.py:699-726 (`plan_bins`); pair choice installer/stage2/steps/s02_download_bins.py:45 (`choose_pair`) | none set / httpx default | row 1 behavior; NS-E103 when stage 1 read CUDA < 12.4 (s02_download_bins.py:48 docstring) |
| 8 | https://github.com/AbdulrahmanAmer/nightshift/releases/download/v0.1.4/Nightshift-VoiceEngine-win-cuda-x64.zip | Pinned qwentts.cpp voice engine zip (manifest.json:175-179) | downloader.py:766-771 (`qwentts_item`); s02_download_bins.py:141 | none set / httpx default | row 1 behavior; if the manifest pin is null: row 9 |
| 9 | https://github.com/AbdulrahmanAmer/nightshift/releases/latest/download/Nightshift-VoiceEngine-win-cuda-x64.zip and .../qwentts-win-cuda-x64.zip (old name) | Unpinned fallback when the manifest has no qwentts pin: HEAD probe of both names, then download without sha | installer/stage2/steps/s02_download_bins.py:35-42,80-91 (`probe_release`), :143-148 | none set / httpx default | probe: `status_code < 400`; `httpx.HTTPError` -> False, logged `qwentts_probe_failed` (:88); nothing found -> NS-W301 and PyTorch fallback (:156) |
| 10 | https://github.com/anomalyco/opencode/releases/download/v1.19.5/opencode-windows-x64.zip | OpenCode ("Nightshift Code"), only when chosen (manifest.json:180-184) | downloader.py:728-734 | none set / httpx default | row 1 behavior; not downloaded in dev mode (installer/stage2/devmode.py:34 `SKIPPED_BINS`) |
| 11 | https://github.com/astral-sh/uv/releases/download/0.13.6/uv-x86_64-pc-windows-msvc.zip | Pinned uv (manifest.json:185-189), fetched by two code paths plus a winget fallback | (a) installer/stage1.ps1:853-865 `Invoke-WebRequest -UseBasicParsing`, no `-TimeoutSec`; (b) installer/stage2/web/bootstrap.py:211-245 `urllib.request.urlopen(url, timeout=120)` (:224), caller :649; (c) winget id `astral-sh.uv`, stage1.ps1:883 | (a) PowerShell default (library default); (b) `Python-urllib/3.x` (library default, no Request headers) | (a) sha mismatch throws (:866), then winget (:878-896): NS-E110 if winget existed and failed (:894), else NS-E105 (:896); (b) NS-E105 (bootstrap.py:229,232); no retry in either |
| 12 | https://github.com/yt-dlp/yt-dlp/releases/download/2026.10.26/yt-dlp.exe | Pinned yt-dlp exe (manifest.json:190-194) | downloader.py:735-741 | none set / httpx default | row 1 behavior |
| 13 | https://github.com/explosion/spacy-models/releases/download/en_core_web_md-3.8.0/en_core_web_md-3.8.0-py3-none-any.whl | spaCy model wheel, hash-locked via `uv pip install --require-hashes` (manifest.json:18-22). URL template without manifest pin: installer/stage2/steps/s04_venvs.py:34-39 | s04_venvs.py:108-117 | uv's own UA (not set in repo) | `uv_error` -> NS-E105 / NS-E104 / NS-E113 (installer/stage2/errors.py:34-47) |
| 14 | https://github.com/opencv/opencv_zoo/raw/f12e1279.../models/face_detection_yunet/face_detection_yunet_2023mar.onnx | YuNet face detector (manifest.json:200-204) | downloader.py:836-842 | none set / httpx default | row 1 behavior |
| 15 | https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-10-31-12-59/ffmpeg-n8.1.3-27-g8dca1586f4-win64-gpl-8.1.zip | FFmpeg static build, fallback when winget fails (manifest.json:205-209) | installer/stage2/web/bootstrap.py:555-570 (`_install_ffmpeg`) via `download_file` :247-290 | `Python-urllib/3.x` (library default) | `download_file`: 3 attempts with sleep 2^n (:258,277-280), timeout 120 s (:264); NS-E105 (:280), NS-E109 digest (:283-285), NS-E113 AV block (:275); required tool -> NS-E110 (bootstrap.py:519,576) |
| 16 | winget source: ids `Gyan.FFmpeg`, `Microsoft.WindowsTerminal`, `Microsoft.VCRedist.2015+.x64`, `astral-sh.uv`, run with `--source winget` | Preferred installer for FFmpeg, VC++ runtime, Terminal, uv (winget's own CDN, hosts not in repo) | installer/stage2/web/bootstrap.py:35-42,496; ns_common/vcruntime.py:17 (id); installer/stage1.ps1:883 | winget (n/a) | falls back to rows 15/17; NS-E114 (vcruntime) / NS-E110 (ffmpeg) when still missing (bootstrap.py:519,576); wait up to `ELEVATED_WAIT_S`=600 s (bootstrap.py:46); stage1 text for NS-E110/NS-E111 at stage1.ps1:67-68 |
| 17 | https://download.visualstudio.microsoft.com/download/pr/990c720a-.../VC_redist.x64.exe | Pinned VC++ 14.44.35211.0 redistributable (manifest.json:210-214) | installer/stage1.ps1:902-950 (`Install-VcRuntime`, `Invoke-WebRequest` :921, no `-TimeoutSec`); installer/stage2/web/bootstrap.py:532-553 (`_install_vcruntime` via `download_file`) | stage1: PowerShell default; bootstrap: `Python-urllib/3.x` (library defaults) | stage1: not fatal, prints fix and returns false (:922-929); bootstrap: NS-E114 (:537,551), NS-E105/109/113 from `download_file` |
| 18 | https://aka.ms/vs/17/release/vc_redist.x64.exe | Constant `REDIST_ALIAS` (ns_common/vcruntime.py:16) and manual-fix text (installer/stage1.ps1:928,949). No product caller dials it | ns_common/vcruntime.py:16; installer/stage1.ps1:928,949 | none | display only |
| 19 | https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/1.0.2903.40/microsoft.web.webview2.1.0.2903.40.nupkg | WebView2 SDK DLLs (3 files); skipped when the release bundle already carries them (installer/webview2_files.json; downloader.py:654-674 `adopt_bundled_webview2`) | downloader.py:743-745, :748-765 (`webview2_item`) | none set / httpx default | row 1 behavior; sha "TBD-release" -> item skipped and Edge/Chrome `--app` used (webview2_item docstring). A download of the Evergreen WebView2 Runtime: not found in code |
| 20 | https://download.pytorch.org/whl/cu128 (`--index-url`) | torch==2.9.1+cu128 and torchaudio==2.9.1+cu128 for the PyTorch fallback venv (manifest.json:6-7) | installer/stage2/fallback.py:44-48,63-70 (`parse_pin`, `uv pip install --index-url`) | uv's own UA | `uv_error` NS-E105 / NS-E104 / NS-E113 (errors.py:34-47); NS-E108 if torch cannot see the GPU (contracts/errors.json:10) |
| 21 | https://pypi.org (simple index) and https://files.pythonhosted.org | All other pip/uv packages: `uv pip install -e <app>[extras]` for studio/supervisor/tts venvs, whisper_cuda wheels (manifest.json:14-17, sha256-pinned), qwen-tts, fastapi, uvicorn (manifest.json:8-11). No index URL in code or pyproject.toml, so this is uv's default (implicit) | installer/stage2/steps/s04_venvs.py:90-106,118-134; ns_supervisor/update.py:409-441 (`resync_venvs`); reachability probe stage1.ps1:54 | uv's own UA | stage1 preflight: pypi.org, files.pythonhosted.org and github.com are `CriticalHosts` (stage1.ps1:57) -> fail NS-E105 (:670-675); uv failure -> NS-E105 via `uv_error`; update re-sync -> NS-E702 (ns_supervisor/update.py:439) |
| 22 | python-build-standalone via github.com (implicit; URL is not in the repo) | `uv python install 3.12` downloads managed CPython | installer/stage1.ps1:1265,1275 | uv's own UA | NS-E105, exit 13 (stage1.ps1:1278) |
| 23 | https://api.github.com/repos/AbdulrahmanAmer/nightshift/releases/latest | `ns update --check/--github` and the weekly background check (asset list, `digest`, `.sha256` sidecar) | ns_supervisor/releases.py:24-25 (`REPO`, `LATEST_URL`), :132-145 (`fetch_latest`, GET at :136); background: ns_supervisor/update_check.py:79; ns_supervisor/update.py:556-558 | header `Accept: application/vnd.github+json` only; no User-Agent constant (httpx default) | `_unreachable` (releases.py:70-78): HTTP status errors -> NS-E118, connection errors -> NS-E105; NS-E701 for missing tag/digest/mismatch (:104,145,170,173,210). Timeout 30 s (:134), no retry |
| 24 | Release assets of AbdulrahmanAmer/nightshift: `Nightshift-Update-<version>.zip` (older `nightshift-app-<version>.zip`) plus `.sha256` sidecars (`nightshift-setup-<version>.zip.sha256`, `nightshift-<version>.zip.sha256`), at `browser_download_url`, only github.com / *.githubusercontent.com (`TRUSTED_HOSTS`, releases.py:26) | Update package and checksum | ns_supervisor/releases.py:107-125 (selection), :160-173 (`expected_digest`), :184-232 (`download`, stream :215) | none set / httpx default | NS-E701 when redirected off GitHub (:218) or SHA mismatch (:210, checked on the temporary file before it is moved under its name); NS-E105/NS-E118 on connection/status (:224). Timeout 60 s connect / 300 s read (:197), no retry |
| 25 | git remote `origin` of the install folder (URL not in code) | `ns update --git`: `git fetch --tags --prune origin` | ns_supervisor/update.py:482-483 (`_git_run`, timeout=900 s), :522 | git's own | NS-E105 "git fetch failed" (update.py:524); NS-E702 if git missing or not a clone (:510-512); hint text mentions https://git-scm.com/download/win (:50-53, text only) |
| 26 | https://api.pexels.com/v1/search?query=nature&per_page=1 (Authorization: key) via httpx | Setup validation of the Pexels key (wizard) | installer/stage2/steps/s01_questions.py:53-65 (`validate_pexels`) | none set / httpx default (client from installer/stage2/context.py:31) | network error -> NS-E503 (:61); non-200 other than 429 -> NS-E501 (:65); 429 counts as a good key. Timeout 30 s connect / 120 s read. A saved key that fails is non-fatal and forgotten (s01_questions.py:221-225) |
| 27 | https://pixabay.com/api/?key=...&q=nature&per_page=3 via httpx | Setup validation of the Pixabay key (wizard) | s01_questions.py:68-78 (`validate_pixabay`) | none set / httpx default | NS-E503 (:73), NS-E502 (:77), 429 ok |
| 28 | https://api.pexels.com/v1/search and https://pixabay.com/api/ via urllib | Same two key checks from the setup window (web server) | installer/stage2/web/keys.py:40-61 (`check_key`; URLs at :50,:54) | keys.py:24 `USER_AGENT = f"Nightshift-Setup/{_VERSION} (+https://github.com/AbdulrahmanAmer/nightshift)"` (reason in keys.py:19-23) | NS-E501 / NS-E502 on rejection (`REJECTED` :25), NS-E503 on OSError/ValueError (:58); timeout `TIMEOUT_S`=15 s (:19) |
| 29 | https://api.pexels.com/videos/search (base `PEXELS_URL`, ns_studio/media/clip_sources.py:25) | Stock-footage search at run time (token bucket 180 per 3600 s, :242) | clip_sources.py:247-256 (`PexelsSource.search`), `_get` :140-157; wired at ns_studio/context.py:83 | none set / httpx default (client `timeout=60, follow_redirects=True`, :135) | httpx error -> NS-E503 (:150); 429 -> sleep per Retry-After / X-RateLimit-Reset, 4 attempts, then NS-E501 (:151-157); other >=400 -> NS-E501 (:155) |
| 30 | https://pixabay.com/api/videos/ (`PIXABAY_URL`, clip_sources.py:26) with key, q, video_type=film, safesearch | Pixabay video search (24 h response cache, bucket 90 per minute, :337) | clip_sources.py:360-366; ns_studio/context.py:84 | none set / httpx default | same `_get` logic; NS-E502 as `api_error` (:322) |
| 31 | Pexels/Pixabay CDN video URLs (hosts come from API JSON `video_files[].link` / `videos.*.url`; not in repo) | Download the chosen mp4 | clip_sources.py:160-176 (`_fetch_file`, `client.stream`); `download` methods :258, :372 | none set / httpx default | HTTP >= 400 -> NS-E501/NS-E502 (:165); httpx error -> NS-E503 (:171); `.part` removed |
| 32 | Any user-supplied http(s) URL (Create page, `studio clip <url>`; YouTube is the documented case) | yt-dlp download of a source video | ns_studio/clipper/source.py:226 (`is_url`), :236-247 (`ytdlp_args`: `-f bv*[height<=1080]+ba/b --merge-output-format mp4 --no-playlist --newline --write-info-json -- <url>`), :309-376 (`import_url`); dashboard path ns_dashboard/studio_bridge.py:167-176, ns_dashboard/studio_job.py:171; link validation ns_dashboard/svc_jobs.py:10-21 | yt-dlp's own; the repo passes no `--user-agent`, `--cookies`, `--cookies-from-browser`, `--proxy` or `--impersonate` (confirmed absent in `ytdlp_args`) | NS-E409 (`DOWNLOAD_FAILED`, source.py:219): not http(s) (:327), binary missing (:330), spawn failure (:342), cancelled (:369), non-zero exit with last 12 lines (:371), no output file (:374). No timeout and no retry in code |
| 33 | https://www.nvidia.com/Download/index.aspx | "Open the driver page" button (opened in the browser, not fetched) | installer/stage2/web/preflight.py:23,117 | n/a | n/a (nvidia-smi is local) |
| 34 | ms-windows-store://pdp/?ProductId=9NBLGGH4NNS1 | Opens the App Installer page when winget is missing (not an HTTP dial) | installer/stage2/web/preflight.py:24,123 | n/a | n/a |
| 35 | https://www.pexels.com/api/ and https://pixabay.com/api/docs/ | Key help links opened in the system browser | installer/stage2/steps/s01_questions.py:44-45; installer/ui/src/screens/Choices.tsx:318-319 (`openExternal`) | browser | n/a |
| 36 | https://studio.youtube.com/ | "Open YouTube Studio" link on the Queue page | ns_dashboard/ui/src/pages/Queue.tsx:187 | browser | n/a (Nightshift never uploads, ns_dashboard/assistant/docs_index.py:206) |
| 37 | Stage 1 reachability probe, HEAD to https://huggingface.co, https://github.com, https://download.pytorch.org, https://api.pexels.com, https://pixabay.com, https://pypi.org, https://files.pythonhosted.org | Pre-flight "Internet" row | installer/stage1.ps1:52-55 (list), :366-376 (`Test-HostReachable`: `-Method Head -UseBasicParsing -TimeoutSec 10`), :470-472; TLS 1.2 forced :1121 | PowerShell default (library default) | Any HTTP answer counts as reachable; unreachable critical hosts (github.com, pypi.org, files.pythonhosted.org; :57) -> `fail` NS-E105, others -> `warn` NS-E105 (:670-679) |
| 38 | Manifest tool: https://huggingface.co/api/models/{repo}/tree/{rev}, https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}, https://raw.githubusercontent.com/{owner}/{repo}/{path} (LFS pointer sha), HEAD on `resolve` URLs | Dev/release tool that fills and checks manifest.json hashes (not shipped runtime) | tools/resolve_manifest.py:11-13,21-24,31-36,43-50,96-103,125-134,166 | tools/resolve_manifest.py:166 `User-Agent: nightshift-resolve` | prints `PROBLEM:` lines and returns 1 (:172-176); `raise_for_status` raises; timeout 60 s (:166) |
| 39 | https://opencode.ai/config.json | `$schema` string written into config/opencode.json; the product does not fetch it (config also sets `autoupdate: false`, `share: disabled`) | installer/stage2/steps/s07_configs.py:24,40-41 | n/a | not dialed by product code |
| 40 | https://nodejs.org/ | Text in DEMO.cmd error message only | DEMO.cmd:7 | n/a | display only |

Not found (honest negatives): no HF mirror hosts, no HF token or `Authorization` header for Hugging Face, no Evergreen WebView2 Runtime download (go.microsoft.com / microsoft.com/go) anywhere in .py/.ps1/.cmd/.json, no spaCy `download` command (the wheel is the direct GitHub URL), no npm / opencode.ai download, no ffmpeg source other than the BtbN GitHub build and winget `Gyan.FFmpeg`, no Google Drive fetch (installer/support.json `driveUrl` is only checked for `https://` and shown: installer/stage2/web/controller.py:432-436, never requested).

### Notes

1. **Inconsistent User-Agent policy.** Three explicit UAs exist: `Nightshift-Setup/<ver> (+https://github.com/AbdulrahmanAmer/nightshift)` (installer/stage2/web/keys.py:24), `NightshiftSetup` (installer/stage2/web/modelcheck.py:68), `nightshift-resolve` (tools/resolve_manifest.py:166). Everything else (downloader.py, the wizard key checks at s01_questions.py:53-78, ns_studio/media/clip_sources.py, releases.py, stage1.ps1 Invoke-WebRequest, bootstrap.py urllib) relies on library defaults. keys.py:19-23 states that Pexels and Pixabay sit behind Cloudflare, which 403s the default `Python-urllib` signature, yet the same hosts are called with httpx's default UA at s01_questions.py:56-71 and at run time in clip_sources.py. I did not test whether httpx's default UA is blocked (no network use). bootstrap.py `download_file` / `download_uv` use bare `urllib.request.urlopen(url, timeout=120)` (:224,264) with `Python-urllib/3.x` against github.com and download.visualstudio.microsoft.com.
2. **Pexels/Pixabay key validation exists twice** with different transports and UAs: httpx (s01_questions.py:53-78) and urllib (keys.py:40-61). The API URLs are hard-coded in both and again in clip_sources.py:25-26.
3. **Hard-coded duplicates.** `https://huggingface.co` appears in downloader.py:47, modelcheck.py:19 and tools/resolve_manifest.py:11; the repo slug `AbdulrahmanAmer/nightshift` in manifest.json:177, releases.py:24, s02_download_bins.py:36,41 and keys.py:24; the spaCy wheel URL template in s04_venvs.py:34-39 duplicates manifest.json:20; the PyTorch index is in manifest.json:6-7 and in the hint of contracts/errors.json:10; the VC++ redist is pinned (manifest.json:212) and aliased (ns_common/vcruntime.py:16, stage1.ps1:928,949); the host list is in stage1.ps1:52-55 and in the NS-E105 hint (contracts/errors.json:7), and the two lists differ (the hint omits pypi.org and files.pythonhosted.org).
4. **qwentts source mismatch.** manifest.json:177 pins `AbdulrahmanAmer/nightshift` v0.1.4, while s02_download_bins.py:35-42 hard-codes unpinned `releases/latest/download/` URLs (no sha) as the fallback when the pin is null. CI builds from `ServeurpersoCom/qwentts.cpp` (.github/workflows/build-qwentts.yml:21). The pinned OpenCode owner is `anomalyco` (manifest.json:182); I did not verify upstream naming.
5. **Pre-flight host list is incomplete.** It probes 7 hosts (stage1.ps1:52-55) but runtime also needs api.nuget.org (WebView2), download.visualstudio.microsoft.com (VC++ redist), api.github.com (updates), raw/objects.githubusercontent.com and Hugging Face CDN redirect hosts (never named in code), and the Pexels/Pixabay CDNs. None of these are probed.
6. **Env-var spelling and handling.** Two prefixes are used: `NIGHTSHIFT_*` (home, dev mode, bootstrap uv, under-engine) and `NS_*` (everything else). `NS_SUPERVISOR_URL` is honoured only by Python (supervisor_link.py:30), not by the PowerShell launchers. The literal `"NIGHTSHIFT_HOME"` is defined as `HOME_VAR` twice (installer/stage2/winenv.py:14, installer/stage2/web/bootstrap.py:33) and spelled as a literal in ns_common/paths.py:28, ns_common/ffmpeg.py:16, services.py:61, schedule.py:60, studio_runner.py:140 and controller.py:441. `NIGHTSHIFT_DEV_MODE` truthiness is implemented twice (stage1.ps1:1155 regex `^(1|true|yes)$`; devmode.py:39-40 lowercase tuple). `NS_EXIT` (cmd-local) and `NS_CODE` / `NIGHTSHIFT_OWNERS` (Python constants) look like environment names but are not.
7. **Home resolution order differs by component.** Python: `NIGHTSHIFT_HOME`, else `%LOCALAPPDATA%\Nightshift` (paths.py:27-32). PowerShell launchers prefer the folder two levels above the script when it holds `config\runtime.json`, even over the variable (launchers/_common.ps1:21-26). stage1 prefers the user-registry value over the process variable (stage1.ps1:826-828).
8. **Stage-1 leakage is handled in several places.** `PYTHONPATH` and `NIGHTSHIFT_BOOTSTRAP_UV` are cleared by launchers/_common.ps1:158-159 and by `STAGE1_ONLY_ENV` (controller.py:178); `PYTHONPATH` is also dropped by studio_runner.py:41. `NS_DASHBOARD_NO_HOST` is scrubbed only by ns_supervisor/relaunch.py:31; ns_supervisor/client.py:138 (engine start) removes only `NIGHTSHIFT_UNDER_ENGINE`.
9. **Timeouts and retries are uneven.** The model downloader has full retry, backoff and offline-wait logic (downloader.py:49-58); stage1 `Invoke-WebRequest` downloads of uv and the VC++ redist have no timeout or retry (stage1.ps1:864,921); releases.py has timeouts but no retry; yt-dlp has no timeout (source.py:309-376); Pexels/Pixabay `_get` retries only 429 (4 attempts).
10. **Implicit endpoints not visible as URLs in the repo:** uv's PyPI index and python-build-standalone fetch, the winget source CDN, Hugging Face redirect/CDN hosts, and whichever hosts the Pexels/Pixabay API JSON names.
11. **Setup HTTP clients.** `default_http` (installer/stage2/context.py:29-31) uses 30 s connect / 120 s read; `downloader._new_client` (downloader.py:215-216) uses 30 s connect / 60 s read. Both follow redirects; the only header the downloader sends is `Range` (downloader.py ~310).
12. Local-only HTTP (127.0.0.1 supervisor, llama, embed, TTS: ns_common/models.py, llm_client.py:35, embed_client.py:111, tts_client.py:91, ns_supervisor/client.py:33, ns_dashboard/supervisor_link.py:47) was excluded from Table B per the brief.
13. **Concurrent edits.** Source files were modified by something else at 23:33 and 23:42 during this research (installer/stage2/downloader.py, steps/s02_download_bins.py, context.py, services.py, studio sources, controller.py, uninstall.ps1, models.py). Line numbers above were re-verified after those edits for the files listed, but may still shift.

## 9. Windows integration

| item | created by | removed by (uninstall.ps1?) | file:line |
|---|---|---|---|
| Scheduled task `Nightshift Daily` (daily trigger, `WakeToRun`, `StartWhenAvailable`, battery-safe, 6 h limit, principal S4U else Interactive, `RunLevel Limited`; action `cmd.exe /d /s /c "set NIGHTSHIFT_HOME=... && studio.exe run --daily"`) | `ns schedule install` -> `ns_supervisor/schedule.py:42-88` (installer `steps/s09_shortcuts_scheduler.py:140-141`; a dev install or a kept-off task registers it disabled in the one call, `New-ScheduledTaskSettingsSet -Disable`) | `ns schedule remove` (`schedule.py:91-103,164-168`); `installer/uninstall.ps1:171` (`Unregister-ScheduledTask`) | `ns_supervisor/schedule.py:13,60-88`; `installer/uninstall.ps1:171` |
| Task state mirror `config\schedule.json` (mode `registered` / `interactive_only` / `disabled` = a dev install's task / `switched_off` = the owner switched it off, kept by `ns update`, Settings and `ns schedule install`) | `schedule_cfg.write_run_time` | `schedule_cfg.clear_run_time`; deleted with the home on uninstall | `ns_supervisor/schedule_cfg.py:43-69` |
| 7 Desktop shortcuts `Nightshift Dashboard / Studio / Outbox / - Stop All / - Update / - Diagnostics / Code` (target `powershell.exe -NoProfile -ExecutionPolicy Bypass -File <launcher>`, working dir = home, brand icon) | installer step s09 via `winenv.create_shortcut` (a PowerShell/WScript.Shell script) | `installer/uninstall.ps1:186-191` (all `Nightshift*.lnk` on the user and the public Desktop) | `installer/stage2/steps/s09_shortcuts_scheduler.py:35-43,128-136`; `installer/stage2/winenv.py:154` |
| AppUserModelId registry key `HKCU\Software\Classes\AppUserModelId\Nightshift` (DisplayName, IconUri) | `winenv.register_aumid` | `winenv.unregister_aumid` (`winenv.py:318`); `installer/uninstall.ps1:176-181` | `installer/stage2/winenv.py:192-197,292-317`; `installer/stage2/steps/s09_shortcuts_scheduler.py:172`; `ns_supervisor/notify.py:24` (`AUMID = "Nightshift"`) |
| AppUserModelId property stamped on the Dashboard shortcut (IPropertyStore) | `winenv.set_shortcut_aumid` | goes with the shortcut | `installer/stage2/winenv.py:328-350`; `s09_shortcuts_scheduler.py:177-181` |
| Toast notifications (`Windows.UI.Notifications` through PowerShell, `ToastText02`, AUMID `Nightshift`) | `ns_supervisor/notify.py:44-58` (`toast`), `toast_or_note` `:106`; callers `ns notify` (`cli.py:393`), studio `notifications.py`, `update_check.py:89` | n/a | `ns_supervisor/notify.py:24-58,106-123` |
| Morning note instead of a toast in session 0 (`ProcessIdToSessionId`) | `ns_supervisor/notify.py:60-76,93-100` | dashboard deletes after showing (`ns_dashboard/svc_engine.py:256`) | `ns_supervisor/notify.py:60-100` |
| Windows Credential Manager, service `Nightshift`, names `pexels_api_key`, `pixabay_api_key` (via the `keyring` package) | installer `KeyringStore.set` (`installer/stage2/secrets.py:41-63`); dashboard Settings `RealKeyringAdapter` (`ns_dashboard/adapters.py:565`) | `installer/uninstall.ps1:208` (python -c keyring.delete_password for both names) with `cmdkey /delete:Nightshift` fallback `:213` | `ns_common/secrets_names.py:5-8`; `ns_studio/jobs.py:85-115` (reader) |
| DPAPI blob file `config\secrets.dpapi` (machine scope, readable by the S4U nightly task; ACL restricted with `icacls`) | `ns_common/secret_file.py:39-111` (`CryptProtectData`); `ns_common/secret_file.py` `restrict_to_user` (grants the token's SID, reads the ACL back, raises NS-E120; re-exported from `installer/stage2/secrets.py`), `store_secret`/`delete_secret` (lock the temp file before the keys go in), `relock_existing` (engine start, `ns_supervisor/app.py` `_serve`), `ResilientStore.set` in `installer/stage2/secrets.py` | `installer/uninstall.ps1:216-219` (always removed) | `ns_common/secret_file.py:26-35` |
| User environment variable `NIGHTSHIFT_HOME` (HKCU\Environment) | `installer/stage2/web/bootstrap.py:110-118,700-702` (`set_user_variable`); `installer/stage2/winenv.py:31-35` via `steps/s01_questions.py:290` | `installer/uninstall.ps1:254` (`SetEnvironmentVariable(..., $null, 'User')`) | `bootstrap.py:33`, `winenv.py:14` (two copies of the constant) |
| User PATH entry `<home>\bin\ffmpeg` (only when the pinned FFmpeg fallback is used) | `installer/stage2/web/bootstrap.py:135-160,568` (`add_user_path_entry`, REG_EXPAND_SZ kept) | `installer/uninstall.ps1:73-92,185` (`Remove-UserPathEntry`) | as left |
| Session PATH prepends: WebView2 dir; cuBLAS / cuDNN dirs | `launchers/nightshift-host.ps1:235`; `ns_common/gpu_dlls.py:40`; `ns_studio/media/transcribe.py:211` | process-local | as left |
| Windows Job Object `KILL_ON_JOB_CLOSE` (+ `BREAKAWAY_OK`) holding every engine child | `ns_supervisor/procs.py:99-112,139-163` (assigned `procs.py:229-230`), created by `Supervisor.__init__` `core.py:107` | `Supervisor.close_job` `core.py:190-192` (lifespan end `app.py:111`) or the OS when the engine dies | `ns_supervisor/procs.py:36-137` |
| Breakaway / WMI spawn so the update worker outlives the engine's job | `ns_supervisor/update_job.py:37-40,191-241` | n/a | as left |
| Keep-awake (`SetThreadExecutionState ES_CONTINUOUS|ES_SYSTEM_REQUIRED`) | dashboard jobs `ns_dashboard/jobs.py:18` (uses `ns_common/keepawake.py:36`); setup run `installer/stage2/web/controller.py:1204,1829` (`web/keepawake.py:32`) | released when the block ends | `ns_common/keepawake.py`, `installer/stage2/web/keepawake.py:15-38` |
| Wake from sleep for the nightly run | task setting `-WakeToRun` | with the task | `ns_supervisor/schedule.py:71`; pre-flight "Wake timers" row `installer/stage1.ps1:418` (`powercfg /q`), `web/preflight.py:27,124` |
| Windows long paths (`HKLM\SYSTEM\CurrentControlSet\Control\FileSystem\LongPathsEnabled`) | read by `stage1.ps1:387-392` and `web/controller.py:258-261`; set (elevated PowerShell) by action `elevate-longpaths` `controller.py:248-255` | not reverted by uninstall | as left |
| Firewall / loopback assumptions | no firewall rule, no `netsh`, nothing binds `0.0.0.0` (grep); every service is `127.0.0.1`; a foreign program on a product port is reported as `NS-E116` | n/a | `ns_common/ports.py:1-12,67-80`; `ns_supervisor/procs.py:213`; `ns_dashboard/security.py:8-35` |
| Single-instance named mutex `Global\Nightshift.AppHost.<role>` | `launchers/nightshift-host.ps1:38,177` | released at exit | as left |
| Single-instance file locks (`msvcrt.locking`): `run\supervisor.lock`, `bootstrap\setup.lock` | `ns_common/singleton.py:63-93`; `ns_supervisor/app.py:196-218`; `installer/stage2/web/controller.py:1822` | OS drops it with the process | as left |
| WebView2 Runtime (Evergreen) detection: registry `...\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}` `pv` (HKLM WOW6432Node, HKLM, HKCU) | not installed by the product; checked by the host (exit 3 -> browser fallback) | n/a | `launchers/nightshift-host.ps1:37,51-70,98-101` |
| WebView2 SDK DLLs (3) in `<home>\bin\webview2` and user-data folder `<home>\state\webview2` | `installer/stage2/downloader.py:617-765`; WebView2 itself | `installer/uninstall.ps1:228,245` | `nightshift-host.ps1:72-81,391` |
| Visual C++ runtime check (msvcp140, vcruntime140, vcruntime140_1) | `ns_common/vcruntime.py:14-28`; `stage1.ps1:400` | not removed (system component) | as left |
| Visual C++ runtime install (elevated, `/install /quiet /norestart`) | `installer/stage1.ps1:902-950` (`Start-Process -Verb RunAs` :934); `installer/stage2/web/bootstrap.py:43,532-553`; winget id in `ns_common/vcruntime.py:17` | not removed | as left |
| winget installs (VC++ runtime, FFmpeg, Windows Terminal, uv) | `installer/stage2/web/bootstrap.py:35-42,478-496`; `installer/stage1.ps1:883` | not removed | as left |
| Mark-of-the-Web stripping on the extracted app | `installer/stage1.ps1:1142-1150` (best effort) | n/a | as left |
| Console title `Nightshift Engine` | `ns_supervisor/app.py:238-240` (`SetConsoleTitleW`) | n/a | as left |
| Windows Terminal / console hidden vs minimized engine window | `launchers/_common.ps1:150-160` (`Minimized` or `Hidden`) | `ns stop` / Stop All | as left |
| Session-0 detection to restart a night-started engine in the user's session | `launchers/_common.ps1:79-98`; `ns_supervisor/core.py` status `session` field (`status()` 584-627) | n/a | as left |
| Process-tree kills (`taskkill /T`, `/F`), `tasklist` image lookup, `netstat` | `ns_supervisor/procs.py:246-280`; `control.py:33-49`; `presence.py:73-85` | n/a | as left |
| Explorer / dialogs: `explorer.exe` (outbox, Desktop, reveal), `OpenFileDialog` (update zip), `FolderBrowserDialog` (`installer/pick-folder.ps1`), `clip.exe` (copy status), `control.exe powercfg.cpl` | `launchers/open-outbox.ps1:9`, `diagnostics.ps1:11`, `launchers/update.ps1:7-8`, `installer/stage2/web/folderdialog.py:21-34`, `controller.py:88,273` | n/a | as left |
| `os.startfile` (open a folder from the dashboard) | `ns_dashboard/adapters.py:896` | n/a | as left |
| Desktop / Downloads folder lookup in the registry `HKCU\...\Explorer\User Shell Folders` | `installer/stage2/web/controller.py:125-130`, `web/pathcheck.py:73-77` | n/a | as left |
| `OneDrive` folder refusal for the install path | `installer/stage2/web/pathcheck.py:98-100` | n/a | as left |
| PowerShell execution-policy bypass on every shortcut / spawn | `s09_shortcuts_scheduler.py:129`; `launchers/nightshift-dashboard.ps1:55`; `launch.py:92-98`; `relaunch.py` | n/a | as left |
| Windows Task Scheduler `ExecutionTimeLimit` 6 h | `schedule.py:73` | with the task | as left |

## 10. Configuration keys


Facts shared by many rows:
- runtime.json is loaded/saved only through `load_runtime` / `save_runtime` (ns_common/config.py:29-36). `save_runtime` call sites: installer/stage2/runtime_cfg.py:70,78 (via `ensure_baseline` / `update_runtime`), ns_supervisor/tune.py:194, ns_supervisor/models_get.py:156, ns_dashboard/svc_settings.py:148.
- "dash-generic" = the Settings page edit path ns_dashboard/svc_settings.py:135-148 (`_save_runtime`) + ns_dashboard/runtime_flags.py (`flatten`, `apply_flags`). It lets the owner edit every plain scalar leaf (str/int/float/bool) except top-level `profile`, `supervisor_port`, `dev_mode` (HIDDEN, runtime_flags.py:14; `profile` can still be sent as body["profile"], svc_settings.py:140). Lists and None-valued fields are neither shown nor edited. It reads generically too (ns_dashboard/svc_engine.py:330 `runtime_summary`); that generic display is not counted as a reader below.
- "default" = `Runtime.default(profile)` (models.py:157-168; cpu-dev: llama.flags n_gpu_layers=0, ctx=8192, threads=4; tts engine=reference, device=cpu; whisper tiny/cpu/int8; video libx264/ultrafast) used by installer/stage2/runtime_cfg.py:`build_runtime` (line 31+). s07_configs.py:144 only calls `ensure_baseline`; it never writes runtime fields.

#### 2a. runtime.json (Runtime, models.py:146-156)

| field | type/default (models.py line) | written by | read by | reader-or-NONE |
|---|---|---|---|---|
| profile | Literal gpu/cpu-dev = "gpu" (147) | runtime_cfg.py:33-34; dash body["profile"] svc_settings.py:140-141 | ns_studio/text/stages_master.py:273; ns_supervisor/core.py:112; ns_supervisor/tune.py:53,60,72; ns_supervisor/models_get.py:144; ns_supervisor/verify.py:428,494 | read |
| llama.host | str "127.0.0.1" (92) | default; dash-generic | ns_supervisor/services.py:82,99,103; `LlamaCfg.base_url` (models.py:101) -> ns_common/acquire.py:54, ns_studio/context.py:70 | read |
| llama.port | int 8080 (93) | default; dash-generic; tune.py:242 (scratch copy, not saved) | ns_supervisor/services.py:81,99,104; installer/stage2/steps/s08_smoke.py:59; dev/run_services_dev.ps1:10,19; base_url users above | read |
| llama.model_path | str "" (94) | runtime_cfg.py:36; installer/stage2/steps/s03_download_models.py:98 (custom GGUF); dash-generic | ns_supervisor/services.py:76; ns_supervisor/tune.py:62,72,204; ns_supervisor/cli.py:156; ns_supervisor/doctor.py:213; dev/run_services_dev.ps1:10 | read |
| llama.story_model_path | str or None, None (95) | ns_supervisor/models_get.py:155 (`ns models get story_llm`) | ns_supervisor/services.py:49,73; ns_supervisor/tune.py:119; ns_studio/cli.py:620 | read |
| llama.aliases | list[str] ["code","story"] (96) | default only (list: not dash-editable) | ns_supervisor/services.py:77; dev/run_services_dev.ps1:11 | read |
| llama.flags (object) | LlamaFlags (97) | runtime_cfg.py:44-49; ns_supervisor/tune.py:193-194 (replaced by sweep winner) | ns_supervisor/services.py:72; ns_supervisor/tune.py:136; dev/run_services_dev.ps1:9 | read |
| llama.flags.n_gpu_layers | int 99 (82) | default; tune.py:144,194; dash-generic | services.py:83,105; dev/run_services_dev.ps1:11 | read |
| llama.flags.n_cpu_moe | int 0 (83) | runtime_cfg.py:45 (8 balanced / 12 best); tune.py:171,194; dash-generic | services.py:89-90; tune.py:62,169; dev/run_services_dev.ps1:12 | read |
| llama.flags.ctx | int 16384 (84) | runtime_cfg.py:49 (32768, tier best); tune.py:159,194; dash-generic | services.py:84; ns_studio/text/stages_master.py:273; ns_dashboard/assistant/loop.py:138; tune.py:157; dev/run_services_dev.ps1:10 | read |
| llama.flags.kv_type | Literal f16/q8_0 "f16" (85) | runtime_cfg.py:44; tune.py:159,194; dash-generic | services.py:87-88; tune.py:157; dev/run_services_dev.ps1:11 | read |
| llama.flags.spec_type | Literal draft-mtp or None, None (86) | runtime_cfg.py:46; tune.py:180,194 | services.py:91-92; dev/run_services_dev.ps1:13 | read |
| llama.flags.spec_draft_n_max | int or None, None (87) | runtime_cfg.py:47 (3); tune.py:180,194 | services.py:92; dev/run_services_dev.ps1:13 | read |
| llama.flags.threads | int or None, None (88) | default (cpu-dev threads=4, models.py:164); dash-generic only while non-null | services.py:96-97 | read |
| llama.story_flags | LlamaFlags or None, None (98) | nothing in code (only tests/test_models.py:71-75 and contracts/CHANGE_REQUESTS.md:170-172) | none; docs/HANDOFF.md:39 also says "Runtime.llama.story_flags read by nothing". Same for its 7 sub-fields (story_flags.n_gpu_layers, .n_cpu_moe, .ctx, .kv_type, .spec_type, .spec_draft_n_max, .threads) | **NONE** |
| llama.build | Literal cuda13/cuda12 or None, None (99) | nothing in code (the llama.cpp pair is stored in config/wizard_flags.json `llama_pair`, s02_download_bins.py:117, not here) | none (grep llama.build, "build", build= finds only unrelated `opts.build()` caption calls and tests) | **NONE** |
| embed.host | str "127.0.0.1" (107) | default; dash-generic | ns_supervisor/services.py:119,123,128; `EmbedCfg.base_url` -> ns_common/acquire.py:56, ns_studio/context.py:73 | read |
| embed.port | int 8081 (108) | default; dash-generic | services.py:118,123,128; s08_smoke.py:59; dev/run_services_dev.ps1:17,19 | read |
| embed.model_path | str "" (109) | runtime_cfg.py:37 | services.py:116; ns_supervisor/doctor.py:213; dev/run_services_dev.ps1:17 | read |
| embed.ctx_size | int 2048 (112) | default; dash-generic | ns_common/config.py:26 `embed_server_args` (-c) -> ns_supervisor/services.py:121 | read |
| embed.batch_size | int 2048 (113) | default; dash-generic | ns_common/config.py:26 (-b, -ub) -> services.py:121; ns_studio/text/stages_master.py:346; ns_studio/text/stages_shorts.py:220; ns_studio/series/stages.py:57; ns_studio/policy/text_checks.py:202 | read |
| tts.host | str "127.0.0.1" (121) | default; dash-generic | ns_supervisor/services.py:144; ns_tts/server.py:406; `TtsCfg.base_url` -> ns_common/acquire.py:58, ns_studio/context.py:72, ns_dashboard/adapters.py:669 | read |
| tts.port | int 8091 (122) | default; dash-generic | services.py:136,141,144; ns_tts/server.py:404; s08_smoke.py:59; dev/run_services_dev.ps1:18,19 | read |
| tts.engine | Literal qwentts/reference/faster/fake "qwentts" (125) | runtime_cfg.py:56 (qwentts, or reference when wizard flag tts_fallback), :58 (fake for answers fake_voice), :63 (qwentts in dev_mode); s02_download_bins.py:155 and s08_smoke.py:275 (reference fallback); cpu-dev default models.py:163 | ns_supervisor/services.py:135,139; ns_tts/engine.py:867; ns_supervisor/models_get.py:144; installer/stage2/web/report.py:194 | read |
| tts.model_dir | str "" (126) | runtime_cfg.py:38 | ns_tts/engine.py:857 (`tts_models_root`) | read |
| tts.device | Literal cuda/cpu "cuda" (127) | default; cpu-dev default models.py:163; dash-generic | ns_supervisor/services.py:144; ns_tts/engine.py:868,871; installer/stage2/web/report.py:195 | read |
| whisper.model | str "distil-large-v3" (135) | runtime_cfg.py:41,51 (absolute models\whisper\<name>); cpu-dev default "tiny" | ns_studio/media/transcribe.py:231; ns_supervisor/verify.py:123-125,439; installer/stage2/whisper_probe.py:45 | read |
| whisper.device | Literal cuda/cpu "cuda" (136) | runtime_cfg.py:41,51; installer/stage2/steps/s08_smoke.py:261 (`_use_whisper`: CPU fallback, or back to CUDA) | ns_studio/media/transcribe.py:227,233; ns_supervisor/core.py:199; verify.py:125,136,439; whisper_probe.py:45; s08_smoke.py:244; installer/stage2/web/report.py:196 | read |
| whisper.compute_type | str "int8" (137) | runtime_cfg.py:41,51; s08_smoke.py:262 | transcribe.py:233; verify.py:125; whisper_probe.py:45; s08_smoke.py:241 | read |
| video.encoder | Literal h264_nvenc/libx264 (141) | s08_smoke.py:266 (`_use_libx264` after an NVENC failure); cpu-dev default (models.py:166); dash-generic | ns_studio/media/render.py:167,213; ns_studio/media/longform.py:285,345; ns_studio/clipper/stages_clip.py:641; ns_studio/media/derive.py:142; installer/stage2/web/controller.py:1725; web/report.py:197 | read |
| video.preset | str "p4" (142) | s08_smoke.py:267 ("veryfast"); cpu-dev default "ultrafast" | ns_studio/media/render.py:168,170; ns_studio/clipper/stages_clip.py:642 | read |
| video.fps | int 30 (143) | default; dash-generic | ns_studio/media/render.py:192,193,196,205; ns_studio/media/longform.py:254 | read |
| supervisor_port | int 8099 (153) | default only (hidden from the dashboard) | ns_supervisor/app.py:236; ns_supervisor/client.py:38; ns_supervisor/core.py:178; ns_dashboard/supervisor_link.py:35; installer/stage2/steps/s08_smoke.py:60; launchers/_common.ps1:48 | read |
| dev_mode | bool False (156) | runtime_cfg.py:62 (`INSTALL.cmd --dev`) | ns_common/devmode.py:29 (raw json); ns_supervisor/services.py:98,122,139,144; ns_supervisor/cli.py:153; ns_supervisor/verify.py:430-504; ns_studio/jobs.py:154; ns_studio/series/workshop.py:1134 | read |

2a count: 33 leaf rows (profile ... dev_mode, counting the `llama.flags` object as its own row and `llama.story_flags` as one row for itself plus 7 rolled-up sub-fields).
NONE in 2a: llama.story_flags (and its 7 sub-fields), llama.build.

#### 2b. install_state.json (InstallState / StepState, models.py:172-185)

Writers of the file: installer/stage2/state.py (`StateStore`, through ns_common/config.py:62-87); installer/stage2/web/controller.py:333-349 `write_step` (raw dict, setup window); installer/stage2/devmode.py:134-165 `reset_for_mode_switch` (raw dict).

| field | type/default (models.py line) | written by | read by | reader-or-NONE |
|---|---|---|---|---|
| version | str "0.1.0" (179) | installer/stage2/web/controller.py:343 (program version, every step); only the default on the console path | ns_dashboard/svc_engine.py:339 (Settings install block, display only) | read |
| app_path | str "" (180) | state.py:31; controller.py:343 | ns_supervisor/control.py:22; ns_supervisor/doctor.py:219; ns_dashboard/assistant/docs_index.py:196; ns_dashboard/svc_engine.py:339; installer/stage2/diagzip.py:55 | read |
| profile | gpu/cpu-dev or None, None (181) | state.py:35-36 (`begin`) | installer/stage2/context.py:118; installer/stage2/verify_downloads.py:91; ns_dashboard/svc_engine.py:339; diagzip.py:54 | read |
| steps | dict[str, StepState] {} (182) | state.py:62 (`mark`); controller.py:339-343; devmode.py:157 | state.py:45,55; controller.py:459-470; diagzip.py:46,58 | read |
| steps.*.status | pending/done/failed/skipped (173) | state.py:62-63; controller.py:340 | state.py:46,55; controller.py:463; diagzip.py:58 | read |
| steps.*.error | str or None (174) | state.py:63; controller.py:340 | installer/stage2/diagzip.py:59 only (install-report) | read (report only) |
| steps.*.finished_at | str or None (175) | state.py:61-63; controller.py:339-340 | controller.py:501; diagzip.py:58 | read |
| started_at | str or None (183) | state.py:33; controller.py:345 | controller.py:513,524; diagzip.py:56 | read |
| completed_at | str or None (184) | state.py:69 (`complete`); devmode.py:159; controller.py:346 | controller.py:513; diagzip.py:57 | read |
| dev_mode | bool False (185) | state.py:32; devmode.py:158 | installer/stage2/devmode.py:148; installer/stage2/verify_downloads.py:99-100; diagzip.py:53 | read |

2b count: 10 rows. NONE: none. Quirk (candidate): the setup-window `write_step` only does `setdefault("profile", None)` (controller.py:344), so `profile` is set only by the console StateStore.begin.

#### 2c. answers.json (Answers / AnswersChannel / LlmCustom, models.py:188-225; InstallerAnswers installer/stage2/answers.py:23-39)

File writers: installer/stage2/steps/s01_questions.py:352 (`write_answers`, keys blanked); installer/stage2/web/answers_view.py:176-215 `draft_to_answers` (setup window) -> controller.py:1290; user-supplied answers.json / installer/answers.example.json; s07_configs.py:166 + answers.py:108 blank the keys again. File readers: answers.py:52 `load_raw`, :66 `build_answers`, context.py:90-100 `ensure_answers`, verify_downloads.py:85-100, diagzip.py:38, ns_supervisor/doctor.py:127.

| field | type/default (models.py line) | written by | read by | reader-or-NONE |
|---|---|---|---|---|
| channel.display_name | str required (189) | s01_questions.py:289-308; answers_view.py:189 | installer/stage2/steps/s07_configs.py:49,132; s01_questions.py:362 | read |
| channel.niche | str required (190) | s01_questions.py; answers_view.py | s07_configs.py:51; s01_questions.py:362 | read |
| channel.language | str "en" (191) | s01_questions.py; answers_view.py | s07_configs.py:50; s01_questions.py:362 | read |
| channel.seed_keywords | list[str] [] (192) | s01_questions.py:303-308; answers_view.py:192 (always []) | s07_configs.py:52 | read |
| voice.mode | preset/design/clone (Voice, models.py:22) | s01_questions.py; answers_view.py:194 | installer/stage2/answers.py:39,76,82; s01_questions.py:311,350,363; s07_configs.py:44,86,91 | read |
| voice.speaker | str or None, "Ryan" | answers_view.py:194; s01_questions.py | s01_questions.py:363; installer/stage2/steps/v01_samples.py:62; via channel yaml: ns_studio/media/voice.py:147, ns_tts/engine.py:203 | read |
| voice.description | str or None | s01_questions.py; answers_view.py | answers.py:82; s07_configs.py:92 | read |
| voice.name | str or None | s07_configs.py:46 (auto name) | s07_configs.py:86-90; s01_questions.py:351; v01_samples.py:62 | read |
| voice.speed | float 1.0 (0.7-1.3) | answers_view.py:195; s01_questions.py | only via the channel yaml: ns_studio/media/voice.py:37 | read (indirect) |
| voice_ref_audio_path | str or None (207) | s01_questions.py:314,342; answers_view.py:196 | answers.py:77; s07_configs.py:95 | read |
| voice_ref_text | str or None (208) | s01_questions.py:315,342; answers_view.py:197 | answers.py:77; s07_configs.py:96 | read |
| voice_rights_confirmed | bool False (209) | s01_questions.py:316,342; answers_view.py:198 | answers.py:77; s07_configs.py:97 | read |
| pexels_api_key | str or None (210) | user answers file / setup window (controller.py:705-713) | s07_configs.py:159-161 (`getattr(ans, key)` -> keyring and secrets.dpapi), :166 | read |
| pixabay_api_key | str or None (211) | same | same | read |
| masters_per_week | int 1 (212) | s01_questions.py:318-319; answers_view.py:201 | answers.py:72; s07_configs.py:54 | read |
| run_time | str "03:00" HH:MM (213) | s01_questions.py:320-325; answers_view.py:202 | answers.py:74; installer/stage2/steps/s09_shortcuts_scheduler.py:139,147,165; s10_summary.py:67,69 | read |
| install_code | bool True (214) | s01_questions.py:327-330; answers_view.py:203 | installer/stage2/steps/s02_download_bins.py:105; s05_opencode_prewarm.py:46; s07_configs.py:137,155,187; s08_smoke.py:221; s09_shortcuts_scheduler.py:51; verify_downloads.py:95 | read |
| allow_cpu | bool False (215) | s01_questions.py:339 (pass-through); answers_view.py:205 | installer/stage1.ps1:1152; installer/stage2/web/controller.py:752 | read (only stage 1 and the setup window act on it) |
| home_path | str or None (217; redeclared answers.py:26) | s01_questions.py:292 | installer/stage1.ps1:824; s01_questions.py:146; web/controller.py:443-444,1850 | read |
| models_source | str or None (218; redeclared answers.py:27) | s01_questions.py:335-338; answers_view.py:184 | installer/stage2/steps/s03_download_models.py:63 -> downloader.py:1001 | read |
| llm_tier | small/balanced/best "small" (219; redeclared answers.py:28) | s01_questions.py:273-283; answers_view.py:186 | installer/stage2/context.py:123; verify_downloads.py:98; web/controller.py:717 | read |
| llm_custom.repo | str regex (199) | answers_view.py:178-181 | runtime_cfg.py:21; downloader.py:692-695; answers.py:85 | read |
| llm_custom.file | str 6-240 chars (200) | answers_view.py | runtime_cfg.py:21; s03_download_models.py:82; downloader.py:692; answers.py:85 | read |
| llm_custom.size | int > 0 (201) | answers_view.py | runtime_cfg.py:21; downloader.py:695; answers.py:88 | read |
| restore_from | str or None (221; redeclared answers.py:29) | s01_questions.py:340; answers_view.py:206 (always None) | s07_configs.py:169-170 (`ns restore`) | read |
| owner_name | str or None, max 40 (223) | answers_view.py:207 | s07_configs.py:68 (-> config/ui_prefs.json) | read |
| ui_theme | light/dark/soft "dark" (224) | answers_view.py:208 | s07_configs.py:68 (-> ui_prefs.json `theme`) | read |
| models_source_kind | internet/folder/drive_zip "internet" (225) | answers_view.py:185 | installer/stage2/web/answers_view.py:226 only (setup UI round trip); no installer step reads it, the behaviour is carried by `models_source` being None or a path | read (UI only; candidate: no behavioural reader) |
| include_voicedesign (InstallerAnswers only, answers.py:32; not in models.py nor answers.schema.json) | bool or None, None | s01_questions.py:332-333; answers_view.py:204 | answers.py:37; s01_questions.py:350; s03_download_models.py:40; verify_downloads.py:99 | read |
| fake_voice (InstallerAnswers only, answers.py:36, added during this survey; not in models.py nor answers.schema.json) | bool False | user answers.json only (setup window never asks) | installer/stage2/context.py:121-124; runtime_cfg.py:57-58; s02_download_bins.py:135; s03_download_models.py:41; verify_downloads.py:100 | read |
| fake_footage (InstallerAnswers only, answers.py; not in models.py's Answers nor answers.schema.json; lands in `Runtime.fake_footage`) | bool False | user answers.json only (setup window never asks) | installer/stage2/context.py (`fake_footage`, false in dev mode); runtime_cfg.py (`build_runtime`); s01_questions.py (notice instead of the no-key warning); s07_configs.py (`apply_engine_test`, every run); ns_common/devmode.py `fake_footage(paths)` for the dashboard; ns_studio/context.py `build_services` | read |

2c count: 28 Answers rows (counting voice.* x5 and llm_custom.* x3) + 3 InstallerAnswers-only rows = 31. NONE: none.

#### 2d. contracts/schemas/*.schema.json vs models.py
- runtime.schema.json: all 34 leaf fields match models.py (defaults, enums incl. tts.engine with "fake"); nothing extra or missing.
- install_state.schema.json: matches models.py (version, app_path, profile, steps{status,error,finished_at}, started_at, completed_at, dev_mode).
- answers.schema.json: matches Answers (required ["channel"]) but lacks `include_voicedesign`, `fake_voice` and `fake_footage` (InstallerAnswers-only fields). Nothing validates against these json files at runtime (models.py:895-905 only exports them); models.py is the validator, so the drift is harmless but real.

#### 2e. Other files under config/ (paths.config) that code reads or writes

| file | keys | written by | read by |
|---|---|---|---|
| config/runtime.json | see 2a | see 2a | see 2a |
| config/install_state.json | see 2b | see 2b | see 2b |
| config/answers.json | see 2c | see 2c | see 2c |
| config/opencode.json | $schema, enabled_providers, provider.local.{npm,name,options.baseURL,options.apiKey,models.code.name}, model, small_model, autoupdate, share (constant OPENCODE_CONFIG) | installer/stage2/steps/s07_configs.py:23-38,155-157 (only when install_code) | no Python reader; consumed by the opencode binary through env OPENCODE_CONFIG: s05_opencode_prewarm.py:23, launchers/nightshift-code.ps1:23. baseURL port 8080 is hard-coded and does not follow runtime.json llama.port (candidate drift) |
| config/dashboard.json | run_time, notifications (DEFAULT_SETTINGS ns_dashboard/state.py:24) | installer/stage2/steps/s09_shortcuts_scheduler.py:76-91 (run_time); ns_dashboard/state.py:133-142 `save_dashboard_settings` (via svc_settings.py:128) | ns_dashboard/state.py:112-130; ns_common/config.py:95-107 `notifications_enabled` <- ns_studio/notifications.py:84, ns_supervisor/update_check.py:88 |
| config/ui_prefs.json | owner_name, theme | installer/stage2/steps/s07_configs.py:64-69 | ns_dashboard/state.py:172-191 `load_ui_prefs` (also accepts alias key ui_theme) <- svc_settings.py:84 |
| config/pending_voice.json | channel, mode, name, description (design) or ref_audio_path, ref_text, rights_confirmed (clone) | installer/stage2/steps/s07_configs.py:77-101 | ns_studio/voice_setup.py:47-50 `read_pending`, :195-214; ns_dashboard/adapters.py:830; deleted by ns_dashboard/fakes.py:371 |
| config/schedule.json | run_time, task_name, mode (registered/interactive_only/disabled/switched_off) | ns_supervisor/schedule_cfg.py:56-65 `write_run_time` <- ns_supervisor/schedule.py:157; removed schedule.py:167 | `read_run_time` <- ns_supervisor/core.py:695, cli.py:178, schedule.py:150,173, ns_dashboard/state.py:100,109; listed in backup.py:23 MACHINE_FILES |
| config/supervisor.json | health_timeout_s | installer/stage2/steps/s08_smoke.py:94 (`write_health_timeout`) | ns_supervisor/app.py:242 (`read_health_timeout`); installer diagzip.py:91 |
| config/update_check.json | last_checked, latest | ns_supervisor/update_check.py:85-86 | ns_supervisor/update_check.py:35-46 `load_state`/`due`; only `last_checked` is read (candidate: `latest` write-only) |
| config/tts_speed.json | rtf, quick, measured_at | ns_common/tts_timeout.py:67-80 `save_rtf` <- ns_supervisor/verify.py:417 | ns_common/tts_timeout.py:83-91 `load_rtf` <- ns_studio/media/voice.py:105; only `rtf` is read (candidate: `quick`, `measured_at` write-only) |
| config/secrets.dpapi | {pexels_api_key, pixabay_api_key: base64 DPAPI blob} | ns_common/secret_file.py:94-98 `store_secret` <- installer/stage2/secrets.py:149, ns_dashboard/adapters.py:622 | `load_secret` <- ns_studio/jobs.py:117, ns_dashboard/adapters.py:609, installer/stage2/secrets.py:128; `stored_names` <- installer/stage2/web/controller.py:48,1812; installer/uninstall.ps1:216 deletes |
| config/wizard_flags.json | llama_pair, cuda, qwentts, tts_fallback, missing_optional, tts_venv_installed, venvs_installed, venvs_version, venvs_app, opencode_local_code, opencode_ok, tuned, smoke, smoke_version, schedule, schedule_mode, aumid, shortcuts_app, samples, bundle, summary_shown, _install | installer/stage2/context.py:83-87 `set_flag` (callers s02:117-148, s03:80, s04:138-140, s05:26,58, s06:30-44, s08:223-224,271, s09:145-181, s10:77,88, v01_samples:52, fallback.py:86) | `ctx.flag(...)` in the same steps; verify_downloads.py:92-93 (llama_pair); web/report.py:183-201; web/controller.py:1068,1751-1803; ns_supervisor/doctor.py:124. Candidate write-only flags: `cuda` (s02:118), `opencode_local_code` (s05:26), `summary_shown` (s10:88) |
| config/channels/<slug>.yaml | Channel model (models.py:44-77): slug, display_name, status, language, niche, seed_keywords, voice_notes, voice{mode,speaker,description,name,speed}, format, masters_per_week, long_minutes, shorts_per_master, shorts_per_day_max, length_buckets, publish_window, audience, made_for_kids, fiction_label, creator_note, critic_bar, qc_pass_target, pools{hooks,structures,pov_tones,pacings,caption_styles,visual_treatments,title_formulas}, avoid_topics, formats_allowed, captions{mask_profanity,remove_fillers}, created_at | ns_common/config.py:50-58 `save_channel` <- installer s07_configs.py:150, ns_studio/channels/crud.py:193,227, ns_dashboard/svc_channels.py:121,134,141, ns_dashboard/fakes.py:598 | ns_common/config.py:44-47 `load_channel` <- ns_studio/channels/crud.py:153,164, ns_dashboard/svc_channels.py:31,40, ns_studio/clipper/cli.py:100,143, ns_studio/series/workshop.py:231, ns_studio/voice_setup.py:190; `list_channels` config.py:61 |
| config/formats/<id>.yaml (user format overrides) | Format model (models.py:729+) | ns_dashboard/formats.py:106-110 `_write` (create :124, update :157) | ns_dashboard/formats.py:72-85 `list_formats` only; ns_studio/media/formats.py:21 reads only the shipped presets/formats, never config/formats (candidate: user formats unused by the studio) |
| config/*.bak, *.bad | backup/quarantine copies of install_state.json | ns_common/config.py:85-87 (keep_backup), :62-82 (quarantine) | config.py:75-82; web/controller.py:320-330 |

2e count: 15 rows (3 are pointers to 2a-2c).

Not under paths.config but related: logs/verify-report.json, logs/install-report.json, logs/tune-*.json (written only by code), data/backup/studio-*.db.

---------------------------------------------------------------------------------------------------

### Row counts and NONE list (from the config pass)


- TABLE 1: 21 database-table rows + 8 mechanism rows (M1-M8) = 29 rows.
- TABLE 2: 2a runtime 33 rows; 2b install_state 10 rows; 2c answers 30 rows (28 + 2 InstallerAnswers-only); 2e other config files 15 rows (12 distinct files beyond the three pointers). Total 88 field/file rows.
- Reader NONE: `llama.story_flags` (plus its sub-fields story_flags.n_gpu_layers, .n_cpu_moe, .ctx, .kv_type, .spec_type, .spec_draft_n_max, .threads) and `llama.build`. Both are also never written by any code. Weak case (a reader exists but only the setup UI): `models_source_kind`.

## 11. Leftover candidates (code-derived; section 12 says what became of each)

| # | candidate | evidence (file:line) |
|---|---|---|
| 1 | Config field `llama.story_flags` (and its 7 sub-fields) is read by nothing and written by nothing | `ns_common/models.py:98`; only `tests/test_models.py:71-75`; `docs/HANDOFF.md:39` agrees; two-model mode takes flags from `llama.flags` (`ns_supervisor/services.py:70-109`) |
| 2 | Config field `llama.build` is read and written by nothing (the llama.cpp pair is stored in `config\wizard_flags.json` key `llama_pair`) | `ns_common/models.py:99`; `installer/stage2/steps/s02_download_bins.py:117` |
| 3 | `schedule.json` key `task_name` is written but never read (readers use the `TASK_NAME` constant) | written `ns_supervisor/schedule_cfg.py:52`; reads of `run_time` / `mode` only (`schedule_cfg.py:27-64`); `uninstall.ps1:34` hard-codes the name a third time |
| 4 | `config\update_check.json` key `latest`, and `config\tts_speed.json` keys `quick`, `measured_at`, are written but only `last_checked` / `rtf` are read | `ns_supervisor/update_check.py:35-46,85-86`; `ns_common/tts_timeout.py:67-91` |
| 5 | User format overrides `config\formats\<id>.yaml` are written by the dashboard but the studio reads only the shipped formats | write `ns_dashboard/formats.py:106-157`; reader `ns_studio/media/formats.py:21` (shipped presets only) |
| 6 | Answers field `models_source_kind` is consumed only by the setup UI round trip; no installer step reads it | `ns_common/models.py:148`; `installer/stage2/web/answers_view.py:185,226` |
| 7 | Supervisor route `POST /events` and `SupervisorClient.publish_event` have no product caller (the studio does not publish job progress through it) | `ns_supervisor/app.py:166`; `ns_supervisor/client.py:124`; only `tests/test_supervisor_client.py:55` |
| 8 | TTS route `GET /v1/voices/{name}/embedding` has no product caller | `ns_tts/server.py:358`; tests only |
| 9 | Dashboard route `GET /api/uploads/{upload_id}` is called only from a test | `ns_dashboard/server.py:338`; `tests/test_dashboard_api.py:330` |
| 10 | Dashboard SSE `job` frames (and hub `dashboard_job`) have no UI consumer: `app.tsx` handles only `kind==="status"` and polls status regardless; the SSE `job` frame is produced but not read | `ns_dashboard/ui/src/lib/app.tsx:84`; `ns_dashboard/events.py:104-109` |
| 11 | Supervisor event kinds `update` and `run` are relabelled `service` by the dashboard relay, so nothing downstream can tell them apart; no consumer of either found | `ns_dashboard/supervisor_link.py:161`; publishers `ns_supervisor/core.py:684,723` |
| 12 | `Paths.clips_index` (`cache\clips\index.sqlite`) is never opened or created; the clip index is the `clips` table of `studio.db` | `ns_common/paths.py:106`; `ns_studio/media/clips.py:114` |
| 13 | `Paths` folders created by `ensure()` that nothing reads: `models\cv` (`models_cv`), `bin\uv` (`bin_uv`, bootstrap uses a literal `home/bin/uv`), `stories` (outbox writes `outbox\<slug>\stories` instead) | `ns_common/paths.py:40,58,86,117-125`; `installer/stage2/web/bootstrap.py:424,602`; `ns_studio/media/outbox.py:114` |
| 14 | `Paths.exemplars` and `Paths.prompt_overrides` are read but not in `all_dirs()`, so `ensure()` never creates them | `ns_common/paths.py:88,90,117-125`; `ns_studio/text/exemplars.py:56`, `text/prompts.py:222` |
| 15 | DB tables written but never read (channels, calibration, assistant_messages outside its self-test), a table read but never written (`parts`), `sources` nearly write-only, `ideas.setting/protagonist/keywords/updated_at` probed but never created, no `PRAGMA user_version` | see section 6 flags; `contracts/schema.sql` |
| 16 | Dashboard port 8100 is a constant in four places and not a `runtime.json` key: `ns_dashboard/__init__.py:5`, `ns_supervisor/services.py:18`, `launchers/nightshift-dashboard.ps1:11`, `installer/stage2/web/controller.py:194` (plus `dev/gate_m3_dashboard.py:27`) | listed in section 2 |
| 17 | Supervisor port 8099 fallback literal in four places: `ns_supervisor/client.py:23`, `ns_dashboard/supervisor_link.py:24`, `launchers/_common.ps1:50`, `contracts/schemas/runtime.schema.json:353` | section 2 |
| 18 | `config\opencode.json` bakes `http://127.0.0.1:8080/v1`; if `llama.port` is changed in `runtime.json` the OpenCode config is stale (written once by s07) | `installer/stage2/steps/s07_configs.py:30`; `ns_common/models.py:93` |
| 19 | TTS port fallback literal `http://127.0.0.1:8091` in the dashboard voice adapter | `ns_dashboard/adapters.py:671` |
| 20 | `NS_SUPERVISOR_URL` is honoured only by the dashboard's Python link; every PowerShell launcher and the `ns` CLI build the URL from `runtime.json` | `ns_dashboard/supervisor_link.py:30`; `launchers/_common.ps1:53-55`; `ns_supervisor/client.py:37` |
| 21 | Env vars read but never set by the product: `NS_DEVTOOLS`, `NS_FFMPEG`, `NS_FFPROBE`, `NS_LOG_STDERR`, `NS_SUPERVISOR_URL`, `NS_TTS_FAKE` (dev installs pass `--engine fake` instead), `NS_NONINTERACTIVE` for stage 1 | section 4 |
| 22 | `NS_DASHBOARD_NO_HOST` is scrubbed only by the update relaunch; other children inherit it | `ns_supervisor/relaunch.py:31`; `ns_supervisor/client.py:138` removes only `NIGHTSHIFT_UNDER_ENGINE` |
| 23 | The constant `NIGHTSHIFT_HOME` is defined twice in the installer (`HOME_VAR`) and spelled as a literal elsewhere; `KeepAwake` is duplicated on purpose (`ns_common/keepawake.py` vs `installer/stage2/web/keepawake.py`, stdlib-only rule) | `installer/stage2/winenv.py:14`; `installer/stage2/web/bootstrap.py:33` |
| 24 | Two top-level folder lists must agree: `Paths.all_dirs()` and the installer's `TOP_DIRS` | `ns_common/paths.py:117-125`; `installer/stage2/web/bootstrap.py:34` |
| 25 | Pexels / Pixabay key validation is implemented twice (httpx in the wizard, urllib in the setup server) with different user-agents; the API URLs are hard-coded in three files | `installer/stage2/steps/s01_questions.py:53-78`; `installer/stage2/web/keys.py:24,40-61`; `ns_studio/media/clip_sources.py:25-26` |
| 26 | Voice-engine source mismatch: the manifest pins release `v0.1.4` with a sha256, while `s02_download_bins.py` falls back to unpinned `releases/latest/download` URLs without a hash when the pin is null | `manifest.json:175-179`; `installer/stage2/steps/s02_download_bins.py:35-42,80-91,143-148` |
| 27 | Pre-flight network probe lists 7 hosts but the run also needs `api.nuget.org`, `download.visualstudio.microsoft.com`, `api.github.com`, Pexels / Pixabay CDNs | `installer/stage1.ps1:52-55` vs section 8 |
| 28 | Folders left behind by an update: `app.failed`, `app.swap` and the scripts `*.old` copies are not removed by the uninstaller (it removes `app`, `app.new`, `app.prev`) | `ns_supervisor/update.py:49,271,343,370-374`; `installer/uninstall.ps1:232` |
| 29 | CLI commands not reachable from a launcher, installer step, UI or the docs listed: `ns notify` (nightly toasts go through `ns_studio/notifications.py`, not this command), `ns update --git` / `--rollback`; `studio ask` is a placeholder when the assistant CLI cannot be imported | `ns_supervisor/cli.py:390-393,229-232`; `ns_studio/cli.py:80-92` |
| 30 | `launchers/nightshift-studio.ps1` accepts any studio command from a prompt, so all `studio` subcommands (channel, voice, review, series, clip, learn, calibrate, ...) are reachable only that way or from the scheduled task / dashboard jobs | `launchers/nightshift-studio.ps1:13-18`; `ns_studio/cli.py:58-61,138-595` |
| 31 | The host command `open-external` is handled by the host and sent by the setup UI but is missing from the dashboard's typed `HostCommand` | `launchers/nightshift-host.ps1:486-488`; `installer/ui/src/host.ts:10`; `ns_dashboard/ui/src/lib/host.ts:5-11` |
| 32 | `api.fileUrl` in the setup UI has no caller outside the demo transport (the three media URLs come from state) | `installer/ui/src/api/client.ts:84` |
| 33 | A dev-mode `tts.engine` value `faster` is a legacy alias kept in the Literal; the supervisor treats anything but `reference` as the qwentts venv | `ns_common/models.py:121-123`; `ns_supervisor/services.py:135` |
| 34 | TODO / FIXME / XXX / HACK markers in the product sources: none found (grep over `*.py`, `*.ps1`, `*.cmd`, `*.ts`, `*.tsx`, `*.json`, excluding bundles, docs and tests) | grep result: no matches |

## 12. What became of the leftover candidates (2026-11-08/09)

| # | Candidate | Disposition |
|---|---|---|
| 1 | `llama.story_flags` read and written by nothing | **Later, deliberate.** Two-model mode takes `llama.flags` for both models (`services._llama`). Removing the field would reject every installed `runtime.json` that carries it (`Strict` models); giving it meaning is a tuning feature for the GPU PC after the first real nights. Kept, documented here and in `docs/HANDOFF.md`. |
| 2 | `llama.build` unused | **Later**, same reason as 1 (the llama.cpp pair lives in `wizard_flags.json` `llama_pair`). |
| 3 | `schedule.json` `task_name` written, never read | **By design.** The name is a constant; the file carries it for a human reading the folder. |
| 4 | `update_check.json` `latest`, `tts_speed.json` `quick` / `measured_at` written, not read | **By design** (diagnostic fields; the diagnostics zip ships the files). |
| 5 | Format overrides in `config\formats` never read by the studio | **Fixed (PR #93):** `ns_studio.media.formats.load_formats` overlays `<home>\config\formats\*.yaml` on the shipped presets; `Paths.formats_dir` is the one definition of the folder. Test: `tests/test_formats.py`. A real defect: Library > Formats edits never reached a render. |
| 6 | `models_source_kind` consumed only by the setup UI | **By design** (a UI-side choice; the installer reads `models_source`). |
| 7 | Supervisor `POST /events` without a product caller | **Kept.** The client method and route are the hook for studio progress events; `tests/test_supervisor_client.py` covers them. |
| 8 | Voice `GET /v1/voices/{name}/embedding` without a caller | **Kept** (debug route of the voice service, covered by its tests). |
| 9 | `GET /api/uploads/{upload_id}` called only from a test | **Kept** (the upload status probe the UI will need for resumable uploads). |
| 10 | Dashboard SSE `job` frames without a UI consumer | **Later.** The UI polls `/api/jobs`; the frames are correct and cheap. A UI change needs the rebuilt `ns_dashboard/static`. |
| 11 | Supervisor `update` / `run` events relabelled `service` by the relay | **Later**, with 10 (one UI round for events). |
| 12 | `Paths.clips_index` never used | **Fixed (PR #93):** removed. |
| 13 | Folders created but read by nothing (`stories`); literals instead of `Paths` attributes for `models\cv`, `bin\uv` | **Fixed in part (PR #93):** `stories` removed from `Paths` and `ensure()`. The two literals stay (the installer's bootstrap is stdlib-only by rule and cannot import `Paths`). |
| 14 | `exemplars` and `prompt_overrides` never created by `ensure()` | **Fixed (PR #93):** both in `all_dirs()`; `tests/test_paths.py`. |
| 15 | Write-only / read-only tables | **By design** for `channels` (the YAML is the source, the row the index), `calibration` (its own command reads it), `assistant_messages` (read by the self-test and the export). **Later** for `parts`: the dashboard reads it (`svc_outputs.py`) and no code writes it (confirmed by a second grep); either the series produce path should fill it or the table and its reader go. No `PRAGMA user_version`: migrations are idempotent `CREATE IF NOT EXISTS`. |
| 16, 17, 19 | Port literals 8100 (four places), 8099 fallback (four), 8091 fallback (one) | **By design, documented.** PowerShell launchers and the stdlib-only setup server cannot import `ns_common`; the Python copies are the documented fallbacks for a missing `runtime.json`. The authoritative values are `Runtime.supervisor_port`, `DASHBOARD_PORT` (`ns_dashboard/__init__.py`), `TtsCfg.port`. |
| 18 | `opencode.json` bakes `127.0.0.1:8080` | **Later.** `llama.port` is never changed by the product; a Settings change would need `s07`'s writer re-run. Noted in `docs/HANDOFF.md` gaps. |
| 20 | `NS_SUPERVISOR_URL` honoured only by the dashboard link | **By design** (a test hook for the dashboard; launchers and the CLI read `runtime.json`). |
| 21 | Env vars read but never set by the product | **By design**: `NS_TTS_FAKE`, `NS_DEVTOOLS`, `NS_FFMPEG`, `NS_FFPROBE`, `NS_LOG_STDERR` are developer and support switches (`docs/DEV_MODE.md`, `OPERATOR.md`). |
| 22 | `NS_DASHBOARD_NO_HOST` inherited by children | **Later** (harmless: only the dashboard launcher reads it; the update relaunch already scrubs it). |
| 23, 24 | Duplicated constants (`HOME_VAR`, `KeepAwake`, `TOP_DIRS` vs `all_dirs()`) | **By design** (stdlib-only setup server rule); `tests/test_uv_shim.py` keeps `TOP_DIRS` and `all_dirs()` in step. |
| 25 | Key validation twice (httpx wizard, urllib setup server), URLs in three files | **Later** (one `ns_common.footage_hosts` module when the next key provider is added; both paths are **V-dev** with the owner's Pixabay key). |
| 26 | Unpinned `releases/latest` voice-engine fallback when the pin is null | **By design**: the fallback exists for a bundle older than its binary; the shipped manifest pins v0.1.4 with a SHA256, so the path is dormant (warned `NS-W301` when taken). |
| 27 | Pre-flight probe lists 7 hosts, the run needs 10 | **Fixed (PR #93):** `api.github.com`, `api.nuget.org`, `download.visualstudio.microsoft.com` added to `installer/stage1.ps1` `NetworkHosts` (warning rows, not critical). |
| 28 | `app.failed`, `app.swap` not removed by the uninstaller | **Fixed (PR #93):** `installer/uninstall.ps1`. The `*.old` entry points live under `venvs\`, which uninstall removes. |
| 29, 30 | CLI commands reachable only from a prompt (`ns notify`, `ns update --git/--rollback`, `studio <subcommand>`) | **By design**: operator commands documented in `OPERATOR.md`; the launcher `Nightshift Studio` is the prompt. |
| 31 | `open-external` missing from the dashboard's typed `HostCommand` | **Later** (TypeScript type only; the dashboard never sends it; needs a UI rebuild). |
| 32 | `api.fileUrl` in the setup UI unused outside the demo | **Later**, with the next setup-UI round. |
| 33 | `tts.engine` value `faster` kept as a legacy alias | **By design** (installed homes from 0.1.0 carry it; `select_engine` maps it to qwentts). |
| 34 | TODO / FIXME markers | None in the product sources (grep). |

Two more things came out of the engine-test run itself (2026-11-08/09):

- **Clip loudness (defect, fix on `orchestrator/clip-loudness`).** The clipper's two-pass loudnorm cannot reach
  -14 LUFS on a quiet, peaky source (measured: input -20.4 LUFS at -0.47 dBTP; FFmpeg falls back to dynamic
  mode and lands at -17.1), and the QC gate then fails every clip of that source with `NS-E404` and no remedy
  for the owner. The render needs a peak-control stage before loudnorm when linear mode is impossible.
- **One user variable per Windows user (by design, documented).** Setup writes `NIGHTSHIFT_HOME` as a user
  environment variable and uninstall removes it. Two installs under one Windows user (the laptop's dev install
  and the engine test) therefore share and overwrite it; the launchers are immune (they resolve the home from
  their own folder) but a bare `ns` or `studio` in a new console follows the last install. Supported use is one
  install per PC; keep it so.

Two defects outside this list came from reading the same wiring on 2026-11-08 and are in PR #92: a real
install on a PC without an NVIDIA card wrote `tts.engine: reference` and so pointed its voice service at the
PyTorch venv that only the fallback creates; and `ns verify` on that profile failed the encoder and idle-VRAM
rows for want of `nvidia-smi`.
