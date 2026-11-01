# Nightshift — Build Spec v2 (final, for Claude Code, sliced build)

> **Nightshift** is a Windows one-click bundle that turns one RTX 5060 Ti (16 GB) PC into an **overnight YouTube story studio** that writes an original story, makes a **long-form video first, then cuts it into Shorts**, so one story earns on both formats. It also ships an optional **private local coding assistant** on the same model.
> The target device needs **no Claude account, no paid AI keys, and no cloud CLI**. Claude Code is only the *builder*.
> The build runs in **slices** (§12). Each slice is proven on the target PC before the next one starts. The main session owns every shared contract (§15). Worker agents build their packages in parallel inside a slice and never edit shared files.

**Changes from v1** (summary; details inline): **no-hardware build**: a `cpu-dev` profile runs the whole pipeline on the build laptop with tiny models, the installer is distributed through Google Drive and run by a remote operator with no developer present, and `ns doctor --bundle` ships diagnostics back · sliced build order replaces the one-shot parallel build · one LLM by default, abliterated model is opt-in · word budgets derived from runtime · Shorts selection by sentence span, timed after captions · combo-ledger window bounded by pool size · embedding + NER tools added to the stack · clip budget and relaxed reuse rules · long-form SEO rules split from Shorts · `story` schema and per-beat visual plan defined · QC targets set · captions aligned to the script, not ASR text · TTS sentence chunking · 16 GB RAM profile constrained · scheduler wake-to-run · calibration harness · pause/incident procedure · go/no-go milestone · two-machine dev workflow.

---

## A. Product description (also used verbatim as installer and README text)

### A.1 One line
**Nightshift works while you sleep: it writes an original story, turns it into a full long-form YouTube video, then cuts the best moments into Shorts that send viewers back to it. Everything runs on your own PC.**

### A.2 Installer welcome screen (exact text shown by `INSTALL.cmd`)
> **Welcome to Nightshift**
>
> Nightshift sets up a story-to-video factory on this PC. It writes an original story, narrates it, adds captions and matching stock footage, and produces **one long-form video (10+ minutes, with chapters and ad-break points)** plus **several vertical Shorts cut from it**, each with its own hook. Everything lands in your Outbox, ready for you to review and post.
>
> Optionally, it also installs **Nightshift Code**, an AI coding assistant in your terminal, powered by the same model running on your graphics card. No account, no subscription, nothing leaves your PC.
>
> **What setup will do:** check your PC (graphics card, driver, memory, disk space), install the free tools Nightshift needs, download the AI models (~15 GB, or copy them from a folder you already have), tune them for your hardware, and run a quick test of everything. You'll answer a few questions along the way. You can close and re-run setup at any time; it picks up where it left off.
>
> **You'll need:** an NVIDIA RTX GPU with 16 GB, ~40 GB of free disk space, an internet connection during setup (or a folder with the model files), and two free stock-footage API keys (Pexels and Pixabay). Setup shows you where to get them.

### A.3 How it installs itself
1. **Checks the PC**: Windows version, NVIDIA GPU, driver, RAM, free disk, internet, power settings. It tells you exactly what to fix if something is missing.
2. **Installs free tools**: Git, Windows Terminal, FFmpeg, Python (via uv). Everything it already finds is skipped.
3. **Asks you a few questions**: first channel name and niche, language, narrator voice (listen to samples), stock-footage keys, how many videos per week, what time to run, whether to install Nightshift Code.
4. **Downloads the AI engine and models** from their official sources, verifying every file.
5. **Tunes itself** to your exact hardware for maximum speed.
6. **Tests everything** and shows a pass/fail checklist.
7. **Creates desktop shortcuts** and a nightly scheduled task that can wake the PC.

### A.4 What it does after install
- **Nightshift Studio** (runs nightly, or on demand): for each channel it
  1. invents an original story idea in your niche and avoids anything it has made before
  2. plans the story in chapters, then writes, critiques and rewrites it until it passes a quality bar
  3. narrates it in the channel's voice and adds captions
  4. finds matching stock footage and edits a **long-form 16:9 video** (10–14 min), with chapters and suggested mid-roll ad points
  5. makes a thumbnail and the long-form title, description and chapter timestamps
  6. picks the strongest **self-contained moments** and turns each into a **vertical Short** with a freshly written and voiced hook, its own vertical edit, and an ending that points to the full story
  7. writes titles, descriptions, tags and hashtags for every Short
  8. checks quality and YouTube-policy safety on everything
  9. drops the long-form video, its Shorts, and all upload details into the Outbox
- **Nightshift Clipper** (in the Dashboard): drop in any long video you own (a stream VOD, a podcast episode, a talking-head recording). Nightshift transcribes it, finds the moments that stand on their own, and cuts each into a vertical clip with captions, using a layout template that fits the video: facecam on top of gameplay, stacked or single-speaker framing for podcasts, tracked crop for talking heads. Every clip gets a title, description and tags and lands in the Outbox like everything else.
- **Nightshift Dashboard** (shortcut, opens in your browser): channels, the review queue with video preview and approve/reject, tonight's run and its log, the Clipper drop zone and templates, analytics import, pause/resume, and settings.
- **You** review each video (`approve` / `reject`) in the Dashboard and upload approved ones in YouTube Studio.
- Over time, you can feed it your YouTube Analytics export so it learns which hooks, styles and lengths work best for each channel.
- **Nightshift Code** (optional shortcut): opens a terminal coding assistant (OpenCode) on the local model. It reads, writes and edits code in your projects folder, fully offline.

### A.5 What it takes in
| Input | When | Required |
|---|---|---|
| Channel name, niche, language | Setup / `studio channel add` | Yes |
| Narrator voice: preset, described in words, or cloned from a 3–10 s clip **you have rights to** | Setup / anytime | Yes |
| Pexels + Pixabay API keys (free) | Setup | Yes |
| Masters per week, run time, publish window | Setup | Defaults provided |
| Optional: seed keywords, topics to avoid, style notes | Channel profile | No |
| Optional: your own example stories | `studio/exemplars/` | No |
| Optional: YouTube Analytics CSV export | Dashboard → Learn, or `studio learn --import` | No |
| Clipper: a video file you own (MP4/MKV/MOV, up to 4 h), a template choice, a target channel | Dashboard → Clipper (drag and drop), or `studio clip <file>` | For clips |
| Coding: your project files and instructions | Nightshift Code | — |

### A.6 What it puts out
| Output | Where |
|---|---|
| **Long-form master**: 1920×1080 MP4, 10–14 min, narration + captions + stock footage, **no music**, chapters | `Outbox\<channel>\<date>\<job>\long\` |
| **Thumbnails**: 3 variants (1280×720) for the long-form | same folder |
| **Shorts**: 4–6 vertical 1080×1920 MP4s cut from the master, each with its own voiced hook and vertical edit | `Outbox\<channel>\<date>\<job>\shorts\` |
| Upload sheet per video (`.json` + copy-ready `.txt`): title, description, tags, hashtags, chapters (long), suggested publish time, suggested mid-roll points (long), "Related video" instruction (Shorts), AI-disclosure answer, "made for kids" answer | next to each MP4 |
| **Clips** from your own videos: 3–8 vertical 1080×1920 MP4s per source video, captions burned in, template layout, each with an upload sheet | `Outbox\<channel>\<date>\clip-<id>\` |
| Daily run report: what was made, what was rejected and why, timings | Dashboard → Runs, `logs\`, `studio status` |
| Coding: code changes in your projects | your Projects folder |

---

## 0. Ground rules for the builder

1. **Never bundle models or large binaries.** The installer downloads everything from pinned sources.
2. **Pin everything** in `manifest.json` (versions, URLs, HF repo + filename, SHA256). The main session resolves and verifies them in Slice 0.
3. **Contracts first, per slice.** Every shared name, path, port, schema, API and CLI command is defined in §15. A slice's contracts are frozen before its workers start. Later slices may **add** contracts; they may change earlier ones only through `contracts/CHANGE_REQUESTS.md`.
4. **Exclusive ownership.** Each work package owns only its directories (§4). Only the main session edits `contracts/` and `ns_common/`.
5. **Verify, don't assume.** Every install step checks before it runs and verifies after.
6. **Idempotent and resumable.** Every stage can be re-run; a job resumes from its last completed stage.
7. **Everything visible.** The wizard runs in a visible terminal and logs per §15.10.
8. **One GPU tenant at a time** (`llama`, `tts` or `whisper`), enforced by the supervisor. CPU services (`embed`) are exempt.
9. **No hidden network calls at runtime** except the stock-footage APIs (and the optional YouTube API, §10).
10. **YouTube safety is a build requirement, not a feature.** §18 rules are enforced in code by the policy gate.
11. **Real-hardware gates.** No slice is "done" until its gate (§12) passes on the target PC. Mocked tests prove plumbing, not quality.
12. **Honest reporting.** Benchmarks, calibration scores and gate results go into `logs\` as JSON and are quoted, not paraphrased.

---

## 1. Machines

### 1.1 Build machine (where Claude Code runs)
The developer laptop (no NVIDIA GPU, 8 GB RAM, Windows 11). **Nothing heavy is installed here**: no models, no llama.cpp, no PyTorch, no whisper. The only additions are a lightweight dev venv inside the repo (`.venv\`, pytest + pydantic + httpx + jinja2 + Pillow + pyyaml, ~200 MB) and the FFmpeg that is already present. **The target PC is never available to the developer during the build.**

Therefore every heavy dependency sits behind an interface with a **fake** that produces real files:
| Component | Interface | Fake used on the build machine |
|---|---|---|
| LLM | `LlmClient.complete_json/complete_text` | `FakeLlm`: returns fixture JSON per prompt name (`tests/fixtures/llm/<prompt>.json`), validated against the same schemas |
| TTS | `TtsClient.speak/speak_batch` | `FakeTts`: writes a 24 kHz WAV of `words/wpm` seconds containing a tone burst per word |
| Whisper | `Transcriber.words(wav) -> list[Word]` | `FakeTranscriber`: evenly spaces the script's words over the WAV duration |
| Stock clips | `ClipSource.search/download` | `FakeClipSource`: generates `testsrc2`/`color` clips with FFmpeg, tagged with the keywords |
| Embeddings | `EmbedClient.embed` | `FakeEmbed`: hashed bag-of-words vectors (deterministic, cosine-meaningful) |
| GPU lock | `Acquirer.acquire/release` | `DevAcquirer`: fixed endpoints, no locking |
| Encoder | `runtime.video.encoder` | `libx264 -preset ultrafast` |

`studio run --fake-services` wires all fakes at once. The output is a **real MP4 with real captions and a valid upload sheet**, produced by the real stages, orchestrator, validators, QC and outbox code. That run is the local gate for every slice (§12). Story quality is never judged on the build machine.

**What cannot be tested before shipping** (and is therefore wrapped in the strongest install-time checks and clearest error messages): CUDA 12.8 PyTorch on Blackwell, `faster-qwen3-tts` CUDA graphs, NVENC, llama.cpp CUDA offload and MTP speed, `ns tune` results, real model output quality. Each has an explicit verify step and a documented fallback: TTS → reference engine; NVENC → `libx264`; MTP → plain decoding; tune failure → safe default flags.

### 1.2 Target machine (detected, not assumed)
| Item | Expectation | Wizard behaviour |
|---|---|---|
| OS | Windows 10/11 x64 | Block on ARM64 or 32-bit (`NS-E101`) |
| GPU | RTX 5060 Ti 16 GB (Blackwell, **sm_120**) | `nvidia-smi`; block if no NVIDIA GPU (`NS-E102`); warn if VRAM < 16 GB |
| Driver | `nvidia-smi` reports CUDA ≥ 12.8 | Open the NVIDIA driver page, pause, re-check (`NS-E103`) |
| RAM | 32 GB recommended, 16 GB minimum | Selects the hardware profile (§3) |
| Disk | ≥ 40 GB free for the default install (15 GB models + venvs + working space); +15/+22 GB for Balanced/Best LLM; +20 GB two-model mode | Block if too small (`NS-E104`) |
| Disk type | NVMe/SSD for `NIGHTSHIFT_HOME` | Warn on HDD (`NS-W106`): load times will miss §14.6 |
| Network | HF, GitHub, PyTorch, Pexels, Pixabay reachable | `NS-E105` |
| Power | Scheduled task can wake the PC; sleep allowed, hibernate discouraged | Set task `WakeToRun`; warn if "wake timers" disabled (`NS-W107`) |
| winget | `winget --version` works (App Installer present) | If missing: open the Microsoft Store App Installer page and the direct installer links from §21.1, pause, re-check (`NS-E111`) |
| Long paths | Registry `LongPathsEnabled` = 1 (model file names and job paths can exceed 260 chars) | Set it (needs admin) or warn and keep paths short (`NS-W112`) |

**Blackwell:** PyTorch must be a **CUDA 12.8+ build (2.7+)**, installed from the cu128 (or newer) index URL.

### 1.3 Shipping and the first run (decision D1, §13)
- **The bundle is the repository.** `dev\make_bundle.ps1` produces `Nightshift-<version>.zip`: the full codebase (code, tests, contracts, prompts, presets, docs, plans, `CLAUDE.md`, `uv.lock`), `INSTALL.cmd` at the root, `answers.example.json`, and `OPERATOR.md`. No models, no binaries, no venvs (§0 rule 1). It is uploaded to a **Google Drive folder**.
- **The partner** extracts the zip to a folder of their choice (recommended `D:\NightShift`) and double-clicks `INSTALL.cmd`. That runs `installer\stage1.ps1` with execution policy bypassed for that process only, which installs tools, creates `NIGHTSHIFT_HOME` (`%LOCALAPPDATA%\Nightshift`) for models, venvs, config and outputs, records the repo path as `app_path` in `install_state.json`, and runs the wizard. Every model and tool is downloaded from pinned sources on that machine.
- Setup needs **zero decisions** when `answers.json` is present next to `INSTALL.cmd`; questions are asked only for missing answers.
- At the end of setup, and on demand, **`ns doctor --bundle`** writes `%USERPROFILE%\Desktop\nightshift-diagnostics-<date>.zip` (logs, `install_state.json`, `runtime.json`, `verify-report.json`, `nvidia-smi` output, `pip freeze` per venv, the last 3 job directories without media) for upload to Drive if the developer is not at the machine.
- **The codebase stays on the work machine as the development checkout.** After the first run, further work (hardware gates, tuning, prompt iteration, Slices 2+ if they were not finished) continues there with Claude Code against the real GPU. `ns update <zip>` exists for the case where fixes are shipped from elsewhere: it replaces the repo contents (keeping `.git`, `config`, and untracked data), re-syncs venvs from `uv.lock`, and re-runs `ns verify --quick`.
- Secrets (Pexels/Pixabay keys) may be placed in `answers.json`; the wizard moves them into the Windows keyring and blanks them in the file.

---

## 2. Final stack

| Layer | Choice | Notes |
|---|---|---|
| LLM runtime | **llama.cpp `llama-server`**, official Windows CUDA release + cudart, build with merged MTP support (PR #22673 or later) | MoE offload, MTP, OpenAI-compatible API, `--alias` |
| LLM (`code` and `story`, **one file**) | **14B-class dense Qwen GGUF, Q4_K_M, ~9 GB**: `Qwen/Qwen3-14B-GGUF` Q4_K_M by default; Slice 0 Task 14 swaps in a Qwen3.6 14B-class GGUF if one exists. Fits fully in 16 GB VRAM with 16K context. Installer offers **Balanced** (`mudler/Qwen3.6-35B-A3B-APEX-MTP-GGUF` I-Mini, ~14 GB) and **Best** (`unsloth/Qwen3.6-35B-A3B-MTP-GGUF` UD-Q4_K_XL, ~21 GB) as opt-in upgrades | Decision D13: smallest download that keeps story quality acceptable |
| Story model, two-model mode (opt-in) | `Youssofal/Qwen3.6-35B-A3B-Abliterated-Heretic-GGUF` Q4_K_M (MTP build if available) | Enabled only if calibration (§16.5) shows refusals on fiction; set `models.story` in `manifest.json` |
| Embeddings (`embed`) | `nomic-ai/nomic-embed-text-v1.5-GGUF` Q8_0 (~150 MB) served by a second `llama-server --embeddings` on **CPU** | For self-similarity and opening-line checks; not a GPU tenant |
| NER | **spaCy** `en_core_web_md` in the studio venv + curated lists in `contracts/policy/` | `real_entities` check |
| Coding CLI (optional) | **OpenCode**, pinned Windows binary, local-only lock (§15.9) | Not `npm -g` |
| TTS | **Qwen3-TTS-12Hz-1.7B as GGUF Q8_0** run by **`qwentts.cpp`** (C++/GGML port, CUDA build, no PyTorch): talkers `CustomVoice` (presets) and `Base` (custom voices from a stored speaker embedding) + the shared codec tokenizer GGUF, from `Serveurperso/Qwen3-TTS-GGUF`. `VoiceDesign` Q8_0 is **downloaded on demand** the first time a user designs a voice, then the result is stored as an embedding and VoiceDesign is no longer needed. `ns_tts` wraps the `qwentts` binary. | Fallback if no prebuilt Windows CUDA `qwentts.cpp` binary passes the smoke test: reference `qwen-tts` (PyTorch cu128) with the original safetensors, downloaded only in that case (`NS-W301`) |
| Captions + transcription | **faster-whisper** `distil-large-v3` (CTranslate2 int8, ~1.5 GB; `tiny` on `cpu-dev`), one model for both narration alignment (word timestamps → **aligned to the script** → ASS) and Clipper transcription (segments + words, VAD, speaker-change heuristic) | Rendered caption words are the script's, never ASR output |
| Face / facecam detection (Clipper) | **OpenCV** (`opencv-python-headless`) with the **YuNet** face detector ONNX (~230 KB, pinned in manifest) + edge/rectangle scan for a facecam overlay | No mediapipe, no torch |
| Dashboard | **FastAPI** + static single-page UI (vanilla HTML/CSS/JS, no build step) on **127.0.0.1:8100** | §20; opened by `Nightshift Dashboard.lnk` |
| Stock footage | **Pexels** + **Pixabay** APIs | Commercial use with no required attribution; no reselling unmodified clips; Pixabay requires 24 h caching |
| Render | **FFmpeg**; encoder `h264_nvenc` on GPU profiles, `libx264` on `cpu-dev` or when NVENC fails verify; master 1920×1080, Shorts 1080×1920; 30 fps; AAC 192 kbps 48 kHz | One filter graph per video |
| Thumbnails | **Pillow** + bundled OFL font (`Anton`) | Font licence file shipped in `presets/fonts/` |
| Audio measure | FFmpeg `ebur128` / `loudnorm` | QC loudness |
| Python | **uv**, Python 3.12, venv per component | |
| Data | SQLite `data\studio.db` | WAL mode; nightly backup |
| Installer | `INSTALL.cmd` → `installer\stage1.ps1` → `wizard.py` | No compiled installer, no code signing, no SmartScreen prompt; Windows may show a "Mark of the Web" warning on the zip, documented in `OPERATOR.md` |

Storage (default install): LLM 9 GB + TTS 4.4 GB (two Q8_0 talkers + tokenizer) + whisper 1.5 GB + embed/face/spaCy 0.2 GB ≈ **15 GB of models**, venvs ~1.5 GB (no PyTorch). On-demand extras: VoiceDesign +2.1 GB, Balanced LLM +14 GB, Best LLM +21 GB, two-model story LLM +20 GB.

---

## 3. Hardware profiles

| Profile | LLM file | Context | Strategy |
|---|---|---|---|
| **`gpu`** (default; any RTX 16 GB, ≥ 16 GB RAM) | 14B Q4_K_M (~9 GB) | 16K | Fully on GPU, KV f16; one GPU tenant at a time (§0 rule 8) |
| **`gpu`** with `llm_tier: balanced|best` (≥ 32 GB RAM) | 35B-A3B MoE I-Mini (~14 GB) / UD-Q4_K_XL (~21 GB) | 16K / 32K | `--fit` on; tune `--n-cpu-moe`; KV q8_0 when needed; MTP if faster |
| **`cpu-dev`** (any machine, no GPU needed) | `Qwen/Qwen3-1.7B-GGUF` Q4_K_M (~1.1 GB), llama.cpp **CPU** build | 8K | TTS: `qwentts.cpp` CPU build with the **0.6B CustomVoice Q8_0** GGUF (~1 GB); whisper `tiny`; FFmpeg `libx264 -preset ultrafast`; embed model unchanged. Selected by `NIGHTSHIFT_PROFILE=cpu-dev` or `--allow-cpu`. **Not used during the build** (nothing is downloaded on the build laptop, §1.1); it exists so tests and prompt work on the work machine can run without taking the GPU from a nightly job. Never offered by the installer to end users. |

`ns tune` sweeps ctx (8K/16K/32K), KV (f16, q8_0), `--n-gpu-layers` (99, then step down only if load fails) and, for MoE files, `--n-cpu-moe` (0, 4, 8, 12, 16) and MTP `--spec-draft-n-max` (2, 3, 4), measuring tok/s on a fixed 400-token prompt and 300-token generation, then writes the winner to `config\runtime.json`. In two-model mode it runs per model.

---

## 4. Repo layout (exact — owned by the main session)

```
nightshift/
  manifest.json
  pyproject.toml              # main session: workspace, console scripts ns, studio
  README.md                   # main session: from §A
  contracts/                  # main session only
    CONTRACTS.md  CHANGE_REQUESTS.md  schema.sql  errors.json
    schemas/*.schema.json
    policy/                   # §18 lists
      banned_phrases.txt  misleading_claims.txt  unsafe_topics.yaml
      real_entities_block.txt  real_entities_allow.txt
  ns_common/                  # main session only
    paths.py config.py logging.py errors.py db.py llm_client.py tts_client.py
    embed_client.py supervisor_client.py models.py textstats.py
  dev/                        # main session: make_bundle.ps1, run_services_dev.ps1 (hand-start llama/tts/embed on a GPU machine), gate scripts
  tools/                      # main session: resolve_manifest.py
  ns_studio/                  # package ns_studio (directory name == import name)
    text/ prompts/ exemplars/ # WP-D
    media/ presets/           # WP-E
    policy/ learn/            # WP-H
    variation/ channels/ cli.py orchestrator.py stages.py   # WP-F
    calibrate/                # WP-D: studio calibrate
    clipper/                  # WP-I: transcribe, select, detect (faces/facecam), templates, cut, stages_clip.py
    presets/clip_templates.yaml
  ns_tts/                     # WP-C: TTS FastAPI service
  ns_dashboard/               # WP-J: FastAPI API + static/ (index.html, app.js, app.css), jobs queue, uploads
  ns_supervisor/              # WP-B: supervisor, tune.py, verify.py, doctor.py, gate.py (ns CLI)
  INSTALL.cmd                 # WP-A: double-click bootstrap
  OPERATOR.md                 # WP-A: one page for the person running the install
  CLAUDE.md                   # main session: how to develop in this repo (tests, fakes, contracts rules, gates)
  installer/                  # WP-A
    stage1.ps1 welcome.txt (A.2) answers.example.json
    stage2/wizard.py stage2/steps/*.py
  launchers/                  # WP-A
    nightshift-code.ps1 nightshift-studio.ps1 open-outbox.ps1 stop-all.ps1
  opencode/                   # WP-B: opencode.json AGENTS.md
  tests/                      # WP-G: unit + mocked e2e; fixtures/ holds tiny synthetic media
  docs/superpowers/plans/     # implementation plans, one per slice
```

Single `pyproject.toml` (setuptools, Python 3.12) with optional-dependency groups `studio`, `tts`, `supervisor`, `dev`. Development uses one venv with all groups; the installer creates the per-component venvs from the same file (`uv pip install -e ".[tts]"` etc.).

Work packages: **A** installer+launchers · **B** supervisor+tune+opencode · **C** TTS server · **D** text+prompts+exemplars+calibrate · **E** media+presets · **F** studio core (variation, channels, orchestrator, CLI) · **G** tests · **H** policy+learn · **I** clipper · **J** dashboard. Which packages are active in each slice: §12.

---

## 5. Installer / wizard (WP-A, Slice 5)

**Stage 0 — `INSTALL.cmd`:** `powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\stage1.ps1" -AppPath "%~dp0."` and pauses on exit so the window stays readable.

**Stage 1 — `stage1.ps1`:** preflight (§1.2) → winget installs (Git, Windows Terminal, FFmpeg) → uv → Python 3.12 → create `NIGHTSHIFT_HOME` tree → bootstrap venv `venvs\studio` with `uv sync --locked --extra studio` from the repo → run `wizard.py [--answers <path>]`.

**Unattended mode:** the installer looks for `answers.json` next to `INSTALL.cmd`, then in `%NIGHTSHIFT_HOME%\config\`. Every question in `s01_questions` is answered from it when present; missing keys are asked interactively. Schema: `contracts/schemas/answers.schema.json` (`home_path?, models_source?, llm_tier:"small"|"balanced"|"best"="small", channel{display_name,niche,language,seed_keywords[]}, voice{mode,speaker|description|ref_audio_path+ref_text+rights_confirmed}, pexels_api_key?, pixabay_api_key?, masters_per_week, run_time, install_code:bool, allow_cpu:bool`). A template `installer/answers.example.json` ships in the Drive folder.

**Stage 2 — `wizard.py`** (rich TUI; each step = `check()` / `run()` / `verify()`; every step writes its result to `install_state.json` so a crash or reboot resumes at the failed step):
| Step | Does |
|---|---|
| `s00_welcome` | Shows A.2 text + the hardware check summary (incl. disk type, wake timers) |
| `s01_questions` | **Home folder** for models, venvs, config and videos (default: `<drive with most free space, SSD preferred>\Nightshift`, must pass the 60 GB check; written to the user environment variable `NIGHTSHIFT_HOME`, so every later shortcut, task and shell sees it) · Channel (name, niche, language) · voice (preview 4 presets / design / clone, with a rights confirmation for clones) · Pexels + Pixabay keys (validated live, with links to get them, §21.6) · masters/week (default 1 for a new channel, §18.6) · run time (default 03:00) · install Nightshift Code? (default yes) |
| `s02_download_bins` | llama.cpp + cudart, OpenCode (if chosen) |
| `s03_download_models` | LLM GGUF, embed GGUF, TTS GGUFs (CustomVoice, Base, tokenizer), faster-whisper `distil-large-v3`, YuNet, spaCy wheel (resumable, SHA256). **Local source mode:** if `answers.models_source` (or the wizard's "I have the model files" option) points at a folder, every file whose name and SHA256 match is copied from there instead of downloaded; the rest is downloaded. |
| `s04_venvs` | `studio` (incl. spaCy, faster-whisper + cuBLAS/cuDNN wheels, OpenCV), `supervisor` (+ dashboard). No `tts` venv unless the PyTorch fallback is triggered (§2). |
| `s05_opencode_prewarm` | If chosen: run OpenCode once headless; confirm `local/code` |
| `s06_tune` | `ns tune` |
| `s07_configs` | runtime, OpenCode, channel profile, secrets (keyring) |
| `s08_smoke` | `ns verify --quick` (§12 Slice 5 gate list) incl. TTS engine selection |
| `s09_shortcuts_scheduler` | §15.8; task has `WakeToRun`, `StartWhenAvailable`, runs whether user is logged on or not |
| `s10_summary` | Status table, where things live, next steps (A.4); runs `ns doctor --bundle` and tells the operator exactly which file to upload to the Drive folder |

**Error UX:** code + plain message + fix hint from `errors.json` → `[R]etry · [S]kip (optional only) · [L]og · [Q]uit`. In unattended mode, a required step retries 3 times with backoff, then writes the bundle and exits non-zero with the code on screen. Resume state in `config\install_state.json`.

**Install-time verification of the untestable parts (§1.1):** `s08_smoke` tests `h264_nvenc` with a 2-second colour source and falls back to `libx264` in `runtime.json` (`NS-W401`) on failure; tests `qwentts.cpp` on CUDA with one sentence per installed talker and, on failure, creates the `tts` venv with PyTorch cu128 + `qwen-tts` (checking `torch.cuda.is_available()` and capability ≥ (12,0), else `NS-E108` with the exact cu128 reinstall command), downloads the original safetensors and switches `runtime.tts.engine` to `reference` (`NS-W301`); tests faster-whisper on CUDA (`NS-E112` with the cuBLAS/cuDNN fix hint on failure); for MoE LLM tiers tests `llama-server` with and without `--spec-type draft-mtp` and keeps MTP only if it is faster (`NS-W302` otherwise).

**`ns update <zip>`:** verifies the zip's `VERSION` and SHA256 sidecar, extracts to `<app_path>.new\`, copies `.git\`, `config\` and untracked data over, runs `uv sync --locked` per venv, swaps directories (keeping `<app_path>.prev\` for `ns update --rollback`), re-runs `ns verify --quick`, and re-registers shortcuts and the task. When the repo has a git remote, `git pull` is the simpler path and `ns update` says so.

---

## 6. Runtime (WP-B, Slice 4)

- **Supervisor** (§15.3) handles start/stop, health checks, crash restart (max 3 in 10 min, then `error`), the **GPU lock**, and idle unload after 10 min. `embed` is CPU-only, started on demand, never locked.
- **Shortcuts** run launchers that acquire the needed service, wait for health, then open (§15.8).
- **Scheduled task** runs `studio run --daily`. If the GPU is held by `opencode`, it waits up to 2 h, then logs `NS-E201` and retries next cycle.
- **Before the supervisor exists** (M1 development on a GPU machine) `dev\run_services_dev.ps1` starts `llama`, `embed` and `tts` in visible windows with the tuned flags. `ns_common` clients talk to fixed ports either way.

### 6.1 Process model (how models are loaded and unloaded)
- **Service table** from `runtime.json`: name → command line, port, health URL, GPU flag.
  - `llama`: `bin\llama\llama-server.exe -m <llm.gguf> --port 8080 --host 127.0.0.1 -ngl <n> -c <ctx> --alias code,story [-ctk/-ctv q8_0] [--n-cpu-moe N] [--spec-type draft-mtp --spec-draft-n-max K -np 1]`
  - `tts`: `bin\qwentts\qwentts.exe` with the talker + tokenizer GGUFs in server mode on 8091; if the binary has no server mode, `ns_tts` (in the supervisor venv) is the server and spawns `qwentts` per batch.
  - `embed`: `llama-server.exe -m <embed.gguf> --embeddings --port 8081 -ngl 0` (CPU).
  - `whisper`: no process; the studio worker loads `WhisperModel(..., device="cuda")` under the GPU lock and drops it on release.
- **`/acquire`**: FIFO `asyncio.Lock` for the GPU with timeout → if another GPU tenant is resident, terminate it first → `subprocess.Popen` with stdout/stderr to `logs\<service>-YYYYMMDD.jsonl` → poll `/health` every 500 ms until 200 or 120 s (`NS-E203`) → record pid, port, VRAM (`nvidia-smi --query-compute-apps`) → return endpoint.
- **`/release`**: lock released; idle timer armed; after 10 min without an owner the process gets `terminate()`, then `kill()` after 10 s.
- **Crash policy**: child exit → restarted on next acquire; 3 crashes in 10 min → state `error`, jobs fail `NS-E202`.
- **Guaranteed cleanup**: the supervisor creates a **Windows Job Object** with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and assigns every child to it, so if the supervisor exits for any reason (quit, crash, log-off, shutdown) all model processes die with it. No orphaned VRAM.

### 6.2 App lifecycle (open / warm-up / close)
| Event | What happens |
|---|---|
| **Open** (`Nightshift Dashboard.lnk`) | Launcher starts the supervisor if not running, as a minimized console window titled **"Nightshift Engine"**, waits for `/status`, opens `http://127.0.0.1:8100`. Nothing is loaded yet; opening is instant. |
| **Warm-up** | Dashboard status strip: `Engine starting` → `Services idle` → `Loading LLM (9 GB)…` with elapsed time from the acquire's health polling → `Ready`. Triggered by the first job or the **Warm up** button. |
| **Close** | Closing the browser tab stops nothing (it is only a page; the Engine window stays). **Quit Nightshift** (dashboard button), `Nightshift - Stop All.lnk`, or closing the Engine window → `POST /shutdown`: current stage finishes or is cancelled within 30 s, children terminated, VRAM freed, supervisor exits. |
| **Auto-exit** | Supervisor exits after 30 idle minutes unless a scheduled run is due within 60 min. |
| **Nightly** | The scheduled task starts the supervisor headless (no window, no browser), runs `studio run --daily`, and the supervisor exits when the run ends. If the dashboard is opened during a run, it attaches to the running supervisor and shows progress. |
| **OS shutdown / log-off** | Job Object kills children; a stage interrupted mid-way resumes from `stage_state.json` on the next run. |

Supervisor API additions (§15.3): `POST /shutdown {grace_s:30}` · `POST /warmup {service}` · `GET /events` (server-sent events: service state changes, job progress) used by the dashboard status strip.

---

## 7. Studio pipeline — "master-first" (per channel, per job)

One job = **one story → one long-form master → 4–6 derived Shorts.** Write and voice the story once; render it twice (16:9 and 9:16).

**Why master-first:** mid-roll ads need videos of **8 minutes or longer**. Long-form builds the **watch hours** that Shorts can't count toward. Shorts bring discovery and feed viewers to the master through YouTube's **"Related video"** link.

### 7.0 Word and duration budgets (hard rules, in code)
- `wpm` comes from the pacing option (§17) adjusted by the voice's measured speed (updated after each job from actual audio).
- Master: `target_minutes` drawn from `long_minutes` → `target_words = target_minutes × wpm`. For 10–14 min at 155 wpm: **1,550–2,170 words**. Chapters: 5–7, each `target_words / n_chapters ± 20%` (≈ 300–450 words).
- **Floor:** rendered master ≥ **8:30**. Planning floor is 9:00 (`min_words = 9 × wpm`) for buffer.
- After voicing (M10), measured duration < 9:00 → `expand.md` on the shortest chapter (+120–200 words), re-voice that chapter only, re-measure. Max 2 expansions, else the job fails `NS-E305`.
- Shorts: `length_bucket` seconds → words = `seconds × wpm / 60` for the body **plus** the hook (≤ 12 words, spoken in ≤ 1.5 s at the front) and closing line (≤ 15 words). Validator checks measured audio against the bucket ±15%.

### 7.1 Master (long-form) track
| # | Stage | Owner | Tenant | Output |
|---|---|---|---|---|
| M1 | Ideas (N=8), dedupe vs history (embedding similarity < 0.85); pick one with **chapter potential** | D | `llama` | `idea.json` |
| M2 | Plan: 5–7 chapters, each with its own mini-arc and a cliff into the next; ad-break-safe transitions; word budget per chapter (§7.0) | D | `llama` | `plan.json` |
| M3 | Draft **per chapter**, given plan + running summary of previous chapters | D | `llama` | `story.json` |
| M4 | Critique → revise per chapter (≤ 2 rounds), then a whole-story continuity pass (§16.2); 16 GB profile runs continuity on chapter pairs | D | `llama` | `story.json` |
| M5 | Long-form SEO: title best-of-5 + judge, description, tags; chapter titles (timestamps filled at M13) | D | `llama` | `seo_long.json` |
| M6 | Visual plan **per beat** (every 2–3 sentences; ≈ 7 s of narration): 3 literal keywords + mood (§7.4 clip budget) | D | `llama` | `visual_plan.json` |
| M7 | **Shorts selection**: 4–6 self-contained moments identified as `{chapter, sentence_start, sentence_end}` (no timestamps yet), each sized to a length bucket by word count; for each, 5 standalone hooks + judge pick + a closing line pointing to the full story | D | `llama` | `shorts_plan.json` |
| M8 | Short SEO per moment (best-of-5 titles, Shorts description rules §9.2) | D | `llama` | `seo_shorts.json` |
| M9 | **Policy gate — text** (§18.8) on story + all metadata | H | `llama`, `embed` | `policy_text.json` |
| M10 | Voice: narration per chapter (sentence-chunked, §7.5) + each Short's hook and closing line, one batch; measure duration; expansion loop (§7.0) | E | `tts` | `audio\` |
| M11 | Captions: whisper word timestamps per chapter → align to script (§7.6) → `words\`, ASS for 16:9 and 9:16 | E | `whisper` | `words\`, `captions\` |
| M11b | **Shorts timing**: resolve each Short's sentence span to `start_s/end_s` in the master audio from `words\`; snap to sentence boundaries | F | none | `shorts_plan.json` (updated) |
| M12 | Clips: fetch per unique keyword, both orientations only for beats inside selected Shorts (parallel with M10–M11) | E | none | `clips\h\`, `clips\v\` |
| M13 | Render master 1920×1080 + chapter title cards ≤ 1.5 s; write final chapter timestamps + **mid-roll suggestions** at chapter breaks (none in first/last 2 min) | E | NVENC | `long\master.mp4`, `long\chapters.json` |
| M14 | Thumbnails ×3 (frame pick + ≤ 4-word text via presets) | E | none | `long\thumb_[1-3].jpg` |

### 7.2 Shorts track (derived from the master)
| # | Stage | Owner | Output |
|---|---|---|---|
| S1 | Cut the selected narration segment from the master audio; prepend the voiced hook; append the closing line; loudness-normalize | E | `shorts\s<k>\audio.wav` |
| S2 | **Fresh vertical edit**: vertical clips (not a crop of the master), own caption style and visual treatment from its combo; cuts every 1.5–3 s | E | `shorts\s<k>\short.mp4` |
| S3 | QC gate (both formats, §7.7) | E | `qc\` |
| S4 | **Policy gate — final** (§18.8) | H | `policy_final.json` |
| S5 | Outbox + upload sheets; each Short's sheet says *"Set Related video → <master title>"* | E | Outbox |

**Upload order the sheet recommends:** master first (so Shorts can link to it), then Shorts spread across the following days (§18.6). The "Related video" link is set in YouTube Studio; third-party tools can't set it through the API.

### 7.3 Other formats (same engine)
- `--format shorts_only`: story → parts → Shorts (Slice 1 path), for niches that don't suit 10+ minutes. Stages: ideas, plan, draft, critic/revise, split, hook, SEO, visual plan, policy text, voice, captions, clips, render, QC, policy final, outbox.
- `--format series_compilation`: stitches an existing multi-part Shorts series into one long-form master (re-renders 16:9 from the parts' narration and new landscape clips; adds chapter cards). Deferred to after Slice 3 if time allows; the orchestrator accepts the flag from day one and errors `NS-E306 not implemented` until then.

### 7.4 Clip budget and reuse (replaces v1 §17 clip rules)
- **Master shot length:** 5–9 s (visual change at every beat or sentence, whichever is longer). 12 min ≈ **90–140 clips**.
- **Shorts shot length:** 1.5–3 s; a 50 s Short ≈ 20 clips, drawn from the vertical pool fetched for its beats.
- **Keyword dedupe:** `visual_plan` keywords are normalized (lowercase, lemmatized) and deduped per job; one API search (per source, per orientation) per unique keyword, page size 80. Typical job: **≤ 60 unique keywords → ≤ 240 API calls**, under Pexels' 200/hour when spread across the job (client enforces a token bucket: Pexels 180/h, Pixabay 90/min, and honours `429`).
- **Cache:** every downloaded clip is kept in `cache\clips\<source>\<id>.mp4` with metadata (`source,id,url,license,duration,w,h,fetched_at,keywords[]`) for 90 days. Cache hits cost no API call. Pixabay search responses are cached 24 h as required.
- **Reuse rules:** no clip twice in one video · no clip twice on one channel within **14 days** · no clip on two channels within **30 days** · a Short may use clips also used in its own master (different orientation anyway). Enforced from `clip_usage` in SQLite; a violation picks the next candidate, never fails the job unless the pool is empty (`NS-E402`, then the beat falls back to a mood-only keyword search).
- **Selection:** prefer ≥ 1080 p, duration ≥ shot length + 1 s, no watermark flag, no faces when `mood` is `intimate`/`horror` (Pexels/Pixabay don't expose this; we skip clips whose title/tags include `person, portrait, face`). Landscape in a vertical edit → centre crop over a blurred fill; portrait in a landscape edit is never used.

### 7.5 TTS chunking (WP-C + WP-E)
- Split narration into sentences (spaCy sentencizer); merge sentences shorter than 25 chars with the next; cap a chunk at 280 chars (split on clause punctuation when longer).
- Batch all chunks of a job in one `/v1/audio/speech/batch` call; the server generates in order with a fixed seed per voice so timbre is stable.
- Stitch with 350 ms silence between sentences, 700 ms at paragraph/chapter breaks, 1,200 ms before a cliff line (marked in `story.json`). Trim leading/trailing silence per chunk (−45 dBFS threshold).
- Per chapter `loudnorm` to −16 LUFS integrated, −1.5 dBTP, then a whole-master pass to −14 LUFS.

### 7.6 Caption alignment (WP-E)
1. Run faster-whisper with `word_timestamps=True` on each chapter WAV.
2. Align ASR tokens to the script's tokens (normalized: lowercase, punctuation stripped, numbers spelled) with `difflib.SequenceMatcher`; matched script words take ASR times; unmatched runs are interpolated linearly between neighbours.
3. Emit `words\<chapter>.json` = `[{w, start, end, sentence_idx}]` using the **script's** spelling and punctuation.
4. Build ASS with 2–4 words on screen, active word highlighted per `caption_style`. Drift check: median |ASR time − interpolated time| on matched words ≤ 120 ms, else `NS-E403`.

### 7.7 QC targets (`qc.json`)
| Check | Master | Short |
|---|---|---|
| `duration` | 8:30 ≤ d ≤ 15:00 | bucket ±15 %, ≤ 179 s |
| `loudness` | −14 ± 1 LUFS, TP ≤ −1 dBTP | same |
| `caption_drift_ms` | ≤ 120 | ≤ 120 |
| `black_frames` | 0 frames > 0.3 s at ≥ 98 % black | same |
| `file_opens` | ffprobe OK, 1 video + 1 audio stream, `moov` at front (faststart) | same |
| `hook_first_1500ms` | n/a | first caption at 0.0 s; speech onset ≤ 300 ms |
| `aspect` | 1920×1080 | 1080×1920 |

---

## 8. Variation engine (WP-F)

YouTube's spam policy names this pattern as a violation example: channels using the same background music and repetitive AI imagery across many videos, each reading an AI-generated script. Templates are therefore **composed from parts** (§17), never rotated whole.
- Combo per video: `{hook, structure, pov_tone, pacing, caption_style, visual_treatment, title_formula, length_bucket}`. A master and its Shorts share `structure, pov_tone, pacing` (same story) but each Short draws its own `hook, caption_style, visual_treatment, title_formula, length_bucket`.
- Draws are weighted (weights updated by §18.9 learning), restricted to the channel's allowed pools.
- **Combo ledger:** no repeated full combo per channel within 30 days; no repeated `{hook, structure}` pair within the last `min(10, pairs − 1)` videos, where `pairs = |hooks| × |structures|`.
- **Pool minimums** (enforced by `studio channel add|edit`): ≥ 3 hooks, ≥ 3 structures, `pairs ≥ 9`, ≥ 2 caption styles, ≥ 2 visual treatments. Warn below 4×4.
- **Cross-channel distinctness:** two channels on one install may not share the same niche, voice, caption-style set **and** visual-treatment set. `studio channel add` enforces this.

---

## 9. SEO rules (WP-D, enforced in code)

### 9.1 Long-form (master)
- **Title:** ≤ 70 chars target, 100 max. Primary keyword in the first 40 chars. Formula from the combo. Must match the content (§18.4). No "Part X" unless `series_compilation`.
- **Description:** 2–3 honest summary lines (no spoilers past chapter 1) · blank line · chapter list `00:00 Title` (first entry must be `00:00`, ≥ 3 chapters, each ≥ 10 s) · "Original fiction." line when `fiction_label` · optional creator note · 3–5 hashtags (**no `#Shorts`**).
- **Tags:** 10–20 relevant tags, ≤ 500 chars total. No unrelated trending tags, no competitor channel names.

### 9.2 Shorts
- **Title:** ≤ 70 chars target, 100 max. Keyword in the first 40 chars. Includes "Part X" only for shorts-only series.
- **Description:** 1–2 honest lines · "From the full story: <master title>" · "Original fiction." when `fiction_label` · 3–5 hashtags incl. `#Shorts`.
- **Tags:** 10–20, ≤ 500 chars, same relevance rule.

---

## 10. Publishing

- **Default:** review queue + manual upload in YouTube Studio, using the upload sheet. `studio review` lists pending items, opens the MP4 in the default player on request, shows story text, policy-gate notes and disclosure advice, and records `approve|reject <reason>`.
- **`studio mark-uploaded <job_id> <long|s<k>> <url>`** records the public URL so analytics import can match rows.
- **Optional API upload (v2, not in this build):** unaudited API projects created after 28 Jul 2020 get uploads locked to private until they pass the compliance audit; quota cost 1,600 units per upload (verify in Cloud Console). OAuth per channel via `keyring`. The `uploads` table and CLI are shaped so this can be added without schema changes.

---

## 11. Channel profile example

`config\channels\after-dark.yaml`
```yaml
slug: after-dark
display_name: After Dark Stories
status: active                    # active | paused (§18.11)
language: en
niche: "original short horror fiction set in everyday places"
seed_keywords: [horror story, scary story, creepy story, horror short]
voice_notes: "Plain, intimate first person. Short sentences. Dread over gore."
voice: { mode: preset, speaker: Ryan, speed: 1.0 }
format: master_first              # master_first | shorts_only | series_compilation
masters_per_week: 1               # governor adjusts (§18.6)
long_minutes: [10, 14]            # target runtime range; rendered floor 8:30
shorts_per_master: [4, 6]
shorts_per_day_max: 2
length_buckets: [35, 50, 75]      # Shorts seconds; learning reweights (§18.5)
publish_window: "18:00-21:00"
audience: general                 # general | mature (sets age-gate advice)
made_for_kids: false
fiction_label: true
creator_note: ""                  # optional line appended to descriptions
pools:
  hooks: [cold_open, question, confession, warning]
  structures: [twist, reverse_reveal, slow_burn, unreliable_narrator]
  pov_tones: [first_person_intimate, narrator_calm]
  pacings: [standard, measured]
  caption_styles: [punch_yellow, clean_white]
  visual_treatments: [dark_slowzoom, handheld_grit]
  title_formulas: [question, statement_twist, i_never]
avoid_topics: []
```

---

## 12. Build plan — slices with fake-services gates, then the first run on the work machine

Each slice: **(a)** main session writes/extends contracts and `ns_common` for the slice, freezes them (`contracts-s<N>` git tag) · **(b)** worker agents build their packages in parallel (Sonnet-class workers for standard code, Opus-class only for prompt design, policy logic and orchestrator design; Haiku for mechanical sweeps) · **(c)** main session integrates, runs lint + typecheck + unit tests · **(d)** the **local gate** runs on the build laptop with `--fake-services` (§1.1) via `dev\gate_s<N>.py` (or `ns gate s<N>` from Slice 4 on) and writes `logs\gate-s<N>-*.json` · **(e)** fix pass until the gate passes. Only then does the next slice start. Nothing is uploaded to Drive before Slice 5's gate passes.

| Slice | Builds | Active WPs | Local gate (build laptop, fakes + real FFmpeg) |
|---|---|---|---|
| **0 Foundations** | Repo tree, `manifest.json` with verified pins (GPU and `cpu-dev` model sets; SHA256 resolved from the Hugging Face API, files **not** downloaded), `ns_common` incl. all fakes, all Slice 1 contracts and schemas, policy lists, `pyproject.toml` + `uv.lock`, CI config, `dev\` scripts, `CLAUDE.md`, `README.md` | main | Unit tests green; `tools\resolve_manifest.py --check` confirms every pinned URL answers a HEAD request and every HF file has a SHA; fakes produce a WAV, a vector and a JSON that validate against the schemas; FFmpeg renders a 2 s test clip with `libx264`. |
| **1 One Short** | Shorts-only text stages (ideas → split → hooks → SEO → visual plan), TTS server (engine abstraction; both engines coded against their documented APIs and unit-tested with a fake engine; server tested with FastAPI's test client), captions + alignment, clip fetch + cache (both APIs coded against their documented responses, tested with recorded fixtures), vertical render, QC, outbox sheet, minimal CLI (`studio run --format shorts_only --count 1`), `studio calibrate` | C, D, E, F, G | `studio run --fake-services --format shorts_only --count 3` produces 3 Shorts: real MP4s with burned captions, QC passing (duration, loudness, drift, file opens, aspect), upload sheets valid against schema; `studio calibrate --n 5 --fake-services` produces the scoring template. If the developer has Pexels/Pixabay keys, the gate additionally runs one job with real clip fetching (`--fake-services=llm,tts,whisper,embed`). |
| **2 Master-first** | Long plan, chapter draft, critique/revise, continuity, expansion loop, long SEO, per-beat visual plan, Shorts selection + timing, landscape render + chapter cards + mid-roll suggestions, thumbnails, derived Shorts (S1–S2), outbox for both | D, E, F, G | `--fake-services` master job with `long_minutes: [9, 10]`: floor/expansion logic exercised by a fixture that is deliberately short; `chapters.json` timestamps match ffprobe; thumbnails render; mid-roll suggestions respect the 2-minute rule; 4 Shorts cut and rendered. |
| **3 Safety & growth** | Policy gate (text + final, all §18.8 checks), variation engine + ledger + pool checks, channel CRUD + distinctness, cadence governor, `studio review`, `studio learn --import`, `studio status`, `studio pause`, SQLite backup | F, H, G | One full fake job passes both gates; seeded negative fixtures (true-story claim, real name, duplicate story, music track) are rejected with the right `NS-E6xx`; a sample analytics CSV import reweights pools and `studio status` shows it. |
| **4 Clipper** | Source import (copy + ffprobe), transcription wrapper (segments + words), `clip_select.md` prompt + stage, face/facecam detection, `clip_templates.yaml` and layout engine, cut + render (reusing the Slice 1 renderer and caption builder), clip SEO + policy text gate on metadata, Outbox `clip-<id>` sheets, `studio clip` CLI | I, E, G | `studio clip tests/fixtures/clipper/sample.mp4 --template auto --fake-services` on a synthetic 90 s fixture (FFmpeg `testsrc2` with a drawn "facecam" rectangle and a tone track) yields ≥ 2 clips: facecam rectangle detected within 5 % of its true position, layouts render at 1080×1920, captions burned, QC passes, sheets valid. |
| **5 Runtime** | Supervisor + GPU lock + idle unload, `embed` service management, `ns tune`, `ns verify`, `ns doctor --bundle`, `ns stop`, `ns gate`, `ns update`, scheduled task, launchers | B, A (launchers), G | Supervisor manages a **stub service** (a tiny HTTP server standing in for llama-server): two concurrent acquirers serialize; idle unload stops it; crash restart works; scheduled task registers and fires a `--dry-run` at a test time; `ns doctor --bundle` produces a zip with the listed contents; `ns update` swaps the repo directory and rolls back in a temp copy. |
| **6 Dashboard** | `ns_dashboard` API + static UI (§20): channels, review queue with video streaming, runs + log tail, Clipper drop zone + job progress, learn import, pause/resume, settings; background job worker using the supervisor; `Nightshift Dashboard.lnk` | J, G | API tests with FastAPI's test client against a fake-services job queue; browser smoke via Playwright-free check (`curl` the SPA, exercise each API route); a `--fake-services` clip job started from the UI's upload endpoint finishes and appears in the review queue; approve/reject writes `outputs.status`. |
| **7 Installer** | `INSTALL.cmd`, `stage1.ps1`, wizard + all steps, `--answers`, `OPERATOR.md`, `dev\make_bundle.ps1` | A, G | In a temp `NIGHTSHIFT_HOME`: `stage1.ps1 -DryRun` lists every action it would take; `wizard.py --answers answers.example.json --simulate-downloads` runs all steps with downloads replaced by HEAD checks + tiny fixture files, resumes after a forced kill at step 3, and ends with the summary and a diagnostics zip; `make_bundle.ps1` produces a zip that contains no `.venv`, models or binaries and extracts to a working repo. |
| **8 Nightshift Code (optional)** | OpenCode pin + download step, `opencode.json`, `AGENTS.md`, prewarm, shortcut | B, A | Config validates against OpenCode's schema; launcher acquires/releases through the supervisor stub; prewarm step is exercised with `--simulate-downloads`. |
| **9 First run on the work machine** | Upload `Nightshift-<version>.zip` + `answers.json` to Drive; partner extracts and runs `INSTALL.cmd`; `verify-report.json` and the diagnostics zip come back (or the developer continues directly on that machine) | main | `verify-report.json` shows all §14.6 gates measured, one real master job and one real clip job in the Outbox, `ns tune` results recorded. Then the hardware-dependent calibration (§16.5) and the prompt-quality work begin on that machine. |

Deferred to after Slice 9: `series_compilation`, API upload, non-English caption/NER models beyond spaCy's multilingual set, active-speaker detection by audio (§19.4 uses a visual heuristic in v1).

---

## 13. Decisions (defaults until changed)

| # | Question | Default | Why |
|---|---|---|---|
| D1 | Where does testing happen? | Entirely on the build laptop in the `cpu-dev` profile; the target PC is reached only through Google Drive (installer out, diagnostics zip back, update zips out) | Developer has no access to the target PC at any time |
| D2 | Nightshift Code in scope? | Yes, as the last optional slice; the installer asks | Keeps the money-making path unblocked |
| D3 | One LLM or two? | One (standard MTP GGUF) for both aliases; abliterated story model only if calibration shows fiction refusals | Saves ~20 GB, half the tuning, no model swap between coding and studio |
| D4 | First niche / language | Horror fiction, English, 1 channel | Matches the example profile |
| D5 | Visuals | Pexels + Pixabay stock | Free, commercial-use licences; AI video generation is out of scope for v1 |
| D6 | Upload | Review queue + manual | API uploads are private-locked until audit |
| D7 | Shorts length | Buckets 35 / 50 / 75 s, learned per channel | |
| D8 | Cadence | Master-first: 1/week for 2 weeks, then up to 2/week | §18.6 |
| D9 | Go/no-go | Reviewed at week 8 (§18.12) | |
| D10 | Dashboard technology | Local FastAPI + vanilla HTML/JS single page on 127.0.0.1:8100, no login | Runs on the same machine as the GPU; no build toolchain; no exposure outside localhost |
| D11 | Clipper transcription model | faster-whisper `distil-large-v3` on GPU profiles, `tiny` on `cpu-dev` | Accuracy on arbitrary speech matters more than for script alignment |
| D12 | Active-speaker framing for podcasts | v1: visual heuristic (largest face / mouth-region motion); audio diarization deferred | Avoids a diarization model; good enough for stacked two-speaker layouts |
| D13 | Download size | **~15 GB default**: 14B Q4 LLM (9), TTS CustomVoice + Base Q8_0 GGUF + tokenizer (4.4), one whisper (1.5), small extras; VoiceDesign (+2.1) and bigger LLM tiers on demand; `models_source` folder mode for offline/pre-downloaded installs | Egyptian bandwidth; quantized GGUF is already the compressed form, zipping gains ≤ 3 % |
| D14 | TTS engine | `qwentts.cpp` (GGML, CUDA, no PyTorch) with PyTorch `qwen-tts` as automatic fallback | Removes ~2.5 GB of torch and ~6 GB of fp16 weights from the target |

---

## 14. Performance & quality mandate

### 14.1 LLM speed
- **MTP:** `--spec-type draft-mtp --spec-draft-n-max` from the sweep; `-np 1` (required with MTP in current builds). Community figures: +17 % on the 35B-A3B MoE, more on dense models; do not promise more than the sweep measures.
- Tuned expert offload; KV `q8_0` when needed; fixed system prompts for prefix caching; JSON-schema-constrained output (`response_format` with `json_schema`); `--alias code,story`.

### 14.2 TTS speed
`qwentts.cpp` CUDA build with Q8_0 talkers; one process kept warm by the supervisor, chunks streamed through it in order. Target RTF ≤ 0.5 (measured by `ns verify`). Fallback `qwen-tts` reference must still beat real time.

### 14.3 Pipeline
Clip fetching overlaps GPU stages; one filter graph per video; NVENC; pre-trimmed proxies for Shorts clips.

### 14.4 Lightness
One LLM runtime, no Ollama or Docker, supervisor-only background process, idle unload, embed server on CPU (~300 MB RAM).

### 14.5 Quality
Plan → draft → critique → revise; best-of-5 + judge for hooks and titles; exemplars; thinking on for plan/critique/judge/continuity/select only; hard validators; critic bar calibrated against human scores (§16.5). No cloud fallback.

### 14.6 Benchmark gates (`ns verify`, 32 GB profile; 16 GB profile targets in brackets; measured on the target PC by the operator's run and reported in the diagnostics bundle — never asserted by the developer)
| Metric | Target |
|---|---|
| `code` generation, 16K ctx | ≥ 40 tok/s [≥ 30] |
| Model load, warm page cache, NVMe | ≤ 30 s (soft; logged, HDD exempt) |
| TTS real-time factor | ≤ 0.5 (2× faster than real time) [≤ 0.8] |
| One 50-second Short, idea → MP4 (shorts-only) | ≤ 3 min [≤ 5] |
| One master job: 12-min long-form + 5 Shorts + thumbnails | ≤ 60 min [≤ 90] |
| Nightly run, 2 channels × 1 master job | ≤ 2 h [≤ 3] |
| Idle VRAM after unload | ≤ 300 MB |

---

## 15. Contracts registry (frozen per slice)

### 15.1 Names
| Thing | Value |
|---|---|
| Product | `Nightshift` |
| Home env var | `NIGHTSHIFT_HOME` (default `%LOCALAPPDATA%\Nightshift`) |
| Python packages | `ns_common`, `ns_supervisor`, `ns_tts`, `ns_studio` |
| Console scripts | `ns` (tune, verify, doctor, stop, gate), `studio` |
| Logical models | `code`, `story` (aliases; same file in one-model mode) |
| Services | `llama`, `embed`, `tts`, `whisper` (whisper is in-process in the studio venv but takes the GPU lock) |
| Keyring | service `Nightshift`; keys `pexels_api_key`, `pixabay_api_key`, `yt_oauth_<slug>` |
| Installer | `INSTALL.cmd` (bundle `Nightshift-<version>.zip`) |
| App path | `install_state.app_path` (the extracted repo, e.g. `D:\NightShift`) |

### 15.2 Paths (under `NIGHTSHIFT_HOME`)
```
bin\llama\ bin\qwentts\ bin\opencode\
models\llm\ models\embed\ models\tts\ models\tts\voices\ models\whisper\ models\spacy\
venvs\tts\ venvs\studio\ venvs\supervisor\
config\runtime.json config\install_state.json config\opencode.json config\channels\<slug>.yaml
data\studio.db data\backup\studio-YYYYMMDD.db data\jobs\<job_id>\ data\calibration\<slug>\
cache\clips\<source>\<id>.mp4 cache\clips\index.sqlite cache\search\   imports\analytics\
outbox\<slug>\<YYYY-MM-DD>\<job_id>\long\  outbox\<slug>\<YYYY-MM-DD>\<job_id>\shorts\
rejected\<slug>\<YYYY-MM-DD>\<job_id>\ logs\
```
Coding workspace: `%USERPROFILE%\Projects`.

### 15.3 Ports & supervisor API (127.0.0.1)
llama **8080** · embed **8081** · TTS **8091** · supervisor **8099** · dashboard **8100**.
| Method | Path | Body → Response |
|---|---|---|
| GET | `/status` | → `{gpu_owner, services:{<name>:{state,pid,port,vram_mb,model_alias?}}}` |
| POST | `/acquire` | `{service, owner, model_alias?, wait:true, timeout_s:7200}` → `{endpoint, model_alias}` |
| POST | `/release` | `{service, owner}` → `{ok}` |
| POST | `/stop_all` | → `{ok}` |
| POST | `/shutdown` | `{grace_s:30}` → `{ok}`; stops children and exits the supervisor |
| POST | `/warmup` | `{service}` → `{state}`; starts a service without an owner so the first job is fast |
| GET | `/events` | server-sent events `{ts, kind:"service"|"job", data}` |

States: `stopped|starting|ready|stopping|error`. Owners: `opencode`, `studio:<job_id>`, `verify`, `gate`. `embed` acquisition always succeeds immediately (no lock). In two-model mode, acquiring `llama` with a different `model_alias` than the loaded one triggers a swap once the current owner releases.

### 15.4 TTS API (port 8091)
| Method | Path | Body |
|---|---|---|
| POST | `/v1/audio/speech` | `{model:"qwen3-tts", input, voice, speed, response_format:"wav", seed?}` → WAV 24 kHz mono |
| POST | `/v1/audio/speech/batch` | `{items:[{id,input,voice,speed}], out_dir, seed?}` → `{files:[{id,path,seconds}], engine, rtf}` |
| GET | `/v1/voices` | → `[{name, mode:"preset"|"design"|"clone", source}]` |
| POST | `/v1/voices/design` | `{name, description}` → `{name, sample_path}` |
| POST | `/v1/voices/clone` | `{name, ref_audio_path, ref_text, rights_confirmed:true}` → `{name, sample_path}` (400 if `rights_confirmed` false) |
| GET | `/health` | → `{ok, engine:"qwentts"|"reference"|"fake", model_loaded}` |
| GET | `/v1/voices/{name}/embedding` | → the stored speaker-embedding file (bytes) for backup/transfer between machines |

**Voice library = embeddings, not audio.** `models\tts\voices\<name>\{voice.json, speaker.bin, sample.wav}`: `speaker.bin` is the precomputed speaker prompt produced once (from a clone sample via Base, or from a VoiceDesign render that is immediately re-encoded through Base). Every later synthesis passes the stored prompt to the Base talker directly; nothing is re-derived. Presets need no embedding (CustomVoice talker). VoiceDesign is only needed at design time and may be deleted afterwards.

### 15.5 Embed API (port 8081)
Standard llama-server `POST /v1/embeddings` `{input:[...]}` → OpenAI-shaped response. `ns_common.embed_client.embed(texts) -> list[list[float]]`, `cosine(a,b)`.

### 15.6 JSON schemas (`contracts/schemas/`)
`channel, runtime, install_state, answers, manifest, idea, plan, story, parts, seo, seo_long, shorts_plan, chapters, visual_plan, qc, policy, metadata, combo, analytics_row, calibration_row, gate_report, transcript, clip_plan, clip_layout, clip_template, dashboard_job`.
- `transcript`: `{source, duration_s, language, segments:[{i, start, end, text, speaker?}], words:[{w, start, end, seg}]}`
- `clip_plan`: `[{k, start_s, end_s, title, hook_line?, why, score:0-1, speakers?:[..], keywords[]}]`
- `clip_layout`: `{template, facecam?:{x,y,w,h}, faces:[{t, boxes:[{x,y,w,h}]}], crops:[{t_start, t_end, region:{x,y,w,h}}]}`
- `metadata.kind` gains `"clip"`; `metadata.source_video` and `metadata.template` are set for clips.
- `runtime`: `{profile:"gpu"|"cpu-dev", llama:{port, model_path, aliases:["code","story"], flags:{n_gpu_layers, n_cpu_moe, ctx, kv_type, spec_type?, spec_draft_n_max?, threads?}}, embed:{port, model_path}, tts:{port, engine:"faster"|"reference", model_dir, device:"cuda"|"cpu"}, whisper:{model, device, compute_type}, video:{encoder:"h264_nvenc"|"libx264", preset}}`
- `manifest`: `{version, profiles:{<profile>:{llm:{repo,file,sha256,bytes}, embed:{...}, tts:[{repo,sha256_index}], whisper:{model}}}, bins:{llama:{url,sha256,version}, cudart:{...}, opencode:{...}}, python:{version}, spacy:{model, version}}`
- `idea`: `{premise, twist, setting, protagonist, why_chapters, keywords[]}`
- `plan`: `{target_minutes, target_words, chapters:[{n, title, beats[], cliff, word_budget}]}`
- `story`: `{working_title, chapters:[{n, title, text, summary, word_count, est_seconds, cliff_line_idx?, sentences:[{idx, text}]}], revisions:int}`
- `parts` (shorts-only): `[{n, text, hook, word_count, est_seconds, cliffhanger}]`
- `seo` (Short): `{title, description, tags[], hashtags[], keywords[]}`
- `seo_long`: `seo` + `{chapter_titles:[{n,title}]}`
- `shorts_plan`: `[{k, chapter, sentence_start, sentence_end, start_s?, end_s?, hooks[5], chosen_hook, closing_line, length_bucket, combo_id, why_standalone}]`
- `chapters`: `{chapters:[{n, title, start_s, end_s}], midroll_suggestions_s:[..]}`
- `visual_plan`: `[{chapter|part, beat, sentence_start, sentence_end, keywords[3], mood, need_vertical:bool}]`
- `qc`: `{item:"long"|"s<k>", passed, checks:{duration, loudness, caption_drift_ms, black_frames, file_opens, hook_first_1500ms?, aspect}, reason?}` (each check `{passed, value, limit}`)
- `policy`: `{stage:"text"|"final", passed, checks:{<check>:{passed, detail}}, advice:{ai_disclosure:"yes"|"no", made_for_kids:bool, age_restrict_suggested:bool}}`
- `metadata` (upload sheet): `{channel, job_id, kind:"long"|"short", k?, title, description, tags, hashtags, publish_after, chapters?, midroll_suggestions_s?, thumbnails?, related_video_instruction?, parent_job_id?, ai_disclosure, made_for_kids, age_restrict_suggested, clips:[{source,id,url,license}], combo_id, prompt_versions:{...}}`
- `combo`: `{combo_id, hook, structure, pov_tone, pacing, caption_style, visual_treatment, title_formula, length_bucket?}`
- `calibration_row`: `{story_id, rubric:{hook, clarity, pacing, payoff, cliffhangers, originality, voice_fit}, critic_avg, human_avg?}`
- `gate_report`: `{slice, date, machine:{gpu, ram_gb, disk_type}, checks:[{name, passed, value?, limit?, log?}], benchmarks:{...}}`

### 15.7 SQLite (`schema.sql`)
`channels, ideas, stories, parts, clips, clip_usage, combos, outputs, runs, uploads(output_id, video_url, published_at), analytics(output_id, date, views, viewed_pct, avg_pct_viewed, likes, comments, shares, subs_gained), pool_weights(channel, pool, option, weight, updated_at), calibration(channel, story_id, critic_json, human_json), incidents(channel, ts, kind, note), sources(source_id, channel, path, duration_s, sha256, imported_at, template, status), dashboard_jobs(job_id, kind, channel, status, progress, message, created_at, started_at, finished_at, params_json)`. `outputs.kind` gains `clip`; `outputs.source_id` references `sources`. WAL mode. `db.backup()` copies to `data\backup\` nightly, keeps 14.

### 15.8 Stage interface & job directory
```python
class Stage(Protocol):
    name: str
    tenant: Literal["llama", "tts", "whisper", None]
    def run(self, ctx: JobContext) -> None: ...   # idempotent; skips if its outputs exist and inputs' hashes match
```
`JobContext` carries `job_id, channel, combo, job_dir, db, llm, tts, embed, log, runtime`. The orchestrator groups runnable stages across jobs by tenant and acquires each tenant once per group.

Job dir `data\jobs\<job_id>\`: `idea.json plan.json story.json seo_long.json seo_shorts.json visual_plan.json shorts_plan.json policy_text.json policy_final.json audio\ words\ captions\ clips\h\ clips\v\ long\master.mp4 long\chapters.json long\thumb_[1-3].jpg shorts\s<k>\{audio.wav,short.mp4} qc\ stage_state.json`.
Outbox `outbox\<slug>\<YYYY-MM-DD>\<job_id>\`: `long\master.mp4 + .json + .txt + thumb_*.jpg` and `shorts\s<k>.mp4 + .json + .txt`. `job_id`: `<slug>-<YYYYMMDD>-<4hex>`.

### 15.9 CLI, launchers, shortcuts, task
- `studio run [--daily] [--channel <slug>] [--count N] [--format master_first|shorts_only|series_compilation] [--dry-run] [--resume <job_id>]`
- `studio review [--channel]` · `studio status` · `studio pause <slug> --reason "<text>"` · `studio resume <slug>`
- `studio channel add|list|edit|validate` · `studio voice list|preview|design|clone`
- `studio learn --import <csv>` · `studio mark-uploaded <job_id> <long|s<k>> <url>`
- `studio calibrate --channel <slug> [--n 20]` · `studio calibrate --score <csv>`
- `studio clip <video> --channel <slug> [--template auto|gaming_facecam|podcast_stacked|podcast_single|talking_head|center_crop] [--max-clips 6] [--min-s 20] [--max-s 60]`
- `studio dashboard [--port 8100] [--open]`
- `ns tune` · `ns verify [--quick]` · `ns doctor [--bundle]` · `ns stop` · `ns gate s<N>` · `ns update <zip> | --rollback`

| Shortcut | Launcher |
|---|---|
| `Nightshift Dashboard.lnk` | `nightshift-dashboard.ps1`: start the supervisor as a minimized "Nightshift Engine" window if not running, wait for `/status`, open `http://127.0.0.1:8100` in the default browser (§6.2) |
| `Nightshift Studio.lnk` | `nightshift-studio.ps1`: `studio status` + interactive prompt |
| `Nightshift Outbox.lnk` | `open-outbox.ps1` |
| `Nightshift - Stop All.lnk` | `stop-all.ps1` → `ns stop` |
| `Nightshift - Update.lnk` | `update.ps1`: file picker for `nightshift-app-*.zip` → `ns update` |
| `Nightshift - Diagnostics.lnk` | `diagnostics.ps1` → `ns doctor --bundle`, then opens the Desktop folder |
| `Nightshift Code.lnk` (optional) | `nightshift-code.ps1`: acquire `llama` as `code`, set `OPENCODE_CONFIG`, open OpenCode in Projects; release on exit |

Scheduled task `Nightshift Daily` → `studio run --daily`; `WakeToRun=true`, `StartWhenAvailable=true`, run whether user is logged on or not, stop if running > 6 h.

### 15.10 OpenCode local-only lock (`config\opencode.json` via `OPENCODE_CONFIG`)
```json
{
  "$schema": "https://opencode.ai/config.json",
  "enabled_providers": ["local"],
  "provider": {
    "local": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Nightshift Local Qwen",
      "options": { "baseURL": "http://127.0.0.1:8080/v1", "apiKey": "local" },
      "models": { "code": { "name": "Qwen3.6 35B-A3B (local)" } }
    }
  },
  "model": "local/code",
  "small_model": "local/code",
  "autoupdate": false,
  "share": "disabled"
}
```

### 15.11 Logging
JSON lines `logs\<component>-YYYYMMDD.jsonl`: `ts, level, component, event, msg, data`. Every stage logs `stage_start`/`stage_end` with `job_id`, duration and tenant.

### 15.12 Error codes
`NS-E1xx` install (`NS-W1xx` warnings; `E108` CUDA torch check failed; `E111` winget missing; `W112` long paths disabled; `E112` faster-whisper cannot load on CUDA: cuBLAS/cuDNN DLLs) · `NS-E2xx` runtime/GPU lock · `NS-E3xx` text/LLM (`E305` floor not reached, `E306` not implemented; `W301` TTS fell back to reference engine, `W302` MTP disabled) · `NS-E4xx` media/QC (`E402` clip pool empty, `E403` caption drift; `W401` NVENC unavailable, using libx264) · `NS-E5xx` stock APIs/network · `NS-E6xx` policy gate (one code per check) · `NS-E7xx` update/rollback.

### 15.13 Prompt versioning
Every prompt file starts with YAML front-matter `{version: int, thinking: bool, schema: <name>}`. `metadata.prompt_versions` records the versions used so learning can attribute results to prompt changes.

---

## 16. Prompts, rubric, validators, calibration (WP-D)

### 16.1 Prompt files (Jinja2; receive `channel`, `combo`, `exemplars`, `policy_rules`, `budget`)
| File | Purpose | Thinking |
|---|---|---|
| `ideas.md` | 8 original premises + twist; no real people, no real crimes, no retellings of existing works or posts | off |
| `story_plan.md` | (shorts-only) Beat sheet: setup, escalation, turn, payoff, part breaks | on |
| `story_draft.md` | (shorts-only) Full story in `voice_notes` style, spoken rhythm, within `budget.words` | off |
| `critic.md` | Rubric scores (§16.2) + concrete fixes, JSON | on |
| `revise.md` | Apply fixes only | off |
| `split.md` | (shorts-only) Parts per target length; cliffhangers except the last; last line of a single-part story loops into its first line | off |
| `hook.md` | 5 hooks per part in the combo style; **must honestly reflect the content**; ≤ 12 words | off |
| `judge.md` | Best of N, index + reason | on |
| `seo.md` / `long_seo.md` | Per §9.2 / §9.1 and §18.4 | off |
| `visual_plan.md` | Per beat: 3 literal, filmable stock keywords + mood + whether vertical is needed | off |
| `policy_check.md` | Classify against `unsafe_topics.yaml` (§18.3), JSON | on |
| `long_plan.md` | 5–7 chapters with mini-arcs, cliffs, natural ad-break transitions, per-chapter word budgets | on |
| `chapter_draft.md` | One chapter, given the plan + summaries of previous chapters + last 2 sentences verbatim | off |
| `continuity.md` | Whole-story (or pairwise) pass: names, timeline, facts; returns fixes | on |
| `expand.md` | Lengthen one chapter by N words without changing plot points | off |
| `shorts_select.md` | Pick 4–6 moments that stand alone; return sentence spans + why; sized to buckets | on |
| `short_hook.md` | 5 standalone hooks + a closing line ("The full story is on the channel" style, varied) per moment | off |

### 16.2 Critic rubric (1–10 each)
Hook · Clarity (understood on one listen) · Pacing (no filler) · Payoff (earned + surprising) · Cliffhangers · Originality (no stock phrases, unlike the exemplars) · Voice fit.
**Pass (default):** average ≥ 8 and no score < 6, **adjusted by calibration (§16.5)**. Max 2 revisions; otherwise discard and draw a new idea (max 3 ideas per job, else `NS-E304`).

### 16.3 Hard validators (code)
Words per part/chapter within budget ±20 % · banned phrases · no opening line with embedding similarity ≥ 0.8 to the channel's last 50 openings · SEO limits (§9) · reading level grade 5–8 (Flesch-Kincaid via `textstats.py`, configurable) · no sentence > 35 words (spoken rhythm) · no two consecutive chapters ending on the same cliff device.

### 16.4 Exemplars
3 original exemplars per default niche (horror, mystery, revenge, wholesome twist), written by WP-D, in `studio/exemplars/<niche>/`, each 600–900 words with a `meta.yaml` (structure, pov_tone, why it works).

### 16.5 Calibration harness (`studio calibrate`, Slice 1 gate)
1. Generate N=20 stories (shorts-only length) for the channel with the critic loop **disabled**; save text + critic scores to `data\calibration\<slug>\stories.csv` and one `.txt` per story.
2. The owner scores each on the same rubric in `scores.csv` (template provided).
3. `studio calibrate --score scores.csv` reports Spearman correlation per rubric item and overall, the critic's mean bias, and recommends a pass bar such that ≥ 80 % of human-passing stories pass and ≤ 20 % of human-failing stories pass. Writes `critic_bar` into the channel profile.
4. Also reports the **refusal rate** (critic or draft returned a refusal/meta text). If > 2 % on fiction prompts, D3 flips to two-model mode.

---

## 17. Variation presets

| Pool | Options |
|---|---|
| Hooks | `cold_open`, `question`, `confession`, `shocking_fact`, `in_medias_res`, `warning` |
| Structures | `twist`, `reverse_reveal`, `slow_burn`, `unreliable_narrator`, `countdown`, `two_timelines` |
| POV/tone | `first_person_intimate`, `second_person`, `narrator_calm`, `narrator_urgent` |
| Pacing | `brisk` 175 wpm, `standard` 155, `measured` 140 |
| Title formulas | `question`, `statement_twist`, `i_never`, `number_hook`, `warning` |
| Length buckets (Shorts) | 20–35 s, 35–60 s, 60–90 s (§18.5) |

**Caption styles** (`caption_styles.yaml`: font, size, colours, outline, y%, words-on-screen 2–4, animation `pop|none|karaoke`, separate 16:9 and 9:16 variants): `punch_yellow`, `clean_white`, `boxed_dark`, `minimal_lower`.
**Visual treatments** (`visual_treatments.yaml`: zoom, cut length range, colour filter, vignette, grain): `dark_slowzoom`, `handheld_grit`, `clean_bright`, `cinematic_teal`.
**Chapter cards** (`chapter_cards.yaml`): 3 styles, ≤ 1.5 s, no sound.
**Thumbnail templates** (`thumbnails.yaml`): 3 layouts, bundled `Anton` font, ≤ 4 words, high-contrast frame pick by luminance variance.
**Clip rules:** §7.4.

---

## 18. YouTube safety & growth playbook (enforced by WP-H)

### 18.1 The money math (plan for it honestly)
- Full ad revenue currently needs **1,000 subscribers + 4,000 public watch hours in 12 months or 10M Shorts views in 90 days**. A 500-subscriber tier (3,000 hours or 3M views) unlocks fan funding and shopping.
- **Reported for 1 Feb 2027** (third-party sources; confirm on YouTube's own help page before relying on it): new applicants need **8,000 watch hours or 20M Shorts views**; existing partners need **10M Shorts views per rolling 90 days** to earn from the Shorts pool; Shorts watch time does not count toward the hours path.
- Shorts pay far less per view than long-form: commonly cited Shorts RPM is a few cents per 1,000 views versus dollars per 1,000 for long-form.
- **Mid-roll ads need videos of 8 minutes or longer.**
- **Strategy built into Nightshift (master-first, §7):** every story becomes a **10–14 min long-form master** (mid-roll eligible, builds watch hours) plus **4–6 Shorts** (discovery, subscribers). The **watch-hours path is the primary plan**; Shorts views are a bonus, especially after the 2027 change.

### 18.2 Policies Nightshift is designed around
| Policy | What it says | Nightshift's countermeasure |
|---|---|---|
| **Inauthentic content** (monetization) | No pay for generic, repetitive or template-based content; judged on the finished video, tool-agnostic | Variation engine (§8), critic loop + calibration, combo ledger, owner's editorial direction, human review |
| **Spam & deceptive practices** (Community Guidelines) | Bans posting the same content repeatedly across channels, and mass-uploading scraped content | Never upload the same render twice; original stories only; per-channel distinctness; similarity checks |
| **Originality ranking** (Oct 2026) | Original Shorts get more visibility; reposts limited | Original writing, no reuploads, no compilations of others' clips |
| **Altered/synthetic disclosure** | Required when AI content could be mistaken for real footage of a real person, place or event | Disclosure decision logic (§18.7) |

**Known weakness, stated plainly:** AI narration over stock footage is the visual fingerprint of channels removed in the reported January 2026 sweep. Nightshift's differentiators are the writing quality, the voice, honest metadata and variety. Visuals are the weakest layer and the first thing to upgrade after launch (custom B-roll, generated imagery, or on-brand motion graphics).

### 18.3 Content safety rules (`contracts/policy/unsafe_topics.yaml`)
Blocked from ideas and stories, and re-checked at both gates:
- Real, named people (living or dead), real brands as villains, real crimes or tragedies presented as fact.
- Minors in any sexual, violent or endangering context.
- Graphic gore, self-harm/suicide methods, instructions for weapons or drugs, hate or harassment of protected groups.
- Medical, legal, financial or political advice.
- Content imitating news reports or emergency alerts.

Horror and dark themes are allowed **as fiction** with implied, not graphic, violence. The `mature` audience adds an "age-restriction suggested" note when the classifier flags intensity.

### 18.4 Honest metadata rules
- **No "true story", "real", "happened to me" claims** in titles, descriptions or tags for fiction. Every description carries "Original fiction." when `fiction_label: true`.
- The hook and title must match what actually happens (judged by `policy_check.md`). No bait.
- Tags must be relevant. No unrelated trending tags or competitor channel names.
- Thumbnails must reflect the actual story.

### 18.5 Retention design
**Shorts**
- The main distribution gate is the **viewed vs. swiped-away** ratio. Commonly cited targets: **≥ 70 % viewed**; **below 50 %, reach stops.**
- The **hook line is spoken within the first 1.5 s**, first caption at 0.0 s. No title cards, no fades.
- **Length:** buckets 20–35 s, 35–60 s, 60–90 s; the learning loop picks per channel.
- **Loops:** single-part stories end with a line that flows back into the opening line.
- **Pacing:** a visual change every 1.5–3 s; captions 2–4 words with the active word highlighted.
- **Derived Shorts must stand alone:** freshly written and voiced hook, fresh vertical edit, closing line to the master.

**Long-form**
- Cold open is the story's strongest hook (no "welcome back", no channel intro); the title promise is paid off in the first 30 s.
- Chapter cliffs every 1.5–2.5 min; chapter cards ≤ 1.5 s.
- Shot length 5–9 s; one chapter card between chapters; captions on (clean style) because much long-form is watched muted on mobile.
- Mid-roll suggestions only at chapter breaks, never in the first or last 2 minutes.
- Runtime never below **8:30**.
- Thumbnails: 3 variants; reviewer picks one.

### 18.6 Cadence governor (per channel)
| Channel stage | Rule |
|---|---|
| Weeks 1–2 (new) | 1 master/week; its Shorts released 1/day |
| Weeks 3–6 | 2 masters/week; up to 2 Shorts/day if the last 14 days' average viewed rate ≥ 60 % (from imported analytics; without analytics, stay at 1/day) |
| Any time | Never more than 3 Shorts/day or 3 masters/week; publish times jittered ±45 min inside `publish_window` |
| Release order | Master first; its Shorts over the next 2–4 days, interleaved with other masters' Shorts so consecutive Shorts never come from the same story |
| Quality drop | If the 7-day viewed rate falls under 50 %, drop to 1 Short/day and raise `critic_bar` by 0.5 |
| Paused | `status: paused` channels are skipped by `--daily` |
| Multi-channel | Max 3 channels per install; each must pass the distinctness check |

### 18.7 Disclosure & settings logic (written onto each upload sheet)
- **Altered/synthetic content:** `No` by default. AI voiceover counts as production assistance; stock footage is real footage used as illustration. Flip to **`Yes`** if anything could be mistaken for real footage of a real person, place or event (story framed as real, or a real place name as the setting). When in doubt the sheet says `Yes`.
- **Made for kids:** `No` unless the channel profile says otherwise.
- **Age restriction:** suggestion only, per §18.3.

### 18.8 Policy gate checks (text gate = M9, final gate = S4)
| Check | Gate | Fails when | Code |
|---|---|---|---|
| `unsafe_topics` | text | LLM classifier flags any §18.3 item | E601 |
| `real_entities` | text | spaCy `PERSON`/`ORG`/`GPE`/`EVENT` entity matches `real_entities_block.txt` or is a multi-token PERSON not in `real_entities_allow.txt` (common first names) | E602 |
| `misleading_claims` | text | Any `misleading_claims.txt` phrase ("true story", "real footage", "caught on camera"…) in title/description/tags/hooks | E603 |
| `hook_title_honesty` | text | Judge says the hook or title promises something the story doesn't deliver | E604 |
| `self_similarity` | text | Embedding similarity > 0.85 to any of the channel's past stories, or > 0.80 to another channel's | E605 |
| `metadata_spam` | text | Tag count/length limits exceeded, or judge marks tags irrelevant | E606 |
| `standalone_short` | text | Judge says a selected Short moment doesn't make sense without the master | E607 |
| `render_uniqueness` | final | Perceptual hash (pHash of 10 sampled frames) too close to any prior render on any channel. A Short may share narration with its parent master but must differ in visuals and include its new hook | E611 |
| `long_runtime` | final | Master < 8:30, or a mid-roll suggestion in the first/last 2 min | E612 |
| `clip_reuse` | final | Violates §7.4 rules | E613 |
| `audio_only_voice` | final | Any non-voice audio track present (no music by design); checked by stream count and spectral flatness on silent stretches | E614 |
| `disclosure_advice` | final | Always runs; writes the §18.7 answers | — |

A failure moves the item to `rejected\` with the reason, regenerates once from the earliest affected stage, and logs the code. Second failure ends the job.

### 18.9 Learning loop (local)
1. Export analytics from YouTube Studio as CSV (Content → Shorts and Videos, with: views, viewed vs swiped away, average % viewed, average view duration, likes, comments, shares, subscribers).
2. `studio learn --import <csv>` matches rows to outputs via recorded URLs, else exact title match; unmatched rows are listed.
3. Per-channel reweighting of hooks, structures, length buckets, caption styles, visual treatments and title formulas by **viewed % × average % viewed** (Shorts) and **average view duration ÷ runtime** (masters), exponentially smoothed. Bounded: no option's weight below 5 %.
4. `studio status` shows top/bottom performers and what changed since the last import.

### 18.10 Human-in-the-loop (required for safety and originality)
- Every video passes `studio review` before upload. The reviewer sees the story, policy notes and disclosure advice.
- The channel's `voice_notes`, niche, exemplars and `creator_note` are written by the owner. That human editorial direction is the channel's point of view.

### 18.11 Incident procedure
If YouTube issues any warning, strike or monetization notice: `studio pause <slug> --reason "<notice text>"` records an incident, stops nightly runs for that channel, and `studio status` shows it. Before `studio resume`, the owner reviews the last 10 outputs against §18.2–18.4 and raises `critic_bar` or trims pools as needed. The procedure is printed on the README.

### 18.12 Go/no-go milestone (week 8 after first upload)
Continue if **either**: Shorts 7-day viewed rate ≥ 55 % and subscribers growing week over week, **or** masters' average view duration ≥ 3:00 and watch hours on a pace to reach 4,000 within 12 months. Otherwise stop, change niche/voice/visual approach, and re-run calibration. `studio status --milestone` computes this from imported analytics.

---

## 19. Nightshift Clipper — clips from videos you own (WP-I)

### 19.1 What it does
Takes one long video the owner has rights to (stream VOD, podcast episode, lecture, talking-head recording), transcribes it, picks the moments that stand alone, and renders each as a vertical 1080×1920 clip with burned-in captions and a layout template chosen for the kind of video. Output goes through the same QC, text policy gate (metadata only), Outbox and review queue as everything else. **Rights:** the Dashboard asks the user to confirm they own or are licensed to use the video before a job starts; the confirmation is stored on the `sources` row.

### 19.2 Pipeline (per source video)
| # | Stage | Tenant | Output |
|---|---|---|---|
| C1 | Import: copy or link the file into `imports\clipper\<source_id>\source.<ext>`, ffprobe (duration, fps, size, audio streams), sha256; reject > 4 h or no audio (`NS-E405`) | none | `source.json` |
| C2 | Audio extract to 16 kHz mono WAV | none | `audio.wav` |
| C3 | Transcribe with faster-whisper (`distil-large-v3`, VAD filter on, word timestamps) → `transcript.json`; speaker-change heuristic: a new segment after ≥ 1.2 s pause or a question/answer boundary starts a new `speaker` index (visual refinement in C5) | `whisper` | `transcript.json` |
| C4 | **Select**: `clip_select.md` (thinking on) over the transcript in windows of ≤ 12 min with 2 min overlap; returns candidate moments 20–60 s (configurable) with title, why, score; merge overlaps, snap edges to sentence boundaries from `words`, drop candidates whose text is mostly filler (< 2.2 words/s or > 40 % "um/uh/like" tokens), keep the top `max_clips` by score with ≥ 60 s spacing between clip starts | `llama` | `clip_plan.json` |
| C5 | **Detect**: sample frames at 1 fps inside the chosen ranges (plus 10 frames spread over the whole video for the facecam scan). YuNet face boxes per frame; **facecam scan**: find a stable rectangular region (consistent edges across sampled frames, aspect 4:3–16:9, 8–30 % of frame area, in a corner) → `facecam`. Template `auto` decides: facecam found → `gaming_facecam`; ≥ 2 stable faces → `podcast_stacked`; 1 stable face → `talking_head`; else `center_crop` | none (CPU) | `clip_layout.json` |
| C6 | **Captions**: words from the transcript in the clip range → ASS with the channel's caption style (same builder as §7.6 step 4), 2–4 words on screen, active-word highlight | none | `captions\c<k>.ass` |
| C7 | **Cut + render**: per clip, build one FFmpeg filter graph from the template (below), trim, scale, crop, stack, burn captions, audio `loudnorm` to −14 LUFS, `h264_nvenc`/`libx264`, faststart | NVENC | `clips\c<k>.mp4` |
| C8 | Clip SEO: `clip_seo.md` → title (≤ 70), description (honest, includes "Clip from: <source title or filename>"), tags, hashtags incl. `#Shorts`; policy text gate checks `misleading_claims`, `metadata_spam`, `real_entities` **on metadata only** (the spoken content is the owner's) | `llama` | `seo_clips.json`, `policy_text.json` |
| C9 | QC (§7.7 Short targets) → Outbox `clip-<source_id>\c<k>.mp4 + .json + .txt` → `outputs` rows (`kind="clip"`) | none | Outbox |

### 19.3 Templates (`ns_studio/presets/clip_templates.yaml`)
| Template | Layout (1080×1920) | When |
|---|---|---|
| `gaming_facecam` | Facecam region scaled to 1080 wide on top (height from its aspect, max 640 px); gameplay below: source frame minus the facecam region, scaled to fill the remaining area with a centre crop; captions on the seam | Facecam detected |
| `podcast_stacked` | Two speaker crops (face-centred, 1080×960 each) stacked; the speaker currently talking (visual heuristic: largest mouth-region motion in the last 1 s, else the larger face) gets a subtle 2 px border; captions over the lower third | ≥ 2 stable faces |
| `podcast_single` | One face-centred crop with smooth tracking (box smoothed over 1 s; re-centre only when the face leaves the middle 40 %), full 1080×1920 | ≥ 2 faces but the user picks single |
| `talking_head` | Same tracked crop as `podcast_single` | 1 stable face |
| `center_crop` | Centre 9:16 crop over a blurred, darkened full-frame fill | No faces, screen recordings, slides |
| `screen_plus_cam` | Facecam top (as gaming) + full source frame letterboxed below (for tutorials with a small cam) | User choice |

Each template defines: regions, caption anchor (`y%`), safe margins (top 220 px / bottom 320 px for YouTube UI), and whether to show a 1.2 s title card at 0.0 s (default off; the hook is the speech itself). Caption styles come from `caption_styles.yaml`.

### 19.4 Detection details
- **Faces:** `cv2.FaceDetectorYN` with the pinned YuNet ONNX, input resized to 640 px wide, score ≥ 0.8. A face is **stable** when it appears in ≥ 70 % of sampled frames within ±15 % of frame width of a cluster centre.
- **Facecam:** Canny edges on 10 frames spread across the video → accumulate; find axis-aligned rectangles whose four edges persist in ≥ 8/10 frames; keep the candidate in a corner with the most faces detected inside it. If none, `facecam = null`.
- **Speaking heuristic (podcast_stacked):** for each stable face, mean absolute frame difference inside the lower third of the face box, smoothed over 1 s; the larger value is "speaking". Deferred (D12): audio diarization.
- Everything here runs on CPU and is tested on synthetic fixtures (`testsrc2` + a drawn rectangle containing a moving ellipse "face").

### 19.5 Clip selection prompt (`clip_select.md`, thinking on, schema `clip_plan`)
Given transcript segments with timestamps, the kind of video (detected or chosen), and the channel's niche, return 3–10 candidate moments of 20–60 s that: start on a complete sentence that works as an opener, end on a payoff or punchline, need no outside context, contain no private information about third parties, and are not mostly filler. Each with `title` (≤ 70 chars, honest), `why` (one line), `score` (0–1), `keywords`. The judge rule from §16.1 applies: honesty beats flash.

### 19.6 Limits and performance
- Source ≤ 4 h; transcription on a 16 GB GPU runs at ≥ 20× real time with `distil-large-v3` (expected; measured by `ns verify`).
- Target: a 60 min podcast → 6 clips in ≤ 12 min wall time on the 32 GB profile.
- Clips from one source are spaced ≥ 60 s apart and never overlap.

### 19.7 Errors
`NS-E405` unsupported source (too long, no audio, unreadable) · `NS-E406` transcription produced < 30 words (silence/music) · `NS-E407` no clip candidates passed the filters · `NS-W402` requested template not applicable (facecam not found), fell back to `center_crop`.

---

## 20. Nightshift Dashboard — local web UI (WP-J)

### 20.1 Shape
`ns_dashboard` is a FastAPI app on **127.0.0.1:8100** serving a static single-page UI (vanilla HTML/CSS/JS from `ns_dashboard/static/`, no bundler) and a JSON API. It is started by the supervisor as service `dashboard` (not a GPU tenant) and opened by `Nightshift Dashboard.lnk`. No authentication: it binds to localhost only and refuses non-loopback `Host` headers.

### 20.2 Pages
| Page | Shows | Actions |
|---|---|---|
| **Status strip** (every page) | Engine state (`starting` / `idle` / `loading <service>` with elapsed seconds / `ready` / `job running: <stage>`), VRAM owner, next scheduled run | Warm up, Quit Nightshift (confirms; cancels a running job after 30 s grace) |
| **Overview** | Tonight's schedule per channel, last run summary, Outbox counts (pending / approved / rejected), service states, disk free, incidents | Run now (channel, format), Pause/Resume channel, Stop all |
| **Review** | Queue of pending outputs (long, short, clip) with thumbnail, duration, title; video player (HTTP range streaming), story text / transcript excerpt, policy notes, disclosure advice, upload sheet copy buttons | Approve, Reject with reason, Mark uploaded (URL), Open folder |
| **Clipper** | Drop zone (drag and drop or file picker), rights confirmation checkbox, channel and template selectors with template thumbnails, options (max clips, min/max seconds, caption style); job list with progress bar and stage name; finished clips preview grid | Start job, Cancel, Re-run select with different settings |
| **Channels** | Channel cards; profile editor (every §11 field with validation), voice picker with preview playback, pool editor with distinctness warnings | Add, Edit, Validate, Pause |
| **Runs & Logs** | Runs table (started, finished, jobs, rejected, timings); live tail of `logs\*.jsonl` filtered by component/level | Download diagnostics bundle |
| **Learn** | Drop zone for the YouTube Analytics CSV; import result; top/bottom performers per pool; weight changes | Import |
| **Settings** | Runtime profile and flags (read-only unless unlocked), API keys (masked; set/replace via keyring), schedule time, update from zip | Save, Re-run verify, `ns update` |

### 20.3 API (all JSON, prefix `/api`)
`GET /status` · `GET/POST /channels`, `GET/PUT /channels/{slug}`, `POST /channels/{slug}/pause|resume|validate` · `GET /outputs?status=pending&kind=` · `GET /outputs/{id}` · `POST /outputs/{id}/approve|reject|uploaded` · `GET /media/{output_id}` (range streaming of the MP4) · `GET /media/{output_id}/thumb.jpg` · `POST /jobs/run {channel, format}` · `POST /jobs/clip` (multipart upload or `{path}` for a local file; `{channel, template, max_clips, min_s, max_s, rights_confirmed}`) · `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel` · `GET /runs` · `GET /logs/tail?component=&level=&n=` · `POST /learn/import` (multipart CSV) · `GET /learn/summary` · `GET/PUT /settings` · `POST /settings/keys` · `POST /diagnostics` · `GET /templates` (clip templates with preview PNGs) · `GET /voices`, `POST /voices/preview`.

### 20.4 Job worker
A single background thread drains `dashboard_jobs` (FIFO). Job kinds: `run` (studio job via the orchestrator), `clip` (§19 pipeline), `learn`, `verify`. Progress is written per stage to `dashboard_jobs.progress/message`; the UI polls `GET /jobs/{id}` every 2 s. The worker acquires GPU tenants through the supervisor like the nightly task does; if the GPU is busy with a nightly run, the job waits and the UI says so. Cancel sets a flag checked between stages.

### 20.5 Build notes
- UI files: `index.html` (shell + nav), `app.js` (hash router, fetch helpers, one module per page), `app.css` (tokens on `:root`, light/dark via `prefers-color-scheme`). No external CDN: the page must work offline.
- Video streaming implements `Range` requests (206) so the browser player can seek.
- Uploads stream to `imports\clipper\<source_id>\` in 8 MB chunks; the UI shows upload progress separately from job progress.
- All destructive actions (reject, cancel, stop all) confirm in the UI.
- Tests: FastAPI test client for every route with a fake job worker; a `--fake-services` clip job through `/api/jobs/clip` with the synthetic fixture.

---

## 21. Install sources, dependencies and on-disk layout (what the installer actually pulls)

Everything below is pinned in `manifest.json` (exact versions, URLs, SHA256) by `tools/resolve_manifest.py` in Slice 0 Task 14 and verified by `--check`. This section is the human-readable list; the manifest is the machine-readable truth. Nothing is bundled (§0 rule 1).

### 21.1 System tools (`stage1.ps1`, via winget; direct links shown when winget is unavailable)
| Tool | winget id | Direct fallback | Used for |
|---|---|---|---|
| Git | `Git.Git` | https://git-scm.com/download/win | `ns update`, future `git pull` |
| Windows Terminal | `Microsoft.WindowsTerminal` | Microsoft Store | Wizard and launchers (optional; falls back to conhost) |
| FFmpeg (full build) | `Gyan.FFmpeg` | https://www.gyan.dev/ffmpeg/builds/ | Render, probe, loudness, captions (`libass` included in the full build) |
| uv | manifest `bins.uv` zip from https://github.com/astral-sh/uv/releases | `winget install astral-sh.uv` | Python install + venvs; no system Python needed |
| Python 3.12 | `uv python install 3.12` (uv-managed, under `%APPDATA%\uv\python`) | python.org 3.12 installer | All venvs |
| NVIDIA driver | — | https://www.nvidia.com/Download/index.aspx | Must report CUDA ≥ 12.8 in `nvidia-smi` (`NS-E103`) |

### 21.2 Binaries (`s02_download_bins`, into `%NIGHTSHIFT_HOME%\bin\`)
| What | Source pattern | Notes |
|---|---|---|
| llama.cpp CUDA build | `https://github.com/ggml-org/llama.cpp/releases/download/<tag>/llama-<tag>-bin-win-cuda-12.8-x64.zip` | Tag pinned to a release with MTP (`--spec-type draft-mtp`) support; unzip to `bin\llama\` |
| cudart for llama.cpp | `https://github.com/ggml-org/llama.cpp/releases/download/<tag>/cudart-llama-bin-win-cuda-12.8-x64.zip` | Same tag; DLLs next to `llama-server.exe` |
| llama.cpp CPU build | `…/llama-<tag>-bin-win-cpu-x64.zip` | Only for `cpu-dev` and as the embed server fallback |
| qwentts.cpp CUDA build | pinned release asset from the `qwentts.cpp` GitHub repo (Windows x64 CUDA zip); if the project publishes no Windows CUDA binary, Slice 0 Task 14 records `bins.qwentts = null` and the installer uses the PyTorch fallback path (§2) | `bin\qwentts\` |
| OpenCode (optional) | `https://github.com/anomalyco/opencode/releases/download/v<ver>/opencode-windows-x64.zip` | `bin\opencode\` |
| YuNet face detector | `https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx` | `models\cv\yunet.onnx` (~230 KB) |

### 21.3 Models (`s03_download_models`, Hugging Face `https://huggingface.co/<repo>/resolve/<revision>/<file>`, public repos, no token; resumable with HTTP Range; SHA256 from the HF tree API)
| Logical | Repo / file | Folder | Size | When |
|---|---|---|---|---|
| `llm` (small, default) | `Qwen/Qwen3-14B-GGUF` Q4_K_M (or a Qwen3.6 14B-class GGUF if Task 14 finds one) | `models\llm\` | ~9 GB | always |
| `llm` (balanced) | `mudler/Qwen3.6-35B-A3B-APEX-MTP-GGUF` I-Mini | `models\llm\` | ~14 GB | `llm_tier: balanced` |
| `llm` (best) | `unsloth/Qwen3.6-35B-A3B-MTP-GGUF` UD-Q4_K_XL | `models\llm\` | ~21 GB | `llm_tier: best` |
| `story_llm` (opt-in) | `Youssofal/Qwen3.6-35B-A3B-Abliterated-Heretic-GGUF` Q4_K_M | `models\llm\` | ~20 GB | two-model mode only |
| `embed` | `nomic-ai/nomic-embed-text-v1.5-GGUF` Q8_0 | `models\embed\` | ~150 MB | always |
| TTS talker, presets | `Serveurperso/Qwen3-TTS-GGUF` 1.7B CustomVoice Q8_0 | `models\tts\` | ~2.1 GB | always |
| TTS talker, custom voices | same repo, 1.7B Base Q8_0 | `models\tts\` | ~2.1 GB | always |
| TTS codec tokenizer | same repo, tokenizer Q8_0 | `models\tts\` | ~0.2 GB | always |
| TTS talker, voice design | same repo, 1.7B VoiceDesign Q8_0 | `models\tts\` | ~2.1 GB | on demand (first "design a voice") |
| Whisper | `Systran/faster-distil-whisper-large-v3` (CTranslate2 int8) | `models\whisper\distil-large-v3\` | ~1.5 GB | always |
| spaCy | `https://github.com/explosion/spacy-models/releases/download/en_core_web_md-<ver>/en_core_web_md-<ver>-py3-none-any.whl` | studio venv | ~40 MB | always |
| PyTorch fallback set | `Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice`, `-Base` safetensors | `models\tts\hf\` | ~7 GB | only if `qwentts.cpp` fails smoke |
| `cpu-dev` set | `Qwen/Qwen3-1.7B-GGUF` Q4_K_M, 0.6B CustomVoice Q8_0 GGUF, `Systran/faster-whisper-tiny` | same folders | ~2.5 GB total | dev only |

**Default install total: ~15 GB.** Quantized GGUF/CTranslate2 files are already the compressed form; archiving them gains ≤ 3 %, so the bundle never zips models. For low-bandwidth sites, put the files in a folder (USB or Drive-synced) and set `models_source`; the installer copies and verifies instead of downloading.

### 21.4 Python environments (`s04_venvs`, `uv venv` + `uv pip install`, under `%NIGHTSHIFT_HOME%\venvs\`)
| venv | Installs | GPU DLL notes |
|---|---|---|
| `studio` | `-e <app_path>[studio]` (pydantic, httpx, jinja2, rich, typer, numpy, Pillow, spaCy + model wheel, faster-whisper, imagehash, keyring, opencv-python-headless) **plus** `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` wheels | faster-whisper (CTranslate2) needs cuBLAS 12 and cuDNN 9 DLLs on Windows. `ns_common.gpu_dlls.ensure_on_path()` prepends `<venv>\Lib\site-packages\nvidia\cublas\bin` and `…\nvidia\cudnn\bin` to `PATH` before `faster_whisper` is imported; `ns verify` checks that `WhisperModel(device="cuda")` loads (`NS-E112` with the fix hint otherwise) |
| `supervisor` | `-e <app_path>[supervisor]` (fastapi, uvicorn, psutil) and the dashboard deps; also hosts `ns_tts` (which only shells out to `bin\qwentts\`) | CPU only |
| `tts` (**fallback only**) | created by `s08_smoke` if `qwentts.cpp` fails: `torch==<pin>+cu128` from `https://download.pytorch.org/whl/cu128`, `qwen-tts==<pin>`, `soundfile`, then `-e <app_path>[tts] --no-deps` | `torch.cuda.is_available()` + capability ≥ (12,0) check (`NS-E108`) |

All venvs are created from the same repo checkout (`install_state.app_path`), so `ns update` can re-sync them with `uv pip install -e …` without re-downloading models.

### 21.5 On-disk layout the user sees
```
<NIGHTSHIFT_HOME>\            default <largest free SSD>\Nightshift ; chosen in the wizard; user env var NIGHTSHIFT_HOME
  bin\llama\  bin\qwentts\  bin\opencode\
  models\llm\  models\embed\  models\tts\*.gguf  models\tts\voices\<name>\  models\whisper\distil-large-v3\  models\cv\  models\spacy\
  venvs\studio\  venvs\tts\  venvs\supervisor\
  config\runtime.json  config\install_state.json  config\opencode.json  config\channels\<slug>.yaml
  data\studio.db  data\backup\  data\jobs\<job_id>\  data\calibration\
  cache\clips\  cache\search\
  imports\analytics\  imports\clipper\<source_id>\
  outbox\<slug>\<YYYY-MM-DD>\<job_id>\   ← the folder the user opens; "Nightshift Outbox" shortcut
  rejected\  logs\
<app_path>\                   the extracted repo (code only), e.g. D:\NightShift
```
Disk budget: models ~15 GB + venvs ~1.5 GB + working space ≥ 20 GB → the 40 GB check in §1.2; Balanced/Best LLM +14/+21 GB; two-model mode +20 GB; PyTorch fallback +10 GB.

### 21.6 Accounts and keys the user needs
| Key | Where to get it | Stored |
|---|---|---|
| Pexels API key (free) | https://www.pexels.com/api/ → "Get Started" | Windows keyring `Nightshift/pexels_api_key` |
| Pixabay API key (free) | https://pixabay.com/api/docs/ (shown after login) | keyring `Nightshift/pixabay_api_key` |
| Hugging Face | none needed (public repos) | — |
| YouTube | none needed in v1 (manual upload) | — |

### 21.7 Download behaviour
Every download: HEAD for size → stream to `<file>.part` with `Range` resume → SHA256 verify (`NS-E109`) → atomic rename. Concurrency 2 files at a time; progress bar per file and overall; a failed file retries 3 times with backoff; the step is re-runnable and skips verified files. `s03` prints the total to download and an ETA before starting and tells the operator they can close the window and re-run `INSTALL.cmd` to resume.

---

## Sources (verified 1 Nov 2026)

Model, runtime and tooling
- Qwen3.6-35B-A3B vs Gemma 4: https://artificialanalysis.ai/models/releases/comparisons/qwen3-6-35b-a3b-vs-gemma-4-26b-a4b
- unsloth MTP GGUF: https://huggingface.co/unsloth/Qwen3.6-35B-A3B-MTP-GGUF
- mudler APEX MTP GGUF: https://huggingface.co/mudler/Qwen3.6-35B-A3B-APEX-MTP-GGUF
- Abliterated GGUF: https://huggingface.co/Youssofal/Qwen3.6-35B-A3B-Abliterated-Heretic-GGUF
- MTP in llama.cpp (guide): https://mer.vin/2026/05/run-qwen-3-6-mtp-in-llama-cpp-faster-local-inference-with-built-in-speculative-decoding/
- MTP on RTX PRO 6000 (speedup figures): https://jarvislabs.ai/blog/qwen36-mtp-llamacpp-rtxpro6000
- MTP on a single L4: https://github.com/hanxiao/Qwen3.6-35B-A3B-MTP-L4
- llama.cpp MoE flags: https://www.developersdigest.tech/blog/llama-cpp-moe-offload-n-cpu-moe-vs-moe-cache-2026
- PyTorch on RTX 50-series: https://discuss.pytorch.org/t/pytorch-support-for-sm-120-nvidia-geforce-rtx-5060/220941
- Qwen3-TTS models: https://github.com/Usimian/Qwen3-TTS
- faster-qwen3-tts: https://pypi.org/project/faster-qwen3-tts/ and https://docsearch.algolia.com/mcp/docs/repo/andimarafioti/faster-qwen3-tts
- nomic-embed-text GGUF: https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF
- OpenCode Windows npm issue: https://github.com/anomalyco/opencode/issues/2447
- OpenCode config reference: https://learnopencode.com/en/appendix/config-ref

Stock footage
- Pexels API docs (200/h, 20k/month): https://www.pexels.com/api/documentation/
- Pexels license: https://help.pexels.com/hc/en-us/articles/360042295174
- Pixabay API (100/min, 24 h caching): https://pixabay.com/api/docs/
- Pixabay license: https://pixabay.com/service/license-summary/

YouTube policy and growth
- YouTube upload API: https://developers.google.com/youtube/v3/docs/videos/insert
- Spam & deceptive practices policy: https://support.google.com/youtube/answer/2801973
- YPP requirements (official): https://support.google.com/youtube/answer/72851
- YPP 2027 thresholds (third-party): https://vidiq.com/blog/post/youtube-partner-program-guide/ · https://www.subsub.io/blog/youtube-monetization-requirements · https://milx.app/en/trends/youtube-monetization-requirements-for-2026
- Inauthentic content clarification (Jul 2026): https://techcrunch.com/2026/07/20/youtube-clarifies-policies-around-ai-slop-and-upsetting-videos/ · https://creatorblade.com/blog/youtube-inauthentic-content-policy-2026-stay-monetized
- AI demonetization rules vs myths: https://ytgrowth.io/blog/youtube-ai-policy
- Oct 2026 originality update: https://async.com/blog/youtube-monetization-update/
- AI disclosure rules: https://air.io/en/youtube-hacks/youtube-ai-disclosure-in-2026-label-it-yourself-or-youtube-will
- AI voiceover = production assistance: https://shortsfast.com/blog/youtube-ai-content-disclosure-rules-2026/
- Shorts algorithm (viewed vs swiped): https://ytgrowth.io/blog/youtube-shorts-algorithm
- Shorts length study: https://piktochart.com/blog/how-long-youtube-shorts.md
- Mid-roll 8-minute minimum: https://fluxnote.io/guides/youtube-mid-roll-ads-minimum-video-length-8-minutes-2026
- Mid-roll placement: https://fluxnote.io/guides/youtube-mid-roll-ads-eligibility-2026
- Shorts "Related video" link: https://www.searchenginejournal.com/youtube-adds-shorts-links-limits-links-elsewhere/495463
- Related video not settable via API: https://help.vizard.ai/en/articles/13643189-can-i-add-a-link-to-my-long-form-youtube-video-when-publishing-shorts-via-vizard
- Repurposing long-form into Shorts: https://quso.ai/blog/youtube-shorts-algorithm
