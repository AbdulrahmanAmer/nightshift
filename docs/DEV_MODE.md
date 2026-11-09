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
| s09 shortcuts | Desktop shortcuts, toast registration, `ns schedule install` | the same shortcuts and registration (real), but the task is registered **disabled** (`ns schedule install --disabled`): it exists as "Nightshift Daily", it never wakes or runs the PC. `config\schedule.json` has `"mode": "disabled"` (a dev install's task; `"switched_off"` is the different state of a task the owner switched off, which updates keep), so the supervisor and the dashboard expect no night run. Installing for real over it registers the task enabled. The time you chose is kept as a preference in `config\dashboard.json`. |
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
   stored in Settings; with a key they download from Pixabay or Pexels for real. Any tool can also be
   chosen with the explicit cue `tool: <name> {json args}` as the whole message (args optional, default `{}`),
   for example `tool: get_output {"output_id": "dev-night-tales-20261109-ab12/s1"}`; the fake then calls exactly that
   tool. Arguments that are not one JSON object, or a name that is no tool, get a plain reply that says so. A message
   that starts with `tool:` is always read as a cue, so "tool: what is the status" is answered as an unknown tool
   named `what`. The Assistant offers at most 18 tools per turn, picked from the words of the question, so many
   tools are not offered for a bare cue such as `tool: system_map` or `tool: tail_logs`. The fake then looks the
   tool up the way a model would: `list_tools` (all names), `list_tools` for the topic that holds the name, and
   then the cued call, three steps in one turn. Every Assistant tool can be reached this way from any page.
10. **Diagnostics.** `ns doctor` shows `nvidia-smi` as "not needed (dev mode)" and the bundle holds no GPU readout;
    the Settings page hides `dev_mode`.
11. **Numbers.** Nothing here measures tokens per second, real-time factor, model load time, memory or video
    memory budgets, or how long a real night takes.

If a dev install passes and the GPU PC still fails, look first at items 1 to 5.

## Real engines, fake voice (the engine test)

A dev install proves the plumbing and nothing about a model. The step between it and the GPU PC is an
install with the **real** language model, embeddings and speech recognition and only the voice faked:
`answers.json` with `"fake_voice": true` (no `--dev`). Setup then

- downloads llama.cpp (the CPU build on a PC without an NVIDIA card, `allow_cpu: true`), the LLM, embed and
  whisper files of the profile with their pinned SHA256, the Python packages including faster-whisper and
  CTranslate2;
- downloads **no** voice model and **no** qwentts.cpp, makes no PyTorch venv, and writes `tts.engine: fake` in
  `runtime.json` (`ns_supervisor.services` starts `ns_tts.server --engine fake` in the supervisor venv);
- runs `ns tune` and the smoke test against the real `llama-server.exe`, the real embeddings and the real
  speech recognition; the voice rows pass on the fake engine (`engine_kind: fake`, no NS-W301).

Everything else is a real install: the nightly task is enabled, `dev_mode` is false, the Settings page shows
no dev banner. It is an `answers.json` setting only; the setup window never asks it. To make the download
small, the orchestrator's laptop run edited the **extracted** bundle's `manifest.json` (`profiles.cpu-dev.llm`
pointed at `Qwen/Qwen3-0.6B-GGUF` `Qwen3-0.6B-Q8_0.gguf`, 639 MB, with its Hugging Face SHA256 and revision);
the shipped manifest is unchanged. What such a run proves that a dev pass does not: items 2 (the language
model starts, answers JSON, `ns tune`), 3 (embeddings), 4 (speech recognition on the processor) and the
download path with real hashes. It still proves nothing about the GPU, NVENC, the voice engine or quality.

### What the fake voice is on an engine test

Real speech recognition cannot align tone bursts, so with a tone-burst voice every story and script job stopped
at captions with NS-E403 ("drift inf ms, matched 0%", defect D-26). The fake voice of an engine test (`tts.engine:
fake` and not `dev_mode`) therefore **speaks the actual text with the speech synthesizer that ships with
Windows** (SAPI, `System.Speech.Synthesis.SpeechSynthesizer`, started through Windows PowerShell 5.1): no
download, no model, CPU only. It is still a fake: one Windows voice stands in for every preset, designed and
cloned voice (they differ only in what the library stores for them), and Settings shows `tts.engine: fake` as
before.

- **Same service contract.** `POST /v1/audio/speech` and `/v1/audio/speech/batch`, the voice list, design and
  clone are the code of the real service; only the engine behind them is the fake. A batch is one synthesizer
  run for all its sentences.
- **Which voice.** Chosen from what is installed, so two runs on one PC pick the same one: English first
  (en-US, then the other en-*), then by name. On a stock Windows 10 or 11 that is Microsoft David Desktop
  (Zira sorts after it). `GET /health` of the voice service says which: `voice_mode` (`sapi`), `voice` (its
  name) and `voice_wpm` (its pace at SAPI rate 0, measured by one test sentence at start-up).
- **Speed.** The voice's `speed` setting picks the SAPI rate (an integer, -10 to 10, about 11 % per step) whose
  pace is closest to 155 words a minute times the speed, so speed 1.0 lands within about 6 % of the pace the
  planner and the tone bursts assume and the extremes are limited to what SAPI offers.
- **Output the same checks accept.** 16-bit mono WAV at the service's 24 kHz; peaks held under -1 dBFS (SAPI
  voices peak near full scale); the service trims each sentence's silence and puts its own pause between
  sentences, exactly as for the real engine; the voice stage then loudness-normalizes to -16 LUFS with the
  true-peak ceiling the Short and master stages rely on, and writes the script sidecar next to each clip
  (`tests/test_fake_voice_note.py::test_the_voice_stage_treats_the_windows_speaking_fake_like_real_engine_output`).
- **A PC with no SAPI voice** (a stripped Windows, a Windows without PowerShell, a non-Windows test runner) gets
  today's tone bursts and says so: `/health` has `voice_mode: tones`, `voice_fallback: true` and
  `voice_warning` with the reason and `voice_warning_code: NS-W303`; the voice service log and the job log carry
  the warning `fake_voice_tones_only` with `code: NS-W303` ("The fake voice made tone bursts, which speech
  recognition cannot align."); the job keeps `audio\voice_engine.json` (`mode`, `voice`, `warning`, and `code`
  when the mode is `tones`); and the NS-E403 of the captions stage ends with "the voice is the fake one of this engine test and
  it made tone bursts ... this is the test voice, not a product fault". With a Windows voice the same sentence
  names the voice. A real engine never gets that sentence.
  The start-up check (voice list, one test sentence) is capped at 80 s in total so a hung PowerShell ends in
  tone bursts and this warning, never in a service the supervisor kills for not answering.
- **Lines the Windows voice cannot speak.** The one Windows voice is English: text in another script (Japanese,
  for example) or only punctuation comes back as no sound. The clip is then silent and the voice service log
  carries the warning `fake_voice_unspoken` (the line, and "it speaks English" when the request names another
  language); the captions stage would stop with NS-E403 otherwise without saying why.
- **Switches.** Only `tts.engine: fake` without `dev_mode` asks for Windows speech. A dev install, the test suite
  and CI (`NS_TTS_FAKE=1`) keep the tone bursts. `NS_TTS_FAKE_SPEECH=sapi` or `tones` forces either way, for a
  developer who wants to hear the dev install or to take the engine test back to tones.

The twin answer is `"fake_footage": true`. A story job needs a Pixabay or Pexels key and stops with NS-E502
without one, which blocks an engine test on a PC with no stock-footage account. With the answer, setup writes
`fake_footage: true` into `runtime.json` and the studio, the nightly planner, `studio run`, the dashboard's
job refusal and Library > Footage all use the synthetic clip source, as a dev install does, while the language
model, embeddings and speech recognition stay real. The videos then carry placeholder footage, so the answer
is for an engine test only and never for the partner's PC. Settings shows it as `fake_footage` next to
`dev_mode` in the settings payload.

### A tiny model finishes a story (`tiny_story`)

The shipped word budgets assume a model that writes 300 to 450 words a chapter. A 1 to 2 GB model on the
processor (the engine-test laptop) wrote a Short of 40 words at reading grade 0.21 against a budget of 54 to 72
(Qwen3-1.7B) and one of 104 words at grade 10.1 against 60 to 80 (Qwen3-0.6B), both in `test-loop-2026-11-08.md`,
so the story job stopped at the text validators with `NS-E303` and never reached the voice. The third engine-test answer, `"tiny_story": true`,
puts the text stages on the thresholds such a model can meet:

- **Where it lives.** `ns_studio/text/story_profile.py` is the one place. `NORMAL` holds the numbers every install
  has always used; `TINY` holds the scaled ones. The budgets (`ns_studio/text/budget.py`), the validators
  (`validators.py`), the stages, the series workshop, the duration check and the prompts take the profile of the
  job (`profile_of(ctx)`, read from `runtime.json`) and read its fields. Since H-8b so do render, QC, the final
  gate's runtime check and the outbox (`duration_profile`, `job_mark`); the quality checks do not read it.
- **What moves** (tiny against normal). A chapter may have 0.5 to 2 times its word budget (0.8 to 1.2). A Short is
  still asked for the words that fill its length bucket, because QC measures the rendered Short against that
  bucket, but may have 0.35 to 1.6 times them (about 0.85 to 1.15); QC takes the same shares of the bucket. A
  master is planned for what such a model writes (H-8b): 4 minutes in 5 chapters (about 110 words each, 549
  words), with a planning floor of half its words, a "story is short" warning at half its target (80 %), a
  duration-check floor of 2:00 (9:00, and 9:00 for a script of the owner's own), a text-estimate ceiling of 8:00
  (14:30) and a chapter band of 0.5 to 3 times its budget (0.8 to 1.2). The reading
  grade floor is -2 (5); the prompts ask for grade 3-8 (5-8), 1 to 2 beats a chapter (2 to 4) and 3 to 4 beats a
  Short (4 to 7). A master must yield 2 Shorts (the channel's 4), an expansion must add a quarter of the words
  it asked for (half), and a series part uses chapters of about 190 words (350) and is rejected below 40 % of its
  word target (75 %).
- **How long a tiny video is (H-8b).** The numbers are in `story_profile.py` and are derived there, not tuned
  per stage. The only chapter sizes of the real Qwen3-1.7B in a log are the five of job
  `engine-test-20261109-8071` (asked for 394 words each: 146, 99, 126, 162 and 323 words, 856 in all; the stage
  measured chapter 1 at 120 after the critic's rewrite). At 155 wpm a word takes 60/155 s of speech plus 0.035 s of
  pauses, a chapter break 2.05 s of title card and cliff pause: five chapters of 110 words are 549 words, 240 s.
  That is the plan; the duration check (`NS-E305`) accepts a master from half of it, 120 s; the text estimate
  stops one over 480 s; QC's window is 110 to 900 s (the floor less 10 s; the normal 15:00 ceiling, so that
  a tiny job planned on 0.1.14 at 9:00 and resumed on this version still passes QC), and the final policy gate's
  runtime check uses the same minimum. The 1.7B's 856 words are 6:09,
  inside all of them; a tenth of that is under every floor and still fails (`NS-E303`, `NS-E305`, `NS-E404`).
  A Short is judged at 0.35 to 1.6 of its length bucket by the text band and by QC alike. The `NS-E305` detail
  names the floor in effect (`floor 120 s, 2:00`; a normal install's text is unchanged).
- **What does not move.** Every validator still runs and can still fail: a chapter under 0.5 times its budget, a
  grade under -2, a banned phrase, a sentence over 35 words, a repeated opening and repeated cliffs all stop the
  job with `NS-E303`, as before. The reading-grade ceiling (8) is unchanged. The policy and safety gates (the
  banned phrases, clip reuse, render uniqueness, the audio check, the mid-roll margin) do not read the profile,
  and neither do QC's loudness and true peak, resolution, caption drift, black frames, hook and file checks:
  each still fails the video that breaks it, with the profile on or off. The one gate number that moves is the
  final gate's 8:30 minimum, which is a length like QC's.
- **A test video says so (H-8b).** A video made while the switch is on carries `"profile": "tiny_story"` in its
  `render/report.json` and `render/long.json`, its `qc/<item>.json` and `qc/report.json`, its outbox sidecar
  (`.json`) and its `outputs` row; its `.txt` upload sheet opens with `TEST OUTPUT (tiny_story) ... Do not
  upload`. The marker is sticky: a run of a job with the switch on leaves `profile.json` in the job folder, so a
  job resumed after the switch was turned off stays marked (its numbers follow the install, so the strict ones
  apply). A clip job is never marked. The Queue shows a "Test output" badge, and `studio mark-uploaded`, the
  dashboard's "I posted it" route and the nightly scheduler's publish slots refuse it with `NS-E329`; a
  reviewer may still approve or reject it. Reports and sidecars from before the marker have no `profile` key and
  load as before, and a normal install writes no `profile` key at all.
- **How to switch it on.** `answers.json` of a PC with no NVIDIA card: `"allow_cpu": true, "tiny_story": true`.
  Setup refuses the answer without `allow_cpu` (`tiny_story is for an engine test ...`), writes
  `"tiny_story": true` into `runtime.json`, and a dev install ignores it (it runs a fake model on fixtures). The
  setup window never asks it. Settings shows it as `tiny_story` next to `dev_mode` and `fake_footage`; it is not
  an engine flag, and a settings call that names `tiny_story`, `fake_footage` or `dev_mode` is refused with
  `test_switch` (HTTP 422) however it is sent, so a normal install cannot turn it on from the dashboard.
- **Tests.** `tests/test_tiny_story.py` (a fake model that writes at the tiny scale passes all nine master text
  stages, one that writes less still fails with `NS-E303`, the same model fails on a normal install, and every
  normal-profile number is asserted as it was), `tests/test_tiny_story_switch.py` (refused without `allow_cpu`,
  setup to `runtime.json`, the dashboard cannot set it), one reinstall test in
  `tests/test_installer_reinstall.py` and, for the lengths and the marker, `tests/test_tiny_story_length.py` (every
  threshold with the profile on and off, each unchanged QC check failing under the profile, the markers in the
  reports, the sidecar, the sheet and the row, the sticky note on resume, the refusals; one run through FFmpeg
  that only CI makes).

## Developer switches

Environment variables and answers that exist for development and the engine test. None is set by the product
and none changes a real install; the voice-service ones are read by the **fake** engine only, when the voice
service starts, from the environment of the process that starts it (the supervisor hands its environment to the
service, so set them before starting the engine, and restart the engine to change them).

| Switch | Where | What it does |
| --- | --- | --- |
| `INSTALL.cmd --dev`, `NIGHTSHIFT_DEV_MODE=1` | installer | the dev install above: fake engines, stub models, 10 GB |
| `"fake_voice": true` in `answers.json` | installer | the engine test above: real models, the fake voice (`tts.engine: fake`) |
| `"fake_footage": true` in `answers.json` | installer | the synthetic clip source with real models |
| `"tiny_story": true` in `answers.json` | installer | the text stages' thresholds of a 1 to 2 GB model (needs `allow_cpu`; "A tiny model finishes a story" above) |
| `NS_TTS_FAKE=1` | voice service | use the fake engine whatever `runtime.json` says (the test suite, CI); tone bursts |
| `NS_TTS_FAKE_SPEECH=sapi` or `tones` | fake engine | force Windows speech or tone bursts for the fake voice (see "What the fake voice is") |
| `NS_TTS_FAKE_DELAY_S=<seconds>` | fake engine | make design, clone and speech wait that long before they answer (below); default 0, at most 120 |

### `NS_TTS_FAKE_DELAY_S`: a slow fake voice engine

The fake engine designs and clones a voice in an instant, so a Cancel could never land while "Create it now" is
running, and the release of the `studio:voice-<name>` lease could not be timed (box F-E12). With the variable
set, the fake engine waits that many seconds, in slices of 0.1 s, before it answers a design, a clone or a
speech request:

- **Set it** in the environment of the process that starts the engine (the supervisor hands its environment to
  the voice service) and start the engine afterwards: the voice service reads it once, when it starts, so a
  running engine keeps what it began with. Unset, empty, `0`, negative or not a number mean no delay (a value
  that is not a number is logged as `fake_delay_ignored`); anything above 120 is 120. `GET /health` of the voice
  service shows `fake_delay_s` while a delay is on.
- **One wait per request.** A clone waits once (the sample it stores is part of the same request); a speech
  batch waits once per engine call. The log events `fake_delay` and `fake_delay_cancelled` (`logs\tts-*.jsonl`)
  say when a wait started and why it ended early.
- **Cancel ends it at once.** The wait stops within 0.1 s when the studio asks the service to cancel
  (`POST /v1/audio/speech/cancel`, which its client sends when a batch times out) or when the client closes its
  connection, which is what the dashboard's Cancel does to a running voice job (its child process is stopped).
  The request then answers NS-E310 ("the wait of the fake engine was cancelled") before anything is written, so
  no half-made voice folder, clip or resume marker is left, the service lock is free for the next request and
  the same voice name can be used again.
- **Only the fake.** The qwentts and PyTorch engines never read the variable; with a real engine it does
  nothing.

## Where the code is

| What | Where |
| --- | --- |
| the switch | `ns_common/devmode.py` (reads `runtime.json`), `installer/stage2/devmode.py` (numbers, stubs, mode switch) |
| stage 1 | `installer/stage1.ps1` (`-Dev`, `Get-PreflightRows -DevMode`) |
| setup server | `installer/stage2/web/` (`Config.dev_mode`, `preflight.apply_dev`, `plan.build_plan(dev=...)`, `state.devMode`) |
| wizard steps | `installer/stage2/steps/s01` to `s10` (`if ctx.dev_mode`), `dev_stubs.py` |
| fake services | `ns_supervisor/devfake.py`, `ns_supervisor/services.py`, `ns_supervisor/verify.py` |
| the fake voice | `ns_tts/engine.py` (`FakeEngine`), `ns_tts/sapi.py` (Windows speech), `ns_studio/media/fake_voice.py` (job log, NS-E403 note) |
| fake jobs | `ns_studio/jobs.py`, `ns_studio/series/workshop.py`, `ns_dashboard/studio_job.py` |
| disabled task | `ns_supervisor/schedule.py` (`disabled=True`), `schedule_cfg.py` |
| contracts | `Runtime.dev_mode`, `InstallState.dev_mode`, `GateReport.dev_mode` and their schemas |
