# Nightshift M0 Installer (C1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take a bare Windows PC to a verified Nightshift install, resumably: `INSTALL.cmd` runs `stage1.ps1` (machine check, tools, uv, Python, `NIGHTSHIFT_HOME`, bootstrap venv) which hands over to a rich-TUI wizard (steps s00 to s10) driven by `install_state.json`, with a resumable, verifying downloader, an unattended mode, and a `--simulate-downloads` mode that lets CI exercise every step. `UNINSTALL.cmd` reverses it. `OPERATOR.md` tells the partner what to do.

**Architecture:** Two stages. Stage 1 is PowerShell (no Python exists yet): preflight table, winget/uv/Python installs, home folder, studio venv, then `python -m installer.stage2.wizard`. Stage 2 is Python: a `Wizard` loop over `Step` objects (`check` / `run` / `verify`), state persisted per step through `ns_common.config`, errors shown as code + message + hint with R/S/L/Q (interactive) or 3 retries with backoff then a diagnostics bundle (unattended). Everything that touches the machine goes through three injectable collaborators: `Runner` (subprocess), `SecretStore` (keyring), and the downloader's `httpx.Client`. `--simulate-downloads` swaps in a scripted `FakeRunner`, an in-memory store and a fixture-file downloader, so the whole wizard runs in CI.

**Tech Stack:** PowerShell 5.1 compatible (ASCII only), Python 3.12, rich (in base deps), httpx, pydantic (via `ns_common.models`), keyring (studio extra, imported lazily on the target only), pytest (`httpx.MockTransport`, a loopback `http.server` thread for Range tests, `subprocess` for `stage1.ps1 -DryRun`).

**Spec:** `BUILD_SPEC.md` A.2 (welcome text, verbatim), 1.2 (machine table), 1.3 (shipping), 5 (installer, all), 13 (D13), 15.1 to 15.2, 15.9, 21 (all), 22 J1 and J8. `docs/ENGINEERING.md` sections 2, 4, 5, 6, 8, 9, 10.

## Global Constraints

- Everything in `docs/ENGINEERING.md` section 5 applies: nothing executes on the authoring laptop; CI on `windows-latest` is the only authority on green; every completion claim carries a run URL.
- Ownership: `INSTALL.cmd`, `UNINSTALL.cmd`, `OPERATOR.md`, `installer/**`, `tests/test_installer*.py`, `tests/test_wizard*.py`, `tests/test_downloader.py`. Nothing in `ns_common/`, `contracts/` (except appending to `contracts/CHANGE_REQUESTS.md`), `pyproject.toml`, `.github/`, or other components.
- PowerShell: every `*.ps1` has `Set-StrictMode -Version Latest` and `$ErrorActionPreference = 'Stop'` right after its `param` block, ASCII only, no `Invoke-Expression`. Execution-policy bypass only in `INSTALL.cmd` / `UNINSTALL.cmd`.
- `-DryRun` prints every action as `[dry-run] <action>`, changes nothing (no files, registry, env vars, installs) and exits 0, even when the preflight would block.
- Python: no `print(` (all console output goes through `installer/stage2/ui.py`); logging through `NsLogger`; errors are `NsError` with codes from `contracts/errors.json`; no heavy import at module level; `keyring` imported only inside `KeyringStore`.
- Tests: network only through `httpx.MockTransport` or a loopback `http.server`; no dependence on the real internet, a GPU, the real home folder or wall-clock sleeps (`sleep` is injected).
- Missing contract pieces are filed in `contracts/CHANGE_REQUESTS.md` and worked around inside `installer/` (Deviations below).
- Worker allocation: all tasks `sonnet` (effort high); the orchestrator reviews.

## Coordinator decisions (2026-11-01)

1. **llama.cpp build pair from `nvidia-smi` CUDA version.** CUDA >= 13.0: the `llama_cuda13` + `cudart13` pair (Blackwell needs it). CUDA 12.4 to 12.x: the `llama_cuda` + `cudart` (12.4) pair, with a warning that 12.8 is needed for Blackwell sm_120. Below 12.4: show the NVIDIA driver page and raise `NS-E103`. The chosen pair (`llama_pair: "cuda13" | "cuda12"`) is written to `config\wizard_flags.json` (`InstallState` and `Runtime` have no field for it; CR-C1-3) and the llama paths in `runtime.json`.
2. **`bins.qwentts` is null.** `s02` HEADs `https://github.com/AbdulrahmanAmer/nightshift/releases/latest/download/qwentts-win-cuda-x64.zip`; on 2xx it downloads and unzips it into `bin\qwentts\`; on 404 or no network it records `NS-W301` as a warning, sets `runtime.tts.engine = "reference"`, and sets the flag `tts_fallback` so `s04` creates the PyTorch `tts` venv and `s03` downloads the Hugging Face safetensors for the reference engine (`Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice`, `-Base`) through the HF tree API.

## Deviations and assumptions

1. `NS-E111`, `NS-E112`, `NS-W112` are not in `contracts/errors.json` at the pinned base. `installer/stage2/errors.py::make_error` returns an `NsError` when the code exists and an `InstallerError` with the same message/hint from a pending table otherwise (CR-C1-1).
2. `Answers` / `answers.schema.json` lack `home_path`, `models_source`, `llm_tier`, `restore_from`. `installer/stage2/answers.py::InstallerAnswers` subclasses `Answers` and adds them (CR-C1-2).
3. The home folder is decided in stage 1 (the venv is bootstrapped before the wizard); `s01_questions` re-confirms it, checks space (60 GB plus tier extras, per spec 5) and disk type, and only asks when no home exists yet (standalone wizard, CI). Preflight hard-blocks below 40 GB (spec 1.2) and warns up to 60 GB.
4. `s06_tune` writes a baseline `runtime.json` before `ns tune` (tune needs model paths); `s07_configs` completes and validates it rather than overwriting tuned flags.
5. `ns` and `studio` both run from the studio venv, installed with `[studio,supervisor]` so `ns_supervisor` imports resolve; the `supervisor` venv gets `[supervisor,tts]`.
6. cuBLAS/cuDNN wheel ranges and the spaCy model wheel version are constants in `s04` (CR-C1-4 asks for manifest pins incl. a SHA256 for the spaCy wheel; OpenCV is not installed in M0, the Clipper is M3).
7. `s05` is a binary check only in M0 (the full OpenCode prewarm needs `opencode.json` and a running `llama`; it lands with the M4 Nightshift Code plan).
8. Launchers (`launchers/*.ps1`) are not in this plan's ownership list; `s09` creates shortcuts only for launcher scripts that exist.

## File structure created by this plan

```
INSTALL.cmd  UNINSTALL.cmd  OPERATOR.md
installer/
  __init__.py  welcome.txt  answers.example.json  stage1.ps1  uninstall.ps1
  stage2/
    __init__.py  __main__.py  wizard.py  ui.py  base.py  context.py  state.py
    errors.py  answers.py  runner.py  simulate.py  secrets.py  winenv.py  sysinfo.py
    downloader.py  runtime_cfg.py  testing.py
    steps/__init__.py  s00_welcome.py ... s10_summary.py
tests/
  test_installer_files.py  test_installer_stage1.py  test_installer_uninstall.py
  test_downloader.py  test_wizard.py  test_wizard_steps.py  test_wizard_e2e.py
```

---

### Task 1: Static files (INSTALL.cmd, UNINSTALL.cmd, welcome.txt, answers.example.json)

**Files:** Create `INSTALL.cmd`, `UNINSTALL.cmd`, `installer/__init__.py`, `installer/welcome.txt`, `installer/answers.example.json`, `tests/test_installer_files.py`.

- [ ] **Step 1: Tests first.** `INSTALL.cmd` contains the exact stage 0 command from spec 5 and `pause`; `UNINSTALL.cmd` points at `installer\uninstall.ps1`; `welcome.txt` equals the blockquote of spec A.2 with `> ` prefixes removed (read from `BUILD_SPEC.md`); every `*.ps1` under `installer/` is ASCII, has StrictMode and `$ErrorActionPreference = 'Stop'` in its first 40 lines and no `Invoke-Expression`; `answers.example.json` validates as `InstallerAnswers` and, for the keys the exported schema knows, against `contracts/schemas/answers.schema.json`; secrets are blank.
- [ ] **Step 2: Write the files.** `welcome.txt` is rendered through `rich.markdown.Markdown` by s00 (the `**bold**` markers of A.2 stay verbatim in the file).
- [ ] **Step 3: Commit** `feat(c1): INSTALL/UNINSTALL launchers, welcome text, answers example`.

### Task 2: `stage1.ps1` with preflight and `-DryRun`

**Files:** Create `installer/stage1.ps1`, `tests/test_installer_stage1.py`.

**Interface:** `stage1.ps1 -AppPath <dir> [-DryRun] [-Answers <path>] [-FakeProbe <json>]`. `-FakeProbe` (only with `-DryRun`) replaces the machine probes with values from a JSON file so CI can test each preflight row. Rows: `os` (NS-E101), `gpu` (NS-E102, VRAM warning), `driver` (NS-E103: fail below CUDA 12.4, warn 12.4 to 12.7), `ram` (profile), `disk` (NS-E104), `disktype` (NS-W106), `network` (NS-E105, five hosts), `winget` (NS-E111, only when a tool must be installed), `longpaths` (NS-W112), `waketimers` (NS-W107). Writes `%TEMP%\nightshift-preflight.json` (schema 1: `checks[]`, `gpu`, `cuda`, `llama_pair`, `ram_gb`, `drives[]`, `home`, `profile`). Dry run prints it as one `[dry-run] preflight-json: {...}` line.

- [ ] **Step 1: Tests first** (skip when no PowerShell): real `-AppPath . -DryRun` exits 0 and every action line starts `[dry-run]`; all-green fake probe has no failures; fake probes for ARM64, no GPU, CUDA 12.2, CUDA 12.4 (warn), CUDA 13.0, low disk, HDD, unreachable host, no winget, long paths off, wake timers off each show the right code and `llama_pair`; default home = largest-free SSD; `-DryRun` leaves the temp dir untouched; `-FakeProbe` without `-DryRun` is rejected.
- [ ] **Step 2: Implement** probes, rows, fix-and-recheck loop (opens the driver / Store page and pauses; skipped when unattended), tools (Git, Windows Terminal, FFmpeg via winget with PATH refresh, `NS-E110`), uv (manifest zip with SHA256, winget fallback), `uv python install 3.12`, home tree and user env var, studio venv with `uv pip install -e "<app>[studio]"`, preflight JSON, hand-over to the wizard with `PYTHONUTF8=1`, exit code propagated.
- [ ] **Step 3: Commit** `feat(c1): stage1.ps1 preflight, tool installs and dry run`.

### Task 3: `uninstall.ps1`

**Files:** Create `installer/uninstall.ps1`, `tests/test_installer_uninstall.py`.

**Interface:** `uninstall.ps1 [-Home <dir>] [-DryRun] [-DeleteModels] [-DeleteData] [-SkipSystem]`. Removes the `Nightshift Daily` task, desktop shortcuts, `venvs\`, `bin\` and the user env var; `models\` only with `-DeleteModels` (asked, default No); `outbox\ rejected\ data\ imports\ cache\` only with `-DeleteData` (asked, default No); prints what was kept. Idempotent. `-SkipSystem` leaves task, shortcuts and env var alone (CI uses it on a temp home).

- [ ] **Step 1: Tests first:** dry run exits 0 and deletes nothing; temp-home run removes `venvs`, `bin`, keeps `models` and `outbox`; second run exits 0; `-DeleteModels` removes models only; `-DeleteData` removes data dirs only.
- [ ] **Step 2: Implement.** **Step 3: Commit** `feat(c1): uninstall.ps1 with dry run and keep-by-default prompts`.

### Task 4: Downloader

**Files:** Create `installer/stage2/downloader.py`, `tests/test_downloader.py`.

**Interface:**
```python
@dataclass(frozen=True)
class DownloadItem: name; url; dest: Path; sha256: str|None; size: int|None; kind: Literal["file","zip"]="file"; extract_to: Path|None=None; expect: tuple[str,...]=(); optional: bool=False
def download(url, dest, sha256, *, resume=True, progress=None, client=None, size=None, retries=3, sleep=time.sleep) -> Path   # NS-E109 / NS-E105
def copy_from_source(models_source, dest, sha256) -> bool
def plan_from_manifest(manifest, profile, tier, home, *, include_code=False, cuda=None) -> list[DownloadItem]
def plan_from_hf_tree(repo, revision, dest_dir, client) -> list[DownloadItem]
def run_plan(items, *, client_factory, concurrency=2, models_source=None, reporter=None, sleep=...) -> PlanResult
```
HEAD for size, `Range` resume into `<file>.part`, a 200 instead of 206 restarts, 416 handled, SHA256 verify then atomic rename, verified marker `<file>.ok` (size + mtime) so reruns skip without rehashing, 3 retries with exponential backoff, `ThreadPoolExecutor(2)`, zip kind extracted with a zip-slip guard and `expect` files verified.

- [ ] **Step 1: Tests first** (`MockTransport` and a loopback `http.server` honouring `Range`): full download, resume from partial `.part`, server ignoring Range, wrong SHA (`NS-E109`, `.part` removed, three attempts), retry then success, unreachable (`NS-E105`), atomic rename, marker skip, `copy_from_source` match / mismatch / missing, concurrency 2, zip extract and zip-slip, optional 404 skipped, `plan_from_manifest` URLs and tiers and cuda pair, `plan_from_hf_tree`.
- [ ] **Step 2: Implement.** **Step 3: Commit** `feat(c1): resumable verified downloader and manifest download plan`.

### Task 5: Wizard framework

**Files:** Create `installer/stage2/{__init__,__main__,wizard,ui,base,context,state,errors,answers,runner,simulate,secrets,winenv,sysinfo,testing}.py`, `tests/test_wizard.py`.

**Interface:** `Step` (`id`, `title`, `optional`, `skip_reason(ctx)`, `check(ctx) -> bool`, `run(ctx)`, `verify(ctx)`), `StepContext`, `Wizard(ctx, steps).run() -> int`, `main(argv)` with `--app-path --answers --home --simulate-downloads --non-interactive --halt-after --preflight --no-head-checks`. Exit codes: 0 ok, 2 user quit, 3 unattended failure (bundle written), 75 halted by `--halt-after`, 130 interrupted.

- [ ] **Step 1: Tests first:** a test iterates every registered step (`check`, `run`, `verify`, unique ordered ids `s00..s10`); state written after each step; done step with true `check()` skipped on resume; kill (BaseException) at step 3 then rerun completes; interactive R retries, S only for optional steps, L prints the log tail, Q exits 2; unattended retries 3 times with injected backoff `[5, 15, 45]`, runs `ns doctor --bundle`, returns 3; warnings never abort; secrets never in logs or `install_state.json`.
- [ ] **Step 2: Implement.** **Step 3: Commit** `feat(c1): wizard framework, error UX, state, runner and secret store`.

### Task 6: Steps s00 to s02

**Files:** Create `installer/stage2/steps/{__init__,s00_welcome,s01_questions,s02_download_bins}.py`, `installer/stage2/runtime_cfg.py`, `tests/test_wizard_steps.py` (first part).

- [ ] **Step 1: Tests first:** s00 reads a preflight JSON, shows the table, raises the row's code on a red row; s01 answers from a file, asks the missing ones from a scripted `FakeUi`, default home = largest-free SSD, space (`NS-E104`) and HDD warning (`NS-W106`), key validation via `MockTransport` (Pexels 200 / 401, Pixabay 200 / 400), blanked `config/answers.json`, key never logged, keys in the `MemoryStore`, voice preview plays the `FakeTts` sample in simulate mode; s02 chooses the cuda13 / cuda12 pair, unzips and verifies `llama-server.exe`, OpenCode only with `install_code`, qwentts probe (200 downloads; 404 sets reference engine, `tts_fallback` flag and `NS-W301`).
- [ ] **Step 2: Implement.** **Step 3: Commit** `feat(c1): wizard steps s00 welcome, s01 questions, s02 binaries`.

### Task 7: Steps s03 and s04

- [ ] **Step 1: Tests first:** s03 prints total and ETA, honours `models_source`, simulate mode writes tiny fixtures after HEAD checks, tier selection, adds the HF safetensors plan when `tts_fallback`, rerun is a no-op; s04 issues the exact `uv venv` / `uv pip install` commands, no `tts` venv unless flagged, the flagged fallback installs the manifest `tts_packages` with their index URLs.
- [ ] **Step 2: Implement.** **Step 3: Commit** `feat(c1): wizard steps s03 models and s04 venvs`.

### Task 8: Steps s05 to s07

- [ ] **Step 1: Tests first:** s05 skips unless `install_code`; s06 writes the baseline `runtime.json` then calls `ns tune`, failing tune keeps defaults with a warning; s07 validates `runtime.json`, writes `opencode.json` (spec 15.10), the channel yaml via `save_channel`, ensures keyring holds both keys and blanks them in every `answers.json`; rerun is a no-op.
- [ ] **Step 2: Implement.** **Step 3: Commit** `feat(c1): wizard steps s05 prewarm, s06 tune, s07 configs`.

### Task 9: Steps s08 to s10

- [ ] **Step 1: Tests first:** s08 runs `ns verify --quick` and applies fallbacks (NVENC to `libx264` `NS-W401`; TTS to reference engine with the fallback flag `NS-W301`; MTP off `NS-W302`; whisper failure `NS-E112`); s09 creates shortcut files for existing launchers (WScript.Shell via encoded PowerShell) and calls `ns schedule install --time`, "No such command" is a warning; s10 prints the status table, runs `ns doctor --bundle`, names the Desktop zip to upload, stamps `completed_at`.
- [ ] **Step 2: Implement.** **Step 3: Commit** `feat(c1): wizard steps s08 smoke, s09 shortcuts and task, s10 summary`.

### Task 10: End to end in CI, OPERATOR.md, change requests

**Files:** Create `tests/test_wizard_e2e.py`, `OPERATOR.md`; append to `contracts/CHANGE_REQUESTS.md`.

- [ ] **Step 1: Tests:** full wizard with `--simulate-downloads --non-interactive --answers` in a temp home; `--halt-after s03_download_models` then a second run finishes; third run is a no-op; the module run as a subprocess exits 0 and `install_state.json` has every step `done` and validates against `install_state.schema.json`.
- [ ] **Step 2: Write `OPERATOR.md`** (numbered, plain English; J1, J2, J8; each red row; diagnostics zip; how to send it back).
- [ ] **Step 3: File CRs; commit** `docs(c1): OPERATOR.md and change requests`.
- [ ] **Step 4: Open the PR** when CI is green: `gh pr create --fill`.
