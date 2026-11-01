# Nightshift M0 / C10 - CI, Bundle, Release and Hygiene Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the single-job CI into fast parallel jobs, add a release workflow that turns a `v*` tag into a GitHub Release carrying `Nightshift-<ver>.zip` (+ `.sha256`, + the `nightshift-app-<ver>.zip` update alias), enforce component ownership on pull requests, and add repo-wide hygiene tests, so that every other builder's work is checked by CI and the partner bundle is reproducible.

**Architecture:** `.github/workflows/ci.yml` runs six jobs on `windows-latest` (`lint`, `types`, `test`, `powershell-parse`, `bundle`, and `scope-check` on PRs only) under a per-branch concurrency group. `dev/make_bundle.ps1` does `git archive HEAD`, extracts to a staging folder, filters and augments it, verifies the content rules, and zips with forward-slash entry names. `dev/check_scope.py` maps `feat/c<K>-*` branch names to the path table of `docs/ENGINEERING.md` section 2.1 and fails on foreign paths. `tests/test_hygiene.py` holds the AST and file-content rules that cannot be expressed in ruff.

**Tech Stack:** GitHub Actions (`windows-latest`, Python 3.12, pip cache), PowerShell 5.1 and 7 (the bundle script runs under both), Python stdlib only for `dev/check_scope.py`, ruff `TID251`, pytest.

**Spec:** `BUILD_SPEC.md` section 0 (ground rules 1 and 4), section 1.3 (the bundle is the repository), section 4 (repo layout), section 12 (slice 7: `make_bundle.ps1` produces a zip with no `.venv`, models or binaries), section 21 (what the installer pulls, so what must not be bundled). Process: `docs/ENGINEERING.md` sections 5 to 8.

## Global Constraints

- **No local execution** (ENGINEERING section 1, rule 13). Nothing in this plan is run on the authoring laptop, including `dev/make_bundle.ps1`. CI is the only authority; a task is done with a run URL whose conclusion is `success`.
- Only C10 edits `.github/` and `pyproject.toml` (dependencies and tool config only). Dependency additions that other builders request in `contracts/CHANGE_REQUESTS.md` and that only add packages to the `dev` extra are applied here and their CR status line is set to `applied in <commit>`. `ns_common/` and `contracts/` are not edited (except CR status lines).
- CI runs stay under 3 minutes: jobs run in parallel, pip is cached through `actions/setup-python`, FFmpeg is installed only in `test`.
- `make_bundle.ps1` stays compatible with Windows PowerShell 5.1 (the partner PC) and PowerShell 7 (`pwsh` in the release job): no `??`, no ternary, no `&&`, zip entries written with forward slashes through `System.IO.Compression`.
- Bundle rules (ENGINEERING non-negotiable 1): no `.venv`, `*.gguf`, `*.onnx`, `*.safetensors`, `*.mp4`, `*.exe`; zip below 25 MB; `.github/`, `docs/superpowers/plans/_*` and fixtures above 1 MB are left out.
- Hygiene rules are checked by reading source (AST) so they never import heavy packages.
- Commits: `<type>(c10): <subject>` with the two trailers from ENGINEERING section 5. Stage files explicitly.
- Worker allocation: all tasks `sonnet`; Task 5 (workflows) benefits from a second read by `opus` if CI is red twice.

---

## File structure created by this plan

```
.github/workflows/ci.yml          lint, types, test, powershell-parse, bundle, scope-check
.github/workflows/release.yml     tag v* -> test -> make_bundle -> GitHub Release
dev/check_scope.py                branch-to-component path guard (stdlib only)
dev/make_bundle.ps1               git archive + filter + verify + zip + sha256 (-DryRun)
tests/test_hygiene.py             version, imports, shell=True, print, PowerShell prelude, error codes, schema drift
tests/test_check_scope.py         unit tests for the scope mapping
VERSION                           0.1.0
CHANGELOG.md                      Keep a Changelog, Unreleased section
docs/REPO_SETTINGS.md             branch protection, tag ruleset, Actions permissions
pyproject.toml                    dev extra additions, ruff TID251 + per-file-ignores
.gitattributes / .gitignore       small tweaks (binary types, report.xml)
```

---

### Task 1: VERSION, CHANGELOG, version test

**Files:** Create `VERSION`, `CHANGELOG.md`; test in `tests/test_hygiene.py` (Task 3).

- [x] **Step 1:** `VERSION` holds `0.1.0` and a newline. `CHANGELOG.md` follows Keep a Changelog with an `## [Unreleased]` section listing the C10 deliverables.
- [x] **Step 2:** `test_version_files_agree` asserts `VERSION == ns_common.__version__ == pyproject project.version`, semver-shaped, and that `CHANGELOG.md` has the Unreleased heading.
- [x] **Step 3:** Commit `feat(c10): VERSION, CHANGELOG and version consistency test`.

### Task 2: scope check

**Files:** Create `dev/check_scope.py`, `tests/test_check_scope.py`.

- [x] **Step 1 (tests first):** cases for owned path accepted, foreign path rejected, `ns_common/` and `contracts/` rejected for everyone except `feat/c3-*` and `orchestrator/*`, `contracts/CHANGE_REQUESTS.md` allowed for all, C6 versus C8 on `clip_templates.yaml`, own plan file allowed and other component's plan rejected, `tests/conftest.py` orchestrator-only, unknown branch names only subject to the protected-path rule, `main` unchecked.
- [x] **Step 2:** `OWNERSHIP` table from ENGINEERING section 2.1; `check(branch, files) -> list[Violation]`; CLI `--base origin/main --branch <name>` runs `git diff --name-only -z --no-renames <base>...HEAD`, prints one line per violation, exits 1 on any.
- [x] **Step 3:** Commit `feat(c10): PR scope check mapping branches to component paths`.

### Task 3: hygiene tests

**Files:** Create `tests/test_hygiene.py`.

- [x] **Step 1:** Rules: version files agree; no module-level import of `torch`, `faster_whisper`, `spacy`, `cv2`, `qwen_tts`, `faster_qwen3_tts` under `ns_*/` (function-level and `TYPE_CHECKING` imports allowed); no `shell=True` in `ns_*`, `installer`, `tools`, `dev`; no `print(` in `ns_*` outside `cli.py`, `__main__.py`, `if __name__ == "__main__":` blocks and the NsLogger console sink `ns_common/logging.py`; every `.ps1` has `Set-StrictMode -Version Latest` then `$ErrorActionPreference = 'Stop'` as its first statements (after an optional `[CmdletBinding()]`/`param()` block, which PowerShell requires first), is ASCII and avoids `Invoke-Expression`; every `NS-[EW]nnn` literal in source exists in `contracts/errors.json`; `contracts/schemas/*.schema.json` equal `export_schemas` output.
- [x] **Step 2:** Add `Set-StrictMode`/`$ErrorActionPreference` to `dev/run_services_dev.ps1` (the one existing script that lacked them).
- [x] **Step 3:** Commit `test(c10): repo hygiene tests`.

### Task 4: bundle script

**Files:** Create `dev/make_bundle.ps1`.

- [x] **Step 1:** Parameters `-Version`, `-OutDir`, `-DryRun`. Version defaults to `VERSION`; a leading `v` is stripped; a mismatch with `VERSION` is an error.
- [x] **Step 2:** `git archive --format=zip HEAD`, extract to a temp stage, delete `.github`, `docs/superpowers/plans/_*`, fixtures above 1 MB; write `VERSION`; lift `installer/answers.example.json` to the root when no root copy exists; report whether `OPERATOR.md` is present.
- [x] **Step 3:** Verify: forbidden names and `.venv` segments, zip below 25 MB, required files (`VERSION`, `pyproject.toml`). Write the zip with forward-slash entry names, then `Nightshift-<ver>.zip.sha256` (`<hash>  <name>`) and a copy `nightshift-app-<ver>.zip`. `-DryRun` runs the same flow in a temp folder, prints every entry and writes nothing to `dist`.
- [x] **Step 4:** Commit `feat(c10): make_bundle.ps1 with content verification and dry run`.

### Task 5: workflows

**Files:** Rewrite `.github/workflows/ci.yml`, create `.github/workflows/release.yml`.

- [x] **Step 1:** `ci.yml` as in the architecture paragraph. `powershell-parse` runs under Windows PowerShell 5.1 (the target shell): parser check of every tracked `.ps1`, structural check of every `.cmd` (CRLF, ASCII; the PowerShell parser cannot read batch syntax), PSScriptAnalyzer errors, `installer/stage1.ps1 -DryRun` when present, `make_bundle.ps1 -DryRun`. `bundle` builds the real zip with `pwsh` and uploads it as an artifact so the release path is exercised on every push.
- [x] **Step 2:** `release.yml`: on tags `v*`, job `test` (pytest), job `release` (`contents: write` only here): tag must equal `VERSION` and `CHANGELOG.md` must contain the version heading, `pwsh dev/make_bundle.ps1 -Version ${{ github.ref_name }}`, `gh release create` with `--generate-notes`.
- [x] **Step 3:** Commit `ci(c10): split CI into parallel jobs, add release workflow`; push; read the run with `gh run list --branch feat/c10-delivery --limit 1` and `gh run view <id> --log-failed` until green.

### Task 6: pyproject and ruff

**Files:** Modify `pyproject.toml`, `.gitattributes`, `.gitignore`.

- [x] **Step 1:** `dev` extra gains `fastapi`, `psutil`, `python-multipart`, `opencv-python-headless`, `Pillow`, `imagehash` (httpx, numpy, pyyaml are base dependencies; jsonschema is already present).
- [x] **Step 2:** ruff: add `TID251`, banned-api `subprocess`, per-file-ignores for `ns_common/ffmpeg.py`, `ns_supervisor/**`, `installer/**`, `tools/**`, `dev/**`, `tests/**`.
- [x] **Step 3:** Apply open dependency CRs from `contracts/CHANGE_REQUESTS.md` (status line `applied in <commit>`).
- [x] **Step 4:** Commit `chore(c10): dev dependencies and subprocess ban`.

### Task 7: repository settings

**Files:** Create `docs/REPO_SETTINGS.md`.

- [x] **Step 1:** Document branch protection for `main` (required checks `lint`, `types`, `test`; no force-push; no deletion; administrators not forced so direct pushes by the owner token keep working), the tag ruleset for `v*` and `contracts-m*`, and default workflow permissions `read`.
- [x] **Step 2:** Apply with `gh api` once CI names exist; record the outcome (success or the API error) in the doc and in the final report.
- [x] **Step 3:** Commit `docs(c10): repository settings`.

### Task 8: green and PR

- [x] **Step 1:** CI green on `feat/c10-delivery` (run URL recorded in the report).
- [x] **Step 2:** `gh pr create`; read the `scope-check` run on the PR.
- [x] **Step 3:** Report: PR URL, CI URLs and conclusions, CRs applied, deviations.

### Task 9 (added by the orchestrator): qwentts.cpp CUDA build

**Files:** Create `.github/workflows/build-qwentts.yml`; document the result in `docs/REPO_SETTINGS.md` section "qwentts build".

- [x] **Step 1:** Workflow on `workflow_dispatch` and tags `v*`: pinned commit of `ServeurpersoCom/qwentts.cpp`, CUDA 12.9.1 through `Jimver/cuda-toolkit`, CMake + Ninja + MSVC, `GGML_CUDA=ON`, architectures `75-virtual;86-real;89-real;120a-real`, package `qwentts-win-cuda-x64.zip` + `.sha256`, upload as artifact, attach to the release on tags.
- [x] **Step 2:** Run it from the feature branch (temporary branch trigger), fix until green: `windows-latest` and `windows-2025` ship Visual Studio 2026 which nvcc 12.9 rejects, so the job runs on `windows-2022`. Remove the temporary trigger.
