# Nightshift — Build Spec (for Claude Code, parallel one-shot build)

> **Nightshift** is a Windows one-click bundle that turns one RTX 5060 Ti (16 GB) PC into a **private local coding assistant** and an **overnight YouTube story studio** that makes a **long-form video first, then cuts it into Shorts**, so one story earns on both formats.
> The target device needs **no Claude account, no paid AI keys, and no cloud CLI**. Claude Code is only the *builder*.
> The build runs in **waves**. The main session owns every shared contract (§15). Worker agents build their packages in parallel and never edit shared files.

---

## A. Product description (also used verbatim as installer and README text)

### A.1 One line
**Nightshift works while you sleep: it writes an original story, turns it into a full long-form YouTube video, then cuts the best moments into Shorts that send viewers back to it — plus it gives you a free, private AI coding assistant. Everything runs on your own PC.**

### A.2 Installer welcome screen (exact text for `Nightshift-Setup.exe`)
> **Welcome to Nightshift**
>
> Nightshift sets up two things on this PC:
> 1. **Nightshift Code** — an AI coding assistant in your terminal, powered by a model running on your own graphics card. No account, no subscription, nothing leaves your PC.
> 2. **Nightshift Studio** — a story-to-video factory. It writes an original story, narrates it, adds captions and matching stock footage, and produces **one long-form video (10+ minutes, with chapters and ad-break points)** plus **several vertical Shorts cut from it**, each with its own hook. Everything lands in your Outbox, ready for you to review and post.
>
> **What setup will do:** check your PC (graphics card, driver, memory, disk space), install the free tools Nightshift needs, download the AI models (~45 GB, so allow time), tune them for your hardware, and run a quick test of everything. You'll answer a few questions along the way. You can close and re-run setup at any time; it picks up where it left off.
>
> **You'll need:** an NVIDIA RTX GPU with 16 GB, ~80 GB of free disk space, an internet connection during setup, and two free stock-footage API keys (Pexels and Pixabay). Setup shows you where to get them.

### A.3 How it installs itself
1. **Checks the PC**: Windows version, NVIDIA GPU, driver, RAM, free disk, internet. It tells you exactly what to fix if something is missing.
2. **Installs free tools**: Git, Windows Terminal, FFmpeg, Python (via uv). Everything it already finds is skipped.
3. **Asks you a few questions**: first channel name and niche, language, narrator voice (listen to samples), stock-footage keys, how many videos per day, what time to run.
4. **Downloads the AI engine and models** from their official sources, verifying every file.
5. **Tunes itself** to your exact hardware for maximum speed.
6. **Tests everything** and shows a pass/fail checklist.
7. **Creates desktop shortcuts** and a nightly scheduled task.

### A.4 What it does after install
- **Nightshift Code** (shortcut): opens a terminal coding assistant (OpenCode) running on a local Qwen model. It reads, writes and edits code in your projects folder, like Claude Code or Codex, fully offline.
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
- **You** review each video (`approve` / `reject`) and upload approved ones in YouTube Studio.
- Over time, you can feed it your YouTube Analytics export so it learns which hooks, styles and lengths work best for each channel.

### A.5 What it takes in
| Input | When | Required |
|---|---|---|
| Channel name, niche, language | Setup / `studio channel add` | Yes |
| Narrator voice: preset, described in words, or cloned from a 3–10 s clip **you have rights to** | Setup / anytime | Yes |
| Pexels + Pixabay API keys (free) | Setup | Yes |
| Videos per day, run time, publish window | Setup | Defaults provided |
| Optional: seed keywords, topics to avoid, style notes | Channel profile | No |
| Optional: your own example stories | `studio/exemplars/` | No |
| Optional: YouTube Analytics CSV export | `studio learn --import` | No |
| Coding: your project files and instructions | Nightshift Code | — |

### A.6 What it puts out
| Output | Where |
|---|---|
| **Long-form master**: 1920×1080 MP4, 10–14 min, narration + captions + stock footage, **no music**, chapters | `Outbox\<channel>\<date>\<job>\long\` |
| **Thumbnails**: 3 variants (1280×720) for the long-form | same folder |
| **Shorts**: 4–6 vertical 1080×1920 MP4s cut from the master, each with its own voiced hook and vertical edit | `Outbox\<channel>\<date>\<job>\shorts\` |
| Upload sheet per video (`.json` + copy-ready `.txt`): title, description, tags, hashtags, chapters (long), suggested publish time, suggested mid-roll points (long), "Related video" instruction (Shorts), AI-disclosure answer, "made for kids" answer | next to each MP4 |
| Daily run report: what was made, what was rejected and why, timings | `logs\` + `studio status` |
| Coding: code changes in your projects | your Projects folder |

---

## 0. Ground rules for the builder

1. **Never bundle models or large binaries.** The installer downloads everything from pinned sources.
2. **Pin everything** in `manifest.json` (versions, URLs, HF repo + filename, SHA256). The main session resolves and verifies them in Wave 0.
3. **Contracts first.** Every shared name, path, port, schema, API and CLI command is defined in §15 and frozen before Wave 1.
4. **Exclusive ownership.** Each work package owns only its directories (§12). Contract changes go to `contracts/CHANGE_REQUESTS.md`. Only the main session applies them.
5. **Verify, don't assume.** Every install step checks before it runs and verifies after.
6. **Idempotent and resumable.**
7. **Everything visible.** The wizard runs in a visible terminal and logs per §15.10.
8. **One GPU tenant at a time** (LLM, TTS or Whisper), enforced by the supervisor.
9. **No hidden network calls at runtime** except the stock-footage APIs (and the optional YouTube API, Phase 9).
10. **YouTube safety is a build requirement, not a feature.** §18 rules are enforced in code by the policy gate.

---

## 1. Target machine (detected, not assumed)

| Item | Expectation | Wizard behaviour |
|---|---|---|
| OS | Windows 10/11 x64 | Block on ARM64 or 32-bit (`NS-E101`) |
| GPU | RTX 5060 Ti 16 GB (Blackwell, **sm_120**) | `nvidia-smi`; block if no NVIDIA GPU (`NS-E102`) |
| Driver | `nvidia-smi` reports CUDA ≥ 12.8 | Open the NVIDIA driver page, pause, re-check (`NS-E103`) |
| RAM | 32 GB recommended, 16 GB minimum | Selects the hardware profile (§3) |
| Disk | ≥ 80 GB free | Block if too small (`NS-E104`) |
| Network | HF, GitHub, PyTorch, Pexels, Pixabay reachable | `NS-E105` |

**Blackwell:** PyTorch must be a **CUDA 12.8+ build (2.7+)**, installed from the cu128 (or newer) index URL.

---

## 2. Final stack

| Layer | Choice | Notes |
|---|---|---|
| LLM runtime | **llama.cpp `llama-server`**, official Windows CUDA release + cudart; build **after PR #23269** | MoE offload, MTP, OpenAI-compatible API |
| Coding model (`code`) | **Qwen3.6-35B-A3B**, MTP-tagged GGUF. Prefer `mudler/Qwen3.6-35B-A3B-APEX-GGUF` I-Compact (~17 GB) if it carries the MTP head; else `ggml-org/` or `unsloth/Qwen3.6-35B-A3B-MTP-GGUF`. 16 GB-RAM fallback: APEX I-Mini (~14 GB) | Wave 0 decides |
| Story/SEO model (`story`) | **Qwen3.6-35B-A3B abliterated**: `Youssofal/Qwen3.6-35B-A3B-Abliterated-Heretic-GGUF` Q4_K_M; alt `Bahushruth/Qwen3.6-35B-A3B-abliterated-v4-GGUF`. Prefer an MTP build | Content limits come from the policy gate (§18), not the model |
| Coding CLI | **OpenCode**, pinned Windows binary, local-only lock (§15.9) | Not `npm -g` |
| TTS | **Qwen3-TTS** 1.7B (`-CustomVoice`, `-Base`, `-VoiceDesign`) + tokenizer; engine **`faster-qwen3-tts`**, with fallback to reference `qwen-tts` | |
| Captions | **faster-whisper** → ASS | |
| Stock footage | **Pexels** + **Pixabay** APIs | Commercial use with no required attribution; no reselling unmodified clips |
| Render | **FFmpeg** `h264_nvenc`, 1080×1920, 30 fps | |
| Python | **uv**, Python 3.12, venv per component | |
| Data | SQLite `data\studio.db` | |
| Installer | **Inno Setup** `Nightshift-Setup.exe` → `stage1.ps1` → `wizard.py` | Unsigned exe triggers SmartScreen; document "More info → Run anyway" |

---

## 3. Hardware profiles

| RAM | Code model | Strategy |
|---|---|---|
| ≥ 32 GB | APEX I-Compact / MTP Q4-class | `--fit`; tune `--n-cpu-moe` |
| 16–31 GB | APEX I-Mini | Fully on GPU |

`ns tune` sweeps `--n-cpu-moe`, ctx 16K/32K, KV f16/q8_0, and MTP `--spec-draft-n-max` 2/3/4, then writes the winner to `config\runtime.json`. It runs for both models.

---

## 4. Repo layout (exact — owned by the main session)

```
nightshift/
  manifest.json
  contracts/                  # main session only
    CONTRACTS.md  CHANGE_REQUESTS.md  schema.sql  errors.json
    schemas/*.schema.json
    policy/                   # §18 lists: banned_phrases.txt, unsafe_topics.yaml, misleading_claims.txt
  ns_common/                  # main session only
    paths.py config.py logging.py errors.py db.py llm_client.py tts_client.py supervisor_client.py models.py
  installer/                  # WP-A
    setup.iss stage1.ps1 build_installer.ps1 welcome.txt (A.2)
    stage2/wizard.py stage2/steps/*.py
  launchers/                  # WP-A
    nightshift-code.ps1 nightshift-studio.ps1 open-outbox.ps1 stop-all.ps1
  supervisor/                 # WP-B (ns_supervisor)
  opencode/                   # WP-B: opencode.json AGENTS.md
  tune/                       # WP-B: ns tune, ns verify
  services/tts_server/        # WP-C (ns_tts)
  studio/                     # ns_studio
    text/ prompts/ exemplars/ # WP-D
    media/ presets/           # WP-E
    policy/                   # WP-H: policy gate + cadence governor
    learn/                    # WP-H: analytics import + pool reweighting
    variation/ channels/ cli.py orchestrator.py   # WP-F
  tests/                      # WP-G
  README.md                   # main session: from §A
  pyproject.toml              # main session
```

---

## 5. Installer / wizard (WP-A)

**Stage 1 — `stage1.ps1`:** preflight (§1) → winget installs (Git, Windows Terminal, FFmpeg) → uv → Python 3.12 → copy payload to `%NIGHTSHIFT_HOME%\app` → run `wizard.py`.

**Stage 2 — `wizard.py`** (rich TUI; each step = `check()` / `run()` / `verify()`):
| Step | Does |
|---|---|
| `s00_welcome` | Shows A.2 text + the hardware check summary |
| `s01_questions` | Channel (name, niche, language) · voice (preview 4 presets / design / clone, with a rights confirmation for clones) · Pexels + Pixabay keys (validated live, with links to get them) · videos/day (default 1 for a new channel, §18.6) · run time (default 03:00) |
| `s02_download_bins` | llama.cpp + cudart, OpenCode |
| `s03_download_models` | GGUFs, Qwen3-TTS + tokenizer, faster-whisper (resumable, SHA256) |
| `s04_venvs` | `tts` (PyTorch cu128+), `studio`, `supervisor` |
| `s05_opencode_prewarm` | Run OpenCode once headless; confirm `local/code` |
| `s06_tune` | `ns tune` |
| `s07_configs` | runtime, OpenCode, channel profile, secrets |
| `s08_smoke` | Smoke tests (§12 Wave 3) |
| `s09_shortcuts_scheduler` | §15.8 |
| `s10_summary` | Status table, where things live, next steps (A.4) |

**Error UX:** code + plain message + fix hint from `errors.json` → `[R]etry · [S]kip (optional only) · [L]og · [Q]uit`. Resume state in `config\install_state.json`.

---

## 6. Runtime (WP-A + WP-B)

- **Supervisor** (§15.3) handles start/stop, health checks, crash restart, the **GPU lock**, and idle unload after 10 min.
- **Shortcuts** run launchers that acquire the needed service, wait for health, then open (§15.8).
- **Scheduled task** runs `studio run --daily`. If the GPU is busy with coding, it waits up to 2 h, then logs `NS-E201` and retries next cycle.

---

## 7. Studio pipeline — "master-first" (per channel, per job)

One job = **one story → one long-form master → 4–6 derived Shorts.** Write and voice the story once; render it twice (16:9 and 9:16).

**Why master-first:** mid-roll ads need videos of **8 minutes or longer** (shorter videos get only pre-roll and post-roll ads). Long-form also builds the **watch hours** that Shorts can't count toward. Shorts bring discovery and feed viewers to the master through YouTube's **"Related video"** link.

### 7.1 Master (long-form) track
| # | Stage | Owner | GPU tenant | Output |
|---|---|---|---|---|
| M1 | Ideas (N=8), dedupe vs history; pick one with **chapter potential** | D | `story` | `idea.json` |
| M2 | Plan: 5–7 chapters, each with its own mini-arc and a cliff into the next; ad-break-safe transitions | D | `story` | `plan.json` |
| M3 | Draft **per chapter** (fits context; ~250–350 words each) | D | `story` | `story.json` |
| M4 | Critique → revise per chapter, then a whole-story pass for continuity (§16.2) | D | `story` | `story.json` |
| M5 | Long-form SEO: title best-of-5 + judge, description, **chapter timestamps** (filled after voice), tags | D | `story` | `seo_long.json` |
| M6 | Visual plan (16:9 keywords + mood per sentence) | D | `story` | `visual_plan.json` |
| M7 | **Shorts selection**: pick 4–6 self-contained moments (35–90 s each, §18.5) that make sense without context; for each, write 5 standalone hooks + a closing line pointing to the full story | D | `story` | `shorts_plan.json` |
| M8 | Short SEO per selected moment (best-of-5 titles) | D | `story` | `seo_shorts.json` |
| M9 | **Policy gate — text** (§18.8) on story + all metadata | H | `story` | `policy_text.json` |
| M10 | Voice: full narration per chapter + each Short's new hook and closing line (one batch) | E | `tts` | `audio\` |
| M11 | Captions (word timestamps) for everything | E | `whisper` | `words\`, `captions\` |
| M12 | Clips: fetch **both orientations** per keyword (parallel with M10–M11) | E | none | `clips\` |
| M13 | Render master 1920×1080 + chapter title cards ≤ 1.5 s; write final chapter timestamps + **mid-roll suggestions** at chapter breaks | E | NVENC | `long\master.mp4`, `long\chapters.json` |
| M14 | Thumbnails ×3 (frame pick + bold title text via presets) | E | none | `long\thumb_[1-3].jpg` |

### 7.2 Shorts track (derived from the master)
| # | Stage | Owner | Output |
|---|---|---|---|
| S1 | Cut the selected narration segments from the master audio; prepend the new voiced hook; append the closing line | E | `shorts\s<k>\audio.wav` |
| S2 | **Fresh vertical edit**: vertical clips (not a crop of the master), own caption style and visual treatment from its combo | E | `shorts\s<k>\short.mp4` |
| S3 | QC gate (both formats) | E | `qc\` |
| S4 | **Policy gate — final** (§18.8) | H | `policy_final.json` |
| S5 | Outbox + upload sheets; each Short's sheet says *"Set Related video → <master title>"* | E | Outbox |

**Upload order the sheet recommends:** master first (so Shorts can link to it), then Shorts spread across the following days (§18.6). The "Related video" link is set in YouTube Studio; third-party tools can't set it through the API.

### 7.3 Other formats (same engine)
- `--format shorts-only`: the older flow (story → parts → Shorts), for niches that don't suit 10+ minutes.
- `--format series-compilation`: stitches an existing multi-part Shorts series into one long-form master.

The orchestrator (WP-F) batches by GPU tenant across all jobs of the run. The **cadence governor** (§18.6) decides how many masters run per channel.

---

## 8. Variation engine (WP-F)

YouTube's spam policy names this exact pattern as a violation example: channels using the same background music and repetitive AI imagery across many videos, each reading out an AI-generated script. Templates are therefore **composed from parts** (§17), never rotated whole.
- Combo per video: `{hook, structure, pov_tone, pacing, caption_style, visual_treatment, title_formula, length_bucket}`.
- Draws are weighted (weights updated by §18.9 learning), restricted to the channel's allowed pools.
- **Combo ledger:** no repeated full combo per channel within 30 days; no repeated `{hook, structure}` within the last 10 videos.
- **Cross-channel distinctness:** two channels on one install may not share the same niche, voice, caption style **and** visual treatment set. `studio channel add` enforces this.

---

## 9. SEO rules (WP-D, enforced in code)

- **Title:** ≤ 70 chars target, 100 max. Keyword in the first 40 chars. Includes "Part X" for series. Must match the content (§18.4).
- **Description:** 2–3 honest summary lines + "Part X of Y" + an "Original fiction" line (§18.4) + 3–5 hashtags incl. `#Shorts`.
- **Tags:** 10–20 relevant tags, ≤ 500 chars. No unrelated trending tags (metadata spam).

---

## 10. Publishing

- **Default:** review queue + manual upload in YouTube Studio, using the upload sheet. `studio review` shows each video with its policy-gate notes.
- **Phase 9 (optional) API upload:** unaudited API projects created after 28 Jul 2020 get uploads **locked to private** until they pass the compliance audit. The quota cost is documented as 1,600 units (verify in Cloud Console). OAuth per channel via `keyring`.

---

## 11. Channel profile example

`config\channels\after-dark.yaml`
```yaml
slug: after-dark
display_name: After Dark Stories
language: en
niche: "original short horror fiction set in everyday places"
seed_keywords: [horror story, scary story, creepy story, horror short]
voice_notes: "Plain, intimate first person. Short sentences. Dread over gore."
voice: { mode: preset, speaker: Ryan, speed: 1.0 }
format: master_first              # master_first | shorts_only | series_compilation
masters_per_week: 2               # governor adjusts (§18.6)
long_minutes: [10, 14]            # target runtime range; never below 8:30
shorts_per_master: [4, 6]
shorts_per_day_max: 2
length_buckets: [35, 50, 75]      # Shorts seconds; learning reweights (§18.5)
publish_window: "18:00-21:00"
audience: general                 # general | mature (sets age-gate advice)
made_for_kids: false
fiction_label: true
pools:
  hooks: [cold_open, question, confession]
  structures: [twist, reverse_reveal, slow_burn]
  caption_styles: [punch_yellow, clean_white]
  visual_treatments: [dark_slowzoom, handheld_grit]
avoid_topics: []
```

---

## 12. Orchestration plan (parallel one-shot)

### Wave 0 — main session
1. Resolve and verify all pins → `manifest.json`; decide MTP/APEX (§2).
2. Create the exact repo tree (§4) with stubs.
3. Write all contracts (§15) and policy lists (§18).
4. Write `ns_common` in full.
5. Write `pyproject.toml` (console scripts `ns`, `studio`) and `README.md` from §A.
6. Freeze (`contracts-v1`), then spawn Wave 1.

### Wave 1 — parallel workers (exclusive ownership)
| WP | Owns | Builds |
|---|---|---|
| **A** Installer | `installer/`, `launchers/` | Inno script, stage1, wizard + steps, launchers, shortcuts, task |
| **B** Runtime | `supervisor/`, `opencode/`, `tune/` | Supervisor, llama profiles, OpenCode config + AGENTS.md, `ns tune`, `ns verify` |
| **C** TTS | `services/tts_server/` | FastAPI service, engine + fallback, voice library |
| **D** Text | `studio/text/`, `prompts/`, `exemplars/` | Stages M1–M9 text work (plus shorts-only text stages), prompts, exemplars, validators |
| **E** Media | `studio/media/`, `presets/` | Stages M10–M14 + S1–S3, S5 (voice, captions, clips, renders, thumbnails, QC, outbox), presets, clip cache + license ledger |
| **F** Studio core | `variation/`, `channels/`, `cli.py`, `orchestrator.py` | Variation engine, channel CRUD + distinctness check, orchestrator, CLI, long-form |
| **G** Tests | `tests/` | Mocked unit + e2e tests per contract |
| **H** Policy & growth | `studio/policy/`, `studio/learn/` | Policy gate (§18.8), cadence governor (§18.6), analytics import + reweighting (§18.9) |

### Wave 2 — main session
Merge, resolve change requests, run mocked tests + lint + typecheck, build `Nightshift-Setup.exe` (Inno compiles on Windows only; otherwise ship `.iss` + payload).

### Wave 3 — on the target PC
Installer → `ns verify` → `logs\verify-report.json`: llama health + completion, OpenCode edits a file, TTS per voice mode, Whisper transcription, one full master job (long-form + Shorts) through both policy gates, benchmarks vs §14.6. Feed the report back for a fix pass if needed.

---

## 13. Open decisions (defaults until answered)

| Question | Default |
|---|---|
| System RAM | Detected |
| Visuals | Pexels + Pixabay |
| Niche & language | Per channel; English |
| Upload | Review queue + manual |
| Channels | 1 at install |
| Shorts length | Buckets 35 / 50 / 75 s, learned per channel |
| Format | Master-first: 2 long-form/week, 4–6 Shorts each |

---

## 14. Performance & quality mandate

### 14.1 LLM speed
- **MTP:** `--spec-type draft-mtp --spec-draft-n-max` from the sweep (one run measured 37.9 → 48.3 tok/s at n-max 3, while n-max 6 was slower).
- Tuned expert offload; KV `q8_0` when needed; fixed system prompts for prefix caching; JSON-schema-constrained output; `--alias code|story`.

### 14.2 TTS speed
`faster-qwen3-tts` (CUDA graphs; reported 2–6× speedups across 4 GPUs, up to 9.8× faster first audio on an RTX 4060). Batch the day's parts, bf16, SDPA.

### 14.3 Pipeline
Clip fetching overlaps GPU stages; one filter graph per video; NVENC; pre-trimmed proxies.

### 14.4 Lightness
One LLM runtime, no Ollama or Docker, supervisor-only background process, idle unload.

### 14.5 Quality
Plan → draft → critique → revise; best-of-5 + judge for hooks and titles; exemplars; thinking on for plan/critique/judge only; hard validators; OpenCode `AGENTS.md` rules. No cloud fallback by default.

### 14.6 Benchmark gates (`ns verify`)
| Metric | Target |
|---|---|
| `code` generation, 16K ctx | ≥ 40 tok/s |
| Cold model load | ≤ 20 s |
| TTS | faster than real time |
| One 50-second Short, idea → MP4 (shorts-only) | ≤ 3 min |
| One master job: 12-min long-form + 5 Shorts + thumbnails | ≤ 60 min |
| Nightly run, 2 channels × 1 master job | ≤ 2 h |
| Idle VRAM | ~0 GB |

---

## 15. Contracts registry (frozen in Wave 0)

### 15.1 Names
| Thing | Value |
|---|---|
| Product | `Nightshift` |
| Home env var | `NIGHTSHIFT_HOME` (default `%LOCALAPPDATA%\Nightshift`) |
| Python packages | `ns_common`, `ns_supervisor`, `ns_tts`, `ns_studio` |
| Console scripts | `ns` (tune, verify, doctor, stop), `studio` |
| Logical models | `code`, `story` |
| Services | `llama-code`, `llama-story`, `tts`, `whisper` |
| Keyring | service `Nightshift`; keys `pexels_api_key`, `pixabay_api_key`, `yt_oauth_<slug>` |
| Installer | `Nightshift-Setup.exe` |

### 15.2 Paths (under `NIGHTSHIFT_HOME`)
```
app\ bin\llama\ bin\opencode\
models\llm\ models\tts\ models\tts\voices\ models\whisper\
venvs\tts\ venvs\studio\ venvs\supervisor\
config\runtime.json config\install_state.json config\opencode.json config\channels\<slug>.yaml
data\studio.db data\jobs\<job_id>\ cache\clips\ imports\analytics\
outbox\<slug>\<YYYY-MM-DD>\<job_id>\long\  outbox\<slug>\<YYYY-MM-DD>\<job_id>\shorts\
rejected\<slug>\<YYYY-MM-DD>\ logs\
```
Coding workspace: `%USERPROFILE%\Projects`.

### 15.3 Ports & supervisor API (127.0.0.1)
llama **8080** · TTS **8091** · supervisor **8099**.
| Method | Path | Body → Response |
|---|---|---|
| GET | `/status` | → `{gpu_owner, services:{<name>:{state,pid,port,vram_mb}}}` |
| POST | `/acquire` | `{service, owner, wait:true, timeout_s:7200}` → `{endpoint, model_alias}` |
| POST | `/release` | `{service, owner}` → `{ok}` |
| POST | `/stop_all` | → `{ok}` |

States: `stopped|starting|ready|stopping|error`. Owners: `opencode`, `studio:<job_id>`, `verify`.

### 15.4 TTS API (port 8091)
| Method | Path | Body |
|---|---|---|
| POST | `/v1/audio/speech` | `{model:"qwen3-tts", input, voice, speed, response_format:"wav"}` → WAV |
| POST | `/v1/audio/speech/batch` | `{items:[{id,input,voice,speed}], out_dir}` → `{files:[{id,path,seconds}]}` |
| GET | `/v1/voices` | → `[{name, mode, source}]` |
| POST | `/v1/voices/design` | `{name, description}` |
| POST | `/v1/voices/clone` | `{name, ref_audio_path, ref_text, rights_confirmed:true}` |
| GET | `/health` | → `{ok, engine}` |

### 15.5 JSON schemas (`contracts/schemas/`)
`channel, runtime, install_state, idea, plan, story, parts, seo, seo_long, shorts_plan, chapters, visual_plan, qc, policy, metadata, combo, analytics_row`.
- `plan`: `{chapters:[{n, title, beats[], cliff}]}`
- `chapters`: `[{n, title, start_s, end_s}]`, `midroll_suggestions_s: [..]` (at chapter breaks, none in the first or last 2 min)
- `shorts_plan`: `[{k, chapter, start_s, end_s, hooks[5], chosen_hook, closing_line, length_bucket, combo_id}]`
- `parts`: `[{n, text, hook, word_count, est_seconds, cliffhanger}]`
- `seo`: `{title, description, tags[], hashtags[], keywords[]}`
- `visual_plan`: `[{part, sentence_idx, text, keywords[3], mood}]`
- `qc`: `{part, passed, checks:{duration, loudness_lufs, caption_drift_ms, black_frames, opens}, reason?}`
- `policy`: `{stage:"text"|"final", passed, checks:{<check>:{passed, detail}}, advice:{ai_disclosure, made_for_kids, age_restrict_suggested}}`
- `metadata`: `{channel, job_id, kind:"long"|"short", k?, title, description, tags, hashtags, publish_after, chapters?, midroll_suggestions_s?, thumbnails?, related_video_instruction?, parent_job_id?, ai_disclosure, made_for_kids, clips:[{source,id,url,license}], combo_id}`

### 15.6 SQLite (`schema.sql`)
`ideas, stories, parts, clips, clip_usage, combos, outputs, runs`, plus `uploads(output_id, video_url, published_at)` and `analytics(output_id, date, views, viewed_pct, avg_pct_viewed, likes, comments, shares, subs_gained)`.

### 15.7 Stage interface & job directory
```python
TENANT: Literal["story","tts","whisper",None]
def run(ctx: JobContext) -> None
```
Job dir `data\jobs\<job_id>\`: `idea.json plan.json story.json seo_long.json seo_shorts.json visual_plan.json shorts_plan.json policy_text.json policy_final.json audio\ words\ captions\ clips\h\ clips\v\ long\master.mp4 long\chapters.json long\thumb_[1-3].jpg shorts\s<k>\{audio.wav,short.mp4} qc\`.
Outbox `outbox\<slug>\<YYYY-MM-DD>\<job_id>\`: `long\master.mp4 + .json + .txt + thumb_*.jpg` and `shorts\s<k>.mp4 + .json + .txt`. `job_id`: `<slug>-<YYYYMMDD>-<4hex>`.

### 15.8 CLI, launchers, shortcuts, task
- `studio run [--daily] [--channel] [--count] [--format master_first|shorts_only|series_compilation] [--dry-run]` · `studio review` · `studio status`
- `studio channel add|list|edit` · `studio voice list|preview|design|clone`
- `studio learn --import <csv>` · `studio mark-uploaded <job_id> <long|s<k>> <url>`
- `ns tune` · `ns verify` · `ns doctor` · `ns stop`

| Shortcut | Launcher |
|---|---|
| `Nightshift Code.lnk` | `nightshift-code.ps1`: acquire `llama-code`, set `OPENCODE_CONFIG`, open OpenCode in Projects; release on exit |
| `Nightshift Studio.lnk` | `nightshift-studio.ps1`: `studio status` + interactive prompt |
| `Nightshift Outbox.lnk` | `open-outbox.ps1` |
| `Nightshift - Stop All.lnk` | `stop-all.ps1` → `ns stop` |

Scheduled task: `Nightshift Daily` → `studio run --daily`.

### 15.9 OpenCode local-only lock (`config\opencode.json` via `OPENCODE_CONFIG`)
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

### 15.10 Logging
JSON lines `logs\<component>-YYYYMMDD.jsonl`: `ts, level, component, event, msg, data`.

### 15.11 Error codes
`NS-E1xx` install · `NS-E2xx` runtime/GPU lock · `NS-E3xx` text/LLM · `NS-E4xx` media/QC · `NS-E5xx` stock APIs/network · `NS-E6xx` policy gate.

---

## 16. Prompts, rubric, validators (WP-D)

### 16.1 Prompt files (Jinja2; receive `channel`, `combo`, `exemplars`, `policy_rules`)
| File | Purpose | Thinking |
|---|---|---|
| `ideas.md` | 8 original premises + twist; no real people, no real crimes, no retellings of existing works or posts | off |
| `story_plan.md` | Beat sheet: setup, escalation, turn, payoff, part breaks | on |
| `story_draft.md` | Full story in `voice_notes` style, spoken rhythm | off |
| `critic.md` | Rubric scores (§16.2) + concrete fixes, JSON | on |
| `revise.md` | Apply fixes only | off |
| `split.md` | Parts per target length; cliffhangers except the last; last line of a single-part story loops into its first line | off |
| `hook.md` | 5 hooks per part in the combo style; **must honestly reflect the content** | off |
| `judge.md` | Best of N, index + reason | on |
| `seo.md` | Title/description/tags/hashtags per §9 and §18.4 | off |
| `visual_plan.md` | 3 literal, filmable stock keywords + mood per sentence | off |
| `policy_check.md` | Classify against `unsafe_topics.yaml` (§18.3), JSON | on |
| `long_plan.md` | 5–7 chapters with mini-arcs, cliffs between chapters, natural ad-break transitions | on |
| `chapter_draft.md` | One chapter at a time, given the plan + previous chapter summary | off |
| `continuity.md` | Whole-story pass: names, timeline, facts consistent; returns fixes | on |
| `long_seo.md` | Long-form title/description/tags; chapter titles; must match content | off |
| `shorts_select.md` | Pick 4–6 moments that stand alone without context; return timestamps + why | on |
| `short_hook.md` | 5 standalone hooks + a closing line ("The full story is on the channel" style, varied) per moment | off |

### 16.2 Critic rubric (1–10 each)
Hook · Clarity (understood on one listen) · Pacing (no filler) · Payoff (earned + surprising) · Cliffhangers · Originality (no stock phrases, unlike the exemplars) · Voice fit.
**Pass:** average ≥ 8 and no score < 6. Max 2 revisions; otherwise discard and draw a new idea.

### 16.3 Hard validators (code)
Words per part fit the length bucket at the voice's speed · banned phrases · no opening line repeated in the channel's last 50 videos (similarity < 0.8) · SEO limits · reading level grade 5–8 (configurable).

### 16.4 Exemplars
3 original exemplars per default niche (horror, mystery, revenge, wholesome twist), written by WP-D, in `studio/exemplars/<niche>/`.

---

## 17. Variation presets

| Pool | Options |
|---|---|
| Hooks | `cold_open`, `question`, `confession`, `shocking_fact`, `in_medias_res`, `warning` |
| Structures | `twist`, `reverse_reveal`, `slow_burn`, `unreliable_narrator`, `countdown`, `two_timelines` |
| POV/tone | `first_person_intimate`, `second_person`, `narrator_calm`, `narrator_urgent` |
| Pacing | `brisk` 175 wpm, `standard` 155, `measured` 140 |
| Title formulas | `question`, `statement_twist`, `i_never`, `number_hook`, `warning` |
| Length buckets | 20–35 s, 35–60 s, 60–90 s (§18.5) |

**Caption styles** (`caption_styles.yaml`: font, size, colours, outline, y%, words-on-screen 2–4, animation `pop|none|karaoke`): `punch_yellow`, `clean_white`, `boxed_dark`, `minimal_lower`.
**Visual treatments** (`visual_treatments.yaml`: zoom, cut length, colour filter, vignette, grain): `dark_slowzoom`, `handheld_grit`, `clean_bright`, `cinematic_teal`.
**Clip rules:** vertical ≥ 1080 px tall preferred; landscape → centre crop over a blurred fill. Record source/id/url/license. No clip reused on a channel within 60 days, and never across channels within 30 days.

---

## 18. YouTube safety & growth playbook (enforced by WP-H)

### 18.1 The money math (plan for it honestly)
- Full ad revenue from Shorts currently needs **1,000 subscribers + 10M qualified Shorts views in 90 days**. A 500-subscriber tier with **3M views in 90 days** unlocks fan funding and shopping.
- **From 1 Feb 2027:** new applicants need **20M Shorts views or 8,000 watch hours**. Existing partners must keep **10M Shorts views per rolling 90 days** to earn from the Shorts pool, and **Shorts watch hours don't count** toward the long-form hours path.
- Shorts pay far less per view than long-form: one comparison puts 1M Shorts views at about **$30** vs **$2,000** for a long-form video at a $2 RPM.
- **Mid-roll ads need videos of 8 minutes or longer.** Under 8 minutes, only pre-roll and post-roll ads run.
- **Strategy built into Nightshift (master-first, §7):** every story becomes a **10–14 min long-form master** (mid-roll eligible, builds watch hours toward the 4,000/8,000-hour path) plus **4–6 Shorts** (discovery, subscribers, Shorts-views path). Each Short links to its master via "Related video". **Both monetization paths grow from the same story.**

### 18.2 Policies Nightshift is designed around
| Policy | What it says | Nightshift's countermeasure |
|---|---|---|
| **Inauthentic content** (monetization) | No pay for generic, repetitive or template-based content, plus 2 other categories | Variation engine (§8), critic loop, combo ledger, human review |
| **Spam & deceptive practices** (Community Guidelines) | Bans posting the same content repeatedly across one or more channels, and mass-uploading content scraped from other creators | Never upload the same render twice anywhere; original stories only; per-channel distinctness; similarity checks |
| **Originality ranking** (Oct 2026) | Original Shorts get more visibility; reposts are limited | Original writing, no reuploads, no compilations of others' clips |
| **Altered/synthetic disclosure** | Required when AI content could be mistaken for real footage of a real person, place or event | Disclosure decision logic (§18.7) |

### 18.3 Content safety rules (`contracts/policy/unsafe_topics.yaml`)
Blocked from ideas and stories, and re-checked at both gates:
- Real, named people (living or dead), real brands as villains, real crimes or tragedies presented as fact.
- Minors in any sexual, violent or endangering context.
- Graphic gore, self-harm/suicide methods, instructions for weapons or drugs, hate or harassment of protected groups.
- Medical, legal, financial or political advice (AI personas on sensitive topics are demonetized).
- Content imitating news reports or emergency alerts.

Horror and dark themes are allowed **as fiction** with implied, not graphic, violence. The `mature` channel audience adds an "age-restriction suggested" note on the upload sheet when the classifier flags intensity.

### 18.4 Honest metadata rules
- **No "true story", "real", "happened to me" claims** in titles, descriptions or tags for fiction. Every description carries "Original fiction." when `fiction_label: true`.
- The hook and title must match what actually happens in the video (judged by `policy_check.md`). No bait: hooks that overpromise drive viewers away and hurt more than an honest, less flashy hook.
- Tags must be relevant. No unrelated trending tags or competitor channel names.

### 18.5 Retention design (what makes Shorts get pushed)
- The main distribution gate is the **viewed vs. swiped-away** ratio. Commonly cited targets: **≥ 70% viewed**; **below 50%, reach stops.**
- The first 1–2 seconds decide it, so the **hook line is spoken in the first 1.5 s** and the first caption appears at 0.0 s. No title cards, no slow fade-ins.
- **Length:** Shorts can run up to 3 min, but shorter ones keep a higher share of viewers. One study of 5,400 Shorts found 50–60 s clips earned the most total views. So Nightshift **tests length buckets** (20–35 s, 35–60 s, 60–90 s) and lets the learning loop pick per channel.
- **Loops:** single-part stories end with a line that flows back into the opening line.
- **Series:** multi-part stories end on cliffhangers. The description says "Part X of Y".
- **Pacing:** a visual change every 1.5–3 s; captions 2–4 words with the active word highlighted.
- **Derived Shorts must stand alone:** lazy repurposing of long-form usually fails; a moment often needs a new first line and tighter trimming. Hence the freshly written, voiced hook (M7/S1) and a fresh vertical edit (S2), not a crop of the master.

**Long-form retention rules:**
- The cold open is the story's strongest hook (no "welcome back", no channel intro); the title promise is paid off in the first 30 s.
- Chapter cliffs every 1.5–2.5 min; chapter cards ≤ 1.5 s.
- Mid-roll suggestions only at chapter breaks, never in the first or last 2 minutes.
- Runtime never below **8:30**, giving a safety buffer over the 8:00 mid-roll line.
- Thumbnails: 3 variants (high-contrast frame + ≤ 4-word text); the reviewer picks one. They must reflect the actual story (§18.4).

### 18.6 Cadence governor (per channel)
| Channel stage | Rule |
|---|---|
| Weeks 1–2 (new) | 1 master/week; its Shorts released 1/day |
| Weeks 3–6 | 2 masters/week; up to 2 Shorts/day if the last 14 days' average viewed rate ≥ 60% (from imported analytics) |
| Any time | Never more than 3 Shorts/day or 3 masters/week; publish times jittered ±45 min |
| Release order | Master first; its Shorts over the next 2–4 days, interleaved with other masters' Shorts so consecutive Shorts never come from the same story |
| Quality drop | If the 7-day viewed rate falls under 50%, drop to 1 Short/day and tighten the critic pass bar |
| Multi-channel | Max 3 channels per install by default; each must pass the distinctness check |

### 18.7 Disclosure & settings logic (written onto each upload sheet)
- **Altered/synthetic content:** `No` by default. AI voiceover counts as production assistance and doesn't need the label; stock footage is real footage, used as illustration. Flip to **`Yes`** automatically if anything could be mistaken for real footage of a real person, place or event (e.g., the story is framed as a real event, or uses a real place name). When in doubt, the sheet says `Yes`: over-disclosing is the safer side.
- **Made for kids:** `No` unless the channel profile says otherwise.
- **Age restriction:** suggestion only, per §18.3.

### 18.8 Policy gate checks (text gate = M9, final gate = S4)
| Check | Gate | Fails when |
|---|---|---|
| `unsafe_topics` | text | LLM classifier flags any §18.3 item |
| `real_entities` | text | Named-entity scan finds real people, brands, or real crime/tragedy references |
| `misleading_claims` | text | Any `misleading_claims.txt` phrase ("true story", "real footage", "caught on camera"…) |
| `hook_title_honesty` | text | Judge says the hook or title promises something the story doesn't deliver |
| `self_similarity` | text | Embedding similarity > 0.85 to any of the channel's past stories, or > 0.80 to another channel's |
| `metadata_spam` | text | Tag count/length limits exceeded, or irrelevant tags |
| `render_uniqueness` | final | Perceptual hash too close to any prior render on any channel. **Exception:** a Short may share narration with its own parent master (expected for derived Shorts) but must differ in visuals (vertical edit) and include its new hook |
| `long_runtime` | final | Master shorter than 8:30, or a mid-roll suggestion falls in the first/last 2 min |
| `standalone_short` | text | Judge says a selected Short moment doesn't make sense without the master |
| `clip_reuse` | final | Violates §17 clip rules |
| `audio_only_voice` | final | Any non-voice audio track present (no music by design) |
| `disclosure_advice` | final | Always runs; writes the §18.7 answers |

A failure moves the item to `rejected\` with the reason, regenerates once, and logs `NS-E6xx`.

### 18.9 Learning loop (gets better over time, stays local)
1. Export Shorts analytics from YouTube Studio as CSV (Content → Shorts, with: views, viewed vs swiped away, average % viewed, likes, comments, shares, subscribers).
2. `studio learn --import <csv>` matches rows to outputs (via the recorded video URLs or title match).
3. Per-channel reweighting of hooks, structures, length buckets, caption styles, visual treatments and title formulas, by **viewed % × average % viewed**. Bounded: no option's weight goes below 5%, so variety never collapses into a template.
4. `studio status` shows the top and bottom performers and what changed.

### 18.10 Human-in-the-loop (required for safety and originality)
- Every video passes `studio review` before upload. The reviewer sees the story, policy notes and disclosure advice.
- The channel's `voice_notes`, niche and exemplars are written by the owner. That human editorial direction is the channel's point of view.
- Optional per-series "creator note" line in the description, written by the owner.

---

## Sources (verified 1 Nov 2026)

- Qwen3.6-35B-A3B vs Gemma 4: https://artificialanalysis.ai/models/releases/comparisons/qwen3-6-35b-a3b-vs-gemma-4-26b-a4b
- APEX quants: https://huggingface.co/mudler/Qwen3.6-35B-A3B-APEX-GGUF
- Abliterated GGUF: https://huggingface.co/Youssofal/Qwen3.6-35B-A3B-Abliterated-Heretic-GGUF
- Qwen3-TTS models: https://github.com/Usimian/Qwen3-TTS
- faster-qwen3-tts: https://www.sourcepulse.org/projects/25451367
- llama.cpp MoE flags: https://www.developersdigest.tech/blog/llama-cpp-moe-offload-n-cpu-moe-vs-moe-cache-2026
- MTP in llama.cpp: https://mer.vin/2026/05/run-qwen-3-6-mtp-in-llama-cpp-faster-local-inference-with-built-in-speculative-decoding/
- MTP benchmarks: https://github.com/schmared/tesla-p40-wsl2-cuda-llamacpp
- MTP build caveat: https://levelup.gitconnected.com/mtp-is-now-in-lm-studio-how-to-set-up-mtp-by-lm-studio-a5db39e52ac9
- PyTorch on RTX 50-series: https://discuss.pytorch.org/t/pytorch-support-for-sm-120-nvidia-geforce-rtx-5060/220941
- OpenCode Windows npm issue: https://github.com/anomalyco/opencode/issues/2447
- OpenCode config reference: https://learnopencode.com/en/appendix/config-ref
- OpenCode config locations: https://learnopencode.com/en/5-advanced/01a-config-basics
- Pexels license: https://help.pexels.com/hc/en-us/articles/360042295174
- Pixabay license: https://ras.ac.uk/licence/pixabay-license
- YouTube upload API: https://developers.google.com/youtube/v3/docs/videos/insert
- YouTube spam & deceptive practices policy: https://support.google.com/youtube/answer/2801973
- Inauthentic content clarification (Jul 2026): https://techcrunch.com/2026/07/20/youtube-clarifies-policies-around-ai-slop-and-upsetting-videos/
- Oct 2026 originality update: https://async.com/blog/youtube-monetization-update/
- YPP thresholds + 2027 changes: https://air.io/en/youtube-glossary/what-is-the-youtube-partner-program-ypp-requirements-benefits
- YPP tiers: https://vidiq.com/pt/blog/post/youtube-partner-program-guide
- Shorts vs long-form earnings: https://vidiq.com/pt/blog/post/youtube-shorts-monetization
- AI disclosure rules: https://air.io/en/youtube-hacks/youtube-ai-disclosure-in-2026-label-it-yourself-or-youtube-will
- AI voiceover = production assistance: https://shortsfast.com/blog/youtube-ai-content-disclosure-rules-2026/
- Shorts algorithm (viewed vs swiped): https://ytgrowth.io/blog/youtube-shorts-algorithm
- Shorts retention + honest hooks: https://www.go-viral.app/blog/youtube-shorts-algorithm-2026/
- Shorts length (3 min max, 50–60 s study): https://piktochart.com/blog/how-long-youtube-shorts.md
- Mid-roll 8-minute minimum: https://fluxnote.io/guides/youtube-mid-roll-ads-minimum-video-length-8-minutes-2026
- Mid-roll placement (avoid first/last 2 min): https://fluxnote.io/guides/youtube-mid-roll-ads-eligibility-2026
- Shorts "Related video" link: https://www.searchenginejournal.com/youtube-adds-shorts-links-limits-links-elsewhere/495463
- Related video not settable via third-party API: https://help.vizard.ai/en/articles/13643189-can-i-add-a-link-to-my-long-form-youtube-video-when-publishing-shorts-via-vizard
- Repurposing long-form into Shorts needs new first lines: https://quso.ai/blog/youtube-shorts-algorithm
