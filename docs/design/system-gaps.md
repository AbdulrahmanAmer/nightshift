# Nightshift: gaps found while mapping

Re-checked on the clean clone of main at `54c9d42` (2 Nov 2026), after PRs #48 (loose ends), #49 (runtime), #50 (dashboard) and #51 (studio and TTS), while rebuilding `docs/design/system-diagram.html`. Every row cites the lines that were read. Severity: **Blocks first run** (the first real run on the partner PC), **Degrades**, **Cosmetic**.

Of the 16 rows open on the third map (`54ce3a1`), 10 are fixed (one partly; its remainder is an open row) and 6 are still open. A pass over `git diff 54ce3a1..54c9d42` found 5 new gaps at the new seams. Open now: 0 block the first run, 3 degrade it, 9 are cosmetic.

## Open

> **Verified on the partner PC** is empty on purpose: every row says *pending first run* until the first real run on the partner's GPU PC. Nothing here was run (no model, no Python, no tests). The note after it says what that run must confirm; a dash means the run cannot settle it. Fill the column in after the first night.

| # | Severity | Kind | Origin | Gap | Where | What the owner sees | Verified on the partner PC |
|---|---|---|---|---|---|---|---|
| 1 | Degrades | Journey not backed | new · unconfirmed | The whole voice of a job (every master chapter and Short hook) is one `POST /v1/audio/speech/batch`, now one `qwen-tts` process per voice. The studio's HTTP client gives up after a fixed 1,800 s, while the server lets the process run 900 s + 60 s per line (about 3.6 h for 200 chunks). | `ns_common/tts_client.py:87-89, :114-128; ns_studio/media/voice.py:83; ns_tts/server.py:128-157; ns_tts/engine.py:98, :404-444` | If synthesis on the partner GPU is slower than about 1.5x real time for a 14-minute master plus Shorts, the voice stage fails with NS-E310 after 30 minutes while the TTS service keeps the GPU busy, and the retry starts the whole batch again. | pending first run. Confirm: Time the voice stage of the first master (logs\tts-*.jsonl `tts_batch` rtf). |
| 2 | Degrades | Journey not backed | new | Queue > Release the graphics card only frees the lock (`release_owner`); it stops no process. The confirm text says "Anything still running on it is stopped" and the docstring says a running dashboard job is asked to stop first; neither happens. | `ns_dashboard/svc_engine.py:278-291; ns_supervisor/core.py:420-447; ns_dashboard/ui/src/pages/Queue.tsx:352-356` | Pressed while the holder is alive (the 03:00 run, a job in its child), the next tenant loads beside the old one; on a 16 GB card that can end in an out-of-memory crash (NS-E202). | pending first run. Confirm: Only if the owner uses the button: check that the holder's job ended first. |
| 3 | Degrades | Journey not backed | open #7 (narrowed) | A task registered in interactive mode (NS-W205, Windows refused S4U) is reported as registered: `schedule_registered` is only "schedule.json exists" and `schedule_mode` from setup never reaches the dashboard. | `installer/stage2/steps/s09_shortcuts_scheduler.py:139-144; ns_dashboard/svc_engine.py:156; ns_dashboard/svc_settings.py:74; ns_supervisor/schedule.py:124` | Settings shows the 03:00 countdown, but if the owner signs out (instead of locking the screen) nothing runs that night. | pending first run. Confirm: Read setup's NS-W205 line, or the task's "Run only when user is logged on" setting in Task Scheduler. |
| 4 | Cosmetic | Journey not backed | new | Cancel frees only the owners `studio:<job id>` (`studio_owners`). A setup voice being made at the start of Run now (`apply_pending_voices`, owner `studio:voice-<name>`) is not among them. | `ns_dashboard/adapters.py:239-248, :311-319; ns_studio/voice_setup.py:117-124; ns_studio/policy/regen.py:146` | Cancelling during that first minute leaves "GPU held by studio:voice-…" until the 10-minute lease expiry or the Release button; the next job waits. | pending first run. Confirm: Not needed unless the first Run now is cancelled. |
| 5 | Cosmetic | Journey not backed | new | Library > Voices > Create it now runs the `apply_voice` child inside the HTTP request (`VoiceMaker.apply_pending` → `runner.run`), not as a dashboard job: no progress, no Cancel, and it waits up to 10 minutes for the `tts` tenant when the GPU is busy. | `ns_dashboard/svc_voices.py:83-88; ns_dashboard/adapters.py:737-741; ns_studio/voice_setup.py:119` | The button spins for minutes with no step shown; a reload does not stop the child. | pending first run. Confirm: Press Create it now once with the GPU idle; note how long it takes. |
| 6 | Cosmetic | Spec arrow, no code | new | The setup window's hint says "Without it, the first voice you design downloads it then"; nothing downloads it on demand. The dashboard answers NS-E314 with a Get voice design model button. | `installer/ui/src/copy.ts:269; ns_dashboard/ui/src/pages/Library.tsx:188-201` | The first design fails once and asks for a 2 GB download the setup text said would happen by itself. | pending first run |
| 7 | Cosmetic | Config read nowhere | open #9 | `Runtime.llama.story_flags` is still read by nothing: in two-model mode the story model starts with the code model's `flags`. | `ns_common/models.py:98; ns_supervisor/services.py:63-79` | The story model cannot get its own GPU-layer or context settings; harmless while both models fit the same flags. | pending first run |
| 8 | Cosmetic | Journey not backed | open #10 | J5.3: the governor's plan for tonight (Shorts per day after a change) is still not shown; Tonight's plan prints the static format and the GPU holder. | `ns_studio/daily.py:128-140; ns_dashboard/ui/src/pages/Queue.tsx:345-366` | No sign that Shorts per day changed. | pending first run |
| 9 | Cosmetic | Journey not backed | open #11 | J1.4: the setup window's voice step plays no preset samples (no sample WAVs exist; only the demo build has one). | `installer/ui/src/screens/Choices.tsx; installer/ui/public/demo-voice.wav` | Voices are picked by name only. | pending first run |
| 10 | Cosmetic | Spec arrow, no code | open #12 | J8: no changelog is shown after an update; `UpdateResult.notes` lists carried-over files, not release notes. | `ns_supervisor/update.py:68, :262-297; ns_dashboard/ui/src/pages/Settings.tsx:25` | No release notes after updating. | pending first run |
| 11 | Cosmetic | Caller of a missing name | open #13 | `studio dashboard [--port 8100] [--open]` (§15.9) does not exist; the dashboard starts only as a supervisor service. | `BUILD_SPEC.md:630; ns_studio/cli.py` | "No such command" when following the spec. | pending first run |
| 12 | Cosmetic | Error never raised | open #14 | An uncoded exception in the dashboard's studio child is still sent with `code: null` and stored as `job_failed` with no hint; NS-E327 is used only on the CLI and nightly paths. | `ns_dashboard/studio_job.py:218-226; ns_dashboard/studio_runner.py:83-89; ns_dashboard/jobs.py:615-622; ns_studio/daily.py:168` | A red card that says "TypeError: …" with no code or advice. | pending first run. Confirm: If a dashboard job fails, check the card has a code. |

## Fixed since the third map (PRs #48–#51)

| Row | Was | Status | Gap | Fixed by | PR |
|---|---|---|---|---|---|
| 1 | Blocks first run | Fixed | A designed or cloned setup voice was made only by `studio run`; Run now and series parts never made it | `ns_studio/policy/regen.py:131-146 (apply_pending_voices first); ns_dashboard/studio_job.py:200-214 (apply_voice); ns_dashboard/server.py:498-504` | #50, #51 |
| 2 | Blocks first run | Fixed | Picking a designed voice in setup did not download the VoiceDesign talker | `installer/stage2/answers.py:30-39 (absent = yes for a designed voice); installer/stage2/steps/s01_questions.py:314 (NS-W114); installer/ui/src/screens/Choices.tsx:208 (checkbox)` | #48 |
| 3 | Blocks first run | Fixed | `load_keys()` caught only `KeyringError`; a raw backend error in the S4U task skipped the DPAPI fallback | `ns_studio/jobs.py:84-121 (every keyring call under except Exception)` | #51 |
| 4 | Degrades | Fixed | Settings > Keys wrote Credential Manager only, not `config\secrets.dpapi` | `ns_dashboard/adapters.py:533-593, :868 (RealKeyringAdapter writes both)` | #50 |
| 5 | Degrades | Fixed | `embed_server_args` never passed; llama-server kept its 512-token batch | `ns_supervisor/services.py:101-111; ns_common/embed_client.py:64-84 (token-aware window)` | #48, #51 |
| 6 | Degrades | Fixed | On the PyTorch engine, Get voice design fetched the GGUF talker | `ns_supervisor/models_get.py:65-84, :134 (follows runtime.tts.engine)` | #49 |
| 7 | Degrades | Partly fixed (open part listed above) | schedule.json written when the task was refused; the dashboard showed a run time nothing would start | `installer/stage2/steps/s09_shortcuts_scheduler.py:77-83, :153; ns_dashboard/svc_engine.py:156; ns_dashboard/ui/src/shell/Shell.tsx:148 (schedule_registered). Interactive-only task still not shown (open row)` | #49, #50 |
| 8 | Degrades | Fixed | Thumbnail picker regex `/d/` showed thumbnail 1 three times | `ns_dashboard/ui/src/pages/Queue.tsx:184 (/\d/)` | #50 |
| 9 | Cosmetic | Still open (open part listed above) | `story_flags` read by nothing | `Not fixed (open row)` | — |
| 10 | Cosmetic | Still open (open part listed above) | Governor plan for tonight not shown | `Not fixed (open row)` | — |
| 11 | Cosmetic | Still open (open part listed above) | Setup voice step plays no samples | `Not fixed (open row)` | — |
| 12 | Cosmetic | Still open (open part listed above) | No release notes after an update | `Not fixed (open row)` | — |
| 13 | Cosmetic | Still open (open part listed above) | `studio dashboard` missing | `Not fixed (open row)` | — |
| 14 | Cosmetic | Still open (open part listed above) | Uncoded exception in the dashboard child has no NS-E327 | `Not fixed (open row)` | — |
| 15 | Cosmetic | Fixed | Every TTS 409 `missing_model` shown as the voice design model | `ns_dashboard/adapters.py:795-810 (names the missing model); ns_dashboard/ui/src/pages/Library.tsx:188` | #50, #51 |
| 16 | Cosmetic | Fixed | `download_source(cancel_token=…)` never given a token | `ns_dashboard/studio_job.py:144-178 (flag file → CancelToken)` | #48 |

## Fixed in the review round

| Row | Was | Status | Gap | Fixed by |
|---|---|---|---|---|
| 1 | Blocks first run | Fixed | Studio read the stock-footage keys as `pexels` / `pixabay`; setup and Settings store `pexels_api_key` / `pixabay_api_key` | `ns_common/secrets_names.py; ns_studio/jobs.py:75-92 (keyring, else config/secrets.dpapi)` |
| 2 | Blocks first run | Fixed | PyTorch fallback TTS started with the supervisor venv, which has no torch | `ns_supervisor/services.py:113-118 (venvs/tts when runtime.tts.engine is reference); ns_tts/engine.py:647 (models/tts/hf)` |
| 3 | Degrades | Fixed | Queue > Approved and posted asked for two statuses (422), rows had no reason or link | `ns_dashboard/svc_outputs.py:89-94; ns_dashboard/common.py:61-63` |
| 4 | Degrades | Fixed | Channel editor saved the card (422) and would reset pools and cadence | `ns_dashboard/ui/src/pages/Channels.tsx:95, :130-137 (GET /channels/{slug}, whole profile)` |
| 5 | Degrades | Fixed | Library > Ideas used `i.id` | `ns_dashboard/ui/src/pages/Library.tsx:74, :91 (idea_id)` |
| 6 | Degrades | Fixed | Library > Stories went blank on `s.words` | `ns_dashboard/svc_library.py:177; ns_dashboard/ui/src/main.tsx:34, :46 (error boundaries)` |
| 7 | Degrades | Fixed | Results read fields `/learn/summary` never sent | `ns_dashboard/svc_settings.py:212-242 (top, bottom, daily, milestone)` |
| 8 | Degrades | Fixed | Settings > Models showed `[object Object]` and failed to save | `ns_dashboard/svc_settings.py:60 (runtime_flags.flatten, dotted keys)` |
| 9 | Degrades | Fixed | Any failed status poll switched the page to demo data for good | `ns_dashboard/ui/src/lib/api.ts:42-53 (first call only, then backoff)` |
| 10 | Degrades | Partly fixed (open part carried into the third map) | Nothing downloaded the VoiceDesign talker | `Partly: ns_supervisor/models_get.py; ns_dashboard/svc_library.py:257; ns_dashboard/ui/src/pages/Settings.tsx:78. A designed voice from setup still needs it by hand (open row)` |
| 11 | Degrades | Fixed | Cadence publish times globbed the old Outbox layout | `ns_studio/daily.py:40-60 (<job>/outbox/manifest.json, both sheets)` |
| 12 | Degrades | Fixed | Series parts lost title, Part N of M, end card and delay | `ns_studio/text/seo.py:81-107, :181-183; ns_studio/daily.py:30-36` |
| 13 | Degrades | Fixed | `tag_relevance` judge returned PASS without asking | `ns_studio/policy/judge.py:72-85` |
| 14 | Degrades | Partly fixed (open part carried into the third map) | Two-model mode could not be reached | `Partly: ns_supervisor/models_get.py:95 (ns models get story_llm sets story_model_path); ns_studio/cli.py:604. `story_flags` still unread (open row)` |
| 15 | Degrades | Fixed | TTS HTTP failures reported as NS-E301 | `ns_common/tts_client.py:57, :97, :116, :125 (NS-E310 / E311 / E315)` |
| 16 | Degrades | Fixed | Caption contrast fallback only in the Clipper | `ns_studio/media/longform.py:231-238; ns_studio/media/render.py:268` |
| 17 | Degrades | Fixed | Owner's exemplars and prompt_overrides never read | `ns_studio/text/exemplars.py:50-75; ns_studio/text/prompts.py:221` |
| 18 | Degrades | Fixed | `ns update` re-synced different extras than setup | `ns_common/venv_extras.py (one table for s04 and update)` |
| 19 | Degrades | Fixed | `ns update` had no `--git` / `--channel` | `ns_supervisor/cli.py:218-221` |
| 20 | Degrades | Fixed | Run time from setup not shown; a refused task change still toasted success | `ns_dashboard/state.py:94-100 (schedule.json); ns_dashboard/ui/src/pages/Settings.tsx:31` |
| 21 | Degrades | Fixed | Create > Clip `caption_style` dropped | `ns_studio/clipper/models.py:88; ns_dashboard/studio_bridge.py:186-188` |
| 22 | Degrades | Fixed | No step words or step/steps for story and clip jobs | `ns_dashboard/jobs.py:77-78; ns_dashboard/ui/src/lib/format.ts:23-39` |
| 23 | Degrades | Fixed | Designed thumbnails not served | `ns_dashboard/server.py:295 (/api/thumbs/{n}/{id}); ns_dashboard/ui/src/pages/Queue.tsx:184` |
| 24 | Degrades | Fixed | J7: no red error card, skipped jobs ignored | `ns_dashboard/ui/src/ui/JobError.tsx; ns_dashboard/ui/src/pages/Queue.tsx:465-478` |
| 25 | Degrades | Fixed | J6: distinctness check only after saving | `ns_dashboard/ui/src/pages/Channels.tsx:130 (validate before the save)` |
| 26 | Degrades | Fixed | Library: no footage delete, Make a Short, Reuse as series; formats without channels | `ns_dashboard/ui/src/pages/Library.tsx:136-137, :327, :395` |
| 27 | Cosmetic | Fixed | Supervisor GET /health had no caller | `launchers/_common.ps1:47` |
| 28 | Cosmetic | Fixed | Setup wrote whisper float16 | `installer/stage2/runtime_cfg.py:41 (int8)` |
| 29 | Cosmetic | Fixed | Footage cards read `c.id` / `c.uses` | `ns_dashboard/svc_library.py:194 (clip_id, uses)` |
| 30 | Cosmetic | Partly fixed (open part carried into the third map) | GPU holder and governor plan never shown | `Partly: ns_dashboard/ui/src/pages/Queue.tsx:358 (GPU holder). Governor plan still not shown (open row)` |
| 31 | Cosmetic | Fixed | J4.3: clip result did not say which layout `auto` chose | `ns_dashboard/svc_jobs.py:64-87; ns_dashboard/ui/src/pages/Create.tsx:340` |
| 32 | Cosmetic | Fixed | Design pages in the production bundle | `ns_dashboard/ui/src/main.tsx:16-20 (import.meta.env.DEV)` |
| 33 | Cosmetic | Fixed | Assistant labels used old tool names | `ns_dashboard/server.py:626 (GET /api/assistant/tools); ns_dashboard/ui/src/pages/Assistant.tsx:43` |
| 34 | Cosmetic | Fixed | Clip upload in one request | `ns_dashboard/ui/src/pages/Create.tsx:75-76 (chunked /api/uploads)` |
| 35 | Cosmetic | Still open (open part carried into the third map) | J1.4: setup voice step plays no samples | `Not fixed: no sample WAVs exist (open row)` |
| 36 | Cosmetic | Partly fixed (open part carried into the third map) | No zone on upload sheets; no changelog after an update | `Partly: ns_studio/media/outbox.py:218 (zone_label). No release notes after updating (open row)` |
| 37 | Cosmetic | Partly fixed (open part carried into the third map) | `studio dashboard` and `ns gate m1-m4` missing | `Partly: `ns gate` removed from ns_supervisor/cli.py. `studio dashboard` still missing (open row)` |
| 38 | Cosmetic | Fixed | NS-W301 only from the installer | `ns_supervisor/verify.py:365; ns_supervisor/check_names.py:21` |
| 39 | Cosmetic | Fixed | s05 local/code before s07; creator note only on the master; filler removal not a setting | `installer/stage2/steps/s08_smoke.py:113; ns_studio/text/seo.py:106; ns_common/models.py:31` |

## Fixed since the first map

| Old # | Status | Gap | Fixed by |
|---|---|---|---|
| 1 | Fixed | Nightly `studio run --daily` used `DevAcquirer` and never started a model | `ns_studio/acquirer.py:51 (StudioAcquirer, 2 h wait); ns_studio/context.py:88` |
| 2 | Fixed | Run now called the missing `run_job_with_progress` | `ns_dashboard/studio_bridge.py:92-124 (policy.regen.run_job_guarded)` |
| 3 | Fixed | Clipper called the missing `run_clip_job` / `import_url` | `ns_dashboard/studio_bridge.py:152-196 (create_clip_job, run_job, source.import_url)` |
| 4 | Fixed | Nobody started `embed` for dedupe and the text gate | `ns_studio/orchestrator.py:96-132 (embed_lease); ns_studio/stages.py:90` |
| 5 | Fixed | Designed or cloned voice from setup never created | `ns_studio/voice_setup.py:28; ns_studio/cli.py:385 (voice apply-pending)` |
| 6 | Fixed | Jobs worker did not know `series_part` | `ns_dashboard/jobs.py:22; ns_dashboard/studio_bridge.py:292` |
| 7 | Fixed | Workshop tools missing from the Assistant | `ns_dashboard/assistant/tools.py:353-374` |
| 8 | Fixed | Library > Series was a stub | `ns_dashboard/ui/src/pages/Library.tsx:444 (Produce next part)` |
| 9 | Fixed | Update button ran non-existent `ns update --check/--github` | `ns_supervisor/cli.py:167-214; ns_supervisor/update_check.py` |
| 10 | Fixed | Diagnostics called `make_bundle` | `ns_dashboard/adapters.py:431 (build_bundle)` |
| 11 | Fixed | Verify job used the wrong `run_verify` signature | `ns_dashboard/adapters.py:403; ns_supervisor/verify.py:250` |
| 12 | Fixed | CSV import called `learn.import_csv` | `ns_dashboard/adapters.py:347 (importer.read_csv, import_rows)` |
| 13 | Fixed | Run time change never reached the Windows task | `ns_dashboard/adapters.py:643 (schedule.install)` |
| 14 | Fixed | No end-of-run toast; notifications toggle unread | `ns_studio/notifications.py:16-28; ns_studio/daily.py:73` |
| 15 | Fixed | Cliff lines never sent to TTS | `ns_studio/media/voice.py:24; ns_common/tts_client.py:82` |
| 16 | Fixed | No weekly update check | `ns_supervisor/core.py:531-544; ns_supervisor/app.py:198` |
| 17 | Fixed | No Export / Import settings or Uninstall in Settings | `ns_dashboard/server.py:447-462; ns_dashboard/ui/src/pages/Settings.tsx:102` |
| 18 | Fixed | Clip re-run with another template had no route | `ns_dashboard/server.py:361; ns_dashboard/studio_bridge.py:218` |
| 19 | Fixed | NS-E408 never raised | `ns_studio/media/transcribe.py:180` |
| 20 | Fixed | NS-E311 never raised | `ns_tts/engine.py:321, :491; ns_common/tts_client.py:65` |
| 21 | Fixed | Assistant history, conversations and queue replay unused | `ns_dashboard/ui/src/pages/Assistant.tsx:72, :162, :173` |
| 22 | Fixed | Diagnostics zip had no download | `ns_dashboard/svc_settings.py:151; ns_dashboard/ui/src/pages/Settings.tsx:140` |
| 23 | Fixed | Format edit route had no UI | `ns_dashboard/ui/src/pages/Library.tsx:326-346` |
| 24 | Fixed | Two Outbox layouts in the code | `Paths.outbox_dir removed; ns_studio/media/outbox.py:76 (daily.py:30 docstring still names the old layout, see the cadence row)` |
| 25 | Fixed | Engine stayed up 30 min after the nightly run | `ns_supervisor/app.py:143; ns_supervisor/core.py:567-587; ns_studio/daily.py:128` |
| 26 | Fixed | No Last night summary; GET /api/runs unused | `ns_dashboard/ui/src/pages/Queue.tsx:453-460` |
| 27 | Fixed | Warm up, Stop all, Self-check had no buttons | `ns_dashboard/ui/src/pages/Settings.tsx:53, :56, :61` |
| 28 | Partly fixed (open half carried into the review table) | Logs by component unused; template preview PNG unused; supervisor /health unused | `Partly: ns_dashboard/ui/src/pages/Settings.tsx:24; preview route removed. /health still open` |
| 29 | Fixed | series_compilation offered in Run now | `ns_dashboard/common.py:17-21` |
| 30 | Partly fixed (open half carried into the review table) | owner_name, ui_theme, story_flags, qc_pass_target read nowhere | `Partly: ns_dashboard/state.py:131-143; qc_pass_target drawn (channel_stats.py:101). story_flags still open` |
| 31 | Partly fixed (open half carried into the review table) | Whisper default small.en / float16 | `Partly: ns_common/models.py:128-130. Installer still writes float16` |

Checked and found clean on 54c9d42: the lease seam (StudioAcquirer heartbeat → POST /heartbeat → Supervisor.heartbeat; dashboard Cancel and Release → POST /release_owner → GpuLock.drop_waiters) has the same owner names and fields on both sides; every studio: owner the studio uses goes through StudioAcquirer, so it beats; the cancel flag (NS_JOB_CANCEL_FILE) is created by the runner and read by studio_job.report and the download watcher; GET /api/voices/pending reads config/pending_voice.json with the fields setup writes; schedule_registered is in ns_common.models.Status, status.schema.json and ui/src/lib/types.ts; the updater copy and relaunch are wired from ns_supervisor.cli and update_job; ns models get voicedesign follows runtime.tts.engine, which the installer fallback sets to reference; embed_window gets EmbedCfg.batch_size at all four call sites and the embed service starts with the same -b; every NS- code used in code and UI (75) is in contracts/errors.json; the API/UI contract tests now cover POST/PUT answers and the chat stream (tests/test_api_ui_contract_calls.py). Earlier passes: studio, supervisor and TTS names resolve (tests/test_dashboard_wiring.py), the keyring names are one module, setup and ns update read one extras table. No TODO, FIXME or NotImplementedError in shipped code.

Not checked: §9 SEO rules, §16 prompts and validators in depth, §14 benchmarks, §15.10 OpenCode lock, the compiled `ns_dashboard/static` and `installer/ui/static` bundles (UI claims come from the sources), the `qwen-tts --stream-by-line` flag against the real binary. Nothing was run: no model, no Python, no tests.
