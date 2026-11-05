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

## 4. Status table (per block, honest as of 2026-11-05)

| Block | Static | Unit/contract | Dev integration | E2E journeys | Lifecycle | Operations | Resilience | Security | Perf | Docs | GPU-only |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A stage 1 | V-ci | V-ci | V-dev (headless, dev flag) | **U** (setup window in dev) | V-dev | — | **U** (console closed mid-run) | V-ci | V-dev 258 s | V-dev | V-gpu |
| B stage 2 | V-ci | V-ci | V-dev | **U** (wizard clicks) | V-dev | — | **U** (network off, retry) | V-ci | V-dev | partial | V-gpu (downloads, smoke) |
| C launchers | V-ci | V-ci | V-dev (dashboard, host, stop-all, update) | V-dev | V-dev | V-dev | V-dev (dead server → window closes) | — | — | V-dev | V-gpu (host on a stock PC) |
| D supervisor | V-ci | V-ci | V-dev (services, leases, verify) | — | V-dev | V-dev (task fired by hand: headless engine, master, engine left) / **U** keep-awake | V-dev (kill mid-job → NS-E328 at its stage, Retry continues; PR #86) / **U** lease TTL | V-ci | V-dev | partial | V-gpu (GPU gate, tune) |
| E studio | V-ci | V-ci | V-dev (Shorts, master, clip, planner, QC) | V-dev | — | — | partial (resume proven; kill mid-stage U) | V-ci | V-dev (Short 83 s) | partial | V-gpu (quality, timing) |
| F dashboard API | V-ci | V-ci | V-dev (20 groups probed) | V-dev | V-dev | V-dev (update, uninstall routes) | **U** | V-ci | — | — | — |
| G dashboard UI | V-ci | V-ci (callers, contract) | V-dev (Chrome) | V-dev (all pages) | — | — | — | — | — | partial | V-gpu (WebView2) |
| H assistant | V-ci | V-ci (eval, router, redact) | V-dev (fake tool routing) | V-dev | — | — | **U** (engine busy, timeouts) | V-ci (redaction) | — | V-dev | V-gpu (real model answers) |
| I engines/clients | V-ci | V-ci | V-dev (fakes; real Pixabay) | — | — | — | **U** (Pexels live; 429 handling live) | V-ci | — | — | V-gpu (all real engines) |
| J contracts | V-ci | V-ci | — | — | — | — | — | — | — | V-ci | — |
| K operations | V-ci | V-ci | V-dev (update, uninstall, diagnostics) | V-dev | V-dev | V-dev (task fires, rollback, export/import, backup/restore, notify command) / V-gpu toast visible, keep-awake | V-dev (restore while running refused, PR #87) / **U** disk full, network off | V-dev (keys forgotten) | V-dev (update 51 s) | partial | — |
| L release/CI | V-ci | V-ci (release wiring) | V-dev (assets, sha256, manifest) | — | V-dev | — | — | — | — | V-dev | — |

"partial" means some items of the angle are verified and the rest are U; the block checklists below say which.

## 5. Block checklists (the items the loop works through; U items first)

### A. Stage 1
- [ ] The setup window (`-Ui`) in dev mode: Welcome → Choices → Download → Installing → Testing → Done, every screen's words true (U)
- [ ] Console closed mid-run resumes on the next INSTALL.cmd (U)
- [x] Headless dev install from the official zip (V-dev 258 s)
- [x] Preflight rows, Smart App Control, antivirus copy (V-ci)

### B. Stage 2
- [ ] Wizard clicks: change a choice, Browse a folder, key screen with a rejected then a good key (U)
- [ ] Network off during a download: retry note, Failed screen, Continue (U)
- [x] answers.json headless, keys optional, rejected stored key forgotten (V-dev, V-ci)
- [x] Steps b01-b03, s00-s10, v01 status in install-report.json (V-dev)

### C. Launchers
- [x] Dashboard launcher with window, headless, Edge fallback text (V-dev, V-ci)
- [x] stop-all stops engine, dashboard and the window; the window closes itself when the server is gone (V-dev)
- [x] Update from a local zip (V-dev 51 s)
- [ ] Studio and Code launchers from a fresh install (U)

### D. Supervisor
- [x] The 03:00 scheduled task fired by hand (`Start-ScheduledTask`) on the 0.1.11 dev install: `studio run --daily` started the engine headless, made a master (22 stages, 18.5 min, 1 master + 4 Shorts, publish times set), the engine left by itself, the slate was CLEAN 20 s later, the task was disabled again, LastTaskResult 0 (V-dev 2026-11-05)
- [ ] Keep-awake during a run: `powercfg -requests` needs an elevated prompt, not available in the loop; V-ci (`tests/test_keepawake.py`) plus a partner line: run it elevated during the nightly run and expect the studio process under EXECUTION (V-gpu)
- [x] Kill the engine mid-job (supervisor, dashboard, child, ffmpeg force-stopped at clips 12/16): after relaunch the job is failed at stage clips with NS-E328 and its studio job id; Retry continued the same studio job to done in 75 s. Before PR #86 the stage and the id were lost and Retry refused (V-dev 2026-11-05)
- [ ] Lease TTL: a holder that stops heartbeating is reclaimed (V-ci only; prove on dev) (U)
- [x] Services start on demand, `ns verify --quick`, `ns stop` (V-dev)

### E. Studio
- [x] shorts_only, master_first (fresh install: Shorts; master on the 0.1.9 install), clipper, planner, QC drop, footage reuse (V-dev)
- [x] master_first on the fresh 0.1.11 install through the scheduled task: 18.5 min, 199 MB master, Shorts 13-109 MB (V-dev 2026-11-05)
- [ ] Series workshop end to end (concepts → map → scripts → produce) (U)
- [ ] Review: approve, reject with note, mark uploaded, re-review (V-dev approve/reject; uploaded U)
- [x] Learn: a YouTube-Studio-style CSV naming two Shorts imported through `/api/learn/import`: 2 matched, weights moved in five pools, channel retention 61.5 % and a spark point (V-dev 2026-11-05); the Results page rendering those numbers is checked in the browser pass (U)
- [ ] Calibrate (U)

### F. Dashboard API
- [x] 20 route groups answer; jobs, outputs, settings, keys, footage, assistant (V-dev)
- [ ] Uploads: a real file upload → clip job from it (U)
- [ ] URL clips (`/api/url` path) with a local file URL (U)
- [ ] Security: path confinement on media/thumbs, 127.0.0.1 only (V-ci; prove by request) (U)

### G. Dashboard UI
- [x] Every page renders with data; Create wizard; Queue approve/reject; Library tabs; Settings keys; Hide (V-dev)
- [ ] The WebView2 window's bridge: minimize, maximize, drag, close confirm (U)
- [ ] Results page with imported analytics (U)

### H. Assistant
- [x] Chat → tool call → confirm card → Yes → result on card (V-dev, fake routing)
- [ ] Engine busy (a job running) → the Assistant says so and waits/refuses (U)
- [ ] Offline eval suite passes on the current prompt (V-ci; rerun after prompt changes)

### I. Engines and clients
- [x] Pixabay live: search, cache, download, key check (V-dev)
- [ ] Pexels live with a key (needs a Pexels key; keys are entered in Settings) (U)
- [ ] 429 handling live (V-ci; live U)

### K. Operations
- [x] `ns notify` exits 0 and the AppUserModelId registration exists (V-dev 2026-11-05); the toast being visible is a partner line (V-gpu)
- [x] Rollback: 0.1.11 → 0.1.10 in 21 s, dashboard reports 0.1.10, update to 0.1.11 again in 32 s (V-dev 2026-11-05)
- [x] Settings export (16-entry zip) → run time changed back → import restores it, machine-specific files skipped, keys kept (V-dev 2026-11-05 on the same install; a second PC is V-gpu)
- [x] `ns backup` 16-entry zip; `ns restore` with nothing running restores config and studio.db; with the engine up it failed halfway and rolled back, now refused up front with NS-E703 (PR #87) (V-dev 2026-11-05)
- [ ] Diagnostics zip contents: logs, settings without keys, reports (V-dev partial)
- [x] Uninstall leftovers (V-dev)

### L. Release and CI
- [x] Tag → release.yml → Setup/Update zips + sha256, manifest version, WebView2 DLLs (V-dev 0.1.10)
- [x] v0.1.11 assets: Setup and Update zips + sha256, manifest 0.1.11, teardown fix inside; dev install updated from the zip in 41 s (V-dev 2026-11-05)
- [ ] `ns update` finds the new release on GitHub from an older install (V-ci; live U)

## 6. The loop (one iteration)

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
