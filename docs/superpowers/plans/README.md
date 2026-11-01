# Nightshift implementation plans - index

**Status (2026-11-01):** build process is defined in `../../ENGINEERING.md` (read it first). Spec: `../../../BUILD_SPEC.md` v2. Plans are organized by milestone (M0 to M4) and component (C1 to C10). M0: Tasks 1-5 of the foundations plan are implemented and committed; Tasks 6-16 and the other M0 plans are pending. M1: plan parts 1-3 are written; parts 4-5 are to write. M2 to M4 plans are not written yet.

The old spec "slice" vocabulary maps to milestones as follows: Slice 0, 5 (runtime) and 7 (installer core) are M0; Slice 1 is M1; Slices 2-3 are M2; Slices 4 and 6 are M3; Slice 8, `ns tune`, calibration and operator docs are M4. Inside the existing plan files, read `s<N>` names as the milestone gate: `dev/gate_s0.py` is `dev/gate_m0.py`, `dev/gate_s1.py` is `dev/gate_m1.py`, and the tag `contracts-s1` is `contracts-m0`.

**Execution rule that overrides every plan below.** All local "Run: pytest ..." (or ruff, pyright, pip, uv) steps are to be read as: **commit, push, and CI must be green** (GitHub Actions, `windows-latest`). Nothing is executed on the authoring laptop. A task is done only with a CI run URL whose conclusion is `success`.

**When plans get written.** A milestone's remaining plans are written after the previous milestone's CI gate is green, because their contracts depend on what that milestone settled. M0 and M1 plans are written now. Plan authoring is orchestrator work (`opus` for design-heavy plans, `sonnet` for the rest); builders execute plans and do not rewrite them.

Plan filename pattern for new plans: `m<N>-c<K>-<kebab>.md`. Existing files keep their names.

---

## M0 "Engine installs" (release v0.1.0)

Exit: partner installs v0.1, all services start, `verify-report.json` green. CI gate: `dev/gate_m0.py`.

| Component | Plan file | Status | Notes |
|---|---|---|---|
| C3 ns_common + contracts | `2026-11-01-slice0-foundations.md` | **Tasks 1-5 implemented and committed; Tasks 6-16 pending** | Task 14 manifest follows spec §21.3; `Runtime.profile` is `"gpu" \| "cpu-dev"`; see the override section at the top of the file. Ends with tag `contracts-m0`. |
| C3 amendments | `m0-c3-amendments.md` | to write | Change committed Task 5 `Runtime.profile` literal from `32gb\|16gb\|cpu-dev` to `gpu\|cpu-dev` (models, schema export, config tests); add `ns_common/gpu_dlls.py` (`ensure_on_path`), a keyring wrapper, a `__version__` equals `pyproject` version test. Orchestrator work. |
| C10 CI + delivery | `m0-c10-ci-bundle.md` | to write | Split `ci.yml` into `lint`, `types`, `test`, `gate`, `bundle`; `dev/check_scope.py`; `tests/test_hygiene.py`; `dev/make_bundle.ps1`; release job; `CHANGELOG.md`. Task 1 workflow stays as is until this plan runs. |
| C1 Installer | `m0-c1-installer-core.md` | to write | `INSTALL.cmd`, `stage1.ps1` (preflight, `-DryRun`), wizard framework, machine-check step (`NS-E1xx`), downloader (Range, SHA256, `models_source`), venv step, `answers.json`. |
| C2 Runtime | `m0-c2-supervisor-verify.md` | to write | Supervisor API (`/status /acquire /release /stop_all /shutdown /warmup /events`), GPU lock, Job Object, idle unload, service table, stub service, `ns verify`, `ns doctor --bundle`, `ns stop`, `SupervisorClient`. |

## M1 "First Short" (release v0.2.0)

Exit: `studio run` produces a Short on the partner PC. CI gate: `dev/gate_m1.py` (`studio run --fake-services --format shorts_only --count 3` yields 3 QC-passing MP4s with valid sheets).

| Component | Plan file | Status | Notes |
|---|---|---|---|
| C7 + C5 (partial) | `_s1_part1.md` | written | Task 1 orchestrator, stage registry, job context (C7, `opus`); Task 2 prompt loader and shorts-only prompts (C5, `opus`). |
| C5 | `_s1_part2.md` | written | Tasks 3-5: word budgets and validators, shorts-only text stages, exemplars. |
| C4 + C6 (partial) | `_s1_part3.md` | written | Task 6 `ns_tts` service (C4); Task 7 voice stage (C6). |
| C6 (rest) | `_s1_part4.md` | to write | Tasks 8-12: transcribe and caption alignment, clip sources and cache, clips stage, vertical render, QC, outbox and upload sheet. |
| C7 + C5 + C10 | `_s1_part5.md` | to write | Task 13 minimal CLI (`studio run`, `channel`, `voice`), `studio calibrate` harness (C5), `dev/gate_m1.py`, README/CLAUDE.md touch-ups. |
| C1 Installer | `m1-c1-config-smoke.md` | to write | Config writing, keyring, channel creation from `answers.json`, `s08_smoke` (NVENC, qwentts, whisper, LLM) with the fallbacks and warnings. |

## M2 "Nightly channel" (release v0.3.0)

Exit: an unattended night yields a long-form master and its Shorts. CI gate: `dev/gate_m2.py` (floor and expansion loop, `chapters.json` vs ffprobe, 2-minute mid-roll rule, 4 Shorts, seeded negative fixtures with `NS-E6xx`, analytics reweighting).

| Component | Plan file | Status | Notes |
|---|---|---|---|
| C5 | `m2-c5-long-form-text.md` | to write after M1 gate | Schemas and fixtures for `plan`, `story`, `shorts_plan`, `chapters`, `seo_long`; prompts `long_plan`, `chapter_draft`, `continuity`, `expand`, `long_seo`, `shorts_select`, `short_hook`; word-budget module. |
| C6 | `m2-c6-long-form-media.md` | to write after M1 gate | Landscape filter graph, chapter cards, mid-roll rules, thumbnails, derived Shorts audio cut and vertical edit. |
| C7 | `m2-c7-master-first.md` | to write after M1 gate | Master track stages M1-M14, expansion loop, Shorts timing from `words/`, outbox for `long/` and `shorts/`. |
| C7 | `m2-c7-policy-gate.md` | to write after M1 gate | One class per check (`E601` to `E614`), regenerate-once, disclosure advice, negative fixtures. |
| C7 | `m2-c7-variation-cadence.md` | to write after M1 gate | Combo draw and ledger, pool minimums, channel CRUD and distinctness, cadence governor. |
| C7 | `m2-c7-review-learn-status.md` | to write after M1 gate | `studio review`, `status`, `pause`, `resume`, `mark-uploaded`, `learn --import`, DB backup, `run --daily`. |
| C2 | `m2-c2-scheduler.md` | to write after M1 gate | `Nightshift Daily` task with `WakeToRun`, `StartWhenAvailable`, 6 h stop; GPU-busy wait of 2 h (`NS-E201`). |
| C1 | `m2-c1-shortcuts-summary.md` | to write after M1 gate | Shortcuts, scheduler step, summary step. |
| C3 | `m2-c3-contracts.md` | only if a CR is accepted | Orchestrator work; tag `contracts-m2`. |
| C10 | `m2-c10-gate.md` | to write after M1 gate | `dev/gate_m2.py`. |

## M3 "Clipper + Dashboard" (release v0.4.0)

Exit: drop a video, review clips in the Dashboard. C8 and C9 are built in parallel on disjoint paths. CI gate: `dev/gate_m3.py`.

| Component | Plan file | Status | Notes |
|---|---|---|---|
| C8 | `m3-c8-clipper.md` | to write after M2 gate | Schemas and fixtures (`transcript`, `clip_plan`, `clip_layout`, `clip_template`), import, transcription wrapper, selection stage, detection, templates, layout, cut and render reusing C6, clip SEO, `studio clip`, synthetic fixture generator. |
| C9 | `m3-c9-dashboard.md` | to write after M2 gate | App skeleton with loopback check, job worker, API routes per spec §20.3, range streaming, chunked upload, static UI ported from the mockup (no CDN), fake job worker for tests. |
| C5 | `m3-c5-clip-prompts.md` | to write after M2 gate | `clip_select.md`, `clip_seo.md` with fixtures. |
| C2 | `m3-c2-dashboard-service.md` | to write after M2 gate | `dashboard` service entry, `/events` consumers, status strip data. |
| C1 | `m3-c1-dashboard-shortcut.md` | to write after M2 gate | Dashboard launcher and shortcut. |
| C3 | `m3-c3-contracts.md` | to write after M2 gate | Clipper and dashboard schemas, SQL additions (`sources`, `dashboard_jobs`); tag `contracts-m3`. Orchestrator work. |
| C10 | `m3-c10-gate.md` | to write after M2 gate | `dev/gate_m3.py`. |

## M4 "Hardening" (release v1.0.0)

Exit: spec §14.6 targets measured on the partner PC, calibration returned, operator docs final. CI gate: `dev/gate_m4.py`.

| Component | Plan file | Status | Notes |
|---|---|---|---|
| C2 | `m4-c2-tune-update-backup.md` | to write after M3 gate | `ns tune` sweep, `ns update` with rollback, `ns backup` and `ns restore`, toast notifications. |
| C5 | `m4-c5-calibration.md` | to write after M3 gate | Calibration scoring, critic bar, refusal rate, two-model switch. |
| C1 | `m4-c1-uninstall-operator.md` | to write after M3 gate | `UNINSTALL.cmd`, `OPERATOR.md` final. |
| C2 + C1 | `m4-c2-nightshift-code.md` | optional, after M3 gate | OpenCode pin, `opencode.json`, `AGENTS.md`, prewarm step, launcher. |
| C6 | `m4-c6-performance.md` | to write after partner measurements | Changes driven by measured numbers in `docs/hardware/`. |
| C10 | `m4-c10-gate-release.md` | to write after M3 gate | `dev/gate_m4.py`, v1.0.0 release checklist. |

---

## Rules every plan follows

- **No local execution.** Nothing runs on the authoring laptop: no Python, pip, uv, pytest, ruff, pyright, models, llama.cpp, torch or whisper. Heavy components are coded against their documented APIs and tested through fakes (spec §1.1); FFmpeg is the one real external tool, and it runs in CI.
- **CI gate.** Every task ends with commit, push and a green GitHub Actions run on `windows-latest`. "Run: pytest ..." steps mean exactly that. No completion claim without the CI run URL.
- **Never Fable agents.** Workers are `sonnet` (effort high) for building and ordinary code, `opus` for prompt design, policy logic and orchestrator design, `haiku` for mechanical sweeps. Always set `model` explicitly; no `fork` subagents (they inherit the Fable parent). Triage policy: `~/.claude/CLAUDE.md`.
- **Contracts freeze per milestone** with a git tag `contracts-m<N>`. Later milestones add; they change earlier contracts only via `contracts/CHANGE_REQUESTS.md`. Only the orchestrator edits `ns_common/` and `contracts/`.
- **Ownership.** One builder per component per milestone; builders touch only their component's paths (ENGINEERING.md §2.1); parallel builders push to `main` only with disjoint file sets, otherwise they use `feat/<component>` branches merged by the orchestrator.
- **TDD per task**, commit per task, conventional commit prefixes (`feat:`, `test:`, `chore:`, `docs:`) with the component scope `feat(cN):` and the two trailer lines from ENGINEERING.md §5.
- **Definition of Done** per ENGINEERING.md §5: tests in CI, ruff and pyright clean, docstrings, `NsError` codes, `NsLogger`, pinned dependencies, plan checkbox ticked.
- **Clipper and Dashboard reuse the M1 media code** (captions builder, renderer, QC, outbox) rather than duplicating it.
- **The dashboard UI must work offline (no CDN)** and is prototyped in `docs/superpowers/mockups/dashboard.html`, which the M3 C9 plan turns into `ns_dashboard/static/`.

---

## Appendix: outline of the later plans by the old slice names

Kept for the task lists. Slice 2-3 content belongs to M2, Slices 4 and 6 to M3, Slices 5, 7, 8 are split across M0, M2, M3 and M4 as the tables above show.

**Slice 2 - Master-first** (WP-D, E, F, G). Tasks: `plan`/`story`/`shorts_plan`/`chapters`/`seo_long` schemas and fixtures · prompts `long_plan`, `chapter_draft`, `continuity`, `expand`, `long_seo`, `shorts_select`, `short_hook` · stages M2–M8 with the word-budget module (`ns_studio/text/budget.py`: `target_words`, `chapter_budgets`, `min_words`) · duration check + expansion loop after voice · per-beat visual plan with `need_vertical` · Shorts timing from `words/` (M11b) · landscape filter graph, chapter cards, mid-roll suggestion rules · thumbnails (frame pick by luminance variance, Pillow text with bundled Anton) · S1 audio cut + hook prepend, S2 vertical edit reusing the Slice 1 renderer · outbox for `long/` + `shorts/` with §9.1 description builder · gate script.

**Slice 3 - Safety & growth** (WP-F, H, G). Tasks: `policy` checks as one class per check with a common `Check.run(ctx) -> PolicyCheck` · spaCy NER wrapper + allow/block lists · misleading-claims matcher · embedding self-similarity against `stories` table · pHash render uniqueness (`imagehash`) · `audio_only_voice` (stream count + spectral flatness) · clip-reuse check from `clip_usage` · disclosure advice · gate orchestration with regenerate-once · variation engine (`combo.py` weighted draw, ledger queries, pool minimums) · channel CRUD + distinctness · cadence governor (stage by channel age + analytics) · `studio review`, `status`, `pause/resume`, `learn --import` (CSV column mapping for YouTube Studio exports, matching by URL/title, reweighting with 5 % floor) · DB backup · gate script with seeded negative fixtures.

**Slice 4 - Clipper** (WP-I, E, G). Tasks: `transcript`/`clip_plan`/`clip_layout`/`clip_template` schemas + fixtures · source import + ffprobe + sha256 (`NS-E405`) · audio extract (16 kHz mono WAV) · Transcriber extension for arbitrary speech (segments + words, VAD, speaker-change heuristic) with a `FakeTranscriber` mode that reads a sidecar transcript fixture · `clip_select.md` prompt + windowing/merging/snapping/filler-filter stage · detection module (YuNet via `cv2.FaceDetectorYN` with pinned ONNX in the manifest, stable-face clustering, facecam rectangle scan with Canny edge persistence, speaking heuristic) tested on a synthetic FFmpeg fixture that draws a rectangle with a moving ellipse · `clip_templates.yaml` + layout engine producing crop regions per template · render reusing `build_vertical_graph` with new stacked/overlay graph variants · clip SEO prompt + metadata-only policy checks · Outbox `clip-<id>` · `studio clip` CLI · synthetic fixture generator `tests/fixtures/clipper/make_sample.py` · gate script.

**Slice 5 - Runtime** (WP-B, A-launchers, G). Tasks: supervisor FastAPI app (`/status`, `/acquire`, `/release`, `/stop_all`), service definitions from `runtime.json`, process management with `psutil`, GPU lock as an asyncio lock with FIFO waiters and timeouts, idle unload timer, crash restart policy, a stub service for tests · `SupervisorClient` implementing `Acquirer` · `ns tune` sweep runner (settings matrix, benchmark prompt, writes `runtime.json`) · `ns verify` (health, completion, TTS per mode, whisper, one fake or real job, §14.6 table, `verify-report.json`) · `ns doctor [--bundle]` · `ns stop` · `ns gate` · `ns update` with rollback · scheduled task registration via `schtasks`/`Register-ScheduledTask` with `WakeToRun` · launchers.

**Slice 6 - Dashboard** (WP-J, G). Tasks: FastAPI app skeleton with loopback-only `Host` check and static file serving · `dashboard_jobs` worker thread with FIFO, progress, cancel flag, supervisor acquisition · API routes grouped by page exactly as §20.3 lists them · range-request media streaming + thumbnail generation · chunked upload endpoint for the Clipper · porting the prototype HTML into `static/index.html` + `app.js` + `app.css` with a fetch layer replacing the in-memory fakes · template preview PNG generation from `clip_templates.yaml` · launcher `nightshift-dashboard.ps1` and supervisor service entry · tests with FastAPI `TestClient` for every route + a fake-services clip job through the API · gate script.

**Slice 7 - Installer** (WP-A, G). Tasks: `INSTALL.cmd` · `stage1.ps1` (preflight table with `NS-E1xx`, winget installs, uv, venv bootstrap, `-DryRun`) · wizard framework (`Step` base with `check/run/verify`, state file, retry UX, unattended mode, `--simulate-downloads`) · steps s00–s10 · downloader with resume + SHA256 + HF URL builder · `answers.json` handling + keyring · `OPERATOR.md` · `dev/make_bundle.ps1` (git archive + VERSION + sha256 sidecar) · gate script that runs the wizard in a temp home and kills it mid-way.

**Slice 8 - Nightshift Code** (WP-B, A). Tasks: OpenCode release pin in manifest · `opencode.json` + `AGENTS.md` · `s05_opencode_prewarm` step · `nightshift-code.ps1` launcher · optional-install plumbing in answers and summary.
