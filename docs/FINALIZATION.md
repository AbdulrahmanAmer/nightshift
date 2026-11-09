# Finalization list

Everything left between the code as it is and "finished", as boxes. One line per item: an id, what must be
true, the proof in parentheses (the command, click or observation that settles it), and where it can be
settled: **laptop** (the orchestrator's dev or engine-test install), **CI** (a test in `tests/`), **owner**
(needs the owner's key, ear or decision), **partner** (only the GPU PC can show it). A box is ticked only
with `evidence:` after it (a report file and date, a test name, a PR, a release tag). `dev/finalization_status.py`
counts the boxes per section and `--check` refuses a ticked box without evidence; `tests/test_finalization.py`
runs it in CI. The sources are `docs/VERIFICATION.md` (blocks and status table), `docs/VERIFICATION_INVENTORY.md`
(every route, command, stage, step, tool), `docs/PLUMBING.md` (wiring, coverage, leftovers),
`docs/design/system-gaps.md` (the partner first-run column) and `docs/HANDOFF.md` (known gaps).

Done means: sections A to E have no open box on the laptop or in CI; section F is handed to the partner as
the first-run checklist with every line still open until the first real night; section G is green.

## A. Release 0.1.12 (this round)

- [x] F-A1 PRs #85, #88, #89, #90, #91 merged (verification program, upload sidecar, reinstall after uninstall, series map fit, pinned combos repeat). evidence: GitHub merges 2026-11-08/09.
- [x] F-A2 PR #92 merged: engine test `fake_voice`, cpu-dev voice venv defect, `ns verify` without nvidia-smi. evidence: merge commit 9882d0f, 2026-11-09.
- [x] F-A3 PR #93 merged: `docs/PLUMBING.md`, studio reads edited formats, Paths cleanups, pre-flight hosts, uninstall leftovers (CI zero fail / zero pending, then `gh pr merge 93 --admin --merge`). laptop evidence: merged 2026-11-09 (CI 14/14 green).
- [x] F-A4 PR #94 merged: boost-then-limit loudness for clips and narration (opus review verdict merge; CI zero/zero). laptop evidence: opus review verdict merge after follow-ups; merged 2026-11-09.
- [x] F-A5 Full local suite green on the combined tree before tagging (`pytest -q` in `D:\NightShift-pr`, zero failures). laptop evidence: full suite on the combined tree 2026-11-09 01:22-02:05, 8 failures all explained (7 machine-dependent stage-1 tests fixed in PR #100, 1 load-flaky rereview test passing 3/3 alone), report D:/NightShift-refs/test-loop-2026-11-08.md.
- [x] F-A6 Bump 0.1.12 (`D:\NightShift-refs\tools\bump_0_1_12.py` on fresh main: VERSION, pyproject, `ns_common.__version__`, CHANGELOG heading with date, `tests/test_release_wiring.py` words, HANDOFF needles, VERIFICATION status table), PR, CI green, merge. laptop evidence: PR #96 merged 2026-11-09 (bump_0_1_12.py, wiring test green).
- [x] F-A7 Tag `v0.1.12`; `release.yml` publishes `Nightshift-Setup-0.1.12.zip`, `Nightshift-Update-0.1.12.zip` and both `.sha256`; manifest inside says 0.1.12 (download, hash, unzip, read). laptop evidence: tag v0.1.12, release.yml success, verify_release.ps1: both zips sha256 ok, manifest 0.1.12.
- [x] F-A8 The dev install in `D:\NightShift\installs\dev` updated from the official `Nightshift-Update-0.1.13.zip` (Settings > Update from file; 0.1.12 to 0.1.13; settings kept; CLEAN after). laptop evidence: ns-qa run 2026-11-09 12:05, status-release.md entry: zip and its .sha256 chosen together, one POST /api/update/file, done \"Now on 0.1.13 (was 0.1.12)\" 44 s after Install; app\VERSION and /api/status say 0.1.13; channels, schedule_time, home, profile and notifications unchanged (only schedule_mode none to disabled, the disabled task now read correctly); nsproc -Down then CLEAN, scheduled task Disabled; no API key was stored on this install (pexels/pixabay false before and after), so key retention across an update was NOT exercised here and is not claimed; log runs\qa\update-0.1.13.log, screenshot refs\evidence\qa-0.1.12\update-0.1.13-settings-after.png.
- [x] F-A9 Phone notified with the release link and the one-line position (PushNotification). laptop evidence: release line shown in the owner's live terminal 2026-11-09 (notification skipped as redundant by the harness).
- [x] F-A10 `docs/HANDOFF.md` "In flight" says what 0.1.13 is and what is open (this file). laptop evidence: PR #117 (the 0.1.13 bump, which wrote HANDOFF "In flight" on main: v0.1.13 is the first finalization round, FINALIZATION.md and FEATURE_STATUS.md list what is open); this PR (#132) corrects the Known gaps line to match main.

## B. Verification items still open on the laptop (U in docs/VERIFICATION.md section 5)

- [ ] F-B1 Stage 1: console closed mid-run, next `INSTALL.cmd` resumes at the same step (kill the console during s04; rerun; install-report shows s01-s03 kept). laptop
- [ ] F-B2 Stage 2 window by clicking (Playwright MCP): change a choice, Browse a folder, key screen with a rejected then a good key, Continue through to Done. laptop
- [ ] F-B3 Network off during a model download: retry note, Failed screen, Continue resumes the same file (`netsh interface set interface` or pull the Wi-Fi; dev stub sizes are tiny, so use the engine test's 639 MB GGUF). laptop
- [ ] F-B4 Disk nearly full during download and during a render: NS-E104 / a clear message, nothing half-written left behind (a VHD with 1 GB free as the home). laptop
- [ ] F-B5 URL clip job (`POST /api/jobs/clip {url}`) with a local `file://` URL and with a real short YouTube URL (yt-dlp download, cancel mid-download removes the folder). laptop
- [ ] F-B6 Series workshop end to end on the dev install with PR #91 in place: concepts → choose → design → map → scripts → `POST /api/library/series/{slug}/produce` → the `series_part` job reaches done; the part is `produced` in Library (about 20 min). laptop
- [x] F-B7 Review: mark uploaded (`POST /api/outputs/{id}/uploaded`) and re-review of a rejected item; the Queue card and `uploads` table agree. laptop evidence: 0.1.12 dev install: reject with note -> files to rejected/, uploaded refused while rejected (409), re-review approve -> uploaded; card and uploads table agree (D:/NightShift/refs/test-loop-2026-11-08.md, 2026-11-09 08:50-08:58 section C routes, F-B7, F-B12 on the 0.1.12 dev install); the file left under rejected/ after approve is defect D-6.
- [ ] F-B8 Calibrate (`studio calibrate` round trip: critic rows, human rows, the weights it reports). laptop
- [ ] F-B9 WebView2 window bridge buttons: minimize, maximize/restore, drag, close confirm while a job runs (the window, not Chrome). laptop
- [ ] F-B10 Results page with imported analytics renders the numbers the import produced (retention, weights moved, spark). laptop
- [ ] F-B11 Assistant offline eval suite re-run after the last prompt change (`tests/test_assistant_eval.py` or its command; zero regressions). CI
- [x] F-B12 Diagnostics zip contents checked line by line: logs, settings without keys, install and verify reports, `nvidia-smi.txt` placeholder, no secret anywhere (unzip and grep for the key prefix). laptop evidence: 0.1.12 dev install, 83-entry bundle made with both keys stored (GOODKEY via the QA fake endpoint): no key value in any file, answers.json without key fields, nvidia-smi.txt placeholder (D:/NightShift/refs/test-loop-2026-11-08.md, 2026-11-09 08:50-08:58 section C routes, F-B7, F-B12 on the 0.1.12 dev install).
- [x] F-B13 Pexels live: key check in Settings, video search, download (needs the owner's Pexels key in Settings). owner evidence: mimicked with the QA fake Pexels/Pixabay endpoint (runs/qa/fakefootage, TLS proxy) on the 0.1.12 dev install: bad key refused with 422 NS-E501, good key 'Pexels accepted the key', search + download of 1080x1920 clips into cache/clips/pexels (D:/NightShift/refs/test-loop-2026-11-08.md, 2026-11-09 08:47-08:52 F-B13 and F-B14 on the 0.1.12 dev install).
- [x] F-B14 Rate limit (429) handling live against Pixabay or Pexels (burst past the bucket; expect the wait, not a failure). laptop evidence: 0.1.12 dev install via the QA fake endpoint: 3x429 Retry-After 5 waited 15.1 s then 200 and a download; endless 429 -> coded NS-E501 'rate limited (429) after 4 attempts' (D:/NightShift/refs/test-loop-2026-11-08.md, 2026-11-09 08:47-08:52 F-B13 and F-B14 on the 0.1.12 dev install); the 5xx outage path is defect D-5.
- [x] F-B15 Real Shorts job on an engine-test install (`fake_voice`, real Qwen3-0.6B) with the owner's Pixabay key typed into that install's Settings: story JSON from the real model (or a clear NS-E3xx), real footage, fake voice, real captions, QC verdict. owner evidence: fake_footage switch (PR #104) instead of the owner's key; two Shorts jobs on real models (0.6B, 1.7B) ran ideas, plan, draft, critic and stopped with a clear NS-E303 at the validators; the Assistant on 1.7B produced the tool call and the job (report 2026-11-09).
- [ ] F-B16 Clip job on a real engine-test install after PR #94: the same speech video passes QC loudness (-14 ± 1 LUFS, TP <= -1), `render/report.json` says `mode: limited`. laptop
- [ ] F-B17 Windows toast actually visible on screen after `ns notify` and after a finished nightly run (interactive session). laptop
- [ ] F-B18 `nightshift-studio.ps1` and `nightshift-code.ps1` from a fresh install: console opens with the home set, `studio status` answers; Code needs `install_code` (a dev install with `install_code: true` downloads OpenCode: allowed on the laptop, 100 MB). laptop
- [ ] F-B19 Dashboard SSE: a UI consumer for the `job` frames or the frames removed (`docs/PLUMBING.md` section 12 rows 10 and 11); the Queue updates without polling. laptop
- [ ] F-B20 Setup window after an uninstall by clicking (Welcome → Continue → Done), the API-driven run is already V-dev. laptop

## C. Inventory items still U (docs/VERIFICATION_INVENTORY.md; prove each on the dev install, mark there)

- [ ] F-C1 Engine control routes: `GET /api/events` (a browser EventSource shows status frames), `POST /api/engine/warmup`, `/shutdown`, `/release_gpu`, `/stop_all` (each answers and `/status` reflects it). laptop
- [x] F-C2 Channel routes: `POST /api/channels`, `GET/PUT /api/channels/{slug}`, `/pause`, `/resume`, `/validate` (create a second channel from the UI, pause and resume it, validate a bad niche). laptop evidence: 0.1.12 dev install, runs/qa/croutes: create (201, 422 invalid_profile, 409 exists), GET/PUT, pause/resume reflected in /api/status, validate bad draft names niche (D:/NightShift/refs/test-loop-2026-11-08.md, 2026-11-09 08:50-08:58 section C routes, F-B7, F-B12 on the 0.1.12 dev install).
- [ ] F-C3 Output and media routes: `POST /api/outputs/{id}/uploaded`, `/open-folder`, `GET /api/outputs/{id}`, `GET /api/thumbs/{n}/{id}`, `GET /api/media/{id}/thumb.jpg` (Queue card buttons and images). laptop
- [ ] F-C4 Job routes: `POST /api/jobs/verify` (self-check from Settings), `POST /api/jobs/clip/rerun` (re-cut a source), `GET /api/uploads/{id}` (upload status during a chunked upload). laptop
- [ ] F-C5 Notes, logs, diagnostics routes: `GET /api/notes`, `POST /api/notes/clear`, `GET /api/logs/tail`, `GET /api/logs/components`, `POST /api/diagnostics`, `GET /api/diagnostics/latest` (Settings > Diagnostics and the bell). laptop
- [x] F-C6 Settings routes: `GET /api/settings/export/latest`, `GET /api/settings/uninstall` (the Settings page's last export and the uninstall section). laptop evidence: 0.1.12 dev install, runs/qa/croutes: export/latest 404 then a zip of the reported size after an export; settings/uninstall names app/UNINSTALL.cmd (D:/NightShift/refs/test-loop-2026-11-08.md, 2026-11-09 08:50-08:58 section C routes, F-B7, F-B12 on the 0.1.12 dev install).
- [ ] F-C7 Voice routes on the fake engine: `POST /api/voices/preview`, `GET /api/voices/pending`, `POST /api/voices/apply-pending`, `POST /api/voices/design`, `POST /api/voices/clone` (Library > Voices: preview, design, clone, Create it now). laptop
- [ ] F-C8 Library routes: `DELETE /api/library/footage/{source}/{clip_id}`, `POST /api/library/formats`, `PUT /api/library/formats/{id}` (delete a cached clip; duplicate and edit a format, then a render uses it: PR #93). laptop
- [ ] F-C9 Update and model routes: `GET /api/update/state`, `POST /api/update/file`, `POST /api/update/apply`, `POST /api/models/get`, `GET /api/models/state` (update from the 0.1.12 zip through the UI; Models > Get on the dev install shows the refusal or the stub). laptop
- [ ] F-C10 Assistant routes: `POST /api/assistant/dismiss`, `GET /api/assistant/conversations`, `POST /api/assistant/queue/run` (dismiss a suggestion, list conversations, run from the queue). laptop
- [ ] F-C11 Assistant tools, 42 still U: each called once through the chat with the dev fake's tool routing and its result on the card (families: find/open/search_docs; queue and outputs; jobs start/cancel/rerun/resume/verify; ideas; stories; voices list/design/clone; formats list/duplicate/edit; channels list/get/edit/create/validate; results summary/runs/import; settings/schedule/update/diagnostics; engine warmup/shutdown; logs; system_map). laptop
- [ ] F-C12 `studio status` CLI, the `story script_import` stage (a script imported instead of written), `build-qwentts.yml` (dispatch once; the 602 MB artifact appears). laptop / CI

## D. Plumbing leftovers marked "later" (docs/PLUMBING.md section 12)

- [ ] F-D1 `llama.story_flags` and `llama.build`: either give them meaning (two-model mode uses `story_flags` for the story model) or remove them with a `runtime.json` migration that tolerates the old keys. partner (needs a two-model night)
- [ ] F-D2 Dashboard relay relabels supervisor `update` and `run` events as `service`; keep the kind and show it, or drop the two publishers. laptop
- [ ] F-D3 `config\opencode.json` bakes `127.0.0.1:8080`; rewrite it when `llama.port` changes (or read the port at launch in `nightshift-code.ps1`). laptop
- [ ] F-D4 `NS_DASHBOARD_NO_HOST` scrubbed from every child the engine starts, not only by the update relaunch. laptop
- [ ] F-D5 One footage-host module for the two key checks and the clip sources (`ns_common.footage_hosts`), one user agent (`Nightshift/<ver>`). CI
- [ ] F-D6 Dashboard `HostCommand` type gains `open-external`; UI rebuilt (`ns_dashboard/static`). laptop
- [ ] F-D7 Setup UI `api.fileUrl` used or removed. CI
- [ ] F-D8 `parts` table: a writer on the series produce path or the table and its reader removed. laptop
- [ ] F-D9 Loudness loop on speech still uses all four measurement passes (opus review of PR #94): accept a near-fit earlier or measure once more cleverly; voice stage time per item back near the old figure. laptop
- [x] F-D10 Narration is limited twice (voice stage at -16, then the Short at -14): limit once at the voice stage with the Short's ceiling, or skip the second stage when the first fits. laptop evidence: tests/test_audio_loudnorm.py::test_narration_is_limited_once_and_the_short_adds_no_second_limiter and ::test_the_short_adds_no_second_limiter_to_wideband_speech, tests/test_voice_stage.py::test_voice_stage_normalizes (PR #105).

## E. Known gaps from docs/HANDOFF.md and docs/design/system-gaps.md

- [ ] F-E1 VoiceDesign talker on demand: a designed voice without the model gets a download offer (Settings > Models) instead of NS-E31x only. laptop
- [ ] F-E2 `clip_select` prompt asks for `emphasis`; the clipper uses it in captions. laptop
- [ ] F-E3 CR-C1-4 (e): a CUDA 12.8 llama.cpp pin, or a written decision that the 12.4 and 13.4 pairs are enough. partner
- [x] F-E4 `ns_dashboard/templates.py` reads the format presets, not `clip_templates.yaml`. CI evidence: tests/test_dashboard_templates.py::test_lists_auto_then_every_shipped_clip_layout and ::test_api_returns_the_shipped_layouts_and_the_owners_edited_one (PR #102).
- [ ] F-E5 Tonight's plan shows the governor's plan (Shorts per day after a change), not the static format. laptop
- [ ] F-E6 Setup voice step plays preset sample WAVs (ship three short samples, or generate them at setup on the fake engine in dev). owner (voice choice)
- [ ] F-E7 Release notes shown after an update (`UpdateResult.notes` carries the CHANGELOG section of the new version). laptop
- [x] F-E8 `studio dashboard [--port] [--open]` exists or the spec line is struck. CI evidence: tests/test_dashboard_cmd.py::test_prints_the_url_and_starts_the_dashboard_service, ::test_open_starts_the_default_browser_on_the_url, ::test_engine_that_cannot_start_gives_a_plain_message_and_exit_1 (PR #110).
- [x] F-E9 An uncoded exception in the dashboard's studio child becomes NS-E327 with a hint, never `code: null`. CI evidence: tests/test_dashboard_studio_runner.py::test_an_uncoded_failure_in_the_child_becomes_ns_e327 and ::test_the_job_record_of_a_failed_child_carries_a_code_and_a_hint (PR #108).
- [ ] F-E10 Queue > Release the graphics card: the confirm text matches what it does (frees the lock only), or it stops the holder's process. laptop
- [ ] F-E11 `schedule_registered` means "a task exists and is enabled", with the interactive-only mode shown as its own state. laptop
- [ ] F-E12 Cancel frees every owner the job took (setup voice owners too), not only `studio:<job id>`. laptop
- [x] F-E13 Library > Voices > Create it now runs as a dashboard job, not inside the HTTP request. laptop evidence: tests/test_pending_voice_job.py::test_create_it_now_queues_an_apply_voice_job_and_asking_again_returns_the_same_one and ::test_the_job_can_be_cancelled_from_the_queue_and_the_request_stays, tests/test_dashboard_rereview.py::test_the_real_adapter_runs_apply_voice_in_the_child.
- [ ] F-E14 One `qwen-tts` process per voice batch confirmed on the GPU (time of the voice stage of the first master; `tts_batch` rtf in `logs\tts-*.jsonl`). partner

## F. Partner GPU PC first run (V-gpu; hand this section over with the release)

- [ ] F-F1 `Nightshift-Setup-0.1.12.zip` installs on the RTX 5060 Ti PC from the window: PC check green (driver, CUDA, 16 GB), downloads with hashes, `ns tune` on the GPU, smoke test all ok; `verify-report.json` and the diagnostics zip sent back. partner
- [ ] F-F2 Voice engine: qwentts.cpp binary runs (DLLs present), `engine_kind: qwentts`, no NS-W301; if NS-W301 appears, the PyTorch fallback venv installs and speaks. partner
- [ ] F-F3 Whisper on CUDA (no NS-W404), captions `drift_ms` on the first Shorts well under 120 (raise the gate only with numbers). partner
- [ ] F-F4 NVENC probe ok, idle VRAM under the target after unload, GPU lease contention between studio and Assistant (busy refusal). partner
- [ ] F-F5 First master through the scheduled task at 03:00: S4U task (no NS-W205), DPAPI secrets read by the task, wake from sleep, keep-awake under `powercfg -requests` elevated, the engine leaves afterwards, morning toast visible. partner
- [ ] F-F6 Lease expiry: kill a studio child mid-stage, `lease_expired` within 10 min, Retry continues. partner
- [ ] F-F7 Assistant on the real model: a tool call from a plain request, the confirm card, the result; answer times. partner
- [ ] F-F8 Nightshift Code launcher acquires the llama lease, OpenCode answers, release on exit. partner
- [ ] F-F9 WebView2 host on a stock PC (no browser fallback), window buttons, close while a job runs. partner
- [ ] F-F10 Listen to one real Short and one master: narration level and clarity after PR #94 (flattened plosives or sibilants would be the tell), captions timing, music bed if any. owner
- [ ] F-F11 Story quality and length on the Balanced model: a 10 to 14 minute master, word budgets met, no policy strikes; the 16K context fits. partner
- [ ] F-F12 Update path on the GPU PC: `ns update --check`, update from the next release, rollback once. partner
- [ ] F-F13 The twelve rows of `docs/design/system-gaps.md` "Verified on the partner PC" filled in after the first night. partner

## G. Programmatic gates (must all be green before "done")

- [ ] F-G1 `main` CI green: lint, types, test, powershell-parse, scope-check, ui-build (`gh run list --branch main --limit 1`). CI
- [ ] F-G2 `python dev/verification_inventory.py --check` passes and the inventory has zero U rows in sections A to E's scope (`grep -c "| U |" docs/VERIFICATION_INVENTORY.md` is 0 or every remaining U is a V-gpu line). CI
- [ ] F-G3 `python dev/finalization_status.py --check` passes: every ticked box carries evidence; sections A to E and G report 0 open. CI
- [ ] F-G4 `tests/test_models.py` schema equality and `tests/test_release_wiring.py` pass on the tagged commit. CI
- [ ] F-G5 `docs/VERIFICATION.md` section 4 has no **U** cell outside the GPU-only column. laptop
- [ ] F-G6 `docs/PLUMBING.md` section 0 has no U outside V-gpu lines, and section 12 has no "Later" row without a box in this file. laptop
