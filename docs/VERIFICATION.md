# Nightshift verification program

The standard for calling this program "checked": every block of the system below has a checklist, every
item on it carries evidence of one of four kinds, and no item is left at "unverified". This file is the map,
the checklists, the status table and the loop that works through it. It is updated by the orchestrator at the
end of every loop iteration; a reader should be able to tell exactly what was checked, how, and what was not.

## 1. Evidence levels

| Level | Meaning | Where the evidence lives |
|---|---|---|
| **V-dev** | Run on the orchestrator's laptop against a dev-mode install made from the official release zip (fake models, real FFmpeg, real Windows integration). Timed. | `D:\NightShift-refs\test-loop-*.md`, logs under the dev home |
| **V-ci** | A unit, contract or fake-services test in `tests/` that runs in the `test` job of `ci.yml` | the test name |
| **V-gpu** | Can only be observed on the partner's GPU PC; a named line in the first-run checklist (`docs/design/system-gaps.md`, "Verified on the partner PC") | `verify-report.json`, the diagnostics zip |
| **U** | Not verified by anyone yet | — |

Rules: a V-dev item names the command or click and the observed result; a V-ci item names the test; a V-gpu
item names what the partner's run must show; "the code looks right" is not evidence.

## 2. System map (what serves what)

```
INSTALL.cmd ─► A stage 1 (stage1.ps1: preflight, uv/Python, WebView2, dev flag)
                 └─► B stage 2 (web server + wizard; steps b01-b03, s00-s10, v01; answers.json)
                        └─► the home: venvs, bin, models, config, shortcuts, scheduled task, launchers
C launchers (_common, dashboard, host, studio, code, update, diagnostics, open-outbox, stop-all)
   └─► D supervisor / engine (services llama, embed, tts, dashboard; leases; GPU gate; presence;
        keep-awake; scheduled runs; ns verify/tune/doctor/update/backup/restore/schedule/models/stop)
          ├─► E studio pipeline (orchestrator: shorts_only 14 stages, master_first 21, master_from_script;
          │    clipper; series workshop; daily planner + cadence; review; policy; QC; outbox; learn; calibrate;
          │    notifications)  ◄── I engines and clients (llm, embed, tts server, whisper, clip sources
          │    Pexels/Pixabay, FFmpeg render)
          └─► F dashboard API (83 routes in 20 groups; svc_* services; job worker; events)
                ├─► G dashboard UI (Queue, Create, Library, Assistant, Channels, Results, Settings; shell;
                │    WebView2 host bridge; installer wizard UI)
                └─► H assistant (loop, router, prompt budgets, 68 tools, confirm gating, store, eval)
J contracts (schemas, errors.json, schema.sql, policy) are read by B, D, E, F, H
K operations (scheduled task, toasts, logs, diagnostics zip, update + rollback, uninstall, backup/restore,
   settings export/import) cut across C, D, F
L release and CI (ci.yml: lint, types, test, bundle, ui-build, powershell-parse, scope-check; release.yml;
   make_bundle; manifest pins; WebView2 bundle; qwentts build)
```

## 3. Testing angles (applied to every block)

1. **Static**: ruff, pyright, tsc, PowerShell parse (CI).
2. **Unit and contract**: `tests/` (248 files), schemas, `errors.json` vs source, `schema.sql` migrations.
3. **Integration on the dev install**: the real process boundaries (launcher → supervisor → dashboard →
   studio → FFmpeg) with fake engines; every CLI command; every API route with a real request.
4. **End-to-end journeys**: the owner's clicks in the real UI (WebView2 window or Chrome): install, first
   run, create, review, library, assistant, settings, update, uninstall.
5. **Lifecycle**: fresh install → use → update over it → use → uninstall → leftovers check.
6. **Operations**: the scheduled task fires; toasts show; logs rotate; diagnostics zip holds the evidence;
   rollback restores; export/import round-trips.
7. **Resilience**: engine killed mid-job, dashboard killed, disk nearly full, network off, a service that
   never comes up, a stale lease, a second instance; every `NS-E` path reachable and worded.
8. **Security and privacy**: keys never in logs, files or chat; paths confined to the home; no listening
   outside 127.0.0.1; uploads validated.
9. **Performance budgets**: the timings the owner reads (install, job, update) measured, not guessed.
10. **Docs and copy**: every screen's words true for the build that ships; OPERATOR.md, DEV_MODE.md, HANDOFF.
11. **GPU-only**: the partner checklist, one line per item, with the measurement it must return.

## 4. Status table (per block, honest as of 2026-11-09; the engine test of 2026-11-08 is docs/DEV_MODE.md "Real engines, fake voice", report `D:\NightShift-refs\test-loop-2026-11-08.md`)

| Block | Static | Unit/contract | Dev integration | E2E journeys | Lifecycle | Operations | Resilience | Security | Perf | Docs | GPU-only |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A stage 1 | V-ci | V-ci | V-dev (headless, dev flag; window run via its API) | partial (window run proven by API; clicks U) | V-dev | — | **U** (console closed mid-run) | V-ci | V-dev 258 s | V-dev | V-gpu |
| B stage 2 | V-ci | V-ci | V-dev (dev install; engine test: real downloads with hash checks, `ns tune` and the smoke test on a real model, fake voice) | V-dev (wizard by clicking to Done, 0.1.12, 2026-11-09) | V-dev | — | **U** (network off, retry) | V-ci | V-dev | partial | V-gpu (downloads, smoke) |
| C launchers | V-ci | V-ci | V-dev (dashboard, host, stop-all, update) | V-dev | V-dev | V-dev | V-dev (dead server → window closes) | — | — | V-dev | V-gpu (host on a stock PC) |
| D supervisor | V-ci | V-ci | V-dev (services, leases, verify) | — | V-dev | V-dev (task fired by hand: headless engine, master, engine left) / **U** keep-awake | V-dev (kill mid-job → NS-E328 at its stage, Retry continues; PR #86) / **U** lease TTL | V-ci | V-dev | partial | V-gpu (GPU gate, tune) |
| E studio | V-ci | V-ci | V-dev (Shorts, master, clip, planner, QC; a clip job on real engines) | V-dev | — | — | partial (resume proven; kill mid-stage U) | V-ci | V-dev (Short 83 s) | partial | V-gpu (quality, timing) |
| F dashboard API | V-ci | V-ci | V-dev (20 groups probed, chunked upload → clips) | V-dev | V-dev | V-dev (update, uninstall routes) | **U** | V-dev (traversal 404, loopback only) | — | — | — |
| G dashboard UI | V-ci | V-ci (callers, contract) | V-dev (Chrome) | V-dev (all pages) | — | — | — | — | — | partial | V-gpu (WebView2) |
| H assistant | V-ci | V-ci (eval, router, redact) | V-dev (fake tool routing) | V-dev | — | — | **U** (engine busy, timeouts) | V-ci (redaction) | — | V-dev | V-gpu (real model answers) |
| I engines/clients | V-ci | V-ci | V-dev (fakes; real Pixabay; engine test: real llama-server, embeddings and whisper on the CPU inside a clip job) | — | — | — | V-dev (Pexels and 429 through the QA fake endpoint; 5xx outage: defect D-5) | V-ci | — | — | V-gpu (all real engines) |
| J contracts | V-ci | V-ci | — | — | — | — | — | — | — | V-ci | — |
| K operations | V-ci | V-ci | V-dev (update, uninstall, diagnostics) | V-dev | V-dev | V-dev (task fires, rollback, export/import, backup/restore, notify command) / V-gpu toast visible, keep-awake | V-dev (restore while running refused, PR #87) / **U** disk full, network off | V-dev (keys forgotten) | V-dev (update 51 s) | partial | — |
| L release/CI | V-ci | V-ci (release wiring) | V-dev (assets, sha256, manifest) | — | V-dev | — | — | — | — | V-dev | — |

"partial" means some items of the angle are verified and the rest are U; the block checklists below say which.

## 5. Block checklists (the items the loop works through; U items first)

### A. Stage 1
- [x] The setup window (`-Ui`) in dev mode after an uninstall: Welcome (resume) → Continue → downloading → installing → verifying → Done in 290 s, every step done, read through the window's own API (V-dev 2026-11-05, PR #89 fixed Done-over-nothing and the 30,265 s duration); the screens by clicking were proven on 0.1.12 on 2026-11-09 (Welcome → This PC → Your choices → Install → Done, page clicks on the setup server)
- [ ] Console closed mid-run resumes on the next INSTALL.cmd (U)
- [x] Headless dev install from the official zip (V-dev 258 s)
- [x] Preflight rows, Smart App Control, antivirus copy (V-ci)

### B. Stage 2
- [x] Wizard clicks: change a choice, Browse a folder, key screen with a rejected then a good key, through to Done (V-dev 0.1.12, 2026-11-09, page clicks on the setup server; Browse after an uninstall-keep-data returns a nested folder: defect D-22)
- [ ] Network off during a download: retry note, Failed screen, Continue (U)
- [x] answers.json headless, keys optional, rejected stored key forgotten (V-dev, V-ci)
- [x] Steps b01-b03, s00-s10, v01 status in install-report.json (V-dev)

### C. Launchers
- [x] Dashboard launcher with window, headless, Edge fallback text (V-dev, V-ci)
- [x] stop-all stops engine, dashboard and the window; the window closes itself when the server is gone (V-dev)
- [x] Update from a local zip (V-dev 51 s)
- [x] `diagnostics.ps1` writes the zip to the Desktop (V-dev 2026-11-05); `nightshift-studio.ps1` and `nightshift-code.ps1` open consoles/Code and stay V-ci (`tests/test_launchers.py`); `open-outbox.ps1` opens Explorer, V-ci

### D. Supervisor
- [x] The 03:00 scheduled task fired by hand (`Start-ScheduledTask`) on the 0.1.11 dev install: `studio run --daily` started the engine headless, made a master (22 stages, 18.5 min, 1 master + 4 Shorts, publish times set), the engine left by itself, the slate was CLEAN 20 s later, the task was disabled again, LastTaskResult 0 (V-dev 2026-11-05)
- [ ] Keep-awake during a run: `powercfg -requests` needs an elevated prompt, not available in the loop; V-ci (`tests/test_keepawake.py`) plus a partner line: run it elevated during the nightly run and expect the studio process under EXECUTION (V-gpu)
- [x] Kill the engine mid-job (supervisor, dashboard, child, ffmpeg force-stopped at clips 12/16): after relaunch the job is failed at stage clips with NS-E328 and its studio job id; Retry continued the same studio job to done in 75 s. Before PR #86 the stage and the id were lost and Retry refused (V-dev 2026-11-05)
- [ ] Lease TTL (600 s): in dev mode there is no GPU lock to hold, so the reclaim is V-ci (`tests/test_supervisor_lock.py`) and a partner line: kill a studio child mid-stage on the GPU PC and expect `lease_expired` within 10 min (V-gpu)
- [x] Services start on demand, `ns verify --quick`, `ns stop` (V-dev)

### E. Studio
- [x] shorts_only, master_first (fresh install: Shorts; master on the 0.1.9 install), clipper, planner, QC drop, footage reuse (V-dev)
- [x] master_first on the fresh 0.1.11 install through the scheduled task: 18.5 min, 199 MB master, Shorts 13-109 MB (V-dev 2026-11-05)
- [ ] Series workshop end to end (concepts → map → scripts → produce) (U)
- [x] Review: approve, reject with note, mark uploaded, re-review: reject moves the files to rejected/, uploaded refused while rejected, approve then uploaded; card and `uploads` table agree (V-dev 0.1.12, 2026-11-09; the approved file stays under rejected/: defect D-6)
- [x] Learn: a YouTube-Studio-style CSV naming two Shorts imported through `/api/learn/import`: 2 matched, weights moved in five pools, channel retention 61.5 % and a spark point (V-dev 2026-11-05); the Results page rendering those numbers is checked in the browser pass (U)
- [x] Calibrate: generate, human scores, spearman, bias and the critic_bar it writes (V-dev 0.1.12, 2026-11-09; D-10 refusal counting)

### F. Dashboard API
- [x] 20 route groups answer; jobs, outputs, settings, keys, footage, assistant (V-dev)
- [x] Uploads: chunked upload of a Short → clip job → one 15 s clip (V-dev 2026-11-05 after PR #88: the dev sidecar was deleted with the upload folder, then copied onto itself with WinError 32). An upload id is not usable after a dashboard restart once adopted (409 upload_incomplete); re-run from the job is the path (wording item, U)
- [ ] URL clips (`/api/url` path) with a local file URL (U)
- [x] Security: traversal requests on media, thumbs, outputs and footage delete answer 404; dashboard and engine refuse the LAN address and listen on 127.0.0.1 only (V-dev 2026-11-05)

### G. Dashboard UI
- [x] Every page renders with data; Create wizard; Queue approve/reject; Library tabs; Settings keys; Hide (V-dev)
- [x] The WebView2 window's bridge: minimize, maximize/restore, drag, close confirm while a job runs (V-dev 0.1.12, 2026-11-09, UI Automation; D-12 restore size)
- [x] Results page with imported analytics: the import's numbers on the page (V-dev 0.1.12, 2026-11-09; D-11 chart and labels)

### H. Assistant
- [x] Chat → tool call → confirm card → Yes → result on card (V-dev, fake routing)
- [x] Assistant while a job runs: answered at once with the engine state (render 13/16); the busy refusal needs a real GPU owner, V-ci (`tests/test_dashboard_assistant.py` EngineBusy) and a partner line (V-gpu) (2026-11-05)
- [ ] Offline eval suite passes on the current prompt (V-ci; rerun after prompt changes)

### I. Engines and clients
- [x] Pixabay live: search, cache, download, key check (V-dev)
- [x] Pexels with a key, mimicked: the QA fake Pexels/Pixabay endpoint (local TLS proxy, `D:/NightShift/runs/qa/fakefootage`) answered the product's own key check, search and download on the 0.1.12 dev install: bad key refused (422 NS-E501), good key accepted, 1080x1920 clips cached (V-dev 2026-11-09). The real service's answers stay as pinned by `tests/`
- [x] 429 handling on an install: 3x429 with Retry-After 5 waited out in 15.1 s, then the download; endless 429 → coded NS-E501 after 4 attempts (V-dev 0.1.12 via the fake endpoint, 2026-11-09). A 5xx outage is not retried and is worded as a key problem: defect D-5

### K. Operations
- [x] `ns notify` exits 0 and the AppUserModelId registration exists (V-dev 2026-11-05); the toast being visible is a partner line (V-gpu)
- [x] Rollback: 0.1.11 → 0.1.10 in 21 s, dashboard reports 0.1.10, update to 0.1.11 again in 32 s (V-dev 2026-11-05)
- [x] Settings export (16-entry zip) → run time changed back → import restores it, machine-specific files skipped, keys kept (V-dev 2026-11-05 on the same install; a second PC is V-gpu)
- [x] `ns backup` 16-entry zip; `ns restore` with nothing running restores config and studio.db; with the engine up it failed halfway and rolled back, now refused up front with NS-E703 (PR #87) (V-dev 2026-11-05)
- [x] Diagnostics zip contents: 83 entries with both keys stored; no key value anywhere, answers.json without key fields, every component log, reports, nvidia-smi placeholder (V-dev 0.1.12, 2026-11-09)
- [x] Uninstall leftovers (V-dev)

### L. Release and CI
- [x] Tag → release.yml → Setup/Update zips + sha256, manifest version, WebView2 DLLs (V-dev 0.1.10)
- [x] v0.1.11 assets: Setup and Update zips + sha256, manifest 0.1.11, teardown fix inside; dev install updated from the zip in 41 s (V-dev 2026-11-05)
- [x] `ns update --check` live against GitHub: "Up to date (0.1.11)" with the release link (V-dev 2026-11-05); the download-and-apply path from an older install is V-ci (`tests/test_ns_update_github.py`)

## 6. The inventory (the mechanical checklist)

`docs/VERIFICATION_INVENTORY.md` is generated from the code by `dev/verification_inventory.py`: every API
route, CLI command, studio stage, installer step, launcher, dashboard page, assistant tool, supervisor
service and workflow, each with a status (section 1) and its evidence. `tests/test_verification_inventory.py`
fails in CI when the code defines an item the inventory does not list or the file is stale, so a new route
or stage cannot appear without a status. The loop marks items there as it proves them; the block checklists
above hold the narrative, the inventory holds the count.

`docs/FINALIZATION.md` is the box list: every open item from this file, the inventory, the plumbing map, the
partner column and the handoff gaps as one line with its proof; `dev/finalization_status.py` counts the boxes
and refuses a ticked box without evidence.
`docs/PLUMBING.md` is the third view: the wiring. Every port, process, file, environment variable, event
channel, external host, Windows integration point and configuration key, derived from the code with a
`file:line` each, a coverage table per section (what is V-dev, V-ci, V-gpu or U) and the disposition of every
leftover candidate it turned up. Read it before touching a place where two components meet.

## 7. The loop (one iteration)

0. **Slate**: `nsproc.ps1` must say CLEAN. Nothing from a previous test stays up.
1. **Pick**: the highest-risk U item (operations and resilience before polish).
2. **Specify**: write the exact observation that proves it (command or click, expected words, file, timing).
3. **Run**: one test; evidence captured to the loop report; teardown; CLEAN again.
4. **Gap?** Fix by TDD on a branch: a failing test first, then the change. Builders are `sonnet` agents,
   reviewers `opus`, mappers `haiku`; the orchestrator never spawns itself (`~/.claude/CLAUDE.md`).
5. **Review**: an `opus` reviewer reads the diff against this file's checklist item.
6. **CI**: print check counts; merge only at zero fail and zero pending.
7. **Re-verify** the item on the dev install from the official zip (hot-patch is for finding, not for proving).
8. **Mark** the item V-dev / V-ci / V-gpu here, with the evidence pointer. Release (bump, tag, assets) when a
   block's U items are gone or a user-facing fix merged.
9. Repeat from 0.

Definition of done for the program: no U in section 4; every V-gpu line present in the partner checklist;
every fix released; this file and HANDOFF current.
