# proofs/partner: the partner kit (order H-14), design

Status: design only. No script here exists yet. Line references are to `main` at fdb058b (2026-11-09) and to the
runner on `origin/wf/h13-prove` / `origin/pr/195` (`dev/prove.py`, `dev/prove_ctx.py`). The Head's decisions of
2026-11-09 (23:15, `refs/departments/head-orders.md`, H-14 and H-15) are applied throughout.

The kit is one proof script per box F-F1 to F-F14, run by `dev/prove.py --home <install> --kit partner` from the
launcher `Nightshift-Partner-Check.cmd` on the RTX 5060 Ti PC. Each script measures what its box asks, writes its
evidence, and adds files to one zip. Only F-F10 (three questions per video) and F-F5's toast yes/no ask the
person anything. The full list is in "People steps" after the summary table.

**H-15 and H-10 marks.** The facts the kit used to find missing are now order H-15 for Builders: each goes into a
file the product already writes. A part marked "after H-15 item N" reads that future file (N is the item number in
H-15). A part marked "after H-10" needs the fake GPU switch, which starts after Builders' 0.1.15 items. The kit is
run on a release that has all nine H-15 items. Until an item is in the code under test, the laptop dry-run skips
the part with the item named; once it is in, the part runs for real. On the partner PC a skipped part fails the
proof (strict mode).

## 1. How the kit runs

**Two launcher runs.** The boxes need one real night.

- Run 1, "evening": any time after the install, at least 2 hours before the run time (03:00 by default). It runs
  F-F1, F-F2, F-F4, F-F6, F-F7, F-F8, F-F9, the evening part of F-F5, and F-F12 when a newer release exists. The
  boxes that read the night (F-F3, F-F5 morning, F-F10, F-F11, F-F13, F-F14) end as "waiting" (`ctx.defer`).
  Run 1 ends by putting the PC to sleep itself (see "Sleep and UAC" below).
- The night: the product's own task `Nightshift Daily` wakes the PC and runs at 03:00. The kit is not running.
- Run 2, "morning": the same launcher. Passed proofs are not run again. Waiting proofs run from the start. F-F5's
  morning part runs first, before the runner starts any engine (it must see that the engine left). F-F10 runs
  last because it asks the person.
- A run 3 is needed only when something is still waiting: a night that did not sleep or did not start, or no newer
  release for F-F12 yet. The progress line says what is waiting and what the person can do.

**The first night.** Every "night" part below reads the same night: the first `runs` row with `mode = 'daily'`
whose `started_at` is after the time F-F5 first deferred (`contracts/schema.sql:11-17`). Its jobs are those in
`summary_json.jobs` and in `logs\nightly-<date>.json` (`ns_studio/daily.py:197-205`). If no such row exists and
the task's next run is still ahead, the proof defers again. If the run time plus the task's 6 hour limit
(`ns_supervisor/schedule.py:81-82`) has passed with no row, the night part fails and records the task's
`Last Run Time` and `Last Result` from `schtasks /query /v /fo LIST`.

**The proof channel.** Run-1 proofs that need a job make Shorts on their own channel `proof-partner`
(`proofs/_lib/__init__.py:proof_channel`, preset voice `Ryan`, never the setup's designed voice). Every such proof
pauses that channel at its end, pass or fail (`POST /api/channels/proof-partner/pause`, `ns_dashboard/server.py:343`),
so the 03:00 run never plans it. Its few Shorts stay in Review under that name; the final progress line lists
them. This is the only lasting change the kit makes on its own, and it is made through the product's own routes.

**Time guard.** Before each run-1 proof the runner reads `next_run_in_s` from `GET /api/status`
(`ns_dashboard/svc_engine.py:189`). A proof whose duration estimate would end less than 30 minutes before the run
time is not started; it defers with "too close to the nightly run". A kit job holding the GPU at 03:00 would make
the night wait (`ns_studio/daily.py:448`), and F-F5 would then measure the kit, not the product.

**Engine.** The runner starts the engine only if it is down, and stops only an engine it started, through the
product's own stop (`ns stop`, `ns_supervisor/cli.py:61`). If the engine stays up overnight with the dashboard
service running, the product keeps it after the night on purpose (`ns_supervisor/core.py:733-735`); F-F5 then
records "window was open" (section F-F5, part 6).

**Evidence.** One folder per kit session under the home (see runner needs), one `<box>.json` per box, and at the
end of each launcher run one zip `partner-evidence-<version>-<date>.zip` with the evidence files and what each
proof attached. The last line names the zip and says where to send it.

**Sleep and UAC.** The launcher asks for one UAC prompt at the start of the kit, and only one. It starts one
small elevated helper that does the admin-only reads on an allowlist (`powercfg /waketimers`) and the sleep step.
The proofs themselves run as the person, so the engine and the files it writes are not elevated. At the end of
run 1, when a night is still waited for, the helper checks that `powercfg /a` lists a sleep state and that
`powercfg /waketimers` lists the product task's timer (F-F5 part 2). It then closes any Nightshift dashboard window
through its own Close button and "Close window" (never "Quit"). Closing the window does not stop the dashboard
service, and while that service runs the engine never leaves after the night (`dashboard_open()`,
`ns_supervisor/core.py:700, :716-751`). So the kit then runs Stop All (`POST /api/engine/stop_all`,
`ns_dashboard/server.py:318`), whoever started the engine, and waits until the supervisor port stops answering.
It sleeps the PC only when, just before, `engine.vram_owner` was null (nothing holds the GPU, so the night's jobs
are not skipped) and the engine is down; otherwise it does not sleep, and the progress line names the holder. It
then writes the zip, prints a sentence that the PC sleeps in 60 seconds, and sleeps it (sleep, not hibernate). The kit adds no wake timer of its own: the task's `WakeToRun` timer wakes the PC. If the timer is
not listed, the kit does not sleep the PC and F-F5 part 2 fails with that reason. Nobody has to be at the PC.
The sleep time goes into the defer data, for F-F5 part 3.

**Fields each partner proof declares.** `"install": "engine"`, `"gpu": True`, `"window"` as needed, and the fields
the runner needs for the kit (proposed): `"order"`, `"phase"` (`"evening"`, `"morning"`, `"release"`),
`"admin"` (`False` or `True`: the proof reads through the elevated helper), `"estimate_s"`.

**Parts.** Each box below is split into parts. On the partner PC a skipped part fails the proof (strict mode).
On the laptop a part that cannot happen there is skipped with its reason and the rest must pass.

## 2. The fake GPU switch (H-10) today

H-10 starts after Builders' 0.1.15 items (which include H-15). Every part marked "after H-10" waits for it.
`fake_gpu` does not exist on `origin/main` or any branch. `git log --all` finds no H-10 commit. The only mentions
are the runner's skip text `ctx.skip("needs fake_gpu")` and its test (`origin/pr/195:dev/prove_ctx.py:166`,
`tests/test_prove.py:301`). What exists in the same family: the engine-test switches `fake_voice`, `fake_footage`
and `tiny_story` (`installer/stage2/answers.py:37-38`, `ns_common/models.py:168, :201-204`), the stage-1
`-FakeProbe` for CI (`installer/stage1.ps1:15`), `FakeGpuProbe` for tests (`ns_supervisor/gpu.py:118`) and an RTX
5060 Ti `nvidia-smi` text fixture (`tests/fixtures/supervisor/nvidia-smi-rtx-5060-ti.txt`, commit 9bd5c91).

What H-10 is ordered to fake: on a dev or engine-test install, the services that would be GPU services on the
target take the one GPU lock, with the real lock, lease, heartbeat, expiry, busy refusal, queue and release code.
Nothing else: no VRAM numbers, `nvidia-smi` probes unchanged, and a developer variable that shortens the lease
time (H-15 item 5 adds that test-only lease-time override to config). Today a CPU install never takes the lock
(`ns_supervisor/core.py:387-395`, `if not d.gpu`), so every lease part below is skipped on the laptop until H-10
lands. Those parts are marked "after H-10": they run for real on the laptop once the switch exists.

The laptop dry-run plan does not change. The install is an engine-test install under `D:\NightShift\installs` with
`fake_voice`, `fake_footage` and `tiny_story`: real llama-server on a tiny model, real whisper on the CPU, real
embeddings, fake voice and fake stock footage. Its profile is `cpu-dev`. Every script is run there first, with the
lease parts skipped (reason "after H-10") and the H-15 parts skipped until their item lands.

---

## F-F1 Install from the window, PC check, downloads, tune, smoke, reports back

Script `f_f1_install.py`, phase evening, order 1 (it reads the setup's reports before F-F4 runs `ns verify`; after
H-15 item 1 that run no longer overwrites the setup's `verify-report.json`, so the order is for tidiness only).

**(a) Parts.** 1 installed from the window; 2 PC check green; 3 downloads match their hashes; 4 `ns tune` ran on
the GPU; 5 smoke test all ok; 6 `verify-report.json` and the diagnostics zip go back.

**(b) Effects and pass conditions.**

1. `logs\install-report.json`. The setup window writes it (`installer/stage2/web/controller.py:1113-1131`,
   `installer/stage2/web/report.py:143-206`); a console install writes a different one with `"mode": "console"`
   (`installer/stage2/diagzip.py:32-60`). Pass: `outcome == "done"`, the file has `machine` and `statusLine`, and
   has no `"mode": "console"`.
2. Same file, `machine` (`report.py:172-184`): `gpu` contains `5060 Ti`, `vramMb >= 15000` (the card reports
   16311 MiB in the fixture), `driver >= 570` (BUILD_SPEC 1.2, the RTX 50 rule), `cuda` recorded, `profile ==
   "gpu"`. `warnings` (`report.py:203`) holds no `NS-W1xx` code (the PC-check warnings of `contracts/errors.json`).
   Cross-read live: `ctx.tool(["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
   "--format=csv,noheader,nounits"])` gives the same name, driver and memory. After H-15 item 1, setup keeps the
   PC-check rows (`preflight.check_rows`, each with pass or warn) in `install-report.json`; the script reads every
   row and passes only if none failed, and keeps the value checks above as the cross-read. Before that item, the
   row part is skipped and the script judges from the values.
3. `ctx.cli(["ns", "verify", "--downloads"], timeout=1800)` (`ns_supervisor/cli.py:113-117`,
   `installer/stage2/verify_downloads.py`): re-hashes every model and program against `manifest.json`, no engine.
   Pass: exit 0 and no line marked failed.
4. Newest `logs\tune-*.json` (`ns_supervisor/tune.py:199-210`, written by setup step
   `installer/stage2/steps/s06_tune.py:45`). Pass: `ok == true`, `winner` set with a GPU layer count above 0,
   `tok_s` recorded; `config\runtime.json` `llama.flags` equal `winner` (read, never attached).
5. `install-report.json` `engines.smoke == "passed"` (`installer/stage2/steps/s08_smoke.py:225`), and the setup's
   `logs\verify-report.json` (`ns_supervisor/verify.py:511-518`) has every `checks[].passed == true`. After H-15
   item 1 the setup's file is never overwritten by `ns verify` (which writes its own report), so the script reads
   it where it is. Before that item, the script copies it first as `verify-report-setup.json`.
6. `ctx.cli(["ns", "doctor", "--bundle", "--out-dir", str(ctx.scratch)])` (`ns_supervisor/cli.py:69-78`,
   `ns_supervisor/doctor.py:151-168`; the hidden `--out-dir` keeps it off the Desktop). Pass: the zip exists and
   opens.

**(c) Starts and stops.** Two `ns` commands, neither starts the engine. Nothing changes on the PC except a zip in
the evidence folder.

**(d) Waiting.** None.

**(e) Zip.** `install-report.json`, the setup's `verify-report.json`, the tune log, the `ns verify --downloads`
output, the diagnostics zip (the product's own send-back file; the kit does not open or filter it).

**(f) Laptop.** Real: parts 1 (if the engine-test install was made from the window; a console install fails part 1
there, which is correct), 3, 5, 6, and part 2's PC-check rows (after H-15 item 1). Skipped: part 2's card values
(no NVIDIA card, `nvidia-smi` absent) and part 4's GPU layer check (`cpu-dev` tunes on the CPU or not at all).
Faked: nothing.

**(g) Person.** None in the kit. The install itself is the person's act, done before the kit runs.

---

## F-F2 Voice engine: qwentts.cpp, `engine_kind`, NS-W301, fallback

Script `f_f2_voice_engine.py`, phase evening.

**(a) Parts.** 1 the qwentts.cpp programs are there; 2 the running service says `engine_kind: qwentts`; 3 no
NS-W301 anywhere; 4 the voice speaks; 5 fallback branch: if NS-W301 did appear, the PyTorch engine speaks.

**(b) Effects and pass conditions.**

1. `<home>\bin\qwentts\qwen-tts.exe` and `qwen-codec.exe` (`installer/stage2/downloader.py:921`, folder
   `paths.bin_qwentts`, checked by setup in `installer/stage2/steps/s08_smoke.py:99-115`). Pass: both files exist.
   "DLLs present" is shown by part 4: the program loads and speaks. After H-15 item 2, `verify-report.json` lists
   the qwentts DLLs it found; the script checks that each listed DLL is in `bin\qwentts` and that the list is not
   empty.
2. Start the voice service through the product (part 4's preview call starts it), then
   `ctx.get("tts", "/health")` (runner need). The route is `ns_tts/server.py:218-232`; `engine_kind` is at :224.
   Pass: `engine_kind == "qwentts"` and `model_loaded == true`.
3. `install-report.json` `warnings` has no `NS-W301` (set by `s02_download_bins.py:157` and `s08_smoke.py:278`);
   `engines.tts == "qwentts"` and `engines.ttsFallback` empty; `runtime.json` `tts.engine == "qwentts"`
   (`ns_common/models.py:167`). After H-15 item 2, `verify-report.json` always names the voice engine kind (today
   the `tts.engine` check is written only for the reference engine, `ns_supervisor/check_names.py:21`,
   `ns_supervisor/verify.py:377-387`); pass: it says `qwentts`.
4. `POST /api/voices/preview` with the preset voice and one sentence (`ns_dashboard/server.py:596-599`, answers
   `audio/wav`; needs `ctx.api` raw bytes, runner need). Save to scratch; measure with FFmpeg (`_lib.ebur128`).
   Pass: duration at least 1.0 s and integrated loudness above -40 LUFS (not silence).
5. Only when part 2 reads `reference`: part 4 must still pass, the step that installs the PyTorch venv is `done` in
   `install-report.json` `steps`, and the box passes with NS-W301 recorded as a finding. `engine_kind == "fake"`
   on the partner PC fails the box (not a real install).

**(c) Starts and stops.** The voice service, started by the product for the preview. The supervisor unloads it
after its idle time (`ns_supervisor/core.py:40`, 600 s); the runner stops the engine only if it started it.

**(d) Waiting.** None.

**(e) Zip.** The `/health` body, the preview WAV (a few seconds), the loudness numbers.

**(f) Laptop.** Real: the routes in parts 2 and 4 (`engine_kind == "fake"` there, and the script checks it
matches `runtime.json`). Skipped: part 1 (the `fake_voice` engine test downloads no voice programs,
`installer/stage2/downloader.py:948`) and the `qwentts` value in parts 2 and 3.

**(g) Person.** None.

---

## F-F3 Whisper on CUDA, captions `drift_ms` on the first Shorts

Script `f_f3_whisper_drift.py`, phase evening (part 1) and morning (part 2).

**(a) Parts.** 1 speech recognition runs on CUDA, no NS-W404; 2 `drift_ms` of every Short of the first night,
against the 120 ms gate.

**(b) Effects and pass conditions.**

1. `runtime.json` `whisper.device == "cuda"`; `install-report.json` `engines.whisperDevice == "cuda"` and no
   `NS-W404` in `warnings` (setup switches to the CPU and warns in `installer/stage2/steps/s08_smoke.py:230-252`;
   code at `contracts/errors.json:59`); the setup's `verify-report.json` check `whisper.load` passed with
   `log` ending `on cuda` (`ns_supervisor/verify.py:434-445`). After H-15 item 3, each job records the device
   whisper ran on, and the reason when it is not CUDA (today `ns_studio/media/transcribe.py:225-237` does not);
   pass: every job of the first night says `cuda` and has no fallback reason. Before that item, a finished night
   job with captions is the only run-time evidence (a CUDA load failure in a job raises NS-E411).
2. For each job of the first night: `data\jobs\<job>\captions\report.json` (`ns_studio/media/captions.py:859-869`),
   one entry `s<k>` per Short with `drift_ms` and `match_ratio`; the master's entry `long`
   (`ns_studio/media/derive.py:158`, `ns_studio/media/master.py:299`). Pass: every `drift_ms` is a number and at
   most 120 (`DRIFT_LIMIT_MS`, `captions.py:30` and `ns_studio/media/qc.py:24`). Recorded: max, median, and the
   margin to 120. That part can never fail by itself, because the product already rejects a Short over 120 ms
   (`captions.py:30`), so "well under 120" has its own threshold: pass when every Short's `drift_ms` is at most
   60 ms and the median at most 40 ms (the Head's reading of "well under 120", 2026-11-10). The script holds
   them as two named constants with that comment, so a later change is one line:
   `DRIFT_EACH_MAX_MS = 60` and `DRIFT_MEDIAN_MAX_MS = 40`. The evidence records every value either way.

**(c) Starts and stops.** Nothing. Reads only.

**(d) Waiting.** Part 2 defers until the first night exists (section 1). The morning run re-runs the whole proof;
part 1 is cheap and runs again.

**(e) Zip.** Each `captions\report.json` of the night (small; the `.ass` and timeline files are not attached).

**(f) Laptop.** Real: part 2's reading code, against a night that QA makes by hand with `studio run --daily` under
the engine-test home (whisper on the CPU, real word timestamps on fake-voice tone bursts: the numbers mean
nothing, the reading is proven). Skipped: part 1's `cuda` values.

**(g) Person.** None.

---

## F-F4 NVENC probe, idle VRAM after unload, GPU lease contention

Script `f_f4_gpu_probe_lease.py`, phase evening.

**(a) Parts.** 1 NVENC probe ok; 2 idle VRAM under 300 MB after unload; 3 contention: while a studio job holds
the GPU, the Assistant is refused as busy and its request is queued, the Release button is refused while the job
beats, and the queued request runs after the job.

**(b) Effects and pass conditions.**

1. `ctx.cli(["ns", "verify"], timeout=1800)` (full, `ns_supervisor/cli.py:103-137`), then the report that run
   writes (`logs\verify-report.json` today; after H-15 item 1 its own file, so setup's report stays): check
   `nvenc.probe` passed with value `h264_nvenc ok` (`ns_supervisor/verify.py:145-158, :446-455`, failure code
   NS-W401).
2. Same report: check `vram.idle` passed, value at most 300 (`verify.py:468-489`; targets `verify.py:70-75`; spec
   14.6). Then an independent read after `ns verify` has ended: `ctx.tool(["nvidia-smi",
   "--query-gpu=memory.used", "--format=csv,noheader,nounits"])` at most 300, and
   `--query-compute-apps=pid,process_name,used_memory` recorded, so that a miss caused by another program (a
   browser, a game) is visible as such.
3. Contention, in order:
   - a. `POST /api/jobs/run` one Short on `proof-partner`. Wait until `GET /api/status` `engine.vram_owner` starts
     with `studio:` (`ns_dashboard/svc_engine.py:153`, from the supervisor's `gpu_owner`,
     `ns_supervisor/core.py:614`). Pass within 180 s.
   - b. `POST /api/assistant/chat` with a plain question, read as a stream (`ctx.events`, runner need). The loop
     refuses before it asks the model (`ns_dashboard/assistant/llm.py:120-127`) and sends a `busy` frame with the
     holder and `queued: true` (`ns_dashboard/assistant/loop.py:207-214`). Pass: a `busy` frame whose `owner`
     equals `vram_owner`, within 10 s, and no `message` frame from the model; a new row in `queued_messages` of
     `data\assistant.db` (`ctx.db(..., db="assistant")`, runner need).
   - c. `POST /api/engine/release_gpu {"owner": <vram_owner>}`. The supervisor refuses while the holder beat in
     the last 90 s (`ns_supervisor/core.py:53, :466-479`; `ns_dashboard/svc_engine.py:372-396`). Pass: status 409
     with error `gpu_in_use`, `vram_owner` unchanged, the job still `running`. Nothing is released.
   - d. Wait for the job to end (`_lib.wait_job`). `POST /api/assistant/queue/run`
     (`ns_dashboard/server.py:771`, `loop.py:375`). Pass: the answer lists the queued request with a reply.

**(c) Starts and stops.** `ns verify` starts and stops services itself (`verify.py:307-309` stops all for a cold
load; `cli.py:121-130` stops an engine it started). It overwrites `logs\verify-report.json` (hence F-F1's copy)
and saves the measured voice speed that the voice stage uses (`ns_common/tts_timeout.py:68`): both are the
product's own effects of `ns verify`. After H-15 item 1 the setup's report is no longer overwritten. One Short on
`proof-partner`; the channel is paused at the end.

**(d) Waiting.** None.

**(e) Zip.** The report `ns verify` wrote, the two `nvidia-smi` outputs, the stream frames of 3b with times, the
409 answer, the queue/run answer.

**(f) Laptop.** Real: `ns verify` runs; on `cpu-dev` it takes the CPU branch (`verify.py:447-451` encodes with
libx264, `:470-472` answers `n/a` for VRAM). Parts 1 and 2 are therefore skipped with "no NVIDIA card". Part 3
is skipped until H-10 (the lock is never taken on a CPU install); after H-10 it runs for real, all four steps.

**(g) Person.** None.

---

## F-F5 First master through the 03:00 task

Script `f_f5_nightly_task.py`, phase evening (parts 1, 2) and morning (parts 3 to 8, part 6 first of all).

**(a) Parts.** 1 the task is S4U, no NS-W205; 2 the PC can wake for it; 3 the PC, put to sleep by the kit, woke for
it; 4 the task read the DPAPI secrets; 5 the run kept the PC awake (after H-15 item 4); 6 the engine left
afterwards (after H-15 item 4); 7 the morning message was shown; 8 a master was made by that run.

**(b) Effects and pass conditions.**

1. `ctx.tool(["schtasks", "/query", "/tn", "Nightshift Daily", "/xml"])`. The task is built in
   `ns_supervisor/schedule.py:49-96` (S4U first, interactive logon when Windows refuses). Pass: `LogonType ==
   S4U`, `WakeToRun == true`, enabled, daily trigger at the time in `GET /api/status` `schedule_time`. Also
   `schedule_mode == "registered"` in `/api/status` (`ns_dashboard/svc_engine.py:192`,
   `ns_dashboard/state.py:133-137`, `ns_supervisor/schedule_cfg.py:53-76`), and `install-report.json` has no
   `NS-W205` (`installer/stage2/steps/s09_shortcuts_scheduler.py:163-166`).
2. `ctx.tool(["powercfg", "/a"])` lists a sleep state; `powercfg /q SCHEME_CURRENT SUB_SLEEP` shows wake timers
   allowed and the sleep timeout (the setting stage 1 reads, `installer/stage1.ps1:471`), and `powercfg
   /waketimers` lists the task's timer (both need the allowlist; the second runs in the elevated helper, runner
   need). Pass: a sleep state exists, wake timers are allowed, the task's timer is listed. The kit does not rely on
   the PC's own sleep timer: it sleeps the PC itself at the end of run 1 (section 1, "Sleep and UAC").
3. Morning: `ctx.tool(["powershell", "-NoProfile", "Get-WinEvent", ...])` on the System log, provider
   `Microsoft-Windows-Power-Troubleshooter` id 1, from run time - 10 min to run time + 15 min. Pass: one event
   whose wake source is a timer, the sleep the kit started (its time is in the defer data) before it, and the
   night's `runs.started_at` within 10 minutes after the wake. If the log shows no sleep before the run time (the
   kit did not sleep the PC, or someone woke it), the part is not exercised and the proof defers one more night.
4. `config\secrets.dpapi` exists (`ns_common/secret_file.py:26`); the script reads only its names, never a value.
   The night's job fetched real footage: rows of `clips` with `source` in (`pexels`, `pixabay`) and `fetched_at`
   inside the night (`contracts/schema.sql:57-68`), and no `NS-E501` in that night's `logs\studio-*.jsonl`. The
   key path is `ns_studio/jobs.py:85-121` (keyring first, then the DPAPI file for the S4U session). After H-15
   item 4 the nightly report also records the session kind (session 0 or interactive), the key source (keyring or
   DPAPI, names only) and how late the run started after the task's time. Pass: session kind is session 0, key
   source is `dpapi`, the clips are real, and the start delay is recorded. When the night read no key (every clip
   was reused), the part is "not exercised" and fails strict mode.
5. After H-15 item 4, the nightly report records that the run took keep-awake (`ES_CONTINUOUS |
   ES_SYSTEM_REQUIRED`, `ns_studio/daily.py:129-145`, `ns_common/keepawake.py:32-38`) and released it, with the
   `SetThreadExecutionState` results and times. Pass: taken before the first plan, released at the report, both
   results non-zero. This replaces the old route of reading `powercfg /requests` elevated overnight: no night
   watch and no kit left open is needed. Before that item the part is skipped (not a pass).
6. Morning, before any engine is started: the supervisor port does not answer and `run\supervisor.json` is gone
   (written at `ns_supervisor/core.py:168-184`, removed by `remove_state`, `:185`, from `shutdown`,
   `:542-553`). After H-15 item 4 the nightly report records the engine's exit (reason and time); today it is only
   the log line `run_finished`, then `auto_exit` with `reason: "nightly run finished"` (`core.py:717-739`, grace
   60 s at `core.py:48`). Pass: the recorded exit has that reason, with `after_s <= 120`, and the port and state
   file agree. If `run_finished` shows `dashboard_open: true` the product kept the engine on purpose: the part is
   "not applicable" and defers one more night. The kit runs Stop All before it sleeps the PC (section 1), so
   the dashboard service is not running at 03:00 and this is not expected.
7. An S4U run is in session 0 and cannot show a toast (`ns_supervisor/notify.py:73-75`); it leaves
   `run\morning_note.json` instead (`notify.py:56, :106-124`). Pass: the night's `toast` log line says
   `shown: false` (`ns_studio/notifications.py:146`), `GET /api/notes` (`ns_dashboard/svc_engine.py:287-301`)
   lists the night's message, and the open dashboard window shows that text (read through `ctx.uia` on the
   window, runner need). The script reads `/api/notes` before it opens the window, because the dashboard clears
   the notes once shown. In an interactive task (NS-W205 case) the log says `shown: true`, and the script asks the
   person one yes/no question (`ctx.ask`): "Did a Nightshift notification appear at about <run end time>?" The
   answer is recorded. This is the only question outside F-F10. The box text should say that a session-0 run
   leaves the morning note, not a toast; the tick PR updates it.
8. `outputs` rows of the night's jobs (`contracts/schema.sql:105-123`): at least one `kind = 'long'` with
   `created_at` inside the night, and its file exists. `logs\nightly-<date>.json` lists the job with no failure.

**(c) Starts and stops.** The kit registers nothing and runs no task. At the end of run 1 it closes any Nightshift
dashboard window and puts the PC to sleep itself (section 1, "Sleep and UAC"); it sets no wake timer of its own.
The task, the night and the engine's exit are the product's. Part 7 opens the dashboard window (the Dashboard
launcher, recorded pid) and closes it through its own Close button and "Close window" (see F-F9), never "Quit".
There is no night watch.

**(d) Waiting.** Run 1 does parts 1 and 2, then `ctx.defer("waiting for the 03:00 run", data={"evening_at": ...,
"slept_at": ...})`. Run 2 finds the first night (section 1). Parts 3 and 6 can each send the proof back to waiting
for one more night with a plain sentence. A night that never started fails, with the task's last result.

**(e) Zip.** Parsed task facts (logon type, wake flag, trigger, enabled), not the raw XML (it holds the user name
and SID). The `powercfg` outputs. The filtered event records (time, id, wake source). `nightly-<date>.json` (with
the H-15 item 4 fields), the night's lines of the supervisor and studio logs, `morning_note.json`, the `runs` row.

**(f) Laptop.** Real: part 1's query (the engine-test task may be registered disabled by design,
`schedule.py:285`; then the S4U check is skipped with that reason), part 2's `powercfg /a`, and parts 6, 7, 8
against a night QA starts by hand with `studio run --daily` (that run is in an interactive session, so part 7
takes the `shown: true` branch, answered from the scripted answer file). Parts 5 and 6 read the H-15 item 4
fields once that item lands. Skipped: part 3 and the sleep step (the laptop is never put to sleep: GUI safety
rule; the sleep call is recorded, not made), part 4's key source (fake footage, no key).

**(g) Person.** Only one yes/no: part 7 in the interactive case, whether a toast appeared. The kit puts the PC to
sleep itself and closes the Nightshift window itself. The one UAC prompt at the start of the kit is the only other
step, and it is the same prompt for the whole kit.

---

## F-F6 Lease expiry and Retry

Script `f_f6_lease_expiry.py`, phase evening.

The Head's decision of 2026-11-09 (H-15 notes): F-F6 means "a dead child's lease does not stay taken". The box
ticks when both routes are shown. When a dashboard job's studio child dies, the dashboard frees its lease at once
(`ns_dashboard/adapters.py:346-348, :358-369`), so `lease_expired` is never reached on that route. The supervisor
reclaims a lease only when nobody releases it: a `studio run` from the command line, the nightly run, or a dead
dashboard (`ns_supervisor/core.py:497-519`). On that route the lease reaches `lease_expired` within the lease time
and Retry continues. The tick PR updates the box text to say so.

**(a) Parts.** 1 (CLI or nightly route, after H-15 item 5) a command-line studio job killed in a GPU stage: the
lease reaches `lease_expired` within the lease time; 2 its Retry continues from the stage it reached; 3 (dashboard
route) a dashboard job's child killed in a GPU stage: the dashboard frees the lease at once, the job fails with a
code, Retry continues.

**(b) Effects and pass conditions.**

1. `ctx.spawn([<studio.exe>, "run", "--channel", "proof-partner", "--format", "shorts_only", "--count", "1"])`
   (the install's own `studio`, `ctx.cli_program("studio")`). Wait until `engine.vram_owner == "studio:<job>"` and
   the job's `stage_state.json` shows a GPU stage running. Stop exactly that process tree (`ctx.stop(proc)`,
   runner need). Then poll `/api/status` every 5 s. This is the route of the nightly run too: the CLI and the
   nightly run use the same acquirer and the same supervisor reclaim. After H-15 item 5, `lease_expired` is
   recorded in the job's files (owner, `silent_s`, time), not only as a log line and event (`core.py:517-519`).
   Pass: `vram_owner` becomes null between 570 s and 660 s after the kill (expiry 600 s at `core.py:51`, heartbeat
   every 30 s at `ns_studio/acquirer.py:30`, monitor every 1 s), and the job's record has `lease_expired` with
   `owner == "studio:<job>"` and `silent_s >= 600` before any `owner_released` for it (`core.py:452, :517`). The
   record names the cause; the effect is the freed lock. The partner PC runs with the default lease time. The
   test-only lease override (H-15 item 5, written to config only when not the default) is never set there, and
   the runner refuses to set it on a normal install.
2. `ctx.cli(["studio", "run", "--resume", <job>])`, the Retry the CLI prints for an interrupted manual job
   (`ns_studio/cli.py:146, :197, :215, :249-263`). Pass: exit 0, `resumed: N stages visited`, the stages finished
   before the kill keep their finish times in `stage_state.json`, and the job's `outputs` rows exist.
3. `POST /api/jobs/run` one Short on `proof-partner`. When `vram_owner == "studio:<id>"`, read the child's pid from
   `data\owners\<studio job>.json` (`ns_studio/ownership.py:9, :158-160, :394`). Stop exactly that pid with
   `ctx.interrupt(pid, ...)` (runner need: the pid's program must be under `<home>\venvs\studio` and the record must
   name the job). Pass: the dashboard job ends `failed` with an NS code (NS-E328 or the stage's), `vram_owner`
   null within 30 s (the dashboard's own release; the script checks there was no `lease_expired` for it), then
   `POST /api/jobs/<id>/resume` (`ns_dashboard/svc_jobs.py:326-356`, the card's Retry,
   `ns_dashboard/ui/src/ui/JobError.tsx:16`) answers 201 and the new job ends `done` without repeating the
   finished stages.

**(c) Starts and stops.** One studio process the proof started and stops itself (by its recorded pid); one studio
child stopped by the pid the product recorded for it. Both jobs are then finished by the product's own Retry, so
nothing is left half made. Channel paused at the end.

**(d) Waiting.** None. The ten-minute wait is inside the proof (`timeout_s` about 1500).

**(e) Zip.** The status polls with times, the job's `lease_expired` record and the supervisor log lines of the
window, both jobs' `stage_state.json`, the owners record of part 3.

**(f) Laptop.** Real: the kills and both Retries on the CPU (already shown on a dev install,
`docs/VERIFICATION.md:70`). Skipped until H-10: the lock parts (no lock on a CPU install). After H-10, with the
H-15 item 5 lease-time override set short by the runner on the dev or engine-test install only, part 1 runs for
real in about a minute, and part 3 shows the dashboard's immediate release. Today there is no override
(`ns_supervisor/app.py:242` builds the config without one).

**(g) Person.** None.

---

## F-F7 Assistant on the real model: tool call, confirm card, result, answer times

Script `f_f7_assistant.py`, phase evening.

**(a) Parts.** 1 the Assistant uses the real Balanced model; 2 a plain request leads to a tool call and a confirm
card; 3 Yes on the card starts the job; 4 the result is reported; 5 answer times.

**(b) Effects and pass conditions.**

1. `runtime.json` llama model file is the Balanced model of `install-report.json` `choices.tier`; the Assistant
   uses alias `code` (`ns_dashboard/assistant/llm.py:21`). Pass: model file exists, not the dev fake.
2. `POST /api/assistant/chat` "Make one Short on the proof-partner channel now", read as a stream with a time per
   frame (`ctx.events`). The tool loop is `ns_dashboard/assistant/loop.py:242-334`. Pass: a `confirm` frame with
   an `action_id`, a tool that starts a job, and `args` naming `proof-partner`.
3. `POST /api/assistant/confirm {"action_id": ...}` (`ns_dashboard/server.py:744`, `loop.py:337`). Pass: the answer
   names a job id; `GET /api/jobs/<id>` exists; it ends `done` (`_lib.wait_job`).
4. `GET /api/assistant/history?conversation_id=...` (`server.py:754`). Pass: the last assistant message names the
   job or its files.
5. Three more requests: one question with no side effect ("How many videos wait for review?"), one that ends in a
   card the proof dismisses (`POST /api/assistant/dismiss`), one follow-up in the same conversation. For each:
   seconds to the first frame, to the first `message` or `confirm` frame, and to `done`; first request with the
   model cold, the rest warm; `messages.created_at` from `data\assistant.db` as a second clock. After H-15 item 6,
   the tool loop records per-step answer times (model wait, first token, total) in its transcript record, and the
   script reads them as a third clock and checks they agree with its frame times. Pass: every request ends with
   `done` and no `error` frame. The spec has no answer-time target (14.6 lists none) and gains one only after the
   first real numbers; the times are recorded, not judged.

**(c) Starts and stops.** The llama service through the product; one Short on `proof-partner` (the confirmed card);
one dismissed card. Channel paused at the end.

**(d) Waiting.** None.

**(e) Zip.** Every frame with its time, the history, the four timing rows, the loop's own step times.

**(f) Laptop.** Real: all five parts on the engine-test tiny model (a real tool loop; on 2026-11-09 a Qwen3-1.7B
produced a real tool call and card, `docs/HANDOFF.md:37`); the loop's own step times after H-15 item 6. A tiny
model may answer without a tool call; on the laptop that is reported as observed, on the partner PC it fails
part 2.

**(g) Person.** None.

---

## F-F8 Nightshift Code launcher: llama lease, OpenCode answers, release on exit

Script `f_f8_code_launcher.py`, phase evening.

**(a) Parts.** 1 the launcher takes the llama lease as `opencode`; 2 OpenCode answers; 3 the lease is released when
it exits.

**(b) Effects and pass conditions.**

1. `ctx.spawn(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
   <home>\app\launchers\nightshift-code.ps1, "-Prompt", "<question>"])` (the switch is H-15 item 7). The launcher
   posts `/acquire` for `llama`, owner `opencode`, alias `code` (`launchers/nightshift-code.ps1:7, :19-20`). Pass:
   `engine.vram_owner == "opencode"` and llama `ready` within 180 s (polled every second, so the short run is
   seen; the launcher's console log shows the acquire as a second witness).
2. After H-15 item 7, the launcher takes `-Prompt "<text>"`: it runs `opencode run <text>` inside the same acquire
   and `finally`, prints the answer and exits with it (today it starts the OpenCode window with no prompt,
   `nightshift-code.ps1:30-37`, and a script cannot type into it without global input). The script starts it with
   a one-sentence question; no typing by anyone. Pass: exit 0 and a non-empty answer on stdout.
3. After the launcher exits by itself: `vram_owner` null within 10 s (`nightshift-code.ps1:42-49`). `opencode` is
   not a leased owner (`ns_supervisor/core.py:56-59`), so a missed release would hold the GPU until Release in the
   Queue: a fail here is a real finding.

**(c) Starts and stops.** The launcher (recorded pid). With `-Prompt` it ends by itself. A hard stop by the runner
would skip the launcher's own `finally` and leave the lock held (`opencode` is not a leased owner, so nothing
expires it). So the proof's own `finally`, pass, fail or time-out, and before the runner may stop the launcher:
reads `engine.vram_owner`, and when it is still `opencode` releases it through the product
(`POST /api/engine/release_gpu {"owner": "opencode"}`, a call with no deadline) and reads it back null; the
proof then fails if it had to do that, naming the holder. The kit's sleep step is gated on `vram_owner` being
null (section 1, "Sleep and UAC"), so the PC never sleeps with the GPU held. The launcher creates `%USERPROFILE%\Projects` on C: (`nightshift-code.ps1:32-34`) and rewrites
`config\opencode.json`'s port: both are the product's own effects; the proof records whether `Projects` existed
before.

**(d) Waiting.** None.

**(e) Zip.** The status polls, the answer text, the launcher's console log.

**(f) Laptop.** Real: part 2 after H-15 item 7, on the tiny model, if the engine-test install has OpenCode.
Skipped until H-10: parts 1 and 3, because a CPU llama takes no lock (`core.py:387-395`). After H-10 they run for
real.

**(g) Person.** None. The launcher's `-Prompt` switch (H-15 item 7) means nobody types.

---

## F-F9 WebView2 host on a stock PC: no browser fallback, window buttons, close while a job runs

Script `f_f9_window_host.py`, phase evening, `"window": True`.

**(a) Parts.** 1 the WebView2 runtime and DLLs are there; 2 the Dashboard opens in the WebView2 host, not in a
browser; 3 Minimize, Maximize and Restore work; 4 Close while a job runs closes the window and the job goes on.

**(b) Effects and pass conditions.**

1. `ctx.spawn(["powershell.exe", "-NoProfile", "-File", <app>\launchers\nightshift-host.ps1, "-CheckOnly"])`
   prints `{webview2, runtime, missing}` (`launchers/nightshift-host.ps1:121-125`; runtime read from the EdgeUpdate
   keys at `:84-103`). Pass: `webview2 == true`, `missing == []`. Cross-read: `Get-ItemProperty` on the same
   EdgeUpdate key gives the same `pv`.
2. `ctx.spawn` the Dashboard launcher `launchers\nightshift-dashboard.ps1`. Pass: `run\apphost-dashboard.json`
   appears with `hwnd`, `pid` and `ready: true` within 60 s (`nightshift-host.ps1:167, :240-241`); that pid's
   command line (`Get-CimInstance Win32_Process`, filtered to that pid) runs `nightshift-host.ps1`; no `msedge` or
   `chrome` process with `--app=<dashboard url>` started after the launcher (the fallback,
   `launchers/nightshift-dashboard.ps1:63-98`).
3. `ctx.uia(hwnd)` (runner need): buttons named `Minimize`, `Maximize`, `Close` exist
   (`ns_dashboard/ui/src/shell/TitleBar.tsx:74-77`). Invoke `Minimize`: the window's visual state reads
   `Minimized` within 3 s; set it back to `Normal` through that window's WindowPattern. Invoke `Maximize`: state
   `Maximized`; invoke `Restore`: `Normal`. The host saves the state in its window file
   (`nightshift-host.ps1:300-313`); the proof leaves it as it found it.
4. `POST /api/jobs/run` one Short on `proof-partner`; while it is `running`, invoke `Close`; the page asks "Close
   Nightshift?" (`TitleBar.tsx:46-54`); invoke `Close window`. Pass: the host pid ends within 5 s, the job is still
   `running`, the dashboard still answers, and the job then ends `done`. "Quit Nightshift" is never pressed.

**(c) Starts and stops.** The CheckOnly host and the Dashboard launcher (recorded pids). The host window is closed
through its own button. If part 4 fails before the window closes, the proof closes it the same way; only if that
fails, the runner stops the host pid read from `apphost-dashboard.json` (`ctx.interrupt`, guarded). No global
input: every click is a UI Automation Invoke on an element of that one window.

**(d) Waiting.** None.

**(e) Zip.** The CheckOnly JSON, `apphost-dashboard.json`, the UIA reads with times, the job's end.

**(f) Laptop.** Real: all four parts (Windows 11 has the runtime; the window shows on the laptop screen, driven only
through its handle). Faked: the job runs on fake services.

**(g) Person.** None. The progress line asks the person not to touch the window for about five minutes.

---

## F-F10 The owner listens: one real Short and one master

Script `f_f10_listen.py`, phase morning, last.

**(a) Parts.** 1 measured facts of the two videos; 2 three answers for the Short; 3 three answers for the master.

**(b) Effects and pass conditions.**

1. The night's master (`outputs.kind = 'long'`) and its first Short (`kind = 'short'`, lowest `k`). For each:
   `_lib.ebur128` (integrated loudness, true peak, LRA, measured by FFmpeg, not by the product); the product's
   `qc.json` in the output folder (`ns_studio/media/qc.py:284`); its `drift_ms` (F-F3); its `quiet_flatness`
   (F-F14). Pass: `qc.json` passed; loudness -14 ± 1 LUFS and true peak at most -1.0 dBTP (the product's own limits,
   `qc.py:25`).
2. `ctx.show(<short>)`, then `ctx.ask` three times, choices `yes` / `no`: "Is the voice clear, with no flattened p,
   b, t or s sounds?", "Do the captions keep time with the voice?", "Is the level comfortable, with no need to
   change the volume?". Pass: three `yes`.
3. The same for the master.

The box also says "music bed if any". The product makes narration only and checks it (`AudioOnlyVoice`,
`ns_studio/policy/final_checks.py:345-352`), so there is no fourth question; the flatness value covers it.

**(c) Starts and stops.** The default video player, opened by `ctx.show`; the person closes it.

**(d) Waiting.** Deferred until the night's videos exist; runs last in run 2.

**(e) Zip.** The six answers, the measured values, both `qc.json` files. Not the videos.

**(f) Laptop.** Real: part 1 (fake-voice tone bursts: the numbers mean nothing, the reading is proven). Faked:
`ctx.ask` answered from a scripted answer file and `ctx.show` recorded but not opened (runner need).

**(g) Person.** Yes, by design: watching both videos (about 15 minutes) and six answers.

---

## F-F11 Story quality and length on the Balanced model

Script `f_f11_story_length.py`, phase morning.

**(a) Parts.** 1 made on the Balanced model; 2 the master is 10 to 14 minutes; 3 word budgets met; 4 no policy
strikes; 5 the 16K context fits (after H-15 item 8); 6 the product's own critic score, recorded.

**(b) Effects and pass conditions.**

1. `install-report.json` `choices.tier == "balanced"` (`report.py:185-191`) and `runtime.json` names its file.
2. `outputs.duration_s` of the night's `long` row, confirmed with ffprobe. Pass: 600 to 840 s (box; the longest draw
   is `MASTER_MAX_MINUTES = 14`, `ns_studio/text/budget.py:18`). The product's QC accepts 510 to 900 s
   (`ns_studio/media/qc.py:23`); a master between those and the box's range is reported as such.
3. `data\jobs\<job>\plan.json` budgets (`ns_studio/text/stages_master.py:156`) against the chapters' word counts,
   counted again by the script with the same rule (`ns_studio/text/validators.py:33-39`, limits ±20 % from
   `budget.py:28`). Pass: every chapter and every Short body within its limits.
4. `policy_text.json` and `policy_final.json` (`ns_studio/policy/gate.py:48`) have `passed == true` and no failed
   check; `policy_attempts.json` (`ns_studio/policy/regen.py:26`) shows no regeneration. Pass: zero failed checks,
   zero regenerations.
5. `runtime.json` `llama.flags.ctx == 16384` (the Assistant reads the same value,
   `ns_dashboard/assistant/loop.py:170-171`) and no context-overflow error (NS-E3xx) in the night's studio and
   llama logs. After H-15 item 8, the prompt token count of every LLM call is recorded with the job
   (`usage.prompt_tokens`; today `ns_common/llm_client.py` keeps none). Pass: every call's prompt tokens are below
   the context size. Recorded: the largest call, and its margin to 16384.
6. The product scores its own story. `critic.json` of the master (`ns_studio/text/stages_master.py:281-282`) holds
   `critic_avg` and the per-chapter scores; the channel's `critic_bar` (`stages_master.py:235`) is the bar. Pass:
   the file exists with a number for every chapter. Recorded: `critic_avg`, the lowest chapter, the bar, and the
   revisions used. The kit does not judge the story and no reader is needed: a chapter that missed the bar twice
   stops the job with NS-E304, so a finished master has passed the product's own bar.

**(c) Starts and stops.** Nothing. Reads only.

**(d) Waiting.** Deferred until the night's master exists.

**(e) Zip.** `plan.json`, the policy files, the word counts table, `critic.json`, the prompt-size table. Not the
story text.

**(f) Laptop.** Real: the reading code, against a master made by hand on fake services; part 5 after H-15 item 8.
Skipped: parts 2 and 3's numbers on `tiny_story` (tiny models stop at the text validators by design,
`docs/HANDOFF.md:37`).

**(g) Person.** None. Minutes, word budgets, policy strikes and context fit are measured, and the critic score is
the product's own. F-F10 stays as it is (three questions per video, no fourth question).

---

## F-F12 Update path: check, update from the next release, rollback once

Script `f_f12_update_rollback.py`, phase release, last of its run.

**(a) Parts.** 1 `ns update --check` answers; 2 update from the next release; 3 rollback once; 4 back to the newest
version.

**(b) Effects and pass conditions.**

1. `ctx.cli(["ns", "update", "--check"])` (`ns_supervisor/cli.py:289-291, :331-342`; GitHub API at
   `ns_supervisor/releases.py:27, :151`). Pass: exit 0; the last line is JSON with `update_available` and `latest`;
   the current version equals `<home>\app\VERSION`.
2. When `update_available` is false: `ctx.defer("waiting for the next release")`. Else `ctx.cli(["ns", "update",
   "--github"], timeout=3600)` (download `cli.py:224-234`, apply `ns_supervisor/update.py:642-717`, which runs `ns
   verify --quick` on the new version and rolls back on failure, `:685-711`). Pass: exit 0;
   `app\VERSION == latest`; `app.prev\VERSION ==` the old version; `run\update_state.json`
   (`ns_supervisor/update_job.py:69-80`) says done; the task is re-registered with the same logon type and time as
   F-F5 part 1 read (`cli.py:177-200`).
3. `ctx.cli(["ns", "update", "--rollback"])` (`update.py:720-753`). Pass: `app\VERSION ==` old,
   `app.prev\VERSION ==` new, the settings sentence recorded, `/api/status` `version ==` old once the engine is back.
4. `ctx.cli(["ns", "update", "--rollback"])` again: a second rollback swaps the folders back (`update.py:721`).
   Pass: `app\VERSION == latest` and `/api/status` `version == latest`.

**(c) Starts and stops.** The product stops the engine, swaps folders, re-syncs the venvs and starts the engine
again (`ns_supervisor/relaunch.py`); a window may open. The PC ends on the newest version with the old one in
`app.prev`, exactly as after a normal update. The settings copy (F-10a) restores settings on each swap; the
proof changes no setting in between. The runner must not run from `<home>\app` or from a venv the update
re-syncs (runner need), and the evidence folder must not depend on the version (runner need).

**(d) Waiting.** Part 2 waits for a release newer than the installed one. Every launcher run checks again.

**(e) Zip.** The `--check` JSON, the three update outputs, `update_state.json` after each, `logs\update-*.jsonl` of
the window, both task reads.

**(f) Laptop.** Real: part 1 (a read-only GET to api.github.com with the product's own user agent), parts 3 and 4,
and part 2 applied from a local zip with `ns update <zip>` (same `update_app`). After H-15 item 9 the release source is
configurable for tests (an environment variable read in one place, refused unless dev mode or a test flag is
set), so the laptop points `ns update --github` at a fake GitHub endpoint (`proofs/_fakes`) and part 2 runs for
real, download included. Until then the GitHub download of part 2 is skipped: the URL is a constant
(`releases.py:27`). The variable is never set on the partner PC.

**(g) Person.** None. The progress line asks the person not to use Nightshift for about 20 minutes.

---

## F-F13 The twelve "Verified on the partner PC" rows of `docs/design/system-gaps.md`

Script `f_f13_system_gaps.py`, phase morning (after F-F12 for row 10, else row 10 stays pending).

**(a) Parts.** One part per row. Each writes the text for that row's last column.

**(b) Effects (row: what is read).** Pass for each row: a value or a stated "not exercised, because ...". The
proof passes when every row has one; it does not judge the gap.

1. `logs\tts-*.jsonl` `tts_batch` lines of the night's master: `rtf`, seconds, and whether the voice stage stayed
   under the client's 1800 s (`ns_common/tts_client.py:87-89`).
2. `engine_release_gpu` lines in the dashboard log (`ns_dashboard/svc_engine.py:390`) during the night or morning;
   none means "not used". If used: the holder job's state at that moment.
3. The task's logon type (F-F5 part 1) and `/api/status` `schedule_mode`. Note: `schedule_mode` now reaches the
   dashboard (`ns_dashboard/svc_engine.py:192`, `ns_dashboard/state.py:133`), so this row looks out of date.
4. `dashboard_jobs` rows cancelled within 60 s of their start; none means "not needed".
5. Whether "Create it now" was pressed (dashboard log); the kit never presses it (it would use the setup voice).
6. `NS-E314` in the dashboard log.
7. Two-model mode only: `runtime.json` `llama.story_flags` against the story llama-server's command line (pid from
   `/api/status` services, `Get-CimInstance Win32_Process` for that pid). One-model mode: "-".
8. `/api/status` `channels[].tonight` text, recorded as seen.
9. "-" (no run can settle it).
10. The update JSON line of F-F12: whether `release_notes` was present (`ns_supervisor/cli.py:245-246`).
11. `ctx.cli(["studio", "dashboard", "--help"])`: exit code and text.
12. `dashboard_jobs` rows with `status = 'failed'`: whether each message carries an NS code.

**(c) Starts and stops.** Nothing beyond one `studio --help`.

**(d) Waiting.** After the night, and after F-F12 for row 10. A row still waiting is written "pending" with why.

**(e) Zip.** `f_f13_rows.md`: the twelve cells ready to paste, each with its source.

**(f) Laptop.** Real: every read, against the engine-test install's own logs and rows.

**(g) Person.** None.

---

## F-F14 `quiet_flatness` of the first real night

Script `f_f14_flatness.py`, phase morning.

**(a) Parts.** 1 the values from the QC files; 2 the same values from the logs; 3 the context the threshold needs.

**(b) Effects and pass conditions.**

1. `data\jobs\<job>\qc\audio_flatness.json` of every night job (`QC_FLATNESS_FILE`,
   `ns_studio/policy/final_checks.py:45`; written at `:383-403`): `items.<item>.quiet_flatness` and `warned`.
   Pass: one value for the master and for every Short that was rendered.
2. The night's `logs\studio-<date>.jsonl` lines `audio_flatness` and `audio_flatness_warning`
   (`final_checks.py:405-421`). Pass: one line per item, equal to the file to 4 decimals.
3. Recorded with them: `TONAL_FLATNESS_MAX` (0.1, `final_checks.py:44`), the count under it, min, median, max, and
   the voice engine from F-F2 (the values only matter on real qwen-tts narration).

**(c) Starts and stops.** Nothing. Reads only.

**(d) Waiting.** Deferred until the night's jobs exist.

**(e) Zip.** Every `audio_flatness.json` of the night and the matching log lines.

**(f) Laptop.** Real: all three parts against a hand-made night (fake-voice values, meaningless but read).

**(g) Person.** None.

---

## Summary table

| Box | Script | Duration (estimate) | Deferred | Needs admin |
|---|---|---|---|---|
| F-F1 | `f_f1_install.py` | 8 min (hashing about 25 GB) | no | no |
| F-F2 | `f_f2_voice_engine.py` | 2 min | no | no |
| F-F3 | `f_f3_whisper_drift.py` | 1 min + 1 min | yes (night) | no |
| F-F4 | `f_f4_gpu_probe_lease.py` | 20 min (`ns verify` full, one Short) | no | no |
| F-F5 | `f_f5_nightly_task.py` | 2 min + 3 min | yes (night) | yes (the kit's one UAC, helper) |
| F-F6 | `f_f6_lease_expiry.py` | 25 min (10 min expiry wait, two Shorts) | no | no |
| F-F7 | `f_f7_assistant.py` | 8 min | no | no |
| F-F8 | `f_f8_code_launcher.py` | 4 min | no | no |
| F-F9 | `f_f9_window_host.py` | 8 min | no | no |
| F-F10 | `f_f10_listen.py` | 20 min (the person watches) | yes (night) | no |
| F-F11 | `f_f11_story_length.py` | 2 min | yes (night) | no |
| F-F12 | `f_f12_update_rollback.py` | 25 min | yes (next release) | no |
| F-F13 | `f_f13_system_gaps.py` | 3 min | yes (night, F-F12) | no |
| F-F14 | `f_f14_flatness.py` | 1 min | yes (night) | no |

Which parts wait for what, and who answers anything:

| Box | Reads files of | Laptop lease parts | Person |
|---|---|---|---|
| F-F1 | H-15 item 1 | - | none |
| F-F2 | H-15 item 2 | - | none |
| F-F3 | H-15 item 3 | - | none |
| F-F4 | H-15 item 1 | after H-10 (part 3) | none |
| F-F5 | H-15 item 4 | - | toast yes/no, interactive case only |
| F-F6 | H-15 item 5 | after H-10 (parts 1, 3) | none |
| F-F7 | H-15 item 6 | - | none |
| F-F8 | H-15 item 7 | after H-10 (parts 1, 3) | none |
| F-F9 | - | - | none |
| F-F10 | - | - | three questions per video |
| F-F11 | H-15 item 8 | - | none |
| F-F12 | H-15 item 9 | - | none |
| F-F13 | F-F5, F-F12 | - | none |
| F-F14 | - | - | none |

Run 1 is about 1 h 40 min without F-F12 and must end 30 minutes before the run time; it ends with the zip and the
sleep step. Run 2 is about 30 minutes, most of it F-F10.

## People steps

What the person does, in order. Everything else the kit does itself.

1. Install Nightshift from the window (before the kit).
2. Run `Nightshift-Partner-Check.cmd` and accept the one UAC prompt at its start.
3. Leave the PC alone for about 1 h 40 min while run 1 goes. Do not touch the Nightshift window during F-F9
   (about 5 minutes) and do not use Nightshift during F-F12 (about 20 minutes). The kit then closes the window and
   puts the PC to sleep itself, with the task's wake timer armed.
4. Next morning, run the same launcher again. Answer the three questions for the Short and the three for the master
   (F-F10).
5. Only if the nightly task ran in an interactive session (the NS-W205 case): one yes/no on whether a toast
   appeared (F-F5 part 7).
6. Send the zip the kit names.

Not people steps any more: sleeping the PC before 03:00 (the kit does it), typing a question into OpenCode (F-F8
uses the launcher's `-Prompt` switch, H-15 item 7), reading the story (F-F11 is measured and the product's critic
score is recorded), leaving an elevated console open overnight (F-F5 part 5 reads the nightly report, H-15 item 4),
closing the Nightshift window before bed (the kit does it).

## Product gaps: now order H-15 (and H-10)

The nine facts the product did not record are order H-15 for Builders (`refs/departments/head-orders.md`, H-15):
each goes into a file the product already writes. The boxes above describe their parts as reading those files and
mark them "after H-15 item N". The old gap numbers map as follows.

| H-15 item | Fact | Box | Old gaps |
|---|---|---|---|
| 1 | PC-check rows in `install-report.json`; `ns verify` never overwrites setup's `verify-report.json` | F-F1, F-F4 | 1, 13 |
| 2 | `verify-report.json` names the voice engine kind always and lists the qwentts DLLs found | F-F2 | 2 |
| 3 | A job records the device whisper ran on, and the reason when not CUDA | F-F3 | 3 |
| 4 | Nightly report: session kind, key source, start delay, keep-awake taken and released, engine exit | F-F5 | 4, 5, 6 |
| 5 | `lease_expired` in the job's files; test-only lease-time override in config | F-F6 | 8 |
| 6 | Assistant tool loop records per-step answer times in its transcript record | F-F7 | 9 |
| 7 | Code launcher `-Prompt "<text>"`, exits with the answer | F-F8 | 10 |
| 8 | Prompt token counts per LLM call recorded with the job | F-F11 | 11 |
| 9 | Release source configurable for tests (one place, refused unless dev mode or test flag) | F-F12 | 12 |

Not in H-15, so still open:

- Old gap 7 (F-F5): an S4U run cannot show a toast by design (`ns_supervisor/notify.py:73-75`). The box text says
  so in the tick PR (a session-0 run leaves the morning note); the toast yes/no stays with the person in the
  interactive case (part 7).
- Old gap 14 (H-10): `fake_gpu` is not built on any branch and starts after Builders' 0.1.15 items. Until then
  every lease part of F-F4, F-F6 and F-F8 is skipped on the laptop; they are marked "after H-10".

Settled by the Head's decisions of 2026-11-09, no longer gaps: F-F6 means "a dead child's lease does not stay
taken", proven on both routes; story quality in F-F11 is the product's own critic score, recorded.

## Runner needs beyond the six proposed members

1. Partner mode: no `LAPTOP.lock` and no `refs\tools` on that PC (`dev/prove.py` requires both today). Start the
   engine through the product and stop only an engine the runner started, with `ns stop`. Never `nsproc -Down`
   (a pattern kill) there.
2. Evidence root under the home, one folder per kit session, not keyed by version. Today it is
   `<home>\..\..\runs\proofs\<version>` (`dev/prove.py:337, :648`): on a default home that is on C:, and F-F12
   changes the version mid-kit.
3. Run from a copy outside `<home>\app`, with a Python the update does not replace (F-F12 swaps `app` and re-syncs
   the venvs).
4. Kit order and phases: `order`, `phase` (evening, morning, release), F-F5's morning part before any engine start,
   F-F10 last; a passed proof is not re-run on the next launcher run, a waiting one is.
5. Time guard: do not start a proof whose `estimate_s` reaches into run time minus 30 minutes; defer it instead.
6. Defer data: `ctx.defer(reason, data)` and `ctx.deferred` (time and data of the earlier defer, including the
   time the kit slept the PC). The scratch folder is wiped at every run (`dev/prove.py:393-397`) and cannot hold
   it.
7. Part results: `ctx.part(name)` with its own pass, fail or skip; strict mode on the partner PC (a skipped part
   fails the proof), non-strict on the laptop. A part waiting for an H-15 item or for H-10 is skipped with that
   name as the reason.
8. `ctx.get(service, path)`: read-only GET on the install's own service ports (`ctx.ports`), for `tts /health`.
9. `ctx.api(..., raw=True)` or a `to=path` form: `/api/voices/preview` answers WAV bytes, which `ctx.api` decodes
   as text today.
10. `ctx.db(sql, db="assistant")` for `data\assistant.db`.
11. `ctx.events(method, path, body, until, timeout)`: a bounded event-stream reader with a time per frame, for
    `/api/assistant/chat` and `/api/events` (`ctx.api` refuses streams by design).
12. `ctx.stop(proc)`: stop one spawned process tree in the middle of a proof (F-F6 part 1).
13. `ctx.interrupt(pid, exe_under, record)`: stop one product pid read from a product record, refused unless the
    program is under `ctx.home` and the record names it (F-F6 part 3, last resort in F-F9).
14. `ctx.uia(hwnd)`: UI Automation bound to one window handle: find by name, Invoke, read and set the window state,
    read text. No global input. The partner PC has no Playwright, so window reads there (F-F5 part 7, F-F9, and
    closing the dashboard window before the sleep step) go through this, not `ctx.page()`.
15. `ctx.tool` allowlist additions: `powercfg /q SCHEME_CURRENT SUB_SLEEP`, `powercfg /waketimers`,
    `schtasks /query /v /fo LIST`, and `ffprobe`/`ffmpeg` from the install's own `bin`. A missing program
    (`nvidia-smi` on the laptop) returns "absent", not an exception.
16. Elevation: the launcher asks for UAC once, at the start of the kit, and starts one elevated helper that runs
    only the admin reads on the allowlist (`powercfg /waketimers`) and the sleep step. The proofs run as the
    person. No night watch and no kit left open overnight.
17. `ctx.sleep_pc()`: puts the PC to sleep (not hibernate) after the checks of section 1, "Sleep and UAC", and
    records the time. On the laptop it records the call and does not make it (GUI safety rule).
18. Laptop dry-run of F-F10 and F-F5 part 7: `ctx.ask` answered from a scripted answer file, `ctx.show` recorded
    without opening.
19. The launcher, the progress line per proof, and the zip writer that refuses `config\` and any secret file.
20. Packaging: `proofs\`, `dev\prove.py`, `dev\prove_ctx.py` and `Nightshift-Partner-Check.cmd` in the release
    (`dev/make_bundle.ps1`), so the PC needs nothing else.
21. The fake GPU switch the runner turns on for a CPU install (H-10, after Builders' 0.1.15 items) must exist, and
    the runner must refuse to set it on a normal install. The same refusal applies to the H-15 item 5 lease-time
    override.
22. A fake GitHub release endpoint under `proofs/_fakes` for F-F12 on the laptop, and the runner setting the H-15
    item 9 variable only on dev and engine-test installs (after H-15 item 9).
