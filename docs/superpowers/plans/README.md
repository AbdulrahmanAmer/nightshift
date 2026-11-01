# Nightshift implementation plans — index

Spec: `../../../BUILD_SPEC.md` (v2). One plan per slice (§12 of the spec). A slice's plan is written when the previous slice's gate has passed, because its contracts depend on what that slice settled. Plans for Slices 0 and 1 exist now.

| Slice | Plan file | Status | Gate |
|---|---|---|---|
| 0 Foundations | `2026-11-01-slice0-foundations.md` | written | unit tests green; `tools/resolve_manifest.py --check` passes; fakes validate against schemas; FFmpeg test clip renders |
| 1 One Short | `2026-11-01-slice1-one-short.md` | written | `studio run --fake-services --format shorts_only --count 3` yields 3 QC-passing MP4s + valid sheets |
| 2 Master-first | `slice2-master-first.md` | to write after Slice 1 gate | fake master job, floor/expansion exercised, chapters match ffprobe, 4 Shorts cut |
| 3 Safety & growth | `slice3-safety-growth.md` | to write after Slice 2 gate | both policy gates pass on a good job, reject 4 seeded bad fixtures with the right `NS-E6xx`, analytics import reweights |
| 4 Runtime | `slice4-runtime.md` | to write after Slice 3 gate | supervisor serializes two acquirers against a stub service, idle unload, task registration, doctor bundle, update/rollback |
| 5 Installer | `slice5-installer.md` | to write after Slice 4 gate | `wizard.py --answers --simulate-downloads` runs all steps in a temp home, resumes after a kill, bundle zip is clean |
| 6 Nightshift Code | `slice6-code.md` | optional, after Slice 5 | OpenCode config validates, launcher acquires/releases via stub |
| 7 First run on work machine | no plan; an `OPERATOR.md` checklist + the diagnostics bundle | after Slice 5 (6) | `verify-report.json` from the GPU PC shows §14.6 numbers and one real master in the Outbox |

## Rules every plan follows

- **Nothing heavy on the build laptop.** No models, torch, llama.cpp, whisper. Heavy components are coded against their documented APIs and tested through fakes (spec §1.1). FFmpeg is the one real external tool used in tests.
- **Contracts freeze per slice** with a git tag `contracts-s<N>`. Later slices add; they change earlier contracts only via `contracts/CHANGE_REQUESTS.md`.
- **Worker model triage** (from `~/.claude/CLAUDE.md`): `sonnet` for ordinary code tasks, `opus` for prompt design, policy logic and orchestrator design, `haiku` for mechanical sweeps. Never inherit the session model by default.
- **TDD per task**, commit per task, conventional commit prefixes (`feat:`, `test:`, `chore:`, `docs:`).

## Outline of the later plans (so the shape is known now)

**Slice 2 — Master-first** (WP-D, E, F, G). Tasks: `plan`/`story`/`shorts_plan`/`chapters`/`seo_long` schemas and fixtures · prompts `long_plan`, `chapter_draft`, `continuity`, `expand`, `long_seo`, `shorts_select`, `short_hook` · stages M2–M8 with the word-budget module (`ns_studio/text/budget.py`: `target_words`, `chapter_budgets`, `min_words`) · duration check + expansion loop after voice · per-beat visual plan with `need_vertical` · Shorts timing from `words/` (M11b) · landscape filter graph, chapter cards, mid-roll suggestion rules · thumbnails (frame pick by luminance variance, Pillow text with bundled Anton) · S1 audio cut + hook prepend, S2 vertical edit reusing the Slice 1 renderer · outbox for `long/` + `shorts/` with §9.1 description builder · gate script.

**Slice 3 — Safety & growth** (WP-F, H, G). Tasks: `policy` checks as one class per check with a common `Check.run(ctx) -> PolicyCheck` · spaCy NER wrapper + allow/block lists · misleading-claims matcher · embedding self-similarity against `stories` table · pHash render uniqueness (`imagehash`) · `audio_only_voice` (stream count + spectral flatness) · clip-reuse check from `clip_usage` · disclosure advice · gate orchestration with regenerate-once · variation engine (`combo.py` weighted draw, ledger queries, pool minimums) · channel CRUD + distinctness · cadence governor (stage by channel age + analytics) · `studio review`, `status`, `pause/resume`, `learn --import` (CSV column mapping for YouTube Studio exports, matching by URL/title, reweighting with 5 % floor) · DB backup · gate script with seeded negative fixtures.

**Slice 4 — Runtime** (WP-B, A-launchers, G). Tasks: supervisor FastAPI app (`/status`, `/acquire`, `/release`, `/stop_all`), service definitions from `runtime.json`, process management with `psutil`, GPU lock as an asyncio lock with FIFO waiters and timeouts, idle unload timer, crash restart policy, a stub service for tests · `SupervisorClient` implementing `Acquirer` · `ns tune` sweep runner (settings matrix, benchmark prompt, writes `runtime.json`) · `ns verify` (health, completion, TTS per mode, whisper, one fake or real job, §14.6 table, `verify-report.json`) · `ns doctor [--bundle]` · `ns stop` · `ns gate` · `ns update` with rollback · scheduled task registration via `schtasks`/`Register-ScheduledTask` with `WakeToRun` · launchers.

**Slice 5 — Installer** (WP-A, G). Tasks: `INSTALL.cmd` · `stage1.ps1` (preflight table with `NS-E1xx`, winget installs, uv, venv bootstrap, `-DryRun`) · wizard framework (`Step` base with `check/run/verify`, state file, retry UX, unattended mode, `--simulate-downloads`) · steps s00–s10 · downloader with resume + SHA256 + HF URL builder · `answers.json` handling + keyring · `OPERATOR.md` · `dev/make_bundle.ps1` (git archive + VERSION + sha256 sidecar) · gate script that runs the wizard in a temp home and kills it mid-way.

**Slice 6 — Nightshift Code** (WP-B, A). Tasks: OpenCode release pin in manifest · `opencode.json` + `AGENTS.md` · `s05_opencode_prewarm` step · `nightshift-code.ps1` launcher · optional-install plumbing in answers and summary.
