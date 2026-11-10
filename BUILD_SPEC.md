# Nightshift — Build Spec v2 (final, for Claude Code, sliced build)

> **Nightshift** is a Windows one-click bundle that turns one RTX 5060 Ti (16 GB) PC into an **overnight YouTube story studio** that writes an original story, makes a **long-form video first, then cuts it into Shorts**, so one story earns on both formats. It also ships an optional **private local coding assistant** on the same model.
> The target device needs **no Claude account, no paid AI keys, and no cloud CLI**. Claude Code is only the *builder*.
> The build runs in **slices** (§12). Each slice is proven on the target PC before the next one starts. The main session owns every shared contract (§15). Worker agents build their packages in parallel inside a slice and never edit shared files.

**Changes from v1** (summary; details inline): **no-hardware build**: a `cpu-dev` profile runs the whole pipeline on the build laptop with tiny models, the installer is distributed through Google Drive and run by a remote operator with no developer present, and `ns doctor --bundle` ships diagnostics back · sliced build order replaces the one-shot parallel build · one LLM by default, abliterated model is opt-in · word budgets derived from runtime · Shorts selection by sentence span, timed after captions · combo-ledger window bounded by pool size · embedding + NER tools added to the stack · clip budget and relaxed reuse rules · long-form SEO rules split from Shorts · `story` schema and per-beat visual plan defined · QC targets set · captions aligned to the script, not ASR text · TTS sentence chunking · 16 GB RAM profile constrained · scheduler wake-to-run · calibration harness · pause/incident procedure · go/no-go milestone · two-machine dev workflow.

---

## A. Product description (also used verbatim as installer and README text)

### A.1 One line
**Nightshift works while you sleep: it writes an original story, turns it into a full long-form YouTube video, then cuts the best moments into Shorts that send viewers back to it. Everything runs on your own PC.**

### A.2 Installer welcome text (printed by the headless console path; the setup window shows a short welcome)
The setup window opens on a short welcome of its own (what Nightshift makes, what setup installs with sizes, the time; `installer/ui/src/copy.ts`, `welcome`). The text below is `installer/welcome.txt`: the headless console path (`-NoUi`, `NS_NONINTERACTIVE=1`, or an `answers.json` next to `INSTALL.cmd` unless `-Ui` is given) prints it as step `s00_welcome` (§13 D26).

> **Welcome to Nightshift**
>
> Nightshift sets up a story-to-video factory on this PC. It writes an original story, narrates it, adds captions and matching stock footage, and produces **one long-form video (10+ minutes, with chapters and ad-break points)** plus **several vertical Shorts cut from it**, each with its own hook. Everything lands in your Outbox, ready for you to review and post.
>
> Optionally, it also installs **Nightshift Code**, an AI coding assistant in your terminal, powered by the same model running on your graphics card. No account, no subscription, nothing leaves your PC.
>
> **What setup will do:** check your PC (graphics card, driver, memory, disk space), install the free tools Nightshift needs, download the AI models (~20 GB, or copy them from a folder you already have), tune them for your hardware, and run a quick test of everything. You'll answer a few questions along the way. You can close and re-run setup at any time; it picks up where it left off.
>
> **You'll need:** an NVIDIA RTX GPU with 16 GB, 60 to 72 GB of free disk space for a first install (60 GB with the Small model, 64 GB with the recommended Balanced one; setup shows the exact number for your PC and your choices, and asks for less when the model files are already on the drive), an internet connection during setup (or a folder with the model files), and, optionally, a free stock-footage API key from Pixabay (or Pexels, if you already have one). At least one is recommended; setup finishes without, and you can add it later in Settings. Setup shows you where to get them.

### A.3 How it installs itself
1. **Checks the PC**: Windows version, NVIDIA GPU, driver, RAM, free disk, internet, power settings. It tells you exactly what to fix if something is missing.
2. **Installs free tools**: the Visual C++ runtime, FFmpeg, Windows Terminal and Python (via uv). Stage 1 downloads the pinned Visual C++ runtime itself (no winget) and runs it with a Windows permission prompt; the setup window's tools step then installs what is still missing, trying winget first (FFmpeg, Windows Terminal, and the runtime when stage 1 could not install it) and falling back to a pinned direct download for the runtime and FFmpeg. Everything it already finds is skipped. Git is not needed: only the optional `ns update --git` uses it, on a cloned install, and on a clone without Git that command stops with an `NS-E702` hint (a zip install has no clone, so `ns update --git` there stops with `NS-E702` "is not a git clone"; use `--github` or an update zip).
3. **Asks you a few questions**: first channel name and niche, language, narrator voice (listen to samples), stock-footage keys (optional), how many videos per week, what time to run, whether to install Nightshift Code.
4. **Downloads the AI engine and models** from their official sources, verifying every file.
5. **Tunes itself** to your exact hardware for maximum speed.
6. **Tests everything** and shows a pass/fail checklist.
7. **Creates desktop shortcuts**, and a nightly scheduled task that can wake the PC if you answered yes to "Run Nightshift by itself every night?" (off by default; Settings switches it on later).

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
- **Nightshift Dashboard** (shortcut, opens in its own Nightshift window, or in a browser window when that cannot start): channels, the review queue with video preview and approve/reject, tonight's run and its log, the Clipper drop zone and templates, analytics import, pause/resume, and settings.
- **You** review each video (`approve` / `reject`) in the Dashboard and upload approved ones in YouTube Studio.
- Over time, you can feed it your YouTube Analytics export so it learns which hooks, styles and lengths work best for each channel.
- **Nightshift Code** (optional shortcut): opens a terminal coding assistant (OpenCode) on the local model. It reads, writes and edits code in your projects folder, fully offline.

### A.5 What it takes in
| Input | When | Required |
|---|---|---|
| Channel name, niche, language | Setup / `studio channel add` | Yes |
| Narrator voice: preset, described in words, or cloned from a 3–10 s clip **you have rights to** | Setup / anytime | Yes |
| Pexels + Pixabay API keys (free; at least one recommended; Pexels is not issuing new keys, so Pixabay is the one to get) | Setup, or Settings at any time | No |
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
| Driver | `nvidia-smi` reports CUDA ≥ 13.0 (driver 580+; the llama.cpp CUDA 13.4 build, the only one with Blackwell code); a pre-Blackwell card with CUDA 12.4–12.x is accepted with the CUDA 12.4 build | Below 12.4, or an RTX 50-series / RTX PRO card below CUDA 13.0: open the NVIDIA driver page, pause, re-check (`NS-E103`) |
| RAM | 32 GB recommended, 16 GB minimum | Selects the hardware profile (§3) |
| Disk | Free space for THIS install, worked out by `ns_common/diskneed.py` from the manifest and from what is already on the drive (order H-6): the bytes of every file setup still has to download or copy (nothing for a file already in place with a good `.ok` mark), the unpacked size of the programs and environments it still has to make, and a working reserve of 38 GB for videos (twelve weeks of the default cadence, rounded up). A first install of the Small tier on an empty drive comes out at 60 GB (about 14 GB of models, 7 GB of programs and environments, the reserve); with today's manifest Balanced is 64 GB and Best 72 GB (each tier adds exactly its larger model); a custom model replaces the tier's story model with its own size; a rerun, repair or update over a finished install needs what is missing plus 2 GB; a dev install needs 10 GB; +20 GB two-model mode. The setup window, the PC check and `NS-E104` say the number, what it is made of in one line, and how much is free; jobs ask the same helper (a Short about 100 MB, a 14-minute video with its Shorts about 3 GB) | The Checks screen is red (`NS-E104`) when the free space is under the number this install needs (on a locked resume the space setup already holds on that drive counts toward it, so a half-finished install is amber, not red); the Choices folder check and step `s01_questions` use the same number and block with `NS-E104` (§13 D23) |
| Disk type | NVMe/SSD for `NIGHTSHIFT_HOME` | Warn on HDD (`NS-W106`): load times will miss §14.6 |
| Network | `huggingface.co`, `github.com`, `download.pytorch.org`, `api.pexels.com`, `pixabay.com` and five more hosts setup downloads from (PyPI and its file host, the GitHub API, NuGet, Visual Studio downloads) are probed | Red (`NS-E105`) only when `github.com`, `pypi.org` or `files.pythonhosted.org` cannot be reached; any other unreachable host, Hugging Face, PyTorch, Pexels and Pixabay included, is a warning (an amber `NS-E105` row) and setup continues: with Hugging Face blocked, point Choices at a models folder or zip; a download that still cannot reach its site stops with `NS-E105` naming the host (§13 D28) |
| Power | Scheduled task can wake the PC; sleep allowed, hibernate discouraged | Set task `WakeToRun`; warn if "wake timers" disabled (`NS-W107`) |
| winget | `winget --version` works (App Installer present) | If missing: a warning (amber row), and only when FFmpeg or the Visual C++ runtime is also missing; setup downloads the pinned FFmpeg and Visual C++ runtime builds itself (pinned direct downloads, §21.1), with no page to open and no pause (§13 D27) |
| Long paths | Registry `LongPathsEnabled` = 1 (model file names and job paths can exceed 260 chars) | Set it (needs admin) or warn and keep paths short (`NS-W112`) |

**Blackwell:** PyTorch must be a **CUDA 12.8+ build (2.7+)**, installed from the cu128 (or newer) index URL.

### 1.3 Shipping and the first run (decision D1, §13)
- **The bundle is the repository.** `dev\make_bundle.ps1` produces `Nightshift-<version>.zip`: the full codebase (code, tests, contracts, prompts, presets, docs, plans, `CLAUDE.md`, `uv.lock`), `INSTALL.cmd` at the root, `answers.example.json`, and `OPERATOR.md`. No models, no binaries, no venvs (§0 rule 1). It is uploaded to a **Google Drive folder**.
- **The partner** extracts the zip to a folder of their choice (recommended `D:\NightShift`) and double-clicks `INSTALL.cmd`. That runs `installer\stage1.ps1` with execution policy bypassed for that process only, which installs tools, creates `NIGHTSHIFT_HOME` (by default `Nightshift` on the drive with the most free space, an SSD preferred; §5 `s01_questions`, §13 D29) for models, venvs, config and outputs, records the repo path as `app_path` in `install_state.json`, and runs the wizard. Every model and tool is downloaded from pinned sources on that machine.
- Setup needs **zero decisions** when `answers.json` is present next to `INSTALL.cmd`; questions are asked only for missing answers.
- At the end of setup, and on demand, **`ns doctor --bundle`** writes `%USERPROFILE%\Desktop\nightshift-diagnostics-<date>.zip` (logs, `install_state.json`, `runtime.json`, `verify-report.json`, `nvidia-smi` output, `pip freeze` per venv, the last 3 job directories without media) for upload to Drive if the developer is not at the machine.
- **The codebase stays on the work machine as the development checkout.** After the first run, further work (hardware gates, tuning, prompt iteration, Slices 2+ if they were not finished) continues there with Claude Code against the real GPU. `ns update <zip>` exists for the case where fixes are shipped from elsewhere: it replaces the repo contents (keeping `.git`, `config`, and untracked data), re-syncs venvs from `uv.lock`, and re-runs `ns verify --quick`.
- Secrets (Pexels/Pixabay keys) may be placed in `answers.json`; the wizard moves them into the Windows keyring and blanks them in the file.

---

## 2. Final stack

| Layer | Choice | Notes |
|---|---|---|
| LLM runtime | **llama.cpp `llama-server`**, official Windows CUDA release + cudart, build with merged MTP support (PR #22673 or later) | MoE offload, MTP, OpenAI-compatible API, `--alias` |
| LLM (`code` and `story`, **one file**) | **Balanced is the recommended default** (owner decision 2026-11-02): `mudler/Qwen3.6-35B-A3B-APEX-MTP-GGUF` I-Mini, ~14 GB, needs 32 GB RAM. **Small**, the fallback preselected under 32 GB RAM, is the **14B-class dense Qwen GGUF, Q4_K_M, ~9 GB** (`Qwen/Qwen3-14B-GGUF`; Slice 0 Task 14 swaps in a Qwen3.6 14B-class GGUF if one exists) and fits fully in 16 GB VRAM with 16K context. **Best** (`unsloth/Qwen3.6-35B-A3B-MTP-GGUF` UD-Q4_K_XL, ~21 GB) and a user-named **custom GGUF** (up to 15.5 GB) are opt-ins | Decision D13: Balanced for story and coding quality, Small as the lighter fallback |
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

Storage (default install, Balanced): LLM 14 GB + TTS 4.4 GB (two Q8_0 talkers + tokenizer) + whisper 1.5 GB + embed/face/spaCy 0.2 GB ≈ **20 GB of models**, venvs ~1.5 GB (no PyTorch). The Small fallback (9 GB LLM) is about 15 GB. On-demand extras: VoiceDesign +2.1 GB, Best LLM +9 GB over Balanced, two-model story LLM +20 GB.

---

## 3. Hardware profiles

| Profile | LLM file | Context | Strategy |
|---|---|---|---|
| **`gpu`** with `llm_tier: small` (the fallback when the PC has < 32 GB RAM; any RTX 16 GB, ≥ 16 GB RAM) | 14B Q4_K_M (~9 GB) | 16K | Fully on GPU, KV f16; one GPU tenant at a time (§0 rule 8) |
| **`gpu`** with `llm_tier: balanced` (**recommended default**) or `best` (≥ 32 GB RAM) | 35B-A3B MoE I-Mini (~14 GB) / UD-Q4_K_XL (~21 GB) | 16K / 32K | `--fit` on; tune `--n-cpu-moe`; KV q8_0 when needed; MTP if faster |
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
  INSTALL.cmd  UNINSTALL.cmd  # WP-A: double-click bootstrap / removal
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

**Stage 0 — `INSTALL.cmd`:** `powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\stage1.ps1" -AppPath "%~dp0."` and pauses on exit so the window stays readable; it does not pause in a run that says nobody is at the keyboard (`-NoUi`, `NS_NONINTERACTIVE=1`, or `-Answers` without `-Ui`), so a script that starts it never waits for a key (D-40).

**Stage 1 — `stage1.ps1`:** preflight (§1.2) → uv → Python 3.12 → Visual C++ runtime (stage 1 downloads the pinned build and runs it elevated, no winget) → setup server, whose tools step installs what is still missing (the runtime if stage 1 could not, FFmpeg, Windows Terminal: winget first, then a pinned direct download for the runtime and FFmpeg; Git is not installed, §13 D25) → create `NIGHTSHIFT_HOME` tree → bootstrap venv `venvs\studio` with `uv sync --locked --extra studio` from the repo → run `wizard.py [--answers <path>]`.

**Unattended mode:** the installer looks for `answers.json` next to `INSTALL.cmd`, then in `%NIGHTSHIFT_HOME%\config\`. Every question in `s01_questions` is answered from it when present; missing keys are asked interactively. Schema: `contracts/schemas/answers.schema.json` (`home_path?, models_source?, llm_tier:"small"|"balanced"|"best"="small" (the schema default is the safe fallback; the installer preselects "balanced", or "small" under 32 GB RAM), llm_custom:{repo,file,size}|null, channel{display_name,niche,language,seed_keywords[]}, voice{mode,speaker|description|ref_audio_path+ref_text+rights_confirmed}, pexels_api_key?, pixabay_api_key?, masters_per_week, run_time, install_code:bool, allow_cpu:bool`). A template `installer/answers.example.json` ships in the Drive folder.

**Stage 2 — `wizard.py`** (rich TUI; each step = `check()` / `run()` / `verify()`; every step writes its result to `install_state.json` so a crash or reboot resumes at the failed step):
| Step | Does |
|---|---|
| `s00_welcome` | Console path only (the setup window has its own short welcome, §A.2): shows A.2 text + the hardware check summary (incl. disk type, wake timers) |
| `s01_questions` | **Home folder** for models, venvs, config and videos (default: `<drive with most free space, SSD preferred>\Nightshift`, must pass the disk check of §1.2, the space that install needs; written to the user environment variable `NIGHTSHIFT_HOME`, so every later shortcut, task and shell sees it) · Channel (name, niche, language) · voice (preview 4 presets / design / clone, with a rights confirmation for clones) · Pexels + Pixabay keys (both optional, at least one recommended; validated live when given, with links to get them, §21.6) · masters/week (default 1 for a new channel, §18.6) · run time (default 03:00) · install Nightshift Code? (default yes) |
| `s02_download_bins` | llama.cpp + cudart, OpenCode (if chosen) |
| `s03_download_models` | LLM GGUF (or the custom GGUF named in `llm_custom`, downloaded with no SHA256 and flagged `NS-W113`), embed GGUF, TTS GGUFs (CustomVoice, Base, tokenizer), faster-whisper `distil-large-v3`, YuNet, spaCy wheel (resumable, SHA256). **Local source mode:** if `answers.models_source` (or the wizard's "I have the model files" option) points at a folder, every file whose name and SHA256 match is copied from there instead of downloaded; the rest is downloaded. |
| `s04_venvs` | `studio` (incl. spaCy, faster-whisper + cuBLAS/cuDNN wheels, OpenCV), `supervisor` (+ dashboard). No `tts` venv unless the PyTorch fallback is triggered (§2). |
| `s05_opencode_prewarm` | If chosen: run OpenCode once headless; confirm `local/code` |
| `s06_tune` | `ns tune` |
| `s07_configs` | runtime, OpenCode, channel profile, secrets (keyring) |
| `s08_smoke` | `ns verify --quick` (§12 Slice 5 gate list) incl. TTS engine selection |
| `s09_shortcuts_scheduler` | §15.8; task has `WakeToRun`, `StartWhenAvailable`, runs whether user is logged on or not |
| `s10_summary` | Status table, where things live, next steps (A.4); runs `ns doctor --bundle` and tells the operator exactly which file to upload to the Drive folder |

**Error UX:** code + plain message + fix hint from `errors.json` → `[R]etry · [S]kip (optional only) · [L]og · [Q]uit`. In unattended mode, a required step retries 3 times with backoff, then writes the bundle and exits non-zero with the code on screen. Resume state in `config\install_state.json`.

**Install-time verification of the untestable parts (§1.1):** `s08_smoke` tests `h264_nvenc` with a 2-second colour source and falls back to `libx264` in `runtime.json` (`NS-W401`) on failure; tests `qwentts.cpp` on CUDA with one sentence per installed talker and, on failure, creates the `tts` venv with PyTorch cu128 + `qwen-tts` (checking `torch.cuda.is_available()` and capability ≥ (12,0), else `NS-E108` with the exact cu128 reinstall command), downloads the original safetensors and switches `runtime.tts.engine` to `reference` (`NS-W301`); tests faster-whisper on CUDA (`NS-E112` with the cuBLAS/cuDNN fix hint on failure); for MoE LLM tiers tests `llama-server` with and without `--spec-type draft-mtp` and keeps MTP only if it is faster (`NS-W302` otherwise).

**`UNINSTALL.cmd` → `installer\uninstall.ps1`:** removes the scheduled task, desktop shortcuts, `venvs\`, `bin\` and the `NIGHTSHIFT_HOME` user env var; asks separately (default **No**) whether to delete `models\` and whether to delete `outbox\`, `data\`, `imports\`, `cache\`; prints what was kept and where. Idempotent. Also reachable from Dashboard → Settings → Uninstall (opens the console).

**`ns update`** (three sources, same safety): `ns update --github` asks the GitHub Releases API for the latest `v*` release, downloads `nightshift-app-<ver>.zip` + `.sha256` from https://github.com/AbdulrahmanAmer/nightshift/releases, verifies, and applies; `ns update <zip>` applies a file received by other means; `ns update --git [--channel stable|main]` is for an install that is a clone: it refuses when tracked files have local changes, runs `git fetch --tags --prune origin`, checks out (detached) the newest release tag (`stable`, the default) or `origin/main` (`--channel main`, which works with `--git` only), re-syncs the venvs, runs `ns verify --quick`, and goes back to the previous commit with `NS-E702` if a step fails (`ns_supervisor/update.py` `git_update`). It is an optional developer path: setup does not install Git. On a clone without Git on `PATH` the command stops with `NS-E702` and a hint to use `--github` or an update zip; on a zip install, which is not a clone, it stops with `NS-E702` "is not a git clone", §13 D25). Apply = extract to `<app_path>.new\` → copy `.git\`, `config\` and untracked data over → `uv pip install -e` per venv → swap directories keeping `<app_path>.prev\` (`ns update --rollback`) → `ns verify --quick` → re-register shortcuts and the task → show the changelog section for the new version. Settings copy (F-10a, F-10c): before the swap the updater (and INSTALL.cmd over an existing install) copies the settings files of `config\` to `<app_path>.prev-config.zip`, paired with `<app_path>.prev\` by a fingerprint, and `--rollback` puts them back with the program and names the files (what the owner changed after the update is lost, and said). An updater older than 0.1.15 takes no copy, so the first start of the new version takes it: every writer of a settings file calls `ns_common/first_start.py` `guard` first, which, once per process, sees `<app_path>.prev\` without a copy that belongs to it and copies the settings before anything changes them (writing over the zip only where an update may: never a whole copy of another program, one a rollback could not put back, or one that cannot be read); when that copy cannot be made the write goes on and one line is logged. A rollback without a usable copy keeps the files but removes every setting the older version does not know (`ns_common/config_compat.py` `NEW_FIELDS`, the one list of fields the strict settings models gained since 0.1.14) and names them, so the older version can start (a file it cannot change is named as one the older version may refuse); and a field in that list is written to its file only when it differs from its default. The rollback is crash-safe: it writes `<app_path>.rollback-pending.json` first, makes the settings readable by the older version BEFORE that program is put in place (it may have no rollback code), then swaps the folders; the next start of a version with this code (every settings writer, the engine start, `ns update`) finishes or undoes an interrupted rollback (`ns_common/rollback_intent.py`), and two processes starting at once take the first-start copy one at a time (`<app_path>.prev-config.zip.lock`). `ns update --check` only reports current vs latest; the supervisor runs it weekly and posts a notification, never auto-installs. The Dashboard's Settings → Update button calls the same code. The UI (`ns_dashboard/static`) and prompts/presets update this way too, so quality work on them can ship any time without touching the engine or models.

---

## 6. Runtime (WP-B, Slice 4)

- **Supervisor** (§15.3) handles start/stop, health checks, crash restart (max 3 in 10 min, then `error`), the **GPU lock**, and idle unload after 10 min. `embed` is CPU-only, started on demand, never locked.
- **Shortcuts** run launchers that acquire the needed service, wait for health, then open (§15.8).
- **Scheduled task** runs `studio run --daily`. If the GPU is held by `opencode`, it waits up to 2 h, then logs `NS-E201` and retries next cycle. If the home's drive has less room than a job writes (`ns_common/diskneed.py`, a Short about 100 MB), the nightly starts no job and writes nothing for it: the report lists the jobs as `no_room`, the morning note and the dashboard's Last night card say "not enough disk space" with the amounts, and the task exits 1 when nothing at all could be made for that reason (a night that made some videos exits 0).
- **Before the supervisor exists** (M1 development on a GPU machine) `dev\run_services_dev.ps1` starts `llama`, `embed` and `tts` in visible windows with the tuned flags. `ns_common` clients talk to fixed ports either way.

### 6.1 Process model (how models are loaded and unloaded)
- **Service table** from `runtime.json`: name → command line, port, health URL, GPU flag.
  - `llama`: `bin\llama\llama-server.exe -m <llm.gguf> --port 8080 --host 127.0.0.1 -ngl <n> -c <ctx> --alias code,story [-ctk/-ctv q8_0] [--n-cpu-moe N] [--spec-type draft-mtp --spec-draft-n-max K -np 1]`
  - `tts`: `bin\qwentts\qwentts.exe` with the talker + tokenizer GGUFs in server mode on 8091; if the binary has no server mode, `ns_tts` (in the supervisor venv) is the server and spawns `qwentts` per batch.
  - `embed`: `llama-server.exe -m <embed.gguf> --embeddings --port 8081 -ngl 0` (CPU).
  - `whisper`: no process; the studio worker loads `WhisperModel(..., device="cuda")` under the GPU lock and drops it on release.
- **`/acquire`**: FIFO `asyncio.Lock` for the GPU with timeout → if another GPU tenant is resident, terminate it first → `subprocess.Popen` with stdout/stderr to `logs\<service>-YYYYMMDD.jsonl` → poll `/health` every 500 ms until 200 or the health wait (`NS-E203`): **300 s on an SSD, 600 s on an HDD or an unknown drive**, read from `config\supervisor.json` (`health_timeout_s`), which setup (step s08) writes before `ns verify`; 300 s when the file is missing or its value is outside 10 to 3600 s (D31) → record pid, port, VRAM (`nvidia-smi --query-compute-apps`) → return endpoint.
- **`/release`**: lock released; idle timer armed; after 10 min without an owner the process gets `terminate()`, then `kill()` after 10 s.
- **Crash policy**: child exit → restarted on next acquire; 3 crashes in 10 min → state `error`, jobs fail `NS-E202`.
- **Guaranteed cleanup**: the supervisor creates a **Windows Job Object** with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and assigns every child to it, so if the supervisor exits for any reason (quit, crash, log-off, shutdown) all model processes die with it. No orphaned VRAM.

### 6.2 App lifecycle (open / warm-up / close)
| Event | What happens |
|---|---|
| **Open** (`Nightshift Dashboard.lnk`) | Launcher starts the supervisor if not running, as a minimized console window titled **"Nightshift Engine"**, waits for `/status`, opens `http://127.0.0.1:8100` as an app window (`msedge.exe --app=...` or `chrome.exe --app=...`, else the default browser). Nothing is loaded yet; opening is instant. |
| **Warm-up** | Dashboard status strip: `Engine starting` → `Services idle` → `Loading LLM (9 GB)…` with elapsed time from the acquire's health polling → `Ready`. Triggered by the first job or the **Warm up** button. |
| **Close** | Closing the browser tab stops nothing (it is only a page; the Engine window stays). **Quit Nightshift** (dashboard button), `Nightshift - Stop All.lnk`, or closing the Engine window → `POST /shutdown`: current stage finishes or is cancelled within 30 s, children terminated, VRAM freed, supervisor exits. |
| **Auto-exit** | Supervisor exits after 30 idle minutes unless a scheduled run is due within 60 min. |
| **Nightly** | The scheduled task starts the supervisor headless (no window, no browser), runs `studio run --daily`, and the supervisor exits when the run ends. If the dashboard is opened during a run, it attaches to the running supervisor and shows progress. |
| **OS shutdown / log-off** | Job Object kills children; a stage interrupted mid-way resumes from `stage_state.json` on the next run. |

Supervisor API additions (§15.3): `POST /shutdown {grace_s:30}` · `POST /warmup {service}` · `GET /events` (server-sent events: service state changes, the weekly update-check result and the end of the nightly run) which the dashboard relays to its status strip; job progress is not on this stream, it reaches the browser as `job` frames of the dashboard's own `GET /api/events` (§15.3, D32).

---

## 7. Studio pipeline — "master-first" (per channel, per job)

One job = **one story → one long-form master → 4–6 derived Shorts.** Write and voice the story once; render it twice (16:9 and 9:16).

**Why master-first:** mid-roll ads need videos of **8 minutes or longer**. Long-form builds the **watch hours** that Shorts can't count toward. Shorts bring discovery and feed viewers to the master through YouTube's **"Related video"** link.

### 7.0 Word and duration budgets (hard rules, in code)
- `wpm` comes from the pacing option (§17) adjusted by the voice's measured speed (updated after each job from actual audio).
- Master: `target_minutes` drawn from `long_minutes` (9–14) → `target_words` = the words whose speech **plus the silence the TTS service adds** fill `target_minutes` (`master_budget` in `ns_studio/text/budget.py`, `words_for_seconds` in `ns_common/pacing.py`). The silence is about 0.035 s per word (350 ms per sentence, 700 ms per paragraph), plus 1.2 s before each chapter's cliff line (instead of 0.35 s) and a 1.2 s title card between chapters. Plain `target_minutes × wpm` is **not** used: a 14-minute draw at that count rendered at 15:12. At 155 wpm, 10 min is **1,402 words** and 14 min is **1,970** (9 min = 1,259, 12 min = 1,686). Chapters: 5–7, each `target_words / n_chapters ± 20%`; at 155 wpm that is 5 chapters of about **252–394 words** over 9–14 min (decision D15, §13).
- **Floor:** rendered master ≥ **8:30**. Planning floor is 9:00 for buffer: `min_words` is the pause-adjusted word count of 9 minutes (1,259 at 155 wpm).
- After voicing (M10), measured duration < 9:00 → `expand.md` on the shortest chapter (+120–200 words), re-voice that chapter only, re-measure. Max 2 expansions, else the job fails `NS-E305`.
- Shorts: the `length_bucket` is the whole Short. Hook (≤ 12 words, spoken in ≤ 1.5 s at the front) + body + closing line (≤ 15 words) **equal** the bucket's words; the hook and closing are subtracted from the bucket, not added on top (decision D16, §13). A `shorts_only` Short: `short_budget` counts the bucket's words pause-adjusted (`seconds / (60/wpm + 0.035)`), subtracts the hook and closing words, floors the body at 40 words, and narrows the ±15 % limits so a story at either edge stays within 12 % of the bucket in seconds. A master-derived Short: `short_body_words` = `bucket × wpm / 60` − 12 (hook) − 12 (closing), at least 20. Validator checks measured audio against the bucket ±15%.

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
| M11b | **Shorts timing**: resolve each Short's sentence span to `start_s/end_s` in the master audio from `words\`; snap to sentence boundaries | F | none | `shorts_timing.json` (`shorts_plan.json` is left untouched, so a resume does not invalidate its own input; decision D17, §13) |
| M12 | Clips: landscape clips for the master, fetched per unique keyword **after** voice and captions (the shot slots need the caption timings, so M12 is not parallel with M10–M11). Vertical clips for the beats inside the selected Shorts are fetched later, in `shorts_derive` (S1/S2) | E | none | `clips\shots.json` (master), `clips\shorts_shots.json` (Shorts) |
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
- `--format series_compilation`: **dropped from this slice** (decision D18, §13). It would stitch an existing multi-part Shorts series into one long-form master (re-render 16:9 from the parts' narration and new landscape clips, chapter cards). It has no stages: the orchestrator still accepts the flag and errors `NS-E306` (not implemented), and the dashboard does not offer it.

### 7.4 Clip budget and reuse (replaces v1 §17 clip rules)
- **Master shot length:** 5–9 s (visual change at every beat or sentence, whichever is longer). 12 min ≈ **90–140 clips**.
- **Shorts shot length:** 1.5–3 s; a 50 s Short ≈ 20 clips, drawn from the vertical pool fetched for its beats.
- **Keyword dedupe:** `visual_plan` keywords are normalized (lowercase, lemmatized) and deduped per job; one API search (per source, per orientation) per unique keyword, page size 80. Typical job: **≤ 60 unique keywords → ≤ 240 API calls**, under Pexels' 200/hour when spread across the job (client enforces a token bucket: Pexels 180/h, Pixabay 90/min, and honours `429`).
- **Cache:** every downloaded clip is kept in `cache\clips\<source>\<id>.mp4` with metadata (`source,id,url,license,duration,w,h,fetched_at,keywords[]`) for 90 days. Cache hits cost no API call. Pixabay search responses are cached 24 h as required.
- **Reuse rules:** no clip twice in one video (unless the relaxation below applies) · no clip twice on one channel within **14 days** · no clip on two channels within **30 days** · a Short may use clips also used in its own master (different orientation anyway). Enforced from `clip_usage` in SQLite; a violation picks the next candidate, never fails the job unless the pool is empty (`NS-E402`, then the beat falls back to a mood-only keyword search). **Relaxation (decision D19, §13):** when every candidate for a beat is used or blocked, the picker first drops the 14/30-day windows ("earlier footage is reused"); if the pool is still too small it lets the same video repeat footage ("footage from this video is repeated"). Either way it logs `NS-W405` and marks those shots `relaxed` for the final gate, instead of failing the job after the writing and the voice are done. `NS-E402` is raised only when no relaxed pick exists.
- **Selection:** prefer ≥ 1080 p, duration ≥ shot length + 1 s, no watermark flag, no faces when `mood` is `intimate`/`horror` (Pexels/Pixabay don't expose this; we skip clips whose title/tags include `person, portrait, face`). Landscape in a vertical edit → centre crop over a blurred fill; portrait in a landscape edit is never used.

### 7.5 TTS chunking (WP-C + WP-E)
- Split narration into sentences (`split_sentences` in `ns_common/textstats.py`: a regex on `. ! ?` and the Arabic, Devanagari and CJK full stops, plus an abbreviation list such as `Mr`, `Dr`, `e.g`; not spaCy, decision D20, §13); merge sentences shorter than 25 chars with the next; cap a chunk at 280 chars (split on clause punctuation when longer).
- Batch all chunks of a job in one `/v1/audio/speech/batch` call; the server generates in order with a fixed seed per voice so timbre is stable.
- Stitch with 350 ms silence between sentences, 700 ms at paragraph/chapter breaks, 1,200 ms before a cliff line (marked in `story.json`). Trim leading/trailing silence per chunk (−45 dBFS threshold).
- Per chapter `loudnorm` to −16 LUFS integrated, −1.5 dBTP, then a whole-master pass to −14 LUFS.

### 7.6 Caption alignment (WP-E)
1. Run faster-whisper with `word_timestamps=True` on each chapter WAV.
2. Align ASR tokens to the script's tokens (normalized: lowercase, punctuation stripped, numbers spelled) with `difflib.SequenceMatcher`; matched script words take ASR times; unmatched runs are interpolated linearly between neighbours.
3. Emit `words\<chapter>.json` = `[{w, start, end, sentence_idx}]` using the **script's** spelling and punctuation.
4. Build ASS with 2–4 words on screen, active word highlighted per `caption_style`. Drift check: the median offset between the aligned caption word starts and the ASR word starts, taken over the matched words only (interpolated words have no ASR time and do not count), must be ≤ 120 ms (`DRIFT_LIMIT_MS` in `ns_studio/media/captions.py`), else `NS-E403`.

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
- Combo per video: `{hook, structure, pov_tone, pacing, caption_style, visual_treatment, title_formula, length_bucket}`. A master and its Shorts share `structure, pov_tone, pacing` (same story); each Short draws its own `hook, caption_style, visual_treatment, title_formula`, and its `length_bucket` is the one the Shorts plan gives that moment, fixed for the draw (`ns_studio/variation/short_draw.py` `draw_short_combos`, D37).
- Draws are weighted (weights updated by §18.9 learning), restricted to the channel's allowed pools.
- **Combo ledger:** no repeated full combo per channel within 30 days; no repeated `{hook, structure}` pair within the last `min(10, pairs − 1)` videos, where `pairs = |hooks| × |structures|`.
- **Shorts derived from a master** keep the master's `structure`, so the pair window holds for them only while some `{hook, structure}` pair with that structure lies outside it. When every hook of that structure is already inside the window (the fourth Short of a master on a channel with four hooks, the master's own pair counting as the first), the window is waived for that Short and only the 30-day full-combo rule applies (`ns_studio/variation/draw.py` `draw_combo`, D37). Earlier videos of the same structure inside the window use up hooks too.
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
| D13 | Download size | **~20 GB default (owner decision 2026-11-02: Balanced is the recommended tier)**: Balanced LLM (14), TTS CustomVoice + Base Q8_0 GGUF + tokenizer (4.4), one whisper (1.5), small extras. **Small (~15 GB)** is the fallback, preselected when the PC has < 32 GB RAM; Best (~22.9 GB LLM) and a user-supplied custom GGUF (up to 15.5 GB) are opt-in; VoiceDesign (+2.1) on demand; `models_source` folder mode for offline/pre-downloaded installs | Egyptian bandwidth; quantized GGUF is already the compressed form, zipping gains ≤ 3 % |
| D14 | TTS engine | `qwentts.cpp` (GGML, CUDA, no PyTorch) with PyTorch `qwen-tts` as automatic fallback | Removes ~2.5 GB of torch and ~6 GB of fp16 weights from the target |
| D15 | Master word budget (§7.0) | `target_words` subtracts the TTS pauses and chapter cards: at 155 wpm 10 min = 1,402 words, 14 min = 1,970; chapters about 252–394 words (5 chapters) over 9–14 min. Not `minutes × wpm` | A 14-minute draw at `minutes × wpm` rendered 15:12, over QC's 15:00 ceiling |
| D16 | Shorts word budget (§7.0) | Hook + body + closing = the bucket (pause-adjusted for `shorts_only`); hook and closing words are subtracted from the bucket, not added on top | Keeps Shorts inside the ±15 % QC duration band |
| D17 | Shorts timing file (§7.1 M11b) | `shorts_timing.json` holds `start_s/end_s`; M11b does not rewrite `shorts_plan.json` (M10's `expand_chapter` does rewrite it, `ns_studio/text/stages_master.py`), so after M11b `ShortsPlanItem.start_s/end_s` stay empty | Resume safety: the stage's own input stays unchanged |
| D18 | `series_compilation` (§7.3) | Dropped from this slice; `--format series_compilation` errors `NS-E306` and the dashboard hides it | No stages exist; the master-first and Shorts paths do not need it |
| D19 | Footage reuse relaxation (§7.4) | When every candidate is used or blocked, reuse earlier footage, then repeat footage inside the same video, with `NS-W405` (not `NS-E402`) | The first dev nightly died at the clips stage after the writing and the voice (HANDOFF v0.1.10); a repeated clip is better than a lost video |
| D20 | Sentence splitting (§7.5) | Regex plus abbreviation list (`ns_common.textstats.split_sentences`), shared by TTS chunking and `pause_plan`; not spaCy | One splitter for budget, voice and captions, no model load. spaCy is used only for the policy NER and keyword lemmas |
| D21 | Tenant grouping (§15.8) | Per job: consecutive stages of one job share one acquisition; a daily run does not batch stages across jobs | Simple and resumable per job (`stage_state.json`); the next job acquires the tenants again |
| D22 | Setup window: waits, install folder, wording | Setup window as shipped (`docs/design/installer-ui.md` is the detail): P1 gets 60 s to answer `/api/ping` (a "still waiting" note after 15 s, exit 14 after 60 s); the WebView2 host started with `-Role setup` is ready when `run/apphost-setup.json` is written, within 20 s; the install folder is printable ASCII only (`NS-E117`; llama.cpp and qwentts.cpp open files via the ANSI code page) and Downloads/OneDrive folders are refused; the hardware check has 11 rows (incl. "Visual C++ runtime"); the window and its dialogs call the console "Nightshift setup" | Spec text now matches the shipped behaviour (owner decision taken: keep the code) |
| D23 | Free disk space (§1.2, A.2, §5 s01, §21.5) | The space is worked out for each install (`ns_common/diskneed.py`, order H-6): the bytes of the files setup still has to download or copy, the programs and environments it still has to make, and a 38 GB working reserve for videos. 60 GB for a first Small install on an empty drive (Balanced 64, Best 72 with today's manifest); less when files are already in place with a good `.ok` mark or a smaller custom model is chosen; what is missing plus 2 GB for a rerun, repair or update over a finished install. The Checks screen (`installer/stage2/web/preflight.py` `raw_disk_rows`), the Choices folder check (`answers_view.py`) and `s01_questions` ask that one function and say the number (`NS-E104`), what it is made of and how much is free (on a locked resume the space setup already holds on that drive counts: `held_gb`); `stage1.ps1` only refuses a drive too small for the setup tools. A.2 and `welcome.txt` say about 60 GB, not ~40 GB | Owner decision 2026-11-09 (the code's figures are the truth), replaced the same day by order H-6: a fixed figure per tier refused installs that needed far less (a 1 GB test model with 49 GB free, a rerun over downloaded models). The Checks screen can now know the number because it reads what is on the drive and the choices made so far. The earlier fixed 74 GB (Balanced) and 81 GB (Best) were 60 plus the whole Balanced or Best model, which counted Small's 9 GB model a second time (setup downloads one story model); the sum gives 64 and 72, because the larger model adds 5.3 and 13.9 GB to Small's |
| D24 | Stock-footage keys (A.2, A.5, §5 s01, §21.6, J1 step 4) | Both keys are optional, at least one recommended (Pixabay; Pexels if the person already has a key). Setup finishes without any and reminds the person on three routes: the headless console prints `NO_KEY_NOTICE` (`s01_questions.py`); the setup window's Done screen shows the reminder when the result has `stockKeyMissing` (`controller.py`, `Done.tsx`, `copy.ts` `keyReminder`); and a resumed setup (locked answers) shows the "no-stock-key" notice (`controller.py` `NO_KEY_NOTICE`). The Choices step itself shows no notice, only the optional labels. A video job then stops up front with `NS-E502` until a key is added in Settings > Stock footage keys. A key that is given is still checked live | Owner decision 2026-11-09: Pexels stopped issuing new keys, so requiring both would block installs for nothing |
| D25 | Git (A.3 step 2, §5 Stage 1, §21.1, J1 step 3) | Git is not installed by setup; nothing in setup or at run time needs it (`installer/stage2/web/bootstrap.py` `TOOLS` has the Visual C++ runtime, FFmpeg and Windows Terminal). `ns update --git` is the optional developer path for a cloned install. `ns_supervisor/update.py` `git_update` checks for a clone first: the partner's zip install has no `.git`, so there it stops with `NS-E702` "is not a git clone; use --github or an update zip instead"; only a clone without Git on `PATH` gets `NO_GIT_HINT` (install Git, or use `ns update --github` or an update zip) | Owner decision 2026-11-09: Git's machine-wide installer would add an administrator prompt, and `ns update --github` and the update zip cover everyone else |
| D26 | Welcome text (A.2, J1 step 2) | The setup window opens on a short welcome of its own (`installer/ui/src/copy.ts` `welcome`: the plan, its size and the time). The full A.2 text, `installer/welcome.txt`, is printed by the headless console path as step `s00_welcome` (`-NoUi`, `NS_NONINTERACTIVE=1`, or an `answers.json` next to `INSTALL.cmd`; the `-Ui` switch opens the window even with an `answers.json`; `stage1.ps1` `$script:Headless`). The machine check table is the window's Checks screen; red rows block | Owner decision 2026-11-09: keep the window's design; `tests/test_installer_files.py` pins A.2 to `welcome.txt` |
| D27 | winget missing (§1.2, §21.1; `NS-E111`) | A missing winget is a warning (amber row), and only when FFmpeg or the Visual C++ runtime is also missing. Setup downloads the pinned FFmpeg and Visual C++ runtime builds itself (`manifest.json` `bins.ffmpeg`, `bins.vcredist`, SHA256 checked) as pinned direct downloads; no page is opened and setup does not pause. Windows Terminal is skipped when winget cannot install it. `NS-E111` stays in `errors.json` but is not raised | Owner decision 2026-11-09: setup must not depend on App Installer, and a pinned build is the same on every PC |
| D28 | Network reachability (§1.2) | Only `github.com`, `pypi.org` and `files.pythonhosted.org` block setup (`NS-E105`, `stage1.ps1` `CriticalHosts`). An unreachable `huggingface.co`, `download.pytorch.org`, `api.pexels.com`, `pixabay.com` or any other probed host is a warning and setup continues; a download that still cannot reach its site stops with `NS-E105` naming the host | Owner decision 2026-11-09: Hugging Face can be bypassed with a models folder or zip (`models_source`), PyTorch is needed only for the fallback (§2) and the keys are optional (D24) |
| D29 | Default install folder (§1.3, §5 s01, §21.5) | The default `NIGHTSHIFT_HOME` is `Nightshift` on the drive with the most free space (`stage1.ps1` `Select-DefaultHome`): drives with 60 GB or more free come first, and among those an SSD beats a bigger HDD; a drive under 60 GB ranks last (it is the pick only when no drive has 60 GB). The name is `Nightshift-home` when the folder holds someone else's files or the extracted program. Only when no drive gives a usable folder does stage 1 fall back to `%LOCALAPPDATA%\Nightshift\Home`; otherwise the default is not under `%LOCALAPPDATA%`. A `home_path` in `answers.json`, or the home of an earlier install (the user variable), wins. `ns_common/paths.py` falls back to `%LOCALAPPDATA%\Nightshift` only when `NIGHTSHIFT_HOME` is unset | Owner decision 2026-11-09: an install needs 60 GB or more (D23) and the system drive is often the smallest |
| D30 | NVIDIA driver and CUDA (§1.2, §21.1, §21.2) | CUDA 12.4 or newer is enough: 12.4 to 12.x takes the CUDA 12.4 llama.cpp build, 13.0 or newer the 13.4 build. An RTX 50-series / RTX PRO card needs CUDA 13.0 (driver 580 or newer) and below that stops with `NS-E103`; so does any card below CUDA 12.4. A version that cannot be read is `NS-W115` and uses the 12.4 build (13.4 on an RTX 50-series card). Code: `installer/stage2/sysinfo.py` `llama_pair_for`, `s02_download_bins.py` `choose_pair`, `stage1.ps1` | Owner decision 2026-11-09: only the 13.4 build has Blackwell code; every other card runs the 12.4 build on the widest range of drivers |
| D31 | `/acquire` health wait (§6.1) | The engine polls `/health` until 300 s on an SSD and 600 s on an HDD or an unknown drive; the value is `health_timeout_s` in `config\supervisor.json`, written by setup (s08) before `ns verify`; 300 s without the file | A cold load of the 14 GB story model takes about 30 s from an SSD and several minutes from a hard disk, longer than the 120 s this section used to state (`ns_supervisor/health_cfg.py`); owner decision taken: keep the code |
| D32 | Supervisor `/events` kinds (§6.2, §15.3) | The supervisor stream carries `service`, `update` and `run` frames; job progress reaches the browser as `job` frames of the dashboard's own `GET /api/events`, not from the supervisor | The dashboard owns the job store and publishes job records itself (`ns_dashboard/jobs.py`); no product code posts to `POST /events`; a browser consumer for the `job` frames is FINALIZATION F-B19; owner decision taken: keep the code |
| D33 | Voice library files (§15.4) | A voice folder holds `voice.json`, `sample.wav` and the engine's speaker files, listed in `voice.json` `files`: `speaker.spk`, `speaker.rvq` and `ref.txt` for qwentts, `speaker.bin` for the reference and fake engines | CR-C4-6 (`contracts/CHANGE_REQUESTS.md`) added `files` when qwentts needed three files; a voice without `files` is read as `speaker.bin`; owner decision taken: keep the code |
| D34 | Dashboard home, window and stage words (J2 steps 1 and 2) | J2 follows the pages as shipped. The shortcut starts the engine in a minimized "Nightshift Engine" window and opens the dashboard in its own window (the WebView2 host, `launchers/nightshift-host.ps1`; an Edge or Chrome app window when it cannot start, your default browser when neither is installed, `launchers/nightshift-dashboard.ps1`) on the **Queue** page, not an "Overview" (`ns_dashboard/ui/src/lib/app.tsx` `parseHash`). The top bar reads `Idle`, `Loading model`, `Working: <stage> (i/n)` or `Ready` (the engine's detail is the job's progress message `name (i/n)`; the engine object carries no step fields, so the bar shows no step count: `ns_dashboard/svc_engine.py` `engine`, `ns_dashboard/adapters.py`) (`ui/src/shell/Shell.tsx` `engineText`; `Services idle` is only the API's detail text, `ns_dashboard/svc_engine.py` `engine`), and shows the raw stage name with its count. There is no "Run now": **Create** > **A new story video** > **One Short** or **Full video + Shorts** > **Start** (`ui/src/pages/Create.tsx`). The setup summary (`installer/stage2/steps/s10_summary.py`, J1 step 7) still says "press Run now"; that is product text and stays open. The job's card names the stage in words (`ui/src/lib/format.ts` `STAGE_WORDS`; the card on Create adds `, step i of n` from the job's `step` and `steps`, the Queue card shows `i/n steps`): `Writing the story`, `Recording narration`, `Adding captions`, `Finding footage`, `Rendering`, `Checking quality`; a full video says `Writing the chapters` and `Rendering the full video` | Approved UI direction (the Queue home and the app window; docs/FEATURE_STATUS.md, the J2 rows). `tests/test_spec_operator_text.py` pins the words to the pages, and OPERATOR.md "Make your first video" uses the same words |
| D35 | Opening-line similarity (16.3) | A story whose opening line has embedding cosine similarity of 0.9 or more to any of the channel's last 50 openings (other jobs only) fails with `NS-E303` (`ns_studio/text/validators.py` `check_opening_similarity`, default `threshold=0.9`). It runs on chapter 1 of a master and on the story of a Short-only job (`validate_chapter`, `validate_story`), not on later chapters. In dev mode the list of past openings is empty, so the check never fires (`is_dev`: `ns_studio/text/stages_master.py` `_past_openings`, `ns_studio/text/stages_shorts.py`) | Commit 318655a (recorded in CHANGELOG.md) changed 0.8 to 0.9 because nomic similarities between two openings of the same niche sit around 0.8, so 0.8 failed a job after the writing for a false match, and skipped the check in dev because the fake model tells the same story every run (the second dev job failed `NS-E303` with cosine 1.00). The 0.9 is not backed by a measurement on real stories (none is recorded), so the spec states the value the code uses |
| D36 | Governor quality drop details (18.6) | The rejection rule needs at least 5 reviewed outputs among the last 10 (`REJECT_MIN_REVIEWED = 5`, `REJECT_WINDOW = 10`; approved, uploaded and rejected outputs count, pending ones do not) and a rejected share of 0.4 or more (`REJECT_RATE_LIMIT`): four rejections of four reviewed do not drop. While a drop lasts, the nightly planner raises `critic_bar` by 0.5 (`CRITIC_BAR_STEP`) up to 10, at most once per 7 days (`CRITIC_BAR_COOLDOWN_DAYS`), and writes an incident of kind `warning` (`ns_studio/channels/cadence.py` `rejection_rate`, `plan_channel`, `apply_quality_drop`, called from `ns_studio/daily.py`) | Package P41 records the extras instead of removing them: without a minimum one rejected output of one reviewed would be 100 %, and the cooldown keeps the bar from climbing every night while the drop lasts (`apply_quality_drop` docstring). `tests/test_cadence.py::test_quality_drop_by_viewed_rate_and_rejections`, `::test_apply_quality_drop_raises_bar_once_per_cooldown` |
| D37 | Derived Shorts and the pair window (8) | A Short derived from a master draws its combo with `structure`, `pov_tone`, `pacing` and `length_bucket` fixed (`ns_studio/variation/short_draw.py` `draw_short_combos`). `draw_combo` keeps the `{hook, structure}` window for it while a pair with that structure lies outside the window; when none does and a hook or structure is fixed it uses all pairs, so the window is waived for that Short and the 30-day full-combo ledger still applies (a pinned combo with no unused combination repeats with `NS-W325`). A draw with no hook or structure fixed still stops with `NS-E325` when every pair is inside the window | The comment in `draw_combo`: a Short derived from a master shares its structure, so the pair window cannot apply to it. Requiring more hooks than a master has Shorts, or a separate ledger for `s<k>` items, would be a product change (F-H82) |
| D38 | Incident procedure routes (18.11, J7) | Pause: `studio pause <slug> --reason "<notice text>"` (the reason is required, `NS-E326` without it; `ns_studio/cli.py` `pause_cmd`, `ns_studio/review.py` `pause`) sets the channel to paused, writes an incident with the text, and the nightly planner skips it (`cadence.plan_daily`); `studio status` lists the incident (`ns_studio/learn/status.py`). The Dashboard's Channels switch posts pause and resume with no body (`ns_dashboard/ui/src/pages/Channels.tsx`), so it records an incident with no note (`ns_dashboard/svc_channels.py` `_set_status`); the Assistant's `pause_channel` tool takes a `note` (`ns_dashboard/assistant/tools.py`). A story run request (`create_run`, `check_run`) for a paused channel is refused with `channel_paused` (`ns_dashboard/svc_jobs.py` `_run_params`); a Clipper request is not, because `_clip_params` only loads the channel. A story job already queued or scheduled (Create > Pick a time) does not start while its channel is paused: `JobWorker` asks `hold` (`ns_dashboard/channel_hold.py` `make_channel_hold`, wired in `ns_dashboard/handlers.py`) about every queued job of kind `run` or `series_part` before it starts one, reading the channel profile each time; a held job stays `queued`, its `message` reads "Paused: <channel> is paused since <time>; resume it to release this job" (the time is the channel's latest `pause` incident when no `resume` incident is newer; otherwise, or when it cannot be read, the message has no time and the job is held all the same), it is held also when its `schedule_at` has passed, and it starts by itself in its turn after the resume (the Channels pause and resume refresh the messages at once, `JobWorker.refresh_holds`; a pause by `studio pause` is seen on the worker's next pass). The worker marks a job `running` before it waits for the GPU gate (up to two hours while the nightly holds the GPU), so it asks `hold` once more when the job holds the gate (`JobWorker._requeue_if_held`): a job whose channel was paused meanwhile goes back to `queued` with the reason (`JobStore.requeue`, its place in line kept, no hook of its kind called, so a Series Workshop part stays with its job), and no video is made; until the gate opens the card keeps reading "Waiting for GPU (held by ...)", and a Cancel pressed meanwhile still wins. A Clipper job is not held, because a Clipper request for a paused channel is accepted. The Queue's Render jobs card prints it (Stage: Paused, `ns_dashboard/ui/src/pages/Queue.tsx` `RenderJobs`); the Create page does not print the message yet (it says "Waiting for the engine", or "Scheduled for <time>" while the set time is still ahead): that UI change is open. A running story job stops before its next stage: `ns_studio/orchestrator.py` `run_job` reads the profile again immediately before every stage, after any wait for the GPU or the embed lease (one call, `ns_studio/pause.py` `require_not_paused`, the one place every route passes: the dashboard's studio child, a Retry, a series part, `studio run`, the nightly), and also before it asks for the GPU tenant of a group of stages or for the embed service of a stage, so a job paused between two groups makes the engine load no model for it, and raises NS-E330 with the stage it did not start; the tenant of the group is released at once by the `finally` of that group, also when the wait for it ended after the pause (no stage of it runs), so a held job holds no lease; the stage under way is never cut (Cancel does that), and its outputs and `stage_state.json` stay, so continuing the job after the resume runs only the stages that did not run, once (a Clipper job, `clip`, is not stopped). On the dashboard such a job goes back to `queued` with everything its request still owes (`ns_dashboard/jobs.py` `owed_after_stop`, the one place that decides it, read by the worker and by Retry): `resume_job_id` is the studio job it was making and `count` every video still to make, that one included (nothing asked for is lost: `studio_bridge._owed_on_pause` adds `half_made` and `left` to the NS-E330, they cross the child as `studio_job.CARRIED`, and `run_story_jobs` with `resume_job_id` and a `count` continues that video and then makes the rest, none twice); a stop that carries no such account (no studio job was made yet) owes the request as it was asked. The videos made before the stop are kept in `made_job_ids`, so the waiting card and the finished card list every video of the request, not only those made after the resume (`JobWorker._whole`, `JobStore.outputs_of`). When the worker sees the pause too, the job waits, held: the message says how many ("(3 videos still to make, the first one half made)", `channel_hold.owed_of`), the Queue's Render jobs card shows it (Stage: Paused), and it continues by itself after the resume (`JobWorker._requeue_paused`, `JobStore.requeue`; a series part keeps its job id and folder, no hook of its kind is called). When the worker does not see the pause (the owner resumed before the worker looked at the stopped job), the job goes back as plain "Queued" with the same owed videos and continues in its turn: it does not fail. Only after `PAUSE_REQUEUE_LIMIT` (3) such turns in a row, which a person cannot cause (the studio would have to keep reporting a pause the worker cannot see), or for a worker that has no hold, does it end `failed` with NS-E330, so a job never goes round in a loop; its Retry then makes exactly what is owed. A Cancel always wins over putting a job back: `JobStore.requeue` refuses when Cancel was asked, and `JobWorker.cancel` looks at the row again after it set its flag, so a job that went back to waiting meanwhile is cancelled there. Cancel of a waiting request that had started says what it made and names the half-made studio job, whose folder is left as it is, not removed (`jobs.cancelled_text`: "Cancelled while it waited: 1 of 3 videos were made; the half-made one (<job id>) is not finished and its files stay in its job folder; no further video of this request is made"). `studio run` leaves the video half made, its record `failed` with NS-E330, and prints how to continue it (`studio run --resume <job id>`, `_say_how_to_continue`); with `--count` the jobs it had not started are not made. The nightly closes it as interrupted with its Queue card, as a killed job (`daily._paused_mid_job`; the card says the cause and a way back that works, "its channel was paused during the night's run. Resume the channel: the next nightly run then continues it, if the job started no more than 3 days before that run; or press Retry after the resume. A nightly run comes only while Automatic runs are on in Settings", with NS-E330 instead of NS-E328: `ownership._card_text`, `PAUSED_OWNER`; a resume during the same night does not continue it that night; the cause is worded by who stopped the job last and the way back by why it waits now (`PAUSED_REASON`, `_card_error`), so a job that a power cut interrupted and a pause then held, during the continuing night or at a later night's scan, reads "the PC or Nightshift stopped during the night's run. Its channel was paused after that. Resume the channel: ..." with NS-E330 and never "Press Retry", which is refused while paused; a card written while the channel was active reads "The next nightly run continues it, or press Retry (if its channel is paused by then, both wait for the resume). A nightly run comes only while Automatic runs are on in Settings", because no pause or resume rewrites a card), lists it under `paused` with its `job_id`, starts no further job of that channel, and the first night after the resume continues it while the job started no more than `RESUME_WITHIN_DAYS` (3) days before that night (an older one waits for Retry). A run job with a `count` makes its videos in one loop (`ns_dashboard/studio_bridge.py` `run_story_jobs`), which reads the channel again before each video, the first too (`paused_now`), starts no further one and raises `ChannelPaused` with `left` (the videos still owed), so the worker holds the request with that `count`. A pause that comes in the seconds between the worker's look at the gate and the first video of a run job, a Retry or a series part (`run_story_jobs`, `run_series_part`) makes that studio process stop with `ChannelPaused` (an `NsError` with NS-E330, "The channel is paused.", so the child reports its code and hint and the card does not read NS-E327) before any video, combo or job folder is made, and a run job goes back in line with its whole `count` (held when the worker sees the pause, plain "Queued" when the owner resumed first; the bound is the one above); a series part goes back to waiting with its job id the same way (`JobWorker._requeue_paused`), and goes back to `approved` only when it fails or is cancelled. `paused_now` and the nightly's `_paused_now` count a profile that cannot be read (invalid, gone, or held open by another program: OSError) as not paused, with a WARN `channel_profile_unreadable`, and the run goes on. `studio run --channel <paused>` and `studio run --resume <a job of a paused channel>` typed in a terminal (`ns_studio/cli.py` `run_cmd`, `_run_jobs`) refuse with NS-E330 and start nothing (no job folder, no stage; a `--dry-run` still prints the plan); `_run_jobs` reads the profile again before every job, so a pause typed during a `--count` run stops its next job (NS-E330 for the named channel; a run of every channel, which leaves paused channels out as before, leaves out one paused meanwhile and goes on with the others). After a resume a held job's message is "Queued" again, or its title for a Series Workshop part (`jobs.released_text`, from `params["title"]`). The Assistant's `tonight_plan` gives a held job of a paused channel the start "when <display name> is resumed" (`ns_dashboard/assistant/composite.py` `_start_of`). At an engine start an overdue scheduled story job of a paused channel does not start the dashboard (`ns_supervisor/scheduled.py` `overdue_scheduled_jobs`; a profile that cannot be read counts as before). A failed story job's **Retry from where it stopped** (`ns_dashboard/ui/src/ui/JobError.tsx`, `POST /api/jobs/{id}/resume`, `create_resume` and the Assistant's `check_resume`) goes through `_resume_params`, which is refused for a paused channel with the same `channel_paused` answer as `_run_params` (one helper, `_require_not_paused`). Retry covers what the request still owed when it stopped, not one video: the video under way is continued and the videos that had not started are made (`owed_after_stop`: from the studio's own account after a pause stop, else the last studio job the request announced counts as half made, as after a restart, NS-E328, or a killed studio process; a job that stopped before it made anything is still answered `cannot_resume`). A Retry that would make new videos is refused with `already_continued` once a continuation of the same card has started work, so pressing it twice makes nothing twice (it reads the newest `RETRY_LOOKBACK`, 200, story jobs for that). The message after the button is pressed says how many videos the Retry makes ("Continuing from where it stopped, then making 2 more videos of that request", `ns_dashboard/ui/src/ui/JobError.tsx` `retryWords`, from `params.count` and `resume_job_id` of the job it queued); the button itself reads "Retry from where it stopped" for every card. The nightly route is the same in effect (`ns_studio/ownership.py` refuses to continue a job of a paused channel). The nightly run reads the channel again before each job (`ns_studio/daily.py` `_paused_now`): a channel paused during the night gets no further job that night, and the report lists them in a key of their own, `paused` (`{channel, jobs, reason}`, plus `job_id` for a half-made job), not under `skipped`, which keeps its meaning (an earlier job stopped, so the others wait) and is worded so by the Queue page. The first part of the night, the continuing of half-made jobs (`_resume_interrupted`), checks the channel when it scans (`ownership.pending_resumes`, which refuses a job of a paused channel and gives its card Retry) and again before each job: a half-made job of a channel paused meanwhile is left as it was, `interrupted`, the morning note says it waits, and the next night continues it after the resume, but only while the job started no more than `RESUME_WITHIN_DAYS` (3) days before that night (`ownership.resume_problem`); an older one is not continued by the night, and the note then tells the owner to press Retry in the Queue (or run `studio run --resume <job id>` when the job has no card). The Queue's "Last night" card prints them: "N jobs not started: <channel> is paused" (`ns_dashboard/ui/src/pages/Queue.tsx` `LastNight`; `Run.summary.paused` in `types.ts`). Resume: `studio resume <slug>` prints the last 10 outputs (`review.last_outputs`) and the 18.2–18.4 reminder and asks first (`--yes` skips the question); the switch resumes at once. `critic_bar`: `studio channel edit <slug> --set critic_bar=8.5` or the Assistant's `edit_channel` tool; pools: Channels > Edit channel. README.md and OPERATOR.md print the four steps | The text follows the code (packages P41, P47). A Pause dialog with a notice field, a resume dialog that lists the last 10 outputs, and a link from the dialog to the guide are product changes and stay open in the audit row "Incident procedure: before resume" of docs/FEATURE_STATUS.md. `tests/test_spec_operator_text.py` fails when the switch starts sending a note, so this row and the guide are updated then |
| D39 | Facecam candidate limits (§19.2 C5, §19.4) | A facecam candidate is 5–30 % of the frame area (not 8–30 %), its width/height ratio lies between 4:3 and 16:9 with a ±0.06 tolerance at both ends (1.27 to 1.84), and its centre is in the outer 35 % of the frame width and height (`ns_studio/clipper/detect.py` `FacecamParams`: `min_area_frac` 0.05, `max_area_frac` 0.30, `min_aspect` 4/3 − 0.06, `max_aspect` 16/9 + 0.06, `corner_frac` 0.35) | CR-C8-7 is accepted: the prescribed fixture facecam (320×180 in 1280×720) is 6.25 % and small overlay cams are common; the ±0.06 aspect tolerance was in the code without a record, and this row is its record; owner decision taken: keep the code |
| D40 | Caption filler words in clips (§17.2) | `captions.remove_fillers` (default off) leaves only hesitations out of clip captions: `um`, `uh`, `uhm`, `umm`, `er`, `erm`, `hmm` (`ns_studio/clipper/select.py` `HESITATIONS`, `is_filler_word`); `like` is kept on purpose; the C4 candidate filter counts `like` as a filler token (`FILLER_TOKENS`, more than 40 % of a moment's words) and removes nothing; the removal tests the ASR word (`is_filler_word(w.w)` in `CaptionsStage.run`), so the cleaned spelling of `clip_caption_words.json` (§17.2) never changes which words go, and a hesitation the model left out of `caption_text` is still removed only by this setting | §17.2 says never silently change meaning, and `like` is a real word that no rule here can tell from the filler use; the audio is never touched; `CaptionsStage.run` (`ns_studio/clipper/stages_clip.py`) builds the caption words and the speaker list for `speaker_plan` from one list of kept words, so a clip with a hesitation keeps each word's own speaker; owner decision taken: keep the code |
| D41 | Clip metadata policy (§19.2 C8) | On clips the policy check `real_entities` runs on the metadata the model wrote (title, description lines before `Clip from:`, tags, hashtags) and flags only links (`http://`, `https://`, `www.`) and `@handles` (an `@` word at the start of a text or after a space; the pattern is `_HANDLE_OR_URL`); real names of the owner's guests, games and brands are allowed; the story-video check of §18.8 (spaCy entities, `real_entities_block.txt`) does not run on clips (`ns_studio/clipper/seo.py` `check_real_entities`) | The footage and the people in it are the owner's, so naming a guest or a game in the title is honest, and the fiction rules of §18.3 do not apply to clips (the same reasoning as the clip-only `misleading_claims` list); a link or a handle in the model's metadata is what the check looks for; owner decision taken: keep the code |

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
| Home env var | `NIGHTSHIFT_HOME` (set by setup, default `Nightshift` on the drive with the most free space, §13 D29; `%LOCALAPPDATA%\Nightshift` only when the variable is unset) |
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
stories\<story-slug>\ (§23)  outbox\<slug>\<YYYY-MM-DD>\<job_id>\long\  outbox\<slug>\<YYYY-MM-DD>\<job_id>\shorts\
rejected\<slug>\<YYYY-MM-DD>\<job_id>\ logs\
```
Coding workspace: `%USERPROFILE%\Projects`.

### 15.2a Outbox layout is organized by content, not by job
```
outbox\<channel>\
  stories\<story-title-slug>\full\<story-title>.mp4 + .json + .txt + thumb_1..3.jpg
  stories\<story-title-slug>\shorts\<story-title> - Short 01.mp4 + .json + .txt
  series\<series-title-slug>\part-01\full\<series-title> - Part 01.mp4 + …
  series\<series-title-slug>\part-01\shorts\<series-title> - Part 01 - Short 01.mp4 + …
  clips\<source-video-name-slug>\clip-01 <clip-title>.mp4 + .json + .txt
```
`outputs.path` in SQLite and `metadata.job_id` in each sheet keep traceability; `data\jobs\<job_id>\` keeps the working files. File names are Windows-safe slugs (≤ 80 chars, no reserved characters); collisions get a `-2` suffix. `rejected\` mirrors the same tree. The Dashboard's "Open folder" opens the item's folder in Explorer. Posted items stay where they are; status lives in the database.

### 15.3 Ports & supervisor API (127.0.0.1)
llama **8080** · embed **8081** · TTS **8091** · supervisor **8099** · dashboard **8100**.
| Method | Path | Body → Response |
|---|---|---|
| GET | `/status` | → `{gpu_owner, services:{<name>:{state,pid,port,vram_mb,model_alias?}}, session:{id,interactive}, busy}` (`session.interactive` is false for an engine the 03:00 task started in Windows session 0; `busy` is true while a run uses the engine) |
| POST | `/acquire` | `{service, owner, model_alias?, wait:true, timeout_s:7200}` → `{endpoint, model_alias}` |
| POST | `/release` | `{service, owner}` → `{ok}` |
| POST | `/stop_all` | → `{ok}` |
| POST | `/shutdown` | `{grace_s:30}` → `{ok}`; stops children and exits the supervisor |
| POST | `/warmup` | `{service}` → `{state}`; starts a service without an owner so the first job is fast |
| GET | `/events` | server-sent events `{ts, kind:"service"|"update"|"run", data}`; the first frame is a `service` snapshot (`data.event:"snapshot"` plus the `/status` body) and a `: keepalive` comment follows 15 s of silence; `service` frames carry service state changes and lock events, `update` the weekly update-check result, `run` `{event:"finished"}` when the nightly run ends |
| POST | `/events` | `{kind:"job", data:{}}` → `{ok}`; publishes the frame to every subscriber, but no product code posts job progress (D32) |

States: `stopped|starting|ready|stopping|error`. Owners: `opencode`, `studio:<job_id>`, `verify`, `gate`. `embed` acquisition always succeeds immediately (no lock). In two-model mode, acquiring `llama` with a different `model_alias` than the loaded one triggers a swap once the current owner releases.

Job progress is not a supervisor event. The dashboard's own `GET /api/events` (port 8100) streams `{kind, payload}` frames: `status` (the `/api/status` object, first and after every event), `job` (a dashboard job record from the dashboard's job store, the one-off `{draining: true}` frame when the engine shuts down, or a supervisor event of kind `job` should one be posted), `update` and `run` (the supervisor's kinds, kept as they are). The browser (`ns_dashboard/ui/src/lib/app.tsx`) acts on `status`, `update` and `run` frames only; a consumer for the `job` frames is FINALIZATION F-B19.

### 15.4 TTS API (port 8091)
| Method | Path | Body |
|---|---|---|
| POST | `/v1/audio/speech` | `{model:"qwen3-tts", input, voice, speed, response_format:"wav", seed?}` → WAV 24 kHz mono |
| POST | `/v1/audio/speech/batch` | `{items:[{id,input,voice,speed}], out_dir, seed?, timeout_s?, resume?}` → `{files:[{id,path,seconds}], engine, rtf}`; `timeout_s` is the client's wait (`ns_common.tts_timeout`: 120 s + 3 s per line, cap 2 h), no process is stopped before it; `resume` reuses items an interrupted attempt left in `out_dir` |
| POST | `/v1/audio/speech/cancel` | → `{cancelled}`; kills the running batch process (the client timed out) |
| GET | `/v1/voices` | → `[{name, mode:"preset"|"design"|"clone", source}]` |
| POST | `/v1/voices/design` | `{name, description}` → `{name, sample_path}` |
| POST | `/v1/voices/clone` | `{name, ref_audio_path, ref_text, rights_confirmed:true}` → `{name, sample_path}` (400 if `rights_confirmed` false) |
| GET | `/health` | → `{ok, engine:"qwentts"|"reference"|"fake", model_loaded}` |
| GET | `/v1/voices/{name}/embedding` | → the stored speaker file (bytes) for backup/transfer between machines; a voice with several speaker files comes as one zip, `<name>.voice.zip` |

**Voice library = embeddings, not audio.** `models\tts\voices\<name>\{voice.json, sample.wav, <speaker files>}`. The speaker files are the precomputed speaker prompt, produced once (from a clone sample, or from a VoiceDesign render that is immediately re-encoded), and depend on the engine that made the voice; `voice.json` names them in `files` and the engine in `engine`. **qwentts** stores `speaker.spk` (the speaker embedding), `speaker.rvq` (the reference codes) and `ref.txt` (the transcript), made by `qwen-codec`. The **reference** (PyTorch) and **fake** engines store one `speaker.bin`. A folder whose `voice.json` has no `files` (a voice made before the key existed) is read as `speaker.bin` (CR-C4-6, D33). Every later synthesis passes the stored prompt to the Base talker directly; nothing is re-derived. Presets need no embedding (CustomVoice talker). VoiceDesign is only needed at design time and may be deleted afterwards.

### 15.5 Embed API (port 8081)
Standard llama-server `POST /v1/embeddings` `{input:[...]}` → OpenAI-shaped response. `ns_common.embed_client.embed(texts) -> list[list[float]]`, `cosine(a,b)`.

### 15.6 JSON schemas (`contracts/schemas/`)
`channel, runtime, install_state, answers, manifest, idea, plan, story, parts, seo, seo_long, shorts_plan, chapters, visual_plan, qc, policy, metadata, combo, analytics_row, calibration_row, gate_report, transcript, clip_plan, clip_layout, clip_template`. There is no `dashboard_job` schema: a dashboard job is a row of the `dashboard_jobs` table (§15.7), read by the dashboard-local `ns_dashboard.jobs.JobRecord` whose `to_dict()` is the API object of `/api/jobs` (the request bodies are `job_run_request` and `job_clip_request`).
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
`JobContext` carries `job_id, channel, combo, job_dir, db, llm, tts, embed, log, runtime`. The orchestrator groups the consecutive stages of **one job** by tenant and acquires each tenant once per group (`run_job`); a daily run takes its jobs one after the other, so each job acquires `llama`, `tts` and `whisper` again in turn (decision D21, §13).

Job dir `data\jobs\<job_id>\`: `idea.json plan.json story.json seo_long.json seo_shorts.json visual_plan.json shorts_plan.json shorts_timing.json policy_text.json policy_final.json audio\ words\ captions\ clips\shots.json clips\shorts_shots.json long\master.mp4 long\chapters.json long\thumb_[1-3].jpg shorts\s<k>\{audio.wav,short.mp4} qc\ stage_state.json`.
Outbox `outbox\<slug>\<YYYY-MM-DD>\<job_id>\`: `long\master.mp4 + .json + .txt + thumb_*.jpg` and `shorts\s<k>.mp4 + .json + .txt`. `job_id`: `<slug>-<YYYYMMDD>-<4hex>`.

### 15.9 CLI, launchers, shortcuts, task
- `studio run [--daily] [--channel <slug>] [--count N] [--format master_first|shorts_only|series_compilation] [--dry-run] [--resume <job_id>]`
- `studio review [--channel]` · `studio status` · `studio pause <slug> --reason "<text>"` · `studio resume <slug>`
- `studio channel add|list|edit|validate` · `studio voice list|preview|design|clone`
- `studio learn --import <csv>` · `studio mark-uploaded <job_id> <long|s<k>> <url>`
- `studio calibrate --channel <slug> [--n 20]` · `studio calibrate --score <csv>`
- `studio clip <video> --channel <slug> [--template auto|gaming_facecam|podcast_stacked|podcast_single|talking_head|center_crop] [--max-clips 6] [--min-s 20] [--max-s 60]`
- `studio dashboard [--port 8100] [--open]`
- `ns tune` · `ns verify [--quick]` · `ns doctor [--bundle]` · `ns stop` · `ns gate <milestone>` · `ns update <zip> | --rollback` · `ns backup [--out <zip>]` · `ns restore <zip>`

| Shortcut | Launcher |
|---|---|
| `Nightshift Dashboard.lnk` | `nightshift-dashboard.ps1`: start the supervisor as a minimized "Nightshift Engine" window if not running, wait for `/status`, open `http://127.0.0.1:8100` as an app window (`msedge.exe --app=...`, else `chrome.exe --app=...`) when Edge or Chrome is installed, otherwise in the default browser (§6.2) |
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
Words per part/chapter within budget ±20 % · banned phrases · no opening line with embedding similarity ≥ 0.9 to the channel's last 50 openings (the story's first line: chapter 1 of a master, the story of a Short-only job; the check is skipped in dev mode, D35) · SEO limits (§9) · reading level grade 5–8 (Flesch-Kincaid via `textstats.py`, configurable) · no sentence > 35 words (spoken rhythm) · no two consecutive chapters ending on the same cliff device.

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

### 17.1 Formats are data, not code (modularity rule)
Every output shape is a **format preset** in `ns_studio/presets/formats/<id>.yaml`, loaded at runtime; adding a format never touches Python. A format defines: `canvas {w,h,fps}`, `kind: short|long|square|clip`, `duration {min_s,max_s,buckets?}`, `captions {styles_allowed[], anchor_y_pct, words_on_screen}`, `layout {regions:[{name, x,y,w,h, source: full|facecam|face:n|screen|blur_fill}], seam_captions?}`, `safe_margins {top,bottom}`, `shots {min_s,max_s}`, `encoder {preset}`, `sheet {hashtags_required[], description_rules}`. Shipped presets: `short_9x16`, `long_16x9`, `square_1x1`, and the clip layouts of §19.3 (`clip_gaming_facecam`, `clip_podcast_stacked`, `clip_podcast_single`, `clip_talking_head`, `clip_center_crop`, `clip_screen_plus_cam`). The renderer builds its FFmpeg graph from the regions list generically (`ns_studio/media/layout.py`), so split screens, picture-in-picture and new aspect ratios are new YAML files. Channels list `formats_allowed`; the Library → Formats page lets the owner duplicate and edit presets with a live layout thumbnail and validates them against `contracts/schemas/format.schema.json`.

---

### 17.2 Captions: presets plus smart placement and emphasis
- **Style is a preset** (`caption_styles.yaml`, §17) and is chosen per video: by the variation engine for story videos, by the model in `clip_select` for clips (returned next to `layout`), or fixed in a series' `design.yaml`.
- **Placement avoids what matters on screen**: the caption anchor moves away from detected face and facecam regions and from YouTube's UI safe zones (top 220 px, bottom 320 px on 9:16); stacked podcast layouts put captions on the seam; gaming layouts put them on the gameplay region, never over the facecam.
- **Emphasis**: one word per caption group is highlighted (the active word by default). For clips and story videos the LLM marks up to one **key word per sentence** (`emphasis` in the visual plan / clip plan) that gets the style's accent colour and a slightly larger size; no emoji, no stickers.
- **Text source**: story and series videos render the script's exact words; clips render the words of the speech recogniser (ASR) with the model's cleaned spelling laid over them: `clip_select` returns a `caption_text` per clip (the spoken words with casing, punctuation and numbers as spoken restored; `ns_studio/prompts/clip_select.md`, version 4), and `align_caption_text` (`ns_studio/clipper/select.py`) lays it over the ASR words. A model word is accepted only when its letters match the spoken word (case and punctuation aside); digits are accepted only for a spoken number whose neighbours are accepted model words, and never with a sign, decimal point, currency, percent, hash or emoji (`_display`, `_is_spoken_digits`). A word the model dropped, added or rephrased, or a decimal such as `3.5`, keeps the ASR spelling, so the word count and every word timing stay the ASR's. A clip whose cleaning cannot be aligned (fewer than 80 % of its spoken words match, `MIN_CLEANED_SHARE`, or nothing changes) stays uncleaned with the ASR spelling. The cleaned words live in the sidecar `clip_caption_words.json` (`CAPTION_WORDS_FILE`, one display string per ASR word of a clip, written by `SelectStage` beside the plan), not in `clip_plan.json`; `CaptionsStage` uses an entry only when it has one string per ASR word of the clip, and a job without the file shows the ASR spelling. Optional hesitation removal stays the captions stage's job: the channel setting `captions.remove_fillers` (default off) leaves `um`, `uh`, `uhm`, `umm`, `er`, `erm` and `hmm` out of the captions and never touches the audio (`like` is a real word, so it is never removed, D40); numbers kept as spoken. Never silently change meaning. C4's candidate filter (§19.2) counts `like` among its filler tokens only to judge whether a moment is mostly filler and removes no word.
- **Speakers**: in two-speaker layouts each speaker's captions take a distinct colour from the style's palette; speaker change starts a new group.
- **Profanity**: per-channel option `captions.mask_profanity` masks letters in captions (audio untouched); default off.
- **Timing**: groups of 2–4 words, 0.9–2.2 s on screen, never spanning a sentence end; drift limit per §7.7.
- **Accessibility**: minimum contrast 4.5:1 against the style's backing box or outline; the renderer verifies by sampling the frame behind the caption and switches to the boxed variant when contrast is too low.

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
| Quality drop | If the 7-day viewed rate falls under 50 %, **or the owner rejected ≥ 40 % of the last 10 reviewed outputs** (at least 5 reviewed; outputs still pending do not count), drop to 1 Short/day and raise `critic_bar` by 0.5 (never above 10, at most once per 7 days while the drop lasts, recorded as a `warning` incident; D36) (reject reasons are shown in `studio status` and the Dashboard) |
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
| `audio_only_voice` | final | Any non-voice audio track present (no music by design); fails on the stream count and on a noise floor above -55 dBFS; spectral flatness of the quiet frames is measured and warned only (`qc/audio_flatness.json`, no code) until calibrated on real narration | E614 |
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
If YouTube issues any warning, strike or monetization notice: `studio pause <slug> --reason "<notice text>"` records an incident, stops nightly runs for that channel, and `studio status` shows it. Before `studio resume`, the owner reviews the last 10 outputs against §18.2–18.4 and raises `critic_bar` or trims pools as needed; `studio resume` lists those 10 outputs and asks before it resumes. The procedure is printed on the README and in `OPERATOR.md` ("If YouTube sends a warning or strike"). The Dashboard's Channels switch also pauses and resumes, but it records no notice text and shows no review list (the Assistant's pause tool takes the notice as a note); D38 gives the routes.

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
| C4 | **Select**: `clip_select.md` (thinking on) over the transcript in windows of ≤ 12 min with 2 min overlap; returns candidate moments 20–60 s (configurable) with title, why, score; merge overlaps, snap edges to sentence boundaries from `words`, drop candidates whose text is mostly filler (< 2.2 words/s or > 40 % filler tokens: `um`, `uh`, `uhm`, `umm`, `er`, `erm`, `like`, D40), keep the top `max_clips` by score with ≥ 60 s spacing between clip starts | `llama` | `clip_plan.json` |
| C5 | **Detect**: sample frames at 1 fps inside the chosen ranges (plus 10 frames spread over the whole video for the facecam scan). YuNet face boxes per frame; **facecam scan**: find a stable rectangular region (consistent edges across sampled frames, aspect 4:3–16:9 with a ±0.06 tolerance on the width/height ratio, 5–30 % of frame area, centre in a corner, §19.4, D39) → `facecam`. Template `auto` decides: facecam found → `gaming_facecam`; ≥ 2 stable faces → `podcast_stacked`; 1 stable face → `talking_head`; else `center_crop` | none (CPU) | `clip_layout.json` |
| C6 | **Captions**: words from the transcript in the clip range → ASS with the channel's caption style (same builder as §7.6 step 4), 2–4 words on screen, active-word highlight | none | `captions\c<k>.ass` |
| C7 | **Cut + render**: per clip, build one FFmpeg filter graph from the template (below), trim, scale, crop, stack, burn captions, audio `loudnorm` to −14 LUFS, `h264_nvenc`/`libx264`, faststart | NVENC | `clips\c<k>.mp4` |
| C8 | Clip SEO: `clip_seo.md` → title (≤ 70), description (honest, includes "Clip from: <source title or filename>"), tags, hashtags incl. `#Shorts`; policy text gate checks `misleading_claims`, `metadata_spam`, `real_entities` **on metadata only** (the spoken content is the owner's); on clips `real_entities` flags only links (`http://`, `https://`, `www.`) and `@handles` in the title, the description lines the model wrote, the tags and the hashtags: real names of the owner's guests, games and brands are allowed, and the fixed `Clip from:` line with the creator note after it is never checked (D41) | `llama` | `seo_clips.json`, `policy_text.json` |
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
- **Facecam:** Canny edges on 10 frames spread across the video → accumulate; find axis-aligned rectangles whose four edges persist in ≥ 8/10 frames and that have an **area of 5–30 % of the frame** and an **aspect of 4:3 to 16:9 with a ±0.06 tolerance** (width/height from 1.27 to 1.84), their centre in a corner (the outer 35 % of the frame width and of the frame height); keep the candidate with the most faces detected inside it (then the better edge coverage, then the larger rectangle). If none, `facecam = null`. The 5 % floor is CR-C8-7 (`contracts/CHANGE_REQUESTS.md`): the 320×180 facecam of the test fixture in a 1280×720 frame is 6.25 %, and small overlay cams are common (D39).
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

### 20.2 Pages: purpose and functionality

The dashboard follows the owner's loop: things get made overnight → watch → approve → post → learn. Every page has one purpose; its primary action is visible without scrolling at 1366×768. Plain words on primary surfaces, technical detail behind "Advanced".

| Page | Purpose (one sentence) | Functionality |
|---|---|---|
| **Queue** (home) | Get finished videos onto YouTube correctly, one at a time. | Tabs Ready / Approved & posted / Rejected. Lands on the first ready video playing. Right side is a **posting checklist in YouTube's upload order**: 1 Approve or Reject (reason) · 2 Open the video file (Outbox folder) · 3 Open YouTube Studio · 4 Title (copy) · 5 Description incl. chapters and hashtags (copy) · 6 Thumbnail (long-form) · 7 Audience answer · 8 Altered-content answer · 9 Tags (copy) · 10 Schedule and Related-video instruction · 11 Paste the YouTube link → Done. Progress per video is remembered. Filmstrip of the other ready items; keys A / R / →. "Why it passed" and the story text under a disclosure. "Ask about this video" opens the Assistant with this item as context. |
| **Create** | Start new work without waiting for tonight. | Two choices: *A new story video* (channel → Full video + Shorts or One Short → Start) or *Clips from my own video* (drop file → channel + layout format → options → rights → Start). One progress bar per job with plain stage names and step count; cancel; "Review it" when done. Working-now and Earlier lists with plain failure reasons. Story ideas can be picked from the Library's idea bank instead of letting Nightshift invent one. |
| **Library** | Everything Nightshift knows and can reuse, organized. | Tabs: **Ideas** (the story bank: add ideas by typing or via the Assistant; statuses New / Approved for production / Used / Dropped; Nightshift draws approved ideas first, invents only when the bank is empty; per channel) · **Stories** (every written story with its outputs, critic score, where it was posted; search; "make a Short from this" / "reuse as a series") · **Voices** (presets, designed and cloned voices; play sample; design or clone a new one; VoiceDesign downloads on demand) · **Formats** (video formats and clip layouts as editable presets, §17.1: duplicate, edit, preview thumbnail; which channels may use each) · **Footage** (cached stock clips by keyword, licence, last used; delete). |
| **Assistant** | Drive Nightshift by talking to it, with safety. | Chat with the local Qwen (through the supervisor's `llama` service; nothing leaves the PC). It can: explain any video, failure or result; add ideas to the bank; start story or clip jobs; pause/resume channels; change schedule; search the Library; draft channel style notes. Every state-changing action appears as a **confirm card** in the chat (what will happen, Yes/No) before it runs; Approve/Reject of videos are never done by the Assistant. Available as a drawer on every page (context = current page/item) and as a full page with history. Offline-safe: when the engine is busy with a nightly run the Assistant says so and queues the request. |
| **Channels** | Set up and adjust each YouTube channel. | Cards; editor in sections Basics / Voice / Schedule / Content style (pools as chips with plain warnings) / Formats allowed. Pause/Resume. |
| **Results** | See what works and what Nightshift will change. | Doing well / Not landing lists, plain-English learnings, "what changes next" per pool, drop zone for the YouTube Studio export, unmatched videos with a link to fix them in the Queue. Go/no-go milestone card (§18.12). |
| **Settings** | Keep the machine running. | Run time, notifications, stock-footage keys (keyring), storage location, **Update** (check GitHub for a new version, one-click install with rollback, §5), diagnostics report, Advanced: engine flags (unlock to edit), recent log with level filter, uninstall. |
| **Status strip** (every page) | Know what the engine is doing right now. | One pill: Idle / Loading model 9 GB · 12 s / Ready / Working: Rendering (step 6 of 9); next run time; Quit Nightshift (in-page confirm, 30 s grace). |
### 20.3 API (all JSON, prefix `/api`)
`GET /status` · `GET/POST /channels`, `GET/PUT /channels/{slug}`, `POST /channels/{slug}/pause|resume|validate` · `GET /outputs?status=pending&kind=` · `GET /outputs/{id}` · `POST /outputs/{id}/approve|reject|uploaded` · `GET /media/{output_id}` (range streaming of the MP4) · `GET /media/{output_id}/thumb.jpg` · `POST /jobs/run {channel, format}` · `POST /jobs/clip` (multipart upload or `{path}` for a local file; `{channel, template, max_clips, min_s, max_s, rights_confirmed}`) · `GET /jobs`, `GET /jobs/{id}`, `POST /jobs/{id}/cancel` · `GET /runs` · `GET /logs/tail?component=&level=&n=` · `POST /learn/import` (multipart CSV) · `GET /learn/summary` · `GET/PUT /settings` · `POST /settings/keys` · `POST /diagnostics` · `GET /templates` (clip templates with preview PNGs) · `GET /voices`, `POST /voices/preview`.

### 20.3a Assistant: a real operator over the whole system

**Goal:** the owner can run Nightshift by talking to it. The Assistant is a tool-using agent on the local Qwen (`llama` service via the supervisor, OpenAI-compatible tool calling), not a chat box with canned answers.

**Tool registry = the API.** Every `/api/*` route declares a `tool` descriptor next to its handler (`name`, one-line `description`, JSON-schema `args`, `side_effect: bool`, `confirm_text` template). `ns_dashboard/assistant/registry.py` collects them at startup, so any new feature is usable by the Assistant the moment its route exists. Read-only tools (`side_effect=false`) run immediately; side-effect tools return a **confirm card** (what will happen, in the owner's words) and run only after the owner clicks Yes (`POST /api/assistant/confirm {action_id}`). Never registered as tools: approve/reject outputs, keys, uninstall, engine flags, update apply.

**Orientation tools (so it knows where things are fast):**
- `system_map()` → compact JSON: pages and their purpose (§20.2), folders under `NIGHTSHIFT_HOME` and what lives in each (§21.5), services and ports, channels, formats, error-code ranges, current engine state. Also injected once into the system prompt (≤ 1,500 tokens).
- `find(query)` → one call that searches outputs, stories, ideas, channels, jobs, logs and docs and returns typed hits with ids and paths.
- `where_is(thing)` → the absolute path or page for an output, job, channel, model, log or setting.
- `describe_tool(name)` / `list_tools(topic?)` → the registry itself, grouped by topic (queue, create, library, channels, results, settings, engine, diagnostics).
- `search_docs(query)` → ranked passages from `BUILD_SPEC.md`, `README.md`, `OPERATOR.md`, `docs/ENGINEERING.md`, `contracts/errors.json` and prompt front-matter (local full-text index built at startup; no embeddings needed, optional `embed` reranking when the service is idle).
- `explain_error(code)` → message + hint + last log lines carrying that code.
- `status()` → the same as `/api/status`.

**Operating tools (examples; all derived from routes):** `list_queue`, `get_output`, `open_folder`, `start_story_job`, `start_clip_job`, `cancel_job`, `list_jobs`, `pause_channel`, `resume_channel`, `edit_channel`, `set_schedule`, `add_idea`, `set_idea_status`, `search_library`, `list_formats`, `duplicate_format`, `import_analytics`, `summary_results`, `tail_logs`, `make_diagnostics`, `check_update`, `warmup_engine`, `shutdown_engine`.

**Behaviour contract (`ns_dashboard/prompts/assistant.md`, versioned):** answer in plain English in 1–4 sentences; call `system_map`/`find` before guessing; when asked to do something, do it with tools and report what happened with ids and paths; never claim a result it did not read back; when the engine is busy, say what is running and queue the request; refuse the never-registered actions with the one-line reason and point to the page.

**Everywhere:** drawer on every page (context: current page and selected item ids are passed as `context` and offered as the first tool arguments), full page with history, and `studio ask "<text>"` on the command line for the same loop. Transcripts in `data\assistant.db`. Tests: a fake LLM that emits scripted tool calls proves the loop, confirm gating, and that every route's descriptor validates; a hygiene test fails CI if a new route lacks a descriptor or a `side_effect` flag.

**Library endpoints:** `GET/POST /api/library/ideas`, `PUT /api/library/ideas/{id}` (status), `GET /api/library/stories?q=`, `GET /api/library/footage?q=`, `GET/POST/PUT /api/library/formats`, `GET /api/update/check`, `POST /api/update/apply`. Ideas feed M1 stage `ideas`: approved bank entries for the channel are consumed first (status → Used); invention happens only when none are left.

### 20.4 Job worker
A single background thread drains `dashboard_jobs` (FIFO). Job kinds: `run` (studio job via the orchestrator), `clip` (§19 pipeline), `learn`, `verify`. Progress is written per stage to `dashboard_jobs.progress/message`; the UI polls `GET /jobs/{id}` every 2 s. The worker acquires GPU tenants through the supervisor like the nightly task does; if the GPU is busy with a nightly run, the job waits and the UI says so. Cancel sets a flag checked between stages.

### 20.5 Build notes
- UI files: `index.html` (shell + nav), `app.js` (hash router, fetch helpers, one module per page), `app.css` (tokens on `:root`, light/dark via `prefers-color-scheme`). No external CDN: the page must work offline.
- Video streaming implements `Range` requests (206) so the browser player can seek.
- Uploads stream to `imports\clipper\<source_id>\` in 8 MB chunks; the UI shows upload progress separately from job progress.
- All destructive actions (reject, cancel, stop all) confirm in the UI.
- Tests: FastAPI test client for every route with a fake job worker; a `--fake-services` clip job through `/api/jobs/clip` with the synthetic fixture.

---

### 20.6 Visual direction (owner reference: a smart-home "bento" dashboard)
- **Bento grid with one hero per screen**: on Queue the hero is the video (9:16 or 16:9) with floating chips over it (kind, duration, channel, status); the posting checklist is the tall side card. Create, Library and Results use the same grid of rounded cards, one card per task or object.
- **Soft two-tone surfaces**: a tinted page background with lighter cards, 24 px radii, generous padding, no hairline tables. Light theme is the reference; the dark theme keeps the same shapes with deep slate surfaces and one warm accent.
- **Big obvious controls**: pill buttons ≥ 44 px, large toggles for channel on/off and settings, one primary action per card.
- **Keep labels on the nav**: icon + label in the left rail; icon-only rails are not allowed. Body text stays 16 px; chips 13 px minimum.
- **Media first**: thumbnails and players get the space; numbers appear as chips on media, not as separate stat tiles.

## 21. Install sources, dependencies and on-disk layout (what the installer actually pulls)

Everything below is pinned in `manifest.json` (exact versions, URLs, SHA256) by `tools/resolve_manifest.py` in Slice 0 Task 14 and verified by `--check`. This section is the human-readable list; the manifest is the machine-readable truth. Nothing is bundled (§0 rule 1).

### 21.1 System tools (`stage1.ps1` and the setup window's tools step, `installer/stage2/web/bootstrap.py`; winget where it works, otherwise pinned direct downloads)
| Tool | winget id | Direct download | Used for |
|---|---|---|---|
| Visual C++ runtime | `Microsoft.VCRedist.2015+.x64` | manifest `bins.vcredist` (`vc_redist.x64.exe`, SHA256 checked, run with a Windows permission prompt). Stage 1 uses this download first, with no winget; the setup window's tools step tries winget, then this pinned build, only when stage 1 could not install it | llama.cpp and the voice programs do not start without it |
| Windows Terminal | `Microsoft.WindowsTerminal` | none (winget only; skipped when it cannot be installed) | Wizard and launchers (optional; falls back to conhost) |
| FFmpeg (full build) | `Gyan.FFmpeg` | manifest `bins.ffmpeg` (static build from BtbN FFmpeg-Builds, SHA256 checked, unpacked and put on the user `PATH`) | Render, probe, loudness, captions (`libass` included in the full build) |
| uv | `astral-sh.uv` (only when the direct download fails) | manifest `bins.uv` zip from https://github.com/astral-sh/uv/releases | Python install + venvs; no system Python needed |
| Python 3.12 | — (`uv python install 3.12`, uv-managed, under `%APPDATA%\uv\python`) | none; when uv cannot fetch it, setup stops with `NS-E105` | All venvs |
| NVIDIA driver | — | https://www.nvidia.com/Download/index.aspx | Must report CUDA ≥ 12.4 in `nvidia-smi` (12.4 to 12.x takes the CUDA 12.4 build of llama.cpp, 13.0 or newer the 13.4 build), and ≥ 13.0 (driver 580) for an RTX 50-series / RTX PRO card, which only runs the 13.4 build (`NS-E103`) |

A missing winget is a warning, not a stop: setup fetches the pinned builds itself and shows no download pages to open (§13 D27). Git is not in this table: setup does not install it. Only the optional `ns update --git` needs it, on a cloned install: a clone without Git stops with `NS-E702` and says so, and a zip install (not a clone) stops with `NS-E702` "is not a git clone" (§13 D25); Git for Windows is at https://git-scm.com/download/win.

### 21.2 Binaries (`s02_download_bins`, into `%NIGHTSHIFT_HOME%\bin\`)
| What | Source pattern | Notes |
|---|---|---|
| llama.cpp CUDA build | `https://github.com/ggml-org/llama.cpp/releases/download/<tag>/llama-<tag>-bin-win-cuda-12.4-x64.zip` (or `-13.4-`, see the variants row) | Tag pinned to a release with MTP (`--spec-type draft-mtp`) support; unzip to `bin\llama\` |
| cudart for llama.cpp | `https://github.com/ggml-org/llama.cpp/releases/download/<tag>/cudart-llama-bin-win-cuda-12.4-x64.zip` (or `-13.4-`) | Same tag; DLLs next to `llama-server.exe` |
| llama.cpp CPU build | `…/llama-<tag>-bin-win-cpu-x64.zip` | Only for `cpu-dev` and as the embed server fallback |
| qwentts.cpp CUDA build | The upstream project publishes **no Windows CUDA binary** (verified 2026-11-01, `bins.qwentts = null`). Nightshift builds it in GitHub Actions (`.github/workflows/build-qwentts.yml`, pinned commit, CUDA ≥ 12.8 toolkit, sm_120) and attaches `qwentts-win-cuda-x64.zip` to its own Releases; the installer fetches it from there. Until that asset exists the installer uses the PyTorch fallback path (§2, `NS-W301`) | `bin\qwentts\` |
| llama.cpp CUDA variants | Releases ship **CUDA 12.4 and 13.4** assets (no 12.8). The pair follows the driver's CUDA version: 13.0 or newer takes the 13.4 pair (`llama_cuda13`, `cudart13`; the only one with Blackwell code, so an RTX 50-series card needs it), 12.4 to 12.x the 12.4 pair; an unreadable version gets the 12.4 pair (13.4 on an RTX 50-series card, `NS-W115`); §13 D30 | `bin\llama\` |
| OpenCode (optional) | `https://github.com/anomalyco/opencode/releases/download/v<ver>/opencode-windows-x64.zip` | `bin\opencode\` |
| YuNet face detector | `https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx` | `models\cv\yunet.onnx` (~230 KB) |

### 21.3 Models (`s03_download_models`, Hugging Face `https://huggingface.co/<repo>/resolve/<revision>/<file>`, public repos, no token; resumable with HTTP Range; SHA256 from the HF tree API)
| Logical | Repo / file | Folder | Size | When |
|---|---|---|---|---|
| `llm` (small, fallback) | `Qwen/Qwen3-14B-GGUF` Q4_K_M (or a Qwen3.6 14B-class GGUF if Task 14 finds one) | `models\llm\` | ~9 GB | `llm_tier: small` (preselected when RAM < 32 GB) |
| `llm` (balanced, **recommended default**) | `mudler/Qwen3.6-35B-A3B-APEX-MTP-GGUF` I-Mini | `models\llm\` | ~14 GB | `llm_tier: balanced` (needs 32 GB RAM) |
| `llm` (best) | `unsloth/Qwen3.6-35B-A3B-MTP-GGUF` UD-Q4_K_XL | `models\llm\` | ~21 GB | `llm_tier: best` |
| `llm` (custom, opt-in) | any public Hugging Face repo and `.gguf` file the user names (`llm_custom: {repo, file, size}`), at most 15.5 GB; no pinned SHA256 (`NS-W113`) | `models\llm\` | ≤ 15.5 GB | `llm_custom` set |
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

**Default install total: ~20 GB (Balanced); ~15 GB with the Small fallback.** Quantized GGUF/CTranslate2 files are already the compressed form; archiving them gains ≤ 3 %, so the bundle never zips models. For low-bandwidth sites, put the files in a folder (USB or Drive-synced) and set `models_source`; the installer copies and verifies instead of downloading.

### 21.4 Python environments (`s04_venvs`, `uv venv` + `uv pip install`, under `%NIGHTSHIFT_HOME%\venvs\`)
| venv | Installs | GPU DLL notes |
|---|---|---|
| `studio` | `-e <app_path>[studio]` (pydantic, httpx, jinja2, rich, typer, numpy, Pillow, spaCy + model wheel, faster-whisper, imagehash, keyring, opencv-python-headless) **plus** `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` wheels | faster-whisper (CTranslate2) needs cuBLAS 12 and cuDNN 9 DLLs on Windows. `ns_common.gpu_dlls.ensure_on_path()` prepends `<venv>\Lib\site-packages\nvidia\cublas\bin` and `…\nvidia\cudnn\bin` to `PATH` before `faster_whisper` is imported; `ns verify` checks that `WhisperModel(device="cuda")` loads (`NS-E112` with the fix hint otherwise) |
| `supervisor` | `-e <app_path>[supervisor]` (fastapi, uvicorn, psutil) and the dashboard deps; also hosts `ns_tts` (which only shells out to `bin\qwentts\`) | CPU only |
| `tts` (**fallback only**) | created by `s08_smoke` if `qwentts.cpp` fails: `torch==<pin>+cu128` from `https://download.pytorch.org/whl/cu128`, `qwen-tts==<pin>`, `soundfile`, then `-e <app_path>[tts] --no-deps` | `torch.cuda.is_available()` + capability ≥ (12,0) check (`NS-E108`) |

All venvs are created from the same repo checkout (`install_state.app_path`), so `ns update` can re-sync them with `uv pip install -e …` without re-downloading models. From the first release with a `uv.lock`, the packages of the `studio` and `supervisor` venvs are the locked ones: `uv export --locked` per venv into `logs\pins-<venv>.txt`, `uv pip install --require-hashes -r` that file, then `-e <app_path> --no-deps` (setup and `ns update` alike; NS-E124 when the lock is missing or stale). The `tts` venv stays on the manifest pins.

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
Disk budget (computed per install by `ns_common/diskneed.py`, §1.2): the models from the manifest (Small about 14 GB: story model 9, voice 4.4, speech recognition 1.5) + programs and environments about 7 GB (2.5 GB of that the GPU libraries) + the 38 GB working reserve → 60 GB for the Small tier on an empty drive (Balanced 64 GB, Best 72 GB); two-model mode +20 GB; PyTorch fallback +10 GB.

### 21.6 Accounts and keys the user needs
Both stock-footage keys are optional and at least one is recommended; Pexels is not issuing new keys, so Pixabay is the one to get. Setup finishes without any and says so; a video job without a key stops up front with `NS-E502` until one is added in Settings > Stock footage keys (§13 D24).

| Key | Where to get it | Stored |
|---|---|---|
| Pexels API key (free) | https://www.pexels.com/api/ → "Get Started" | Windows keyring `Nightshift/pexels_api_key` |
| Pixabay API key (free) | https://pixabay.com/api/docs/ (shown after login) | keyring `Nightshift/pixabay_api_key` |
| Hugging Face | none needed (public repos) | — |
| YouTube | none needed in v1 (manual upload) | — |

### 21.7 Download behaviour
Every download: HEAD for size → stream to `<file>.part` with `Range` resume → SHA256 verify (`NS-E109`) → atomic rename. Concurrency 2 files at a time; progress bar per file and overall; a failed file retries 3 times with backoff; Pause stops the stream within one 64 KiB chunk, closes the connection and resumes with `Range`; a transfer that receives no bytes for 60 s is aborted and retried with backoff; the step is re-runnable and skips verified files. `s03` prints the total to download and an ETA before starting and tells the operator they can close the window and re-run `INSTALL.cmd` to resume.

---

## 22. User journeys (what the owner actually does, step by step)

Each step names the component that serves it, so coverage can be checked against §12/§20. "Owner" is the person running the GPU PC.

### J1 — Install (day 0, ~1 hour mostly waiting)
1. Owner downloads `Nightshift-<ver>.zip` (and, if provided, the `models\` folder) from the Drive link, extracts to `D:\NightShift`. **C10**
2. Double-clicks `INSTALL.cmd`. The setup window opens on a short welcome (the plan, its size and the time; the headless console path prints the full welcome text of §A.2 instead), then its Checks screen shows the **machine check table**: each row green/amber/red with a fix hint; red blocks. **C1**
3. Tools install (Visual C++ runtime, FFmpeg, Windows Terminal, uv, Python) with "already here" rows; setup does not install Git (only the optional `ns update --git` needs it). **C1**
4. Questions (skipped when `answers.json` is present): home folder (default suggested with free space shown), channel name/niche/language, voice (plays 4 preset samples, pick one), Pexels/Pixabay keys (both optional; links shown; validated live when given), masters per week, run time, install Nightshift Code. **C1**
5. Download screen: list of files with sizes, one progress bar per file and one overall, speed and ETA; "you can close this window and re-run INSTALL.cmd to resume". Files found in `models_source` are copied, not downloaded. **C1**
6. Venvs created; tuning runs (1–3 min; shows tokens/s found); smoke test table (NVENC, TTS, whisper, LLM) with fallbacks noted. **C1 C2**
7. Summary: where things live, the three shortcuts created, the nightly time, and "Next: open Nightshift Dashboard and press Run now to make your first video." (the shipped text; the Dashboard has no Run now, D34) A diagnostics zip is written to the Desktop. **C1**

### J2 — First video (day 0, ~20 min)
1. Owner opens **Nightshift Dashboard** from the desktop. The engine window appears minimized and the dashboard opens in its own Nightshift window (WebView2; an Edge or Chrome app window when that cannot start, your default browser when neither is installed) on the **Queue** page, which is the home page. The top bar reads `Idle`. **C2 C9** (D34)
2. Owner opens **Create**, picks **A new story video**, then **One Short** (fast first result) or **Full video + Shorts**, chooses the channel and idea, the voice and *Now*, and presses **Start**. The top bar reads `Loading model`, then `Working: <stage> (i/n)`; the job's card names the stage in words (the card on Create adds `, step i of n`): `Writing the story` → `Recording narration` → `Adding captions` → `Finding footage` → `Rendering` → `Checking quality`. A Full video + Shorts job says `Writing the chapters` for the story and `Rendering the full video` for the render, and adds stages that make thumbnails and cut the Shorts. **C7 C9** (D34)
3. Review page shows the new item(s). Owner plays the video in the browser, reads the story and policy notes, presses **Approve** (or Reject with a reason, which feeds the next run's critic bar). **C9**
4. **Upload**: owner opens the Outbox folder (button), drags the MP4 into YouTube Studio, pastes title/description/tags from the sheet's Copy buttons, sets the disclosure/made-for-kids answers shown, publishes, then pastes the YouTube URL into **Mark uploaded** so analytics can match later. **C9 C7**

### J3 — Nightly automation (every night, zero touch)
1. 03:00: the scheduled task wakes the PC, starts the supervisor headless, runs the cadence governor's plan per active channel (§18.6). **C2 C7**
2. Each job: text → policy gate → voice → captions → clips → render → QC → final gate → Outbox. Failures go to `rejected\` with a reason; one regeneration attempt. **C5 C6 C7**
3. Supervisor exits when done; PC may sleep. **C2**
4. Morning: owner opens the Dashboard; Overview shows "Last night: 1 master + 5 Shorts ready, 0 rejected" and the Review queue. Owner approves and uploads per the sheet's recommended order and dates (master first, Shorts spread over the next days). **C9**

### J4 — Clipper (whenever)
1. Clipper page: owner drags a VOD/podcast file onto the drop zone, ticks "I own or have rights to this video", picks channel and template (`auto` default), presses **Start**. Upload progress, then job progress by stage. **C9 C8**
2. Finished clips appear in the grid with thumbnails; each has Approve/Reject and its sheet; approved clips sit in `Outbox\<channel>\<date>\clip-<id>\`. **C8 C9**
3. If the template guess was wrong (e.g., no facecam found), the job note says so and the owner can re-run selection with another template without re-transcribing. **C8**

### J5 — Learning loop (weekly, 5 min)
1. In YouTube Studio: Analytics → Content → Export CSV (Shorts and Videos). **owner**
2. Dashboard → Learn → drop the CSV. Import summary: matched / unmatched rows, top and bottom hooks, structures, lengths, caption styles; weight changes shown as arrows. Unmatched rows list which uploads lack a URL. **C7 C9**
3. Cadence governor may raise Shorts per day when the viewed rate is healthy; the Overview shows the new plan. **C7**

### J6 — Voices and channels (occasionally)
- **New channel**: Channels → Add; the distinctness check warns if it overlaps an existing one; a validation summary (pool sizes, publish window) appears before Save. **C7 C9**
- **Custom voice**: Channels → Voice → Design (type a description; VoiceDesign downloads on first use, 2.1 GB, with progress) or Clone (drop a 3–10 s sample, confirm rights). A sample plays; Save stores the embedding. From then on that voice loads instantly. **C4 C9**

### J7 — Something goes wrong
- **A run failed**: Overview shows a red card with the error code and plain message; Runs & Logs shows the stage; one click downloads the diagnostics zip to send to the developer. **C2 C9**
- **YouTube sends a warning/strike**: owner presses **Pause** on the channel with a note; nothing is produced until Resume; README/Operator doc lists the review checklist (§18.11). **C7 C9**
- **GPU busy** (owner coding with Nightshift Code at 03:00): the nightly run waits up to 2 h, then skips with `NS-E201` and retries the next night; Overview explains. **C2**
- **PC was off**: the task runs as soon as the PC is on (`StartWhenAvailable`); the governor never doubles up to "catch up". **C2 C7**

### J8 — Updates and uninstall
- **Update**: owner receives `nightshift-app-<ver>.zip` (or `git pull` if the repo is cloned), uses the **Update** shortcut or Settings → Update; venvs re-sync, verify runs, a changelog is shown. Models are untouched. **C2 C9**
- **Uninstall**: `UNINSTALL.cmd` (also in Settings): removes the scheduled task, shortcuts, venvs, binaries and the `NIGHTSHIFT_HOME` env var; asks separately whether to delete **models** (15 GB) and **outbox/data** (your videos and history). Default keeps both. **C1**

### J10 — Backup, transfer, time and notifications
- **Backup / move to another PC**: Settings → **Export settings** (or `ns backup`) writes `nightshift-backup-<date>.zip` with `config\`, `models\tts\voices\` (embeddings), `data\studio.db`, exemplars and prompt overrides, never models or videos (~ MBs). On a new install, `answers.json` may point `restore_from` at that zip, or Settings → **Import settings**. **C2 C9**
- **Time**: every schedule and `publish_after` is in the PC's local time zone; the zone name is written to each upload sheet so a reviewer in another zone is not misled. DST shifts are handled by the Windows task scheduler, not by Nightshift. **C7**
- **Notifications**: at the end of a nightly run (and on failure) the supervisor posts a Windows toast: "Nightshift: 1 master + 5 Shorts ready for review" / "Nightshift: run failed (NS-E203), open Dashboard". Toggle in Settings. Implemented with PowerShell `Windows.UI.Notifications`, no extra package. **C2**
- **Language**: UI and docs are English in v1; stories can be in any language the LLM and the TTS presets support (channel `language`). **C5**

### J9 — Optional: Nightshift Code
Owner opens the **Nightshift Code** shortcut; it acquires the LLM (status shown in the Dashboard strip as `owner: opencode`), opens OpenCode in `%USERPROFILE%\Projects`; closing the terminal releases the GPU. **C2**

Gaps this section closed in the spec: uninstall flow (new `UNINSTALL.cmd`, §5), reject-reason feedback into the critic bar (§18.6 "Quality drop" now also counts owner rejections), Clipper re-run without re-transcribing (§19.2 C4 is re-runnable from `transcript.json`), VoiceDesign on-demand download surfaced in the UI (§20.2 Channels page).

---

## 23. Series Workshop — develop stories with the Assistant, then produce them automatically

### 23.1 Purpose
The owner and the Assistant develop a story together before any video exists: concepts → one chosen → part map → full scripts → approval → design → automatic production. Everything lives as plain files the owner can edit by hand.

### 23.2 Folder = source of truth (`<NIGHTSHIFT_HOME>\stories\<story-slug>\`)
```
concept.md        premise, characters, setting, tone, why it works, target audience
series.md         part map: for each part a title, hook, cliff, runtime target (min), status
design.yaml       format id (§17.1), visual_treatment, caption_style, voice, thumbnail style, publish cadence
status.yaml       story status: drafting | approved | producing | published ; per-part status and job ids
parts\part-01\
  full\script.md  complete script; chapter cliffs marked [BREAK]; sentences numbered in comments for timing
  shorts\s01.md   one standalone moment per file: its own first line (hook), span reference into script.md, closing line
  notes.md        critic scores, continuity notes, owner feedback
parts\part-02\ …
```
Nightshift reads these files; it never needs the chat history to produce. Editing `script.md` by hand and re-running production uses the edited text.

### 23.3 Flow (Assistant tools, each a confirm card when it writes files)
1. `workshop_start(channel, premise?)` creates the folder and `concept.md` skeleton.
2. `workshop_concepts(n=3)` writes three concept drafts (`concept-A.md` … `concept-C.md`): premise, cast, arc in 5 beats, series potential, risks against §18.3. `workshop_regenerate(letter, feedback)` rewrites one with the owner's notes. `workshop_choose(letter)` promotes it to `concept.md`.
3. `workshop_map(parts=N, minutes_per_part=12–25)` writes `series.md`: per-part hook, cliff, what is revealed, runtime target; checks that each part can stand alone for a new viewer (one-line recap allowed) and that the series arc pays off.
4. `workshop_write_part(n)` writes `full\script.md` through the master-first text stages (§7.1 M2–M4 with the part's runtime budget: 20 min ≈ 3,100 words at 155 wpm, 8–12 chapters) plus `shorts\s*.md` through M7/M8 (4–8 standalone moments with fresh hooks). Owner reads, comments in `notes.md` or chat; `workshop_revise_part(n, feedback)` applies notes only.
5. `workshop_approve(n | all)` sets status; `workshop_design(...)` writes `design.yaml` from the channel's allowed formats with a rendered preview frame per option.
6. `workshop_produce(n | all)` enqueues one master-first job per approved part using the script files as the story source (new stage `script_import` replaces M1–M4 when `script.md` exists); Shorts come from `shorts\*.md`. Cadence governor spaces parts (default one part per week per series) and the Queue shows them as they finish.
7. Library → Stories shows every workshop story with its status; `studio series <cmd>` mirrors the tools on the command line.

### 23.4 Rules
- Parts are 12–25 minutes (per-series setting; default 15). Longer parts are allowed only after the channel's long-form average view duration exceeds 40 %.
- Shorts under a part are written as standalone moments (own first line, own closing line pointing to the part) and are validated by the `standalone_short` check.
- The policy text gate runs on `concept.md` at step 2 and on every script at step 4, so unsafe concepts are rejected before any writing time is spent.
- Series videos carry "Part N of M" in titles and descriptions (§9) and the end card instruction for the next part.

### 23.5 Clipper, revised flow (replaces the fixed rules in §19.2 C4–C5 order)
1. **Analyze with FFmpeg first**: scene cuts (`select='gt(scene,0.3)'`), loudness peaks and silences (`ebur128`, `silencedetect`), aspect and resolution, plus face/facecam detection (§19.4) sampled at 1 fps. Output `analysis.json`.
2. **Transcribe** (distil-large-v3, words + segments).
3. **Let the model choose the split**: `clip_select.md` receives the transcript windows *and* `analysis.json` (faces, facecam, scene density, peaks) and returns candidate moments **with a layout per candidate** (one of the format presets) and a reason. Auto layout is therefore a model decision informed by detection, with the §19.4 rules as the fallback when the model's choice is not applicable (`NS-W402`).
4. **One clip at a time with a logic judge**: candidates are produced in score order; before rendering each, `clip_judge.md` checks: starts on a complete sentence, ends on a payoff or punchline, understandable without outside context, no cut mid-word (snapped to word timestamps), speech density above the filler threshold, duration in range. Failing candidates are dropped with the reason in `clip_plan.json`; the next is tried, until `max_clips` or candidates run out (`NS-E407` if none).
5. Render → QC → SEO → Outbox per clip as in §19.2; the Create page shows each clip appearing as it passes.

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
