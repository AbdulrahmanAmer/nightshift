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
C3 foundation + contracts amendments (`contracts-m0`, `contracts-m1` tags) · C7 studio core (stage runner, orchestrator with canonical `FORMAT_STAGES`, CLI, variation, policy gates, learn, cadence, review) · C4 TTS service (qwentts wrapper + PyTorch reference fallback, voice embeddings) · C5 text (M1 shorts-only, M2 master-first, 20 prompts, 12 exemplars) · C6 media (voice, captions, clips, render, QC, outbox M1+M2, content-organized Outbox §15.2a) · C2 runtime (supervisor, Job Object, lifecycle, `ns` tools, launchers) · C1 installer (INSTALL/UNINSTALL, stage1, wizard, downloader, OPERATOR.md) · C8 clipper (analysis-first, model-chosen layout, per-clip judge) · C9 dashboard backend (API, jobs worker, Assistant with tool registry and orientation tools, Library, update endpoints, URL clip jobs) · C10 delivery (CI jobs, release workflow, bundle script, hygiene tests, qwentts.cpp Windows CUDA build workflow: binary builds, 602 MB artifact) · C11 series workshop.

## In flight
- `feat/c9-ui`: dashboard front-end. Vanilla version exists (bento look, posters, charts, Library, Assistant). An **Opus agent is rebuilding it in React + TypeScript + Vite + Tailwind under `ns_dashboard/ui/` with build output to `ns_dashboard/static/`**, using the `unslop` and `screenshot-to-code` skills and the reference images in `D:\NightShift-refs\ref-01..10.png`. Decide after review which version ships; CI must get a `ui-build` job (node on the runner) before the bundle includes the React build.
- `feat/integration-pass` (PR #18, conflicting, builder active): CLI mounts for `studio clip|series|ask`, series stages import, idea bank feeding `ideas`, local model copies replaced by `ns_common.models`.
- `feat/media-finish`: caption placement/emphasis/speaker colours wired into stages, formats-as-YAML presets (§17.1), generic layout engine.
- `feat/c8-url-source`: `import_url` via yt-dlp for the Clipper (manifest pin for yt-dlp pending).
- Known gaps: `bins.qwentts` in `manifest.json` is null until a release asset exists (tag a `v0.1.0` release to produce it); installer falls back to PyTorch TTS otherwise. Launcher should open the dashboard in Chrome/Edge `--app=` mode (spec update pending).

## Decisions log (short)
D1 build blind, D2 Code optional last, D3 one LLM default, D10 local FastAPI + static UI, D11 distil-large-v3, D13 ~15 GB, D14 qwentts.cpp with PyTorch fallback; milestones M0 Provisioning → M1 Shorts pipeline → M2 Long-form nightly → M3 Clipper + Dashboard → M4 Release hardening (code complete + CI green = done; first real run on the partner PC).

## Next steps for a fresh orchestrator
1. Check `gh pr list` and `gh run list --branch main --limit 1`; merge green PRs in dependency order (contracts → core → others), rebase conflicting ones via their builders.
2. Review the Opus React UI against the references with screenshots (all pages, both themes, 1366×768 and 390×844); pick the shipping UI; add the CI `ui-build` job (C10 scope) and make `dev/make_bundle.ps1` include `ns_dashboard/static`.
3. Tag `v0.1.0` to run the release workflow (bundle + qwentts binary), then pin `bins.qwentts` in `manifest.json` to the release asset hash.
4. Run a full read-through of BUILD_SPEC.md vs the code for gaps (series workshop production path, Assistant tool coverage, installer steps vs `ns` commands), file them as plan tasks, assign Sonnet builders.
5. Produce the Drive bundle and `OPERATOR.md` for the partner's first run; collect `verify-report.json` back.
