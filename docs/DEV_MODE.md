# Dev mode: a real install on a PC with no NVIDIA card

`INSTALL.cmd --dev` runs the **actual installer** end to end on a Windows PC that has no NVIDIA graphics card
and not much room (an 8 GB laptop is enough), **without downloading any AI model or PyTorch**. It exists so the
orchestrator can exercise the real call paths (stage 1, the setup server, the WebView2 or Edge window, steps
s01 to s10, shortcuts, the first dashboard launch, the supervisor, a "Create it now" job) before a build goes to
the partner's GPU PC.

It is **not** `--simulate`. `--simulate` fakes the steps themselves (nothing is installed). Dev mode runs the
real steps and changes only what a laptop cannot do. Everything it changes is an `if ctx.dev_mode` branch (or
a read of `dev_mode` in `runtime.json`), and each branch has a test (`tests/test_installer_dev_mode.py`,
`test_installer_web_dev.py`, `test_installer_stage1_dev.py`, `test_dev_mode_core.py`, `test_dev_mode_job.py`).
A real install does not run any of it.

## Run it

From a Command Prompt in the extracted folder (a double click cannot pass an argument):

```
INSTALL.cmd --dev
```

or, with the environment variable (the same thing; it reaches stage 1, the setup server and the wizard):

```
set NIGHTSHIFT_DEV_MODE=1
INSTALL.cmd
```

The console and the setup window say **DEV MODE: fake engines, no models** (the console header, the Welcome
status line, the footer of every setup screen, the Done screen, `ns verify`'s report). Use the window as
usual: Welcome, Check this PC, Your choices, the download and install screens, Test, Done. Press **Open the
dashboard** at the end, or use the Desktop shortcut "Nightshift Dashboard", then press **Create it now**.

The install folder is whatever you pick (default: the drive with the most free space, `<drive>:\Nightshift`).
Keep it on a drive with at least 10 GB free.

### Uninstall

`UNINSTALL.cmd` works as for a real install (it removes the shortcuts, the notification registration, the
disabled night task, the `NIGHTSHIFT_HOME` variable, the FFmpeg PATH entry setup added and, if you tick the
boxes, the models and your data). Nothing is dev-specific. Run it before you try a **real** install on the
same PC for the first time: installing for real over a dev install works (setup notices the other mode, redoes
the later steps and replaces the stubs, `installer/stage2/devmode.py: reset_for_mode_switch`), but a clean
folder is the honest test.

## What each stage does in dev mode

| Stage | Real install | Dev mode |
| --- | --- | --- |
| `INSTALL.cmd` and stage 1 | probes the PC, blocks on no GPU, a driver below CUDA 12.4, under 40 GB free | `-Dev` (or `--dev`, or `NIGHTSHIFT_DEV_MODE=1`): the graphics card, driver and video memory rows are **information rows** ("dev mode, not needed"), memory warns under 4 GB, disk needs **10 GB**. Profile is always `cpu-dev`, no llama.cpp pair. uv, Python 3.12, the Visual C++ runtime and the network checks are real. The hand-over JSON has `"dev_mode": true`. |
| Setup server and window | plan lists the CUDA build, OpenCode, the models | plan lists the CPU build, yt-dlp, the window files, FFmpeg and the models as "(stub, dev)"; Small only; the footer carries the DEV MODE banner; `state.devMode` is true. FFmpeg (winget, else the pinned build), Windows Terminal, the Visual C++ runtime and the setup venv are real. |
| s01 questions | 60 GB (+14 or +21 for bigger tiers) | 10 GB; the answers are forced to Small, no Nightshift Code, no voice design model, no models folder (`StepContext.constrain`). `config\answers.json` records that. |
| s02 programs | CUDA llama.cpp + cudart, OpenCode, yt-dlp, window files, qwentts.cpp | **real downloads** of the CPU llama.cpp build, yt-dlp.exe and the WebView2 files. **Skipped** (said in the log): cudart, OpenCode, qwentts.cpp. No PyTorch fallback is scheduled (`tts_fallback` false). |
| s03 models | ~15 GB of GGUFs, Whisper, YuNet with SHA256 | **stub files** with the real manifest names (`Qwen3-1.7B-Q8_0.gguf`, `nomic-embed-text-v1.5.Q8_0.gguf`, `qwen-talker-0.6b-customvoice-Q8_0.gguf`, `qwen-tokenizer-12hz-Q8_0.gguf`, Whisper `tiny` `model.bin` and its JSON files): 4 KB each, header `NIGHTSHIFT-DEV-STUB`, next to a `<name>.dev-stub` marker and a download marker whose hash is `dev-stub`, so a real install never accepts one. The face finder (YuNet, 230 KB) is downloaded for real. Verification accepts stubs (`ns verify --downloads` says "stub (dev)"). |
| s04 environments | `studio` and `supervisor` venvs (faster-whisper, CTranslate2, cuBLAS, cuDNN), `tts` venv only on the fallback | real `uv` venvs with the real project installed, but the studio venv gets the **`dev-install`** extra (`pyproject.toml`): `studio` minus faster-whisper, PyAV and CTranslate2; **no** cuBLAS/cuDNN wheels, no torch, no `tts` venv. The spaCy model wheel is real. `ns update` re-syncs the same extras. |
| s05 Nightshift Code | OpenCode first start | skipped ("dev mode") |
| s06 tune | `ns tune` (1 to 3 min) | skipped ("nothing to tune"); the baseline `runtime.json` stays |
| s07 configs | channel, `runtime.json`, `opencode.json`, keys | the same, except no `opencode.json`. `runtime.json` gets `"profile": "cpu-dev"` and `"dev_mode": true`; the voice engine entry is `qwentts` so the voice service runs in the supervisor venv. |
| s08 smoke | `ns verify --quick`, a Whisper transcription | `ns verify --quick` **against the fakes** (below); the Whisper probe is skipped. `verify-report.json` says `"dev_mode": true`. |
| v01 samples | a spoken sample and a test render | unchanged: the studio is in dev mode, so the voice is the fake engine's tone narration and the render is a real FFmpeg video |
| s09 shortcuts | Desktop shortcuts, toast registration, `ns schedule install` | the same shortcuts and registration (real), but the task is registered **disabled** (`ns schedule install --disabled`): it exists as "Nightshift Daily", it never wakes or runs the PC. `config\schedule.json` has `"mode": "disabled"`, so the supervisor and the dashboard expect no night run. The time you chose is kept as a preference in `config\dashboard.json`. |
| s10 summary | status, locations, diagnostics zip | the same, with the banner and "Create it now" as the next step |

### The fake engines

`runtime.json` has `dev_mode: true`. Everything reads that flag through `ns_common.devmode`:

- **Supervisor** (`ns_supervisor/services.py`): `llama` and `embed` run `python -m ns_supervisor.devfake`
  (standard library; `/health`, `/v1/chat/completions` answering the schema the client sent, `/completion`,
  `/v1/embeddings` returning deterministic vectors), `tts` runs the real `ns_tts.server --engine fake`
  (tone-burst narration, the preset voices, design and clone). None needs the GPU, so the GPU lock is never
  contended. The ports and health checks are the real ones (8080, 8081, 8091, 8099, 8100).
- **`ns verify`**: the same checks, the names `check_names.QUICK_CHECKS` lists. Whisper load, NVENC and idle VRAM
  become "dev mode" passes (NVENC is replaced by a 2 second `libx264` encode, so FFmpeg is proven for real).
- **Studio and dashboard jobs**: `ns_studio.jobs.open_env`, `default_workshop` and the dashboard's studio child
  (`ns_dashboard.studio_job`) turn every service into the in-process fakes of `ns_studio.context.FAKEABLE`
  (`FakeLlm` with the fixtures under `tests/fixtures/llm`, `FakeTts`, `FakeTranscriber`, `FakeEmbed`,
  `FakeClipSource`) when `runtime.json` says `dev_mode`. A "Create it now" job runs every stage of the format and
  ends with a real FFmpeg render into the outbox (`libx264`).

### First dashboard launch and "Create it now"

The shortcut starts the real supervisor and the real dashboard (port 8100). The Library, Voices (design and
clone use the fake engine), Settings, Queue, Review and the Assistant work. The fake llama has no
opinions, but it turns plain requests into tool calls so the Assistant's whole path can be tried: "fetch
two clips of a dark hallway", "what is the status", "pause Dev Night Tales", "where is the footage
cache", "make a Short for dev-night-tales", "what is ready to review"; anything else gets a fixed
sentence. After a Yes the card names the files or job the tool made. **Create it now** creates a job that runs the whole
pipeline on the fakes and puts a real video in `<home>\outbox`. A Shorts job takes a few minutes. A channel's
long video (the default format) is a 9 to 14 minute master that FFmpeg encodes with `libx264` on the laptop's CPU,
so allow much longer, or pick Shorts only in Create for the quick pass.

## What it needs

| | |
| --- | --- |
| Free disk | at least **10 GB** on the install drive (the check). Used at the end: about 2.5 to 3 GB (FFmpeg ~0.6 GB when setup installs its own, two Python environments ~1.5 GB, the app, the stubs, logs). |
| Memory | 8 GB is plenty. The installer's pip runs peak near 1 GB; afterwards the supervisor, the dashboard, the two fakes, the studio child and FFmpeg stay under about 2.5 GB. |
| Time | 10 to 20 minutes on a normal line (it is the Python packages that take time). |
| Windows | 10 or 11, 64-bit; Edge/WebView2 for the window (the browser address is printed if it cannot open). |

What is downloaded (sizes of the pinned files; a re-run downloads only what is missing):

| What | Size |
| --- | --- |
| llama.cpp CPU build (`bins.llama_cpu`) | 24.6 MB |
| yt-dlp.exe | 23.6 MB |
| WebView2 NuGet package (three DLLs are unpacked) | 8.8 MB |
| YuNet face finder | 0.2 MB |
| uv (stage 1, if missing) | 15.2 MB |
| Python 3.12 (uv, if missing) | about 30 MB |
| FFmpeg (winget's build, or the pinned build when winget is not usable) | about 100 to 194 MB |
| Visual C++ runtime (only if missing) | about 25 MB |
| spaCy model wheel `en_core_web_md` | 33.5 MB |
| Python packages for the two venvs (spaCy, NumPy, OpenCV, SciPy, Pillow, FastAPI, uvicorn, ...) | roughly 250 to 350 MB |
| **Not** downloaded | every GGUF and Whisper model (written as 4 KB stubs), cudart, the CUDA llama.cpp builds, OpenCode, qwentts.cpp, PyTorch, faster-whisper, CTranslate2, cuBLAS and cuDNN |

In total the programs and models of a dev install come to **about 57 MB** (llama.cpp CPU, yt-dlp, the window
files, YuNet) plus the tools and packages above: **roughly 0.5 to 0.8 GB** in all.

## What a dev pass does NOT prove

A green dev install proves the installer, the supervisor, the dashboard and the studio plumbing run on real
Windows. It proves **nothing** about anything that needs the GPU or a model. The places where it differs from a
real install:

1. **The GPU path.** No `nvidia-smi`, no driver or CUDA checks (NS-E102, NS-E103, NS-W115, the RTX 50-series
   rule), no CUDA llama.cpp or cudart download, no VRAM measurement, no GPU lease contention (every dev service
   has `gpu = false`), no NVENC (the render uses `libx264`), `Status.gpu` is null.
2. **The language model.** `llama-server.exe` is downloaded and unpacked but **never started**: the CPU build is
   not run, so the Visual C++ runtime for it, a blocked program (NS-E113) or a taken port 8080 against the real
   server are not exercised. Prompts, JSON repair, story length, the word budgets and the policy text checks run
   against fixtures (the text checks that skip "fake LLM" output are skipped), so story quality, the 16K context
   fit, MTP and `--n-cpu-moe` flags are unproven. `ns tune` never runs.
3. **The models.** No Hugging Face download, no pinned SHA256 or revision check on a model, no resume of a
   multi-GB file, no Drive-zip or folder source, no custom model, no Balanced or Best tier, no disk-space
   behaviour at 15 to 50 GB. (The real downloader does run, on the small programs and YuNet, so its resume and
   hash code is exercised on those.)
4. **The voice.** `qwen-tts.exe`, the GGUF talkers, the speaker files, VoiceDesign and clone with the real
   engine, the PyTorch fallback (NS-W301, NS-E108) and the real-time factor and batch timeouts are unproven; the
   fake engine speaks tone bursts.
5. **Speech recognition and captions.** No faster-whisper, CTranslate2, cuBLAS/cuDNN, no PyAV: the
   `[studio]` extra's wheels and their version caps (`tests/test_install_caps.py`) are not installed here, the
   CPU fallback (NS-W404) is not tried, and caption alignment against real speech (`drift_ms`) is untested. The
   Clipper cannot transcribe an arbitrary video: the fake transcriber needs a `<video>.transcript.json` sidecar
   (NS-E406 otherwise), and the voice-clone "words in the recording" step returns a fixed sentence.
6. **Stock footage and policy.** Footage is `FakeClipSource`: no Pexels or Pixabay search or download, no
   perceptual-hash repeat check (skipped for fake footage). Key validation in s01 is real (if you give keys).
7. **The night run.** The task is disabled, so the 03:00 start, the wake timer, S4U versus "only while signed in"
   (NS-W205), `ns studio run --daily` and the post-run behaviour are not exercised. Registering the task is.
8. **Nightshift Code.** OpenCode is not installed, `opencode.json` is not written, `nightshift-code.ps1` has no
   shortcut.
8b. **Clips from an upload.** The fake transcriber needs a sidecar; an upload has none at import time. Drop
   `source.transcript.json` next to the stored file (`imports\clipper\<source id>\source.mp4`) before
   starting the clip job: the transcribe stage looks there when no sidecar was recorded.
9. **The Assistant** runs on keyword routing in the fake llama (`ns_supervisor.devfake.plan_tool_call`): the
   confirm card, the tool run and the result are real, the wording and the choice of tool on a real model are
   not. Library > Footage and the Assistant's `fetch_footage` use the synthetic clip source unless a key is
   stored in Settings; with a key they download from Pixabay or Pexels for real.
10. **Diagnostics.** `ns doctor` shows `nvidia-smi` as "not needed (dev mode)" and the bundle holds no GPU readout;
    the Settings page hides `dev_mode`.
11. **Numbers.** Nothing here measures tokens per second, real-time factor, model load time, memory or video
    memory budgets, or how long a real night takes.

If a dev install passes and the GPU PC still fails, look first at items 1 to 5.

## Where the code is

| What | Where |
| --- | --- |
| the switch | `ns_common/devmode.py` (reads `runtime.json`), `installer/stage2/devmode.py` (numbers, stubs, mode switch) |
| stage 1 | `installer/stage1.ps1` (`-Dev`, `Get-PreflightRows -DevMode`) |
| setup server | `installer/stage2/web/` (`Config.dev_mode`, `preflight.apply_dev`, `plan.build_plan(dev=...)`, `state.devMode`) |
| wizard steps | `installer/stage2/steps/s01` to `s10` (`if ctx.dev_mode`), `dev_stubs.py` |
| fake services | `ns_supervisor/devfake.py`, `ns_supervisor/services.py`, `ns_supervisor/verify.py` |
| fake jobs | `ns_studio/jobs.py`, `ns_studio/series/workshop.py`, `ns_dashboard/studio_job.py` |
| disabled task | `ns_supervisor/schedule.py` (`disabled=True`), `schedule_cfg.py` |
| contracts | `Runtime.dev_mode`, `InstallState.dev_mode`, `GateReport.dev_mode` and their schemas |
