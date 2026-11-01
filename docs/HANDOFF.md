# Nightshift — orchestrator handoff (position file)

Read this first in a fresh session. It says where the project stands, what is decided, what is in flight, and what to do next. Update it whenever the position changes.

## What we are building
A Windows installer package (`Nightshift-<ver>.zip` with `INSTALL.cmd`) that turns one RTX 16 GB PC into an offline AI studio: writes original stories, narrates them (Qwen3-TTS GGUF via qwentts.cpp), renders 10–20 min long-form videos plus Shorts, cuts clips from the owner's own videos, with a local web dashboard and an Assistant on the local Qwen model. Spec: `BUILD_SPEC.md` (v2). Process: `docs/ENGINEERING.md`. Plans: `docs/superpowers/plans/`.

## Non-negotiables (from the owner)
- Nothing heavy runs on the authoring laptop: no models, torch, llama.cpp, whisper, no Python venv for the product. Tests run in GitHub Actions (public repo, windows-latest). Builders write, commit, push, read CI.
- Never spawn agents on the Fable model; builders are Sonnet 5.5 at effort high; Opus only for design/architecture. Fable orchestrates, reviews, merges, keeps contracts.
- The partner's GPU PC is reached only through the shipped bundle (Google Drive). The codebase stays there for further work.
- Download budget ~15 GB: 14B Q4 LLM (9 GB), TTS Q8_0 GGUF talkers CustomVoice + Base + tokenizer (4.4 GB), one whisper distil-large-v3 (1.5 GB). VoiceDesign and bigger LLM tiers on demand. `models_source` folder mode for offline installs.

## Repo and workflow
- GitHub: https://github.com/AbdulrahmanAmer/nightshift (public). Branch protection on `main` requires lint, types, test, powershell-parse, scope-check; orchestrator merges with `gh pr merge --admin`.
- Local worktrees: `D:\NightShift` (shared, do not use for new work), `D:\NightShift-main` (orchestrator's `main`), one per builder `D:\NightShift-c<N>`, UI in `D:\NightShift-ui` (branch `feat/c9-ui`). Create new ones with `git -C D:\NightShift-main worktree add D:\NightShift-<x> -b feat/<x> origin/main`.
- Builders may not edit `ns_common/`, `contracts/`, `pyproject.toml`, `.github/`; they append to `contracts/CHANGE_REQUESTS.md` and a contracts builder applies CRs (`ns_common.models` is the single source of truth; schemas in `contracts/schemas/` must equal `model_json_schema()`).
- Commit trailers: `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>` and `Claude-Session: https://claude.ai/code/session_016qaaopKpuHaLfNntdGyAbq`.

## Merged on main (2026-11-02)
C3 foundation + contracts amendments (`contracts-m0`, `contracts-m1` tags) · C7 studio core (stage runner, orchestrator with canonical `FORMAT_STAGES`, CLI, variation, policy gates, learn, cadence, review) · C4 TTS service (qwentts wrapper + PyTorch reference fallback, voice embeddings) · C5 text (M1 shorts-only, M2 master-first, 20 prompts, 12 exemplars) · C6 media (voice, captions, clips, render, QC, outbox M1+M2, content-organized Outbox §15.2a) · C2 runtime (supervisor, Job Object, lifecycle, `ns` tools, launchers) · C1 installer (INSTALL/UNINSTALL, stage1, wizard, downloader, OPERATOR.md) · C8 clipper (analysis-first, model-chosen layout, per-clip judge) · C9 dashboard backend (API, jobs worker, Assistant with tool registry and orientation tools, Library, update endpoints, URL clip jobs) · C10 delivery (CI jobs, release workflow, bundle script, hygiene tests, qwentts.cpp Windows CUDA build workflow: binary builds, 602 MB artifact) · C11 series workshop · integration pass (CLI mounts, idea bank) · media finish (caption wiring, format presets as YAML) · URL clips via yt-dlp (C8/C9) · C9 React UI (`ns_dashboard/ui`, built to `ns_dashboard/static`).

## In flight
- `feat/release-readiness` (this change, one PR): CI `ui-build` job, UI build in `release.yml`, `make_bundle.ps1` requires the committed `ns_dashboard/static`; packaging (`ns_dashboard` in the package list, package-data, pyright scope, `dashboard` extra installed by installer s04, opencv in the `studio` extra); contracts (`NS-E409`, `Channel.formats_allowed` / `captions.mask_profanity`, `emphasis` on `VisualBeat` and `ClipPlanItem`, assistant tables in `schema.sql`); `bins.yt_dlp` pin and installer download to `bin\yt-dlp\yt-dlp.exe`; the dashboard launcher opens an Edge/Chrome `--app=` window; CHANGELOG Unreleased filled. `ui-build` is already a required check on `main` (applied 2026-11-02, `docs/REPO_SETTINGS.md`); branches opened before this PR merges need `main` merged in or an admin merge.
- PR #22 (`style(dashboard)`: unslop polish and responsive layout): any change under `ns_dashboard/ui/` must come with the rebuilt `ns_dashboard/static` (CI `ui-build` fails otherwise; build with node 20, `npm ci && npm run build`).
- Known gaps: `bins.qwentts` in `manifest.json` is null until a release asset exists (tag `v0.1.0` to produce it, then pin it as described in `docs/REPO_SETTINGS.md`); the installer falls back to PyTorch TTS otherwise. The `visual_plan` and `clip_select` prompts do not ask the model for `emphasis` yet (the field is accepted and read when present). CR-C1-4 (a)(b)(c)(e) pins (spaCy wheel hash, cuBLAS/cuDNN, fallback repos, CUDA 12.8 llama.cpp) are still open. `ns_dashboard/templates.py` still reads `clip_templates.yaml` rather than the format presets.

## Decisions log (short)
D1 build blind, D2 Code optional last, D3 one LLM default, D10 local FastAPI + static UI, D11 distil-large-v3, D13 ~15 GB, D14 qwentts.cpp with PyTorch fallback; milestones M0 Provisioning → M1 Shorts pipeline → M2 Long-form nightly → M3 Clipper + Dashboard → M4 Release hardening (code complete + CI green = done; first real run on the partner PC).

## Next steps for a fresh orchestrator
1. Check `gh pr list` and `gh run list --branch main --limit 1`; merge green PRs in dependency order (contracts -> core -> others), rebase conflicting ones via their builders.
2. Merge #22 (UI polish) with its rebuilt `ns_dashboard/static`; review the React UI with screenshots (all pages, both themes, 1366x768 and 390x844).
3. Cut `v0.1.0`: move `## [Unreleased]` in `CHANGELOG.md` under `## [0.1.0]`, confirm `VERSION`, `pyproject.toml` and `ns_common.__version__` say 0.1.0, merge, wait for green main CI, then `git tag v0.1.0 && git push origin v0.1.0` (release workflow: tests, UI build check, bundle, GitHub Release; `build-qwentts.yml` attaches `qwentts-win-cuda-x64.zip`). Then pin `bins.qwentts` in `manifest.json` to that asset and its sha256 (see `docs/REPO_SETTINGS.md`).
4. Run a full read-through of BUILD_SPEC.md vs the code for gaps (series workshop production path, Assistant tool coverage, installer steps vs `ns` commands, prompts asking for `emphasis`), file them as plan tasks, assign Sonnet builders.
5. Produce the Drive bundle and `OPERATOR.md` for the partner's first run; collect `verify-report.json` back.
