# Changelog

All notable changes to Nightshift are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/). Milestones map to minors: 0.1.0 is M0, 0.2.0 is M1, and so on
(`docs/ENGINEERING.md` section 7).

## [Unreleased]

- Clipper import: a video that already sits in its hash folder with its sidecar is recorded without copying the
  sidecar onto itself (WinError 32 on Windows; it stopped every dev clip job from an upload at 1 s).
- Dev mode: a transcript sidecar dropped beside an upload moves with the video into its hash folder (it was
  deleted with the upload folder when the clip job started).
- Dev mode: a clip job from an upload finds a transcript sidecar dropped next to the stored file after the
  upload (it was only read when recorded at import, so every dev clip-from-upload stopped with NS-E406).

## [0.1.11] - 2026-11-05

From the clean-teardown test loop on the 0.1.10 dev install (one test at a time, each torn down and the
process list checked; report `D:\NightShift-refs\test-loop-2026-11-05.md`). Send `Nightshift-Setup-0.1.11.zip`;
an install started with 0.1.6 to 0.1.10 resumes under it (INSTALL.cmd, Continue).

- Stop-all now closes the Nightshift window too (it stopped the engine and the dashboard and left the WebView2
  window on a dead page, 150 MB, in the 0.1.10 teardown test); the window also closes itself when its server
  stops answering for 15 s (the nightly task, an update, uninstall).
- Create wizard: the Review step keeps its Back button (it had only Start; the way back was the step rail).
- `studio run --daily` says "nothing due tonight" on the console when the planner has nothing to make (it printed only the backup path).

## [0.1.10] - 2026-11-04

From the browser-driven user journey on the 0.1.9 dev install (docs/DEV_MODE.md; every page and flow
clicked through in Chrome, the Pixabay key tested live). Send `Nightshift-Setup-0.1.10.zip`; an install
started with 0.1.6 to 0.1.9 resumes under it (INSTALL.cmd, Continue).

- Dev mode: the fake language model now turns plain requests in the Assistant into tool calls ("fetch two
  clips of a dark hallway", "what is the status", "pause Dev Night Tales", "where is the footage cache",
  "make a Short for ..."), answers in text after the tool ran (the paths it returned, or the Yes/No card it
  raised), and the Library's footage fetch uses the synthetic clip source when no key is stored. The
  Assistant's whole path (confirm card, tool run, result) can now be tried on a dev install.
- Create page: a finished job card (done, stopped or cancelled) has a Hide button; the job and its videos stay,
  the card leaves the Earlier list (`POST /api/jobs/{id}/dismiss`). Library > Footage shows each clip's file
  path and has a "Fetch 2 clips" button for the search words (the Assistant's `fetch_footage`, by hand).
- Settings > Stock footage keys: a saved key is checked with the service right away (the same check as the
  installer, with the product User-Agent) and the toast says "Pixabay accepted the key"; a key the service
  rejects is not stored (the working key, if any, is kept) and the dialog says to paste it again; when the
  service cannot be reached the key is stored and the toast says it could not be checked.
- Channel card: "This week N of P made" counts the jobs that made a full video (the plan is full videos a
  week); a Shorts-only channel counts every job. The dev channel showed "5 of 1 made" after four one-Short
  jobs and a clip job.
- Assistant: after a Yes the card now says what happened, not only "Done": the file paths a tool made (fetch
  footage), the job id it started, or its note. The model gets no turn after a confirmed action, so "fetch
  two clips and show me where they are" used to end with the paths only in the transcript.

- Dev mode: a clip job on any video now runs through selection, cutting and rendering: the fake model's
  `clip_select` answer is fitted to the window in the prompt when the recorded fixture's moments (62-262 s of
  a test recording) fall outside it (the first dev-mode clip job failed with NS-E407). The clipper's dev-mode
  transcript sidecar is `<video stem>.transcript.json` next to the video (docs/DEV_MODE.md).
- Clips stage: when every candidate for a scene is ruled out by the reuse rules (14 days on the channel, 30
  across channels) the scene reuses footage from an earlier video with a warning (`NS-W405`) instead of
  failing the whole master after the writing and the voice (the first dev-mode nightly run died with
  NS-E402 "24 candidates for 2 slots"); when the pool is smaller than the video (a master plus its Shorts on
  a niche with few clips) a clip may repeat inside the video, again with the warning.
- Nightly planner: a master job that failed no longer counts as the week's master (its combo row did, so the
  channel went silent for the whole spacing, a week at one master a week); finished long videos count, and a
  long job started in the last 12 hours counts as still running.
- Quality check: a derived Short that fails QC (the first dev-mode master died with "s5: duration", 61 s
  for a 50 s bucket, after the master and four Shorts were rendered) is left out with `NS-W406` and listed
  in `qc\\dropped.json`; the master and the other Shorts are published. The master's own QC failure, or
  every Short failing, still stops the job.

## [0.1.9] - 2026-11-04

From the clean dev-mode install of the official 0.1.8 zip on the orchestrator's laptop. Send
`Nightshift-Setup-0.1.9.zip`; an install started with 0.1.6 to 0.1.8 resumes under it (INSTALL.cmd, Continue).

- A stock-footage key saved by an earlier setup (Credential Manager or `config\\secrets.dpapi`) that the
  service rejects no longer stops setup: it is forgotten with a notice, and Settings takes a new one (the
  first clean dev-mode install of 0.1.8 died with NS-E502 on a placeholder key left behind). A key given in
  `answers.json` that is rejected still stops setup. Settings clears a key when it is saved empty, and the
  uninstall forgets both copies of the keys.

## [0.1.8] - 2026-11-04

The first release that ran end to end on a real PC before shipping: a dev-mode install on the orchestrator's
laptop (`INSTALL.cmd --dev`, docs/DEV_MODE.md) exercised stage 1, the setup server and window (WebView2 host),
every step, the shortcuts, the engine, the dashboard, a job with a real FFmpeg render, an update over the
install, and the uninstall. Also two review rounds (installer, studio) and the assistant fit work. Send
`Nightshift-Setup-0.1.8.zip`; an install started with 0.1.6 or 0.1.7 resumes under it (INSTALL.cmd, Continue).

- First dev-mode run on a laptop (`INSTALL.cmd --dev`, unattended): the welcome step waited for Enter and
  died with `EOFError` when standard input was not a console; the console UI now treats end-of-input as
  "not interactive" and takes the defaults, and an unattended run never waits for Enter. A setup that fails
  before the engine venv exists could not write the diagnostics zip (`ns doctor` is not installed yet); the
  wizard now writes a plain zip of the logs, install state, answers and bootstrap files itself
  (`installer/stage2/diagzip.py`).
- Launchers (`launchers/_common.ps1` `Get-NsHome`): the install home is found from the launcher's own folder
  (`<home>\app\launchers`) when `NIGHTSHIFT_HOME` is unset or points at a folder without `config\runtime.json`;
  the first dev-mode dashboard launch from a process started before setup had set the variable looked under
  `%LOCALAPPDATA%\Nightshift` and stopped with "Supervisor venv not found".
- Dashboard: an `/api/health` route for the supervisor's readiness probe (`ServiceDef.health_path`); without it the
  dashboard service stayed "starting" and the engine pill said "Loading dashboard..." for ever (first dev-mode
  launch). Dev mode no longer needs a stock-footage key to start a job (its clip source is a fake).
- Renders are bitrate-capped (`-maxrate 12M -bufsize 24M`, NVENC and libx264, masters, Shorts and clips) and
  the CPU encoder never uses `ultrafast`: the first dev-mode Short came out at 134 Mbit/s (900 MB for 54 s)
  with `libx264 -preset ultrafast -crf 21` under the film-grain filter.
- Story validators: the repeated-opening check (`opening_similarity`) uses cosine >= 0.9 instead of 0.8 (nomic
  similarities between two openings of the same niche sit around 0.8, a false rejection fails the job after
  the writing), and dev mode skips it (the fake LLM tells the same story every run: the second dev job failed
  with NS-E303 `cosine 1.00`).
- Unattended and console setup runs write `logs\\install-report.json` too (built from `install_state.json`,
  answers without keys, the last error), so the owner gets the same file whichever way setup ran.
- The setup window opened over an install finished by the console or unattended path (which has no samples
  step) showed "Setup is halfway" instead of Done; `v01_samples` is now recorded as skipped for such an
  install and the window opens on Done with Open dashboard.
- The setup window wrote naive local timestamps into `install_state.json` while the console wizard wrote UTC
  ones; subtracting the two for the Done-screen duration raised `TypeError` and the window never started
  over a console-finished install (exit 14). Both now write UTC stamps and the arithmetic tolerates a mix.
- `install_state.json` carries the version of the program that ran setup last: after an update through
  INSTALL.cmd the Settings page showed the old install version.
- Dashboard Queue page: the review list reloads when the outbox counts change (it said "Nothing to review"
  under a "Ready 2" tab after the first dev job), and the "Next automatic run" countdown and "Tonight's run
  starts at" line only show when the nightly task is registered (the pill said "No nightly task" beside a
  13 h countdown).
- Uninstall also removes the setup environment and the run/state/tmp folders under `%LOCALAPPDATA%\Nightshift`,
  the install folder's `state` and `tmp`, and (with "delete my videos") `stories`; the first dev-mode uninstall left
  them behind. The "kept" line lists `stories` when it stays.

### Installer dev mode (`orchestrator/installer-dev-mode`)

`INSTALL.cmd --dev` (or `NIGHTSHIFT_DEV_MODE=1`) runs the real installer end to end on a PC with no NVIDIA card and
without downloading any model or PyTorch, so stage 1, the setup server and window, steps s01 to s10, the shortcuts, the
first dashboard launch, the supervisor and a "Create it now" job can be exercised on a laptop. It is not `--simulate`:
the steps run for real; only what a laptop cannot do is replaced. Read `docs/DEV_MODE.md` for what it proves and what it
does not.

- **Stage 1** (`-Dev`): the graphics card, driver and video memory rows are information rows, memory warns under 4 GB,
  disk needs 10 GB; the profile is always `cpu-dev`; the hand-over has `dev_mode: true` and the setup server gets `--dev`.
- **Setup window**: the plan lists the CPU build and the models as "(stub, dev)"; `state.devMode` shows
  "DEV MODE: fake engines, no models" in the footer of every screen, and the Done screen lists stubs and fakes.
- **Steps**: s02 downloads the CPU llama.cpp build, yt-dlp and the window files and skips cudart, OpenCode and qwentts.cpp;
  s03 writes 4 KB stubs with the manifest names (`.dev-stub` marker, hash `dev-stub`, never accepted by a real install);
  s04 installs the new `dev-install` extra (studio without faster-whisper, PyAV and CTranslate2; no cuBLAS/cuDNN, no
  `tts` venv); s05 and s06 are skipped; s08 verifies against the fakes; s09 registers the night task disabled
  (`ns schedule install --disabled`, `schedule.json` mode `disabled`). Installing for real over a dev install redoes the
  later steps and replaces the stubs.
- **Fake engines** (`runtime.json` `dev_mode`): the supervisor starts `ns_supervisor.devfake` (llama, embed) and
  `ns_tts.server --engine fake`; `ns verify` passes against them (libx264 replaces NVENC, Whisper and VRAM are dev-mode
  passes) and its report says `dev_mode`; studio and dashboard jobs run on `FAKEABLE` with a real FFmpeg render.
- **Contracts** (CR-DEVMODE-1): `Runtime.dev_mode`, `InstallState.dev_mode`, `GateReport.dev_mode` and their schemas.
  `ns tune` and `ns doctor` know the mode; the Settings page hides the flag.

### Installer round 3 (`orchestrator/installer-round3`, from the round-3 installer gap report)

- **INSTALL.cmd over a finished install no longer opens an empty window (blocker B1, a 0.1.7 regression).** On
  start the setup server compares the installed program (`<home>\app`, the appcopy fingerprint: VERSION plus hashes
  of `manifest.json` and `pyproject.toml`) with the extracted one. A different program: b02, b03, s04, s08, s09
  and s10 go back to pending (in the window and in `install_state.json`), the window shows the Welcome resume
  screen, and Continue copies the new program (the old one stays as `app.prev`), refreshes the environments, runs
  the final test, remakes the shortcuts and the night task, and ends on Done. The same program: Done opens at once
  with the result a finished run builds (home, outbox, night run time, diagnostics zip) and **Open dashboard**
  works. An install that stopped half way is left alone. This restores the documented paths "update by running
  INSTALL.cmd from the new zip" and "open Nightshift by double-clicking INSTALL.cmd".
- Resume on a nearly full drive (L1): the free-disk row of a locked resume is a warning, not a blocker, when free
  space plus what setup already holds under the install folder reaches the 40 GB limit ("N GB free on D:; setup
  already holds X GB here"). The 2 GB guard during downloads and `NS-E104` from the steps still protect the run.
- Final test (L2): `s08` runs `ns stop` before the port check, so a detached engine left by a closed console is
  stopped instead of ending as `NS-E116` against Nightshift's own python.exe. `NS-E116` no longer says "change
  the port in Settings" (there is no such setting): "Close the program using that port (the message names it), or
  restart the PC, then press Retry".
- The unattended run (L3, `answers.json` next to INSTALL.cmd) holds `SetThreadExecutionState` like the window does,
  and takes a lock (`<data>\bootstrap\setup.lock`, released by Windows if the process dies): a second unattended
  setup says "Setup is already running on this PC" and exits with code 15 (A1-#14).
- Stage 1 (P2, P3, A1-#25, A1-#26): it uses the pinned uv (`manifest.json` `bins.uv`, 0.13.6) and downloads it when
  the copy it finds, including one on PATH, is another version; it asks uv for the managed Python 3.12 only
  (`UV_PYTHON_PREFERENCE=only-managed`, also in `ns_common.uv.uv_env` for b03, s04 and `ns update`) and never takes
  a Python from a `WindowsApps` folder (Microsoft Store alias); it runs `Unblock-File` on the extracted programs
  (`.dll .exe .ps1 .cmd .pyd`) so the WebView2 helpers load and the setup window no longer falls back to Edge on a
  browser-downloaded zip; the setup server gets 60 s to answer (was 20) and the splash says it is still waiting
  after 15 s.
- **Open dashboard (P1, A1-#20):** the engine the Done screen starts no longer inherits stage 1's `PYTHONPATH`
  (the extracted folder) or `NIGHTSHIFT_BOOTSTRAP_UV`, so it and its dashboard import the program from
  `<home>\app` and keep working when the extracted folder is deleted. The same variables are cleared in
  `Start-NsSupervisor`. The engine started by a shortcut and the 03:00 task now run with the install home as their
  working directory, not the program folder, so an update is no longer blocked while either runs.
- Windows Security (P4): `NS-E113` for Smart App Control (WinError 4551, "Application Control policy") says that it
  blocks programs it does not know and cannot allow one file: "Turn it off in Windows Security > App & browser
  control > Smart App Control settings (this cannot be turned back on without reinstalling Windows), then press
  Retry". Antivirus blocks keep "Protection history, allow the file". `contracts/errors.json`, the operator copy,
  the window's fallback copy and OPERATOR.md say both.
- Browser windows (P6, P7): the Edge or Chrome fallback of the Dashboard launcher gets `--no-first-run
  --no-default-browser-check`. The Dashboard launcher starts the WebView2 window as its own hidden PowerShell
  process (`-STA`, working directory = install home) and ends once the window reports ready, instead of keeping
  the window in its own process: no "Windows PowerShell" console is left behind the dashboard when Windows Terminal
  is the default console, and closing it no longer closes the dashboard. A host that exits 3 (WebView2 missing) or
  fails still falls back to the browser app window.
- One engine per install (P8): `python -m ns_supervisor.app` takes `run\supervisor.lock` (a lock Windows drops with
  the process) before anything else; a second start prints "The Nightshift engine is already running" and exits 0
  without touching `run\supervisor.json`, which the failed second start used to delete on its way out.
- Keys (P9): `s01` already puts the stock footage keys in Credential Manager and `config\secrets.dpapi` as soon as it
  runs, so a restart after s01 keeps them; the server's in-memory copy only covers the minutes between Start and
  s01. A resumed window now reports what is really stored (`pexelsKeySet` / `pixabayKeySet` from `secrets.dpapi`,
  not "set because the questions step is done") and, when neither key is there, adds the notice "No stock footage
  key is stored. Add a Pixabay key (free) in Settings > Stock footage keys before the first video".
- A2-#13: `dev/make_bundle.ps1` already rewrites `manifest.json` `version` from VERSION; `tests/test_installer_rerun.py`
  now proves the chain (bundle manifest, `<home>\app` copy, wizard manifest) makes `s04` and `s08` run again after
  an update through INSTALL.cmd. OPERATOR.md step 4 says that an `answers.json` makes setup unattended (no window;
  `INSTALL.cmd -Ui` for the window), and its update and key paragraphs describe the new INSTALL.cmd behaviour.
- Deliberately left (listed in the PR): system proxy and TLS-scanning antivirus (A1-#15 / A2-#12), a retry with the
  CUDA 12.4 pair after `NS-E203` (A1-#17), the PyTorch fallback limits (A1-#23, A1-#24), a constraints file or
  `uv.lock` (A1-#12), and a key field on the Welcome resume screen (P9 only reports a missing key).

### Studio round 3: engine stays up through a job, keep-awake at night, no-key refusal up front (`orchestrator/studio-round3`)

From the third studio review of 0.1.7 (`gaps-round3-studio`). Nothing here has run on the GPU PC yet.

- **The engine no longer leaves in the middle of a job (L1).** After `captions` every stage holds no GPU service, and
  the engine's 30-minute idle clock only saw acquires and releases, so a long download-and-render stretch made it exit
  (the next job then failed with NS-E202 "supervisor unreachable", every night after the first video). `POST /heartbeat`
  now restarts the idle clock for a `studio:` run or the dashboard job worker (`dashboard:job`) even when it holds
  nothing. `StudioAcquirer` beats from its first `acquire` until `run_finished` (with the run's owner while nothing is
  held), the dashboard's job worker pulses the engine every minute while a job runs, and an `acquire` that meets "supervisor
  unreachable" connects again once (the nightly run starts a new engine) before it fails.
- **The PC stays awake (L2).** `ns_common/keepawake.py` (`ES_CONTINUOUS | ES_SYSTEM_REQUIRED`, per thread, released in
  `finally`) is held around `run_daily`, around `studio run`, and for the length of every job on the dashboard's worker
  thread. The installer keeps its own identical copy (its web package may import only the standard library); a test
  keeps the two equal.
- **No stock-footage key means no story video, said up front (L3).** The nightly planner (per job, before a combo is drawn),
  `studio run` and the dashboard's Run now / Retry refuse with NS-E502 "No stock footage key. Add a Pixabay key (free)
  in Settings > Stock footage keys" before any GPU work. When both sources fail, the Pixabay error is raised first
  (Pexels gives out no keys). The setup text now says videos cannot be made until a key is added.
- **The voice stage's time limit follows this PC's speed (L4).** `ns verify` stores the slowest voice mode's real-time
  factor in `config/tts_speed.json`; the batch limit is 120 s plus `max(3, 6 x rtf x 1.5)` s per line (unchanged without
  a measurement), and a batch that times out is retried once at twice the limit, resuming from the items the service had
  finished.
- **Embed server off the card (P1)**: started with `CUDA_VISIBLE_DEVICES=-1`. **llama-server always `-np 1` (P2).**
- **Pending setup voice card (P3)** says a video cannot be voiced until the voice exists (NS-E311) and points at
  "Create it now".
- **The Assistant's GPU hold is leased (P4)**: it beats every 30 s during a turn and the engine reclaims a silent hold
  after 10 minutes, like a studio lease.
- **Retry from where it stopped (P5).** A failed story job on the Queue / Create pages has a button that continues the
  same studio job from its failed stage (`POST /api/jobs/{id}/resume`, assistant tool `resume_job` with a confirm card).
- **Updater (P7).** Settings > Update from a file now installs `Nightshift-Update-<version>.zip` together with its
  `.sha256` (`POST /api/update/file`, then `ns update <zip>`); the release notes say the checksum is required. A GitHub
  error status (403/429 rate limit, 404, 5xx) is NS-E118 and says so instead of "check the internet connection"; the
  Settings page shows the `error` line of `ns`, not the `hint:` line after it.
- **Reject moves the files (P8)**: the dashboard's Reject takes the video, its sheet and thumbnails out of the Outbox
  (same code as `studio review reject`, into the `rejected` folder of the Nightshift home, under channel, day and job). **Whisper failures are NS-E411**
  (was NS-E202).

### Installer: first-run support (`orchestrator/installer-first-run`)

What the owner needs to help a partner who cannot describe what went wrong, and the copy a non-technical partner reads.
This branch edits `contracts/errors.json` (two hints), so it is an `orchestrator/` branch.

- **Copy status** (every screen after Start: Welcome resume, Downloading, Installing, Testing, Failed, Done). One click
  puts a line like `Nightshift setup 0.1.7 · step 6/14 Download AI models · 41% · 12:03 elapsed · last error NS-E105 (A
  download site cannot be reached)` on the clipboard (new action `copy-status`; `installer/stage2/web/report.py`). The
  last error survives a Retry.
- **`install-report.json`** next to `verify-report.json` (the `logs` folder of the install; the setup data folder before the install
  folder exists). Written at every failure, stop and finish: version, outcome, failed step, code, attempts, last error,
  machine (Windows, graphics card, memory, driver, CUDA, free space), choices, engines (voice engine, captions device,
  encoder, fallbacks, schedule mode), warning codes, every step with its status and seconds, the answers without keys.
- **One diagnostics zip with the setup evidence.** `ns doctor --bundle` and the setup window's fallback now both hold
  `install-report.json`, `setup/wizard_flags.json`, `setup/answers.json` (keys removed), `setup/preflight.json` (the
  hardware check), `setup/setup-server.err` and `setup/bootstrap/*`. The name carries the minute
  (`nightshift-diagnostics-2026-11-04-1203.zip`), so two zips on one day do not overwrite each other. The zip the wizard
  makes before the last step gets the final report added when setup ends.
- **Failed screen.** Buttons Save a help file (was Copy diagnostics), Open log folder and, for NS-E113, Open Protection
  history; one sentence says where the file is and who gets it ("Send this file to <name>", from the new
  `installer/support.json`, or "the person who sent you Nightshift" until the owner fills it in). Wake timers (NS-W107)
  get an Open power settings button. New action `open-windows-page` (`power`, `protection-history`; names only).
- **Done screen.** Three numbered next steps (Open dashboard; Create it now once with the graphics card idle; tomorrow
  morning's note and the schedule), the "Add a Pixabay key in Settings before the first video" reminder when no key is
  stored (`result.stockKeyMissing`), warnings now show their instruction next to the title, "What was installed" is a
  fold-out.
- **Copy fixes** (`installer/ui/src/copy.ts`, `operator_copy.json`, `contracts/errors.json`, `OPERATOR.md`, Settings):
  no assumed download speed or "black window"; Choices says which choices are final; model cards without "context";
  step and check names a partner understands ("Program parts", "Story memory", "graphics card video"); "Copy full
  test report (for the developer)"; uninstall no longer says "Nothing else was changed on this PC" and lists what stays
  (settings and logs, keys, Visual C++ runtime, FFmpeg, Windows Terminal, the Python download cache); Settings names the
  `Nightshift-Update` zip and the folder UNINSTALL.cmd is in. The UI's code table is now `operator_copy.json` itself, so
  the two can no longer disagree.

### Assistant slice 1: make it fit (`feat/assistant-fit`)

The Assistant overflowed the local model's 16,384-token context in long chats (about 9-10k tokens were used before any
history: the prompt, a 1,700-token system map, all 59 tools, and tool results of up to 6,000 characters).

- **Prompt budgets** (`assistant/prompt.py`). Every request is planned against the context from `runtime.json`
  (`llama.flags.ctx`, 16,384 when unknown): 16k = reply 1,200, system 1,300, tools 2,500, context block 2,500, margin
  1,000, history the rest (7,884); 32k = 1,500 / 1,300 / 3,000 / 4,500 / 1,500 / 20,968. One estimator everywhere
  (`approx_tokens` plus 10 %). The reply limit (`max_tokens`) now follows the plan.
- **Stable system message, live facts on the question.** The system message is the same bytes for the whole run of the
  app (no live state), so llama-server keeps its cache; engine state, GPU owner, videos waiting, jobs, next run,
  channels, the page and a reminder are attached to the latest user message once per turn, labelled as data. They are
  never stored in the transcript.
- **History compaction.** Tool results of earlier turns become one-line digests; this turn's results are kept up to
  1,500 tokens and shrunk by re-building the JSON (summary fields kept, lists and strings shortened), never cut through
  the middle of a string. Over budget, the oldest whole turn goes first, so a tool call is never split from its result.
  Stored results are no longer truncated at 6,000 characters (they are rebuilt smaller above 20,000).
- **Topic router and trimmed tools** (`assistant/router.py`). No model call: the page, keyword phrases, ids, `NS-` codes,
  channel slugs and the previous turn's topics pick at most two topics. The model sees 8 core tools (`status`, `find`,
  `where_is`, `open_path`, `search_docs`, `diagnose`, `explain_error`, `list_tools`) plus at most 10 tools of those
  topics: at most 18 instead of 59. Calling `list_tools(topic)` adds that topic's tools for the next step of the turn.
  `fetch_footage` is in the library topic; the 12 workshop tools appear only with the series topic.
- **Task-shaped read tools** (`assistant/composite.py`): `diagnose` (job, failed stage, plain meaning of the code, log
  lines, policy retries, next step), `review_brief` (policy notes, disclosure, earlier rejections on the same options;
  the owner still decides), `what_worked` (best options per pool with the number of videos and a confidence word),
  `tonight_plan`, `video_stats`, `get_story` and `open_path` (opens a folder inside the Nightshift home only).
- **Model alias `code`** instead of `story` for the Assistant: in two-model mode `story` is the 21 GB writing model.
- **Docs scope.** `search_docs` and `find` serve `README.md`, `OPERATOR.md`, the error catalogue and a generated list of
  what the Assistant can and cannot do. `BUILD_SPEC.md` and `docs/ENGINEERING.md` are no longer indexed (they describe a
  build, not this install). The system prompt is version 2.
- **Pasted keys are blanked** (`assistant/redact.py`) before they are stored in `assistant.db` or sent to the model;
  the next turn tells the model to point to Settings > Stock footage keys.
- **Offline evaluation** (`assistant/eval.py`, `tests/assistant_eval/`): the 20 partner questions on the scripted fake
  model assert the topics and tools offered, the retrieval scope, the token budget of every request (also after a
  30-turn history) and the confirm gate. No database schema change.

## [0.1.7] - 2026-11-04

Hotfix after the partner's first run of 0.1.6 and two fresh audits (install blockers from the Start button
onward, and installer functionality). Do NOT send 0.1.5 or 0.1.6 to a new PC; send `Nightshift-Setup-0.1.7.zip`.
An install started with 0.1.6 resumes under 0.1.7: run INSTALL.cmd from the new zip and press Continue.

### 0.1.7 install-path fixes (`orchestrator/hotfix-0.1.7-install`, from the second install audit)
- faster-whisper and PyAV (blocker): the `studio` extra now pins `faster-whisper>=1.0,<1.3` and
  `av>=11,<19`. faster-whisper 1.2.1 calls `av.open(metadata_errors=...)`, which PyAV 19.0 removed, so on a
  fresh install `uv` resolved av 19.0.2 and every transcription raised `TypeError`. The partner would have
  reached the end of the roughly 20 GB download, then seen "speech recognition failed on the processor"
  (`NS-E202`) in the final test, and every caption and Clipper job would have failed the same way. The caps
  reach the one venv that runs faster-whisper through `s04`, the setup venv (`b03`) and `ns update`, which all
  install `nightshift[studio,...]` from `pyproject.toml`; `tests/test_install_caps.py` fails if either cap goes.
- The PC no longer sleeps during setup: the setup server holds `SetThreadExecutionState(ES_CONTINUOUS |
  ES_SYSTEM_REQUIRED)` on the thread that runs the steps and releases it when the run finishes, fails or is
  stopped. The partner would have found the download stopped after a 1 to 2 hour wait. A downloader that gets a
  network error now probes the site's host and waits (2, 4, 8 ... up to two minutes) for the network to come
  back before it counts the attempt, so a PC that woke from sleep no longer burns its three tries in seconds
  and shows `NS-E105` ("a required download site is unreachable"). The row reads "No internet right now,
  waiting for it to come back".
- Visual C++ runtime: stage 1 (the black window, in front) now installs it when it is missing: it downloads
  the pinned `bins.vcredist`, checks the SHA256, says "Windows will ask for permission to install the
  Microsoft Visual C++ runtime; press Yes", runs it with `Start-Process -Verb RunAs -Wait` and reports the
  result. The partner would have seen the old prompt only as a flashing taskbar shield behind the setup
  window, a row reading "installing" for up to 50 minutes and then `NS-E114`. A declined prompt prints the fix
  and setup goes on; the setup window keeps its own path as a fallback with a 10 minute wait, and `NS-E114`
  now says "Look for a flashing shield icon on the taskbar and press Yes, or install it from
  aka.ms/vs/17/release/vc_redist.x64.exe and press Retry".
- Install folders with characters outside printable ASCII (an Arabic or accented folder name) are refused in
  the setup window (instantly, and by the server) and in the console question (`NS-E117`; the question asks
  again) with "Use a folder name with plain English letters, digits, spaces, - and _ (the model programs
  cannot open other paths)". llama.cpp, qwentts.cpp and CTranslate2 open model paths through the ANSI code
  page, so such a home made `ns tune` fall back to defaults and the story model fail its test (`NS-E203`).
- A program Windows refuses to start (Smart App Control `WinError 4551`, an Application Control policy
  `1260`, antivirus `225`/`226`, "the file contains a virus") is `NS-E113` with "Windows blocked <file> when
  it was started ... Allow it in Windows Security > App & browser control, then press Retry", from the engine's
  service start, the installer's command runner and the failed checks of the final test. The partner would
  have seen "a service failed" (`NS-E202`), "the story model did not pass" (`NS-E203`) or a quiet switch to the
  7 GB PyTorch voice engine.
- uv failures show uv's last 15 lines on the Failed screen instead of a bare "A required download site is
  unreachable"; a full disk is `NS-E104` and a blocked program `NS-E113`. Every uv call (setup venv, `s04`,
  the PyTorch fallback, `ns update`) now uses `UV_CACHE_DIR=<home>/cache/uv`, so the roughly 3 GB uv cache
  fills the install drive, not C:. If that folder cannot be made, `s04` checks that C: has 8 GB free first
  (`NS-E104`). uv's own managed Python (about 30 MB) stays where uv puts it.
- Busy ports: before the final test (`s08`), before an engine start and before a service start, the ports
  (story model 8080, memory index 8081, voice 8091, engine 8099, dashboard 8100) are bind-tested. A taken one
  is the new `NS-E116` naming the port and the program that holds it (psutil) and saying "Close that program
  or change the port in Settings". The partner would have seen "the story model did not pass" with no port.
- Final test: a stale `logs/verify-report.json` is deleted before each `ns verify`, so a Retry whose verify
  died before writing can no longer pass on an old report. The supervisor's start timeout (a fixed 120 s, too
  short to load the 14 GB story model from a hard disk, which would have ended as `NS-E203`) is now read from
  `config/supervisor.json`: setup writes 600 s when the install drive is not an SSD (or is unknown) and 300 s
  otherwise, and the nightly run uses it too.
- Not in this change: certificates and proxies, pinning Hugging Face file revisions and the qwen-tts temp
  folder (`orchestrator/pins-0.1.7`), re-syncing venvs when INSTALL.cmd is run over an existing install.


- Installer: running INSTALL.cmd again after the questions step (window closed, PC slept or rebooted, power
  cut, update over an install) showed a greyed-out Choices screen whose Install button re-sent the locked
  answers and stopped; the setup window now opens on the "Setup is halfway" screen and Continue resumes the
  run from the first unfinished step (`machine.derive_phase` follows the steps when the answers are locked).
### Assistant: pull stock footage now (`feat/assistant-fetch-footage`)

- **`fetch_footage` tool and `POST /api/library/footage/fetch`.** The Assistant (after the owner's Yes) can search
  Pixabay and Pexels for a scene and download up to 5 clips into `cache\clips\<source>\`, the same folder and `clips`
  table the nightly jobs use, so later jobs reuse them and Library > Footage lists them. The answer starts with the
  absolute file paths, then source, clip id, page link, licence, size, duration and keywords. Clips already in the cache
  are not downloaded again; a dead download is skipped as long as another succeeds. It is a plain network download:
  no GPU lease and no studio job. Keys are read from Credential Manager or the DPAPI file; with none stored the answer is
  `NS-E502` "No stock footage key. Add a Pixabay key (free) in Settings > Stock footage keys" (keys are entered only in
  Settings, never in chat).
- **`where_is` answers "footage cache", "stock footage", "stock clips" and "clip cache"** with the cache folder.
- Confirm cards show a tool's schema defaults (count 1, landscape, auto) instead of "(default)".
- `ClipCache.record` is the one place that writes a `clips` row (used by `ensure` and by the new tool).

### Pins and voice-engine temp files (`orchestrator/pins-0.1.7`, from install audit 2, findings 3 and 9)
- Every Hugging Face file in `manifest.json` (all LLM tiers, embed, voice GGUFs, whisper) now names the commit
  it is fetched from (`revision`, a 40-character sha), so a re-upload of a file under the same name on `main`
  can no longer fail every download with NS-E109. Each file was checked at that commit: it exists, its size
  and the LFS sha256 equal the manifest. The PyTorch fallback repos (and the VoiceDesign weights) are pinned
  in `tts_fallback_revisions`. `HfFile.revision` defaults to `main` for an entry that names none, and the
  custom model chosen in setup still comes from `main`. A test fails if the shipped manifest holds `main`.
- `bins.yunet` points at the opencv_zoo commit that holds the file instead of `raw/main`; its sha256 is
  unchanged (re-hashed from the commit URL).
- Voice engine: `qwen-tts.exe` and `qwen-codec.exe` never convert argv to UTF-8, so a path with Arabic (or
  other non-ASCII) letters, such as a `%TEMP%` under that Windows user name, made the voice test fail with
  "WAV write failed" and setup fell back to the 10 GB PyTorch engine (NS-W301). The engine now keeps every
  scratch file handed to the tools under `<home>\cache	ts-tmp` (`Paths.cache_tts_tmp`), one folder per call,
  removed afterwards (leftovers older than a day are swept). A voice whose files sit on a non-ASCII path is
  copied there before the tool reads it. The test stand-in now rejects non-ASCII path arguments like the real
  tools do.

## [0.1.6] - 2026-11-04

### Hotfix (`orchestrator/hotfix-0.1.6-key-check`, from the partner's first run of 0.1.5)
- Installer, API keys screen: the live check of the Pexels and Pixabay keys sent Python's default
  `User-Agent` (`Python-urllib/3.12`). Cloudflare in front of both services answers HTTP 403 (error 1010)
  to that signature, so every valid key was reported as `NS-E501`/`NS-E502` ("not accepted, copy the key
  again, no spaces"). The check now sends `Nightshift-Setup/<version>`; verified live against both
  services. The studio's own stock-footage client (httpx) was never affected.
- Both stock-footage keys are optional, so setup never blocks on them: Pexels has paused new API keys, and a
  Pixabay key can be added later in Settings > Stock footage keys. The setup screen marks both fields optional
  with a hint, a pasted key is still checked live, the console wizard lets Enter skip a key and prints a notice
  when none is given, `s01`/`s07` no longer require a stored key, and the studio builds only the clip sources
  that have a key (both when none, so the first video's clips stage fails with `NS-E502`, whose hint now says
  to add the key in Settings).
- Pixabay video renditions: the studio takes the smallest rendition that still covers a 1080p master (Pixabay
  now serves `large` at 3840x2160, about 35 MB per clip; `small` is 1920x1080 at about 10 MB). The request
  (`/api/videos/`, `video_type=film`, `safesearch=true`, 24 h response cache, 90 requests/min) was checked
  live against the current Pixabay API docs and response.
- Do NOT send v0.1.5: its key screen rejects every key. Send `Nightshift-Setup-0.1.6.zip`.

## [0.1.5] - 2026-11-03

### Final pre-tag fixes (`feat/release-0.1.5-pretag`, from the last review before tagging)

- **Shortcuts and the nightly task follow the program folder.** Running `INSTALL.cmd` over an install made with an
  older version moved the program to `<home>\app`, but the Desktop shortcuts and the "Nightshift Daily" task kept the
  folder the zip had been extracted to (and Task Scheduler could not start the 03:00 task once that folder was
  deleted, as `OPERATOR.md` suggests). s09 now records `shortcuts_app` and runs again when it differs from the
  program folder, which rewrites every shortcut and re-registers the task with the new command and working directory.
- **`ns update` marks the program folder as finished.** It writes `.ns-app.done` (the same text setup writes) into the
  new tree, and `ns update --rollback` into the restored one. A later `INSTALL.cmd` run used to find no marker, call
  `<home>\app` "an unfinished folder from an earlier try" and delete it without stopping the engine; it now swaps it
  with `app.prev` like any update, with the engine stopped first.
- **The CPU fallback for captions is not sticky.** With a graphics card, s08 probes whisper on CUDA on every run. A
  passing probe puts `runtime.json` back on `cuda`; only a failing probe keeps or sets the CPU, with `NS-W404` each
  time. A driver update or a restored DLL no longer leaves captions slow for good.
- **RTX 50 card with an unreadable driver version.** Stage 1 hands over `cuda13` (as s02 chooses), so the setup
  window lists the llama.cpp and CUDA runtime rows that are really downloaded, and the total matches.
- **Zip extracted to a drive root.** `D:\Nightshift` counted as "inside" `D:\`, so the preselected install folder was
  refused. A drive root holds every folder of its drive: only the root itself is refused now, in stage 1 and in the
  window. Copying from a root takes only the bundle's own top-level entries, never the whole drive.
- **Uninstall leaves nothing behind.** It takes the FFmpeg fallback's folder (`<home>\bin\ffmpeg`) out of the user
  PATH, and the console uninstall (used when no Python is found) removes `app`, `app.new` and `app.prev` too.
- **`UNINSTALL.cmd` no longer keeps `<home>\app` alive.** Explorer starts the console in that folder and it waited at
  `pause`, so the emptied folder was left after the window closed. The script changes to `%TEMP%` first, and the
  deferred cleanup keeps trying for two minutes per folder (was 90 seconds).

### Downloads resume, verify and recover (`feat/release-download-hardening`, #61)

- **Real-network resume tests (CI only).** `tests/test_downloader_network.py` (marker `network`, skipped unless
  `NS_NETWORK_TESTS` is set; the CI `test` job sets it and runs them in their own step, failing if any is skipped)
  downloads the pinned `yt-dlp.exe` from the GitHub release and a real file through the Hugging Face redirect with the
  real `download()`, pauses after 2 MiB (1 MiB), checks the `.part` size, resumes and asserts exactly one `Range` request
  from that size, the pinned/straight-download hash, and that the `.ok` marker is written after the hash. A `.part`
  cut short by hand resumes from the cut. The evidence lines go to the job log and summary.
- **Resume edge cases.** A `.part` longer than the server's file, a server that ignores `Range`, and a `416` all restart
  the file from zero without using up a retry (a `416` that says the part is the whole file goes straight to the hash).
  A wrong hash deletes the file and fetches it once more from the start, then `NS-E109` (was three tries).
- **Re-run after a crash.** A finished file whose `.ok` marker was lost (killed between the rename and the marker) is
  hashed on the disk and adopted instead of downloaded again; a marker whose file was deleted, resized or touched is
  ignored; verified files cost two stat calls and no request.
- **No handle held across a pause.** A Pause landing between the downloader's gate check and its progress report used
  to park inside the open `.part` with the connection open (the bridge reporter blocked at the gate). The downloader now
  reports through a non-blocking `update_nowait`, so the file and the connection are always closed before it waits.
  Stop keeps the `.part`; a stop in the middle of a `models_source` copy removes the half copy, and copies use a
  `.copy` file so they can never overwrite or delete a resumable `.part`.
- **Disk full is `NS-E104`**, with the free space at the destination, not a traceback or three network retries.
- **Retries are capped and shown.** At most five tries per file (three by default), waits of 2, 4, 8 ... capped at
  30 s; the row note says "(try 2 of 3)".
- **Smoother progress.** The downloader reports every 64 KiB or 250 ms, whichever comes first (it now reads blocks as
  they arrive instead of waiting to fill 64 KiB). The overall time left rises by at most 20 % from one poll to the
  next unless a file restarted.
- **`ns verify --downloads`** (`installer/stage2/verify_downloads.py`): re-hashes every model and program of this
  install against `manifest.json` and prints ok / missing / bad hash with the fix for each; exit 1 when something needs
  fixing. OPERATOR.md has a new "If a download stops" section.


### Install audit fixes (`feat/release-install-audit`, findings #1 to #8 and #10 to #12 of `install-audit.md`)

- **Visual C++ runtime.** llama.cpp and qwentts.cpp are built against the dynamic MSVC runtime and do not start on a
  clean Windows 11 without it (the symptom used to show only at the smoke test, after the 20 GB download). The PC
  check has a new row "Visual C++ runtime" (`msvcp140.dll`, `vcruntime140.dll`, `vcruntime140_1.dll` in System32).
  Step b01 installs it when missing: winget `Microsoft.VCRedist.2015+.x64` first, then the pinned `bins.vcredist`
  (`VC_redist.x64.exe` 14.44.35211.0, SHA256 in `manifest.json`) run elevated with `/install /quiet /norestart`. The
  smoke test checks the DLLs before it starts llama-server. New `NS-E114` (declined permission prompt, failed
  installer, missing DLLs).
- **The program lives in `<home>\app`.** Setup copies the extracted folder into `<home>\app` (without `venvs`, `models`
  or any `bin` folder but the bundle's `bin\webview2`), records that path as `app_path`, and the venvs, the shortcuts, the
  nightly task, the launchers and `ns update` all use it. The extracted folder can be deleted. The install folder may
  not be the extracted folder (NTFS ignores case: `D:\NightShift` and `D:\Nightshift` were the same folder, so the first
  update tried to rename the folder that held the venvs and models), inside it, or in Downloads or OneDrive; the window
  says so and stage 1 picks `<drive>:\Nightshift-home` as the default when the usual name is taken. Running
  `INSTALL.cmd` from a newer zip over an existing install updates it with the `ns update` swap (`app.prev` is the
  rollback target, `.git`, `config` and `answers.json` are carried over). New `NS-E115`. The setup environment and
  the venvs also refresh when only the version or the program folder changed.
- **Git is not required.** Setup no longer installs it (its machine-wide installer was an extra administrator prompt
  and a way to stop setup). `ns update --git` without Git says "Install Git for this" and names the Git-free ways.
- **FFmpeg fallback.** When winget is missing or refuses, b01 downloads the pinned static build
  (`bins.ffmpeg`: BtbN `autobuild-2026-10-31-12-59`, n8.1.3, SHA256 in `manifest.json`), unpacks `ffmpeg.exe` and
  `ffprobe.exe` into `<home>\bin\ffmpeg` and adds that folder to the user's PATH. `ns_common.ffmpeg` also looks there.
  winget is called with `--source winget`. A PC without a working winget is a yellow row, not a blocker.
- **Re-run over a half venv.** `uv venv` gets `--clear` when the folder exists but has no `python.exe`
  (uv 0.10 and later refuse otherwise); applied once, never in a loop.
- **Antivirus.** WinError 225, a downloaded program that disappears between write and verify, a zip member removed
  right after unpacking, and a `llama-server.exe` or `qwen-tts.exe` that is gone before the smoke test are `NS-E113`
  with the allow-list hint (were `NS-E105`, an unexplained exception, or `NS-E203` and a silent 7 GB detour to the
  PyTorch engine).
- **`manifest.version`** is written from `VERSION` by `dev/make_bundle.ps1` (and checked in the finished zip), which
  is what the re-run refresh of the venvs and the smoke test compares.
- **Morning use.** `/status` reports the Windows session of the engine and whether a run is using it. The Dashboard
  shortcut restarts an idle engine that the 03:00 task started in session 0 (no desktop: Open Outbox and the folder
  pickers did nothing there) and never interrupts a busy one.
- **Wake timers.** "Important wake timers only" (power setting 2) now counts as not enough for the 03:00 task, and
  the row says which setting to change.
- **Voice engine pin.** `bins.qwentts` is the v0.1.4 `Nightshift-VoiceEngine-win-cuda-x64.zip` asset; the unpinned
  fallback URL uses the new asset name first and the old one second.

### Installer: whisper CUDA libraries and a CPU fallback for captions (CR-C1-4 a, b, c):

- **cuBLAS and cuDNN are pinned and installed.** faster-whisper runs on CTranslate2, whose Windows build needs the
  CUDA 12 cuBLAS DLL (and ships a cuDNN 9 loader); a fresh PC has only the NVIDIA driver, so the first video's
  captions ran on the CPU or failed with `NS-E408`. `manifest.json` has a new `python.whisper_cuda` list
  (`nvidia-cublas-cu12==12.8.4.1`, `nvidia-cudnn-cu12==9.10.2.21`, each `--hash=sha256:` of its win_amd64 wheel) and
  s04 installs it into `venvs\studio` in hash-checking mode. Before it used the ranges `>=12.8,<13` and `>=9,<10`.
  The studio extra now also says `ctranslate2>=4.6.3,<4.9` (CUDA 12.8 Windows builds). The DLL folders reach PATH
  through `ns_common.gpu_dlls.ensure_on_path()` (verify) and the transcriber's own `ensure_gpu_dlls_on_path()`
  before `faster_whisper` is imported, so no shell setting is involved.
- **Setup tests a real transcription and falls back to the CPU.** `ns verify --quick` only loads the whisper model,
  and CTranslate2 loads cuBLAS lazily. s08 now transcribes a generated 3 second WAV in the studio venv on
  `device=cuda`; when that (or the model load) fails it writes `device=cpu, compute_type=int8` to `runtime.json`,
  warns `NS-W404` ("Whisper runs on the CPU: captions will be slow") and tests the CPU too. Only a CPU that cannot
  transcribe stops setup (`NS-E202`); the old hard stop `NS-E112` on a GPU failure is gone. `OPERATOR.md` and the
  setup window's copy know `NS-W404`.
- **spaCy model wheel and the PyTorch fallback repos come from the manifest.** `python.spacy_wheel`
  (`en_core_web_md` 3.8.0 and its sha256) replaces the hard-coded URL, installed with `--require-hashes`;
  `tts_fallback_repos` replaces the constant in `installer/stage2/fallback.py`. `Manifest` gained `python`
  (`PythonPins`) and `tts_fallback_repos`, both defaulted; `contracts/schemas/manifest.schema.json` follows.
- `tools/resolve_manifest.py --check` also validates the `python` pins; CI tests compare the pinned cuBLAS/cuDNN
  hashes with PyPI and the spaCy wheel hash with the real download.
- Still open: CR-C1-4 (e), a CUDA 12.8 llama.cpp build for drivers between CUDA 12.4 and 12.7.

### RTX 50-series cards stop early on a CUDA 12 driver
- The only llama.cpp builds Nightshift ships are CUDA 12.4 and CUDA 13.x, and the 12.4 build has no code for
  RTX 50-series (Blackwell) cards. Setup used to warn and go on, then fail the smoke test after the full
  download. The "This PC" check and step s02 now stop with `NS-E103` ("RTX 50-series cards need a driver with
  CUDA 13, driver 580 or newer") before anything is downloaded; an RTX 50 card whose driver version cannot be
  read gets the CUDA 13 build. Other cards keep the 12.4 rule.

## [0.1.4] - 2026-11-03

### Same content as 0.1.3, released
- The `v0.1.3` tag's release build failed on one new test whose expectation was wrong (70 MB at 1 MB/s is 70 s,
  not 70 min), so no 0.1.3 bundle was published. The test is corrected and 0.1.4 ships everything listed under 0.1.3.

## [0.1.3] - 2026-11-03

### Download time left is measured, never assumed
- The console no longer prints "about N hours at 50 Mbit/s" before the downloads; its progress lines now carry
  the measured file speed and the whole download's time left, the same numbers the setup window shows.
- The overall time left divides the remaining bytes by the whole download's speed over the last 30 s
  (`SpeedMeter.total_rate`), which is steadier than adding up per-file averages; per-file averages stand in
  only while that window is still filling.
### First-run fixes from the partner PC (`feat/release-0.1.3-hotfix`)
- **The first setup window is the Nightshift window.** The bundle now carries the three WebView2 DLLs in `bin/webview2/`
  (`dev/make_bundle.ps1 -WebView2Dir`; the release workflow downloads the pinned nupkg, checks its SHA256 and each DLL
  against `installer/webview2_files.json`, and packs exactly those three files; nothing else is allowed under `bin/` in
  the zip). The window launcher looks in the install folder and in `<app>/bin/webview2`, and step s02 copies matching
  DLLs into the install folder instead of downloading the nupkg. Before, the DLLs arrived after the first window opened,
  so the first run showed an Edge app window with Edge's title bar and icon.
- **A current NVIDIA driver is no longer reported as too old.** Stage 1 finds `nvidia-smi.exe` in the DCH driver store
  (`System32/DriverStore/FileRepository/nv*`) too, reads the CUDA version from the banner, then from the driver number
  (one table, 580 = 13.0, 570 = 12.8, 560 = 12.6, 550 = 12.4, 545 = 12.3), then from Windows' own display adapter record.
  The driver row is labelled "NVIDIA driver (CUDA 12.4 or newer; 12.8+ for RTX 50-series)". `NS-E103` (fail) only when a
  version was read and is below 12.4. A version that could not be read at all is the new warning `NS-W115` and setup
  uses the CUDA 12.4 build. A driver between 12.4 and 12.8 warns, without a code, only for RTX 50-series (Blackwell) cards.
- **Browse buttons for the install folder and the models folder.** `POST /api/pick-folder` shows the Windows folder
  dialog (`installer/pick-folder.ps1`, in front of the setup window) and returns the path with its disk check. The install
  folder is validated when typed, on blur and on pick: absolute path on a local fixed drive (no network paths, no
  removable drives), at most 120 characters (was 80), no characters Windows forbids, spaces and non-ASCII letters
  allowed. A picked drive root or a folder that already holds files gets a `Nightshift` folder inside it. The folder is
  created when Install starts, not before.

## [0.1.2] - 2026-11-03

### Hotfix: the setup window never started on a stock PC
- `INSTALL.cmd` stopped with "[FAIL] Setup window: the setup program did not start" (exit code 14) on the first
  real install, although the setup server was up and had opened Edge. Stage 1's health ping passed the session
  cookie through `-Headers`, and Windows PowerShell 5.1 silently drops that restricted header, so the server
  answered 401 and the console killed it. `Invoke-ServerPing` now sends the cookie through a `WebRequestSession`
  and also as an `X-NS-Token` header, which the server accepts alongside the cookie.
- New CI test runs the real `Invoke-ServerPing` under `powershell.exe` (5.1) against a live setup server, and pins
  that the old form fails there, so the gap that let v0.1.1 ship cannot reopen.

### Release asset names
- Clearer release assets from the next tag on: `Nightshift-Setup-<ver>.zip` (the installer bundle, was `Nightshift-<ver>.zip`),
  `Nightshift-Update-<ver>.zip` (update-from-file package, was `nightshift-app-<ver>.zip`) and
  `Nightshift-VoiceEngine-win-cuda-x64.zip` (was `qwentts-win-cuda-x64.zip`), each with a `.sha256`.
- `ns update --github` and the dashboard updater accept both the new and the old update-package name (new first), so
  v0.1.0 and v0.1.1 releases keep working. `manifest.json` still pins the v0.1.0 voice-engine asset by its old name.
- The release body now starts with a "Which file do I download?" table (`dev/release_notes.md`) followed by the changelog entries.

## [0.1.1] - 2026-11-03

Second code-complete release. It carries the clean-clone review fixes, the re-review rounds (pull requests #48 to #51),
the pre-tag polish (this release's last pull request) and the `bins.qwentts` pin to the v0.1.0 release asset.

### Pre-tag polish (`feat/release-0.1.1-pretag`; `docs/design/system-gaps.md` and the final engine and dashboard checks)
- **Voice stage timeout** (gap 1): the studio's wait for a whole job's narration is 120 s + 3 s per line, at most 2 h
  (`ns_common.tts_timeout`, one formula for `ns_studio/media/voice.py`, `TtsClient` and the TTS service, which never stops a
  `qwen-tts` process before the client gives up). When the client times out it asks `POST /v1/audio/speech/cancel` to kill
  the process, so the GPU is free before the retry; whole items the service had finished stay on disk and the retry
  (`resume`) skips them. Was a fixed 30 minutes against a service that kept the GPU busy for up to 3.6 hours.
- **Queue, Release the graphics card** (gap 2): the supervisor refuses while the holder sent a heartbeat in the last 90 s
  (a nightly run that overran shows nothing in the Queue); otherwise a Nightshift holder is stopped through the jobs worker,
  and for a foreign holder such as `opencode` it only releases and says the other program may still be using the GPU. The
  confirm text says so.
- **Nightly task that runs only while you are signed in** (gap 3, NS-W205): `Status.schedule_mode` (`registered`,
  `interactive_only`, `none`; contract change `CR-PRETAG-C9-1`). The top bar and Settings say "Runs at 03:00 only while you
  are signed in" in a warning style; the next-run countdown stays.
- **Setup voice** (gaps 4, 5, 6): it is made under the job's own GPU owner, so Cancel frees it at once; Library, Voices,
  Create it now is a job (`apply_voice`) with progress and Cancel in the card and in the Queue; the setup window no longer
  promises an automatic voice design download (Settings, Models, Get voice design model).
- **Series parts stay produced**: a finished `series_part` job moves its part to `produced`, and the start sweep resets a
  part to `approved` only when its job is missing, failed or cancelled (it also settles parts 0.1.0 left on `producing`).
  Before, reopening the dashboard offered Part 1 again and made the video twice.
- **Update page**: the 10-minute "did not come back" wait starts when `update_state.json` leaves `running`, not at the
  click; during a long install the page keeps saying it is installing.
- **Uninstall removes `<home>\run`** (the update worker's private venv copy, 150-300 MB) and lists it in its report; the
  update worker deletes its own copy after a relaunch that worked.
- **CI**: the test job may take 40 minutes (`ci.yml`, `release.yml`); the duplicate 70 s child smoke run is gone.

### Fixed (re-review round, pull requests #48 to #51)
- **Loose ends (#48)**: the embed service gets `-c/-b/-ub` from `EmbedCfg`; link-download Cancel reaches the Clipper
  `CancelToken` (flag file, 3 s grace, then the process tree); the drift gate is described as it works (median offset
  between caption and ASR word starts, 120 ms); the setup window has an "Also download the voice design model" checkbox
  (a designed voice implies it; `NS-W114` when ticked off); `OPERATOR.md` explains the PyTorch fallback (`NS-W301`), Get
  voice design model and the DPAPI key file.
- **Runtime (#49)**: the update worker starts the engine (and the dashboard window, if one was open) again after the update
  or a rollback and writes `done` or `failed` only after `/status` answers; the rollback retries its renames and waits for
  the verify engine; browser presence uses `psutil.net_connections`; a uv shim is replaced by the real exe; Get voice design
  follows `runtime.tts.engine` (PyTorch gets the Hugging Face weights); `update_state.json` is written atomically and a
  dead worker is shown as failed; the deferred uninstall waits for the dashboard window and lists leftovers in
  `%TEMP%\Nightshift-uninstall-leftovers.txt`; `schedule.json` is not written when Windows refused the task (NS-W206); the
  worker runs from a private copy of the supervisor venv so uv can rewrite `venvs\supervisor`.
- **Dashboard (#50)**: Cancel releases the GPU lease (flag file, 10 s grace, kill, `POST /release_owner`); `studio:` leases
  are renewed by a 30 s heartbeat and a silent lease is reclaimed after 10 minutes; the Queue shows "GPU held by ..." with a
  Release button; Settings follows the update across the restart and reloads on the new version; saved Assistant requests
  are claimed in one statement and answered when a job ends; series parts go back to `approved` when cancelled or failed;
  caption look "Automatic"; failed jobs keep their stage and studio job ids; stock-footage keys also go to the DPAPI file;
  Library, Voices, "Pending voice from setup" with Create it now; a real `studio_job` child process is run in CI.
- **Studio and TTS (#51)**: embed windows are token-aware per script; CJK captions are joined without spaces and aligned per
  character; channel languages reach `qwen-tts --lang`; series parts keep their pacing; publish slots use the PC's local date
  and count Shorts already scheduled; master and Short budgets count the pauses; the regenerate-once table has the master
  names; **one `qwen-tts --stream-by-line -o -` process per batch** instead of one per sentence; pending voices are created
  by every job (`apply_pending_voices`); `NS-E311` for an unknown voice; the clip Outbox with nothing in it is `NS-E404`;
  `load_keys` falls back to the DPAPI file on any keyring error.

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
