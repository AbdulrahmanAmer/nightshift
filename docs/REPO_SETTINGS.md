# Repository settings

Settings that live in GitHub rather than in files. Owner: C10. Rationale: `docs/ENGINEERING.md` section 7.
Applied on 2026-11-01 with `gh api` using the owner's token; every command is repeatable.

## Branch protection on `main` (applied)

| Setting | Value | Why |
|---|---|---|
| Required status checks | `lint`, `types`, `test`, `ui-build`, `powershell-parse`, `scope-check` | The jobs of `.github/workflows/ci.yml` that decide merge safety (`scope-check` runs on pull requests only; `ui-build` rebuilds `ns_dashboard/ui` with node 20 and fails when the committed `ns_dashboard/static` differs). `bundle` reports but does not block. Names must equal the job ids in ci.yml. `test` is the job that gathers the unit-test shards `test-shard (1/6)` to `(6/6)` and `test-collect` (they are not required themselves): it always runs, and fails when any of them failed, was skipped or was cancelled, or when the shards did not together run exactly the unsharded collection (`dev/ci_shards.py verify`). A docs-only pull request (H-11; `dev/ci_changes.py decide` in the job `changes`: only `docs/**`, top-level `*.md` and `contracts/CHANGE_REQUESTS.md` changed) runs no shard: `test` then gathers `test-docs` (the test files that read documents) and passes only when they ran and passed, saying how many; `types` and `ui-build` still report, with one step that says "docs-only change: not applicable" (a skipped required job would count as passed without saying why); `lint`, `powershell-parse` and `scope-check` run as always. |
| Require branches to be up to date | off | Builders on disjoint paths would otherwise rebase constantly. |
| Force pushes | forbidden | History of published branches is never rewritten (ENGINEERING section 5). |
| Branch deletion | forbidden | |
| Pull request reviews | not required | A solo orchestrator merges; the checks are the gate. |
| Include administrators | off | The owner token pushes to `main` directly when file sets are disjoint (ENGINEERING section 5); with this off, admins bypass the required checks. Turn it on once every merge goes through a pull request. |

### The checks a pull request shows (H-11, H-3b)

Read the checks by name, not by count: a docs-only pull request and a code pull request show different sets.
A docs-only pull request is one whose changes are all `docs/**`, top-level `*.md` or `contracts/CHANGE_REQUESTS.md`
and touch none of the CI machinery (`dev/ci_changes.py`, `dev/ci_shards.py`, `dev/ci_shard_weights.json`,
`.github/`); every other pull request is a code pull request. The six shards are `SHARDS` in `dev/ci_shards.py`.

| Check name (as GitHub shows it) | Required | Docs-only pull request | Code pull request |
|---|---|---|---|
| `changes` | no | pass (says `docs_only=true`) | pass (says `docs_only=false`) |
| `lint` | yes | pass | pass |
| `types` | yes | pass: one step, "docs-only change: not applicable" | pass: pyright |
| `ui-build` | yes | pass: one step, "docs-only change: not applicable" | pass: build equals the committed one |
| `powershell-parse` | yes | pass | pass |
| `scope-check` | yes | pass | pass |
| `test` | yes | pass: "docs-only change: N document tests ran, the code suite was not run" | pass: every shard and `test-collect` passed and ran the whole collection |
| `test-docs` | no | pass | skipped |
| `test-shard (1/6)` ... `test-shard (6/6)` | no | one skipped entry named `test-shard (${{ matrix.shard }}/6)` (a matrix skipped before it expands) | six entries, each pass |
| `test-collect` | no | skipped | pass |
| `bundle` | no | skipped | pass |

So a green docs-only pull request shows 8 passing and 3 skipped checks; a green code pull request shows 15
passing and 1 skipped (`test-docs`). A cancelled run is never green: its `test` fails ("this run was
cancelled"). Any other result, or a missing name, is not a mergeable set.
```powershell
@'
{
  "required_status_checks": {"strict": false, "contexts": ["lint", "types", "test", "ui-build", "powershell-parse", "scope-check"]},
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": false,
  "required_conversation_resolution": false
}
'@ | gh api -X PUT repos/AbdulrahmanAmer/nightshift/branches/main/protection --input -
```

Result of the call: HTTP 200; `required_status_checks.contexts = ["lint","types","test","powershell-parse","scope-check"]`, `allow_force_pushes = false`,
`allow_deletions = false`, `enforce_admins = false`.

`ui-build` was added to the required checks on 2026-11-02 (PR #24, release readiness) by re-running the PUT above with the
`contexts` list shown (response: `["lint","types","test","powershell-parse","scope-check","ui-build"]`). The check only exists on branches that carry the job, so PRs opened before #24 merges need `main`
merged in (or an admin merge).

## Tag ruleset (applied)

Ruleset `protect-release-tags` (id 26322610), target `tag`, refs `refs/tags/v*` and `refs/tags/contracts-m*`,
rules `update`, `deletion`, `non_fast_forward`; the repository admin role bypasses. Effect: a published release tag or
contract freeze tag cannot be moved or deleted; only the owner can create one.

```powershell
@'
{
  "name": "protect-release-tags", "target": "tag", "enforcement": "active",
  "conditions": {"ref_name": {"include": ["refs/tags/v*", "refs/tags/contracts-m*"], "exclude": []}},
  "bypass_actors": [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}],
  "rules": [{"type": "update"}, {"type": "deletion"}, {"type": "non_fast_forward"}]
}
'@ | gh api -X POST repos/AbdulrahmanAmer/nightshift/rulesets --input -
```

## Actions permissions (applied)

Default `GITHUB_TOKEN` permissions are read-only (`default_workflow_permissions=read`); workflows that publish declare
`contents: write` on the single job that needs it (`release.yml` job `release`, `build-qwentts.yml` job `release-asset`).
No secrets are stored in the repository.

```powershell
gh api -X PUT repos/AbdulrahmanAmer/nightshift/actions/permissions/workflow -f default_workflow_permissions=read -F can_approve_pull_request_reviews=false
```

## Workflows

| File | Trigger | What |
|---|---|---|
| `.github/workflows/ci.yml` | pull request; push to `main` and to tags; nightly schedule | `changes` (the docs-only decision), `lint`, `types`, `test` (gathers `test-shard (1/6)` to `(6/6)` and `test-collect`, or `test-docs` on a docs-only pull request), `ui-build`, `powershell-parse`, `bundle`; `scope-check` on pull requests only. A pull request branch is tested by its `pull_request` run alone (a push to it starts nothing a second time); a branch with no pull request has no CI until one is opened, and a draft pull request runs it. |
| `.github/workflows/release.yml` | tag `v*` | tests (the same six shards and the same proof, no marker filter: a full run; a tag run has no docs-only path), UI build (node 20) and check, `dev/make_bundle.ps1`, GitHub Release (body from `dev/release_notes.md` plus the changelog entries) with `Nightshift-Setup-<ver>.zip`, `Nightshift-Update-<ver>.zip` and a `.sha256` for each (v0.1.0 and v0.1.1 used `Nightshift-<ver>.zip`, `.sha256`, `nightshift-app-<ver>.zip`) |
| `.github/workflows/build-qwentts.yml` | `workflow_dispatch`, tag `v*` | builds `Nightshift-VoiceEngine-win-cuda-x64.zip` (v0.1.0: `qwentts-win-cuda-x64.zip`) on `windows-2022` (see "qwentts build") |

Cutting a release (orchestrator; the full loop including the qwentts pin is under "Release loop" below): move the `## [Unreleased]` entries in `CHANGELOG.md` under `## [x.y.z]`, set `VERSION`,
`pyproject.toml` and `ns_common.__version__` to `x.y.z`, merge to `main`, wait for green CI, then
`git tag vX.Y.Z && git push origin vX.Y.Z`. `release.yml` fails when the tag, `VERSION` or the changelog heading disagree.

## Release loop: tag, release workflow, `bins.qwentts` pin

`bins.qwentts` in `manifest.json` is `null` until a release carries `qwentts-win-cuda-x64.zip`; the loop below fills it.
Release tags cannot be moved or deleted (tag ruleset above), so each step is done once, in this order.

1. **Before tagging.** `main` CI is green; `VERSION`, `pyproject.toml` and `ns_common.__version__` say `0.1.0`; `CHANGELOG.md` has
   `## [0.1.0] - <date>` with the entries under it.
2. **Tag.** `git tag v0.1.0 && git push origin v0.1.0` on the green `main` commit.
3. **Two workflows start on the tag, independently.**
   - `release.yml` (about 10 minutes): tests, the tag/`VERSION`/changelog check, `ns_dashboard/ui` build (`npm run build` also builds the installer
     UI) and a diff against the committed `ns_dashboard/static` and `installer/ui/static`, `dev/make_bundle.ps1`, then a
     GitHub Release `Nightshift v0.1.0` with `Nightshift-0.1.0.zip`, `Nightshift-0.1.0.zip.sha256` and `nightshift-app-0.1.0.zip`.
   - `build-qwentts.yml` (about 45 minutes, `windows-2022`): builds qwentts.cpp with CUDA and attaches
     `qwentts-win-cuda-x64.zip` and `qwentts-win-cuda-x64.zip.sha256` to the same release (it creates the release when
     `release.yml` has not yet; the other workflow then only uploads its assets).
4. **Pin.** When both are green, read the digest of the published asset (never of another build; the zip is not reproducible):
   `gh api repos/AbdulrahmanAmer/nightshift/releases/tags/v0.1.0 --jq '.assets[]|{name,digest}'`. Set in `manifest.json`:
   `"qwentts": {"version": "0.1.0", "url": "https://github.com/AbdulrahmanAmer/nightshift/releases/download/v0.1.0/qwentts-win-cuda-x64.zip", "sha256": "<digest without the sha256: prefix>"}`.
   Open a PR (`tools/resolve_manifest.py --check` and the manifest tests run in CI) and merge it.
5. **What the pin changes.** `Nightshift-0.1.0.zip` was built from the tagged commit, so its manifest still has `qwentts: null`:
   the installer then probes `releases/latest/download/qwentts-win-cuda-x64.zip` and uses it unverified when it exists. The
   verified pin ships in the next bundle (cut `v0.1.1` with the pin, or any later release). If the qwentts build fails, fix
   `build-qwentts.yml`, run it with `workflow_dispatch` (it uploads a workflow artifact only; attach the zip to the release by
   hand with `gh release upload v0.1.0 <zip> <zip>.sha256`) and continue at step 4. To bump qwentts.cpp later, change
   `QWENTTS_SHA` in the workflow, tag the next release, and repeat step 4.

### Release asset names (from v0.1.2)

| Asset | Meaning | Before (v0.1.0, v0.1.1) |
|---|---|---|
| `Nightshift-Setup-<ver>.zip` + `.sha256` | the installer bundle; the file to download | `Nightshift-<ver>.zip` |
| `Nightshift-Update-<ver>.zip` + `.sha256` | update-from-file package (Settings > Update from a file, `ns update`) | `nightshift-app-<ver>.zip` |
| `Nightshift-VoiceEngine-win-cuda-x64.zip` + `.sha256` | qwentts voice engine, downloaded by the installer | `qwentts-win-cuda-x64.zip` (v0.1.0 only) |

`ns update --github` and the dashboard updater pick `Nightshift-Update-<ver>.zip` first and fall back to `nightshift-app-<ver>.zip`,
so a machine on v0.1.1 can update to a newer release and the reverse lookup of older releases still works. The v0.1.0 asset
`qwentts-win-cuda-x64.zip` stays where it is: `manifest.json` `bins.qwentts.url` keeps pointing at it (the sha256 pin is of that file)
until a release with `Nightshift-VoiceEngine-win-cuda-x64.zip` exists; then re-pin to that release's URL and digest in a PR.
The installer's unpinned probe `releases/latest/download/qwentts-win-cuda-x64.zip` only applies while the pin is `null`.

## qwentts build

Workflow: `.github/workflows/build-qwentts.yml` (triggers: `workflow_dispatch` and tags `v*`; on a tag the zip is also
attached to the GitHub Release, creating the release first if `release.yml` has not yet).

Last build: run https://github.com/AbdulrahmanAmer/nightshift/actions/runs/41341093235, conclusion `success`
(build job 19:18 to 20:01 UTC, about 42 minutes: CUDA install 6 min, compile 35 min).

| Item | Value |
|---|---|
| Source | `ServeurpersoCom/qwentts.cpp` commit `5916bf7c79dbb4e66fc059cdc507a79a23a28bb4` (master, 2026-10-16), submodule `ggml` fetched recursively; the project has a plain `CMakeLists.txt` |
| Toolchain | CUDA 12.9.1 via `Jimver/cuda-toolkit@v0.2.36` (network method, sub-packages nvcc, cudart, cublas, cublas_dev, thrust, visual_studio_integration), CMake + Ninja, MSVC 14.4x |
| Runner | `windows-2022`, not `windows-latest` (see failures below) |
| CMake flags | `-G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES="75-virtual;86-real;89-real;120a-real" -DCMAKE_CUDA_FLAGS=-allow-unsupported-compiler -DGGML_NATIVE=OFF -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON`. `120a-real` is the Blackwell (sm_120) SASS the project itself uses for CUDA 12.8+; 89 covers Ada, 86 Ampere, 75-virtual is PTX for anything else. `GGML_NATIVE=OFF` stops the CPU code being tuned to the CI runner's CPU. |
| Targets | `tts-server`, `qwen-tts`, `qwen-codec` |
| Output | artifact `Nightshift-VoiceEngine-win-cuda-x64` with `Nightshift-VoiceEngine-win-cuda-x64.zip` and `.zip.sha256` (the v0.1.0 build, 602.7 MB, was named `qwentts-win-cuda-x64.zip`). Content: the three exes, `ggml.dll`, `ggml-base.dll`, `ggml-cpu.dll`, `ggml-cuda.dll`, `cudart64_12.dll`, `cublas64_12.dll`, `cublasLt64_12.dll`, the MSVC runtime DLLs, `BUILD-INFO.txt`. |
| Pin for manifest.json | `bins.qwentts`: URL `https://github.com/AbdulrahmanAmer/nightshift/releases/download/<tag>/Nightshift-VoiceEngine-win-cuda-x64.zip` (only for a release that has the new name; `v0.1.0` has `qwentts-win-cuda-x64.zip` and `manifest.json` keeps that URL until a newer release has been built and re-pinned), SHA256 from the `.sha256` asset of that same release. The zip is not byte-reproducible, so the hash must come from the published asset, never from another build. |
| Not verified | The exes were not run on a GPU (no GPU in CI). `dumpbin /dependents tts-server.exe` resolves to the shipped DLLs; `tts-server.exe --help` exited 1 without output on the runner (informational step, `continue-on-error`). `ns verify` on the partner PC is the real test (risk 1 in ENGINEERING section 9, wrapper `NS-W301`). |

What failed on the way, exactly:

1. `windows-latest` (run 41339184370): configure failed while identifying the CUDA compiler,
   `nvcc error : 'cudafe++' died with status 0xC0000005 (ACCESS_VIOLATION)`. `windows-latest` is now the Visual Studio 2026
   image (MSVC 14.51, `--microsoft_version=1951`), which nvcc 12.9 does not support.
2. `windows-2025` (run 41339438559): the same image family; `host_config.h(170): fatal error C1189: #error: -- unsupported
   Microsoft Visual Studio version! Only the versions between 2017 and 2022 (inclusive) are supported!` and the same
   `cudafe++` crash. `windows-2022` has Visual Studio 2022 and builds cleanly.

Size: `cublasLt64_12.dll` alone is 669 MB, so the zip is large (about 600 MB, below the 2 GB release asset limit). It is
downloaded once by the installer, never bundled into `Nightshift-<ver>.zip`. If that is too heavy for the partner's link,
the options are (a) pin the zip to a CUDA 13.x build whose cuBLAS may be smaller, or (b) let the installer fetch the CUDA
runtime DLLs separately like the llama.cpp `cudart-*` assets. Neither is needed for M0.

## manifest.json binary pins

`manifest.json` `bins` entries carry `{version, url, sha256}`; the `Manifest` model is strict, so notes live here, not in the file.

| Key | Source | Notes |
|---|---|---|
| `yt_dlp` | `https://github.com/yt-dlp/yt-dlp/releases/download/<tag>/yt-dlp.exe` | Pinned to `2026.10.26`. The sha256 is the `digest` field of the asset in `gh api repos/yt-dlp/yt-dlp/releases/latest` (the same value as the release's `SHA2-256SUMS`), so nothing was downloaded to compute it. Installer step `s02_download_bins` places it at `bin\yt-dlp\yt-dlp.exe`; the Clipper runs it for `studio clip <url>`. To bump: read the new tag and asset digest the same way and edit the three fields. |
| `webview2` (CR-C2-host, applied) | `https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/<ver>/microsoft.web.webview2.<ver>.nupkg` | Pinned to `1.0.2903.40` (stable). The sha256 was computed from the `.nupkg` (8,769,233 bytes) inside a GitHub Actions run, never on the authoring laptop. Installer step `s02_download_bins` verifies it, then unpacks only `Microsoft.Web.WebView2.Core.dll`, `Microsoft.Web.WebView2.Wpf.dll` (from `lib/net462/`, else `lib/net45/`) and `WebView2Loader.dll` (from `runtimes/win-x64/native/`) into `bin\webview2\`; no DLL is committed. `launchers/nightshift-host.ps1` loads them from there. A sha256 that is not 64 hex digits (the placeholder `TBD-release`, see the rule below) makes the installer skip the package, and the dashboard launcher then uses the Edge/Chrome `--app=` window. The entry keeps exactly the keys `{version, url, sha256}`; the member list and destination live in `installer/stage2/downloader.py` (`WEBVIEW2_MEMBERS`). To bump: add a throwaway test that downloads the new `.nupkg` in CI and prints `hashlib.sha256(...)` (read it with `gh run view <id> --log-failed`), then edit the three fields and delete the test. |
| `qwentts` | `null` on purpose | Filled from our own release asset `qwentts-win-cuda-x64.zip` of the `v*` release (see "qwentts build"): URL `https://github.com/AbdulrahmanAmer/nightshift/releases/download/<tag>/qwentts-win-cuda-x64.zip` and the sha256 from that release's `.sha256` asset (`gh api repos/AbdulrahmanAmer/nightshift/releases/tags/<tag> --jq '.assets[]|{name,digest}'`). Until then the installer probes `releases/latest/download/qwentts-win-cuda-x64.zip` and falls back to the PyTorch engine (`NS-W301`). |

**The `TBD-release` rule.** A `bins` entry may carry the placeholder `sha256` `"TBD-release"` only when it is marked `"optional": true`. `tools/resolve_manifest.py --check` skips the hash test for such an entry only (for any other entry the placeholder is a reported problem), and `tests/test_resolve_manifest.py` and `tests/test_contracts_ui.py` accept it for no other. Replace the placeholder with the real 64-digit SHA256 as soon as the release job (or a throwaway CI test that prints `hashlib.sha256(...)`) has computed it; required pins (everything else in the table) always need a real hash. `optional` is the only `bins` field beyond `{version, url, sha256}`. `webview2` is pinned with a real hash today, so it does not carry `optional`; if its hash ever has to go back to the placeholder, add `"optional": true` to the entry in the same change (and relax the exact-keys assertion in `tests/test_webview2_pin.py`).
